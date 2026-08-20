"""Serial configuration constants and validated settings model.

Contract (docs/requirements/REQ-0001-serial-raw-io.md):
- BAUD_RATES: the 8 standard tiers; arbitrary manual input is also legal.
- SerialSettings defaults: port='', baudrate=115200, data_bits=8, parity='N',
  stop_bits=1. Empty port is allowed; every other illegal parameter raises
  ValueError. The constructor also accepts the pyserial-style aliases
  ``bytesize`` / ``stopbits`` (exposed as read-only properties).
"""

from __future__ import annotations

BAUD_RATES: tuple = (
    9600,
    19200,
    38400,
    57600,
    115200,
    230400,
    460800,
    921600,
)

_VALID_DATA_BITS = (5, 6, 7, 8)
_VALID_PARITY = frozenset("NEOMS")  # pyserial uppercase serial letters
_VALID_STOP_BITS = (1, 1.5, 2)

# Sentinel distinguishing "keyword not given" from an explicit None,
# which must be rejected as an illegal value.
_MISSING = object()


class SerialSettings:
    """Validated serial port parameters.

    ``data_bits``/``stop_bits`` are the canonical names; the pyserial-style
    ``bytesize``/``stopbits`` keywords are accepted as aliases and exposed as
    read-only properties.
    """

    __slots__ = ("port", "baudrate", "data_bits", "parity", "stop_bits")

    def __init__(
        self,
        port: str = "",
        baudrate: int = 115200,
        data_bits: object = _MISSING,
        parity: str = "N",
        stop_bits: object = _MISSING,
        bytesize: object = _MISSING,
        stopbits: object = _MISSING,
    ) -> None:
        if bytesize is not _MISSING:
            if data_bits is not _MISSING:
                raise ValueError("data_bits and bytesize are aliases: provide only one")
            data_bits = bytesize
        if stopbits is not _MISSING:
            if stop_bits is not _MISSING:
                raise ValueError("stop_bits and stopbits are aliases: provide only one")
            stop_bits = stopbits
        if data_bits is _MISSING:
            data_bits = 8
        if stop_bits is _MISSING:
            stop_bits = 1

        if not isinstance(port, str):
            raise ValueError(f"port must be a string, got {port!r}")
        if (
            not isinstance(baudrate, int)
            or isinstance(baudrate, bool)
            or baudrate <= 0
        ):
            raise ValueError(f"baudrate must be a positive int, got {baudrate!r}")
        if (
            not isinstance(data_bits, int)
            or isinstance(data_bits, bool)
            or data_bits not in _VALID_DATA_BITS
        ):
            raise ValueError(
                f"data_bits must be one of {_VALID_DATA_BITS}, got {data_bits!r}"
            )
        if not isinstance(parity, str) or parity not in _VALID_PARITY:
            valid_parity = "/".join(sorted(_VALID_PARITY))
            raise ValueError(
                f"parity must be one of {valid_parity}, got {parity!r}"
            )
        if (
            isinstance(stop_bits, bool)
            or not isinstance(stop_bits, (int, float))
            or stop_bits not in _VALID_STOP_BITS
        ):
            raise ValueError(
                f"stop_bits must be one of {_VALID_STOP_BITS}, got {stop_bits!r}"
            )

        self.port = port
        self.baudrate = baudrate
        self.data_bits = data_bits
        self.parity = parity
        self.stop_bits = stop_bits

    @property
    def bytesize(self) -> int:
        """pyserial-style alias of :attr:`data_bits`."""
        return self.data_bits

    @property
    def stopbits(self) -> float:
        """pyserial-style alias of :attr:`stop_bits`."""
        return self.stop_bits
