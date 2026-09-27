"""Qt-free oscilloscope frame model for the issue 012 tracer slice.

The receive thread already recognizes the strict ``0D 0A`` boundary and stamps
each ``ReceivedEvent`` with both the local wall clock and a high-resolution
monotonic clock (``event.monotonic_ns``). This module owns the waveform page's
independent frame history and samples: it turns those complete frames into
records whose relative time is computed against the acquisition origin.

Issue 013 extends the payload grammar to 1..8 integers and owns the full
invalid-frame matrix; this slice only accepts a single signed decimal int32 so
every other complete payload safely becomes a parse failure (no sample).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647

#: issue 012 tracer grammar: one signed decimal integer, ASCII digits only.
_SINGLE_INTEGER = re.compile(rb"[+-]?[0-9]+")


def parse_frame_payload(payload: bytes) -> Optional[tuple[int, ...]]:
    """Parse one complete frame payload; ``None`` means "解析失败"."""
    if _SINGLE_INTEGER.fullmatch(payload) is None:
        return None
    value = int(payload)
    if not INT32_MIN <= value <= INT32_MAX:
        return None
    return (value,)


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


class OscilloscopeSession:
    """Acquisition origin plus the waveform page's frame and sample history."""

    def __init__(self, monotonic_ns: Optional[Callable[[], int]] = None) -> None:
        self._monotonic_ns = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
        self._origin_ns: Optional[int] = None
        self._records: List[OscilloscopeFrameRecord] = []
        self._samples: List[OscilloscopeSample] = []

    @property
    def origin_ns(self) -> Optional[int]:
        return self._origin_ns

    @property
    def records(self) -> List[OscilloscopeFrameRecord]:
        return list(self._records)

    @property
    def samples(self) -> List[OscilloscopeSample]:
        return list(self._samples)

    def begin_acquisition(self) -> None:
        """Capture ``T+0`` on the first successful start of the session.

        Later starts continue the same acquisition, so the origin is only set
        once (REQ-0005 §6.2.1, §14.6).
        """
        if self._origin_ns is None:
            self._origin_ns = self._monotonic_ns()

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


def format_frame_line(record: OscilloscopeFrameRecord) -> str:
    """One data-area line: relative time, raw payload and parse status."""
    status = "解析成功" if record.parse_ok else "解析失败"
    return (
        f"{format_relative_seconds(record.relative_seconds)}  "
        f"payload={format_payload(record.payload)}  {status}"
    )
