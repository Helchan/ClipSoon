from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence
from PySide6.QtWidgets import QFrame, QMenu, QStyleOptionViewItem

import clipsoon.ui as ui_module
from clipsoon import __version__
from clipsoon.core import AppSettings, ClipItem, ClipKind
from clipsoon.ui import (
    ClipDelegate,
    ClipPanel,
    SearchIcon,
    SettingsDialog,
    _compact_menu,
    _hotkey_display,
    _hover_color,
    _parse_hotkey,
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
    assert dialog.version_label.text() == f"ClipSoon v{__version__}"
    dialog.hotkey_mode.setCurrentText("双击 Shift")
    assert dialog.values()["hotkey"] == "double:shift"


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


def test_copied_image_file_uses_image_thumbnail(tmp_path: Path) -> None:
    path = tmp_path / "copied-image.png"
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#e23b4f"))
    assert image.save(str(path), "PNG")

    delegate = ClipDelegate()
    thumbnail = delegate._file_image_thumbnail((str(path),), image.size())

    assert not thumbnail.isNull()
    assert thumbnail.toImage().pixelColor(0, 0) == QColor("#e23b4f")
    assert delegate._file_image_thumbnail((str(path), str(path)), image.size()).isNull()


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
    assert not panel.image_preview.pixmap().isNull()
    assert panel.info_type_value.text() == "文件"
    assert panel.info_detail_label.text() == "路径"
    assert panel.info_detail_value.text() == str(path)
    assert panel.list.itemDelegate().sizeHint(QStyleOptionViewItem(), panel.model.index(0)).height() == 52


def test_list_item_content_has_equal_top_and_bottom_padding() -> None:
    row = QRect(4, 1, 500, 50)
    content = ClipDelegate._thumbnail_rect(row)

    assert content.top() - row.top() == row.bottom() - content.bottom()


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


def test_list_context_menu_uses_compact_content_width(qtbot) -> None:
    menu = QMenu()
    qtbot.addWidget(menu)
    menu.addAction("删除所选")
    menu.addSeparator()
    menu.addAction("清空历史")

    _compact_menu(menu)

    expected = max(68, menu.fontMetrics().horizontalAdvance("删除所选") + 18)
    assert menu.width() == expected


def test_filter_and_list_background_align_with_bordered_search(qtbot) -> None:
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
    assert search_box.mapTo(panel, QPoint()).x() == list_background_left
    search_right = search_box.mapTo(panel, QPoint(search_box.width(), 0)).x()
    detail_right = panel.detail.mapTo(panel, QPoint(panel.detail.width(), 0)).x()
    assert search_right == detail_right
    assert search_icon is not None and search_icon.width() == 36


def test_clicking_search_icon_requests_settings(qtbot) -> None:
    panel = ClipPanel(AppSettings)
    qtbot.addWidget(panel)
    requested: list[bool] = []
    panel.settings_requested.connect(lambda: requested.append(True))

    qtbot.mouseClick(panel.search_icon, Qt.MouseButton.LeftButton)

    assert requested == [True]


def test_hover_background_is_visible_but_weaker_than_selection(qtbot) -> None:
    panel = ClipPanel(AppSettings)
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
    assert hover != QColor("#5B6CFF")
    assert hover != panel.palette().color(panel.backgroundRole())
    assert hovered_row == 1
    assert rendered.pixelColor(sample_at) == hover
    assert panel.model.data(second, Qt.ItemDataRole.ToolTipRole) is None

    blank = QPoint(panel.list.viewport().width() - 2, panel.list.viewport().height() - 2)
    qtbot.mouseMove(panel.list.viewport(), blank)
    qtbot.wait(20)
    assert panel.list.itemDelegate().hovered_row == -1
