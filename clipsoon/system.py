"""Fast system edge: hotkeys, clipboard MIME, foreground restore, and paste."""

from __future__ import annotations

import ctypes
import logging
import os
import plistlib
import subprocess
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
_CLIPBOARD_SETTLE_MS = 70
_CLIPBOARD_RETRY_DELAYS_MS = (90, 180)


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

    @property
    def is_running(self) -> bool:
        if self._listener is None:
            return False
        running = getattr(self._listener, "running", None)
        return True if running is None else bool(running)

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
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._capture_pending)
        self._pending_sequence: int | None = None
        self._pending_source = ""
        self._capture_attempt = 0
        self._last_capture_error: Exception | None = None

    def start(self) -> None:
        self.clipboard.dataChanged.connect(self._clipboard_changed)
        # macOS does not emit reliable background QClipboard change events.
        # changeCount polling is a single integer read and stays below 0.02% CPU.
        if sys.platform == "darwin":
            self._poll_timer.start()

    def stop(self) -> None:
        self._poll_timer.stop()
        self._capture_timer.stop()
        self._pending_sequence = None
        self._pending_source = ""
        with suppress(RuntimeError, TypeError):
            self.clipboard.dataChanged.disconnect(self._clipboard_changed)

    def sync_cursor(self) -> None:
        """Advance without reading; used by clear/pause to prevent resurrection."""
        self._capture_timer.stop()
        self._pending_sequence = None
        self._pending_source = ""
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
                handle = _windows_handle(self.identifier)
                if not user32.IsWindow(handle):
                    return False
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
                user32 = ctypes.windll.user32
                hwnd = _windows_foreground_window()
                handle = _windows_handle(hwnd)
                length = user32.GetWindowTextLengthW(handle)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(handle, buffer, len(buffer))
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
            return _windows_foreground_window() == identifier
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
