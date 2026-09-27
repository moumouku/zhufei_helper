"""X/Y 缩放、平移与原始点悬停检查（REQ-0005 issue 018）。

页面交互通过真实 Qt 事件驱动：滚轮/悬停事件经 ``QApplication.sendEvent``
送到 ``chart_view.viewport()``，拖拽用 ``QTest`` 合成；只读公开轴状态（``QValueAxis``）和模型
原始采样，不依赖真实串口或真实睡眠。X/Y 轴数学与悬停邻近判定来自
``paimon_assistant.oscilloscope_chart`` / 页面公开行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from paimon_assistant.oscilloscope import (  # noqa: E402
    INT32_MAX,
    INT32_MIN,
    OscilloscopeSample,
    OscilloscopeSession,
)
from paimon_assistant.oscilloscope_chart import (  # noqa: E402
    DEFAULT_Y_MAX,
    DEFAULT_Y_MIN,
    build_chart_segments,
    fit_x_range,
)
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402

ORIGIN_NS = 1_000_000_000


def sample(relative_seconds: float) -> OscilloscopeSample:
    return OscilloscopeSample(relative_seconds, (0,))


def frame(clock: StepClock, relative_ns: int, payload: bytes) -> BoundaryEvent:
    clock.monotonic_ns = ORIGIN_NS + relative_ns
    return BoundaryEvent(
        clock.monotonic_ns, 1_700_000_000_000, payload, payload + b"\r\n"
    )


def make_page(qtbot) -> tuple[OscilloscopePage, StepClock]:
    clock = StepClock()
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


def show_page(qtbot, page: OscilloscopePage, width: int = 800, height: int = 500) -> None:
    page.resize(width, height)
    page.show()
    qtbot.waitUntil(lambda: page.chart.plotArea().width() > 0)


def viewport_point_of(page: OscilloscopePage, x: float, y: float, channel: int = 0) -> QPoint:
    """把图表坐标的点转换成 QChartView viewport 坐标。"""
    series = page.channel_series[channel][0]
    chart_pos = page.chart.mapToPosition(QPointF(x, y), series)
    result = page.chart_view.mapFromScene(page.chart.mapToScene(chart_pos))
    return QPoint(int(round(result.x())), int(round(result.y())))


def send_wheel(
    page: OscilloscopePage,
    position: QPoint,
    delta: int,
    modifiers=Qt.KeyboardModifier.NoModifier,
) -> None:
    event = QWheelEvent(
        QPointF(position),
        QPointF(position),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(page.chart_view.viewport(), event)


def move_mouse(page: OscilloscopePage, position: QPoint) -> None:
    """送一个真实 QMouseEvent 到 viewport；无按键悬停在原生平台也生效。"""
    viewport = page.chart_view.viewport()
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(position),
        QPointF(viewport.mapToGlobal(position)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)


# --------------------------------------------------- 纯数据：X 适配范围


def test_fit_x_range_keeps_exact_extent_for_distinct_times_under_one_second():
    assert fit_x_range([sample(0.10), sample(0.40)]) == pytest.approx((0.10, 0.40)), (
        "多个不同时间点必须精确覆盖真实保留范围，不得人为放大到一秒"
    )


def test_build_chart_segments_preserves_input_order_for_equal_timestamps():
    points = [(1.0, 9), (1.0, 2)]  # 同一相对时间：最大值先出现

    segments = build_chart_segments([points], x_min=0.0, x_max=2.0, pixel_width=1)

    assert segments == [[(1.0, 9), (1.0, 2)]], (
        "同一时间戳的局部极值必须保持输入顺序，不得按比较结果重新排序"
    )


def test_build_chart_segments_uses_adjacent_endpoints_to_draw_crossing_lines():
    segment = [(0.0, 1), (10.0, 2)]

    segments = build_chart_segments([segment], x_min=4.0, x_max=6.0, pixel_width=100)

    assert segments == [[(0.0, 1), (10.0, 2)]], (
        "稀疏连续分段在视口内没有样本时，必须用相邻原始端点画出穿越线"
    )


def test_build_chart_segments_does_not_connect_across_gaps():
    first = [(0.0, 1), (1.0, 2)]
    second = [(5.0, 5), (6.0, 6)]  # 短帧造成的真实缺口

    segments = build_chart_segments(
        [first, second], x_min=2.0, x_max=4.0, pixel_width=100
    )

    assert segments == [], "缺口两侧端点不得为了穿越视口而互相连接"


# ------------------------------------------- 可见的 X/Y 缩放按钮（非滚轮入口）


def test_visible_zoom_buttons_change_only_their_own_axis_state(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [frame(clock, 0, b"1"), frame(clock, 10_000_000_000, b"2")]
    )

    for button in (
        page.x_zoom_in_button,
        page.x_zoom_out_button,
        page.y_zoom_in_button,
        page.y_zoom_out_button,
    ):
        assert not button.isHidden(), "X/Y 缩放必须有可见入口，滚轮不是唯一途径"

    x_span_before = page.x_view_range()[1] - page.x_view_range()[0]
    page.x_zoom_in_button.click()
    x_in = page.x_view_range()
    assert x_in[1] - x_in[0] < x_span_before, "X+ 必须缩小 X 轴范围"
    assert page.chart_following is False, "可见缩放按钮也是手动缩放，必须退出跟随"
    assert page.axis_x.min() == pytest.approx(x_in[0])
    assert page.axis_x.max() == pytest.approx(x_in[1])

    y_before = page.y_view_range()
    page.x_zoom_out_button.click()
    x_out = page.x_view_range()
    assert x_out[1] - x_out[0] > x_in[1] - x_in[0]
    assert page.y_view_range() == pytest.approx(y_before), "X 按钮不得改动 Y 轴"

    y_span_before = page.y_view_range()[1] - page.y_view_range()[0]
    page.y_zoom_in_button.click()
    y_in = page.y_view_range()
    assert y_in[1] - y_in[0] < y_span_before, "Y+ 必须缩小 Y 轴范围"
    assert page.axis_y.min() == pytest.approx(y_in[0])
    assert page.axis_y.max() == pytest.approx(y_in[1])
    assert page.x_view_range() == pytest.approx(x_out), "Y 按钮不得改动 X 轴"

    page.y_zoom_out_button.click()
    y_out = page.y_view_range()
    assert y_out[1] - y_out[0] > y_in[1] - y_in[0] >= 0.0


# ------------------------------------------------- 滚轮：以指针图表坐标为锚点


def test_default_wheel_zooms_x_only_and_ctrl_wheel_zooms_y_only_at_pointer(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [
            frame(clock, 0, b"0"),
            frame(clock, 5_000_000_000, b"50"),
            frame(clock, 10_000_000_000, b"100"),
        ]
    )
    page.set_view_range(0.0, 10.0, y_min=0.0, y_max=100.0)

    def chart_position_of(point: QPoint):
        return page.chart.mapFromScene(page.chart_view.mapToScene(point))

    position = viewport_point_of(page, 6.0, 60.0)
    x_before = page.x_view_range()
    x_anchor = page.chart.mapToValue(chart_position_of(position), page.ch1_series).x()
    assert x_anchor == pytest.approx(6.0, abs=0.05), "指针应对应图表坐标而非视口中心"
    x_ratio_before = (x_anchor - x_before[0]) / (x_before[1] - x_before[0])

    send_wheel(page, position, 120)

    x_after = page.x_view_range()
    assert x_after[1] - x_after[0] < 10.0, "默认滚轮必须缩放 X 轴"
    assert page.chart_following is False, "手动滚轮缩放必须退出实时跟随"
    assert page.y_view_range() == pytest.approx((0.0, 100.0)), "默认滚轮不得改动 Y 轴"
    x_ratio_after = (x_anchor - x_after[0]) / (x_after[1] - x_after[0])
    assert x_ratio_after == pytest.approx(x_ratio_before, abs=1e-6), (
        "X 缩放必须以指针处的图表坐标为锚点，而不是视口中心"
    )
    assert x_ratio_after != pytest.approx(0.5, abs=1e-3)

    x_before_ctrl = page.x_view_range()
    y_before = page.y_view_range()
    y_anchor = page.chart.mapToValue(chart_position_of(position), page.ch1_series).y()
    y_ratio_before = (y_anchor - y_before[0]) / (y_before[1] - y_before[0])

    send_wheel(page, position, 120, Qt.KeyboardModifier.ControlModifier)

    y_after = page.y_view_range()
    assert y_after[1] - y_after[0] < y_before[1] - y_before[0], "Ctrl+滚轮只缩放 Y 轴"
    assert page.x_view_range() == pytest.approx(x_before_ctrl), "Ctrl+滚轮不得改动 X 轴"
    y_ratio_after = (y_anchor - y_after[0]) / (y_after[1] - y_after[0])
    assert y_ratio_after == pytest.approx(y_ratio_before, abs=1e-6), (
        "Y 缩放必须以指针处的图表坐标为锚点，而不是视口中心"
    )
    assert y_ratio_after != pytest.approx(0.5, abs=1e-3)

    send_wheel(page, position, -120)
    x_out = page.x_view_range()
    assert x_out[1] - x_out[0] > x_after[1] - x_after[0], "反向滚轮放大 X 跨度"


# --------------------------------------------------------- 左键拖动平移


def _drag(
    page: OscilloscopePage, start: QPoint, end: QPoint, modifiers=Qt.KeyboardModifier.NoModifier
) -> None:
    viewport = page.chart_view.viewport()
    QTest.mousePress(viewport, Qt.LeftButton, modifiers, pos=start)
    QTest.mouseMove(viewport, pos=end)
    QTest.mouseRelease(viewport, Qt.LeftButton, modifiers, pos=end)


def test_left_drag_pans_both_axes_and_modifiers_restrict_single_axis(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [
            frame(clock, 0, b"0"),
            frame(clock, 10_000_000_000, b"50"),
            frame(clock, 20_000_000_000, b"100"),
        ]
    )
    page.set_view_range(4.0, 14.0, y_min=20.0, y_max=80.0)

    center = page.chart_view.viewport().rect().center()
    _drag(page, center, center + QPoint(40, 30))

    x_both, y_both = page.x_view_range(), page.y_view_range()
    assert x_both[1] - x_both[0] == pytest.approx(10.0), "平移不得改变 X 宽度"
    assert x_both[0] < 4.0, "左键拖动必须同时平移 X（向右拖动使视图左移）"
    assert y_both[1] - y_both[0] == pytest.approx(60.0), "平移不得改变 Y 宽度"
    assert y_both[0] > 20.0, "左键拖动必须同时平移 Y"
    assert page.chart_following is False, "手动平移必须退出实时跟随"

    x_before, y_before = page.x_view_range(), page.y_view_range()
    _drag(page, center, center + QPoint(-30, 25), Qt.KeyboardModifier.ShiftModifier)
    x_shift, y_shift = page.x_view_range(), page.y_view_range()
    assert (x_shift[0], x_shift[1]) != pytest.approx(x_before), "Shift 拖动必须平移 X"
    assert y_shift == pytest.approx(y_before), "Shift 拖动不得改动 Y 轴"

    x_before, y_before = x_shift, y_shift
    _drag(page, center, center + QPoint(-30, 25), Qt.KeyboardModifier.ControlModifier)
    assert page.x_view_range() == pytest.approx(x_before), "Ctrl 拖动不得改动 X 轴"
    y_ctrl = page.y_view_range()
    assert (y_ctrl[0], y_ctrl[1]) != pytest.approx(y_before), "Ctrl 拖动必须平移 Y"

    # 极限位置反复拖动仍停留在保留窗口与 int32 硬范围内，且非零、非反向。
    _drag(page, center, center + QPoint(4000, 0), Qt.KeyboardModifier.ShiftModifier)
    clamped_x = page.x_view_range()
    assert 0.0 <= clamped_x[0] < clamped_x[1] <= 20.0

    page.set_view_range(4.0, 14.0, y_min=INT32_MAX - 100.0, y_max=float(INT32_MAX))
    _drag(page, center, center + QPoint(0, 4000), Qt.KeyboardModifier.ControlModifier)
    clamped_y = page.y_view_range()
    assert INT32_MIN <= clamped_y[0] < clamped_y[1] <= INT32_MAX


# ------------------------------- 手动视口退出跟随；新数据不移动，仅保留边界夹紧


def test_manual_view_does_not_move_on_new_data_and_clamps_at_retention_edge(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1"),
            frame(clock, 100_000_000_000, b"2"),
            frame(clock, 150_000_000_000, b"3"),
        ]
    )
    page.set_view_range(10.0, 40.0)
    assert page.chart_following is False

    page.consume_events([frame(clock, 160_000_000_000, b"5")])
    assert page.x_view_range() == pytest.approx((10.0, 40.0)), (
        "手动视口下新数据继续接收，但不得移动视口"
    )
    assert page.history_hint_label.isHidden()

    # 250 s 的采样把 0/10 s 点淘汰出 180 秒窗口，视口左缘越界后夹紧。
    page.consume_events([frame(clock, 250_000_000_000, b"6")])

    clamped = page.x_view_range()
    assert clamped[1] - clamped[0] == pytest.approx(30.0), "夹紧必须保持手动选择宽度"
    assert clamped[0] >= 100.0
    assert not page.history_hint_label.isHidden()


# --------------------------------- 回到最新：保留手动 X 宽度，不动已选 Y


def test_return_to_latest_preserves_manual_x_width_and_chosen_y(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1"),
            frame(clock, 10_000_000_000, b"2"),
            frame(clock, 20_000_000_000, b"3"),
        ]
    )
    page.set_view_range(2.0, 6.0, y_min=100.0, y_max=200.0)

    page.return_to_latest()

    assert page.chart_following is True
    x_latest = page.x_view_range()
    assert x_latest[1] - x_latest[0] == pytest.approx(4.0), (
        "回到最新只移动 X 视口，必须保留用户选定的宽度"
    )
    assert x_latest[1] == pytest.approx(20.0), "视口右缘移动到最新保留采样"
    assert page.y_view_range() == pytest.approx((100.0, 200.0)), (
        "回到最新不得隐式改动用户已选的 Y 范围"
    )

    page.consume_events([frame(clock, 30_000_000_000, b"4")])
    followed = page.x_view_range()
    assert followed[1] - followed[0] == pytest.approx(4.0)
    assert followed[1] == pytest.approx(30.0), "恢复跟随后仍以手动宽度跟随最新"
    assert page.y_view_range() == pytest.approx((100.0, 200.0))


# -------------------------------------------- 首个有效采样的初始 Y 适配


def test_initial_y_fit_makes_first_legal_sample_visible_and_resets_on_clear(qtbot):
    page, clock = make_page(qtbot)

    page.consume_events([frame(clock, 250_000_000, b"12")])
    y_first = page.y_view_range()
    assert y_first[0] < 12.0 < y_first[1], "首发采样 12 必须落在默认 Y 范围内可见"
    assert y_first[1] > y_first[0]

    page.consume_events([frame(clock, 1_000_000_000, b"99")])
    assert page.y_view_range() == pytest.approx(y_first), (
        "初始适配只对首个有效帧生效，后续新数据不得隐式改 Y"
    )

    page.clear_acquisition()
    assert page.y_view_range() == pytest.approx((DEFAULT_Y_MIN, DEFAULT_Y_MAX))

    page.consume_events([frame(clock, 2_000_000_000, b"-500")])
    y_after_clear = page.y_view_range()
    assert y_after_clear[0] < -500.0 < y_after_clear[1], "清空后新一轮采集重新适配初始 Y"


def test_initial_y_fit_for_constant_int32_extremes_stays_nonzero_inside_int32(qtbot):
    page, clock = make_page(qtbot)

    page.consume_events([frame(clock, 0, b"2147483647")])
    y_max = page.y_view_range()
    assert INT32_MIN <= y_max[0] < y_max[1] <= INT32_MAX, "常值极值仍须非零、非反向"
    assert y_max[0] < INT32_MAX <= y_max[1]

    page.clear_acquisition()
    page.consume_events([frame(clock, 1_000_000_000, b"-2147483648")])
    y_min = page.y_view_range()
    assert INT32_MIN <= y_min[0] < y_min[1] <= INT32_MAX
    assert y_min[0] <= INT32_MIN < y_min[1]


def test_explicit_y_choice_before_first_data_is_not_overridden_by_initial_fit(qtbot):
    page, clock = make_page(qtbot)

    page.set_view_range(0.0, 1.0, y_min=-5.0, y_max=5.0)
    page.consume_events([frame(clock, 0, b"12")])

    assert page.y_view_range() == pytest.approx((-5.0, 5.0)), (
        "用户显式选择的 Y 不得被首个有效帧的初始适配覆盖"
    )


# ----------------------------------------------------- 悬停读值（原始采样）


def _rendered_point_count(page: OscilloscopePage, index: int = 0) -> int:
    return sum(series.count() for series in page.channel_series[index])


def test_hover_shows_actual_frame_time_and_all_present_fields(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events([frame(clock, 250_000_000, b"12,34")])
    page.set_view_range(0.0, 1.0, y_min=0.0, y_max=100.0)
    point = viewport_point_of(page, 0.25, 12.0)

    move_mouse(page, point)

    assert page.hover_label.isVisible(), "靠近真实原始点必须显示读值"
    text = page.hover_label.text()
    assert "T+000.250 s" in text
    assert "CH1=12" in text and "CH2=34" in text, (
        "悬停显示该帧实际存在的全部通道和值"
    )


def test_hover_threshold_is_screen_pixel_distance(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events([frame(clock, 500_000_000, b"0")])
    page.set_view_range(0.0, 1.0, y_min=-10.0, y_max=10.0)
    point = viewport_point_of(page, 0.5, 0.0)

    move_mouse(page, point + QPoint(5, 0))
    assert page.hover_label.isVisible(), "8 像素阈值内的真实采样必须显示"

    move_mouse(page, point + QPoint(20, 0))
    assert not page.hover_label.isVisible(), "阈值外的指针不得显示读值"


def test_hover_hides_far_on_leave_and_after_view_or_data_changes(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events([frame(clock, 250_000_000, b"12")])
    page.set_view_range(0.0, 1.0, y_min=0.0, y_max=100.0)
    point = viewport_point_of(page, 0.25, 12.0)

    move_mouse(page, point)
    assert page.hover_label.isVisible()

    move_mouse(page, point + QPoint(30, 0))
    assert not page.hover_label.isVisible(), "远离有效采样必须隐藏旧读值"
    assert page.hover_label.text() == "", "隐藏时不得保留可能误导的旧文本"

    move_mouse(page, point)
    assert page.hover_label.isVisible()
    QApplication.sendEvent(page.chart_view.viewport(), QEvent(QEvent.Type.Leave))
    assert not page.hover_label.isVisible(), "指针离开图表必须隐藏读值"

    move_mouse(page, point + QPoint(1, 1))
    assert page.hover_label.isVisible()
    page.zoom_x(0.8)
    assert not page.hover_label.isVisible(), "视口变化后不得保留旧读值"

    point_after_zoom = viewport_point_of(page, 0.25, 12.0)
    move_mouse(page, point_after_zoom)
    assert page.hover_label.isVisible()
    page.consume_events([frame(clock, 500_000_000, b"13")])
    assert not page.hover_label.isVisible(), "新数据到达后不得保留可能过期的读值"


def test_clear_hides_hover_readout(qtbot):
    """清空同时清除悬停读值，不得把旧采集值留在新采集中（REQ-0005 §9.1）。"""
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events([frame(clock, 250_000_000, b"12")])
    page.set_view_range(0.0, 1.0, y_min=0.0, y_max=100.0)
    move_mouse(page, viewport_point_of(page, 0.25, 12.0))
    assert page.hover_label.isVisible()

    page.clear_acquisition()

    assert not page.hover_label.isVisible(), "清空后旧悬停读值必须失效"
    assert page.hover_label.text() == ""


def test_hover_queries_candidate_band_without_materializing_all_samples(
    qtbot, qapp, monkeypatch
):
    """悬停只查询指针附近时间带，不得为全量 180 秒保留点建列表。"""
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [
            frame(clock, 0, b"1"),
            frame(clock, 500_000_000, b"2"),
            frame(clock, 1_000_000_000, b"3"),
        ]
    )
    page.set_view_range(0.0, 1.25, y_min=0.0, y_max=10.0)
    point = viewport_point_of(page, 0.5, 2.0)

    def fail_materialize(_session):
        raise AssertionError("悬停不得构造 session.samples 全量列表")

    monkeypatch.setattr(
        OscilloscopeSession, "samples", property(fail_materialize)
    )

    move_mouse(page, point)

    assert page.hover_label.isVisible(), "候选带内的真实采样必须可悬停"
    assert "CH1=2" in page.hover_label.text()


def test_hover_ignores_disabled_channels_and_hides_when_none_enabled(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [
            frame(clock, 250_000_000, b"10,1000"),
            frame(clock, 750_000_000, b"20,2000"),
        ]
    )
    page.set_view_range(0.0, 1.0, y_min=0.0, y_max=2100.0)
    ch2_point = viewport_point_of(page, 0.75, 2000.0, channel=1)

    move_mouse(page, ch2_point)
    assert page.hover_label.isVisible()
    assert "CH2=2000" in page.hover_label.text()

    page.channel_checks["CH2"].setChecked(False)
    assert not page.hover_label.isVisible(), "关闭悬停中的通道必须立即隐藏旧读值"

    move_mouse(page, ch2_point + QPoint(0, 5))
    assert not page.hover_label.isVisible(), "关闭的通道不参与悬停邻近判定"

    page.channel_checks["CH1"].setChecked(False)
    ch1_point = viewport_point_of(page, 0.75, 20.0)
    move_mouse(page, ch1_point)
    assert not page.hover_label.isVisible(), "没有开启通道时隐藏读值"


def test_hover_uses_raw_samples_not_downsampled_series_points(qtbot, qapp):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    values = [0 if index % 2 == 0 else 1000 for index in range(1000)]
    values[500] = 500  # 既不是局部最小也不是局部最大，降采样会丢弃
    page.consume_events(
        [
            frame(clock, index * 1_000_000, str(value).encode())
            for index, value in enumerate(values)
        ]
    )
    page.set_view_range(0.0, 0.999, y_min=-1.0, y_max=1001.0)
    page.chart_view.setFixedWidth(120)
    qtbot.waitUntil(lambda: page.chart.plotArea().width() <= 100)
    qapp.processEvents()  # 落定 resize 触发的 singleShot 重绘

    assert _rendered_point_count(page) < len(values)
    rendered = {
        (round(series.at(i).x(), 9), int(series.at(i).y()))
        for series in page.channel_series[0]
        for i in range(series.count())
    }
    assert (0.5, 500) not in rendered, "测试前提：原始点已被降采样绘制丢弃"

    point = viewport_point_of(page, 0.5, 500.0)
    move_mouse(page, point)

    assert page.hover_label.isVisible(), "绘制点被降采样丢弃后仍须能悬停原始采样"
    assert "CH1=500" in page.hover_label.text(), (
        "悬停必须查询 session.samples 原始采样，而不是降采样绘制点"
    )


def test_hover_does_not_invent_values_for_missing_channel_fields(qtbot):
    page, clock = make_page(qtbot)
    show_page(qtbot, page)
    page.consume_events(
        [
            frame(clock, 250_000_000, b"1,5"),
            frame(clock, 500_000_000, b"2"),
            frame(clock, 1_000_000_000, b"3,5"),
        ]
    )
    page.set_view_range(0.0, 1.25, y_min=0.0, y_max=10.0)

    interpolated = viewport_point_of(page, 0.625, 5.0, channel=1)
    move_mouse(page, interpolated)
    assert not page.hover_label.isVisible(), "缺口中间不得生成虚构的 CH2 读值"

    real = viewport_point_of(page, 1.0, 5.0, channel=1)
    move_mouse(page, real)
    assert page.hover_label.isVisible()
    text = page.hover_label.text()
    assert "CH1=3" in text and "CH2=5" in text, "显示该帧实际存在的全部字段"


# ------------------------------- 极值反复缩放 / 缩小后仍保留穿越线与缺口断线


def test_repeated_zoom_after_int32_extremes_keeps_valid_axis(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events([frame(clock, 0, b"2147483647")])

    for _ in range(40):
        page.y_zoom_in_button.click()
        y_in = page.y_view_range()
        assert INT32_MIN <= y_in[0] < y_in[1] <= INT32_MAX

    for _ in range(80):
        page.y_zoom_out_button.click()
        y_out = page.y_view_range()
        assert INT32_MIN <= y_out[0] < y_out[1] <= INT32_MAX

    for _ in range(40):
        page.x_zoom_in_button.click()
        x_in = page.x_view_range()
        assert 0.0 <= x_in[0] < x_in[1] <= 1.0

    for _ in range(80):
        page.x_zoom_out_button.click()
        x_out = page.x_view_range()
        assert 0.0 <= x_out[0] < x_out[1] <= 1.0


def test_zoomed_sparse_view_keeps_crossing_line_and_does_not_bridge_gap(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1,5"),
            frame(clock, 5_000_000_000, b"2"),  # CH2 缺口
            frame(clock, 10_000_000_000, b"3,5"),
        ]
    )
    page.set_view_range(4.0, 6.0)

    ch1_points = [
        (series.at(i).x(), series.at(i).y())
        for series in page.channel_series[0]
        for i in range(series.count())
    ]
    assert (0.0, 1.0) in ch1_points and (10.0, 3.0) in ch1_points, (
        "连续分段在视口内无采样时，必须用相邻原始端点画出穿越线"
    )
    assert _rendered_point_count(page, index=1) == 0, (
        "缺口两侧的 CH2 分段不得为了穿越视口而互相连接"
    )
    assert page.session.channel_segments(1) == [
        [(0.0, 5)],
        [(10.0, 5)],
    ], "缺口分段仍由原始采样保持独立"
