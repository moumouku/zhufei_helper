"""Pure-Python serial port monitor (no Qt dependency).

Polls an injected port lister and reports port additions/removals relative to
the last snapshot. The first tick only records the initial snapshot so ports
already present at startup are not reported as newly added.
"""

from __future__ import annotations

from typing import Callable, List, Optional

try:
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover - pyserial not installed
    _list_ports = None


class PortMonitor:
    """Diff port-list snapshots between ticks and drive add/remove events."""

    def __init__(
        self,
        port_lister: Optional[Callable[[], List]] = None,
        on_added: Optional[Callable[[List[str]], None]] = None,
        on_removed: Optional[Callable[[List[str]], None]] = None,
        on_lost: Optional[Callable[[str], None]] = None,
    ) -> None:
        if port_lister is None:
            # 默认与 SerialController 同一数据源
            port_lister = (
                _list_ports.comports if _list_ports is not None else (lambda: [])
            )
        self._port_lister = port_lister
        self._on_added = on_added
        self._on_removed = on_removed
        self._on_lost = on_lost
        self._last: Optional[List[str]] = None
        self._connected_port: Optional[str] = None
        self._missing_ticks = 0

    @staticmethod
    def _port_name(item) -> str:
        """兼容 str / 带 .port 或 .device 属性的条目（如 pyserial ListPortInfo）。"""
        if isinstance(item, str):
            return item
        for attr in ("port", "device"):
            value = getattr(item, attr, None)
            if value:
                return str(value)
        return str(item)

    def set_connected(self, port: str) -> None:
        """开始跟踪当前连接端口，并清零缺失去抖计数。"""
        self._connected_port = self._port_name(port)
        self._missing_ticks = 0

    def clear_connected(self) -> None:
        """停止跟踪当前连接端口。"""
        self._connected_port = None
        self._missing_ticks = 0

    def tick(self) -> None:
        """枚举一次、驱动差量事件，并检查连接端口拔出去抖。"""
        try:
            current = [self._port_name(e) for e in self._port_lister()]
        except Exception:
            return  # 枚举失败：保留快照，不把查询失败当成拔出

        if self._last is None:
            self._last = current
        else:
            added = [p for p in current if p not in self._last]
            removed = [p for p in self._last if p not in current]
            self._last = current
            if added and self._on_added is not None:
                self._on_added(added)
            if removed and self._on_removed is not None:
                self._on_removed(removed)

        if self._connected_port is None:
            return
        if self._connected_port in current:
            self._missing_ticks = 0
            return

        self._missing_ticks += 1
        if self._missing_ticks < 2:
            return

        lost_port = self._connected_port
        self.clear_connected()
        if self._on_lost is not None:
            self._on_lost(lost_port)
