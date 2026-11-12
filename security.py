"""
Organize My Phone - connector security.

The threat this exists to stop: any page the user happens to have open in their
browser can attempt requests to 127.0.0.1. Binding to loopback keeps the
internet out, but it does not keep evil.example.com out, because that request
originates from the user's own machine. A connector that rewrites Home Screens
must not be reachable by an arbitrary origin.

Three layers:

  1. Origin allowlist - requests from anywhere else are refused outright.
  2. Pairing - an allowlisted origin still has to prove the human is present,
     by reading a 6-digit code the connector shows locally.
  3. Bearer token - issued on successful pairing, required for every route
     that reads or writes the device.

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import Response

import config


# ------------------------------------------------------------- persisted state


def _read_state() -> dict[str, Any]:
    try:
        if config.STATE_FILE.exists():
            return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_state(state: dict[str, Any]) -> None:
    config.ensure_directories()
    tmp = config.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(config.STATE_FILE)
    try:
        # Best effort on Windows; no-op where unsupported.
        config.STATE_FILE.chmod(0o600)
    except OSError:
        pass


def get_or_create_token() -> str:
    state = _read_state()
    token = state.get("token")
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
        state["token"] = token
        state.setdefault("installId", secrets.token_hex(8))
        state["createdAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_state(state)
    return token


def rotate_token() -> str:
    state = _read_state()
    state["token"] = secrets.token_urlsafe(32)
    state["rotatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_state(state)
    return state["token"]


def install_id() -> str:
    state = _read_state()
    if not state.get("installId"):
        state["installId"] = secrets.token_hex(8)
        _write_state(state)
    return state["installId"]


# -------------------------------------------------------------------- pairing


@dataclass
class _Pairing:
    code: str
    origin: str
    created: float = field(default_factory=time.time)
    attempts: int = 0

    @property
    def expired(self) -> bool:
        return time.time() - self.created > config.PAIRING_CODE_TTL_SECONDS


_PENDING: dict[str, _Pairing] = {}


def start_pairing(origin: str) -> tuple[str, str]:
    """Begin pairing. Returns (pairing_id, code). The code is shown locally."""
    _prune_pairings()
    pairing_id = secrets.token_urlsafe(12)
    code = f"{secrets.randbelow(1_000_000):06d}"
    _PENDING[pairing_id] = _Pairing(code=code, origin=origin)
    return pairing_id, code


def current_pairing_code() -> str | None:
    """The newest live code, for the local /pair page to display."""
    _prune_pairings()
    if not _PENDING:
        return None
    newest = max(_PENDING.values(), key=lambda p: p.created)
    return newest.code


def complete_pairing(pairing_id: str, code: str, origin: str) -> str:
    _prune_pairings()
    pending = _PENDING.get(pairing_id)

    if pending is None:
        raise HTTPException(status_code=400, detail="That pairing request expired. Start again.")

    if pending.origin != origin:
        _PENDING.pop(pairing_id, None)
        raise HTTPException(status_code=403, detail="Pairing origin mismatch.")

    pending.attempts += 1
    if pending.attempts > config.PAIRING_MAX_ATTEMPTS:
        _PENDING.pop(pairing_id, None)
        raise HTTPException(status_code=429, detail="Too many incorrect codes. Start again.")

    if not secrets.compare_digest(pending.code, str(code).strip()):
        raise HTTPException(status_code=401, detail="That code is not correct.")

    _PENDING.pop(pairing_id, None)
    return get_or_create_token()


def _prune_pairings() -> None:
    for key in [k for k, v in _PENDING.items() if v.expired]:
        _PENDING.pop(key, None)


# ------------------------------------------------------------------ transport


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def host_allowed(request: Request) -> bool:
    """
    Refuse requests whose Host header is not this machine.

    Without this, a malicious site can point its own domain at 127.0.0.1
    ("DNS rebinding"); the browser then treats the connector as that site's
    own server and the Origin check never sees a foreign origin.
    """
    host = (request.headers.get("host") or "").strip().lower()
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name in {"127.0.0.1", "localhost", "[::1]"}


def is_direct_visit(request: Request) -> bool:
    """
    True when a person opened the page themselves, as opposed to another
    site's script or frame requesting it.
    """
    origin = request.headers.get("origin")
    local = {f"http://127.0.0.1:{config.DEFAULT_PORT}", f"http://localhost:{config.DEFAULT_PORT}"}
    if origin and origin not in local:
        return False
    # Modern browsers label every request. A cross-site request is only
    # acceptable as a top-level navigation (a link or a typed address).
    site = request.headers.get("sec-fetch-site")
    if site == "cross-site":
        return (
            request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
    return True


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        # Same-origin fetches from the connector's own page send no Origin
        # header for GETs. Loopback is checked separately.
        return True
    return origin in config.load_allowed_origins()


def apply_cors(response: Response, origin: str | None) -> Response:
    """
    CORS plus the Local Network Access opt-in.

    Chrome is rolling out a permission gate on requests from public sites to
    software running on the user's own machine. Answering the preflight with
    Access-Control-Allow-Private-Network is what keeps the hosted editor able
    to reach this connector.
    """
    if origin and origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "false"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


# Routes reachable without a bearer token. Everything else is locked.
PUBLIC_PATHS = {
    "/",
    "/pair",
    "/favicon.ico",
    "/api/hello",
    "/api/pair/start",
    "/api/pair/complete",
    "/api/pair/code",
}


def require_token(request: Request) -> None:
    """Raise unless the caller presented the connector token."""
    expected = get_or_create_token()

    header = request.headers.get("authorization", "")
    supplied = ""
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()

    if not supplied:
        # <img> tags cannot set headers, so icon requests may pass ?t=
        supplied = request.query_params.get("t", "").strip()

    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="This connector is not paired with that site. Reconnect to continue.",
        )
