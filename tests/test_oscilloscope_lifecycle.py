"""波形页生命周期、接收所有权与连接边界测试（REQ-0005 issue 014）。

只通过公开行为验证：注入单调时钟、墙上时间与可控 fake serial / fake
controller；停止与恢复使用真实控制器网关（gate）同步，不依赖 sleep 推导
精确时间戳，也不断言与 014 无关的协议细节（归 issue 013）。
"""

from __future__ import annotations

import importlib
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.oscilloscope import (  # noqa: E402
    OscilloscopeConnectionBoundary,
    OscilloscopeSession,
    format_connection_boundary_line,
)
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from paimon_assistant.serial_controller import (  # noqa: E402
    SerialController,
    SerialSettings,
)

RAW_MODE = "原始字节"
FRAMED_MODE = "按 \\r\\n 分帧"


class StepClock:
    """可手动推进的注入式时钟：单调时间与墙上时间分别可设。"""

    def __init__(
        self,
        monotonic_ns: int = 1_000_000_000,
        wall_ms: int = 1_700_000_000_000,
    ) -> None:
        self.monotonic_ns = monotonic_ns
        self.wall_ms = wall_ms

    def mono(self) -> int:
        return self.monotonic_ns

    def wall(self) -> int:
        return self.wall_ms


class SteppingClock:
    """每次读取自动前进固定步长，用于固定时间原点的采样顺序。"""

    def __init__(
        self,
        start_ns: int = 1_000_000_000,
        step_ns: int = 1_000_000_000,
    ) -> None:
        self.now_ns = start_ns
        self.step_ns = step_ns

    def mono(self) -> int:
        value = self.now_ns
        self.now_ns += self.step_ns
        return value

    def wall(self) -> int:
        return 1_700_000_000_000


@dataclass(frozen=True)
class BoundaryEvent:
    """测试替身：完整帧在接收线程边界记录的双时间戳。"""

    monotonic_ns: int
    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class ScriptedSerial:
    """pyserial 兼容 fake：按脚本返回读取块，并记录缓冲清理。"""

    def __init__(self, port, chunks=(), **kwargs) -> None:
        self.port = port
        self.chunks = [bytes(chunk) for chunk in chunks]
        self.closed = False
        self.reset_input_calls = 0
        self.written = bytearray()

    def read(self, n=1):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def reset_input_buffer(self):
        self.reset_input_calls += 1

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def close(self):
        self.closed = True


class ScriptedFactory:
    """注入式 serial_factory：每次 open 取一份脚本并记录实例。"""

    def __init__(self, scripts) -> None:
        self._scripts = [list(script) for script in scripts]
        self.instances = []

    def __call__(self, *args, **kwargs):
        chunks = self._scripts.pop(0) if self._scripts else []
        instance = ScriptedSerial(kwargs.get("port", "?"), chunks=chunks)
        self.instances.append(instance)
        return instance


class GatedSerial:
    """pyserial 兼容 fake：read() 停在门控上，由测试 release()/cancel_read() 放行。

    帧边界时间只在 release() 之后被读取，因此测试可以用注入时钟精确
    控制“帧完成时刻”，不依赖 sleep 推导时序。
    """

    def __init__(self, port, **kwargs) -> None:
        self.port = port
        self.closed = False
        self.reset_input_calls = 0
        self.read_starts = 0
        self.written = bytearray()
        self._chunks: queue.Queue = queue.Queue()
        self._release = threading.Event()

    def feed(self, data: bytes) -> None:
        self._chunks.put(bytes(data))

    def feed_error(self, exc: Exception) -> None:
        self._chunks.put(exc)

    def release(self) -> None:
        self._release.set()

    def read(self, n=1):
        self.read_starts += 1
        self._release.wait(timeout=5.0)  # bounded: a broken test cannot hang
        self._release.clear()
        if self.closed:
            return b""
        try:
            item = self._chunks.get_nowait()
        except queue.Empty:
            return b""
        if isinstance(item, Exception):
            raise item
        return item

    def reset_input_buffer(self):
        self.reset_input_calls += 1

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def cancel_read(self):
        self._release.set()

    def close(self):
        self.closed = True
        self._release.set()


class GatedFactory:
    """注入式 serial_factory：每次 open 创建一个 GatedSerial 并记录实例。"""

    def __init__(self) -> None:
        self.instances = []

    def __call__(self, *args, **kwargs):
        instance = GatedSerial(kwargs.get("port", "?"))
        self.instances.append(instance)
        return instance


class FailingFactory:
    """注入式 serial_factory：构造串口就失败（模拟端口被占用）。"""

    def __call__(self, *args, **kwargs):
        raise OSError("device busy")


@pytest.fixture
def silent_dialogs(mw, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(mw.QMessageBox, "warning", staticmethod(lambda *a, **k: None))


class SlowOpenController(SerialController):
    """测试接缝：open() 等首帧已发布后才返回，固定注入时钟的采样顺序。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.first_event_ready = threading.Event()

    def open(self, settings) -> None:
        super().open(settings)
        assert self.first_event_ready.wait(timeout=2.0), "reader never framed a frame"

    def _publish_events(self, session, data) -> None:
        super()._publish_events(session, data)
        if not session.received_queue.empty():
            self.first_event_ready.set()


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


def make_window(qtbot, mw, tmp_path, controller, **kwargs):
    window = mw.MainWindow(
        controller=controller,
        log_service=ReceiveLogService(tmp_path / "logs"),
        **kwargs,
    )
    window._timer.stop()
    qtbot.addWidget(window)
    return window


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def select(combo, text):
    index = combo.findText(text)
    assert index != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(index)
    assert combo.currentText() == text


# -------------------------------------------- 时间原点在打开边界建立


def test_first_waveform_origin_precedes_first_published_frame(qtbot, mw, tmp_path):
    """首帧边界早于 begin_acquisition 时，相对时间也不得为负（012 发现）。"""
    clock = SteppingClock(start_ns=1_000_000_000, step_ns=1_000_000_000)
    factory = ScriptedFactory([[b"12\r\n"]])
    controller = SlowOpenController(
        serial_factory=factory,
        port_lister=lambda: ["COM9"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    window.page_tabs.setCurrentIndex(1)

    window.oscilloscope_page.start_button.click()
    window._drain_queues()

    samples = window.oscilloscope_page.session.samples
    assert len(samples) == 1
    assert samples[0].relative_seconds >= 0.0, "首帧相对时间不得为负"
    assert samples[0].relative_seconds == pytest.approx(1.0)

    window.close()


# -------------------------------------------- 数据页原始字节与波形隔离


def test_data_raw_mode_isolation_across_waveform_session(qtbot, mw, tmp_path):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page

    select(window.parse_mode_combo, RAW_MODE)
    window.open_button.click()
    data_serial = factory.instances[0]
    data_serial.feed(b"ab")
    data_serial.release()
    assert wait_until(lambda: not controller.raw_queue.empty())
    window._drain_queues()
    assert window.display_edit.toPlainText() == "ab"
    window.open_button.click()  # 停止数据页
    assert not controller.is_open

    # 波形页开始：无论数据页选什么解析模式都必须严格 \r\n 分帧解析。
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    wave_serial = factory.instances[1]
    wave_serial.feed(b"12\r\n")
    wave_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert [sample.values for sample in page.session.samples] == [(12,)]
    assert page.ch1_series.count() == 1
    page.start_button.click()  # 停止波形页

    # 数据页模式与历史不变；再次接收仍走原始字节通道，且不重放旧数据。
    assert window.parse_mode_combo.currentText() == RAW_MODE
    assert window.display_edit.toPlainText() == "ab"
    assert controller.raw_mode is True
    window.page_tabs.setCurrentIndex(0)
    window.open_button.click()
    data_serial = factory.instances[2]
    data_serial.feed(b"cd")
    data_serial.release()
    assert wait_until(lambda: not controller.raw_queue.empty())
    window._drain_queues()
    assert window.display_edit.toPlainText() == "abcd"
    window.open_button.click()
    window.close()


def test_waveform_start_failure_restores_data_raw_preference(
    qtbot, mw, tmp_path, silent_dialogs
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller = SerialController(
        serial_factory=FailingFactory(),
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    select(window.parse_mode_combo, RAW_MODE)
    assert controller.raw_mode is True
    window.page_tabs.setCurrentIndex(1)

    window.oscilloscope_page.start_button.click()

    assert not window.oscilloscope_page.is_receiving
    assert window.oscilloscope_page.start_button.text() == "开始接收"
    assert controller.raw_mode is True, "失败的波形启动不得改动数据页模式"
    assert not controller.is_open
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0, "打开失败后必须恢复可切页"
    window.close()


def test_default_controller_shares_window_monotonic_clock_domain(
    qtbot, mw, tmp_path, monkeypatch
):
    captured = {}

    class RecordingController:
        def __init__(self, monotonic_ns=None):
            captured["monotonic_ns"] = monotonic_ns

        def list_ports(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(mw, "SerialController", RecordingController)
    clock = StepClock()
    window = mw.MainWindow(
        log_service=ReceiveLogService(tmp_path / "logs"),
        monotonic_ns=clock.mono,
    )
    window._timer.stop()
    window._poll_timer.stop()
    qtbot.addWidget(window)

    assert captured["monotonic_ns"] == clock.mono
    assert captured["monotonic_ns"] is not None
    window.close()


# -------------------------------------------- 程序性越页启动防护


def test_programmatic_waveform_start_on_data_page_is_ignored(qtbot, mw, tmp_path):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    assert window.page_tabs.currentIndex() == 0

    window._on_waveform_start_clicked()  # 数据页当前：不得越页开波形

    assert not window.oscilloscope_page.is_receiving
    assert not controller.is_open
    assert factory.instances == []
    window.close()


def test_programmatic_data_open_on_waveform_page_is_ignored(qtbot, mw, tmp_path):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    window.page_tabs.setCurrentIndex(1)

    window._on_open_clicked()  # 波形页当前：不得越页开数据接收

    assert not controller.is_open
    assert factory.instances == []
    assert window.open_button.text() == "打开"
    window.close()


# -------------------------------------------- 停止边界丢弃与线性化清空


def test_data_stop_discards_pending_events_and_resume_only_receives_new(
    qtbot, mw, tmp_path
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    window.timestamp_checkbox.setChecked(False)

    window.open_button.click()
    first = factory.instances[0]
    first.feed(b"old\r\n")
    first.release()
    assert wait_until(lambda: not controller.received_queue.empty())

    window.open_button.click()  # 停止：在线性化连接边界丢弃待处理事件
    window._drain_queues()

    assert window.display_edit.toPlainText() == ""

    window.open_button.click()  # 重新开始：只接收重开后进入链路的新数据
    resumed = factory.instances[1]
    assert resumed.reset_input_calls == 1, "新 reader 启动前必须清理驱动输入缓冲"
    resumed.feed(b"new\r\n")
    resumed.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert window.display_edit.toPlainText() == "new\n"
    window.open_button.click()
    window.close()


def test_programmatic_clear_on_data_page_keeps_waveform_history(qtbot, mw, tmp_path):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_250_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    page.start_button.click()  # 停止波形，保留历史

    window.page_tabs.setCurrentIndex(0)
    window._on_waveform_clear_clicked()  # 数据页当前：不得清空波形历史

    assert [sample.values for sample in page.session.samples] == [(12,)]
    assert page.display_edit.toPlainText() == "12"
    window.close()


def test_stopped_clear_next_start_opens_empty_acquisition_without_boundary(
    qtbot, mw, tmp_path
):
    """停止时清空：原点保持 None，下一次成功开始建立新原点且不插伪边界。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_500_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    page.start_button.click()  # 停止：保留历史与原点
    assert page.session.origin_ns == 1_000_000_000

    window._on_waveform_clear_clicked()  # 停止状态清空：不打开串口

    assert not controller.is_open
    assert page.session.origin_ns is None, "停止清空后原点保持未建立"
    assert page.session.records == []
    assert page.session.samples == []

    window.baud_combo.setEditText("9600")  # 停止期间更改设置
    clock.monotonic_ns = 5_000_000_000
    page.start_button.click()  # 下一次成功开始建立新原点

    assert page.session.origin_ns == 5_000_000_000
    boundaries = [
        record
        for record in page.session.records
        if isinstance(record, OscilloscopeConnectionBoundary)
    ]
    assert boundaries == [], "新建的空采集不得插入连接边界"
    assert controller.is_open
    window.close()


def test_active_clear_then_empty_resume_with_changed_settings_has_no_boundary(
    qtbot, mw, tmp_path
):
    """活动清空后采集为空时，即使端口参数变化也不得插入伪连接边界。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_500_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    window._on_waveform_clear_clicked()  # 活动清空：新采集为空
    assert page.is_receiving and controller.is_open
    assert page.session.records == [] and page.session.samples == []

    page.start_button.click()  # 停止
    window.baud_combo.setEditText("9600")
    clock.monotonic_ns = 5_000_000_000
    page.start_button.click()  # 空采集 + 不同设置：无旧数据可混，不插边界

    boundaries = [
        record
        for record in page.session.records
        if isinstance(record, OscilloscopeConnectionBoundary)
    ]
    assert boundaries == [], "新建的空采集不得插入伪连接边界"
    assert page.session.origin_ns == 1_500_000_000
    window.close()


def test_active_clear_then_new_frames_changed_resume_keeps_boundary(
    qtbot, mw, tmp_path
):
    """清空后已有新采集数据时，停止后换设置恢复仍必须记录连接边界。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_500_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    window._on_waveform_clear_clicked()  # 活动清空
    serial.release()  # 清空时阻塞的那次 read 属于清空前，直接丢弃
    assert wait_until(lambda: serial.read_starts >= 3), "清空后 reader 未继续读"
    clock.monotonic_ns = 2_000_000_000
    serial.feed(b"13\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[0].values == (13,)

    page.start_button.click()  # 停止：新采集已有数据
    window.baud_combo.setEditText("9600")
    clock.monotonic_ns = 5_000_000_000
    page.start_button.click()  # 不同设置恢复：必须标明连接变化

    boundaries = [
        record
        for record in page.session.records
        if isinstance(record, OscilloscopeConnectionBoundary)
    ]
    assert len(boundaries) == 1, "已有新采集数据时不得漏掉真实连接边界"
    assert boundaries[0].settings.baudrate == 9600
    assert page.session.origin_ns == 1_500_000_000
    window.close()


def test_active_waveform_clear_keeps_receiving_drops_queue_and_reanchors(
    qtbot, mw, tmp_path
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_250_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[0].relative_seconds == pytest.approx(0.25)

    # 第二帧已入队但尚未处理；清空必须丢弃它且不关闭串口。
    serial.feed(b"13\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    clock.monotonic_ns = 3_000_000_000

    window._on_waveform_clear_clicked()

    assert controller.is_open
    assert page.is_receiving
    assert page.session.records == []
    assert page.session.samples == []
    assert page.session.origin_ns == 3_000_000_000
    window._drain_queues()
    assert page.session.records == [], "清空前待处理帧不得在清空后复现"

    # 清空时仍在阻塞的那次 read 属于清空前：必须丢弃，reader 继续下一次读。
    serial.release()
    assert wait_until(lambda: serial.read_starts >= 4), "清空后 reader 未继续读"
    assert controller.received_queue.empty()

    clock.monotonic_ns = 3_500_000_000
    serial.feed(b"14\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[0].relative_seconds == pytest.approx(0.5)
    assert page.session.samples[0].values == (14,)
    page.start_button.click()
    window.close()


class OriginOnlyController:
    """Fake controller: 会话重置在锁语义内返回固定新原点。"""

    def __init__(self, origin_ns: int) -> None:
        self.origin_ns = origin_ns
        self.ports = ["COM7"]
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.is_open = False
        self.reset_calls = 0

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        self.reset_calls += 1
        return self.origin_ns


def test_waveform_clear_uses_controller_origin_without_second_clock(
    qtbot, mw, tmp_path
):
    """活动清空的 T+0 来自控制器会话锁，页面不再读第二次时钟（issue 019）。"""
    calls = []

    def mono():
        calls.append(True)
        return 1_000_000_000 * len(calls)

    controller = OriginOnlyController(7_000_000_000)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    window._on_waveform_start_clicked()
    calls_before = len(calls)

    window._on_waveform_clear_clicked()

    assert controller.reset_calls == 1
    assert len(calls) == calls_before, "提供控制器原点时不得再读第二次时钟"
    assert page.session.origin_ns == 7_000_000_000
    assert controller.is_open, "活动清空不得关闭串口"
    assert page.is_receiving, "活动清空不得释放接收状态"
    window.close()


class FailingResetController:
    """Fake controller: 会话重置失败（清空线性化点不可用）。"""

    def __init__(self, error: str = "reset failed") -> None:
        self.error = error
        self.ports = ["COM7"]
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.is_open = False
        self.reset_attempts = 0

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        self.reset_attempts += 1
        raise RuntimeError(self.error)


def test_waveform_clear_failure_keeps_history_and_reports(qtbot, mw, tmp_path):
    """清空线性化点失败时不得静默清除页面并声称成功。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller = FailingResetController("reset failed")
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    window._on_waveform_start_clicked()
    page.consume_events(
        [BoundaryEvent(1_250_000_000, 1_700_000_000_000, b"12", b"12\r\n")]
    )
    assert page.session.samples

    window._on_waveform_clear_clicked()

    assert controller.reset_attempts == 1
    assert "清空波形失败" in page.diagnostic_label.text()
    assert "reset failed" in page.diagnostic_label.text()
    assert page.session.samples[0].values == (12,), "失败不得先清页面再宣称成功"
    assert page.session.records, "失败不得丢弃旧完整帧历史"
    assert page.is_receiving
    window.close()


def test_waveform_clear_keeps_data_page_state_and_log_untouched(qtbot, mw, tmp_path):
    """波形清空不影响数据页历史/解析模式/诊断，不删除或改写 RX 日志。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.timestamp_checkbox.setChecked(False)

    # 数据页先接收一帧，形成页面历史与 RX 日志。
    window.open_button.click()
    data_serial = factory.instances[0]
    data_serial.feed(b"data\r\n")
    data_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert window.display_edit.toPlainText() == "data\n"
    window.open_button.click()  # 停止数据页

    window.receive_error_label.setText("数据页诊断")
    window._raw_history.append(b"RAW")
    before_history = list(window._event_history)
    before_text = window.display_edit.toPlainText()
    before_mode = window.parse_mode_combo.currentText()
    before_raw = window._raw_history.raw()
    before_diag = window.receive_error_label.text()
    log_file = next((tmp_path / "logs").iterdir())

    # 波形页接收一帧，然后清空。
    window.page_tabs.setCurrentIndex(1)
    window.oscilloscope_page.start_button.click()
    wave_serial = factory.instances[1]
    clock.monotonic_ns = 1_500_000_000
    wave_serial.feed(b"12\r\n")
    wave_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert wait_until(lambda: wave_serial.read_starts >= 2), "reader 未进入下一次读"
    before_clear_log = log_file.read_bytes()

    window._on_waveform_clear_clicked()

    assert window._event_history == before_history
    assert window.display_edit.toPlainText() == before_text
    assert window.parse_mode_combo.currentText() == before_mode
    assert window._raw_history.raw() == before_raw
    assert window.receive_error_label.text() == before_diag
    assert log_file.read_bytes() == before_clear_log, "清空不得删除或改写既有 RX 日志"

    # 清空后的新完整帧仍按正常规则写日志。
    wave_serial.release()  # 清空时阻塞的读属于清空前
    assert wait_until(lambda: wave_serial.read_starts >= 3)
    clock.monotonic_ns = 2_500_000_000
    wave_serial.feed(b"13\r\n")
    wave_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[0].values == (13,)
    after_log = log_file.read_bytes()
    assert after_log.startswith(before_clear_log)
    assert len(after_log) > len(before_clear_log), "清空后新帧仍正常写日志"
    window.close()


class ClearBoundaryController:
    """Fake controller: reset_receive_session 在会话边界同步发布一条新帧。"""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.ports = ["COM7"]
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.is_open = False
        self.reset_calls = 0

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        self.reset_calls += 1
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        # 会话锁内捕获新原点；会话边界后立即到达的完整帧不得早于该原点。
        origin = self._clock.mono()
        self.received_queue.put(
            BoundaryEvent(origin, 1_700_000_000_000, b"12", b"12\r\n")
        )
        return origin


def test_clear_boundary_frame_never_gets_negative_relative_time(qtbot, mw, tmp_path):
    clock = SteppingClock(start_ns=1_000_000_000, step_ns=1_000_000_000)
    controller = ClearBoundaryController(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    window._on_waveform_start_clicked()
    assert page.is_receiving
    clock.now_ns = 2_000_000_000

    window._on_waveform_clear_clicked()
    window._drain_queues()

    assert len(page.session.samples) == 1
    assert page.session.samples[0].relative_seconds >= 0.0, (
        "清空边界发布的新帧不得得到负的相对时间"
    )
    window.close()


def test_waveform_resume_same_settings_does_not_join_stopped_half_frame(
    qtbot, mw, tmp_path
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    serial.feed(b"AB\r")  # 停止前未完成尾部
    serial.release()
    assert wait_until(lambda: serial.read_starts >= 2), "尾部块未被处理"
    assert controller.received_queue.empty()
    page.start_button.click()  # 停止：旧尾部随会话失效

    page.start_button.click()  # 相同设置恢复
    resumed = factory.instances[1]
    resumed.feed(b"\n")
    resumed.release()
    assert wait_until(lambda: resumed.read_starts >= 2), "恢复后首块未被处理"
    assert controller.received_queue.empty()

    resumed.feed(b"C\r\n")
    resumed.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert [record.payload for record in page.session.records] == [b"\nC"]
    page.start_button.click()
    window.close()


# -------------------------------------------- 异常路径解锁一致性


def test_read_error_while_waveform_receiving_unlocks_pages(
    qtbot, mw, tmp_path, silent_dialogs
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]
    assert not window.page_tabs.tabBar().isEnabled()

    serial.feed_error(OSError("device removed"))
    serial.release()
    assert wait_until(lambda: not controller.error_queue.empty())
    window._drain_queues()

    assert not controller.is_open
    assert not page.is_receiving
    assert page.start_button.text() == "开始接收"
    assert window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    assert window.open_button.text() == "打开"
    window.close()


def test_write_error_while_waveform_receiving_unlocks_pages(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    assert not window.page_tabs.tabBar().isEnabled()

    def fail_write(data):
        raise OSError("write failed")

    monkeypatch.setattr(controller, "write", fail_write)
    window.send_edit.setText("ping")
    window.send_button.click()

    assert not controller.is_open
    assert not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    window.close()


class CloseFailingController:
    """Fake controller whose physical close fails; window state must still unlock."""

    def __init__(self) -> None:
        self.ports = ["COM7"]
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.is_open = False
        self.close_calls = 0

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        self.is_open = True

    def close(self):
        self.close_calls += 1
        raise OSError("close failed")

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()


def test_close_failure_still_unlocks_pages(qtbot, mw, tmp_path):
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller = CloseFailingController()
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    assert page.is_receiving
    assert not window.page_tabs.tabBar().isEnabled()

    page.start_button.click()  # 停止：close() 失败也不得留下假接收状态

    assert not page.is_receiving
    assert page.start_button.text() == "开始接收"
    assert window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    assert window.open_button.text() == "打开"
    window.close()


# -------------------------------------------- 清空重置（012 修复）


def test_page_clear_while_stopped_clears_history_and_releases_origin(qtbot):
    clock = StepClock(monotonic_ns=1_000_000_000)
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)

    page.begin_acquisition()
    clock.monotonic_ns = 1_200_000_000
    page.consume_events(
        [BoundaryEvent(clock.monotonic_ns, 1_700_000_000_200, b"12", b"12\r\n")]
    )
    page.end_acquisition()

    page.clear_acquisition()

    assert page.session.records == []
    assert page.session.samples == []
    assert page.session.origin_ns is None
    assert page.display_edit.toPlainText() == ""
    assert not page.is_receiving


def test_page_clear_with_supplied_origin_does_not_read_its_own_clock(qtbot):
    """活动清空使用控制器锁内捕获的原点，不再读第二次时钟（issue 019）。"""
    calls = []

    def mono():
        calls.append(True)
        return 1_000_000_000 * len(calls)

    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=mono))
    qtbot.addWidget(page)
    page.begin_acquisition(2_000_000_000)
    page.consume_events(
        [BoundaryEvent(2_500_000_000, 1_700_000_000_200, b"12", b"12\r\n")]
    )

    page.clear_acquisition(origin_ns=9_000_000_000)

    assert calls == [], "提供原点时清空不得再读第二次时钟"
    assert page.session.origin_ns == 9_000_000_000
    assert page.session.records == []
    assert page.session.samples == []


def test_page_clear_while_receiving_reanchors_origin_at_clear(qtbot):
    clock = StepClock(monotonic_ns=1_000_000_000)
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)

    page.begin_acquisition()
    clock.monotonic_ns = 1_100_000_000
    page.consume_events(
        [BoundaryEvent(clock.monotonic_ns, 1_700_000_000_100, b"12", b"12\r\n")]
    )
    assert page.session.origin_ns == 1_000_000_000

    clock.monotonic_ns = 2_000_000_000
    page.clear_acquisition()

    assert page.is_receiving
    assert page.session.records == []
    assert page.session.samples == []
    assert page.session.origin_ns == 2_000_000_000, "清空须以清空时刻重设 T+0"

    clock.monotonic_ns = 2_500_000_000
    page.consume_events(
        [BoundaryEvent(clock.monotonic_ns, 1_700_000_001_000, b"13", b"13\r\n")]
    )
    assert page.session.samples[0].relative_seconds == pytest.approx(0.5)


# -------------------------------------------- 连接边界记录


def make_settings(port="COM7", baudrate=115200, data_bits=8, parity="N", stop_bits=1):
    return SerialSettings(
        port=port,
        baudrate=baudrate,
        data_bits=data_bits,
        parity=parity,
        stop_bits=stop_bits,
    )


def test_connection_boundary_records_relative_time_and_full_settings():
    clock = StepClock(monotonic_ns=1_000_000_000)
    session = OscilloscopeSession(monotonic_ns=clock.mono)
    session.begin_acquisition()
    settings = make_settings("COM4", 460800, 7, "E", 2)

    record = session.note_connection_boundary(settings, at_ns=5_000_000_000)

    assert record.relative_seconds == pytest.approx(4.0)
    assert record.settings is settings
    assert session.records == [record]
    label = format_connection_boundary_line(record)
    assert "T+" not in label, "连接边界提示不得显示相对时间"
    assert "连接边界" in label
    assert "COM4" in label
    assert "460800" in label
    assert "7E2" in label


# -------------------------------------------- 停止/恢复与重连边界


def test_waveform_resume_with_same_settings_keeps_origin_history_and_channels(
    qtbot, mw, tmp_path
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_250_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[0].relative_seconds == pytest.approx(0.25)

    page.start_button.click()  # 停止：保留历史，只关闭连接
    assert not page.is_receiving
    assert page.session.origin_ns == 1_000_000_000

    clock.monotonic_ns = 4_000_000_000
    page.start_button.click()  # 相同设置恢复：同一采集会话，不插边界
    resumed = factory.instances[1]
    window._drain_queues()

    boundaries = [
        record
        for record in page.session.records
        if isinstance(record, OscilloscopeConnectionBoundary)
    ]
    assert boundaries == [], "相同设置恢复不得插入连接边界"
    assert [sample.values for sample in page.session.samples] == [(12,)]
    assert page.session.origin_ns == 1_000_000_000
    assert page.channel_labels["CH1"].text() == "CH1  12"

    clock.monotonic_ns = 4_500_000_000
    resumed.feed(b"13\r\n")
    resumed.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[-1].relative_seconds == pytest.approx(3.5)
    assert page.session.samples[-1].values == (13,)

    window.close()


def test_waveform_resume_with_changed_settings_inserts_connection_boundary(
    qtbot, mw, tmp_path
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: ["COM7"],
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_250_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    page.start_button.click()  # 停止

    window.baud_combo.setEditText("9600")  # 停止期间更改串口参数
    clock.monotonic_ns = 5_000_000_000
    page.start_button.click()  # 恢复：新连接参数 → 连接边界

    boundaries = [
        record
        for record in page.session.records
        if isinstance(record, OscilloscopeConnectionBoundary)
    ]
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary.relative_seconds == pytest.approx(4.0)
    assert boundary.settings.port == "COM7"
    assert boundary.settings.baudrate == 9600
    assert (boundary.settings.data_bits, boundary.settings.parity) == (8, "N")
    assert boundary.settings.stop_bits == 1
    assert "连接边界" not in page.display_edit.toPlainText(), (
        "机器生成的连接边界不得进入接收正文"
    )
    assert "连接边界" in page.connection_boundary_label.text()
    assert "COM7" in page.connection_boundary_label.text()
    assert "T+" not in page.connection_boundary_label.text()

    # 历史、原点与通道保留；新帧继续按原原点计时。
    assert page.session.origin_ns == 1_000_000_000
    assert page.session.samples[0].values == (12,)
    resumed = factory.instances[1]
    clock.monotonic_ns = 5_500_000_000
    resumed.feed(b"13\r\n")
    resumed.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert page.session.samples[-1].relative_seconds == pytest.approx(4.5)
    assert page.session.records[-1].values == (13,)

    window.close()


def test_unplug_while_waveform_receiving_unlocks_pages(
    qtbot, mw, tmp_path, silent_dialogs
):
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller = ClearBoundaryController(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    window._on_waveform_start_clicked()
    assert page.is_receiving
    assert not window.page_tabs.tabBar().isEnabled()

    controller.ports = []  # 热拔插：端口从监测列表消失
    window._monitor.tick()
    window._monitor.tick()

    assert not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    window.close()
