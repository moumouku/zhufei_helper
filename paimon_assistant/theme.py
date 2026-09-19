"""Window-scoped Graphite theme; no application or serial state is changed."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QPushButton, QWidget

COLORS = {
    "receive": "#0F131A",
    "window": "#171C24",
    "panel": "#1E2530",
    "hover": "#293344",
    "divider": "#333E4D",
    "border": "#718198",
    "text": "#E6EDF3",
    "secondary": "#A9B4C2",
    "muted": "#91A0B4",
    "accent": "#82AAFF",
    "accent_hover": "#A5C3FF",
    "accent_pressed": "#7194DE",
    "selection": "#243A56",
    "success": "#8BD49C",
    "warning": "#EBCB8B",
    "error": "#F28B82",
    "brand": "#D9BF8C",
}

FONTS = {
    "ui_family": "Microsoft YaHei UI",
    "data_family": "Consolas",
    "ui_size": 10,
    "data_size": 11,
}

SIZES = {
    "page_margin": 12,
    "spacing": 8,
    "group_spacing": 16,
    "control_height": 32,
    "border": 2,
    "radius": 4,
    "padding_x": 4,
    "padding_y": 3,
    "scrollbar": 12,
    "editable_width_em": 8,
}


def ui_font() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if FONTS["ui_family"] in QFontDatabase.families():
        font.setFamily(FONTS["ui_family"])
    font.setPointSize(FONTS["ui_size"])
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
    return font


def data_font() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if FONTS["data_family"] in QFontDatabase.families():
        font.setFamily(FONTS["data_family"])
    font.setPointSize(FONTS["data_size"])
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setKerning(False)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
    font.setStyleStrategy(QFont.StyleStrategy.PreferNoShaping)
    return font


def build_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "window",
        QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "receive",
        QPalette.ColorRole.AlternateBase: "panel",
        QPalette.ColorRole.Text: "text",
        QPalette.ColorRole.Button: "panel",
        QPalette.ColorRole.ButtonText: "text",
        QPalette.ColorRole.ToolTipBase: "panel",
        QPalette.ColorRole.ToolTipText: "text",
        QPalette.ColorRole.Highlight: "selection",
        QPalette.ColorRole.HighlightedText: "text",
        QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.BrightText: "error",
        QPalette.ColorRole.Light: "border",
        QPalette.ColorRole.Midlight: "hover",
        QPalette.ColorRole.Mid: "divider",
        QPalette.ColorRole.Dark: "receive",
        QPalette.ColorRole.Shadow: "receive",
    }
    for role, name in roles.items():
        palette.setColor(role, QColor(COLORS[name]))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS["muted"]))
    return palette


def build_qss() -> str:
    c, s = COLORS, SIZES
    content_height = s["control_height"] - 2 * (s["border"] + s["padding_y"])
    return f"""
QMainWindow, QMessageBox {{
    background-color: {c['window']};
    color: {c['text']};
}}
QLabel {{
    background-color: transparent;
    color: {c['secondary']};
}}
QPushButton, QLineEdit, QComboBox {{
    background-color: {c['panel']};
    color: {c['text']};
    border: {s['border']}px solid {c['border']};
    border-radius: {s['radius']}px;
    padding: {s['padding_y']}px {s['padding_x']}px;
    min-height: {content_height}px;
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
/* Allow room for the baud rate and the embedded line edit's caret. */
QComboBox:editable {{ min-width: {s['editable_width_em']}em; }}
QPushButton:hover, QComboBox:hover {{
    background-color: {c['hover']};
}}
QPushButton:pressed {{
    background-color: {c['selection']};
}}
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border-color: {c['accent']};
}}
QPushButton:disabled, QLineEdit:disabled, QComboBox:disabled {{
    background-color: {c['window']};
    color: {c['muted']};
    border-color: {c['divider']};
}}
/* Primary action: flat accent fill, darker text, matching hover/pressed shades.
   The focus ring switches to the light text color because an accent ring would
   be invisible on the accent fill. Border widths stay untouched so no state
   changes the control size. */
QPushButton[primary="true"] {{
    background-color: {c['accent']};
    color: {c['receive']};
    border-color: {c['accent']};
}}
QPushButton[primary="true"]:hover {{
    background-color: {c['accent_hover']};
    border-color: {c['accent_hover']};
}}
QPushButton[primary="true"]:pressed {{
    background-color: {c['accent_pressed']};
    border-color: {c['accent_pressed']};
}}
QPushButton[primary="true"]:focus {{
    border-color: {c['text']};
}}
QPushButton[primary="true"]:disabled {{
    background-color: {c['window']};
    color: {c['muted']};
    border-color: {c['divider']};
}}
QComboBox QLineEdit, QComboBox QLineEdit:focus {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    min-height: 0px;
}}
QPlainTextEdit {{
    background-color: {c['receive']};
    color: {c['text']};
    border: {s['border']}px solid {c['divider']};
    border-radius: {s['radius']}px;
    padding: {s['padding_x']}px;
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QCheckBox {{
    color: {c['text']};
    spacing: {s['padding_x']}px;
    border: {s['border']}px solid transparent;
    border-radius: {s['radius']}px;
}}
QCheckBox:focus {{ border-color: {c['accent']}; }}
QCheckBox:disabled {{ color: {c['muted']}; }}
QComboBox QAbstractItemView, QMenu {{
    background-color: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QMenu::item:selected {{ background-color: {c['selection']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QToolTip {{
    background-color: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    padding: {s['padding_x']}px;
}}
QScrollBar:vertical {{
    background-color: {c['window']};
    width: {s['scrollbar']}px;
    margin: 0px;
}}
QScrollBar:horizontal {{
    background-color: {c['window']};
    height: {s['scrollbar']}px;
    margin: 0px;
}}
QScrollBar::handle {{
    background-color: {c['border']};
    border-radius: {s['radius']}px;
}}
QScrollBar::handle:vertical {{ min-height: 24px; }}
QScrollBar::handle:horizontal {{ min-width: 24px; }}
QScrollBar::handle:hover {{ background-color: {c['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
"""


def apply_theme(window: QWidget) -> None:
    """Apply inherited colors and UI font without changing QApplication defaults."""
    window.setPalette(build_palette())
    window.setFont(ui_font())
    window.setStyleSheet(build_qss())


def set_primary(button: QPushButton, primary: bool) -> None:
    """Toggle the boolean ``primary`` property and refresh the button style.

    QSS attribute selectors are resolved while a widget is polished, so the
    dynamic property alone does not restyle the button; the explicit
    unpolish/polish cycle makes the change take effect immediately.
    """
    primary = bool(primary)
    if button.property("primary") == primary:
        return
    font = button.font()
    button.setProperty("primary", primary)
    button.style().unpolish(button)
    button.style().polish(button)
    # Repolishing drops the font inherited from the themed ancestor and falls
    # back to the application font, which would resize the button; restore it.
    button.setFont(font)
    button.update()
