"""Graphite theme integration, font fallback and application-state isolation."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (  # noqa: E402
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from paimon_assistant.main_window import MainWindow  # noqa: E402
from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.serial_controller import SerialController  # noqa: E402
from paimon_assistant.theme import (  # noqa: E402
    COLORS,
    FONTS,
    SIZES,
    apply_theme,
    build_palette,
    build_qss,
    data_font,
    set_primary,
    ui_font,
)


@pytest.fixture
def window(qtbot, tmp_path):
    win = MainWindow(
        controller=SerialController(port_lister=lambda: []),
        log_service=ReceiveLogService(tmp_path / "logs"),
    )
    qtbot.addWidget(win)
    win.ensurePolished()
    return win


def test_error_color_matches_approved_design():
    assert COLORS["error"] == "#F28B82"


@pytest.mark.parametrize(
    "group",
    [QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled],
)
def test_palette_covers_active_inactive_and_disabled_groups(qapp, group):
    palette = build_palette()
    for role, name in (
        (QPalette.ColorRole.Window, "window"),
        (QPalette.ColorRole.Base, "receive"),
        (QPalette.ColorRole.Button, "panel"),
        (QPalette.ColorRole.Highlight, "selection"),
        (QPalette.ColorRole.HighlightedText, "text"),
        (QPalette.ColorRole.ToolTipBase, "panel"),
        (QPalette.ColorRole.ToolTipText, "text"),
    ):
        assert palette.color(group, role) == QColor(COLORS[name])
    text = "muted" if group == QPalette.ColorGroup.Disabled else "text"
    assert palette.color(group, QPalette.ColorRole.ButtonText) == QColor(COLORS[text])


def test_editable_baud_has_room_for_standard_rates_and_caret(window, qapp):
    window.show()
    qapp.processEvents()
    box = window.baud_combo
    edit = box.lineEdit()
    metrics = QFontMetricsF(edit.font())
    longest_rate = max(
        metrics.horizontalAdvance(box.itemText(i)) for i in range(box.count())
    )
    # The line edit needs spare space for its internal text margins and caret.
    assert edit.contentsRect().width() >= longest_rate + metrics.horizontalAdvance("0")


def test_main_window_uses_themed_colors_and_fonts(window):
    assert window.styleSheet() == build_qss()
    assert window.palette().color(QPalette.ColorRole.Window) == QColor(COLORS["window"])
    assert window.open_button.font().family() == ui_font().family()
    assert window.open_button.font().pointSizeF() == FONTS["ui_size"]
    for control in (window.display_edit, window.send_edit):
        control.ensurePolished()
        font = control.font()
        assert font.family() == data_font().family()
        assert font.pointSizeF() == FONTS["data_size"]
        assert font.fixedPitch()
        assert not font.kerning()
        assert font.letterSpacing() == 0
        assert font.styleStrategy() & QFont.StyleStrategy.PreferNoShaping
        metrics = QFontMetricsF(font)
        assert metrics.horizontalAdvance("iiii") == pytest.approx(
            metrics.horizontalAdvance("WWWW"), abs=0.1
        )
    assert window.display_edit.palette().color(QPalette.ColorRole.Base) == QColor(
        COLORS["receive"]
    )
    assert window.send_edit.palette().color(QPalette.ColorRole.Base) == QColor(
        COLORS["panel"]
    )


@pytest.mark.parametrize("name", ["receive_error_label", "log_error_label"])
def test_error_labels_render_with_theme_color(window, name):
    label = getattr(window, name)
    label.ensurePolished()
    assert label.text() == ""
    assert label.palette().color(QPalette.ColorRole.WindowText) == QColor(COLORS["error"])


def test_disabled_button_and_combobox_popup_keep_readable_colors(window):
    button = window.send_button
    button.ensurePolished()
    assert not button.isEnabled()
    assert button.palette().color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
    ) == QColor(COLORS["muted"])
    view = window.receive_mode_combo.view()
    view.ensurePolished()
    assert view.palette().color(QPalette.ColorRole.Base) == QColor(COLORS["panel"])
    assert view.palette().color(QPalette.ColorRole.HighlightedText) == QColor(COLORS["text"])


def test_parented_message_box_inherits_theme(window, qtbot):
    dialog = QMessageBox(window)
    qtbot.addWidget(dialog)
    dialog.ensurePolished()
    assert dialog.palette().color(QPalette.ColorRole.Window) == QColor(COLORS["window"])
    assert dialog.font().family() == ui_font().family()


def test_apply_theme_is_repeatable_and_window_scoped(qapp, qtbot):
    original_palette = QPalette(qapp.palette())
    original_font = QFont(qapp.font())
    original_qss = qapp.styleSheet()
    untouched = QWidget()
    themed = QWidget()
    qtbot.addWidget(untouched)
    qtbot.addWidget(themed)
    untouched_palette = QPalette(untouched.palette())
    untouched_font = QFont(untouched.font())
    apply_theme(themed)
    first_palette = QPalette(themed.palette())
    first_font = QFont(themed.font())
    apply_theme(themed)
    assert themed.styleSheet() == build_qss()
    assert themed.palette() == first_palette
    assert themed.font() == first_font
    assert qapp.palette() == original_palette
    assert qapp.font() == original_font
    assert qapp.styleSheet() == original_qss
    assert untouched.palette() == untouched_palette
    assert untouched.font() == untouched_font
    assert untouched.styleSheet() == ""


def test_fonts_fall_back_without_installing_assets(qapp, monkeypatch):
    monkeypatch.setattr(QFontDatabase, "families", staticmethod(lambda: []))
    assert ui_font().family() == QFontDatabase.systemFont(
        QFontDatabase.SystemFont.GeneralFont
    ).family()
    assert data_font().family() == QFontDatabase.systemFont(
        QFontDatabase.SystemFont.FixedFont
    ).family()
    assert data_font().pointSizeF() == FONTS["data_size"]
    assert data_font().fixedPitch()


def test_preferred_fonts_are_used_when_available(qapp, monkeypatch):
    monkeypatch.setattr(
        QFontDatabase,
        "families",
        staticmethod(lambda: [FONTS["ui_family"], FONTS["data_family"]]),
    )
    assert ui_font().family() == FONTS["ui_family"]
    assert data_font().family() == FONTS["data_family"]


def test_focused_controls_keep_their_size(window, qtbot):
    window.show()
    window.activateWindow()
    window.send_edit.setFocus()
    qtbot.waitUntil(lambda: window.send_edit.hasFocus())
    size = window.send_edit.size()
    assert size.height() >= SIZES["control_height"]
    window.open_button.setFocus()
    qtbot.waitUntil(lambda: window.open_button.hasFocus())
    assert window.send_edit.size() == size
    window.send_edit.setFocus()
    qtbot.waitUntil(lambda: window.send_edit.hasFocus())
    assert window.send_edit.size() == size


# ---------------------------------------------------------------------------
# 第 3 步：唯一 primary 按钮的布尔动态属性样式
# ---------------------------------------------------------------------------


def _relative_luminance(color: QColor) -> float:
    """WCAG 2.x relative luminance used by the readability assertions."""

    def channel(value: int) -> float:
        value = value / 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def _contrast_ratio(first: QColor, second: QColor) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter, darker = sorted((first_luminance, second_luminance), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _render_button(
    button: QPushButton,
    *,
    hover: bool = False,
    pressed: bool = False,
    focus: bool = False,
) -> QImage:
    """Render one state through the button's real style so QSS rules are resolved."""
    option = QStyleOptionButton()
    option.initFrom(button)
    option.rect = button.rect()
    option.text = button.text()
    option.state &= ~(
        QStyle.StateFlag.State_MouseOver
        | QStyle.StateFlag.State_Sunken
        | QStyle.StateFlag.State_HasFocus
    )
    if hover:
        option.state |= QStyle.StateFlag.State_MouseOver
    if pressed:
        option.state |= QStyle.StateFlag.State_Sunken
    if focus:
        option.state |= QStyle.StateFlag.State_HasFocus
    pixmap = QPixmap(button.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    button.style().drawControl(
        QStyle.ControlElement.CE_PushButton, option, painter, button
    )
    painter.end()
    return pixmap.toImage()


def _button_background(button: QPushButton, **state: bool) -> QColor:
    image = _render_button(button, **state)
    return QColor(image.pixel(8, button.height() // 2))


def _button_top_border(button: QPushButton, **state: bool) -> QColor:
    image = _render_button(button, **state)
    return QColor(image.pixel(button.width() // 2, 1))


def _widget_background(button: QPushButton) -> QColor:
    return QColor(button.grab().toImage().pixel(8, button.height() // 2))


def _widget_top_border(button: QPushButton) -> QColor:
    return QColor(button.grab().toImage().pixel(button.width() // 2, 1))


@pytest.fixture
def themed_buttons(qtbot, qapp):
    """Two buttons on a themed panel, isolated from the MainWindow state."""
    panel = QWidget()
    qtbot.addWidget(panel)
    apply_theme(panel)
    layout = QVBoxLayout(panel)
    layout.setSpacing(SIZES["spacing"])
    buttons = []
    for text in ("打开", "发送"):
        button = QPushButton(text)
        button.setMinimumWidth(160)
        layout.addWidget(button)
        buttons.append(button)
    panel.resize(400, 140)
    panel.show()
    qapp.processEvents()
    for button in buttons:
        button.ensurePolished()
    # The panel is returned so the test keeps the parent of both buttons alive.
    return panel, buttons


def test_set_primary_repolishes_property_and_reverts(themed_buttons):
    _, (primary_button, other_button) = themed_buttons
    primary_button.clearFocus()
    other_button.clearFocus()
    assert _button_background(primary_button) == QColor(COLORS["panel"])

    set_primary(primary_button, True)
    assert primary_button.property("primary") is True
    assert _button_background(primary_button) == QColor(COLORS["accent"])
    assert primary_button.palette().color(QPalette.ColorRole.ButtonText) == QColor(
        COLORS["receive"]
    )

    set_primary(primary_button, True)
    assert primary_button.property("primary") is True
    assert _button_background(primary_button) == QColor(COLORS["accent"])

    set_primary(primary_button, False)
    assert primary_button.property("primary") is False
    assert _button_background(primary_button) == QColor(COLORS["panel"])
    assert primary_button.palette().color(QPalette.ColorRole.ButtonText) == QColor(
        COLORS["text"]
    )


def test_set_primary_leaves_other_buttons_and_widget_state_unchanged(themed_buttons):
    _, (primary_button, other_button) = themed_buttons
    other_button.clearFocus()
    before = (other_button.text(), other_button.isEnabled(), other_button.sizeHint())
    primary_size_hint = primary_button.sizeHint()
    primary_minimum_hint = primary_button.minimumSizeHint()

    set_primary(primary_button, True)

    assert _button_background(other_button) == QColor(COLORS["panel"])
    assert other_button.palette().color(QPalette.ColorRole.ButtonText) == QColor(
        COLORS["text"]
    )
    assert (
        other_button.text(),
        other_button.isEnabled(),
        other_button.sizeHint(),
    ) == before
    assert primary_button.isEnabled()
    assert primary_button.text() == "打开"
    assert primary_button.sizeHint() == primary_size_hint
    assert primary_button.minimumSizeHint() == primary_minimum_hint
    # Repolishing must keep the UI font inherited from the themed ancestor.
    assert primary_button.font().family() == ui_font().family()
    assert primary_button.font().pointSizeF() == FONTS["ui_size"]


def test_primary_button_states_stay_readable_without_size_change(themed_buttons):
    _, (primary_button, _) = themed_buttons
    primary_button.clearFocus()
    set_primary(primary_button, True)
    primary_button.ensurePolished()
    baseline_size = primary_button.size()
    baseline_hint = primary_button.sizeHint()
    baseline_minimum_hint = primary_button.minimumSizeHint()
    states = {
        "default": (COLORS["accent"], {}),
        "hover": (COLORS["accent_hover"], {"hover": True}),
        "pressed": (COLORS["accent_pressed"], {"pressed": True}),
        "focus": (COLORS["accent"], {"focus": True}),
    }
    for name, (expected_color, state) in states.items():
        background = _button_background(primary_button, **state)
        assert background == QColor(expected_color), name
        assert _contrast_ratio(background, QColor(COLORS["receive"])) >= 4.5, name

    # Focus stays visible: the ring differs from the flat accent fill and the panel.
    focus_border = _button_top_border(primary_button, focus=True)
    assert focus_border == QColor(COLORS["text"])
    assert focus_border != _button_top_border(primary_button)
    assert _contrast_ratio(focus_border, QColor(COLORS["window"])) >= 3.0
    assert primary_button.size() == baseline_size
    assert primary_button.sizeHint() == baseline_hint
    assert primary_button.minimumSizeHint() == baseline_minimum_hint


def test_primary_button_widget_states_keep_size_and_disabled_colors(
    themed_buttons, qtbot, qapp
):
    _, (primary_button, _) = themed_buttons
    primary_button.clearFocus()
    set_primary(primary_button, True)
    primary_button.ensurePolished()
    baseline_size = primary_button.size()

    assert _widget_background(primary_button) == QColor(COLORS["accent"])
    primary_button.setDown(True)
    qapp.processEvents()
    assert _widget_background(primary_button) == QColor(COLORS["accent_pressed"])
    assert primary_button.size() == baseline_size
    primary_button.setDown(False)

    primary_button.setFocus()
    qtbot.waitUntil(primary_button.hasFocus)
    assert _widget_top_border(primary_button) == QColor(COLORS["text"])
    assert _widget_background(primary_button) == QColor(COLORS["accent"])
    assert primary_button.size() == baseline_size
    primary_button.clearFocus()
    qapp.processEvents()

    primary_button.setEnabled(False)
    primary_button.ensurePolished()
    qapp.processEvents()
    disabled_background = _widget_background(primary_button)
    assert disabled_background == QColor(COLORS["window"])
    assert _widget_top_border(primary_button) == QColor(COLORS["divider"])
    assert primary_button.palette().color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
    ) == QColor(COLORS["muted"])
    assert _contrast_ratio(disabled_background, QColor(COLORS["muted"])) >= 4.5
    assert primary_button.size() == baseline_size

    primary_button.setEnabled(True)
    primary_button.ensurePolished()
    qapp.processEvents()
    assert _widget_background(primary_button) == QColor(COLORS["accent"])
    assert primary_button.size() == baseline_size


def test_primary_style_applies_to_main_window_buttons(window):
    button = window.open_button
    button.clearFocus()
    set_primary(button, False)
    button.ensurePolished()
    assert _button_background(button) == QColor(COLORS["panel"])

    set_primary(button, True)
    assert _button_background(button) == QColor(COLORS["accent"])
    assert button.palette().color(QPalette.ColorRole.ButtonText) == QColor(
        COLORS["receive"]
    )
