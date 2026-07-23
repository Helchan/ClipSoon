"""Out-of-process Windows hotkey host backed only by ``RegisterHotKey``.

Windows owns the registration and posts ``WM_HOTKEY`` to this helper's message
queue.  There is deliberately no Raw Input device, low-level keyboard hook, or
sampled modifier state whose local health can diverge from the operating
system.  If the helper exits, the supervisor starts a fresh process and the
registration is acquired again.

The host is independent from Qt and pynput.  A frozen ``--windowed`` executable
can have ``sys.stdin`` and ``sys.stdout`` set to ``None``; in that case the
helper reads and writes the inherited standard pipe handles directly through
``ReadFile`` and ``WriteFile``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from clipsoon.core import WINDOWS_DEFAULT_HOTKEY

# Window messages and timer identifiers.
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_HOTKEY = 0x0312
WM_TIMER = 0x0113
HEARTBEAT_TIMER_ID = 0xC501
REGISTERED_HOTKEY_ID = 1

# RegisterHotKey modifier flags.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

DEFAULT_HEARTBEAT_INTERVAL_MS = 500
PROTOCOL_VERSION = 1
PROTOCOL_ROLE = "hotkey"
HOTKEY_MUTEX_NAME = r"Local\ClipSoon.GlobalHotkeyHost"

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_ERROR_BROKEN_PIPE = 109
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_FAILED = 0xFFFFFFFF

# Fixed-width aliases keep the Windows structures correct and importable on
# Unix, where ctypes.wintypes maps C ``long`` to the host's 64-bit long.
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_UINT = ctypes.c_uint32
_LONG = ctypes.c_int32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_HWND = ctypes.c_void_p
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t
_WNDPROC_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class _Point(ctypes.Structure):
    _fields_ = (("x", _LONG), ("y", _LONG))


class _Message(ctypes.Structure):
    _fields_ = (
        ("hwnd", _HWND),
        ("message", _UINT),
        ("wParam", _WPARAM),
        ("lParam", _LPARAM),
        ("time", _DWORD),
        ("pt", _Point),
        ("lPrivate", _DWORD),
    )


_WindowProcedure = _WNDPROC_FACTORY(_LRESULT, _HWND, _UINT, _WPARAM, _LPARAM)


class _WindowClass(ctypes.Structure):
    _fields_ = (
        ("style", _UINT),
        ("lpfnWndProc", _WindowProcedure),
        ("cbClsExtra", ctypes.c_int32),
        ("cbWndExtra", ctypes.c_int32),
        ("hInstance", _HANDLE),
        ("hIcon", _HANDLE),
        ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    )


class _LineWriter(Protocol):
    def write_line(self, line: str) -> None: ...


class _LineReader(Protocol):
    def readline(self) -> str: ...


class _TextLineWriter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write_line(self, line: str) -> None:
        self._stream.write(line)
        self._stream.flush()


class JsonLineEmitter:
    """Thread-safe NDJSON emitter with per-session, strictly increasing IDs."""

    def __init__(self, output: TextIO | _LineWriter, session_id: str | None = None) -> None:
        self._output = output if hasattr(output, "write_line") else _TextLineWriter(output)  # type: ignore[arg-type]
        self.session_id = session_id or uuid.uuid4().hex
        self._event_id = 0
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: object) -> dict[str, object]:
        with self._lock:
            self._event_id += 1
            message: dict[str, object] = {
                "type": event_type,
                "protocol": PROTOCOL_VERSION,
                "role": PROTOCOL_ROLE,
                "session_id": self.session_id,
                "event_id": self._event_id,
            }
            if {"type", "protocol", "role", "session_id", "event_id"} & payload.keys():
                raise ValueError("协议 envelope 字段不可被 payload 覆盖")
            message.update(payload)
            line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            self._output.write_line(line)  # type: ignore[union-attr]
            return message


class NativeHotkeyEngine:
    """Protocol layer for ready, heartbeat, and registered-hotkey events."""

    def __init__(
        self,
        emitter: JsonLineEmitter,
        *,
        hotkey: str = WINDOWS_DEFAULT_HOTKEY,
        clock: Callable[[], float] = time.monotonic,
        process_id: int | None = None,
        session_id: str | None = None,
        activation_context: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.emitter = emitter
        if session_id is not None:
            self.emitter.session_id = session_id
        self.hotkey = hotkey
        self._clock = clock
        self._process_id = os.getpid() if process_id is None else process_id
        self._activation_context = activation_context

    @staticmethod
    def _milliseconds(value: float) -> int:
        return int(round(value * 1_000))

    def ready(self) -> None:
        self.emitter.emit(
            "ready",
            pid=self._process_id,
            hotkey=self.hotkey,
            monotonic_ms=self._milliseconds(self._clock()),
        )

    def heartbeat(self) -> None:
        now = self._clock()
        self.emitter.emit("heartbeat", monotonic_ms=self._milliseconds(now))

    def activate(self, at: float | None = None) -> None:
        self._activated(self._clock() if at is None else at)

    def _activated(self, at: float) -> None:
        context = dict(self._activation_context()) if self._activation_context is not None else {}
        self.emitter.emit(
            "hotkey",
            hotkey=self.hotkey,
            monotonic_ms=self._milliseconds(at),
            **context,
        )


def is_shutdown_command(line: str) -> bool:
    value = line.strip()
    if value.casefold() == "shutdown":
        return True
    if not value.startswith("{"):
        return False
    try:
        message = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(message, dict):
        return False
    return str(message.get("type") or message.get("command") or "").casefold() == "shutdown"


@dataclass(frozen=True, slots=True)
class RegisteredHotkey:
    modifiers: int
    virtual_key: int


_NAMED_VIRTUAL_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "plus": 0xBB,
}

_PUNCTUATION_VIRTUAL_KEYS = {
    ";": (0xBA, 0),
    ":": (0xBA, MOD_SHIFT),
    "=": (0xBB, 0),
    ",": (0xBC, 0),
    "<": (0xBC, MOD_SHIFT),
    "-": (0xBD, 0),
    "_": (0xBD, MOD_SHIFT),
    ".": (0xBE, 0),
    ">": (0xBE, MOD_SHIFT),
    "/": (0xBF, 0),
    "?": (0xBF, MOD_SHIFT),
    "`": (0xC0, 0),
    "~": (0xC0, MOD_SHIFT),
    "[": (0xDB, 0),
    "{": (0xDB, MOD_SHIFT),
    "\\": (0xDC, 0),
    "|": (0xDC, MOD_SHIFT),
    "]": (0xDD, 0),
    "}": (0xDD, MOD_SHIFT),
    "'": (0xDE, 0),
    '"': (0xDE, MOD_SHIFT),
}


def parse_registered_hotkey(spec: str) -> RegisteredHotkey:
    if not spec.startswith("combo:"):
        raise ValueError(f"不是组合快捷键：{spec}")
    tokens = [token.strip().casefold() for token in spec.removeprefix("combo:").split("+") if token.strip()]
    modifiers = MOD_NOREPEAT
    key_tokens: list[str] = []
    for token in tokens:
        if token in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif token == "shift":
            modifiers |= MOD_SHIFT
        elif token == "alt":
            modifiers |= MOD_ALT
        elif token in {"meta", "win", "windows", "cmd", "command"}:
            modifiers |= MOD_WIN
        else:
            key_tokens.append(token)
    if len(key_tokens) != 1 or modifiers == MOD_NOREPEAT:
        raise ValueError(f"无效的组合快捷键：{spec}")
    token = key_tokens[0]
    if len(token) == 1 and token.isascii() and token.isalnum():
        virtual_key = ord(token.upper())
    elif token in _PUNCTUATION_VIRTUAL_KEYS:
        virtual_key, implied_modifiers = _PUNCTUATION_VIRTUAL_KEYS[token]
        modifiers |= implied_modifiers
    elif token.startswith("f") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
        virtual_key = 0x70 + int(token[1:]) - 1
    else:
        virtual_key = _NAMED_VIRTUAL_KEYS.get(token, 0)
        if token == "plus":
            modifiers |= MOD_SHIFT
    if not virtual_key:
        raise ValueError(f"不支持的组合快捷键按键：{token}")
    return RegisteredHotkey(modifiers, virtual_key)


class _WindowsApiProtocol(Protocol):
    def acquire_mutex(self, name: str) -> bool: ...

    def wait_for_process_exit(self, process_id: int) -> bool: ...

    def foreground_window(self) -> int: ...

    def allow_set_foreground_window(self, process_id: int) -> bool: ...

    def create_message_window(self, wndproc: Callable[[int, int, int, int], int]) -> int: ...

    def register_hotkey(self, hwnd: int, hotkey_id: int, modifiers: int, virtual_key: int) -> None: ...

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> None: ...

    def set_timer(self, hwnd: int, timer_id: int, interval_ms: int) -> None: ...

    def message_loop(self) -> int: ...

    def def_window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int: ...

    def destroy_window(self, hwnd: int) -> None: ...

    def post_quit(self) -> None: ...

    def post_message(self, hwnd: int, message: int) -> None: ...

    def close(self) -> None: ...


class _Win32Api:
    """Narrow ctypes wrapper; construction is deferred until Windows runtime."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows 原生快捷键宿主仅可在 Windows 上启动")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()
        self._class_name = f"ClipSoonHotkeyHost-{os.getpid()}-{id(self):x}"
        self._instance = self.kernel32.GetModuleHandleW(None)
        self._window_procedure = None
        self._hwnd = 0
        self._class_registered = False
        self._mutex_handle = 0

    @staticmethod
    def _error(operation: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{operation}: {ctypes.FormatError(code)}")

    def _configure_signatures(self) -> None:
        self.kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
        self.kernel32.GetModuleHandleW.restype = _HANDLE
        self.kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, _BOOL, ctypes.c_wchar_p)
        self.kernel32.CreateMutexW.restype = _HANDLE
        self.kernel32.OpenProcess.argtypes = (_DWORD, _BOOL, _DWORD)
        self.kernel32.OpenProcess.restype = _HANDLE
        self.kernel32.WaitForSingleObject.argtypes = (_HANDLE, _DWORD)
        self.kernel32.WaitForSingleObject.restype = _DWORD
        self.kernel32.CloseHandle.argtypes = (_HANDLE,)
        self.kernel32.CloseHandle.restype = _BOOL

        self.user32.RegisterClassW.argtypes = (ctypes.POINTER(_WindowClass),)
        self.user32.RegisterClassW.restype = _WORD
        self.user32.UnregisterClassW.argtypes = (ctypes.c_wchar_p, _HANDLE)
        self.user32.UnregisterClassW.restype = _BOOL
        self.user32.CreateWindowExW.argtypes = (
            _DWORD,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            _DWORD,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            _HWND,
            _HANDLE,
            _HANDLE,
            ctypes.c_void_p,
        )
        self.user32.CreateWindowExW.restype = _HWND
        self.user32.DestroyWindow.argtypes = (_HWND,)
        self.user32.DestroyWindow.restype = _BOOL
        self.user32.IsWindow.argtypes = (_HWND,)
        self.user32.IsWindow.restype = _BOOL
        self.user32.DefWindowProcW.argtypes = (_HWND, _UINT, _WPARAM, _LPARAM)
        self.user32.DefWindowProcW.restype = _LRESULT
        self.user32.PostMessageW.argtypes = (_HWND, _UINT, _WPARAM, _LPARAM)
        self.user32.PostMessageW.restype = _BOOL
        self.user32.PostQuitMessage.argtypes = (ctypes.c_int32,)
        self.user32.GetMessageW.argtypes = (ctypes.POINTER(_Message), _HWND, _UINT, _UINT)
        self.user32.GetMessageW.restype = _BOOL
        self.user32.TranslateMessage.argtypes = (ctypes.POINTER(_Message),)
        self.user32.TranslateMessage.restype = _BOOL
        self.user32.DispatchMessageW.argtypes = (ctypes.POINTER(_Message),)
        self.user32.DispatchMessageW.restype = _LRESULT
        self.user32.RegisterHotKey.argtypes = (_HWND, ctypes.c_int32, _UINT, _UINT)
        self.user32.RegisterHotKey.restype = _BOOL
        self.user32.UnregisterHotKey.argtypes = (_HWND, ctypes.c_int32)
        self.user32.UnregisterHotKey.restype = _BOOL
        self.user32.SetTimer.argtypes = (_HWND, _WPARAM, _UINT, ctypes.c_void_p)
        self.user32.SetTimer.restype = _WPARAM
        self.user32.GetForegroundWindow.argtypes = ()
        self.user32.GetForegroundWindow.restype = _HWND
        self.user32.AllowSetForegroundWindow.argtypes = (_DWORD,)
        self.user32.AllowSetForegroundWindow.restype = _BOOL

    @staticmethod
    def _as_hwnd(hwnd: int) -> _HWND:
        return _HWND(hwnd)

    def acquire_mutex(self, name: str) -> bool:
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise self._error("CreateMutexW")
        already_exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        if already_exists:
            self.kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = int(handle)
        return True

    def wait_for_process_exit(self, process_id: int) -> bool:
        handle = self.kernel32.OpenProcess(_SYNCHRONIZE, False, process_id)
        if not handle:
            raise self._error("OpenProcess")
        try:
            result = int(self.kernel32.WaitForSingleObject(handle, _INFINITE))
            if result == _WAIT_FAILED:
                raise self._error("WaitForSingleObject")
            return result == _WAIT_OBJECT_0
        finally:
            self.kernel32.CloseHandle(handle)

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def allow_set_foreground_window(self, process_id: int) -> bool:
        return bool(self.user32.AllowSetForegroundWindow(_DWORD(process_id)))

    def create_message_window(self, wndproc: Callable[[int, int, int, int], int]) -> int:
        self._window_procedure = _WindowProcedure(wndproc)
        window_class = _WindowClass(
            0,
            self._window_procedure,
            0,
            0,
            self._instance,
            None,
            None,
            None,
            None,
            self._class_name,
        )
        if not self.user32.RegisterClassW(ctypes.byref(window_class)):
            raise self._error("RegisterClassW")
        self._class_registered = True
        hwnd_message = _HWND(-3)
        value = self.user32.CreateWindowExW(
            0,
            self._class_name,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            hwnd_message,
            None,
            self._instance,
            None,
        )
        if not value:
            raise self._error("CreateWindowExW")
        self._hwnd = int(value)
        return self._hwnd

    def register_hotkey(self, hwnd: int, hotkey_id: int, modifiers: int, virtual_key: int) -> None:
        if not self.user32.RegisterHotKey(self._as_hwnd(hwnd), hotkey_id, modifiers, virtual_key):
            raise self._error("RegisterHotKey")

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> None:
        self.user32.UnregisterHotKey(self._as_hwnd(hwnd), hotkey_id)

    def set_timer(self, hwnd: int, timer_id: int, interval_ms: int) -> None:
        if not self.user32.SetTimer(self._as_hwnd(hwnd), timer_id, interval_ms, None):
            raise self._error("SetTimer")

    def message_loop(self) -> int:
        message = _Message()
        while True:
            result = int(self.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
            if result == 0:
                return 0
            if result < 0:
                raise self._error("GetMessageW")
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))

    def def_window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        return int(self.user32.DefWindowProcW(self._as_hwnd(hwnd), message, wparam, lparam))

    def destroy_window(self, hwnd: int) -> None:
        self.user32.DestroyWindow(self._as_hwnd(hwnd))

    def post_quit(self) -> None:
        self.user32.PostQuitMessage(0)

    def post_message(self, hwnd: int, message: int) -> None:
        self.user32.PostMessageW(self._as_hwnd(hwnd), message, 0, 0)

    def close(self) -> None:
        if self._hwnd and self.user32.IsWindow(self._as_hwnd(self._hwnd)):
            self.user32.DestroyWindow(self._as_hwnd(self._hwnd))
        self._hwnd = 0
        if self._class_registered:
            self.user32.UnregisterClassW(self._class_name, self._instance)
            self._class_registered = False
        if self._mutex_handle:
            self.kernel32.CloseHandle(_HANDLE(self._mutex_handle))
            self._mutex_handle = 0
        self._window_procedure = None


class _Win32StandardHandle:
    """Shared kernel32 setup for frozen ``--windowed`` pipe I/O."""

    def __init__(self, standard_handle: int) -> None:
        if sys.platform != "win32":
            raise OSError("Win32 标准句柄仅可在 Windows 上使用")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetStdHandle.argtypes = (_DWORD,)
        self._kernel32.GetStdHandle.restype = _HANDLE
        self._kernel32.ReadFile.argtypes = (
            _HANDLE,
            ctypes.c_void_p,
            _DWORD,
            ctypes.POINTER(_DWORD),
            ctypes.c_void_p,
        )
        self._kernel32.ReadFile.restype = _BOOL
        self._kernel32.WriteFile.argtypes = (
            _HANDLE,
            ctypes.c_void_p,
            _DWORD,
            ctypes.POINTER(_DWORD),
            ctypes.c_void_p,
        )
        self._kernel32.WriteFile.restype = _BOOL
        self.handle = self._kernel32.GetStdHandle(_DWORD(standard_handle & 0xFFFFFFFF))
        value = int(self.handle or 0)
        if not value or value == _INVALID_HANDLE_VALUE:
            raise OSError(f"标准句柄不可用：{standard_handle}")


class Win32PipeWriter(_Win32StandardHandle):
    def __init__(self) -> None:
        super().__init__(_STD_OUTPUT_HANDLE)

    def write_line(self, line: str) -> None:
        remaining = memoryview(line.encode("utf-8"))
        while remaining:
            data = (ctypes.c_char * len(remaining)).from_buffer_copy(remaining)
            written = _DWORD()
            if not self._kernel32.WriteFile(self.handle, data, len(remaining), ctypes.byref(written), None):
                code = ctypes.get_last_error()
                raise BrokenPipeError(code, ctypes.FormatError(code))
            if not written.value:
                raise BrokenPipeError("WriteFile 未写入数据")
            remaining = remaining[written.value :]


class Win32PipeReader(_Win32StandardHandle):
    def __init__(self) -> None:
        super().__init__(_STD_INPUT_HANDLE)
        self._buffer = bytearray()

    def readline(self) -> str:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                value = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return value.decode("utf-8", errors="replace")
            chunk = ctypes.create_string_buffer(4096)
            received = _DWORD()
            if not self._kernel32.ReadFile(self.handle, chunk, len(chunk), ctypes.byref(received), None):
                code = ctypes.get_last_error()
                if code == _ERROR_BROKEN_PIPE:
                    value = bytes(self._buffer)
                    self._buffer.clear()
                    return value.decode("utf-8", errors="replace")
                raise OSError(code, ctypes.FormatError(code))
            if not received.value:
                value = bytes(self._buffer)
                self._buffer.clear()
                return value.decode("utf-8", errors="replace")
            self._buffer.extend(chunk.raw[: received.value])


class WindowsHotkeyHost:
    """Hidden-window host with injectable Win32 and pipe boundaries."""

    def __init__(
        self,
        *,
        hotkey: str = WINDOWS_DEFAULT_HOTKEY,
        heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS,
        output: TextIO | _LineWriter | None = None,
        control_input: TextIO | _LineReader | None = None,
        api: _WindowsApiProtocol | None = None,
        clock: Callable[[], float] = time.monotonic,
        process_id: int | None = None,
        session_id: str | None = None,
        parent_pid: int | None = None,
        hard_exit: Callable[[int], None] = os._exit,
    ) -> None:
        if output is None:
            output = sys.stdout if sys.stdout is not None else Win32PipeWriter()
        self._api = api or _Win32Api()
        self._control_input = control_input
        self._heartbeat_interval_ms = max(100, heartbeat_interval_ms)
        self._parent_pid = parent_pid
        self._engine = NativeHotkeyEngine(
            JsonLineEmitter(output, session_id),
            hotkey=hotkey,
            clock=clock,
            process_id=process_id,
            session_id=session_id,
            activation_context=self._activation_context,
        )
        self._clock = clock
        self._hard_exit = hard_exit
        self._registered_combo: RegisteredHotkey | None = None
        self._configuration_error = ""
        try:
            if not hotkey.startswith("combo:"):
                raise ValueError(
                    f"Windows 仅支持由系统注册的组合快捷键，不支持：{hotkey}"
                )
            self._registered_combo = parse_registered_hotkey(hotkey)
        except ValueError as exc:
            self._configuration_error = str(exc)
        self._hwnd = 0
        self._control_thread: threading.Thread | None = None
        self._parent_thread: threading.Thread | None = None
        self._combo_is_registered = False

    def run(self) -> int:
        try:
            if self._configuration_error:
                self._engine.emitter.emit(
                    "error",
                    fatal=True,
                    code="invalid_hotkey",
                    message=self._configuration_error,
                )
                return 5
            if not self._api.acquire_mutex(HOTKEY_MUTEX_NAME):
                self._engine.emitter.emit("error", fatal=True, code="already_active")
                return 3
            self._hwnd = self._api.create_message_window(self._window_proc)
            assert self._registered_combo is not None
            try:
                self._api.register_hotkey(
                    self._hwnd,
                    REGISTERED_HOTKEY_ID,
                    self._registered_combo.modifiers,
                    self._registered_combo.virtual_key,
                )
            except OSError as exc:
                self._engine.emitter.emit(
                    "error",
                    fatal=True,
                    code="registration_failed",
                    message=f"无法注册全局快捷键：{exc}",
                )
                return 4
            self._combo_is_registered = True
            self._api.set_timer(self._hwnd, HEARTBEAT_TIMER_ID, self._heartbeat_interval_ms)
            self._engine.ready()
            self._start_control_reader()
            self._start_parent_monitor()
            return self._api.message_loop()
        except OSError as exc:
            with suppress(BrokenPipeError, OSError, ValueError):
                self._engine.emitter.emit(
                    "error",
                    fatal=True,
                    code="startup_failed",
                    message=f"Windows 热键宿主启动失败：{exc}",
                )
            return 6
        finally:
            if self._combo_is_registered and self._hwnd:
                self._api.unregister_hotkey(self._hwnd, REGISTERED_HOTKEY_ID)
                self._combo_is_registered = False
            self._api.close()

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        hwnd_value = int(hwnd or 0)
        if message == WM_HOTKEY and int(wparam) == REGISTERED_HOTKEY_ID:
            try:
                self._engine.activate(self._clock())
            except (BrokenPipeError, OSError):
                self._api.post_message(hwnd_value, WM_CLOSE)
            return 0
        if message == WM_TIMER and int(wparam) == HEARTBEAT_TIMER_ID:
            try:
                self._engine.heartbeat()
            except (BrokenPipeError, OSError):
                self._api.post_message(hwnd_value, WM_CLOSE)
            return 0
        if message == WM_CLOSE:
            self._api.destroy_window(hwnd_value)
            return 0
        if message == WM_DESTROY:
            self._api.post_quit()
            return 0
        return self._api.def_window_proc(hwnd_value, message, int(wparam), int(lparam))

    def _start_control_reader(self) -> None:
        stream = self._control_input
        if stream is None:
            return
        self._control_thread = threading.Thread(
            target=self._watch_control_stream,
            args=(stream,),
            name="ClipSoon-hotkey-control",
            daemon=True,
        )
        self._control_thread.start()

    def _start_parent_monitor(self) -> None:
        if self._parent_pid is None:
            return
        self._parent_thread = threading.Thread(
            target=self._watch_parent_process,
            name="ClipSoon-hotkey-parent",
            daemon=True,
        )
        self._parent_thread.start()

    def _watch_control_stream(self, stream: TextIO | _LineReader) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    self._hard_exit(0)
                    return
                if is_shutdown_command(line):
                    if self._hwnd:
                        self._api.post_message(self._hwnd, WM_CLOSE)
                    return
        except (BrokenPipeError, OSError, ValueError):
            self._hard_exit(0)

    def _activation_context(self) -> dict[str, object]:
        try:
            target_window = self._api.foreground_window()
        except OSError:
            target_window = 0
        foreground_granted = False
        if self._parent_pid is not None:
            try:
                foreground_granted = self._api.allow_set_foreground_window(self._parent_pid)
            except OSError:
                foreground_granted = False
        return {
            "target_hwnd": target_window or None,
            "foreground_granted": foreground_granted,
        }

    def _watch_parent_process(self) -> None:
        if self._parent_pid is None:
            return
        try:
            parent_exited = self._api.wait_for_process_exit(self._parent_pid)
        except OSError:
            # Failure to open the advertised parent means it is already gone
            # or cannot be trusted as this helper's lifetime owner.
            parent_exited = True
        if parent_exited:
            self._hard_exit(0)


def _default_control_input() -> TextIO | _LineReader:
    return sys.stdin if sys.stdin is not None else Win32PipeReader()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClipSoon Windows native hotkey helper")
    parser.add_argument("--hotkey", default=WINDOWS_DEFAULT_HOTKEY)
    parser.add_argument("--heartbeat-ms", type=int, default=DEFAULT_HEARTBEAT_INTERVAL_MS)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--parent-pid", type=int, default=None)
    # The frozen executable dispatcher consumes this option; accepting it here
    # also keeps direct module tests and manual launches symmetric.
    parser.add_argument("--windows-helper", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        return 2
    arguments, _unknown = _argument_parser().parse_known_args(argv)
    try:
        host = WindowsHotkeyHost(
            hotkey=arguments.hotkey,
            heartbeat_interval_ms=arguments.heartbeat_ms,
            control_input=_default_control_input(),
            session_id=arguments.session_id,
            parent_pid=arguments.parent_pid,
        )
        return host.run()
    except (BrokenPipeError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
