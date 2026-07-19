"""ClipSoon composition root."""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_data_path
from PySide6.QtCore import QLockFile, QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from clipsoon import __version__
from clipsoon.core import HistoryRepository, JsonSettingsStore, ObservableSettings
from clipsoon.system import (
    ClipboardController,
    ForegroundTargetHandle,
    GlobalHotkeyService,
    PlatformBridge,
    PynputPasteAdapter,
    SelectionSender,
)
from clipsoon.ui import ClipPanel, SettingsDialog, create_tray_icon

LOGGER = logging.getLogger(__name__)


class _Signals(QObject):
    hotkey = Signal()
    hotkey_failed = Signal(str)


class ClipSoonApplication(QObject):
    def __init__(self, qt_app: QApplication, data_dir: Path) -> None:
        super().__init__()
        self.qt_app, self.data_dir = qt_app, data_dir
        self.settings = ObservableSettings(JsonSettingsStore(data_dir / "settings.json"))
        self.repository = HistoryRepository(data_dir)
        self.signals = _Signals()
        self.target: ForegroundTargetHandle | None = None

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
            self.panel.hide,
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
        self.sender.finished.connect(self._send_finished)
        self.tray_actions["show"].triggered.connect(self.show_panel)
        self.tray_actions["pause"].toggled.connect(self._toggle_capture)
        self.tray_actions["settings"].triggered.connect(self.show_settings)
        self.tray_actions["quit"].triggered.connect(self.qt_app.quit)
        self.tray.activated.connect(self._tray_activated)
        self.qt_app.aboutToQuit.connect(self.shutdown)

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self.show_panel()

    def show_panel(self) -> None:
        if (
            PlatformBridge.accessibility_permission_status() is True
            and self.panel.has_accessibility_warning()
        ):
            self.panel.set_status("准备就绪")
        self.target = PlatformBridge.capture_target()
        elapsed = self.panel.show_panel()
        LOGGER.info("Panel visible in %.1f ms; target=%s", elapsed, self.target.name if self.target else "none")
        if elapsed > 100:
            LOGGER.warning("Hotkey-to-visible budget exceeded: %.1f ms", elapsed)

    def show_settings(self) -> None:
        self.panel.keep_open(True)
        dialog = SettingsDialog(
            self.settings.value,
            self.panel if self.panel.isVisible() else None,
            accessibility_granted=PlatformBridge.accessibility_permission_status(),
        )
        dialog.clear_requested.connect(self.clear_history)
        dialog.reveal_requested.connect(lambda: PlatformBridge.reveal(self.data_dir))
        dialog.accessibility_requested.connect(self.open_accessibility_settings)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            old = self.settings.value
            new = self.settings.update(**dialog.values())
            self.panel.apply_theme()
            if old.hotkey != new.hotkey or old.double_tap_interval_ms != new.double_tap_interval_ms:
                self.hotkey.start(new)
            if old.capture_enabled != new.capture_enabled:
                self.clipboard.sync_cursor()
            self.repository.cleanup(new.max_history_items, new.retention_days)
            self._reload_history()
        self.panel.keep_open(False)

    def clear_history(self) -> None:
        removed = self.repository.clear_unpinned()
        self.clipboard.sync_cursor()
        self._reload_history()
        self.panel.set_status(f"已清除 {removed} 条未置顶历史")

    def clear_all_history(self) -> None:
        removed = self.repository.clear_all()
        self.clipboard.sync_cursor()
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
        self.hotkey.stop()
        self.clipboard.stop()
        QThreadPool.globalInstance().waitForDone(3_000)
        self.repository.close()
        self.tray.hide()


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    arguments = argv if argv is not None else sys.argv
    qt_app = QApplication(arguments)
    qt_app.setApplicationName("ClipSoon")
    qt_app.setApplicationVersion(__version__)
    qt_app.setOrganizationName("ClipSoon")
    qt_app.setQuitOnLastWindowClosed(False)
    PlatformBridge.configure_macos_accessory()

    data_dir = Path(os.environ.get("CLIPSOON_DATA_DIR") or user_data_path("ClipSoon", appauthor=False))
    data_dir.mkdir(parents=True, exist_ok=True)
    _configure_logging(data_dir)
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


if __name__ == "__main__":
    raise SystemExit(main())
