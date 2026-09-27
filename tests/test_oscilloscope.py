"""示波器数据模型（REQ-0005 issue 012）：Qt 无关的帧记录与单通道采样。

本文件只通过 ``paimon_assistant.oscilloscope`` 的公开接口验证行为：
完整帧在帧边界携带单调时间与墙上时间；相对时间由采集原点计算；
单通道整数帧产生采样，多通道合法载荷由 issue 013 协议测试覆盖，
完整非法载荷安全地解析失败。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.oscilloscope import (  # noqa: E402
    OscilloscopeSample,
    OscilloscopeSession,
    format_frame_line,
)


@dataclass(frozen=True)
class BoundaryEvent:
    """测试替身：一条完整帧在接收线程边界记录的双时间戳。"""

    monotonic_ns: int
    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class StepClock:
    """可推进的注入式时钟，单调时间与墙上时间一起推进。"""

    def __init__(self, monotonic_ns: int, received_at_ms: int) -> None:
        self.monotonic_ns = monotonic_ns
        self.received_at_ms = received_at_ms

    def mono(self) -> int:
        return self.monotonic_ns


def make_session(clock: StepClock) -> OscilloscopeSession:
    return OscilloscopeSession(monotonic_ns=clock.mono)


# --------------------------------------------------- 帧边界与相对时间


def test_valid_single_integer_frame_produces_one_sample_at_boundary_time():
    clock = StepClock(1_000_000_000, 1_700_000_000_000)
    session = make_session(clock)
    session.begin_acquisition()

    clock.monotonic_ns = 1_250_000_000  # 帧边界：开始后 250 ms
    clock.received_at_ms += 250
    record = session.consume(
        BoundaryEvent(
            monotonic_ns=clock.monotonic_ns,
            received_at_ms=clock.received_at_ms,
            payload=b"12",
            raw_frame=b"12\r\n",
        )
    )

    assert record.relative_seconds == pytest.approx(0.250)
    assert record.payload == b"12"
    assert record.raw_frame == b"12\r\n"
    assert record.values == (12,)
    assert record.parse_ok is True
    assert session.samples == [OscilloscopeSample(0.250, (12,))]
    assert session.records == [record]


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"1 2",
        b"1\t2",
        b"1e5",
        b"0x12",
        "１２".encode(),
        b"abc",
        b"2147483648",
        b"-2147483649",
    ],
)
def test_payload_outside_tracer_grammar_is_parse_failure_without_sample(payload):
    clock = StepClock(1_000_000_000, 1_700_000_000_000)
    session = make_session(clock)
    session.begin_acquisition()

    record = session.consume(
        BoundaryEvent(clock.monotonic_ns, clock.received_at_ms, payload, payload + b"\r\n")
    )

    assert record.payload == payload
    assert record.parse_ok is False
    assert record.values is None
    assert session.samples == []
    assert session.records == [record]
