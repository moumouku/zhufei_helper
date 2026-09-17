"""REQ-0003 §13 验收矩阵（18 条）端到端测试。

环境：fake serial + fake 时间源 + 临时日志目录 + Qt offscreen。每个测试名以
``test_acNN_`` 标注对应 REQ §13 的验收编号；断言只使用外部可观察行为：事件字段、
显示区 ``toPlainText()``、错误标签文本、队列内容、日志文件内容、fake 调用记录。

真实串口端到端（REQ §14.7：com0com COM17<->COM19, 115200/8/N/1）保留为 Windows
手工验收，不纳入本文件。

运行：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_req0003_acceptance.py -q``
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from paimon_assistant import main_window as mw  # noqa: E402
from paimon_assistant.receive_framer import ReceivedEvent  # noqa: E402
from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.serial_controller import (  # noqa: E402
    SerialController,
    SerialSettings,
)

#: 固定“今天”的本地时间（日志清理/日期文件的确定性基准）。
TODAY = datetime(2026, 5, 17, 9, 12, 3)


def local_ms(at: datetime) -> int:
    """本地 datetime -> epoch 毫秒。"""
    return int(at.timestamp() * 1000)


def rx_line(ms: int, raw_frame: bytes) -> str:
    """日志服务对某事件的期望行（不含换行；配合 log_lines 使用）。"""
    received = datetime.fromtimestamp(ms // 1000)
    return f"[{received:%H:%M:%S}.{ms % 1000:03d}] RX {raw_frame.hex(' ').upper()}"


class FakeNow:
    """可推进的毫秒时间源，同时供控制器时间戳与日志清理截止日期使用。"""

    def __init__(self, at: datetime):
        self.ms = local_ms(at)

    def __call__(self) -> int:
        return self.ms


class FakeSerial:
    """pyserial 兼容 fake：``script`` 逐项返回；``block`` 时每次 read 先等 release()。"""

    def __init__(self, port, script=(), block=False, **kwargs):
        self.port = port
        self.script = list(script)
        self.block = block
        self.written = bytearray()
        self.closed = False
        self.parks = 0
        self._gate = threading.Event()

    def read(self, n=1):
        if self.block:
            self.parks += 1
            self._gate.wait(timeout=5.0)
            self._gate.clear()
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return bytes(item)

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def close(self):
        self.closed = True

    def release(self):
        self._gate.set()


class FakeFactory:
    """按打开顺序发牌：每次 ``open()`` 拿一份新的读脚本。"""

    def __init__(self, scripts, *, block=False):
        self._scripts = [list(script) for script in scripts]
        self._block = block
        self.instances = []

    def __call__(self, *args, **kwargs):
        inst = FakeSerial(
            kwargs.get("port", "?"), script=self._scripts.pop(0), block=self._block
        )
        self.instances.append(inst)
        return inst


class FailingOpener:
    """日志文件打开器测试替身：统计调用次数并持续失败。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        raise PermissionError("disk is full")


def make_settings(port="COM1") -> SerialSettings:
    return SerialSettings(
        port=port, baudrate=115200, bytesize=8, parity="N", stopbits=1
    )


def make_controller(scripts, *, block=False, clock_ms=None, ports=()):
    """构造真实 SerialController + fake serial；返回 (controller, factory)。"""
    factory = FakeFactory(scripts, block=block)
    controller = SerialController(
        serial_factory=factory, port_lister=lambda: list(ports), clock_ms=clock_ms
    )
    return controller, factory


def make_window(qtbot, controller, log_service, *, log_dir_opener=None):
    """构造 offscreen 主窗口；停掉 drain 定时器，由测试手动调用 _drain_queues()。"""
    window = mw.MainWindow(
        controller=controller,
        log_service=log_service,
        log_dir_opener=log_dir_opener,
    )
    window._timer.stop()
    qtbot.addWidget(window)
    return window


def wait_until(predicate, timeout=2.0):
    """有界轮询（不驱动 Qt 事件循环），返回 predicate 最终值。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def get_events(controller, count, timeout=2.0):
    """从控制器接收队列取 ``count`` 个事件（有界）。"""
    events = []
    deadline = time.monotonic() + timeout
    while len(events) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        events.append(controller.received_queue.get(timeout=remaining))
    return events


def select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


def log_lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


# ------------------------------------------------- §13.1-§13.8 分帧与事件


def test_ac01_cr_and_lf_in_separate_reads_form_one_event():
    """§13.1 `A\r` 与后续 `\n` 分属两个读取块时生成一个完整事件。"""
    controller, _ = make_controller([[b"A\r", b"\n"]])
    try:
        controller.open(make_settings())
        (event,) = get_events(controller, 1)
        assert (event.payload, event.raw_frame) == (b"A", b"A\r\n")
        assert controller.received_queue.empty()
    finally:
        controller.close()


def test_ac02_single_read_with_two_frames_yields_ordered_events():
    """§13.2 一次读取 `A\r\nB\r\n` 时按序生成两个事件。"""
    controller, _ = make_controller([[b"A\r\nB\r\n"]])
    try:
        controller.open(make_settings())
        first, second = get_events(controller, 2)
        assert [(e.payload, e.raw_frame) for e in (first, second)] == [
            (b"A", b"A\r\n"),
            (b"B", b"B\r\n"),
        ]
        assert controller.received_queue.empty()
    finally:
        controller.close()


def test_ac03_lone_cr_lone_lf_and_cr_cr_lf_follow_strict_0d0a():
    """§13.3 单独 `\r`/`\n`、`\r\r\n` 严格按 0D 0A 规则分帧。"""
    controller, factory = make_controller(
        [[b"AB\r", b"C\r\n", b"\n\r\n", b"\r\r\n"]], block=True
    )
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]

        serial.release()  # 单独 `\r`：不结束帧，也未产生事件
        assert wait_until(lambda: serial.parks >= 2), "第一块未被分帧处理"
        assert controller.received_queue.empty()

        serial.release()  # 补齐 `\r\n`：单独的 `\r` 属于载荷
        assert wait_until(lambda: serial.parks >= 3), "第二块未被分帧处理"
        (first,) = get_events(controller, 1)
        assert (first.payload, first.raw_frame) == (b"AB\rC", b"AB\rC\r\n")
        assert controller.received_queue.empty()

        serial.release()  # 单独 `\n` 是载荷字节
        assert wait_until(lambda: serial.parks >= 4), "第三块未被分帧处理"
        (second,) = get_events(controller, 1)
        assert (second.payload, second.raw_frame) == (b"\n", b"\n\r\n")

        serial.release()  # `\r\r\n`：前一个 `\r` 属于载荷
        assert wait_until(lambda: serial.parks >= 5), "第四块未被分帧处理"
        (third,) = get_events(controller, 1)
        assert (third.payload, third.raw_frame) == (b"\r", b"\r\r\n")
        assert controller.received_queue.empty()
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac04_crlf_yields_empty_payload_event():
    """§13.4 `\r\n` 生成空载荷事件。"""
    controller, _ = make_controller([[b"\r\n"]])
    try:
        controller.open(make_settings())
        (event,) = get_events(controller, 1)
        assert (event.payload, event.raw_frame) == (b"", b"\r\n")
    finally:
        controller.close()


def test_ac05_unfinished_tail_never_joins_data_after_close_and_reopen():
    """§13.5 关闭连接后未完成尾部不与新连接数据拼接（清空路径见 ac17）。"""
    controller, factory = make_controller(
        [[b"AB\r", b"old-after-stop"], [b"\n", b"C\r\n"]], block=True
    )
    first = second = None
    try:
        controller.open(make_settings("COM1"))
        first = factory.instances[0]
        first.release()  # 未完成尾部 `AB\r`
        assert wait_until(lambda: first.parks >= 2), "第一连接首块未被处理"
        assert controller.received_queue.empty()

        controller.close()
        controller.open(make_settings("COM2"))
        second = factory.instances[1]

        second.release()  # 单独 `\n`：旧尾部若幸存，会在这里拼成 `AB` 事件
        assert wait_until(lambda: second.parks >= 2), "第二连接首块未被处理"
        assert controller.received_queue.empty()

        second.release()
        assert wait_until(lambda: second.parks >= 3), "第二连接第二块未被处理"
        (event,) = get_events(controller, 1)
        assert (event.payload, event.raw_frame) == (b"\nC", b"\nC\r\n")
    finally:
        for serial in (first, second):
            if serial is not None:
                serial.release()
        controller.close()


def test_ac06_oversized_frame_yields_no_event_or_log_and_recovers(
    tmp_path, qtbot
):
    """§13.6 超过 1 MiB 且无结束符 -> 不生成事件/日志，下一个 `\r\n` 后恢复。"""
    now = FakeNow(TODAY)
    oversized = b"x" * (1_048_576 + 1)
    controller, factory = make_controller(
        [[oversized, b"\r\n", b"OK\r\n"]], block=True, clock_ms=now
    )
    window = make_window(
        qtbot, controller, ReceiveLogService(tmp_path / "logs", now_ms=now)
    )
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]

        serial.release()  # 超长帧：进入丢弃状态并产生一次诊断
        assert wait_until(lambda: serial.parks >= 2, timeout=5.0), "超长帧未被处理"
        window._drain_queues()
        assert window.receive_error_label.text() == "接收帧超过 1 MiB，已丢弃"
        assert controller.received_queue.empty()
        assert not (tmp_path / "logs").exists(), "超长帧不得写日志"

        serial.release()  # 结束符解除丢弃状态：本身不产生事件
        assert wait_until(lambda: serial.parks >= 3, timeout=5.0)
        window._drain_queues()
        assert window.display_edit.toPlainText() == ""
        assert not (tmp_path / "logs").exists()

        serial.release()  # 恢复正常分帧
        assert wait_until(
            lambda: not controller.received_queue.empty(), timeout=5.0
        ), "恢复后未生成事件"
        window._drain_queues()
        assert window.display_edit.toPlainText() == "OK\n"
        assert log_lines(tmp_path / "logs" / "2026-05-17.txt") == [
            rx_line(now.ms, b"OK\r\n")
        ]
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac07_event_carries_received_at_ms_payload_and_raw_frame():
    """§13.7 每个事件包含正确的 received_at_ms / payload / raw_frame。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3, 125000))
    controller, _ = make_controller([[b"Hello\r\n", b"\r\n"]], clock_ms=now)
    try:
        controller.open(make_settings())
        first, second = get_events(controller, 2)
        assert first.received_at_ms == now.ms
        assert first.payload == b"Hello"
        assert first.raw_frame == b"Hello\r\n"
        assert second.payload == b""
        assert second.raw_frame == b"\r\n"
    finally:
        controller.close()


def test_ac08_timestamp_recorded_at_boundary_per_event_in_order():
    """§13.8 时间戳在完整 `\r\n` 识别后记录；多帧事件按序独立记录。"""
    values = [1_700_000_000_000, 1_700_000_000_001]
    calls = []

    def clock_ms():
        calls.append(1)
        return values[len(calls) - 1]

    controller, factory = make_controller(
        [[b"A\r", b"\nB\r\n"]], block=True, clock_ms=clock_ms
    )
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]

        serial.release()  # 只有 `A\r`：未识别结束边界，不得读时钟
        assert wait_until(lambda: serial.parks >= 2), "第一块未被分帧处理"
        assert calls == []
        assert controller.received_queue.empty()

        serial.release()  # `\n` 结束 A，随后 `B\r\n` 结束 B：两次独立记录
        assert wait_until(lambda: serial.parks >= 3), "第二块未被分帧处理"
        first, second = get_events(controller, 2)
        assert calls == [1, 1]
        assert (first.received_at_ms, first.payload) == (values[0], b"A")
        assert (second.received_at_ms, second.payload) == (values[1], b"B")
        assert controller.received_queue.empty()
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac09_text_hex_and_encoding_switches_redraw_event_history(
    tmp_path, qtbot
):
    """§13.9 文本/HEX 模式与 UTF-8/GBK 切换能正确重绘事件历史。"""
    gbk = "你好".encode("gbk")
    now = FakeNow(TODAY)
    controller, _ = make_controller([[b"Hello\r\n", gbk + b"\r\n"]], clock_ms=now)
    window = make_window(
        qtbot, controller, ReceiveLogService(tmp_path / "logs", now_ms=now)
    )
    window.timestamp_checkbox.setChecked(False)
    try:
        controller.open(make_settings())
        assert wait_until(lambda: controller.received_queue.qsize() == 2)
        window._drain_queues()

        assert window.display_edit.toPlainText() == (
            "Hello\n" + gbk.decode("utf-8", "replace") + "\n"
        )

        select(window.encoding_combo, "GBK")
        assert window.display_edit.toPlainText() == "Hello\n你好\n"

        select(window.receive_mode_combo, "HEX")
        expected_hex = "\n".join(
            " ".join(f"{b:02X}" for b in raw)
            for raw in (b"Hello\r\n", gbk + b"\r\n")
        )
        assert window.display_edit.toPlainText() == expected_hex + "\n"

        window.timestamp_checkbox.setChecked(True)
        assert window.display_edit.toPlainText() == (
            f"[09:12:03.000] 48 65 6C 6C 6F 0D 0A\n"
            f"[09:12:03.000] {gbk.hex(' ').upper()} 0D 0A\n"
        )
    finally:
        controller.close()


def test_ac10_timestamp_switch_default_on_and_display_only(tmp_path, qtbot):
    """§13.10 时间戳开关默认开启；关闭只改变显示，不改变事件与日志。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3, 125000))
    controller, _ = make_controller([[b"Hi\r\n"]], clock_ms=now)
    log_dir = tmp_path / "logs"
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    try:
        assert window.timestamp_checkbox.objectName() == "timestamp_checkbox"
        assert window.timestamp_checkbox.isChecked(), "每次启动默认开启时间戳显示"

        controller.open(make_settings())
        assert wait_until(lambda: not controller.received_queue.empty())
        window._drain_queues()
        assert window.display_edit.toPlainText() == "[09:12:03.125] Hi\n"
        log_file = log_dir / "2026-05-17.txt"
        logged = log_file.read_text(encoding="utf-8")

        window.timestamp_checkbox.setChecked(False)
        assert window.display_edit.toPlainText() == "Hi\n"
        assert log_file.read_text(encoding="utf-8") == logged

        window.timestamp_checkbox.setChecked(True)
        assert window.display_edit.toPlainText() == "[09:12:03.125] Hi\n"
        assert log_file.read_text(encoding="utf-8") == logged

        # 原始帧未因开关变化：HEX 仍显示完整 raw_frame
        select(window.receive_mode_combo, "HEX")
        assert window.display_edit.toPlainText() == "[09:12:03.125] 48 69 0D 0A\n"
        assert log_lines(log_file) == [rx_line(now.ms, b"Hi\r\n")]
    finally:
        controller.close()


def test_ac11_log_file_named_by_local_date_without_repeating_date(tmp_path, qtbot):
    """§13.11 日志按本地日期写入 `YYYY-MM-DD.txt`，事件时间不重复写日期。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3, 125000))
    controller, _ = make_controller([[b"hello\r\n"]], clock_ms=now)
    log_dir = tmp_path / "logs"
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    window.timestamp_checkbox.setChecked(False)
    try:
        controller.open(make_settings())
        assert wait_until(lambda: not controller.received_queue.empty())
        window._drain_queues()

        assert [p.name for p in log_dir.iterdir()] == ["2026-05-17.txt"]
        (line,) = log_lines(log_dir / "2026-05-17.txt")
        assert line == "[09:12:03.125] RX 68 65 6C 6C 6F 0D 0A"
        assert "2026" not in line and "05-17" not in line
    finally:
        controller.close()


def test_ac12_log_stores_complete_raw_frame_hex_including_0d0a(tmp_path, qtbot):
    """§13.12 日志记录保存完整原始帧 HEX，含 `0D 0A`（含空帧）。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3, 125000))
    controller, _ = make_controller([[b"Hi\r\n", b"\r\n"]], clock_ms=now)
    log_dir = tmp_path / "logs"
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    window.timestamp_checkbox.setChecked(False)
    try:
        controller.open(make_settings())
        assert wait_until(lambda: controller.received_queue.qsize() == 2)
        window._drain_queues()

        lines = log_lines(log_dir / "2026-05-17.txt")
        assert lines == [rx_line(now.ms, b"Hi\r\n"), rx_line(now.ms, b"\r\n")]
        assert lines[0].endswith("48 69 0D 0A")
        assert lines[1].endswith("RX 0D 0A")
    finally:
        controller.close()


def test_ac13_log_failure_shows_red_label_logs_and_keeps_receiving(
    tmp_path, qtbot, caplog
):
    """§13.13 日志写入失败时显示指定红色提示、写入系统日志，接收/队列/显示继续工作。"""
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory", encoding="utf-8")
    controller, factory = make_controller([[b"first\r\n", b"second\r\n"]], block=True)
    window = make_window(
        qtbot, controller, ReceiveLogService(blocked, now_ms=FakeNow(TODAY))
    )
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]
        serial.release()
        assert wait_until(lambda: serial.parks >= 2), "第一个事件未入队"

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            window._drain_queues()

        label = window.log_error_label
        assert label.objectName() == "log_error_label"
        assert label.text() == "日志写入失败，请检查磁盘空间或权限"
        assert "red" in label.styleSheet()
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert "Receive log write failed" in caplog.text

        # 日志故障不影响接收、事件队列与显示
        assert window.display_edit.toPlainText() == "first\n"
        assert controller.received_queue.empty()
        serial.release()
        assert wait_until(lambda: serial.parks >= 3), "第二个事件未入队"
        window._drain_queues()
        assert window.display_edit.toPlainText() == "first\nsecond\n"
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac14_after_log_failure_no_memory_buffer_no_retry(tmp_path, qtbot, caplog):
    """§13.14 日志失败后不产生内存缓冲或重试队列。"""
    opener = FailingOpener()
    now = FakeNow(TODAY)
    controller, factory = make_controller(
        [[b"a\r\n", b"b\r\n"]], block=True, clock_ms=now
    )
    log_dir = tmp_path / "logs"
    service = ReceiveLogService(log_dir, now_ms=now, opener=opener)
    window = make_window(qtbot, controller, service)
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]
        serial.release()
        assert wait_until(lambda: serial.parks >= 2), "第一个事件未入队"

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            window._drain_queues()  # 首次失败：熔断
            assert opener.calls == 1

            serial.release()
            assert wait_until(lambda: serial.parks >= 3), "第二个事件未入队"
            window._drain_queues()  # 熔断后：不碰文件系统、不缓存、不重试
            assert service.write_event(ReceivedEvent(now.ms, b"c", b"c\r\n")) is False

        assert opener.calls == 1
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert not (log_dir / "2026-05-17.txt").exists()
        # 事件仍然全部显示，只是不再进入日志
        assert window.display_edit.toPlainText() == "a\nb\n"
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac15_retention_cleans_expired_logs_at_startup_and_across_midnight(
    tmp_path, qtbot
):
    """§13.15 启动及跨日首次写日志前清理超过 30 天的日期日志，且不误删其他文件。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    boundary = log_dir / "2026-04-17.txt"  # 恰为 2026-05-17 - 30 天：保留
    boundary.write_text("keep", encoding="utf-8")
    stale = log_dir / "2026-04-16.txt"  # 超出 30 天：启动清理删除
    stale.write_text("drop", encoding="utf-8")
    notes = log_dir / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    compact = log_dir / "20260416.txt"
    compact.write_text("keep", encoding="utf-8")
    subdir = log_dir / "2020-01-01.txt"
    subdir.mkdir()

    controller, factory = make_controller(
        [[b"first\r\n", b"second\r\n"]], block=True, clock_ms=now
    )
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]
        serial.release()
        assert wait_until(lambda: serial.parks >= 2), "第一个事件未入队"
        window._drain_queues()  # 启动后首次写入前的清理

        assert not stale.exists()
        assert boundary.exists()
        assert notes.exists() and compact.exists() and subdir.is_dir()
        assert log_lines(log_dir / "2026-05-17.txt") == [rx_line(now.ms, b"first\r\n")]

        # 跨过本地日期后，新日期首次写入前再次清理
        now.ms = local_ms(datetime(2026, 5, 18, 0, 0, 1))
        serial.release()
        assert wait_until(lambda: serial.parks >= 3), "第二个事件未入队"
        window._drain_queues()

        assert not boundary.exists()  # 2026-05-18 的截止日为 2026-04-18
        assert log_lines(log_dir / "2026-05-18.txt") == [
            rx_line(now.ms, b"second\r\n")
        ]
        assert log_lines(log_dir / "2026-05-17.txt") == [
            rx_line(local_ms(datetime(2026, 5, 17, 9, 12, 3)), b"first\r\n")
        ]
        assert notes.exists() and compact.exists() and subdir.is_dir()
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac15_startup_cleanup_runs_without_any_received_event(tmp_path, qtbot):
    """§13.15/§8.1 零事件启动：构造主窗口即清理过期日志，且不误删其他文件。"""
    now = FakeNow(TODAY)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "2026-04-16.txt"  # 超出 30 天：启动时即删除
    stale.write_text("drop", encoding="utf-8")
    boundary = log_dir / "2026-04-17.txt"  # 恰为 30 天边界：保留
    boundary.write_text("keep", encoding="utf-8")
    notes = log_dir / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    subdir = log_dir / "2020-01-01.txt"
    subdir.mkdir()

    controller, _ = make_controller([[]])
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    try:
        assert not stale.exists()
        assert boundary.exists() and notes.exists() and subdir.is_dir()
        # 启动清理本身不写日志，也不创建当天文件
        assert not (log_dir / "2026-05-17.txt").exists()
    finally:
        controller.close()


def test_ac15_startup_cleanup_missing_dir_skipped_without_creating_it(
    tmp_path, qtbot
):
    """§13.15/§8.1 日志目录不存在时启动清理不报错，也不为清理创建目录。"""
    log_dir = tmp_path / "logs"
    controller, _ = make_controller([[]])
    window = make_window(
        qtbot, controller, ReceiveLogService(log_dir, now_ms=FakeNow(TODAY))
    )
    try:
        assert not log_dir.exists()
    finally:
        controller.close()


def test_ac15_startup_cleanup_failure_does_not_break_window_startup(
    tmp_path, qtbot, monkeypatch
):
    """§13.15/§8.1 启动清理失败只记日志，不影响主窗口启动与后续接收显示。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    controller, _ = make_controller([[]])

    def _raise_permission_error(self):
        raise PermissionError("日志目录无法列举")

    with monkeypatch.context() as mp:
        mp.setattr(Path, "iterdir", _raise_permission_error)
        window = make_window(
            qtbot, controller, ReceiveLogService(log_dir, now_ms=FakeNow(TODAY))
        )

    # 清理失败不阻断启动：接收事件仍能显示
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(ReceivedEvent(0, b"ok", b"ok\r\n"))
    window._drain_queues()
    try:
        assert window.display_edit.toPlainText() == "ok\n"
    finally:
        controller.close()


def test_ac16_log_dir_entry_opens_the_log_directory(tmp_path, qtbot):
    """§13.16 “日志目录”入口能打开日志目录（目录本身，而非某个日期文件）。"""
    log_dir = tmp_path / "logs"
    opened = []
    controller, _ = make_controller([[]])
    window = make_window(
        qtbot,
        controller,
        ReceiveLogService(log_dir, now_ms=FakeNow(TODAY)),
        log_dir_opener=opened.append,
    )
    try:
        button = window.log_dir_button
        assert button.objectName() == "log_dir_button"
        button.click()

        assert log_dir.is_dir()
        assert opened == [log_dir]
        assert window.log_error_label.text() == ""
    finally:
        controller.close()


# ---------------------------------------------------- §13.17 清空边界


def test_ac17_clear_drops_preclear_tail_and_pending_events(tmp_path, qtbot):
    """§13.17 清空前未完成尾部与已入队未显示事件都不泄漏到清空后。

    会话锁+代次线性化的控制器级版本由 tests/test_serial_controller.py 的
    ``test_reset_receive_session_discards_pending_items_tail_and_keeps_reading``
    与 tests/test_regressions.py 的旧 reader 隔离用例冻结；本测试是主窗口端到端版本。
    """
    now = FakeNow(TODAY)
    controller, factory = make_controller(
        [[b"OLD\r\nAB\r", b"\nC\r\n"]], block=True, clock_ms=now
    )
    window = make_window(
        qtbot, controller, ReceiveLogService(tmp_path / "logs", now_ms=now)
    )
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]

        # 先显示一次超长帧诊断，验证清空会一并清除它
        controller.diagnostic_queue.put("接收帧超过 1 MiB，已丢弃")
        window._drain_queues()
        assert window.receive_error_label.text() == "接收帧超过 1 MiB，已丢弃"

        serial.release()  # 一个完整旧事件 + 未完成尾部 `AB\r`
        assert wait_until(lambda: serial.parks >= 2), "第一块未被分帧处理"
        assert not controller.received_queue.empty(), "旧事件应已入队且尚未显示"

        window.clear_button.click()

        assert window.display_edit.toPlainText() == ""
        assert window.receive_error_label.text() == ""
        # 清空后切换显示设置不复活旧事件
        select(window.receive_mode_combo, "HEX")
        window.timestamp_checkbox.setChecked(True)
        assert window.display_edit.toPlainText() == ""

        serial.release()  # 旧尾部 `AB\r` 若幸存，会在这里拼成一个 `AB` 事件
        assert wait_until(
            lambda: not controller.received_queue.empty()
        ), "清空后新数据未形成事件"
        window._drain_queues()
        assert window.display_edit.toPlainText() == "[09:12:03.000] 0A 43 0D 0A\n"
        assert controller.received_queue.empty()
    finally:
        if serial is not None:
            serial.release()
        controller.close()


def test_ac17_clear_never_deletes_or_modifies_existing_log_files(tmp_path, qtbot):
    """§13.17 点击清空不删除也不修改任何既有日志文件。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "2026-05-16.txt").write_text(
        "[08:00:00.000] RX 41 0D 0A\n", encoding="utf-8"
    )
    (log_dir / "notes.txt").write_text("keep", encoding="utf-8")
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in log_dir.iterdir()
    }

    controller, _ = make_controller([[]])
    window = make_window(
        qtbot, controller, ReceiveLogService(log_dir, now_ms=FakeNow(TODAY))
    )
    try:
        window.clear_button.click()

        after = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in log_dir.iterdir()
        }
        assert after == before
    finally:
        controller.close()


def test_ac17_post_clear_data_forms_new_events_display_and_log(tmp_path, qtbot):
    """§13.17 清空后新到达数据正常成事件/显示/写日志；切换显示不复活旧事件。"""
    now = FakeNow(datetime(2026, 5, 17, 9, 12, 3, 125000))
    controller, factory = make_controller(
        [[b"old\r\n", b"new\r\n"]], block=True, clock_ms=now
    )
    log_dir = tmp_path / "logs"
    window = make_window(qtbot, controller, ReceiveLogService(log_dir, now_ms=now))
    window.timestamp_checkbox.setChecked(False)
    serial = None
    try:
        controller.open(make_settings())
        serial = factory.instances[0]
        serial.release()
        assert wait_until(lambda: serial.parks >= 2), "旧事件未入队"
        window._drain_queues()
        assert window.display_edit.toPlainText() == "old\n"

        window.clear_button.click()
        assert window.display_edit.toPlainText() == ""

        serial.release()
        assert wait_until(lambda: not controller.received_queue.empty())
        window._drain_queues()
        assert window.display_edit.toPlainText() == "new\n"
        assert log_lines(log_dir / "2026-05-17.txt") == [
            rx_line(now.ms, b"old\r\n"),
            rx_line(now.ms, b"new\r\n"),
        ]

        # 清空后切换显示设置不得复活旧事件
        select(window.receive_mode_combo, "HEX")
        assert window.display_edit.toPlainText() == "6E 65 77 0D 0A\n"
    finally:
        if serial is not None:
            serial.release()
        controller.close()


# ---------------------------------------------------- §13.18 发送行为


def test_ac18_send_never_appends_crlf_and_never_enters_rx_log(tmp_path, qtbot):
    """§13.18 发送不自动追加 `\r\n`，不进入接收事件队列、显示或 `RX` 日志。"""
    log_dir = tmp_path / "logs"
    controller, factory = make_controller([[]], ports=["COM1"])
    window = make_window(
        qtbot, controller, ReceiveLogService(log_dir, now_ms=FakeNow(TODAY))
    )
    try:
        window.open_button.click()
        assert window.open_button.text() == "关闭"
        serial = factory.instances[0]

        window.send_edit.setText("hello")
        window.send_button.click()
        assert bytes(serial.written) == b"hello"  # 文本模式不追加结束符

        select(window.send_mode_combo, "HEX")
        window.send_edit.setText("0D 0A")
        window.send_button.click()
        assert bytes(serial.written) == b"hello\r\n"  # 仅在 HEX 模式显式输入时发送

        # 发送数据不生成 ReceivedEvent、不显示为 RX、不写接收日志
        window._drain_queues()
        assert controller.received_queue.empty()
        assert window.display_edit.toPlainText() == ""
        assert not log_dir.exists()
    finally:
        controller.close()
