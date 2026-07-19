"""Fast system edge: hotkeys, clipboard MIME, foreground restore, and paste."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QClipboard, QImage

from clipsoon.core import AppSettings, ClipItem, ClipKind, HistoryRepository

LOGGER = logging.getLogger(__name__)
_MODIFIERS = {"ctrl", "shift", "alt", "meta"}


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
        if self._combo_latched and not self._combo <= self._pressed:
            self._combo_latched = False


class GlobalHotkeyService:
    def __init__(
        self,
        activated: Callable[[], None],
        failed: Callable[[str], None],
    ) -> None:
        self._activated = activated
        self._failed = failed
        self._listener = None
        self._machine: HotkeyStateMachine | None = None

    def start(self, settings: AppSettings) -> None:
        self.stop()
        self._machine = HotkeyStateMachine(
            settings.hotkey, settings.double_tap_interval_ms, self._activated
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
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                LOGGER.debug("Hotkey listener stop failed", exc_info=True)
        self._listener = None

    def _on_press(self, key: object) -> None:
        if self._machine is not None:
            self._machine.press(_pynput_key_name(key))

    def _on_release(self, key: object) -> None:
        if self._machine is not None:
            self._machine.release(_pynput_key_name(key))


class _WorkerSignals(QObject):
    stored = Signal(object)
    failed = Signal(str)


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
        self._active_tasks: set[_ImageStoreTask] = set()
        self._self_write = False
        self._suppressed_sequence: int | None = None
        self._last_sequence = self._sequence_number()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(220)
        self._poll_timer.timeout.connect(self._poll_native_sequence)

    def start(self) -> None:
        self.clipboard.dataChanged.connect(self._clipboard_changed)
        # macOS does not emit reliable background QClipboard change events.
        # changeCount polling is a single integer read and stays below 0.02% CPU.
        if sys.platform == "darwin":
            self._poll_timer.start()

    def stop(self) -> None:
        self._poll_timer.stop()
        with suppress(RuntimeError, TypeError):
            self.clipboard.dataChanged.disconnect(self._clipboard_changed)

    def sync_cursor(self) -> None:
        """Advance without reading; used by clear/pause to prevent resurrection."""
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
        self._capture_current()

    def _clipboard_changed(self) -> None:
        sequence = self._sequence_number()
        if sequence is not None:
            if sequence == self._last_sequence and not self._self_write:
                return
            self._last_sequence = sequence
        if self._self_write or (sequence is not None and sequence == self._suppressed_sequence):
            self._suppressed_sequence = None
            return
        self._capture_current()

    def _capture_current(self) -> None:
        if not self._settings().capture_enabled:
            return
        mime = self.clipboard.mimeData(QClipboard.Mode.Clipboard)
        if mime is None or self._is_secret(mime):
            return
        source = self._source_app()
        try:
            local_files = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if local_files:
                item = self.repository.add_files(local_files, source)
                self._finish_capture(item)
                return
            if mime.hasImage():
                image = self.clipboard.image(QClipboard.Mode.Clipboard)
                if not image.isNull():
                    self._store_image_async(image.copy(), source)
                return
            if mime.hasText():
                text = mime.text()
                if text:
                    self._finish_capture(self.repository.add_text(text, source))
        except Exception as exc:
            LOGGER.exception("Clipboard capture failed")
            self.failed.emit(f"剪贴板记录失败：{exc}")

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
            mime.setImageData(image)
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
                if not user32.IsWindow(self.identifier):
                    return False
                user32.ShowWindow(self.identifier, 9)  # SW_RESTORE
                return bool(user32.SetForegroundWindow(self.identifier))
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
                return int(ctypes.windll.user32.GetForegroundWindow()) == self.identifier
        except Exception:
            return False
        return False


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
                user32 = ctypes.windll.user32
                hwnd = user32.GetForegroundWindow()
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, len(buffer))
                return buffer.value
        except Exception:
            LOGGER.debug("Current app name unavailable", exc_info=True)
        return ""

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
                hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                if hwnd:
                    return ForegroundTargetHandle("windows", hwnd, PlatformBridge.current_app_name())
        except Exception:
            LOGGER.exception("Could not capture foreground target")
        return None

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
    ) -> None:
        super().__init__()
        self.clipboard, self.repository = clipboard, repository
        self.paste_adapter, self._settings = paste_adapter, settings
        self._hide_panel = hide_panel
        self._busy = False

    def send(self, item: ClipItem, target: ForegroundTargetHandle | None) -> None:
        if self._busy:
            return
        self._busy = True
        if not self.clipboard.write_item(item):
            self._busy = False
            self.finished.emit("无法写入系统剪贴板", False)
            return
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
