"""接收事件渲染器行为测试（REQ-0003 §6，issue 007）。

公共接口：``render_event`` / ``render_events`` / ``format_timestamp_local``。
事件按 duck-typing 契约用本地 fake 事件构造，不依赖 receive_framer 实现细节。
时间戳用固定 epoch 毫秒 + 本机时区换算，不依赖真实当前时间。

运行：``.venv/Scripts/python.exe -m pytest tests/test_receive_renderer.py -v``
"""

import time
from dataclasses import dataclass

from paimon_assistant.receive_renderer import (
    format_timestamp_local,
    render_event,
    render_events,
)


@dataclass(frozen=True)
class FakeEvent:
    """received_at_ms / payload / raw_frame 三字段契约（与 ReceivedEvent 一致）。"""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes


def _local_hms(ms: int) -> str:
    """用标准库本地时区把 epoch 毫秒换算成 HH:mm:ss.SSS。"""
    lt = time.localtime(ms // 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms % 1000:03d}"


def _local_midnight_ms(day_offset: int = 0) -> int:
    """本机时区下偏移 day_offset 天的 00:00:00.000 对应 epoch 毫秒。"""
    import datetime

    today = datetime.date(*time.localtime()[:3])
    local_midnight = datetime.datetime.combine(
        today + datetime.timedelta(days=day_offset), datetime.time()
    )
    return int(time.mktime(local_midnight.timetuple())) * 1000


def test_text_mode_without_timestamp_shows_payload_only():
    event = FakeEvent(0, b"Hello", b"Hello\r\n")
    assert render_event(event, "text", "utf-8", False) == "Hello"


def test_timestamp_prefix_is_local_hh_mm_ss_mmm():
    ms = 1_700_000_000_123
    event = FakeEvent(ms, b"Hi", b"Hi\r\n")
    assert render_event(event, "text", "utf-8", True) == f"[{_local_hms(ms)}] Hi"


def test_hex_mode_shows_full_raw_frame_uppercase_with_crlf():
    event = FakeEvent(0, b"Hello", b"Hello\r\n")
    assert render_event(event, "hex", "utf-8", False) == "48 65 6C 6C 6F 0D 0A"


def test_render_events_joins_in_order_with_trailing_newline_per_event():
    events = [
        FakeEvent(0, b"one", b"one\r\n"),
        FakeEvent(1000, b"two", b"two\r\n"),
    ]
    assert render_events(events, "text", "utf-8", False) == "one\ntwo\n"


def test_render_events_of_no_events_is_empty_text():
    assert render_events([], "text", "utf-8", True) == ""


def test_gbk_encoding_decodes_gbk_payload():
    payload = "你好".encode("gbk")
    event = FakeEvent(0, payload, payload + b"\r\n")
    assert render_event(event, "text", "gbk", False) == "你好"


def test_empty_payload_text_mode_shows_only_timestamp():
    event = FakeEvent(1000, b"", b"\r\n")
    assert render_event(event, "text", "utf-8", True) == f"[{_local_hms(1000)}] "


def test_empty_payload_hex_mode_shows_crlf_bytes():
    event = FakeEvent(1000, b"", b"\r\n")
    assert render_event(event, "hex", "utf-8", False) == "0D 0A"


def test_timestamp_format_is_local_midnight_and_last_millisecond():
    midnight_ms = _local_midnight_ms()
    assert format_timestamp_local(midnight_ms) == "00:00:00.000"
    assert format_timestamp_local(midnight_ms - 1) == "23:59:59.999"


def test_rendering_does_not_change_event_bytes_and_is_repeatable():
    payload = "数据".encode("gbk") + b"\x00\xff"
    raw_frame = payload + b"\r\n"
    ms = 1_700_000_000_123
    event = FakeEvent(ms, payload, raw_frame)

    text_render = render_event(event, "text", "gbk", True)
    hex_render = render_event(event, "hex", "utf-8", False)

    assert render_event(event, "text", "gbk", True) == text_render
    assert render_event(event, "hex", "utf-8", False) == hex_render
    assert (event.received_at_ms, event.payload, event.raw_frame) == (
        ms,
        payload,
        raw_frame,
    )
