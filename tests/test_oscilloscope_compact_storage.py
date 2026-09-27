"""紧凑原始采样存储与追加原子性（REQ-0005 §7.2.5，issue 016 验收修正）。

只通过公开行为验证：真实 ``OscilloscopeSession`` 加注入单调时钟与边界事件
替身，衡量保留原始采样的实际内存开销，并在窄存储边界注入 MemoryError，
验证失败帧不提交、不淘汰旧窗口。Qt 页面重渲与窗口语义由既有
retention/channels 测试覆盖。
"""

from __future__ import annotations

import sys
import tracemalloc
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant.oscilloscope import (  # noqa: E402
    CompactSampleStore,
    OscilloscopeSession,
)
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402

ORIGIN_NS = 1_000_000_000


class FailingValuesStore(CompactSampleStore):
    """窄存储边界替身：第 N 次采样提交时抛 MemoryError。"""

    def __init__(self, fail_on_append: int) -> None:
        super().__init__()
        self.appends = 0
        self.fail_on_append = fail_on_append

    def _append_values(self, values) -> None:
        self.appends += 1
        if self.appends == self.fail_on_append:
            raise MemoryError("injected sample storage failure")
        super()._append_values(values)


def make_session(clock: StepClock, sample_store=None) -> OscilloscopeSession:
    session = OscilloscopeSession(monotonic_ns=clock.mono, sample_store=sample_store)
    session.begin_acquisition()
    return session


def frame_at_ns(
    session: OscilloscopeSession, clock: StepClock, relative_ns: int, payload: bytes
):
    """一条在采集原点后 ``relative_ns`` 纳秒完成边界识别的完整帧。"""
    clock.monotonic_ns = ORIGIN_NS + relative_ns
    return session.consume(
        BoundaryEvent(clock.monotonic_ns, 1_700_000_000_000, payload, payload + b"\r\n")
    )


def eight_channel_payload(index: int) -> bytes:
    return ",".join(str(index + offset) for offset in range(8)).encode()


def test_retained_eight_channel_samples_use_compact_numeric_backing():
    """20k 个 8 通道原始点应接近 8+1+4×8 字节/点，而不是逐点对象加每通道元组。"""
    clock = StepClock()
    session = make_session(clock)

    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        point_count = 20_000
        for index in range(point_count):
            frame_at_ns(session, clock, index * 1_000_000, eight_channel_payload(index))
        # 用非法完整帧推进数据区窗口并淘汰全部帧记录，只测量采样存储本身。
        frame_at_ns(session, clock, 300_000_000_000, b"bad")
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert session.sample_count == point_count
    assert len(session.records) == 1, "帧记录应已随 180 秒数据区窗口淘汰"
    bytes_per_point = (current - baseline) / point_count
    assert bytes_per_point < 150, (
        f"保留的原始采样每点实测 {bytes_per_point:.1f} 字节，"
        "8 通道紧凑数值存储应远低于 150 字节/点"
    )


def test_storage_memory_error_rejects_frame_without_evicting_existing_window():
    """采样提交中途 MemoryError：本帧不提交，旧采样/记录/通道元数据原样保留。"""
    clock = StepClock()
    session = make_session(clock, sample_store=FailingValuesStore(fail_on_append=2))

    frame_at_ns(session, clock, 0, b"1")
    before_samples = session.samples
    before_records = session.records
    before_latest = session.channel_latest_value(0)
    before_segments = session.channel_segments(0)
    before_segment_count = session.channel_segment_count(0)

    # 若成功，这条 200 秒宽帧会淘汰 0 秒旧点并建立 8 个通道；失败则都不得发生。
    with pytest.raises(MemoryError):
        frame_at_ns(session, clock, 200_000_000_000, b"1,2,3,4,5,6,7,8")

    assert session.samples == before_samples, "失败帧不得提交采样"
    assert session.records == before_records, "失败帧不得提交完整帧记录"
    assert session.channel_count == 1, "失败帧不得建立通道"
    assert session.channel_latest_value(0) == before_latest
    assert session.channel_latest_value(1) is None
    assert session.channel_segments(0) == before_segments, "失败帧不得淘汰旧采样"
    assert session.channel_segment_count(0) == before_segment_count

    # 失败后存储仍可用：下一条合法帧正常提交并按新时间推进两个窗口。
    frame_at_ns(session, clock, 200_500_000_000, b"7,8")

    assert [sample.values for sample in session.samples] == [(7, 8)]
    assert [sample.relative_seconds for sample in session.samples] == pytest.approx(
        [200.5]
    ), "恢复后的新采样淘汰 0 秒旧点，且失败帧不残留"
    assert [record.relative_seconds for record in session.records] == pytest.approx(
        [200.5]
    ), "失败帧的记录不得残留，数据区窗口按新完整帧推进"
    assert session.channel_count == 2
    assert session.channel_latest_value(0) == 7
    assert session.channel_latest_value(1) == 8
    assert session.channel_segments(0) == [[(200.5, 7)]]
    assert session.channel_segments(1) == [[(200.5, 8)]]
    assert session.channel_segment_count(0) == 1
    assert session.channel_segment_count(1) == 1


def test_eight_channel_high_rate_variable_widths_and_gaps_reconstruct_exactly():
    """240 秒 1 kHz 变宽帧：窗口内全量保留，缺口分段与逐帧原始值逐点一致。"""
    clock = StepClock()
    session = make_session(clock)
    last_index = 240_000
    first_retained = last_index - 180_000  # 最新点向前恰好 180 秒内的全部点
    expected_retained = []
    for index in range(last_index + 1):
        width = index % 8 + 1
        values = tuple(index + offset for offset in range(width))
        frame_at_ns(
            session, clock, index * 1_000_000, ",".join(map(str, values)).encode()
        )
        if index >= first_retained:
            expected_retained.append((index / 1000.0, width, values))

    assert session.sample_count == 180_001, "180 秒内 1 kHz 原始点不得按固定点数截断"
    assert session.channel_count == 8, "变宽帧必须发现全部 8 个通道"

    samples = session.samples
    assert samples[0].relative_seconds == pytest.approx(60.0)
    assert samples[-1].relative_seconds == pytest.approx(240.0)
    assert samples[0].values == expected_retained[0][2]
    assert samples[-1].values == expected_retained[-1][2]

    for channel in range(8):
        expected_segments = []
        current = None
        latest = None
        for time, width, values in expected_retained:
            if channel < width:
                if current is None:
                    current = []
                    expected_segments.append(current)
                current.append((time, values[channel]))
                latest = values[channel]
            else:
                current = None
        assert session.channel_segment_count(channel) == len(expected_segments)
        actual_segments = session.channel_segments(channel)
        assert [len(segment) for segment in actual_segments] == [
            len(segment) for segment in expected_segments
        ], "缺口分段边界必须与逐帧字段存在性完全一致"
        assert [
            point for segment in actual_segments for point in segment
        ] == pytest.approx(
            [point for segment in expected_segments for point in segment]
        )
        assert session.channel_latest_value(channel) == latest


def test_exact_180_second_boundary_and_invalid_frame_anchor_with_eight_channels():
    """距最新合法采样恰好 180 秒的点保留；多 1 纳秒淘汰；非法帧不推进采样窗口。"""
    clock = StepClock()
    session = make_session(clock)
    wide = b"1,2,3,4,5,6,7,8"

    frame_at_ns(session, clock, 20_000_000_000, wide)
    frame_at_ns(session, clock, 200_000_000_000, wide)
    assert session.sample_count == 2, "恰好 180 秒的点必须保留"

    frame_at_ns(session, clock, 200_000_000_001, wide)
    assert [sample.relative_seconds for sample in session.samples] == [
        200.0,
        200_000_000_001 / 1_000_000_000,
    ], "比最新合法采样早 180 秒零 1 纳秒的点必须淘汰"
    assert session.channel_latest_value(7) == 8, "淘汰旧点不得改变通道最新值"

    frame_at_ns(session, clock, 400_000_000_000, b"bad")
    assert session.sample_count == 2, "非法完整帧不得推进采样窗口"
    assert [record.payload for record in session.records] == [
        b"bad"
    ], "非法完整帧推进数据区窗口，旧帧记录淘汰"
    assert session.channel_segments(0) == [
        [(200.0, 1), (200_000_000_001 / 1_000_000_000, 1)]
    ], "保留原始点的时间与值在窗口边界处不变"


def test_long_run_physically_discards_evicted_prefix():
    """500 秒 32 Hz 采集：窗口外点不仅逻辑淘汰，物理数组也不随累计追加无界增长。"""
    clock = StepClock()
    store = CompactSampleStore()
    session = make_session(clock, sample_store=store)
    period_ns = 31_250_000  # 1/32 秒，时间点在二进制下精确
    total = 16_000
    for index in range(total + 1):
        frame_at_ns(session, clock, index * period_ns, str(index).encode())

    assert session.sample_count == 5_761, "保留最新点向前 180 秒内的全部点"
    samples = session.samples
    assert samples[0].values == (10_240,), "窗口左边界应为最新点向前 180 秒的点"
    assert samples[-1].values == (16_000,)

    # 摊还前缀压缩的物理上界：start < max(1024, count) 时才不压缩。
    assert len(store.times) <= 2 * session.sample_count + 1024, (
        "已淘汰前缀必须被物理丢弃，不能永久滞留在数值数组中"
    )
    assert len(store.widths) == len(store.times)
    assert len(store.values) <= len(store.times)
