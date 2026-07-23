"""Shared Win32 SendInput implementation and frozen-package smoke entrypoint."""

from __future__ import annotations

import ctypes
import logging
import sys

LOGGER = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_INPUT_FAILED = 69
PASTE_HELPER_TIMEOUT_SECONDS = 1.5

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_CONTROL = 0x11
_VK_V = 0x56

_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t
_WORD = ctypes.c_uint16


class _WindowsMouseInput(ctypes.Structure):
    _fields_ = (
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _WindowsKeyboardInput(ctypes.Structure):
    _fields_ = (
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _WindowsHardwareInput(ctypes.Structure):
    _fields_ = (
        ("uMsg", _DWORD),
        ("wParamL", _WORD),
        ("wParamH", _WORD),
    )


class _WindowsInputPayload(ctypes.Union):
    _fields_ = (
        ("mouse", _WindowsMouseInput),
        ("keyboard", _WindowsKeyboardInput),
        ("hardware", _WindowsHardwareInput),
    )


class _WindowsInput(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = (
        ("type", _DWORD),
        ("payload", _WindowsInputPayload),
    )


def _key_input(virtual_key: int, flags: int = 0) -> _WindowsInput:
    event = _WindowsInput()
    event.type = _INPUT_KEYBOARD
    event.keyboard = _WindowsKeyboardInput(
        wVk=virtual_key,
        wScan=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return event


def _set_last_error(kernel32: object | None, value: int) -> None:
    if kernel32 is None:
        return
    try:
        set_last_error = kernel32.SetLastError
        set_last_error.argtypes = (_DWORD,)
        set_last_error.restype = None
        set_last_error(_DWORD(value))
    except Exception:
        pass


def _last_error(kernel32: object | None) -> int:
    if kernel32 is None:
        return 0
    try:
        get_last_error = kernel32.GetLastError
        get_last_error.argtypes = ()
        get_last_error.restype = _DWORD
        return int(get_last_error())
    except Exception:
        return 0


def send_windows_paste_input(
    user32: object,
    kernel32: object | None = None,
) -> bool:
    """Inject exactly Ctrl-down, V-down, V-up and Ctrl-up as one batch."""

    send_input = user32.SendInput
    send_input.argtypes = (
        _DWORD,
        ctypes.POINTER(_WindowsInput),
        ctypes.c_int32,
    )
    send_input.restype = _DWORD
    events = (_WindowsInput * 4)(
        _key_input(_VK_CONTROL),
        _key_input(_VK_V),
        _key_input(_VK_V, _KEYEVENTF_KEYUP),
        _key_input(_VK_CONTROL, _KEYEVENTF_KEYUP),
    )
    _set_last_error(kernel32, 0)
    sent = int(send_input(len(events), events, ctypes.sizeof(_WindowsInput)))
    if sent == len(events):
        LOGGER.info("Windows paste input injected; events=%d", sent)
        return True

    error = _last_error(kernel32)
    LOGGER.warning(
        "Windows paste input rejected or incomplete; events=%d/%d error=%d",
        sent,
        len(events),
        error,
    )
    if sent:
        # A partial insertion could leave Ctrl or V pressed. Best-effort
        # releases prevent a failed paste from poisoning subsequent input.
        releases = (_WindowsInput * 2)(
            _key_input(_VK_V, _KEYEVENTF_KEYUP),
            _key_input(_VK_CONTROL, _KEYEVENTF_KEYUP),
        )
        send_input(len(releases), releases, ctypes.sizeof(_WindowsInput))
    return False


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32" or (argv is not None and argv):
        return EXIT_USAGE
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return (
            EXIT_OK
            if send_windows_paste_input(user32, kernel32)
            else EXIT_INPUT_FAILED
        )
    except Exception:
        LOGGER.exception("Windows paste helper failed")
        return EXIT_INPUT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
