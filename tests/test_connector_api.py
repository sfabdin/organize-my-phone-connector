"""
Tests for the connector's HTTP surface, against a simulated phone.

Covers the guards around uninstall (the only thing no snapshot can undo: two
steps, a typed phrase, a single-use token, a batch limit, built-in apps
refused) and the rules about who may talk to the connector at all.

Run directly:   python tests/test_connector_api.py
Needs fastapi and httpx installed. No phone required.
"""

import copy
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge  # noqa: E402
import layout_ops as L  # noqa: E402


def an_app(bundle, name):
    return {"bundleIdentifier": bundle, "displayIdentifier": bundle, "displayName": name}


PHONE = [
    [an_app("com.apple.mobilephone", "Phone")],
    [an_app("com.chase", "Chase"), an_app("com.spotify", "Spotify"),
     {"listType": "folder", "displayName": "Travel",
      "iconLists": [[an_app("com.waze.iphone", "Waze")]]}],
]


class FakePhone(bridge.DeviceBridge):
    name = "fake"

    def __init__(self):
        self.state = copy.deepcopy(PHONE)
        self.deleted = []

    async def device_info(self):
        return {"connected": True}

    async def get_icon_state(self):
        return copy.deepcopy(self.state)

    async def set_icon_state(self, layout):
        self.state = copy.deepcopy(layout)

    async def get_icon_png(self, bundle_id):
        return None

    async def list_user_apps(self, with_sizes=False):
        sizes = {"com.chase": (300, 50), "com.spotify": (200, 9000), "com.waze.iphone": (100, 100)}
        return {i["bundleIdentifier"]: {"name": i["displayName"],
                                        "appBytes": sizes[i["bundleIdentifier"]][0] if with_sizes else 0,
                                        "dataBytes": sizes[i["bundleIdentifier"]][1] if with_sizes else 0}
                for i in L.iter_leaves(self.state)
                if not i["bundleIdentifier"].startswith("com.apple.")}

    async def uninstall_app(self, bundle_id):
        self.deleted.append(bundle_id)
        for page in self.state:
            page[:] = [i for i in page if i.get("bundleIdentifier") != bundle_id]
            for item in page:
                if L.is_folder(item):
                    item["iconLists"] = [[c for c in fp if c["bundleIdentifier"] != bundle_id]
                                         for fp in item["iconLists"]]


phone = FakePhone()
bridge._BRIDGE = phone

import app as connector  # noqa: E402
import security  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

connector.bridge = phone
client = TestClient(connector.app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 50000))
AUTH = {"Authorization": f"Bearer {security.get_or_create_token()}"}


def prepare(*bundles):
    return client.post("/api/apps/uninstall/prepare", json={"bundleIds": list(bundles)}, headers=AUTH)


def confirm(token, phrase="DELETE"):
    return client.post("/api/apps/uninstall", json={"confirmToken": token, "phrase": phrase},
                       headers=AUTH)


def reset():
    phone.state = copy.deepcopy(PHONE)
    phone.deleted = []


def test_requires_pairing_token():
    assert client.post("/api/apps/uninstall/prepare", json={"bundleIds": ["com.chase"]}).status_code == 401
    assert client.post("/api/apps/uninstall", json={"confirmToken": "x", "phrase": "DELETE"}).status_code == 401


def test_prepare_deletes_nothing():
    reset()
    out = prepare("com.chase").json()
    assert [a["bundleId"] for a in out["apps"]] == ["com.chase"]
    assert out["confirmToken"]
    assert phone.deleted == []


def test_built_in_apps_are_refused():
    reset()
    out = prepare("com.apple.mobilephone").json()
    assert out["apps"] == [] and out["confirmToken"] is None
    assert out["blocked"][0]["name"] == "Phone"


def test_wrong_phrase_deletes_nothing_and_burns_the_token():
    reset()
    token = prepare("com.chase").json()["confirmToken"]
    assert confirm(token, "delete").status_code == 400
    assert phone.deleted == []
    # The token is single-use even on failure: a retry needs a fresh preview.
    assert confirm(token, "DELETE").status_code == 400
    assert phone.deleted == []


def test_made_up_token_deletes_nothing():
    reset()
    assert confirm("not-a-real-token").status_code == 400
    assert phone.deleted == []


def test_confirm_deletes_exactly_what_was_previewed():
    reset()
    token = prepare("com.chase", "com.waze.iphone", "com.apple.mobilephone").json()["confirmToken"]
    out = confirm(token)
    assert out.status_code == 200
    assert sorted(phone.deleted) == ["com.chase", "com.waze.iphone"]
    body = out.json()
    assert body["uninstalledCount"] == 2 and body["failedCount"] == 0
    assert body["stats"]["apps"] == 2          # Phone and Spotify remain
    # Replay must not delete again.
    assert confirm(token).status_code == 400


def test_batch_limit():
    reset()
    too_many = [f"com.example.app{i}" for i in range(26)]
    assert prepare(*too_many).status_code == 400


def test_restoring_an_old_snapshot_skips_uninstalled_apps():
    reset()
    confirm(prepare("com.chase").json()["confirmToken"])
    out = client.post("/api/layout/restore-original", headers=AUTH)
    assert out.status_code == 200
    bundles = {i["bundleIdentifier"] for i in L.iter_leaves(phone.state)}
    assert "com.chase" not in bundles


def test_app_sizes_are_listed_biggest_first_with_location():
    reset()
    assert client.get("/api/apps/sizes").status_code == 401
    out = client.get("/api/apps/sizes", headers=AUTH).json()
    assert [a["bundleId"] for a in out["apps"]] == ["com.spotify", "com.chase", "com.waze.iphone"]
    assert out["apps"][0]["totalBytes"] == 9200
    assert out["apps"][2]["where"] == 'the "Travel" folder'
    assert out["totalBytes"] == 9750
    assert phone.deleted == []


def test_foreign_host_header_is_refused():
    """DNS rebinding: a site pointing its own name at 127.0.0.1."""
    rebound = TestClient(connector.app, base_url="http://evil.example:8777", client=("127.0.0.1", 50000))
    assert rebound.get("/api/health", headers=AUTH).status_code == 403
    assert rebound.get("/").status_code == 403


# A stand-in for the editor page. The real one lives in the website repository,
# so these tests must not depend on it being on disk or on the site being up.
FAKE_EDITOR = (
    "<html><head><title>Organize My Phone</title></head><body>"
    '<script>const T = "__CONNECTOR_TOKEN__"; fetch("/api/layout");</script></body></html>'
)


def test_editor_page_is_only_given_to_a_direct_visit():
    """The page at / carries the token, so no other site may fetch or frame it."""
    import editor_source
    real_load = editor_source.load
    editor_source.load = lambda: FAKE_EDITOR
    try:
        page = client.get("/")
        assert page.status_code == 200
        assert security.get_or_create_token() in page.text
        assert "__CONNECTOR_TOKEN__" not in page.text
        # Even the product's own website must pair rather than read the page.
        assert client.get("/", headers={"Origin": "https://organizemyphone.com"}).status_code == 403
        assert client.get("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                                        "Sec-Fetch-Dest": "iframe"}).status_code == 403
        link = client.get("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                        "Sec-Fetch-Dest": "document"})
        assert link.status_code == 200
        assert link.headers["x-frame-options"] == "DENY"
    finally:
        editor_source.load = real_load


def test_status_page_when_the_editor_cannot_be_found():
    import editor_source
    real_load = editor_source.load
    editor_source.load = lambda: None
    try:
        page = client.get("/")
        assert page.status_code == 200
        assert "could not be loaded" in page.text
    finally:
        editor_source.load = real_load


def test_editor_download_must_be_the_editor():
    import editor_source
    assert not editor_source.looks_like_editor("<html><title>404: NOT_FOUND</title></html>")
    assert not editor_source.looks_like_editor("")
    assert editor_source.looks_like_editor(FAKE_EDITOR)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
