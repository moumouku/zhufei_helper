"""PortMonitor 单元测试。

固定 API 契约（来自 Issue 001 任务书）：
- ``paimon_assistant.port_monitor.PortMonitor(port_lister, on_added=, on_removed=)``
- ``tick()``：枚举一次并与上次快照差量，驱动 ``on_added(ports)`` /
  ``on_removed(ports)`` 事件；事件收到的是端口名（str）列表。
- 首轮 tick 只建立初始化快照，不把启动时已存在的端口误判为新增。
- 相同快照不产生事件；新增事件保持当前枚举顺序、移除事件保持上次
  快照顺序；同一轮先报新增再报移除。
- 注入的 port_lister 抛异常时保留上次快照，不产生事件、不崩溃。

运行：``PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests/test_port_monitor.py -q``
"""

import importlib

import pytest  # noqa: F401


@pytest.fixture
def pm():
    return importlib.import_module("paimon_assistant.port_monitor")


class FakePortLister:
    """注入的 port_lister：返回可变的端口列表，可注入异常。"""

    def __init__(self, ports):
        self.ports = list(ports)
        self.calls = 0
        self.exc = None

    def __call__(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.ports)


class EventRecorder:
    """记录 on_added / on_removed 回调调用序列。"""

    def __init__(self):
        self.calls = []  # ("added"|"removed", [names])

    def on_added(self, ports):
        self.calls.append(("added", list(ports)))

    def on_removed(self, ports):
        self.calls.append(("removed", list(ports)))


def make_monitor(pm, ports, recorder):
    return pm.PortMonitor(
        port_lister=FakePortLister(ports),
        on_added=recorder.on_added,
        on_removed=recorder.on_removed,
    )


# ---------- 初始化快照 ----------


def test_first_tick_establishes_snapshot_without_events(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    assert recorder.calls == [], "首轮 tick 不得把启动已有端口误判为新增"


def test_repeated_same_snapshot_produces_no_events(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    monitor.tick()
    assert recorder.calls == []


# ---------- 差量增 / 删事件 ----------


def test_added_ports_emit_on_added_in_enumeration_order(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    monitor._port_lister.ports = ["COM3", "COM4", "COM9", "COM10"]
    monitor.tick()
    assert recorder.calls == [("added", ["COM9", "COM10"])]


def test_removed_port_emits_on_removed(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    monitor._port_lister.ports = ["COM4"]
    monitor.tick()
    assert recorder.calls == [("removed", ["COM3"])]


def test_mixed_change_reports_added_before_removed(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    monitor._port_lister.ports = ["COM4", "COM9"]
    monitor.tick()
    assert recorder.calls == [("added", ["COM9"]), ("removed", ["COM3"])]


def test_removed_order_follows_last_snapshot_order(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["A", "B", "C", "D"], recorder)
    monitor.tick()
    monitor._port_lister.ports = ["A", "C"]
    monitor.tick()
    assert recorder.calls == [("removed", ["B", "D"])]


def test_entries_are_normalized_to_port_names(pm):
    """lister 返回带 .port 属性的对象时，事件收到的是端口名字符串。"""

    class PortItem:
        def __init__(self, port):
            self.port = port

    recorder = EventRecorder()
    monitor = pm.PortMonitor(
        port_lister=FakePortLister([PortItem("COM3")]),
        on_added=recorder.on_added,
        on_removed=recorder.on_removed,
    )
    monitor.tick()
    monitor._port_lister.ports = [PortItem("COM3"), PortItem("COM9")]
    monitor.tick()
    assert recorder.calls == [("added", ["COM9"])]


# ---------- 枚举失败 ----------


def test_lister_exception_keeps_last_snapshot(pm):
    recorder = EventRecorder()
    monitor = make_monitor(pm, ["COM3", "COM4"], recorder)
    monitor.tick()
    monitor._port_lister.exc = OSError("device query failed")
    monitor.tick()
    assert recorder.calls == [], "枚举失败不得产生增/删误报"
    monitor._port_lister.exc = None
    monitor._port_lister.ports = ["COM4", "COM9"]
    monitor.tick()
    assert recorder.calls == [("added", ["COM9"]), ("removed", ["COM3"])]


def test_connected_port_loss_requires_two_missing_ticks(pm):
    lister = FakePortLister(["COM3"])
    events = []
    monitor = pm.PortMonitor(
        port_lister=lister,
        on_removed=lambda ports: events.append(("removed", list(ports))),
        on_lost=lambda port: events.append(("lost", port)),
    )
    monitor.tick()
    monitor.set_connected("COM3")

    lister.ports = []
    monitor.tick()
    assert events == [("removed", ["COM3"])]

    monitor.tick()
    assert events == [("removed", ["COM3"]), ("lost", "COM3")]
    monitor.tick()
    assert events == [("removed", ["COM3"]), ("lost", "COM3")]


def test_connected_port_reappearance_cancels_pending_loss(pm):
    lister = FakePortLister(["COM3"])
    lost = []
    monitor = pm.PortMonitor(port_lister=lister, on_lost=lost.append)
    monitor.tick()
    monitor.set_connected("COM3")

    lister.ports = []
    monitor.tick()
    lister.ports = ["COM3"]
    monitor.tick()
    assert lost == []

    lister.ports = []
    monitor.tick()
    assert lost == []
    monitor.tick()
    assert lost == ["COM3"]


def test_clear_connected_cancels_pending_loss(pm):
    lister = FakePortLister(["COM3"])
    lost = []
    monitor = pm.PortMonitor(port_lister=lister, on_lost=lost.append)
    monitor.tick()
    monitor.set_connected("COM3")

    lister.ports = []
    monitor.tick()
    monitor.clear_connected()
    monitor.tick()
    monitor.tick()
    assert lost == []
