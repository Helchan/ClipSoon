"""Spotlight-style Qt interface kept in one file for a small, fast codebase."""

from __future__ import annotations

import locale
import logging
import math
import ntpath
import sys
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QFileInfo,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QImageReader,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
    QScreen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileIconProvider,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QPlainTextEdit,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleFactory,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from clipsoon import __version__
from clipsoon.core import (
    FAVORITES_FILTER,
    WINDOWS_DEFAULT_HOTKEY,
    AppSettings,
    ClipItem,
    ClipKind,
    HistoryFilter,
    format_bytes,
)
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
_STATUS_TIMEOUT_MS = 4_000
_THUMBNAIL_CACHE_BYTES = 32 * 1024 * 1024
_LIST_ROW_HEIGHT = 44
_LIST_THUMBNAIL_SIZE = 30
_DETAIL_CACHE_BYTES = 64 * 1024 * 1024
_DETAIL_CACHE_ENTRIES = 12
_IMAGE_VIEWER_CACHE_BYTES = 128 * 1024 * 1024
_IMAGE_VIEWER_CACHE_ENTRIES = 3
_IMAGE_VIEWER_MAX_DECODE_BYTES = 128 * 1024 * 1024
_IMAGE_VIEWER_MIN_ZOOM = 0.05
_IMAGE_VIEWER_MAX_ZOOM = 8.0
_IMAGE_VIEWER_MINIMUM_SIZE = QSize(180, 140)
_IMAGE_VIEWER_WINDOW_MARGIN = 96
_IMAGE_VIEWER_FRAME_MARGIN = 0
_IMAGE_VIEWER_RESIZE_MARGIN = 8
_PREVIEW_SIZE_BUCKET = 64
_FILE_REVISION_TTL_SECONDS = 1.0


@dataclass(frozen=True)
class _UiMetrics:
    root_font_size_pt: int
    search_font_size_pt: int
    content_font_size_pt: int
    empty_message_font_size_pt: int
    muted_font_size_pt: int
    thumbnail_letter_font_size_pt: int
    popup_item_font_size_pt: int
    settings_title_font_size_pt: int
    settings_section_font_size_pt: int
    settings_control_font_size_pt: int
    settings_label_font_size_pt: int
    settings_help_font_size_pt: int
    settings_checkbox_indicator_size: int
    settings_control_min_height: int
    settings_label_column_width: int
    settings_form_column_gap: int
    settings_checkbox_row_height: int
    list_row_height: int
    list_thumbnail_size: int
    list_min_width: int
    search_icon_size: int
    settings_window_width: int
    panel_min_width: int
    panel_min_height: int
    panel_initial_width: int
    panel_initial_height: int
    panel_max_width: int
    panel_max_height: int
    panel_width_ratio: float
    panel_height_ratio: float
    filter_chip_vertical_padding: int
    filter_chip_horizontal_padding: int
    text_preview_padding: int
    popup_item_min_height: int


@dataclass(frozen=True)
class _ThemeAppearance:
    """Resolved theme with the app-owned frosted material state."""

    dark: bool
    frosted: bool = False


@dataclass(frozen=True)
class _ThemeColors:
    """Semantic colors shared by Qt stylesheets and custom-painted controls."""

    window: str
    card: str
    panel: str
    control: str
    text: str
    muted: str
    border: str
    accent: str
    accent_focus: str
    hover: str
    thumbnail: str
    popup: str
    menu: str
    menu_hover: str
    menu_separator: str


@dataclass(frozen=True)
class _ListOperationContext:
    selected_ids: tuple[str, ...]
    current_id: str | None
    fallback_row: int
    scroll_value: int


_LIGHT_COLORS = _ThemeColors(
    window="#F7F8FC",
    card="rgba(248, 249, 253, 250)",
    panel="#F0F2F8",
    control="#E9ECF4",
    text="#171A24",
    muted="#62697A",
    border="rgba(45, 53, 76, 32)",
    accent="#5264E8",
    accent_focus="#6677F5",
    hover="#E3E8F4",
    thumbnail="#E7EBF5",
    popup="#EEF2F8",
    menu="#F8F9FD",
    menu_hover="#E0E6F3",
    menu_separator="#D5DAE5",
)
_DARK_COLORS = _ThemeColors(
    window="#1C1F27",
    card="rgba(29, 32, 40, 248)",
    panel="#252934",
    control="#303541",
    text="#F2F4F8",
    muted="#B1B7C6",
    border="rgba(255, 255, 255, 28)",
    accent="#5264E8",
    accent_focus="#7180F5",
    hover="#3A4050",
    thumbnail="#343A48",
    popup="#292D39",
    menu="#2A2F3B",
    menu_hover="#444B5C",
    menu_separator="#454C5C",
)

_SETTINGS_THEME_OPTIONS: tuple[tuple[str, str], ...] = (
    ("磨砂", "frosted"),
    ("浅色", "light"),
    ("深色", "dark"),
)
_SETTINGS_THEME_KEYS = {key for _, key in _SETTINGS_THEME_OPTIONS}
_SETTINGS_SHIELD_BLUR_DOWNSAMPLE = 96
_SETTINGS_SHIELD_LIGHT_VEIL = "#D8DBDF"
_SETTINGS_SHIELD_DARK_VEIL = "#0F1724"
_SETTINGS_SHIELD_LIGHT_VEIL_ALPHA = 230
_SETTINGS_SHIELD_DARK_VEIL_ALPHA = 234
_SETTINGS_SHIELD_LIGHT_GLOW_ALPHA = 16
_SETTINGS_SHIELD_DARK_GLOW_ALPHA = 14

_FROSTED_LIGHT_COLORS = _ThemeColors(
    window="#EEF4FF",
    card="rgba(247, 251, 255, 248)",
    panel="rgba(225, 236, 253, 244)",
    control="rgba(255, 255, 255, 238)",
    text="#142039",
    muted="#53637A",
    border="rgba(58, 87, 134, 72)",
    accent="#2C63D9",
    accent_focus="#3D78EE",
    hover="#DCE9FC",
    thumbnail="#D6E4FA",
    popup="#EAF2FA",
    menu="#F7FAFF",
    menu_hover="#DCE9FC",
    menu_separator="#C8D7EF",
)
_FROSTED_DARK_COLORS = _ThemeColors(
    window="#17202E",
    card="rgba(24, 34, 50, 248)",
    panel="rgba(34, 47, 68, 244)",
    control="rgba(42, 57, 80, 240)",
    text="#F2F7FF",
    muted="#B5C4D8",
    border="rgba(190, 215, 255, 48)",
    accent="#6B9DFF",
    accent_focus="#83AEFF",
    hover="#354967",
    thumbnail="#3B5273",
    popup="#243249",
    menu="#26364E",
    menu_hover="#3D5475",
    menu_separator="#4A6388",
)


def _theme_appearance(settings: AppSettings) -> _ThemeAppearance:
    theme = _settings_theme_key(settings.theme)
    return _ThemeAppearance(dark=theme == "dark", frosted=theme == "frosted")


def _as_theme_appearance(value: bool | _ThemeAppearance) -> _ThemeAppearance:
    return value if isinstance(value, _ThemeAppearance) else _ThemeAppearance(dark=bool(value))


def _theme_colors(theme: bool | _ThemeAppearance) -> _ThemeColors:
    """Return semantic tokens while keeping bool callers backward compatible."""
    appearance = _as_theme_appearance(theme)
    if not appearance.frosted:
        return _DARK_COLORS if appearance.dark else _LIGHT_COLORS
    return _FROSTED_DARK_COLORS if appearance.dark else _FROSTED_LIGHT_COLORS


def _settings_theme_key(theme: str) -> str:
    return theme if theme in _SETTINGS_THEME_KEYS else "frosted"


def _surface_divider_token(appearance: _ThemeAppearance) -> tuple[str, QColor]:
    """Return the shared quiet divider in QSS and painter forms."""

    if appearance.dark:
        return "rgba(224, 238, 255, 46)", QColor(224, 238, 255, 46)
    return "rgba(35, 65, 98, 38)", QColor(35, 65, 98, 38)


def _settings_control_border_token(appearance: _ThemeAppearance) -> tuple[str, QColor]:
    """Return an idle settings-control edge without changing standard themes."""

    if appearance.frosted:
        return _surface_divider_token(appearance)
    token = _theme_colors(appearance).border
    return token, QColor(token)


def _active_foreground(appearance: _ThemeAppearance) -> str:
    """Return the readable foreground for an active semantic surface.

    Frosted active fills stay intentionally luminous in both appearances.
    White text falls below readable contrast on those translucent blues, so
    every frosted active state shares one deep navy foreground instead.
    """

    return "#0A192F" if appearance.frosted else "#FFFFFF"


def _accent_foreground(appearance: _ThemeAppearance) -> str:
    """Return the readable foreground for a solid accent control.

    This is intentionally distinct from :func:`_active_foreground`: list
    selections and filter chips are translucent, luminous surfaces, whereas
    a combo popup or primary button uses the opaque accent token itself.  The
    light frosted accent needs white text; its darker counterpart needs the
    same deep navy used by the frosted active surface.
    """

    return "#0A192F" if appearance.frosted and appearance.dark else "#FFFFFF"


_APP_OWNED_CARET_PROPERTY = "_clipsoon_app_owned_caret"
_APP_OWNED_CARET_STYLE: _AppOwnedCaretStyle | None = None
_COMPACT_MENU_PROPERTY = "_clipsoon_compact_menu"
_COMPACT_MENU_HAS_ICONS_PROPERTY = "_clipsoon_compact_menu_has_icons"
_COMPACT_MENU_TEXT_COLOR_PROPERTY = "_clipsoon_compact_menu_text_color"
_COMPACT_MENU_SHORTCUT_COLOR_PROPERTY = "_clipsoon_compact_menu_shortcut_color"
_COMPACT_MENU_DISABLED_TEXT_COLOR_PROPERTY = "_clipsoon_compact_menu_disabled_text_color"
_COMPACT_MENU_ACTION_SHORTCUT_PROPERTY = "_clipsoon_compact_menu_action_shortcut"
_COMPACT_MENU_ICON_SIZE = 14
_COMPACT_MENU_ICON_GAP = 3
_COMPACT_MENU_ICON_LEFT_OFFSET = 10
_COMPACT_MENU_SHELL_HORIZONTAL_INSET = 0
_COMPACT_MENU_ITEM_HORIZONTAL_MARGIN = 6
_COMPACT_MENU_ITEM_HORIZONTAL_PADDING = 8
_COMPACT_MENU_VERTICAL_INSET = 5
_COMPACT_MENU_CORNER_RADIUS = 10
_POPUP_ITEM_FONT_SIZE_PT = 12
_SETTINGS_TITLE_FONT_SIZE_PT = 13
_SETTINGS_SECTION_FONT_SIZE_PT = 12
_SETTINGS_CONTROL_FONT_SIZE_PT = 12
_SETTINGS_LABEL_FONT_SIZE_PT = 11
_SETTINGS_HELP_FONT_SIZE_PT = 10
_SETTINGS_CHECKBOX_INDICATOR_SIZE = 13
_SETTINGS_CONTROL_MIN_HEIGHT = 34
_SETTINGS_LABEL_COLUMN_WIDTH = 112
_SETTINGS_FORM_COLUMN_GAP = 14
_SETTINGS_CHECKBOX_ROW_HEIGHT = 26
_SETTINGS_EXTERNAL_DISMISS_POLL_MS = 25
_DEFAULT_UI_METRICS = _UiMetrics(
    root_font_size_pt=10,
    search_font_size_pt=16,
    content_font_size_pt=13,
    empty_message_font_size_pt=10,
    muted_font_size_pt=9,
    thumbnail_letter_font_size_pt=16,
    popup_item_font_size_pt=_POPUP_ITEM_FONT_SIZE_PT,
    settings_title_font_size_pt=_SETTINGS_TITLE_FONT_SIZE_PT,
    settings_section_font_size_pt=_SETTINGS_SECTION_FONT_SIZE_PT,
    settings_control_font_size_pt=_SETTINGS_CONTROL_FONT_SIZE_PT,
    settings_label_font_size_pt=_SETTINGS_LABEL_FONT_SIZE_PT,
    settings_help_font_size_pt=_SETTINGS_HELP_FONT_SIZE_PT,
    settings_checkbox_indicator_size=_SETTINGS_CHECKBOX_INDICATOR_SIZE,
    settings_control_min_height=_SETTINGS_CONTROL_MIN_HEIGHT,
    settings_label_column_width=_SETTINGS_LABEL_COLUMN_WIDTH,
    settings_form_column_gap=_SETTINGS_FORM_COLUMN_GAP,
    settings_checkbox_row_height=_SETTINGS_CHECKBOX_ROW_HEIGHT,
    list_row_height=_LIST_ROW_HEIGHT,
    list_thumbnail_size=_LIST_THUMBNAIL_SIZE,
    list_min_width=410,
    search_icon_size=30,
    settings_window_width=580,
    panel_min_width=720,
    panel_min_height=500,
    panel_initial_width=900,
    panel_initial_height=610,
    panel_max_width=920,
    panel_max_height=630,
    panel_width_ratio=0.68,
    panel_height_ratio=0.66,
    filter_chip_vertical_padding=5,
    filter_chip_horizontal_padding=12,
    text_preview_padding=11,
    popup_item_min_height=30,
)
_WINDOWS_UI_METRICS = _UiMetrics(
    root_font_size_pt=9,
    search_font_size_pt=13,
    content_font_size_pt=10,
    empty_message_font_size_pt=9,
    muted_font_size_pt=8,
    thumbnail_letter_font_size_pt=13,
    popup_item_font_size_pt=10,
    settings_title_font_size_pt=11,
    settings_section_font_size_pt=10,
    settings_control_font_size_pt=10,
    settings_label_font_size_pt=9,
    settings_help_font_size_pt=9,
    settings_checkbox_indicator_size=12,
    settings_control_min_height=30,
    settings_label_column_width=100,
    settings_form_column_gap=12,
    settings_checkbox_row_height=24,
    list_row_height=38,
    list_thumbnail_size=26,
    list_min_width=350,
    search_icon_size=26,
    settings_window_width=520,
    panel_min_width=620,
    panel_min_height=420,
    panel_initial_width=780,
    panel_initial_height=520,
    panel_max_width=840,
    panel_max_height=560,
    panel_width_ratio=0.60,
    panel_height_ratio=0.58,
    filter_chip_vertical_padding=4,
    filter_chip_horizontal_padding=9,
    text_preview_padding=9,
    popup_item_min_height=26,
)


def _ui_metrics() -> _UiMetrics:
    return _WINDOWS_UI_METRICS if sys.platform == "win32" else _DEFAULT_UI_METRICS


def _platform_font_family_rule() -> str:
    if sys.platform != "win32":
        return ""
    return 'font-family: "Segoe UI", "Microsoft YaHei UI"; '


def _apply_platform_font_family(font: QFont) -> QFont:
    if sys.platform == "win32":
        font.setFamily("Segoe UI")
    return font


class _AppOwnedCaretStyle(QProxyStyle):
    """Suppress Qt's platform caret only for editors owned by ClipSoon.

    macOS can render a white native insertion caret even after the QLineEdit
    text palette has been updated.  Returning a zero cursor width is a public
    Qt paint metric: editing, selection, undo and IME handling stay native,
    while the app can draw one predictable themed caret above the editor.
    """

    def pixelMetric(self, metric, option=None, widget=None) -> int:
        if (
            metric == QStyle.PixelMetric.PM_SmallIconSize
            and isinstance(widget, QMenu)
            and bool(widget.property(_COMPACT_MENU_HAS_ICONS_PROPERTY))
        ):
            # QMenu normally reserves a platform-sized icon gutter. The
            # contextual menu has a deliberately smaller 14 px icon column,
            # so the symbol and label read as one compact action while the
            # outer whitespace remains symmetric.
            return _COMPACT_MENU_ICON_SIZE
        if (
            metric == QStyle.PixelMetric.PM_TextCursorWidth
            and isinstance(widget, QLineEdit)
            and bool(widget.property(_APP_OWNED_CARET_PROPERTY))
        ):
            return 0
        return super().pixelMetric(metric, option, widget)


class _CompactMenuFrameFilter(QObject):
    """Keep custom context menus physically rounded, not only stylesheet-rounded."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QMenu)
            and bool(watched.property(_COMPACT_MENU_PROPERTY))
            and event.type() in {QEvent.Type.Show, QEvent.Type.Resize}
        ):
            _balance_compact_menu_action_margins(watched)
            _apply_compact_menu_mask(watched)
        return super().eventFilter(watched, event)


class _CompactMenu(QMenu):
    """QMenu variant with a muted, right-aligned shortcut column."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        shortcut_actions = [
            action
            for action in self.actions()
            if not action.isSeparator() and action.isVisible() and _action_shortcut_text(action)
        ]
        if not shortcut_actions:
            return
        shortcut_color = QColor(
            str(self.property(_COMPACT_MENU_SHORTCUT_COLOR_PROPERTY) or _theme_colors(False).muted)
        )
        disabled_color = QColor(
            str(self.property(_COMPACT_MENU_DISABLED_TEXT_COLOR_PROPERTY) or shortcut_color.name())
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self.font())
        for action in shortcut_actions:
            rect = self.actionGeometry(action)
            if rect.isEmpty():
                continue
            shortcut_rect = QRect(
                rect.left(),
                rect.top(),
                rect.width() - _COMPACT_MENU_ITEM_HORIZONTAL_PADDING,
                rect.height(),
            )
            painter.setPen(shortcut_color if action.isEnabled() else disabled_color)
            painter.drawText(
                shortcut_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                _action_shortcut_text(action),
            )
        painter.end()


def _apply_compact_menu_mask(menu: QMenu) -> None:
    rect = menu.rect()
    if rect.isEmpty():
        return
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), _COMPACT_MENU_CORNER_RADIUS, _COMPACT_MENU_CORNER_RADIUS)
    menu.setMask(QRegion(path.toFillPolygon().toPolygon()))


def _balance_compact_menu_action_margins(menu: QMenu) -> None:
    """Compensate Qt's native action geometry so hover pills sit visually centered."""

    action = next((candidate for candidate in menu.actions() if not candidate.isSeparator()), None)
    if action is None:
        return
    rect = menu.actionGeometry(action)
    if rect.isEmpty():
        return
    left_margin = rect.x()
    right_margin = menu.width() - rect.x() - rect.width()
    if left_margin <= right_margin:
        return
    menu.setFixedWidth(menu.width() + left_margin - right_margin)


def _install_app_owned_caret_style(qt_app: QApplication | None = None) -> None:
    """Install one application proxy before widgets receive their QSS.

    A per-widget QProxyStyle is unreliable below QStyleSheetStyle and can
    transfer ownership of a private QSpinBox editor's style.  An app-level
    proxy with a cloned base style avoids both problems and is a no-op for all
    controls except explicitly marked QLineEdits.
    """

    global _APP_OWNED_CARET_STYLE
    application = qt_app if qt_app is not None else QApplication.instance()
    if not isinstance(application, QApplication):
        return
    if isinstance(application.style(), _AppOwnedCaretStyle):
        _APP_OWNED_CARET_STYLE = application.style()
        return

    base_style = QStyleFactory.create(application.style().objectName())
    style = _AppOwnedCaretStyle(base_style) if base_style is not None else _AppOwnedCaretStyle()
    application.setStyle(style)
    _APP_OWNED_CARET_STYLE = style


def _apply_text_input_palette(
    editor: QLineEdit,
    colors: _ThemeColors,
    appearance: _ThemeAppearance,
) -> None:
    """Apply the semantic text palette after Qt has polished the input QSS.

    Applying these values to the actual QLineEdit (rather than only to a
    parent QSpinBox/QKeySequenceEdit) keeps every editable field readable on
    a translucent backing store.  The application-owned caret below uses the
    same text token rather than trusting the platform's native white caret.
    """

    palette = editor.palette()
    palette.setColor(QPalette.ColorRole.Text, QColor(colors.text))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors.muted))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.accent))
    palette.setColor(
        QPalette.ColorRole.HighlightedText,
        QColor(_accent_foreground(appearance)),
    )
    editor.setPalette(palette)


class _ThemedCaretOverlay(QWidget):
    """A tiny non-interactive caret painted over a native QLineEdit."""

    _WIDTH = 2

    def __init__(self, editor: QLineEdit) -> None:
        super().__init__(editor)
        self._color = QColor("#10203A")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def set_color(self, color: QColor) -> None:
        color = QColor(color)
        if color == self._color:
            return
        self._color = color
        self.update()

    def follow_cursor(self, cursor: QRect) -> None:
        # QLineEdit.cursorRect() reserves a wide blink area. Its centre stays
        # on the actual insertion position even when the native width is zero.
        self.setGeometry(
            cursor.center().x(),
            cursor.y(),
            self._WIDTH,
            max(1, cursor.height()),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._color)


class _ThemedTextCaret(QObject):
    """Keep a dark, genuinely blinking caret without changing native editing."""

    _REFRESH_EVENTS = {
        QEvent.Type.FocusIn,
        QEvent.Type.FocusOut,
        QEvent.Type.Show,
        QEvent.Type.Hide,
        QEvent.Type.Move,
        QEvent.Type.Resize,
        QEvent.Type.FontChange,
        QEvent.Type.StyleChange,
        QEvent.Type.PaletteChange,
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.WindowActivate,
        QEvent.Type.WindowDeactivate,
    }

    def __init__(self, editor: QLineEdit, *, focus_owner: QWidget | None = None) -> None:
        super().__init__(editor)
        self._editor = editor
        self._focus_owner = focus_owner if focus_owner is not None else editor
        self._window = editor.window()
        self._ime_composing = False
        self._phase_visible = True
        self._overlay = _ThemedCaretOverlay(editor)
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._advance_phase)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._refresh)
        _install_app_owned_caret_style()

        # The global proxy sees this marker while QLineEdit paints.  That
        # removes the native white caret for both visible and hidden phases.
        editor.setProperty(_APP_OWNED_CARET_PROPERTY, True)
        editor.installEventFilter(self)
        if self._focus_owner is not editor:
            self._focus_owner.installEventFilter(self)
        if self._window is not editor and self._window is not self._focus_owner:
            self._window.installEventFilter(self)
        editor.cursorPositionChanged.connect(self._restart_blink)
        editor.selectionChanged.connect(self._restart_blink)
        editor.textChanged.connect(self._restart_blink)
        flash_changed = getattr(QApplication.styleHints(), "cursorFlashTimeChanged", None)
        if flash_changed is not None:
            flash_changed.connect(self._restart_blink)
        self._restart_blink()

    @property
    def overlay(self) -> _ThemedCaretOverlay:
        return self._overlay

    def set_color(self, color: QColor) -> None:
        self._overlay.set_color(color)
        self._schedule_refresh()

    def _restart_blink(self, *_args: object) -> None:
        self._phase_visible = True
        flash_time = QApplication.styleHints().cursorFlashTime()
        if flash_time >= 2:
            self._blink_timer.start(max(1, flash_time // 2))
        else:
            self._blink_timer.stop()
        self._schedule_refresh()

    def _advance_phase(self) -> None:
        self._phase_visible = not self._phase_visible
        self._refresh()

    def _schedule_refresh(self) -> None:
        if not self._sync_timer.isActive():
            self._sync_timer.start(0)

    def _should_draw(self) -> bool:
        editor = getattr(self, "_editor", None)
        focus_owner = getattr(self, "_focus_owner", None)
        window = getattr(self, "_window", None)
        if editor is None or focus_owner is None or window is None:
            return False
        if (
            self._ime_composing
            or not editor.isVisible()
            or not editor.isEnabled()
            or editor.isReadOnly()
            or not (editor.hasFocus() or focus_owner.hasFocus())
        ):
            return False
        return window.isActiveWindow()

    def _refresh(self) -> None:
        if not self._should_draw():
            self._overlay.hide()
            return
        self._overlay.follow_cursor(self._editor.cursorRect())
        if self._phase_visible:
            self._overlay.show()
            self._overlay.raise_()
        else:
            self._overlay.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        editor = getattr(self, "_editor", None)
        blink_timer = getattr(self, "_blink_timer", None)
        if editor is None or blink_timer is None:
            return False
        if watched is editor and event.type() == QEvent.Type.InputMethod:
            input_event = event if isinstance(event, QInputMethodEvent) else None
            self._ime_composing = bool(input_event and input_event.preeditString())
            self._restart_blink()
            return False

        if event.type() in self._REFRESH_EVENTS:
            if event.type() in {
                QEvent.Type.FocusOut,
                QEvent.Type.Hide,
                QEvent.Type.WindowDeactivate,
            }:
                self._blink_timer.stop()
                self._phase_visible = True
                self._schedule_refresh()
            else:
                self._restart_blink()
        return False


_FROSTED_RADIUS = 18.0
_SETTINGS_WINDOW_RADIUS = 16.0
_WINDOWS_PANEL_EDGE_GUARD = 2


def _panel_outer_margin() -> int:
    return _WINDOWS_PANEL_EDGE_GUARD if sys.platform == "win32" else 1


def _rounded_widget_path(widget: QWidget, radius: float) -> QPainterPath:
    path = QPainterPath()
    if widget.rect().isEmpty():
        return path
    rect = QRectF(widget.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
    path.addRoundedRect(rect, radius, radius)
    return path


def _apply_rounded_widget_mask(widget: QWidget, radius: float) -> None:
    if widget.rect().isEmpty():
        return
    widget.setMask(QRegion(_rounded_widget_path(widget, radius).toFillPolygon().toPolygon()))


def _paint_frosted_material(
    painter: QPainter,
    rect: QRectF,
    appearance: _ThemeAppearance,
    *,
    light_position: QPointF | None = None,
    light_strength: float = 0.0,
    radius: float = _FROSTED_RADIUS,
) -> None:
    """Paint a self-contained, lens-inspired material without screen capture.

    The scene below the material is deliberately app-owned, so the same depth,
    rim light, and interaction response stay private and predictable across
    macOS, Windows, and Linux.
    """

    if rect.width() <= 2 or rect.height() <= 2:
        return

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    shell = QPainterPath()
    shell.addRoundedRect(rect, radius, radius)
    painter.setClipPath(shell)

    if appearance.dark:
        base = QColor(19, 29, 43, 255)
        top_tint = QColor(85, 141, 220, 72)
        lower_tint = QColor(84, 107, 143, 62)
        warm_tint = QColor(94, 194, 186, 42)
        top_gloss = QColor(238, 248, 255, 42)
        bottom_shade = QColor(2, 8, 20, 94)
        top_field_alpha = 58
        lower_field_alpha = 58
    else:
        # Keep the lower half close to the light macOS screenshot: the material
        # should read as pale frost, not a dense blue overlay.
        base = QColor(239, 247, 252, 255)
        top_tint = QColor(111, 169, 246, 54)
        lower_tint = QColor(208, 225, 238, 24)
        warm_tint = QColor(121, 220, 207, 24)
        top_gloss = QColor(255, 255, 255, 126)
        bottom_shade = QColor(41, 66, 94, 14)
        top_field_alpha = 34
        lower_field_alpha = 22

    painter.fillPath(shell, base)

    # A wide low-frequency colour field provides something meaningful for the
    # translucent lens to bend even when the desktop behind the app is flat.
    ambient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    ambient.setColorAt(0.0, top_tint)
    ambient.setColorAt(0.44, warm_tint)
    ambient.setColorAt(1.0, lower_tint)
    painter.fillPath(shell, QBrush(ambient))

    def fill_radial(center: QPointF, extent: float, center_color: QColor) -> None:
        glow = QRadialGradient(center, extent)
        glow.setColorAt(0.0, center_color)
        edge = QColor(center_color)
        edge.setAlpha(0)
        glow.setColorAt(1.0, edge)
        painter.fillPath(shell, QBrush(glow))

    fill_radial(
        QPointF(rect.left() + rect.width() * 0.15, rect.top() + rect.height() * 0.1),
        max(rect.width(), rect.height()) * 0.68,
        QColor(255, 255, 255, top_field_alpha),
    )
    fill_radial(
        QPointF(rect.right() - rect.width() * 0.08, rect.bottom() - rect.height() * 0.12),
        max(rect.width(), rect.height()) * 0.58,
        QColor(139, 188, 230, lower_field_alpha),
    )

    if light_position is not None and light_strength > 0:
        # The hover response is intentionally local and low-contrast: it
        # should make the surface feel responsive, never read as a cursor-sized
        # spotlight or decorative ring around the user's work.
        bloom = min(rect.width(), rect.height()) * 0.16
        fill_radial(
            light_position,
            max(42.0, bloom),
            QColor(255, 255, 255, int(5 + light_strength * 18)),
        )

    # A clear upper sheen and denser lower edge make the surface read as a
    # thick material rather than a low-opacity card.
    gloss = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.top() + rect.height() * 0.48)
    gloss.setColorAt(0.0, top_gloss)
    gloss.setColorAt(0.46, QColor(top_gloss.red(), top_gloss.green(), top_gloss.blue(), 18))
    gloss.setColorAt(1.0, QColor(top_gloss.red(), top_gloss.green(), top_gloss.blue(), 0))
    painter.fillPath(shell, QBrush(gloss))

    lower_edge = QLinearGradient(rect.left(), rect.bottom() - rect.height() * 0.32, rect.left(), rect.bottom())
    lower_edge.setColorAt(0.0, QColor(bottom_shade.red(), bottom_shade.green(), bottom_shade.blue(), 0))
    lower_edge.setColorAt(1.0, bottom_shade)
    painter.fillPath(shell, QBrush(lower_edge))
    painter.restore()

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(_theme_colors(appearance).border), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(shell)
    painter.restore()


class _FrostedSurface(QFrame):
    """One primary custom-painted material shell for a widget hierarchy."""

    material_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._appearance = _ThemeAppearance(dark=False)
        self._light_position = QPointF()
        self._light_strength = 0.0
        self._paint_material = True
        self._hover_animation = self._create_hover_animation()
        self.setFrameShape(QFrame.Shape.NoFrame)

    def _create_hover_animation(self) -> QVariantAnimation:
        """Create the short-lived pointer sheen animation for this surface."""

        animation = QVariantAnimation(self)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(self._set_light_strength)
        return animation

    def _active_hover_animation(self) -> QVariantAnimation:
        """Return a live animation, recreating one Qt already tore down.

        A theme refresh can arrive while Qt is unwinding a modal window or
        test widget tree.  In that narrow lifecycle window the surface still
        exists but its parent-owned animation may already have been destroyed.
        Recreate the cosmetic animation rather than letting the refresh fail;
        the material remains fully functional.
        """

        try:
            self._hover_animation.state()
        except RuntimeError:
            self._hover_animation = self._create_hover_animation()
        return self._hover_animation

    def set_appearance(self, appearance: _ThemeAppearance) -> None:
        self._appearance = appearance
        if not appearance.frosted:
            # The effect is optional, and a later hover will lazily create a
            # fresh animation if this QObject was torn down.
            with suppress(RuntimeError):
                self._hover_animation.stop()
            self._light_strength = 0.0
        self.update()
        self.material_changed.emit()

    def set_paint_material(self, enabled: bool) -> None:
        if self._paint_material == enabled:
            return
        self._paint_material = enabled
        self.update()
        self.material_changed.emit()

    def set_light_position(self, position: QPointF) -> None:
        if not self._appearance.frosted:
            return
        self._light_position = position
        self.update()
        self.material_changed.emit()

    def set_light_active(self, active: bool) -> None:
        if not self._appearance.frosted:
            return
        animation = self._active_hover_animation()
        target = 1.0 if active else 0.0
        if (
            animation.endValue() == target
            and animation.state() == QVariantAnimation.State.Running
        ):
            return
        animation.stop()
        animation.setStartValue(self._light_strength)
        animation.setEndValue(target)
        animation.setDuration(140 if active else 220)
        animation.start()

    def _set_light_strength(self, value: object) -> None:
        self._light_strength = float(value)
        self.update()
        self.material_changed.emit()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._appearance.frosted or not self._paint_material:
            return
        painter = QPainter(self)
        _paint_frosted_material(
            painter,
            QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6),
            self._appearance,
            light_position=self._light_position,
            light_strength=self._light_strength,
        )


def _soft_blurred_snapshot(pixmap: QPixmap) -> QPixmap:
    """Approximate a frosted background by downsampling then upsampling.

    Qt Widgets does not provide CSS-style background blur for another top-level
    window.  Capturing the panel and blurring that app-owned snapshot gives the
    settings layer the expected disabled-background cue without screen-capture
    permissions or platform-specific APIs.
    """

    if pixmap.isNull():
        return pixmap
    size = pixmap.size()
    small = pixmap.scaled(
        QSize(
            max(1, size.width() // _SETTINGS_SHIELD_BLUR_DOWNSAMPLE),
            max(1, size.height() // _SETTINGS_SHIELD_BLUR_DOWNSAMPLE),
        ),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred = small.scaled(
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred.setDevicePixelRatio(pixmap.devicePixelRatio())
    return blurred


class _PanelInteractionShield(QWidget):
    """Blur and disable the command panel while settings owns interaction."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._appearance = _ThemeAppearance(dark=False)
        self._snapshot = QPixmap()
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def _rounded_path(self) -> QPainterPath:
        return _rounded_widget_path(self, _FROSTED_RADIUS)

    def _apply_rounded_mask(self) -> None:
        _apply_rounded_widget_mask(self, _FROSTED_RADIUS)

    def set_appearance(self, appearance: _ThemeAppearance) -> None:
        self._appearance = appearance
        self.update()

    def refresh_from(self, source: QWidget) -> None:
        was_visible = self.isVisible()
        if was_visible:
            self.hide()
        self._snapshot = _soft_blurred_snapshot(source.grab())
        if was_visible:
            self.show()
            self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipPath(self._rounded_path())
        if not self._snapshot.isNull():
            painter.drawPixmap(self.rect(), self._snapshot)
        colors = _theme_colors(self._appearance)
        veil = QColor(
            _SETTINGS_SHIELD_LIGHT_VEIL if not self._appearance.dark else _SETTINGS_SHIELD_DARK_VEIL
        )
        veil.setAlpha(
            _SETTINGS_SHIELD_LIGHT_VEIL_ALPHA
            if not self._appearance.dark
            else _SETTINGS_SHIELD_DARK_VEIL_ALPHA
        )
        painter.fillRect(self.rect(), veil)
        if self._appearance.frosted:
            highlight = QRadialGradient(
                QPointF(self.rect().center().x(), self.rect().top() + self.height() * 0.18),
                max(1.0, self.width() * 0.42),
            )
            glow = QColor(colors.accent_focus if not self._appearance.dark else colors.accent)
            glow.setAlpha(
                _SETTINGS_SHIELD_LIGHT_GLOW_ALPHA
                if not self._appearance.dark
                else _SETTINGS_SHIELD_DARK_GLOW_ALPHA
            )
            highlight.setColorAt(0.0, glow)
            highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(self.rect(), QBrush(highlight))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_rounded_mask()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_rounded_mask()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def contextMenuEvent(self, event) -> None:
        event.accept()


class _SettingsCheckBox(QCheckBox):
    """A native-behaviour checkbox with an app-owned, legible indicator.

    Qt's platform checkbox indicator can remain stark white when the macOS
    system appearance disagrees with the selected ClipSoon theme.  Retaining
    ``QCheckBox`` preserves keyboard and accessibility semantics; only the
    small visual indicator is repainted with the active theme tokens.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._appearance = _ThemeAppearance(dark=False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

    def set_theme(self, appearance: _ThemeAppearance) -> None:
        if self._appearance != appearance:
            self._appearance = appearance
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            self,
        )
        if not indicator.isValid():
            return

        colors = _theme_colors(self._appearance)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        partially_checked = bool(option.state & QStyle.StateFlag.State_NoChange)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)

        if checked or partially_checked:
            fill = QColor(colors.accent)
            border = QColor(colors.accent_focus if hovered or focused else colors.accent)
        elif self._appearance.frosted:
            _, border = _settings_control_border_token(self._appearance)
            fill = (
                QColor(223, 240, 255, 27)
                if self._appearance.dark
                else QColor(232, 244, 255, 54)
            )
            if hovered:
                fill.setAlpha(min(112, fill.alpha() + 24))
        elif self._appearance.dark:
            fill = QColor("#303541")
            border = QColor("#697184")
        else:
            fill = QColor("#EEF1F7")
            border = QColor("#AEB8CC")

        if focused:
            border = QColor(colors.accent_focus)
        if not enabled:
            fill.setAlpha(max(36, fill.alpha() // 2))
            border.setAlpha(max(64, border.alpha() // 2))

        painter = QPainter(self)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(indicator).adjusted(0.55, 0.55, -0.55, -0.55)
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(bounds, 3.0, 3.0)

        if checked:
            check_color = QColor(_accent_foreground(self._appearance))
            if not enabled:
                check_color.setAlpha(165)
            check_pen = QPen(check_color, 1.55)
            check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(check_pen)
            painter.drawLine(
                QPointF(bounds.left() + bounds.width() * 0.23, bounds.center().y()),
                QPointF(bounds.left() + bounds.width() * 0.43, bounds.bottom() - bounds.height() * 0.25),
            )
            painter.drawLine(
                QPointF(bounds.left() + bounds.width() * 0.43, bounds.bottom() - bounds.height() * 0.25),
                QPointF(bounds.right() - bounds.width() * 0.20, bounds.top() + bounds.height() * 0.27),
            )
        elif partially_checked:
            dash_color = QColor(colors.text)
            dash_color.setAlpha(210 if enabled else 130)
            dash_pen = QPen(dash_color, 1.35)
            dash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(dash_pen)
            painter.drawLine(
                QPointF(bounds.left() + bounds.width() * 0.27, bounds.center().y()),
                QPointF(bounds.right() - bounds.width() * 0.27, bounds.center().y()),
            )
        painter.restore()


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
        self._dark_theme = False
        self._appearance = _ThemeAppearance(dark=False)
        self.setFixedSize(_ui_metrics().search_icon_size, _ui_metrics().search_icon_size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The command panel has one permanent typing target.  Opening settings
        # is a pointer action here, so the small magnifier must never displace
        # the search editor's input caret or create a competing focus state.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("设置")
        self.setAccessibleDescription("打开设置")
        self.setToolTip("打开设置")

    def set_dark_theme(self, dark: bool) -> None:
        self.set_theme(_ThemeAppearance(dark=dark))

    def set_theme(self, appearance: _ThemeAppearance) -> None:
        if self._appearance != appearance:
            self._appearance = appearance
            self._dark_theme = appearance.dark
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = _theme_colors(self._appearance)
        # The magnifier is a compact pointer affordance, not a second typing
        # target. Its color stays stable while the search caret remains the
        # only focus indicator in the active command panel.
        painter.setPen(QPen(QColor(colors.accent), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(4, 3, 18, 18)
        painter.drawLine(19, 19, 26, 26)

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # QListView caches a uniform delegate size.  When the list is first
        # inserted into the empty-state stack, that cache can retain the
        # pre-layout 640 px width and paint selection/hover material beyond
        # the actual viewport.  Re-layout after every viewport resize keeps
        # row geometry flush with the visible pane.
        self.doItemsLayout()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.hover_index_changed.emit(QModelIndex())


ImageLoadKey = tuple[str, int, int, int, int, bool]


class _ByteLruCache[CacheKey, CacheValue]:
    """A small exact-cost LRU with both byte and entry limits."""

    def __init__(self, max_bytes: int, max_entries: int) -> None:
        self.max_bytes = max(0, max_bytes)
        self.max_entries = max(0, max_entries)
        self._entries: OrderedDict[CacheKey, tuple[CacheValue, int]] = OrderedDict()
        self.total_bytes = 0

    def get(self, key: CacheKey) -> CacheValue | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: CacheKey, value: CacheValue, cost: int) -> bool:
        self.remove(key)
        if not self.max_entries or not self.max_bytes:
            return False
        normalized_cost = max(1, cost)
        if normalized_cost > self.max_bytes:
            return False
        self._entries[key] = (value, normalized_cost)
        self.total_bytes += normalized_cost
        while len(self._entries) > self.max_entries or self.total_bytes > self.max_bytes:
            _old_key, (_old_value, old_cost) = self._entries.popitem(last=False)
            self.total_bytes -= old_cost
        return key in self._entries

    def remove(self, key: CacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self.total_bytes -= entry[1]

    def remove_paths(self, paths: set[str]) -> None:
        for key in tuple(self._entries):
            if isinstance(key, tuple) and key and key[0] in paths:
                self.remove(key)

    @property
    def keys(self) -> tuple[CacheKey, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class _ImageLoadSignals(QObject):
    finished = Signal(object, object)


class _ImageLoadTask(QRunnable):
    def __init__(self, key: ImageLoadKey) -> None:
        super().__init__()
        self.key = key
        self.signals = _ImageLoadSignals()

    def run(self) -> None:
        path, _modified_ns, _file_size, width, height, keep_aspect = self.key
        image = _read_scaled_image(path, QSize(width, height), keep_aspect)
        self.signals.finished.emit(self.key, image)


class _ScaledImageLoader(QObject):
    image_ready = Signal(object, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        max_cache_bytes: int,
        max_cache_entries: int,
        priority: int = 0,
    ) -> None:
        super().__init__(parent)
        self._cache = _ByteLruCache[ImageLoadKey, QImage](max_cache_bytes, max_cache_entries)
        self._tasks: dict[ImageLoadKey, _ImageLoadTask] = {}
        self._discarded_tasks: set[ImageLoadKey] = set()
        self._blocked_paths: set[str] = set()
        self._revisions: dict[str, tuple[float, int, int]] = {}
        self._priority = priority

    def key(self, path: str, size: QSize, keep_aspect: bool) -> ImageLoadKey:
        modified_ns, file_size = self._revision(path)
        return (
            path,
            modified_ns,
            file_size,
            max(1, size.width()),
            max(1, size.height()),
            keep_aspect,
        )

    def request(self, path: str, size: QSize, *, keep_aspect: bool) -> QImage | None:
        self._blocked_paths.discard(path)
        key = self.key(path, size, keep_aspect)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if key not in self._tasks:
            task = _ImageLoadTask(key)
            task.signals.finished.connect(self._complete)
            self._tasks[key] = task
            QThreadPool.globalInstance().start(task, self._priority)
        return None

    def _complete(self, key: ImageLoadKey, image: QImage) -> None:
        self._tasks.pop(key, None)
        discarded = key in self._discarded_tasks or key[0] in self._blocked_paths
        self._discarded_tasks.discard(key)
        if not discarded:
            self._cache.put(key, image, _image_cost(image))
        self.image_ready.emit(key, image if not discarded else QImage())

    def retain_only(self, path: str) -> None:
        pool = QThreadPool.globalInstance()
        for key, task in tuple(self._tasks.items()):
            if key[0] == path:
                self._discarded_tasks.discard(key)
                continue
            self._discard_or_take(pool, key, task)

    def invalidate_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        self._blocked_paths.update(paths)
        self._cache.remove_paths(paths)
        for path in paths:
            self._revisions.pop(path, None)
        pool = QThreadPool.globalInstance()
        for key, task in tuple(self._tasks.items()):
            if key[0] not in paths:
                continue
            self._discard_or_take(pool, key, task)

    def _discard_or_take(
        self,
        pool: QThreadPool,
        key: ImageLoadKey,
        task: _ImageLoadTask,
    ) -> None:
        try:
            taken = pool.tryTake(task)
        except RuntimeError:
            self._tasks.pop(key, None)
            self._discarded_tasks.add(key)
            return
        if taken:
            self._tasks.pop(key, None)
        else:
            self._discarded_tasks.add(key)

    def _revision(self, path: str) -> tuple[int, int]:
        now = time.monotonic()
        cached = self._revisions.get(path)
        if cached is not None and now - cached[0] < _FILE_REVISION_TTL_SECONDS:
            return cached[1], cached[2]
        try:
            stat = Path(path).stat()
            revision = stat.st_mtime_ns, stat.st_size
        except OSError:
            revision = -1, -1
        self._revisions[path] = (now, *revision)
        return revision

    @property
    def cache_bytes(self) -> int:
        return self._cache.total_bytes

    @property
    def cache_count(self) -> int:
        return len(self._cache)

    @property
    def cache_keys(self) -> tuple[ImageLoadKey, ...]:
        return self._cache.keys


class ClipDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thumbnails = _ByteLruCache[ImageLoadKey, QPixmap](
            _THUMBNAIL_CACHE_BYTES,
            1_500,
        )
        self._failed_thumbnails = _ByteLruCache[ImageLoadKey, bool](512, 512)
        self._file_icons: dict[str, QPixmap] = {}
        self._file_icon_provider = QFileIconProvider()
        # Thumbnail QImages are handed off directly and not retained by the
        # loader, so the final QPixmap is the only cached pixel copy.
        self._image_loader = _ScaledImageLoader(
            self,
            max_cache_bytes=0,
            max_cache_entries=0,
        )
        self._image_loader.image_ready.connect(self._image_loaded)
        self._invalidated_paths: set[str] = set()
        self.hovered_row = -1
        self.dark_theme = False
        self._appearance = _ThemeAppearance(dark=False)

    def set_dark_theme(self, dark: bool) -> None:
        self.set_theme(_ThemeAppearance(dark=dark))

    def set_theme(self, appearance: _ThemeAppearance) -> None:
        self._appearance = appearance
        self.dark_theme = appearance.dark
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
        return QSize(option.rect.width(), _ui_metrics().list_row_height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item: ClipItem = index.data(ITEM_ROLE)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 1, -5, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == self.hovered_row
        colors = _theme_colors(self._appearance)
        if selected or hovered:
            if self._appearance.frosted:
                if selected:
                    # Selection and hover deliberately use the same blue-tinted
                    # material language.  Selection earns its priority through
                    # density, not a separate opaque-blue card and white rim.
                    fill = QColor(77, 143, 233, 154 if not self.dark_theme else 122)
                else:
                    fill = QColor(120, 178, 236, 42 if not self.dark_theme else 28)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.accent) if selected else _hover_color(self._appearance))
            painter.drawRoundedRect(rect, 8, 8)

        thumb_rect = self._thumbnail_rect(rect)
        self._paint_thumbnail(painter, thumb_rect, item, selected)
        text_left = thumb_rect.right() + 13
        text_right = rect.right() - 10
        title_rect = QRect(text_left, thumb_rect.top(), text_right - text_left, thumb_rect.height())
        foreground = option.palette.color(QPalette.ColorRole.Text)
        if selected:
            foreground = QColor(_active_foreground(self._appearance))

        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(foreground)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, _elide(painter, item.title, title_rect.width()))
        if item.is_favorite:
            painter.setPen(foreground)
            pin_rect = QRect(rect.right() - 26, rect.center().y() - 9, 16, 18)
            painter.drawText(pin_rect, Qt.AlignmentFlag.AlignCenter, "★")
        painter.restore()

    @staticmethod
    def _thumbnail_rect(row_rect: QRect) -> QRect:
        size = _ui_metrics().list_thumbnail_size
        top = row_rect.top() + (row_rect.height() - size) // 2
        return QRect(row_rect.left() + 8, top, size, size)

    def _paint_thumbnail(self, painter: QPainter, rect: QRect, item: ClipItem, selected: bool) -> None:
        colors = _theme_colors(self._appearance)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 32) if selected else QColor(colors.thumbnail))
        painter.drawRoundedRect(rect, 8, 8)
        if item.kind is ClipKind.IMAGE:
            pixmap = self._image_thumbnail(item.image_path, rect.size() * 2)
            if not pixmap.isNull():
                painter.drawPixmap(rect, pixmap, pixmap.rect())
                return
        elif item.kind is ClipKind.FILES and item.files:
            image_path = _single_image_file_path(item.files)
            if image_path:
                pixmap = self._image_thumbnail(image_path, rect.size() * 2)
                if not pixmap.isNull():
                    painter.drawPixmap(rect, pixmap, pixmap.rect())
                    return
            else:
                pixmap = self._file_thumbnail(item.files[0])
                if not pixmap.isNull():
                    target = self._centered_file_icon_rect(rect)
                    painter.drawPixmap(target, pixmap, pixmap.rect())
                    return
        color = QColor(colors.accent)
        if selected:
            color = QColor(_active_foreground(self._appearance))
        painter.setPen(color)
        font = painter.font()
        font.setPointSize(_ui_metrics().thumbnail_letter_font_size_pt)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T" if item.kind is ClipKind.TEXT else "F")

    @staticmethod
    def _centered_file_icon_rect(container: QRect) -> QRect:
        inset = 3
        return container.adjusted(inset, inset, -inset, -inset)

    def _image_thumbnail(self, path: str, size: QSize) -> QPixmap:
        if not path:
            return QPixmap()
        self._invalidated_paths.discard(path)
        key = self._image_loader.key(path, size, False)
        thumbnail = self._thumbnails.get(key)
        if thumbnail is not None:
            return thumbnail
        if self._failed_thumbnails.get(key):
            return QPixmap()
        self._image_loader.request(path, size, keep_aspect=False)
        return QPixmap()

    def _image_loaded(self, key: ImageLoadKey, image: QImage) -> None:
        if key[0] not in self._invalidated_paths and not image.isNull():
            size = QSize(key[3], key[4])
            thumbnail = QPixmap.fromImage(image).scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._thumbnails.put(key, thumbnail, _pixmap_cost(thumbnail))
        elif key[0] not in self._invalidated_paths:
            self._failed_thumbnails.put(key, True, 1)
        view = self.parent()
        if isinstance(view, QListView):
            view.viewport().update()

    def invalidate_paths(self, paths: set[str]) -> None:
        self._invalidated_paths.update(paths)
        self._thumbnails.remove_paths(paths)
        self._failed_thumbnails.remove_paths(paths)
        self._image_loader.invalidate_paths(paths)

    @property
    def thumbnail_cache_bytes(self) -> int:
        return self._thumbnails.total_bytes

    @property
    def thumbnail_cache_count(self) -> int:
        return len(self._thumbnails)

    @property
    def thumbnail_cache_keys(self) -> tuple[ImageLoadKey, ...]:
        return self._thumbnails.keys

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
    activated = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._path = ""
        self._image_loader = _ScaledImageLoader(
            self,
            max_cache_bytes=_DETAIL_CACHE_BYTES,
            max_cache_entries=_DETAIL_CACHE_ENTRIES,
            priority=1,
        )
        self._image_loader.image_ready.connect(self._image_loaded)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._render)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(220, 240)
        self.setText("图片预览")

    def set_path(self, path: str) -> None:
        self._resize_timer.stop()
        self._path = path
        self._image_loader.retain_only(path)
        self.clear()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not path:
            self.setText("图片预览")
            return
        self.setText("正在加载预览…")
        self._render()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._path:
            self._resize_timer.start(40)

    def _render(self) -> None:
        if not self._path or self.width() < 20 or self.height() < 20:
            return
        logical_bounds = QSize(max(1, self.width() - 20), max(1, self.height() - 20))
        device_scale = max(1.0, float(self.devicePixelRatioF()))
        physical_bounds = _scaled_size(logical_bounds, device_scale)
        decode_bounds = _bucketed_size(physical_bounds)
        image = self._image_loader.request(self._path, decode_bounds, keep_aspect=True)
        if image is None:
            return
        if image.isNull():
            self.setText("无法预览图片")
            return
        pixmap = QPixmap.fromImage(image)
        display_size = _fit_image_size(pixmap.size(), physical_bounds, allow_upscale=False)
        if display_size != pixmap.size():
            pixmap = pixmap.scaled(
                display_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        pixmap.setDevicePixelRatio(device_scale)
        self.setPixmap(pixmap)

    def _image_loaded(self, key: ImageLoadKey, image: QImage) -> None:
        if key[0] == self._path:
            if image.isNull():
                self.setText("无法预览图片")
            else:
                self._render()

    def invalidate_paths(self, paths: set[str]) -> None:
        self._image_loader.invalidate_paths(paths)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._path:
            self.activated.emit(self._path)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _ZoomableImageCanvas(QWidget):
    background_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._path = ""
        self._image = QImage()
        self._status = ""
        self._zoom = 1.0
        self._fit_mode = True
        self._offset = QPointF()
        self._drag_origin: QPointF | None = None
        self._dragging = False
        self._drag_offset = QPointF()
        self._image_loader = _ScaledImageLoader(
            self,
            max_cache_bytes=_IMAGE_VIEWER_CACHE_BYTES,
            max_cache_entries=_IMAGE_VIEWER_CACHE_ENTRIES,
            priority=2,
        )
        self._image_loader.image_ready.connect(self._image_loaded)
        self.setObjectName("imageViewerCanvas")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(1, 1)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_path(self, path: str) -> None:
        self._path = path
        self._image = QImage()
        self._status = ""
        self._fit_mode = True
        self._zoom = 1.0
        self._offset = QPointF()
        self._drag_origin = None
        self._dragging = False
        self._image_loader.retain_only(path)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        if path:
            self._request_image()
        else:
            self._status = ""

    def fit_to_window(self) -> None:
        if self._image.isNull():
            return
        self._fit_mode = True
        self._zoom = self._fit_zoom()
        self._offset = QPointF()
        self.update()

    def actual_size(self, anchor: QPointF | None = None) -> None:
        if self._image.isNull():
            return
        self._fit_mode = False
        self._set_zoom(1.0, anchor or QRectF(self.rect()).center())

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def image(self) -> QImage:
        return self._image

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._image.isNull():
            return
        if self._fit_mode:
            self.fit_to_window()
        else:
            self._constrain_offset()

    def wheelEvent(self, event) -> None:
        if self._image.isNull():
            super().wheelEvent(event)
            return
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.accept()
            return
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            event.accept()
            return
        self._fit_mode = False
        self._set_zoom(self._zoom * (1.16**steps), event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._image.isNull():
            super().mousePressEvent(event)
            return
        if not self._image_rect().contains(event.position()):
            self.background_clicked.emit()
            event.accept()
            return
        self._drag_origin = event.position()
        self._dragging = False
        self._drag_offset = QPointF(self._offset)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is None or self._image.isNull():
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._drag_origin
        if not self._dragging:
            if math.isclose(delta.x(), 0.0, abs_tol=0.1) and math.isclose(delta.y(), 0.0, abs_tol=0.1):
                event.accept()
                return
            self._dragging = True
        self._offset = self._drag_offset + delta
        self._constrain_offset()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            close_requested = not self._dragging
            self._drag_origin = None
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if close_requested:
                self.background_clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101318"))
        if self._image.isNull():
            painter.setPen(QColor("#C5CEDA"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not math.isclose(self._zoom, 1.0))
        painter.drawImage(self._image_rect(), self._image, QRectF(self._image.rect()))

    def _request_image(self) -> None:
        if not self._path:
            return
        image = self._image_loader.request(
            self._path,
            _viewer_decode_bounds(self._path),
            keep_aspect=True,
        )
        if image is not None:
            self._apply_image(image)

    def _image_loaded(self, key: ImageLoadKey, image: QImage) -> None:
        if key[0] == self._path:
            self._apply_image(image)

    def _apply_image(self, image: QImage) -> None:
        if image.isNull():
            self._image = QImage()
            self._status = "无法预览图片"
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            return
        self._image = image
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.fit_to_window()

    def _set_zoom(self, zoom: float, anchor: QPointF) -> None:
        old_rect = self._image_rect()
        self._zoom = min(max(zoom, _IMAGE_VIEWER_MIN_ZOOM), _IMAGE_VIEWER_MAX_ZOOM)
        if old_rect.isValid() and not old_rect.isEmpty() and old_rect.contains(anchor):
            relative_x = (anchor.x() - old_rect.left()) / old_rect.width()
            relative_y = (anchor.y() - old_rect.top()) / old_rect.height()
            width, height = self._display_size()
            top_left = QPointF(anchor.x() - width * relative_x, anchor.y() - height * relative_y)
            center = top_left + QPointF(width / 2.0, height / 2.0)
            self._offset = center - QRectF(self.rect()).center()
        self._constrain_offset()

    def _fit_zoom(self) -> float:
        width, height = self._image_logical_size()
        if width <= 0 or height <= 0 or self.width() <= 0 or self.height() <= 0:
            return 1.0
        fit = min(self.width() / width, self.height() / height)
        return min(1.0, max(_IMAGE_VIEWER_MIN_ZOOM, fit))

    def _image_rect(self) -> QRectF:
        width, height = self._display_size()
        center = QRectF(self.rect()).center() + self._offset
        return QRectF(center.x() - width / 2.0, center.y() - height / 2.0, width, height)

    def _display_size(self) -> tuple[float, float]:
        width, height = self._image_logical_size()
        return width * self._zoom, height * self._zoom

    def _image_logical_size(self) -> tuple[float, float]:
        if self._image.isNull():
            return 0.0, 0.0
        device_scale = max(1.0, float(self.devicePixelRatioF()))
        return self._image.width() / device_scale, self._image.height() / device_scale

    def _constrain_offset(self) -> None:
        width, height = self._display_size()
        max_x = self._axis_drag_limit(width, self.width())
        max_y = self._axis_drag_limit(height, self.height())
        self._offset = QPointF(
            min(max(self._offset.x(), -max_x), max_x) if max_x else 0.0,
            min(max(self._offset.y(), -max_y), max_y) if max_y else 0.0,
        )
        self.update()

    @staticmethod
    def _axis_drag_limit(display_length: float, viewport_length: int) -> float:
        if display_length <= 0 or viewport_length <= 0:
            return 0.0
        limit = abs(display_length - viewport_length) / 2.0
        if limit < 1.0:
            return min(display_length, float(viewport_length)) / 2.0
        return limit


class ImageViewerDialog(QDialog):
    def __init__(self, path: str, appearance: _ThemeAppearance, parent: QWidget | None = None) -> None:
        super().__init__(parent if sys.platform == "win32" else None)
        self._appearance = appearance
        self._external_dismiss_filter_installed = False
        self._resize_edges: set[str] = set()
        self._resize_origin_geometry = QRect()
        self._resize_origin_global = QPoint()
        self.setObjectName("imageViewerDialog")
        self.setWindowTitle("")
        window_flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
        )
        if sys.platform != "win32":
            window_flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(window_flags)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(_IMAGE_VIEWER_MINIMUM_SIZE)
        self._apply_screen_size_limits(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(
            _IMAGE_VIEWER_FRAME_MARGIN,
            _IMAGE_VIEWER_FRAME_MARGIN,
            _IMAGE_VIEWER_FRAME_MARGIN,
            _IMAGE_VIEWER_FRAME_MARGIN,
        )
        root.setSpacing(0)

        self.canvas = _ZoomableImageCanvas()
        root.addWidget(self.canvas, 1)

        self.canvas.background_clicked.connect(self.reject)
        self.canvas.installEventFilter(self)
        self.set_appearance(appearance)
        self.resize(_image_viewer_default_size(path, parent))
        self.canvas.set_path(path)

    def set_appearance(self, appearance: _ThemeAppearance) -> None:
        self._appearance = appearance
        self.setStyleSheet(_image_viewer_style_sheet(appearance))

    def center_on_widget(self, anchor: QWidget | None) -> None:
        self._apply_screen_size_limits(anchor)
        own_size = self.size()
        if anchor is not None and anchor.isVisible():
            anchor_geometry = anchor.frameGeometry()
            target = anchor_geometry.center() - QPoint(own_size.width() // 2, own_size.height() // 2)
            screen = QApplication.screenAt(anchor_geometry.center()) or anchor.screen()
        else:
            screen = QApplication.primaryScreen()
            available = screen.availableGeometry() if screen is not None else QRect(0, 0, 960, 720)
            target = available.center() - QPoint(own_size.width() // 2, own_size.height() // 2)
        if screen is not None:
            available = screen.availableGeometry()
            target.setX(min(max(target.x(), available.left()), available.right() - own_size.width() + 1))
            target.setY(min(max(target.y(), available.top()), available.bottom() - own_size.height() + 1))
        self.move(target)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._install_external_dismiss_filter()
        _apply_rounded_widget_mask(self, 16)

    def hideEvent(self, event) -> None:
        self._remove_external_dismiss_filter()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        _apply_rounded_widget_mask(self, 16)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        edges = self._resize_edges_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and edges:
            self._resize_edges = edges
            self._resize_origin_geometry = self.geometry()
            self._resize_origin_global = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._resize_edges:
            self._resize_to(event.globalPosition().toPoint())
            event.accept()
            return
        self._set_resize_cursor(self._resize_edges_at(event.position()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._resize_edges:
            self._resize_edges = set()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._resize_edges:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not self.isVisible():
            return super().eventFilter(watched, event)
        if watched is self.canvas and isinstance(event, QMouseEvent) and self._handle_canvas_resize_event(event):
            return True
        event_type = event.type()
        if event_type == QEvent.Type.ApplicationDeactivate:
            self.reject()
            return super().eventFilter(watched, event)
        if event_type in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.NonClientAreaMouseButtonPress,
        ) and isinstance(event, QMouseEvent):
            global_pos = self._mouse_global_position(event)
            if global_pos is not None and not self.frameGeometry().contains(global_pos):
                self.reject()
                return True
        return super().eventFilter(watched, event)

    def _handle_canvas_resize_event(self, event: QMouseEvent) -> bool:
        local_position = QPointF(self.canvas.mapTo(self, event.position().toPoint()))
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            edges = self._resize_edges_at(local_position)
            if event.button() == Qt.MouseButton.LeftButton and edges:
                self._resize_edges = edges
                self._resize_origin_geometry = self.geometry()
                self._resize_origin_global = event.globalPosition().toPoint()
                event.accept()
                return True
        if event_type == QEvent.Type.MouseMove:
            if self._resize_edges:
                self._resize_to(event.globalPosition().toPoint())
                event.accept()
                return True
            if not event.buttons():
                self._set_resize_cursor(self._resize_edges_at(local_position))
        if event_type == QEvent.Type.MouseButtonRelease and self._resize_edges:
            self._resize_edges = set()
            self._set_resize_cursor(set())
            event.accept()
            return True
        return False

    def _install_external_dismiss_filter(self) -> None:
        if self._external_dismiss_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        self._external_dismiss_filter_installed = True

    def _remove_external_dismiss_filter(self) -> None:
        if not self._external_dismiss_filter_installed:
            return
        app = QApplication.instance()
        if app is not None:
            with suppress(RuntimeError):
                app.removeEventFilter(self)
        self._external_dismiss_filter_installed = False

    def _apply_screen_size_limits(self, anchor: QWidget | None) -> None:
        screen = _image_viewer_screen(anchor)
        if screen is None:
            return
        maximum = _image_viewer_maximum_size(screen)
        self.setMaximumSize(maximum)
        if self.width() > maximum.width() or self.height() > maximum.height():
            self.resize(
                min(self.width(), maximum.width()),
                min(self.height(), maximum.height()),
            )

    def _resize_edges_at(self, position: QPointF) -> set[str]:
        edges: set[str] = set()
        margin = _IMAGE_VIEWER_RESIZE_MARGIN
        if position.x() <= margin:
            edges.add("left")
        elif position.x() >= self.width() - margin:
            edges.add("right")
        if position.y() <= margin:
            edges.add("top")
        elif position.y() >= self.height() - margin:
            edges.add("bottom")
        return edges

    def _set_resize_cursor(self, edges: set[str]) -> None:
        if edges in ({"top", "left"}, {"bottom", "right"}):
            cursor = Qt.CursorShape.SizeFDiagCursor
        elif edges in ({"top", "right"}, {"bottom", "left"}):
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif edges & {"left", "right"}:
            cursor = Qt.CursorShape.SizeHorCursor
        elif edges & {"top", "bottom"}:
            cursor = Qt.CursorShape.SizeVerCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self.setCursor(cursor)
        self.canvas.setCursor(cursor)

    def _resize_to(self, global_position: QPoint) -> None:
        delta = global_position - self._resize_origin_global
        origin = self._resize_origin_geometry
        maximum = self.maximumSize()
        width = origin.width()
        height = origin.height()
        x = origin.x()
        y = origin.y()
        if "left" in self._resize_edges:
            width = origin.width() - delta.x()
            width = min(max(width, self.minimumWidth()), maximum.width())
            x = origin.x() + origin.width() - width
        elif "right" in self._resize_edges:
            width = min(max(origin.width() + delta.x(), self.minimumWidth()), maximum.width())
        if "top" in self._resize_edges:
            height = origin.height() - delta.y()
            height = min(max(height, self.minimumHeight()), maximum.height())
            y = origin.y() + origin.height() - height
        elif "bottom" in self._resize_edges:
            height = min(max(origin.height() + delta.y(), self.minimumHeight()), maximum.height())
        self.setGeometry(QRect(x, y, width, height))

    @staticmethod
    def _mouse_global_position(event: QMouseEvent) -> QPoint | None:
        with suppress(RuntimeError, AttributeError):
            return event.globalPosition().toPoint()
        return None


class TextFilePreview(QPlainTextEdit):
    """A fixed, non-scrollable excerpt rather than a miniature file viewer."""

    def wheelEvent(self, event) -> None:
        event.accept()


class SettingsDialog(QDialog):
    clear_requested = Signal()
    reveal_requested = Signal()
    accessibility_requested = Signal()
    settings_changed = Signal(object)

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
        running_app_provider: Callable[[], Sequence[tuple[str, str]]] | None = None,
    ) -> None:
        _install_app_owned_caret_style()
        super().__init__(parent)
        self._updating_controls = False
        # A QKeySequenceEdit can emit ``editingFinished`` while Qt tears down
        # a focused editor.  That is not a user edit and must never create a
        # modal validation prompt while this dialog is closing.
        self._closing = False
        self._theme_settings = settings
        self._running_app_provider = running_app_provider or (lambda: ())
        self._plain_text_target_apps = tuple(settings.plain_text_target_apps)
        self._external_dismiss_filter_installed = False
        self._external_dismiss_armed = False
        self._external_dismiss_timer = QTimer(self)
        self._external_dismiss_timer.setInterval(_SETTINGS_EXTERNAL_DISMISS_POLL_MS)
        self._external_dismiss_timer.timeout.connect(self._poll_external_dismiss)
        self._external_dismiss_suppressed = False
        self._external_dismiss_resume_timer = QTimer(self)
        self._external_dismiss_resume_timer.setSingleShot(True)
        self._external_dismiss_resume_timer.timeout.connect(self._resume_external_dismiss)
        self._appearance = _theme_appearance(settings)
        self._dark_theme = self._appearance.dark
        self.setObjectName("settingsDialog")
        self.setWindowTitle("ClipSoon 设置")
        self.setAccessibleName("ClipSoon 设置")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setModal(False)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        metrics = _ui_metrics()
        self.setFixedWidth(metrics.settings_window_width)
        if sys.platform == "win32":
            # Keep an ordinary Qt backing store; frosted material is painted
            # in-app and never requests DWM composition.
            self.setAutoFillBackground(True)
        else:
            # Keep the macOS top-level mode stable while the user switches
            # themes.  Qt cannot reliably restore opaque backing after a
            # shown translucent window; normal themes are instead painted as
            # a fully opaque app-owned shell in ``paintEvent``.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAutoFillBackground(False)
        self.setStyleSheet(
            _style_sheet(
                self._appearance,
                dialog_transparent=sys.platform != "win32",
            )
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 12)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("ClipSoon 设置")
        title.setObjectName("settingsWindowTitle")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        def section(title_text: str) -> tuple[QFrame, QVBoxLayout]:
            frame = QFrame()
            frame.setObjectName("settingsSection")
            section_layout = QVBoxLayout(frame)
            section_layout.setContentsMargins(14, 6, 14, 6)
            section_layout.setSpacing(3)
            section_title = QLabel(title_text)
            section_title.setObjectName("settingsSectionTitle")
            section_layout.addWidget(section_title)
            return frame, section_layout

        def form_grid() -> QGridLayout:
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(metrics.settings_form_column_gap)
            grid.setVerticalSpacing(2)
            grid.setColumnMinimumWidth(0, metrics.settings_label_column_width)
            grid.setColumnStretch(1, 1)
            return grid

        def add_row(grid: QGridLayout, row: int, text: str, control: QWidget) -> QLabel:
            label = QLabel(text)
            label.setObjectName("settingsFieldLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            control.setMinimumHeight(metrics.settings_control_min_height)
            control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.addWidget(label, row, 0)
            grid.addWidget(control, row, 1)
            return label

        shortcut_section, shortcut_layout = section("快捷键")
        shortcut_form = form_grid()
        shortcut_layout.addLayout(shortcut_form)

        self.custom_hotkey = QKeySequenceEdit()
        self.custom_hotkey.setMaximumSequenceLength(1)
        self.custom_hotkey.setClearButtonEnabled(True)
        default_custom = WINDOWS_DEFAULT_HOTKEY
        hotkey_text = _hotkey_display(settings.hotkey if settings.hotkey.startswith("combo:") else default_custom)
        self.custom_hotkey.setKeySequence(QKeySequence(hotkey_text))
        self.hotkey_mode: QComboBox | None = None
        if sys.platform == "win32":
            self._available_hotkeys = {"快捷键": "custom"}
            add_row(shortcut_form, 0, "快捷键", self.custom_hotkey)
        else:
            self._available_hotkeys = self._HOTKEYS
            self.hotkey_mode = QComboBox()
            self.hotkey_mode.addItems(self._available_hotkeys)
            current = next(
                (
                    label
                    for label, value in self._available_hotkeys.items()
                    if value == settings.hotkey
                ),
                "自定义组合键",
            )
            self.hotkey_mode.setCurrentText(current)
            add_row(shortcut_form, 0, "呼出方式", self.hotkey_mode)
            self.custom_hotkey.setEnabled(current == "自定义组合键")
            self.hotkey_mode.currentTextChanged.connect(
                lambda value: self.custom_hotkey.setEnabled(value == "自定义组合键")
            )
            add_row(shortcut_form, 1, "自定义组合键", self.custom_hotkey)

        self.interval = _spin(settings.double_tap_interval_ms, 180, 900, " ms")
        self.interval.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        interval_label = add_row(shortcut_form, 2, "双击间隔", self.interval)
        if sys.platform == "win32":
            interval_label.hide()
            self.interval.hide()
        layout.addWidget(shortcut_section)

        history_section, history_layout = section("历史与行为")
        history_form = form_grid()
        history_layout.addLayout(history_form)
        self.maximum = _spin(settings.max_history_items, 50, 10_000, " 条")
        self.retention = _spin(settings.retention_days, 0, 3_650, " 天（0 = 永久）")
        self.delay = _spin(settings.paste_delay_ms, 60, 2_000, " ms")
        for spin_box in (self.maximum, self.retention, self.delay):
            spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        add_row(history_form, 0, "历史容量", self.maximum)
        add_row(history_form, 1, "保留时间", self.retention)
        add_row(history_form, 2, "恢复等待", self.delay)

        self.theme = QComboBox()
        for label, value in _SETTINGS_THEME_OPTIONS:
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(_settings_theme_key(settings.theme))))
        add_row(history_form, 3, "主题", self.theme)

        # A QComboBox popup is a separate top-level window on macOS.  It does
        # not reliably inherit the dialog stylesheet, so an explicitly dark
        # dialog could otherwise show white text on the system's light popup.
        for combo in (self.hotkey_mode, self.theme):
            if combo is None:
                continue
            _style_combo_popup(combo, self._appearance)

        self.selection_memory = _spin(settings.selection_memory_seconds, 1, 300, " 秒")
        self.selection_memory.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.selection_memory.setEnabled(settings.remember_selection)
        add_row(history_form, 4, "记忆时长", self.selection_memory)

        self.capture = _SettingsCheckBox("记录新的剪贴板内容")
        self.capture.setChecked(settings.capture_enabled)
        self.paste = _SettingsCheckBox("选择后自动粘贴到原应用")
        self.paste.setChecked(settings.paste_after_selection)
        # Keep QWidget.hide() callable on the settings dialog.  Naming this
        # checkbox ``hide`` shadows the inherited method and turns a normal
        # dialog.hide() call into a TypeError.
        self.hide_on_deactivate_checkbox = _SettingsCheckBox("面板失去焦点时自动隐藏")
        self.hide_on_deactivate_checkbox.setChecked(settings.hide_on_deactivate)
        self.launch_at_login = _SettingsCheckBox("登录时自动启动")
        self.launch_at_login.setAccessibleName("登录时自动启动 ClipSoon")
        self.launch_at_login.setToolTip("登录系统时自动启动 ClipSoon")
        self.launch_at_login.setChecked(settings.launch_at_login)
        # Keep the persisted ``remember_selection`` key for upgrade compatibility,
        # while the user-facing feature now restores the complete panel state.
        self.remember_selection = _SettingsCheckBox("记住上次状态")
        self.remember_selection.setChecked(settings.remember_selection)
        self.remember_selection.toggled.connect(self.selection_memory.setEnabled)
        behavior_checkboxes = (
            self.capture,
            self.paste,
            self.hide_on_deactivate_checkbox,
            self.remember_selection,
            self.launch_at_login,
        )
        # Native QCheckBox metrics differ enough across macOS scales that a
        # content-driven grid can place consecutive labels on top of one
        # another. Give this compact three-row group an explicit row rhythm.
        checkbox_row_height = metrics.settings_checkbox_row_height
        for checkbox in behavior_checkboxes:
            checkbox.setFixedHeight(checkbox_row_height)
            checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        behavior_options = QGridLayout()
        behavior_options.setContentsMargins(
            metrics.settings_label_column_width + metrics.settings_form_column_gap,
            3,
            0,
            0,
        )
        behavior_options.setHorizontalSpacing(18)
        behavior_options.setVerticalSpacing(2)
        behavior_options.setRowMinimumHeight(0, checkbox_row_height)
        behavior_options.setRowMinimumHeight(1, checkbox_row_height)
        behavior_options.setRowMinimumHeight(2, checkbox_row_height)
        behavior_options.setColumnStretch(0, 1)
        behavior_options.setColumnStretch(1, 1)
        behavior_options.addWidget(self.capture, 0, 0)
        behavior_options.addWidget(self.paste, 0, 1)
        behavior_options.addWidget(self.hide_on_deactivate_checkbox, 1, 0)
        behavior_options.addWidget(self.remember_selection, 1, 1)
        behavior_options.addWidget(self.launch_at_login, 2, 0)
        history_layout.addLayout(behavior_options)
        layout.addWidget(history_section)
        self.plain_text_compat: _SettingsCheckBox | None = None
        self.running_apps_combo: QComboBox | None = None
        self.target_apps_combo: QComboBox | None = None
        self.add_target_app_button: QPushButton | None = None
        self.refresh_running_apps_button: QPushButton | None = None
        self.remove_target_app_button: QPushButton | None = None
        self._plain_text_compat_enabled = bool(settings.plain_text_compat_enabled)
        settings_checkboxes = list(behavior_checkboxes)
        if sys.platform == "win32":
            compatibility_section, compatibility_layout = section("剪贴板兼容")
            self.plain_text_compat = _SettingsCheckBox("目标应用纯文本兼容")
            self.plain_text_compat.setChecked(settings.plain_text_compat_enabled)
            self.plain_text_compat.setFixedHeight(checkbox_row_height)
            compatibility_layout.addWidget(self.plain_text_compat)
            compatibility_description = QLabel(
                "切换到指定应用时，将当前文本剪贴板转换为纯文本；不会自动粘贴。"
            )
            compatibility_description.setObjectName("settingsSubtitle")
            compatibility_description.setWordWrap(True)
            compatibility_layout.addWidget(compatibility_description)

            compatibility_form = form_grid()
            self.running_apps_combo = QComboBox()
            self.add_target_app_button = QPushButton("添加")
            self.refresh_running_apps_button = QPushButton("刷新")
            running_row = QWidget()
            running_layout = QHBoxLayout(running_row)
            running_layout.setContentsMargins(0, 0, 0, 0)
            running_layout.setSpacing(6)
            running_layout.addWidget(self.running_apps_combo, 1)
            running_layout.addWidget(self.refresh_running_apps_button)
            running_layout.addWidget(self.add_target_app_button)
            add_row(compatibility_form, 0, "运行中的应用", running_row)

            self.target_apps_combo = QComboBox()
            self.remove_target_app_button = QPushButton("移除")
            target_row = QWidget()
            target_layout = QHBoxLayout(target_row)
            target_layout.setContentsMargins(0, 0, 0, 0)
            target_layout.setSpacing(6)
            target_layout.addWidget(self.target_apps_combo, 1)
            target_layout.addWidget(self.remove_target_app_button)
            add_row(compatibility_form, 1, "目标应用", target_row)
            compatibility_layout.addLayout(compatibility_form)
            layout.addWidget(compatibility_section)

            self.add_target_app_button.clicked.connect(self._add_selected_target_app)
            self.refresh_running_apps_button.clicked.connect(self._refresh_running_apps)
            self.remove_target_app_button.clicked.connect(self._remove_selected_target_app)
            self.plain_text_compat.toggled.connect(self._emit_settings_changed)
            settings_checkboxes.append(self.plain_text_compat)
            self._sync_target_app_controls()
            self._refresh_running_apps()
        self._settings_checkboxes = tuple(settings_checkboxes)

        self.accessibility_button = None
        platform_message = ""
        if sys.platform == "darwin" and accessibility_granted is not True:
            platform_message = "需要辅助功能权限以监听快捷键并自动粘贴。"
        elif sys.platform == "win32":
            platform_message = (
                "Windows 使用系统注册的组合快捷键，不使用双击修饰键监听。"
                "向管理员身份运行的应用自动粘贴时，ClipSoon 也需要以管理员身份运行。"
            )
        if platform_message:
            platform_note = QFrame()
            platform_note.setObjectName("platformNote")
            platform_layout = QHBoxLayout(platform_note)
            platform_layout.setContentsMargins(9, 5, 9, 5)
            platform_layout.setSpacing(6)
            note = QLabel(platform_message)
            note.setWordWrap(True)
            platform_layout.addWidget(note, 1)
        if sys.platform == "darwin" and accessibility_granted is not True:
            self.accessibility_button = QPushButton("打开辅助功能设置")
            self.accessibility_button.clicked.connect(self.accessibility_requested)
            platform_layout.addWidget(self.accessibility_button)
        if platform_message:
            layout.addWidget(platform_note)

        data_section, data_layout = section("数据管理")
        data_description = QLabel("历史数据仅保存在本机；清除操作不会影响已收藏条目。")
        data_description.setObjectName("settingsSubtitle")
        data_layout.addWidget(data_description)
        data_row = QHBoxLayout()
        data_row.setSpacing(8)
        clear = QPushButton("清除非收藏历史")
        reveal_text = {
            "darwin": "在 Finder 中打开",
            "win32": "在资源管理器中打开",
        }.get(sys.platform, "打开数据目录")
        self.reveal_button = QPushButton(reveal_text)
        clear.clicked.connect(self._confirm_clear)
        self.reveal_button.clicked.connect(self._request_reveal)
        data_row.addWidget(clear)
        data_row.addWidget(self.reveal_button)
        data_row.addStretch()
        data_layout.addLayout(data_row)
        layout.addWidget(data_section)

        footer = QHBoxLayout()
        footer.addStretch()
        self.close_button = QPushButton("关闭")
        self.close_button.setObjectName("settingsCloseButton")
        self.close_button.setAccessibleName("关闭设置")
        self.close_button.setToolTip("关闭设置")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        self.reset_button = QPushButton("重置")
        self.reset_button.setToolTip("恢复默认设置")
        self.reset_button.clicked.connect(self._reset)
        footer.addWidget(self.reset_button)
        layout.addLayout(footer)

        # QKeySequenceEdit and QSpinBox delegate text entry to private child
        # QLineEdits. Theme their real editors and attach the same app-owned
        # blinking caret as the main search field, so macOS cannot fall back
        # to a white insertion line in the settings window.
        editable_inputs: list[QLineEdit | None] = [self.custom_hotkey.findChild(QLineEdit)]
        editable_inputs.extend(
            spin_box.lineEdit()
            for spin_box in (
                self.interval,
                self.maximum,
                self.retention,
                self.delay,
                self.selection_memory,
            )
        )
        self._editable_text_inputs = tuple(editor for editor in editable_inputs if editor is not None)
        self._text_carets = tuple(
            _ThemedTextCaret(editor, focus_owner=editor.parentWidget())
            for editor in self._editable_text_inputs
        )

        self._connect_immediate_changes()
        self._apply_theme(settings)
        # A frameless dialog can otherwise place initial keyboard focus on a
        # footer button or the first combo box. Keep initial focus on the
        # dialog shell itself so bare ↑/↓ cannot accidentally change settings
        # immediately after the window opens. The controls remain available in
        # normal tab order and by pointer.
        self._initial_focus_control: QWidget = self

    @staticmethod
    def _target_app_label(path: str, title: str = "") -> str:
        executable = ntpath.basename(path) or path
        concise_title = " ".join(title.split())
        if concise_title and concise_title.casefold() != executable.casefold():
            return f"{concise_title[:64]} — {executable}"
        return executable

    def _refresh_running_apps(self) -> None:
        if self.running_apps_combo is None or self.add_target_app_button is None:
            return
        selected_path = self.running_apps_combo.currentData()
        configured = {ntpath.normcase(path) for path in self._plain_text_target_apps}
        self.running_apps_combo.clear()
        try:
            candidates = tuple(self._running_app_provider())
        except Exception:
            LOGGER.exception("Could not enumerate running applications")
            candidates = ()
        for title, path in candidates:
            if not path or ntpath.normcase(path) in configured:
                continue
            self.running_apps_combo.addItem(self._target_app_label(path, title), path)
            self.running_apps_combo.setItemData(
                self.running_apps_combo.count() - 1,
                path,
                Qt.ItemDataRole.ToolTipRole,
            )
        if selected_path:
            index = self.running_apps_combo.findData(selected_path)
            if index >= 0:
                self.running_apps_combo.setCurrentIndex(index)
        self.add_target_app_button.setEnabled(self.running_apps_combo.count() > 0)

    def _sync_target_app_controls(self) -> None:
        if self.target_apps_combo is None or self.remove_target_app_button is None:
            return
        selected_path = self.target_apps_combo.currentData()
        self.target_apps_combo.clear()
        for path in self._plain_text_target_apps:
            self.target_apps_combo.addItem(self._target_app_label(path), path)
            self.target_apps_combo.setItemData(
                self.target_apps_combo.count() - 1,
                path,
                Qt.ItemDataRole.ToolTipRole,
            )
        if selected_path:
            index = self.target_apps_combo.findData(selected_path)
            if index >= 0:
                self.target_apps_combo.setCurrentIndex(index)
        self.remove_target_app_button.setEnabled(self.target_apps_combo.count() > 0)

    def _add_selected_target_app(self) -> None:
        if self.running_apps_combo is None:
            return
        path = self.running_apps_combo.currentData()
        if not isinstance(path, str) or not path:
            return
        if ntpath.normcase(path) not in {
            ntpath.normcase(candidate) for candidate in self._plain_text_target_apps
        }:
            self._plain_text_target_apps = (*self._plain_text_target_apps, path)
            self._sync_target_app_controls()
            self._refresh_running_apps()
            self._emit_settings_changed()

    def _remove_selected_target_app(self) -> None:
        if self.target_apps_combo is None:
            return
        path = self.target_apps_combo.currentData()
        if not isinstance(path, str) or not path:
            return
        identity = ntpath.normcase(path)
        self._plain_text_target_apps = tuple(
            candidate
            for candidate in self._plain_text_target_apps
            if ntpath.normcase(candidate) != identity
        )
        self._sync_target_app_controls()
        self._refresh_running_apps()
        self._emit_settings_changed()

    def values(self) -> dict[str, object]:
        if self.hotkey_mode is None:
            recorded = self.custom_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            selected = _parse_hotkey(recorded)
        else:
            selected = self._available_hotkeys[self.hotkey_mode.currentText()]
            if selected == "custom":
                recorded = self.custom_hotkey.keySequence().toString(
                    QKeySequence.SequenceFormat.PortableText
                )
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
            "hide_on_deactivate": self.hide_on_deactivate_checkbox.isChecked(),
            "remember_selection": self.remember_selection.isChecked(),
            "selection_memory_seconds": self.selection_memory.value(),
            "launch_at_login": self.launch_at_login.isChecked(),
            "plain_text_compat_enabled": (
                self.plain_text_compat.isChecked()
                if self.plain_text_compat is not None
                else self._plain_text_compat_enabled
            ),
            "plain_text_target_apps": self._plain_text_target_apps,
        }

    def _connect_immediate_changes(self) -> None:
        if self.hotkey_mode is not None:
            self.hotkey_mode.currentTextChanged.connect(self._hotkey_mode_changed)
        self.custom_hotkey.editingFinished.connect(self._emit_hotkey_change)
        for spin_box in (
            self.interval,
            self.maximum,
            self.retention,
            self.delay,
            self.selection_memory,
        ):
            spin_box.valueChanged.connect(self._emit_settings_changed)
        self.theme.currentIndexChanged.connect(self._emit_settings_changed)
        for checkbox in (
            self.capture,
            self.paste,
            self.hide_on_deactivate_checkbox,
            self.remember_selection,
            self.launch_at_login,
        ):
            checkbox.toggled.connect(self._emit_settings_changed)

    def _hotkey_mode_changed(self, value: str) -> None:
        self.custom_hotkey.setEnabled(value == "自定义组合键")
        self._emit_hotkey_change()

    def _emit_hotkey_change(self) -> None:
        if self._updating_controls or self._closing:
            return
        recorded = self.custom_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        custom = (
            self.hotkey_mode is None
            or self._available_hotkeys[self.hotkey_mode.currentText()] == "custom"
        )
        parsed = _parse_hotkey(recorded) if custom else ""
        if custom and not parsed:
            _show_themed_warning(
                self,
                "快捷键无效",
                "组合键必须包含 Ctrl/Shift/Alt/Command 和一个普通键。",
                appearance=self._appearance,
            )
            return
        platform_error = _platform_hotkey_validation_error(parsed)
        if custom and platform_error:
            _show_themed_warning(
                self,
                "快捷键无效",
                platform_error,
                appearance=self._appearance,
            )
            return
        self._emit_settings_changed()

    def done(self, result: int) -> None:
        """Mark teardown before child editors can emit late focus signals."""

        self._closing = True
        self._remove_external_dismiss_filter()
        self._external_dismiss_resume_timer.stop()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._remove_external_dismiss_filter()
        self._external_dismiss_resume_timer.stop()
        super().closeEvent(event)

    def hideEvent(self, event) -> None:
        self._remove_external_dismiss_filter()
        self._external_dismiss_resume_timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._closing or not self.isVisible() or self._has_active_popup_or_child_modal():
            return super().eventFilter(watched, event)
        if self._external_dismiss_suppressed:
            return super().eventFilter(watched, event)
        event_type = event.type()
        if (
            event_type == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            self.reject()
            return True
        if event_type in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.NonClientAreaMouseButtonPress,
        ) and isinstance(event, QMouseEvent):
            global_pos = self._mouse_global_position(event)
            if global_pos is not None and self._dismiss_for_external_point(global_pos):
                return True
        if event_type == QEvent.Type.ApplicationDeactivate:
            self.reject()
        return super().eventFilter(watched, event)

    def _install_external_dismiss_filter(self) -> None:
        if self._external_dismiss_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        self._external_dismiss_filter_installed = True
        self._external_dismiss_armed = not bool(QApplication.mouseButtons())
        self._external_dismiss_timer.start()

    def _remove_external_dismiss_filter(self) -> None:
        if not self._external_dismiss_filter_installed:
            return
        self._external_dismiss_timer.stop()
        self._external_dismiss_armed = False
        self._external_dismiss_suppressed = False
        self._external_dismiss_resume_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._external_dismiss_filter_installed = False

    def _suppress_external_dismiss_briefly(self) -> None:
        self._external_dismiss_suppressed = True
        self._external_dismiss_resume_timer.start(180)

    def _resume_external_dismiss(self) -> None:
        self._external_dismiss_suppressed = False

    def _has_active_popup_or_child_modal(self) -> bool:
        if QApplication.activePopupWidget() is not None:
            return True
        active_modal = QApplication.activeModalWidget()
        return active_modal is not None and active_modal is not self

    def _global_geometry(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def _mouse_global_position(self, event: QMouseEvent) -> QPoint | None:
        with suppress(AttributeError):
            return event.globalPosition().toPoint()
        with suppress(AttributeError):
            return event.globalPos()
        return None

    def _poll_external_dismiss(self) -> None:
        self._poll_external_dismiss_state()

    def _poll_external_dismiss_state(
        self,
        *,
        buttons: object | None = None,
        cursor_pos: QPoint | None = None,
    ) -> bool:
        if self._closing or not self.isVisible() or self._has_active_popup_or_child_modal():
            return False
        if self._external_dismiss_suppressed:
            return False
        active_buttons = QApplication.mouseButtons() if buttons is None else buttons
        if not bool(active_buttons):
            self._external_dismiss_armed = True
            return False
        if not self._external_dismiss_armed:
            return False
        global_pos = QCursor.pos() if cursor_pos is None else cursor_pos
        return self._dismiss_for_external_point(global_pos)

    def _dismiss_for_external_point(self, global_pos: QPoint) -> bool:
        if not self._external_dismiss_armed:
            return False
        if self._global_geometry().contains(global_pos):
            return False
        self.reject()
        return True

    def _emit_settings_changed(self, *_args: object) -> None:
        if not self._updating_controls:
            self.settings_changed.emit(self.values())

    def apply_settings(self, settings: AppSettings) -> None:
        """Reflect the persisted value while leaving the dialog open."""
        self._suppress_external_dismiss_briefly()
        self._updating_controls = True
        try:
            hotkey = settings.hotkey
            if self.hotkey_mode is None:
                self.custom_hotkey.setKeySequence(QKeySequence(_hotkey_display(hotkey)))
            else:
                label = next(
                    (label for label, value in self._available_hotkeys.items() if value == hotkey),
                    "自定义组合键",
                )
                self.hotkey_mode.setCurrentText(label)
                if hotkey.startswith("combo:"):
                    self.custom_hotkey.setKeySequence(QKeySequence(_hotkey_display(hotkey)))
                self.custom_hotkey.setEnabled(label == "自定义组合键")
            self.interval.setValue(settings.double_tap_interval_ms)
            self.maximum.setValue(settings.max_history_items)
            self.retention.setValue(settings.retention_days)
            self.delay.setValue(settings.paste_delay_ms)
            self.theme.setCurrentIndex(max(0, self.theme.findData(_settings_theme_key(settings.theme))))
            self.capture.setChecked(settings.capture_enabled)
            self.paste.setChecked(settings.paste_after_selection)
            self.hide_on_deactivate_checkbox.setChecked(settings.hide_on_deactivate)
            self.remember_selection.setChecked(settings.remember_selection)
            self.selection_memory.setValue(settings.selection_memory_seconds)
            self.selection_memory.setEnabled(settings.remember_selection)
            self.launch_at_login.setChecked(settings.launch_at_login)
            self._plain_text_compat_enabled = bool(settings.plain_text_compat_enabled)
            self._plain_text_target_apps = tuple(settings.plain_text_target_apps)
            if self.plain_text_compat is not None:
                self.plain_text_compat.setChecked(settings.plain_text_compat_enabled)
                self._sync_target_app_controls()
                self._refresh_running_apps()
        finally:
            self._updating_controls = False

        self._apply_theme(settings)

    def _apply_theme(self, settings: AppSettings) -> None:
        self._theme_settings = settings
        self._appearance = _theme_appearance(settings)
        self._dark_theme = self._appearance.dark
        colors = _theme_colors(self._appearance)
        if sys.platform == "win32":
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor(colors.window))
            self.setPalette(palette)
        else:
            # Do not toggle ``WA_TranslucentBackground`` after the window is
            # visible: on macOS that leaves a frosted -> dark root transparent.
            # ``paintEvent`` supplies the opaque normal-theme shell instead.
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
            self.setPalette(palette)
            self.setAutoFillBackground(False)
        self.setStyleSheet(
            _style_sheet(
                self._appearance,
                dialog_transparent=sys.platform != "win32",
            )
        )
        for editor in self._editable_text_inputs:
            _apply_text_input_palette(editor, colors, self._appearance)
        for caret in self._text_carets:
            caret.set_color(QColor(colors.text))
        self.update()
        for checkbox in self._settings_checkboxes:
            checkbox.set_theme(self._appearance)
        for combo in (
            self.hotkey_mode,
            self.theme,
            self.running_apps_combo,
            self.target_apps_combo,
        ):
            if combo is not None:
                _style_combo_popup(combo, self._appearance)
        if sys.platform == "win32":
            # QSS may reset this property while polishing; restore the
            # non-layered backing-store contract after every theme change.
            self.setAutoFillBackground(True)
        self._sync_windows_rounded_window_mask()

    def _sync_windows_rounded_window_mask(self) -> None:
        if sys.platform == "win32":
            _apply_rounded_widget_mask(self, _SETTINGS_WINDOW_RADIUS)
        else:
            self.clearMask()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_windows_rounded_window_mask()
        self._install_external_dismiss_filter()
        QTimer.singleShot(0, self._focus_initial_control)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_windows_rounded_window_mask()

    def _focus_initial_control(self) -> None:
        if self.isVisible() and self.focusWidget() in (None, self.close_button):
            self._initial_focus_control.setFocus(Qt.FocusReason.OtherFocusReason)

    def center_on_widget(self, anchor: QWidget | None) -> None:
        if anchor is None or not anchor.isVisible():
            return
        self.adjustSize()
        own_size = self.size()
        anchor_geometry = anchor.frameGeometry()
        target = anchor_geometry.center() - QPoint(own_size.width() // 2, own_size.height() // 2)
        screen = QApplication.screenAt(anchor_geometry.center()) or anchor.screen()
        if screen is not None:
            available = screen.availableGeometry()
            target.setX(min(max(target.x(), available.left()), available.right() - own_size.width() + 1))
            target.setY(min(max(target.y(), available.top()), available.bottom() - own_size.height() + 1))
        self.move(target)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
        if self._appearance.frosted:
            _paint_frosted_material(
                painter,
                rect,
                self._appearance,
                radius=16.0,
            )
            return
        if sys.platform != "win32":
            # This solid app-owned shell is deliberately painted into the
            # stable transparent top-level so a live frosted -> dark switch
            # cannot expose the system's light backing around the sections.
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            shell = QPainterPath()
            shell.addRoundedRect(rect, 16.0, 16.0)
            painter.fillPath(shell, QColor(_theme_colors(self._appearance).window))
            painter.setPen(
                QPen(
                    QColor(255, 255, 255, 26)
                    if self._appearance.dark
                    else QColor(45, 53, 76, 28),
                    1.0,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(shell)
            painter.restore()

    def _reset(self) -> None:
        self._suppress_external_dismiss_briefly()
        defaults = AppSettings(
            hotkey=WINDOWS_DEFAULT_HOTKEY if sys.platform == "win32" else AppSettings().hotkey
        )
        self.apply_settings(defaults)
        self._emit_settings_changed()

    def _confirm_clear(self) -> None:
        if _confirm_destructive_action(
            self,
            "清除历史",
            "清除所有非收藏历史？此操作无法撤销。",
            "清除历史",
            appearance=self._appearance,
        ):
            self.clear_requested.emit()

    def _request_reveal(self) -> None:
        # A modal settings window owned by the floating panel can otherwise
        # remain above Finder/Explorer and make a successful open look broken.
        self.setVisible(False)
        self.reject()
        self.reveal_requested.emit()


class ClipPanel(QWidget):
    send_requested = Signal(object)
    settings_requested = Signal()
    delete_requested = Signal(object)
    favorite_requested = Signal(object, bool)
    clear_requested = Signal(object)
    clear_non_favorites_requested = Signal()
    accessibility_requested = Signal()
    position_changed = Signal(int, int)

    def __init__(
        self,
        settings: Callable[[], AppSettings],
        *,
        selection_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _install_app_owned_caret_style()
        super().__init__()
        self._settings = settings
        self._selection_clock = selection_clock
        self._items: list[ClipItem] = []
        self._engine = SearchEngine()
        self._kind: HistoryFilter = None
        self._keep_open = False
        self._native_deactivation_managed = sys.platform == "win32"
        self._selection_anchor = 0
        self._remembered_search_text = ""
        self._remembered_kind: HistoryFilter = None
        self._remembered_item_ids: tuple[str, ...] = ()
        self._remembered_current_id: str | None = None
        self._selection_hidden_at: float | None = None
        self._selection_hide_prepared = False
        self._search_ime_composing = False
        self._search_focus_restore_pending = False
        self._settings_interaction_blocked = False
        self._selection_memory_timer = QTimer(self)
        self._selection_memory_timer.setSingleShot(True)
        self._selection_memory_timer.timeout.connect(self._expire_selection_memory)
        self._filter_buttons: list[tuple[QToolButton, HistoryFilter]] = []
        self._filter_index = 0
        self._dark_theme = False
        self._appearance = _ThemeAppearance(dark=False)
        self._drag_offset: QPoint | None = None
        self._drag_origin: QPoint | None = None
        self._settings_interaction_shield: _PanelInteractionShield | None = None
        self._image_viewer_dialog: ImageViewerDialog | None = None
        self._panel_shortcuts: list[QShortcut] = []
        self.setObjectName("panelWindow")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if sys.platform == "win32":
            # A translucent frameless top-level takes Qt's layered-window path.
            # Windows rejects any dirty rectangle that extends even slightly
            # outside that backing store, leaving an invisible-but-visible panel.
            self.setAutoFillBackground(True)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        metrics = _ui_metrics()
        self.setMinimumSize(metrics.panel_min_width, metrics.panel_min_height)
        self.resize(metrics.panel_initial_width, metrics.panel_initial_height)
        self._build()
        self._install_panel_shortcuts()
        self.apply_theme()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        margin = _panel_outer_margin()
        outer.setContentsMargins(margin, margin, margin, margin)
        self._outer_layout = outer
        self.card = _FrostedSurface()
        self.card.setObjectName("card")
        self.card.material_changed.connect(self.update)
        outer.addWidget(self.card)
        root = QVBoxLayout(self.card)
        root.setContentsMargins(12, 10, 12, 8)
        # Major regions are separated by a quiet 1 px rule with three points
        # of air on each side. This keeps the existing command-panel density
        # while making search, filters, content, and footer scannable.
        root.setSpacing(3)

        def section_divider(object_name: str) -> QFrame:
            divider = QFrame()
            divider.setObjectName(object_name)
            divider.setFrameShape(QFrame.Shape.NoFrame)
            divider.setFixedHeight(1)
            divider.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            return divider

        search_row = QHBoxLayout()
        search_row.setContentsMargins(4, 0, 0, 0)
        self.search_box = QFrame()
        self.search_box.setObjectName("searchBox")
        self.search_box.setFrameShape(QFrame.Shape.NoFrame)
        search_layout = QHBoxLayout(self.search_box)
        search_layout.setContentsMargins(8, 0, 8, 0)
        search_layout.setSpacing(5)
        self.search_icon = SearchIcon()
        self.search_icon.clicked.connect(self._request_settings)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索剪贴板历史…")
        self.search.setClearButtonEnabled(True)
        search_layout.addWidget(self.search_icon)
        search_layout.addWidget(self.search, 1)
        search_row.addWidget(self.search_box, 1)
        root.addLayout(search_row)
        self.search_filters_divider = section_divider("searchFiltersDivider")
        root.addWidget(self.search_filters_divider)
        self._search_caret = _ThemedTextCaret(self.search)

        filters = QHBoxLayout()
        filters.setContentsMargins(4, 0, 0, 0)
        filters.setSpacing(7)
        filters_by_kind: tuple[tuple[str, HistoryFilter], ...] = (
            ("收藏", FAVORITES_FILTER),
            ("全部", None),
            ("文本", ClipKind.TEXT),
            ("截图", ClipKind.IMAGE),
            ("文件", ClipKind.FILES),
        )
        for index, (label, kind) in enumerate(filters_by_kind):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("filterChip", True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setChecked(kind is None)
            if kind is None:
                self._filter_index = index
            button.clicked.connect(lambda _checked=False, kind=kind, button=button: self._filter(kind, button))
            self._filter_buttons.append((button, kind))
            filters.addWidget(button)
        filters.addStretch()
        self.count_label = QLabel("0 条")
        self.count_label.setObjectName("muted")
        filters.addWidget(self.count_label)
        root.addLayout(filters)

        content = QHBoxLayout()
        # One quiet divider establishes the list/detail boundary in every
        # theme. Four points on each side preserve the original visual gap
        # without wrapping the detail column in a second card.
        content.setSpacing(4)
        self.model = ClipListModel()
        self.list = ClipListView()
        self.list.setObjectName("historyList")
        self.list.setModel(self.model)
        delegate = ClipDelegate(self.list)
        self.list.setItemDelegate(delegate)
        self.list.hover_index_changed.connect(delegate.set_hovered_index)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Selection remains mouse-driven and arrow navigation is routed from
        # the search editor.  Letting the view receive focus would hide the
        # only text caret while the panel is still active.
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMinimumWidth(_ui_metrics().list_min_width)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._open_list_menu)
        self.list.doubleClicked.connect(lambda index: self._send_row(index.row()))
        self.list.selectionModel().currentChanged.connect(lambda current, _previous: self._show_detail(current.row()))
        self.history_content = QStackedWidget()
        self.history_content.setObjectName("historyContent")
        self.history_content.addWidget(self.list)
        self.empty_state = QWidget()
        self.empty_state.setObjectName("emptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(24, 12, 24, 12)
        empty_layout.setSpacing(4)
        empty_layout.addStretch(1)
        self.empty_state_title = QLabel()
        self.empty_state_title.setObjectName("emptyStateTitle")
        self.empty_state_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_state_message = QLabel()
        self.empty_state_message.setObjectName("emptyStateMessage")
        self.empty_state_message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty_state_message.setWordWrap(True)
        self.empty_state_clear = QToolButton()
        self.empty_state_clear.setObjectName("emptyStateClear")
        self.empty_state_clear.setText("清除搜索")
        self.empty_state_clear.setAutoRaise(True)
        self.empty_state_clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.empty_state_clear.clicked.connect(self._clear_empty_search)
        empty_layout.addWidget(self.empty_state_title, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(self.empty_state_message, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(self.empty_state_clear, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(1)
        self.history_content.addWidget(self.empty_state)
        content.addWidget(self.history_content, 3)

        self.content_divider = QFrame()
        self.content_divider.setObjectName("contentDivider")
        self.content_divider.setFrameShape(QFrame.Shape.NoFrame)
        self.content_divider.setFixedWidth(1)
        self.content_divider.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        self.content_divider.hide()
        content.addWidget(self.content_divider)

        self.detail = QFrame()
        self.detail.setObjectName("detail")
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(6)
        self.preview_stack = QStackedWidget()
        self.text_preview = QPlainTextEdit()
        self.text_preview.setObjectName("textPreview")
        self.text_preview.setReadOnly(True)
        self.text_preview.setFrameShape(QFrame.Shape.NoFrame)
        self.text_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.text_preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.text_preview.customContextMenuRequested.connect(
            lambda position: self._open_preview_context_menu(self.text_preview, position)
        )
        self.text_preview.selectionChanged.connect(
            lambda: self._clear_search_selection_for_preview(self.text_preview)
        )
        self.image_preview = ImagePreview()
        self.image_preview.activated.connect(self._open_image_viewer)
        self.file_preview = QLabel()
        self.file_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_text_preview = TextFilePreview()
        self.file_text_preview.setObjectName("fileTextPreview")
        self.file_text_preview.setReadOnly(True)
        self.file_text_preview.setFrameShape(QFrame.Shape.NoFrame)
        self.file_text_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.file_text_preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_text_preview.customContextMenuRequested.connect(
            lambda position: self._open_preview_context_menu(self.file_text_preview, position)
        )
        self.file_text_preview.selectionChanged.connect(
            lambda: self._clear_search_selection_for_preview(self.file_text_preview)
        )
        self.file_text_preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.file_text_preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview_stack.addWidget(self.text_preview)
        self.preview_stack.addWidget(self.image_preview)
        self.preview_stack.addWidget(self.file_preview)
        self.preview_stack.addWidget(self.file_text_preview)
        detail_layout.addWidget(self.preview_stack, 1)
        self.information_divider = QFrame()
        self.information_divider.setObjectName("informationDivider")
        self.information_divider.setFrameShape(QFrame.Shape.NoFrame)
        self.information_divider.setFixedHeight(1)
        detail_layout.addWidget(self.information_divider)
        information_content = QWidget()
        information_content.setObjectName("informationContent")
        information_content_layout = QVBoxLayout(information_content)
        # The divider is intentionally inset by 8 px. Keep the information
        # title and labels on that same visual baseline instead of letting
        # their default QLabel margins start further left.
        information_content_layout.setContentsMargins(8, 0, 8, 0)
        information_content_layout.setSpacing(6)
        self.information_title = QLabel("信息")
        self.information_title.setObjectName("informationTitle")
        information_content_layout.addWidget(self.information_title)
        information = QGridLayout()
        information.setContentsMargins(0, 0, 0, 0)
        information.setHorizontalSpacing(18)
        information.setVerticalSpacing(10)
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
        information_content_layout.addLayout(information)
        detail_layout.addWidget(information_content)
        content.addWidget(self.detail, 2)
        root.addLayout(content, 1)
        self.content_footer_divider = section_divider("contentFooterDivider")
        root.addWidget(self.content_footer_divider)

        footer = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.status.linkActivated.connect(lambda _link: self.accessibility_requested.emit())
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.clear_status)
        hints = QLabel(f"↑↓ 选择  |  ↵ 发送  |  Esc 隐藏  |  v{__version__}")
        hints.setObjectName("muted")
        self.version_label = hints
        footer.addWidget(self.status)
        footer.addStretch()
        footer.addWidget(hints)
        root.addLayout(footer)
        self.search.textChanged.connect(lambda _text: self._refresh_results())
        self.search.installEventFilter(self)
        self.list.installEventFilter(self)
        self._install_frosted_tracking()
        self._settings_interaction_shield = _PanelInteractionShield(self.card)
        self._sync_settings_interaction_shield_geometry()

    def _install_frosted_tracking(self) -> None:
        """Let one material shell react to pointer movement above its content."""

        for widget in self.findChildren(QWidget):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    def _update_frosted_light(self, global_position: QPointF) -> None:
        if not self._appearance.frosted:
            return
        position = self.card.mapFromGlobal(global_position.toPoint())
        self.card.set_light_position(QPointF(position))
        self.card.set_light_active(True)

    def set_items(
        self,
        items: Sequence[ClipItem],
        *,
        operation_context: _ListOperationContext | None = None,
    ) -> None:
        new_items = list(items)
        removed_image_paths = _item_image_paths(self._items) - _item_image_paths(new_items)
        if removed_image_paths:
            delegate = self.list.itemDelegate()
            if isinstance(delegate, ClipDelegate):
                delegate.invalidate_paths(removed_image_paths)
            self.image_preview.invalidate_paths(removed_image_paths)
        self._items = new_items
        self._engine.replace(self._items)
        self._refresh_results(operation_context=operation_context)

    def set_status(self, text: str, timeout_ms: int = _STATUS_TIMEOUT_MS) -> None:
        self._status_timer.stop()
        self.status.setText(text)
        if text and timeout_ms > 0:
            self._status_timer.start(timeout_ms)

    def clear_status(self) -> None:
        self._status_timer.stop()
        self.status.clear()

    def set_accessibility_warning(self) -> None:
        self._status_timer.stop()
        self.status.setText(
            '需要授予 ClipSoon 辅助功能权限 · <a href="accessibility">打开系统设置</a>'
        )

    def has_accessibility_warning(self) -> bool:
        return 'href="accessibility"' in self.status.text()

    def show_panel(self) -> float:
        started = time.perf_counter()
        settings = self._settings()
        saved_position = (
            QPoint(settings.panel_x, settings.panel_y)
            if settings.panel_x is not None and settings.panel_y is not None
            else None
        )
        screen = (
            QApplication.screenAt(saved_position)
            if saved_position is not None
            else None
        ) or QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        geometry = screen.availableGeometry()
        metrics = _ui_metrics()
        width = min(
            metrics.panel_max_width,
            max(metrics.panel_min_width, int(geometry.width() * metrics.panel_width_ratio)),
        )
        height = min(
            metrics.panel_max_height,
            max(metrics.panel_min_height, int(geometry.height() * metrics.panel_height_ratio)),
        )
        self.resize(width, height)
        if saved_position is None:
            saved_position = QPoint(
                geometry.left() + (geometry.width() - width) // 2,
                geometry.top() + max(34, int(geometry.height() * 0.13)),
            )
        self.move(self._bounded_position(saved_position, geometry))
        restore_state = settings.remember_selection and self._selection_memory_is_valid(settings)
        target_kind = self._remembered_kind if restore_state else None
        search_text = self._remembered_search_text if restore_state else ""
        kind_changed = self._kind != target_kind
        self._set_filter_kind(target_kind)
        if self.search.text() != search_text:
            self.search.setText(search_text)
        elif kind_changed:
            self._refresh_results()
        self._select_for_show(restore=restore_state)
        self._selection_hide_prepared = False
        self._search_ime_composing = False
        self.show()
        self.raise_()
        if self._settings_interaction_blocked:
            self.set_settings_interaction_blocked(True)
        else:
            self.activateWindow()
            self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            self._schedule_search_focus_restore()
        return (time.perf_counter() - started) * 1_000

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._settings_interaction_blocked:
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._update_frosted_light(event.globalPosition())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._can_start_drag(event.position().toPoint())
        ):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_origin = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._settings_interaction_blocked:
            event.accept()
            return
        self._update_frosted_light(event.globalPosition())
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            screen = QApplication.screenAt(event.globalPosition().toPoint()) or self.screen()
            target = event.globalPosition().toPoint() - self._drag_offset
            self.move(self._bounded_position(target, screen.availableGeometry()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        if self._settings_interaction_blocked:
            return
        self._update_frosted_light(QPointF(QCursor.pos()))

    def leaveEvent(self, event) -> None:
        self.card.set_light_active(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._settings_interaction_blocked:
            event.accept()
            return
        if self._drag_offset is not None and event.button() == Qt.MouseButton.LeftButton:
            origin = self._drag_origin
            self._drag_offset = None
            self._drag_origin = None
            self.unsetCursor()
            if origin is not None and self.pos() != origin:
                self.position_changed.emit(self.x(), self.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._settings_interaction_blocked:
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._settings_interaction_blocked:
            event.accept()
            return
        super().keyReleaseEvent(event)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        if self._settings_interaction_blocked:
            self._search_ime_composing = False
            event.accept()
            return
        super().inputMethodEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_windows_rounded_window_mask()
        self._sync_settings_interaction_shield_geometry()
        if self._settings_interaction_blocked:
            QTimer.singleShot(0, self._refresh_settings_interaction_shield)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if sys.platform != "win32" or not self._appearance.frosted:
            return
        painter = QPainter(self)
        light_position = None
        light_strength = self.card._light_strength
        if light_strength > 0:
            light_position = QPointF(self.card.mapTo(self, self.card._light_position.toPoint()))
        _paint_frosted_material(
            painter,
            QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6),
            self._appearance,
            light_position=light_position,
            light_strength=light_strength,
        )

    def _can_start_drag(self, position: QPoint) -> bool:
        if self._settings_interaction_blocked:
            return False
        widget = self.childAt(position)
        blocked = (QAbstractButton, QAbstractItemView, QLineEdit, QPlainTextEdit, QLabel, QStackedWidget)
        while widget is not None and widget is not self:
            if widget is self.search_icon or isinstance(widget, blocked):
                return False
            widget = widget.parentWidget()
        return True

    def _bounded_position(self, position: QPoint, geometry: QRect) -> QPoint:
        maximum_x = max(geometry.left(), geometry.right() - self.width() + 1)
        maximum_y = max(geometry.top(), geometry.bottom() - self.height() + 1)
        return QPoint(
            min(max(position.x(), geometry.left()), maximum_x),
            min(max(position.y(), geometry.top()), maximum_y),
        )

    def hideEvent(self, event) -> None:
        self._prepare_selection_for_hide()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_windows_rounded_window_mask()

    def hide_panel(self) -> None:
        if not self.isVisible():
            return
        self._prepare_selection_for_hide()
        self.hide()

    def _prepare_selection_for_hide(self) -> None:
        if self._selection_hide_prepared:
            return
        self._selection_hide_prepared = True
        if self._settings().remember_selection:
            self._capture_selection_memory()
        else:
            self._clear_selection_memory()

    def _select_for_show(self, *, restore: bool | None = None) -> None:
        selection_model = self.list.selectionModel()
        count = self.model.rowCount()
        if not count:
            selection_model.clearSelection()
            selection_model.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)
            self._show_detail(-1)
            return

        if restore is None:
            settings = self._settings()
            restore = settings.remember_selection and self._selection_memory_is_valid(settings)
        rows_by_id = {
            item.id: row
            for row in range(count)
            if (item := self.model.item_at(row)) is not None
        }
        selected_rows = (
            [
                rows_by_id[item_id]
                for item_id in self._remembered_item_ids
                if item_id in rows_by_id
            ]
            if restore
            else []
        )
        if not selected_rows:
            selected_rows = [0]
            if restore:
                self._clear_selection_memory()

        current_row = rows_by_id.get(self._remembered_current_id, selected_rows[0]) if restore else 0
        current = self.model.index(current_row)
        selection_model.clearSelection()
        for row in selected_rows:
            selection_model.select(
                self.model.index(row),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        selection_model.setCurrentIndex(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._selection_anchor = current_row
        self._show_detail(current_row)
        self.list.scrollTo(
            current,
            QAbstractItemView.ScrollHint.EnsureVisible
            if restore
            else QAbstractItemView.ScrollHint.PositionAtTop,
        )

    def _selection_memory_is_valid(self, settings: AppSettings) -> bool:
        if self._selection_hidden_at is None:
            return False
        elapsed = self._selection_clock() - self._selection_hidden_at
        if 0 <= elapsed <= settings.selection_memory_seconds:
            return True
        self._clear_selection_memory()
        return False

    def _capture_selection_memory(self) -> None:
        settings = self._settings()
        self._remembered_search_text = self.search.text()
        self._remembered_kind = self._kind
        rows = sorted(index.row() for index in self.list.selectionModel().selectedRows())
        self._remembered_item_ids = tuple(
            item.id for row in rows if (item := self.model.item_at(row)) is not None
        )
        current = self.model.item_at(self.list.currentIndex().row())
        self._remembered_current_id = current.id if current is not None else None
        # A query with no matches is still meaningful state, so the snapshot is
        # timestamped even when there is no selected item.
        self._selection_hidden_at = self._selection_clock()
        self._selection_memory_timer.start(settings.selection_memory_seconds * 1_000)

    def _clear_selection_memory(self) -> None:
        self._selection_memory_timer.stop()
        self._remembered_search_text = ""
        self._remembered_kind = None
        self._remembered_item_ids = ()
        self._remembered_current_id = None
        self._selection_hidden_at = None

    def _expire_selection_memory(self) -> None:
        self._clear_selection_memory()
        if not self.isVisible():
            self._set_filter_kind(None)
            if self.search.text():
                self.search.clear()
            elif self._kind is None:
                self._refresh_results()
            self._select_for_show(restore=False)

    def apply_theme(self) -> None:
        settings = self._settings()
        self._appearance = _theme_appearance(settings)
        self._dark_theme = self._appearance.dark
        self.card.set_appearance(self._appearance)
        self.card.set_paint_material(sys.platform != "win32")
        # A theme must not change the panel's visible card geometry.
        margin = _panel_outer_margin()
        self._outer_layout.setContentsMargins(margin, margin, margin, margin)
        colors = _theme_colors(self._appearance)
        if sys.platform == "win32":
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor(colors.window))
            self.setPalette(palette)
        delegate = self.list.itemDelegate()
        if isinstance(delegate, ClipDelegate):
            delegate.set_theme(self._appearance)
        self.search_icon.set_theme(self._appearance)
        self.setStyleSheet(_style_sheet(self._appearance))
        # Keep the text palette and the application-owned blinking caret on
        # the same token after QSS polishing. The global proxy guarantees that
        # macOS cannot reveal its native white caret during the hidden phase.
        self._apply_search_input_palette(colors)
        self._search_caret.set_color(QColor(colors.text))
        if self._settings_interaction_shield is not None:
            self._settings_interaction_shield.set_appearance(self._appearance)
            if self._settings_interaction_blocked:
                QTimer.singleShot(0, self._refresh_settings_interaction_shield)
        if self._image_viewer_dialog is not None:
            self._image_viewer_dialog.set_appearance(self._appearance)
        if sys.platform == "win32":
            # QSS may reset this property while polishing; restore the
            # non-layered backing-store contract after every theme change.
            self.setAutoFillBackground(True)
        self._sync_windows_rounded_window_mask()
        self._sync_search_box_height()

    def _sync_windows_rounded_window_mask(self) -> None:
        if sys.platform == "win32":
            _apply_rounded_widget_mask(self, _FROSTED_RADIUS + _WINDOWS_PANEL_EDGE_GUARD)
        else:
            self.clearMask()

    def _sync_search_box_height(self) -> None:
        """Keep each search-region gap at half the visible search-text height."""
        self.search.ensurePolished()
        metrics = QFontMetrics(self.search.font())
        text_height = metrics.tightBoundingRect("Ag").height() or metrics.height()
        # One text-height is reserved for the glyphs and the other for the
        # combined top and bottom breathing space. The search area itself is
        # deliberately borderless in every appearance.
        self.search_box.setFixedHeight(text_height * 2)

    def _sync_settings_interaction_shield_geometry(self) -> None:
        if self._settings_interaction_shield is None:
            return
        self._settings_interaction_shield.setGeometry(self.card.rect())

    def _refresh_settings_interaction_shield(self) -> None:
        if self._settings_interaction_shield is None or not self._settings_interaction_blocked or not self.isVisible():
            return
        self._sync_settings_interaction_shield_geometry()
        self._settings_interaction_shield.set_appearance(self._appearance)
        self._settings_interaction_shield.refresh_from(self.card)
        self._settings_interaction_shield.show()
        self._settings_interaction_shield.raise_()

    def set_settings_interaction_blocked(self, blocked: bool) -> None:
        blocked = bool(blocked)
        self._settings_interaction_blocked = blocked
        if blocked:
            self._drag_offset = None
            self._drag_origin = None
            self.unsetCursor()
            delegate = self.list.itemDelegate()
            if isinstance(delegate, ClipDelegate):
                delegate.set_hovered_index(QModelIndex())
            self.card.set_light_active(False)
            self.search.clearFocus()
            if self.isVisible():
                self._refresh_settings_interaction_shield()
            return
        if self._settings_interaction_shield is not None:
            self._settings_interaction_shield.hide()
        self.card.update()
        self.update()

    def _apply_search_input_palette(self, colors: _ThemeColors) -> None:
        """Keep the search text and themed blinking caret readable per theme."""

        _apply_text_input_palette(self.search, colors, self._appearance)

    def _schedule_search_focus_restore(self) -> None:
        """Restore the panel's sole input target after an in-panel click."""

        if self._search_focus_restore_pending:
            return
        if self._settings_interaction_blocked:
            return
        self._search_focus_restore_pending = True
        QTimer.singleShot(0, self._restore_search_focus_if_panel_active)

    def _restore_search_focus_if_panel_active(self) -> None:
        self._search_focus_restore_pending = False
        if (
            not self.isVisible()
            or self._settings_interaction_blocked
            or not self.isActiveWindow()
            or QApplication.activeModalWidget() is not None
            or QApplication.activePopupWidget() is not None
            or self.search.hasFocus()
        ):
            return
        self.search.setFocus(Qt.FocusReason.OtherFocusReason)

    def _open_image_viewer(self, path: str) -> None:
        if not path or self._settings_interaction_blocked:
            return
        if self._image_viewer_dialog is not None and self._image_viewer_dialog.isVisible():
            self._image_viewer_dialog.canvas.set_path(path)
            self._image_viewer_dialog.center_on_widget(self)
            self._image_viewer_dialog.raise_()
            self._image_viewer_dialog.activateWindow()
            return
        self.keep_open(True)
        dialog = ImageViewerDialog(path, self._appearance, self)
        self._image_viewer_dialog = dialog

        def viewer_finished() -> None:
            if self._image_viewer_dialog is not dialog:
                return
            self._image_viewer_dialog = None
            if not self._settings_interaction_blocked:
                self._restore_after_image_viewer()
                self.keep_open(False)
            dialog.deleteLater()

        dialog.finished.connect(viewer_finished)
        dialog.center_on_widget(self)
        dialog.show()
        dialog.center_on_widget(self)
        dialog.raise_()
        dialog.activateWindow()
        dialog.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _restore_after_image_viewer(self) -> None:
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._schedule_search_focus_restore()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._settings_interaction_blocked and self._is_blocked_panel_input_event(event):
            if watched is self.search and event.type() == QEvent.Type.InputMethod:
                self._search_ime_composing = False
            return True
        if (
            self._appearance.frosted
            and isinstance(event, QMouseEvent)
            and (
                event.type() == QEvent.Type.MouseMove
                or (
                    event.type() == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                )
            )
        ):
            self._update_frosted_light(event.globalPosition())
        if watched is self.search and event.type() == QEvent.Type.InputMethod:
            input_event = event if isinstance(event, QInputMethodEvent) else None
            if input_event is not None:
                self._search_ime_composing = bool(input_event.preeditString())
            return super().eventFilter(watched, event)
        if watched is self.search and event.type() == QEvent.Type.FocusOut:
            self._search_ime_composing = False
            # A list, preview or incidental native child can still request
            # focus on some platform styles. Reclaim it after that handler
            # finishes, but never steal it from a modal dialog or a context
            # menu owned by the panel.
            self._schedule_search_focus_restore()
            return super().eventFilter(watched, event)
        if (
            watched is not self.search
            and isinstance(watched, QWidget)
            and event.type() == QEvent.Type.FocusIn
        ):
            # The NoFocus policies cover ordinary clicks. This catches a
            # platform control or an explicit focus request that bypasses
            # them, while the deferred callback lets that interaction finish.
            self._schedule_search_focus_restore()
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        key_event = event if isinstance(event, QKeyEvent) else None
        if key_event is None:
            return False
        if _matches_copy_shortcut(key_event):
            # Search owns keyboard focus by contract. Preserve its normal copy
            # semantics when it has a fresh selection; otherwise route the
            # platform copy shortcut to the visibly selected preview text.
            if not self.search.hasSelectedText() and self._copy_preview_selection_if_available():
                return True
            return super().eventFilter(watched, event)
        if self._handle_panel_shortcut(key_event):
            return True
        key = key_event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            reverse = key == Qt.Key.Key_Backtab or bool(
                key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            self._cycle_filter(-1 if reverse else 1)
            self.search.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        if key == Qt.Key.Key_Escape:
            self.hide_panel()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if watched is self.search and self._search_ime_composing:
                # If an input method has left a composition in the editor,
                # this Enter belongs to that composition rather than a send.
                # In normal GUI operation the IME consumes it before Qt sends
                # this event; accepting the residual event prevents it from
                # bubbling into the panel on minimal/offscreen backends.
                return True
            self._send_selected()
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
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
            and not self._settings_interaction_blocked
        ):
            self._schedule_search_focus_restore()
            return
        if (
            event.type() == QEvent.Type.ActivationChange
            and not self._native_deactivation_managed
            and not self.isActiveWindow()
            and self.isVisible()
            and not self._keep_open
            and self._settings().hide_on_deactivate
        ):
            QTimer.singleShot(35, self._hide_if_unfocused)

    def _hide_if_unfocused(self) -> None:
        if not self.isActiveWindow() and QApplication.activeModalWidget() is None and not self._keep_open:
            self.hide_panel()

    def keep_open(self, value: bool) -> None:
        self._keep_open = value

    def is_kept_open(self) -> bool:
        return self._keep_open

    def _is_blocked_panel_input_event(self, event: QEvent) -> bool:
        blocked_events = {
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
            QEvent.Type.ShortcutOverride,
            QEvent.Type.InputMethod,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.MouseMove,
            QEvent.Type.HoverEnter,
            QEvent.Type.HoverMove,
            QEvent.Type.HoverLeave,
            QEvent.Type.Wheel,
            QEvent.Type.ContextMenu,
            QEvent.Type.FocusIn,
        }
        shortcut_event = getattr(QEvent.Type, "Shortcut", None)
        if shortcut_event is not None:
            blocked_events.add(shortcut_event)
        return event.type() in blocked_events

    def set_native_deactivation_managed(self, value: bool) -> None:
        self._native_deactivation_managed = bool(value)

    def _filter(self, kind: HistoryFilter, active: QToolButton) -> None:
        del active
        self._set_filter_kind(kind)
        self._refresh_results()

    def _set_filter_kind(self, kind: HistoryFilter) -> None:
        for index, (button, _button_kind) in enumerate(self._filter_buttons):
            active = _button_kind == kind
            button.setChecked(active)
            if active:
                self._filter_index = index
        self._kind = kind

    def _cycle_filter(self, direction: int) -> None:
        self._filter_index = (self._filter_index + direction) % len(self._filter_buttons)
        button, kind = self._filter_buttons[self._filter_index]
        self._filter(kind, button)

    def _refresh_results(self, *, operation_context: _ListOperationContext | None = None) -> None:
        started = time.perf_counter()
        favorites_only = self._kind == FAVORITES_FILTER
        kind = None if favorites_only else self._kind
        results = self._engine.rank(
            self.search.text(),
            now=time.time(),
            kind=kind,
            favorites_only=favorites_only,
            limit=500,
        )
        self.model.replace([result.item for result in results])
        self.count_label.setText(f"{len(results)} 条")
        if operation_context is not None:
            self._restore_list_operation_context(operation_context)
        elif results:
            self.list.setCurrentIndex(self.model.index(0))
            self._selection_anchor = 0
            self._show_detail(0)
        else:
            self.list.setCurrentIndex(QModelIndex())
            self._show_detail(-1)
        elapsed = (time.perf_counter() - started) * 1_000
        if elapsed > 20:
            LOGGER.warning("Search paint preparation took %.1f ms", elapsed)

    def _clear_empty_search(self) -> None:
        """Return from a no-match state without changing the active category."""

        self.search.clear()
        self.search.setFocus(Qt.FocusReason.MouseFocusReason)

    def _show_empty_state(self) -> None:
        """Explain why the history area is empty without adding another surface."""

        has_query = bool(self.search.text())
        if has_query:
            title = "没有找到匹配内容"
            message = "尝试更换关键词，或清除搜索后查看全部历史。"
        elif not self._items:
            title = "还没有剪贴板历史"
            message = "复制文本、图片或文件后，内容会出现在这里。"
        else:
            kind_name = {
                FAVORITES_FILTER: "收藏",
                ClipKind.TEXT: "文本",
                ClipKind.IMAGE: "截图",
                ClipKind.FILES: "文件",
            }.get(self._kind, "内容")
            title = f"暂无{kind_name}历史"
            message = "切换分类或继续复制内容。"
        self.empty_state_title.setText(title)
        self.empty_state_message.setText(message)
        self.empty_state.setAccessibleName(title)
        self.empty_state.setAccessibleDescription(message)
        self.empty_state_clear.setVisible(has_query)
        self.history_content.setCurrentWidget(self.empty_state)
        self.content_divider.hide()
        self.detail.hide()

    def _show_detail(self, row: int) -> None:
        item = self.model.item_at(row)
        if item is None:
            self._show_empty_state()
            self.image_preview.set_path("")
            self.text_preview.setPlainText("")
            self.preview_stack.setCurrentWidget(self.text_preview)
            self.info_type_value.clear()
            self.info_detail_label.clear()
            self.info_detail_value.clear()
            return
        self.history_content.setCurrentWidget(self.list)
        self.content_divider.show()
        self.detail.show()
        self.info_type_value.setText({ClipKind.TEXT: "文本", ClipKind.IMAGE: "图片", ClipKind.FILES: "文件"}[item.kind])
        image_path = item.image_path if item.kind is ClipKind.IMAGE else _single_image_file_path(item.files)
        if image_path:
            self.image_preview.set_path(image_path)
            self.preview_stack.setCurrentWidget(self.image_preview)
        elif item.kind is ClipKind.FILES:
            self.image_preview.set_path("")
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
            self.image_preview.set_path("")
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

    def _send_selected(self) -> None:
        items = self._selected_items()
        if items:
            self.send_requested.emit(items)
            return
        self._send_row(self.list.currentIndex().row())

    def _send_row(self, row: int) -> None:
        item = self.model.item_at(row)
        if item is not None:
            self.send_requested.emit((item,))

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

    def capture_list_operation_context(
        self,
        affected_items: Sequence[ClipItem],
    ) -> _ListOperationContext:
        rows_by_id = {
            item.id: row
            for row in range(self.model.rowCount())
            if (item := self.model.item_at(row)) is not None
        }
        selected_ids = tuple(item.id for item in self._selected_items())
        current = self.model.item_at(self.list.currentIndex().row())
        affected_rows = [
            rows_by_id[item.id]
            for item in affected_items
            if item.id in rows_by_id
        ]
        current_row = rows_by_id.get(current.id, 0) if current is not None else 0
        fallback_row = min(affected_rows) if affected_rows else current_row
        return _ListOperationContext(
            selected_ids=selected_ids,
            current_id=current.id if current is not None else None,
            fallback_row=max(0, fallback_row),
            scroll_value=self.list.verticalScrollBar().value(),
        )

    def _restore_list_operation_context(self, context: _ListOperationContext) -> None:
        row_count = self.model.rowCount()
        selection_model = self.list.selectionModel()
        if row_count <= 0:
            selection_model.clearSelection()
            selection_model.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)
            self._selection_anchor = 0
            self._show_detail(-1)
            return
        rows_by_id = {
            item.id: row
            for row in range(row_count)
            if (item := self.model.item_at(row)) is not None
        }
        selected_rows = [
            rows_by_id[item_id]
            for item_id in context.selected_ids
            if item_id in rows_by_id
        ]
        if selected_rows:
            current_row = rows_by_id.get(context.current_id or "", selected_rows[0])
        else:
            current_row = min(context.fallback_row, row_count - 1)
            selected_rows = [current_row]
        selection_model.clearSelection()
        for row in selected_rows:
            selection_model.select(
                self.model.index(row),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        current = self.model.index(current_row)
        selection_model.setCurrentIndex(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._selection_anchor = current_row
        self._show_detail(current_row)
        self.list.verticalScrollBar().setValue(
            min(context.scroll_value, self.list.verticalScrollBar().maximum())
        )
        self.list.scrollTo(current, QAbstractItemView.ScrollHint.EnsureVisible)

    def _select_all_results(self) -> None:
        row_count = self.model.rowCount()
        if row_count <= 0:
            return
        selection_model = self.list.selectionModel()
        current = self.list.currentIndex()
        current_row = current.row() if current.isValid() and 0 <= current.row() < row_count else 0
        selection_model.select(
            QItemSelection(self.model.index(0), self.model.index(row_count - 1)),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(self.model.index(current_row), QItemSelectionModel.SelectionFlag.NoUpdate)
        self._selection_anchor = current_row

    def _install_panel_shortcuts(self) -> None:
        shortcut_handlers = {
            "select_all": self._select_all_results,
            "favorite": lambda: self._request_favorite_selected(True),
            "unfavorite": lambda: self._request_favorite_selected(False),
            "delete": self._request_delete_selected,
            "clear": lambda: self._request_clear_current_kind()
            if self._has_history_in_current_kind()
            else None,
            "clear_non_favorites": lambda: self._request_clear_non_favorites()
            if self._has_non_favorites()
            else None,
            "settings": self._request_settings,
        }
        for shortcut_id, handler in shortcut_handlers.items():
            for sequence in _shortcut_sequence_texts(shortcut_id):
                shortcut = QShortcut(QKeySequence(sequence), self)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(handler)
                self._panel_shortcuts.append(shortcut)

    def _has_non_favorites(self) -> bool:
        return any(not item.is_favorite for item in self._items)

    def _handle_panel_shortcut(self, event: QKeyEvent) -> bool:
        if _matches_select_all_shortcut(event):
            self._select_all_results()
            return True
        if _matches_favorite_shortcut(event):
            self._request_favorite_selected(True)
            return True
        if _matches_unfavorite_shortcut(event):
            self._request_favorite_selected(False)
            return True
        if _matches_delete_shortcut(event):
            self._request_delete_selected()
            return True
        if _matches_clear_shortcut(event):
            if self._has_history_in_current_kind():
                self._request_clear_current_kind()
            return True
        if _matches_clear_non_favorites_shortcut(event):
            if self._has_non_favorites():
                self._request_clear_non_favorites()
            return True
        if _matches_settings_shortcut(event):
            self._request_settings()
            return True
        return False

    def _request_delete_selected(self) -> None:
        items = self._selected_items()
        if items:
            self.delete_requested.emit(items)

    def _request_favorite_selected(self, favorite: bool) -> None:
        items = self._selected_items()
        if items:
            self.favorite_requested.emit(items, favorite)

    def _request_settings(self) -> None:
        """Open settings through the same signal used by the search icon."""

        self.settings_requested.emit()

    def _has_history_in_current_kind(self) -> bool:
        if self._kind == FAVORITES_FILTER:
            return any(item.is_favorite for item in self._items)
        if self._kind is None:
            return bool(self._items)
        return any(item.kind is self._kind for item in self._items)

    def _request_clear_current_kind(self) -> None:
        kind = self._kind
        confirmation_text = {
            FAVORITES_FILTER: "清空收藏历史？此操作无法撤销。",
            None: "清空全部剪贴板历史？此操作无法撤销。",
            ClipKind.TEXT: "清空剪切板文本历史？此操作无法撤销。",
            ClipKind.IMAGE: "清空剪切板截图历史？此操作无法撤销。",
            ClipKind.FILES: "清空剪切板文件历史？此操作无法撤销。",
        }[kind]
        if _confirm_destructive_action(
            self,
            "清空历史",
            confirmation_text,
            "确定",
            appearance=self._appearance,
        ):
            self.clear_requested.emit(kind)

    def _request_clear_non_favorites(self) -> None:
        if _confirm_destructive_action(
            self,
            "清空历史",
            "清空所有非收藏历史？此操作无法撤销。",
            "确定",
            appearance=self._appearance,
        ):
            self.clear_non_favorites_requested.emit()

    def _clear_search_selection_for_preview(self, preview: QPlainTextEdit) -> None:
        """Let a newly selected preview range be the intended copy target.

        The search editor intentionally remains the sole keyboard-focus owner.
        Clearing its stale selection when a user selects preview text prevents
        Command/Ctrl+C from copying an earlier search range instead.
        """

        if preview.textCursor().hasSelection() and self.search.hasSelectedText():
            self.search.deselect()

    def _active_preview_with_selection(self) -> QPlainTextEdit | None:
        preview = self.preview_stack.currentWidget()
        if preview in (self.text_preview, self.file_text_preview) and preview.textCursor().hasSelection():
            return preview
        return None

    def _copy_preview_selection_if_available(self) -> bool:
        preview = self._active_preview_with_selection()
        if preview is None:
            return False
        preview.copy()
        return True

    def _create_preview_menu(self, preview: QPlainTextEdit) -> tuple[QMenu, QAction, QAction]:
        """Create the two localised actions available for selectable previews."""

        menu = QMenu(preview)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(preview.textCursor().hasSelection())
        copy_action.triggered.connect(preview.copy)
        select_all_action = menu.addAction("全选")
        select_all_action.setEnabled(bool(preview.toPlainText()))
        select_all_action.triggered.connect(preview.selectAll)
        _compact_menu(menu, appearance=self._appearance)
        return menu, copy_action, select_all_action

    def _open_preview_context_menu(self, preview: QPlainTextEdit, position: QPoint) -> None:
        menu, _copy_action, _select_all_action = self._create_preview_menu(preview)
        menu.exec(preview.mapToGlobal(position))

    def _create_list_menu(
        self,
    ) -> tuple[QMenu, QAction, QAction, QAction, QAction, QAction, QAction, QAction]:
        menu = _CompactMenu(self.list)

        def add_menu_action(icon_name: str, text: str, shortcut_id: str) -> QAction:
            action = menu.addAction(
                _menu_action_icon(icon_name, self._appearance),
                _menu_action_text(text, shortcut_id),
            )
            _set_menu_action_shortcut(action, shortcut_id)
            action.setIconVisibleInMenu(True)
            return action

        selected_items = self._selected_items()
        select_all_action = add_menu_action("select_all", "全选", "select_all")
        select_all_action.setEnabled(self.model.rowCount() > 0)
        menu.addSeparator()
        favorite_action = add_menu_action("favorite", "收藏", "favorite")
        favorite_action.setEnabled(any(not item.is_favorite for item in selected_items))
        unfavorite_action = add_menu_action("unfavorite", "取消收藏", "unfavorite")
        unfavorite_action.setEnabled(any(item.is_favorite for item in selected_items))
        menu.addSeparator()
        delete_action = add_menu_action("delete", "删除", "delete")
        delete_action.setEnabled(bool(selected_items))
        clear_action = add_menu_action("clear", "清空", "clear")
        clear_action.setEnabled(self._has_history_in_current_kind())
        clear_non_favorites_action = add_menu_action("clear", "清空NF", "clear_non_favorites")
        clear_non_favorites_action.setEnabled(self._has_non_favorites())
        menu.addSeparator()
        settings_action = add_menu_action("settings", "设置", "settings")
        _compact_menu(menu, appearance=self._appearance)
        return (
            menu,
            select_all_action,
            favorite_action,
            unfavorite_action,
            delete_action,
            clear_action,
            clear_non_favorites_action,
            settings_action,
        )

    def _handle_list_menu_action(
        self,
        selected: QAction | None,
        select_all_action: QAction,
        delete_action: QAction,
        clear_action: QAction,
        clear_non_favorites_action: QAction,
        favorite_action: QAction,
        unfavorite_action: QAction,
        settings_action: QAction,
    ) -> None:
        if selected is select_all_action:
            self._select_all_results()
        elif selected is delete_action:
            self._request_delete_selected()
        elif selected is clear_action:
            self._request_clear_current_kind()
        elif selected is clear_non_favorites_action:
            self._request_clear_non_favorites()
        elif selected is favorite_action:
            self._request_favorite_selected(True)
        elif selected is unfavorite_action:
            self._request_favorite_selected(False)
        elif selected is settings_action:
            self._request_settings()

    def _open_list_menu(self, position) -> None:
        index = self.list.indexAt(position)
        if index.isValid() and not self.list.selectionModel().isSelected(index):
            self.list.setCurrentIndex(index)
        (
            menu,
            select_all_action,
            favorite_action,
            unfavorite_action,
            delete_action,
            clear_action,
            clear_non_favorites_action,
            settings_action,
        ) = self._create_list_menu()
        selected = menu.exec(self.list.viewport().mapToGlobal(position))
        # QMenu closes in a nested event loop and can leave the panel with no
        # focus widget or inactive state even when the list itself is NoFocus.
        # It is still the panel's own transient interaction, so reactivate the
        # panel before returning to its permanent input target.
        if self.isVisible():
            self.activateWindow()
        self._schedule_search_focus_restore()
        self._handle_list_menu_action(
            selected,
            select_all_action,
            delete_action,
            clear_action,
            clear_non_favorites_action,
            favorite_action,
            unfavorite_action,
            settings_action,
        )


def create_tray_icon(parent: QWidget) -> tuple[QSystemTrayIcon, QMenu, dict[str, QAction]]:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(_LIGHT_COLORS.accent))
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
        "show": menu.addAction("显示窗口"),
        "pause": menu.addAction("暂停记录"),
        "settings": menu.addAction("设置…"),
    }
    actions["pause"].setCheckable(True)
    menu.addSeparator()
    actions["quit"] = menu.addAction("退出")
    tray.setContextMenu(menu)
    return tray, menu, actions


def _style_sheet(
    theme: bool | _ThemeAppearance,
    *,
    dialog_transparent: bool | None = None,
) -> str:
    appearance = _as_theme_appearance(theme)
    colors = _theme_colors(appearance)
    metrics = _ui_metrics()
    font_family_rule = _platform_font_family_rule()
    search_text_rule = (
        f"color: {colors.text}; font-size: {metrics.search_font_size_pt}pt; padding: 0 2px;"
    )
    filter_chip_padding = (
        f"padding: {metrics.filter_chip_vertical_padding}px "
        f"{metrics.filter_chip_horizontal_padding}px;"
    )
    history_font_rule = f"font-size: {metrics.content_font_size_pt}pt;"
    preview_typography_rule = (
        f"font-size: {metrics.content_font_size_pt}pt; "
        f"padding: {metrics.text_preview_padding}px;"
    )
    active_foreground = _active_foreground(appearance)
    accent_foreground = _accent_foreground(appearance)
    # One shared low-contrast rule defines every app boundary: settings sections,
    # search, filters, content, footer, list/detail, and preview/information.
    # Keeping the material identical avoids a mix of unrelated gray rules across
    # themes.
    section_divider, _ = _surface_divider_token(appearance)
    detail_background = "transparent"
    detail_border = "none"
    preview_rule = "background: transparent; border: none;"
    transparent_root = appearance.frosted and sys.platform != "win32"
    panel_window_rule = "#panelWindow { background: transparent; }" if transparent_root else ""
    dialog_background = (
        "transparent"
        if (transparent_root if dialog_transparent is None else dialog_transparent)
        else colors.window
    )
    if appearance.frosted:
        overlay = "rgba(232, 244, 255, 54)" if not appearance.dark else "rgba(223, 240, 255, 27)"
        overlay_hover = "rgba(255, 255, 255, 82)" if not appearance.dark else "rgba(230, 244, 255, 48)"
        overlay_border = "rgba(255, 255, 255, 104)" if not appearance.dark else "rgba(205, 230, 255, 68)"
        active_chip = "rgba(68, 130, 237, 190)" if not appearance.dark else "rgba(102, 161, 255, 174)"
        card_background = "transparent"
        card_border = f"1px solid {colors.border}"
        settings_background = "transparent"
        scrollbar_handle = (
            "rgba(43, 74, 112, 148)"
            if not appearance.dark
            else "rgba(225, 238, 255, 136)"
        )
        scrollbar_handle_hover = (
            "rgba(37, 91, 161, 184)"
            if not appearance.dark
            else "rgba(236, 246, 255, 176)"
        )
    else:
        overlay = colors.control
        overlay_hover = colors.control
        overlay_border = colors.border
        active_chip = colors.accent
        card_background = colors.card
        card_border = f"1px solid {colors.border}"
        settings_background = colors.panel
        scrollbar_handle = (
            "rgba(52, 65, 86, 132)"
            if not appearance.dark
            else "rgba(224, 232, 244, 132)"
        )
        scrollbar_handle_hover = (
            "rgba(47, 94, 172, 172)"
            if not appearance.dark
            else "rgba(239, 246, 255, 176)"
        )

    # Settings retain their rounded grouping, but their outer rule is the same
    # quiet 1 px material divider used by the main panel.  In particular, do
    # not reuse the luminous frosted control border here: it reads as a white
    # card outline rather than a section boundary.
    settings_border = f"1px solid {section_divider}"
    # In the frosted appearance, idle form controls use the same quiet edge as
    # their containing section.  Focus remains the accent color below, so the
    # task surface stays calm without obscuring which field is editable.
    settings_control_border, _ = _settings_control_border_token(appearance)

    # Each scroll area owns its vertical scrollbar as a child widget. Without
    # explicit sub-control rules, applying the app stylesheet lets the
    # platform's opaque fallback groove become a bright divider. This is
    # deliberately scoped to the two scrolling content surfaces: settings and
    # all other native scroll areas retain their platform treatment.
    content_scrollbar_rule = f"""
        #historyList QScrollBar:vertical,
        #textPreview QScrollBar:vertical {{
            background: transparent; border: none; width: 12px;
            margin: 8px 1px 8px 3px;
        }}
        #historyList QScrollBar::groove:vertical,
        #textPreview QScrollBar::groove:vertical,
        #historyList QScrollBar::add-page:vertical,
        #textPreview QScrollBar::add-page:vertical,
        #historyList QScrollBar::sub-page:vertical,
        #textPreview QScrollBar::sub-page:vertical {{
            background: transparent; border: none;
        }}
        #historyList QScrollBar::handle:vertical,
        #textPreview QScrollBar::handle:vertical {{
            background: {scrollbar_handle}; border: 2px solid transparent;
            border-radius: 4px; min-height: 30px;
        }}
        #historyList QScrollBar::handle:vertical:hover,
        #textPreview QScrollBar::handle:vertical:hover {{
            background: {scrollbar_handle_hover};
        }}
        #historyList QScrollBar::add-line:vertical,
        #textPreview QScrollBar::add-line:vertical,
        #historyList QScrollBar::sub-line:vertical,
        #textPreview QScrollBar::sub-line:vertical {{
            background: transparent; border: none; height: 0px;
        }}
    """
    return f"""
        QWidget {{ color: {colors.text}; {font_family_rule}font-size: {metrics.root_font_size_pt}pt; }}
        {panel_window_rule}
        #card {{ background: {card_background}; border: {card_border}; border-radius: 18px; }}
        #searchBox {{ background: transparent; border: none; }}
        #search {{
            background: transparent; border: none; {search_text_rule}
            selection-background-color: {colors.accent}; selection-color: {accent_foreground};
        }}
        QToolButton {{ border: none; border-radius: 8px; padding: 7px 10px; background: transparent; }}
        QToolButton:hover {{ background: {overlay_hover}; }}
        QToolButton[filterChip="true"] {{
            color: {colors.muted}; {history_font_rule} font-weight: 500; {filter_chip_padding}
        }}
        QToolButton[filterChip="true"]:checked {{
            color: {active_foreground}; background: {active_chip}; border: 1px solid {overlay_border};
        }}
        #historyList {{ background: transparent; border: none; outline: none; {history_font_rule} }}
        {content_scrollbar_rule}
        #emptyState {{ background: transparent; border: none; }}
        #emptyStateTitle {{ color: {colors.text}; font-size: {metrics.content_font_size_pt}pt; font-weight: 600; }}
        #emptyStateMessage {{ color: {colors.muted}; font-size: {metrics.empty_message_font_size_pt}pt; }}
        QToolButton#emptyStateClear {{
            color: {colors.accent_focus}; background: transparent; border: none;
            border-radius: 7px; padding: 4px 6px;
        }}
        QToolButton#emptyStateClear:hover {{ background: {overlay_hover}; }}
        #detail {{ background: {detail_background}; border: {detail_border}; }}
        #contentDivider {{ background: {section_divider}; border: none; min-width: 1px; max-width: 1px; }}
        #searchFiltersDivider, #contentFooterDivider {{
            background: {section_divider}; border: none; min-height: 1px; max-height: 1px;
        }}
        #textPreview, #fileTextPreview {{ {preview_typography_rule} {preview_rule} }}
        #informationDivider {{
            background: {section_divider}; margin: 3px 8px 1px; min-height: 1px; max-height: 1px;
        }}
        #informationTitle {{ font-size: {metrics.content_font_size_pt}pt; font-weight: 650; padding: 6px 0 0 0; }}
        #informationLabel, #informationValue {{
            color: {colors.muted}; font-size: {metrics.content_font_size_pt}pt; font-weight: 500;
        }}
        #muted {{ color: {colors.muted}; font-size: {metrics.muted_font_size_pt}pt; }}
        #muted a {{ color: {colors.accent_focus}; text-decoration: none; }}
        #settingsDialog #platformNote {{
            background: {overlay}; border: 1px solid {settings_control_border}; border-radius: 10px;
        }}
        #settingsWindowTitle {{ font-size: {metrics.settings_title_font_size_pt}pt; font-weight: 650; }}
        #settingsSubtitle {{ color: {colors.muted}; font-size: {metrics.settings_help_font_size_pt}pt; }}
        #settingsSection {{ background: {settings_background}; border: {settings_border}; border-radius: 12px; }}
        #settingsSectionTitle {{ font-size: {metrics.settings_section_font_size_pt}pt; font-weight: 650; }}
        #settingsFieldLabel {{
            color: {colors.muted}; font-size: {metrics.settings_label_font_size_pt}pt; font-weight: 500;
        }}
        #settingsDialog QPlainTextEdit, #settingsDialog QLineEdit,
        #settingsDialog QKeySequenceEdit, #settingsDialog QComboBox, #settingsDialog QSpinBox {{
            background: {overlay}; border: 1px solid {settings_control_border}; border-radius: 10px; padding: 6px 8px;
            font-size: {metrics.settings_control_font_size_pt}pt;
            selection-background-color: {colors.accent}; selection-color: {accent_foreground};
        }}
        #settingsDialog QPlainTextEdit:focus, #settingsDialog QLineEdit:focus,
        #settingsDialog QKeySequenceEdit:focus, #settingsDialog QComboBox:focus,
        #settingsDialog QSpinBox:focus {{
            border: 1px solid {colors.accent_focus};
        }}
        #settingsDialog QComboBox::drop-down {{
            border: none; border-left: 1px solid {settings_control_border}; width: 27px;
        }}
        #settingsDialog QComboBox:disabled, #settingsDialog QLineEdit:disabled,
        #settingsDialog QKeySequenceEdit:disabled, #settingsDialog QSpinBox:disabled {{
            color: {colors.muted}; background: {colors.panel};
        }}
        #settingsDialog QCheckBox {{
            color: {colors.text}; spacing: 6px; font-size: {metrics.settings_control_font_size_pt}pt;
        }}
        #settingsDialog QCheckBox:disabled {{ color: {colors.muted}; }}
        #settingsDialog QCheckBox::indicator {{
            width: {metrics.settings_checkbox_indicator_size}px;
            height: {metrics.settings_checkbox_indicator_size}px;
            image: none; background: transparent; border: none;
        }}
        #settingsDialog QPlainTextEdit {{ selection-background-color: {colors.accent}; }}
        #settingsDialog QPushButton {{
            background: {overlay}; border: 1px solid {settings_control_border};
            border-radius: 10px; padding: 6px 13px; font-size: {metrics.settings_control_font_size_pt}pt;
        }}
        #settingsDialog QPushButton:hover {{ border-color: {colors.accent_focus}; }}
        #settingsDialog QPushButton:focus {{ border: 1px solid {colors.accent_focus}; }}
        QDialog {{ background: {dialog_background}; }}
    """


def _confirmation_style_sheet(
    theme: bool | _ThemeAppearance,
    *,
    destructive: bool = True,
) -> str:
    appearance = _as_theme_appearance(theme)
    colors = _theme_colors(appearance)
    metrics = _ui_metrics()
    font_family_rule = _platform_font_family_rule()
    dialog_typography_rule = (
        f"color: {colors.text}; {font_family_rule}font-size: {metrics.root_font_size_pt}pt;"
    )
    confirmation_title_rule = (
        f"color: {colors.text}; "
        f"font-size: {metrics.settings_title_font_size_pt + 1}pt; "
        "font-weight: 650;"
    )
    # On macOS and Linux the confirmation root only reserves breathing room
    # for the rounded card's shadow. Painting it opaque creates a second,
    # rectangular surface around the card. Windows keeps its opaque backing
    # because its top-level dialog must not use Qt's layered-window path.
    dialog_background = "transparent" if sys.platform != "win32" else colors.window
    card_background = "transparent" if appearance.frosted else colors.card
    card_border = "none" if appearance.frosted else f"1px solid {colors.border}"
    control = (
        "rgba(232, 244, 255, 54)" if appearance.frosted and not appearance.dark else colors.control
    )
    if appearance.frosted and appearance.dark:
        control = "rgba(223, 240, 255, 27)"
    control_border = (
        "rgba(255, 255, 255, 104)"
        if appearance.frosted and not appearance.dark
        else ("rgba(205, 230, 255, 68)" if appearance.frosted else colors.border)
    )
    if destructive:
        # Deleting history is materially different from acknowledging a
        # validation message.  Reserve the danger token for the final action
        # so users can recognize the irreversible choice at a glance without
        # making the whole confirmation visually noisy.
        if appearance.frosted and appearance.dark:
            confirm_background = "#F0838D"
            confirm_hover = "#FF9AA3"
            confirm_foreground = "#0A192F"
        else:
            confirm_background = "#C13749"
            confirm_hover = "#AB2D3D"
            confirm_foreground = "#FFFFFF"
    else:
        confirm_background = colors.accent
        confirm_hover = colors.accent_focus
        confirm_foreground = _accent_foreground(appearance)
    return f"""
        QDialog {{ background: {dialog_background}; {dialog_typography_rule} }}
        #confirmationCard {{
            background: {card_background}; border: {card_border}; border-radius: 16px;
        }}
        #confirmationTitle {{ {confirmation_title_rule} }}
        #confirmationMessage {{ color: {colors.muted}; font-size: {metrics.settings_label_font_size_pt}pt; }}
        #confirmationCancel, #confirmationConfirm {{
            border-radius: 9px; padding: 7px 10px;
        }}
        #confirmationCancel {{
            background: {control}; border: 1px solid {control_border}; color: {colors.text};
        }}
        #confirmationCancel:hover, #confirmationCancel:focus {{ border-color: {colors.accent_focus}; }}
        #confirmationConfirm {{
            background: {confirm_background}; border: 1px solid {confirm_background}; color: {confirm_foreground};
        }}
        #confirmationConfirm:hover, #confirmationConfirm:focus {{
            background: {confirm_hover}; border-color: {confirm_hover};
        }}
    """


def _image_viewer_style_sheet(theme: bool | _ThemeAppearance) -> str:
    appearance = _as_theme_appearance(theme)
    colors = _theme_colors(appearance)
    metrics = _ui_metrics()
    font_family_rule = _platform_font_family_rule()
    root_background = colors.window if appearance.frosted else colors.card
    return f"""
        QDialog#imageViewerDialog {{
            background: {root_background}; border: none;
            border-radius: 16px; color: {colors.text};
            {font_family_rule}font-size: {metrics.root_font_size_pt}pt;
        }}
        #imageViewerCanvas {{
            background: #101318; border: none; border-radius: 0px;
        }}
    """


def _single_image_file_path(files: Sequence[str]) -> str:
    if len(files) != 1:
        return ""
    path = files[0]
    return path if Path(path).suffix.casefold() in _IMAGE_FILE_SUFFIXES else ""


def _read_scaled_image(path: str, bounds: QSize, keep_aspect: bool) -> QImage:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    scaled_size = bounds
    if keep_aspect:
        natural = reader.size()
        if natural.isValid():
            scaled_size = _fit_image_size(natural, bounds, allow_upscale=False)
    reader.setScaledSize(scaled_size)
    return reader.read()


def _fit_image_size(source: QSize, bounds: QSize, *, allow_upscale: bool) -> QSize:
    if not source.isValid() or source.isEmpty():
        return QSize(max(1, bounds.width()), max(1, bounds.height()))
    if not bounds.isValid() or bounds.isEmpty():
        return QSize(max(1, source.width()), max(1, source.height()))
    fitted = QSize(source)
    fitted.scale(bounds, Qt.AspectRatioMode.KeepAspectRatio)
    if allow_upscale or fitted.width() < source.width() or fitted.height() < source.height():
        return fitted
    return QSize(source)


def _viewer_decode_bounds(path: str) -> QSize:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    natural = reader.size()
    if not natural.isValid() or natural.isEmpty():
        return QSize(4096, 4096)
    return _bounded_image_bytes(natural, _IMAGE_VIEWER_MAX_DECODE_BYTES)


def _bounded_image_bytes(size: QSize, max_bytes: int) -> QSize:
    if not size.isValid() or size.isEmpty():
        return QSize(max(1, size.width()), max(1, size.height()))
    max_pixels = max(1, max_bytes // 4)
    pixels = max(1, size.width()) * max(1, size.height())
    if pixels <= max_pixels:
        return QSize(size)
    ratio = math.sqrt(max_pixels / pixels)
    return QSize(max(1, int(size.width() * ratio)), max(1, int(size.height() * ratio)))


def _image_viewer_default_size(path: str, anchor: QWidget | None) -> QSize:
    screen = _image_viewer_screen(anchor)
    maximum = _image_viewer_maximum_size(screen) if screen is not None else QSize(720, 520)
    natural = _image_natural_size(path)
    frame = _IMAGE_VIEWER_FRAME_MARGIN * 2
    if not natural.isValid() or natural.isEmpty():
        desired = QSize(720, 520)
    else:
        desired = QSize(natural.width() + frame, natural.height() + frame)
    return QSize(
        min(max(desired.width(), _IMAGE_VIEWER_MINIMUM_SIZE.width()), maximum.width()),
        min(max(desired.height(), _IMAGE_VIEWER_MINIMUM_SIZE.height()), maximum.height()),
    )


def _image_natural_size(path: str) -> QSize:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return reader.size()


def _image_viewer_screen(anchor: QWidget | None) -> QScreen | None:
    if anchor is not None and anchor.screen() is not None:
        return anchor.screen()
    return QApplication.primaryScreen()


def _image_viewer_maximum_size(screen: QScreen) -> QSize:
    available = screen.availableGeometry()
    return QSize(
        max(_IMAGE_VIEWER_MINIMUM_SIZE.width(), available.width() - _IMAGE_VIEWER_WINDOW_MARGIN),
        max(_IMAGE_VIEWER_MINIMUM_SIZE.height(), available.height() - _IMAGE_VIEWER_WINDOW_MARGIN),
    )


def _scaled_size(size: QSize, scale: float) -> QSize:
    normalized = max(1.0, float(scale))
    return QSize(
        max(1, round(size.width() * normalized)),
        max(1, round(size.height() * normalized)),
    )


def _image_cost(image: QImage) -> int:
    return max(1, image.sizeInBytes())


def _pixmap_cost(pixmap: QPixmap) -> int:
    return max(1, pixmap.width() * pixmap.height() * max(1, pixmap.depth()) // 8)


def _bucketed_size(size: QSize, bucket: int = _PREVIEW_SIZE_BUCKET) -> QSize:
    def rounded(value: int) -> int:
        return max(bucket, ((max(1, value) + bucket - 1) // bucket) * bucket)

    return QSize(rounded(size.width()), rounded(size.height()))


def _item_image_paths(items: Sequence[ClipItem]) -> set[str]:
    paths: set[str] = set()
    for item in items:
        if item.kind is ClipKind.IMAGE and item.image_path:
            paths.add(item.image_path)
        elif item.kind is ClipKind.FILES and (image_path := _single_image_file_path(item.files)):
            paths.add(image_path)
    return paths


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


def _style_combo_popup(combo: QComboBox, theme: bool | _ThemeAppearance) -> None:
    appearance = _as_theme_appearance(theme)
    colors = _theme_colors(appearance)
    metrics = _ui_metrics()
    font_family_rule = _platform_font_family_rule()
    accent_foreground = _accent_foreground(appearance)
    popup_background = colors.popup
    popup_hover = colors.hover
    border = _settings_control_border_token(appearance)[0]
    view = combo.view()
    container = view.window()
    container.setObjectName("comboPopup")
    container_palette = container.palette()
    container_palette.setColor(QPalette.ColorRole.Window, QColor(popup_background))
    container_palette.setColor(QPalette.ColorRole.Base, QColor(popup_background))
    container.setPalette(container_palette)
    container.setAutoFillBackground(True)
    container.setStyleSheet(
        f"#comboPopup {{ background: {popup_background}; border: 1px solid {border}; "
        "border-radius: 6px; }"
    )
    palette = view.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(popup_background))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(popup_background))
    palette.setColor(QPalette.ColorRole.Window, QColor(popup_background))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors.text))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(accent_foreground))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors.muted))
    view.setStyleSheet(
        f"QAbstractItemView {{ background: {popup_background}; color: {colors.text}; "
        f"border: none; outline: none; padding: 2px; {font_family_rule}"
        f"font-size: {metrics.popup_item_font_size_pt}pt; }}"
        f"QAbstractItemView::item {{ min-height: {metrics.popup_item_min_height}px; padding: 4px 8px; }}"
        f"QAbstractItemView::item:hover {{ background: {popup_hover}; color: {colors.text}; }}"
        f"QAbstractItemView::item:selected {{ background: {colors.accent}; color: {accent_foreground}; }}"
        f"QAbstractItemView::item:disabled {{ color: {colors.muted}; }}"
    )
    # Apply the palette after QSS: some platform styles repolish the popup view
    # when a stylesheet is installed and otherwise restore the system accent.
    view.setPalette(palette)


class _DestructiveConfirmationDialog(QDialog):
    """Application-styled confirmation or acknowledgement dialog."""

    def __init__(
        self,
        parent: QWidget,
        title: str,
        text: str,
        confirm_text: str,
        *,
        dark: bool = False,
        appearance: _ThemeAppearance | None = None,
        cancel_text: str | None = "取消",
        destructive: bool = True,
    ) -> None:
        super().__init__(parent)
        requested_appearance = appearance or _ThemeAppearance(dark=dark)
        # A transient confirmation window renders the complete app-owned
        # frosted material instead of inheriting any parent surface shortcut.
        self._appearance = _ThemeAppearance(
            dark=requested_appearance.dark,
            frosted=requested_appearance.frosted,
        )
        self._destructive = destructive
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        if sys.platform == "win32":
            # Keep this secondary top-level window off Qt's layered path too.
            self.setAutoFillBackground(True)
        else:
            # The confirmation card is the sole visible surface. Its root is
            # transparent in every theme so the shadow margin cannot become an
            # exposed rectangular outer ring.
            self.setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground,
                True,
            )
            self.setAutoFillBackground(False)
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
            self.setPalette(palette)
        self.setFixedWidth(380 if sys.platform == "win32" else 420)
        self.setStyleSheet(
            _confirmation_style_sheet(self._appearance, destructive=self._destructive)
        )
        if sys.platform == "win32":
            self.setAutoFillBackground(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 14)
        card = _FrostedSurface()
        card.setObjectName("confirmationCard")
        card.set_appearance(self._appearance)
        self._material_card = card
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("confirmationTitle")
        message = QLabel(text)
        message.setObjectName("confirmationMessage")
        message.setWordWrap(True)
        card_layout.addWidget(title_label)
        card_layout.addWidget(message)
        card_layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        self.cancel_button: QPushButton | None = None
        if cancel_text is not None:
            self.cancel_button = QPushButton(cancel_text)
            self.cancel_button.setObjectName("confirmationCancel")
            self.cancel_button.clicked.connect(self.reject)
            buttons.addWidget(self.cancel_button)
        self.confirm_button = QPushButton(confirm_text)
        self.confirm_button.setObjectName("confirmationConfirm")
        self.confirm_button.clicked.connect(self.accept)
        (self.cancel_button or self.confirm_button).setDefault(True)
        buttons.addWidget(self.confirm_button)
        card_layout.addLayout(buttons)
        if sys.platform != "win32":
            shadow = QGraphicsDropShadowEffect(card)
            shadow.setBlurRadius(24)
            shadow.setOffset(0, 8)
            shadow.setColor(QColor(0, 0, 0, 72 if self._appearance.dark else 48))
            card.setGraphicsEffect(shadow)
        root.addWidget(card)
        if self._appearance.frosted:
            self._install_material_tracking()

    def _install_material_tracking(self) -> None:
        """Forward child-pointer movement to the one confirmation material shell."""

        self.setMouseTracking(True)
        for widget in self.findChildren(QWidget):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    def _update_material_light(self, global_position: QPointF) -> None:
        position = self._material_card.mapFromGlobal(global_position.toPoint())
        self._material_card.set_light_position(QPointF(position))
        self._material_card.set_light_active(self._material_card.rect().contains(position))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self._appearance.frosted
            and isinstance(event, QMouseEvent)
            and (
                event.type() == QEvent.Type.MouseMove
                or (
                    event.type() == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                )
            )
        ):
            self._update_material_light(event.globalPosition())
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._update_material_light(event.globalPosition())
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        if self._appearance.frosted:
            self._update_material_light(QPointF(QCursor.pos()))

    def leaveEvent(self, event) -> None:
        self._material_card.set_light_active(False)
        super().leaveEvent(event)


def _confirm_destructive_action(
    parent: QWidget,
    title: str,
    text: str,
    confirm_text: str,
    *,
    dark: bool = False,
    appearance: _ThemeAppearance | None = None,
) -> bool:
    prompt = _DestructiveConfirmationDialog(
        parent,
        title,
        text,
        confirm_text,
        dark=dark,
        appearance=appearance,
    )
    return prompt.exec() == QDialog.DialogCode.Accepted


def _show_themed_warning(
    parent: QWidget,
    title: str,
    text: str,
    *,
    appearance: _ThemeAppearance,
) -> None:
    """Present validation feedback without falling back to a system-coloured alert."""

    prompt = _DestructiveConfirmationDialog(
        parent,
        title,
        text,
        "知道了",
        appearance=appearance,
        cancel_text=None,
        destructive=False,
    )
    prompt.exec()


def _menu_shortcut_text(shortcut_id: str) -> str:
    if sys.platform == "darwin":
        shortcuts = {
            "select_all": "⌘A",
            "favorite": "⌘D",
            "unfavorite": "⇧⌘D",
            "delete": "⌘⌫",
            "clear": "⇧⌘⌫",
            "clear_non_favorites": "⌥⌘N",
            "settings": "⌘,",
        }
    else:
        shortcuts = {
            "select_all": "Ctrl+A",
            "favorite": "Ctrl+D",
            "unfavorite": "Ctrl+⇧+D",
            "delete": "Del",
            "clear": "Ctrl+⇧+Del",
            "clear_non_favorites": "Ctrl+Alt+N",
            "settings": "Ctrl+,",
        }
    return shortcuts[shortcut_id]


def _shortcut_sequence_texts(shortcut_id: str) -> tuple[str, ...]:
    if sys.platform == "darwin" and shortcut_id == "delete":
        return ("Ctrl+Backspace", "Ctrl+Delete")
    if sys.platform == "darwin" and shortcut_id == "clear":
        return ("Ctrl+Shift+Backspace", "Ctrl+Shift+Delete")
    shortcuts = {
        "select_all": ("Ctrl+A",),
        "favorite": ("Ctrl+D",),
        "unfavorite": ("Ctrl+Shift+D",),
        "delete": ("Delete",),
        "clear": ("Ctrl+Shift+Delete",),
        "clear_non_favorites": ("Ctrl+Alt+N",),
        "settings": ("Ctrl+,",),
    }
    return shortcuts[shortcut_id]


def _menu_action_text(label: str, shortcut_id: str) -> str:
    del shortcut_id
    return label


def _set_menu_action_shortcut(action: QAction, shortcut_id: str) -> None:
    action.setProperty(_COMPACT_MENU_ACTION_SHORTCUT_PROPERTY, _menu_shortcut_text(shortcut_id))


def _action_shortcut_text(action: QAction) -> str:
    return str(action.property(_COMPACT_MENU_ACTION_SHORTCUT_PROPERTY) or "")


def _menu_action_icon(icon_name: str, appearance: _ThemeAppearance) -> QIcon:
    """Draw compact contextual-menu symbols in the active theme."""

    logical_size = _COMPACT_MENU_ICON_SIZE
    device_scale = 2
    pixmap = QPixmap(logical_size * device_scale, logical_size * device_scale)
    pixmap.setDevicePixelRatio(device_scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(_theme_colors(appearance).muted)
    pen = QPen(color, 1.45)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if icon_name == "select_all":
        painter.drawRoundedRect(QRectF(2.7, 2.7, 8.6, 8.6), 1.0, 1.0)
        painter.drawLine(QPointF(4.7, 7.0), QPointF(6.4, 8.7))
        painter.drawLine(QPointF(6.4, 8.7), QPointF(9.4, 5.1))
    elif icon_name == "delete":
        painter.drawLine(QPointF(3.0, 3.0), QPointF(11.0, 11.0))
        painter.drawLine(QPointF(11.0, 3.0), QPointF(3.0, 11.0))
    elif icon_name == "clear":
        painter.drawLine(QPointF(2.5, 4.5), QPointF(11.5, 4.5))
        painter.drawLine(QPointF(5.3, 3.0), QPointF(8.7, 3.0))
        painter.drawRoundedRect(QRectF(3.7, 4.5, 6.6, 7.0), 1.1, 1.1)
        painter.drawLine(QPointF(6.0, 6.3), QPointF(6.0, 9.9))
        painter.drawLine(QPointF(8.0, 6.3), QPointF(8.0, 9.9))
    elif icon_name in {"favorite", "unfavorite"}:
        icon_font = QFont()
        icon_font.setPointSizeF(10.5)
        painter.setFont(icon_font)
        painter.drawText(QRectF(0, 0, logical_size, logical_size), Qt.AlignmentFlag.AlignCenter, "★")
        if icon_name == "unfavorite":
            painter.drawLine(QPointF(2.6, 2.6), QPointF(11.4, 11.4))
    elif icon_name == "settings":
        # Six short rectangular teeth make this read as a cog rather than a
        # crosshair, while retaining the menu's restrained monochrome weight.
        painter.save()
        painter.translate(7.0, 7.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for degrees in range(0, 360, 60):
            painter.save()
            painter.rotate(degrees)
            painter.drawRoundedRect(QRectF(-1.1, -5.7, 2.2, 2.7), 0.45, 0.45)
            painter.restore()
        painter.restore()
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(3.2, 3.2, 7.6, 7.6))
        painter.drawEllipse(QRectF(5.7, 5.7, 2.6, 2.6))
    else:
        raise ValueError(f"Unknown contextual menu icon: {icon_name}")

    painter.end()
    return QIcon(pixmap)


def _matches_copy_shortcut(event: QKeyEvent) -> bool:
    """Recognise the platform copy gesture even on headless Qt backends."""

    if event.matches(QKeySequence.StandardKey.Copy):
        return True
    return _matches_primary_key(event, Qt.Key.Key_C)


def _primary_shortcut_modifiers() -> tuple[Qt.KeyboardModifiers, ...]:
    return (Qt.KeyboardModifier.ControlModifier,)


def _shortcut_modifiers(event: QKeyEvent) -> Qt.KeyboardModifiers:
    return event.modifiers() & (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.MetaModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.ShiftModifier
    )


def _matches_primary_key(
    event: QKeyEvent,
    key: Qt.Key,
    *,
    shift: bool = False,
    alt: bool = False,
) -> bool:
    expected_modifiers = []
    for modifiers in _primary_shortcut_modifiers():
        expected = modifiers
        if shift:
            expected |= Qt.KeyboardModifier.ShiftModifier
        if alt:
            expected |= Qt.KeyboardModifier.AltModifier
        expected_modifiers.append(expected)
    return event.key() == key and any(
        _shortcut_modifiers(event) == modifiers
        for modifiers in expected_modifiers
    )


def _matches_select_all_shortcut(event: QKeyEvent) -> bool:
    return _matches_primary_key(event, Qt.Key.Key_A)


def _matches_favorite_shortcut(event: QKeyEvent) -> bool:
    return _matches_primary_key(event, Qt.Key.Key_D)


def _matches_unfavorite_shortcut(event: QKeyEvent) -> bool:
    return _matches_primary_key(event, Qt.Key.Key_D, shift=True)


def _matches_delete_shortcut(event: QKeyEvent) -> bool:
    if sys.platform == "darwin":
        return event.key() in {
            Qt.Key.Key_Backspace,
            Qt.Key.Key_Delete,
        } and any(
            _shortcut_modifiers(event) == modifiers
            for modifiers in _primary_shortcut_modifiers()
        )
    return event.key() == Qt.Key.Key_Delete and _shortcut_modifiers(event) == Qt.KeyboardModifier.NoModifier


def _matches_clear_shortcut(event: QKeyEvent) -> bool:
    if sys.platform == "darwin":
        return event.key() in {Qt.Key.Key_Backspace, Qt.Key.Key_Delete} and any(
            _shortcut_modifiers(event) == (modifiers | Qt.KeyboardModifier.ShiftModifier)
            for modifiers in _primary_shortcut_modifiers()
        )
    return event.key() == Qt.Key.Key_Delete and _shortcut_modifiers(event) == (
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )


def _matches_clear_non_favorites_shortcut(event: QKeyEvent) -> bool:
    return _matches_primary_key(event, Qt.Key.Key_N, alt=True)


def _matches_settings_shortcut(event: QKeyEvent) -> bool:
    return _matches_primary_key(event, Qt.Key.Key_Comma)


def _compact_menu(
    menu: QMenu,
    *,
    dark: bool | None = None,
    appearance: _ThemeAppearance | None = None,
) -> None:
    if appearance is None:
        if dark is None:
            dark = menu.palette().color(QPalette.ColorRole.Window).lightness() < 128
        appearance = _ThemeAppearance(dark=dark)
    colors = _theme_colors(appearance)
    metrics = _ui_metrics()
    font_family_rule = _platform_font_family_rule()
    disabled = "#9299A9" if appearance.dark else "#757C8D"
    has_icons = any(not action.isSeparator() and not action.icon().isNull() for action in menu.actions())
    # Keep horizontal space on the item rather than on the QMenu shell.  Qt
    # paints the hover/active background inside the action item, so shell
    # padding visually narrows the highlight capsule even though the popup
    # itself has enough width.  Item margin creates the intended outer gap;
    # item padding creates the icon/text breathing room.
    shell_horizontal_inset = _COMPACT_MENU_SHELL_HORIZONTAL_INSET
    vertical_inset = _COMPACT_MENU_VERTICAL_INSET
    item_margin = _COMPACT_MENU_ITEM_HORIZONTAL_MARGIN
    item_padding = _COMPACT_MENU_ITEM_HORIZONTAL_PADDING
    # Menus with icons get a 14 px icon column and a deliberate 6 px text
    # allocation; plain text menus do not reserve an empty icon gutter.
    icon_column_width = (_COMPACT_MENU_ICON_SIZE + _COMPACT_MENU_ICON_GAP) if has_icons else 0
    menu.setProperty(_COMPACT_MENU_PROPERTY, True)
    menu.setProperty(_COMPACT_MENU_HAS_ICONS_PROPERTY, has_icons)
    menu.setProperty(_COMPACT_MENU_TEXT_COLOR_PROPERTY, colors.text)
    menu.setProperty(_COMPACT_MENU_SHORTCUT_COLOR_PROPERTY, colors.muted)
    menu.setProperty(_COMPACT_MENU_DISABLED_TEXT_COLOR_PROPERTY, disabled)
    menu.setStyleSheet(
        f"QMenu {{ background: {colors.menu}; color: {colors.text}; "
        f"padding: {vertical_inset}px {shell_horizontal_inset}px; "
        f"border: 1px solid {colors.menu_separator}; "
        f"border-radius: {_COMPACT_MENU_CORNER_RADIUS}px; "
        f"{font_family_rule}font-size: {metrics.popup_item_font_size_pt}pt; }}"
        f"QMenu::item {{ margin: 0px {item_margin}px; "
        f"padding: 6px {item_padding}px 6px {item_padding}px; border-radius: 6px; }}"
        f"QMenu::item:selected {{ background: {colors.menu_hover}; color: {colors.text}; }}"
        f"QMenu::item:disabled {{ color: {disabled}; }}"
        f"QMenu::icon {{ left: {_COMPACT_MENU_ICON_LEFT_OFFSET}px; }}"
        f"QMenu::separator {{ background: {colors.menu_separator}; height: 1px; "
        f"margin: 2px {item_margin + item_padding}px; }}"
    )
    if not isinstance(getattr(menu, "_clipsoon_frame_filter", None), _CompactMenuFrameFilter):
        menu._clipsoon_frame_filter = _CompactMenuFrameFilter(menu)  # type: ignore[attr-defined]
        menu.installEventFilter(menu._clipsoon_frame_filter)  # type: ignore[attr-defined]
    font = QFont(menu.font())
    font = _apply_platform_font_family(font)
    font.setPointSize(metrics.popup_item_font_size_pt)
    menu.setFont(font)
    text_metrics = QFontMetrics(font)

    def action_text_width(action: QAction) -> int:
        shortcut = _action_shortcut_text(action)
        label_width = text_metrics.horizontalAdvance(action.text())
        if not shortcut:
            return label_width
        return label_width + 24 + text_metrics.horizontalAdvance(shortcut)

    text_width = max((action_text_width(action) for action in menu.actions() if not action.isSeparator()), default=0)
    # The icon column and text gap are explicit, while shell inset, item
    # margin and item padding remain mirrored so the item hover capsule and
    # content have equal optical space on both sides.
    menu.setFixedWidth(
        text_width
        + icon_column_width
        + 2 * (shell_horizontal_inset + item_margin + item_padding)
    )
    _balance_compact_menu_action_margins(menu)


def _hover_color(theme: bool | _ThemeAppearance) -> QColor:
    return QColor(_theme_colors(theme).hover)


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
    plus_key = text.rstrip().endswith("++")
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
    if plus_key:
        parts.append("plus")
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
    return "+".join(labels.get(value, "+" if value == "plus" else value.upper()) for value in values)


def _platform_hotkey_validation_error(spec: str) -> str:
    if sys.platform != "win32" or not spec:
        return ""
    from clipsoon.windows_hotkey_host import parse_registered_hotkey

    try:
        parse_registered_hotkey(spec)
    except ValueError as exc:
        return f"Windows 不支持该全局组合键：{exc}"
    return ""
