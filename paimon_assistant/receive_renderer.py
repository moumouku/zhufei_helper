"""把接收事件渲染为显示文本（REQ-0003 §6，无 Qt 依赖）。

公共接口：``format_timestamp_local`` / ``render_event`` / ``render_events``。
渲染是纯函数：不修改事件，不持有状态。
"""

from __future__ import annotations

from datetime import datetime

from .codec import decode_text, format_hex


def format_timestamp_local(received_at_ms: int) -> str:
    """Unix epoch 毫秒 -> 本机时区 ``HH:mm:ss.SSS``。"""
    seconds, millis = divmod(received_at_ms, 1000)
    local = datetime.fromtimestamp(seconds)
    return f"{local:%H:%M:%S}.{millis:03d}"


def render_event(event, mode: str, encoding: str, show_timestamp: bool) -> str:
    """渲染单个事件为一行显示文本（不含换行）。"""
    if mode == "text":
        body = decode_text(event.payload, encoding)
    else:  # HEX：显示完整 raw_frame，含末尾 0D 0A
        body = format_hex(event.raw_frame)
    if show_timestamp:
        return f"[{format_timestamp_local(event.received_at_ms)}] {body}"
    return body


def render_events(events, mode: str, encoding: str, show_timestamp: bool) -> str:
    """按顺序渲染全部事件，每个事件后跟一个换行（含最后一个）。"""
    return "".join(
        render_event(event, mode, encoding, show_timestamp) + "\n" for event in events
    )
