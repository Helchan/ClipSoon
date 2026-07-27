from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QItemSelectionModel,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QMenu,
    QStyle,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QWidget,
)

import clipsoon.ui as ui_module
from clipsoon import __version__
from clipsoon.core import WINDOWS_DEFAULT_HOTKEY, AppSettings, ClipItem, ClipKind
from clipsoon.ui import (
    ClipDelegate,
    ClipPanel,
    ImagePreview,
    SearchIcon,
    SettingsDialog,
    _accent_foreground,
    _bucketed_size,
    _ByteLruCache,
    _compact_menu,
    _DestructiveConfirmationDialog,
    _hotkey_display,
    _hover_color,
    _paint_liquid_glass_material,
    _parse_hotkey,
    _platform_hotkey_validation_error,
    _ScaledImageLoader,
    _settings_control_border_token,
    _style_sheet,
    _theme_colors,
    _ThemeAppearance,
    create_tray_icon,
)


def clip(item_id: str, text: str, updated: float) -> ClipItem:
    return ClipItem(item_id, ClipKind.TEXT, item_id, updated, updated, text=text)


def test_panel_search_keyboard_send_and_escape(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    panel.set_items([clip("old-exact", "invoice 2026", 1), clip("new-prefix", "invoice 2026 final", 2)])
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.search.setText("invoice 2026")
    assert panel.model.item_at(0).id == "old-exact"
    sent: list[ClipItem] = []
    panel.send_requested.connect(sent.append)
    qtbot.keyPress(panel.search, Qt.Key.Key_Return)
    assert sent and sent[0].id == "old-exact"
    qtbot.keyPress(panel.search, Qt.Key.Key_Escape)
    assert not panel.isVisible()


def test_panel_search_return_sends_when_input_method_ui_is_visible(qtbot, monkeypatch) -> None:
    panel = ClipPanel(AppSettings)
    panel.set_items([clip("item", "内容", 1)])
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)
    sent: list[ClipItem] = []
    panel.send_requested.connect(sent.append)
    monkeypatch.setattr(QApplication.inputMethod(), "isVisible", lambda: True)

    qtbot.keyPress(panel.search, Qt.Key.Key_Return)
    qtbot.keyPress(panel.search, Qt.Key.Key_Enter)

    assert [item.id for item in sent] == ["item", "item"]


def test_panel_search_return_waits_for_active_input_method_composition(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    panel.set_items([clip("item", "内容", 1)])
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)
    sent: list[ClipItem] = []
    panel.send_requested.connect(sent.append)

    QApplication.sendEvent(panel.search, QInputMethodEvent("pin", []))
    assert panel._search_caret._ime_composing
    qtbot.keyPress(panel.search, Qt.Key.Key_Return)
    assert sent == []

    QApplication.sendEvent(panel.search, QInputMethodEvent("", []))
    assert not panel._search_caret._ime_composing
    qtbot.keyPress(panel.search, Qt.Key.Key_Return)
    assert [item.id for item in sent] == ["item"]


def test_status_is_empty_when_idle_and_transient_messages_clear(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    assert panel.status.text() == ""
    panel.set_status("已删除 1 条", timeout_ms=20)
    assert panel.status.text() == "已删除 1 条"
    qtbot.waitUntil(lambda: panel.status.text() == "", timeout=500)


def test_new_status_restarts_timer_and_permission_warning_persists(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    panel.set_status("旧消息", timeout_ms=20)
    qtbot.wait(10)
    panel.set_status("新消息", timeout_ms=80)
    qtbot.wait(25)
    assert panel.status.text() == "新消息"
    qtbot.waitUntil(lambda: panel.status.text() == "", timeout=500)

    panel.set_accessibility_warning()
    qtbot.wait(30)
    assert panel.has_accessibility_warning()
    panel.clear_status()
    assert panel.status.text() == ""


def test_empty_history_uses_an_explanatory_state_without_inert_details(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    assert panel.history_content.currentWidget() is panel.empty_state
    assert panel.empty_state_title.text() == "还没有剪贴板历史"
    assert panel.empty_state_message.text() == "复制文本、图片或文件后，内容会出现在这里。"
    assert not panel.empty_state_clear.isVisible()
    assert not panel.detail.isVisible()
    assert panel.info_type_value.text() == ""
    assert panel.info_detail_label.text() == ""
    assert panel.info_detail_value.text() == ""


def test_no_match_empty_state_can_clear_search_and_restore_the_list(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    panel.set_items([clip("item", "可找到的内容", 1)])
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    panel.search.setText("不存在")

    assert panel.history_content.currentWidget() is panel.empty_state
    assert panel.empty_state_title.text() == "没有找到匹配内容"
    assert panel.empty_state_clear.isVisible()
    assert not panel.detail.isVisible()

    qtbot.mouseClick(panel.empty_state_clear, Qt.MouseButton.LeftButton)

    assert panel.search.text() == ""
    assert panel.history_content.currentWidget() is panel.list
    assert panel.detail.isVisible()
    assert panel.list.currentIndex().row() == 0


def test_empty_filter_state_does_not_offer_an_irrelevant_clear_action(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    panel.set_items([clip("text", "仅有文本", 1)])
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    screenshot_filter = panel._filter_buttons[2][0]
    qtbot.mouseClick(screenshot_filter, Qt.MouseButton.LeftButton)

    assert panel.history_content.currentWidget() is panel.empty_state
    assert panel.empty_state_title.text() == "暂无截图历史"
    assert panel.empty_state_message.text() == "切换分类或继续复制内容。"
    assert not panel.empty_state_clear.isVisible()
    assert not panel.detail.isVisible()


def test_settings_and_custom_hotkey_validation(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(hotkey="combo:ctrl+shift+v"))
    qtbot.addWidget(dialog)
    assert _parse_hotkey("Control + Shift + V") == "combo:ctrl+shift+v"
    expected_qt_ctrl = "combo:shift+meta+v" if sys.platform == "darwin" else "combo:ctrl+shift+v"
    assert _parse_hotkey("Ctrl + Shift + V") == expected_qt_ctrl
    assert _hotkey_display("combo:ctrl+shift+v") == (
        "Meta+Shift+V" if sys.platform == "darwin" else "Ctrl+Shift+V"
    )
    assert _parse_hotkey("V") == ""
    expected_plus_ctrl = "meta" if sys.platform == "darwin" else "ctrl"
    assert _parse_hotkey("Ctrl++") == f"combo:{expected_plus_ctrl}+plus"
    assert _hotkey_display("combo:ctrl+plus").endswith("++")
    assert dialog.findChildren(QFrame, "settingsSection")
    assert dialog.findChild(QFrame, "settingsSection") is not None
    assert not dialog.isModal()
    assert not any(
        label.text() == "配置快捷键、历史记录与粘贴行为"
        for label in dialog.findChildren(QLabel)
    )
    assert not hasattr(dialog, "version_label")
    assert dialog.close_button.text() == "关闭"
    assert dialog.close_button.accessibleName() == "关闭设置"
    assert dialog.reset_button.text() == "重置"
    assert not dialog.findChildren(QDialogButtonBox)


def test_settings_dialog_keeps_qwidget_hide_callable(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.hide()

    assert not dialog.isVisible()


def test_windows_settings_only_offer_registered_combo_and_hide_double_interval(
    qtbot,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    dialog = SettingsDialog(
        AppSettings(hotkey="double:ctrl", double_tap_interval_ms=650),
        accessibility_granted=True,
    )
    qtbot.addWidget(dialog)

    assert dialog.hotkey_mode is None
    assert dialog.custom_hotkey.isEnabled()
    assert _hotkey_display(WINDOWS_DEFAULT_HOTKEY).casefold() == "ctrl+shift+space"
    assert dialog.values()["hotkey"] == WINDOWS_DEFAULT_HOTKEY
    field_labels = dialog.findChildren(QLabel, "settingsFieldLabel")
    assert "呼出方式" not in [label.text() for label in field_labels]
    assert "自定义组合键" not in [label.text() for label in field_labels]
    assert [label.text() for label in field_labels].count("快捷键") == 1
    assert dialog.findChildren(QComboBox) == [dialog.theme]
    dialog.custom_hotkey.setKeySequence(QKeySequence("Ctrl+Alt+K"))
    assert dialog.values()["hotkey"] == "combo:ctrl+alt+k"
    warnings: list[tuple[str, object]] = []
    monkeypatch.setattr(
        ui_module,
        "_show_themed_warning",
        lambda _parent, _title, message, *, appearance: warnings.append((message, appearance)),
    )
    dialog.custom_hotkey.clear()
    dialog._emit_hotkey_change()
    assert warnings == [
        ("组合键必须包含 Ctrl/Shift/Alt/Command 和一个普通键。", dialog._appearance)
    ]
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.interval.isHidden()
    interval_labels = [
        label
        for label in field_labels
        if label.text() == "双击间隔"
    ]
    assert len(interval_labels) == 1
    assert interval_labels[0].isHidden()


def test_non_windows_settings_retain_double_modifier_modes(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "linux")
    dialog = SettingsDialog(
        AppSettings(hotkey="double:shift", double_tap_interval_ms=360),
        accessibility_granted=True,
    )
    qtbot.addWidget(dialog)

    assert dialog.hotkey_mode is not None
    assert dialog.hotkey_mode.count() == len(SettingsDialog._HOTKEYS)
    assert dialog.hotkey_mode.currentText() == "双击 Shift"
    assert dialog.values()["hotkey"] == "double:shift"
    assert not dialog.interval.isHidden()
    assert dialog.interval.value() == 360


def test_windows_rejects_custom_keys_not_supported_by_register_hotkey(monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")

    assert _platform_hotkey_validation_error("combo:ctrl+print")
    assert _platform_hotkey_validation_error("combo:ctrl+,") == ""


def test_settings_layout_is_compact_and_controls_are_aligned(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    sections = dialog.findChildren(QFrame, "settingsSection")
    assert len(sections) == 3
    assert dialog.width() == 580
    assert dialog.height() < 720
    controls = [
        dialog.custom_hotkey,
        dialog.interval,
        dialog.maximum,
        dialog.retention,
        dialog.delay,
        dialog.theme,
        dialog.selection_memory,
    ]
    if dialog.hotkey_mode is not None:
        controls.insert(0, dialog.hotkey_mode)
    visible_controls = [control for control in controls if not control.isHidden()]
    assert len({control.width() for control in visible_controls}) == 1
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.findChild(QLabel, "settingsWindowTitle").text() == "ClipSoon 设置"
    assert dialog.close_button.geometry().top() == dialog.reset_button.geometry().top()
    assert dialog.close_button.geometry().right() < dialog.reset_button.geometry().left()
    assert "主题" in [label.text() for label in dialog.findChildren(QLabel, "settingsFieldLabel")]
    assert "外观" not in [label.text() for label in dialog.findChildren(QLabel, "settingsFieldLabel")]


def test_settings_typography_and_component_scale_matches_main_panel(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(theme="liquid_glass"), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    title = dialog.findChild(QLabel, "settingsWindowTitle")
    section_title = dialog.findChildren(QLabel, "settingsSectionTitle")[0]
    field_label = dialog.findChildren(QLabel, "settingsFieldLabel")[0]
    subtitle = dialog.findChild(QLabel, "settingsSubtitle")

    assert title.font().pointSizeF() == ui_module._SETTINGS_TITLE_FONT_SIZE_PT
    assert section_title.font().pointSizeF() == ui_module._SETTINGS_SECTION_FONT_SIZE_PT
    assert field_label.font().pointSizeF() == ui_module._SETTINGS_LABEL_FONT_SIZE_PT
    assert subtitle.font().pointSizeF() == ui_module._SETTINGS_HELP_FONT_SIZE_PT

    controls = (
        dialog.custom_hotkey,
        dialog.interval,
        dialog.maximum,
        dialog.retention,
        dialog.delay,
        dialog.theme,
        dialog.selection_memory,
    )
    for control in controls:
        if control.isHidden():
            continue
        assert control.font().pointSizeF() == ui_module._SETTINGS_CONTROL_FONT_SIZE_PT
        assert control.height() >= ui_module._SETTINGS_CONTROL_MIN_HEIGHT

    for checkbox in (
        dialog.capture,
        dialog.paste,
        dialog.hide_on_deactivate_checkbox,
        dialog.remember_selection,
        dialog.launch_at_login,
    ):
        assert checkbox.font().pointSizeF() == ui_module._SETTINGS_CONTROL_FONT_SIZE_PT
        assert checkbox.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT

    assert dialog.close_button.font().pointSizeF() == ui_module._SETTINGS_CONTROL_FONT_SIZE_PT
    assert dialog.reset_button.font().pointSizeF() == ui_module._SETTINGS_CONTROL_FONT_SIZE_PT
    assert field_label.font().pointSizeF() > subtitle.font().pointSizeF()
    assert dialog.maximum.font().pointSizeF() > field_label.font().pointSizeF()
    assert dialog.height() < 700


def test_settings_behavior_checkboxes_have_stable_non_overlapping_rows(qtbot) -> None:
    for theme in ("light", "dark", "liquid_glass"):
        dialog = SettingsDialog(AppSettings(theme=theme), accessibility_granted=True)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)

        assert dialog.capture.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT, theme
        assert dialog.paste.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT, theme
        assert (
            dialog.hide_on_deactivate_checkbox.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT
        ), theme
        assert dialog.remember_selection.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT, theme
        assert dialog.launch_at_login.height() == ui_module._SETTINGS_CHECKBOX_ROW_HEIGHT, theme
        option = QStyleOptionButton()
        dialog.capture.initStyleOption(option)
        indicator = dialog.capture.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            dialog.capture,
        )
        assert indicator.size() == QSize(
            ui_module._SETTINGS_CHECKBOX_INDICATOR_SIZE,
            ui_module._SETTINGS_CHECKBOX_INDICATOR_SIZE,
        ), theme
        assert dialog.capture.geometry().bottom() < dialog.hide_on_deactivate_checkbox.geometry().top(), theme
        assert dialog.paste.geometry().bottom() < dialog.remember_selection.geometry().top(), theme
        assert (
            dialog.hide_on_deactivate_checkbox.geometry().bottom()
            < dialog.launch_at_login.geometry().top()
        ), theme
        dialog.close()


def test_frameless_settings_footer_has_an_app_owned_close_control(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.findChild(QLabel, "settingsWindowTitle").text() == "ClipSoon 设置"
    assert dialog.close_button.text() == "关闭"
    assert dialog.close_button.geometry().top() == dialog.reset_button.geometry().top()
    assert dialog.close_button.geometry().right() < dialog.reset_button.geometry().left()
    assert dialog.close_button.accessibleName() == "关闭设置"
    qtbot.wait(10)
    assert dialog.focusWidget() is dialog
    assert not dialog.close_button.hasFocus()
    qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_frameless_settings_can_close_with_escape_from_child_editor(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.custom_hotkey.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.keyPress(dialog.custom_hotkey, Qt.Key.Key_Escape)

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_frameless_settings_can_close_when_clicking_outside(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    inside_global = dialog.mapToGlobal(QPoint(18, 18))
    inside = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(18, 18),
        QPointF(inside_global),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(dialog, inside)
    assert dialog.isVisible()

    outside_point = QPoint(dialog.width() + 24, dialog.height() + 24)
    outside_global = dialog.mapToGlobal(outside_point)
    outside = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(outside_point),
        QPointF(outside_global),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(dialog, outside)

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_frameless_settings_dismisses_main_panel_click_even_without_widget_mouse_event(
    qtbot,
) -> None:
    panel = ClipPanel(AppSettings)
    panel.resize(900, 700)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    dialog = SettingsDialog(AppSettings(), panel, accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.move(panel.mapToGlobal(QPoint(260, 120)))
    dialog.show()
    qtbot.waitExposed(dialog)

    panel_click = panel.mapToGlobal(QPoint(24, 24))
    assert not dialog._global_geometry().contains(panel_click)

    dismissed = dialog._poll_external_dismiss_state(
        buttons=Qt.MouseButton.LeftButton,
        cursor_pos=panel_click,
    )

    assert dismissed
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_frameless_settings_from_tray_waits_for_open_click_release_then_closes_outside(
    qtbot,
) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    outside_point = dialog.mapToGlobal(QPoint(dialog.width() + 24, dialog.height() + 24))
    dialog._external_dismiss_armed = False

    assert not dialog._poll_external_dismiss_state(
        buttons=Qt.MouseButton.LeftButton,
        cursor_pos=outside_point,
    )
    assert dialog.isVisible()

    assert not dialog._poll_external_dismiss_state(
        buttons=Qt.MouseButton.NoButton,
        cursor_pos=outside_point,
    )
    assert dialog._external_dismiss_armed

    dismissed = dialog._poll_external_dismiss_state(
        buttons=Qt.MouseButton.LeftButton,
        cursor_pos=outside_point,
    )

    assert dismissed
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_state_memory_setting_is_an_optional_three_second_default(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)

    assert dialog.remember_selection.text() == "记住上次状态"
    assert not dialog.remember_selection.isChecked()
    assert dialog.selection_memory.value() == 3
    assert not dialog.selection_memory.isEnabled()
    dialog.remember_selection.setChecked(True)
    assert dialog.selection_memory.isEnabled()
    assert dialog.values()["remember_selection"] is True
    assert dialog.values()["selection_memory_seconds"] == 3


def test_settings_changes_and_reset_are_emitted_immediately(qtbot) -> None:
    dialog = SettingsDialog(
        AppSettings(theme="light", max_history_items=750, capture_enabled=False),
        accessibility_granted=True,
    )
    qtbot.addWidget(dialog)
    changes: list[dict[str, object]] = []
    dialog.settings_changed.connect(changes.append)

    dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
    assert changes[-1]["theme"] == "dark"

    dialog.reset_button.click()
    assert changes[-1]["theme"] == "liquid_glass"
    assert changes[-1]["max_history_items"] == 500
    assert changes[-1]["capture_enabled"] is True


def test_destructive_confirmation_uses_the_clipsoon_dialog(qtbot) -> None:
    parent = QLabel()
    qtbot.addWidget(parent)
    dialog = _DestructiveConfirmationDialog(parent, "清空历史", "确认清空？", "确定", dark=False)
    qtbot.addWidget(dialog)

    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.cancel_button.text() == "取消"
    assert dialog.confirm_button.text() == "确定"
    assert dialog.cancel_button.isDefault()
    assert dialog.findChild(QFrame, "confirmationCard") is not None
    assert "min-width" not in dialog.styleSheet()
    assert "padding: 7px 10px" in dialog.styleSheet()

    acknowledgement = _DestructiveConfirmationDialog(
        parent,
        "快捷键无效",
        "请输入有效组合键。",
        "知道了",
        dark=False,
        cancel_text=None,
    )
    qtbot.addWidget(acknowledgement)
    assert acknowledgement.cancel_button is None
    assert acknowledgement.confirm_button.isDefault()


def test_launch_at_login_setting_round_trips_through_dialog(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(launch_at_login=True), accessibility_granted=True)
    qtbot.addWidget(dialog)

    assert dialog.launch_at_login.text() == "开机时自动启动 ClipSoon"
    assert dialog.launch_at_login.isChecked()
    dialog.launch_at_login.setChecked(False)
    assert dialog.values()["launch_at_login"] is False


def test_dark_settings_combo_popups_use_readable_theme_colors(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(theme="dark"), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    for combo in (dialog.hotkey_mode, dialog.theme):
        if combo is None:
            continue
        view = combo.view()
        palette = view.palette()
        assert palette.color(QPalette.ColorRole.Base) == QColor("#292D39")
        assert palette.color(QPalette.ColorRole.Text) == QColor("#F2F4F8")
        assert "background: #292D39" in view.styleSheet()
        assert "color: #F2F4F8" in view.styleSheet()
        assert "background: #5264E8; color: #FFFFFF" in view.styleSheet()
        assert "background: #292D39" in view.window().styleSheet()
        assert view.window().palette().color(QPalette.ColorRole.Window) == QColor("#292D39")

    dialog.theme.showPopup()
    qtbot.waitUntil(dialog.theme.view().isVisible, timeout=500)
    popup = dialog.theme.view()
    rendered = popup.viewport().grab().toImage()
    normal_rect = popup.visualRect(popup.model().index(0, 0))
    sample_x = rendered.width() - 5
    assert rendered.pixelColor(sample_x, normal_rect.center().y()) == QColor("#292D39")


def test_dark_settings_uses_an_app_owned_opaque_shell_on_macos_after_a_live_theme_switch(
    qtbot,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "darwin")
    dialog = SettingsDialog(AppSettings(theme="dark"), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert dialog.palette().color(QPalette.ColorRole.Window) == QColor(0, 0, 0, 0)
    assert "QDialog { background: transparent; }" in dialog.styleSheet()
    rendered = dialog.grab().toImage()
    assert rendered.pixelColor(4, dialog.height() // 2) == QColor("#1C1F27")

    dialog.apply_settings(AppSettings(theme="liquid_glass"))
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not dialog.autoFillBackground()

    dialog.apply_settings(AppSettings(theme="dark"))
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert dialog.palette().color(QPalette.ColorRole.Window) == QColor(0, 0, 0, 0)
    rendered = dialog.grab().toImage()
    assert rendered.pixelColor(4, dialog.height() // 2) == QColor("#1C1F27")


def test_settings_controls_and_confirmation_buttons_show_theme_focus(qtbot) -> None:
    dialog = SettingsDialog(
        AppSettings(theme="dark", hotkey="combo:ctrl+shift+space"),
        accessibility_granted=True,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.activateWindow()
    dialog.theme.setFocus()
    QApplication.processEvents()
    assert dialog.theme.hasFocus()
    rendered = dialog.grab().toImage()
    origin = dialog.theme.mapTo(dialog, QPoint(0, 0))
    assert rendered.pixelColor(origin.x(), origin.y() + dialog.theme.height() // 2) == QColor("#7180F5")
    assert "QKeySequenceEdit:focus" in dialog.styleSheet()
    assert "QKeySequenceEdit:disabled" in dialog.styleSheet()

    parent = QDialog()
    prompt = _DestructiveConfirmationDialog(
        parent,
        "清空历史",
        "确认清空？",
        "确定",
        appearance=_ThemeAppearance(dark=True),
    )
    qtbot.addWidget(parent)
    qtbot.addWidget(prompt)
    prompt.show()
    qtbot.waitExposed(prompt)
    prompt.activateWindow()
    prompt.cancel_button.setFocus()
    QApplication.processEvents()
    assert prompt.cancel_button.hasFocus()
    rendered = prompt.grab().toImage()
    origin = prompt.cancel_button.mapTo(prompt, QPoint(0, 0))
    assert rendered.pixelColor(origin.x(), origin.y() + prompt.cancel_button.height() // 2) == QColor(
        "#7180F5"
    )


def test_settings_checkboxes_use_the_active_theme_and_keep_a_visible_checkmark(qtbot) -> None:
    def indicator_pixels(dialog: SettingsDialog, checkbox) -> list[QColor]:
        option = QStyleOptionButton()
        checkbox.initStyleOption(option)
        indicator = checkbox.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            checkbox,
        )
        assert indicator.width() < checkbox.fontMetrics().height(), theme
        origin = checkbox.mapTo(dialog, indicator.topLeft())
        image = dialog.grab().toImage()
        return [
            image.pixelColor(origin.x() + x, origin.y() + y)
            for y in range(indicator.height())
            for x in range(indicator.width())
        ]

    for theme in ("light", "dark", "liquid_glass"):
        dialog = SettingsDialog(AppSettings(theme=theme), accessibility_granted=True)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)

        checkbox = dialog.capture
        colors = indicator_pixels(dialog, checkbox)
        expected = QColor(ui_module._theme_colors(dialog._appearance).accent)
        checkmark = QColor(ui_module._accent_foreground(dialog._appearance))
        assert expected in colors, theme
        assert checkmark in colors, theme

        checkbox.setChecked(False)
        colors = indicator_pixels(dialog, checkbox)
        assert checkmark not in colors, theme

        option = QStyleOptionButton()
        checkbox.initStyleOption(option)
        indicator = checkbox.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            checkbox,
        )
        origin = checkbox.mapTo(dialog, indicator.topLeft())
        idle_pixel = dialog.grab().toImage().pixelColor(
            origin.x(), origin.y() + indicator.height() // 2
        )

        dialog.activateWindow()
        checkbox.setFocus()
        QApplication.processEvents()
        assert checkbox.hasFocus(), theme
        rendered = dialog.grab().toImage()
        focus_color = QColor(ui_module._theme_colors(dialog._appearance).accent_focus)
        focused_pixel = rendered.pixelColor(origin.x(), origin.y() + indicator.height() // 2)
        assert focused_pixel != idle_pixel, theme
        assert all(
            abs(actual - expected) <= 18
            for actual, expected in zip(focused_pixel.getRgb()[:3], focus_color.getRgb()[:3], strict=True)
        ), theme
        dialog.close()


def test_macos_permission_note_keeps_the_settings_dialog_below_a_720px_work_area(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "darwin")
    dialog = SettingsDialog(AppSettings(), accessibility_granted=False)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.accessibility_button is not None
    assert dialog.height() <= 720


def test_light_settings_combo_popups_keep_dark_text_on_light_background(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(theme="light"), accessibility_granted=True)
    qtbot.addWidget(dialog)

    for combo in (dialog.hotkey_mode, dialog.theme):
        if combo is None:
            continue
        palette = combo.view().palette()
        assert palette.color(QPalette.ColorRole.Base) == QColor("#EEF2F8")
        assert palette.color(QPalette.ColorRole.Text) == QColor("#171A24")


def test_frosted_material_theme_is_selectable_and_uses_a_readable_popup_palette(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(theme="liquid_glass"), accessibility_granted=True)
    qtbot.addWidget(dialog)
    legacy_dialog = SettingsDialog(AppSettings(theme="system"), accessibility_granted=True)
    qtbot.addWidget(legacy_dialog)

    assert dialog.theme.currentData() == "liquid_glass"
    assert dialog.theme.currentText() == "磨砂质感（随系统）"
    assert dialog.theme.itemData(0) == "liquid_glass"
    assert legacy_dialog.theme.currentData() == "liquid_glass"
    assert f"font-size: {ui_module._POPUP_ITEM_FONT_SIZE_PT}pt" in dialog.theme.view().styleSheet()
    assert [dialog.theme.itemText(index) for index in range(dialog.theme.count())] == [
        "磨砂质感（随系统）",
        "浅色",
        "深色",
    ]
    assert dialog.theme.findText("跟随系统") == -1
    assert dialog.theme.findText("玻璃半透（随系统）") == -1
    assert dialog.theme.findText("液态玻璃（随系统）") == -1
    assert dialog.theme.findText("柔光半透（随系统）") == -1
    for combo in (dialog.hotkey_mode, dialog.theme):
        if combo is None:
            continue
        palette = combo.view().palette()
        assert palette.color(QPalette.ColorRole.Base) == QColor("#EAF2FA")
        assert palette.color(QPalette.ColorRole.Text) == QColor("#142039")
        assert "background: #EAF2FA" in combo.view().styleSheet()
        assert "background: #2C63D9; color: #FFFFFF" in combo.view().styleSheet()


def test_dark_liquid_combo_popup_uses_the_dark_foreground_for_its_solid_accent(qtbot) -> None:
    combo = QComboBox()
    combo.addItems(["一", "二"])
    qtbot.addWidget(combo)

    ui_module._style_combo_popup(combo, _ThemeAppearance(dark=True, liquid_glass=True))

    assert "background: #6B9DFF; color: #0A192F" in combo.view().styleSheet()


def test_confirmation_reserves_danger_color_for_irreversible_actions(qtbot) -> None:
    parent = QDialog()
    destructive = _DestructiveConfirmationDialog(parent, "清空历史", "确认清空？", "清空")
    acknowledgement = _DestructiveConfirmationDialog(
        parent,
        "快捷键无效",
        "请重试。",
        "知道了",
        cancel_text=None,
        destructive=False,
    )
    qtbot.addWidget(parent)
    qtbot.addWidget(destructive)
    qtbot.addWidget(acknowledgement)

    assert "background: #C13749" in destructive.styleSheet()
    assert "background: #5264E8" in acknowledgement.styleSheet()


def _render_liquid_material(
    *,
    light_strength: float = 0.0,
    native_backdrop: bool = False,
    transparent_canvas: bool = False,
) -> QImage:
    image = QImage(320, 220, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0) if transparent_canvas else QColor("#101721"))
    painter = QPainter(image)
    _paint_liquid_glass_material(
        painter,
        QRectF(20, 20, 280, 180),
        _ThemeAppearance(dark=False, liquid_glass=True, native_backdrop=native_backdrop),
        light_position=QPointF(112, 84),
        light_strength=light_strength,
    )
    painter.end()
    return image


def test_liquid_glass_material_has_environmental_depth_rim_and_subtle_pointer_sheen() -> None:
    resting = _render_liquid_material()
    active = _render_liquid_material(light_strength=1.0)

    top_rim = resting.pixelColor(160, 21)
    just_inside = resting.pixelColor(160, 44)
    upper_left = resting.pixelColor(64, 58)
    lower_right = resting.pixelColor(260, 176)
    resting_light = resting.pixelColor(112, 84)
    active_light = active.pixelColor(112, 84)

    assert sum(top_rim.getRgb()[:3]) > sum(just_inside.getRgb()[:3])
    assert upper_left != lower_right
    assert sum(active_light.getRgb()[:3]) > sum(resting_light.getRgb()[:3]) + 4


def test_liquid_surface_recovers_when_qt_tears_down_its_hover_animation(qtbot) -> None:
    """A deferred theme refresh must not call a stale parent-owned animation."""

    panel = ClipPanel(lambda: AppSettings(theme="liquid_glass"))
    qtbot.addWidget(panel)
    stale_animation = panel.card._hover_animation
    stale_animation.deleteLater()
    QCoreApplication.sendPostedEvents(stale_animation, QEvent.Type.DeferredDelete)

    with pytest.raises(RuntimeError):
        stale_animation.state()

    # Theme reconciliation can occur before the next pointer event. It must
    # safely clear the stale effect, and the next hover must restore it.
    panel.card.set_appearance(_ThemeAppearance(dark=False))
    panel.card.set_appearance(_ThemeAppearance(dark=False, liquid_glass=True))
    panel.card.set_light_active(True)

    assert panel.card._hover_animation is not stale_animation
    assert panel.card._hover_animation.state() == QVariantAnimation.State.Running


def test_native_soft_translucent_material_keeps_a_neutral_frosted_backdrop_visible() -> None:
    fallback = _render_liquid_material(transparent_canvas=True)
    frosted = _render_liquid_material(native_backdrop=True, transparent_canvas=True)

    for point in ((64, 58), (160, 120), (260, 176)):
        fallback_pixel = fallback.pixelColor(*point)
        frosted_pixel = frosted.pixelColor(*point)
        assert 0 < frosted_pixel.alpha() < 100
        assert frosted_pixel.alpha() + 110 < fallback_pixel.alpha()

    # The lower-right field must remain a neutral cool tint rather than turn
    # into a saturated blue panel over the system material.
    lower_right = frosted.pixelColor(260, 176)
    assert lower_right.blue() - lower_right.green() < 28


@pytest.mark.parametrize("theme", ("light", "dark", "system", "liquid_glass"))
def test_every_panel_theme_uses_one_visible_vertical_divider_without_a_detail_card(
    qtbot,
    theme: str,
) -> None:
    panel = ClipPanel(lambda: AppSettings(theme=theme))
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "divider", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    divider = panel.content_divider
    history_right = panel.history_content.mapTo(panel, QPoint(panel.history_content.width(), 0)).x()
    divider_left = divider.mapTo(panel, QPoint(0, 0)).x()
    divider_right = divider.mapTo(panel, QPoint(divider.width(), 0)).x()
    detail_left = panel.detail.mapTo(panel, QPoint(0, 0)).x()

    assert divider.isVisible()
    assert divider.width() == 1
    assert history_right < divider_left < divider_right < detail_left
    assert divider_left - history_right == detail_left - divider_right
    style = panel.styleSheet()
    divider_color = (
        "rgba(224, 238, 255, 46)"
        if panel._appearance.dark
        else "rgba(35, 65, 98, 38)"
    )
    assert "#detail { background: transparent; border: none; }" in style
    assert f"#contentDivider {{ background: {divider_color};" in panel.styleSheet()
    assert (
        "#textPreview, #fileTextPreview { font-size: 13pt; padding: 11px; "
        "background: transparent; border: none; }"
    ) in style

    panel.set_items([])
    assert not divider.isVisible()


@pytest.mark.parametrize(
    ("appearance", "divider_color"),
    (
        pytest.param(
            _ThemeAppearance(dark=False),
            "rgba(35, 65, 98, 38)",
            id="light",
        ),
        pytest.param(
            _ThemeAppearance(dark=True),
            "rgba(224, 238, 255, 46)",
            id="dark",
        ),
        pytest.param(
            _ThemeAppearance(dark=False, liquid_glass=True),
            "rgba(35, 65, 98, 38)",
            id="frosted-light",
        ),
        pytest.param(
            _ThemeAppearance(dark=True, liquid_glass=True),
            "rgba(224, 238, 255, 46)",
            id="frosted-dark",
        ),
    ),
)
def test_settings_sections_share_the_main_panel_divider_material(
    appearance: _ThemeAppearance,
    divider_color: str,
) -> None:
    style = _style_sheet(appearance)
    settings_rule = re.search(r"#settingsSection \{(?P<rule>[^}]*)\}", style)

    assert settings_rule is not None
    assert f"border: 1px solid {divider_color};" in settings_rule.group("rule")
    assert f"#contentDivider {{ background: {divider_color};" in style
    assert f"#searchFiltersDivider, #contentFooterDivider {{\n            background: {divider_color};" in style


@pytest.mark.parametrize(
    ("appearance", "divider_color"),
    (
        pytest.param(
            _ThemeAppearance(dark=False, liquid_glass=True),
            "rgba(35, 65, 98, 38)",
            id="frosted-light",
        ),
        pytest.param(
            _ThemeAppearance(dark=True, liquid_glass=True),
            "rgba(224, 238, 255, 46)",
            id="frosted-dark",
        ),
    ),
)
def test_frosted_settings_controls_share_the_main_panel_divider_material(
    appearance: _ThemeAppearance,
    divider_color: str,
) -> None:
    style = _style_sheet(appearance)

    assert _settings_control_border_token(appearance)[0] == divider_color
    assert "#settingsDialog QPlainTextEdit, #settingsDialog QLineEdit," in style
    assert f"border: 1px solid {divider_color}; border-radius: 10px; padding: 6px 8px;" in style
    assert "#settingsDialog QComboBox::drop-down {" in style
    assert f"border: none; border-left: 1px solid {divider_color}; width: 27px;" in style
    assert "#settingsDialog QPushButton {" in style


@pytest.mark.parametrize("theme", ("light", "dark", "system", "liquid_glass"))
def test_every_panel_theme_keeps_search_and_footer_section_rules_without_a_search_frame(
    qtbot,
    theme: str,
) -> None:
    panel = ClipPanel(lambda: AppSettings(theme=theme))
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "divider", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    section_dividers = (
        panel.search_filters_divider,
        panel.content_footer_divider,
    )
    style = panel.styleSheet()
    assert panel.search_box.frameShape() == QFrame.Shape.NoFrame
    assert "#searchBox { background: transparent; border: none; }" in style
    assert "#searchFiltersDivider, #contentFooterDivider {" in style
    assert not hasattr(panel, "filters_content_divider")
    assert "filtersContentDivider" not in style

    content_left = panel.history_content.mapTo(panel, QPoint()).x()
    content_right = panel.detail.mapTo(panel, QPoint(panel.detail.width(), 0)).x()
    for divider in section_dividers:
        assert divider.isVisible()
        assert divider.frameShape() == QFrame.Shape.NoFrame
        assert divider.height() == 1
        assert divider.mapTo(panel, QPoint()).x() == content_left
        assert divider.mapTo(panel, QPoint(divider.width(), 0)).x() == content_right

    def top(widget: QWidget) -> int:
        return widget.mapTo(panel, QPoint()).y()

    def bottom(widget: QWidget) -> int:
        return widget.mapTo(panel, QPoint(0, widget.height())).y()

    first_filter = panel._filter_buttons[0][0]
    content_top = min(top(panel.history_content), top(panel.detail))
    content_bottom = max(bottom(panel.history_content), bottom(panel.detail))
    assert bottom(panel.search_box) < top(panel.search_filters_divider) < top(first_filter)
    assert bottom(first_filter) < content_top
    assert content_bottom < top(panel.content_footer_divider) < top(panel.version_label)

    reference_pixel = panel.content_divider.grab().toImage().pixelColor(
        0,
        panel.content_divider.height() // 2,
    )
    assert reference_pixel.alpha() > 0
    for divider in (*section_dividers, panel.information_divider):
        image = divider.grab().toImage()
        painted_pixels = [
            image.pixelColor(x, y)
            for y in range(image.height())
            for x in range(image.width())
        ]
        assert reference_pixel in painted_pixels

    panel.set_items([])
    assert all(divider.isVisible() for divider in section_dividers)


@pytest.mark.parametrize(
    ("theme", "divider_is_dark"),
    (("light", True), ("dark", False)),
)
def test_standard_detail_has_no_card_edge_and_its_divider_paints(
    qtbot,
    theme: str,
    divider_is_dark: bool,
) -> None:
    """The standard appearances use a rule, not a second rounded card."""
    panel = ClipPanel(lambda: AppSettings(theme=theme))
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "divider", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    rendered_panel = panel.grab().toImage()
    detail = panel.detail
    top_edge = detail.mapTo(panel, QPoint(detail.width() // 2, 0))
    just_above = top_edge - QPoint(0, 3)
    left_edge = detail.mapTo(panel, QPoint(0, detail.height() // 2))
    just_left = left_edge - QPoint(2, 0)

    def maximum_rgb_delta(first: QColor, second: QColor) -> int:
        return max(
            abs(first.red() - second.red()),
            abs(first.green() - second.green()),
            abs(first.blue() - second.blue()),
        )

    # A former detail-card border would be visible at either of these edges.
    assert maximum_rgb_delta(
        rendered_panel.pixelColor(top_edge),
        rendered_panel.pixelColor(just_above),
    ) <= 1
    assert maximum_rgb_delta(
        rendered_panel.pixelColor(left_edge),
        rendered_panel.pixelColor(just_left),
    ) <= 1

    # Grab the one-pixel child directly: Qt's offscreen parent compositing
    # intentionally folds semitransparent child pixels into its background.
    divider_ink = panel.content_divider.grab().toImage().pixelColor(
        0,
        panel.content_divider.height() // 2,
    )
    assert divider_ink.alpha() > 0
    assert (divider_ink.lightness() < 150) is divider_is_dark


def test_liquid_glass_panel_uses_one_custom_painted_primary_shell(qtbot) -> None:
    current = {"settings": AppSettings(theme="light")}
    panel = ClipPanel(lambda: current["settings"])
    qtbot.addWidget(panel)

    expected_inset = 1 if sys.platform == "win32" else 14
    light_margins = panel.layout().contentsMargins()
    assert light_margins.left() == expected_inset
    assert light_margins.top() == expected_inset

    current["settings"] = AppSettings(theme="liquid_glass")
    panel.apply_theme()

    assert panel.card.__class__.__name__ == "_LiquidGlassSurface"
    liquid_margins = panel.layout().contentsMargins()
    assert liquid_margins.left() == expected_inset
    assert liquid_margins.top() == expected_inset
    assert "#card { background: transparent; border: none;" in panel.styleSheet()
    assert "#detail { background: transparent; border: none;" in panel.styleSheet()
    if panel.card.graphicsEffect() is not None:
        assert not panel.card.graphicsEffect().isEnabled()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows confirmation dialogs must retain opaque non-layered backing",
)
@pytest.mark.parametrize(
    "appearance",
    (
        _ThemeAppearance(dark=False),
        _ThemeAppearance(dark=True),
        _ThemeAppearance(dark=False, liquid_glass=True),
        _ThemeAppearance(dark=True, liquid_glass=True),
    ),
    ids=("light", "dark", "glass-light", "glass-dark"),
)
def test_confirmation_has_no_opaque_rectangular_root_ring(qtbot, appearance) -> None:
    parent = QDialog()
    prompt = _DestructiveConfirmationDialog(
        parent,
        "清空历史",
        "确认清空？",
        "确定",
        appearance=appearance,
    )
    qtbot.addWidget(parent)
    qtbot.addWidget(prompt)
    prompt.show()
    qtbot.waitExposed(prompt)

    card = prompt.findChild(QFrame, "confirmationCard")
    assert card is not None
    assert prompt.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not prompt.autoFillBackground()
    assert "QDialog { background: transparent;" in prompt.styleSheet()

    rendered = prompt.grab().toImage()
    # The outer corner belongs to the transparent root, not an opaque
    # rectangular second surface. The retained soft card shadow may occupy
    # nearby margin pixels, but never as a hard opaque strip.
    assert rendered.pixelColor(1, 1).alpha() == 0
    for point in (
        card.mapTo(prompt, QPoint(-2, card.height() // 2)),
        card.mapTo(prompt, QPoint(card.width() // 2, card.height() + 2)),
    ):
        assert rendered.pixelColor(point).alpha() < 96
    center = card.mapTo(prompt, card.rect().center())
    assert rendered.pixelColor(center).alpha() >= 200


def test_dark_confirmation_uses_bright_title_and_cancel_text(qtbot) -> None:
    parent = QDialog()
    prompt = _DestructiveConfirmationDialog(
        parent,
        "清空历史",
        "确认清空？",
        "确定",
        appearance=_ThemeAppearance(dark=True),
    )
    qtbot.addWidget(parent)
    qtbot.addWidget(prompt)
    prompt.show()
    qtbot.waitExposed(prompt)

    if sys.platform == "win32":
        assert not prompt.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    else:
        assert prompt.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert "#confirmationTitle { color: #F2F4F8;" in prompt.styleSheet()
    assert "color: #F2F4F8;" in prompt.styleSheet()
    rendered = prompt.grab().toImage()
    root_pixel = rendered.pixelColor(2, prompt.height() // 2)
    if sys.platform == "win32":
        assert root_pixel.alpha() == 255
        assert root_pixel.lightness() < 40
    else:
        assert root_pixel.alpha() < 96
    title = prompt.findChild(QLabel, "confirmationTitle")
    assert title is not None
    for widget in (title, prompt.cancel_button):
        origin = widget.mapTo(prompt, QPoint(0, 0))
        rect = QRect(origin, widget.size())
        assert any(
            rendered.pixelColor(x, y).lightness() > 180
            for y in range(rect.top(), rect.bottom() + 1)
            for x in range(rect.left(), rect.right() + 1)
        )


def test_confirmation_falls_back_to_complete_liquid_material_without_parent_native_backdrop(qtbot) -> None:
    parent = QDialog()
    prompt = _DestructiveConfirmationDialog(
        parent,
        "清空历史",
        "确认清空？",
        "确定",
        appearance=_ThemeAppearance(dark=False, liquid_glass=True, native_backdrop=True),
    )
    qtbot.addWidget(parent)
    qtbot.addWidget(prompt)

    assert prompt._appearance.liquid_glass
    assert not prompt._appearance.native_backdrop


def test_liquid_list_selection_and_hover_share_one_blue_material_language(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="liquid_glass"))
    qtbot.addWidget(panel)
    panel.set_items(
        [
            clip("selected", "selected", 3),
            clip("hovered", "hovered", 2),
            clip("plain", "plain", 1),
        ]
    )
    panel.show_panel()
    qtbot.waitExposed(panel)
    hovered = panel.model.index(1)
    qtbot.mouseMove(panel.list.viewport(), panel.list.visualRect(hovered).center())
    qtbot.wait(20)

    image = panel.list.viewport().grab().toImage()
    selected_rect = panel.list.visualRect(panel.model.index(0))
    hover_rect = panel.list.visualRect(hovered)
    assert selected_rect.width() == image.width()
    assert hover_rect.width() == image.width()
    selected_point = selected_rect.center()
    selected_point.setX(selected_rect.right() - 10)
    hover_point = hover_rect.center()
    hover_point.setX(hover_rect.right() - 10)

    selected_fill = image.pixelColor(selected_point)
    hover_fill = image.pixelColor(hover_point)
    assert selected_fill.alpha() == 154
    assert hover_fill.alpha() == 42
    assert selected_fill.blue() - selected_fill.red() > 140
    assert hover_fill.blue() - hover_fill.red() > 100
    assert selected_fill.alpha() > hover_fill.alpha()


def test_liquid_history_scrollbar_is_narrow_and_has_no_opaque_track(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="liquid_glass"))
    qtbot.addWidget(panel)
    panel.set_items([clip(str(index), f"item {index}", index) for index in range(42)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    scrollbar = panel.list.verticalScrollBar()
    rendered = scrollbar.grab().toImage()

    assert scrollbar.isVisible()
    assert scrollbar.width() == 12
    assert rendered.pixelColor(scrollbar.width() // 2, scrollbar.height() // 2).alpha() == 0
    style = panel.styleSheet()
    assert "#historyList QScrollBar:vertical" in style
    assert "#historyList QScrollBar::groove:vertical" in style
    assert "#historyList QScrollBar::add-page:vertical" in style
    assert "#historyList QScrollBar::sub-page:vertical" in style


def test_standard_history_scrollbars_are_narrow_and_have_no_opaque_track(qtbot) -> None:
    for theme, handle in (
        ("light", "rgba(52, 65, 86, 132)"),
        ("dark", "rgba(224, 232, 244, 132)"),
    ):
        panel = ClipPanel(lambda theme=theme: AppSettings(theme=theme))
        qtbot.addWidget(panel)
        panel.set_items([clip(str(index), f"item {index}", index) for index in range(42)])
        panel.show_panel()
        qtbot.waitExposed(panel)

        scrollbar = panel.list.verticalScrollBar()
        rendered = scrollbar.grab().toImage()

        assert scrollbar.isVisible(), theme
        assert scrollbar.width() == 12, theme
        assert rendered.pixelColor(scrollbar.width() // 2, scrollbar.height() // 2).alpha() == 0, theme
        assert handle in panel.styleSheet()


def test_long_text_preview_scrollbar_is_narrow_and_transparent_in_every_theme(qtbot) -> None:
    text = "\n".join(f"long preview line {index}" for index in range(160))
    for theme in ("light", "dark", "liquid_glass"):
        panel = ClipPanel(lambda theme=theme: AppSettings(theme=theme))
        qtbot.addWidget(panel)
        panel.set_items([clip("long", text, 1)])
        panel.show_panel()
        qtbot.waitExposed(panel)

        scrollbar = panel.text_preview.verticalScrollBar()
        rendered = scrollbar.grab().toImage()

        assert panel.text_preview.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert panel.file_text_preview.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert scrollbar.isVisible(), theme
        assert scrollbar.width() == 12, theme
        assert rendered.pixelColor(scrollbar.width() // 2, scrollbar.height() // 2).alpha() == 0, theme
        assert "#textPreview QScrollBar:vertical" in panel.styleSheet()


def test_windows_liquid_glass_keeps_qt_non_layered_before_and_after_native_backdrop(
    qtbot,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    panel = ClipPanel(lambda: AppSettings(theme="liquid_glass"))
    qtbot.addWidget(panel)

    assert panel.card.graphicsEffect() is None
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert panel.autoFillBackground()
    assert not panel.native_backdrop_active
    assert "#panelWindow { background: transparent; }" not in panel.styleSheet()

    panel.set_native_backdrop_active(True)

    assert panel.native_backdrop_active
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert panel.autoFillBackground()
    assert "#panelWindow { background: transparent; }" in panel.styleSheet()
    assert "#card { background: transparent; border: none;" in panel.styleSheet()
    assert "rgba(232, 244, 255, 54)" in panel.styleSheet()


def test_windows_dialogs_keep_non_layered_backing_without_qt_drop_shadows(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    dialog = SettingsDialog(AppSettings(theme="liquid_glass"), accessibility_granted=True)
    prompt = _DestructiveConfirmationDialog(dialog, "清空历史", "确认清空？", "确定", dark=False)
    qtbot.addWidget(dialog)
    qtbot.addWidget(prompt)

    assert dialog.autoFillBackground()
    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert prompt.autoFillBackground()
    assert not prompt.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    card = prompt.findChild(QFrame, "confirmationCard")
    assert card is not None and card.graphicsEffect() is None


def test_open_data_directory_closes_settings_before_emitting(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "darwin")
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    revealed: list[bool] = []
    dialog.reveal_requested.connect(lambda: revealed.append(dialog.isVisible()))
    dialog.show()

    qtbot.mouseClick(dialog.reveal_button, Qt.MouseButton.LeftButton)

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert revealed == [False]
    assert dialog.reveal_button.text() == "在 Finder 中打开"


def test_panel_footer_places_version_after_hide_hint(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    assert panel.version_label.text() == f"↑↓ 选择  |  ↵ 发送  |  Esc 隐藏  |  v{__version__}"


def test_styles_use_point_fonts_so_windows_styles_never_receive_negative_point_size(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    assert not re.search(r"font-size:\s*\d+(?:\.\d+)?px", _style_sheet(False))
    for widget in (panel, panel.search, panel.list, panel.version_label):
        assert widget.font().pointSizeF() > 0
        assert widget.font().pixelSize() == -1


def test_main_panel_uses_readable_raycast_like_font_hierarchy(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    information_title = panel.information_title
    assert panel.search.font().pointSizeF() == 16
    assert all(button.font().pointSizeF() == 13 for button, _kind in panel._filter_buttons)
    assert panel.list.font().pointSizeF() == 13
    assert panel.text_preview.font().pointSizeF() == 13
    assert panel.file_text_preview.font().pointSizeF() == 13
    assert information_title.font().pointSizeF() == 13
    assert panel.info_type_label.font().pointSizeF() == 13
    assert panel.info_type_value.font().pointSizeF() == 13
    assert panel.information_divider.frameShape() == QFrame.Shape.NoFrame
    assert panel.info_type_label.font().weight() == panel.info_type_value.font().weight()
    assert panel.info_detail_label.font().weight() == panel.info_detail_value.font().weight()
    assert information_title.geometry().left() == panel.info_type_label.geometry().left()
    assert information_title.geometry().left() == panel.information_divider.geometry().left()
    style = _style_sheet(False)
    assert "#informationLabel, #informationValue" in style
    assert "color: #62697A; font-size: 13pt; font-weight: 500;" in style
    assert (
        "#informationDivider {\n            background: rgba(35, 65, 98, 38); "
        "margin: 3px 8px 1px; min-height: 1px; max-height: 1px;\n        }" in style
    )
    assert "#searchFiltersDivider, #contentFooterDivider {" in style
    assert "background: rgba(35, 65, 98, 38); border: none; min-height: 1px; max-height: 1px;" in style
    assert "#informationTitle { font-size: 13pt; font-weight: 650; padding: 6px 0 0 0; }" in style
    assert panel.search_icon.size() == QSize(30, 30)
    text_height = panel.search.fontMetrics().tightBoundingRect("Ag").height()
    assert panel.search_box.height() == text_height * 2
    assert panel.search_box.frameShape() == QFrame.Shape.NoFrame
    search_margins = panel.search.parentWidget().layout().contentsMargins()
    assert search_margins.top() == 0
    assert search_margins.bottom() == 0
    search_style = _style_sheet(False)
    assert "#searchBox { background: transparent; border: none; }" in search_style
    assert "#search {" in search_style
    assert "color: #171A24; font-size: 16pt; padding: 0 2px;" in search_style
    assert "selection-background-color: #5264E8; selection-color: #FFFFFF;" in search_style


def test_windows_panel_uses_opaque_backing_store_without_drop_shadow(
    qtbot, monkeypatch
) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")

    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    assert panel.card.graphicsEffect() is None
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert panel.autoFillBackground()


def test_panel_defaults_to_first_item_each_time_it_is_shown(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 3), clip("second", "second", 2), clip("third", "third", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.list.setCurrentIndex(panel.model.index(2))
    panel.hide()

    panel.show_panel()

    assert panel.list.currentIndex().row() == 0
    assert {index.row() for index in panel.list.selectionModel().selectedRows()} == {0}


class _FakeMouseEvent:
    def __init__(
        self,
        local: QPoint,
        global_position: QPoint,
        button: Qt.MouseButton,
        buttons: Qt.MouseButton,
    ) -> None:
        self._local = QPointF(local)
        self._global = QPointF(global_position)
        self._button = button
        self._buttons = buttons
        self.accepted = False

    def position(self) -> QPointF:
        return self._local

    def globalPosition(self) -> QPointF:
        return self._global

    def button(self) -> Qt.MouseButton:
        return self._button

    def buttons(self) -> Qt.MouseButton:
        return self._buttons

    def accept(self) -> None:
        self.accepted = True


def test_panel_drag_from_blank_area_emits_and_restores_position(qtbot) -> None:
    settings = AppSettings()
    panel = ClipPanel(lambda: settings)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)
    assert panel._can_start_drag(QPoint(5, 5))
    assert not panel._can_start_drag(panel.search.mapTo(panel, QPoint(5, 5)))
    moved: list[tuple[int, int]] = []
    panel.position_changed.connect(lambda x, y: moved.append((x, y)))
    local = QPoint(5, 5)
    start = panel.mapToGlobal(local)
    delta = QPoint(20, 20)

    panel.mousePressEvent(
        _FakeMouseEvent(local, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    )
    panel.mouseMoveEvent(
        _FakeMouseEvent(
            local + delta,
            start + delta,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    panel.mouseReleaseEvent(
        _FakeMouseEvent(
            local + delta,
            start + delta,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )

    assert moved == [(panel.x(), panel.y())]
    settings.panel_x, settings.panel_y = moved[0]
    restored = ClipPanel(lambda: settings)
    qtbot.addWidget(restored)
    restored.show_panel()
    qtbot.waitExposed(restored)
    assert restored.pos() == panel.pos()


def test_panel_saved_position_is_clamped_to_available_screen(qtbot) -> None:
    settings = AppSettings(panel_x=100_000, panel_y=100_000)
    panel = ClipPanel(lambda: settings)
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    assert panel.screen().availableGeometry().contains(panel.frameGeometry())


def test_panel_restores_multi_selection_by_id_before_memory_expires(qtbot) -> None:
    now = [100.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    original = [clip("first", "first", 3), clip("second", "second", 2), clip("third", "third", 1)]
    panel.set_items(original)
    panel.show_panel()
    qtbot.waitExposed(panel)
    selection = panel.list.selectionModel()
    selection.select(
        panel.model.index(1),
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.select(
        panel.model.index(2),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection.setCurrentIndex(panel.model.index(2), QItemSelectionModel.SelectionFlag.NoUpdate)
    panel.hide()

    panel.set_items([clip("new", "new", 4), *original])
    now[0] += 2.5
    panel.show_panel()

    assert {item.id for item in panel._selected_items()} == {"second", "third"}
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "third"

    panel.hide()
    now[0] += 3.1
    panel.show_panel()
    assert panel.list.currentIndex().row() == 0
    assert {item.id for item in panel._selected_items()} == {"new"}


def test_panel_restores_filter_search_and_selection_before_state_memory_expires(qtbot) -> None:
    now = [100.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    file_item = ClipItem(
        "file",
        ClipKind.FILES,
        "alpha file",
        4,
        4,
        files=("/tmp/alpha-file.txt",),
    )
    panel.set_items([file_item, clip("alpha", "alpha", 3), clip("alphabet", "alphabet", 2)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.mouseClick(panel._filter_buttons[3][0], Qt.MouseButton.LeftButton)
    panel.search.setText("alp")
    panel.hide()

    now[0] += 2.5
    panel.show_panel()

    assert panel._kind is ClipKind.FILES
    assert panel._filter_buttons[3][0].isChecked()
    assert panel.search.text() == "alp"
    assert panel.model.rowCount() == 1
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "file"


def test_state_memory_preserves_a_search_with_no_results(qtbot) -> None:
    now = [100.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    panel.set_items([clip("alpha", "alpha", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.search.setText("no matches")
    assert panel.model.rowCount() == 0
    panel.hide()

    now[0] += 1
    panel.show_panel()

    assert panel.search.text() == "no matches"
    assert panel.model.rowCount() == 0
    assert not panel.list.currentIndex().isValid()


def test_expired_state_memory_clears_search_and_selects_global_first(qtbot) -> None:
    now = [100.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    panel.set_items([clip("alpha", "alpha", 3), clip("other", "other", 2)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.mouseClick(panel._filter_buttons[1][0], Qt.MouseButton.LeftButton)
    panel.search.setText("other")
    panel.hide()

    now[0] += 3.1
    panel.show_panel()

    assert panel._kind is None
    assert panel._filter_buttons[0][0].isChecked()
    assert panel.search.text() == ""
    assert panel.model.rowCount() == 2
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "alpha"


def test_disabled_state_memory_never_restores_search(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("alpha", "alpha", 2), clip("other", "other", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.search.setText("other")
    panel.hide()

    panel.show_panel()

    assert panel.search.text() == ""
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "alpha"


def test_missing_remembered_items_fall_back_to_first_result(qtbot) -> None:
    now = [50.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("removed", "removed", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.list.setCurrentIndex(panel.model.index(1))
    panel.hide()
    panel.set_items([clip("replacement", "replacement", 3)])
    now[0] += 1

    panel.show_panel()

    assert panel.model.item_at(panel.list.currentIndex().row()).id == "replacement"
    assert panel._remembered_item_ids == ()


def test_hidden_selection_memory_is_actively_cleared_when_timer_expires(qtbot) -> None:
    settings = AppSettings(remember_selection=True, selection_memory_seconds=1)
    panel = ClipPanel(lambda: settings)
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("second", "second", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.mouseClick(panel._filter_buttons[1][0], Qt.MouseButton.LeftButton)
    panel.search.setText("second")
    panel.list.setCurrentIndex(panel.model.index(0))

    panel.hide()
    assert panel._remembered_search_text == "second"
    assert panel._remembered_kind is ClipKind.TEXT
    assert panel._remembered_item_ids == ("second",)
    qtbot.waitUntil(lambda: panel._remembered_item_ids == (), timeout=1_500)
    assert panel._remembered_search_text == ""
    assert panel._remembered_kind is None
    assert panel._kind is None
    assert panel._filter_buttons[0][0].isChecked()
    assert panel.search.text() == ""
    assert panel.list.currentIndex().row() == 0
    panel.show_panel()

    assert panel.list.currentIndex().row() == 0
    assert {item.id for item in panel._selected_items()} == {"first"}


def test_default_three_second_state_memory_with_real_qt_interactions(qtbot) -> None:
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings)
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 3), clip("second", "second", 2), clip("other", "other", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.mouseClick(panel._filter_buttons[1][0], Qt.MouseButton.LeftButton)
    panel.search.setText("second")
    panel.hide_panel()

    qtbot.wait(500)
    panel.show_panel()
    assert panel._kind is ClipKind.TEXT
    assert panel._filter_buttons[1][0].isChecked()
    assert panel.search.text() == "second"
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "second"

    panel.hide_panel()
    qtbot.waitUntil(lambda: panel._selection_hidden_at is None, timeout=3_500)
    assert panel._kind is None
    assert panel._filter_buttons[0][0].isChecked()
    assert panel.search.text() == ""
    assert panel.model.item_at(panel.list.currentIndex().row()).id == "first"


def test_focus_loss_hide_starts_memory_once_and_expires_to_first_item(qtbot) -> None:
    now = [100.0]
    settings = AppSettings(remember_selection=True, selection_memory_seconds=3)
    panel = ClipPanel(lambda: settings, selection_clock=lambda: now[0])
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("second", "second", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.list.setCurrentIndex(panel.model.index(1))

    panel._hide_if_unfocused()

    assert not panel.isVisible()
    assert panel._selection_hidden_at == 100.0
    assert panel._remembered_item_ids == ("second",)
    now[0] += 3.1
    panel.show_panel()

    assert panel.list.currentIndex().row() == 0
    assert {item.id for item in panel._selected_items()} == {"first"}


def test_selection_expiry_does_not_reset_a_panel_reopened_within_the_limit(qtbot) -> None:
    settings = AppSettings(remember_selection=True, selection_memory_seconds=1)
    panel = ClipPanel(lambda: settings)
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("second", "second", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    panel.list.setCurrentIndex(panel.model.index(1))
    panel.hide()

    qtbot.wait(400)
    panel.show_panel()
    assert panel.list.currentIndex().row() == 1
    qtbot.waitUntil(lambda: panel._remembered_item_ids == (), timeout=1_000)

    assert panel.isVisible()
    assert panel.list.currentIndex().row() == 1


def test_macos_accessibility_prompt_only_when_not_granted(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "darwin")
    missing = SettingsDialog(AppSettings(), accessibility_granted=False)
    granted = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(missing)
    qtbot.addWidget(granted)

    assert missing.accessibility_button is not None
    assert missing.findChild(QFrame, "platformNote") is not None
    assert granted.accessibility_button is None
    assert granted.findChild(QFrame, "platformNote") is None


def test_windows_settings_has_platform_note_without_accessibility_button(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)

    assert dialog.accessibility_button is None
    assert dialog.findChild(QFrame, "platformNote") is not None


def test_windows_meta_hotkey_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    displayed = _hotkey_display("combo:meta+v")
    sequence = QKeySequence(displayed)
    assert displayed == "Meta+V"
    assert not sequence.isEmpty()
    assert _parse_hotkey(sequence.toString(QKeySequence.SequenceFormat.PortableText)) == "combo:meta+v"


def test_file_row_paints_with_qfileinfo(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "文件.txt"
    path.write_text("hello", encoding="utf-8")
    file_item = ClipItem("file", ClipKind.FILES, "file", 1, 1, files=(str(path),))
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([file_item])
    panel.show_panel()
    qtbot.waitExposed(panel)
    pixmap = panel.grab()
    assert not pixmap.isNull()


def test_copied_image_file_uses_image_thumbnail(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "copied-image.png"
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#e23b4f"))
    assert image.save(str(path), "PNG")

    delegate = ClipDelegate()
    thumbnail = delegate._file_image_thumbnail((str(path),), image.size())

    assert thumbnail.isNull()
    qtbot.waitUntil(
        lambda: not delegate._file_image_thumbnail((str(path),), image.size()).isNull(),
        timeout=1_000,
    )
    thumbnail = delegate._file_image_thumbnail((str(path),), image.size())
    assert thumbnail.toImage().pixelColor(0, 0) == QColor("#e23b4f")
    assert delegate._image_loader.cache_count == 0
    assert delegate.thumbnail_cache_bytes <= ui_module._THUMBNAIL_CACHE_BYTES
    assert delegate._file_image_thumbnail((str(path), str(path)), image.size()).isNull()


def test_byte_lru_cache_enforces_cost_entry_limit_and_recency() -> None:
    cache = _ByteLruCache[str, bytes](max_bytes=10, max_entries=2)
    assert cache.put("first", b"1", 4)
    assert cache.put("second", b"2", 4)
    assert cache.get("first") == b"1"
    assert cache.put("third", b"3", 4)

    assert cache.keys == ("first", "third")
    assert cache.total_bytes == 8
    assert not cache.put("oversized", b"x", 11)
    assert cache.keys == ("first", "third")


def test_scaled_image_cache_is_byte_bounded_lru_and_revision_aware(
    tmp_path: Path, monkeypatch
) -> None:
    loader = _ScaledImageLoader(max_cache_bytes=80_000, max_cache_entries=2)
    paths = [tmp_path / f"image-{index}.png" for index in range(4)]
    for index, path in enumerate(paths):
        path.write_bytes(bytes(index + 1))

    image = QImage(100, 100, QImage.Format.Format_RGB32)
    keys = [loader.key(str(path), QSize(100, 100), True) for path in paths]
    loader._complete(keys[0], image)
    loader._complete(keys[1], image)
    assert loader.request(str(paths[0]), QSize(100, 100), keep_aspect=True) is not None
    loader._complete(keys[2], image)

    assert loader.cache_bytes <= 80_000
    assert loader.cache_count == 2
    assert keys[0] in loader.cache_keys
    assert keys[1] not in loader.cache_keys
    assert keys[2] in loader.cache_keys

    oversized = QImage(150, 150, QImage.Format.Format_RGB32)
    loader._complete(keys[3], oversized)
    assert loader.cache_bytes <= 80_000
    assert keys[3] not in loader.cache_keys

    monkeypatch.setattr(ui_module, "_FILE_REVISION_TTL_SECONDS", 0.0)
    before = loader.key(str(paths[0]), QSize(100, 100), True)
    paths[0].write_bytes(b"changed size")
    after = loader.key(str(paths[0]), QSize(100, 100), True)
    assert before != after


def test_scaled_image_loader_discards_qt_deleted_task_on_invalidate(tmp_path: Path, monkeypatch) -> None:
    loader = _ScaledImageLoader(max_cache_bytes=80_000, max_cache_entries=2)
    path = tmp_path / "deleted-task.png"
    path.write_bytes(b"placeholder")
    key = (str(path), 1, 1, 100, 100, True)
    loader._tasks[key] = object()

    class DeletedTaskPool:
        def tryTake(self, task) -> bool:
            del task
            raise RuntimeError("libshiboken: Internal C++ object already deleted.")

    monkeypatch.setattr(ui_module.QThreadPool, "globalInstance", DeletedTaskPool)

    loader.invalidate_paths({str(path)})

    assert key not in loader._tasks
    assert key in loader._discarded_tasks


def test_preview_size_is_bucketed_to_avoid_resize_cache_churn() -> None:
    assert _bucketed_size(QSize(301, 449)) == QSize(320, 512)
    assert _bucketed_size(QSize(319, 500)) == QSize(320, 512)


def test_history_removal_invalidates_thumbnail_cache(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "history-image.png"
    image = QImage(20, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#6677ff"))
    assert image.save(str(path), "PNG")
    item = ClipItem("image", ClipKind.FILES, "image", 1, 1, files=(str(path),))
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([item])
    delegate = panel.list.itemDelegate()
    assert isinstance(delegate, ClipDelegate)
    assert delegate._file_image_thumbnail(item.files, QSize(72, 72)).isNull()
    qtbot.waitUntil(lambda: delegate.thumbnail_cache_count == 1, timeout=1_000)

    panel.set_items([])

    assert delegate.thumbnail_cache_count == 0
    assert panel.image_preview._image_loader.cache_count == 0


def test_failed_thumbnail_decode_is_negatively_cached(qtbot, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")
    attempts: list[str] = []

    def failed_decode(path: str, bounds: QSize, keep_aspect: bool) -> QImage:
        del bounds, keep_aspect
        attempts.append(path)
        return QImage()

    monkeypatch.setattr(ui_module, "_read_scaled_image", failed_decode)
    delegate = ClipDelegate()
    assert delegate._file_image_thumbnail((str(path),), QSize(72, 72)).isNull()
    qtbot.waitUntil(lambda: len(delegate._failed_thumbnails) == 1, timeout=1_000)

    for _index in range(5):
        assert delegate._file_image_thumbnail((str(path),), QSize(72, 72)).isNull()
    qtbot.wait(20)

    assert attempts == [str(path)]


def test_outdated_detail_tasks_do_not_fill_cache(qtbot, tmp_path: Path, monkeypatch) -> None:
    paths = [tmp_path / "old.jpg", tmp_path / "current.jpg"]
    for path in paths:
        path.write_bytes(b"placeholder")

    def slow_decode(path: str, bounds: QSize, keep_aspect: bool) -> QImage:
        del path, keep_aspect
        time.sleep(0.05)
        result = QImage(bounds, QImage.Format.Format_RGB32)
        result.fill(QColor("#536cff"))
        return result

    monkeypatch.setattr(ui_module, "_read_scaled_image", slow_decode)
    preview = ImagePreview()
    preview.resize(360, 320)
    qtbot.addWidget(preview)
    preview.show()
    preview.set_path(str(paths[0]))
    preview.set_path(str(paths[1]))

    qtbot.waitUntil(
        lambda: preview.pixmap() is not None and not preview.pixmap().isNull(),
        timeout=1_000,
    )
    assert preview._image_loader.cache_count <= 1
    assert all(key[0] == str(paths[1]) for key in preview._image_loader.cache_keys)


def test_copied_image_file_uses_detail_image_preview(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "detail.jpg"
    image = QImage(16, 9, QImage.Format.Format_RGB32)
    image.fill(QColor("#3986e8"))
    assert image.save(str(path), "JPEG")
    item = ClipItem("jpg", ClipKind.FILES, "jpg", 1, 1, files=(str(path),))
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    panel.set_items([item])

    assert panel.preview_stack.currentWidget() is panel.image_preview
    assert panel.image_preview.text() == "正在加载预览…"
    qtbot.waitUntil(
        lambda: panel.image_preview.pixmap() is not None and not panel.image_preview.pixmap().isNull(),
        timeout=1_000,
    )
    assert not panel.image_preview.pixmap().isNull()
    assert panel.info_type_value.text() == "文件"
    assert panel.info_detail_label.text() == "路径"
    assert panel.info_detail_value.text() == str(path)
    assert panel.list.itemDelegate().sizeHint(QStyleOptionViewItem(), panel.model.index(0)).height() == 44


def test_detail_image_preview_does_not_upscale_small_images(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "small-preview.png"
    image = QImage(40, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("#3986e8"))
    assert image.save(str(path), "PNG")
    preview = ImagePreview()
    preview.resize(360, 320)
    qtbot.addWidget(preview)
    preview.show()

    preview.set_path(str(path))

    qtbot.waitUntil(
        lambda: preview.pixmap() is not None and not preview.pixmap().isNull(),
        timeout=1_000,
    )
    assert preview.pixmap().size() == QSize(40, 24)


def test_detail_image_preview_treats_image_pixels_as_physical_on_high_dpi(qtbot, tmp_path: Path) -> None:
    class RetinaPreview(ImagePreview):
        def devicePixelRatioF(self) -> float:  # noqa: N802 - Qt API override
            return 2.0

    path = tmp_path / "retina-preview.png"
    image = QImage(162, 126, QImage.Format.Format_RGB32)
    image.fill(QColor("#3986e8"))
    assert image.save(str(path), "PNG")
    preview = RetinaPreview()
    preview.resize(360, 320)
    qtbot.addWidget(preview)
    preview.show()

    preview.set_path(str(path))

    qtbot.waitUntil(
        lambda: preview.pixmap() is not None and not preview.pixmap().isNull(),
        timeout=1_000,
    )
    pixmap = preview.pixmap()
    assert pixmap.size() == QSize(81, 63)
    assert pixmap.deviceIndependentSize().width() == 81
    assert pixmap.deviceIndependentSize().height() == 63


def test_detail_image_preview_scales_large_images_down_to_fit(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "large-preview.png"
    image = QImage(800, 400, QImage.Format.Format_RGB32)
    image.fill(QColor("#3986e8"))
    assert image.save(str(path), "PNG")
    preview = ImagePreview()
    preview.resize(360, 320)
    qtbot.addWidget(preview)
    preview.show()

    preview.set_path(str(path))

    qtbot.waitUntil(
        lambda: preview.pixmap() is not None and not preview.pixmap().isNull(),
        timeout=1_000,
    )
    assert preview.pixmap().size() == QSize(340, 170)


def test_large_image_decode_does_not_block_selection(qtbot, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.jpg"
    path.write_bytes(b"image placeholder")

    def slow_decode(_path: str, bounds, _keep_aspect: bool) -> QImage:
        time.sleep(0.2)
        image = QImage(bounds, QImage.Format.Format_RGB32)
        image.fill(QColor("#3d8bea"))
        return image

    monkeypatch.setattr(ui_module, "_read_scaled_image", slow_decode)
    delegate = ClipDelegate()
    thumbnail_started = time.perf_counter()
    thumbnail = delegate._file_image_thumbnail((str(path),), QSize(72, 72))
    thumbnail_elapsed = time.perf_counter() - thumbnail_started
    assert thumbnail.isNull()
    assert thumbnail_elapsed < 0.02

    readme = tmp_path / "README.md"
    readme.write_text("preview", encoding="utf-8")
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items(
        [
            ClipItem("text-file", ClipKind.FILES, "text-file", 1, 2, files=(str(readme),)),
            ClipItem("large-image", ClipKind.FILES, "large-image", 1, 1, files=(str(path),)),
        ]
    )

    started = time.perf_counter()
    panel.list.setCurrentIndex(panel.model.index(1))
    elapsed = time.perf_counter() - started

    assert panel.list.currentIndex().row() == 1
    assert panel.preview_stack.currentWidget() is panel.image_preview
    assert panel.image_preview.text() == "正在加载预览…"
    assert elapsed < 0.02
    qtbot.waitUntil(
        lambda: panel.image_preview.pixmap() is not None and not panel.image_preview.pixmap().isNull(),
        timeout=1_000,
    )
    qtbot.waitUntil(
        lambda: not delegate._file_image_thumbnail((str(path),), QSize(72, 72)).isNull(),
        timeout=1_000,
    )


def test_list_item_content_has_equal_top_and_bottom_padding() -> None:
    row = QRect(4, 1, 500, 42)
    content = ClipDelegate._thumbnail_rect(row)

    assert content.size() == QSize(30, 30)
    assert content.top() - row.top() == row.bottom() - content.bottom()


def test_file_icon_is_centered_inside_thumbnail() -> None:
    thumbnail = QRect(12, 8, 30, 30)
    icon = ClipDelegate._centered_file_icon_rect(thumbnail)

    assert icon.left() - thumbnail.left() == thumbnail.right() - icon.right()
    assert icon.top() - thumbnail.top() == thumbnail.bottom() - icon.bottom()
    assert thumbnail.contains(icon)


def test_filter_tabs_cycle_forward_and_backward(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("text", "text", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    for expected in (ClipKind.TEXT, ClipKind.IMAGE, ClipKind.FILES, None, ClipKind.TEXT):
        qtbot.keyPress(panel.search, Qt.Key.Key_Tab)
        assert panel._kind is expected

    qtbot.keyPress(panel.search, Qt.Key.Key_Backtab)
    assert panel._kind is None
    qtbot.keyPress(panel.search, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    assert panel._kind is ClipKind.FILES


def test_image_filter_tab_is_named_screenshot(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    assert panel._filter_buttons[2][0].text() == "截图"


def test_text_file_uses_bounded_read_only_preview(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_text("# ClipSoon\n\n" + "预览内容\n" * 10_000, encoding="utf-8")
    item = ClipItem("markdown", ClipKind.FILES, "markdown", 1, 1, files=(str(path),))
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    panel.set_items([item])

    preview = panel.file_text_preview.toPlainText()
    assert panel.preview_stack.currentWidget() is panel.file_text_preview
    assert preview.startswith("# ClipSoon\n\n预览内容")
    assert preview.endswith("\n...")
    assert len(preview) <= 224
    assert panel.file_text_preview.isReadOnly()
    assert panel.file_text_preview.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel.info_type_value.text() == "文件"
    assert panel.info_detail_value.text() == str(path)

    class WheelEvent:
        accepted = False

        def accept(self) -> None:
            self.accepted = True

    wheel = WheelEvent()
    panel.file_text_preview.wheelEvent(wheel)
    assert wheel.accepted


@pytest.mark.parametrize(
    ("platform_name", "modifier"),
    (
        ("darwin", Qt.KeyboardModifier.MetaModifier),
        ("win32", Qt.KeyboardModifier.ControlModifier),
    ),
)
def test_selected_text_preview_copies_with_platform_shortcut_while_search_keeps_focus(
    qtbot,
    monkeypatch,
    platform_name: str,
    modifier: Qt.KeyboardModifier,
) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("copy", "预览内容可以复制", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)
    cursor = panel.text_preview.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(4, cursor.MoveMode.KeepAnchor)
    panel.text_preview.setTextCursor(cursor)
    monkeypatch.setattr(ui_module.sys, "platform", platform_name)
    QApplication.clipboard().clear()

    handled = panel.eventFilter(
        panel.search,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, modifier),
    )

    assert handled
    assert QApplication.clipboard().text() == "预览内容"
    assert panel.search.hasFocus()


def test_selected_text_file_preview_copies_with_windows_shortcut_while_search_keeps_focus(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "preview.txt"
    path.write_text("文件预览可以复制", encoding="utf-8")
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([ClipItem("file", ClipKind.FILES, "file", 1, 1, files=(str(path),))])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)
    cursor = panel.file_text_preview.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(4, cursor.MoveMode.KeepAnchor)
    panel.file_text_preview.setTextCursor(cursor)
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    QApplication.clipboard().clear()

    handled = panel.eventFilter(
        panel.search,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        ),
    )

    assert handled
    assert QApplication.clipboard().text() == "文件预览"
    assert panel.search.hasFocus()


def test_preview_copy_keeps_a_fresh_search_selection_as_the_priority(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("copy", "预览内容可以复制", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)
    preview_cursor = panel.text_preview.textCursor()
    preview_cursor.setPosition(0)
    preview_cursor.setPosition(4, preview_cursor.MoveMode.KeepAnchor)
    panel.text_preview.setTextCursor(preview_cursor)
    panel.search.setText("搜索框优先")
    panel.search.selectAll()
    QApplication.clipboard().setText("sentinel")

    handled = panel.eventFilter(
        panel.search,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        ),
    )

    assert not handled
    assert panel.search.hasSelectedText()
    assert QApplication.clipboard().text() == "sentinel"


def test_preview_context_menu_uses_chinese_copy_and_select_all_actions(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("copy", "预览内容可以复制", 1)])
    preview = panel.text_preview

    menu, copy_action, select_all_action = panel._create_preview_menu(preview)
    qtbot.addWidget(menu)
    assert [action.text() for action in menu.actions()] == ["复制", "全选"]
    assert not copy_action.isEnabled()
    assert select_all_action.isEnabled()

    select_all_action.trigger()
    assert preview.textCursor().selectedText() == "预览内容可以复制"
    QApplication.clipboard().clear()
    copy_action.setEnabled(True)
    copy_action.trigger()
    assert QApplication.clipboard().text() == "预览内容可以复制"


def test_binary_file_keeps_file_icon_preview(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"\x00\x01\x02\xff" * 32)
    item = ClipItem("binary", ClipKind.FILES, "binary", 1, 1, files=(str(path),))
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)

    panel.set_items([item])

    assert panel.preview_stack.currentWidget() is panel.file_preview


def test_extended_keyboard_selection_and_batch_delete_signal(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip(str(index), f"item {index}", index) for index in range(4)])
    deleted: list[tuple[ClipItem, ...]] = []
    panel.delete_requested.connect(deleted.append)

    panel._move_selection(1, Qt.KeyboardModifier.ShiftModifier)
    panel._move_selection(1, Qt.KeyboardModifier.ControlModifier)
    selected = panel._selected_items()

    assert len(selected) == 3
    panel._request_delete_selected()
    assert deleted == [selected]


def test_physical_ctrl_mouse_modifier_toggles_selection(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip(str(index), f"item {index}", index) for index in range(3)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    second = panel.model.index(1)

    qtbot.mouseClick(
        panel.list.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.MetaModifier,
        panel.list.visualRect(second).center(),
    )

    assert {index.row() for index in panel.list.selectionModel().selectedRows()} == {0, 1}


def test_detail_information_for_text_and_image(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    text = ClipItem("text", ClipKind.TEXT, "text", 1, 1, text="你好 world")
    image = ClipItem("image", ClipKind.IMAGE, "image", 1, 2, byte_size=2048)
    panel.set_items([text, image])

    panel._show_detail(1)
    assert panel.info_type_value.text() == "文本"
    assert panel.info_detail_label.text() == "字数"
    assert panel.info_detail_value.text() == "8 字"
    panel._show_detail(0)
    assert panel.info_type_value.text() == "图片"
    assert panel.info_detail_label.text() == "图片大小"
    assert panel.info_detail_value.text() == "2.0 KB"


def test_settings_shield_blur_removes_readable_detail(qtbot) -> None:
    pixmap = QPixmap(352, 88)
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    for x in range(0, pixmap.width(), 4):
        color = QColor("black") if (x // 4) % 2 else QColor("white")
        painter.fillRect(x, 0, 4, pixmap.height(), color)
    painter.end()

    blurred = ui_module._soft_blurred_snapshot(pixmap).toImage()
    source = pixmap.toImage()

    def max_neighbor_lightness_delta(image: QImage) -> int:
        y = image.height() // 2
        return max(
            abs(image.pixelColor(x, y).lightness() - image.pixelColor(x + 1, y).lightness())
            for x in range(image.width() - 1)
        )

    assert ui_module._SETTINGS_SHIELD_BLUR_DOWNSAMPLE >= 40
    assert ui_module._SETTINGS_SHIELD_LIGHT_VEIL_ALPHA >= 180
    assert max_neighbor_lightness_delta(source) > 200
    assert max_neighbor_lightness_delta(blurred) < 55


def test_settings_interaction_shield_is_physically_rounded(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="liquid_glass"))
    qtbot.addWidget(panel)
    panel.set_items([clip("text", "content", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    panel.set_settings_interaction_blocked(True)
    shield = panel._settings_interaction_shield

    assert shield is not None
    assert shield.isVisible()
    assert not shield.mask().contains(QPoint(0, 0))
    assert shield.mask().contains(shield.rect().center())

    panel.set_settings_interaction_blocked(False)
    assert not shield.isVisible()


def test_tray_menu_uses_generic_show_window_label(qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    tray, menu, actions = create_tray_icon(parent)
    qtbot.addWidget(menu)

    assert actions["show"].text() == "显示窗口"
    assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
        "显示窗口",
        "暂停记录",
        "设置…",
        "退出",
    ]

    tray.hide()


def test_list_context_menu_uses_compact_content_width(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="light"))
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "item", 1)])
    menu, delete_action, _clear_action, _clear_unpinned_action, _pin_action, _settings_action = (
        panel._create_list_menu()
    )
    qtbot.addWidget(menu)

    font = QFont(menu.font())
    font.setPointSize(ui_module._POPUP_ITEM_FONT_SIZE_PT)
    expected = (
        QFontMetrics(font).horizontalAdvance("删除")
        + (ui_module._COMPACT_MENU_ICON_SIZE + ui_module._COMPACT_MENU_ICON_GAP)
        + 2
        * (
            ui_module._COMPACT_MENU_SHELL_HORIZONTAL_INSET
            + ui_module._COMPACT_MENU_ITEM_HORIZONTAL_MARGIN
            + ui_module._COMPACT_MENU_ITEM_HORIZONTAL_PADDING
        )
    )
    assert menu.width() >= expected
    assert menu.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize, None, menu) == 14
    assert f"font-size: {ui_module._POPUP_ITEM_FONT_SIZE_PT}pt" in menu.styleSheet()
    assert (
        f"padding: {ui_module._COMPACT_MENU_VERTICAL_INSET}px "
        f"{ui_module._COMPACT_MENU_SHELL_HORIZONTAL_INSET}px"
    ) in menu.styleSheet()
    assert f"border-radius: {ui_module._COMPACT_MENU_CORNER_RADIUS}px" in menu.styleSheet()
    assert (
        f"QMenu::item {{ margin: 0px {ui_module._COMPACT_MENU_ITEM_HORIZONTAL_MARGIN}px; "
        f"padding: 6px {ui_module._COMPACT_MENU_ITEM_HORIZONTAL_PADDING}px "
        f"6px {ui_module._COMPACT_MENU_ITEM_HORIZONTAL_PADDING}px; border-radius: 6px; }}"
    ) in menu.styleSheet()
    assert f"QMenu::icon {{ left: {ui_module._COMPACT_MENU_ICON_LEFT_OFFSET}px; }}" in menu.styleSheet()
    assert "QMenu::item:selected" in menu.styleSheet()
    assert "background: #E0E6F3" in menu.styleSheet()

    menu.show()
    qtbot.waitExposed(menu)
    action_rect = menu.actionGeometry(delete_action)
    assert action_rect.x() == menu.width() - action_rect.x() - action_rect.width()
    assert action_rect.width() >= menu.width() - 2
    qtbot.mouseMove(menu, action_rect.center())
    qtbot.wait(20)

    assert menu.activeAction() is delete_action
    assert not menu.mask().isEmpty()
    assert not menu.mask().contains(QPoint(0, 0))


def test_list_context_menu_actions_use_their_own_semantic_icons(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "item", 1)])

    menu, delete_action, clear_action, clear_unpinned_action, pin_action, settings_action = (
        panel._create_list_menu()
    )
    qtbot.addWidget(menu)

    menu_actions = (
        delete_action,
        clear_action,
        clear_unpinned_action,
        pin_action,
        settings_action,
    )
    assert [action.text() for action in menu_actions] == [
        "删除",
        "清空",
        "清空NP",
        "置顶",
        "设置",
    ]
    assert all(not action.icon().isNull() for action in menu_actions)
    assert all(action.isIconVisibleInMenu() for action in menu_actions)
    assert clear_unpinned_action.isEnabled()
    assert pin_action.isEnabled()
    assert pin_action.data() is True

    panel.set_items([clip("pinned", "item", 1).with_pin(True)])
    menu, _delete_action, _clear_action, clear_unpinned_action, pin_action, _settings_action = (
        panel._create_list_menu()
    )
    qtbot.addWidget(menu)

    assert not clear_unpinned_action.isEnabled()
    assert pin_action.text() == "取消置顶"
    assert pin_action.data() is False


def test_compact_context_menu_uses_explicit_dark_theme_contrast(qtbot) -> None:
    menu = QMenu()
    qtbot.addWidget(menu)
    menu.addAction("删除")
    menu.addAction("清空")
    menu.addAction("清空NP")
    menu.addAction("置顶")
    menu.addSeparator()
    menu.addAction("设置")

    _compact_menu(menu, dark=True)

    style = menu.styleSheet()
    assert "background: #2A2F3B" in style
    assert "color: #F2F4F8" in style
    assert f"font-size: {ui_module._POPUP_ITEM_FONT_SIZE_PT}pt" in style
    assert f"border-radius: {ui_module._COMPACT_MENU_CORNER_RADIUS}px" in style
    assert "background: #444B5C" in style
    assert "background: #454C5C" in style


@pytest.mark.parametrize(
    ("kind", "confirmation_text"),
    (
        (None, "清空全部剪贴板历史？此操作无法撤销。"),
        (ClipKind.TEXT, "清空剪切板文本历史？此操作无法撤销。"),
        (ClipKind.IMAGE, "清空剪切板截图历史？此操作无法撤销。"),
        (ClipKind.FILES, "清空剪切板文件历史？此操作无法撤销。"),
    ),
    ids=("all", "text", "image", "files"),
)
def test_clear_current_tab_history_uses_its_own_confirmation_and_signal(
    qtbot, monkeypatch, kind, confirmation_text
) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items(
        [
            clip("text", "text", 3),
            ClipItem("image", ClipKind.IMAGE, "image", 2, 2),
            ClipItem("files", ClipKind.FILES, "files", 1, 1, files=("/tmp/file",)),
        ]
    )
    panel._set_filter_kind(kind)
    confirmations: list[tuple[str, str, str]] = []
    requested: list[ClipKind | None] = []
    panel.clear_requested.connect(requested.append)

    def confirm(parent, title, text, confirm_text, *, dark=False, appearance=None) -> bool:
        del parent, dark, appearance
        confirmations.append((title, text, confirm_text))
        return True

    monkeypatch.setattr(ui_module, "_confirm_destructive_action", confirm)

    panel._request_clear_current_kind()

    assert confirmations == [("清空历史", confirmation_text, "确定")]
    assert requested == [kind]


def test_clear_unpinned_history_uses_its_own_confirmation_and_signal(qtbot, monkeypatch) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    confirmations: list[tuple[str, str, str]] = []
    requested: list[bool] = []
    panel.clear_unpinned_requested.connect(lambda: requested.append(True))

    def confirm(parent, title, text, confirm_text, *, dark=False, appearance=None) -> bool:
        del parent, dark, appearance
        confirmations.append((title, text, confirm_text))
        return True

    monkeypatch.setattr(ui_module, "_confirm_destructive_action", confirm)

    panel._request_clear_unpinned()

    assert confirmations == [("清空历史", "清空所有非置顶历史？此操作无法撤销。", "确定")]
    assert requested == [True]


def test_list_context_menu_keeps_clear_scoped_to_tab_and_routes_settings(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items(
        [
            clip("text", "text", 2),
            ClipItem("image", ClipKind.IMAGE, "image", 1, 1),
        ]
    )
    panel._set_filter_kind(ClipKind.TEXT)
    panel.search.setText("no matching text")
    menu, delete_action, clear_action, clear_unpinned_action, pin_action, settings_action = (
        panel._create_list_menu()
    )
    qtbot.addWidget(menu)
    settings_requests: list[bool] = []
    panel.settings_requested.connect(lambda: settings_requests.append(True))

    assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
        "删除",
        "清空",
        "清空NP",
        "置顶",
        "设置",
    ]
    assert not delete_action.isEnabled()
    assert clear_action.isEnabled()
    assert clear_unpinned_action.isEnabled()
    assert not pin_action.isEnabled()
    assert settings_action.isEnabled()

    panel._handle_list_menu_action(
        settings_action,
        delete_action,
        clear_action,
        clear_unpinned_action,
        pin_action,
        settings_action,
    )

    assert settings_requests == [True]


def test_list_context_menu_routes_pin_signal(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    selected = clip("selected", "item", 1)
    panel.set_items([selected])
    menu, delete_action, clear_action, clear_unpinned_action, pin_action, settings_action = (
        panel._create_list_menu()
    )
    qtbot.addWidget(menu)
    requests: list[tuple[tuple[str, ...], bool]] = []
    panel.pin_requested.connect(lambda items, pinned: requests.append((tuple(item.id for item in items), pinned)))

    panel._handle_list_menu_action(
        pin_action,
        delete_action,
        clear_action,
        clear_unpinned_action,
        pin_action,
        settings_action,
    )

    assert requests == [(("selected",), True)]


def test_filter_and_list_background_align_with_borderless_search_region(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("text", "text", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    all_button = panel._filter_buttons[0][0]
    button_left = all_button.mapTo(panel, QPoint()).x()
    list_background_left = panel.list.viewport().mapTo(panel, QPoint(4, 0)).x()
    search_box = panel.findChild(QFrame, "searchBox")
    search_icon = panel.findChild(SearchIcon)

    assert button_left == list_background_left
    assert search_box is not None
    assert search_box.frameShape() == QFrame.Shape.NoFrame
    assert search_box.mapTo(panel, QPoint()).x() == list_background_left
    search_right = search_box.mapTo(panel, QPoint(search_box.width(), 0)).x()
    detail_right = panel.detail.mapTo(panel, QPoint(panel.detail.width(), 0)).x()
    assert search_right == detail_right
    assert search_icon is not None and search_icon.width() == 30


def test_clicking_search_icon_requests_settings(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    requested: list[bool] = []
    panel.settings_requested.connect(lambda: requested.append(True))

    qtbot.mouseClick(panel.search_icon, Qt.MouseButton.LeftButton)

    assert requested == [True]


def test_search_icon_click_keeps_the_main_panel_typing_focus(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    requested: list[bool] = []
    panel.settings_requested.connect(lambda: requested.append(True))
    panel.show_panel()
    qtbot.waitExposed(panel)

    qtbot.mouseClick(panel.search_icon, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)

    assert panel.search_icon.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert panel.search_icon.toolTip() == "打开设置"
    assert requested == [True]


@pytest.mark.parametrize("theme", ("light", "dark", "system", "liquid_glass"))
def test_active_panel_keeps_search_focus_after_every_in_panel_interaction(qtbot, theme: str) -> None:
    panel = ClipPanel(lambda: AppSettings(theme=theme))
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first preview", 2), clip("second", "second preview", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    def assert_search_focus() -> None:
        qtbot.waitUntil(panel.search.hasFocus, timeout=500)
        assert QApplication.focusWidget() is panel.search

    assert_search_focus()
    qtbot.mouseClick(panel._filter_buttons[1][0], Qt.MouseButton.LeftButton)
    assert_search_focus()
    second = panel.model.index(1)
    qtbot.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=panel.list.visualRect(second).center())
    assert_search_focus()
    qtbot.keyClick(panel.search, Qt.Key.Key_Up)
    assert_search_focus()
    assert panel.list.currentIndex().row() == 0
    qtbot.mouseClick(panel.text_preview.viewport(), Qt.MouseButton.LeftButton)
    assert_search_focus()
    qtbot.mouseClick(panel.search_icon, Qt.MouseButton.LeftButton)
    assert_search_focus()
    panel.search.clearFocus()
    assert_search_focus()

    assert panel.list.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert panel.text_preview.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert panel.search_icon.focusPolicy() == Qt.FocusPolicy.NoFocus


@pytest.mark.parametrize("theme", ("light", "dark", "system", "liquid_glass"))
def test_search_uses_an_app_owned_themed_blinking_caret(qtbot, theme: str) -> None:
    panel = ClipPanel(lambda: AppSettings(theme=theme))
    qtbot.addWidget(panel)
    panel.show_panel()
    qtbot.waitExposed(panel)

    colors = _theme_colors(panel._appearance)
    palette = panel.search.palette()
    assert palette.color(QPalette.ColorRole.Text) == QColor(colors.text)
    assert palette.color(QPalette.ColorRole.WindowText) == QColor(colors.text)
    assert f"color: {colors.text}; font-size: 16pt" in panel.styleSheet()
    assert f"selection-color: {_accent_foreground(panel._appearance)}" in panel.styleSheet()

    # The native QLineEdit caret must be absent during the overlay's hidden
    # half-cycle, otherwise macOS paints a white line through glass themes.
    assert panel.search.property("_clipsoon_app_owned_caret") is True
    assert (
        panel.search.style().pixelMetric(
            QStyle.PixelMetric.PM_TextCursorWidth,
            None,
            panel.search,
        )
        == 0
    )
    caret = panel._search_caret
    assert caret.overlay._color == QColor(colors.text)
    prior_phase = caret._phase_visible
    caret._advance_phase()
    assert caret._phase_visible is not prior_phase
    caret._restart_blink()
    if QApplication.styleHints().cursorFlashTime() >= 2:
        assert caret._blink_timer.interval() == max(
            1,
            QApplication.styleHints().cursorFlashTime() // 2,
        )


@pytest.mark.parametrize("theme", ("light", "dark", "system", "liquid_glass"))
def test_settings_text_editors_use_the_same_app_owned_blinking_caret(qtbot, theme: str) -> None:
    dialog = SettingsDialog(AppSettings(theme=theme), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    editors = (
        dialog.custom_hotkey.findChild(QLineEdit),
        dialog.interval.lineEdit(),
        dialog.maximum.lineEdit(),
        dialog.retention.lineEdit(),
        dialog.delay.lineEdit(),
        dialog.selection_memory.lineEdit(),
    )
    assert all(editor is not None for editor in editors)

    def assert_editor_tokens() -> None:
        current_colors = _theme_colors(dialog._appearance)
        assert len(dialog._text_carets) == len(editors)
        for editor, caret in zip(editors, dialog._text_carets, strict=True):
            assert editor is not None
            palette = editor.palette()
            # The custom-hotkey and selection-memory inputs can start disabled;
            # assert their active palette because it is the one used as soon as
            # the editable control receives focus and renders its themed caret.
            assert palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text) == QColor(
                current_colors.text
            )
            assert palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText) == QColor(
                current_colors.text
            )
            assert editor.property("_clipsoon_app_owned_caret") is True
            assert (
                editor.style().pixelMetric(
                    QStyle.PixelMetric.PM_TextCursorWidth,
                    None,
                    editor,
                )
                == 0
            )
            assert caret.overlay._color == QColor(current_colors.text)
        assert f"selection-background-color: {current_colors.accent}; selection-color: " in dialog.styleSheet()
        assert f"selection-color: {_accent_foreground(dialog._appearance)};" in dialog.styleSheet()

    assert_editor_tokens()
    dialog.apply_settings(AppSettings(theme="dark" if not dialog._appearance.dark else "light"))
    assert_editor_tokens()


def test_windows_settings_close_ignores_late_empty_hotkey_editing_finished(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(ui_module.sys, "platform", "win32")
    warnings: list[str] = []
    monkeypatch.setattr(
        ui_module,
        "_show_themed_warning",
        lambda _parent, title, *_args, **_kwargs: warnings.append(title),
    )
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog.close()
    dialog.custom_hotkey.setKeySequence(QKeySequence())
    dialog._emit_hotkey_change()

    assert dialog._closing
    assert warnings == []


def test_empty_action_and_closed_context_menu_restore_the_permanent_search_focus(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("second", "second", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    panel.search.setText("no matching history")
    assert panel.history_content.currentWidget() is panel.empty_state
    qtbot.mouseClick(panel.empty_state_clear, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)
    assert QApplication.focusWidget() is panel.search

    def dismiss_context_menu() -> None:
        popup = QApplication.activePopupWidget()
        if popup is None:
            QTimer.singleShot(5, dismiss_context_menu)
            return
        popup.close()

    panel.search.clearFocus()
    QTimer.singleShot(0, dismiss_context_menu)
    panel._open_list_menu(panel.list.visualRect(panel.model.index(0)).center())
    qtbot.waitUntil(panel.search.hasFocus, timeout=500)
    assert QApplication.focusWidget() is panel.search


def test_hover_background_is_visible_but_weaker_than_selection(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="light"))
    qtbot.addWidget(panel)
    panel.set_items([clip("first", "first", 2), clip("second", "second", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)
    second = panel.model.index(1)

    qtbot.mouseMove(panel.list.viewport(), panel.list.visualRect(second).center())
    qtbot.wait(20)

    hover = _hover_color(False)
    hovered_row = panel.list.itemDelegate().hovered_row
    rendered = panel.list.viewport().grab().toImage()
    sample_at = panel.list.visualRect(second).center()
    sample_at.setX(panel.list.visualRect(second).right() - 10)
    assert hover != QColor("#5264E8")
    assert hover != panel.palette().color(panel.backgroundRole())
    assert hovered_row == 1
    assert rendered.pixelColor(sample_at) == hover
    assert panel.model.data(second, Qt.ItemDataRole.ToolTipRole) is None

    blank = QPoint(panel.list.viewport().width() - 2, panel.list.viewport().height() - 2)
    qtbot.mouseMove(panel.list.viewport(), blank)
    qtbot.wait(20)
    assert panel.list.itemDelegate().hovered_row == -1


def test_dark_list_thumbnails_use_the_dark_surface_token(qtbot) -> None:
    panel = ClipPanel(lambda: AppSettings(theme="dark"))
    qtbot.addWidget(panel)
    panel.set_items([clip("selected", "selected", 2), clip("unselected", "unselected", 1)])
    panel.show_panel()
    qtbot.waitExposed(panel)

    row = panel.model.index(1)
    thumbnail = ClipDelegate._thumbnail_rect(panel.list.visualRect(row))
    rendered = panel.list.viewport().grab().toImage()
    background_sample = QPoint(thumbnail.right() - 3, thumbnail.bottom() - 3)

    assert rendered.pixelColor(background_sample) == QColor("#343A48")
