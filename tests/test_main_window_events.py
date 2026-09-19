"""MainWindow 接收事件显示行为测试（offscreen，REQ-0003 §6，issue 007）。

只断言外部可观察行为：控件存在性、控件文本/状态、显示区 ``toPlainText()``
和队列消费结果；不断言私有属性。

运行：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_main_window_events.py -q``
"""

import os
import queue

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402

from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.theme import COLORS  # noqa: E402


@dataclass(frozen=True)
class FakeEvent:
    """按 duck-typing 契约构造的事件（received_at_ms / payload / raw_frame）。"""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class FakeController:
    """最小 fake：只提供 MainWindow 消费的队列与端口/打开接口。"""

    def __init__(self):
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()

    def list_ports(self):
        return []

    def open(self, settings):
        return True

    def close(self):
        pass

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        """模拟真实控制器：清空时替换队列并丢弃待处理旧事件（REQ §10.1）。"""
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
    # 注入临时日志目录：整套测试不得写入真实 %LOCALAPPDATA%（REQ §14.5）。
    win = mw.MainWindow(
        controller=controller, log_service=ReceiveLogService(tmp_path / "logs")
    )
    qtbot.addWidget(win)
    return win


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


def _local_hms(ms: int) -> str:
    """用标准库本地时区把 epoch 毫秒换算成 HH:mm:ss.SSS。"""
    lt = time.localtime(ms // 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms % 1000:03d}"


# ---------- 事件历史显示 ----------


def test_drained_event_is_displayed_without_timestamp_prefix(window, controller):
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(FakeEvent(0, b"Hello", b"Hello\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "Hello\n"


# ---------- 诊断/错误提示标签 ----------

def test_receive_error_label_exists_red_and_initially_empty(window):
    label = window.receive_error_label
    assert label.objectName() == "receive_error_label"
    assert label.text() == ""
    assert COLORS["error"] in label.styleSheet()


def test_clear_also_clears_receive_error_label(window, controller):
    window.receive_error_label.setText("接收帧超过 1 MiB，已丢弃")
    controller.received_queue.put(FakeEvent(0, b"abc", b"abc\r\n"))
    window._drain_queues()

    window.clear_button.click()

    assert window.display_edit.toPlainText() == ""
    assert window.receive_error_label.text() == ""


# ---------- 时间戳开关 ----------


def test_timestamp_checkbox_is_checked_by_default(window):
    assert window.timestamp_checkbox.objectName() == "timestamp_checkbox"
    assert window.timestamp_checkbox.isChecked()


def test_display_timestamp_prefix_matches_local_time(window, controller):
    ms = 1_700_000_000_123
    controller.received_queue.put(FakeEvent(ms, b"Hi", b"Hi\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == f"[{_local_hms(ms)}] Hi\n"


def test_switching_timestamp_and_mode_redraws_history(window, controller):
    controller.received_queue.put(FakeEvent(0, b"Hi", b"Hi\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == f"[{_local_hms(0)}] Hi\n"

    window.timestamp_checkbox.setChecked(False)
    assert window.display_edit.toPlainText() == "Hi\n"

    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == "48 69 0D 0A\n"

    window.timestamp_checkbox.setChecked(True)
    assert window.display_edit.toPlainText() == f"[{_local_hms(0)}] 48 69 0D 0A\n"


def test_switching_encoding_redraws_gbk_payload(window, controller):
    payload = "你好".encode("gbk")
    controller.received_queue.put(FakeEvent(0, payload, payload + b"\r\n"))
    window._drain_queues()

    window.timestamp_checkbox.setChecked(False)
    _select(window.encoding_combo, "GBK")
    assert window.display_edit.toPlainText() == "你好\n"


def test_display_switching_keeps_event_history_and_raw_bytes(window, controller):
    payload = "你好".encode("gbk")
    raw_frame = payload + b"\r\n"
    event = FakeEvent(0, payload, raw_frame)
    controller.received_queue.put(event)
    window._drain_queues()

    _select(window.receive_mode_combo, "HEX")
    hex_render = window.display_edit.toPlainText()
    expected_hex = " ".join(f"{b:02X}" for b in raw_frame)
    assert hex_render == f"[{_local_hms(0)}] {expected_hex}\n"

    # 来回切换文本/HEX/编码/时间戳后，同一 HEX 视图必须与最初完全一致
    window.timestamp_checkbox.setChecked(False)
    _select(window.receive_mode_combo, "文本")
    _select(window.encoding_combo, "GBK")
    assert window.display_edit.toPlainText() == "你好\n"
    window.timestamp_checkbox.setChecked(True)
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == hex_render

    # 事件本身与原始字节不因显示切换改变
    assert (event.payload, event.raw_frame) == (payload, raw_frame)


# ---------- 多事件顺序与空帧 ----------


def test_multiple_events_are_displayed_in_arrival_order(window, controller):
    controller.received_queue.put(FakeEvent(0, b"one", b"one\r\n"))
    controller.received_queue.put(FakeEvent(1000, b"two", b"two\r\n"))
    window._drain_queues()
    window.timestamp_checkbox.setChecked(False)
    assert window.display_edit.toPlainText() == "one\ntwo\n"


def test_empty_frame_event_displays_timestamp_only_line(window, controller):
    controller.received_queue.put(FakeEvent(0, b"", b"\r\n"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == f"[{_local_hms(0)}] \n"
