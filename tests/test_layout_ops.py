"""
Tests for layout reasoning. No phone required.

Run directly:   python tests/test_layout_ops.py
Or with pytest: pytest

Every rule here exists because breaking it would damage a real Home Screen,
so treat a failure as a release blocker rather than a flaky test.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import layout_ops as L  # noqa: E402


def an_app(bundle, name):
    return {"bundleIdentifier": bundle, "displayName": name, "iconType": "app"}


def a_widget(ident, size):
    return {"widgetIdentifier": ident, "gridSize": size, "iconType": "custom",
            "displayName": ident}


def a_folder(name, items):
    return {"listType": "folder", "displayName": name, "iconLists": [items]}


def sample():
    return [
        [an_app("com.apple.mobilephone", "Phone"), an_app("com.apple.MobileSMS", "Messages")],
        [an_app("com.apple.mobilesafari", "Safari"),
         a_widget("com.apple.weather", "medium"),
         a_folder("Work", [an_app("com.slack", "Slack"), an_app("com.zoom", "Zoom")])],
        [an_app("com.spotify", "Spotify"), an_app("com.apple.stocks", "Stocks")],
    ]


def blocked(layout, **kwargs):
    """Return the refusal message, or fail if the layout was allowed."""
    try:
        L.validate(layout, **kwargs)
    except L.LayoutError as exc:
        return str(exc)
    raise AssertionError("expected this layout to be refused")


def test_stats():
    s = L.stats(sample())
    assert s == {"pages": 2, "dockItems": 2, "apps": 7, "folders": 1, "widgets": 1}, s


def test_clean_layout_passes():
    assert L.validate(sample()) == []


def test_empty_layout_refused():
    assert "empty" in blocked([]).lower()


def test_dock_limit():
    bad = sample()
    bad[0] += [an_app("a", "A"), an_app("b", "B"), an_app("c", "C")]
    assert "Dock holds at most 4" in blocked(bad)


def test_page_capacity_counts_widget_footprints():
    # 5 medium widgets = 40 cells on a 24-cell page.
    bad = [sample()[0], [a_widget(f"w{i}", "medium") for i in range(5)]]
    assert "over capacity" in blocked(bad)


def test_nested_folders_refused():
    bad = copy.deepcopy(sample())
    bad[1][2]["iconLists"][0].append(a_folder("Inner", []))
    assert "nested folders" in blocked(bad)


def test_widget_in_folder_refused():
    bad = copy.deepcopy(sample())
    bad[1][2]["iconLists"][0].append(a_widget("com.apple.weather", "small"))
    assert "Widgets cannot live inside folders" in blocked(bad)


def test_folder_page_limit():
    bad = copy.deepcopy(sample())
    bad[1][2]["iconLists"] = [[an_app(f"x{i}", f"X{i}") for i in range(10)]]
    assert "limit is 9" in blocked(bad)


def test_duplicate_app_refused():
    bad = copy.deepcopy(sample())
    bad[2].append(an_app("com.spotify", "Spotify"))
    assert "more than one position" in blocked(bad)


def test_same_bundle_different_objects_is_not_a_duplicate():
    """
    Regression: a real phone had Waze twice - once in a folder with
    displayIdentifier "com.waze.iphone", once on a page with a UUID
    displayIdentifier. They are different SpringBoard objects. Keying
    identity on bundleIdentifier collapsed them and blocked every write.
    """
    layout = [
        [an_app("com.apple.mobilephone", "Phone")],
        [{"displayIdentifier": "3BA61DCD-C30F-4B8F-A0AE-7BE8BE07B72E",
          "iconType": "app", "iconLists": [],
          "bundleIdentifier": "com.waze.iphone"}],
        [a_folder("Travel", [{"displayIdentifier": "com.waze.iphone",
                              "displayName": "Waze",
                              "bundleIdentifier": "com.waze.iphone"}])],
    ]
    assert L.validate(layout) == []
    assert L.validate(copy.deepcopy(layout), baseline=layout) == []


def test_preexisting_duplicate_is_not_blocked_but_new_one_is():
    base = sample()
    base[2].append(an_app("com.spotify", "Spotify"))   # phone reported it twice

    # Re-applying what the phone gave us must not be refused.
    assert L.validate(copy.deepcopy(base), baseline=base) == []

    # Adding a third copy is our fault and must be.
    worse = copy.deepcopy(base)
    worse[1].append(an_app("com.spotify", "Spotify"))
    assert "more than one position" in blocked(worse, baseline=base)


def test_silent_loss_refused_but_declared_removal_allowed():
    base = sample()
    edited = copy.deepcopy(base)
    edited[2].pop()  # drop Stocks

    assert "silently drop" in blocked(edited, baseline=base)
    assert L.validate(edited, baseline=base,
                      allowed_removals={"app:com.apple.stocks"}) == []


def test_change_description_is_specific():
    base = sample()
    after = copy.deepcopy(base)
    moved = after[2].pop(0)
    after[1].append(moved)
    after[2].pop(0)

    changes = L.describe_changes(base, after)
    assert "Moved Spotify from page 2 to page 1." in changes
    assert any("Removed Stocks" in c for c in changes)
    assert not any("structure changed" in c for c in changes)


def test_folder_rename_reads_as_one_change():
    base = sample()
    after = copy.deepcopy(base)
    after[1][2]["displayName"] = "Focus"

    changes = L.describe_changes(base, after)
    assert changes == ['Renamed folder "Work" to "Focus".'], changes


def test_merging_same_named_folders_is_reported():
    """A real phone had two folders called Food. Merging them must show up."""
    base = sample()
    base[2].append(a_folder("Work", [an_app("com.teams", "Teams")]))
    after = copy.deepcopy(base)
    extra = after[2].pop()
    after[1][2]["iconLists"][0].extend(extra["iconLists"][0])

    changes = L.describe_changes(base, after)
    assert 'Removed 1 of the 2 folders named "Work".' in changes, changes


def test_folder_creation_described():
    base = sample()
    after = copy.deepcopy(base)
    a = after[2].pop(0)
    b = after[2].pop(0)
    after[2].append(a_folder("Media", [a, b]))

    changes = L.describe_changes(base, after)
    assert any('Created folder "Media" with 2 app(s).' in c for c in changes)


def test_added_apps_are_not_treated_as_a_failed_write():
    """
    Regression: a real write came back with missingCount 0 and 49 unexpected
    apps - iOS had put App-Library apps back on the Home Screen. The verifier
    called that a mismatch and rolled back a write that had succeeded.
    A loss is dangerous; a gain is untidy.
    """
    base = sample()
    augmented = copy.deepcopy(base)
    augmented[2].append(an_app("com.apple.podcasts", "Podcasts"))

    verdict = L.verify_write(base, augmented)
    assert verdict["severity"] == "augmented"
    assert verdict["ok"] is True, "an added app must not trigger a rollback"
    assert verdict["missingCount"] == 0
    assert verdict["unexpectedCount"] == 1


def test_loss_still_triggers_rollback_even_alongside_additions():
    base = sample()
    mixed = copy.deepcopy(base)
    mixed[2].pop()
    mixed[2].append(an_app("com.apple.podcasts", "Podcasts"))

    verdict = L.verify_write(base, mixed)
    assert verdict["severity"] == "mismatch"
    assert verdict["ok"] is False


def test_verify_write_distinguishes_reflow_from_loss():
    base = sample()

    assert L.verify_write(base, base)["severity"] == "exact"

    reflowed = copy.deepcopy(base)
    reflowed[2].reverse()
    assert L.verify_write(base, reflowed)["severity"] == "reflowed"

    lost = copy.deepcopy(base)
    lost[2].pop()
    verdict = L.verify_write(base, lost)
    assert verdict["severity"] == "mismatch"
    assert verdict["ok"] is False
    assert verdict["missingCount"] == 1


def test_identity_is_stable_across_copies():
    item = an_app("com.spotify", "Spotify")
    assert L.identity(item) == L.identity(copy.deepcopy(item))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
