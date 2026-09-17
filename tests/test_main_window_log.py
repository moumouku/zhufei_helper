"""MainWindow 事件 -> 显示 -> 日志集成测试（offscreen，REQ-0003，issue 009）。

只断言外部可观察行为：显示区文本、`log_error_label` / `receive_error_label`
文本与 objectName、fake 日志服务收到的调用与顺序、注入的目录打开器参数、
fake controller 的 open/close/write 调用；不断言私有属性。

运行：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_main_window_log.py -q``
"""

import os
import queue

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from paimon_assistant.receive_log import ReceiveLogError  # noqa: E402


@dataclass(frozen=True)
class FakeEvent:
    """按 duck-typing 契约构造的接收事件（received_at_ms / payload / raw_frame）。"""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class FakeController:
    """最小 fake：提供接收/诊断/错误三个队列与端口/打开/写入接口。"""

    def __init__(self, ports=("COM3",)):
        self.ports = list(ports)
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.opened_settings = []
        self.close_calls = 0
        self.writes = []

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        self.opened_settings.append(settings)
        return True

    def close(self):
        self.close_calls += 1

    def write(self, data: bytes):
        self.writes.append(bytes(data))


class FakeLogService:
    """日志服务测试替身：记录调用与目录操作，可注入写失败。"""

    def __init__(self, log_dir, *, fail_writes=0, start_fused=False):
        self.log_dir = Path(log_dir)
        self.calls = []  # 每次 write_event 收到的 event（含熔断后的调用）
        self.events = []  # 成功写入的 event
        self.fs_attempts = 0  # 实际访问文件系统的次数（熔断后不再增长）
        self.ensure_calls = 0
        self.ensure_error = None  # 设置后 ensure_directory() 抛出
        self.on_write = None  # 可选回调：在 write_event 调用点观察外部状态
        self._remaining_failures = fail_writes
        self._fused = start_fused

    def ensure_directory(self):
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    def write_event(self, event):
        self.calls.append(event)
        if self.on_write is not None:
            self.on_write(event)
        if self._fused:  # 熔断：不访问文件系统、不缓存、不重试
            return False
        self.fs_attempts += 1
        if self._remaining_failures:
            self._remaining_failures -= 1
            self._fused = True
            raise ReceiveLogError("Receive log write failed: 磁盘已满")
        self.events.append(event)
        return True


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


@pytest.fixture
def controller():
    return FakeController()


def _window(qtbot, mw, controller, log_service, log_dir_opener=None):
    win = mw.MainWindow(
        controller=controller,
        log_service=log_service,
        log_dir_opener=log_dir_opener,
    )
    qtbot.addWidget(win)
    return win


def _open(window):
    window.open_button.click()
    assert window.open_button.text() == "关闭"


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


@pytest.fixture
def dialogs(mw, monkeypatch):
    """记录 QMessageBox.critical / warning 调用，不弹真实对话框。"""
    rec = {"critical": [], "warning": []}

    def _critical(*args, **kwargs):
        rec["critical"].append((args, kwargs))
        return mw.QMessageBox.Ok

    def _warning(*args, **kwargs):
        rec["warning"].append((args, kwargs))
        return mw.QMessageBox.Ok

    monkeypatch.setattr(mw.QMessageBox, "critical", staticmethod(_critical))
    monkeypatch.setattr(mw.QMessageBox, "warning", staticmethod(_warning))
    return rec


LOG_WRITE_FAILED_TEXT = "日志写入失败，请检查磁盘空间或权限"


# ------------------------------------------------- 事件 -> 显示 -> 日志顺序


def test_batch_is_displayed_before_being_written_to_log_in_order(
    tmp_path, qtbot, mw, controller
):
    log_service = FakeLogService(tmp_path / "logs")
    window = _window(qtbot, mw, controller, log_service)
    window.timestamp_checkbox.setChecked(False)

    first = FakeEvent(1000, b"first", b"first\r\n")
    second = FakeEvent(2000, b"second", b"second\r\n")
    controller.received_queue.put(first)
    controller.received_queue.put(second)

    display_at_write = []
    log_service.on_write = lambda event: display_at_write.append(
        window.display_edit.toPlainText()
    )

    window._drain_queues()

    assert log_service.calls == [first, second]
    # 写入日志时，同一批事件已全部纳入历史并完成显示更新
    assert display_at_write == ["first\nsecond\n", "first\nsecond\n"]


# --------------------------------------------------------- 日志失败隔离


def test_first_log_failure_shows_fixed_red_label_and_keeps_receiving(
    tmp_path, qtbot, mw, controller, dialogs
):
    log_service = FakeLogService(tmp_path / "logs", fail_writes=1)
    window = _window(qtbot, mw, controller, log_service)
    window.timestamp_checkbox.setChecked(False)

    first = FakeEvent(0, b"first", b"first\r\n")
    second = FakeEvent(0, b"second", b"second\r\n")
    controller.received_queue.put(first)
    window._drain_queues()

    label = window.log_error_label
    assert label.objectName() == "log_error_label"
    assert label.text() == LOG_WRITE_FAILED_TEXT
    assert "red" in label.styleSheet()
    assert dialogs["critical"] == [] and dialogs["warning"] == []
    # 日志故障不丢显示
    assert window.display_edit.toPlainText() == "first\n"

    # 熔断后：后续事件继续显示，仍逐条交给服务一次（无缓存、无重试），提示保持
    controller.received_queue.put(second)
    window._drain_queues()
    assert log_service.calls == [first, second]
    assert log_service.fs_attempts == 1  # 只触发一次文件操作，不反复重试
    assert window.display_edit.toPlainText() == "first\nsecond\n"
    assert label.text() == LOG_WRITE_FAILED_TEXT
    assert dialogs["critical"] == [] and dialogs["warning"] == []

    # 只处理首次错误提示：后续明确状态替换后，不再被写失败提示覆盖
    log_service.ensure_error = ReceiveLogError("目录不可用")
    window.log_dir_button.click()
    assert window.log_error_label.text() == "日志目录打开失败：目录不可用"
    controller.received_queue.put(FakeEvent(0, b"third", b"third\r\n"))
    window._drain_queues()
    assert window.log_error_label.text() == "日志目录打开失败：目录不可用"


def test_fused_service_returning_false_shows_label_and_never_retries(
    tmp_path, qtbot, mw, controller, dialogs
):
    """注入一个已熔断的服务：write_event 直接返回 False，不抛异常。"""
    log_service = FakeLogService(tmp_path / "logs", start_fused=True)
    window = _window(qtbot, mw, controller, log_service)
    window.timestamp_checkbox.setChecked(False)

    first = FakeEvent(0, b"first", b"first\r\n")
    second = FakeEvent(0, b"second", b"second\r\n")
    controller.received_queue.put(first)
    window._drain_queues()

    assert window.log_error_label.text() == LOG_WRITE_FAILED_TEXT
    assert window.display_edit.toPlainText() == "first\n"

    # 每个后续事件仍调用一次（由服务自身熔断），不缓存、不重试
    controller.received_queue.put(second)
    window._drain_queues()
    assert log_service.calls == [first, second]
    assert window.display_edit.toPlainText() == "first\nsecond\n"
    assert window.log_error_label.text() == LOG_WRITE_FAILED_TEXT
    assert dialogs["critical"] == [] and dialogs["warning"] == []


# ------------------------------------------------------------ 分帧诊断


def test_diagnostic_queue_shows_fixed_text_without_blocking_display(
    tmp_path, qtbot, mw, controller, dialogs
):
    log_service = FakeLogService(tmp_path / "logs")
    window = _window(qtbot, mw, controller, log_service)
    window.timestamp_checkbox.setChecked(False)

    controller.diagnostic_queue.put("接收帧超过 1 MiB，已丢弃")
    controller.received_queue.put(FakeEvent(0, b"after", b"after\r\n"))
    window._drain_queues()

    label = window.receive_error_label
    assert label.text() == "接收帧超过 1 MiB，已丢弃"
    assert "red" in label.styleSheet()
    assert dialogs["critical"] == [] and dialogs["warning"] == []
    # 诊断不阻断正常显示与日志
    assert window.display_edit.toPlainText() == "after\n"
    assert len(log_service.events) == 1


def test_diagnostics_are_ignored_when_controller_has_no_diagnostic_queue(
    tmp_path, qtbot, mw, controller
):
    """兼容尚未提供诊断队列的旧 controller：不得因缺少属性而报错。"""
    del controller.diagnostic_queue
    window = _window(qtbot, mw, controller, FakeLogService(tmp_path / "logs"))
    window.timestamp_checkbox.setChecked(False)

    controller.received_queue.put(FakeEvent(0, b"ok", b"ok\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "ok\n"


# ------------------------------------------------------------ 日志目录入口


def test_log_dir_button_creates_and_opens_log_directory(tmp_path, qtbot, mw, controller):
    log_dir = tmp_path / "logs"
    log_service = FakeLogService(log_dir)
    opened = []
    window = _window(
        qtbot,
        mw,
        controller,
        log_service,
        log_dir_opener=lambda path: opened.append(path),
    )

    button = window.log_dir_button
    assert button.objectName() == "log_dir_button"
    assert button.text() == "日志目录"

    button.click()

    assert log_service.ensure_calls == 1
    assert opened == [log_dir]  # 打开目录本身，而不是某个日期文件
    assert window.log_error_label.text() == ""


def test_log_dir_ensure_failure_shows_reason_and_keeps_receiving(
    tmp_path, qtbot, mw, controller, dialogs
):
    log_service = FakeLogService(tmp_path / "logs")
    log_service.ensure_error = ReceiveLogError("目录不可用：权限不足")
    opened = []
    window = _window(
        qtbot,
        mw,
        controller,
        log_service,
        log_dir_opener=lambda path: opened.append(path),
    )
    _open(window)

    window.log_dir_button.click()

    assert window.log_error_label.text() == "日志目录打开失败：目录不可用：权限不足"
    assert opened == []
    assert controller.close_calls == 0
    assert window.open_button.text() == "关闭"

    # 接收、显示与日志不受影响
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(FakeEvent(0, b"alive", b"alive\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "alive\n"
    assert len(log_service.events) == 1
    assert dialogs["critical"] == [] and dialogs["warning"] == []


def test_log_dir_opener_failure_shows_reason_and_keeps_receiving(
    tmp_path, qtbot, mw, controller, dialogs
):
    log_service = FakeLogService(tmp_path / "logs")

    def _boom(path):
        raise OSError("没有关联的应用")

    window = _window(
        qtbot, mw, controller, log_service, log_dir_opener=_boom
    )
    _open(window)

    window.log_dir_button.click()

    assert window.log_error_label.text() == "日志目录打开失败：没有关联的应用"
    assert log_service.ensure_calls == 1
    assert controller.close_calls == 0
    assert window.open_button.text() == "关闭"

    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(FakeEvent(0, b"alive", b"alive\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "alive\n"
    assert len(log_service.events) == 1
    assert dialogs["critical"] == [] and dialogs["warning"] == []


# ------------------------------------------------- 显示切换 / 发送回归


def test_display_switches_do_not_change_event_history_or_log_content(
    tmp_path, qtbot, mw, controller
):
    log_service = FakeLogService(tmp_path / "logs")
    window = _window(qtbot, mw, controller, log_service)

    gbk_bytes = "你好".encode("gbk")
    first = FakeEvent(0, gbk_bytes, gbk_bytes + b"\r\n")
    second = FakeEvent(1000, b"world", b"world\r\n")
    controller.received_queue.put(first)
    controller.received_queue.put(second)
    window._drain_queues()
    assert log_service.calls == [first, second]

    # 切换时间戳 / HEX / 编码只重绘显示
    window.timestamp_checkbox.setChecked(False)
    _select(window.receive_mode_combo, "HEX")
    _select(window.encoding_combo, "GBK")

    expected_hex = "\n".join(
        " ".join(f"{b:02X}" for b in event.raw_frame) for event in (first, second)
    )
    assert window.display_edit.toPlainText() == expected_hex + "\n"
    assert log_service.calls == [first, second]  # 显示设置不影响日志内容与次数

    # 切换后新事件仍按原样进日志
    third = FakeEvent(2000, b"tail", b"tail\r\n")
    controller.received_queue.put(third)
    window._drain_queues()
    assert log_service.calls == [first, second, third]


def test_send_writes_only_to_controller_and_never_to_receive_log(
    tmp_path, qtbot, mw, controller
):
    log_service = FakeLogService(tmp_path / "logs")
    window = _window(qtbot, mw, controller, log_service)
    _open(window)

    window.send_edit.setText("hello")
    window.send_button.click()
    assert controller.writes == [b"hello"]

    _select(window.send_mode_combo, "HEX")
    window.send_edit.setText("48 65 6C 6C 6F")
    window.send_button.click()
    assert controller.writes == [b"hello", b"Hello"]

    # 发送不生成 ReceivedEvent、不写 RX 日志、不进入接收显示
    assert log_service.calls == []
    assert log_service.events == []
    assert window.display_edit.toPlainText() == ""
    assert window.receive_error_label.text() == ""
