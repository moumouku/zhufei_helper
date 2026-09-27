"""Qt-free oscilloscope frame model for the issue 012 tracer slice.

The receive thread already recognizes the strict ``0D 0A`` boundary and stamps
each ``ReceivedEvent`` with both the local wall clock and a high-resolution
monotonic clock (``event.monotonic_ns``). This module owns the waveform page's
independent frame history and samples: it turns those complete frames into
records whose relative time is computed against the acquisition origin.

Issue 013 owns the payload grammar: a payload is valid exactly when it is one
or more signed decimal int32 values separated by ASCII commas. Anything else
is a complete but invalid frame: it is still recorded, displayed and logged,
but it produces no sample.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

from .config import SerialSettings

INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647

#: 1 to 8 signed decimal fields separated by ASCII commas, digits ``0``-``9`` only.
_COMMA_SEPARATED_INTEGERS = re.compile(rb"[+-]?[0-9]+(?:,[+-]?[0-9]+){0,7}")

#: ``|INT32_MIN|`` has 10 significant digits; a longer magnitude overflows.
_MAX_SIGNIFICANT_DIGITS = 10

#: REQ-0005 §5.1: at most 8 comma-separated fields per legal frame.
_MAX_CHANNELS = 8


def parse_frame_payload(payload: bytes) -> Optional[tuple[int, ...]]:
    """Parse one complete frame payload; ``None`` means "解析失败".

    Leading zeros are stripped before ``int()`` so up to 1 MiB of zero padding
    never trips CPython's integer-string digit limit; a magnitude longer than
    10 significant digits is rejected as overflow, then the value is checked
    against the signed 32-bit range.
    """
    if _COMMA_SEPARATED_INTEGERS.fullmatch(payload) is None:
        return None
    values = []
    for field in payload.split(b","):
        sign = field[0:1]
        digits = field[1:] if sign in (b"+", b"-") else field
        digits = digits.lstrip(b"0")
        if len(digits) > _MAX_SIGNIFICANT_DIGITS:
            return None
        value = int(digits) if digits else 0
        if sign == b"-":
            value = -value
        if not INT32_MIN <= value <= INT32_MAX:
            return None
        values.append(value)
    return tuple(values)


@dataclass(frozen=True)
class OscilloscopeFrameRecord:
    """One complete frame shown in the waveform data area.

    ``values`` is ``None`` for a complete payload that fails the protocol;
    failed frames are still displayed and logged but never become samples.
    """

    relative_seconds: float
    payload: bytes
    raw_frame: bytes
    values: Optional[tuple[int, ...]]

    @property
    def parse_ok(self) -> bool:
        return self.values is not None


@dataclass(frozen=True)
class OscilloscopeSample:
    """The raw values of one legal frame at its shared relative time."""

    relative_seconds: float
    values: tuple[int, ...]


@dataclass(frozen=True)
class OscilloscopeConnectionBoundary:
    """A connection-parameter boundary inserted into the waveform data area.

    Written when receiving resumes after a stop and the port or any serial
    parameter differs from the previous active connection (REQ-0005 §11.3).
    History, channels and the acquisition origin are preserved.
    """

    relative_seconds: float
    settings: SerialSettings


class OscilloscopeSession:
    """Acquisition origin plus the waveform page's frame and sample history."""

    def __init__(self, monotonic_ns: Optional[Callable[[], int]] = None) -> None:
        self._monotonic_ns = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
        self._origin_ns: Optional[int] = None
        self._records: List[Union[OscilloscopeFrameRecord, OscilloscopeConnectionBoundary]] = []
        self._samples: List[OscilloscopeSample] = []
        self._channel_count = 0
        self._latest_values: List[int] = []
        #: 每个通道各分段的起始采样下标；缺口 = 两个相邻有效点之间存在短帧。
        self._segment_starts: List[List[int]] = [[] for _ in range(_MAX_CHANNELS)]
        self._last_sample_position: List[Optional[int]] = [None] * _MAX_CHANNELS

    @property
    def origin_ns(self) -> Optional[int]:
        return self._origin_ns

    @property
    def channel_count(self) -> int:
        """当前会话见过的最大合法字段数（0～8，只增不减）。"""
        return self._channel_count

    def channel_latest_value(self, index: int) -> Optional[int]:
        """通道 ``index``（0 起）最新一条实际提供该字段的有效值。

        短帧不更新缺失字段；非法帧完全不更新。还没出现过的字段返回 ``None``。
        """
        if 0 <= index < len(self._latest_values):
            return self._latest_values[index]
        return None

    def channel_segment_count(self, index: int) -> int:
        """通道 ``index``（0 起）当前的分段数。"""
        return len(self._segment_starts[index])

    def channel_segments(self, index: int) -> List[List[tuple[float, int]]]:
        """通道 ``index``（0 起）的缺口分段点序列。

        同一合法帧的所有字段共享同一时间；缺失字段不生成伪采样，而是在
        缺口两侧切分为不同分段，便于绘图时真正断线。
        """
        starts = self._segment_starts[index]
        segments: List[List[tuple[float, int]]] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(self._samples)
            segments.append(
                [
                    (sample.relative_seconds, sample.values[index])
                    for sample in self._samples[start:end]
                    if index < len(sample.values)
                ]
            )
        return segments

    @property
    def records(self) -> List[Union[OscilloscopeFrameRecord, OscilloscopeConnectionBoundary]]:
        return list(self._records)

    @property
    def samples(self) -> List[OscilloscopeSample]:
        return list(self._samples)

    def begin_acquisition(self, origin_ns: Optional[int] = None) -> None:
        """Capture ``T+0`` on the first successful start of the session.

        ``origin_ns`` is captured at the successful open boundary, before the
        reader can publish a frame, so the first frame's relative time cannot
        go negative (issue 014). Later starts continue the same acquisition,
        so the origin is only set once (REQ-0005 §6.2.1, §14.6).
        """
        if self._origin_ns is None:
            self._origin_ns = self._monotonic_ns() if origin_ns is None else origin_ns

    def reset(self) -> None:
        """新建采集的最小重置：清空历史并释放时间原点。

        并发/线性化的清空语义由 issue 019 完整交付；本方法只保证页面清空
        不再调用不存在的接口，且不再保留旧采集的记录、采样或原点。
        """
        self._origin_ns = None
        self._records.clear()
        self._samples.clear()
        self._channel_count = 0
        self._latest_values = []
        self._segment_starts = [[] for _ in range(_MAX_CHANNELS)]
        self._last_sample_position = [None] * _MAX_CHANNELS

    def note_connection_boundary(
        self, settings: SerialSettings, at_ns: Optional[int] = None
    ) -> OscilloscopeConnectionBoundary:
        """Append one connection boundary at the current relative time.

        ``at_ns`` is the successful-open boundary candidate; the origin is
        preserved across stop/resume, so the boundary time includes the whole
        stopped interval (REQ-0005 §6.2.3, §11.3).
        """
        if self._origin_ns is None:
            raise RuntimeError("oscilloscope acquisition has not started")
        if at_ns is None:
            at_ns = self._monotonic_ns()
        record = OscilloscopeConnectionBoundary(
            relative_seconds=(at_ns - self._origin_ns) / 1_000_000_000,
            settings=settings,
        )
        self._records.append(record)
        return record

    def consume(self, event) -> OscilloscopeFrameRecord:
        """Turn one complete frame into a record, sampling it when legal."""
        if self._origin_ns is None:
            raise RuntimeError("oscilloscope acquisition has not started")
        monotonic_ns = getattr(event, "monotonic_ns", None)
        if monotonic_ns is None:
            raise ValueError(
                "frame has no boundary monotonic timestamp; "
                "refusing to substitute a later consumption time"
            )
        relative_seconds = (monotonic_ns - self._origin_ns) / 1_000_000_000
        values = parse_frame_payload(event.payload)
        record = OscilloscopeFrameRecord(
            relative_seconds=relative_seconds,
            payload=bytes(event.payload),
            raw_frame=bytes(event.raw_frame),
            values=values,
        )
        self._records.append(record)
        if values is not None:
            self._samples.append(OscilloscopeSample(relative_seconds, values))
            position = len(self._samples) - 1
            if len(values) > self._channel_count:
                self._channel_count = len(values)
                self._latest_values.extend([0] * (self._channel_count - len(self._latest_values)))
            for index, value in enumerate(values):
                if self._last_sample_position[index] != position - 1:
                    self._segment_starts[index].append(position)
                self._last_sample_position[index] = position
                self._latest_values[index] = value
        return record


def format_relative_seconds(seconds: float) -> str:
    """``T+012.345 s``; relative time never claims to be the MCU clock."""
    return f"T+{seconds:07.3f} s"


def format_payload(payload: bytes) -> str:
    """Display payload bytes without changing them or breaking the line."""
    text = "".join(
        chr(byte) if 0x20 <= byte <= 0x7E else f"\\x{byte:02X}" for byte in payload
    )
    return f'"{text}"'


def format_connection_boundary_line(record: OscilloscopeConnectionBoundary) -> str:
    """One data-area boundary line: relative time plus every serial setting."""
    settings = record.settings
    serial_format = f"{settings.data_bits}{settings.parity}{settings.stop_bits:g}"
    return (
        f"{format_relative_seconds(record.relative_seconds)}  "
        f"── 连接边界 {settings.port} · {settings.baudrate} · {serial_format}"
    )


def format_frame_line(record: OscilloscopeFrameRecord) -> str:
    """One data-area line: relative time, raw payload and parse status."""
    status = "解析成功" if record.parse_ok else "解析失败"
    return (
        f"{format_relative_seconds(record.relative_seconds)}  "
        f"payload={format_payload(record.payload)}  {status}"
    )
