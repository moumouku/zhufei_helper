"""Threaded serial-port controller for Paimon Assistant.

The controller owns one serial connection at a time. Every successful open
creates a fresh receive session (a ``ReceiveFramer`` plus its queues); the
reader thread only publishes ``ReceivedEvent`` objects into that session's
queue. ``reset_receive_session()`` swaps the session's framing state under the
session lock, which is the single linearization point between pre-clear and
post-clear data. (REQ-0003 §5.1, §10.1)
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


def _discard_pending(q: queue.Queue) -> None:
    """Drop every item currently queued (used when a session is reset)."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class _ReceiveSession:
    """Framing state shared by the controller and one connection's reader.

    ``generation`` identifies the controller's current receive generation.
    The reader publishes under the session lock only while the generation is
    still current, so a delayed reader from a closed connection can never
    leak into a later session. Resetting the receive session mutates this
    object in place, which keeps the same open reader publishing through the
    fresh framer and queues.
    """

    __slots__ = ("generation", "framer", "received_queue", "diagnostic_queue", "raw_queue")

    def __init__(self, generation: int, clock_ms: Callable[[], int]) -> None:
        self.generation = generation
        self.received_queue: queue.Queue = queue.Queue()
        self.diagnostic_queue: queue.Queue = queue.Queue()
        self.raw_queue: queue.Queue = queue.Queue()
        self.framer = ReceiveFramer(clock_ms, on_overflow=self._on_overflow)

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
    ) -> None:
        if serial_factory is None:
            serial_factory = _serial.Serial if _serial is not None else None
        if port_lister is None:
            port_lister = (
                _list_ports.comports if _list_ports is not None else (lambda: [])
            )
        if clock_ms is None:
            clock_ms = _epoch_ms
        self._factory = serial_factory
        self._port_lister = port_lister
        self._clock_ms = clock_ms
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

        # The reader keeps a direct reference to its session (framer + queues)
        # and publishes only while that session's generation is current, so
        # data from a closed connection can never reach a later one.
        error_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        with self._session_lock:
            self._generation += 1
            session = _ReceiveSession(self._generation, self._clock_ms)
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

    def reset_receive_session(self) -> None:
        """Atomically drop the current session's framing state and queued data.

        This is the linearization point between "before clear" and "after
        clear" data (REQ-0003 §10.1): it bumps the generation, resets the
        framer and replaces both receive queues, discarding every item still
        pending in the old queue objects. Safe while closed: it never touches
        the serial port.
        """
        with self._session_lock:
            self._generation += 1
            old_received = self.received_queue
            old_diagnostic = self.diagnostic_queue
            old_raw = self.raw_queue
            self.received_queue = queue.Queue()
            self.diagnostic_queue = queue.Queue()
            self.raw_queue = queue.Queue()
            session = self._session
            if session is not None:
                session.generation = self._generation
                session.received_queue = self.received_queue
                session.diagnostic_queue = self.diagnostic_queue
                session.raw_queue = self.raw_queue
                session.framer.reset()
            _discard_pending(old_received)
            _discard_pending(old_diagnostic)
            _discard_pending(old_raw)

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
                self._publish_events(session, data)
            else:
                stop_event.wait(self._READ_POLL)

    def _publish_events(self, session: _ReceiveSession, data: bytes) -> None:
        """Publish one read chunk under the session lock, in the active mode.

        Holding the lock across feed + enqueue makes ``reset_receive_session``
        a single linearization point, and the generation check drops output
        from a reader whose connection was closed or replaced mid-read.
        Raw mode bypasses the framer entirely (REQ-0004 §3.2).
        """
        with self._session_lock:
            if session.generation != self._generation:
                return
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
