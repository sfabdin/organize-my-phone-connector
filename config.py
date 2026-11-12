"""
Organize My Phone - connector configuration.

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PRODUCT_NAME = "Organize My Phone"
CONNECTOR_VERSION = "3.1.0"

# Protocol version the web app negotiates against. Bump when the API contract
# changes in a way an older hosted frontend cannot handle.
API_VERSION = 1

DEFAULT_PORT = 8777

# When PyInstaller freezes the app, bundled data lives in a temp folder that
# only exists at runtime. Resolve against that so the status and pair pages
# are found in both a source checkout and a shipped executable.
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SOURCE_DIR = Path(sys.executable).resolve().parent if FROZEN else BASE_DIR
STATIC_DIR = BASE_DIR / "static"

# The editor is a separate work and is not part of this repository. During
# development a copy sits beside the connector at ../web/public/app.html. A
# shipped build fetches it from the website instead (see editor_source.py).
DEV_EDITOR_FILE = SOURCE_DIR.parent / "web" / "public" / "app.html"
EDITOR_URL = "https://organizemyphone.com/app"

_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or Path.home())
DATA_DIR = _LOCAL_APP_DATA / "OrganizeMyPhone"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
ICON_CACHE_DIR = DATA_DIR / "icon_cache"
STATE_FILE = DATA_DIR / "connector.json"
EDITOR_CACHE_FILE = DATA_DIR / "editor-cache.html"
LOG_FILE = DATA_DIR / "connector.log"

# Origins permitted to talk to this connector. Anything not on this list is
# refused before it can reach a write endpoint, which is what stops a random
# page in the user's browser from rewriting their Home Screen.
DEFAULT_ALLOWED_ORIGINS = [
    "https://organizemyphone.com",
    "https://www.organizemyphone.com",
    f"http://127.0.0.1:{DEFAULT_PORT}",
    f"http://localhost:{DEFAULT_PORT}",
]

# Layout safety limits. These are deliberately conservative: it is better to
# refuse a write than to hand SpringBoard a layout it silently mangles.
MAX_DOCK_ITEMS = 4
MAX_PAGE_SLOTS = 24          # 4 columns x 6 rows on every current iPhone
MAX_FOLDER_PAGE_ITEMS = 9
MAX_FOLDER_PAGES = 15
MAX_PAGES = 15

# Snapshot retention. Snapshots are small JSON files but they accumulate fast
# when a user is experimenting.
MAX_SNAPSHOTS = 200

# Uninstalling deletes an app and its data for good, so it is rate-limited by
# design: small batches, and a confirmation that expires quickly.
MAX_UNINSTALL_BATCH = 25
UNINSTALL_CONFIRM_TTL_SECONDS = 180
UNINSTALL_PHRASE = "DELETE"
UNINSTALLED_FILE = DATA_DIR / "uninstalled.json"

PAIRING_CODE_TTL_SECONDS = 180
PAIRING_MAX_ATTEMPTS = 5


def ensure_directories() -> None:
    for folder in (DATA_DIR, SNAPSHOT_DIR, ICON_CACHE_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def dev_mode() -> bool:
    """True when running from source with the editor checked out beside it."""
    return not FROZEN and DEV_EDITOR_FILE.exists()


# A plain text file beside the connector, one site per line. This exists so
# that testing a deployed build does not require setting an environment
# variable in a terminal - paste the URL in, restart, done.
ALLOWED_SITES_FILE = SOURCE_DIR / "allowed-sites.txt"


def read_allowed_sites_file() -> list[str]:
    """One origin per line. Blank lines and # comments ignored."""
    try:
        if not ALLOWED_SITES_FILE.exists():
            return []
        lines = ALLOWED_SITES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    sites: list[str] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip().rstrip("/")
        if not line:
            continue
        if not line.startswith(("http://", "https://")):
            line = "https://" + line
        sites.append(line)
    return sites


def load_allowed_origins() -> list[str]:
    """
    Allowed origins: the built-in list, plus allowed-sites.txt, plus anything
    in the state file, plus ORGANIZE_MY_PHONE_ORIGINS.
    """
    origins = list(DEFAULT_ALLOWED_ORIGINS)
    origins.extend(read_allowed_sites_file())

    env = os.environ.get("ORGANIZE_MY_PHONE_ORIGINS", "").strip()
    if env:
        origins.extend(o.strip() for o in env.split(",") if o.strip())

    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            origins.extend(state.get("extraOrigins") or [])
    except (OSError, json.JSONDecodeError):
        pass

    seen: set[str] = set()
    unique: list[str] = []
    for origin in origins:
        if origin not in seen:
            seen.add(origin)
            unique.append(origin)
    return unique
