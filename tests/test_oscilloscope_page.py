"""波形页 offscreen 集成测试（REQ-0005 issue 012）。

覆盖页面初态、接收互斥与切页锁定、单帧显示、CH1 绘图、日志与停止解锁。
串口与时间源均注入：不依赖真实串口、真实睡眠或系统时钟得到精确时序。
"""

from __future__ import annotations

import importlib
import queue
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from PySide6.QtWidgets import QTabBar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.serial_controller import SerialController  # noqa: E402


class StepClock:
    """可推进的注入式时钟：单调时间与墙上时间分别可设。"""

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


@dataclass(frozen=True)
class BoundaryEvent:
    """测试替身：完整帧在接收线程边界记录的双时间戳。"""

    monotonic_ns: int
    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class FakeController:
    """最小控制器替身：记录 open/close/write，提供各接收队列。"""

    def __init__(self, ports=("COM3",)):
        self.ports = list(ports)
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.opened_settings = []
        self.writes = []
        self.close_calls = 0
        self.reset_calls = 0
        self.is_open = False
        self.list_ports_calls = 0

    def list_ports(self):
        self.list_ports_calls += 1
        return list(self.ports)

    def open(self, settings):
        self.opened_settings.append(settings)
        self.is_open = True

    def close(self):
        self.close_calls += 1
        self.is_open = False

    def write(self, data: bytes):
        self.writes.append(bytes(data))

    def set_raw_mode(self, enabled):
        self.raw_mode = enabled

    def reset_receive_session(self):
        self.reset_calls += 1
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


@pytest.fixture
def controller():
    return FakeController()


@pytest.fixture
def window(qtbot, mw, controller, tmp_path):
    win = mw.MainWindow(
        controller=controller, log_service=ReceiveLogService(tmp_path / "logs")
    )
    win._timer.stop()
    qtbot.addWidget(win)
    return win


def _tab_titles(window):
    return [window.page_tabs.tabText(i) for i in range(window.page_tabs.count())]


def _window_with_clock(qtbot, mw, controller, tmp_path, clock, log_service=None):
    win = mw.MainWindow(
        controller=controller,
        log_service=log_service or ReceiveLogService(tmp_path / "logs"),
        monotonic_ns=clock.mono,
    )
    win._timer.stop()
    qtbot.addWidget(win)
    return win


class GatedSerial:
    """pyserial 兼容 fake：read() 从内部队列取块，否则短超时返回空。"""

    def __init__(self, port, **kwargs):
        self.port = port
        self.closed = False
        self._chunks: queue.Queue = queue.Queue()

    def read(self, n=1):
        try:
            return self._chunks.get(timeout=0.01)
        except queue.Empty:
            return b""

    def feed(self, data: bytes) -> None:
        self._chunks.put(bytes(data))

    def write(self, data):
        return len(data)

    def close(self):
        self.closed = True


class GatedFactory:
    """注入式 serial_factory：记录每次 open 得到的 fake 实例。"""

    def __init__(self):
        self.instances = []

    def __call__(self, *args, **kwargs):
        instance = GatedSerial(kwargs.get("port", "?"))
        self.instances.append(instance)
        return instance


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def rx_line(ms: int, raw_frame: bytes) -> str:
    received = datetime.fromtimestamp(ms // 1000)
    return f"[{received:%H:%M:%S}.{ms % 1000:03d}] RX {raw_frame.hex(' ').upper()}"


def _make_real_controller(clock, ports=("COM7",)):
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: list(ports),
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    return controller, factory


# --------------------------------------------------------------- 页面初态


def test_window_starts_on_data_page_with_both_pages_stopped(window, controller):
    assert _tab_titles(window) == ["数据", "波形"]
    assert window.page_tabs.currentIndex() == 0
    assert window.oscilloscope_page.start_button.text() == "开始接收"
    assert not window.oscilloscope_page.is_receiving
    assert not controller.is_open

    window.page_tabs.setCurrentIndex(1)
    assert window.page_tabs.currentIndex() == 1
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    assert window.open_button.text() == "打开"


def test_page_tabs_expose_protocol_help_tooltips(window):
    tab_bar = window.page_tabs.tabBar()
    data_help = tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    waveform_help = tab_bar.tabButton(1, QTabBar.ButtonPosition.RightSide)

    assert data_help is window.data_protocol_help_button
    assert waveform_help is window.waveform_protocol_help_button
    assert data_help.text() == "?"
    assert waveform_help.text() == "?"
    assert data_help.accessibleName() == "数据页协议说明"
    assert waveform_help.accessibleName() == "波形页协议说明"
    assert "按 \\r\\n 分帧" in data_help.toolTip()
    assert "原始字节" in data_help.toolTip()
    assert "temperature=25.6\\r\\n" in data_help.toolTip()
    assert "单片机" in waveform_help.toolTip()
    assert "0.96,328.00\\r\\n" in waveform_help.toolTip()
    assert "-10,+20.5,0030\\r\\n" in waveform_help.toolTip()
    assert "派蒙助手发送" not in data_help.toolTip()
    assert "派蒙助手发送" not in waveform_help.toolTip()


# ------------------------------------------- 单帧显示、CH1 采样与绘图


def test_waveform_page_shows_single_frame_record_sample_and_channel(
    qtbot, mw, controller, tmp_path
):
    clock = StepClock()
    window = _window_with_clock(qtbot, mw, controller, tmp_path, clock)
    page = window.oscilloscope_page
    page.begin_acquisition()

    clock.monotonic_ns = 1_250_000_000  # 帧边界：开始后 250 ms
    page.consume_events(
        [BoundaryEvent(clock.monotonic_ns, 1_700_000_000_250, b"12", b"12\r\n")]
    )

    assert len(page.session.samples) == 1
    sample = page.session.samples[0]
    assert sample.relative_seconds == pytest.approx(0.250)
    assert sample.values == (12,)
    assert page.ch1_series.count() == 1
    point = page.ch1_series.at(0)
    assert point.x() == pytest.approx(0.250)
    assert point.y() == pytest.approx(12.0)
    assert page.channel_labels["CH1"].text() == "CH1  12"
    text = page.display_edit.toPlainText()
    assert text == "12", "接收正文只显示原始载荷文本"
    assert "T+" not in text and "payload=" not in text
    assert page.protocol_status_label.text() == ""


# --------------------------------- 波形页接收：端口、采样、日志端到端


def test_waveform_start_receives_single_frame_into_chart_and_rx_log(qtbot, mw, tmp_path):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _window_with_clock(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    assert page.is_receiving
    assert controller.is_open
    serial = factory.instances[0]

    clock.monotonic_ns = 1_250_000_000  # 帧边界：开始后 250 ms
    clock.wall_ms = 1_700_000_000_250
    serial.feed(b"12\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    samples = page.session.samples
    assert len(samples) == 1
    assert samples[0].relative_seconds == pytest.approx(0.250)
    assert samples[0].values == (12,)
    assert page.ch1_series.count() == 1
    point = page.ch1_series.at(0)
    assert point.x() == pytest.approx(0.250)
    assert point.y() == pytest.approx(12.0)
    assert page.channel_labels["CH1"].text() == "CH1  12"
    assert page.display_edit.toPlainText() == "12"

    log_path = log_service.log_dir / (
        datetime.fromtimestamp(clock.wall_ms // 1000).date().isoformat() + ".txt"
    )
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(clock.wall_ms, b"12\r\n")
    ]

    window.close()


# ------------------------------------ 互斥、停止解锁与历史保留


def test_stop_unlocks_pages_closes_port_discards_pending_and_keeps_history(
    qtbot, mw, tmp_path
):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    window = _window_with_clock(qtbot, mw, controller, tmp_path, clock)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]

    clock.monotonic_ns = 1_100_000_000
    serial.feed(b"12\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert len(page.session.samples) == 1

    # 接收期间页面切换不可用：程序性切换也会被纠正回活动页
    assert not window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 1

    # 第二帧已进入旧连接的队列但尚未处理；停止必须丢弃它
    clock.monotonic_ns = 1_200_000_000
    serial.feed(b"13\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    page.start_button.click()

    assert not page.is_receiving
    assert page.start_button.text() == "开始接收"
    assert not controller.is_open
    assert serial.closed
    assert window.page_tabs.tabBar().isEnabled()
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    assert window.open_button.text() == "打开"

    # 已显示的波形和帧记录保留；停止时丢弃的帧不得补收进任一页面
    assert len(page.session.samples) == 1
    assert page.session.samples[0].values == (12,)
    assert page.ch1_series.count() == 1
    window._drain_queues()
    assert len(page.session.samples) == 1
    assert window.display_edit.toPlainText() == ""
    assert [frame.values for frame in page.session.records] == [(12,)]

    window.close()


# ------------------------------- 共用设置/发送、页面隔离与非法帧


def test_waveform_start_uses_shared_settings_and_shared_send_path(window, controller):
    window.baud_combo.setEditText("460800")
    window.data_bits_combo.setCurrentText("7")
    window.parity_combo.setCurrentText("E")
    window.stop_bits_combo.setCurrentText("1.5")
    window.page_tabs.setCurrentIndex(1)

    window.oscilloscope_page.start_button.click()

    settings = controller.opened_settings[-1]
    assert (
        settings.port,
        settings.baudrate,
        settings.data_bits,
        settings.parity,
        settings.stop_bits,
    ) == ("COM3", 460800, 7, "E", 1.5)
    assert window.send_button.isEnabled()
    window.send_edit.setText("ping")
    window.send_button.click()
    assert controller.writes == [b"ping"]
    window.oscilloscope_page.start_button.click()
    assert not window.oscilloscope_page.is_receiving


def test_waveform_frames_stay_out_of_data_page_and_data_ownership_still_works(
    qtbot, mw, tmp_path
):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    window = _window_with_clock(qtbot, mw, controller, tmp_path, clock)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_100_000_000
    serial.feed(b"12\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    page.start_button.click()
    window.page_tabs.setCurrentIndex(0)

    # 切页不自动开始接收，波形帧也不进入数据页历史
    assert not controller.is_open
    assert window.open_button.text() == "打开"
    assert window.display_edit.toPlainText() == ""
    assert window._event_history == []

    # 数据页接收仍按原契约工作，并锁定切页
    window.open_button.click()
    assert controller.is_open
    assert window.open_button.text() == "关闭"
    assert not window.page_tabs.tabBar().isEnabled()
    clock.monotonic_ns = 1_300_000_000
    factory.instances[1].feed(b"hello\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert "hello" in window.display_edit.toPlainText()
    assert [sample.values for sample in page.session.samples] == [(12,)]
    window.open_button.click()
    assert not controller.is_open
    assert window.page_tabs.tabBar().isEnabled()

    window.close()


def test_invalid_complete_frame_shows_failure_logs_but_produces_no_sample(
    qtbot, mw, tmp_path
):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _window_with_clock(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)

    page.start_button.click()
    clock.monotonic_ns = 1_200_000_000
    clock.wall_ms = 1_700_000_000_250
    factory.instances[0].feed(b"1 2\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert page.session.samples == []
    assert page.ch1_series.count() == 0
    assert len(page.session.records) == 1
    assert page.session.records[0].payload == b"1 2"
    assert page.session.records[0].parse_ok is False
    text = page.display_edit.toPlainText()
    assert text == "1 2", "非法帧仍按原始内容显示在接收正文"
    assert "解析失败" in page.protocol_status_label.text()
    assert page.protocol_status_label.isVisibleTo(page)
    log_path = log_service.log_dir / (
        datetime.fromtimestamp(clock.wall_ms // 1000).date().isoformat() + ".txt"
    )
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(clock.wall_ms, b"1 2\r\n")
    ]

    page.start_button.click()
    window.close()
