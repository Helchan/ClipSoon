"""Smoke-test the packaged Windows helper modes and native paste boundary."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipsoon.windows_focus_host import FOCUS_HELPER_TIMEOUT_SECONDS
from clipsoon.windows_paste_host import PASTE_HELPER_TIMEOUT_SECONDS

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if (
        sys.platform == "win32"
        and len(arguments) == 4
        and arguments[0] == "--native-input-probe"
        and arguments[2] in {"text", "image"}
    ):
        executable = Path(arguments[1]).resolve()
        expected_text = base64.b64decode(arguments[3]).decode("utf-8")
        _smoke_windows_input_delivery(
            executable,
            expected_text,
            probe_kind=arguments[2],
        )
        return 0
    if sys.platform != "win32" or len(arguments) != 1:
        return 64
    executable = Path(arguments[0]).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="clipsoon-helper-smoke-") as temporary:
        ipc_dir = Path(temporary)
        _smoke_role(executable, "hotkey", ["--hotkey", "combo:ctrl+shift+space"])
        _smoke_clipboard(executable, ipc_dir)
    return 0


def _smoke_role(executable: Path, role: str, role_arguments: list[str]) -> None:
    process, messages, session_id = _start_role(executable, role, role_arguments)
    try:
        _wait_for(messages, role, session_id, "ready", timeout=8)
        _wait_for(messages, role, session_id, "heartbeat", timeout=4)
        _shutdown(process, role)
    except BaseException as exc:
        _abort(process, role, exc)


def _smoke_clipboard(executable: Path, ipc_dir: Path) -> None:
    # Imported only on Windows so this script keeps its historical return-64
    # behavior when release checks invoke it on another operating system.
    from clipsoon.windows_clipboard_host import (
        CF_DIB,
        CF_DIBV5,
        CF_HDROP,
        CF_TEXT,
        CF_UNICODETEXT,
        CtypesWindowsApi,
    )

    process, messages, session_id = _start_role(
        executable,
        "clipboard",
        ["--ipc-dir", str(ipc_dir)],
    )
    api = CtypesWindowsApi()
    try:
        _wait_for(messages, "clipboard", session_id, "ready", timeout=8)
        _wait_for(messages, "clipboard", session_id, "heartbeat", timeout=4)

        text = "ClipSoon 打包剪贴板冒烟\n第二行"
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "text",
            {"text": text},
            before_verify=lambda: _read_clipboard(
                api,
                lambda: api.global_bytes(CF_TEXT),
            ),
        )
        text_bytes = _read_clipboard(api, lambda: api.global_bytes(CF_UNICODETEXT))
        if _decode_unicode_text(text_bytes) != text.replace("\n", "\r\n"):
            raise RuntimeError("CF_UNICODETEXT round-trip mismatch")
        _run_windows_input_delivery_probe(
            executable,
            text.replace("\n", "\r\n"),
        )

        source_file = ipc_dir / "hdrop-source.txt"
        source_file.write_text("ClipSoon CF_HDROP smoke", encoding="utf-8")
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "files",
            {"files": [str(source_file.resolve())]},
        )
        copied_files = _read_clipboard(api, lambda: api.hdrop_files(CF_HDROP))
        if (
            len(copied_files) != 1
            or Path(copied_files[0]).resolve() != source_file.resolve()
        ):
            raise RuntimeError(f"CF_HDROP round-trip mismatch: {copied_files!r}")

        dibv5 = _dibv5_1x1()
        dib = _dib_1x1()
        png_name = f"write-png-{session_id}-{uuid.uuid4().hex}.png"
        dibv5_name = f"write-dibv5-{session_id}-{uuid.uuid4().hex}.bin"
        dib_name = f"write-dib-{session_id}-{uuid.uuid4().hex}.bin"
        (ipc_dir / png_name).write_bytes(_PNG_1X1)
        (ipc_dir / dibv5_name).write_bytes(dibv5)
        (ipc_dir / dib_name).write_bytes(dib)
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "image",
            {
                "png_file": png_name,
                "png_bytes": len(_PNG_1X1),
                "dibv5_file": dibv5_name,
                "dibv5_bytes": len(dibv5),
                "dib_file": dib_name,
                "dib_bytes": len(dib),
            },
        )

        # This ordering is the regression gate: the writer process must be
        # gone before a different process asks Windows for every image format.
        # Delayed-rendering/null-handle implementations fail this check.
        _shutdown(process, "clipboard")
        png_format = api.register_format("PNG")

        def read_image_formats() -> tuple[bytes, bytes, bytes]:
            if not all(
                api.is_format_available(format_id)
                for format_id in (png_format, CF_DIBV5, CF_DIB)
            ):
                raise RuntimeError("PNG, CF_DIBV5, or eager CF_DIB is unavailable")
            return (
                api.global_bytes(png_format),
                api.global_bytes(CF_DIBV5),
                api.global_bytes(CF_DIB),
            )

        png, persisted_dibv5, persisted_dib = _read_clipboard(api, read_image_formats)
        if png[: len(_PNG_1X1)] != _PNG_1X1 or any(png[len(_PNG_1X1) :]):
            raise RuntimeError("registered PNG did not survive clipboard helper exit")
        if persisted_dibv5[: len(dibv5)] != dibv5 or any(persisted_dibv5[len(dibv5) :]):
            raise RuntimeError("CF_DIBV5 did not survive clipboard helper exit")
        if persisted_dib[: len(dib)] != dib or any(persisted_dib[len(dib) :]):
            raise RuntimeError("CF_DIB did not survive clipboard helper exit")
        _run_windows_input_delivery_probe(
            executable,
            "",
            probe_kind="image",
        )
    except BaseException as exc:
        _abort(process, "clipboard", exc)


def _run_windows_input_delivery_probe(
    executable: Path,
    expected_text: str,
    *,
    probe_kind: str = "text",
) -> None:
    encoded_text = base64.b64encode(expected_text.encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--native-input-probe",
                str(executable),
                probe_kind,
                encoded_text,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("native input probe exceeded its 20 second hard limit") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"native input probe exited with {completed.returncode}: {detail}"
        )


def _smoke_windows_input_delivery(
    executable: Path,
    expected_text: str,
    *,
    probe_kind: str,
) -> None:
    """Exercise the real focus-restore and SendInput path against a Win32 EDIT."""

    from clipsoon.system import (
        _windows_focus_window,
        _WindowsGuiThreadInfo,
    )
    from clipsoon.windows_hotkey_host import _GuiThreadInfo
    from clipsoon.windows_paste_host import (
        _WindowsInput,
        _WindowsKeyboardInput,
    )

    if (
        ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(_WindowsInput) != 40
        or ctypes.sizeof(_WindowsKeyboardInput) != 24
        or ctypes.sizeof(_WindowsGuiThreadInfo) != 72
        or ctypes.sizeof(_GuiThreadInfo) != 72
    ):
        raise RuntimeError("Win32 x64 ctypes ABI layout mismatch")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
    hwnd_type = ctypes.c_void_p
    dword_type = ctypes.c_uint32
    message_type = ctypes.c_uint32
    subclass_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    subclass_procedure_type = subclass_factory(
        ctypes.c_ssize_t,
        hwnd_type,
        message_type,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )
    user32.CreateWindowExW.argtypes = (
        dword_type,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        dword_type,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_int32,
        hwnd_type,
        hwnd_type,
        hwnd_type,
        hwnd_type,
    )
    user32.CreateWindowExW.restype = hwnd_type
    user32.ShowWindow.argtypes = (hwnd_type, ctypes.c_int32)
    user32.ShowWindow.restype = ctypes.c_int32
    user32.SetForegroundWindow.argtypes = (hwnd_type,)
    user32.SetForegroundWindow.restype = ctypes.c_int32
    user32.SetFocus.argtypes = (hwnd_type,)
    user32.SetFocus.restype = hwnd_type
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = hwnd_type
    user32.GetWindowThreadProcessId.argtypes = (
        hwnd_type,
        ctypes.POINTER(dword_type),
    )
    user32.GetWindowThreadProcessId.restype = dword_type
    user32.IsClipboardFormatAvailable.argtypes = (message_type,)
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int32
    user32.RegisterClipboardFormatW.argtypes = (ctypes.c_wchar_p,)
    user32.RegisterClipboardFormatW.restype = message_type
    user32.SendMessageTimeoutW.argtypes = (
        hwnd_type,
        message_type,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
        message_type,
        message_type,
        ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = (hwnd_type,)
    user32.DestroyWindow.restype = ctypes.c_int32
    user32.PostThreadMessageW.argtypes = (
        dword_type,
        message_type,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    user32.PostThreadMessageW.restype = ctypes.c_int32
    kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
    kernel32.GetModuleHandleW.restype = hwnd_type
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = dword_type
    comctl32.SetWindowSubclass.argtypes = (
        hwnd_type,
        subclass_procedure_type,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )
    comctl32.SetWindowSubclass.restype = ctypes.c_int32
    comctl32.DefSubclassProc.argtypes = (
        hwnd_type,
        message_type,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
    comctl32.RemoveWindowSubclass.argtypes = (
        hwnd_type,
        subclass_procedure_type,
        ctypes.c_size_t,
    )
    comctl32.RemoveWindowSubclass.restype = ctypes.c_int32

    ws_visible = 0x10000000
    ws_child = 0x40000000
    ws_tabstop = 0x00010000
    ws_overlappedwindow = 0x00CF0000
    ws_ex_clientedge = 0x00000200
    es_multiline = 0x0004
    sw_show = 5
    wm_quit = 0x0012
    wm_paste = 0x0302
    wm_gettext = 0x000D
    wm_gettextlength = 0x000E
    wm_focus_child = 0x8001
    smto_block = 0x0001
    smto_abort_if_hung = 0x0002
    cf_dib = 8
    cf_dibv5 = 17
    png_format = int(user32.RegisterClipboardFormatW("PNG"))
    if probe_kind == "image" and not png_format:
        raise ctypes.WinError(ctypes.get_last_error())
    image_paste_observed = threading.Event()
    image_paste_formats_available = [False]
    subclass_callbacks: list[object] = []
    module = kernel32.GetModuleHandleW(None)

    class Message(ctypes.Structure):
        _fields_ = (
            ("hwnd", hwnd_type),
            ("message", message_type),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_ssize_t),
            ("time", dword_type),
            ("pt_x", ctypes.c_int32),
            ("pt_y", ctypes.c_int32),
            ("lPrivate", dword_type),
        )

    user32.GetMessageW.argtypes = (
        ctypes.POINTER(Message),
        hwnd_type,
        message_type,
        message_type,
    )
    user32.GetMessageW.restype = ctypes.c_int32
    user32.TranslateMessage.argtypes = (ctypes.POINTER(Message),)
    user32.TranslateMessage.restype = ctypes.c_int32
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(Message),)
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    def start_window_thread(
        *,
        role: str,
        title: str,
        x: int,
        width: int,
        height: int,
        multiline: bool,
    ) -> tuple[
        threading.Thread,
        dict[str, int],
        threading.Event,
        list[BaseException],
    ]:
        ready = threading.Event()
        focus_completed = threading.Event()
        handles: dict[str, int] = {}
        failures: list[BaseException] = []

        def message_loop() -> None:
            top = edit = None
            subclass_callback: Any = None
            subclass_installed = False
            try:
                top = user32.CreateWindowExW(
                    0,
                    "STATIC",
                    title,
                    ws_overlappedwindow | ws_visible,
                    x,
                    80,
                    width,
                    height,
                    None,
                    None,
                    module,
                    None,
                )
                edit_style = ws_child | ws_visible | ws_tabstop
                if multiline:
                    edit_style |= es_multiline
                edit = user32.CreateWindowExW(
                    ws_ex_clientedge,
                    "EDIT",
                    "",
                    edit_style,
                    20,
                    30,
                    width - 60,
                    100 if multiline else 50,
                    top,
                    None,
                    module,
                    None,
                )
                if not top or not edit:
                    raise ctypes.WinError(ctypes.get_last_error())
                if role == "target" and probe_kind == "image":

                    @subclass_procedure_type
                    def observe_image_paste(
                        window: int,
                        message_id: int,
                        word_parameter: int,
                        long_parameter: int,
                        _subclass_id: int,
                        _reference_data: int,
                    ) -> int:
                        if int(message_id) == wm_paste:
                            image_paste_formats_available[0] = all(
                                user32.IsClipboardFormatAvailable(
                                    message_type(format_id)
                                )
                                for format_id in (png_format, cf_dibv5, cf_dib)
                            )
                            image_paste_observed.set()
                        return int(
                            comctl32.DefSubclassProc(
                                hwnd_type(window),
                                message_type(message_id),
                                word_parameter,
                                long_parameter,
                            )
                        )

                    subclass_callback = observe_image_paste
                    subclass_callbacks.append(subclass_callback)
                    subclass_installed = bool(
                        comctl32.SetWindowSubclass(
                            edit,
                            subclass_callback,
                            1,
                            0,
                        )
                    )
                    if not subclass_installed:
                        raise ctypes.WinError(ctypes.get_last_error())
                handles.update(
                    {
                        "top": int(top),
                        "edit": int(edit),
                        "thread": int(kernel32.GetCurrentThreadId()),
                    }
                )
                user32.ShowWindow(top, sw_show)
                ready.set()

                message = Message()
                while True:
                    result = int(
                        user32.GetMessageW(
                            ctypes.byref(message),
                            None,
                            0,
                            0,
                        )
                    )
                    if result == 0:
                        break
                    if result < 0:
                        raise ctypes.WinError(ctypes.get_last_error())
                    if message.message == wm_focus_child:
                        user32.SetFocus(edit)
                        focus_completed.set()
                        continue
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
            except BaseException as exc:
                failures.append(exc)
                ready.set()
                focus_completed.set()
            finally:
                if edit and subclass_installed:
                    comctl32.RemoveWindowSubclass(
                        edit,
                        subclass_callback,
                        1,
                    )
                if edit:
                    user32.DestroyWindow(edit)
                if top:
                    user32.DestroyWindow(top)

        thread = threading.Thread(
            target=message_loop,
            name=f"ClipSoon-{role}-window-smoke",
            daemon=True,
        )
        thread.start()
        if not ready.wait(5):
            raise TimeoutError(f"Win32 {role} window did not initialize")
        if failures:
            raise failures[0]
        return thread, handles, focus_completed, failures

    panel_thread, panel_handles, panel_focus_completed, panel_failures = (
        start_window_thread(
            role="panel",
            title="ClipSoon paste smoke panel",
            x=80,
            width=360,
            height=160,
            multiline=False,
        )
    )
    target_thread, target_handles, _target_focus_completed, target_failures = (
        start_window_thread(
            role="target",
            title="ClipSoon paste smoke target",
            x=480,
            width=460,
            height=220,
            multiline=True,
        )
    )
    window_threads = (
        ("target", target_thread, target_handles, target_failures),
        ("panel", panel_thread, panel_handles, panel_failures),
    )
    try:
        panel = panel_handles["top"]
        panel_edit = panel_handles["edit"]
        panel_thread_id = panel_handles["thread"]
        top = target_handles["top"]
        edit = target_handles["edit"]
        target_thread_id = target_handles["thread"]
        process_id = dword_type()
        reported_thread_id = int(
            user32.GetWindowThreadProcessId(
                hwnd_type(top),
                ctypes.byref(process_id),
            )
        )
        if reported_thread_id != target_thread_id or process_id.value != os.getpid():
            raise RuntimeError("Win32 target identity mismatch")

        # Establish the same precondition as a real panel selection. The
        # packaged one-shot focus helper must first move input to the panel and
        # then restore the captured target/edit identity across processes.
        _run_packaged_helper(
            executable,
            "focus",
            [
                "--mode",
                "panel",
                "--panel-hwnd",
                str(int(panel)),
                "--panel-process-id",
                str(os.getpid()),
            ],
        )
        if not user32.PostThreadMessageW(
            dword_type(panel_thread_id),
            message_type(wm_focus_child),
            0,
            0,
        ):
            raise RuntimeError("Win32 panel focus request could not be posted")
        if not panel_focus_completed.wait(1):
            raise RuntimeError("Win32 panel focus request timed out")
        if (
            int(user32.GetForegroundWindow() or 0) != panel
            or _windows_focus_window(user32, panel_thread_id) != panel_edit
        ):
            raise RuntimeError("Win32 panel did not acquire foreground and focus")

        _run_packaged_helper(
            executable,
            "focus",
            [
                "--mode",
                "target",
                "--target-hwnd",
                str(top),
                "--target-thread-id",
                str(target_thread_id),
                "--target-process-id",
                str(int(process_id.value)),
                "--focus-hwnd",
                str(edit),
                "--focus-thread-id",
                str(target_thread_id),
                "--focus-process-id",
                str(int(process_id.value)),
            ],
        )
        if (
            int(user32.GetForegroundWindow() or 0) != top
            or _windows_focus_window(user32, target_thread_id) != edit
        ):
            raise RuntimeError("Win32 target or child focus could not be restored")
        _run_packaged_helper(executable, "paste", [])

        if probe_kind == "image":
            if not image_paste_observed.wait(2):
                raise RuntimeError("Win32 EDIT did not receive WM_PASTE for image data")
            if not image_paste_formats_available[0]:
                raise RuntimeError(
                    "PNG, CF_DIBV5, and CF_DIB were not all available during WM_PASTE"
                )
        else:
            deadline = time.monotonic() + 3
            received = ""
            while time.monotonic() < deadline:
                length_result = ctypes.c_size_t()
                if not user32.SendMessageTimeoutW(
                    hwnd_type(edit),
                    message_type(wm_gettextlength),
                    0,
                    0,
                    smto_block | smto_abort_if_hung,
                    200,
                    ctypes.byref(length_result),
                ):
                    raise RuntimeError("Win32 EDIT did not answer WM_GETTEXTLENGTH")
                length = int(length_result.value)
                buffer = ctypes.create_unicode_buffer(length + 1)
                text_result = ctypes.c_size_t()
                if not user32.SendMessageTimeoutW(
                    hwnd_type(edit),
                    message_type(wm_gettext),
                    len(buffer),
                    ctypes.cast(buffer, ctypes.c_void_p).value or 0,
                    smto_block | smto_abort_if_hung,
                    200,
                    ctypes.byref(text_result),
                ):
                    raise RuntimeError("Win32 EDIT did not answer WM_GETTEXT")
                received = buffer.value
                if received == expected_text:
                    break
                time.sleep(0.02)
            if received != expected_text:
                raise RuntimeError(
                    "Win32 EDIT paste mismatch: "
                    f"expected={expected_text!r} actual={received!r}"
                )
    finally:
        cleanup_failures: list[str] = []
        for role, thread, handles, failures in window_threads:
            if thread.is_alive() and "thread" in handles:
                posted = bool(
                    user32.PostThreadMessageW(
                        dword_type(handles["thread"]),
                        message_type(wm_quit),
                        0,
                        0,
                    )
                )
                thread.join(timeout=5)
                if not posted or thread.is_alive():
                    cleanup_failures.append(f"{role} thread did not stop")
            if failures:
                cleanup_failures.append(f"{role} thread failed: {failures[-1]}")
        if cleanup_failures:
            raise RuntimeError("; ".join(cleanup_failures))


def _run_packaged_helper(
    executable: Path,
    role: str,
    arguments: list[str],
) -> None:
    timeout = (
        FOCUS_HELPER_TIMEOUT_SECONDS
        if role == "focus"
        else PASTE_HELPER_TIMEOUT_SECONDS
    )
    try:
        completed = subprocess.run(
            [str(executable), f"--windows-helper={role}", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"packaged {role} helper timed out") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"packaged {role} helper exited with {completed.returncode}: {detail}"
        )


def _start_role(
    executable: Path,
    role: str,
    role_arguments: list[str],
) -> tuple[subprocess.Popen[str], queue.Queue[dict[str, object]], str]:
    session_id = uuid.uuid4().hex
    command = [
        str(executable),
        f"--windows-helper={role}",
        *role_arguments,
        "--session-id",
        session_id,
        "--parent-pid",
        str(os.getpid()),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    messages: queue.Queue[dict[str, object]] = queue.Queue()

    def read_messages() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.put(value)

    threading.Thread(target=read_messages, daemon=True).start()
    return process, messages, session_id


def _write_clipboard_value(
    process: subprocess.Popen[str],
    messages: queue.Queue[dict[str, object]],
    ipc_dir: Path,
    session_id: str,
    kind: str,
    value: dict[str, Any],
    *,
    before_verify: Callable[[], Any] | None = None,
) -> int:
    request_id = uuid.uuid4().hex
    manifest = {
        "protocol": 1,
        "session_id": session_id,
        "request_id": request_id,
        "kind": kind,
        **value,
    }
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    manifest_name = f"write-manifest-{session_id}-{request_id}.json"
    (ipc_dir / manifest_name).write_bytes(payload)
    _send(
        process,
        {
            "type": "write_clipboard",
            "request_id": request_id,
            "kind": kind,
            "manifest": manifest_name,
            "manifest_bytes": len(payload),
        },
    )
    _wait_for(
        messages,
        "clipboard",
        session_id,
        "write_started",
        request_id=request_id,
        timeout=8,
    )
    result = _wait_for(
        messages,
        "clipboard",
        session_id,
        "write_result",
        request_id=request_id,
        timeout=12,
    )
    if result.get("ok") is not True or not isinstance(result.get("sequence"), int):
        raise RuntimeError(f"{kind} clipboard write failed: {result!r}")
    sequence = int(result["sequence"])
    if before_verify is not None:
        before_verify()
    _send(
        process,
        {
            "type": "verify_clipboard",
            "request_id": request_id,
            "kind": kind,
            "sequence": sequence,
        },
    )
    verification = _wait_for(
        messages,
        "clipboard",
        session_id,
        "verify_result",
        request_id=request_id,
        timeout=8,
    )
    verified_sequence = verification.get("sequence")
    if (
        verification.get("ok") is not True
        or not isinstance(verified_sequence, int)
        or isinstance(verified_sequence, bool)
        or verified_sequence <= 0
    ):
        raise RuntimeError(f"{kind} clipboard verification failed: {verification!r}")
    return int(verified_sequence)


def _wait_for(
    messages: queue.Queue[dict[str, object]],
    role: str,
    session_id: str,
    event_type: str,
    *,
    request_id: str | None = None,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {role} {event_type}")
        message = messages.get(timeout=remaining)
        if message.get("type") != event_type:
            continue
        if request_id is not None and message.get("request_id") != request_id:
            continue
        _assert_envelope(message, role, session_id, event_type)
        return message


def _send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise RuntimeError("helper stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _shutdown(process: subprocess.Popen[str], role: str) -> None:
    _send(process, {"type": "shutdown"})
    if process.wait(timeout=5) != 0:
        raise RuntimeError(f"{role} helper exited with {process.returncode}")


def _abort(
    process: subprocess.Popen[str],
    role: str,
    failure: BaseException,
) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    detail = f"{failure}"
    if stderr:
        detail = f"{detail}; helper stderr: {stderr}"
    raise RuntimeError(f"{role} helper smoke failed: {detail}") from failure


def _read_clipboard(api: Any, read: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + 2
    while not api.open_clipboard(0):
        if time.monotonic() >= deadline:
            raise RuntimeError("OpenClipboard failed during independent verification")
        time.sleep(0.01)
    try:
        return read()
    finally:
        if not api.close_clipboard():
            raise RuntimeError("CloseClipboard failed during independent verification")


def _decode_unicode_text(data: bytes) -> str:
    terminator = next(
        (
            offset
            for offset in range(0, max(0, len(data) - 1), 2)
            if data[offset : offset + 2] == b"\0\0"
        ),
        len(data),
    )
    return data[:terminator].decode("utf-16-le")


def _dibv5_1x1() -> bytes:
    header = bytearray(124)
    struct.pack_into("<IiiHHIIiiII", header, 0, 124, 1, 1, 1, 32, 0, 4, 0, 0, 0, 0)
    struct.pack_into(
        "<IIIII",
        header,
        40,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x57696E20,
    )
    struct.pack_into("<I", header, 108, 4)
    return bytes(header) + b"\x00\x00\xff\xff"


def _dib_1x1() -> bytes:
    return (
        struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 32, 0, 4, 0, 0, 0, 0)
        + b"\x00\x00\xff\xff"
    )


def _assert_envelope(
    message: dict[str, object],
    role: str,
    session_id: str,
    event_type: str,
) -> None:
    if not (
        message.get("protocol") == 1
        and message.get("role") == role
        and message.get("session_id") == session_id
        and message.get("type") == event_type
        and isinstance(message.get("event_id"), int)
    ):
        raise RuntimeError(f"invalid {role} {event_type} message: {message!r}")


if __name__ == "__main__":
    raise SystemExit(main())
