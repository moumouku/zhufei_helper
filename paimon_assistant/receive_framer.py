"""Strict `0D 0A` receive framing producing immutable `ReceivedEvent` records.

Contract: docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md
§4 (receive data protocol) and §5/§5.1 (event protocol and public interface).

No Qt, no real serial port and no system clock: the millisecond time source is
injected, so every timestamp is reproducible in tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceivedEvent:
    """One complete data frame plus the time its `0D 0A` boundary was read."""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes

    def __post_init__(self) -> None:
        # Copy at construction so a reused caller buffer can never mutate
        # an event that was already produced.
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "raw_frame", bytes(self.raw_frame))


class ReceiveFramer:
    """Turns consecutive raw byte blocks into complete framing events."""

    def __init__(self, clock_ms, max_payload_bytes=1_048_576, on_overflow=None):
        self._clock_ms = clock_ms
        self._max_payload_bytes = max_payload_bytes
        self._on_overflow = on_overflow
        self._buffer = bytearray()
        self._pending_cr = False
        self._discarding = False

    def feed(self, data: bytes) -> list[ReceivedEvent]:
        """Return the complete events recognized in `data`, in input order."""
        events: list[ReceivedEvent] = []
        for byte in data:
            if self._discarding:
                if self._pending_cr and byte == 0x0A:
                    self._pending_cr = False
                    self._discarding = False
                else:
                    self._pending_cr = byte == 0x0D
                continue

            if self._pending_cr and byte != 0x0A and len(self._buffer) >= self._max_payload_bytes:
                # The pending CR turns out to be payload and pushes the frame
                # past its limit: abandon it and keep discarding from this byte on.
                self._start_discarding()
                self._pending_cr = byte == 0x0D
                continue

            if self._pending_cr:
                self._pending_cr = False
                if byte == 0x0A:
                    payload = bytes(self._buffer)
                    events.append(ReceivedEvent(self._clock_ms(), payload, payload + b"\r\n"))
                    self._buffer.clear()
                    continue
                self._buffer.append(0x0D)

            if byte == 0x0D:
                self._pending_cr = True
                continue

            if len(self._buffer) >= self._max_payload_bytes:
                self._start_discarding()
                continue

            self._buffer.append(byte)
        return events

    def reset(self) -> None:
        """Drop the unfinished tail; no event is produced."""
        self._buffer.clear()
        self._pending_cr = False
        self._discarding = False

    def _start_discarding(self) -> None:
        self._buffer.clear()
        self._pending_cr = False
        self._discarding = True
        if self._on_overflow is not None:
            self._on_overflow()
