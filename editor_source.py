"""
Organize My Phone - where the editor page comes from.

Running the app should open the editor straight away, with no pairing step.
For that the connector serves the editor from its own address, with its token
already filled in.

The editor is a separate work and is not bundled with this GPL program, so it
is looked up in this order:

  1. A copy sitting beside the connector (development only).
  2. The copy published on the website, fetched over HTTPS and kept on disk so
     the app still opens with no internet connection.

Whatever is served here can read and rewrite the Home Screen, so the only
remote source is the product's own site, and a download is rejected unless it
is recognisably the editor.

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request

import config

log = logging.getLogger("connector")

TOKEN_PLACEHOLDER = "__CONNECTOR_TOKEN__"
_REFRESH_AFTER_SECONDS = 6 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 8
_MAX_BYTES = 4 * 1024 * 1024

_refresh_lock = threading.Lock()


def editor_url() -> str:
    return os.environ.get("ORGANIZE_MY_PHONE_EDITOR_URL", "").strip() or config.EDITOR_URL


def looks_like_editor(html: str) -> bool:
    """Reject error pages, redirects to a login wall, or anything else."""
    return (
        TOKEN_PLACEHOLDER in html
        and "<title>Organize My Phone</title>" in html
        and "/api/layout" in html
    )


def _fetch() -> str | None:
    url = editor_url()
    if not url.startswith("https://"):
        log.warning("Editor URL is not HTTPS; refusing to fetch %s", url)
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "OrganizeMyPhoneConnector"})
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            if not response.geturl().startswith("https://"):
                return None
            html = response.read(_MAX_BYTES + 1).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("Could not fetch the editor from %s: %s", url, exc)
        return None

    if len(html) > _MAX_BYTES or not looks_like_editor(html):
        log.warning("%s did not return the editor; keeping the saved copy.", url)
        return None
    return html


def _read_cache() -> str | None:
    try:
        html = config.EDITOR_CACHE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    return html if looks_like_editor(html) else None


def _cache_age_seconds() -> float:
    try:
        return time.time() - config.EDITOR_CACHE_FILE.stat().st_mtime
    except OSError:
        return float("inf")


def refresh_cache() -> bool:
    """Download the current editor and save it. True if a new copy was saved."""
    with _refresh_lock:
        html = _fetch()
        if html is None:
            return False
        config.ensure_directories()
        tmp = config.EDITOR_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(config.EDITOR_CACHE_FILE)
        log.info("Saved the current editor from %s", editor_url())
        return True


def refresh_in_background() -> None:
    if _cache_age_seconds() > _REFRESH_AFTER_SECONDS:
        threading.Thread(target=refresh_cache, daemon=True).start()


def load() -> str | None:
    """The editor page, token placeholder still in place, or None."""
    if config.dev_mode():
        try:
            return config.DEV_EDITOR_FILE.read_text(encoding="utf-8")
        except OSError:
            pass

    cached = _read_cache()
    if cached is not None:
        refresh_in_background()
        return cached

    # First run: nothing saved yet, so this one request waits for the download.
    return _read_cache() if refresh_cache() else None
