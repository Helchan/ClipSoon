from __future__ import annotations

import threading
import time
from dataclasses import asdict

import pytest
from PySide6.QtCore import QEvent, QPointF, QRunnable, Qt, QThreadPool
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

import clipsoon.app as app_module
from clipsoon.app import ClipSoonApplication, _WindowsPanelGuard
from clipsoon.core import WINDOWS_DEFAULT_HOTKEY, AppSettings, ClipKind, JsonSettingsStore
from clipsoon.system import ForegroundTargetHandle, HotkeyActivationContext, PlatformBridge
from clipsoon.ui import SettingsDialog


def test_windows_panel_guard_hides_on_first_outside_click_without_prior_activation() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=101, primary_button_down=False, cursor_inside=False)
    assert guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)


def test_windows_panel_guard_tracks_activation_and_foreground_changes() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=202, primary_button_down=False, cursor_inside=True)
    assert guard.saw_panel_foreground
    assert guard.should_hide(foreground=303, primary_button_down=False, cursor_inside=False)

    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)
    assert guard.should_hide(foreground=303, primary_button_down=False, cursor_inside=False)


def test_windows_panel_guard_keeps_inside_click_and_can_sync_ignored_input() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=True)
    guard.sync_primary_button(True)
    assert not guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)
    assert not guard.should_hide(foreground=101, primary_button_down=False, cursor_inside=False)
    assert guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)


def test_settings_dialog_applies_changes_and_reset_without_a_save_step(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    dialog = SettingsDialog(application.settings.value, accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.settings_changed.connect(lambda values: application._apply_settings(values, dialog))

    dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
    assert application.settings.value.theme == "dark"
    assert JsonSettingsStore(tmp_path / "settings.json").load().theme == "dark"

    dialog.maximum.setValue(800)
    assert application.settings.value.max_history_items == 800

    dialog.reset_button.click()
    assert application.settings.value.theme == "liquid_glass"
    assert application.settings.value.max_history_items == 500
    application.shutdown()


def test_closing_settings_returns_focus_to_the_permanent_search_target(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.show_panel()
    qtbot.waitExposed(application.panel)
    application.panel.search.clearFocus()

    application.show_settings()
    dialog = application._settings_dialog
    assert dialog is not None
    assert QApplication.activeModalWidget() is None
    assert application.panel._keep_open

    dialog.reject()

    qtbot.waitUntil(lambda: application._settings_dialog is None, timeout=500)
    qtbot.waitUntil(application.panel.search.hasFocus, timeout=500)
    assert not application.panel.search_icon.hasFocus()
    assert not application.panel._keep_open
    application.shutdown()


def test_open_settings_blocks_panel_hover_keyboard_and_search_input(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.repository.add_text("第一条")
    application.repository.add_text("第二条")
    application._reload_history()
    application.panel.show_panel()
    qtbot.waitExposed(application.panel)

    application.show_settings()
    dialog = application._settings_dialog
    assert dialog is not None
    assert application.panel._settings_interaction_blocked
    assert application.panel._settings_interaction_shield is not None
    assert application.panel._settings_interaction_shield.isVisible()
    assert not application.panel.search.hasFocus()

    initial_text = application.panel.search.text()
    initial_row = application.panel.list.currentIndex().row()
    qtbot.keyClicks(application.panel.search, "blocked")
    qtbot.keyPress(application.panel.search, Qt.Key.Key_Down)
    qtbot.keyPress(application.panel, Qt.Key.Key_Down)
    assert application.panel.search.text() == initial_text
    assert application.panel.list.currentIndex().row() == initial_row

    delegate = application.panel.list.itemDelegate()
    target = application.panel.model.index(1)
    hover_position = application.panel.list.visualRect(target).center()
    hover_event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(hover_position),
        QPointF(application.panel.list.viewport().mapToGlobal(hover_position)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert application.panel.eventFilter(application.panel.list.viewport(), hover_event)
    assert delegate.hovered_row == -1

    dialog.reject()

    qtbot.waitUntil(lambda: application._settings_dialog is None, timeout=500)
    qtbot.waitUntil(application.panel.search.hasFocus, timeout=500)
    assert not application.panel._settings_interaction_blocked
    assert not application.panel._settings_interaction_shield.isVisible()
    qtbot.keyClicks(application.panel.search, "ok")
    assert application.panel.search.text() == "ok"
    application.shutdown()


def test_show_settings_centers_dialog_on_visible_panel(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.panel.show_panel()
    qtbot.waitExposed(application.panel)

    application.show_settings()
    dialog = application._settings_dialog
    assert dialog is not None
    qtbot.waitExposed(dialog)

    panel_center = application.panel.frameGeometry().center()
    dialog_center = dialog.frameGeometry().center()
    assert abs(dialog_center.x() - panel_center.x()) <= 1
    assert abs(dialog_center.y() - panel_center.y()) <= 1
    application.shutdown()


def test_settings_up_key_and_reset_keep_dialog_open(qtbot, tmp_path, monkeypatch) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.settings.update(theme="dark", max_history_items=800, double_tap_interval_ms=500)
    monkeypatch.setattr(application.hotkey, "start", lambda _settings: None)
    application.panel.show_panel()
    qtbot.waitExposed(application.panel)

    application.show_settings()
    dialog = application._settings_dialog
    assert dialog is not None
    qtbot.waitExposed(dialog)

    hotkey_before = application.settings.value.hotkey
    interval_before = application.settings.value.double_tap_interval_ms
    qtbot.keyPress(dialog, Qt.Key.Key_Up)
    assert application._settings_dialog is dialog
    assert dialog.isVisible()
    assert application.panel.isVisible()
    assert application.settings.value.hotkey == hotkey_before
    assert application.settings.value.double_tap_interval_ms == interval_before

    dialog.reset_button.click()
    assert application._settings_dialog is dialog
    assert dialog.isVisible()
    assert application.settings.value.theme == "liquid_glass"
    assert application.settings.value.max_history_items == 500
    application.shutdown()


def test_settings_hotkey_restart_is_deferred_out_of_the_input_event(qtbot, tmp_path, monkeypatch) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    try:
        starts: list[AppSettings] = []
        monkeypatch.setattr(application.hotkey, "update_settings", starts.append)

        candidate_hotkey = "combo:ctrl+alt+k"
        values = asdict(application.settings.value)
        values["hotkey"] = candidate_hotkey
        application._apply_settings(values)

        assert application.settings.value.hotkey == candidate_hotkey
        assert starts == []
        qtbot.waitUntil(lambda: len(starts) == 1, timeout=500)
        assert starts[0].hotkey == candidate_hotkey
    finally:
        application.shutdown()


def test_windows_settings_close_reactivates_panel_before_restoring_search_focus(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    """Windows needs the panel reactivated after the settings window closes."""

    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.show_panel()
    qtbot.waitExposed(application.panel)
    original_platform = app_module.sys.platform

    try:
        # Instantiate the app using the local platform, then exercise the
        # Windows-only settings shell. This avoids creating a real Windows
        # clipboard worker on the non-Windows test host.
        monkeypatch.setattr(app_module.sys, "platform", "win32")
        application.panel.search.clearFocus()
        application.show_settings()
        dialog = application._settings_dialog
        assert dialog is not None
        assert QApplication.activeModalWidget() is None

        dialog.reject()

        qtbot.waitUntil(lambda: application._settings_dialog is None, timeout=500)
        qtbot.waitUntil(application.panel.search.hasFocus, timeout=500)
        assert application.panel.isActiveWindow()
        assert QApplication.focusWidget() is application.panel.search
    finally:
        monkeypatch.setattr(app_module.sys, "platform", original_platform)
        application.shutdown()


def test_show_settings_reuses_existing_non_blocking_window(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()

    application.show_settings()
    first = application._settings_dialog
    assert first is not None
    assert first.isVisible()
    assert QApplication.activeModalWidget() is None

    application.show_settings()

    assert application._settings_dialog is first
    first.reject()
    qtbot.waitUntil(lambda: application._settings_dialog is None, timeout=500)
    application.shutdown()


def test_clear_current_tab_history_preserves_other_kinds(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    text = application.repository.add_text("text")
    image = application.repository.add_image(b"image-bytes", 1, 1)
    files = application.repository.add_files((str(source),))
    application._reload_history()

    application.clear_current_tab_history(ClipKind.IMAGE)

    assert application.repository.get(image.id) is None
    assert application.repository.get(text.id) == text
    assert application.repository.get(files.id) == files
    assert application.panel.status.text() == "已清空 1 条截图历史"
    assert {item.id for item in application.panel._items} == {text.id, files.id}

    application.clear_all_history()

    assert application.repository.list_items() == []
    assert application.panel.status.text() == "已清空 2 条历史"
    application.shutdown()


def test_clear_history_removes_only_unpinned_items(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    pinned = application.repository.add_text("pinned")
    unpinned = application.repository.add_text("unpinned")
    application.repository.set_pinned(pinned.id, True)
    application._reload_history()

    application.clear_history()

    assert application.repository.get(pinned.id) is not None
    assert application.repository.get(unpinned.id) is None
    assert [item.id for item in application.panel._items] == [pinned.id]
    assert application.panel.status.text() == "已清除 1 条未置顶历史"
    application.shutdown()


def test_pin_many_updates_history_order_and_status(qtbot, tmp_path) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    older = application.repository.add_text("older")
    newer = application.repository.add_text("newer")
    application._reload_history()

    application._pin_many((older,), True)

    pinned = application.repository.get(older.id)
    assert pinned is not None and pinned.pinned
    assert application.panel._items[0].id == older.id
    assert application.panel.status.text() == "已置顶 1 条"

    application._pin_many((pinned,), False)

    unpinned = application.repository.get(older.id)
    assert unpinned is not None and not unpinned.pinned
    assert {item.id for item in application.panel._items} == {older.id, newer.id}
    assert application.panel.status.text() == "已取消置顶 1 条"
    application.shutdown()


@pytest.mark.parametrize("theme", ("liquid_glass",))
def test_system_appearance_signals_refresh_dynamic_theme_and_open_settings_dialog(
    qtbot,
    tmp_path,
    monkeypatch,
    theme: str,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.settings.update(theme=theme)

    class RecordingDialog:
        def __init__(self) -> None:
            self.applied_settings: list[AppSettings] = []

        def apply_settings(self, settings: AppSettings) -> None:
            self.applied_settings.append(settings)

    dialog = RecordingDialog()
    application._settings_dialog = dialog  # type: ignore[assignment]
    panel_repaints: list[None] = []
    material_syncs: list[object] = []
    monkeypatch.setattr(application.panel, "apply_theme", lambda: panel_repaints.append(None))
    monkeypatch.setattr(application, "_sync_native_material", material_syncs.append)

    # A single macOS/Windows appearance transition can send both notifications.
    # They must resolve the theme only once, after the event loop returns.
    application.qt_app.paletteChanged.emit(application.qt_app.palette())
    application.qt_app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)

    qtbot.waitUntil(lambda: panel_repaints == [None], timeout=500)
    assert dialog.applied_settings == [application.settings.value]
    assert material_syncs == [application.panel, dialog]

    application._settings_dialog = None
    application.shutdown()


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_system_appearance_signals_leave_explicit_theme_untouched(
    qtbot,
    tmp_path,
    monkeypatch,
    theme: str,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.settings.update(theme=theme)
    panel_repaints: list[None] = []
    material_syncs: list[object] = []
    monkeypatch.setattr(application.panel, "apply_theme", lambda: panel_repaints.append(None))
    monkeypatch.setattr(application, "_sync_native_material", material_syncs.append)

    application.qt_app.paletteChanged.emit(application.qt_app.palette())
    application.qt_app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    qtbot.wait(20)

    assert panel_repaints == []
    assert material_syncs == []
    application.shutdown()


def test_windows_liquid_glass_syncs_native_backdrop_and_clears_when_theme_changes(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    applied: list[int] = []
    cleared: list[int] = []
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(
        PlatformBridge,
        "apply_windows_desktop_acrylic",
        lambda identifier: applied.append(identifier) or True,
    )
    monkeypatch.setattr(
        PlatformBridge,
        "clear_windows_desktop_acrylic",
        lambda identifier: cleared.append(identifier) or True,
    )

    application.settings.update(theme="liquid_glass")
    application.panel.apply_theme()
    application._sync_windows_backdrop(application.panel)

    assert applied == [int(application.panel.winId())]
    assert application.panel.native_backdrop_active

    application.settings.update(theme="light")
    application.panel.apply_theme()
    application._sync_windows_backdrop(application.panel)

    assert cleared == [int(application.panel.winId())]
    assert not application.panel.native_backdrop_active
    application.shutdown()


def test_macos_native_material_matches_each_window_shell_radius(qtbot, tmp_path, monkeypatch) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.settings.update(theme="liquid_glass")
    application.panel.apply_theme()

    class RecordingBackdrop:
        def __init__(self) -> None:
            self.applied: list[tuple[int, float, float, str]] = []
            self.removed: list[int] = []

        def apply(
            self,
            window_id: int,
            *,
            corner_radius: float,
            content_inset: float,
            material_role: str,
        ):
            self.applied.append((window_id, corner_radius, content_inset, material_role))
            return type("Result", (), {"applied": True})()

        def remove(self, window_id: int):
            self.removed.append(window_id)
            return type("Result", (), {"applied": True})()

    backdrop = RecordingBackdrop()
    application._macos_backdrop = backdrop  # type: ignore[assignment]
    monkeypatch.setattr(app_module.sys, "platform", "darwin")

    application._sync_native_material(application.panel)
    dialog = SettingsDialog(AppSettings(theme="liquid_glass"), accessibility_granted=True)
    qtbot.addWidget(dialog)
    application._sync_native_material(dialog)

    assert backdrop.applied == [
        (int(application.panel.winId()), 18.0, 14.0, "popover"),
        (int(dialog.winId()), 16.0, 0.0, "under_window"),
    ]
    assert application.panel.native_backdrop_active
    assert dialog.native_backdrop_active
    application.shutdown()


@pytest.mark.parametrize(
    "invalid_hotkey",
    ("double:alt", "combo:ctrl+print"),
)
def test_windows_application_migrates_and_persists_invalid_hotkey(
    qtbot,
    tmp_path,
    monkeypatch,
    invalid_hotkey: str,
) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=invalid_hotkey, double_tap_interval_ms=650))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)

    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()

    assert application._windows_hotkey_migrated
    assert application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY
    persisted = store.load()
    assert persisted.hotkey == WINDOWS_DEFAULT_HOTKEY
    assert persisted.double_tap_interval_ms == 650
    application.shutdown()


def test_windows_hotkey_registration_failure_rolls_back_to_last_ready_setting(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    previous_hotkey = "combo:ctrl+alt+o"
    candidate_hotkey = "combo:ctrl+alt+k"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=previous_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application._hotkey_ready(previous_hotkey)
    previous = application.settings.value
    application.settings.update(hotkey=candidate_hotkey)
    application._pending_hotkey_rollback = application._confirmed_windows_hotkey
    application._pending_hotkey_candidate = candidate_hotkey
    starts: list[AppSettings] = []
    monkeypatch.setattr(application.hotkey, "start", starts.append)

    application._hotkey_registration_failed(
        candidate_hotkey,
        "candidate shortcut already registered",
    )

    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == previous_hotkey,
        timeout=500,
    )
    assert store.load().hotkey == previous_hotkey
    assert starts == [previous]
    assert application._pending_hotkey_rollback is None
    assert application._pending_hotkey_candidate == ""
    assert "已恢复上一个快捷键" in application.panel.status.text()

    # If the restored custom combination is also unavailable, there is one
    # final fallback to the built-in combination.
    application._hotkey_registration_failed(
        previous_hotkey,
        "restored shortcut also unavailable",
    )
    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY,
        timeout=500,
    )
    assert [settings.hotkey for settings in starts] == [
        previous_hotkey,
        WINDOWS_DEFAULT_HOTKEY,
    ]
    assert store.load().hotkey == WINDOWS_DEFAULT_HOTKEY

    # Failure of the built-in fallback is display-only and cannot create a
    # default-to-default restart loop.
    application._hotkey_registration_failed(
        WINDOWS_DEFAULT_HOTKEY,
        "default shortcut also unavailable",
    )
    qtbot.wait(20)
    assert len(starts) == 2
    assert application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY
    assert application.panel.status.text() == "default shortcut also unavailable"
    application.shutdown()


def test_windows_hotkey_candidate_ready_commits_and_clears_rollback(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    previous_hotkey = "combo:ctrl+alt+o"
    candidate_hotkey = "combo:ctrl+alt+k"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=previous_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application._hotkey_ready(previous_hotkey)
    previous = application.settings.value
    candidate = application.settings.update(hotkey=candidate_hotkey)
    application._pending_hotkey_rollback = previous
    application._pending_hotkey_candidate = candidate_hotkey

    application._hotkey_ready(candidate_hotkey)

    assert application._confirmed_windows_hotkey == candidate
    assert application._pending_hotkey_rollback is None
    assert application._pending_hotkey_candidate == ""
    assert store.load().hotkey == candidate_hotkey
    application.shutdown()


def test_initial_windows_custom_registration_failure_falls_back_to_default(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    custom_hotkey = "combo:ctrl+alt+o"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=custom_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    starts: list[AppSettings] = []
    monkeypatch.setattr(application.hotkey, "start", starts.append)

    assert application._confirmed_windows_hotkey is None
    assert application._pending_hotkey_rollback is None
    application._hotkey_registration_failed(
        custom_hotkey,
        "initial shortcut already registered",
    )

    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY,
        timeout=500,
    )
    assert [settings.hotkey for settings in starts] == [WINDOWS_DEFAULT_HOTKEY]
    assert store.load().hotkey == WINDOWS_DEFAULT_HOTKEY
    assert "已改用 Ctrl+Shift+Space" in application.panel.status.text()
    application.shutdown()


def test_application_requests_verified_native_activation_after_show(qtbot, tmp_path, monkeypatch) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.keep_open(True)
    activation_requests: list[int] = []
    monkeypatch.setattr(PlatformBridge, "accessibility_permission_status", lambda: None)
    monkeypatch.setattr(
        PlatformBridge,
        "capture_target",
        lambda: ForegroundTargetHandle("windows", 101, "Editor"),
    )
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: 101)
    monkeypatch.setattr(PlatformBridge, "primary_button_down", lambda: False)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(
        PlatformBridge,
        "request_window_activation",
        lambda identifier: activation_requests.append(identifier) or True,
    )

    application.show_panel()
    assert application._panel_watch_timer.isActive()

    qtbot.waitUntil(lambda: bool(activation_requests), timeout=500)
    assert activation_requests == [int(application.panel.winId())]
    assert application._panel_guard.initial_foreground == 101
    assert application._panel_guard.saw_panel_foreground
    application.panel.keep_open(False)
    application.shutdown()


def test_hotkey_context_preserves_target_across_panel_activation_retry(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    activation_results = iter((False, True))
    activation_requests: list[int] = []
    captured_target_arguments: list[tuple[int, dict[str, int | None]]] = []
    foreground = [303]
    monkeypatch.setattr(PlatformBridge, "accessibility_permission_status", lambda: None)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(
        PlatformBridge,
        "target_from_window_id",
        lambda identifier, **details: (
            captured_target_arguments.append((identifier, details))
            or ForegroundTargetHandle(
                "windows",
                identifier,
                "Editor",
                details["target_thread_id"],
                details["target_process_id"],
                details["focus_window"],
                details["focus_thread_id"],
                details["focus_process_id"],
            )
        ),
    )
    monkeypatch.setattr(
        PlatformBridge,
        "capture_target",
        lambda: (_ for _ in ()).throw(AssertionError("late target capture")),
    )
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: foreground[0])
    monkeypatch.setattr(PlatformBridge, "primary_button_down", lambda: False)
    def request_activation(identifier: int) -> bool:
        activation_requests.append(identifier)
        activated = next(activation_results)
        if activated:
            foreground[0] = identifier
        return activated

    monkeypatch.setattr(PlatformBridge, "request_window_activation", request_activation)
    application.show_panel(
        HotkeyActivationContext(
            target_window=303,
            target_thread_id=7001,
            target_process_id=8001,
            focus_window=304,
            focus_thread_id=7002,
            focus_process_id=8001,
            foreground_granted=True,
        )
    )

    qtbot.waitUntil(lambda: len(activation_requests) == 2, timeout=500)
    panel_window = int(application.panel.winId())
    assert application.target == ForegroundTargetHandle(
        "windows",
        303,
        "Editor",
        7001,
        8001,
        304,
        7002,
        8001,
    )
    assert captured_target_arguments == [
        (
            303,
            {
                "target_thread_id": 7001,
                "target_process_id": 8001,
                "focus_window": 304,
                "focus_thread_id": 7002,
                "focus_process_id": 8001,
            },
        )
    ]
    assert activation_requests == [panel_window, panel_window]
    assert application._panel_guard.initial_foreground == 303
    assert application._panel_guard.saw_panel_foreground
    assert application.panel.isVisible()
    application.shutdown()


def test_windows_toggle_reactivates_visible_background_panel_instead_of_hiding(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.set_native_deactivation_managed(True)
    application.panel.show()
    panel_window = int(application.panel.winId())
    foreground = [404]
    shown: list[HotkeyActivationContext | None] = []
    context = HotkeyActivationContext(target_window=404, foreground_granted=True)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: foreground[0])
    monkeypatch.setattr(application, "show_panel", shown.append)

    application.toggle_panel(context)

    assert application.panel.isVisible()
    assert shown == [context]

    foreground[0] = panel_window
    application.toggle_panel(context)

    assert not application.panel.isVisible()
    application.shutdown()


def test_file_history_sweep_removes_deleted_source_without_blocking_panel(
    qtbot,
    tmp_path,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    path = tmp_path / "external-file.txt"
    path.write_text("content", encoding="utf-8")
    item = application.repository.add_files((str(path),))
    application._reload_history()
    assert application.panel.model.rowCount() == 1
    path.unlink()

    application._schedule_file_history_sweep()

    qtbot.waitUntil(lambda: not application._file_history_sweep_active, timeout=1_000)
    assert application.repository.get(item.id) is None
    assert application.panel.model.rowCount() == 0
    application.shutdown()


def test_hung_file_history_sweep_does_not_occupy_global_pool_or_block_shutdown(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    sweep_started = threading.Event()
    release_sweep = threading.Event()

    def blocked_prune() -> tuple[str, ...]:
        sweep_started.set()
        assert release_sweep.wait(2)
        return ()

    monkeypatch.setattr(application.repository, "prune_missing_file_items", blocked_prune)
    application._schedule_file_history_sweep()
    assert sweep_started.wait(1)

    global_pool_ran = threading.Event()

    class MarkerTask(QRunnable):
        def run(self) -> None:
            global_pool_ran.set()

    QThreadPool.globalInstance().start(MarkerTask())
    assert global_pool_ran.wait(1)

    started = time.perf_counter()
    application.shutdown()
    assert time.perf_counter() - started < 1

    release_sweep.set()
    qtbot.waitUntil(lambda: not application._file_history_sweep_active, timeout=1_000)
