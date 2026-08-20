"""Text/HEX encoding helpers and incremental decoding.

Contract: docs/requirements/REQ-0001-serial-raw-io.md.

Behavior:
- parse_hex_input: tokens split by space/comma (mixed allowed), each token
  exactly two hex chars; empty input, odd digit count, 0x prefix, illegal
  chars -> ValueError.
- format_hex: bytes -> uppercase two-digit hex, space separated.
- encode_text/decode_text: utf-8/gbk only, other encodings -> ValueError;
  decode must replace (not raise on) invalid bytes.
- IncrementalTextDecoder(encoding): incremental decode; split multi-byte
  characters across chunk boundaries decode correctly, flush() replaces a
  trailing truncated sequence.
"""

from __future__ import annotations

import codecs
import re

_SUPPORTED_ENCODINGS = ("utf-8", "gbk")
_HEX_TOKEN = re.compile(r"[0-9a-fA-F]{2}")
_SEPARATOR_RE = re.compile(r"[,\s]+")


def parse_hex_input(text: str) -> bytes:
    """Parse a hex string into bytes.

    Tokens are separated by spaces and/or commas (mixed allowed); each token
    must be exactly two hex characters. Empty input, single-digit tokens,
    odd digit counts, ``0x`` prefixes and illegal characters raise ValueError.
    """
    tokens = [t for t in _SEPARATOR_RE.split(text) if t]
    if not tokens:
        raise ValueError("hex input is empty")
    data = bytearray()
    for token in tokens:
        if _HEX_TOKEN.fullmatch(token) is None:
            raise ValueError(f"invalid hex token: {token!r}")
        data.append(int(token, 16))
    return bytes(data)


def format_hex(data: bytes) -> str:
    """Format bytes as uppercase two-digit hex, space separated."""
    return " ".join(f"{b:02X}" for b in data)


def _check_encoding(encoding: str) -> None:
    if encoding not in _SUPPORTED_ENCODINGS:
        raise ValueError(
            f"unsupported encoding {encoding!r}; only {'/'.join(_SUPPORTED_ENCODINGS)} are supported"
        )


def encode_text(text: str, encoding: str) -> bytes:
    """Encode text with utf-8 or gbk; any other encoding raises ValueError."""
    _check_encoding(encoding)
    return text.encode(encoding)


def decode_text(data: bytes, encoding: str) -> str:
    """Decode bytes with utf-8 or gbk; invalid bytes are replaced, not raised."""
    _check_encoding(encoding)
    return data.decode(encoding, errors="replace")


class IncrementalTextDecoder:
    """Incremental text decoder for utf-8/gbk.

    Multi-byte characters split across ``decode()`` chunk boundaries are held
    internally and decoded when the rest arrives; ``flush()`` emits the final
    output, replacing any truncated trailing sequence.
    """

    def __init__(self, encoding: str) -> None:
        _check_encoding(encoding)
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")

    def decode(self, chunk: bytes) -> str:
        """Decode one chunk; returns whatever text is complete so far."""
        return self._decoder.decode(chunk)

    def flush(self) -> str:
        """Finish decoding, replacing any trailing truncated sequence."""
        return self._decoder.decode(b"", final=True)
