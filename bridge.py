"""
Organize My Phone - device bridge.

Everything that touches the phone lives behind the DeviceBridge interface.
Nothing else in the connector imports pymobiledevice3, so the implementation
can be swapped for a libimobiledevice-backed bridge without touching routes,
validation, or the editor.

The surface is deliberately tiny - six operations are all the product needs:

    is_available()      can we talk to the device stack at all
    device_info()       name / model / iOS version
    get_icon_state()    read the Home Screen
    set_icon_state()    write the Home Screen
    get_icon_png()      one app icon as PNG bytes

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from typing import Any


class BridgeError(RuntimeError):
    """Any failure talking to the device stack."""


class DeviceNotFound(BridgeError):
    """No trusted iPhone is reachable over USB."""


async def _maybe_await(value: Any) -> Any:
    """
    Accept both sync and async APIs.

    pymobiledevice3 has moved methods between sync and async across releases.
    Rather than pinning one exact version forever, tolerate both shapes.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class _Session:
    """
    Async context manager over a lockdown connection that works whether the
    underlying object is an async context manager, a sync one, or neither.
    """

    def __init__(self, factory):
        self._factory = factory
        self._obj = None
        self._mode = None

    async def __aenter__(self):
        obj = await _maybe_await(self._factory())
        if hasattr(obj, "__aenter__"):
            self._mode = "async"
            self._obj = await obj.__aenter__()
            self._raw = obj
        elif hasattr(obj, "__enter__"):
            self._mode = "sync"
            self._raw = obj
            self._obj = obj.__enter__()
        else:
            self._mode = "plain"
            self._raw = obj
            self._obj = obj
        return self._obj

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._mode == "async":
                return await self._raw.__aexit__(exc_type, exc, tb)
            if self._mode == "sync":
                return self._raw.__exit__(exc_type, exc, tb)
            close = getattr(self._raw, "close", None)
            if callable(close):
                await _maybe_await(close())
        except Exception:
            # A failure while closing should never mask the real error.
            pass
        return False


class DeviceBridge(ABC):
    """Interface every backend must satisfy."""

    name: str = "abstract"

    @abstractmethod
    async def device_info(self) -> dict[str, Any]:
        ...

    @abstractmethod
    async def get_icon_state(self) -> list[Any]:
        ...

    @abstractmethod
    async def set_icon_state(self, layout: list[Any]) -> None:
        ...

    @abstractmethod
    async def get_icon_png(self, bundle_id: str) -> bytes | None:
        ...

    async def list_user_apps(self) -> dict[str, dict[str, Any]]:
        """bundle id -> {"name": ...} for every app the user installed."""
        raise NotImplementedError(f"{self.name} bridge cannot list apps.")

    async def uninstall_app(self, bundle_id: str) -> None:
        """Delete an app AND its data from the phone. Irreversible."""
        raise NotImplementedError(f"{self.name} bridge cannot uninstall apps.")

    async def is_available(self) -> bool:
        try:
            info = await self.device_info()
            return bool(info.get("connected"))
        except Exception:
            return False


class PyMobileDevice3Bridge(DeviceBridge):
    """
    Reference implementation.

    pymobiledevice3 is GPL-3.0. This connector is distributed under GPL-3.0 as
    well, so bundling it here is compliant. See docs/LICENSING.md for why the
    editor UI is a separate, independently distributed work.
    """

    name = "pymobiledevice3"

    def __init__(self) -> None:
        # A single lock serialises all device access. lockdown sessions do not
        # like being opened concurrently, and an interleaved read during a
        # write is exactly how a layout gets corrupted.
        self._lock = asyncio.Lock()
        self._springboard_cls = None
        self._connect = None

    def _load(self):
        if self._connect is None:
            try:
                from pymobiledevice3.lockdown import create_using_usbmux
                from pymobiledevice3.services.springboard import SpringBoardServicesService
            except ImportError as exc:  # pragma: no cover
                raise BridgeError(
                    "pymobiledevice3 is not installed. Run: "
                    "python -m pip install -r requirements.txt"
                ) from exc
            self._connect = create_using_usbmux
            self._springboard_cls = SpringBoardServicesService
        return self._connect, self._springboard_cls

    def _session(self) -> _Session:
        connect, _ = self._load()
        return _Session(connect)

    async def device_info(self) -> dict[str, Any]:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    values = await _maybe_await(getattr(lockdown, "all_values", {}))
                    return {
                        "connected": True,
                        "name": values.get("DeviceName"),
                        "model": values.get("ProductType"),
                        "iosVersion": values.get("ProductVersion"),
                        "buildVersion": values.get("BuildVersion"),
                        "udidPrefix": str(values.get("UniqueDeviceID", ""))[:8] or None,
                    }
        except Exception as exc:
            return {"connected": False, "error": _friendly_error(exc)}

    async def get_icon_state(self) -> list[Any]:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    _, springboard_cls = self._load()
                    springboard = springboard_cls(lockdown=lockdown)
                    state = await _maybe_await(springboard.get_icon_state())
        except Exception as exc:
            raise BridgeError(_friendly_error(exc)) from exc

        if not isinstance(state, list) or not state:
            raise BridgeError("The phone returned an unreadable Home Screen state.")
        return state

    async def set_icon_state(self, layout: list[Any]) -> None:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    _, springboard_cls = self._load()
                    springboard = springboard_cls(lockdown=lockdown)
                    await _maybe_await(springboard.set_icon_state(layout))
        except Exception as exc:
            raise BridgeError(_friendly_error(exc)) from exc

    def _installation_proxy(self, lockdown):
        try:
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
        except ImportError as exc:  # pragma: no cover
            raise BridgeError("This pymobiledevice3 build has no installation service.") from exc
        return InstallationProxyService(lockdown=lockdown)

    async def list_user_apps(self) -> dict[str, dict[str, Any]]:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    proxy = self._installation_proxy(lockdown)
                    apps = await _maybe_await(proxy.get_apps(application_type="User"))
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(_friendly_error(exc)) from exc

        out: dict[str, dict[str, Any]] = {}
        for bundle, info in (apps or {}).items():
            info = info if isinstance(info, dict) else {}
            out[str(bundle)] = {
                "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or str(bundle),
            }
        return out

    async def uninstall_app(self, bundle_id: str) -> None:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    proxy = self._installation_proxy(lockdown)
                    await _maybe_await(proxy.uninstall(bundle_id))
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(_friendly_error(exc)) from exc

    async def get_icon_png(self, bundle_id: str) -> bytes | None:
        try:
            async with self._lock:
                async with self._session() as lockdown:
                    _, springboard_cls = self._load()
                    springboard = springboard_cls(lockdown=lockdown)
                    png = await _maybe_await(springboard.get_icon_pngdata(bundle_id))
            return png or None
        except Exception:
            # A missing icon is routine - plenty of bundle ids have none.
            return None


class LibiMobileDeviceBridge(DeviceBridge):
    """
    Placeholder for the production bridge.

    Implementation notes, so the next person does not have to rediscover them:

      * Link the LGPL-2.1 libimobiledevice / libplist / libusbmuxd shared
        libraries via ctypes. Link dynamically, never statically - that is what
        keeps the proprietary side of the product clean. Note the *tools* in
        those repositories are GPL; only the libraries are LGPL.
      * On Windows, do not ship usbmuxd. Apple's AppleMobileDeviceService,
        installed with Apple Devices, already exposes the usbmux socket, and
        libusbmuxd will talk to it.
      * The five calls needed are:
            idevice_new_with_options
            lockdownd_client_new_with_handshake
            lockdownd_start_service("com.apple.springboardservices")
            sbservices_get_icon_state / sbservices_set_icon_state
            sbservices_get_icon_pngdata
      * get_icon_state takes a format version string. Pass "2" to match what
        pymobiledevice3 returns, then diff the two outputs before switching.

    Validate against a real device before making this the default.
    """

    name = "libimobiledevice"

    async def device_info(self) -> dict[str, Any]:
        raise NotImplementedError("libimobiledevice bridge is not implemented yet.")

    async def get_icon_state(self) -> list[Any]:
        raise NotImplementedError("libimobiledevice bridge is not implemented yet.")

    async def set_icon_state(self, layout: list[Any]) -> None:
        raise NotImplementedError("libimobiledevice bridge is not implemented yet.")

    async def get_icon_png(self, bundle_id: str) -> bytes | None:
        raise NotImplementedError("libimobiledevice bridge is not implemented yet.")


def _friendly_error(exc: Exception) -> str:
    """Turn stack-trace noise into something a customer can act on."""
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()

    if "usbmuxd" in lowered or "connectionfailed" in exc.__class__.__name__.lower():
        return (
            "Apple's device service is not running on this PC. Install Apple Devices "
            "from the Microsoft Store (or restart it), replug the iPhone, and try again."
        )
    if "no device" in lowered or "notfound" in lowered or "muxexception" in lowered:
        return (
            "No iPhone detected. Connect it by USB, unlock it, and tap Trust "
            "if prompted. Apple Devices must be installed on this PC."
        )
    if "pair" in lowered or "trust" in lowered or "password" in lowered:
        return (
            "This PC is not trusted by the iPhone. Unlock the phone, unplug and "
            "replug the cable, then tap Trust."
        )
    if "lockdown" in lowered:
        return f"The phone refused the connection ({text})."
    return text


_BRIDGE: DeviceBridge | None = None


def get_bridge() -> DeviceBridge:
    """Single shared bridge instance. Swap the class here to change backends."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = PyMobileDevice3Bridge()
    return _BRIDGE
