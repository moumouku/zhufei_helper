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
    * ``received_queue`` receives the bytes read by the background reader thread
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
        self.read_exc = read_exc
        self.write_exc = write_exc
        if port in FakeSerial.fail_ports:
            raise OSError(f"could not open port {port!r}: device busy")

    def read(self, n=1):
        if self.read_exc is not None:
            raise self.read_exc
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


def collect_bytes(q, expected_len, timeout=2.0):
    """Accumulate bytes from ``q`` until ``expected_len`` or deadline; never blocks forever."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline and len(buf) < expected_len:
        try:
            item = q.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        buf += bytes(item)
    return buf


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


def test_write_forwards_bytes_to_serial(controller, factory):
    controller.open(make_settings())
    controller.write(b"\x01\x02\xff")

    assert bytes(factory.instances[0].written) == b"\x01\x02\xff"
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
        "read_data": bytearray(b"\xaa" * 64),
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


def test_received_queue_delivers_read_bytes(controller, factory):
    factory.seed = {"read_data": bytearray(b"\x01\x02\xffABC")}

    controller.open(make_settings())
    got = collect_bytes(controller.received_queue, expected_len=6)

    assert got == b"\x01\x02\xffABC", f"expected all read bytes, collected {got!r}"
    controller.close()


def test_error_queue_receives_read_exception_text(controller, factory):
    factory.seed = {"read_exc": OSError("device removed from bus")}

    controller.open(make_settings())
    msg = controller.error_queue.get(timeout=2.0)

    assert isinstance(msg, str)
    assert "device removed" in msg
    controller.close()


def test_close_is_idempotent_and_stops_reader(controller, factory):
    factory.seed = {"read_data": bytearray(b"\xaa" * 64)}

    controller.open(make_settings())
    assert collect_bytes(controller.received_queue, expected_len=8), "reader never delivered data"

    controller.close()
    controller.close()  # second close must be a no-op

    assert not controller.is_open
    assert factory.instances[0].closed

    size_after_close = controller.received_queue.qsize()
    time.sleep(0.3)  # give a (buggy) still-running reader time to push more data
    assert controller.received_queue.qsize() == size_after_close, "reader still running after close()"


def test_close_before_open_is_a_noop(controller):
    controller.close()  # must not raise
    assert not controller.is_open
