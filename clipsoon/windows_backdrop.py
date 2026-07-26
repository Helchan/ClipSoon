"""Supported Windows DWM backdrop integration for top-level Qt windows.

This module deliberately uses the documented system-backdrop API instead of
undocumented ACCENT_POLICY / SetWindowCompositionAttribute variants.  It never
changes Qt window flags or enables a layered window.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

_MIN_SYSTEM_BACKDROP_BUILD = 22_621
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
_DWMSBT_TRANSIENTWINDOW = 3

_HWND = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_HRESULT = ctypes.c_int32
_BACKDROP = ctypes.c_int32


def _normalized_hresult(value: object) -> int:
    """Return an HRESULT as a signed 32-bit integer on every Python host."""
    raw = value.value if hasattr(value, "value") else value
    return _HRESULT(int(raw)).value


def _hresult_succeeded(value: object) -> bool:
    return _normalized_hresult(value) >= 0


def _current_windows_build() -> int:
    if sys.platform != "win32":
        return 0
    get_version = getattr(sys, "getwindowsversion", None)
    if get_version is None:
        return 0
    try:
        return int(get_version().build)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class BackdropResult:
    """Outcome of a best-effort native backdrop request."""

    applied: bool
    reason: str
    hresult: int | None = None


class NativeDwmApi:
    """Small lazy ctypes facade so non-Windows imports never load dwmapi.dll."""

    def __init__(self, library_loader: Callable[[], object] | None = None) -> None:
        self._library_loader = library_loader or self._load_library
        self._library: object | None = None

    @staticmethod
    def _load_library() -> object:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("ctypes.WinDLL is unavailable on this host")
        return loader("dwmapi")

    def _function(self, name: str):
        if self._library is None:
            self._library = self._library_loader()
        function = getattr(self._library, name)
        try:
            function.argtypes = (_HWND, _DWORD, ctypes.c_void_p, _DWORD)
            function.restype = _HRESULT
        except (AttributeError, TypeError):
            # Test doubles and a few ctypes-compatible adapters do not expose
            # configurable function metadata; their call contract is identical.
            pass
        return function

    def set_system_backdrop(self, hwnd: int, backdrop: int) -> int:
        value = _BACKDROP(backdrop)
        result = self._function("DwmSetWindowAttribute")(
            _HWND(hwnd),
            _DWORD(_DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value),
            _DWORD(ctypes.sizeof(value)),
        )
        return _normalized_hresult(result)

    def get_system_backdrop(self, hwnd: int) -> tuple[int, int | None]:
        value = _BACKDROP()
        result = self._function("DwmGetWindowAttribute")(
            _HWND(hwnd),
            _DWORD(_DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value),
            _DWORD(ctypes.sizeof(value)),
        )
        hresult = _normalized_hresult(result)
        return hresult, int(value.value) if _hresult_succeeded(hresult) else None


class WindowsBackdropController:
    """Apply Desktop Acrylic only when the documented DWM API supports it."""

    def __init__(
        self,
        api: NativeDwmApi | None = None,
        *,
        platform: str | None = None,
        build_number: Callable[[], int] | None = None,
    ) -> None:
        self._api = api or NativeDwmApi()
        self._platform = sys.platform if platform is None else platform
        self._build_number = build_number or _current_windows_build

    def apply_transient(self, hwnd: int) -> BackdropResult:
        """Request and verify the Desktop Acrylic backdrop for a floating panel."""
        if hwnd <= 0:
            return BackdropResult(False, "invalid-hwnd")
        if self._platform != "win32" or self._build_number() < _MIN_SYSTEM_BACKDROP_BUILD:
            return BackdropResult(False, "unsupported")
        try:
            set_result = self._api.set_system_backdrop(hwnd, _DWMSBT_TRANSIENTWINDOW)
            if not _hresult_succeeded(set_result):
                LOGGER.debug("DWM Desktop Acrylic request failed; HRESULT=0x%08X", set_result & 0xFFFFFFFF)
                return BackdropResult(False, "set-failed", set_result)
            get_result, actual = self._api.get_system_backdrop(hwnd)
            if _hresult_succeeded(get_result) and actual == _DWMSBT_TRANSIENTWINDOW:
                return BackdropResult(True, "applied", set_result)
            LOGGER.debug(
                "DWM Desktop Acrylic request could not be verified; HRESULT=0x%08X, value=%r",
                get_result & 0xFFFFFFFF,
                actual,
            )
            self._clear_best_effort(hwnd)
            return BackdropResult(False, "verification-failed", get_result)
        except Exception:
            LOGGER.debug("DWM Desktop Acrylic is unavailable", exc_info=True)
            return BackdropResult(False, "api-unavailable")

    def clear(self, hwnd: int) -> BackdropResult:
        """Restore the normal DWM backdrop when leaving the liquid-glass theme."""
        if hwnd <= 0:
            return BackdropResult(False, "invalid-hwnd")
        if self._platform != "win32":
            return BackdropResult(False, "unsupported")
        try:
            result = self._api.set_system_backdrop(hwnd, _DWMSBT_NONE)
            if _hresult_succeeded(result):
                return BackdropResult(True, "cleared", result)
            LOGGER.debug("DWM backdrop reset failed; HRESULT=0x%08X", result & 0xFFFFFFFF)
            return BackdropResult(False, "clear-failed", result)
        except Exception:
            LOGGER.debug("DWM backdrop reset is unavailable", exc_info=True)
            return BackdropResult(False, "api-unavailable")

    def _clear_best_effort(self, hwnd: int) -> None:
        try:
            self._api.set_system_backdrop(hwnd, _DWMSBT_NONE)
        except Exception:
            LOGGER.debug("Could not reset unverified DWM backdrop", exc_info=True)
