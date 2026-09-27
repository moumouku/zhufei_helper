"""示波器整数协议纯单元测试（REQ-0005 issue 013）。

只通过 ``paimon_assistant.oscilloscope`` 的公开接口验证行为：
``parse_frame_payload`` 严格接受 1～8 个 ASCII 半角逗号分隔的有符号十进制
值（含小数拼写），每个值必须落于 int32 范围内；合法完整帧经
``OscilloscopeSession`` 恰好产生一个同时间的采样集合，非法完整帧保留载荷
但不产生采样。
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
    parse_frame_payload,
)


def test_comma_separated_values_parse_in_field_order():
    assert parse_frame_payload(b"12,34") == (12, 34)
    assert parse_frame_payload(b"-10,+20,0030") == (-10, 20, 30)


def test_channel_count_is_limited_to_eight():
    assert parse_frame_payload(b"1,2,3,4,5,6,7,8") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert parse_frame_payload(b"1,2,3,4,5,6,7,8,9") is None


def test_int32_endpoints_are_legal_and_first_out_of_range_values_fail():
    assert parse_frame_payload(b"2147483647") == (2147483647,)
    assert parse_frame_payload(b"-2147483648") == (-2147483648,)
    assert parse_frame_payload(b"2147483648") is None
    assert parse_frame_payload(b"-2147483649") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"1,,2",
        b",1",
        b"1,",
        b" ",
        b" 1",
        b"1 ",
        b"1 2",
        b"1\t2",
        b"1e5",
        b"0x12",
        "１２".encode(),
        b"abc",
        b"1;2",
        b"1\r2\n3",
        b"+",
        b"-",
        b"2147483648",
        b"-2147483649",
        b"0002147483648",
        b"-0002147483649",
        b"1,2,3,4,5,6,7,8,9",
    ],
)
def test_payload_outside_grammar_is_parse_failure(payload):
    assert parse_frame_payload(payload) is None


def test_leading_zeros_up_to_one_mib_parse_by_value_semantics():
    # 1 MiB 恰好是分帧上限；前导零数量不受 int() 字符串位数限制影响。
    assert parse_frame_payload(b"0" * 1_048_575 + b"1") == (1,)
    assert parse_frame_payload(b"-" + b"0" * 100_000 + b"42") == (-42,)
    assert parse_frame_payload(b"0" * 500_000 + b"," + b"0" * 500_000) == (0, 0)
    assert parse_frame_payload(b"+0002147483647") == (2147483647,)
    assert parse_frame_payload(b"-0002147483648") == (-2147483648,)
    # 前导零再多也不能把溢出的数值变回合法。
    assert parse_frame_payload(b"0" * 5_000 + b"2147483648") is None
    assert parse_frame_payload(b"-" + b"0" * 5_000 + b"2147483649") is None


@dataclass(frozen=True)
class BoundaryEvent:
    """测试替身：一条完整帧在接收线程边界记录的双时间戳。"""

    monotonic_ns: int
    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class StepClock:
    """可推进的注入式时钟。"""

    def __init__(self, monotonic_ns: int) -> None:
        self.monotonic_ns = monotonic_ns

    def mono(self) -> int:
        return self.monotonic_ns


def test_each_legal_frame_produces_exactly_one_sample_at_boundary_time():
    clock = StepClock(1_000_000_000)
    session = OscilloscopeSession(monotonic_ns=clock.mono)
    session.begin_acquisition()
    clock.monotonic_ns = 1_250_000_000

    eight = session.consume(
        BoundaryEvent(
            clock.monotonic_ns, 0, b"1,2,3,4,5,6,7,8", b"1,2,3,4,5,6,7,8\r\n"
        )
    )
    single = session.consume(BoundaryEvent(clock.monotonic_ns, 0, b"9", b"9\r\n"))

    assert eight.values == (1, 2, 3, 4, 5, 6, 7, 8)
    assert single.values == (9,)
    assert session.samples == [
        OscilloscopeSample(0.250, (1, 2, 3, 4, 5, 6, 7, 8)),
        OscilloscopeSample(0.250, (9,)),
    ]
