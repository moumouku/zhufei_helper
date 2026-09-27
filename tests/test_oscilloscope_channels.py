"""示波器动态多通道与缺口（REQ-0005 issue 015）。

只通过公开行为验证：Qt 无关的 ``OscilloscopeSession`` 通道查询与页面
``OscilloscopePage`` 的 QtCharts 绘制。边界事件、注入时钟与 fake 串口
复用既有测试模块，不重复搭建大段替身；本文件为真实 QtCharts offscreen 测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor  # noqa: E402

from paimon_assistant.oscilloscope import OscilloscopeSession  # noqa: E402
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from paimon_assistant.theme import CHANNEL_COLORS  # noqa: E402
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402


def make_session(clock: StepClock) -> OscilloscopeSession:
    session = OscilloscopeSession(monotonic_ns=clock.mono)
    session.begin_acquisition()
    return session


def frame(clock: StepClock, payload: bytes, at_ns: int) -> BoundaryEvent:
    clock.monotonic_ns = at_ns
    return BoundaryEvent(at_ns, 1_700_000_000_000, payload, payload + b"\r\n")


def make_page(qtbot) -> tuple[OscilloscopePage, StepClock]:
    clock = StepClock()
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


# ------------------------------------------------ 模型：动态通道数


def test_legal_frames_grow_channel_count_and_short_or_invalid_never_reduce():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, b"1", 1_100_000_000))
    assert session.channel_count == 1, "首条合法帧按字段顺序建立通道"

    session.consume(frame(clock, b"2,3", 1_200_000_000))
    assert session.channel_count == 2, "更宽合法帧只增加缺少的通道"

    session.consume(frame(clock, b"4", 1_300_000_000))
    assert session.channel_count == 2, "合法短帧不得减少通道数"

    session.consume(frame(clock, b"9 9", 1_400_000_000))
    assert session.channel_count == 2, "非法帧不得减少通道数"


def test_channel_segments_share_frame_time_and_break_at_short_frames():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, b"1,2", 1_250_000_000))  # T+0.25
    session.consume(frame(clock, b"3", 1_500_000_000))  # T+0.50：CH2 缺口
    session.consume(frame(clock, b"9 9", 1_600_000_000))  # 非法：不产生采样或缺口
    session.consume(frame(clock, b"4,5", 1_750_000_000))  # T+0.75

    assert session.channel_segments(0) == [
        [(0.25, 1), (0.5, 3), (0.75, 4)]
    ], "同一帧的实际字段共享完全相同的相对时间，短帧不生成伪采样"
    assert session.channel_segments(1) == [
        [(0.25, 2)],
        [(0.75, 5)],
    ], "合法短帧使缺失通道在缺口两侧分段，非法帧不得制造缺口"


def test_latest_valid_value_keeps_last_present_field_and_ignores_invalid():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, b"7,8", 1_250_000_000))
    session.consume(frame(clock, b"9", 1_500_000_000))
    session.consume(frame(clock, b"bad", 1_600_000_000))

    assert session.channel_latest_value(0) == 9
    assert session.channel_latest_value(1) == 8, "短帧不更新缺失通道的最新值"
    assert session.channel_latest_value(2) is None, "未发现的通道没有最新值"


# ------------------------------------------------ 页面：通道栏与开关


def test_first_legal_frame_creates_checked_channel_rows_with_latest_values(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1_700_000_000_000, b"-10,20,30", b"-10,20,30\r\n")]
    )

    assert list(page.channel_checks) == ["CH1", "CH2", "CH3"]
    assert all(check.isChecked() for check in page.channel_checks.values()), "新通道默认显示"
    assert page.channel_labels["CH1"].text() == "CH1  -10"
    assert page.channel_labels["CH2"].text() == "CH2  20"
    assert page.channel_labels["CH3"].text() == "CH3  30"


def test_short_frame_splits_qtcharts_segments_without_cross_gap_line(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [
            BoundaryEvent(1_250_000_000, 1, b"1,2", b"1,2\r\n"),  # T+0.25
            BoundaryEvent(1_500_000_000, 1, b"3", b"3\r\n"),  # T+0.50：CH2 缺口
            BoundaryEvent(1_600_000_000, 1, b"x y", b"x y\r\n"),  # 非法：不制造缺口
            BoundaryEvent(1_750_000_000, 1, b"4,5", b"4,5\r\n"),  # T+0.75
        ]
    )

    ch1_segments = page.channel_series[0]
    assert len(ch1_segments) == 1, "CH1 每帧都有值，缺口不适用于它"
    assert ch1_segments[0].count() == 3
    assert [point.x() for point in (ch1_segments[0].at(i) for i in range(3))] == [
        pytest.approx(0.25),
        pytest.approx(0.5),
        pytest.approx(0.75),
    ]

    ch2_segments = page.channel_series[1]
    assert len(ch2_segments) == 2, "合法短帧必须把 CH2 拆成两个绘制分段"
    assert ch2_segments[0].count() == 1
    assert ch2_segments[0].at(0).x() == pytest.approx(0.25)
    assert ch2_segments[0].at(0).y() == pytest.approx(2.0)
    assert ch2_segments[1].count() == 1
    assert ch2_segments[1].at(0).x() == pytest.approx(0.75)
    assert ch2_segments[1].at(0).y() == pytest.approx(5.0)
    assert page.channel_labels["CH2"].text() == "CH2  5", "最新有效值来自缺口后的宽帧"
    assert page.session.channel_count == 2


def test_unchecking_channel_hides_curve_but_keeps_sampling_and_latest(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1, b"1,2", b"1,2\r\n")]
    )

    page.channel_checks["CH2"].setChecked(False)
    assert not any(series.isVisible() for series in page.channel_series[1])

    # 隐藏期间经历一次合法短帧：CH2 的新分段不得自动现身。
    page.consume_events(
        [
            BoundaryEvent(1_400_000_000, 1, b"3", b"3\r\n"),  # T+0.40 短帧
            BoundaryEvent(1_500_000_000, 1, b"4,5", b"4,5\r\n"),  # T+0.50
        ]
    )

    assert page.channel_labels["CH2"].text() == "CH2  5", "关闭通道仍更新最新有效值"
    assert page.session.samples[-1].values == (4, 5), "关闭通道仍保存原始采样"
    assert len(page.channel_series[1]) == 2, "隐藏通道的缺口分段照常建立"
    assert not any(series.isVisible() for series in page.channel_series[1])
    assert page.channel_series[1][-1].count() == 1, "隐藏期间的点仍进入绘制数据"

    page.channel_checks["CH2"].setChecked(True)
    assert all(
        series.isVisible() for series in page.channel_series[1]
    ), "重新打开从原始历史恢复绘制"


def test_eight_channel_series_use_distinct_theme_colors(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [
            BoundaryEvent(
                1_250_000_000, 1, b"1,2,3,4,5,6,7,8", b"1,2,3,4,5,6,7,8\r\n"
            )
        ]
    )

    assert len(CHANNEL_COLORS) == 8
    assert len(set(CHANNEL_COLORS)) == 8, "多色方案必须可区分"
    for index in range(8):
        series = page.channel_series[index][0]
        assert series.pen().color() == QColor(CHANNEL_COLORS[index])
        assert page.axis_y in series.attachedAxes(), "所有显示通道共用同一条 Y 轴"
        assert page.axis_x in series.attachedAxes()
    assert list(page.channel_labels) == [f"CH{i}" for i in range(1, 9)]


def test_invalid_frame_does_not_create_channels_or_change_curves_or_latest(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1, b"1,2", b"1,2\r\n")]
    )
    before = {
        index: [(series.name(), series.count()) for series in series_list]
        for index, series_list in page.channel_series.items()
    }

    page.consume_events(
        [BoundaryEvent(1_500_000_000, 1, b"a,b,c", b"a,b,c\r\n")]
    )

    assert page.session.channel_count == 2, "非法帧不得建立通道"
    assert list(page.channel_checks) == ["CH1", "CH2"]
    assert page.channel_labels["CH1"].text() == "CH1  1", "非法帧不更新最新值"
    assert page.channel_labels["CH2"].text() == "CH2  2"
    after = {
        index: [(series.name(), series.count()) for series in series_list]
        for index, series_list in page.channel_series.items()
    }
    assert after == before, "非法帧不得改变既有曲线"


def test_single_point_channel_is_rendered_with_visible_point_marker(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1, b"7", b"7\r\n")]
    )

    assert page.ch1_series.count() == 1
    assert page.ch1_series.pointsVisible(), "只有一个点时 QLineSeries 必须画出点标记"


def test_clear_resets_channel_inference_rows_and_series(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1, b"1,2", b"1,2\r\n")]
    )
    assert page.channel_labels["CH2"].text() == "CH2  2"

    page.clear_acquisition()

    assert page.session.channel_count == 0
    assert page.channel_rows == {}
    assert page.channel_checks == {}
    assert page.channel_labels == {}
    assert page.channel_series == {0: [page.ch1_series]}
    assert page.ch1_series.count() == 0

    page.consume_events(
        [BoundaryEvent(2_500_000_000, 1, b"7", b"7\r\n")]
    )
    assert list(page.channel_checks) == ["CH1"], "清空后按新合法帧重新推断通道"
    assert page.channel_labels["CH1"].text() == "CH1  7"


def test_eight_channel_rows_fit_the_760x480_minimum_page(qtbot, qapp):
    page, _clock = make_page(qtbot)
    page.resize(760, 480)
    page.show()
    qapp.processEvents()

    longest = b"-2147483648"
    page.consume_events(
        [
            BoundaryEvent(
                1_250_000_000,
                1,
                b",".join([longest] * 8),
                b",".join([longest] * 8) + b"\r\n",
            )
        ]
    )
    qapp.processEvents()

    panel = page.channel_panel
    last_check = page.channel_checks["CH8"]
    bottom = last_check.mapTo(panel, last_check.rect().bottomLeft()).y()
    assert 0 <= bottom <= panel.height(), "760x480 下 8 行通道栏不得溢出"
    assert panel.width() >= 140
    for name, label in page.channel_labels.items():
        assert label.width() >= label.sizeHint().width(), f"{name} 的 int32 值不得被裁剪"
