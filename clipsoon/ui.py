"""Spotlight-style Qt interface kept in one file for a small, fast codebase."""

from __future__ import annotations

import locale
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QFileInfo,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QImageReader,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from clipsoon import __version__
from clipsoon.core import AppSettings, ClipItem, ClipKind, format_bytes
from clipsoon.search import SearchEngine

LOGGER = logging.getLogger(__name__)
ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
_INVALID_INDEX = QModelIndex()
_IMAGE_FILE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".ico",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".pbm",
    ".pgm",
    ".png",
    ".ppm",
    ".svg",
    ".svgz",
    ".tif",
    ".tiff",
    ".webp",
    ".xbm",
    ".xpm",
}
_TEXT_FILE_SUFFIXES = {
    ".bat",
    ".c",
    ".cfg",
    ".cmd",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".log",
    ".md",
    ".mjs",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_FILE_NAMES = {"dockerfile", "license", "makefile", "readme"}
_TEXT_FILE_PREVIEW_BYTES = 4 * 1024
_TEXT_FILE_PREVIEW_CHARS = 220


class ClipListModel(QAbstractListModel):
    def __init__(self) -> None:
        super().__init__()
        self._items: list[ClipItem] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == ITEM_ROLE:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        return None

    def replace(self, items: Sequence[ClipItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> ClipItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None


class SearchIcon(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("设置")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#6574FF"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(5, 4, 21, 21)
        painter.drawLine(22, 22, 31, 31)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ClipListView(QListView):
    """Extended selection that accepts both Command and physical Ctrl on macOS."""

    hover_index_changed = Signal(QModelIndex)

    def selectionCommand(self, index: QModelIndex, event: QEvent | None = None):
        if (
            isinstance(event, QMouseEvent)
            and event.modifiers() & Qt.KeyboardModifier.MetaModifier
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            return QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows
        return super().selectionCommand(index, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        self.hover_index_changed.emit(self.indexAt(event.position().toPoint()))

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.hover_index_changed.emit(QModelIndex())


class ClipDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thumbnails: dict[str, QPixmap] = {}
        self._file_icons: dict[str, QPixmap] = {}
        self._file_icon_provider = QFileIconProvider()
        self.hovered_row = -1
        self.dark_theme = False

    def set_dark_theme(self, dark: bool) -> None:
        self.dark_theme = dark
        view = self.parent()
        if isinstance(view, QListView):
            view.viewport().update()

    def set_hovered_index(self, index: QModelIndex) -> None:
        row = index.row() if index.isValid() else -1
        if row == self.hovered_row:
            return
        self.hovered_row = row
        view = self.parent()
        if isinstance(view, QListView):
            view.viewport().update()

    def helpEvent(self, event, view, option, index) -> bool:
        return False

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), 52)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item: ClipItem = index.data(ITEM_ROLE)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 1, -5, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == self.hovered_row
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#5B6CFF") if selected else _hover_color(self.dark_theme))
            painter.drawRoundedRect(rect, 5, 5)

        thumb_rect = self._thumbnail_rect(rect)
        self._paint_thumbnail(painter, thumb_rect, item, selected)
        text_left = thumb_rect.right() + 13
        text_right = rect.right() - 10
        title_rect = QRect(text_left, thumb_rect.top(), text_right - text_left, thumb_rect.height())
        foreground = QColor("#FFFFFF") if selected else option.palette.color(QPalette.ColorRole.Text)

        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(foreground)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, _elide(painter, item.title, title_rect.width()))
        if item.pinned:
            painter.setPen(foreground)
            pin_rect = QRect(rect.right() - 26, rect.center().y() - 9, 16, 18)
            painter.drawText(pin_rect, Qt.AlignmentFlag.AlignCenter, "◆")
        painter.restore()

    @staticmethod
    def _thumbnail_rect(row_rect: QRect) -> QRect:
        size = 36
        top = row_rect.top() + (row_rect.height() - size) // 2
        return QRect(row_rect.left() + 8, top, size, size)

    def _paint_thumbnail(self, painter: QPainter, rect: QRect, item: ClipItem, selected: bool) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 28) if selected else QColor("#E9ECF5"))
        painter.drawRoundedRect(rect, 11, 11)
        if item.kind is ClipKind.IMAGE:
            pixmap = self._image_thumbnail(item.image_path, rect.size() * 2)
            if not pixmap.isNull():
                painter.drawPixmap(rect, pixmap, pixmap.rect())
                return
        elif item.kind is ClipKind.FILES and item.files:
            pixmap = self._file_image_thumbnail(item.files, rect.size() * 2)
            if not pixmap.isNull():
                painter.drawPixmap(rect, pixmap, pixmap.rect())
                return
            pixmap = self._file_thumbnail(item.files[0])
            if not pixmap.isNull():
                target = self._centered_file_icon_rect(rect)
                painter.drawPixmap(target, pixmap, pixmap.rect())
                return
        color = QColor("#FFFFFF") if selected else QColor("#5664E8")
        painter.setPen(color)
        font = painter.font()
        font.setPixelSize(21)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T" if item.kind is ClipKind.TEXT else "F")

    @staticmethod
    def _centered_file_icon_rect(container: QRect) -> QRect:
        inset = 3
        return container.adjusted(inset, inset, -inset, -inset)

    def _image_thumbnail(self, path: str, size: QSize) -> QPixmap:
        if path not in self._thumbnails:
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            reader.setScaledSize(size)
            image = reader.read()
            self._thumbnails[path] = QPixmap.fromImage(image).scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        return self._thumbnails[path]

    def _file_image_thumbnail(self, files: Sequence[str], size: QSize) -> QPixmap:
        path = _single_image_file_path(files)
        if not path:
            return QPixmap()
        return self._image_thumbnail(path, size)

    def _file_thumbnail(self, path: str) -> QPixmap:
        suffix = Path(path).suffix.casefold() or "folder"
        if suffix not in self._file_icons:
            icon = (
                self._file_icon_provider.icon(QFileIconProvider.IconType.Folder)
                if Path(path).is_dir()
                else self._file_icon_provider.icon(QFileInfo(path))
            )
            self._file_icons[suffix] = icon.pixmap(72, 72)
        return self._file_icons[suffix]


class ImagePreview(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self._path = ""
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(220, 240)
        self.setText("图片预览")

    def set_path(self, path: str) -> None:
        self._path = path
        self._render()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        if not self._path or self.width() < 20 or self.height() < 20:
            return
        reader = QImageReader(self._path)
        reader.setAutoTransform(True)
        natural = reader.size()
        bounds = QSize(max(1, self.width() - 20), max(1, self.height() - 20))
        if natural.isValid():
            natural.scale(bounds, Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(natural)
        image = reader.read()
        if image.isNull():
            self.setText("无法预览图片")
            return
        self.setPixmap(QPixmap.fromImage(image))


class TextFilePreview(QPlainTextEdit):
    """A fixed, non-scrollable excerpt rather than a miniature file viewer."""

    def wheelEvent(self, event) -> None:
        event.accept()


class SettingsDialog(QDialog):
    clear_requested = Signal()
    reveal_requested = Signal()
    accessibility_requested = Signal()

    _HOTKEYS = {
        "双击 Ctrl": "double:ctrl",
        "双击 Shift": "double:shift",
        "双击 Alt / Option": "double:alt",
        "双击 Command / Win": "double:meta",
        "自定义组合键": "custom",
    }

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        accessibility_granted: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ClipSoon 设置")
        self.setModal(True)
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = QLabel("偏好设置")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(13)
        layout.addLayout(form)

        self.hotkey_mode = QComboBox()
        self.hotkey_mode.addItems(self._HOTKEYS)
        current = next((label for label, value in self._HOTKEYS.items() if value == settings.hotkey), "自定义组合键")
        self.hotkey_mode.setCurrentText(current)
        form.addRow("呼出快捷键", self.hotkey_mode)
        self.custom_hotkey = QKeySequenceEdit()
        self.custom_hotkey.setMaximumSequenceLength(1)
        self.custom_hotkey.setClearButtonEnabled(True)
        default_custom = "combo:ctrl+shift+v"
        hotkey_text = _hotkey_display(settings.hotkey if settings.hotkey.startswith("combo:") else default_custom)
        self.custom_hotkey.setKeySequence(QKeySequence(hotkey_text))
        self.custom_hotkey.setEnabled(current == "自定义组合键")
        self.hotkey_mode.currentTextChanged.connect(
            lambda value: self.custom_hotkey.setEnabled(value == "自定义组合键")
        )
        form.addRow("自定义组合键", self.custom_hotkey)

        self.interval = _spin(settings.double_tap_interval_ms, 180, 900, " ms")
        self.maximum = _spin(settings.max_history_items, 50, 10_000, " 条")
        self.retention = _spin(settings.retention_days, 0, 3_650, " 天（0 = 永久）")
        self.delay = _spin(settings.paste_delay_ms, 60, 2_000, " ms")
        form.addRow("双击间隔", self.interval)
        form.addRow("历史容量", self.maximum)
        form.addRow("保留时间", self.retention)
        form.addRow("目标恢复等待", self.delay)

        self.theme = QComboBox()
        self.theme.addItem("跟随系统", "system")
        self.theme.addItem("浅色", "light")
        self.theme.addItem("深色", "dark")
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        form.addRow("外观", self.theme)

        self.capture = QCheckBox("记录新的剪贴板内容")
        self.capture.setChecked(settings.capture_enabled)
        self.paste = QCheckBox("选择后自动粘贴到原应用")
        self.paste.setChecked(settings.paste_after_selection)
        self.hide = QCheckBox("面板失去焦点时自动隐藏")
        self.hide.setChecked(settings.hide_on_deactivate)
        layout.addWidget(self.capture)
        layout.addWidget(self.paste)
        layout.addWidget(self.hide)

        self.accessibility_button = None
        platform_message = ""
        if sys.platform == "darwin" and accessibility_granted is not True:
            platform_message = "macOS 需要辅助功能权限，才能监听全局快捷键并自动粘贴到其他应用。"
        elif sys.platform == "win32":
            platform_message = (
                "Windows 无需开启辅助功能权限；向管理员身份运行的应用自动粘贴时，"
                "ClipSoon 也需要以管理员身份运行。"
            )
        if platform_message:
            platform_note = QFrame()
            platform_note.setObjectName("platformNote")
            platform_layout = QHBoxLayout(platform_note)
            platform_layout.setContentsMargins(12, 10, 12, 10)
            platform_layout.setSpacing(12)
            note = QLabel(platform_message)
            note.setWordWrap(True)
            platform_layout.addWidget(note, 1)
        if sys.platform == "darwin" and accessibility_granted is not True:
            self.accessibility_button = QPushButton("打开辅助功能设置")
            self.accessibility_button.clicked.connect(self.accessibility_requested)
            platform_layout.addWidget(self.accessibility_button)
        if platform_message:
            layout.addWidget(platform_note)

        data_row = QHBoxLayout()
        clear = QPushButton("清除未置顶历史")
        reveal = QPushButton("打开数据目录")
        clear.clicked.connect(self._confirm_clear)
        reveal.clicked.connect(self.reveal_requested)
        data_row.addWidget(clear)
        data_row.addWidget(reveal)
        data_row.addStretch()
        layout.addLayout(data_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate_accept)
        footer = QHBoxLayout()
        self.version_label = QLabel(f"ClipSoon v{__version__}")
        self.version_label.setObjectName("muted")
        footer.addWidget(self.version_label)
        footer.addStretch()
        footer.addWidget(buttons)
        layout.addLayout(footer)

    def values(self) -> dict[str, object]:
        selected = self._HOTKEYS[self.hotkey_mode.currentText()]
        if selected == "custom":
            recorded = self.custom_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            selected = _parse_hotkey(recorded)
        return {
            "hotkey": selected,
            "double_tap_interval_ms": self.interval.value(),
            "max_history_items": self.maximum.value(),
            "retention_days": self.retention.value(),
            "paste_delay_ms": self.delay.value(),
            "theme": self.theme.currentData(),
            "capture_enabled": self.capture.isChecked(),
            "paste_after_selection": self.paste.isChecked(),
            "hide_on_deactivate": self.hide.isChecked(),
        }

    def _validate_accept(self) -> None:
        recorded = self.custom_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        if self._HOTKEYS[self.hotkey_mode.currentText()] == "custom" and not _parse_hotkey(recorded):
            QMessageBox.warning(self, "快捷键无效", "组合键必须包含 Ctrl/Shift/Alt/Command 和一个普通键。")
            return
        self.accept()

    def _confirm_clear(self) -> None:
        answer = QMessageBox.question(self, "清除历史", "清除所有未置顶的历史？此操作无法撤销。")
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_requested.emit()


class ClipPanel(QWidget):
    send_requested = Signal(object)
    settings_requested = Signal()
    delete_requested = Signal(object)
    clear_requested = Signal()
    accessibility_requested = Signal()

    def __init__(self, settings: Callable[[], AppSettings]) -> None:
        super().__init__()
        self._settings = settings
        self._items: list[ClipItem] = []
        self._engine = SearchEngine()
        self._kind: ClipKind | None = None
        self._keep_open = False
        self._selection_anchor = 0
        self._filter_buttons: list[tuple[QToolButton, ClipKind | None]] = []
        self._filter_index = 0
        self.setObjectName("panelWindow")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.setMinimumSize(720, 500)
        self.resize(900, 610)
        self._build()
        self.apply_theme()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        self.card = QFrame()
        self.card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 115))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        root = QVBoxLayout(self.card)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(4, 0, 0, 0)
        search_box = QFrame()
        search_box.setObjectName("searchBox")
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(8, 3, 8, 3)
        search_layout.setSpacing(5)
        self.search_icon = SearchIcon()
        self.search_icon.clicked.connect(self.settings_requested.emit)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索剪贴板历史…")
        self.search.setClearButtonEnabled(True)
        search_layout.addWidget(self.search_icon)
        search_layout.addWidget(self.search, 1)
        search_row.addWidget(search_box, 1)
        root.addLayout(search_row)

        filters = QHBoxLayout()
        filters.setContentsMargins(4, 0, 0, 0)
        filters.setSpacing(7)
        filters_by_kind = (
            ("全部", None),
            ("文本", ClipKind.TEXT),
            ("截图", ClipKind.IMAGE),
            ("文件", ClipKind.FILES),
        )
        for label, kind in filters_by_kind:
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("filterChip", True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setChecked(kind is None)
            button.clicked.connect(lambda _checked=False, kind=kind, button=button: self._filter(kind, button))
            self._filter_buttons.append((button, kind))
            filters.addWidget(button)
        filters.addStretch()
        self.count_label = QLabel("0 条")
        self.count_label.setObjectName("muted")
        filters.addWidget(self.count_label)
        root.addLayout(filters)

        content = QHBoxLayout()
        content.setSpacing(10)
        self.model = ClipListModel()
        self.list = ClipListView()
        self.list.setObjectName("historyList")
        self.list.setModel(self.model)
        delegate = ClipDelegate(self.list)
        self.list.setItemDelegate(delegate)
        self.list.hover_index_changed.connect(delegate.set_hovered_index)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMinimumWidth(410)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._open_list_menu)
        self.list.doubleClicked.connect(lambda index: self._send(index.row()))
        self.list.selectionModel().currentChanged.connect(lambda current, _previous: self._show_detail(current.row()))
        content.addWidget(self.list, 3)

        self.detail = QFrame()
        self.detail.setObjectName("detail")
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(6)
        self.preview_stack = QStackedWidget()
        self.text_preview = QPlainTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setFrameShape(QFrame.Shape.NoFrame)
        self.image_preview = ImagePreview()
        self.file_preview = QLabel()
        self.file_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_text_preview = TextFilePreview()
        self.file_text_preview.setReadOnly(True)
        self.file_text_preview.setFrameShape(QFrame.Shape.NoFrame)
        self.file_text_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.file_text_preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.file_text_preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview_stack.addWidget(self.text_preview)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.file_preview)
        self.preview_stack.addWidget(self.file_text_preview)
        detail_layout.addWidget(self.preview_stack, 1)
        information_title = QLabel("信息")
        information_title.setObjectName("informationTitle")
        detail_layout.addWidget(information_title)
        information = QGridLayout()
        information.setContentsMargins(0, 0, 0, 0)
        information.setHorizontalSpacing(18)
        information.setVerticalSpacing(8)
        self.info_type_label = QLabel("类型")
        self.info_type_value = QLabel("—")
        self.info_type_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.info_detail_label = QLabel("内容")
        self.info_detail_value = QLabel("—")
        self.info_detail_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.info_detail_value.setWordWrap(True)
        for label in (self.info_type_label, self.info_detail_label):
            label.setObjectName("informationLabel")
        for value in (self.info_type_value, self.info_detail_value):
            value.setObjectName("informationValue")
        information.addWidget(self.info_type_label, 0, 0)
        information.addWidget(self.info_type_value, 0, 1)
        information.addWidget(self.info_detail_label, 1, 0, Qt.AlignmentFlag.AlignTop)
        information.addWidget(self.info_detail_value, 1, 1)
        information.setColumnStretch(1, 1)
        detail_layout.addLayout(information)
        content.addWidget(self.detail, 2)
        root.addLayout(content, 1)

        footer = QHBoxLayout()
        self.status = QLabel("准备就绪")
        self.status.setObjectName("muted")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.status.linkActivated.connect(lambda _link: self.accessibility_requested.emit())
        hints = QLabel("↑↓ 选择    ↵ 发送    Esc 隐藏")
        hints.setObjectName("muted")
        footer.addWidget(self.status)
        footer.addStretch()
        footer.addWidget(hints)
        root.addLayout(footer)
        self.search.textChanged.connect(self._refresh_results)
        self.search.installEventFilter(self)
        self.list.installEventFilter(self)

    def set_items(self, items: Sequence[ClipItem]) -> None:
        self._items = list(items)
        self._engine.replace(self._items)
        self._refresh_results()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_accessibility_warning(self) -> None:
        self.status.setText(
            '需要授予 ClipSoon 辅助功能权限 · <a href="accessibility">打开系统设置</a>'
        )

    def has_accessibility_warning(self) -> bool:
        return 'href="accessibility"' in self.status.text()

    def show_panel(self) -> float:
        started = time.perf_counter()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        geometry = screen.availableGeometry()
        width = min(920, max(720, int(geometry.width() * 0.68)))
        height = min(630, max(500, int(geometry.height() * 0.66)))
        self.resize(width, height)
        x = geometry.left() + (geometry.width() - width) // 2
        y = geometry.top() + max(34, int(geometry.height() * 0.13))
        self.move(x, y)
        self.search.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        return (time.perf_counter() - started) * 1_000

    def apply_theme(self) -> None:
        dark = self._settings().theme == "dark" or (
            self._settings().theme == "system"
            and QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
        )
        delegate = self.list.itemDelegate()
        if isinstance(delegate, ClipDelegate):
            delegate.set_dark_theme(dark)
        self.setStyleSheet(_style_sheet(dark))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        key_event = event if isinstance(event, QKeyEvent) else None
        if key_event is None:
            return False
        key = key_event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            reverse = key == Qt.Key.Key_Backtab or bool(
                key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            self._cycle_filter(-1 if reverse else 1)
            self.search.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if watched is self.search and QApplication.inputMethod().isVisible():
                return False
            self._send(self.list.currentIndex().row())
            return True
        if watched is self.search and key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self._move_selection(1 if key == Qt.Key.Key_Down else -1, key_event.modifiers())
            return True
        if (
            watched is self.list
            and key in (Qt.Key.Key_Down, Qt.Key.Key_Up)
            and key_event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        ):
            self._move_selection(1 if key == Qt.Key.Key_Down else -1, key_event.modifiers())
            return True
        if watched is self.list and key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._request_delete_selected()
            return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and not self.isActiveWindow()
            and self.isVisible()
            and not self._keep_open
            and self._settings().hide_on_deactivate
        ):
            QTimer.singleShot(35, self._hide_if_unfocused)

    def _hide_if_unfocused(self) -> None:
        if not self.isActiveWindow() and QApplication.activeModalWidget() is None and not self._keep_open:
            self.hide()

    def keep_open(self, value: bool) -> None:
        self._keep_open = value

    def _filter(self, kind: ClipKind | None, active: QToolButton) -> None:
        for index, (button, _button_kind) in enumerate(self._filter_buttons):
            button.setChecked(button is active)
            if button is active:
                self._filter_index = index
        self._kind = kind
        self._refresh_results()

    def _cycle_filter(self, direction: int) -> None:
        self._filter_index = (self._filter_index + direction) % len(self._filter_buttons)
        button, kind = self._filter_buttons[self._filter_index]
        self._filter(kind, button)

    def _refresh_results(self) -> None:
        started = time.perf_counter()
        results = self._engine.rank(self.search.text(), now=time.time(), kind=self._kind, limit=500)
        self.model.replace([result.item for result in results])
        self.count_label.setText(f"{len(results)} 条")
        if results:
            self.list.setCurrentIndex(self.model.index(0))
            self._selection_anchor = 0
            self._show_detail(0)
        else:
            self.list.setCurrentIndex(QModelIndex())
            self._show_detail(-1)
        elapsed = (time.perf_counter() - started) * 1_000
        if elapsed > 20:
            LOGGER.warning("Search paint preparation took %.1f ms", elapsed)

    def _show_detail(self, row: int) -> None:
        item = self.model.item_at(row)
        if item is None:
            self.text_preview.setPlainText("")
            self.preview_stack.setCurrentWidget(self.text_preview)
            self.info_type_value.setText("—")
            self.info_detail_label.setText("内容")
            self.info_detail_value.setText("—")
            return
        self.info_type_value.setText({ClipKind.TEXT: "文本", ClipKind.IMAGE: "图片", ClipKind.FILES: "文件"}[item.kind])
        image_path = item.image_path if item.kind is ClipKind.IMAGE else _single_image_file_path(item.files)
        if image_path:
            self.image_preview.set_path(image_path)
            self.preview_stack.setCurrentWidget(self.image_preview)
        elif item.kind is ClipKind.FILES:
            file_text = _read_text_file_preview(item.files)
            if file_text is not None:
                self.file_text_preview.setPlainText(file_text)
                self.file_text_preview.moveCursor(self.file_text_preview.textCursor().MoveOperation.Start)
                self.preview_stack.setCurrentWidget(self.file_text_preview)
            else:
                icon = QFileIconProvider().icon(QFileInfo(item.files[0])) if item.files else QIcon()
                self.file_preview.setPixmap(icon.pixmap(160, 160))
                self.preview_stack.setCurrentWidget(self.file_preview)
        else:
            self.text_preview.setPlainText(item.text)
            self.preview_stack.setCurrentWidget(self.text_preview)
        if item.kind is ClipKind.FILES:
            self.info_detail_label.setText("路径")
            self.info_detail_value.setText("\n".join(item.files) or "—")
        elif item.kind is ClipKind.IMAGE:
            self.info_detail_label.setText("图片大小")
            self.info_detail_value.setText(format_bytes(item.byte_size))
        else:
            self.info_detail_label.setText("字数")
            self.info_detail_value.setText(f"{len(item.text)} 字")

    def _send(self, row: int) -> None:
        item = self.model.item_at(row)
        if item is not None:
            self.send_requested.emit(item)

    def _move_selection(self, step: int, modifiers: Qt.KeyboardModifier) -> None:
        count = self.model.rowCount()
        if not count:
            return
        current = self.list.currentIndex().row()
        target_row = max(0, min(count - 1, (current if current >= 0 else 0) + step))
        target = self.model.index(target_row)
        selection_model = self.list.selectionModel()
        additive = bool(modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        extending = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if extending:
            top, bottom = sorted((self._selection_anchor, target_row))
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                if additive
                else QItemSelectionModel.SelectionFlag.ClearAndSelect
            )
            selection_model.select(
                QItemSelection(self.model.index(top), self.model.index(bottom)),
                flags | QItemSelectionModel.SelectionFlag.Rows,
            )
        elif additive:
            selection_model.select(
                target,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
            self._selection_anchor = target_row
        else:
            selection_model.select(
                target,
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )
            self._selection_anchor = target_row
        selection_model.setCurrentIndex(target, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _selected_items(self) -> tuple[ClipItem, ...]:
        rows = sorted(index.row() for index in self.list.selectionModel().selectedRows())
        return tuple(item for row in rows if (item := self.model.item_at(row)) is not None)

    def _request_delete_selected(self) -> None:
        items = self._selected_items()
        if items:
            self.delete_requested.emit(items)

    def _open_list_menu(self, position) -> None:
        index = self.list.indexAt(position)
        if index.isValid() and not self.list.selectionModel().isSelected(index):
            self.list.setCurrentIndex(index)
        menu = QMenu(self.list)
        delete_action = menu.addAction("删除所选")
        delete_action.setEnabled(bool(self._selected_items()))
        menu.addSeparator()
        clear_action = menu.addAction("清空历史")
        clear_action.setEnabled(self.model.rowCount() > 0)
        _compact_menu(menu)
        selected = menu.exec(self.list.viewport().mapToGlobal(position))
        if selected is delete_action:
            self._request_delete_selected()
        elif selected is clear_action:
            answer = QMessageBox.question(self, "清空历史", "清空全部剪贴板历史？此操作无法撤销。")
            if answer == QMessageBox.StandardButton.Yes:
                self.clear_requested.emit()


def create_tray_icon(parent: QWidget) -> tuple[QSystemTrayIcon, QMenu, dict[str, QAction]]:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#5B6CFF"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(5, 5, 54, 54, 16, 16)
    painter.setPen(QPen(QColor("white"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(20, 23, 44, 23)
    painter.drawLine(20, 32, 38, 32)
    painter.drawLine(20, 41, 33, 41)
    painter.end()
    tray = QSystemTrayIcon(QIcon(pixmap), parent)
    tray.setToolTip("ClipSoon")
    menu = QMenu()
    actions = {
        "show": menu.addAction("显示 ClipSoon"),
        "pause": menu.addAction("暂停记录"),
        "settings": menu.addAction("设置…"),
    }
    actions["pause"].setCheckable(True)
    menu.addSeparator()
    actions["quit"] = menu.addAction("退出")
    tray.setContextMenu(menu)
    return tray, menu, actions


def _style_sheet(dark: bool) -> str:
    bg = "rgba(28, 29, 36, 248)" if dark else "rgba(249, 250, 253, 250)"
    panel = "#242630" if dark else "#F1F3F9"
    text = "#F7F7FA" if dark else "#161821"
    muted = "#A6A9B7" if dark else "#707586"
    border = "rgba(255,255,255,24)" if dark else "rgba(46,52,76,26)"
    input_bg = "#343743" if dark else "#EAECF4"
    return f"""
        QWidget {{ color: {text}; font-size: 13px; }}
        #card {{ background: {bg}; border: 1px solid {border}; border-radius: 20px; }}
        #searchBox {{ background: transparent; border: 2px solid #6574FF; border-radius: 10px; }}
        #search {{ background: transparent; border: none; font-size: 22px; padding: 4px 2px; }}
        QToolButton {{ border: none; border-radius: 9px; padding: 7px 10px; background: transparent; }}
        QToolButton:hover {{ background: {input_bg}; }}
        QToolButton[filterChip="true"] {{ color: {muted}; padding: 5px 12px; }}
        QToolButton[filterChip="true"]:checked {{ color: white; background: #5B6CFF; }}
        #historyList {{ background: transparent; border: none; outline: none; }}
        #detail {{ background: {panel}; border: 1px solid {border}; border-radius: 15px; }}
        #informationTitle {{ font-size: 15px; font-weight: 650; padding-top: 6px; }}
        #informationLabel {{ color: {muted}; font-size: 12px; }}
        #informationValue {{ font-size: 12px; }}
        #muted {{ color: {muted}; font-size: 12px; }}
        #muted a {{ color: #6574FF; text-decoration: none; }}
        #platformNote {{ background: {input_bg}; border: 1px solid {border}; border-radius: 10px; }}
        #dialogTitle {{ font-size: 22px; font-weight: 650; }}
        QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {{
            background: {input_bg}; border: 1px solid {border}; border-radius: 9px; padding: 7px;
        }}
        QPlainTextEdit {{ selection-background-color: #5B6CFF; }}
        QPushButton {{ background: {input_bg}; border: 1px solid {border}; border-radius: 8px; padding: 7px 12px; }}
        QPushButton:hover {{ border-color: #7A86FF; }}
        QDialog {{ background: {bg}; }}
    """


def _single_image_file_path(files: Sequence[str]) -> str:
    if len(files) != 1:
        return ""
    path = files[0]
    return path if Path(path).suffix.casefold() in _IMAGE_FILE_SUFFIXES else ""


def _read_text_file_preview(files: Sequence[str]) -> str | None:
    if len(files) != 1:
        return None
    path = Path(files[0])
    if not path.is_file():
        return None
    known_text = (
        path.suffix.casefold() in _TEXT_FILE_SUFFIXES
        or path.name.casefold() in _TEXT_FILE_NAMES
        or (path.name.startswith(".") and not path.suffix)
    )
    try:
        with path.open("rb") as source:
            payload = source.read(_TEXT_FILE_PREVIEW_BYTES + 1)
    except OSError:
        return None
    truncated = len(payload) > _TEXT_FILE_PREVIEW_BYTES
    payload = payload[:_TEXT_FILE_PREVIEW_BYTES]
    if not payload:
        return ""
    utf16 = payload.startswith((b"\xff\xfe", b"\xfe\xff"))
    if b"\x00" in payload and not utf16:
        return None
    allowed_controls = {8, 9, 10, 12, 13}
    control_count = sum(byte < 32 and byte not in allowed_controls for byte in payload)
    if control_count / len(payload) > 0.02:
        return None
    encodings = ["utf-16"] if utf16 else ["utf-8-sig", locale.getpreferredencoding(False), "gb18030"]
    text = None
    for encoding in dict.fromkeys(encodings):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError as error:
            if truncated and error.end == len(payload):
                text = payload[: error.start].decode(encoding)
                break
        except LookupError:
            continue
    if text is None:
        return None
    if not known_text:
        printable = sum(character.isprintable() or character in "\n\r\t" for character in text)
        if printable / max(1, len(text)) < 0.9:
            return None
    if len(text) > _TEXT_FILE_PREVIEW_CHARS:
        text = text[:_TEXT_FILE_PREVIEW_CHARS]
        truncated = True
    return text + ("\n..." if truncated else "")


def _compact_menu(menu: QMenu) -> None:
    dark = menu.palette().color(QPalette.ColorRole.Window).lightness() < 128
    hover_background = "rgba(255,255,255,28)" if dark else "#E7EAF1"
    menu.setStyleSheet(
        "QMenu { padding: 2px; }"
        "QMenu::item { padding: 5px 7px; border-radius: 4px; }"
        f"QMenu::item:selected {{ background: {hover_background}; }}"
        "QMenu::separator { height: 1px; margin: 2px 4px; }"
    )
    text_width = max(
        (menu.fontMetrics().horizontalAdvance(action.text()) for action in menu.actions() if not action.isSeparator()),
        default=0,
    )
    menu.setFixedWidth(max(68, text_width + 18))


def _hover_color(dark: bool) -> QColor:
    return QColor(255, 255, 255, 24) if dark else QColor("#E7EAF1")


def _elide(painter: QPainter, text: str, width: int) -> str:
    return painter.fontMetrics().elidedText(text.replace("\n", " "), Qt.TextElideMode.ElideRight, width)


def _spin(value: int, minimum: int, maximum: int, suffix: str) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    spin.setSuffix(suffix)
    return spin


def _parse_hotkey(text: str) -> str:
    aliases = {"control": "ctrl", "cmd": "meta", "command": "meta", "win": "meta", "option": "alt"}
    parts: list[str] = []
    for part in text.split("+"):
        raw = part.strip().casefold()
        if not raw:
            continue
        # Qt deliberately maps its Ctrl token to physical Command and Meta to
        # physical Control on macOS. The rest of ClipSoon uses physical names.
        if sys.platform == "darwin" and raw in {"ctrl", "meta"}:
            parts.append("meta" if raw == "ctrl" else "ctrl")
        else:
            parts.append(aliases.get(raw, raw))
    modifiers = {"ctrl", "shift", "alt", "meta"}
    if not set(parts) & modifiers or not set(parts) - modifiers:
        return ""
    ordered = [key for key in ("ctrl", "shift", "alt", "meta") if key in parts]
    ordered.extend(key for key in parts if key not in modifiers)
    return "combo:" + "+".join(dict.fromkeys(ordered))


def _hotkey_display(spec: str) -> str:
    values = spec.removeprefix("combo:").split("+")
    labels = (
        {"ctrl": "Meta", "shift": "Shift", "alt": "Alt", "meta": "Ctrl"}
        if sys.platform == "darwin"
        else {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "meta": "Meta"}
    )
    return "+".join(labels.get(value, value.upper()) for value in values)
