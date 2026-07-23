"""One-shot Win32 foreground/focus helper.

``AttachThreadInput`` joins the calling thread's input queue to another GUI
thread.  This helper deliberately performs that operation in a short-lived
process so the operating system tears down every attachment when the process
exits, even if an explicit detach reports failure.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_STALE_IDENTITY = 65
EXIT_ATTACH_FAILED = 66
EXIT_ACTIVATION_FAILED = 67
EXIT_DETACH_FAILED = 68
EXIT_NATIVE_ERROR = 69
FOCUS_HELPER_TIMEOUT_SECONDS = 1.5

_GA_ROOT = 2
_PM_NOREMOVE = 0x0000
_SW_RESTORE = 9

_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_UINT = ctypes.c_uint32
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_HWND = ctypes.c_void_p


class _Point(ctypes.Structure):
    _fields_ = (("x", _LONG), ("y", _LONG))


class _Rect(ctypes.Structure):
    _fields_ = (
        ("left", _LONG),
        ("top", _LONG),
        ("right", _LONG),
        ("bottom", _LONG),
    )


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


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", _DWORD),
        ("flags", _DWORD),
        ("hwndActive", _HWND),
        ("hwndFocus", _HWND),
        ("hwndCapture", _HWND),
        ("hwndMenuOwner", _HWND),
        ("hwndMoveSize", _HWND),
        ("hwndCaret", _HWND),
        ("rcCaret", _Rect),
    )


@dataclass(frozen=True, slots=True)
class TargetActivationRequest:
    target_window: int
    target_thread_id: int
    target_process_id: int
    focus_window: int | None = None
    focus_thread_id: int | None = None
    focus_process_id: int | None = None


@dataclass(frozen=True, slots=True)
class PanelActivationRequest:
    panel_window: int
    panel_process_id: int


class NativeWindowsFocusApi:
    """Small, injectable Win32 boundary used by the one-shot helper."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows focus helper is only available on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.user32.IsWindow.argtypes = (_HWND,)
        self.user32.IsWindow.restype = _BOOL
        self.user32.IsWindowVisible.argtypes = (_HWND,)
        self.user32.IsWindowVisible.restype = _BOOL
        self.user32.IsIconic.argtypes = (_HWND,)
        self.user32.IsIconic.restype = _BOOL
        self.user32.ShowWindow.argtypes = (_HWND, ctypes.c_int32)
        self.user32.ShowWindow.restype = _BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            _HWND,
            ctypes.POINTER(_DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = _DWORD
        self.user32.GetAncestor.argtypes = (_HWND, _UINT)
        self.user32.GetAncestor.restype = _HWND
        self.user32.GetForegroundWindow.argtypes = ()
        self.user32.GetForegroundWindow.restype = _HWND
        self.user32.GetGUIThreadInfo.argtypes = (
            _DWORD,
            ctypes.POINTER(_GuiThreadInfo),
        )
        self.user32.GetGUIThreadInfo.restype = _BOOL
        self.user32.PeekMessageW.argtypes = (
            ctypes.POINTER(_Message),
            _HWND,
            _UINT,
            _UINT,
            _UINT,
        )
        self.user32.PeekMessageW.restype = _BOOL
        self.user32.AttachThreadInput.argtypes = (_DWORD, _DWORD, _BOOL)
        self.user32.AttachThreadInput.restype = _BOOL
        self.user32.BringWindowToTop.argtypes = (_HWND,)
        self.user32.BringWindowToTop.restype = _BOOL
        self.user32.SetForegroundWindow.argtypes = (_HWND,)
        self.user32.SetForegroundWindow.restype = _BOOL
        self.user32.SetFocus.argtypes = (_HWND,)
        self.user32.SetFocus.restype = _HWND
        self.kernel32.GetCurrentThreadId.argtypes = ()
        self.kernel32.GetCurrentThreadId.restype = _DWORD

    @staticmethod
    def _handle(identifier: int) -> _HWND:
        return _HWND(identifier)

    @staticmethod
    def _identifier(value: object) -> int:
        raw = value.value if hasattr(value, "value") else value
        return int(raw or 0)

    def is_window(self, identifier: int) -> bool:
        return bool(self.user32.IsWindow(self._handle(identifier)))

    def is_window_visible(self, identifier: int) -> bool:
        return bool(self.user32.IsWindowVisible(self._handle(identifier)))

    def is_iconic(self, identifier: int) -> bool:
        return bool(self.user32.IsIconic(self._handle(identifier)))

    def restore_window(self, identifier: int) -> None:
        self.user32.ShowWindow(self._handle(identifier), _SW_RESTORE)

    def window_identity(self, identifier: int) -> tuple[int, int]:
        process_id = _DWORD()
        thread_id = int(
            self.user32.GetWindowThreadProcessId(
                self._handle(identifier),
                ctypes.byref(process_id),
            )
        )
        return thread_id, int(process_id.value)

    def root_window(self, identifier: int) -> int:
        return self._identifier(
            self.user32.GetAncestor(self._handle(identifier), _GA_ROOT)
        )

    def foreground_window(self) -> int:
        return self._identifier(self.user32.GetForegroundWindow())

    def focus_window(self, thread_id: int) -> int:
        information = _GuiThreadInfo()
        information.cbSize = ctypes.sizeof(_GuiThreadInfo)
        if not self.user32.GetGUIThreadInfo(
            _DWORD(thread_id),
            ctypes.byref(information),
        ):
            return 0
        return int(information.hwndFocus or 0)

    def ensure_message_queue(self) -> None:
        # AttachThreadInput fails when either side has no message queue.
        # PeekMessageW creates the helper thread's queue even when it finds no
        # pending message.
        message = _Message()
        self.user32.PeekMessageW(
            ctypes.byref(message),
            None,
            0,
            0,
            _PM_NOREMOVE,
        )

    def current_thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def attach_thread_input(
        self,
        current_thread_id: int,
        target_thread_id: int,
        enabled: bool,
    ) -> bool:
        return bool(
            self.user32.AttachThreadInput(
                _DWORD(current_thread_id),
                _DWORD(target_thread_id),
                _BOOL(enabled),
            )
        )

    def bring_to_top(self, identifier: int) -> None:
        self.user32.BringWindowToTop(self._handle(identifier))

    def set_foreground(self, identifier: int) -> None:
        self.user32.SetForegroundWindow(self._handle(identifier))

    def set_focus(self, identifier: int) -> None:
        # A null return value can mean there was no previous focus window, so
        # the post-condition is verified with GetGUIThreadInfo instead.
        self.user32.SetFocus(self._handle(identifier))


def _target_identity_is_current(
    api: NativeWindowsFocusApi,
    request: TargetActivationRequest,
) -> bool:
    if (
        not api.is_window(request.target_window)
        or api.window_identity(request.target_window)
        != (request.target_thread_id, request.target_process_id)
        or api.root_window(request.target_window) != request.target_window
    ):
        return False
    focus_values = (
        request.focus_window,
        request.focus_thread_id,
        request.focus_process_id,
    )
    if all(value is None for value in focus_values):
        return True
    if any(value is None for value in focus_values):
        return False
    assert request.focus_window is not None
    assert request.focus_thread_id is not None
    assert request.focus_process_id is not None
    return bool(
        api.is_window(request.focus_window)
        and api.window_identity(request.focus_window)
        == (request.focus_thread_id, request.focus_process_id)
        and api.root_window(request.focus_window) == request.target_window
    )


def _panel_identity_is_current(
    api: NativeWindowsFocusApi,
    request: PanelActivationRequest,
) -> tuple[int, int] | None:
    if (
        not api.is_window(request.panel_window)
        or not api.is_window_visible(request.panel_window)
        or api.root_window(request.panel_window) != request.panel_window
    ):
        return None
    thread_id, process_id = api.window_identity(request.panel_window)
    if not thread_id or process_id != request.panel_process_id:
        return None
    return thread_id, process_id


def _run_attached_transaction(
    api: NativeWindowsFocusApi,
    thread_ids: Sequence[int],
    *,
    identity_is_current: Callable[[], bool],
    activate: Callable[[], None],
    verify: Callable[[], bool],
) -> int:
    api.ensure_message_queue()
    current_thread_id = api.current_thread_id()
    if not current_thread_id:
        return EXIT_NATIVE_ERROR

    attached_threads: list[int] = []
    operation_result = EXIT_ATTACH_FAILED
    detach_failed = False
    try:
        attachment_ready = True
        for thread_id in dict.fromkeys(thread_ids):
            if not thread_id or thread_id == current_thread_id:
                continue
            if not api.attach_thread_input(
                current_thread_id,
                thread_id,
                True,
            ):
                attachment_ready = False
                break
            attached_threads.append(thread_id)
        if not attachment_ready:
            operation_result = EXIT_ATTACH_FAILED
        elif not identity_is_current():
            operation_result = EXIT_STALE_IDENTITY
        else:
            activate()
            operation_result = EXIT_OK
    except Exception:
        LOGGER.exception("Windows focus transaction failed")
        operation_result = EXIT_NATIVE_ERROR
    finally:
        for thread_id in reversed(attached_threads):
            detached = False
            for _attempt in range(2):
                try:
                    detached = api.attach_thread_input(
                        current_thread_id,
                        thread_id,
                        False,
                    )
                except Exception:
                    LOGGER.exception(
                        "Windows focus helper detach raised; current=%d target=%d",
                        current_thread_id,
                        thread_id,
                    )
                if detached:
                    break
            if not detached:
                detach_failed = True
                LOGGER.error(
                    "Windows focus helper detach failed; current=%d target=%d",
                    current_thread_id,
                    thread_id,
                )

    # A failed explicit detach is never reported as success.  Returning from
    # main immediately terminates this one-shot process, which is the final OS
    # cleanup boundary for any surviving input-queue attachment.
    if detach_failed:
        return EXIT_DETACH_FAILED
    if operation_result != EXIT_OK:
        return operation_result
    try:
        return EXIT_OK if identity_is_current() and verify() else EXIT_ACTIVATION_FAILED
    except Exception:
        LOGGER.exception("Windows focus transaction verification failed")
        return EXIT_NATIVE_ERROR


def activate_target(
    api: NativeWindowsFocusApi,
    request: TargetActivationRequest,
) -> int:
    if not _target_identity_is_current(api, request):
        return EXIT_STALE_IDENTITY
    focus_thread_id = request.focus_thread_id
    foreground_window = api.foreground_window()
    foreground_thread_id = (
        api.window_identity(foreground_window)[0]
        if foreground_window and api.is_window(foreground_window)
        else 0
    )

    def activate() -> None:
        if api.is_iconic(request.target_window):
            api.restore_window(request.target_window)
        api.bring_to_top(request.target_window)
        api.set_foreground(request.target_window)
        if request.focus_window is not None:
            api.set_focus(request.focus_window)

    def verify() -> bool:
        return bool(
            api.foreground_window() == request.target_window
            and (
                request.focus_window is None
                or (
                    focus_thread_id is not None
                    and api.focus_window(focus_thread_id)
                    == request.focus_window
                )
            )
        )

    return _run_attached_transaction(
        api,
        (
            foreground_thread_id,
            request.target_thread_id,
            *((focus_thread_id,) if focus_thread_id is not None else ()),
        ),
        identity_is_current=lambda: _target_identity_is_current(api, request),
        activate=activate,
        verify=verify,
    )


def activate_panel(
    api: NativeWindowsFocusApi,
    request: PanelActivationRequest,
) -> int:
    panel_identity = _panel_identity_is_current(api, request)
    if panel_identity is None:
        return EXIT_STALE_IDENTITY
    panel_thread_id = panel_identity[0]
    foreground_window = api.foreground_window()
    foreground_thread_id = (
        api.window_identity(foreground_window)[0]
        if foreground_window and api.is_window(foreground_window)
        else 0
    )

    def activate() -> None:
        api.bring_to_top(request.panel_window)
        api.set_foreground(request.panel_window)

    return _run_attached_transaction(
        api,
        (foreground_thread_id, panel_thread_id),
        identity_is_current=lambda: _panel_identity_is_current(api, request)
        == panel_identity,
        activate=activate,
        verify=lambda: api.foreground_window() == request.panel_window,
    )


def _positive_integer(value: str) -> int:
    parsed = int(value, 10)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ClipSoon one-shot Windows foreground/focus helper"
    )
    parser.add_argument("--mode", choices=("target", "panel"), required=True)
    parser.add_argument("--target-hwnd", type=_positive_integer)
    parser.add_argument("--target-thread-id", type=_positive_integer)
    parser.add_argument("--target-process-id", type=_positive_integer)
    parser.add_argument("--focus-hwnd", type=_positive_integer)
    parser.add_argument("--focus-thread-id", type=_positive_integer)
    parser.add_argument("--focus-process-id", type=_positive_integer)
    parser.add_argument("--panel-hwnd", type=_positive_integer)
    parser.add_argument("--panel-process-id", type=_positive_integer)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    api_factory: Callable[[], NativeWindowsFocusApi] = NativeWindowsFocusApi,
) -> int:
    if sys.platform != "win32":
        return EXIT_USAGE
    try:
        arguments = _parser().parse_args(argv)
    except (SystemExit, ValueError):
        return EXIT_USAGE

    if arguments.mode == "target":
        target_values = (
            arguments.target_hwnd,
            arguments.target_thread_id,
            arguments.target_process_id,
        )
        focus_values = (
            arguments.focus_hwnd,
            arguments.focus_thread_id,
            arguments.focus_process_id,
        )
        if (
            any(value is None for value in target_values)
            or any(
                value is not None
                for value in (arguments.panel_hwnd, arguments.panel_process_id)
            )
            or not (
                all(value is None for value in focus_values)
                or all(value is not None for value in focus_values)
            )
        ):
            return EXIT_USAGE
        request = TargetActivationRequest(
            target_window=arguments.target_hwnd,
            target_thread_id=arguments.target_thread_id,
            target_process_id=arguments.target_process_id,
            focus_window=arguments.focus_hwnd,
            focus_thread_id=arguments.focus_thread_id,
            focus_process_id=arguments.focus_process_id,
        )
        try:
            return activate_target(api_factory(), request)
        except Exception:
            LOGGER.exception("Windows target focus helper failed")
            return EXIT_NATIVE_ERROR

    if (
        arguments.panel_hwnd is None
        or arguments.panel_process_id is None
        or any(
            value is not None
            for value in (
                arguments.target_hwnd,
                arguments.target_thread_id,
                arguments.target_process_id,
                arguments.focus_hwnd,
                arguments.focus_thread_id,
                arguments.focus_process_id,
            )
        )
    ):
        return EXIT_USAGE
    try:
        return activate_panel(
            api_factory(),
            PanelActivationRequest(
                panel_window=arguments.panel_hwnd,
                panel_process_id=arguments.panel_process_id,
            ),
        )
    except Exception:
        LOGGER.exception("Windows panel focus helper failed")
        return EXIT_NATIVE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
