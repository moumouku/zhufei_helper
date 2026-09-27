"""示波器协议 offscreen 集成测试（REQ-0005 issue 013）。

使用真实 ``ReceiveFramer`` / ``SerialController`` / ``MainWindow``，只注入
串口工厂、单调/墙上时钟和临时日志目录。覆盖严格 ``0D 0A`` 分帧矩阵、
合法/非法完整帧的显示/采样/日志、恰好 1 MiB 载荷、首个超限字节诊断与恢复，
以及未完成帧在边界到达前不可见。
"""

from __future__ import annotations

import importlib
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.oscilloscope import OscilloscopeSample, parse_frame_payload  # noqa: E402
from paimon_assistant.receive_framer import ReceiveFramer  # noqa: E402
from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.serial_controller import SerialController, SerialSettings  # noqa: E402

ONE_MIB = 1_048_576


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


class GatedSerial:
    """pyserial 兼容 fake：read() 从内部队列取块，否则短超时返回空。"""

    def __init__(self, port, **kwargs) -> None:
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

    def close(self) -> None:
        self.closed = True


class GatedFactory:
    """注入式 serial_factory：记录每次 open 得到的 fake 实例。"""

    def __init__(self) -> None:
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


def display_text(payload: bytes) -> str:
    """接收正文的转义规则：可打印 ASCII 原样，其余字节 ``\\xNN``。"""
    return "".join(
        chr(byte) if 0x20 <= byte <= 0x7E else f"\\x{byte:02X}"
        for byte in payload
    )


def _make_real_controller(clock, ports=("COM7",)):
    factory = GatedFactory()
    controller = SerialController(
        serial_factory=factory,
        port_lister=lambda: list(ports),
        clock_ms=clock.wall,
        monotonic_ns=clock.mono,
    )
    return controller, factory


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


def _make_window(qtbot, mw, controller, tmp_path, clock, log_service=None):
    window = mw.MainWindow(
        controller=controller,
        log_service=log_service or ReceiveLogService(tmp_path / "logs"),
        monotonic_ns=clock.mono,
    )
    window._timer.stop()
    qtbot.addWidget(window)
    return window


# ------------------------------------------------ 真实分帧器边界矩阵


def test_receive_framer_strict_boundaries_with_injected_clocks():
    clock = StepClock()
    framer = ReceiveFramer(clock_ms=clock.wall, monotonic_ns=clock.mono)

    clock.wall_ms, clock.monotonic_ns = 1000, 1_000_000
    assert framer.feed(b"1,2\r") == []  # 跨读取块的 CRLF：LF 未到不算边界

    clock.wall_ms, clock.monotonic_ns = 2000, 2_000_000
    events = framer.feed(b"\n")
    assert [(e.payload, e.received_at_ms, e.monotonic_ns) for e in events] == [
        (b"1,2", 2000, 2_000_000)
    ]
    assert parse_frame_payload(events[0].payload) == (1, 2)

    clock.wall_ms, clock.monotonic_ns = 3000, 3_000_000
    (event,) = framer.feed(b"1\r\r\n")  # CR CRLF：第一个 CR 是载荷
    assert event.payload == b"1\r"
    assert parse_frame_payload(event.payload) is None

    clock.wall_ms, clock.monotonic_ns = 4000, 4_000_000
    (event,) = framer.feed(b"1\r2\n3\r\n")  # 单独 CR/LF 不是边界
    assert event.payload == b"1\r2\n3"
    assert parse_frame_payload(event.payload) is None

    clock.wall_ms, clock.monotonic_ns = 5000, 5_000_000
    events = framer.feed(b"4\r\n5,6\r\n")  # 一次读取中的多帧
    assert [(e.payload, e.received_at_ms, e.monotonic_ns) for e in events] == [
        (b"4", 5000, 5_000_000),
        (b"5,6", 5000, 5_000_000),
    ]
    assert [parse_frame_payload(e.payload) for e in events] == [(4,), (5, 6)]


# ------------------------------------- 真实控制器分帧、诊断与边界时间


def test_controller_multichannel_frame_and_oversize_diagnostic_recovery():
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    controller.open(SerialSettings(port="COM7"))
    serial = factory.instances[0]

    clock.monotonic_ns = 1_500_000_000
    clock.wall_ms = 1_700_000_000_500
    serial.feed(b"1,2,3\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    event = controller.received_queue.get_nowait()
    assert event.payload == b"1,2,3"
    assert event.raw_frame == b"1,2,3\r\n"
    assert event.received_at_ms == 1_700_000_000_500
    assert event.monotonic_ns == 1_500_000_000
    assert parse_frame_payload(event.payload) == (1, 2, 3)

    # 首个超限字节（第 1 MiB + 1 字节）触发一次诊断并丢弃到 CRLF
    serial.feed(b"9" * (ONE_MIB + 1) + b"\r\n")
    assert wait_until(lambda: not controller.diagnostic_queue.empty(), timeout=5.0)
    diagnostic = controller.diagnostic_queue.get_nowait()
    assert "1 MiB" in diagnostic
    assert controller.received_queue.empty()  # 超长帧不生成完整帧记录

    # 丢弃到 CRLF 后恢复：下一完整帧正常进入事件队列
    clock.monotonic_ns = 1_750_000_000
    serial.feed(b"7\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    event = controller.received_queue.get_nowait()
    assert event.payload == b"7"
    assert parse_frame_payload(event.payload) == (7,)
    assert controller.diagnostic_queue.empty()  # 同一段溢出只诊断一次

    controller.close()


# ------------------------- 波形页端到端：显示、采样与完整原始帧日志


def test_waveform_end_to_end_protocol_matrix_display_samples_and_log(
    qtbot, mw, tmp_path
):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _make_window(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()

    frames = [
        (b"-10,+20,0030", b"-10,+20,0030\r\n"),  # 合法：符号 + 前导零 + 3 通道
        (b"1,2,3,4,5,6,7,8", b"1,2,3,4,5,6,7,8\r\n"),  # 合法：8 通道
        (b"", b"\r\n"),  # 空帧
        (b"1,,2", b"1,,2\r\n"),  # 空字段
        (b"1,", b"1,\r\n"),  # 尾逗号
        (b"1 2", b"1 2\r\n"),  # 空格
        (b"1\t2", b"1\t2\r\n"),  # TAB
        (b"1e5", b"1e5\r\n"),  # 指数
        ("１２".encode(), "１２".encode() + b"\r\n"),  # 非 ASCII 数字
        (b"abc", b"abc\r\n"),  # 非法字符
        (b"2147483648", b"2147483648\r\n"),  # int32 正溢出
        (b"-2147483649", b"-2147483649\r\n"),  # int32 负溢出
        (b"1,2,3,4,5,6,7,8,9", b"1,2,3,4,5,6,7,8,9\r\n"),  # 超过 8 通道
    ]
    clock.monotonic_ns = 1_250_000_000
    clock.wall_ms = 1_700_000_000_250
    for _payload, raw in frames:
        factory.instances[0].feed(raw)
    assert wait_until(
        lambda: controller.received_queue.qsize() == len(frames), timeout=5.0
    )
    window._drain_queues()

    assert page.session.samples == [
        OscilloscopeSample(0.250, (-10, 20, 30)),
        OscilloscopeSample(0.250, (1, 2, 3, 4, 5, 6, 7, 8)),
    ]
    assert [record.payload for record in page.session.records] == [
        payload for payload, _raw in frames
    ]
    text = page.display_edit.toPlainText()
    assert text.splitlines() == [display_text(payload) for payload, _raw in frames], (
        "接收正文逐行显示每个完整帧的原始载荷文本"
    )
    assert "1\\x092" in text  # TAB 以转义形式显示，不破坏行结构
    assert "T+" not in text and "payload=" not in text
    assert "解析失败" in page.protocol_status_label.text(), (
        "最近一条完整帧非法时协议失败提示可见"
    )

    log_path = log_service.log_dir / (
        datetime.fromtimestamp(clock.wall_ms // 1000).date().isoformat() + ".txt"
    )
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(clock.wall_ms, raw) for _payload, raw in frames
    ]

    page.start_button.click()
    window.close()


# --------------------- 恰好 1 MiB、首个超限字节诊断与恢复（波形页路径）


def test_one_mib_payload_completes_and_oversize_byte_recovers(qtbot, mw, tmp_path):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _make_window(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    # 载荷恰好 1 MiB（全前导零 + 值 1）仍可完成、采样并写日志
    payload = b"0" * (ONE_MIB - 1) + b"1"
    clock.monotonic_ns = 1_250_000_000
    clock.wall_ms = 1_700_000_000_250
    serial.feed(payload + b"\r\n")
    assert wait_until(lambda: not controller.received_queue.empty(), timeout=5.0)
    window._drain_queues()
    assert page.session.samples == [OscilloscopeSample(0.250, (1,))]
    assert page.session.records[0].payload == payload
    assert page.session.records[0].raw_frame == payload + b"\r\n"

    # 第一个超限载荷字节：一次非模态诊断，不生成帧记录、采样或日志
    clock.monotonic_ns = 1_500_000_000
    clock.wall_ms = 1_700_000_000_500
    serial.feed(b"9" * (ONE_MIB + 1) + b"\r\n")
    assert wait_until(lambda: not controller.diagnostic_queue.empty(), timeout=5.0)
    window._drain_queues()
    assert "1 MiB" in page.diagnostic_label.text()
    assert page.diagnostic_label.isVisibleTo(page)
    assert len(page.session.records) == 1
    assert len(page.session.samples) == 1

    # 丢弃到下一个 CRLF 后恢复，新帧使用新的边界时间
    clock.monotonic_ns = 1_750_000_000
    clock.wall_ms = 1_700_000_000_750
    serial.feed(b"7\r\n")
    assert wait_until(lambda: not controller.received_queue.empty(), timeout=5.0)
    window._drain_queues()
    assert [sample.values for sample in page.session.samples] == [(1,), (7,)]
    assert page.session.samples[1].relative_seconds == pytest.approx(0.750)
    assert controller.diagnostic_queue.empty()  # 同一段溢出只诊断一次

    log_path = log_service.log_dir / (
        datetime.fromtimestamp(clock.wall_ms // 1000).date().isoformat() + ".txt"
    )
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(1_700_000_000_250, payload + b"\r\n"),
        rx_line(1_700_000_000_750, b"7\r\n"),
    ]

    page.start_button.click()
    window.close()


# ------------------------------------------- 未完成帧在边界到达前不可见


def test_incomplete_frame_is_not_displayed_sampled_or_logged_before_boundary(
    qtbot, mw, tmp_path
):
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _make_window(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    clock.monotonic_ns = 1_250_000_000
    clock.wall_ms = 1_700_000_000_250
    serial.feed(b"1,")  # 未完成帧
    window._drain_queues()
    assert page.session.records == []
    assert page.session.samples == []
    assert page.display_edit.toPlainText() == ""
    log_path = log_service.log_dir / (
        datetime.fromtimestamp(clock.wall_ms // 1000).date().isoformat() + ".txt"
    )
    assert not log_path.exists()

    clock.monotonic_ns = 1_500_000_000  # 边界在第二块到达时识别
    clock.wall_ms = 1_700_000_000_500
    serial.feed(b"2\r\n")
    assert wait_until(lambda: not controller.received_queue.empty())
    window._drain_queues()

    assert [record.payload for record in page.session.records] == [b"1,2"]
    assert page.session.samples == [OscilloscopeSample(0.500, (1, 2))]
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(1_700_000_000_500, b"1,2\r\n")
    ]

    page.start_button.click()
    window.close()


# ------------- 小数协议修订端到端回归：正文/采样/绘图/RX 日志/数据页隔离


def test_decimal_frames_end_to_end_keep_raw_body_samples_chart_and_rx_hex_log(
    qtbot, mw, tmp_path
):
    """0.96/328.00 与 -0.13/330.00 多帧经真实链路保持原始正文与小数。"""
    clock = StepClock()
    controller, factory = _make_real_controller(clock)
    log_service = ReceiveLogService(tmp_path / "logs")
    window = _make_window(qtbot, mw, controller, tmp_path, clock, log_service)
    page = window.oscilloscope_page
    window.page_tabs.setCurrentIndex(1)
    page.start_button.click()
    serial = factory.instances[0]

    # 第一帧 CRLF 跨两个读取块：`\r` 与 `\n` 分别到达；随后一个读取块
    # 再带出两条完整帧，验证读取块内多帧的顺序与各自边界时间。
    clock.monotonic_ns = 1_250_000_000
    clock.wall_ms = 1_700_000_000_250
    serial.feed(b"0.96,328.00\r")
    clock.monotonic_ns = 1_500_000_000
    clock.wall_ms = 1_700_000_000_500
    serial.feed(b"\n")
    assert wait_until(lambda: controller.received_queue.qsize() == 1)

    clock.monotonic_ns = 1_750_000_000
    clock.wall_ms = 1_700_000_000_750
    serial.feed(b"-0.13,330.00\r\n0.76,329.00\r\n")
    assert wait_until(lambda: controller.received_queue.qsize() == 3)
    window._drain_queues()

    payloads = [b"0.96,328.00", b"-0.13,330.00", b"0.76,329.00"]
    raw_frames = [payload + b"\r\n" for payload in payloads]
    # 边界时间属于识别出 CRLF 的那个读取块；拆分帧不得用载荷块的时间。
    boundary_ms = [1_700_000_000_500, 1_700_000_000_750, 1_700_000_000_750]

    records = page.session.records
    assert [record.payload for record in records] == payloads
    assert [record.raw_frame for record in records] == raw_frames
    assert [record.values for record in records] == [
        (0.96, 328.0),
        (-0.13, 330.0),
        (0.76, 329.0),
    ]
    assert all(record.parse_ok for record in records)

    assert page.session.samples == [
        OscilloscopeSample(0.5, (0.96, 328.0)),
        OscilloscopeSample(0.75, (-0.13, 330.0)),
        OscilloscopeSample(0.75, (0.76, 329.0)),
    ], "会话采样保留小数且相对时间取 CRLF 边界块"
    assert page.session.channel_latest_value(0) == 0.76
    assert page.session.channel_latest_value(1) == 329.0

    # 接收正文只显示完整帧的原始载荷文本，不夹带旧格式标记或解析状态。
    text = page.display_edit.toPlainText()
    assert text == "0.96,328.00\n-0.13,330.00\n0.76,329.00"
    assert "payload=" not in text
    assert '"' not in text
    assert "T+" not in text
    assert "解析" not in text
    assert page.protocol_status_label.text() == ""
    assert not page.protocol_status_label.isVisibleTo(page)

    # CH1/CH2 的真实 QtCharts 系列收到未截断的小数原始点。
    ch1_points = page.channel_series[0][0]
    assert ch1_points.count() == 3
    assert [ch1_points.at(index).x() for index in range(3)] == [
        pytest.approx(0.5),
        pytest.approx(0.75),
        pytest.approx(0.75),
    ]
    assert [ch1_points.at(index).y() for index in range(3)] == [
        pytest.approx(0.96),
        pytest.approx(-0.13),
        pytest.approx(0.76),
    ]
    ch2_points = page.channel_series[1][0]
    assert ch2_points.count() == 3
    assert [ch2_points.at(index).x() for index in range(3)] == [
        pytest.approx(0.5),
        pytest.approx(0.75),
        pytest.approx(0.75),
    ]
    assert [ch2_points.at(index).y() for index in range(3)] == [
        pytest.approx(328.0),
        pytest.approx(330.0),
        pytest.approx(329.0),
    ]

    # RX HEX 日志逐条保留完整原始帧（含 0D 0A），拆分帧只写一行。
    log_path = log_service.log_dir / (
        datetime.fromtimestamp(boundary_ms[0] // 1000).date().isoformat() + ".txt"
    )
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        rx_line(ms, raw) for ms, raw in zip(boundary_ms, raw_frames)
    ]

    # 波形接收不得把帧或原始字节泄漏到数据页历史。
    assert window.display_edit.toPlainText() == ""
    assert window._raw_history.raw() == b""
    assert window._event_history == []

    page.start_button.click()
    window.close()
