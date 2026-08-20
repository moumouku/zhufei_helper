"""Raw receive buffer with text/HEX rendering.

Contract: docs/requirements/REQ-0001-serial-raw-io.md.
Behavior: append/clear/raw/render(mode, encoding); the accumulated raw bytes
are kept as a bytearray and rendering never mutates them, so switching text/HEX
mode or encoding never loses history.
"""

from __future__ import annotations

from .codec import decode_text, format_hex


class ReceiveBuffer:
    """Accumulates raw received bytes and renders them on demand."""

    def __init__(self) -> None:
        self._data = bytearray()

    def append(self, data: bytes) -> None:
        """Append raw bytes to the history."""
        self._data.extend(data)

    def clear(self) -> None:
        """Drop all accumulated bytes."""
        self._data.clear()

    def raw(self) -> bytes:
        """Snapshot of the accumulated raw bytes."""
        return bytes(self._data)

    def render(self, mode: str, encoding: str) -> str:
        """Render the whole history.

        mode="text" decodes with the given encoding (utf-8/gbk only, invalid
        bytes replaced); mode="hex" formats uppercase two-digit hex, space
        separated. Any other mode or encoding raises ValueError.
        """
        if mode == "text":
            return decode_text(self.raw(), encoding)
        if mode == "hex":
            # Validate the encoding up front so both modes enforce the
            # utf-8/gbk-only contract consistently.
            decode_text(b"", encoding)
            return format_hex(self.raw())
        raise ValueError(f"invalid render mode {mode!r}; expected 'text' or 'hex'")
