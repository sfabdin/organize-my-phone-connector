"""
Organize My Phone - connector entry point.

This is what a user actually runs. Double-clicking the built executable calls
straight into main(): no Python, no terminal, no port arguments, no arguments
at all. Everything the developer workflow does by hand happens here instead.

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

import config


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _who_is_listening(port: int) -> dict | None:
    """If our own connector already owns the port, return its /api/hello."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("product") == config.PRODUCT_NAME else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _ensure_standard_streams() -> None:
    """
    A windowless Windows app has no stdout or stderr at all: both are None.
    Logging and the web server both write to them on start-up and crash on
    None, so give them somewhere harmless to write.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _record_failure(message: str) -> None:
    """Leave a trace on disk, since a windowless app cannot print one."""
    try:
        config.ensure_directories()
        with open(config.DATA_DIR / "startup-error.txt", "w", encoding="utf-8") as handle:
            handle.write(message + "\n\n" + traceback.format_exc())
    except OSError:
        pass


def _alert(title: str, message: str) -> None:
    """A dialog box, because a frozen app has no console to print to."""
    _record_failure(message)
    if os.environ.get("OMP_NO_BROWSER"):
        return  # automated run: nobody is there to dismiss a dialog
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except Exception:
        print(f"{title}: {message}")


def _open_browser_when_ready(port: int) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if _port_in_use(port):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return
        time.sleep(0.25)


def main() -> int:
    _ensure_standard_streams()
    port = config.DEFAULT_PORT

    # Already running? Don't fail with a socket error - just bring it forward.
    # This is the single most common way a user "breaks" a local service.
    if _port_in_use(port):
        if _who_is_listening(port):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return 0
        _alert(
            config.PRODUCT_NAME,
            f"Port {port} is being used by another program, so the connector "
            f"cannot start.\n\nClose whatever is using port {port} and try again.",
        )
        return 1

    config.ensure_directories()

    try:
        import uvicorn
        from app import app
    except Exception as exc:
        _alert(config.PRODUCT_NAME, f"The connector could not start.\n\n{exc}")
        return 1

    # OMP_NO_BROWSER lets an automated check start the app without a browser.
    if not os.environ.get("OMP_NO_BROWSER"):
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                       access_log=False, log_config=None)
    )
    app.state.server = server  # lets the editor's Quit button stop the app

    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _alert(config.PRODUCT_NAME, f"The connector stopped unexpectedly.\n\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
