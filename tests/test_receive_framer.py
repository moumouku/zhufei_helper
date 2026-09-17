"""Tests for paimon_assistant.receive_framer.

Contract: docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md
§4 (strict 0D 0A framing), §5 (ReceivedEvent), §5.1 (public interface).

The framer is exercised through its public interface only:
`ReceiveFramer(clock_ms, max_payload_bytes, on_overflow)`, `feed()`, `reset()`
and the three `ReceivedEvent` fields. Time comes from an injected fake source.
"""

import pytest

from dataclasses import FrozenInstanceError

from paimon_assistant.receive_framer import ReceivedEvent, ReceiveFramer


class FakeClock:
    """Deterministic millisecond time source, one value per call."""

    def __init__(self, *values):
        self._values = iter(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return next(self._values)


class TestReceivedEvent:
    def test_bytes_inputs_are_copied_into_stable_snapshots(self):
        payload = bytearray(b"A")
        raw_frame = bytearray(b"A\r\n")

        event = ReceivedEvent(1000, payload, raw_frame)
        payload += b"!"
        raw_frame += b"!"

        assert event.payload == b"A"
        assert event.raw_frame == b"A\r\n"

    def test_event_is_immutable(self):
        event = ReceivedEvent(1000, b"A", b"A\r\n")

        with pytest.raises(FrozenInstanceError):
            event.received_at_ms = 1


class TestFraming:
    def test_single_complete_frame_yields_one_event(self):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        events = framer.feed(b"A\r\n")

        assert events == [ReceivedEvent(1000, b"A", b"A\r\n")]

    def test_earlier_events_do_not_change_with_later_feeds(self):
        framer = ReceiveFramer(clock_ms=FakeClock(1000, 1001))

        first = framer.feed(b"A\r\n")[0]
        framer.feed(b"B\r\n")
        framer.reset()

        assert first == ReceivedEvent(1000, b"A", b"A\r\n")

    def test_empty_frame_yields_empty_payload(self):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        events = framer.feed(b"\r\n")

        assert events == [ReceivedEvent(1000, b"", b"\r\n")]

    @pytest.mark.parametrize("chunks,payload", [
        ([b"A\rB\nC", b"\r\n"], b"A\rB\nC"),
        ([b"A\r\r\n"], b"A\r"),
    ])
    def test_only_cr_lf_ends_a_frame(self, chunks, payload):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        events = []
        for chunk in chunks:
            events.extend(framer.feed(chunk))

        assert events == [ReceivedEvent(1000, payload, payload + b"\r\n")]

    def test_multiple_frames_in_one_block_are_returned_in_order(self):
        clock = FakeClock(1000, 1001)
        framer = ReceiveFramer(clock_ms=clock)

        events = framer.feed(b"A\r\nB\r\n")

        assert events == [
            ReceivedEvent(1000, b"A", b"A\r\n"),
            ReceivedEvent(1001, b"B", b"B\r\n"),
        ]

    def test_boundary_split_across_blocks_is_joined(self):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        assert framer.feed(b"A\r") == []

        events = framer.feed(b"\n")

        assert events == [ReceivedEvent(1000, b"A", b"A\r\n")]

    def test_incomplete_tail_produces_nothing_and_reset_discards_it(self):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        assert framer.feed(b"partial\r") == []
        assert clock.calls == 0

        framer.reset()

        assert framer.feed(b"B\r\n") == [ReceivedEvent(1000, b"B", b"B\r\n")]

    def test_timestamp_is_taken_once_per_completed_frame(self):
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        assert framer.feed(b"A\r\nB\r") == [ReceivedEvent(1000, b"A", b"A\r\n")]
        assert clock.calls == 1


class TestOverflow:
    def test_payload_of_exactly_one_mib_is_still_valid(self):
        payload = b"x" * 1_048_576
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        events = framer.feed(payload + b"\r\n")

        assert events == [ReceivedEvent(1000, payload, payload + b"\r\n")]

    def test_exactly_one_mib_with_split_terminator_is_still_valid(self):
        payload = b"x" * 1_048_576
        framer = ReceiveFramer(clock_ms=FakeClock(1000))

        assert framer.feed(payload) == []
        assert framer.feed(b"\r") == []

        assert framer.feed(b"\n") == [ReceivedEvent(1000, payload, payload + b"\r\n")]

    def test_first_payload_byte_past_one_mib_is_discarded_until_next_terminator(self):
        payload = b"x" * 1_048_576
        clock = FakeClock(1000)
        framer = ReceiveFramer(clock_ms=clock)

        assert framer.feed(payload) == []
        assert framer.feed(b"y") == []
        assert framer.feed(b"z\r\n") == []
        assert clock.calls == 0

        assert framer.feed(b"ok\r\n") == [ReceivedEvent(1000, b"ok", b"ok\r\n")]

    def test_oversized_frame_in_a_single_block_is_dropped_whole(self):
        payload = b"x" * 1_048_576
        framer = ReceiveFramer(clock_ms=FakeClock(1000))

        events = framer.feed(payload + b"y\r\n" + b"ok\r\n")

        assert events == [ReceivedEvent(1000, b"ok", b"ok\r\n")]

    def test_on_overflow_diagnoses_once_per_contiguous_overflow(self):
        diagnostics = []
        framer = ReceiveFramer(
            clock_ms=FakeClock(1000),
            max_payload_bytes=4,
            on_overflow=lambda: diagnostics.append("overflow"),
        )

        framer.feed(b"abcde")           # 5th payload byte starts one overflow
        framer.feed(b"more bytes\r\n")  # still the same overflow
        assert diagnostics == ["overflow"]

        assert framer.feed(b"ab\r\n") == [ReceivedEvent(1000, b"ab", b"ab\r\n")]
        assert diagnostics == ["overflow"]

        framer.feed(b"12345")           # a separate overflow is diagnosed again
        assert diagnostics == ["overflow", "overflow"]

    def test_discard_state_recovers_when_terminator_is_split_across_feeds(self):
        diagnostics = []
        framer = ReceiveFramer(
            clock_ms=FakeClock(1000),
            max_payload_bytes=4,
            on_overflow=lambda: diagnostics.append("overflow"),
        )

        assert framer.feed(b"x" * 5) == []  # 第 5 个载荷字节：进入丢弃
        assert diagnostics == ["overflow"]

        assert framer.feed(b"\r") == []  # 结束符前半块：仍在丢弃
        assert framer.feed(b"\n") == []  # 结束符后半块：恢复分帧

        assert framer.feed(b"ok\r\n") == [ReceivedEvent(1000, b"ok", b"ok\r\n")]
        assert diagnostics == ["overflow"]  # 同一段溢出只诊断一次

    def test_bare_lf_at_the_payload_limit_starts_discarding(self):
        diagnostics = []
        framer = ReceiveFramer(
            clock_ms=FakeClock(1000),
            max_payload_bytes=4,
            on_overflow=lambda: diagnostics.append("overflow"),
        )

        assert framer.feed(b"abcd") == []  # 缓冲恰好到上限仍有效
        assert framer.feed(b"\n") == []  # 裸 LF 是载荷：第 5 个字节触发丢弃
        assert diagnostics == ["overflow"]

        assert framer.feed(b"more\r\n") == []  # 丢弃直到下一个结束符
        assert framer.feed(b"ok\r\n") == [ReceivedEvent(1000, b"ok", b"ok\r\n")]
        assert diagnostics == ["overflow"]

    def test_reset_rearms_overflow_diagnosis(self):
        diagnostics = []
        framer = ReceiveFramer(
            clock_ms=FakeClock(1000),
            max_payload_bytes=2,
            on_overflow=lambda: diagnostics.append("overflow"),
        )

        framer.feed(b"abcd")
        assert diagnostics == ["overflow"]

        framer.reset()

        framer.feed(b"efgh")
        assert diagnostics == ["overflow", "overflow"]

    def test_cr_that_becomes_payload_at_the_limit_also_overflows(self):
        # A CR held at the limit is payload once a non-LF byte follows it.
        framer = ReceiveFramer(clock_ms=FakeClock(1000), max_payload_bytes=4)
        assert framer.feed(b"abcd") == []
        assert framer.feed(b"\r") == []
        assert framer.feed(b"e") == []
        assert framer.feed(b"tail\r\n") == []
        assert framer.feed(b"ok\r\n") == [ReceivedEvent(1000, b"ok", b"ok\r\n")]

        # The CR that triggers the overflow can itself end the discarded frame.
        framer = ReceiveFramer(clock_ms=FakeClock(1000), max_payload_bytes=4)
        assert framer.feed(b"abcd\r\r\n") == []
        assert framer.feed(b"ok\r\n") == [ReceivedEvent(1000, b"ok", b"ok\r\n")]
