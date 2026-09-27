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
from array import array
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Iterable, List, Optional, Union

from .config import SerialSettings

INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647

#: 1 to 8 signed decimal fields separated by ASCII commas, digits ``0``-``9`` only.
_COMMA_SEPARATED_INTEGERS = re.compile(rb"[+-]?[0-9]+(?:,[+-]?[0-9]+){0,7}")

#: ``|INT32_MIN|`` has 10 significant digits; a longer magnitude overflows.
_MAX_SIGNIFICANT_DIGITS = 10

#: REQ-0005 §5.1: at most 8 comma-separated fields per legal frame.
_MAX_CHANNELS = 8

#: REQ-0005 §7.2/§6.3.4: 原始采样与完整帧记录的滚动窗口长度（秒）。
RETENTION_SECONDS = 180.0

#: 物理丢弃已淘汰前缀的最小批量；摊还后每条采样拷贝成本为 O(1)。
_COMPACT_MIN_PREFIX = 1024


class CompactSampleStore:
    """原始采样点的紧凑数值存储（REQ-0005 §7.2.5）。

    每条合法采样共享一个相对时间，只保存该帧实际字段的 int32 值：

    - ``times``：每条采样的共同相对时间（``array('d')``，8 字节/点）；
    - ``widths``：该帧实际字段数 1～8（``array('B')``，1 字节/点）；
    - ``values``：按帧顺序拉平的实际字段值（``array('i')``，4 字节/值）；
    - ``offsets``：每条采样在 ``values`` 中的起始下标（``array('I')``，4 字节/点），
      使按时间二分的区间查询无需从头累计前面所有帧的宽度。

    缺口由 ``widths`` 与拉平的值直接重建，不再保存每通道逐点元组，也不再
    保存逐点样本对象。淘汰只推进起始偏移，按摊还批量物理丢弃前缀，因此
    逐点追加与淘汰都不会重建全部保留数组。

    本类同时是测试注入存储分配失败的窄边界：覆盖 ``_append_values`` 或替换
    其中一个数值数组，即可在一次采样提交的中途抛出 ``MemoryError``，验证
    会话不提交失败帧、也不淘汰旧窗口。
    """

    def __init__(self) -> None:
        self.times = array("d")
        self.widths = array("B")
        self.values = array("i")
        self.offsets = array("I")
        #: 物理数组中第一条保留采样的下标；``base`` 是它的绝对序号。
        self.start = 0
        self.values_start = 0
        self.base = 0

    @property
    def count(self) -> int:
        """保留采样点数；不随数组长度增长，O(1)。"""
        return len(self.times) - self.start

    @property
    def oldest_time(self) -> float:
        return self.times[self.start]

    @property
    def oldest_width(self) -> int:
        return self.widths[self.start]

    def width_at(self, position: int) -> int:
        """相对第一条保留采样 ``position`` 处的实际字段数。"""
        return self.widths[self.start + position]

    def iter_samples(self) -> Iterable[tuple[float, tuple[int, ...]]]:
        """按时间顺序产出 ``(相对时间, 该帧实际值元组)``，不保留元组。"""
        start = self.start
        running = self.values_start
        count = self.count
        for position in range(count):
            width = self.widths[start + position]
            yield self.times[start + position], tuple(
                self.values[running : running + width]
            )
            running += width

    def iter_samples_in_range(
        self, x_min: float, x_max: float
    ) -> Iterable[tuple[float, tuple[int, ...]]]:
        """产出相对时间落在闭区间 ``[x_min, x_max]`` 的保留采样。

        时间数组按追加顺序单调；二分定位第一条不早于 ``x_min`` 的采样，
        再顺序读到超过 ``x_max``。每帧的值区间由 ``offsets`` 直接定位，
        不扫描区间之前的采样，也不构造全量样本列表。
        """
        start = self.start
        count = self.count
        if count == 0 or x_max < x_min:
            return
        first = bisect_left(self.times, x_min, start, start + count)
        last = bisect_right(self.times, x_max, first, start + count)
        for position in range(first, last):
            offset = self.offsets[position]
            width = self.widths[position]
            yield self.times[position], tuple(self.values[offset : offset + width])

    def iter_channel_points(self, index: int) -> Iterable[tuple[int, float, int]]:
        """产出实际提供通道 ``index`` 的 ``(位置, 相对时间, 值)``。

        单次扫描平铺值数组，绘图按位置是否连续把缺口切成分段。
        """
        start = self.start
        running = self.values_start
        count = self.count
        for position in range(count):
            width = self.widths[start + position]
            if index < width:
                yield position, self.times[start + position], self.values[running + index]
            running += width

    def append(self, relative_seconds: float, values: tuple[int, ...]) -> None:
        """提交一条采样；任何一步分配失败都回滚本次追加，保留旧数据。"""
        times_len = len(self.times)
        widths_len = len(self.widths)
        values_len = len(self.values)
        offsets_len = len(self.offsets)
        try:
            self.times.append(relative_seconds)
            self.widths.append(len(values))
            self.offsets.append(values_len)
            self._append_values(values)
        except BaseException:
            del self.times[times_len:]
            del self.widths[widths_len:]
            del self.offsets[offsets_len:]
            del self.values[values_len:]
            raise

    def rollback_last(self) -> None:
        """撤销最近一次成功 ``append``（用于后续帧记录追加失败时）。"""
        width = self.widths[-1]
        del self.times[-1:]
        del self.widths[-1:]
        del self.offsets[-1:]
        del self.values[len(self.values) - width :]

    def evict_oldest(self) -> None:
        """淘汰第一条保留采样，必要时摊还物理丢弃已淘汰前缀。"""
        self.values_start += self.widths[self.start]
        self.start += 1
        self.base += 1
        if self.start >= _COMPACT_MIN_PREFIX and self.start >= self.count:
            removed_values = self.values_start
            del self.times[: self.start]
            del self.widths[: self.start]
            del self.offsets[: self.start]
            del self.values[:removed_values]
            # 偏移量是扁平数组的绝对下标：物理删除前缀后整体前移。
            for index in range(len(self.offsets)):
                self.offsets[index] -= removed_values
            self.start = 0
            self.values_start = 0

    def clear(self) -> None:
        del self.times[:]
        del self.widths[:]
        del self.values[:]
        del self.offsets[:]
        self.start = 0
        self.values_start = 0
        self.base = 0

    def _append_values(self, values: tuple[int, ...]) -> None:
        self.values.extend(values)


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

    def __init__(
        self,
        monotonic_ns: Optional[Callable[[], int]] = None,
        sample_store: Optional[CompactSampleStore] = None,
    ) -> None:
        self._monotonic_ns = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
        self._origin_ns: Optional[int] = None
        self._records: Deque[
            Union[OscilloscopeFrameRecord, OscilloscopeConnectionBoundary]
        ] = deque()
        #: 采样存储只保存数值数组；测试可注入失败替身验证追加原子性。
        self._store = CompactSampleStore() if sample_store is None else sample_store
        self._channel_count = 0
        self._latest_values: List[int] = [0] * _MAX_CHANNELS
        #: 每通道当前保留的缺口语义分段数；逐点 O(1) 更新，淘汰时按
        #: “被淘汰点有值且下一条保留点缺该字段”递减。
        self._segment_counts: List[int] = [0] * _MAX_CHANNELS
        #: 上一条采样是否实际提供了该通道；短帧与首点之后都必须断线。
        self._last_sample_had_field: List[bool] = [False] * _MAX_CHANNELS

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
        if 0 <= index < self._channel_count:
            return self._latest_values[index]
        return None

    def channel_segment_count(self, index: int) -> int:
        """通道 ``index``（0 起）当前的分段数（O(1)）。"""
        return self._segment_counts[index]

    def channel_segments(self, index: int) -> List[List[tuple[float, int]]]:
        """通道 ``index``（0 起）的缺口分段点序列。

        同一合法帧的所有字段共享同一时间；缺失字段不生成伪采样，而是在
        缺口两侧切分为不同分段，便于绘图时真正断线。返回的是模型原始点
        的拷贝，绘图调用方不可能反向修改采样存储。
        """
        if not -_MAX_CHANNELS <= index < _MAX_CHANNELS:
            raise IndexError(index)
        if index < 0:
            index += _MAX_CHANNELS
        segments: List[List[tuple[float, int]]] = []
        current: Optional[List[tuple[float, int]]] = None
        previous_position = -2
        for position, relative_seconds, value in self._store.iter_channel_points(index):
            if position != previous_position + 1:
                current = []
                segments.append(current)
            current.append((relative_seconds, value))
            previous_position = position
        return segments

    @property
    def records(self) -> List[Union[OscilloscopeFrameRecord, OscilloscopeConnectionBoundary]]:
        return list(self._records)

    @property
    def samples(self) -> List[OscilloscopeSample]:
        return [
            OscilloscopeSample(relative_seconds, values)
            for relative_seconds, values in self._store.iter_samples()
        ]

    def iter_samples_in_range(
        self, x_min: float, x_max: float
    ) -> Iterable[OscilloscopeSample]:
        """当前保留采样中相对时间落在闭区间 ``[x_min, x_max]`` 的原始帧。

        悬停只检查指针附近的时间带；此查询按紧凑时间数组二分定位，避免
        为全量保留点构造 ``OscilloscopeSample`` 列表（REQ-0005 §8.4）。
        """
        for relative_seconds, values in self._store.iter_samples_in_range(
            x_min, x_max
        ):
            yield OscilloscopeSample(relative_seconds, values)

    @property
    def sample_count(self) -> int:
        """当前保留的原始采样点数（O(1)，不复制样本列表）。"""
        return self._store.count

    @property
    def record_count(self) -> int:
        """当前保留的完整帧与连接边界记录数（不复制记录列表）。"""
        return len(self._records)

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
        self._store.clear()
        self._channel_count = 0
        self._latest_values = [0] * _MAX_CHANNELS
        self._segment_counts = [0] * _MAX_CHANNELS
        self._last_sample_had_field = [False] * _MAX_CHANNELS

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
        """Turn one complete frame into a record, sampling it when legal.

        提交顺序：先算元数据变更、再写紧凑采样、再写帧记录，全部成功后才
        淘汰；任何一步 ``MemoryError`` 都不提交本帧、也不淘汰旧窗口，
        既有采样/时间/通道保持不变（REQ-0005 §7.2.6）。
        """
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
        if values is None:
            self._records.append(record)
            # 数据区窗口只看完整帧（合法或非法）。
            self._evict_records_before(relative_seconds - RETENTION_SECONDS)
            return record
        metadata_plan = self._plan_sample_metadata(values)
        self._store.append(relative_seconds, values)
        try:
            self._records.append(record)
        except BaseException:
            self._store.rollback_last()
            raise
        self._apply_sample_metadata(values, metadata_plan)
        self._evict_records_before(relative_seconds - RETENTION_SECONDS)
        # 采样窗口只由最新合法采样推进；非法帧不得影响它（REQ-0005 §7.2.3）。
        self._evict_samples_before(relative_seconds - RETENTION_SECONDS)
        return record

    def _plan_sample_metadata(
        self, values: tuple[int, ...]
    ) -> tuple[List[tuple[int, int]], int]:
        """提交前先算出元数据变化，保证分配失败时状态未被触碰。"""
        increments = [
            (index, self._segment_counts[index] + 1)
            for index in range(min(len(values), _MAX_CHANNELS))
            if not self._last_sample_had_field[index]
        ]
        if len(values) > self._channel_count:
            channel_count = len(values)
        else:
            channel_count = self._channel_count
        return increments, channel_count

    def _apply_sample_metadata(
        self, values: tuple[int, ...], plan: tuple[List[tuple[int, int]], int]
    ) -> None:
        """只做赋值、不再分配；在采样与帧记录都提交成功后调用。"""
        increments, channel_count = plan
        for index, new_count in increments:
            self._segment_counts[index] = new_count
        limit = min(len(values), _MAX_CHANNELS)
        for index in range(limit):
            self._last_sample_had_field[index] = True
            self._latest_values[index] = values[index]
        for index in range(limit, _MAX_CHANNELS):
            self._last_sample_had_field[index] = False
        self._channel_count = channel_count

    def _evict_samples_before(self, cutoff_seconds: float) -> None:
        """删除早于 ``cutoff_seconds`` 的原始采样；分段数按存在性 O(1) 递减。"""
        store = self._store
        while store.count and store.oldest_time < cutoff_seconds:
            width = store.oldest_width
            next_width = store.width_at(1) if store.count > 1 else 0
            for index in range(width):
                if index >= next_width:
                    self._segment_counts[index] -= 1
            store.evict_oldest()

    def _evict_records_before(self, cutoff_seconds: float) -> None:
        """删除早于 ``cutoff_seconds`` 的完整帧和连接边界记录。"""
        while self._records and self._records[0].relative_seconds < cutoff_seconds:
            self._records.popleft()


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
