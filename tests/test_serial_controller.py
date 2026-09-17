"""Contract tests for ``paimon_assistant.serial_controller`` (TDD Red phase).

This file pins the fixed public API that the production module must implement:

    paimon_assistant.serial_controller:
        SerialController(serial_factory, port_lister)   # dependencies injected
        SerialConnectionError                            # open failure / write while closed
        SerialPortInfo(port=..., description=...)        # one enumerable port entry
        SerialSettings(port=..., baudrate=..., bytesize=..., parity=..., stopbits=...)

Behaviors pinned here:
    * ``available_ports()`` -> list[SerialPortInfo] via the injected ``port_lister``
    * ``open(settings)`` calls ``serial_factory`` with port/baudrate/bytesize/parity/
      stopbits and the fixed timeout=0.1
    * ``is_open`` reflects the current state
    * ``write(bytes)`` forwards bytes to the opened serial port
    * a failed ``write`` raises ``SerialConnectionError`` while preserving the
      original exception, moves the controller to the closed state, closes the
      underlying port, and stops the reader (later writes fail as not open)
    * ``received_queue`` receives the ``ReceivedEvent`` frames framed by the reader
    * ``diagnostic_queue`` receives the fixed overflow diagnostic text
    * ``error_queue`` receives the *text* of runtime read exceptions
    * open failure raises ``SerialConnectionError`` and the controller stays closed
    * ``write`` while closed raises ``SerialConnectionError``
    * ``close()`` is idempotent and stops the reader thread

All fake serial machinery lives in this file (no conftest.py, no other test files).
Every wait is bounded (queue.get with timeout / explicit deadlines) so a broken
production implementation can never hang the suite.
"""

import queue
import sys
import threading
import time
from pathlib import Path

import pytest

# Make the project root importable regardless of how pytest is invoked
# (e.g. bare `pytest` without `-m`). Idempotent; harmless when root is already on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.receive_framer import ReceivedEvent  # noqa: E402
from paimon_assistant.serial_controller import (  # noqa: E402  (module is the contract under test)
    SerialConnectionError,
    SerialController,
    SerialPortInfo,
    SerialSettings,
)


# ---------------------------------------------------------------------------
# Fakes (all defined in this file)
# ---------------------------------------------------------------------------

class FakeSerial:
    """Minimal pyserial-compatible fake. Per-instance state, optional failure."""

    fail_ports = set()  # ports whose construction raises OSError (simulates busy port)

    def __init__(
        self,
        port,
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=None,
        read_data=None,
        read_chunks=None,
        read_exc=None,
        write_exc=None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self.is_open = True
        self.closed = False
        self.written = bytearray()
        self.read_data = bytearray(read_data or b"")
        # Pre-scripted read() results, delivered one per call (None disables).
        self.read_chunks = None if read_chunks is None else [bytes(c) for c in read_chunks]
        self.read_exc = read_exc
        self.write_exc = write_exc
        if port in FakeSerial.fail_ports:
            raise OSError(f"could not open port {port!r}: device busy")

    def read(self, n=1):
        if self.read_exc is not None:
            raise self.read_exc
        if self.read_chunks is not None:
            if not self.read_chunks:
                return b""
            return self.read_chunks.pop(0)
        if not self.read_data:
            return b""
        chunk = self.read_data[:n]
        del self.read_data[:n]
        return bytes(chunk)

    def write(self, data):
        if self.write_exc is not None:
            raise self.write_exc
        self.written.extend(data)
        return len(data)

    def close(self):
        self.closed = True
        self.is_open = False


class RecordingFactory:
    """Injected ``serial_factory`` dependency: creates FakeSerial, records every call."""

    def __init__(self, fake_cls=FakeSerial):
        self.calls = []  # list of dicts with resolved port params, one per open()
        self.instances = []  # FakeSerial instances created, in order
        self.seed = {}  # extra kwargs merged into every FakeSerial (e.g. read_data)
        self.fake_cls = fake_cls  # FakeSerial subclass to instantiate

    def __call__(self, *args, **kwargs):
        merged = dict(self.seed)
        merged.update(kwargs)
        inst = self.fake_cls(*args, **merged)
        self.calls.append(
            {
                "port": inst.port,
                "baudrate": inst.baudrate,
                "bytesize": inst.bytesize,
                "parity": inst.parity,
                "stopbits": inst.stopbits,
                "timeout": inst.timeout,
            }
        )
        self.instances.append(inst)
        return inst


class GatedReadFakeSerial(FakeSerial):
    """Fake whose read() parks until ``release()``, then returns one chunk.

    ``read_starts`` counts the parks, so a test can prove the previous chunk
    was fully framed and published before releasing the next one: the reader
    parks again only after it looped back from that publication.
    """

    def __init__(self, *args, chunks=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.chunks = list(chunks)
        self.read_starts = 0
        self._gate = threading.Event()

    def read(self, n=1):
        self.read_starts += 1
        self._gate.wait(timeout=5.0)  # bounded: a broken test cannot hang
        self._gate.clear()
        if self.chunks:
            item = self.chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            return bytes(item)
        return b""

    def release(self):
        self._gate.set()


class GatedFactory(RecordingFactory):
    """Creates ``GatedReadFakeSerial`` ports with one chunk script per open."""

    def __init__(self, scripts):
        super().__init__(fake_cls=GatedReadFakeSerial)
        self._scripts = [list(script) for script in scripts]

    def __call__(self, *args, **kwargs):
        kwargs = dict(kwargs, chunks=self._scripts.pop(0))
        return super().__call__(*args, **kwargs)


class BlockingReadFakeSerial(FakeSerial):
    """Fake whose read() blocks until ``cancel_read()`` releases it.

    ``close()`` alone does *not* unblock the parked read -- like a driver
    that parks the OS read until explicit cancellation. This proves the
    controller must call ``cancel_read()`` (not just ``close()``) when a
    write fails, otherwise the reader thread never exits within the join
    window. The block is bounded so a broken implementation cannot hang the
    suite.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_read_calls = 0
        self._read_started = threading.Event()  # set once a read() is parked
        self._read_release = threading.Event()  # set only by cancel_read()

    def read(self, n=1):
        self._read_started.set()
        self._read_release.wait(timeout=10.0)  # bounded: never hang the suite
        return super().read(n)

    def cancel_read(self):
        self.cancel_read_calls += 1
        self._read_release.set()


class FakePortLister:
    """Injected ``port_lister`` dependency: returns a fixed list of SerialPortInfo."""

    def __init__(self, ports):
        self.ports = list(ports)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.ports)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def factory():
    return RecordingFactory()


@pytest.fixture
def port_lister():
    return FakePortLister(
        [
            SerialPortInfo(port="COM3", description="Fake COM3 (com0com)"),
            SerialPortInfo(port="COM4", description="Fake COM4 (com0com)"),
        ]
    )


@pytest.fixture
def controller(factory, port_lister):
    return SerialController(serial_factory=factory, port_lister=port_lister)


@pytest.fixture(autouse=True)
def _reset_fake_state():
    FakeSerial.fail_ports = set()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_settings(port="COM3", baudrate=9600, bytesize=8, parity="N", stopbits=1):
    return SerialSettings(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
    )


def collect_events(q, expected_len, timeout=2.0):
    """Collect ``ReceivedEvent`` items from ``q`` until ``expected_len`` or deadline."""
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline and len(events) < expected_len:
        try:
            events.append(q.get(timeout=max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            break
    return events


def wait_until(predicate, timeout=2.0):
    """Bounded wait for a predicate; returns its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_available_ports_returns_serial_port_info_list(controller, port_lister):
    ports = controller.available_ports()

    assert isinstance(ports, list)
    assert len(ports) == 2
    assert all(isinstance(p, SerialPortInfo) for p in ports)
    assert [p.port for p in ports] == ["COM3", "COM4"]
    assert ports[0].description == "Fake COM3 (com0com)"
    assert ports[1].description == "Fake COM4 (com0com)"
    assert port_lister.calls == 1


def test_open_calls_factory_with_settings_fields_and_fixed_timeout(controller, factory):
    controller.open(
        make_settings(port="COM3", baudrate=115200, bytesize=7, parity="E", stopbits=2)
    )

    assert len(factory.calls) == 1
    call = factory.calls[0]
    assert call["port"] == "COM3"
    assert call["baudrate"] == 115200
    assert call["bytesize"] == 7
    assert call["parity"] == "E"
    assert call["stopbits"] == 2
    assert call["timeout"] == 0.1  # fixed read timeout, regardless of settings
    assert controller.is_open
    controller.close()


def test_is_open_reflects_open_and_close(controller, factory):
    assert not controller.is_open

    controller.open(make_settings())
    assert controller.is_open

    controller.close()
    assert not controller.is_open


def test_write_forwards_bytes_verbatim_and_outside_receive_path(controller, factory):
    controller.open(make_settings())
    controller.write(b"\x01\x02\xff")
    controller.write(b"cmd")

    # 原始字节不变，不自动追加 `0D 0A`，也不生成接收事件（REQ-0003 §11）。
    assert bytes(factory.instances[0].written) == b"\x01\x02\xffcmd"
    assert controller.received_queue.empty()
    controller.close()


def test_reset_receive_session_discards_pending_items_tail_and_keeps_reading():
    """Reset is the linearization point: old tail/queue items never survive."""
    factory = GatedFactory([[b"AB\r", b"\n", b"C\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    serial = None
    try:
        controller.open(make_settings("COM1"))
        serial = factory.instances[0]
        assert wait_until(lambda: serial.read_starts >= 1)
        serial.release()
        assert wait_until(lambda: serial.read_starts >= 2), "第一块未被分帧处理完"

        old_received = controller.received_queue
        old_diagnostic = controller.diagnostic_queue
        old_received.put(ReceivedEvent(0, b"stale", b"stale\r\n"))
        old_diagnostic.put("stale diagnostic")

        controller.reset_receive_session()

        assert controller.received_queue is not old_received
        assert controller.diagnostic_queue is not old_diagnostic
        assert old_received.empty()
        assert old_diagnostic.empty()
        assert controller.received_queue.empty()

        serial.release()  # 单独的 `\n`：旧尾部 `AB\r` 若幸存，会在这里成事件
        assert wait_until(lambda: serial.read_starts >= 3), "重置后首块未被处理"
        assert controller.received_queue.empty()

        serial.release()
        assert controller.received_queue.get(timeout=2.0).payload == b"\nC"
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_reset_receive_session_while_closed_is_safe(controller, factory):
    """Resetting a closed controller swaps/drains queues without opening a port."""
    stale_received = controller.received_queue
    stale_diagnostic = controller.diagnostic_queue
    stale_received.put(ReceivedEvent(0, b"old", b"old\r\n"))
    stale_diagnostic.put("old diagnostic")

    controller.reset_receive_session()

    assert controller.received_queue is not stale_received
    assert controller.diagnostic_queue is not stale_diagnostic
    assert stale_received.empty()
    assert stale_diagnostic.empty()
    assert not controller.is_open
    assert factory.calls == []  # never touched the serial factory
    with pytest.raises(queue.Empty):
        controller.received_queue.get_nowait()


def test_reset_receive_session_clears_overflow_state():
    """After reset the framer reports a fresh overflow instead of staying fused."""
    oversized = b"x" * (1_048_576 + 1)
    factory = GatedFactory([[oversized, b"y" * 16, oversized, b"\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    serial = None
    try:
        controller.open(make_settings("COM1"))
        serial = factory.instances[0]
        assert wait_until(lambda: serial.read_starts >= 1)
        serial.release()
        assert wait_until(lambda: serial.read_starts >= 2, timeout=5.0)
        assert controller.diagnostic_queue.get(timeout=2.0) == "接收帧超过 1 MiB，已丢弃"

        serial.release()  # still the same continuous overflow: no new diagnostic
        assert wait_until(lambda: serial.read_starts >= 3, timeout=5.0)
        with pytest.raises(queue.Empty):
            controller.diagnostic_queue.get_nowait()

        controller.reset_receive_session()
        serial.release()  # overflow state was dropped: reports again
        assert wait_until(lambda: serial.read_starts >= 4, timeout=5.0)
        assert controller.diagnostic_queue.get(timeout=2.0) == "接收帧超过 1 MiB，已丢弃"
        assert controller.received_queue.empty()
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_write_before_open_raises_connection_error(controller):
    with pytest.raises(SerialConnectionError):
        controller.write(b"\x00")


def test_write_failure_calls_cancel_read_and_reader_really_exits(factory):
    """A failed write must cancel_read() the port so the reader thread exits.

    Uses a fake whose read() parks until ``cancel_read()`` and whose close()
    does NOT release it. Without the cancel_read call the reader stays alive
    past the controller's bounded join; with it, the thread exits promptly.
    The error is still surfaced as SerialConnectionError.
    """
    factory.fake_cls = BlockingReadFakeSerial
    factory.seed = {"write_exc": OSError("device disconnected")}
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )

    controller.open(make_settings())
    ser = factory.instances[0]
    assert controller.is_open
    # Make sure the reader is actually parked inside read() before failing the
    # write, so only cancel_read() can release it (bounded wait).
    assert ser._read_started.wait(timeout=2.0), "reader never entered a blocking read"

    with pytest.raises(SerialConnectionError) as excinfo:
        controller.write(b"\x01")
    assert "serial write failed" in str(excinfo.value)

    # close() alone cannot unblock this fake's read: cancel_read() is required
    assert ser.cancel_read_calls == 1, "failed write must cancel_read() the port"
    assert ser.closed, "failed write must close the port"

    # The reader thread must actually exit (cancel_read releases its blocked
    # read, so it sees the stop event and finishes within the join window).
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(
        t.name == "serial-reader" and t.is_alive() for t in threading.enumerate()
    ):
        time.sleep(0.01)
    assert not any(
        t.name == "serial-reader" and t.is_alive() for t in threading.enumerate()
    ), "reader thread still alive after failed write"

    with pytest.raises(SerialConnectionError):
        controller.write(b"\x00")  # later writes fail as not open
    controller.close()


def test_write_failure_preserves_error_and_closes_connection(controller, factory):
    """A failed write must not leave the controller in a fake-open state.

    The original exception is preserved (message + ``__cause__``), the port is
    closed, the reader stops (bounded wait), and later writes fail as not open.
    """
    factory.seed = {
        "read_data": bytearray(b"\xaa\xbb\r\n" * 32),
        "write_exc": OSError("device disconnected"),
    }

    controller.open(make_settings())
    assert controller.is_open

    with pytest.raises(SerialConnectionError) as excinfo:
        controller.write(b"\x01\x02")

    # 1) original exception info is preserved
    assert "serial write failed" in str(excinfo.value)
    assert "device disconnected" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)
    assert "device disconnected" in str(excinfo.value.__cause__)

    # 2) controller immediately left the open state
    assert not controller.is_open
    # 3) underlying port was closed
    assert factory.instances[0].closed

    # 4) reader stopped: queue settles and never grows again
    time.sleep(0.3)  # let any in-flight bytes arrive (bounded)
    size_after = controller.received_queue.qsize()
    time.sleep(0.3)
    assert (
        controller.received_queue.qsize() == size_after
    ), "reader still running after failed write"

    # 5) later writes fail as if the controller were never opened
    with pytest.raises(SerialConnectionError):
        controller.write(b"\x00")

    controller.close()  # must remain a no-op after the failure teardown


def test_open_failure_raises_connection_error_and_stays_closed(controller, factory):
    FakeSerial.fail_ports.add("COM3")

    with pytest.raises(SerialConnectionError):
        controller.open(make_settings(port="COM3"))

    assert not controller.is_open
    assert factory.instances == []  # failed construction leaves no open handle
    with pytest.raises(SerialConnectionError):
        controller.write(b"\x00")


def test_received_queue_delivers_event_for_frame_split_across_reads(controller, factory):
    """`A\r` and `\n` may arrive in separate reads: one event, not raw chunks."""
    factory.seed = {"read_chunks": [b"\x01\x02\xffA\r", b"\n"]}

    controller.open(make_settings())
    event = controller.received_queue.get(timeout=2.0)

    assert isinstance(event, ReceivedEvent)
    assert event.payload == b"\x01\x02\xffA"
    assert event.raw_frame == b"\x01\x02\xffA\r\n"
    assert controller.received_queue.empty()
    controller.close()


def test_single_read_with_multiple_frames_yields_ordered_events(controller, factory):
    """One read may carry several frames: one independent event each, in order."""
    factory.seed = {"read_chunks": [b"A\r\nB\r\n"]}

    controller.open(make_settings())
    first = controller.received_queue.get(timeout=2.0)
    second = controller.received_queue.get(timeout=2.0)

    assert (first.payload, first.raw_frame) == (b"A", b"A\r\n")
    assert (second.payload, second.raw_frame) == (b"B", b"B\r\n")
    assert controller.received_queue.empty()
    controller.close()


def test_events_use_injected_clock_at_frame_boundary(factory, port_lister):
    """Timestamp + payload/raw of an empty cross-read frame and a later frame."""
    ticks = iter([1_700_000_000_000, 1_700_000_000_123])
    controller = SerialController(
        serial_factory=factory,
        port_lister=port_lister,
        clock_ms=lambda: next(ticks),
    )
    factory.seed = {"read_chunks": [b"\r", b"\n", b"xy\r\n"]}

    controller.open(make_settings())
    first = controller.received_queue.get(timeout=2.0)
    second = controller.received_queue.get(timeout=2.0)

    assert first.payload == b""
    assert first.raw_frame == b"\r\n"
    assert first.received_at_ms == 1_700_000_000_000
    assert second.payload == b"xy"
    assert second.raw_frame == b"xy\r\n"
    assert second.received_at_ms == 1_700_000_000_123
    controller.close()


def test_overflow_publishes_one_fixed_diagnostic_and_no_event(controller, factory):
    """A frame past 1 MiB emits the fixed diagnostic once, then frames resume."""
    oversized = b"x" * (1_048_576 + 1)
    factory.seed = {"read_chunks": [oversized, b"y" * 1024, b"\r\n", b"OK\r\n"]}

    controller.open(make_settings())
    assert controller.diagnostic_queue.get(timeout=5.0) == "接收帧超过 1 MiB，已丢弃"
    # The same continuous overflow (more discarded bytes) must not spam again,
    # the oversized frame yields no event, and parsing resumes after `\r\n`.
    assert controller.received_queue.get(timeout=5.0).payload == b"OK"
    with pytest.raises(queue.Empty):
        controller.diagnostic_queue.get_nowait()
    controller.close()


def test_receive_path_delivers_events_while_file_access_is_broken(
    controller, factory, monkeypatch
):
    """The reader does no file I/O: broken open() must not stop event delivery."""
    factory.seed = {"read_chunks": [b"OK\r\n"]}

    def broken_open(*args, **kwargs):
        raise OSError("file access is broken")

    monkeypatch.setattr("builtins.open", broken_open)
    controller.open(make_settings())
    assert controller.received_queue.get(timeout=2.0).payload == b"OK"
    controller.close()


def test_error_queue_receives_read_exception_text(controller, factory):
    factory.seed = {"read_exc": OSError("device removed from bus")}

    controller.open(make_settings())
    msg = controller.error_queue.get(timeout=2.0)

    assert isinstance(msg, str)
    assert "device removed" in msg
    controller.close()


def test_close_is_idempotent_and_stops_reader(controller, factory):
    factory.seed = {"read_data": bytearray(b"\xaa\xbb\r\n" * 32)}

    controller.open(make_settings())
    assert collect_events(controller.received_queue, expected_len=8), "reader never delivered events"

    controller.close()
    controller.close()  # second close must be a no-op

    assert not controller.is_open
    assert factory.instances[0].closed

    size_after_close = controller.received_queue.qsize()
    time.sleep(0.3)  # give a (buggy) still-running reader time to push more data
    assert controller.received_queue.qsize() == size_after_close, "reader still running after close()"


def test_reopen_ignores_delayed_data_from_previous_session():
    """A delayed read from a closed connection must not reach the new queue."""
    factory = GatedFactory([[b"old\r\n"], [b"new\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        old_reader = next(
            t
            for t in threading.enumerate()
            if t.name == "serial-reader" and t.is_alive()
        )
        first = factory.instances[0]
        assert wait_until(lambda: first.read_starts >= 1), "reader 未进入 read"

        controller.close()  # old reader stays parked in read()
        controller.open(make_settings("COM2"))
        second = factory.instances[1]
        assert wait_until(lambda: second.read_starts >= 1), "新 reader 未进入 read"

        second.release()
        assert controller.received_queue.get(timeout=2.0).payload == b"new"

        # Delayed old read returns while the new session is live: it must not
        # publish anything into the new session's queue.
        first.release()
        assert wait_until(lambda: not old_reader.is_alive()), "旧 reader 未退出"
        assert controller.received_queue.empty()
    finally:
        for ser in (first, second):
            if ser is not None:
                ser.release()
        controller.close()


def test_close_discards_unfinished_tail_before_reopen():
    """A partial frame from the old connection must not combine with new data."""
    factory = GatedFactory([[b"AB\r"], [b"\n", b"C\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        first = factory.instances[0]
        assert wait_until(lambda: first.read_starts >= 1)
        first.release()
        assert wait_until(lambda: first.read_starts >= 2), "第一块未被分帧处理完"
        assert controller.received_queue.empty()  # `AB\r` 还是未完成尾部

        controller.close()
        controller.open(make_settings("COM2"))
        second = factory.instances[1]
        assert wait_until(lambda: second.read_starts >= 1)

        second.release()  # 单独的 `\n`：旧尾部若泄漏，这里会补成 `AB` 事件
        assert wait_until(lambda: second.read_starts >= 2), "第二连接首块未被处理"
        assert controller.received_queue.empty()

        second.release()
        # 单独的 `\n` 按严格 `0D 0A` 规则属于载荷（§4.1.3）。
        assert controller.received_queue.get(timeout=2.0).payload == b"\nC"
    finally:
        for ser in (first, second):
            if ser is not None:
                ser.release()
        controller.close()


def test_read_error_discards_unfinished_tail_before_reopen():
    """Read failure discards the framer tail and never reaches a later session."""
    factory = GatedFactory([[b"AB\r", OSError("device removed")], [b"\n", b"C\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        first = factory.instances[0]
        assert wait_until(lambda: first.read_starts >= 1)
        first.release()
        assert wait_until(lambda: first.read_starts >= 2), "第一块未被分帧处理完"
        assert controller.received_queue.empty()

        first.release()  # OSError: reader reports it and closes the controller
        msg = controller.error_queue.get(timeout=2.0)
        assert "device removed" in msg
        assert wait_until(lambda: not controller.is_open)

        controller.open(make_settings("COM2"))
        second = factory.instances[1]
        assert wait_until(lambda: second.read_starts >= 1)
        second.release()  # 旧尾部若幸存，这里会补成 `AB` 事件
        assert wait_until(lambda: second.read_starts >= 2), "第二连接首块未被处理"
        assert controller.received_queue.empty()

        second.release()
        assert controller.received_queue.get(timeout=2.0).payload == b"\nC"
    finally:
        for ser in (first, second):
            if ser is not None:
                ser.release()
        controller.close()


def test_write_failure_discards_unfinished_tail_before_reopen():
    """A failed write tears the session down; its tail cannot join later data."""
    factory = GatedFactory([[b"AB\r"], [b"\n", b"C\r\n"]])
    factory.seed = {"write_exc": OSError("device disconnected")}
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        first = factory.instances[0]
        assert wait_until(lambda: first.read_starts >= 1)
        first.release()
        assert wait_until(lambda: first.read_starts >= 2), "第一块未被分帧处理完"

        with pytest.raises(SerialConnectionError):
            controller.write(b"\x01")
        assert wait_until(lambda: not controller.is_open)

        controller.open(make_settings("COM2"))
        second = factory.instances[1]
        assert wait_until(lambda: second.read_starts >= 1)
        second.release()  # 旧尾部若泄漏，这里会补成 `AB` 事件
        assert wait_until(lambda: second.read_starts >= 2), "第二连接首块未被处理"
        assert controller.received_queue.empty()

        second.release()
        assert controller.received_queue.get(timeout=2.0).payload == b"\nC"
    finally:
        for ser in (first, second):
            if ser is not None:
                ser.release()
        controller.close()


def test_close_discards_overflow_state_before_reopen():
    """Overflow discarding state is per session: a new connection frames directly."""
    oversized = b"x" * (1_048_576 + 1)
    factory = GatedFactory([[oversized], [b"a" * 8 + b"\r\n"]])
    controller = SerialController(
        serial_factory=factory, port_lister=FakePortLister([])
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        first = factory.instances[0]
        assert wait_until(lambda: first.read_starts >= 1)
        first.release()
        assert wait_until(lambda: first.read_starts >= 2, timeout=5.0)
        assert controller.diagnostic_queue.get(timeout=2.0) == "接收帧超过 1 MiB，已丢弃"

        controller.close()
        controller.open(make_settings("COM2"))
        second = factory.instances[1]
        assert wait_until(lambda: second.read_starts >= 1)
        second.release()
        event = controller.received_queue.get(timeout=2.0)
        assert event.payload == b"a" * 8
    finally:
        for ser in (first, second):
            if ser is not None:
                ser.release()
        controller.close()


def test_close_before_open_is_a_noop(controller):
    controller.close()  # must not raise
    assert not controller.is_open
