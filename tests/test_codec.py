"""Tests for paimon_assistant.codec and paimon_assistant.receive_buffer (fixed API).

Contract (docs/requirements/REQ-0001-serial-raw-io.md):
- parse_hex_input: tokens split by space/comma (mixed allowed), each token exactly
  two hex chars; empty input, odd digit count, 0x prefix, illegal chars -> ValueError.
- format_hex: bytes -> uppercase two-digit hex, space separated.
- encode_text/decode_text: utf-8/gbk only, other encodings -> ValueError;
  decode must replace (not raise on) invalid bytes.
- IncrementalTextDecoder(encoding): decode(chunk) / flush(); split multi-byte
  characters across chunk boundaries must decode correctly.
- ReceiveBuffer: append/clear/raw/render(mode, encoding); switching text/HEX
  mode or encoding must never lose the accumulated raw bytes.
"""

import pytest

from paimon_assistant.codec import (
    IncrementalTextDecoder,
    decode_text,
    encode_text,
    format_hex,
    parse_hex_input,
)
from paimon_assistant.receive_buffer import ReceiveBuffer


class TestParseHexInput:
    @pytest.mark.parametrize("text,expected", [
        ("01 02 AF", b"\x01\x02\xaf"),
        ("01,02,af", b"\x01\x02\xaf"),
        ("01 02,AF", b"\x01\x02\xaf"),            # space + comma mixed
        ("01, 02 af,03", b"\x01\x02\xaf\x03"),    # comma + space mixed
        ("AB", b"\xab"),
        ("ab", b"\xab"),
        ("00 FF", b"\x00\xff"),
        (" 01 02 ", b"\x01\x02"),                 # surrounding whitespace
    ])
    def test_valid_inputs(self, text, expected):
        assert parse_hex_input(text) == expected

    @pytest.mark.parametrize("text", [
        "",          # empty input
        "   ",       # separators only
        ",,",        # separators only
        "0",         # single digit
        "123",       # odd digit count
        "010",       # odd digit count
        "0 1",       # single-digit token
        "01 2 34",   # single-digit token inside list
        "0x01",      # 0x prefix
        "0x01 02",   # 0x prefix mixed in
        "01,0x02",   # 0x prefix mixed in
        "GG",        # illegal characters
        "0G",
        "gg",
    ])
    def test_invalid_inputs_raise(self, text):
        with pytest.raises(ValueError):
            parse_hex_input(text)


class TestFormatHex:
    def test_uppercase_two_digit_space_separated(self):
        assert format_hex(b"\x01\x02\xaf") == "01 02 AF"

    def test_zero_byte(self):
        assert format_hex(b"\x00") == "00"

    def test_empty_bytes(self):
        assert format_hex(b"") == ""

    def test_roundtrip_all_256_byte_values(self):
        data = bytes(range(256))
        assert parse_hex_input(format_hex(data)) == data


class TestEncodeText:
    def test_utf8(self):
        assert encode_text("中文abc", "utf-8") == "中文abc".encode("utf-8")

    def test_gbk(self):
        assert encode_text("中文abc", "gbk") == "中文abc".encode("gbk")

    def test_empty_text(self):
        assert encode_text("", "utf-8") == b""

    @pytest.mark.parametrize("encoding", [
        "ascii", "latin-1", "utf-16", "big5", "utf-8-sig", "",
    ])
    def test_unsupported_encoding_raises(self, encoding):
        with pytest.raises(ValueError):
            encode_text("x", encoding)


class TestDecodeText:
    def test_utf8(self):
        assert decode_text("中文".encode("utf-8"), "utf-8") == "中文"

    def test_gbk(self):
        assert decode_text("中文".encode("gbk"), "gbk") == "中文"

    def test_empty_data(self):
        assert decode_text(b"", "utf-8") == ""

    def test_plain_ascii_bytes(self):
        assert decode_text(b"hello", "utf-8") == "hello"

    def test_invalid_utf8_replaced_not_raised(self):
        assert decode_text(b"\xff\xfe\x01", "utf-8") == "\ufffd\ufffd\x01"

    def test_truncated_utf8_sequence_replaced(self):
        assert decode_text(b"\xe4\xb8", "utf-8") == "\ufffd"

    def test_truncated_gbk_lead_byte_replaced(self):
        assert decode_text(b"\xd6", "gbk") == "\ufffd"

    @pytest.mark.parametrize("encoding", [
        "ascii", "latin-1", "utf-16", "big5", "utf-8-sig", "",
    ])
    def test_unsupported_encoding_raises(self, encoding):
        with pytest.raises(ValueError):
            decode_text(b"x", encoding)


class TestIncrementalTextDecoder:
    def test_utf8_chinese_split_across_chunks(self):
        dec = IncrementalTextDecoder("utf-8")
        out = dec.decode(b"\xe4\xb8") + dec.decode(b"\xad\xe6\x96\x87") + dec.flush()
        assert out == "中文"

    def test_utf8_split_every_byte(self):
        dec = IncrementalTextDecoder("utf-8")
        chunks = [b"\xe4", b"\xb8", b"\xad", b"\xe6", b"\x96", b"\x87"]
        out = "".join(dec.decode(c) for c in chunks) + dec.flush()
        assert out == "中文"

    def test_gbk_chinese_split_across_chunks(self):
        dec = IncrementalTextDecoder("gbk")
        out = dec.decode(b"\xd6") + dec.decode(b"\xd0") + dec.flush()
        assert out == "中"

    def test_complete_sequence_in_single_chunk(self):
        dec = IncrementalTextDecoder("utf-8")
        assert dec.decode("中".encode("utf-8")) + dec.flush() == "中"

    def test_multiple_chunks_with_mixed_content(self):
        dec = IncrementalTextDecoder("utf-8")
        out = (dec.decode(b"ab")
               + dec.decode(b"c")
               + dec.decode(b"\xe4\xb8\xad")
               + dec.decode(b"!")
               + dec.flush())
        assert out == "abc中!"

    def test_empty_chunk_and_empty_flush(self):
        dec = IncrementalTextDecoder("utf-8")
        assert dec.decode(b"") == ""
        assert dec.flush() == ""

    def test_invalid_bytes_replaced_not_raised(self):
        dec = IncrementalTextDecoder("utf-8")
        out = dec.decode(b"\xff") + dec.decode(b"a") + dec.flush()
        assert out == "\ufffda"

    @pytest.mark.parametrize("encoding", ["ascii", "latin-1", "utf-16", ""])
    def test_unsupported_encoding_raises(self, encoding):
        with pytest.raises(ValueError):
            IncrementalTextDecoder(encoding)


class TestReceiveBuffer:
    def test_empty_buffer(self):
        buf = ReceiveBuffer()
        assert buf.raw() == b""
        assert buf.render("text", "utf-8") == ""
        assert buf.render("hex", "utf-8") == ""

    def test_append_accumulates_raw(self):
        buf = ReceiveBuffer()
        buf.append(b"\x01\x02")
        buf.append(b"\x03")
        assert buf.raw() == b"\x01\x02\x03"

    def test_text_render_utf8(self):
        buf = ReceiveBuffer()
        buf.append("中文".encode("utf-8"))
        assert buf.render("text", "utf-8") == "中文"

    def test_text_render_gbk(self):
        buf = ReceiveBuffer()
        buf.append("中文".encode("gbk"))
        assert buf.render("text", "gbk") == "中文"

    def test_hex_render_uppercase_space_separated(self):
        buf = ReceiveBuffer()
        buf.append(b"\x01\x02\xaf")
        assert buf.render("hex", "utf-8") == "01 02 AF"

    def test_clear_empties_everything(self):
        buf = ReceiveBuffer()
        buf.append(b"\x01\x02")
        buf.clear()
        assert buf.raw() == b""
        assert buf.render("text", "utf-8") == ""
        assert buf.render("hex", "utf-8") == ""

    def test_append_after_clear(self):
        buf = ReceiveBuffer()
        buf.append(b"\x01")
        buf.clear()
        buf.append(b"\x02")
        assert buf.raw() == b"\x02"

    def test_mode_switch_keeps_history(self):
        # bytes received under text mode stay visible after switching to HEX
        buf = ReceiveBuffer()
        buf.append(b"hello")
        assert buf.render("text", "utf-8") == "hello"
        assert buf.render("hex", "utf-8") == "68 65 6C 6C 6F"
        assert buf.render("text", "utf-8") == "hello"

    def test_encoding_switch_keeps_raw_bytes(self):
        # UTF-8 bytes followed by GBK bytes: switching encoding reinterprets
        # the same raw history instead of dropping anything
        buf = ReceiveBuffer()
        buf.append("中".encode("utf-8"))   # e4 b8 ad
        buf.append("文".encode("gbk"))     # ce c4
        raw = buf.raw()
        assert raw == b"\xe4\xb8\xad\xce\xc4"

        assert buf.render("text", "utf-8") == "中\ufffd\ufffd"
        assert buf.render("hex", "utf-8") == "E4 B8 AD CE C4"
        # GBK view reinterprets the whole history: e4 b8 is one GBK char,
        # trailing lead byte ad is replaced, ce c4 is 文
        assert buf.render("text", "gbk") == b"\xe4\xb8".decode("gbk") + "\ufffd" + "文"

        # rendering never mutates the stored raw bytes
        assert buf.raw() == raw

    def test_rendering_is_repeatable(self):
        buf = ReceiveBuffer()
        buf.append(b"\x00\xff")
        assert buf.render("hex", "utf-8") == "00 FF"
        assert buf.render("hex", "utf-8") == "00 FF"

    def test_invalid_mode_raises(self):
        buf = ReceiveBuffer()
        with pytest.raises(ValueError):
            buf.render("bogus", "utf-8")

    @pytest.mark.parametrize("encoding", ["ascii", "latin-1", "utf-16", ""])
    def test_unsupported_encoding_raises(self, encoding):
        buf = ReceiveBuffer()
        with pytest.raises(ValueError):
            buf.render("text", encoding)
