"""Threaded serial-port controller for Paimon Assistant.

The controller owns one serial connection at a time. Every successful open
uses fresh receive/error queues and passes those queues directly to its reader
thread, which keeps delayed events from an earlier connection out of a newly
opened session.
"""

from __future__ import annotations

import queue
import threading
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

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


class SerialController:
    """Owns one serial connection and its background reader thread."""

    _READ_CHUNK = 4096
    _READ_POLL = 0.01  # fake serials have no in_waiting; avoid busy-waiting
    _JOIN_TIMEOUT = 0.2  # bound GUI shutdown latency if a driver ignores cancel_read

    def __init__(
        self,
        serial_factory: Optional[Callable[..., Any]] = None,
        port_lister: Optional[Callable[[], List[Any]]] = None,
    ) -> None:
        if serial_factory is None:
            serial_factory = _serial.Serial if _serial is not None else None
        if port_lister is None:
            port_lister = (
                _list_ports.comports if _list_ports is not None else (lambda: [])
            )
        self._factory = serial_factory
        self._port_lister = port_lister
        self.received_queue: queue.Queue = queue.Queue()
        self.error_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ser: Any = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._is_open = False

    # -- state -------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        with self._lock:
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

        # A reader keeps direct references to its session queues. Replacing
        # these before each attempt prevents queued data/errors from a closed
        # connection from affecting a later one, even if an old read unblocks
        # after the new port has opened.
        received_queue: queue.Queue = queue.Queue()
        error_queue: queue.Queue = queue.Queue()
        with self._lock:
            self.received_queue = received_queue
            self.error_queue = error_queue

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

        stop_event = threading.Event()
        reader = threading.Thread(
            target=self._reader_loop,
            args=(ser, stop_event, received_queue, error_queue),
            name="serial-reader",
            daemon=True,
        )
        with self._lock:
            self._stop = stop_event
            self._ser = ser
            self._reader = reader
            self._is_open = True
        try:
            reader.start()
        except Exception as exc:
            stop_event.set()
            with self._lock:
                if self._reader is reader:
                    self._reader = None
                if self._ser is ser:
                    self._ser = None
                    self._is_open = False
            try:
                ser.close()
            except Exception:
                pass
            raise SerialConnectionError(
                f"could not start serial reader for {settings.port!r}: {exc}"
            ) from exc

    def close(self) -> None:
        with self._lock:
            if not self._is_open and self._ser is None and self._reader is None:
                return
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
        with self._lock:
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
        with self._lock:
            if self._ser is not ser:
                return
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

    # -- background reader -------------------------------------------------

    def _reader_loop(
        self,
        ser: Any,
        stop_event: threading.Event,
        received_queue: queue.Queue,
        error_queue: queue.Queue,
    ) -> None:
        """Read one connection and publish only to that connection's queues."""
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
                try:
                    received_queue.put(data)
                except Exception:
                    break
            else:
                stop_event.wait(self._READ_POLL)

    def _mark_reader_failed(self, ser: Any, stop_event: threading.Event) -> None:
        """Move the active controller to closed state after a read failure."""
        with self._lock:
            if self._ser is not ser or self._stop is not stop_event:
                return
            self._ser = None
            self._is_open = False
            stop_event.set()
        try:
            ser.close()
        except Exception:
            pass
