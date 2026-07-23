"""Fast system edge: hotkeys, clipboard MIME, foreground restore, and paste."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import plistlib
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QClipboard, QImage

from clipsoon.core import (
    WINDOWS_DEFAULT_HOTKEY,
    AppSettings,
    ClipboardWriteReceipt,
    ClipItem,
    ClipKind,
    FileItemClaimStatus,
    HistoryRepository,
    ValidatedFileItem,
)
from clipsoon.windows_focus_host import FOCUS_HELPER_TIMEOUT_SECONDS
from clipsoon.windows_paste_host import (
    _WindowsInput as _WindowsInput,
)
from clipsoon.windows_paste_host import (
    _WindowsKeyboardInput as _WindowsKeyboardInput,
)
from clipsoon.windows_paste_host import (
    send_windows_paste_input,
)
from clipsoon.windows_workers import WindowsWorkerSupervisor, windows_worker_command

LOGGER = logging.getLogger(__name__)
_MODIFIERS = {"ctrl", "shift", "alt", "meta"}
_CLIPBOARD_SETTLE_MS = 70
_CLIPBOARD_RETRY_DELAYS_MS = (90, 180)
_WINDOWS_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
_WINDOWS_IMAGE_MAX_BYTES = 256 * 1024 * 1024
_WINDOWS_BITMAP_PIXEL_MAX_BYTES = 128 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WINDOWS_WRITE_DEADLINE_MS = 22_000
_WINDOWS_VERIFY_DEADLINE_MS = 5_000
_WINDOWS_MAX_WRITE_ATTEMPTS = 2
_WINDOWS_FOCUS_HELPER_TIMEOUT_SECONDS = FOCUS_HELPER_TIMEOUT_SECONDS
_WINDOWS_FOCUS_RETRY_DELAYS_MS = (40, 80, 120)
_FILE_VALIDATION_TIMEOUT_MS = 3_000
_MAX_CONCURRENT_FILE_VALIDATIONS = 2
_GA_ROOT = 2

_WIN_BOOL = ctypes.c_int32
_WIN_DWORD = ctypes.c_uint32
_WIN_LONG = ctypes.c_int32


class _WindowsRect(ctypes.Structure):
    _fields_ = (
        ("left", _WIN_LONG),
        ("top", _WIN_LONG),
        ("right", _WIN_LONG),
        ("bottom", _WIN_LONG),
    )


class _WindowsGuiThreadInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", _WIN_DWORD),
        ("flags", _WIN_DWORD),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", _WindowsRect),
    )


def _windows_ipc_session(name: str) -> str | None:
    value = name.removeprefix(".")
    prefixes = (
        "write-manifest-",
        "write-dibv5-",
        "write-dib-",
        "write-png-",
        "manifest-",
        "clip-",
    )
    prefix = next((candidate for candidate in prefixes if value.startswith(candidate)), None)
    if prefix is None:
        return None
    session_id = value.removeprefix(prefix).split("-", 1)[0].casefold()
    if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
        return None
    return session_id


def _is_request_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _bitmap_payloads(image: QImage) -> tuple[bytes, bytes]:
    # Windows DIBV5 consumers use native little-endian premultiplied ARGB
    # (BGRA bytes).  Convert in Qt, then flip complete rows in bulk instead of
    # visiting millions of screenshot pixels in Python.
    converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width, height = converted.width(), converted.height()
    pixel_bytes = width * height * 4
    if (
        width <= 0
        or height <= 0
        or pixel_bytes > _WINDOWS_BITMAP_PIXEL_MAX_BYTES
    ):
        raise ValueError("图片尺寸超出 Windows 剪贴板限制")
    source = memoryview(converted.constBits()).cast("B")
    stride = converted.bytesPerLine()
    if len(source) < stride * height:
        raise ValueError("图片像素缓冲区不完整")
    row_bytes = width * 4
    pixels = b"".join(
        source[row_index * stride : row_index * stride + row_bytes]
        for row_index in range(height - 1, -1, -1)
    )

    dib_header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        32,
        0,
        pixel_bytes,
        0,
        0,
        0,
        0,
    )
    v5_header = bytearray(124)
    struct.pack_into(
        "<IiiHHIIiiII",
        v5_header,
        0,
        124,
        width,
        height,
        1,
        32,
        0,
        pixel_bytes,
        0,
        0,
        0,
        0,
    )
    struct.pack_into(
        "<IIIII",
        v5_header,
        40,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x57696E20,  # LCS_WINDOWS_COLOR_SPACE
    )
    struct.pack_into("<I", v5_header, 108, 4)  # LCS_GM_IMAGES
    return dib_header + pixels, bytes(v5_header) + pixels


def _bitmap_v5_payload(image: QImage) -> bytes:
    return _bitmap_payloads(image)[1]


@dataclass(frozen=True, slots=True)
class _PreparedWindowsWrite:
    session_id: str
    manifest_name: str
    manifest_bytes: int
    paths: tuple[Path, ...]


def _prepare_windows_write_artifacts(
    ipc_dir: Path,
    item: ClipItem,
    request_id: str,
    session_id: str,
) -> _PreparedWindowsWrite:
    if not _is_request_id(request_id) or not _is_request_id(session_id):
        raise ValueError("Windows 剪贴板请求标识不合法")
    ipc_dir.mkdir(parents=True, exist_ok=True)
    manifest_name = f"write-manifest-{session_id}-{request_id}.json"
    manifest_path = ipc_dir / manifest_name
    created: list[Path] = []
    manifest: dict[str, object] = {
        "protocol": 1,
        "session_id": session_id,
        "request_id": request_id,
        "kind": item.kind.value,
    }
    try:
        if item.kind is ClipKind.TEXT:
            if not item.text:
                raise ValueError("剪贴板文本为空")
            manifest["text"] = item.text
        elif item.kind is ClipKind.FILES:
            if not item.files:
                raise ValueError("剪贴板文件列表为空")
            manifest["files"] = list(item.files)
        else:
            png = Path(item.image_path).read_bytes()
            if not png.startswith(_PNG_SIGNATURE) or not 0 < len(png) <= _WINDOWS_IMAGE_MAX_BYTES:
                raise ValueError("剪贴板 PNG 数据不合法")
            image = QImage.fromData(png, "PNG")
            if image.isNull():
                raise ValueError("剪贴板 PNG 无法解码")
            dib, dibv5 = _bitmap_payloads(image)
            png_name = f"write-png-{session_id}-{request_id}.png"
            dibv5_name = f"write-dibv5-{session_id}-{request_id}.bin"
            dib_name = f"write-dib-{session_id}-{request_id}.bin"
            png_path = ipc_dir / png_name
            dibv5_path = ipc_dir / dibv5_name
            dib_path = ipc_dir / dib_name
            _atomic_write(png_path, png)
            created.append(png_path)
            _atomic_write(dibv5_path, dibv5)
            created.append(dibv5_path)
            _atomic_write(dib_path, dib)
            created.append(dib_path)
            manifest.update(
                {
                    "png_file": png_name,
                    "png_bytes": len(png),
                    "dibv5_file": dibv5_name,
                    "dibv5_bytes": len(dibv5),
                    "dib_file": dib_name,
                    "dib_bytes": len(dib),
                }
            )
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not 0 < len(encoded) <= _WINDOWS_MANIFEST_MAX_BYTES:
            raise ValueError("剪贴板写入清单大小不合法")
        _atomic_write(manifest_path, encoded)
        created.append(manifest_path)
        return _PreparedWindowsWrite(session_id, manifest_name, len(encoded), tuple(created))
    except Exception:
        for path in created:
            with suppress(OSError):
                path.unlink()
        raise


def _windows_handle(identifier: int):
    return ctypes.c_void_p(identifier)


def _windows_foreground_window() -> int:
    user32 = ctypes.windll.user32
    get_foreground = user32.GetForegroundWindow
    get_foreground.restype = ctypes.c_void_p
    value = get_foreground()
    if hasattr(value, "value"):
        value = value.value
    return int(value or 0)


def _positive_protocol_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        else None
    )


def _windows_window_identity(user32: object, identifier: int) -> tuple[int, int]:
    get_identity = user32.GetWindowThreadProcessId
    get_identity.argtypes = (ctypes.c_void_p, ctypes.POINTER(_WIN_DWORD))
    get_identity.restype = _WIN_DWORD
    process_id = _WIN_DWORD()
    thread_id = int(get_identity(_windows_handle(identifier), ctypes.byref(process_id)))
    return thread_id, int(process_id.value)


def _windows_focus_window(user32: object, thread_id: int) -> int:
    get_gui_thread_info = user32.GetGUIThreadInfo
    get_gui_thread_info.argtypes = (
        _WIN_DWORD,
        ctypes.POINTER(_WindowsGuiThreadInfo),
    )
    get_gui_thread_info.restype = _WIN_BOOL
    information = _WindowsGuiThreadInfo()
    information.cbSize = ctypes.sizeof(_WindowsGuiThreadInfo)
    if not get_gui_thread_info(_WIN_DWORD(thread_id), ctypes.byref(information)):
        return 0
    return int(information.hwndFocus or 0)


def _windows_root_window(user32: object, identifier: int) -> int:
    get_ancestor = user32.GetAncestor
    get_ancestor.argtypes = (ctypes.c_void_p, _WIN_DWORD)
    get_ancestor.restype = ctypes.c_void_p
    value = get_ancestor(_windows_handle(identifier), _GA_ROOT)
    if hasattr(value, "value"):
        value = value.value
    return int(value or 0)


def _run_windows_focus_helper(
    *,
    mode: str,
    target_window: int,
    target_thread_id: int | None = None,
    target_process_id: int,
    focus_window: int | None = None,
    focus_thread_id: int | None = None,
    focus_process_id: int | None = None,
) -> bool:
    arguments = [
        "--mode",
        mode,
        f"--{'panel' if mode == 'panel' else 'target'}-hwnd",
        str(target_window),
    ]
    if mode == "panel":
        arguments.extend(("--panel-process-id", str(target_process_id)))
    elif mode == "target" and target_thread_id:
        arguments.extend(
            (
                "--target-thread-id",
                str(target_thread_id),
                "--target-process-id",
                str(target_process_id),
            )
        )
        focus_values = (focus_window, focus_thread_id, focus_process_id)
        if all(focus_values):
            arguments.extend(
                (
                    "--focus-hwnd",
                    str(focus_window),
                    "--focus-thread-id",
                    str(focus_thread_id),
                    "--focus-process-id",
                    str(focus_process_id),
                )
            )
    else:
        return False

    program, helper_arguments = windows_worker_command("focus", arguments)
    try:
        completed = subprocess.run(
            [program, *helper_arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_WINDOWS_FOCUS_HELPER_TIMEOUT_SECONDS,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        LOGGER.error(
            "Windows focus helper timed out; mode=%s target=%d",
            mode,
            target_window,
        )
        return False
    except OSError:
        LOGGER.exception(
            "Could not start Windows focus helper; mode=%s target=%d",
            mode,
            target_window,
        )
        return False
    if completed.returncode == 0:
        return True
    LOGGER.warning(
        "Windows focus helper failed; mode=%s target=%d exit=%d detail=%s",
        mode,
        target_window,
        completed.returncode,
        (completed.stderr or "").strip(),
    )
    return False


class HotkeyStateMachine:
    """Pure press/release state machine; timestamps are seconds."""

    def __init__(
        self,
        spec: str,
        interval_ms: int,
        callback: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._callback = callback
        self.configure(spec, interval_ms)

    def configure(self, spec: str, interval_ms: int) -> None:
        self.spec = spec
        self.interval = max(0.18, min(0.9, interval_ms / 1_000))
        self._pressed: set[str] = set()
        self._pressed_at: dict[str, float] = {}
        self._press_started: float | None = None
        self._last_tap: float | None = None
        self._chorded = False
        self._combo_latched = False
        if spec.startswith("double:"):
            self._target = spec.removeprefix("double:")
            self._combo: frozenset[str] = frozenset()
        else:
            self._target = ""
            self._combo = frozenset(spec.removeprefix("combo:").split("+"))

    def press(self, key: str, at: float | None = None) -> None:
        key, at = _canonical_key(key), self._clock() if at is None else at
        self._prune_stale_keys(at)
        if not key or key in self._pressed:  # ignore OS auto-repeat
            return
        if self._target:
            if key == self._target:
                self._press_started = at
                self._chorded = bool(self._pressed)
            else:
                if self._target in self._pressed:
                    self._chorded = True
                self._last_tap = None
        self._pressed.add(key)
        self._pressed_at[key] = at
        if self._combo and not self._combo_latched and self._combo <= self._pressed:
            self._combo_latched = True
            self._callback()

    def release(self, key: str, at: float | None = None) -> None:
        key, at = _canonical_key(key), self._clock() if at is None else at
        if key not in self._pressed:
            return
        if self._target and key == self._target:
            duration = at - self._press_started if self._press_started is not None else self.interval + 1
            valid_tap = not self._chorded and 0 <= duration <= self.interval
            if valid_tap and self._last_tap is not None and at - self._last_tap <= self.interval:
                self._last_tap = None
                self._callback()
            elif valid_tap:
                self._last_tap = at
            else:
                self._last_tap = None
            self._press_started = None
            self._chorded = False
        self._pressed.discard(key)
        self._pressed_at.pop(key, None)
        if self._combo_latched and not self._combo <= self._pressed:
            self._combo_latched = False

    def _prune_stale_keys(self, at: float) -> None:
        stale_after = max(1.0, self.interval * 2)
        stale = {key for key, pressed_at in self._pressed_at.items() if at - pressed_at > stale_after}
        if not stale:
            return
        self._pressed.difference_update(stale)
        for key in stale:
            self._pressed_at.pop(key, None)
        if self._target in stale:
            self._press_started = None
            self._chorded = False
            self._last_tap = None
        if self._combo_latched and not self._combo <= self._pressed:
            self._combo_latched = False


@dataclass(frozen=True, slots=True)
class HotkeyActivationContext:
    """Per-trigger Windows state captured before the panel changes foreground."""

    target_window: int | None = None
    target_thread_id: int | None = None
    target_process_id: int | None = None
    focus_window: int | None = None
    focus_thread_id: int | None = None
    focus_process_id: int | None = None
    foreground_granted: bool = False


class GlobalHotkeyService:
    def __init__(
        self,
        activated: Callable[[HotkeyActivationContext | None], None],
        failed: Callable[[str], None],
        ready: Callable[[str], None] | None = None,
        registration_failed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._activated = activated
        self._failed = failed
        self._ready = ready
        self._registration_failed = registration_failed
        self._listener = None
        self._machine: HotkeyStateMachine | None = None
        self._windows_worker: WindowsWorkerSupervisor | None = None
        self._windows_hotkey = ""
        self._suppressed_windows_failure: str | None = None

    def start(self, settings: AppSettings) -> None:
        self.stop()
        if sys.platform == "win32":
            hotkey = settings.hotkey
            if not hotkey.startswith("combo:"):
                hotkey = WINDOWS_DEFAULT_HOTKEY
                self._failed(
                    "Windows 已停用不可靠的双修饰键监听，"
                    "当前使用 Ctrl+Shift+Space；可在设置中修改组合键。"
                )
            worker = WindowsWorkerSupervisor(
                "hotkey",
                lambda: [
                    "--hotkey",
                    hotkey,
                ],
            )
            worker.message.connect(self._on_windows_message)
            worker.failed.connect(self._on_windows_failure)
            self._windows_worker = worker
            self._windows_hotkey = hotkey
            worker.start()
            return
        self._machine = HotkeyStateMachine(
            settings.hotkey,
            settings.double_tap_interval_ms,
            lambda: self._activated(None),
        )
        try:
            from pynput import keyboard

            listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            listener.start()
            self._listener = listener
            if sys.platform == "darwin" and not getattr(listener, "IS_TRUSTED", True):
                self._failed("需要在系统设置中授予 ClipSoon 辅助功能权限")
        except Exception as exc:  # platform backends fail here when permission is denied
            LOGGER.exception("Could not start global hotkey listener")
            self._failed(f"全局快捷键不可用：{exc}")

    def stop(self) -> None:
        if self._windows_worker is not None:
            self._windows_worker.stop()
            self._windows_worker = None
        self._windows_hotkey = ""
        self._suppressed_windows_failure = None
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                LOGGER.debug("Hotkey listener stop failed", exc_info=True)
        self._listener = None
        self._machine = None

    @property
    def is_running(self) -> bool:
        if self._windows_worker is not None:
            return self._windows_worker.is_healthy
        if self._listener is None:
            return False
        running = getattr(self._listener, "running", None)
        if running is not None and not running:
            return False
        is_alive = getattr(self._listener, "is_alive", None)
        return not callable(is_alive) or bool(is_alive())

    def _on_windows_message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "ready":
            if self._ready is not None:
                self._ready(self._windows_hotkey)
            return
        if kind == "error" and message.get("code") == "registration_failed":
            text = str(message.get("message") or "Windows 热键注册失败")
            if self._registration_failed is not None:
                self._suppressed_windows_failure = text
                self._registration_failed(self._windows_hotkey, text)
            return
        if kind != "hotkey":
            return
        target_window = _positive_protocol_int(message.get("target_hwnd"))
        self._activated(
            HotkeyActivationContext(
                target_window=target_window,
                target_thread_id=_positive_protocol_int(
                    message.get("target_thread_id")
                ),
                target_process_id=_positive_protocol_int(
                    message.get("target_process_id")
                ),
                focus_window=_positive_protocol_int(message.get("focus_hwnd")),
                focus_thread_id=_positive_protocol_int(
                    message.get("focus_thread_id")
                ),
                focus_process_id=_positive_protocol_int(
                    message.get("focus_process_id")
                ),
                foreground_granted=message.get("foreground_granted") is True,
            )
        )

    def _on_windows_failure(self, message: str) -> None:
        if message == self._suppressed_windows_failure:
            self._suppressed_windows_failure = None
            return
        self._failed(message)

    def _on_press(self, key: object) -> None:
        if self._machine is not None:
            self._machine.press(_pynput_key_name(key))

    def _on_release(self, key: object) -> None:
        if self._machine is not None:
            self._machine.release(_pynput_key_name(key))


class _WorkerSignals(QObject):
    stored = Signal(object)
    failed = Signal(str)


class _NativeClipboardSignals(QObject):
    stored = Signal(object, int)
    consumed = Signal(int)
    failed = Signal(str, int)


class _FileItemValidationSignals(QObject):
    finished = Signal(object, str)


class _WindowsWritePreparationSignals(QObject):
    finished = Signal(str, object, str)


class _WindowsWritePreparationTask(QRunnable):
    def __init__(
        self,
        ipc_dir: Path,
        item: ClipItem,
        request_id: str,
        session_id: str,
    ) -> None:
        super().__init__()
        self.ipc_dir = ipc_dir
        self.item = item
        self.request_id = request_id
        self.session_id = session_id
        self.signals = _WindowsWritePreparationSignals()

    def run(self) -> None:
        try:
            prepared = _prepare_windows_write_artifacts(
                self.ipc_dir,
                self.item,
                self.request_id,
                self.session_id,
            )
        except Exception as exc:
            LOGGER.exception("Windows clipboard write preparation failed")
            self.signals.finished.emit(self.request_id, None, str(exc))
            return
        self.signals.finished.emit(self.request_id, prepared, "")


@dataclass(slots=True)
class _PendingWindowsWrite:
    request_id: str
    item: ClipItem
    callback: Callable[[ClipboardWriteReceipt | None, str], None]
    deadline: float
    attempts: int = 0
    preparations: int = 0
    preparing: bool = False
    awaiting_result: bool = False
    waiting_for_retry: bool = False
    session_id: str = ""
    paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class _PendingWindowsVerify:
    receipt: ClipboardWriteReceipt
    callback: Callable[[bool, str], None]
    deadline: float
    attempts: int = 0
    waiting_for_retry: bool = False
    session_id: str = ""


class _FileItemValidationTask:
    def __init__(self, repository: HistoryRepository, item_id: str) -> None:
        self.repository = repository
        self.item_id = item_id
        self.signals = _FileItemValidationSignals()

    def run(self) -> None:
        try:
            item = self.repository.validate_file_item(self.item_id)
        except Exception as exc:
            LOGGER.exception("File history validation failed")
            self.signals.finished.emit(None, str(exc))
            return
        self.signals.finished.emit(item, "")


class _ImageStoreTask(QRunnable):
    def __init__(
        self,
        repository: HistoryRepository,
        image: QImage,
        source_app: str,
        settings: AppSettings,
    ) -> None:
        super().__init__()
        self.repository, self.image = repository, image
        self.source_app, self.settings = source_app, settings
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not self.image.save(buffer, "PNG"):
                raise ValueError("图片无法转换为 PNG")
            item = self.repository.add_image(
                bytes(buffer.data()), self.image.width(), self.image.height(), self.source_app
            )
            self.repository.cleanup(
                self.settings.max_history_items, self.settings.retention_days
            )
            self.signals.stored.emit(item)
        except Exception as exc:
            LOGGER.exception("Image capture failed")
            self.signals.failed.emit(f"图片记录失败：{exc}")


class _NativeClipboardStoreTask(QRunnable):
    """Validate and persist one manifest without touching the GUI thread."""

    def __init__(
        self,
        repository: HistoryRepository,
        ipc_dir: Path,
        manifest_name: str,
        manifest_bytes: int,
        sequence: int,
        expected_kind: str,
        settings: AppSettings,
        *,
        store: bool,
        cleanup_on_failure: bool = False,
        store_guard: Callable[[], bool] | None = None,
        store_lock: object | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.ipc_dir = ipc_dir.resolve()
        self.manifest_name = manifest_name
        self.manifest_bytes = manifest_bytes
        self.sequence = sequence
        self.expected_kind = expected_kind
        self.settings = settings
        self.store = store
        self.cleanup_on_failure = cleanup_on_failure
        self.store_guard = store_guard or (lambda: True)
        self.store_lock = store_lock
        self.signals = _NativeClipboardSignals()

    def run(self) -> None:
        cleanup: list[Path] = []
        succeeded = False
        try:
            manifest_path = self._ipc_file(self.manifest_name, ".json")
            cleanup.append(manifest_path)
            size = manifest_path.stat().st_size
            if not 0 < size <= _WINDOWS_MANIFEST_MAX_BYTES or size != self.manifest_bytes:
                raise ValueError("剪贴板清单大小不合法")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("剪贴板清单格式不合法")
            if manifest.get("protocol") != 1 or manifest.get("sequence") != self.sequence:
                raise ValueError("剪贴板清单序号不匹配")
            kind = manifest.get("kind")
            if kind != self.expected_kind:
                raise ValueError("剪贴板清单类型不匹配")
            source = str(manifest.get("source_app") or "")
            payload_path: Path | None = None
            if kind == "image":
                payload_path = self._ipc_file(str(manifest.get("payload_file") or ""), (".png", ".bmp"))
                cleanup.append(payload_path)
            if not self.store or kind in {"ignored", "unsupported"} or not self.store_guard():
                succeeded = True
                self.signals.consumed.emit(self.sequence)
                return
            if kind == "text":
                text = manifest.get("text")
                if not isinstance(text, str) or not text:
                    raise ValueError("剪贴板文本为空")
                prepared: object = text
            elif kind == "files":
                files = manifest.get("files")
                if not isinstance(files, list) or not files or not all(isinstance(path, str) for path in files):
                    raise ValueError("剪贴板文件列表不合法")
                prepared = files
            elif kind == "image" and payload_path is not None:
                prepared = self._decode_image(payload_path, manifest)
            else:
                raise ValueError(f"不支持的剪贴板清单类型：{kind}")
            lock_context = nullcontext() if self.store_lock is None else self.store_lock
            with lock_context:  # type: ignore[attr-defined]
                if not self.store_guard():
                    succeeded = True
                    self.signals.consumed.emit(self.sequence)
                    return
                if kind == "text":
                    item = self.repository.add_text(str(prepared), source)
                elif kind == "files":
                    item = self.repository.add_files(prepared, source)  # type: ignore[arg-type]
                else:
                    png, width, height = prepared  # type: ignore[misc]
                    item = self.repository.add_image(png, width, height, source)
                self.repository.cleanup(self.settings.max_history_items, self.settings.retention_days)
            succeeded = True
            self.signals.stored.emit(item, self.sequence)
        except Exception as exc:
            LOGGER.exception("Windows clipboard manifest persistence failed")
            self.signals.failed.emit(f"剪贴板记录失败：{exc}", self.sequence)
        finally:
            if succeeded or self.cleanup_on_failure:
                for path in cleanup:
                    with suppress(OSError):
                        path.unlink()

    def _decode_image(self, payload_path: Path, manifest: dict[object, object]) -> tuple[bytes, int, int]:
        size = payload_path.stat().st_size
        expected_size = manifest.get("bytes")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or not 0 < size <= _WINDOWS_IMAGE_MAX_BYTES
            or size != expected_size
        ):
            raise ValueError("剪贴板图片大小不合法")
        image = QImage.fromData(payload_path.read_bytes())
        if image.isNull():
            raise ValueError("剪贴板图片无法解码")
        claimed_width, claimed_height = manifest.get("width"), manifest.get("height")
        if (image.width(), image.height()) != (claimed_width, claimed_height):
            raise ValueError("剪贴板图片尺寸不匹配")
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise ValueError("剪贴板图片无法转换为 PNG")
        return bytes(buffer.data()), image.width(), image.height()

    def _ipc_file(self, name: str, suffix: str | tuple[str, ...]) -> Path:
        if not name or Path(name).name != name or not name.endswith(suffix):
            raise ValueError("剪贴板临时文件名不合法")
        unresolved = self.ipc_dir / name
        if unresolved.is_symlink():
            raise ValueError("剪贴板临时文件路径不合法")
        path = unresolved.resolve()
        if path.parent != self.ipc_dir or not path.is_file():
            raise ValueError("剪贴板临时文件路径不合法")
        return path


class ClipboardController(QObject):
    captured = Signal(object)
    failed = Signal(str)

    _SECRET_MARKERS = (
        "concealed",
        "transient",
        "excludeclipboardcontentfrommonitorprocessing",
        "canincludeinclipboardhistory",
        "org.nspasteboard.promised",
    )

    def __init__(
        self,
        clipboard: QClipboard,
        repository: HistoryRepository,
        settings: Callable[[], AppSettings],
        source_app: Callable[[], str],
    ) -> None:
        super().__init__()
        self.clipboard, self.repository = clipboard, repository
        self._settings, self._source_app = settings, source_app
        self._thread_pool = QThreadPool.globalInstance()
        self._active_tasks: set[QRunnable] = set()
        self._self_write = False
        self._suppressed_sequence: int | None = None
        self._last_sequence = self._sequence_number()
        self._windows_worker: WindowsWorkerSupervisor | None = None
        self._windows_ipc_dir = repository.data_dir / "ipc"
        self._windows_accepted_sequence = self._last_sequence
        self._windows_inflight_sequences: set[int] = set()
        self._windows_capture_lock = threading.RLock()
        self._windows_capture_epoch = 0
        self._windows_manifest_queue: deque[tuple[int, dict[object, object]]] = deque()
        self._windows_manifest_task: _NativeClipboardStoreTask | None = None
        self._windows_capture_failures: dict[int, int] = {}
        self._windows_pending_writes: dict[str, _PendingWindowsWrite] = {}
        self._windows_pending_verifications: dict[str, _PendingWindowsVerify] = {}
        self._windows_capture_pipeline_sequence: int | None = None
        self._windows_capture_preemption_session = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(220)
        self._poll_timer.timeout.connect(self._poll_native_sequence)
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._capture_pending)
        self._pending_sequence: int | None = None
        self._pending_source = ""
        self._capture_attempt = 0
        self._last_capture_error: Exception | None = None

    def start(self) -> None:
        if sys.platform == "win32":
            self._start_windows_worker()
            return
        self.clipboard.dataChanged.connect(self._clipboard_changed)
        # macOS does not emit reliable background QClipboard change events.
        # changeCount polling is a single integer read and stays below 0.02% CPU.
        if sys.platform == "darwin":
            self._poll_timer.start()

    def stop(self) -> None:
        native_windows = sys.platform == "win32"
        if native_windows:
            self._fail_all_windows_requests("Windows 剪贴板宿主已停止")
        if self._windows_worker is not None:
            self._windows_worker.stop()
            self._windows_worker = None
        self._windows_capture_pipeline_sequence = None
        self._windows_capture_preemption_session = ""
        self._poll_timer.stop()
        self._capture_timer.stop()
        self._pending_sequence = None
        self._pending_source = ""
        if native_windows:
            return
        with suppress(RuntimeError, TypeError):
            self.clipboard.dataChanged.disconnect(self._clipboard_changed)

    def sync_cursor(self) -> None:
        """Advance without reading; used by clear/pause to prevent resurrection."""
        self._capture_timer.stop()
        self._pending_sequence = None
        self._pending_source = ""
        if sys.platform == "win32":
            with self._windows_capture_lock:
                self._windows_capture_epoch += 1
                self._last_sequence = self._sequence_number()
                self._suppressed_sequence = self._last_sequence
                self._windows_accepted_sequence = self._last_sequence
            if self._windows_worker is not None:
                self._windows_worker.restart()
            return
        self._last_sequence = self._sequence_number()
        self._suppressed_sequence = self._last_sequence

    def _poll_native_sequence(self) -> None:
        sequence = self._sequence_number()
        if sequence is None or sequence == self._last_sequence:
            return
        self._last_sequence = sequence
        if sequence == self._suppressed_sequence:
            self._suppressed_sequence = None
            return
        self._schedule_capture(sequence)

    def _clipboard_changed(self) -> None:
        sequence = self._sequence_number()
        if sequence is not None:
            if sequence == self._last_sequence and not self._self_write:
                return
            self._last_sequence = sequence
        if self._self_write or (sequence is not None and sequence == self._suppressed_sequence):
            self._suppressed_sequence = None
            return
        self._schedule_capture(sequence)

    def _schedule_capture(self, sequence: int | None) -> None:
        self._pending_sequence = sequence
        try:
            self._pending_source = self._source_app()
        except Exception:
            self._pending_source = ""
            LOGGER.debug("Clipboard source lookup failed", exc_info=True)
        self._capture_attempt = 0
        self._last_capture_error = None
        self._capture_timer.start(_CLIPBOARD_SETTLE_MS)

    def _capture_pending(self) -> None:
        current_sequence = self._sequence_number()
        if (
            self._pending_sequence is not None
            and current_sequence is not None
            and current_sequence != self._pending_sequence
        ):
            self._last_sequence = current_sequence
            self._schedule_capture(current_sequence)
            return
        if self._capture_current():
            self._pending_sequence = None
            self._pending_source = ""
            self._last_capture_error = None
            return
        if self._capture_attempt < len(_CLIPBOARD_RETRY_DELAYS_MS):
            delay = _CLIPBOARD_RETRY_DELAYS_MS[self._capture_attempt]
            self._capture_attempt += 1
            self._capture_timer.start(delay)
            return
        error = self._last_capture_error or RuntimeError("系统剪贴板暂时不可用")
        LOGGER.warning("Clipboard remained unavailable after retries: %s", error)
        self.failed.emit(f"剪贴板记录失败：{error}")
        self._pending_sequence = None
        self._pending_source = ""
        self._last_capture_error = None

    def _capture_current(self) -> bool:
        if not self._settings().capture_enabled:
            return True
        try:
            mime = self.clipboard.mimeData(QClipboard.Mode.Clipboard)
            if mime is None:
                raise RuntimeError("系统剪贴板暂时不可用")
            if self._is_secret(mime):
                return True
            source = self._pending_source or self._source_app()
            local_files = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if local_files:
                payload = "files", local_files
            elif mime.hasImage():
                image = self.clipboard.image(QClipboard.Mode.Clipboard)
                payload = ("image", image.copy()) if not image.isNull() else None
            elif mime.hasText():
                text = mime.text()
                payload = ("text", text) if text else None
            else:
                payload = None
        except Exception as exc:
            self._last_capture_error = exc
            LOGGER.debug("Clipboard capture attempt failed", exc_info=True)
            return False
        if payload is None:
            return True
        try:
            kind, value = payload
            if kind == "files":
                self._finish_capture(self.repository.add_files(value, source))
            elif kind == "image":
                self._store_image_async(value, source)
            else:
                self._finish_capture(self.repository.add_text(value, source))
        except Exception as exc:
            LOGGER.exception("Clipboard persistence failed")
            self.failed.emit(f"剪贴板记录失败：{exc}")
        return True

    def _finish_capture(self, item: ClipItem) -> None:
        settings = self._settings()
        self.repository.cleanup(settings.max_history_items, settings.retention_days)
        self.captured.emit(item)

    def _store_image_async(self, image: QImage, source: str) -> None:
        task = _ImageStoreTask(self.repository, image, source, self._settings())
        self._active_tasks.add(task)
        task.signals.stored.connect(lambda item, task=task: self._image_stored(task, item))
        task.signals.failed.connect(lambda message, task=task: self._image_failed(task, message))
        self._thread_pool.start(task)

    def _image_stored(self, task: _ImageStoreTask, item: ClipItem) -> None:
        self._active_tasks.discard(task)
        self.captured.emit(item)

    def _image_failed(self, task: _ImageStoreTask, message: str) -> None:
        self._active_tasks.discard(task)
        self.failed.emit(message)

    def _start_windows_worker(self) -> None:
        if self._windows_worker is not None:
            return
        self._windows_ipc_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_windows_ipc_orphans()
        worker = WindowsWorkerSupervisor("clipboard", self._windows_worker_arguments)
        worker.message.connect(self._on_windows_worker_message)
        worker.failed.connect(self._on_windows_worker_failure)
        worker.health_changed.connect(self._on_windows_worker_health_changed)
        self._windows_worker = worker
        worker.start()

    def _windows_worker_arguments(self) -> list[str]:
        self._cleanup_windows_ipc_orphans(self._live_windows_ipc_sessions())
        arguments = ["--ipc-dir", str(self._windows_ipc_dir)]
        if self._windows_accepted_sequence is not None:
            arguments.extend(("--after-sequence", str(self._windows_accepted_sequence)))
        return arguments

    def _on_windows_worker_message(self, raw_message: object) -> None:
        if not isinstance(raw_message, dict):
            return
        kind = raw_message.get("type")
        if kind in {"capture_started", "capture_materializing"}:
            sequence = raw_message.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
                self._windows_capture_pipeline_sequence = sequence
            self._preempt_windows_capture_pipeline(sequence)
        elif kind == "clipboard":
            self._windows_capture_pipeline_sequence = None
            self._queue_windows_manifest(raw_message)
        elif kind == "write_result":
            self._handle_windows_write_result(raw_message)
        elif kind == "verify_result":
            self._handle_windows_verify_result(raw_message)
        elif kind == "error":
            self._windows_capture_pipeline_sequence = None
            if not raw_message.get("retrying") and not raw_message.get("fatal"):
                self._retry_windows_capture(raw_message)

    def _queue_windows_manifest(self, message: dict[object, object]) -> None:
        sequence = message.get("sequence")
        manifest_name = message.get("manifest")
        manifest_bytes = message.get("manifest_bytes")
        kind = message.get("kind")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or not isinstance(manifest_name, str)
            or not isinstance(manifest_bytes, int)
            or isinstance(manifest_bytes, bool)
            or not isinstance(kind, str)
        ):
            self.failed.emit("剪贴板宿主返回了无效数据")
            return
        if sequence in self._windows_inflight_sequences:
            self._discard_duplicate_windows_manifest(message)
            return
        self._last_sequence = sequence
        self._windows_inflight_sequences.add(sequence)
        self._windows_manifest_queue.append((self._windows_capture_epoch, message))
        self._start_next_windows_manifest()

    def _start_next_windows_manifest(self) -> None:
        if self._windows_manifest_task is not None or not self._windows_manifest_queue:
            return
        epoch, message = self._windows_manifest_queue.popleft()
        sequence = int(message["sequence"])
        store = self._settings().capture_enabled and sequence != self._suppressed_sequence
        if sequence == self._suppressed_sequence:
            self._suppressed_sequence = None
        prior_failures = self._windows_capture_failures.get(sequence, 0)
        task = _NativeClipboardStoreTask(
            self.repository,
            self._windows_ipc_dir,
            str(message["manifest"]),
            int(message["manifest_bytes"]),
            sequence,
            str(message["kind"]),
            self._settings(),
            store=store,
            cleanup_on_failure=prior_failures >= 2,
            store_guard=lambda epoch=epoch: epoch == self._windows_capture_epoch,
            store_lock=self._windows_capture_lock,
        )
        self._windows_manifest_task = task
        self._active_tasks.add(task)
        task.signals.stored.connect(
            lambda item, value, task=task, epoch=epoch: self._windows_item_stored(task, item, value, epoch)
        )
        task.signals.consumed.connect(
            lambda value, task=task, epoch=epoch: self._windows_manifest_consumed(task, value, epoch)
        )
        task.signals.failed.connect(
            lambda text, value, task=task, message=message, epoch=epoch: self._windows_manifest_failed(
                task, message, text, value, epoch
            )
        )
        self._thread_pool.start(task)

    def _discard_duplicate_windows_manifest(self, message: dict[object, object]) -> None:
        task = _NativeClipboardStoreTask(
            self.repository,
            self._windows_ipc_dir,
            str(message["manifest"]),
            int(message["manifest_bytes"]),
            int(message["sequence"]),
            str(message["kind"]),
            self._settings(),
            store=False,
            cleanup_on_failure=True,
        )
        self._active_tasks.add(task)
        task.signals.consumed.connect(lambda _value, task=task: self._active_tasks.discard(task))
        task.signals.failed.connect(lambda _text, _value, task=task: self._active_tasks.discard(task))
        self._thread_pool.start(task)

    def _windows_item_stored(
        self,
        task: _NativeClipboardStoreTask,
        item: ClipItem,
        sequence: int,
        epoch: int,
    ) -> None:
        current = self._finish_windows_task(task, sequence, epoch)
        if current:
            self.captured.emit(item)

    def _windows_manifest_consumed(
        self,
        task: _NativeClipboardStoreTask,
        sequence: int,
        epoch: int,
    ) -> None:
        self._finish_windows_task(task, sequence, epoch)

    def _windows_manifest_failed(
        self,
        task: _NativeClipboardStoreTask,
        manifest: dict[object, object],
        message: str,
        sequence: int,
        epoch: int,
    ) -> None:
        self._remove_windows_task(task, sequence)
        if epoch != self._windows_capture_epoch:
            self._discard_duplicate_windows_manifest(manifest)
            QTimer.singleShot(0, self._start_next_windows_manifest)
            return
        attempts = self._windows_capture_failures.get(sequence, 0) + 1
        self._windows_capture_failures[sequence] = attempts
        if attempts < 3:
            self._windows_inflight_sequences.add(sequence)
            self._windows_manifest_queue.appendleft((epoch, manifest))
            QTimer.singleShot(50, self._start_next_windows_manifest)
            return
        self._windows_capture_failures.pop(sequence, None)
        self._windows_accepted_sequence = sequence
        self.failed.emit(message)
        QTimer.singleShot(0, self._start_next_windows_manifest)

    def _finish_windows_task(self, task: _NativeClipboardStoreTask, sequence: int, epoch: int) -> bool:
        self._remove_windows_task(task, sequence)
        if epoch != self._windows_capture_epoch:
            QTimer.singleShot(0, self._start_next_windows_manifest)
            return False
        self._windows_capture_failures.pop(sequence, None)
        self._windows_accepted_sequence = sequence
        QTimer.singleShot(0, self._start_next_windows_manifest)
        return True

    def _remove_windows_task(self, task: _NativeClipboardStoreTask, sequence: int) -> None:
        self._active_tasks.discard(task)
        if self._windows_manifest_task is task:
            self._windows_manifest_task = None
        self._windows_inflight_sequences.discard(sequence)

    def _retry_windows_capture(self, message: dict[object, object]) -> None:
        sequence = message.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return
        self._restart_or_abandon_windows_sequence(
            sequence,
            f"剪贴板记录失败：{message.get('message') or '无法读取当前内容'}",
        )

    def _restart_or_abandon_windows_sequence(self, sequence: int, message: str) -> None:
        attempts = self._windows_capture_failures.get(sequence, 0) + 1
        self._windows_capture_failures[sequence] = attempts
        if attempts <= 3 and self._windows_worker is not None:
            LOGGER.warning("Restarting Windows clipboard helper after capture failure for sequence %d", sequence)
            self._windows_worker.restart()
            return
        self._windows_accepted_sequence = sequence
        self._windows_capture_failures.pop(sequence, None)
        self.failed.emit(message)

    def _live_windows_ipc_sessions(self) -> set[str]:
        sessions: set[str] = set()
        if self._windows_worker is not None and self._windows_worker.session_id:
            sessions.add(self._windows_worker.session_id)
        for _epoch, message in self._windows_manifest_queue:
            session_id = _windows_ipc_session(str(message.get("manifest") or ""))
            if session_id is not None:
                sessions.add(session_id)
        for task in self._active_tasks:
            if not isinstance(task, _NativeClipboardStoreTask):
                continue
            session_id = _windows_ipc_session(task.manifest_name)
            if session_id is not None:
                sessions.add(session_id)
        sessions.update(
            pending.session_id
            for pending in self._windows_pending_writes.values()
            if pending.session_id
        )
        return sessions

    def _cleanup_windows_ipc_orphans(self, preserve_sessions: set[str] | None = None) -> None:
        preserved = preserve_sessions or set()
        try:
            entries = tuple(self._windows_ipc_dir.iterdir())
        except OSError:
            return
        for path in entries:
            if path.is_symlink() or not path.is_file():
                continue
            if path.name.startswith(
                (
                    "manifest-",
                    "clip-",
                    "write-manifest-",
                    "write-png-",
                    "write-dibv5-",
                    "write-dib-",
                    ".manifest-",
                    ".clip-",
                    ".write-manifest-",
                    ".write-png-",
                    ".write-dibv5-",
                    ".write-dib-",
                )
            ):
                if _windows_ipc_session(path.name) in preserved:
                    continue
                with suppress(OSError):
                    path.unlink()

    def _mime_factory(self, item: ClipItem) -> Callable[[], QMimeData] | None:
        if item.kind is ClipKind.TEXT:
            def text_mime() -> QMimeData:
                mime = QMimeData()
                mime.setText(item.text)
                return mime

            return text_mime
        if item.kind is ClipKind.FILES:
            def files_mime() -> QMimeData:
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(path) for path in item.files])
                return mime

            return files_mime

        image = QImage(item.image_path)
        if image.isNull():
            return None
        try:
            png = Path(item.image_path).read_bytes()
        except OSError:
            return None
        if not png.startswith(_PNG_SIGNATURE):
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG"):
                return None
            png = bytes(buffer.data())

        def image_mime() -> QMimeData:
            mime = QMimeData()
            mime.setImageData(image)
            mime.setData("image/png", png)
            return mime

        return image_mime

    def write_item(self, item: ClipItem) -> bool:
        if sys.platform == "win32":
            LOGGER.error("Synchronous GUI-process clipboard writes are disabled on Windows")
            return False
        mime_factory = self._mime_factory(item)
        if mime_factory is None:
            return False
        mime = mime_factory()
        try:
            self._self_write = True
            self.clipboard.setMimeData(mime, QClipboard.Mode.Clipboard)
            self._last_sequence = self._sequence_number()
            self._suppressed_sequence = self._last_sequence
            return True
        except Exception:
            LOGGER.exception("Clipboard write failed")
            return False
        finally:
            self._self_write = False

    def request_write(
        self,
        item: ClipItem,
        callback: Callable[[ClipboardWriteReceipt | None, str], None],
    ) -> None:
        if sys.platform != "win32":
            request_id = uuid.uuid4().hex
            if self.write_item(item):
                callback(
                    ClipboardWriteReceipt(request_id, item.kind, self._sequence_number()),
                    "",
                )
            else:
                callback(None, "无法写入系统剪贴板")
            return
        worker = self._windows_worker
        if worker is None:
            callback(None, "Windows 剪贴板宿主不可用")
            return
        request_id = uuid.uuid4().hex
        pending = _PendingWindowsWrite(
            request_id,
            item,
            callback,
            time.monotonic() + (_WINDOWS_WRITE_DEADLINE_MS / 1_000),
        )
        self._windows_pending_writes[request_id] = pending
        QTimer.singleShot(
            _WINDOWS_WRITE_DEADLINE_MS,
            lambda request_id=request_id: self._windows_write_timed_out(request_id),
        )
        if self._windows_capture_pipeline_busy(worker):
            pending.waiting_for_retry = True
            self._preempt_windows_capture_pipeline(
                self._windows_capture_pipeline_sequence
            )
            return
        if not worker.is_healthy or not _is_request_id(worker.session_id):
            pending.waiting_for_retry = True
            return
        self._prepare_windows_write(pending)

    def request_verify(
        self,
        receipt: ClipboardWriteReceipt,
        callback: Callable[[bool, str], None],
    ) -> None:
        if sys.platform != "win32":
            callback(True, "")
            return
        worker = self._windows_worker
        if (
            worker is None
            or not _is_request_id(receipt.request_id)
            or receipt.sequence is None
            or receipt.sequence <= 0
        ):
            callback(False, "Windows 剪贴板宿主不可用")
            return
        if (
            self._windows_capture_pipeline_busy(worker)
            or self._windows_capture_preemption_session == worker.session_id
        ):
            callback(False, "剪贴板内容已变化")
            return
        if not worker.is_healthy:
            callback(False, "Windows 剪贴板宿主不可用")
            return
        if receipt.request_id in self._windows_pending_verifications:
            callback(False, "剪贴板验证请求已在处理中")
            return
        pending = _PendingWindowsVerify(
            receipt,
            callback,
            time.monotonic() + (_WINDOWS_VERIFY_DEADLINE_MS / 1_000),
        )
        self._windows_pending_verifications[receipt.request_id] = pending
        QTimer.singleShot(
            _WINDOWS_VERIFY_DEADLINE_MS,
            lambda request_id=receipt.request_id: self._windows_verify_timed_out(request_id),
        )
        if not self._send_windows_verify(pending):
            self._finish_windows_verify(receipt.request_id, False, "无法发送剪贴板验证请求")

    @staticmethod
    def _windows_capture_pipeline_busy(worker: WindowsWorkerSupervisor) -> bool:
        return bool(getattr(worker, "is_capture_pipeline_busy", False))

    def _preempt_windows_capture_pipeline(self, sequence: object = None) -> None:
        # Verification is a guard immediately before Ctrl+V. Once an external
        # capture starts, its receipt cannot still describe the clipboard.
        for request_id in tuple(self._windows_pending_verifications):
            self._finish_windows_verify(request_id, False, "剪贴板内容已变化")

        if not self._windows_pending_writes:
            return
        worker = self._windows_worker
        if worker is None:
            return

        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence >= 0
        ):
            # Preemption intentionally abandons this external capture. Advance
            # the restart cursor so the replacement helper does not recapture
            # the same sequence and starve the user-initiated write again.
            with self._windows_capture_lock:
                self._windows_accepted_sequence = sequence
                self._last_sequence = sequence
            self._windows_capture_failures.pop(sequence, None)

        for pending in tuple(self._windows_pending_writes.values()):
            pending.awaiting_result = False
            pending.waiting_for_retry = True
            self._cleanup_windows_write_paths(pending.paths)
            pending.paths = ()

        session_id = worker.session_id
        if (
            not _is_request_id(session_id)
            or self._windows_capture_preemption_session == session_id
        ):
            return
        self._windows_capture_preemption_session = session_id
        worker.restart()

    def _prepare_windows_write(self, pending: _PendingWindowsWrite) -> None:
        worker = self._windows_worker
        if time.monotonic() >= pending.deadline:
            self._finish_windows_write(pending.request_id, None, "Windows 剪贴板宿主不可用")
            return
        if worker is None:
            self._finish_windows_write(pending.request_id, None, "Windows 剪贴板宿主不可用")
            return
        if (
            self._windows_capture_pipeline_busy(worker)
            or self._windows_capture_preemption_session == worker.session_id
        ):
            pending.waiting_for_retry = True
            if self._windows_capture_pipeline_busy(worker):
                self._preempt_windows_capture_pipeline(
                    self._windows_capture_pipeline_sequence
                )
            return
        if not worker.is_healthy or not _is_request_id(worker.session_id):
            pending.waiting_for_retry = True
            return
        pending.preparations += 1
        pending.preparing = True
        pending.awaiting_result = False
        pending.waiting_for_retry = False
        pending.session_id = worker.session_id
        task = _WindowsWritePreparationTask(
            self._windows_ipc_dir,
            pending.item,
            pending.request_id,
            pending.session_id,
        )
        self._active_tasks.add(task)
        task.signals.finished.connect(
            lambda request_id, prepared, error, task=task: self._windows_write_prepared(
                task,
                request_id,
                prepared,
                error,
            )
        )
        self._thread_pool.start(task)

    def _windows_write_prepared(
        self,
        task: _WindowsWritePreparationTask,
        request_id: str,
        prepared: object,
        error: str,
    ) -> None:
        self._active_tasks.discard(task)
        pending = self._windows_pending_writes.get(request_id)
        if pending is None:
            if isinstance(prepared, _PreparedWindowsWrite):
                self._cleanup_windows_write_paths(prepared.paths)
            return
        pending.preparing = False
        worker = self._windows_worker
        stale_preparation = (
            pending.waiting_for_retry
            or worker is None
            or not worker.is_healthy
            or worker.session_id != task.session_id
            or self._windows_capture_preemption_session == task.session_id
            or self._windows_capture_pipeline_busy(worker)
        )
        if stale_preparation:
            if isinstance(prepared, _PreparedWindowsWrite):
                self._cleanup_windows_write_paths(prepared.paths)
            pending.awaiting_result = False
            pending.waiting_for_retry = True
            if time.monotonic() >= pending.deadline:
                self._finish_windows_write(
                    request_id,
                    None,
                    "Windows 剪贴板写入确认超时",
                )
                return
            if worker is not None and self._windows_capture_pipeline_busy(worker):
                self._preempt_windows_capture_pipeline(
                    self._windows_capture_pipeline_sequence
                )
                return
            if (
                worker is not None
                and worker.is_healthy
                and _is_request_id(worker.session_id)
                and worker.session_id != task.session_id
                and self._windows_capture_preemption_session != worker.session_id
            ):
                # Preparation/session churn is bounded by the request deadline,
                # not by the two native SetClipboardData attempts. A stale
                # preparation must not consume the one transient native replay.
                self._prepare_windows_write(pending)
            return
        if error or not isinstance(prepared, _PreparedWindowsWrite):
            self._finish_windows_write(
                request_id,
                None,
                f"无法准备剪贴板内容：{error or '无效的写入清单'}",
            )
            return
        if (
            worker is None
            or not worker.is_healthy
            or worker.session_id != prepared.session_id
            or time.monotonic() >= pending.deadline
        ):
            self._cleanup_windows_write_paths(prepared.paths)
            if worker is not None and worker.is_healthy:
                self._prepare_windows_write(pending)
            else:
                pending.waiting_for_retry = True
            return
        pending.paths = prepared.paths
        pending.session_id = prepared.session_id
        pending.attempts += 1
        pending.awaiting_result = True
        sent = worker.send(
            {
                "type": "write_clipboard",
                "request_id": request_id,
                "kind": pending.item.kind.value,
                "manifest": prepared.manifest_name,
                "manifest_bytes": prepared.manifest_bytes,
            }
        )
        if not sent:
            pending.awaiting_result = False
            self._finish_windows_write(request_id, None, "无法发送 Windows 剪贴板写入请求")

    def _handle_windows_write_result(self, message: dict[object, object]) -> None:
        request_id = message.get("request_id")
        if not _is_request_id(request_id):
            return
        pending = self._windows_pending_writes.get(request_id)
        worker = self._windows_worker
        message_session = message.get("session_id")
        if (
            pending is None
            or message.get("kind") != pending.item.kind.value
            or not pending.awaiting_result
            or pending.waiting_for_retry
            or worker is None
            or not worker.is_healthy
            or worker.session_id != pending.session_id
            or self._windows_capture_preemption_session == pending.session_id
            or (
                isinstance(message_session, str)
                and message_session != pending.session_id
            )
        ):
            return
        sequence = message.get("sequence")
        if (
            message.get("ok") is True
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > 0
        ):
            receipt = ClipboardWriteReceipt(request_id, pending.item.kind, sequence)
            self._last_sequence = sequence
            self._suppressed_sequence = sequence
            self._finish_windows_write(request_id, receipt, "")
            return
        code = str(message.get("code") or "")
        if (
            code in {"clipboard_busy", "verification_failed", "close_failed"}
            and pending.attempts < _WINDOWS_MAX_WRITE_ATTEMPTS
            and time.monotonic() < pending.deadline
        ):
            # These failures describe a contested/indeterminate Win32
            # transaction, not bad source data. Rebuild all session artifacts
            # and replay the same logical request exactly once.
            pending.awaiting_result = False
            self._cleanup_windows_write_paths(pending.paths)
            pending.paths = ()
            self._prepare_windows_write(pending)
            return
        error = str(message.get("error") or code or "原生剪贴板写入失败")
        self._finish_windows_write(request_id, None, error)

    def _send_windows_verify(self, pending: _PendingWindowsVerify) -> bool:
        worker = self._windows_worker
        if (
            worker is None
            or not worker.is_healthy
            or time.monotonic() >= pending.deadline
            or self._windows_capture_pipeline_busy(worker)
            or self._windows_capture_preemption_session == worker.session_id
        ):
            return False
        pending.attempts += 1
        pending.waiting_for_retry = False
        pending.session_id = worker.session_id
        return worker.send(
            {
                "type": "verify_clipboard",
                "request_id": pending.receipt.request_id,
                "kind": pending.receipt.kind.value,
                "sequence": pending.receipt.sequence,
            }
        )

    def _handle_windows_verify_result(self, message: dict[object, object]) -> None:
        request_id = message.get("request_id")
        if not _is_request_id(request_id):
            return
        pending = self._windows_pending_verifications.get(request_id)
        worker = self._windows_worker
        message_session = message.get("session_id")
        if (
            pending is None
            or message.get("kind") != pending.receipt.kind.value
            or pending.waiting_for_retry
            or worker is None
            or not worker.is_healthy
            or worker.session_id != pending.session_id
            or self._windows_capture_pipeline_busy(worker)
            or self._windows_capture_preemption_session == pending.session_id
            or (
                isinstance(message_session, str)
                and message_session != pending.session_id
            )
        ):
            return
        if message.get("ok") is True:
            sequence = _positive_protocol_int(message.get("sequence"))
            if sequence is None:
                return
            # Windows may advance the sequence while synthesizing a compatible
            # format for the target. The native broker has already revalidated
            # the request marker, owner, and complete required format set at
            # this returned sequence, so an exact match to the original ACK is
            # neither necessary nor correct.
            self._last_sequence = sequence
            self._suppressed_sequence = sequence
            self._finish_windows_verify(request_id, True, "")
            return
        error = str(message.get("error") or message.get("code") or "剪贴板内容已变化")
        self._finish_windows_verify(request_id, False, error)

    def _on_windows_worker_failure(self, message: str) -> None:
        self.failed.emit(message)
        worker = self._windows_worker
        if worker is None or not worker.is_healthy:
            self._on_windows_worker_health_changed(False)

    def _on_windows_worker_health_changed(self, healthy: bool) -> None:
        if not healthy:
            now = time.monotonic()
            for pending in tuple(self._windows_pending_writes.values()):
                if pending.attempts >= _WINDOWS_MAX_WRITE_ATTEMPTS or now >= pending.deadline:
                    self._finish_windows_write(
                        pending.request_id,
                        None,
                        "Windows 剪贴板宿主在写入期间中断",
                    )
                    continue
                pending.awaiting_result = False
                self._cleanup_windows_write_paths(pending.paths)
                pending.paths = ()
                pending.waiting_for_retry = True
            for pending in tuple(self._windows_pending_verifications.values()):
                if pending.attempts >= _WINDOWS_MAX_WRITE_ATTEMPTS or now >= pending.deadline:
                    self._finish_windows_verify(
                        pending.receipt.request_id,
                        False,
                        "Windows 剪贴板宿主在验证期间中断",
                    )
                else:
                    pending.waiting_for_retry = True
            return

        worker = self._windows_worker
        if (
            self._windows_capture_preemption_session
            and (
                worker is None
                or worker.session_id == self._windows_capture_preemption_session
            )
        ):
            return
        self._windows_capture_preemption_session = ""
        self._windows_capture_pipeline_sequence = None
        for pending in tuple(self._windows_pending_writes.values()):
            if pending.waiting_for_retry and not pending.preparing:
                self._prepare_windows_write(pending)
        for pending in tuple(self._windows_pending_verifications.values()):
            if pending.waiting_for_retry and not self._send_windows_verify(pending):
                self._finish_windows_verify(
                    pending.receipt.request_id,
                    False,
                    "无法重试剪贴板验证",
                )

    def _windows_write_timed_out(self, request_id: str) -> None:
        if request_id in self._windows_pending_writes:
            self._finish_windows_write(request_id, None, "Windows 剪贴板写入确认超时")

    def _windows_verify_timed_out(self, request_id: str) -> None:
        if request_id in self._windows_pending_verifications:
            self._finish_windows_verify(request_id, False, "Windows 剪贴板验证超时")

    def _finish_windows_write(
        self,
        request_id: str,
        receipt: ClipboardWriteReceipt | None,
        error: str,
    ) -> None:
        pending = self._windows_pending_writes.pop(request_id, None)
        if pending is None:
            return
        self._cleanup_windows_write_paths(pending.paths)
        try:
            pending.callback(receipt, error)
        except Exception:
            LOGGER.exception("Windows clipboard write callback failed")

    def _finish_windows_verify(self, request_id: str, ok: bool, error: str) -> None:
        pending = self._windows_pending_verifications.pop(request_id, None)
        if pending is None:
            return
        try:
            pending.callback(ok, error)
        except Exception:
            LOGGER.exception("Windows clipboard verification callback failed")

    def _fail_all_windows_requests(self, error: str) -> None:
        for request_id in tuple(self._windows_pending_writes):
            self._finish_windows_write(request_id, None, error)
        for request_id in tuple(self._windows_pending_verifications):
            self._finish_windows_verify(request_id, False, error)

    @staticmethod
    def _cleanup_windows_write_paths(paths: tuple[Path, ...]) -> None:
        for path in paths:
            with suppress(OSError):
                path.unlink()

    def _sequence_number(self) -> int | None:
        try:
            if sys.platform == "darwin":
                from AppKit import NSPasteboard

                return int(NSPasteboard.generalPasteboard().changeCount())
            if sys.platform == "win32":
                return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception:
            LOGGER.debug("Native clipboard sequence unavailable", exc_info=True)
        return None

    @classmethod
    def _is_secret(cls, mime: QMimeData) -> bool:
        formats = [(name, name.casefold()) for name in mime.formats()]
        # A hard opt-out always wins, even when another format explicitly
        # allows Windows clipboard history.
        hard_markers = tuple(marker for marker in cls._SECRET_MARKERS if marker != "canincludeinclipboardhistory")
        if any(marker in folded for _, folded in formats for marker in hard_markers):
            return True
        # Windows CanIncludeInClipboardHistory is private only when its DWORD is 0.
        for name, folded in formats:
            if "canincludeinclipboardhistory" in folded:
                value = bytes(mime.data(name))
                return not bool(value and any(value))
        return False


@dataclass(slots=True)
class ForegroundTargetHandle:
    kind: str
    identifier: int
    name: str = ""
    target_thread_id: int | None = None
    target_process_id: int | None = None
    focus_window: int | None = None
    focus_thread_id: int | None = None
    focus_process_id: int | None = None

    def activate(self) -> bool:
        try:
            if self.kind == "mac":
                from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.identifier)
                return bool(app and app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
            if self.kind == "windows":
                user32 = ctypes.windll.user32
                handle = _windows_handle(self.identifier)
                if not user32.IsWindow(handle):
                    return False
                target_identity = self._validated_windows_target(user32)
                if target_identity is None:
                    return False
                target_thread_id, target_process_id = target_identity
                self.target_thread_id = target_thread_id
                self.target_process_id = target_process_id
                focus_window = (
                    self._validated_windows_focus(user32)
                    if target_process_id
                    else None
                )
                # SW_RESTORE also unmaximizes a maximized window. Only use it
                # for a genuinely minimized target so sending never changes the
                # user's chosen normal/maximized window state.
                if user32.IsIconic(handle):
                    user32.ShowWindow(handle, 9)  # SW_RESTORE
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
                foreground_ready = _windows_foreground_window() == self.identifier
                if focus_window is None:
                    if not foreground_ready:
                        _run_windows_focus_helper(
                            mode="target",
                            target_window=self.identifier,
                            target_thread_id=target_thread_id,
                            target_process_id=target_process_id,
                        )
                        foreground_ready = (
                            _windows_foreground_window() == self.identifier
                        )
                    current_focus = None
                    if foreground_ready and target_thread_id:
                        current_focus = self._adopt_current_windows_focus(
                            user32,
                            target_thread_id,
                        )
                    if current_focus is not None:
                        LOGGER.info(
                            "Windows target adopted current focus; hwnd=%d focus=%d",
                            self.identifier,
                            current_focus[0],
                        )
                        return True
                    LOGGER.info(
                        "Windows target restored; hwnd=%d foreground=%s focus=unavailable",
                        self.identifier,
                        foreground_ready,
                    )
                    # Apps backed by a dedicated renderer may recreate their
                    # focused child after the top-level activation returns.
                    # SelectionSender waits and performs a strict final check
                    # before it injects Ctrl+V.
                    return foreground_ready
                focus_identifier, focus_thread_id = focus_window
                if (
                    foreground_ready
                    and _windows_focus_window(user32, focus_thread_id)
                    == focus_identifier
                ):
                    LOGGER.info(
                        "Windows target and focus restored natively; hwnd=%d focus=%d",
                        self.identifier,
                        focus_identifier,
                    )
                    return True
                restored = _run_windows_focus_helper(
                    mode="target",
                    target_window=self.identifier,
                    target_thread_id=target_thread_id,
                    target_process_id=target_process_id,
                    focus_window=focus_identifier,
                    focus_thread_id=focus_thread_id,
                    focus_process_id=self.focus_process_id,
                )
                foreground_ready = (
                    _windows_foreground_window() == self.identifier
                )
                focus_ready = (
                    foreground_ready
                    and _windows_focus_window(user32, focus_thread_id)
                    == focus_identifier
                )
                if foreground_ready and not focus_ready:
                    focus_ready = (
                        self._adopt_current_windows_focus(
                            user32,
                            target_thread_id,
                        )
                        is not None
                    )
                LOGGER.info(
                    "Windows target focus helper completed; target=%d focus=%d "
                    "helper=%s foreground=%s focus_ready=%s",
                    self.identifier,
                    focus_identifier,
                    restored,
                    foreground_ready,
                    focus_ready,
                )
                # A successful top-level transition is enough to enter the
                # bounded async focus-resolution phase. No paste happens until
                # is_active() validates a target-owned focus window.
                return foreground_ready
        except Exception:
            LOGGER.exception("Could not restore target window")
        return False

    def _validated_windows_target(
        self,
        user32: object,
    ) -> tuple[int, int] | None:
        target_thread_id, target_process_id = _windows_window_identity(
            user32,
            self.identifier,
        )
        if (
            not target_thread_id
            or not target_process_id
            or (
                self.target_thread_id is not None
                and target_thread_id != self.target_thread_id
            )
            or (
                self.target_process_id is not None
                and target_process_id != self.target_process_id
            )
        ):
            LOGGER.warning(
                "Captured Windows target identity is stale; hwnd=%d",
                self.identifier,
            )
            return None
        return target_thread_id, target_process_id

    def _validated_windows_focus(
        self,
        user32: object,
    ) -> tuple[int, int] | None:
        if (
            self.focus_window is None
            or self.focus_thread_id is None
            or self.focus_process_id is None
        ):
            return None
        focus_handle = _windows_handle(self.focus_window)
        if not user32.IsWindow(focus_handle):
            LOGGER.warning(
                "Captured Windows focus window is no longer valid; hwnd=%d",
                self.focus_window,
            )
            return None
        focus_thread_id, focus_process_id = _windows_window_identity(
            user32,
            self.focus_window,
        )
        root_window = _windows_root_window(user32, self.focus_window)
        if (
            focus_thread_id != self.focus_thread_id
            or focus_process_id != self.focus_process_id
            or root_window != self.identifier
        ):
            LOGGER.warning(
                "Captured Windows focus identity is stale; hwnd=%d",
                self.focus_window,
            )
            return None
        return self.focus_window, focus_thread_id

    def _adopt_current_windows_focus(
        self,
        user32: object,
        target_thread_id: int,
    ) -> tuple[int, int] | None:
        candidate_threads = dict.fromkeys(
            thread_id
            for thread_id in (0, target_thread_id, self.focus_thread_id)
            if thread_id is not None
        )
        for thread_id in candidate_threads:
            focus_identifier = _windows_focus_window(user32, thread_id)
            if not focus_identifier or not user32.IsWindow(
                _windows_handle(focus_identifier)
            ):
                continue
            focus_thread_id, focus_process_id = _windows_window_identity(
                user32,
                focus_identifier,
            )
            expected_focus_process_id = (
                self.focus_process_id or self.target_process_id
            )
            if (
                not focus_thread_id
                or not focus_process_id
                or (
                    expected_focus_process_id is not None
                    and focus_process_id != expected_focus_process_id
                )
                or _windows_root_window(user32, focus_identifier)
                != self.identifier
            ):
                continue
            self.focus_window = focus_identifier
            self.focus_thread_id = focus_thread_id
            self.focus_process_id = focus_process_id
            return focus_identifier, focus_thread_id
        return None

    def is_active(self) -> bool:
        try:
            if self.kind == "mac":
                from AppKit import NSWorkspace

                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                return bool(app and int(app.processIdentifier()) == self.identifier)
            if self.kind == "windows":
                if _windows_foreground_window() != self.identifier:
                    return False
                user32 = ctypes.windll.user32
                target_thread_id = self.target_thread_id or 0
                if (
                    self.target_thread_id is not None
                    or self.target_process_id is not None
                ):
                    target_identity = self._validated_windows_target(user32)
                    if target_identity is None:
                        return False
                    target_thread_id = target_identity[0]
                if (
                    self.focus_window is None
                    or self.focus_thread_id is None
                    or self.focus_process_id is None
                ):
                    return bool(
                        target_thread_id
                        and self._adopt_current_windows_focus(
                            user32,
                            target_thread_id,
                        )
                        is not None
                    )
                validated = self._validated_windows_focus(user32)
                if (
                    validated is not None
                    and _windows_focus_window(user32, validated[1])
                    == validated[0]
                ):
                    return True
                return bool(
                    target_thread_id
                    and self._adopt_current_windows_focus(
                        user32,
                        target_thread_id,
                    )
                    is not None
                )
        except Exception:
            return False
        return False


class LaunchAtLoginManager:
    """Own the current user's platform startup registration."""

    _MACOS_LABEL = "com.clipsoon.app"
    _WINDOWS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _WINDOWS_VALUE = "ClipSoon"

    def __init__(
        self,
        *,
        platform: str | None = None,
        executable: Path | None = None,
        frozen: bool | None = None,
        home: Path | None = None,
    ) -> None:
        self.platform = platform or sys.platform
        self.executable = Path(executable or sys.executable).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.home = Path.home() if home is None else home

    @property
    def command(self) -> tuple[str, ...]:
        executable = self.executable
        if self.platform == "win32" and not self.frozen:
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.exists():
                executable = pythonw
        if self.frozen:
            return (str(executable),)
        return (str(executable), "-m", "clipsoon")

    def set_enabled(self, enabled: bool) -> tuple[bool, str]:
        try:
            if self.platform == "darwin":
                self._set_macos_enabled(enabled)
            elif self.platform == "win32":
                self._set_windows_enabled(enabled)
            else:
                return False, "当前平台不支持开机自启动"
        except Exception as exc:
            LOGGER.exception("Could not update launch-at-login registration")
            return False, f"无法更新开机自启动：{exc}"
        return True, "已开启开机自启动" if enabled else "已关闭开机自启动"

    def _set_macos_enabled(self, enabled: bool) -> None:
        launch_agents = self.home / "Library" / "LaunchAgents"
        target = launch_agents / f"{self._MACOS_LABEL}.plist"
        if not enabled:
            target.unlink(missing_ok=True)
            return
        launch_agents.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": self._MACOS_LABEL,
            "ProgramArguments": list(self.command),
            "ProcessType": "Interactive",
            "RunAtLoad": True,
        }
        temporary = target.with_suffix(".plist.tmp")
        try:
            temporary.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _set_windows_enabled(self, enabled: bool) -> None:
        import winreg

        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._WINDOWS_KEY) as key:
                winreg.SetValueEx(
                    key,
                    self._WINDOWS_VALUE,
                    0,
                    winreg.REG_SZ,
                    subprocess.list2cmdline(self.command),
                )
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self._WINDOWS_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, self._WINDOWS_VALUE)
        except FileNotFoundError:
            pass


class PlatformBridge:
    _MACOS_ACCESSIBILITY_URL = (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    )

    @staticmethod
    def current_app_name() -> str:
        try:
            if sys.platform == "darwin":
                from AppKit import NSWorkspace

                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                return str(app.localizedName() or "") if app else ""
            if sys.platform == "win32":
                return PlatformBridge.window_name(_windows_foreground_window())
        except Exception:
            LOGGER.debug("Current app name unavailable", exc_info=True)
        return ""

    @staticmethod
    def window_name(identifier: int) -> str:
        if sys.platform != "win32" or identifier <= 0:
            return ""
        try:
            user32 = ctypes.windll.user32
            handle = _windows_handle(identifier)
            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, len(buffer))
            return buffer.value
        except Exception:
            LOGGER.debug("Windows window title unavailable", exc_info=True)
            return ""

    @staticmethod
    def target_from_window_id(
        identifier: int,
        *,
        target_thread_id: int | None = None,
        target_process_id: int | None = None,
        focus_window: int | None = None,
        focus_thread_id: int | None = None,
        focus_process_id: int | None = None,
    ) -> ForegroundTargetHandle | None:
        if sys.platform != "win32" or identifier <= 0:
            return None
        try:
            user32 = ctypes.windll.user32
            handle = _windows_handle(identifier)
            if user32.IsWindow(handle):
                current_thread_id, current_process_id = _windows_window_identity(
                    user32,
                    identifier,
                )
                if (
                    target_thread_id is not None
                    and current_thread_id != target_thread_id
                ) or (
                    target_process_id is not None
                    and current_process_id != target_process_id
                ):
                    return None
                return ForegroundTargetHandle(
                    "windows",
                    identifier,
                    PlatformBridge.window_name(identifier),
                    current_thread_id,
                    current_process_id,
                    focus_window,
                    focus_thread_id,
                    focus_process_id,
                )
        except Exception:
            LOGGER.debug("Could not restore captured Windows target", exc_info=True)
        return None

    @staticmethod
    def capture_target() -> ForegroundTargetHandle | None:
        try:
            if sys.platform == "darwin":
                from AppKit import NSWorkspace

                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app:
                    return ForegroundTargetHandle(
                        "mac", int(app.processIdentifier()), str(app.localizedName() or "")
                    )
            elif sys.platform == "win32":
                hwnd = _windows_foreground_window()
                if hwnd:
                    name = PlatformBridge.current_app_name()
                    try:
                        user32 = ctypes.windll.user32
                        target_thread_id, target_process_id = (
                            _windows_window_identity(
                                user32,
                                hwnd,
                            )
                        )
                        focus_window = _windows_focus_window(
                            user32,
                            target_thread_id,
                        )
                        focus_thread_id = focus_process_id = None
                        if focus_window:
                            focus_thread_id, focus_process_id = (
                                _windows_window_identity(user32, focus_window)
                            )
                        return ForegroundTargetHandle(
                            "windows",
                            hwnd,
                            name,
                            target_thread_id,
                            target_process_id,
                            focus_window or None,
                            focus_thread_id,
                            focus_process_id,
                        )
                    except Exception:
                        LOGGER.debug(
                            "Could not capture Windows input focus; using top-level target",
                            exc_info=True,
                        )
                        return ForegroundTargetHandle("windows", hwnd, name)
        except Exception:
            LOGGER.exception("Could not capture foreground target")
        return None

    @staticmethod
    def is_windows() -> bool:
        return sys.platform == "win32"

    @staticmethod
    def foreground_window_id() -> int | None:
        if sys.platform != "win32":
            return None
        try:
            return _windows_foreground_window() or None
        except Exception:
            LOGGER.debug("Could not read Windows foreground window", exc_info=True)
            return None

    @staticmethod
    def request_window_activation(identifier: int) -> bool:
        if sys.platform != "win32" or not identifier:
            return False
        try:
            user32 = ctypes.windll.user32
            handle = _windows_handle(identifier)
            if not user32.IsWindow(handle):
                return False
            _thread_id, process_id = _windows_window_identity(user32, identifier)
            if not process_id or process_id != os.getpid():
                LOGGER.warning(
                    "Refusing to activate a panel window not owned by ClipSoon; hwnd=%d",
                    identifier,
                )
                return False
            user32.ShowWindow(handle, 5)  # SW_SHOW
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            if _windows_foreground_window() == identifier:
                return True
            return bool(
                _run_windows_focus_helper(
                    mode="panel",
                    target_window=identifier,
                    target_process_id=process_id,
                )
                and _windows_foreground_window() == identifier
            )
        except Exception:
            LOGGER.debug("Could not activate ClipSoon panel", exc_info=True)
            return False

    @staticmethod
    def primary_button_down() -> bool:
        if sys.platform != "win32":
            return False
        try:
            # The high bit reports the current state; the low bit also catches a
            # short click that began and ended between two panel-guard polls.
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8001)
        except Exception:
            LOGGER.debug("Could not read Windows primary button state", exc_info=True)
            return False

    @staticmethod
    def configure_macos_accessory() -> None:
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

            NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            LOGGER.debug("Could not enable accessory mode", exc_info=True)

    @staticmethod
    def accessibility_permission_status() -> bool | None:
        if sys.platform != "darwin":
            return None
        try:
            from ApplicationServices import AXIsProcessTrusted

            return bool(AXIsProcessTrusted())
        except Exception:
            LOGGER.debug("Could not read macOS accessibility permission", exc_info=True)
            return False

    @staticmethod
    def request_accessibility_permission() -> bool:
        if sys.platform != "darwin":
            return False
        prompted = False
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            prompted = True
        except Exception:
            LOGGER.debug("Could not show macOS accessibility permission prompt", exc_info=True)
        try:
            os.spawnlp(
                os.P_NOWAIT,
                "open",
                "open",
                PlatformBridge._MACOS_ACCESSIBILITY_URL,
            )
            return True
        except OSError:
            return prompted

    @staticmethod
    def reveal(path: Path) -> bool:
        try:
            if sys.platform == "darwin":
                os.spawnlp(os.P_NOWAIT, "open", "open", str(path))
            elif sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                return False
            return True
        except OSError:
            return False


class PynputPasteAdapter:
    def paste(self) -> bool:
        try:
            if sys.platform == "win32":
                return send_windows_paste_input(
                    ctypes.windll.user32,
                    getattr(ctypes.windll, "kernel32", None),
                )
            from pynput import keyboard

            controller = keyboard.Controller()
            modifier = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl
            with controller.pressed(modifier):
                controller.press("v")
                controller.release("v")
            return True
        except Exception:
            LOGGER.exception("Synthetic paste failed")
            return False


class SelectionSender(QObject):
    finished = Signal(str, bool)

    def __init__(
        self,
        clipboard: ClipboardController,
        repository: HistoryRepository,
        paste_adapter: PynputPasteAdapter,
        settings: Callable[[], AppSettings],
        hide_panel: Callable[[], None],
        *,
        file_validation_timeout_ms: int = _FILE_VALIDATION_TIMEOUT_MS,
    ) -> None:
        super().__init__()
        self.clipboard, self.repository = clipboard, repository
        self.paste_adapter, self._settings = paste_adapter, settings
        self._hide_panel = hide_panel
        self._busy = False
        self._validation_tasks: set[_FileItemValidationTask] = set()
        self._validation_generation = 0
        self._send_generation = 0
        self._file_validation_timeout_ms = max(50, int(file_validation_timeout_ms))

    def send(self, item: ClipItem, target: ForegroundTargetHandle | None) -> None:
        if self._busy:
            return
        self._busy = True
        self._send_generation += 1
        send_generation = self._send_generation
        if item.kind is ClipKind.FILES:
            if any(task.item_id == item.id for task in self._validation_tasks):
                self._finish_send(send_generation, "原文件仍在验证，请稍后重试", False)
                return
            if len(self._validation_tasks) >= _MAX_CONCURRENT_FILE_VALIDATIONS:
                self._finish_send(send_generation, "后台文件验证繁忙，请稍后重试", False)
                return
            self._validation_generation += 1
            validation_generation = self._validation_generation
            self._start_file_item_validation(
                item.id,
                target,
                validation_generation,
                send_generation,
            )
            QTimer.singleShot(
                self._file_validation_timeout_ms,
                lambda: self._file_item_validation_timed_out(
                    validation_generation,
                    send_generation,
                ),
            )
            return
        self._request_item_write(item, target, send_generation)

    def _start_file_item_validation(
        self,
        item_id: str,
        target: ForegroundTargetHandle | None,
        validation_generation: int,
        send_generation: int,
    ) -> None:
        task = _FileItemValidationTask(self.repository, item_id)
        self._validation_tasks.add(task)
        task.signals.finished.connect(
            lambda validated, error, task=task, target=target: self._file_item_validated(
                task,
                validated,
                error,
                target,
                validation_generation,
                send_generation,
            )
        )
        threading.Thread(
            target=task.run,
            name=f"ClipSoon-file-validation-{validation_generation}",
            daemon=True,
        ).start()

    def _file_item_validation_timed_out(
        self,
        validation_generation: int,
        send_generation: int,
    ) -> None:
        if (
            validation_generation != self._validation_generation
            or send_generation != self._send_generation
            or not self._busy
        ):
            return
        self._validation_generation += 1
        self._finish_send(send_generation, "原文件验证超时，请重试", False)

    def _file_item_validated(
        self,
        task: _FileItemValidationTask,
        validated: object,
        error: str,
        target: ForegroundTargetHandle | None,
        validation_generation: int,
        send_generation: int,
    ) -> None:
        self._validation_tasks.discard(task)
        if (
            validation_generation != self._validation_generation
            or send_generation != self._send_generation
            or not self._busy
        ):
            return
        if error:
            self._validation_generation += 1
            self._finish_send(send_generation, f"无法验证原文件：{error}", False)
            return
        if not isinstance(validated, ValidatedFileItem):
            self._validation_generation += 1
            self._finish_send(send_generation, "原文件已不存在，已从历史移除", False)
            return
        try:
            claim = self.repository.claim_validated_file_item(validated)
        except Exception as exc:
            LOGGER.exception("Validated file item claim failed")
            self._validation_generation += 1
            self._finish_send(send_generation, f"无法确认原文件：{exc}", False)
            return
        if claim.status is FileItemClaimStatus.MISSING:
            self._validation_generation += 1
            self._finish_send(send_generation, "原文件已不存在，已从历史移除", False)
            return
        if claim.status is FileItemClaimStatus.REFRESHED:
            item_id = claim.item.id if claim.item is not None else validated.item.id
            self._validation_generation += 1
            refreshed_generation = self._validation_generation
            self._start_file_item_validation(
                item_id,
                target,
                refreshed_generation,
                send_generation,
            )
            QTimer.singleShot(
                self._file_validation_timeout_ms,
                lambda: self._file_item_validation_timed_out(
                    refreshed_generation,
                    send_generation,
                ),
            )
            return
        self._validation_generation += 1
        if claim.status is not FileItemClaimStatus.ACCEPTED or claim.item is None:
            self._finish_send(send_generation, "无法确认原文件", False)
            return
        self._request_item_write(claim.item, target, send_generation)

    def _request_item_write(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle | None,
        send_generation: int,
    ) -> None:
        self.clipboard.request_write(
            item,
            lambda receipt, error: self._item_write_finished(
                item,
                target,
                send_generation,
                receipt,
                error,
            ),
        )

    def _item_write_finished(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle | None,
        send_generation: int,
        receipt: ClipboardWriteReceipt | None,
        error: str,
    ) -> None:
        if send_generation != self._send_generation or not self._busy:
            return
        if receipt is None or receipt.kind is not item.kind:
            message = f"无法写入系统剪贴板：{error}" if error else "无法写入系统剪贴板"
            self._finish_send(send_generation, message, False)
            return
        self._dispatch_written(item, target, receipt, send_generation)

    def _dispatch_written(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle | None,
        receipt: ClipboardWriteReceipt,
        send_generation: int,
    ) -> None:
        self.repository.mark_used(item.id)
        self._hide_panel()
        settings = self._settings()
        if not settings.paste_after_selection or target is None:
            self._finish_send(send_generation, "已复制到剪贴板", True)
            return
        QTimer.singleShot(
            35,
            lambda: self._activate_then_verify(
                item,
                target,
                receipt,
                settings.paste_delay_ms,
                send_generation,
            ),
        )

    def _activate_then_verify(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle,
        receipt: ClipboardWriteReceipt,
        delay_ms: int,
        send_generation: int,
    ) -> None:
        if send_generation != self._send_generation or not self._busy:
            return
        if not target.activate():
            self._finish_send(send_generation, "已复制，但无法恢复目标窗口", False)
            return
        QTimer.singleShot(
            delay_ms,
            lambda: self._verify_before_paste(
                item,
                target,
                receipt,
                send_generation,
                rewrite_count=0,
            ),
        )

    def _verify_before_paste(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle,
        receipt: ClipboardWriteReceipt,
        send_generation: int,
        *,
        rewrite_count: int,
        focus_retry_count: int = 0,
    ) -> None:
        if send_generation != self._send_generation or not self._busy:
            return
        # Some macOS apps report activation asynchronously. A successful native
        # activate call is accepted, but a positively different foreground is not.
        if not target.is_active():
            if (
                getattr(target, "kind", "") == "windows"
                and PlatformBridge.foreground_window_id() == target.identifier
                and focus_retry_count < len(_WINDOWS_FOCUS_RETRY_DELAYS_MS)
            ):
                retry_delay = _WINDOWS_FOCUS_RETRY_DELAYS_MS[focus_retry_count]
                QTimer.singleShot(
                    retry_delay,
                    lambda: self._verify_before_paste(
                        item,
                        target,
                        receipt,
                        send_generation,
                        rewrite_count=rewrite_count,
                        focus_retry_count=focus_retry_count + 1,
                    ),
                )
                return
            self._finish_send(send_generation, "已复制，但目标窗口未激活", False)
            return
        self.clipboard.request_verify(
            receipt,
            lambda ok, error: self._clipboard_verified(
                item,
                target,
                send_generation,
                rewrite_count,
                ok,
                error,
            ),
        )

    def _clipboard_verified(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle,
        send_generation: int,
        rewrite_count: int,
        ok: bool,
        error: str,
    ) -> None:
        if send_generation != self._send_generation or not self._busy:
            return
        if not ok:
            if rewrite_count >= 1:
                message = (
                    f"剪贴板验证失败，已取消自动粘贴：{error}"
                    if error
                    else "剪贴板验证失败，已取消自动粘贴"
                )
                self._finish_send(send_generation, message, False)
                return
            self.clipboard.request_write(
                item,
                lambda receipt, write_error: self._rewrite_finished(
                    item,
                    target,
                    send_generation,
                    receipt,
                    write_error,
                ),
            )
            return
        if not target.is_active():
            self._finish_send(send_generation, "已复制，但目标窗口未激活", False)
            return
        pasted = self.paste_adapter.paste()
        success_message = (
            "已触发粘贴"
            if pasted and getattr(target, "kind", "") == "windows"
            else "已发送"
        )
        self._finish_send(
            send_generation,
            success_message if pasted else "已复制，但自动粘贴失败",
            pasted,
        )

    def _rewrite_finished(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle,
        send_generation: int,
        receipt: ClipboardWriteReceipt | None,
        error: str,
    ) -> None:
        if send_generation != self._send_generation or not self._busy:
            return
        if receipt is None or receipt.kind is not item.kind:
            message = (
                f"剪贴板重写失败，已取消自动粘贴：{error}"
                if error
                else "剪贴板重写失败，已取消自动粘贴"
            )
            self._finish_send(send_generation, message, False)
            return
        self._verify_before_paste(
            item,
            target,
            receipt,
            send_generation,
            rewrite_count=1,
        )

    def _finish_send(self, send_generation: int, message: str, success: bool) -> None:
        if send_generation != self._send_generation:
            return
        self._busy = False
        self.finished.emit(message, success)


def _canonical_key(key: str) -> str:
    key = key.casefold().replace("key.", "")
    aliases = {
        "control": "ctrl",
        "ctrl_l": "ctrl",
        "ctrl_r": "ctrl",
        "shift_l": "shift",
        "shift_r": "shift",
        "alt_l": "alt",
        "alt_r": "alt",
        "alt_gr": "altgr",
        "cmd": "meta",
        "cmd_l": "meta",
        "cmd_r": "meta",
    }
    return aliases.get(key, key)


def _pynput_key_name(key: object) -> str:
    character = getattr(key, "char", None)
    return _canonical_key(str(character if character is not None else key))
