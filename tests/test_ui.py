from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence
from PySide6.QtWidgets import QDialog, QFrame, QMenu, QStyleOptionViewItem

import clipsoon.ui as ui_module
from clipsoon import __version__
from clipsoon.core import AppSettings, ClipItem, ClipKind
from clipsoon.ui import (
    ClipDelegate,
    ClipPanel,
    ImagePreview,
    SearchIcon,
    SettingsDialog,
    _bucketed_size,
    _ByteLruCache,
    _compact_menu,
    _hotkey_display,
    _hover_color,
    _parse_hotkey,
    _ScaledImageLoader,
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
    assert dialog.findChildren(QFrame, "settingsSection")
    assert dialog.findChild(QFrame, "settingsSection") is not None
    assert not hasattr(dialog, "version_label")
    dialog.hotkey_mode.setCurrentText("双击 Shift")
    assert dialog.values()["hotkey"] == "double:shift"


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
        dialog.hotkey_mode,
        dialog.custom_hotkey,
        dialog.interval,
        dialog.maximum,
        dialog.retention,
        dialog.delay,
        dialog.theme,
        dialog.selection_memory,
    ]
    assert len({control.width() for control in controls}) == 1


def test_selection_memory_setting_is_an_optional_three_second_default(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), accessibility_granted=True)
    qtbot.addWidget(dialog)

    assert not dialog.remember_selection.isChecked()
    assert dialog.selection_memory.value() == 3
    assert not dialog.selection_memory.isEnabled()
    dialog.remember_selection.setChecked(True)
    assert dialog.selection_memory.isEnabled()
    assert dialog.values()["remember_selection"] is True
    assert dialog.values()["selection_memory_seconds"] == 3


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
    panel.list.setCurrentIndex(panel.model.index(1))

    panel.hide()
    assert panel._remembered_item_ids == ("second",)
    qtbot.waitUntil(lambda: panel._remembered_item_ids == (), timeout=1_500)
    assert panel.list.currentIndex().row() == 0
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
    assert panel.list.itemDelegate().sizeHint(QStyleOptionViewItem(), panel.model.index(0)).height() == 52


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
    row = QRect(4, 1, 500, 50)
    content = ClipDelegate._thumbnail_rect(row)

    assert content.top() - row.top() == row.bottom() - content.bottom()


def test_file_icon_is_centered_inside_thumbnail() -> None:
    thumbnail = QRect(12, 8, 36, 36)
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


def test_list_context_menu_uses_compact_content_width(qtbot) -> None:
    menu = QMenu()
    qtbot.addWidget(menu)
    delete_action = menu.addAction("删除所选")
    menu.addSeparator()
    menu.addAction("清空历史")

    _compact_menu(menu)

    expected = max(68, menu.fontMetrics().horizontalAdvance("删除所选") + 18)
    assert menu.width() == expected
    assert "QMenu::item:selected" in menu.styleSheet()
    assert "background: #CBD2E3" in menu.styleSheet()

    menu.show()
    qtbot.waitExposed(menu)
    action_rect = menu.actionGeometry(delete_action)
    qtbot.mouseMove(menu, action_rect.center())
    qtbot.wait(20)

    rendered = menu.grab().toImage()
    background_sample = QPoint(action_rect.right() - 4, action_rect.center().y())
    assert menu.activeAction() is delete_action
    assert rendered.pixelColor(background_sample) == QColor("#CBD2E3")


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
