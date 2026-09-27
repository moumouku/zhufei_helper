"""Threaded serial-port controller for Paimon Assistant.

The controller owns one serial connection at a time. Every successful open
creates a fresh receive session (a ``ReceiveFramer`` plus its queues); the
reader thread only publishes ``ReceivedEvent`` objects into that session's
queue after checking the generation it captured before the blocking read.
Before each reader starts, the driver's pending input buffer is
cleared so bytes sent while stopped can never be replayed; a cleanup failure
closes the port and raises ``SerialConnectionError``. ``reset_receive_session()``
swaps the session's framing state under the session lock, which is the single
linearization point between pre-clear and post-clear data, and returns the new
monotonic acquisition origin captured in that same lock. (REQ-0003 §5.1, §10.1;
REQ-0005 §4.3.4, §9.2)
"""

from __future__ import annotations

import queue
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from paimon_assistant.receive_framer import ReceiveFramer

try:
    # Re-export from config when it exists so both modules stay in sync.
    from paimon_assistant.config import SerialSettings  # type: ignore
except ImportError:  # config module not present yet -> compatible stand-in
    @dataclass
    class SerialSettings:
        """Serial port parameters (compatible stand-in until config exists)."""

        port: str
        baudrate: int = 9600
        bytesize: int = 8
        parity: str = "N"
        stopbits: int = 1


try:
    import serial as _serial
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover - pyserial not installed
    _serial = None
    _list_ports = None


class SerialConnectionError(Exception):
    """Raised when a serial port cannot be opened or an I/O operation fails."""


SerialPortInfo = namedtuple("SerialPortInfo", ["port", "description"])

#: Fixed text published to ``diagnostic_queue`` for one continuous overflow
#: (REQ-0003 §6.1.7). The callback only does ``queue.put``.
_OVERFLOW_DIAGNOSTIC = "接收帧超过 1 MiB，已丢弃"


def _epoch_ms() -> int:
    """Default millisecond clock: Unix epoch milliseconds."""
    return int(time.time() * 1000)


def _new_queue() -> queue.Queue:
    """Allocation boundary for replacement session queues.

    ``reset_receive_session`` allocates all three replacements here before it
    mutates any session state, so an allocation failure (including
    ``MemoryError``) leaves the old generation, queues and framer untouched.
    """
    return queue.Queue()


def _discard_pending(q: queue.Queue) -> None:
    """Drop every item currently queued (used when a session is reset)."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class _ReceiveSession:
    """Framing state shared by the controller and one connection's reader.

    The reader publishes through this session only while the generation it
    captured before its blocking ``read()`` is still current, so a delayed
    read from a closed, replaced or reset connection can never leak into a
    later receive generation. Resetting the receive session swaps this
    object's queues and framer in place, which keeps the same open reader
    publishing through the fresh state after it observes the new generation.
    """

    __slots__ = ("framer", "received_queue", "diagnostic_queue", "raw_queue")

    def __init__(
        self,
        clock_ms: Callable[[], int],
        monotonic_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        self.received_queue: queue.Queue = queue.Queue()
        self.diagnostic_queue: queue.Queue = queue.Queue()
        self.raw_queue: queue.Queue = queue.Queue()
        self.framer = ReceiveFramer(
            clock_ms, on_overflow=self._on_overflow, monotonic_ns=monotonic_ns
        )

    def _on_overflow(self) -> None:
        # Reads the current queue at call time so a reset that swapped the
        # queue is honored. Only queue.put: no Qt, no file I/O, no logging.
        self.diagnostic_queue.put(_OVERFLOW_DIAGNOSTIC)


class SerialController:
    """Owns one serial connection and its background reader thread."""

    _READ_CHUNK = 4096
    _READ_POLL = 0.01  # fake serials have no in_waiting; avoid busy-waiting
    _JOIN_TIMEOUT = 0.2  # bound GUI shutdown latency if a driver ignores cancel_read

    def __init__(
        self,
        serial_factory: Optional[Callable[..., Any]] = None,
        port_lister: Optional[Callable[[], List[Any]]] = None,
        clock_ms: Optional[Callable[[], int]] = None,
        monotonic_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        if serial_factory is None:
            serial_factory = _serial.Serial if _serial is not None else None
        if port_lister is None:
            port_lister = (
                _list_ports.comports if _list_ports is not None else (lambda: [])
            )
        if clock_ms is None:
            clock_ms = _epoch_ms
        if monotonic_ns is None:
            monotonic_ns = time.monotonic_ns
        self._factory = serial_factory
        self._port_lister = port_lister
        self._clock_ms = clock_ms
        self._monotonic_ns = monotonic_ns
        self.received_queue: queue.Queue = queue.Queue()
        self.diagnostic_queue: queue.Queue = queue.Queue()
        self.raw_queue: queue.Queue = queue.Queue()
        self.error_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ser: Any = None
        self._reader: Optional[threading.Thread] = None
        self._session_lock = threading.Lock()
        self._session: Optional[_ReceiveSession] = None
        self._generation = 0
        self._raw_mode = False
        self._is_open = False

    # -- state -------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        with self._session_lock:
            return self._is_open

    # -- enumeration -------------------------------------------------------

    def available_ports(self) -> List[Any]:
        """Enumerate ports via the (injected or pyserial) port lister."""
        return list(self._port_lister())

    def list_ports(self) -> List[Any]:
        """Compatibility alias for ``available_ports``.

        A GUI fake may return plain string lists; callers are free to handle
        that themselves, so the lister output is passed through unchanged.
        """
        return self.available_ports()

    # -- connection --------------------------------------------------------

    def open(self, settings: SerialSettings) -> None:
        # Also cleans up a reader that failed asynchronously and already set
        # is_open=False. close() is idempotent for a never-opened controller.
        self.close()
        if self._factory is None:
            raise SerialConnectionError(
                "pyserial is not installed; inject a serial_factory"
            )

        ser: Any = None
        try:
            ser = self._factory(
                port=settings.port,
                baudrate=settings.baudrate,
                bytesize=settings.bytesize,
                parity=settings.parity,
                stopbits=settings.stopbits,
                timeout=0.1,
            )
        except Exception as exc:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            raise SerialConnectionError(
                f"could not open serial port {settings.port!r}: {exc}"
            ) from exc
        if ser is None:
            raise SerialConnectionError(
                f"serial factory returned no port for {settings.port!r}"
            )

        # Clear the driver's pending input before the new reader can start:
        # bytes sent while stopped must never be replayed after a resume.
        reset_input = getattr(ser, "reset_input_buffer", None)
        if callable(reset_input):
            try:
                reset_input()
            except Exception as exc:
                try:
                    ser.close()
                except Exception:
                    pass
                raise SerialConnectionError(
                    f"could not clear serial input buffer for {settings.port!r}: {exc}"
                ) from exc

        # The reader keeps a direct reference to its session (framer + queues)
        # and publishes only reads whose start generation is still current, so
        # data from a closed, replaced or cleared connection can never reach a
        # later one.
        error_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        with self._session_lock:
            self._generation += 1
            session = _ReceiveSession(self._clock_ms, self._monotonic_ns)
            reader = threading.Thread(
                target=self._reader_loop,
                args=(ser, stop_event, session, error_queue),
                name="serial-reader",
                daemon=True,
            )
            self._session = session
            self.received_queue = session.received_queue
            self.diagnostic_queue = session.diagnostic_queue
            self.raw_queue = session.raw_queue
            self.error_queue = error_queue
            self._stop = stop_event
            self._ser = ser
            self._reader = reader
            self._is_open = True
        try:
            reader.start()
        except Exception as exc:
            stop_event.set()
            with self._session_lock:
                if self._reader is reader:
                    self._reader = None
                if self._ser is ser:
                    self._ser = None
                    self._is_open = False
                if self._session is session:
                    self._generation += 1
                    self._session = None
            try:
                ser.close()
            except Exception:
                pass
            raise SerialConnectionError(
                f"could not start serial reader for {settings.port!r}: {exc}"
            ) from exc

    def close(self) -> None:
        with self._session_lock:
            if (
                not self._is_open
                and self._ser is None
                and self._reader is None
                and self._session is None
            ):
                return
            # Invalidate the session before the reader can publish again: a
            # delayed read from this connection must not reach any later one.
            self._generation += 1
            self._session = None
            stop_event = self._stop
            stop_event.set()
            ser = self._ser
            reader = self._reader
            self._ser = None
            self._reader = None
            self._is_open = False

        if ser is not None:
            cancel_read = getattr(ser, "cancel_read", None)
            if callable(cancel_read):
                try:
                    cancel_read()
                except Exception:
                    pass
            try:
                ser.close()
            except Exception:
                pass
        if (
            reader is not None
            and reader is not threading.current_thread()
            and reader.is_alive()
        ):
            reader.join(timeout=self._JOIN_TIMEOUT)

    def write(self, data: bytes) -> None:
        with self._session_lock:
            if not self._is_open or self._ser is None:
                raise SerialConnectionError("serial port is not open")
            ser = self._ser
        try:
            ser.write(data)
        except Exception as exc:
            # A failed write leaves the link in an unknown state. Drop the
            # connection and stop the reader so callers never observe a
            # fake-open controller, then surface the original error.
            self._close_after_failed_write(ser)
            raise SerialConnectionError(f"serial write failed: {exc}") from exc

    def _close_after_failed_write(self, ser: Any) -> None:
        """Tear down the connection that failed a write (identity-checked).

        Only closes state that still belongs to ``ser``: if a newer connection
        was opened concurrently, it is left untouched. The reader is stopped
        with a bounded join to keep shutdown latency predictable.
        """
        with self._session_lock:
            if self._ser is not ser:
                return
            self._generation += 1
            self._session = None
            stop_event = self._stop
            stop_event.set()
            reader = self._reader
            self._ser = None
            self._reader = None
            self._is_open = False
        # A driver may park the OS read until explicitly cancelled; close()
        # alone would leave the reader blocked, so the join below could never
        # complete. Release the read first, exactly like close() does.
        cancel_read = getattr(ser, "cancel_read", None)
        if callable(cancel_read):
            try:
                cancel_read()
            except Exception:
                pass
        try:
            ser.close()
        except Exception:
            pass
        if (
            reader is not None
            and reader is not threading.current_thread()
            and reader.is_alive()
        ):
            reader.join(timeout=self._JOIN_TIMEOUT)

    # -- session reset -----------------------------------------------------

    def set_raw_mode(self, enabled: bool) -> None:
        """Select raw-bytes receipt instead of ``0D 0A`` framing (REQ-0004 §3.4).

        The flag lives on the controller so a later ``open()`` reuses it. A
        real mode change also drops the framer's unfinished tail, because
        bytes received while the other mode was active never reached the
        framer and must not be joined with the pre-switch tail.
        """
        enabled = bool(enabled)
        with self._session_lock:
            if enabled == self._raw_mode:
                return
            self._raw_mode = enabled
            if self._session is not None:
                self._session.framer.reset()

    @property
    def raw_mode(self) -> bool:
        """Current receipt mode: ``True`` for raw bytes, ``False`` for framing."""
        with self._session_lock:
            return self._raw_mode

    def reset_receive_session(self) -> Optional[int]:
        """Atomically drop the current session's framing state and queued data.

        This is the linearization point between "before clear" and "after
        clear" data (REQ-0003 §10.1): it bumps the generation, resets the
        framer and replaces both receive queues, discarding every item still
        pending in the old queue objects. Safe while closed: it never touches
        the serial port.

        Returns the new acquisition origin (monotonic ns) captured inside
        this same lock while the connection is open, or ``None`` when closed.
        The waveform page uses it to re-anchor without a second clock read;
        other callers may ignore it (REQ-0005 §9.2).

        All three replacement queues are allocated before the lock, so an
        allocation failure raises without mutating the session: the old
        generation, queue identities, framer tail and pending data all stay
        valid and the caller can report the clear as failed.
        """
        new_received = _new_queue()
        new_diagnostic = _new_queue()
        new_raw = _new_queue()
        with self._session_lock:
            origin_ns = self._monotonic_ns() if self._is_open else None
            self._generation += 1
            old_received = self.received_queue
            old_diagnostic = self.diagnostic_queue
            old_raw = self.raw_queue
            self.received_queue = new_received
            self.diagnostic_queue = new_diagnostic
            self.raw_queue = new_raw
            session = self._session
            if session is not None:
                session.received_queue = new_received
                session.diagnostic_queue = new_diagnostic
                session.raw_queue = new_raw
                session.framer.reset()
            _discard_pending(old_received)
            _discard_pending(old_diagnostic)
            _discard_pending(old_raw)
            return origin_ns

    # -- background reader -------------------------------------------------

    def _reader_loop(
        self,
        ser: Any,
        stop_event: threading.Event,
        session: _ReceiveSession,
        error_queue: queue.Queue,
    ) -> None:
        """Read one connection, frame it and publish its events.

        Only serial reading, framing and queue publication happen here: no
        file I/O and no logging, so the reader never depends on log state.
        """
        while not stop_event.is_set():
            # Capture the generation before the blocking read: a reset while
            # the read is parked must invalidate the whole chunk it returns.
            with self._session_lock:
                read_generation = self._generation
            try:
                data = ser.read(self._READ_CHUNK)
            except Exception as exc:
                if not stop_event.is_set():
                    try:
                        error_queue.put(str(exc))
                    finally:
                        self._mark_reader_failed(ser, stop_event)
                break
            if data:
                if stop_event.is_set():
                    break
                try:
                    self._publish_read(session, data, read_generation)
                except Exception as exc:
                    # 分帧/队列分配失败（含 MemoryError）不得让 reader 抛异常
                    # 退出后仍看似打开：上报异常并关闭这一条连接。
                    if not stop_event.is_set():
                        try:
                            error_queue.put(exc)
                        finally:
                            self._mark_reader_failed(ser, stop_event)
                    break
            else:
                stop_event.wait(self._READ_POLL)

    def _publish_read(
        self, session: _ReceiveSession, data: bytes, read_generation: int
    ) -> None:
        """Publish one read only if its start generation is still current.

        The generation check and the enqueue happen under the same lock as
        ``reset_receive_session``, so a read that began before the clear can
        never cross the linearization point; the reader then captures the new
        generation for its next read and keeps processing normally.
        """
        with self._session_lock:
            if read_generation != self._generation:
                return
            self._publish_events(session, data)

    def _publish_events(self, session: _ReceiveSession, data: bytes) -> None:
        """Publish one read chunk; the caller holds the session lock.

        Raw mode bypasses the framer entirely (REQ-0004 §3.2).
        """
        if self._raw_mode:
            session.raw_queue.put(data)
            return
        for event in session.framer.feed(data):
            session.received_queue.put(event)

    def _mark_reader_failed(self, ser: Any, stop_event: threading.Event) -> None:
        """Move the active controller to closed state after a read failure."""
        with self._session_lock:
            if self._ser is not ser or self._stop is not stop_event:
                return
            self._generation += 1
            self._session = None
            self._ser = None
            self._is_open = False
            stop_event.set()
        try:
            ser.close()
        except Exception:
            pass
