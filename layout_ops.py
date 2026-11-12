"""
Organize My Phone - layout reasoning.

Pure functions over SpringBoard icon-state structures. No device access, no
HTTP, no globals - which means all of this is unit-testable without a phone.

The icon state is a list of lists:

    [
      [dock item, ...],
      [page 1 item, ...],
      [page 2 item, ...],
    ]

An item is one of:
  * an app      - has a bundle identifier
  * a folder    - listType == "folder", holds iconLists (a list of pages)
  * a widget    - iconType "custom" / elementType "widget" / widgetIdentifier

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from config import (
    MAX_DOCK_ITEMS,
    MAX_FOLDER_PAGE_ITEMS,
    MAX_FOLDER_PAGES,
    MAX_PAGE_SLOTS,
    MAX_PAGES,
)


class LayoutError(ValueError):
    """A layout that must not be written to a phone."""


# ---------------------------------------------------------------- classifiers


def is_folder(item: Any) -> bool:
    return isinstance(item, dict) and item.get("listType") == "folder"


def is_widget(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return bool(
        item.get("iconType") == "custom"
        or item.get("elementType") == "widget"
        or item.get("widgetIdentifier")
        or isinstance(item.get("elements"), list)
    )


def is_app(item: Any) -> bool:
    return isinstance(item, dict) and not is_folder(item) and not is_widget(item)


def object_key(item: Any) -> str | None:
    """
    SpringBoard's own unique key for an object.

    displayIdentifier is the field SpringBoard uses to tell two objects apart.
    For an ordinary app it equals the bundle identifier. For widgets, widget
    stacks and certain Home Screen placements it is a UUID, and two objects can
    legitimately share a bundleIdentifier while being different objects. Keying
    on bundleIdentifier collapses those into one and makes a perfectly valid
    layout look like it contains a duplicate.
    """
    if not isinstance(item, dict):
        return None
    return item.get("displayIdentifier") or item.get("bundleIdentifier") or None


def bundle_id(item: Any) -> str | None:
    """The bundle to fetch an icon for - not an identity."""
    if not isinstance(item, dict):
        return None
    return (
        item.get("bundleIdentifier")
        or item.get("displayIdentifier")
        or item.get("containerBundleIdentifier")
        or None
    )


def display_name(item: Any) -> str:
    if not isinstance(item, dict):
        return "Unknown"
    name = item.get("displayName")
    if name:
        return str(name)
    ident = bundle_id(item) or item.get("widgetIdentifier") or ""
    tail = str(ident).split(".")[-1]
    return tail or "Unknown"


def widget_footprint(item: Any) -> tuple[int, int]:
    """(columns, rows) for a widget, matching the editor's size mapping."""
    size = str((item or {}).get("gridSize") or "").lower()
    if "extra" in size or "xlarge" in size:
        return 4, 6
    if "large" in size:
        return 4, 4
    if "medium" in size:
        return 4, 2
    return 2, 2


def slot_cost(item: Any) -> int:
    """How many of a page's 24 grid cells this item consumes."""
    if is_widget(item):
        cols, rows = widget_footprint(item)
        return cols * rows
    return 1


# ------------------------------------------------------------------- identity


def identity(item: Any) -> str:
    """
    A stable key for one on-screen object.

    Folders are keyed by name because SpringBoard folder objects carry no
    durable id of their own, and a rename is a change the user asked for.
    Everything else is keyed by displayIdentifier - see object_key().
    """
    if is_folder(item):
        return f"folder:{display_name(item).casefold()}"
    if is_widget(item):
        key = object_key(item) or (item or {}).get("widgetIdentifier") or "unknown"
        return f"widget:{key}"
    return f"app:{object_key(item) or display_name(item).casefold()}"


def iter_leaves(layout: list[Any]) -> Iterable[Any]:
    """Every app and widget, including those nested inside folders."""
    for page in layout:
        if not isinstance(page, list):
            continue
        for item in page:
            if is_folder(item):
                for folder_page in item.get("iconLists") or []:
                    if isinstance(folder_page, list):
                        for child in folder_page:
                            if isinstance(child, dict):
                                yield child
            elif isinstance(item, dict):
                yield item


def fingerprint(layout: list[Any]) -> Counter:
    """Multiset of every leaf object. Used to detect loss or duplication."""
    return Counter(identity(item) for item in iter_leaves(layout))


def page_label(index: int) -> str:
    return "the Dock" if index == 0 else f"page {index}"


def locations(layout: list[Any]) -> dict[str, str]:
    """identity -> human-readable location. First occurrence wins."""
    found: dict[str, str] = {}
    for page_index, page in enumerate(layout):
        if not isinstance(page, list):
            continue
        for item in page:
            if not isinstance(item, dict):
                continue
            if is_folder(item):
                folder = display_name(item)
                found.setdefault(identity(item), page_label(page_index))
                for folder_page in item.get("iconLists") or []:
                    if not isinstance(folder_page, list):
                        continue
                    for child in folder_page:
                        if isinstance(child, dict):
                            found.setdefault(identity(child), f'the "{folder}" folder')
            else:
                found.setdefault(identity(item), page_label(page_index))
    return found


def stats(layout: list[Any]) -> dict[str, int]:
    apps = folders = widgets = 0
    for page in layout:
        if not isinstance(page, list):
            continue
        for item in page:
            if is_folder(item):
                folders += 1
                for folder_page in item.get("iconLists") or []:
                    if isinstance(folder_page, list):
                        for child in folder_page:
                            if is_widget(child):
                                widgets += 1
                            elif isinstance(child, dict):
                                apps += 1
            elif is_widget(item):
                widgets += 1
            elif isinstance(item, dict):
                apps += 1
    return {
        "pages": max(0, len(layout) - 1),
        "dockItems": len(layout[0]) if layout and isinstance(layout[0], list) else 0,
        "apps": apps,
        "folders": folders,
        "widgets": widgets,
    }


# ----------------------------------------------------------------- validation


def validate(
    layout: Any,
    baseline: list[Any] | None = None,
    allowed_removals: set[str] | None = None,
) -> list[str]:
    """
    Raise LayoutError on anything unsafe. Return a list of non-fatal warnings.

    When `baseline` is supplied (the state currently on the phone), the layout
    is also checked for accidental object loss or duplication - the failure
    mode that actually costs a user their Home Screen.
    """
    if not isinstance(layout, list) or not layout:
        raise LayoutError("Refusing to apply an empty layout.")

    if not isinstance(layout[0], list):
        raise LayoutError("The Dock structure is invalid.")

    if len(layout[0]) > MAX_DOCK_ITEMS:
        raise LayoutError(
            f"The Dock holds at most {MAX_DOCK_ITEMS} items "
            f"({len(layout[0])} were supplied)."
        )

    if len(layout) - 1 > MAX_PAGES:
        raise LayoutError(
            f"{len(layout) - 1} pages exceeds the supported maximum of {MAX_PAGES}."
        )

    warnings: list[str] = []

    for page_index, page in enumerate(layout):
        where = page_label(page_index)
        if not isinstance(page, list):
            raise LayoutError(f"{where.capitalize()} is not a valid page.")

        used = 0

        for position, item in enumerate(page):
            if not isinstance(item, dict):
                raise LayoutError(f"Item {position + 1} on {where} is not a valid object.")

            if page_index == 0 and is_folder(item):
                warnings.append("A folder in the Dock is unusual and may not render as expected.")

            used += slot_cost(item)

            if is_folder(item):
                _validate_folder(item, where)

        if page_index > 0 and used > MAX_PAGE_SLOTS:
            over = used - MAX_PAGE_SLOTS
            raise LayoutError(
                f"{where.capitalize()} is over capacity by {over} cell(s): it needs {used} "
                f"of the {MAX_PAGE_SLOTS} an iPhone page has. Move {over} item(s) to "
                f"another page, or add a page. (A small widget takes 4 cells, "
                f"a medium one 8.)"
            )

    # Duplication, within a page or across pages. A duplicate that the phone
    # itself produced is not
    # our business to refuse - refusing it would block every write on a device
    # whose layout already contains one. Only block duplication this edit adds.
    counts = fingerprint(layout)
    baseline_counts = fingerprint(baseline) if baseline is not None else Counter()

    introduced = [
        key
        for key, count in counts.items()
        if key.startswith("app:") and count > max(1, baseline_counts.get(key, 0))
    ]
    if introduced:
        names = ", ".join(key.split(":", 1)[1] for key in introduced[:5])
        raise LayoutError(
            f"This edit would place these apps in more than one position: {names}. "
            "Each app can only occupy one position."
        )

    if baseline is None:
        pre_existing = [k for k, c in counts.items() if k.startswith("app:") and c > 1]
        if pre_existing:
            warnings.append(
                f"{len(pre_existing)} object(s) appear more than once in this layout. "
                "That is how the phone reported it, so it is being left alone."
            )

    if baseline is not None:
        warnings.extend(_check_against_baseline(layout, baseline, allowed_removals or set()))

    return warnings


def _validate_folder(folder: dict, where: str) -> None:
    name = display_name(folder)
    icon_lists = folder.get("iconLists")

    if not isinstance(icon_lists, list):
        raise LayoutError(f'Folder "{name}" on {where} has invalid contents.')

    if len(icon_lists) > MAX_FOLDER_PAGES:
        raise LayoutError(
            f'Folder "{name}" has {len(icon_lists)} pages, more than the '
            f"{MAX_FOLDER_PAGES} iOS supports."
        )

    for page_index, folder_page in enumerate(icon_lists):
        if not isinstance(folder_page, list):
            raise LayoutError(f'Folder "{name}" page {page_index + 1} is invalid.')

        if len(folder_page) > MAX_FOLDER_PAGE_ITEMS:
            raise LayoutError(
                f'Folder "{name}" page {page_index + 1} holds {len(folder_page)} apps; '
                f"the limit is {MAX_FOLDER_PAGE_ITEMS}."
            )

        for child in folder_page:
            if not isinstance(child, dict):
                raise LayoutError(f'Folder "{name}" contains an invalid entry.')
            if is_folder(child):
                raise LayoutError(
                    f'Folder "{name}" contains another folder. '
                    "iOS does not support nested folders."
                )
            if is_widget(child):
                raise LayoutError(
                    f'Folder "{name}" contains a widget. Widgets cannot live inside folders.'
                )


def _check_against_baseline(
    layout: list[Any],
    baseline: list[Any],
    allowed_removals: set[str],
) -> list[str]:
    warnings: list[str] = []
    before = fingerprint(baseline)
    after = fingerprint(layout)

    lost = []
    for key, count in before.items():
        missing = count - after.get(key, 0)
        if missing > 0 and key not in allowed_removals:
            lost.append(key)

    if lost:
        names = ", ".join(key.split(":", 1)[1] for key in lost[:6])
        more = f" and {len(lost) - 6} more" if len(lost) > 6 else ""
        raise LayoutError(
            f"This layout would silently drop {len(lost)} item(s) that are currently "
            f"on the phone: {names}{more}. iOS does not allow an app to be hidden from "
            "the Home Screen this way. Move it to an Archive folder, or uninstall it."
        )

    appeared = [key for key in after if key not in before]
    if appeared:
        warnings.append(
            f"{len(appeared)} item(s) in this layout were not on the phone when it was read. "
            "Reload from the phone if this is unexpected."
        )

    return warnings


# ----------------------------------------------------------------------- diff


def describe_changes(before: list[Any], after: list[Any], limit: int = 40) -> list[str]:
    """
    A precise, human-readable change list - the thing that replaces
    "Home Screen structure changed."
    """
    changes: list[str] = []

    before_loc = locations(before)
    after_loc = locations(after)
    before_names = {identity(i): display_name(i) for i in _all_objects(before)}
    after_names = {identity(i): display_name(i) for i in _all_objects(after)}

    before_pages = max(0, len(before) - 1)
    after_pages = max(0, len(after) - 1)
    if after_pages > before_pages:
        changes.append(f"Added {after_pages - before_pages} page(s).")
    elif after_pages < before_pages:
        changes.append(f"Removed {before_pages - after_pages} page(s).")

    before_folders = {k for k in before_loc if k.startswith("folder:")}
    after_folders = {k for k in after_loc if k.startswith("folder:")}

    # A rename looks like a delete plus a create unless we pair them up by
    # contents. Users think of it as one action, so report it as one.
    gone = sorted(before_folders - after_folders)
    fresh = sorted(after_folders - before_folders)
    renames: dict[str, str] = {}
    fresh_pool = list(fresh)

    for old_key in list(gone):
        old_contents = _folder_contents(before, old_key)
        if not old_contents:
            continue
        for new_key in fresh_pool:
            if _folder_contents(after, new_key) == old_contents:
                renames[old_key] = new_key
                fresh_pool.remove(new_key)
                gone.remove(old_key)
                break

    for old_key, new_key in renames.items():
        changes.append(
            f'Renamed folder "{before_names.get(old_key, "Folder")}" to '
            f'"{after_names.get(new_key, "Folder")}".'
        )
        old_label = f'the "{before_names.get(old_key, "Folder")}" folder'
        new_label = f'the "{after_names.get(new_key, "Folder")}" folder'
        for key, value in list(before_loc.items()):
            if value == old_label:
                before_loc[key] = new_label

    for key in fresh_pool:
        name = after_names.get(key, "Folder")
        count = _folder_size(after, key)
        changes.append(f'Created folder "{name}" with {count} app(s).')

    for key in gone:
        changes.append(f'Removed folder "{before_names.get(key, "Folder")}".')

    # Folders are told apart by name, and a phone can hold two with the same
    # name. Merging one "Food" into the other leaves the name in place, so
    # compare how many there are as well.
    count_before = Counter(identity(i) for i in _all_objects(before) if is_folder(i))
    count_after = Counter(identity(i) for i in _all_objects(after) if is_folder(i))
    for key in sorted(set(count_before) & set(count_after)):
        delta = count_after[key] - count_before[key]
        name = after_names.get(key, "Folder")
        if delta < 0:
            changes.append(f'Removed {-delta} of the {count_before[key]} folders named "{name}".')
        elif delta > 0:
            changes.append(f'Added {delta} more folder(s) named "{name}".')

    for key in sorted(after_loc):
        if key.startswith("folder:"):
            continue
        name = after_names.get(key, key.split(":", 1)[1])
        old = before_loc.get(key)
        new = after_loc[key]
        if old is None:
            changes.append(f"Added {name} to {new}.")
        elif old != new:
            changes.append(f"Moved {name} from {old} to {new}.")

    for key in sorted(before_loc):
        if key.startswith("folder:") or key in after_loc:
            continue
        changes.append(
            f"Removed {before_names.get(key, key.split(':', 1)[1])} from the Home Screen "
            "(the app stays installed)."
        )

    if not changes and before != after:
        changes.append("Reordered items without moving them between pages.")

    if len(changes) > limit:
        hidden = len(changes) - limit
        changes = changes[:limit] + [f"...and {hidden} more change(s)."]

    return changes


def _all_objects(layout: list[Any]) -> Iterable[Any]:
    for page in layout:
        if not isinstance(page, list):
            continue
        for item in page:
            if isinstance(item, dict):
                yield item
            if is_folder(item):
                for folder_page in item.get("iconLists") or []:
                    if isinstance(folder_page, list):
                        for child in folder_page:
                            if isinstance(child, dict):
                                yield child


def _folder_contents(layout: list[Any], folder_key: str) -> frozenset[str]:
    """The set of app identities inside a folder, used to match renames."""
    for page in layout:
        if not isinstance(page, list):
            continue
        for item in page:
            if is_folder(item) and identity(item) == folder_key:
                children: set[str] = set()
                for folder_page in item.get("iconLists") or []:
                    if isinstance(folder_page, list):
                        for child in folder_page:
                            if isinstance(child, dict):
                                children.add(identity(child))
                return frozenset(children)
    return frozenset()


def _folder_size(layout: list[Any], folder_key: str) -> int:
    for page in layout:
        if not isinstance(page, list):
            continue
        for item in page:
            if is_folder(item) and identity(item) == folder_key:
                return sum(
                    len(fp) for fp in (item.get("iconLists") or []) if isinstance(fp, list)
                )
    return 0


# --------------------------------------------------------- write verification


def verify_write(intended: list[Any], accepted: list[Any]) -> dict[str, Any]:
    """
    Compare what we asked for against what the phone actually took.

    SpringBoard is allowed to reflow a layout - it packs pages and can shuffle
    positions. It also sometimes *adds* objects back: apps it decides belong on
    the Home Screen reappear at the end rather than going to the App Library.

    Those two outcomes are not equally serious, and the original code treating
    them alike caused a successful write to be rolled back:

      missing    - the phone dropped something we sent. Dangerous. Roll back.
      unexpected - the phone kept something we left out. Untidy, not harmful,
                   and rolling back would only repeat it.
    """
    want = fingerprint(intended)
    got = fingerprint(accepted)

    missing = sorted((want - got).elements())
    unexpected = sorted((got - want).elements())

    if missing:
        severity = "mismatch"
    elif unexpected:
        severity = "augmented"
    elif intended != accepted:
        severity = "reflowed"
    else:
        severity = "exact"

    return {
        "severity": severity,
        # Only a loss is a failure worth undoing.
        "ok": severity != "mismatch",
        "missing": [key.split(":", 1)[1] for key in missing[:20]],
        "unexpected": [key.split(":", 1)[1] for key in unexpected[:20]],
        "missingCount": len(missing),
        "unexpectedCount": len(unexpected),
    }
