"""Fast system edge: hotkeys, clipboard MIME, foreground restore, and paste."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import plistlib
import subprocess
import sys
import threading
import time
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
    ClipItem,
    ClipKind,
    FileItemClaimStatus,
    HistoryRepository,
    ValidatedFileItem,
)
from clipsoon.windows_workers import WindowsWorkerSupervisor

LOGGER = logging.getLogger(__name__)
_MODIFIERS = {"ctrl", "shift", "alt", "meta"}
_CLIPBOARD_SETTLE_MS = 70
_CLIPBOARD_RETRY_DELAYS_MS = (90, 180)
_WINDOWS_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
_WINDOWS_IMAGE_MAX_BYTES = 256 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WINDOWS_PNG_MIME = 'application/x-qt-windows-mime;value="PNG"'
_WINDOWS_INTERNAL_WRITE_MIME = 'application/x-qt-windows-mime;value="ClipSoon.InternalWrite"'
_FILE_VALIDATION_TIMEOUT_MS = 3_000
_MAX_CONCURRENT_FILE_VALIDATIONS = 2


def _windows_ipc_session(name: str) -> str | None:
    value = name.removeprefix(".")
    if not value.startswith(("manifest-", "clip-")):
        return None
    parts = value.split("-", 2)
    if len(parts) < 3:
        return None
    session_id = parts[1].casefold()
    if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
        return None
    return session_id


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
        raw_target = message.get("target_hwnd")
        target_window = (
            raw_target
            if isinstance(raw_target, int)
            and not isinstance(raw_target, bool)
            and raw_target > 0
            else None
        )
        self._activated(
            HotkeyActivationContext(
                target_window=target_window,
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
        if self._windows_worker is not None:
            self._windows_worker.stop()
            self._windows_worker = None
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
        worker.failed.connect(self.failed.emit)
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
        if kind == "clipboard":
            self._queue_windows_manifest(raw_message)
        elif kind == "error" and not raw_message.get("retrying") and not raw_message.get("fatal"):
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
            if path.name.startswith(("manifest-", "clip-", ".manifest-", ".clip-")):
                if _windows_ipc_session(path.name) in preserved:
                    continue
                with suppress(OSError):
                    path.unlink()

    def write_item(self, item: ClipItem) -> bool:
        mime = QMimeData()
        if item.kind is ClipKind.TEXT:
            mime.setText(item.text)
        elif item.kind is ClipKind.FILES:
            mime.setUrls([QUrl.fromLocalFile(path) for path in item.files])
        else:
            image = QImage(item.image_path)
            if image.isNull():
                return False
            try:
                png = Path(item.image_path).read_bytes()
            except OSError:
                return False
            if not png.startswith(_PNG_SIGNATURE):
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                if not image.save(buffer, "PNG"):
                    return False
                png = bytes(buffer.data())
            mime.setImageData(image)
            mime.setData("image/png", png)
            if sys.platform == "win32":
                mime.setData(_WINDOWS_PNG_MIME, png)
        if sys.platform == "win32":
            mime.setData(_WINDOWS_INTERNAL_WRITE_MIME, b"1")
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
                # SW_RESTORE also unmaximizes a maximized window. Only use it
                # for a genuinely minimized target so sending never changes the
                # user's chosen normal/maximized window state.
                if user32.IsIconic(handle):
                    user32.ShowWindow(handle, 9)  # SW_RESTORE
                user32.BringWindowToTop(handle)
                return bool(user32.SetForegroundWindow(handle))
        except Exception:
            LOGGER.exception("Could not restore target window")
        return False

    def is_active(self) -> bool:
        try:
            if self.kind == "mac":
                from AppKit import NSWorkspace

                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                return bool(app and int(app.processIdentifier()) == self.identifier)
            if self.kind == "windows":
                return _windows_foreground_window() == self.identifier
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
    def target_from_window_id(identifier: int) -> ForegroundTargetHandle | None:
        if sys.platform != "win32" or identifier <= 0:
            return None
        try:
            handle = _windows_handle(identifier)
            if ctypes.windll.user32.IsWindow(handle):
                return ForegroundTargetHandle(
                    "windows",
                    identifier,
                    PlatformBridge.window_name(identifier),
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
                    return ForegroundTargetHandle("windows", hwnd, PlatformBridge.current_app_name())
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
            user32.ShowWindow(handle, 5)  # SW_SHOW
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            if _windows_foreground_window() == identifier:
                return True

            # The hotkey helper and Qt GUI are separate processes, so foreground
            # privilege transfer can still be denied. Temporarily join the
            # current foreground input queue and retry from the panel's GUI
            # thread.
            foreground = _windows_foreground_window()
            if not foreground:
                return False
            kernel32 = ctypes.windll.kernel32
            get_window_thread = user32.GetWindowThreadProcessId
            get_window_thread.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
            get_window_thread.restype = ctypes.c_uint32
            get_current_thread = kernel32.GetCurrentThreadId
            get_current_thread.argtypes = ()
            get_current_thread.restype = ctypes.c_uint32
            attach_thread_input = user32.AttachThreadInput
            attach_thread_input.argtypes = (
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_int32,
            )
            attach_thread_input.restype = ctypes.c_int32
            foreground_thread = int(
                get_window_thread(_windows_handle(foreground), None)
            )
            current_thread = int(get_current_thread())
            if not foreground_thread or foreground_thread == current_thread:
                return False
            attached = bool(attach_thread_input(current_thread, foreground_thread, True))
            if not attached:
                return False
            try:
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
                return _windows_foreground_window() == identifier
            finally:
                attach_thread_input(current_thread, foreground_thread, False)
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
        self._file_validation_timeout_ms = max(50, int(file_validation_timeout_ms))

    def send(self, item: ClipItem, target: ForegroundTargetHandle | None) -> None:
        if self._busy:
            return
        self._busy = True
        if item.kind is ClipKind.FILES:
            if any(task.item_id == item.id for task in self._validation_tasks):
                self._busy = False
                self.finished.emit("原文件仍在验证，请稍后重试", False)
                return
            if len(self._validation_tasks) >= _MAX_CONCURRENT_FILE_VALIDATIONS:
                self._busy = False
                self.finished.emit("后台文件验证繁忙，请稍后重试", False)
                return
            self._validation_generation += 1
            generation = self._validation_generation
            self._start_file_item_validation(item.id, target, generation)
            QTimer.singleShot(
                self._file_validation_timeout_ms,
                lambda: self._file_item_validation_timed_out(generation),
            )
            return
        self._write_and_dispatch(item, target)

    def _start_file_item_validation(
        self,
        item_id: str,
        target: ForegroundTargetHandle | None,
        generation: int,
    ) -> None:
        task = _FileItemValidationTask(self.repository, item_id)
        self._validation_tasks.add(task)
        task.signals.finished.connect(
            lambda validated, error, task=task, target=target: self._file_item_validated(
                task, validated, error, target, generation
            )
        )
        threading.Thread(
            target=task.run,
            name=f"ClipSoon-file-validation-{generation}",
            daemon=True,
        ).start()

    def _file_item_validation_timed_out(self, generation: int) -> None:
        if generation != self._validation_generation or not self._busy:
            return
        self._validation_generation += 1
        self._busy = False
        self.finished.emit("原文件验证超时，请重试", False)

    def _file_item_validated(
        self,
        task: _FileItemValidationTask,
        validated: object,
        error: str,
        target: ForegroundTargetHandle | None,
        generation: int,
    ) -> None:
        self._validation_tasks.discard(task)
        if generation != self._validation_generation or not self._busy:
            return
        if error:
            self._validation_generation += 1
            self._busy = False
            self.finished.emit(f"无法验证原文件：{error}", False)
            return
        if not isinstance(validated, ValidatedFileItem):
            self._validation_generation += 1
            self._busy = False
            self.finished.emit("原文件已不存在，已从历史移除", False)
            return
        try:
            claim = self.repository.consume_validated_file_item(
                validated,
                self.clipboard.write_item,
            )
        except Exception as exc:
            LOGGER.exception("Validated file clipboard write failed")
            self._validation_generation += 1
            self._busy = False
            self.finished.emit(f"无法写入系统剪贴板：{exc}", False)
            return
        if claim.status is FileItemClaimStatus.MISSING:
            self._validation_generation += 1
            self._busy = False
            self.finished.emit("原文件已不存在，已从历史移除", False)
            return
        if claim.status is FileItemClaimStatus.REFRESHED:
            item_id = claim.item.id if claim.item is not None else validated.item.id
            self._start_file_item_validation(item_id, target, generation)
            return
        self._validation_generation += 1
        if claim.status is FileItemClaimStatus.REJECTED or claim.item is None:
            self._busy = False
            self.finished.emit("无法写入系统剪贴板", False)
            return
        self._dispatch_written(claim.item, target)

    def _write_and_dispatch(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle | None,
    ) -> None:
        if not self.clipboard.write_item(item):
            self._busy = False
            self.finished.emit("无法写入系统剪贴板", False)
            return
        self._dispatch_written(item, target)

    def _dispatch_written(
        self,
        item: ClipItem,
        target: ForegroundTargetHandle | None,
    ) -> None:
        self.repository.mark_used(item.id)
        self._hide_panel()
        settings = self._settings()
        if not settings.paste_after_selection or target is None:
            self._busy = False
            self.finished.emit("已复制到剪贴板", True)
            return
        QTimer.singleShot(35, lambda: self._activate_then_paste(target, settings.paste_delay_ms))

    def _activate_then_paste(self, target: ForegroundTargetHandle, delay_ms: int) -> None:
        if not target.activate():
            self._busy = False
            self.finished.emit("已复制，但无法恢复目标窗口", False)
            return
        QTimer.singleShot(delay_ms, lambda: self._paste(target))

    def _paste(self, target: ForegroundTargetHandle) -> None:
        # Some macOS apps report activation asynchronously. A successful native
        # activate call is accepted, but a positively different foreground is not.
        if not target.is_active():
            self._busy = False
            self.finished.emit("已复制，但目标窗口未激活", False)
            return
        pasted = self.paste_adapter.paste()
        self._busy = False
        self.finished.emit("已发送" if pasted else "已复制，但自动粘贴失败", pasted)


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
