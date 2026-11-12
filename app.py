"""
Organize My Phone - local connector.

A small local service that reads and writes the iPhone Home Screen on behalf of
the Organize My Phone web editor. It does as little as possible: talk to the
device, validate, snapshot, write, verify, roll back on doubt. All of the
product intelligence lives in the hosted editor.

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import threading
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import config
import editor_source
import layout_ops
import security
from bridge import BridgeError, get_bridge

config.ensure_directories()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("connector")

app = FastAPI(
    title=config.PRODUCT_NAME + " Connector",
    version=config.CONNECTOR_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

bridge = get_bridge()

# The Home Screen exactly as it was when this session first read the phone.
SESSION_ORIGINAL: list[Any] | None = None


# ------------------------------------------------------------------- payloads


class ApplyRequest(BaseModel):
    layout: list[Any]
    dryRun: bool = False


class PreviewRequest(BaseModel):
    layout: list[Any]


class PairStartRequest(BaseModel):
    siteName: str | None = None


class PairCompleteRequest(BaseModel):
    pairingId: str
    code: str


class SnapshotRequest(BaseModel):
    filename: str


class UninstallPrepareRequest(BaseModel):
    bundleIds: list[str]


class UninstallRequest(BaseModel):
    confirmToken: str
    phrase: str


# ---------------------------------------------------------------- middlewares


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    origin = request.headers.get("origin")

    if request.method == "OPTIONS":
        return security.apply_cors(Response(status_code=204), origin)

    if not security.is_loopback(request):
        log.warning("Refused non-loopback request from %s", request.client)
        return JSONResponse({"detail": "This connector only accepts local connections."}, 403)

    if not security.host_allowed(request):
        log.warning("Refused request with foreign Host header %s", request.headers.get("host"))
        return JSONResponse({"detail": "This connector only answers to localhost."}, 403)

    if not security.origin_allowed(origin):
        log.warning("Refused request from disallowed origin %s", origin)
        return security.apply_cors(
            JSONResponse({"detail": f"Origin {origin} is not allowed to use this connector."}, 403),
            None,
        )

    if request.url.path not in security.PUBLIC_PATHS:
        try:
            security.require_token(request)
        except HTTPException as exc:
            return security.apply_cors(
                JSONResponse({"detail": exc.detail}, exc.status_code), origin
            )

    try:
        response = await call_next(request)
    except BridgeError as exc:
        response = JSONResponse({"detail": str(exc)}, 503)
    return security.apply_cors(response, origin)


# ----------------------------------------------------------------- snapshots


def _snapshot_payload(layout: list[Any], kind: str, note: str = "") -> dict[str, Any]:
    return {
        "schema": 1,
        "kind": kind,
        "note": note,
        "created": datetime.now().isoformat(timespec="seconds"),
        "connector": config.CONNECTOR_VERSION,
        "stats": layout_ops.stats(layout),
        "layout": layout,
    }


def save_snapshot(layout: list[Any], kind: str, note: str = "") -> str:
    config.ensure_directories()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = config.SNAPSHOT_DIR / f"{kind}-{stamp}.json"
    path.write_text(
        json.dumps(_snapshot_payload(layout, kind, note), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    _prune_snapshots()
    return path.name


def _prune_snapshots() -> None:
    files = sorted(config.SNAPSHOT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[config.MAX_SNAPSHOTS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _safe_snapshot(filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Invalid snapshot name.")
    path = config.SNAPSHOT_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="That snapshot no longer exists.")
    return path


def _load_snapshot(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("layout"), list):
        return data["layout"]
    if isinstance(data, list):
        return data  # v1 format from the original prototype
    raise HTTPException(status_code=400, detail="That snapshot file is not a readable layout.")


# --------------------------------------------------------------- device reads


async def read_layout(capture_original: bool = True) -> list[Any]:
    global SESSION_ORIGINAL
    layout = await bridge.get_icon_state()
    if capture_original and SESSION_ORIGINAL is None:
        SESSION_ORIGINAL = copy.deepcopy(layout)
        save_snapshot(SESSION_ORIGINAL, "session-start", "Captured on first read of this session.")
        log.info("Captured session-start layout: %s", layout_ops.stats(layout))
    return layout


# ---------------------------------------------------------------- basic pages


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    The editor, served from this address with the token already filled in, so
    starting the app is the only step. If the editor cannot be found (first
    run with no internet), a status page explains what to do.

    This page carries the token, so it is only ever handed to a person opening
    it directly in a browser tab: never to another site's script (which would
    let even an allowed site skip pairing) and never inside a frame.
    """
    if not security.is_direct_visit(request):
        return JSONResponse({"detail": "Open this address directly in a browser tab."}, 403)

    html = editor_source.load()
    if html is None:
        return FileResponse(config.STATIC_DIR / "status.html")
    html = html.replace(editor_source.TOKEN_PLACEHOLDER, security.get_or_create_token())
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/pair", response_class=HTMLResponse)
async def pair_page():
    return FileResponse(config.STATIC_DIR / "pair.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


# ------------------------------------------------------------------ handshake


@app.get("/api/hello")
async def hello():
    """Unauthenticated discovery. Deliberately reveals nothing about the user."""
    return {
        "product": config.PRODUCT_NAME,
        "connectorVersion": config.CONNECTOR_VERSION,
        "apiVersion": config.API_VERSION,
        "installId": security.install_id(),
    }


@app.post("/api/pair/start")
async def pair_start(request: Request, body: PairStartRequest):
    origin = request.headers.get("origin") or "local"
    pairing_id, code = security.start_pairing(origin)
    log.info("Pairing started for %s - code %s", origin, code)
    print("\n" + "=" * 46)
    print(f"  PAIRING CODE: {code}")
    print(f"  Requested by: {origin}")
    print(f"  Valid for {config.PAIRING_CODE_TTL_SECONDS // 60} minutes.")
    print("=" * 46 + "\n", flush=True)
    return {
        "pairingId": pairing_id,
        "expiresInSeconds": config.PAIRING_CODE_TTL_SECONDS,
        "codeVisibleAt": f"http://127.0.0.1:{config.DEFAULT_PORT}/pair",
    }


@app.get("/api/pair/code")
async def pair_code(request: Request):
    """Only the local pair page may read the live code."""
    if request.headers.get("origin") not in (None, f"http://127.0.0.1:{config.DEFAULT_PORT}",
                                             f"http://localhost:{config.DEFAULT_PORT}"):
        raise HTTPException(status_code=403, detail="Not available to this origin.")
    return {"code": security.current_pairing_code()}


@app.post("/api/pair/complete")
async def pair_complete(request: Request, body: PairCompleteRequest):
    origin = request.headers.get("origin") or "local"
    token = security.complete_pairing(body.pairingId, body.code, origin)
    log.info("Pairing completed for %s", origin)
    return {"token": token, "apiVersion": config.API_VERSION}


@app.post("/api/pair/reset")
async def pair_reset():
    """Invalidate every paired site. Requires an already-valid token."""
    security.rotate_token()
    log.info("Connector token rotated; all sites unpaired.")
    return {"success": True}


# --------------------------------------------------------------------- device


@app.get("/api/device")
async def device():
    info = await bridge.device_info()
    info["bridge"] = bridge.name
    return info


@app.get("/api/layout")
async def get_layout():
    layout = await read_layout()
    return {
        "success": True,
        "layout": layout,
        "stats": layout_ops.stats(layout),
        "hasSessionOriginal": SESSION_ORIGINAL is not None,
    }


@app.get("/api/icon/{bundle_id:path}")
async def icon(bundle_id: str):
    if not bundle_id:
        raise HTTPException(status_code=404, detail="No bundle identifier.")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)[:180]
    cached = config.ICON_CACHE_DIR / f"{safe}.png"
    headers = {"Cache-Control": "public, max-age=604800"}

    if cached.exists() and cached.stat().st_size > 0:
        return FileResponse(cached, media_type="image/png", headers=headers)

    png = await bridge.get_icon_png(bundle_id)
    if not png:
        raise HTTPException(status_code=404, detail="No icon available.")

    try:
        cached.write_bytes(png)
    except OSError:
        pass
    return Response(content=png, media_type="image/png", headers=headers)


# ------------------------------------------------------------------- previews


@app.post("/api/layout/preview")
async def preview(body: PreviewRequest):
    """
    Validate a proposed layout and describe exactly what it would change,
    without touching the phone. This is what the editor calls to fill the
    confirmation dialog.
    """
    current = await read_layout()
    try:
        warnings = layout_ops.validate(body.layout, baseline=current)
    except layout_ops.LayoutError as exc:
        return {"success": False, "blocked": True, "detail": str(exc)}

    changes = layout_ops.describe_changes(current, body.layout)
    return {
        "success": True,
        "blocked": False,
        "changes": changes,
        "changeCount": len(changes),
        "warnings": warnings,
        "statsBefore": layout_ops.stats(current),
        "statsAfter": layout_ops.stats(body.layout),
    }


# ---------------------------------------------------------------------- write


@app.post("/api/layout/apply")
async def apply_layout(body: ApplyRequest):
    current = await read_layout()

    try:
        warnings = layout_ops.validate(body.layout, baseline=current)
    except layout_ops.LayoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    changes = layout_ops.describe_changes(current, body.layout)

    if body.dryRun:
        return {"success": True, "dryRun": True, "changes": changes, "warnings": warnings}

    before_snapshot = save_snapshot(current, "before-apply", f"{len(changes)} change(s)")
    intended = copy.deepcopy(body.layout)

    log.info("Applying %d change(s); rollback snapshot %s", len(changes), before_snapshot)
    await bridge.set_icon_state(intended)

    accepted = await read_layout(capture_original=False)
    verdict = layout_ops.verify_write(intended, accepted)

    if not verdict["ok"]:
        log.error("Write verification failed: %s", verdict)
        rollback = await _auto_rollback(current)
        if rollback:
            tail = "Your previous Home Screen was restored and verified."
        else:
            tail = (
                "The rollback could not be verified. Restore the snapshot "
                f"{before_snapshot} from the Snapshots panel."
            )
        raise HTTPException(
            status_code=409,
            detail=(
                "The phone did not accept that layout cleanly, so nothing was kept. "
                f"{verdict['missingCount']} item(s) went missing. {tail}"
            ),
        )

    after_snapshot = save_snapshot(accepted, "after-apply", verdict["severity"])
    log.info("Apply succeeded (%s)", verdict["severity"])

    if verdict["severity"] == "augmented":
        shown = ", ".join(verdict["unexpected"][:8])
        more = f" and {verdict['unexpectedCount'] - 8} more" if verdict["unexpectedCount"] > 8 else ""
        warnings.append(
            f"Your changes were applied, but iOS put {verdict['unexpectedCount']} app(s) "
            f"back on the Home Screen instead of moving them to the App Library: "
            f"{shown}{more}. Nothing was lost. To undo everything, restore the "
            f"snapshot {before_snapshot}."
        )
        log.info("iOS re-added %d app(s) after the write", verdict["unexpectedCount"])

    return {
        "success": True,
        "layout": accepted,
        "stats": layout_ops.stats(accepted),
        "changes": changes,
        "warnings": warnings,
        "verification": verdict,
        "beforeSnapshot": before_snapshot,
        "afterSnapshot": after_snapshot,
    }


async def _auto_rollback(known_good: list[Any]) -> bool:
    try:
        await bridge.set_icon_state(copy.deepcopy(known_good))
        restored = await read_layout(capture_original=False)
        ok = layout_ops.verify_write(known_good, restored)["ok"]
        save_snapshot(restored, "after-rollback", "automatic rollback")
        return ok
    except Exception as exc:  # pragma: no cover
        log.exception("Automatic rollback failed: %s", exc)
        return False


@app.post("/api/layout/restore-original")
async def restore_original():
    if SESSION_ORIGINAL is None:
        raise HTTPException(status_code=400, detail="No session-start layout was captured yet.")
    return await _restore(SESSION_ORIGINAL, "session-start")


@app.post("/api/snapshots/restore")
async def restore_snapshot(body: SnapshotRequest):
    target = _load_snapshot(_safe_snapshot(body.filename))
    return await _restore(target, body.filename)


async def _restore(target: list[Any], label: str):
    # A snapshot taken before an uninstall still lists the deleted apps. They
    # cannot come back, so drop them rather than send the phone dead icons.
    target = _strip_bundles(target, _uninstalled_bundles())
    try:
        layout_ops.validate(target)
    except layout_ops.LayoutError as exc:
        raise HTTPException(status_code=400, detail=f"That snapshot is not safe to apply: {exc}")

    current = await read_layout(capture_original=False)
    save_snapshot(current, "before-restore", label)

    await bridge.set_icon_state(copy.deepcopy(target))
    accepted = await read_layout(capture_original=False)
    verdict = layout_ops.verify_write(target, accepted)
    save_snapshot(accepted, "after-restore", label)

    return {
        "success": True,
        "layout": accepted,
        "stats": layout_ops.stats(accepted),
        "verification": verdict,
        "restored": label,
    }


# ------------------------------------------------------------------ uninstall
#
# This is the only destructive thing the connector does. Everything else can
# be rolled back from a snapshot; a deleted app's data cannot. So it takes two
# requests: "prepare" says exactly what would be deleted and hands back a
# short-lived token, and only that token plus the typed phrase deletes.

_PENDING_UNINSTALLS: dict[str, dict[str, Any]] = {}


def _uninstalled_bundles() -> set[str]:
    try:
        return set(json.loads(config.UNINSTALLED_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _remember_uninstalled(bundles: list[str]) -> None:
    known = _uninstalled_bundles() | set(bundles)
    try:
        config.UNINSTALLED_FILE.write_text(json.dumps(sorted(known)), encoding="utf-8")
    except OSError:
        pass


def _app_bundle(item: Any) -> str | None:
    """Most apps carry bundleIdentifier; some only carry displayIdentifier."""
    return item.get("bundleIdentifier") or item.get("displayIdentifier")


def _strip_bundles(layout: list[Any], bundles: set[str]) -> list[Any]:
    """A copy of the layout without apps whose bundle id is in `bundles`."""
    if not bundles:
        return layout

    def keep(item: Any) -> bool:
        return not (layout_ops.is_app(item) and _app_bundle(item) in bundles)

    out = []
    for page in layout:
        new_page = []
        for item in page if isinstance(page, list) else []:
            if layout_ops.is_folder(item):
                item = dict(item)
                item["iconLists"] = [
                    [c for c in fp if keep(c)] for fp in (item.get("iconLists") or [])
                    if isinstance(fp, list)
                ]
                new_page.append(item)
            elif keep(item):
                new_page.append(item)
        out.append(new_page)
    return out


def _bundles_on_home_screen(layout: list[Any]) -> dict[str, str]:
    """bundle id -> display name for every app in the layout."""
    found: dict[str, str] = {}
    for item in layout_ops.iter_leaves(layout):
        if layout_ops.is_app(item) and _app_bundle(item):
            found.setdefault(_app_bundle(item), layout_ops.display_name(item))
    return found


@app.post("/api/apps/uninstall/prepare")
async def uninstall_prepare(body: UninstallPrepareRequest):
    requested = list(dict.fromkeys(b.strip() for b in body.bundleIds if b and b.strip()))
    if not requested:
        raise HTTPException(status_code=400, detail="No apps were selected.")
    if len(requested) > config.MAX_UNINSTALL_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"Uninstall at most {config.MAX_UNINSTALL_BATCH} apps at a time "
                   f"({len(requested)} were selected).",
        )

    installed = await bridge.list_user_apps()
    on_screen = _bundles_on_home_screen(await read_layout())

    allowed, blocked = [], []
    for bundle in requested:
        if bundle not in installed:
            blocked.append({
                "bundleId": bundle,
                "name": on_screen.get(bundle, bundle),
                "reason": "Built into iOS or not installed - it cannot be uninstalled from here.",
            })
        else:
            allowed.append({
                "bundleId": bundle,
                "name": on_screen.get(bundle) or installed[bundle].get("name") or bundle,
            })

    for stale in [k for k, v in _PENDING_UNINSTALLS.items() if v["expires"] < time.time()]:
        _PENDING_UNINSTALLS.pop(stale, None)

    token = None
    if allowed:
        token = secrets.token_urlsafe(16)
        _PENDING_UNINSTALLS[token] = {
            "apps": allowed,
            "expires": time.time() + config.UNINSTALL_CONFIRM_TTL_SECONDS,
        }

    return {
        "apps": allowed,
        "blocked": blocked,
        "confirmToken": token,
        "phrase": config.UNINSTALL_PHRASE,
        "expiresInSeconds": config.UNINSTALL_CONFIRM_TTL_SECONDS,
    }


@app.post("/api/apps/uninstall")
async def uninstall(body: UninstallRequest):
    global SESSION_ORIGINAL

    pending = _PENDING_UNINSTALLS.pop(body.confirmToken, None)
    if pending is None or pending["expires"] < time.time():
        raise HTTPException(
            status_code=400,
            detail="That confirmation expired. Nothing was uninstalled. Start again.",
        )
    if body.phrase.strip() != config.UNINSTALL_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"Type {config.UNINSTALL_PHRASE} exactly to confirm. Nothing was uninstalled.",
        )

    before = await read_layout()
    snapshot = save_snapshot(before, "before-uninstall",
                             f"{len(pending['apps'])} app(s) - layout only, app data is not saved")

    results = []
    for entry in pending["apps"]:
        try:
            await bridge.uninstall_app(entry["bundleId"])
            results.append({**entry, "ok": True})
            log.warning("UNINSTALLED %s (%s)", entry["name"], entry["bundleId"])
        except Exception as exc:
            results.append({**entry, "ok": False, "error": str(exc)})
            log.error("Uninstall failed for %s: %s", entry["bundleId"], exc)

    done = [r["bundleId"] for r in results if r["ok"]]
    _remember_uninstalled(done)

    # The old session-start layout now lists apps that no longer exist.
    after = await read_layout(capture_original=False)
    SESSION_ORIGINAL = copy.deepcopy(after)
    save_snapshot(after, "after-uninstall", f"{len(done)} app(s) uninstalled")

    return {
        "success": True,
        "results": results,
        "uninstalledCount": len(done),
        "failedCount": len(results) - len(done),
        "layout": after,
        "stats": layout_ops.stats(after),
        "layoutSnapshot": snapshot,
    }


# ------------------------------------------------------------------ snapshots


@app.get("/api/snapshots")
async def list_snapshots():
    rows = []
    for path in sorted(config.SNAPSHOT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:40]:
        entry = {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "kind": path.name.split("-")[0],
            "note": "",
            "stats": None,
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                entry["note"] = data.get("note", "")
                entry["stats"] = data.get("stats")
                entry["kind"] = data.get("kind", entry["kind"])
        except (OSError, json.JSONDecodeError):
            pass
        rows.append(entry)
    return {"success": True, "snapshots": rows, "directory": str(config.SNAPSHOT_DIR)}


@app.get("/api/layout/export")
async def export_layout():
    layout = await read_layout(capture_original=False)
    payload = json.dumps(_snapshot_payload(layout, "export"), ensure_ascii=False,
                         indent=2, default=str).encode("utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="home-screen-{stamp}.json"'},
    )


@app.post("/api/quit")
async def quit_app():
    """Close the app. It has no window of its own, so the editor offers this."""
    log.info("Quit requested from the editor.")
    server = getattr(app.state, "server", None)
    if server is not None:
        server.should_exit = True
    else:
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    return {"success": True}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "product": config.PRODUCT_NAME,
        "connectorVersion": config.CONNECTOR_VERSION,
        "apiVersion": config.API_VERSION,
        "bridge": bridge.name,
        "runningFromSource": config.dev_mode(),
        "dataDirectory": str(config.DATA_DIR),
    }
