"""REQ-0005 故障隔离跨功能验收（issue 020）。

只通过公开行为验证资源、日志与连接故障：真实 ``SerialController`` + fake
serial + 注入单调/墙上时钟 + 临时日志目录 + Qt offscreen。覆盖：

1. 绘图资源失败：关闭物理串口、保留已提交采样、解除切页锁、非模态诊断；
2. 波形页清空/新建采集是资源失败的显式恢复路径；
3. 真实控制器的完整波形序列（合法/非法帧、日志原始帧与相对时间）；
4. 日志熔断后多帧继续采样、解析、显示与绘图，固定提示只显示一次；
5. 存储分配失败：不丢已提交历史、不误报接收、解除切页锁；
6. 接收线程分帧/队列分配失败：不留下假连接，按资源故障非模态处理；
7. 波形页活动时状态栏按波形分帧模式报告日志状态；
8. 日志目录入口在波形页同样可用，错误显示同一固定提示；
9. 波形页停止时“开始接收”是唯一主操作，接收中改为“发送”。

读/写/关闭/热拔插既有路径已由 test_oscilloscope_lifecycle.py 覆盖，
本文件不重复实现，只在需要的故障路径中复用其 fake serial 与注入时钟。
"""

from __future__ import annotations

import importlib
import os
import queue
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant import oscilloscope_chart, oscilloscope_page  # noqa: E402
from paimon_assistant.oscilloscope import (  # noqa: E402
    CompactSampleStore,
    OscilloscopeSession,
)
from paimon_assistant.receive_framer import ReceivedEvent  # noqa: E402
from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.serial_controller import (  # noqa: E402
    SerialController,
)
from test_oscilloscope_lifecycle import (  # noqa: E402
    GatedFactory,
    StepClock,
    wait_until,
)


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


@pytest.fixture
def silent_dialogs(mw, monkeypatch):
    """记录模态弹窗；故障验收要求资源失败路径不弹窗。"""
    critical = []
    warning = []
    monkeypatch.setattr(
        mw.QMessageBox, "critical", staticmethod(lambda *a, **k: critical.append(a))
    )
    monkeypatch.setattr(
        mw.QMessageBox, "warning", staticmethod(lambda *a, **k: warning.append(a))
    )
    return critical, warning


def make_window(qtbot, mw, tmp_path, controller, **kwargs):
    window = mw.MainWindow(
        controller=controller,
        log_service=ReceiveLogService(tmp_path / "logs"),
        **kwargs,
    )
    window._timer.stop()
    qtbot.addWidget(window)
    return window


def make_real_controller(clock, ports=("COM7",)):
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: list(ports),
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    return controller, factory


# --------------------------------------- 绘图资源失败：关串口、保数据、解锁


def test_render_resource_failure_closes_port_keeps_data_and_unlocks(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    real_build = oscilloscope_chart.build_chart_segments
    failed = {"once": False}

    def failing_builder(segments, x_min, x_max, pixel_width):
        if any(segments) and not failed["once"]:
            failed["once"] = True
            raise MemoryError("simulated chart allocation failure")
        return real_build(segments, x_min, x_max, pixel_width)

    monkeypatch.setattr(oscilloscope_page, "build_chart_segments", failing_builder)

    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]
    assert page.is_receiving
    assert not window.page_tabs.tabBar().isEnabled()

    clock.monotonic_ns = 1_250_000_000
    serial.feed(b"12,34\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert not controller.is_open, "绘图资源失败必须关闭物理串口"
    assert serial.closed, "底层串口必须收到 close"
    assert not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled(), "资源失败必须解除切页锁"
    window.page_tabs.setCurrentIndex(0)
    assert window.page_tabs.currentIndex() == 0
    assert [sample.values for sample in page.session.samples] == [
        (12, 34)
    ], "已提交的原始采样必须保留"
    assert page.diagnostic_label.isVisibleTo(page)
    assert "资源" in page.diagnostic_label.text()
    assert "保留" in page.diagnostic_label.text()
    assert "资源" in page.state_label.text(), "停止路径不得覆盖资源失败原因"
    critical, warning = silent_dialogs
    assert critical == [] and warning == [], "资源失败用页面内非模态诊断，不弹窗"
    window.close()


def test_resource_failure_blocks_restart_until_clear_then_recovers(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    """资源失败后不得重开串口假装采集；清空/新建采集是显式恢复路径。"""
    real_build = oscilloscope_chart.build_chart_segments
    failed = {"once": False}

    def failing_builder(segments, x_min, x_max, pixel_width):
        if any(segments) and not failed["once"]:
            failed["once"] = True
            raise MemoryError("simulated chart allocation failure")
        return real_build(segments, x_min, x_max, pixel_width)

    monkeypatch.setattr(oscilloscope_page, "build_chart_segments", failing_builder)

    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    first_serial = factory.instances[0]
    clock.monotonic_ns = 1_100_000_000
    first_serial.feed(b"12\r\n")
    first_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert not controller.is_open and not page.is_receiving

    # 未清空直接再点“开始接收”：不得打开串口或进入假接收状态。
    page.start_button.click()

    assert not controller.is_open, "资源失败未清空前不得重开串口"
    assert not page.is_receiving
    assert page.start_button.text() == "开始接收"
    assert "资源" in page.diagnostic_label.text()

    # 绕过主窗口直接调用页面开始接口，也不得进入假接收状态。
    page.begin_acquisition()
    assert not page.is_receiving

    # 清空/新建采集解除失败状态并清诊断，可以重新开始并真正绘图。
    page.clear_button.click()

    assert page.diagnostic_label.text() == ""
    assert page.session.samples == []
    page.start_button.click()
    assert page.is_receiving and controller.is_open
    second_serial = factory.instances[1]
    clock.monotonic_ns = 2_000_000_000
    second_serial.feed(b"7,8\r\n")
    second_serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert [sample.values for sample in page.session.samples] == [(7, 8)]
    assert page.ch1_series.count() == 1, "清空后的新采集必须恢复绘制"
    window.close()


class FailingValuesStore(CompactSampleStore):
    """窄存储边界：第 N 次采样提交抛 MemoryError。"""

    def __init__(self, fail_on_append: int) -> None:
        super().__init__()
        self.appends = 0
        self.fail_on_append = fail_on_append

    def _append_values(self, values) -> None:
        self.appends += 1
        if self.appends == self.fail_on_append:
            raise MemoryError("injected sample storage failure")
        super()._append_values(values)


def test_storage_failure_keeps_committed_history_unlocks_and_leaks_nothing(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    """提交中途内存失败：失败帧不提交、旧数据保留、关串口解锁、不脏数据页。"""

    def session_factory(monotonic_ns=None):
        return OscilloscopeSession(
            monotonic_ns=monotonic_ns,
            sample_store=FailingValuesStore(fail_on_append=2),
        )

    monkeypatch.setattr(mw, "OscilloscopeSession", session_factory)

    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    clock.monotonic_ns = 1_100_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert [sample.values for sample in page.session.samples] == [(12,)]

    # 下一次读取同时带来两条完整帧：第二条触发存储分配失败。
    clock.monotonic_ns = 1_200_000_000
    serial.feed(b"13\r\n14\r\n")
    serial.release()
    assert wait_until(lambda: controller.received_queue.qsize() == 2)
    window._drain_queues()

    assert [sample.values for sample in page.session.samples] == [
        (12,)
    ], "失败帧不得提交，已提交采样必须保留"
    assert [record.values for record in page.session.records] == [(12,)]
    assert not controller.is_open, "存储资源失败必须关闭物理串口"
    assert serial.closed
    assert not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled(), "存储资源失败必须解除切页锁"
    assert "资源" in page.diagnostic_label.text()
    assert "T+000.100 s" in page.display_edit.toPlainText(), "已提交帧仍可见"

    # 已 drain 的剩余帧与旧队列数据都不得泄漏进数据页，也不得二次补收。
    assert window._event_history == []
    assert window.display_edit.toPlainText() == ""
    assert window._raw_history.raw() == b""
    assert controller.received_queue.empty()
    window._drain_queues()
    assert [sample.values for sample in page.session.samples] == [(12,)]
    assert window.display_edit.toPlainText() == ""

    critical, warning = silent_dialogs
    assert critical == [] and warning == [], "存储资源失败用页面内非模态诊断"
    window.close()


class FailingFramer:
    """``ReceiveFramer`` 替身：首次分帧即模拟缓冲分配失败。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def feed(self, data):
        raise MemoryError("injected framer allocation failure")

    def reset(self) -> None:
        pass


def test_reader_framing_memory_failure_closes_port_and_reports_nonmodal(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    """接收线程分帧分配失败：不得留下假连接，按资源故障非模态处理。"""
    import paimon_assistant.serial_controller as serial_controller

    monkeypatch.setattr(serial_controller, "ReceiveFramer", FailingFramer)

    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]
    assert controller.is_open

    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.error_queue.empty())
    window._drain_queues()

    assert not controller.is_open, "分帧分配失败不得留下假连接"
    assert serial.closed
    assert not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled(), "接收线程故障也必须解除切页锁"
    assert "资源" in page.diagnostic_label.text()
    assert page.session.samples == []
    critical, warning = silent_dialogs
    assert critical == [] and warning == [], "接收线程资源失败用非模态诊断"
    window.close()


def test_resource_failure_discards_stale_queues_when_session_reset_fails(
    qtbot, mw, tmp_path, silent_dialogs, monkeypatch
):
    """会话重置也失败时，停止前的残留队列不得在后续 tick 泄漏到数据页。"""
    real_build = oscilloscope_chart.build_chart_segments
    failed = {"once": False}

    def failing_builder(segments, x_min, x_max, pixel_width):
        if any(segments) and not failed["once"]:
            failed["once"] = True
            raise MemoryError("simulated chart allocation failure")
        return real_build(segments, x_min, x_max, pixel_width)

    monkeypatch.setattr(oscilloscope_page, "build_chart_segments", failing_builder)

    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]
    clock.monotonic_ns = 1_100_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())

    # 同一停止边界里的残留队列内容；重置新队列也可能因内存紧张失败。
    controller.raw_queue.put(b"stale-raw")
    controller.diagnostic_queue.put("残留超长帧诊断")

    def failing_reset():
        raise MemoryError("session reset allocation failed")

    monkeypatch.setattr(controller, "reset_receive_session", failing_reset)
    window._drain_queues()

    assert not controller.is_open and not page.is_receiving
    assert window.page_tabs.tabBar().isEnabled()
    assert "资源" in page.diagnostic_label.text()

    # 后续排空不得把旧会话的 raw/诊断交给数据页，也不得覆盖资源诊断。
    window._drain_queues()
    assert window._raw_history.raw() == b""
    assert window.receive_error_label.text() == ""
    assert window.display_edit.toPlainText() == ""
    assert "资源" in page.diagnostic_label.text()
    critical, warning = silent_dialogs
    assert critical == [] and warning == []
    window.close()


class DrainMemoryFailureQueue:
    """排空收集替身：取出一条事件后 ``get_nowait`` 抛 ``MemoryError``。

    ``list.append`` 无法直接注入内存失败，因此在收集列表时从队列边界模拟：
    已取出一条事件、下一次取空时失败；之后退化为普通空队列，让停止路径
    的清理循环能正常结束。
    """

    def __init__(self, first_item) -> None:
        self._item = first_item
        self._raised = False

    def get_nowait(self):
        if self._item is not None:
            item, self._item = self._item, None
            return item
        if not self._raised:
            self._raised = True
            raise MemoryError("injected drain collection failure")
        raise queue.Empty

    def empty(self) -> bool:
        return self._item is None


def test_wave_drain_collection_memory_failure_stops_cleanly_and_keeps_history(
    qtbot, mw, tmp_path, silent_dialogs
):
    """波形页排空收集内存失败：非模态资源停页、保历史、不泄漏数据页。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    clock.monotonic_ns = 1_100_000_000
    serial.feed(b"12\r\n")
    serial.release()
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()
    assert [sample.values for sample in page.session.samples] == [(12,)]
    assert not page.has_resource_failure

    # 收到队列换成收集失败替身：本批事件取出第一条后 MemoryError。
    controller.received_queue = DrainMemoryFailureQueue(
        ReceivedEvent(1_100, b"99", b"99\r\n", 1_200_000_000)
    )
    window._drain_queues()

    assert not controller.is_open, "排空收集资源失败必须关闭物理串口"
    assert serial.closed
    assert not page.is_receiving
    assert page.has_resource_failure
    assert window.page_tabs.tabBar().isEnabled(), "排空资源失败必须解除切页锁"
    assert "资源" in page.diagnostic_label.text()
    assert [sample.values for sample in page.session.samples] == [
        (12,)
    ], "已提交历史必须保留"
    assert [record.values for record in page.session.records] == [(12,)]
    assert window._event_history == [], "半批事件不得进入数据页"
    assert window.display_edit.toPlainText() == ""
    assert window._raw_history.raw() == b""
    assert window.receive_error_label.text() == ""
    critical, warning = silent_dialogs
    assert critical == [] and warning == [], "排空资源失败用页面内非模态诊断"
    window.close()


# ------------------------------------------- 状态栏、主操作与日志目录入口


def test_wave_receive_status_uses_waveform_framing_not_data_raw_mode(
    qtbot, mw, tmp_path
):
    """数据页选择原始字节时，波形页活动接收仍按分帧与日志状态报告。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    raw_index = window.parse_mode_combo.findText(mw.RAW_MODE)
    window.parse_mode_combo.setCurrentIndex(raw_index)
    assert "不记录日志" in window.receive_status_label.text()

    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()

    status = window.receive_status_label.text()
    assert mw.FRAMED_MODE in status, "波形页接收时状态栏不得沿用数据页原始字节模式"
    assert "日志已启用" in status
    assert "不记录日志" not in status

    page.start_button.click()
    window.close()


def test_primary_action_is_wave_start_when_stopped_and_send_when_active(
    qtbot, mw, tmp_path
):
    """波形页停止时“开始接收”唯一主操作，接收中改为“发送”。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page

    def primaries():
        return [
            button
            for button in (window.open_button, window.send_button, page.start_button)
            if button.property("primary")
        ]

    window.page_tabs.setCurrentIndex(1)
    assert primaries() == [page.start_button], "波形页停止时主操作是开始接收"
    assert not window.open_button.property("primary"), "禁用页面不得留下幽灵主操作"

    page.start_button.click()
    assert primaries() == [window.send_button], "波形页接收中主操作是发送"
    assert window.send_button.isEnabled()

    page.start_button.click()
    assert primaries() == [page.start_button], "停止后主操作回到开始接收"

    window.page_tabs.setCurrentIndex(0)
    assert primaries() == [window.open_button], "数据页既有唯一主操作契约不变"
    window.close()


def test_wave_page_log_dir_entry_uses_shared_service_and_shows_same_errors(
    qtbot, mw, tmp_path, silent_dialogs
):
    """日志目录入口在波形页可用，与数据页共用服务且错误提示一致。"""
    opened = []
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, _factory = make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = mw.MainWindow(
        controller=controller,
        log_service=log_service,
        log_dir_opener=opened.append,
        monotonic_ns=clock.mono,
    )
    window._timer.stop()
    qtbot.addWidget(window)
    window.page_tabs.setCurrentIndex(1)

    button = window.oscilloscope_page.log_dir_button
    assert button.text() == "日志目录"
    button.click()

    assert opened == [log_service.log_dir], "波形页入口打开同一个日志目录"
    assert window.oscilloscope_page.log_error_label.isHidden()

    # 目录位置被文件占位：确保失败只进非模态错误提示，不影响接收链路。
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    failing_service = ReceiveLogService(blocked)
    second = mw.MainWindow(
        controller=controller,
        log_service=failing_service,
        log_dir_opener=opened.append,
        monotonic_ns=clock.mono,
    )
    second._timer.stop()
    qtbot.addWidget(second)
    second.page_tabs.setCurrentIndex(1)
    second.oscilloscope_page.log_dir_button.click()

    text = second.oscilloscope_page.log_error_label.text()
    assert "日志目录打开失败" in text
    assert second.oscilloscope_page.log_error_label.isVisibleTo(
        second.oscilloscope_page
    )
    assert second.receive_status_label.text().endswith("日志已启用")
    assert second.page_tabs.tabBar().isEnabled(), "日志目录失败不影响切页"
    critical, warning = silent_dialogs
    assert critical == [] and warning == []
    window.close()
    second.close()


def rx_line(ms: int, raw_frame: bytes) -> str:
    received = datetime.fromtimestamp(ms // 1000)
    return f"[{received:%H:%M:%S}.{ms % 1000:03d}] RX {raw_frame.hex(' ').upper()}"


def log_lines(log_service) -> list[str]:
    path = log_service.log_dir / (
        datetime.fromtimestamp(1_700_000_000_000 // 1000).date().isoformat() + ".txt"
    )
    return path.read_text(encoding="utf-8").splitlines()


def deliver(serial, clock, data, *, monotonic_ns, wall_ms, timeout=2.0):
    """送入一个读取块并等到 reader 完成本次读取（不依赖 sleep 推时序）。"""
    clock.monotonic_ns = monotonic_ns
    clock.wall_ms = wall_ms
    before = serial.read_starts
    serial.feed(data)
    serial.release()
    assert wait_until(
        lambda: serial.read_starts > before, timeout=timeout
    ), "reader 未在限时内完成本次读取"


def test_real_controller_waveform_sequence_samples_records_and_rx_log(
    qtbot, mw, tmp_path
):
    """合法/非法完整帧经真实控制器进入采样、记录、显示和 RX 日志。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    deliver(serial, clock, b"12,-34\r\n", monotonic_ns=1_250_000_000, wall_ms=1_700_000_000_250)
    assert wait_until(lambda: controller.received_queue.qsize() >= 1)
    window._drain_queues()

    deliver(serial, clock, b"1 2\r\n", monotonic_ns=1_500_000_000, wall_ms=1_700_000_000_500)
    assert wait_until(lambda: controller.received_queue.qsize() >= 1)
    window._drain_queues()

    deliver(serial, clock, b"56\r\n", monotonic_ns=2_000_000_000, wall_ms=1_700_000_001_000)
    assert wait_until(lambda: controller.received_queue.qsize() >= 1)
    window._drain_queues()

    assert [sample.relative_seconds for sample in page.session.samples] == pytest.approx(
        [0.25, 1.0]
    )
    assert [sample.values for sample in page.session.samples] == [(12, -34), (56,)]
    assert [record.values for record in page.session.records] == [
        (12, -34),
        None,
        (56,),
    ]
    assert page.session.records[1].parse_ok is False
    assert page.session.channel_count == 2
    assert page.session.channel_latest_value(1) == -34
    text = page.display_edit.toPlainText()
    assert "T+000.250 s" in text and "T+000.500 s" in text and "T+001.000 s" in text
    assert 'payload="12,-34"' in text and "解析失败" in text
    assert page.ch1_series.count() == 2

    assert log_lines(log_service) == [
        rx_line(1_700_000_000_250, b"12,-34\r\n"),
        rx_line(1_700_000_000_500, b"1 2\r\n"),
        rx_line(1_700_000_001_000, b"56\r\n"),
    ], "合法与非法完整帧均按原始字节+墙上时间写 RX 日志"
    page.start_button.click()
    window.close()


def test_waveform_incomplete_and_oversize_frames_are_not_logged(qtbot, mw, tmp_path):
    """未完成帧与超过 1 MiB 的丢弃帧不产生事件、显示或 RX 日志。"""
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = make_window(qtbot, mw, tmp_path, controller, monotonic_ns=clock.mono)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    deliver(
        serial, clock, b"10\r\n", monotonic_ns=1_100_000_000, wall_ms=1_700_000_000_100
    )
    assert wait_until(lambda: controller.received_queue.qsize() >= 1)
    window._drain_queues()

    # 未完成帧：reader 读到字节但没有帧边界，不产生事件/日志。
    deliver(serial, clock, b"tail", monotonic_ns=1_200_000_000, wall_ms=1_700_000_000_150)

    # 超长帧：无结束符超过 1 MiB 后丢弃，不产生事件/日志，页面诊断提示。
    deliver(
        serial,
        clock,
        b"1" * (1_048_576 + 1),
        monotonic_ns=1_250_000_000,
        wall_ms=1_700_000_000_200,
        timeout=5.0,
    )
    assert wait_until(lambda: not controller.diagnostic_queue.empty())
    window._drain_queues()
    assert "1 MiB" in page.diagnostic_label.text()

    # 结束丢弃状态后，新的完整帧恢复采样与日志。
    deliver(serial, clock, b"\r\n", monotonic_ns=1_300_000_000, wall_ms=1_700_000_000_300)
    deliver(serial, clock, b"20\r\n", monotonic_ns=1_400_000_000, wall_ms=1_700_000_000_400)
    assert wait_until(lambda: controller.received_queue.qsize() >= 1)
    window._drain_queues()

    assert [sample.values for sample in page.session.samples] == [(10,), (20,)]
    assert log_lines(log_service) == [
        rx_line(1_700_000_000_100, b"10\r\n"),
        rx_line(1_700_000_000_400, b"20\r\n"),
    ], "未完成帧与超长帧不得写入 RX 日志"
    page.start_button.click()
    window.close()


class ExplodingOpener:
    """日志文件打开器：持续失败并记录实际文件系统尝试次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        raise RuntimeError("日志句柄异常")


def test_log_failure_keeps_multiple_frames_sampling_rendering_and_one_notice(
    qtbot, mw, tmp_path, silent_dialogs
):
    """日志首次失败熔断：后续多帧继续采样、显示、绘图，固定提示只显示一次。"""
    opener = ExplodingOpener()
    log_service = ReceiveLogService(tmp_path / "logs", opener=opener)
    clock = StepClock(monotonic_ns=1_000_000_000)
    controller, factory = make_real_controller(clock)
    window = mw.MainWindow(
        controller=controller, log_service=log_service, monotonic_ns=clock.mono
    )
    window._timer.stop()
    qtbot.addWidget(window)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    for index, value in enumerate((1, 2, 3), start=1):
        deliver(
            serial,
            clock,
            f"{value}\r\n".encode(),
            monotonic_ns=1_000_000_000 + index * 100_000_000,
            wall_ms=1_700_000_000_000 + index * 100,
        )
        assert wait_until(lambda: controller.received_queue.qsize() >= 1)
        window._drain_queues()

    assert [sample.values for sample in page.session.samples] == [(1,), (2,), (3,)], (
        "日志故障不得停止示波器采样"
    )
    assert page.ch1_series.count() == 3, "日志故障不得停止图表更新"
    text = page.display_edit.toPlainText()
    assert "T+000.100 s" in text and "T+000.200 s" in text and "T+000.300 s" in text
    assert log_service.failed
    assert opener.calls == 1, "熔断后不得重试文件系统"
    fixed = "日志写入失败，请检查磁盘空间或权限"
    assert window.log_error_label.text() == fixed
    assert page.log_error_label.text() == fixed
    assert page.log_error_label.isVisibleTo(page)
    assert "日志写入失败" in window.receive_status_label.text()
    critical, warning = silent_dialogs
    assert critical == [] and warning == [], "日志故障只显示页面内固定提示"
    page.start_button.click()
    window.close()
