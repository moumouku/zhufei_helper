"""三分钟无损采样与完整帧历史（REQ-0005 issue 016）。

只通过公开行为验证：注入单调时钟与接收线程边界事件替身，覆盖 180 秒
窗口内全量原始采样保留、按最新合法采样/最新完整帧时间精确淘汰、非法帧
与连接边界对两个推进基准的影响、停止期间不淘汰，以及 Qt 页面从模型
记录渲染数据区、淘汰后重建绘图 series 和跟随最新交互。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant.oscilloscope import (  # noqa: E402
    OscilloscopeConnectionBoundary,
    OscilloscopeSession,
)
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from paimon_assistant.serial_controller import SerialSettings  # noqa: E402
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402

ORIGIN_NS = 1_000_000_000


def make_session(clock: StepClock) -> OscilloscopeSession:
    session = OscilloscopeSession(monotonic_ns=clock.mono)
    session.begin_acquisition()
    return session


def frame_at_ns(session: OscilloscopeSession, clock: StepClock, relative_ns: int, payload: bytes):
    """一条在采集原点后 ``relative_ns`` 纳秒完成边界识别的完整帧。"""
    clock.monotonic_ns = ORIGIN_NS + relative_ns
    return session.consume(
        BoundaryEvent(clock.monotonic_ns, 1_700_000_000_000, payload, payload + b"\r\n")
    )


def settings(port="COM7"):
    return SerialSettings(port=port)


def event_at_ns(relative_ns: int, payload: bytes) -> BoundaryEvent:
    """一条在采集原点后 ``relative_ns`` 纳秒完成边界识别的完整帧事件。"""
    return BoundaryEvent(
        ORIGIN_NS + relative_ns, 1_700_000_000_000, payload, payload + b"\r\n"
    )


def make_page(qtbot) -> tuple[OscilloscopePage, StepClock]:
    clock = StepClock()
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


# ------------------------------------------- 采样窗口：按最新合法采样推进


def test_latest_legal_sample_window_evicts_only_samples_older_than_180_seconds():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 10_000_000_000, b"1")
    frame_at_ns(session, clock, 189_900_000_000, b"2")
    assert [sample.relative_seconds for sample in session.samples] == pytest.approx(
        [10.0, 189.9]
    ), "最新点的 180 秒内样本都必须保留"

    frame_at_ns(session, clock, 200_000_000_000, b"3")
    assert [sample.relative_seconds for sample in session.samples] == pytest.approx(
        [189.9, 200.0]
    ), "淘汰基准是新的最新合法采样时间，10 s 的样本已超出 180 秒窗口"


def test_exact_180_second_boundary_is_retained_and_one_nanosecond_older_evicted():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 20_000_000_000, b"1")
    frame_at_ns(session, clock, 200_000_000_000, b"2")
    assert [sample.relative_seconds for sample in session.samples] == pytest.approx(
        [20.0, 200.0]
    ), "距最新合法采样恰好 180 秒的样本仍在窗口内"

    frame_at_ns(session, clock, 200_000_000_001, b"3")
    assert [sample.relative_seconds for sample in session.samples] == pytest.approx(
        [200.0, 200.000001]
    ), "比最新合法采样早 180 秒零 1 纳秒的样本必须淘汰"


def test_high_rate_full_window_keeps_every_sample_without_point_cap_or_downsampling():
    clock = StepClock()
    session = make_session(clock)
    period_ns = 1_000_000  # 1 kHz
    last_index = 240_000  # 240 秒，后 60 秒持续淘汰旧点
    for index in range(last_index + 1):
        frame_at_ns(session, clock, index * period_ns, str(index).encode())

    assert session.sample_count == 180_001, "180 秒内 1 kHz 的 180001 个原始点必须全量保留"
    samples = session.samples
    assert samples[0].relative_seconds == pytest.approx(60.0)
    assert samples[-1].relative_seconds == pytest.approx(240.0)
    assert samples[0].values == (60_000,)
    assert samples[-1].values == (240_000,)
    steps = {
        round(later.relative_seconds - earlier.relative_seconds, 9)
        for earlier, later in zip(samples, samples[1:])
    }
    assert steps == {0.001}, "窗口内不得按固定点数上限或采样率降采样删除原始点"


# ------------------------------ 两个推进基准：合法采样 vs. 完整帧


def test_invalid_complete_frame_does_not_advance_sample_eviction_anchor():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1")
    frame_at_ns(session, clock, 240_000_000_000, b"bad")

    assert [sample.values for sample in session.samples] == [
        (1,)
    ], "非法完整帧不能单独把采样窗口推进到 240 秒"


def test_invalid_complete_frame_still_advances_record_window():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1")
    frame_at_ns(session, clock, 240_000_000_000, b"bad")

    assert [record.payload for record in session.records] == [
        b"bad"
    ], "数据区窗口以最新完整帧（含非法）推进，旧的合法帧记录也应淘汰"


def test_connection_boundary_does_not_advance_record_window():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1")
    session.note_connection_boundary(settings(), at_ns=ORIGIN_NS + 240_000_000_000)

    records = session.records
    assert [record.payload for record in records[:1]] == [b"1"], (
        "连接边界不是完整帧，不得推进数据区 180 秒窗口"
    )
    assert isinstance(records[1], OscilloscopeConnectionBoundary)


def test_connection_boundary_records_are_evicted_consistently_by_next_frame():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1")
    session.note_connection_boundary(settings(), at_ns=ORIGIN_NS + 240_000_000_000)
    frame_at_ns(session, clock, 241_000_000_000, b"2")

    records = session.records
    assert len(records) == 2
    assert isinstance(records[0], OscilloscopeConnectionBoundary)
    assert records[1].payload == b"2", "窗口内的连接边界与后续帧一致保留，超窗旧帧淘汰"


# --------------------------------------- 淘汰后的通道、最新值与缺口语义


def test_channel_count_and_latest_values_survive_full_sample_eviction():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1,2")
    frame_at_ns(session, clock, 250_000_000_000, b"3")

    assert session.channel_count == 2, "淘汰不得降低已发现的通道数"
    assert session.channel_latest_value(0) == 3
    assert session.channel_latest_value(1) == 2, "陈点被淘汰后 CH2 最新有效值仍保留"
    assert session.channel_segments(0) == [[(250.0, 3)]]
    assert session.channel_segments(1) == [], "CH2 的保留点已全部淘汰"

    frame_at_ns(session, clock, 300_000_000_000, b"4,5")

    assert session.channel_count == 2
    assert session.channel_latest_value(1) == 5
    assert session.channel_segments(1) == [
        [(300.0, 5)]
    ], "全部分段被淘汰后的新点必须重新开段，不能接到已删点"


def test_partial_segment_eviction_keeps_remaining_points_in_order():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1,2")
    frame_at_ns(session, clock, 100_000_000_000, b"3,4")
    frame_at_ns(session, clock, 278_000_000_000, b"5,6")

    assert session.channel_segments(0) == [[(100.0, 3), (278.0, 5)]], (
        "同一分段的部分旧点淘汰后，剩余点保持时间顺序且不丢失"
    )
    assert session.channel_segments(1) == [[(100.0, 4), (278.0, 6)]]


def test_gap_segmentation_is_preserved_after_prefix_eviction():
    clock = StepClock()
    session = make_session(clock)

    frame_at_ns(session, clock, 0, b"1,2")
    frame_at_ns(session, clock, 110_000_000_000, b"7")  # CH2 缺口
    frame_at_ns(session, clock, 250_000_000_000, b"8,9")

    assert session.channel_segments(0) == [[(110.0, 7), (250.0, 8)]], (
        "淘汰前缀点不得在 CH1 连续采样之间制造假缺口"
    )
    assert session.channel_segments(1) == [
        [(250.0, 9)]
    ], "短帧缺口与淘汰后重新出现的 CH2 必须从新分段开始"


# ----------------------------------------------- 页面数据区：从模型重渲


def test_data_area_renders_retained_records_and_drops_evicted_lines(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [
            event_at_ns(0, b"1"),
            event_at_ns(10_000_000_000, b"2"),
            event_at_ns(250_000_000_000, b"bad"),
        ]
    )

    records = page.session.records
    assert [record.payload for record in records] == [b"bad"], (
        "250 s 的非法完整帧推进数据区窗口，0 s 和 10 s 的记录已超出 180 秒"
    )
    text = page.display_edit.toPlainText()
    assert "T+250.000 s" in text
    assert "T+000.000 s" not in text, "已淘汰记录不得继续显示在数据区"
    assert "T+010.000 s" not in text
    assert text.count("解析失败") == 1


def test_page_invalid_frame_advances_display_window_but_not_sample_window(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events([event_at_ns(0, b"1")])
    page.consume_events([event_at_ns(240_000_000_000, b"bad")])

    text = page.display_edit.toPlainText()
    assert "T+240.000 s" in text and "解析失败" in text
    assert "T+000.000 s" not in text, "非法完整帧推进数据区 180 秒窗口"
    assert [sample.values for sample in page.session.samples] == [(1,)], (
        "非法帧不得推进采样窗口"
    )
    assert page.ch1_series.count() == 1, "采样窗口未淘汰，图表仍保留 0 s 原始点"


def test_chart_series_rebuilt_from_model_when_samples_are_evicted(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [
            event_at_ns(0, b"1,2"),
            event_at_ns(100_000_000_000, b"3,4"),
        ]
    )
    assert page.channel_series[0][0].count() == 2

    page.consume_events([event_at_ns(280_000_000_000, b"5,6")])

    assert [sample.relative_seconds for sample in page.session.samples] == [
        100.0,
        280.0,
    ]
    for index in (0, 1):
        segments = page.channel_series[index]
        assert len(segments) == 1, "淘汰后重建的绘图分段必须与模型一致"
        points = [
            (segments[0].at(i).x(), segments[0].at(i).y())
            for i in range(segments[0].count())
        ]
        model_points = [
            point
            for segment in page.session.channel_segments(index)
            for point in segment
        ]
        assert points == pytest.approx(model_points), "图表不得保留已淘汰的旧点"
    assert page.channel_labels["CH1"].text() == "CH1  5"
    assert page.channel_labels["CH2"].text() == "CH2  6"


# ----------------------------------------------- 数据区跟随最新与阅读位置


def _fill_page(page: OscilloscopePage, qtbot, count: int = 200) -> None:
    """喂入足够多行使数据区必须滚动；时间均在 180 秒窗口内。"""
    page.resize(400, 200)
    page.show()
    page.consume_events(
        [
            event_at_ns(index * 100_000_000, str(index).encode())
            for index in range(count)
        ]
    )
    qtbot.waitUntil(lambda: page.display_edit.verticalScrollBar().maximum() > 0)


def test_data_area_defaults_to_following_latest_with_visible_button(qtbot):
    page, _clock = make_page(qtbot)
    assert page.follow_button.isChecked()
    assert page.follow_button.text() == "跟随最新"
    assert page.follow_button.isVisibleTo(page) or page.isVisible()

    _fill_page(page, qtbot)

    scrollbar = page.display_edit.verticalScrollBar()
    assert scrollbar.value() == scrollbar.maximum(), "默认必须显示最新帧"


def test_user_scroll_pauses_follow_and_new_frames_do_not_move_reading_position(qtbot):
    page, _clock = make_page(qtbot)
    _fill_page(page, qtbot)
    scrollbar = page.display_edit.verticalScrollBar()
    paused_value = max(0, scrollbar.maximum() - 3)

    scrollbar.setSliderPosition(paused_value)  # 模拟用户拖动滚动条

    qtbot.waitUntil(lambda: not page.follow_button.isChecked())
    assert page.follow_button.text() == "回到最新"

    page.consume_events([event_at_ns(50_000_000_000, b"tail-line")])

    assert "tail-line" in page.display_edit.toPlainText()
    assert not page.follow_button.isChecked()
    assert scrollbar.value() == paused_value, "查看旧记录时新帧不得强制移动阅读位置"


def test_return_to_latest_restores_follow_for_subsequent_frames(qtbot):
    page, _clock = make_page(qtbot)
    _fill_page(page, qtbot)
    scrollbar = page.display_edit.verticalScrollBar()
    scrollbar.setSliderPosition(max(0, scrollbar.maximum() - 3))
    qtbot.waitUntil(lambda: not page.follow_button.isChecked())

    page.follow_button.click()

    assert page.follow_button.isChecked()
    assert page.follow_button.text() == "跟随最新"
    assert scrollbar.value() == scrollbar.maximum()

    page.consume_events([event_at_ns(60_000_000_000, b"latest-line")])

    qtbot.waitUntil(lambda: scrollbar.value() == scrollbar.maximum())
    assert "latest-line" in page.display_edit.toPlainText()


def test_stop_does_not_evict_history_and_resume_advances_both_windows(qtbot):
    page, clock = make_page(qtbot)
    page.consume_events(
        [
            event_at_ns(0, b"1,2"),
            event_at_ns(10_000_000_000, b"3,4"),
        ]
    )
    before_samples = page.session.samples
    before_records = page.session.records
    before_text = page.display_edit.toPlainText()

    page.end_acquisition()
    clock.monotonic_ns = ORIGIN_NS + 10_000_000_000_000  # 停止后电脑时间前进 10000 秒

    assert page.session.samples == before_samples, "停止期间无新输入，采样不得因时钟前进淘汰"
    assert page.session.records == before_records
    assert page.display_edit.toPlainText() == before_text

    page.begin_acquisition()  # 恢复继续同一采集会话
    page.consume_events([event_at_ns(300_000_000_000, b"5,6")])

    assert [sample.relative_seconds for sample in page.session.samples] == [
        300.0
    ], "恢复后由新合法采样推进采样窗口"
    assert [record.relative_seconds for record in page.session.records] == [
        300.0
    ], "恢复后由新完整帧推进数据区窗口"


def test_clear_restores_follow_and_empties_model_and_display(qtbot):
    page, _clock = make_page(qtbot)
    _fill_page(page, qtbot)
    scrollbar = page.display_edit.verticalScrollBar()
    scrollbar.setSliderPosition(max(0, scrollbar.maximum() - 3))
    qtbot.waitUntil(lambda: not page.follow_button.isChecked())

    page.clear_acquisition()

    assert page.follow_button.isChecked()
    assert page.follow_button.text() == "跟随最新"
    assert page.session.records == []
    assert page.session.samples == []
    assert page.display_edit.toPlainText() == ""


# ------------------------------------------- 原始存储独立于绘制简化


def test_raw_samples_keep_original_values_after_chart_rebuild(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events(
        [
            event_at_ns(0, b"-2147483648,2147483647"),
            event_at_ns(100_000_000_000, b"30,40"),
        ]
    )
    before = list(page.session.samples)

    page.consume_events([event_at_ns(250_000_000_000, b"5,6")])  # 触发绘图重建

    remaining = page.session.samples
    assert remaining, "窗口内仍应有采样点"
    old_remaining = [sample for sample in remaining if sample.relative_seconds != 250.0]
    assert old_remaining == [sample for sample in before if sample.relative_seconds >= 70.0], (
        "绘图 series 重建不得改写已保留采样点的原始值或时间"
    )
    for index in (0, 1):
        model_points = [
            point
            for segment in page.session.channel_segments(index)
            for point in segment
        ]
        series_points = [
            (series.at(i).x(), series.at(i).y())
            for series in page.channel_series[index]
            for i in range(series.count())
        ]
        assert series_points == pytest.approx(model_points)


def test_channel_segments_returns_copies_not_raw_storage():
    clock = StepClock()
    session = make_session(clock)
    frame_at_ns(session, clock, 0, b"1,2")

    segments = session.channel_segments(0)
    segments[0].append((999.0, -1))

    assert session.channel_segments(0) == [[(0.0, 1)]], (
        "调用方修改返回的分段不得反向修改原始采样存储"
    )
