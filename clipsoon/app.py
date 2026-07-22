"""ClipSoon composition root."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_data_path
from PySide6.QtCore import QLockFile, QObject, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from clipsoon import __version__
from clipsoon.core import HistoryRepository, JsonSettingsStore, ObservableSettings
from clipsoon.system import (
    ClipboardController,
    ForegroundTargetHandle,
    GlobalHotkeyService,
    LaunchAtLoginManager,
    PlatformBridge,
    PynputPasteAdapter,
    SelectionSender,
)
from clipsoon.ui import ClipPanel, SettingsDialog, create_tray_icon

LOGGER = logging.getLogger(__name__)
_CRASH_LOG_STREAM = None
_PANEL_WATCH_INTERVAL_MS = 35
_HOTKEY_HEALTH_INTERVAL_MS = 2_000
_HOTKEY_RESTART_BACKOFF_SECONDS = 15.0


class _Signals(QObject):
    hotkey = Signal()
    hotkey_failed = Signal(str)


@dataclass(slots=True)
class _WindowsPanelGuard:
    initial_foreground: int | None = None
    panel_window: int | None = None
    saw_panel_foreground: bool = False
    primary_button_was_down: bool = False

    def arm(
        self,
        *,
        initial_foreground: int | None,
        panel_window: int,
        primary_button_down: bool,
    ) -> None:
        self.initial_foreground = initial_foreground
        self.panel_window = panel_window
        self.saw_panel_foreground = initial_foreground == panel_window
        self.primary_button_was_down = primary_button_down

    def sync_primary_button(self, primary_button_down: bool) -> None:
        self.primary_button_was_down = primary_button_down

    def should_hide(
        self,
        *,
        foreground: int | None,
        primary_button_down: bool,
        cursor_inside: bool,
    ) -> bool:
        newly_pressed = primary_button_down and not self.primary_button_was_down
        self.primary_button_was_down = primary_button_down
        if foreground == self.panel_window:
            self.saw_panel_foreground = True
            return False
        if newly_pressed and not cursor_inside:
            return True
        if self.saw_panel_foreground:
            return foreground is not None
        return (
            self.initial_foreground is not None
            and foreground is not None
            and foreground != self.initial_foreground
        )


class ClipSoonApplication(QObject):
    def __init__(self, qt_app: QApplication, data_dir: Path) -> None:
        super().__init__()
        self.qt_app, self.data_dir = qt_app, data_dir
        self.settings = ObservableSettings(JsonSettingsStore(data_dir / "settings.json"))
        self.repository = HistoryRepository(data_dir)
        self.launch_at_login = LaunchAtLoginManager()
        self.signals = _Signals()
        self.target: ForegroundTargetHandle | None = None
        self._panel_guard = _WindowsPanelGuard()
        self._panel_watch_timer = QTimer(self)
        self._panel_watch_timer.setInterval(_PANEL_WATCH_INTERVAL_MS)
        self._panel_watch_timer.timeout.connect(self._watch_windows_panel)
        self._hotkey_health_timer = QTimer(self)
        self._hotkey_health_timer.setInterval(_HOTKEY_HEALTH_INTERVAL_MS)
        self._hotkey_health_timer.timeout.connect(self._ensure_hotkey_listener)
        self._next_hotkey_restart_at = 0.0

        self.panel = ClipPanel(lambda: self.settings.value)
        self.panel.set_items(self.repository.list_items())
        self.clipboard = ClipboardController(
            qt_app.clipboard(),
            self.repository,
            lambda: self.settings.value,
            PlatformBridge.current_app_name,
        )
        self.sender = SelectionSender(
            self.clipboard,
            self.repository,
            PynputPasteAdapter(),
            lambda: self.settings.value,
            self.panel.hide_panel,
        )
        self.hotkey = GlobalHotkeyService(self.signals.hotkey.emit, self.signals.hotkey_failed.emit)
        self.tray, self.tray_menu, self.tray_actions = create_tray_icon(self.panel)
        self._connect()

    def start(self) -> None:
        self.clipboard.start()
        self.tray_actions["pause"].setChecked(not self.settings.value.capture_enabled)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.panel.hide()
        # Permission failures can be emitted synchronously, so the tray must
        # already be visible for the first-launch warning to reach the user.
        self.hotkey.start(self.settings.value)
        if not PlatformBridge.is_windows():
            self._hotkey_health_timer.start()
        if self.settings.value.launch_at_login:
            success, message = self.launch_at_login.set_enabled(True)
            if not success:
                self._notify_error(message)
        LOGGER.info("ClipSoon %s ready with %d items", __version__, len(self.repository.list_items()))

    def _connect(self) -> None:
        self.signals.hotkey.connect(self.toggle_panel)
        self.signals.hotkey_failed.connect(self._hotkey_failed)
        self.clipboard.captured.connect(self._captured)
        self.clipboard.failed.connect(self._notify_error)
        self.panel.send_requested.connect(lambda item: self.sender.send(item, self.target))
        self.panel.settings_requested.connect(self.show_settings)
        self.panel.delete_requested.connect(self._delete_many)
        self.panel.clear_requested.connect(self.clear_all_history)
        self.panel.accessibility_requested.connect(self.open_accessibility_settings)
        self.panel.position_changed.connect(self._save_panel_position)
        self.sender.finished.connect(self._send_finished)
        self.tray_actions["show"].triggered.connect(self.show_panel)
        self.tray_actions["pause"].toggled.connect(self._toggle_capture)
        self.tray_actions["settings"].triggered.connect(self.show_settings)
        self.tray_actions["quit"].triggered.connect(self.qt_app.quit)
        self.tray.activated.connect(self._tray_activated)
        self.qt_app.aboutToQuit.connect(self.shutdown)

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide_panel()
        else:
            self.show_panel()

    def show_panel(self) -> None:
        if (
            PlatformBridge.accessibility_permission_status() is True
            and self.panel.has_accessibility_warning()
        ):
            self.panel.clear_status()
        self.target = PlatformBridge.capture_target()
        initial_foreground = (
            self.target.identifier
            if self.target is not None and self.target.kind == "windows"
            else PlatformBridge.foreground_window_id()
        )
        elapsed = self.panel.show_panel()
        if PlatformBridge.is_windows():
            panel_window = int(self.panel.winId())
            self._panel_guard.arm(
                initial_foreground=initial_foreground,
                panel_window=panel_window,
                primary_button_down=PlatformBridge.primary_button_down(),
            )
            self._panel_watch_timer.start()
            QTimer.singleShot(0, lambda: self._activate_windows_panel(0))
        LOGGER.info("Panel visible in %.1f ms; target=%s", elapsed, self.target.name if self.target else "none")
        if elapsed > 100:
            LOGGER.warning("Hotkey-to-visible budget exceeded: %.1f ms", elapsed)

    def _activate_windows_panel(self, attempt: int) -> None:
        if not PlatformBridge.is_windows() or not self.panel.isVisible():
            return
        if PlatformBridge.request_window_activation(int(self.panel.winId())):
            self._panel_guard.saw_panel_foreground = True
            self.panel.activateWindow()
            self.panel.search.setFocus()
            return
        if attempt == 0:
            QTimer.singleShot(45, lambda: self._activate_windows_panel(1))

    def _watch_windows_panel(self) -> None:
        if not self.panel.isVisible():
            self._panel_watch_timer.stop()
            return
        primary_down = PlatformBridge.primary_button_down()
        if (
            not self.settings.value.hide_on_deactivate
            or QApplication.activeModalWidget() is not None
            or QApplication.activePopupWidget() is not None
        ):
            self._panel_guard.sync_primary_button(primary_down)
            return
        should_hide = self._panel_guard.should_hide(
            foreground=PlatformBridge.foreground_window_id(),
            primary_button_down=primary_down,
            cursor_inside=self.panel.frameGeometry().contains(QCursor.pos()),
        )
        if should_hide:
            LOGGER.info("Hiding Windows panel after native foreground/pointer change")
            self.panel.hide_panel()
            self._panel_watch_timer.stop()

    def _ensure_hotkey_listener(self) -> None:
        if PlatformBridge.is_windows():
            return
        if self.hotkey.is_running:
            self._next_hotkey_restart_at = 0.0
            return
        now = time.monotonic()
        if now < self._next_hotkey_restart_at:
            return
        LOGGER.warning("Global hotkey listener stopped; restarting it")
        self._next_hotkey_restart_at = now + _HOTKEY_RESTART_BACKOFF_SECONDS
        self.hotkey.start(self.settings.value)

    def show_settings(self) -> None:
        self.panel.keep_open(True)
        dialog = SettingsDialog(
            self.settings.value,
            self.panel if self.panel.isVisible() else None,
            accessibility_granted=PlatformBridge.accessibility_permission_status(),
        )
        dialog.clear_requested.connect(self.clear_history)
        dialog.reveal_requested.connect(self.open_data_directory)
        dialog.accessibility_requested.connect(self.open_accessibility_settings)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            old = self.settings.value
            values = dialog.values()
            requested_launch_at_login = bool(values["launch_at_login"])
            launch_message = ""
            if requested_launch_at_login or requested_launch_at_login != old.launch_at_login:
                success, launch_message = self.launch_at_login.set_enabled(requested_launch_at_login)
                if not success:
                    values["launch_at_login"] = old.launch_at_login
            new = self.settings.update(**values)
            self.panel.apply_theme()
            if old.hotkey != new.hotkey or old.double_tap_interval_ms != new.double_tap_interval_ms:
                self.hotkey.start(new)
            if old.capture_enabled != new.capture_enabled:
                self.clipboard.sync_cursor()
            self.repository.cleanup(new.max_history_items, new.retention_days)
            self._reload_history()
            if launch_message:
                self.panel.set_status(launch_message)
        self.panel.keep_open(False)

    def _save_panel_position(self, x: int, y: int) -> None:
        if self.settings.value.panel_x == x and self.settings.value.panel_y == y:
            return
        self.settings.update(panel_x=x, panel_y=y)

    def open_data_directory(self) -> None:
        if not PlatformBridge.reveal(self.data_dir):
            self.panel.set_status("无法打开数据目录")

    def clear_history(self) -> None:
        self.clipboard.sync_cursor()
        removed = self.repository.clear_unpinned()
        self._reload_history()
        self.panel.set_status(f"已清除 {removed} 条未置顶历史")

    def clear_all_history(self) -> None:
        self.clipboard.sync_cursor()
        removed = self.repository.clear_all()
        self._reload_history()
        self.panel.set_status(f"已清空 {removed} 条历史")

    def open_accessibility_settings(self) -> None:
        if PlatformBridge.request_accessibility_permission():
            self.panel.set_status("已打开辅助功能设置；请启用 ClipSoon 后返回")
        else:
            self.panel.set_status("当前平台不需要 macOS 辅助功能权限")

    def _captured(self, item) -> None:
        self._reload_history()
        self.panel.set_status(f"已记录：{item.title}")

    def _delete_many(self, items) -> None:
        removed = self.repository.delete_many(tuple(item.id for item in items))
        if removed:
            self._reload_history()
            self.panel.set_status(f"已删除 {removed} 条")

    def _reload_history(self) -> None:
        self.panel.set_items(self.repository.list_items(self.settings.value.max_history_items + 1_000))

    def _toggle_capture(self, paused: bool) -> None:
        enabled = not paused
        if self.settings.value.capture_enabled != enabled:
            self.settings.update(capture_enabled=enabled)
            self.clipboard.sync_cursor()
        self.panel.set_status("已暂停记录" if paused else "正在记录")

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_panel()

    def _send_finished(self, message: str, success: bool) -> None:
        self._reload_history()
        self.panel.set_status(message)
        if not success and self.tray.isVisible():
            self.tray.showMessage("ClipSoon", message, QSystemTrayIcon.MessageIcon.Warning, 3_000)

    def _hotkey_failed(self, message: str) -> None:
        if sys.platform == "darwin" and "辅助功能" in message:
            self.panel.set_accessibility_warning()
        else:
            self.panel.set_status(message)
        if self.tray.isVisible():
            self.tray.showMessage("ClipSoon 需要权限", message, QSystemTrayIcon.MessageIcon.Warning, 6_000)

    def _notify_error(self, message: str) -> None:
        LOGGER.warning(message)
        self.panel.set_status(message)

    def shutdown(self) -> None:
        self._panel_watch_timer.stop()
        self._hotkey_health_timer.stop()
        self.hotkey.stop()
        self.clipboard.stop()
        QThreadPool.globalInstance().waitForDone(3_000)
        self.repository.close()
        self.tray.hide()


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv
    started = time.perf_counter()
    qt_app = QApplication(arguments)
    qt_app.setApplicationName("ClipSoon")
    qt_app.setApplicationVersion(__version__)
    qt_app.setOrganizationName("ClipSoon")
    qt_app.setQuitOnLastWindowClosed(False)
    PlatformBridge.configure_macos_accessory()

    data_dir = Path(os.environ.get("CLIPSOON_DATA_DIR") or user_data_path("ClipSoon", appauthor=False))
    data_dir.mkdir(parents=True, exist_ok=True)
    _configure_logging(data_dir)
    _configure_crash_reporting(data_dir)
    lock = QLockFile(str(data_dir / "ClipSoon.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(50):
        LOGGER.warning("Another ClipSoon instance is already running")
        return 2

    application = ClipSoonApplication(qt_app, data_dir)
    application.start()
    if "--show" in arguments:
        QTimer.singleShot(80, application.show_panel)
    LOGGER.info("Warm resident startup completed in %.1f ms", (time.perf_counter() - started) * 1_000)
    exit_code = qt_app.exec()
    lock.unlock()
    return exit_code


def _configure_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "clipsoon.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _configure_crash_reporting(data_dir: Path) -> None:
    global _CRASH_LOG_STREAM
    try:
        crash_log = data_dir / "logs" / "native-crash.log"
        _CRASH_LOG_STREAM = crash_log.open("a", encoding="utf-8")
        faulthandler.enable(_CRASH_LOG_STREAM, all_threads=True)
    except (OSError, RuntimeError):
        LOGGER.exception("Could not enable native crash reporting")

    def log_unhandled(exception_type, exception, traceback) -> None:
        LOGGER.critical(
            "Unhandled Python exception",
            exc_info=(exception_type, exception, traceback),
        )
        sys.__excepthook__(exception_type, exception, traceback)

    sys.excepthook = log_unhandled


if __name__ == "__main__":
    raise SystemExit(main())
