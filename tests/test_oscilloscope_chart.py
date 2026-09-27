"""视口驱动绘图、自动缩放与实时/历史导航（REQ-0005 issue 017）。

分两层：``paimon_assistant.oscilloscope_chart`` 的纯数据 min/max 降采样与
坐标数学不依赖 Qt；``OscilloscopePage`` 的自动缩放、回到最新、手动视口、
历史夹紧和资源失败路径使用 Qt offscreen、注入时钟与注入绘图函数验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant.oscilloscope import (  # noqa: E402
    INT32_MAX,
    INT32_MIN,
    OscilloscopeSample,
    OscilloscopeSession,
)
from paimon_assistant.oscilloscope_chart import (  # noqa: E402
    DEFAULT_X_MAX,
    DEFAULT_X_MIN,
    DEFAULT_Y_MAX,
    DEFAULT_Y_MIN,
    build_chart_segments,
    clamp_x_range,
    fit_x_range,
    fit_y_range,
    keep_x_span_in_range,
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


def make_page(qtbot, chart_builder=None):
    clock = StepClock()
    page = OscilloscopePage(
        OscilloscopeSession(monotonic_ns=clock.mono), chart_builder=chart_builder
    )
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


# --------------------------------------------------- 纯数据：min/max 降采样


def test_build_chart_segments_keeps_ordered_min_max_per_pixel_bucket():
    points = [
        (0.0, 10),
        (0.1, 9),
        (0.2, 500),  # 桶内尖峰必须保留
        (0.3, 8),
        (0.4, 7),  # 桶内最小值
        (2.0, 11),
        (2.1, 12),
    ]
    before = list(points)

    segments = build_chart_segments([points], x_min=0.0, x_max=2.1, pixel_width=2)

    assert len(segments) == 1
    assert segments[0] == pytest.approx(
        [(0.2, 500), (0.4, 7), (2.0, 11), (2.1, 12)]
    ), "每个像素桶只保留按时间有序的局部最小值和最大值"
    assert points == before, "降采样不得改写原始采样输入"


def test_build_chart_segments_preserves_raw_points_gaps_and_adjacent_context():
    first = [(0.0, 1), (1.0, 2), (2.0, 3)]
    second = [(4.0, 4)]  # 短帧造成的真实缺口，必须保持独立分段

    segments = build_chart_segments(
        [first, second], x_min=0.5, x_max=4.0, pixel_width=100
    )

    assert segments == [
        [(0.0, 1), (1.0, 2), (2.0, 3)],
        [(4.0, 4)],
    ], (
        "视口足够宽时透传原始点；左侧相邻端点用于画出穿越视口的连线，"
        "缺口仍保持独立分段且不跨缺口连接"
    )


def test_fit_x_range_covers_retained_times_and_broadens_degenerate_views():
    assert fit_x_range([]) == (DEFAULT_X_MIN, DEFAULT_X_MAX)
    assert fit_x_range([sample(30.0), sample(120.0)]) == pytest.approx((30.0, 120.0))

    single = fit_x_range([sample(0.25)])
    assert single[1] - single[0] >= 1.0, "单点数据仍须有非零宽度"
    assert single[0] >= 0.0, "相对时间轴不从负值开始"
    assert single[0] <= 0.25 <= single[1]

    assert fit_x_range([sample(0.0)]) == pytest.approx((0.0, 1.0))


def test_fit_y_range_uses_only_visible_x_and_int32_limits():
    enabled_ch1 = [[(0.0, 1), (10.0, 5), (20.0, 3)]]
    enabled_ch2 = [[(0.0, -1000), (10.0, 1000)]]

    assert fit_y_range([enabled_ch1, enabled_ch2], 9.0, 11.0) == pytest.approx(
        (5.0, 1000.0)
    ), "Y 只统计当前可见 X 范围内的有效值，忽略范围外的点"
    assert fit_y_range([enabled_ch1], 0.0, 20.0) == pytest.approx((1.0, 5.0))
    assert fit_y_range([], 0.0, 1.0) == (DEFAULT_Y_MIN, DEFAULT_Y_MAX)

    assert fit_y_range([[[(0.0, 7)]]], 0.0, 1.0) == pytest.approx((6.0, 8.0))
    assert fit_y_range([[[(0.0, INT32_MAX)]]], 0.0, 1.0) == (
        INT32_MAX - 1,
        INT32_MAX,
    )
    assert fit_y_range([[[(0.0, INT32_MIN)]]], 0.0, 1.0) == (
        INT32_MIN,
        INT32_MIN + 1,
    )


def test_manual_view_clamp_helpers_never_produce_empty_or_inverted_ranges():
    assert clamp_x_range(-5.0, 5.0, 0.0, 10.0) == (0.0, 5.0)
    assert clamp_x_range(20.0, 30.0, 0.0, 10.0) == (0.0, 10.0)
    assert clamp_x_range(2.0, 8.0, 0.0, 10.0) == (2.0, 8.0)

    assert keep_x_span_in_range(0.0, 10.0, 100.0, 280.0) == (100.0, 110.0)
    assert keep_x_span_in_range(270.0, 290.0, 100.0, 280.0) == (260.0, 280.0)
    assert keep_x_span_in_range(120.0, 130.0, 100.0, 280.0) == (120.0, 130.0)


# --------------------------------------------- 页面：自动缩放（X + 开启通道 Y）


def test_auto_scale_fits_retained_x_and_only_enabled_channels_y(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1,1000"),
            frame(clock, 10_000_000_000, b"2,2000"),
            frame(clock, 20_000_000_000, b"3,3000"),
        ]
    )
    page.channel_checks["CH2"].setChecked(False)

    page.auto_scale_button.click()

    assert page.x_view_range() == pytest.approx((0.0, 20.0)), (
        "自动缩放把 X 适配全部保留采样时间范围"
    )
    assert page.y_view_range() == pytest.approx((1.0, 3.0)), (
        "Y 只统计已开启通道的可见有效值，忽略隐藏通道"
    )


def test_auto_scale_without_data_uses_legal_visible_defaults(qtbot):
    page, _clock = make_page(qtbot)

    page.auto_scale()

    assert page.x_view_range() == pytest.approx((DEFAULT_X_MIN, DEFAULT_X_MAX))
    assert page.y_view_range() == pytest.approx((DEFAULT_Y_MIN, DEFAULT_Y_MAX))


# ------------------------------- 页面：回到最新 / 手动视口 / 历史夹紧


def test_return_to_latest_moves_x_to_latest_keeps_y_and_resumes_following(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1"),
            frame(clock, 10_000_000_000, b"2"),
            frame(clock, 20_000_000_000, b"3"),
        ]
    )
    page.set_view_range(0.0, 10.0, y_min=100.0, y_max=200.0)
    assert page.chart_following is False

    page.chart_latest_button.click()

    assert page.chart_following is True
    # issue 018 修正：回到最新是移动视口，保留用户手动选择的 X 宽度。
    assert page.x_view_range() == pytest.approx((10.0, 20.0))
    assert page.y_view_range() == pytest.approx((100.0, 200.0)), (
        "回到最新只移动 X 视口，不得隐式执行 Y 自动缩放"
    )

    page.consume_events([frame(clock, 30_000_000_000, b"4")])
    assert page.x_view_range() == pytest.approx((20.0, 30.0)), (
        "恢复跟随后以保留的手动宽度跟随最新数据"
    )
    assert page.y_view_range() == pytest.approx((100.0, 200.0))


def test_manual_view_range_leaves_follow_and_survives_new_data(qtbot):
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
    assert page.x_view_range() == pytest.approx((10.0, 40.0))

    page.consume_events([frame(clock, 160_000_000_000, b"5")])  # 未发生淘汰

    assert page.x_view_range() == pytest.approx((10.0, 40.0)), (
        "手动视口下新数据继续接收，但不得强制移动视口"
    )


def test_set_view_range_clamps_to_retained_x_and_int32_y(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [frame(clock, 0, b"1"), frame(clock, 100_000_000_000, b"2")]
    )

    page.set_view_range(
        -50.0, 500.0, y_min=INT32_MIN - 10, y_max=INT32_MAX + 10
    )

    assert page.x_view_range() == pytest.approx((0.0, 100.0)), (
        "X 手动视口不得超出仍保留的采样时间窗"
    )
    assert page.y_view_range() == (INT32_MIN, INT32_MAX), "Y 不得超出 int32 硬范围"


def test_history_eviction_clamps_manual_view_and_shows_nonmodal_hint(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            frame(clock, 0, b"1"),
            frame(clock, 100_000_000_000, b"2"),
            frame(clock, 150_000_000_000, b"3"),
        ]
    )
    page.set_view_range(0.0, 50.0)
    assert page.history_hint_label.isHidden()

    # 250 s 的新采样把 0 s 点淘汰出 180 秒窗口（cutoff = 70 s）。
    page.consume_events([frame(clock, 250_000_000_000, b"4")])

    assert [item.relative_seconds for item in page.session.samples] == pytest.approx(
        [100.0, 150.0, 250.0]
    )
    assert page.x_view_range() == pytest.approx((100.0, 150.0)), (
        "历史淘汰越过左侧时把视口夹紧到仍保留范围并保持跨度"
    )
    assert not page.history_hint_label.isHidden(), "显示非模态“历史正在滚动淘汰”状态"
    assert "淘汰" in page.history_hint_label.text()

    page.return_to_latest()
    assert page.history_hint_label.isHidden()
    assert page.x_view_range() == pytest.approx((200.0, 250.0)), (
        "回到最新保留手动 X 宽度 50 s，右缘移动到最新采样"
    )

    page.consume_events([frame(clock, 260_000_000_000, b"5")])
    assert page.x_view_range() == pytest.approx((210.0, 260.0))


# --------------------------------- 页面：尺寸重绘 / 图例 / 清空 / 资源失败


def _rendered_points(page: OscilloscopePage, index: int = 0) -> int:
    return sum(series.count() for series in page.channel_series[index])


def test_chart_view_resize_regenerates_series_from_raw_samples(qtbot, qapp):
    page, clock = make_page(qtbot)
    page.resize(640, 480)
    page.show()
    qapp.processEvents()
    raw_times = [index * 0.1 for index in range(200)]
    page.consume_events(
        [
            frame(clock, index * 100_000_000, str(index).encode())
            for index in range(200)
        ]
    )

    page.chart_view.setFixedWidth(40)
    qtbot.waitUntil(lambda: _rendered_points(page) < 200)
    narrow_points = _rendered_points(page)

    page.chart_view.setFixedWidth(600)
    qtbot.waitUntil(lambda: _rendered_points(page) == 200)

    assert narrow_points < 200, "窄像素宽度必须按视口从原始采样重新降采样"
    assert [item.relative_seconds for item in page.session.samples] == pytest.approx(
        raw_times
    ), "尺寸变化重绘不得删除或改写原始采样"


def test_gap_segments_keep_one_legend_entry_and_hidden_channel_stays_raw(qtbot, qapp):
    page, clock = make_page(qtbot)
    page.resize(640, 480)
    page.show()
    qapp.processEvents()
    page.consume_events(
        [
            frame(clock, 0, b"1,2"),
            frame(clock, 1_000_000_000, b"3"),  # T+1.0：CH2 缺口
            frame(clock, 2_000_000_000, b"4,5"),  # T+2.0：CH2 新分段
        ]
    )
    qapp.processEvents()

    ch2_segments = page.channel_series[1]
    assert len(ch2_segments) == 2, "缺口两侧必须仍是两个独立绘制分段"
    first_markers = page.chart.legend().markers(ch2_segments[0])
    second_markers = page.chart.legend().markers(ch2_segments[1])
    assert first_markers and first_markers[0].isVisible()
    assert second_markers and not second_markers[0].isVisible(), (
        "同一通道的缺口分段不得在图例中重复出现"
    )

    page.channel_checks["CH2"].setChecked(False)

    assert not any(series.isVisible() for series in ch2_segments), (
        "隐藏通道不进入绘制，但原始数据仍保留"
    )
    assert page.session.channel_segments(1) == [
        [(0.0, 2)],
        [(2.0, 5)],
    ]


def test_clear_restores_empty_default_chart_and_chart_follow(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [frame(clock, 0, b"1"), frame(clock, 10_000_000_000, b"2")]
    )
    page.set_view_range(0.0, 5.0)
    assert page.chart_following is False

    page.clear_acquisition()

    assert page.chart_following is True
    assert page.x_view_range() == pytest.approx((DEFAULT_X_MIN, DEFAULT_X_MAX))
    assert page.y_view_range() == pytest.approx((DEFAULT_Y_MIN, DEFAULT_Y_MAX))
    assert page.ch1_series.count() == 0
    assert page.history_hint_label.isHidden()
    assert page.session.samples == []


def test_clear_after_resource_failure_allows_new_acquisition_to_render(qtbot):
    """显式新建采集解除绘图资源失败状态；清空诊断并恢复绘制（issue 019）。"""
    failed = {"once": False}

    def flaky_builder(segments, x_min, x_max, pixel_width):
        if any(segments) and not failed["once"]:
            failed["once"] = True
            raise MemoryError("simulated chart allocation failure")
        return build_chart_segments(segments, x_min, x_max, pixel_width)

    page, clock = make_page(qtbot, chart_builder=flaky_builder)
    page.consume_events([frame(clock, 0, b"1,2")])
    assert not page.is_receiving, "资源失败先停止页面接收"
    assert "资源" in page.diagnostic_label.text()

    page.clear_acquisition(origin_ns=2_000_000_000)

    assert page.diagnostic_label.text() == "", "清空必须清除资源诊断"
    page.consume_events([frame(clock, 1_000_000_000, b"7")])
    assert page.ch1_series.count() == 1, "清空后新一轮采集必须恢复绘制"


def test_chart_resource_failure_stops_page_and_preserves_raw_samples(qtbot):
    def failing_builder(segments, x_min, x_max, pixel_width):
        if any(segments):
            raise MemoryError("simulated chart allocation failure")
        return build_chart_segments(segments, x_min, x_max, pixel_width)

    page, clock = make_page(qtbot, chart_builder=failing_builder)
    failures = []
    page.resource_failed.connect(failures.append)

    page.consume_events([frame(clock, 0, b"1,2")])

    assert [item.values for item in page.session.samples] == [
        (1, 2)
    ], "绘图资源失败不得删除或降级已采集的原始采样"
    assert page.is_receiving is False, "资源失败必须停止页面接收状态"
    assert page.state_label.text() != "接收中"
    assert "资源" in page.diagnostic_label.text()
    assert failures and "simulated" in failures[0]
