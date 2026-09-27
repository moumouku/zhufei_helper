"""示波器小数协议（REQ-0005 用户修订：在兼容既有整数拼写的前提下支持小数）。

只通过公开行为验证：``parse_frame_payload`` 的十进制语法矩阵、
``OscilloscopeSession`` 的采样/分段/最新值、页面通道标签与悬停读数、
QtCharts 绘制的数值精度。既有整数边界、淘汰窗口与存储原子性由既有
测试模块继续覆盖，本文件只补齐小数引入的新行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant.oscilloscope import (  # noqa: E402
    OscilloscopeSample,
    OscilloscopeSession,
    format_sample_value,
    parse_frame_payload,
)
from paimon_assistant.oscilloscope_chart import fit_y_range  # noqa: E402
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from test_oscilloscope_compact_storage import FailingValuesStore  # noqa: E402
from test_oscilloscope_interactions import (  # noqa: E402
    move_mouse,
    viewport_point_of,
)
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402

ORIGIN_NS = 1_000_000_000


def make_session(clock: StepClock) -> OscilloscopeSession:
    session = OscilloscopeSession(monotonic_ns=clock.mono)
    session.begin_acquisition()
    return session


def make_page(qtbot) -> tuple[OscilloscopePage, StepClock]:
    clock = StepClock()
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


def frame(clock: StepClock, relative_ns: int, payload: bytes) -> BoundaryEvent:
    clock.monotonic_ns = ORIGIN_NS + relative_ns
    return BoundaryEvent(
        clock.monotonic_ns, 1_700_000_000_000, payload, payload + b"\r\n"
    )


def test_decimal_frame_reaches_compact_store_segments_and_latest_without_truncation():
    clock = StepClock()
    session = make_session(clock)

    record = session.consume(frame(clock, 250_000_000, b"0.96,328.00"))

    assert record.parse_ok is True
    assert session.samples == [OscilloscopeSample(0.25, (0.96, 328.0))]
    assert session.channel_segments(0) == [[(0.25, 0.96)]]
    assert session.channel_segments(1) == [[(0.25, 328.0)]]
    assert session.channel_latest_value(0) == 0.96
    assert session.channel_latest_value(1) == 328.0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"0.96", (0.96,)),
        (b"-0.13", (-0.13,)),
        (b"328.00", (328.0,)),
        (b"+.5", (0.5,)),
        (b"-.5", (-0.5,)),
        (b"1.", (1.0,)),
        (b"-000.250", (-0.25,)),
        (b"00012.50", (12.5,)),
        (b"12,+34.5,-.5,0.000", (12.0, 34.5, -0.5, 0.0)),
    ],
)
def test_supported_decimal_spellings_parse_to_binary64(payload, expected):
    assert parse_frame_payload(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"1e5",
        b"1E5",
        b"1e+5",
        b"1e-5",
        b"nan",
        b"NaN",
        b"inf",
        b"-inf",
        b"+Infinity",
        b"1.2.3",
        b"..5",
        b".",
        b"+.",
        b"-.",
        b".5.",
        b"0x1.8p3",
    ],
)
def test_exponent_and_non_finite_spellings_stay_parse_failures(payload):
    assert parse_frame_payload(payload) is None


def test_int32_decimal_bounds_validate_by_intended_decimal_value():
    # int32 两端及其合法小数邻域按真实十进制值接受。
    assert parse_frame_payload(b"2147483647.0") == (2147483647.0,)
    assert parse_frame_payload(b"-2147483648.0") == (-2147483648.0,)

    # 合法值转 binary64 时允许向端点舍入，但十进制值本身仍须在范围内。
    assert parse_frame_payload(b"2147483646.9999999999") == (2147483647.0,)
    assert parse_frame_payload(b"-2147483647.9999999999") == (-2147483648.0,)

    # 只比边界多 1e-10 也必须按十进制值拒绝，不能被 float 舍入进范围。
    assert parse_frame_payload(b"2147483647.0000000001") is None
    assert parse_frame_payload(b"-2147483648.0000000001") is None
    assert parse_frame_payload(b"2147483647.5") is None
    assert parse_frame_payload(b"-2147483648.5") is None
    assert parse_frame_payload(b"2147483648.0") is None
    assert parse_frame_payload(b"-2147483649.0") is None


def test_one_mib_leading_zeros_with_fraction_still_parse_by_value():
    # 1 MiB 恰好是分帧上限；前导零数量不受 int() 字符串位数限制影响。
    assert parse_frame_payload(b"0" * 1_048_572 + b"1.25") == (1.25,)
    assert parse_frame_payload(b"+" + b"0" * 100_000 + b"42.500") == (42.5,)
    assert parse_frame_payload(b"0" * 500_000 + b".5") == (0.5,)
    # 前导零再多也不能把溢出的十进制值变回合法。
    assert parse_frame_payload(b"0" * 5_000 + b"2147483648.5") is None
    assert parse_frame_payload(b"0" * 5_000 + b"2147483647.5") is None


def test_actual_decimal_sequence_flows_through_store_segments_and_latest():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, 250_000_000, b"0.96,328.00"))
    session.consume(frame(clock, 500_000_000, b"0.76,329.00"))
    session.consume(frame(clock, 750_000_000, b"-0.13,330.00"))

    assert session.samples == [
        OscilloscopeSample(0.25, (0.96, 328.0)),
        OscilloscopeSample(0.5, (0.76, 329.0)),
        OscilloscopeSample(0.75, (-0.13, 330.0)),
    ]
    assert session.channel_segments(0) == [
        [(0.25, 0.96), (0.5, 0.76), (0.75, -0.13)]
    ]
    assert session.channel_segments(1) == [
        [(0.25, 328.0), (0.5, 329.0), (0.75, 330.0)]
    ]
    assert session.channel_latest_value(0) == -0.13
    assert session.channel_latest_value(1) == 330.0
    assert session.sample_count == 3


def test_readout_keeps_integral_values_as_plain_integers():
    assert format_sample_value(328.0) == "328"
    assert format_sample_value(12.0) == "12"
    assert format_sample_value(2147483647.0) == "2147483647"
    assert format_sample_value(-2147483648.0) == "-2147483648"
    assert format_sample_value(-0.0) == "0"


def test_readout_uses_shortest_roundtrip_fraction_text():
    assert format_sample_value(0.96) == "0.96"
    assert format_sample_value(-0.13) == "-0.13"
    assert format_sample_value(0.5) == "0.5"
    assert format_sample_value(0.1 + 0.2) == "0.30000000000000004"
    assert float(format_sample_value(0.96)) == 0.96


def test_page_channel_label_shows_shortest_decimal_readout(qtbot):
    page, clock = make_page(qtbot)

    page.consume_events([frame(clock, 250_000_000, b"0.96,328.00")])

    assert page.channel_labels["CH1"].text() == "CH1  0.96"
    assert page.channel_labels["CH2"].text() == "CH2  328"


def test_page_hover_readout_uses_shortest_decimal_text(qtbot):
    page, clock = make_page(qtbot)
    page.resize(800, 500)
    page.show()
    qtbot.waitUntil(lambda: page.chart.plotArea().width() > 0)
    page.consume_events([frame(clock, 250_000_000, b"0.96,328.00")])
    page.set_view_range(0.0, 1.0, y_min=0.0, y_max=400.0)

    move_mouse(page, viewport_point_of(page, 0.25, 0.96))

    assert page.hover_label.isVisible(), "靠近真实小数原始点必须显示读值"
    text = page.hover_label.text()
    assert "T+000.250 s" in text
    assert "CH1=0.96" in text
    assert "CH2=328" in text
    assert "328.0" not in text, "整数值不得回退为 328.0 形式的读值"


def test_chart_series_receives_decimal_values_without_truncation(qtbot):
    page, clock = make_page(qtbot)

    page.consume_events([frame(clock, 250_000_000, b"0.96,328.00")])

    assert page.ch1_series.count() == 1
    point = page.ch1_series.at(0)
    assert point.x() == pytest.approx(0.25)
    assert point.y() == pytest.approx(0.96)
    assert page.session.channel_segments(1) == [[(0.25, 328.0)]]


def test_fit_y_range_keeps_fractional_extent_and_widens_constants_by_one():
    assert fit_y_range([[[(0.0, -0.13), (1.0, 0.76)]]], 0.0, 1.0) == pytest.approx(
        (-0.13, 0.76)
    )
    assert fit_y_range([[[(0.0, 328.5)]]], 0.0, 1.0) == pytest.approx((327.5, 329.5))


def test_decimal_samples_obey_180_second_eviction():
    clock = StepClock()
    session = make_session(clock)
    session.consume(frame(clock, 20_000_000_000, b"0.96,328.00"))
    session.consume(frame(clock, 200_000_000_000, b"0.76,329.00"))
    assert session.sample_count == 2, "恰好 180 秒的点必须保留"

    session.consume(frame(clock, 200_000_000_001, b"-0.13,330.00"))

    assert [sample.values for sample in session.samples] == [
        (0.76, 329.0),
        (-0.13, 330.0),
    ]
    assert [sample.relative_seconds for sample in session.samples] == [
        200.0,
        200_000_000_001 / 1_000_000_000,
    ], "窗口边界淘汰不得受 binary64 存储影响"
    assert session.channel_latest_value(0) == -0.13
    assert session.channel_latest_value(1) == 330.0


def test_mixed_integer_and_decimal_fields_share_one_binary64_frame():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, 250_000_000, b"12,0.5,-7"))

    assert session.samples == [OscilloscopeSample(0.25, (12.0, 0.5, -7.0))]
    assert session.channel_latest_value(0) == 12
    assert session.channel_latest_value(2) == -7
    assert type(session.channel_latest_value(0)) is float, (
        "整数拼写也必须落在 binary64 存储而不是回归整数数组"
    )


def test_short_decimal_frame_keeps_gap_segmentation():
    clock = StepClock()
    session = make_session(clock)

    session.consume(frame(clock, 250_000_000, b"0.96,328.00"))
    session.consume(frame(clock, 500_000_000, b"0.76"))
    session.consume(frame(clock, 750_000_000, b"-0.13,330.00"))

    assert session.channel_segments(0) == [
        [(0.25, 0.96), (0.5, 0.76), (0.75, -0.13)]
    ]
    assert session.channel_segments(1) == [
        [(0.25, 328.0)],
        [(0.75, 330.0)],
    ], "小数帧的缺失字段必须在缺口两侧分段，不能连线"
    assert session.channel_segment_count(1) == 2
    assert session.channel_latest_value(1) == 330.0


def test_decimal_storage_failure_rejects_frame_and_keeps_existing_window():
    clock = StepClock()
    session = OscilloscopeSession(
        monotonic_ns=clock.mono, sample_store=FailingValuesStore(fail_on_append=2)
    )
    session.begin_acquisition()
    session.consume(frame(clock, 0, b"0.96"))
    before_samples = session.samples
    before_segments = session.channel_segments(0)

    with pytest.raises(MemoryError):
        session.consume(frame(clock, 250_000_000, b"1.5,2.5"))

    assert session.samples == before_samples, "失败的小数帧不得提交采样"
    assert session.channel_segments(0) == before_segments
    assert session.channel_count == 1
    assert session.channel_latest_value(0) == 0.96
    assert session.channel_latest_value(1) is None


def test_page_hover_readout_keeps_negative_decimal_sign(qtbot):
    page, clock = make_page(qtbot)
    page.resize(800, 500)
    page.show()
    qtbot.waitUntil(lambda: page.chart.plotArea().width() > 0)
    page.consume_events(
        [
            frame(clock, 250_000_000, b"0.96,328.00"),
            frame(clock, 500_000_000, b"0.76,329.00"),
            frame(clock, 750_000_000, b"-0.13,330.00"),
        ]
    )
    page.set_view_range(0.0, 1.0, y_min=-1.0, y_max=400.0)

    move_mouse(page, viewport_point_of(page, 0.75, -0.13))

    assert page.hover_label.isVisible()
    text = page.hover_label.text()
    assert "T+000.750 s" in text
    assert "CH1=-0.13" in text and "CH2=330" in text
