"""MainWindow 接收解析模式（协议分帧 / 原始字节）测试（offscreen，REQ-0004）。

只断言外部可观察行为：控件存在性、控件文本/启用状态、显示区 ``toPlainText()``、
日志文件内容与队列消费结果；不断言私有属性。

运行：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_main_window_parse_mode.py -q``
"""

import os
import queue

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402

from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402

FRAMED_MODE = "按 \\r\\n 分帧"
RAW_MODE = "原始字节"


@dataclass(frozen=True)
class FakeEvent:
    """按 duck-typing 契约构造的事件（received_at_ms / payload / raw_frame）。"""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes


class FakeController:
    """最小 fake：提供 MainWindow 消费的四个队列与模式接口。"""

    def __init__(self):
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.raw_mode = False

    def list_ports(self):
        return []

    def open(self, settings):
        return True

    def close(self):
        pass

    def write(self, data):
        pass

    def set_raw_mode(self, enabled):
        self.raw_mode = bool(enabled)

    def reset_receive_session(self):
        """模拟真实控制器：清空时替换队列并丢弃待处理数据（REQ-0003 §10.1）。"""
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
    qtbot.addWidget(win)
    return win


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


@pytest.fixture
def raw_window(window):
    _select(window.parse_mode_combo, RAW_MODE)
    return window


def _log_text(tmp_path):
    files = sorted((tmp_path / "logs").glob("*.txt"))
    return [f.read_text(encoding="utf-8") for f in files]


# ---------- 控件契约 ----------


def test_parse_mode_combo_exists_with_framed_default(window):
    combo = window.parse_mode_combo
    assert combo.objectName() == "parse_mode_combo"
    assert [combo.itemText(i) for i in range(combo.count())] == [FRAMED_MODE, RAW_MODE]
    assert combo.currentText() == FRAMED_MODE


def test_default_framed_mode_keeps_timestamp_checkbox_enabled(window):
    assert window.timestamp_checkbox.isEnabled() is True


def test_raw_mode_disables_the_timestamp_checkbox(raw_window):
    assert raw_window.timestamp_checkbox.isEnabled() is False
    _select(raw_window.parse_mode_combo, FRAMED_MODE)
    assert raw_window.timestamp_checkbox.isEnabled() is True


def test_selecting_raw_mode_asks_the_controller_for_raw_receipt(raw_window, controller):
    assert controller.raw_mode is True
    _select(raw_window.parse_mode_combo, FRAMED_MODE)
    assert controller.raw_mode is False


# ---------- 模式提示不进入正文 ----------


def test_framed_placeholder_is_not_received_text(window):
    hint = window.display_edit.placeholderText()
    assert "等待完整帧" in hint
    assert "\\r\\n" in hint
    assert "原始字节" in hint
    assert window.display_edit.toPlainText() == ""
    assert window.display_edit.document().isEmpty()


def test_raw_hint_and_disabled_timestamp_reason_follow_mode(window):
    window.timestamp_checkbox.setChecked(False)
    _select(window.parse_mode_combo, RAW_MODE)
    assert "收到即显示" in window.display_edit.placeholderText()
    assert "不记录日志" in window.display_edit.placeholderText()
    assert "时间戳" in window.timestamp_checkbox.toolTip()
    assert "原始字节" in window.timestamp_checkbox.toolTip()
    assert not window.timestamp_checkbox.isEnabled()
    assert not window.timestamp_checkbox.isChecked()
    assert window.display_edit.toPlainText() == ""

    _select(window.parse_mode_combo, FRAMED_MODE)
    assert window.timestamp_checkbox.isEnabled()
    assert not window.timestamp_checkbox.isChecked()
    assert "原始字节" not in window.timestamp_checkbox.toolTip()
    assert "等待完整帧" in window.display_edit.placeholderText()


@pytest.mark.parametrize("mode", [FRAMED_MODE, RAW_MODE])
def test_clear_restores_empty_placeholder_without_logging_it(
    window, controller, tmp_path, mode
):
    _select(window.parse_mode_combo, mode)
    if mode == RAW_MODE:
        controller.raw_queue.put(b"hello")
    else:
        controller.received_queue.put(FakeEvent(0, b"hello", b"hello\r\n"))
    window._drain_queues()
    assert "hello" in window.display_edit.toPlainText()
    logs = _log_text(tmp_path)
    window.clear_button.click()
    assert window.display_edit.document().isEmpty()
    assert window.display_edit.placeholderText()
    assert _log_text(tmp_path) == logs


# ---------- 原始字节显示 ----------


def test_raw_bytes_without_terminator_are_displayed_immediately(raw_window, controller):
    controller.raw_queue.put(b"hello")
    raw_window._drain_queues()

    assert raw_window.display_edit.toPlainText() == "hello"


def test_raw_mode_does_not_split_on_cr_lf(raw_window, controller):
    # 一条连续流：不分帧、不逐事件换行、也没有时间戳前缀。
    _select(raw_window.receive_mode_combo, "HEX")
    controller.raw_queue.put(b"A\r\nB\r\n")
    raw_window._drain_queues()

    assert raw_window.display_edit.toPlainText() == "41 0D 0A 42 0D 0A"


def test_raw_mode_splits_nothing_in_text_mode_either(raw_window, controller):
    controller.raw_queue.put(b"A\r\nB\r\n")
    raw_window._drain_queues()

    text = raw_window.display_edit.toPlainText()
    assert "[" not in text  # 原始模式不使用事件时间戳前缀
    assert "A" in text and "B" in text


def test_raw_mode_accumulates_successive_chunks(raw_window, controller):
    controller.raw_queue.put(b"hel")
    raw_window._drain_queues()
    controller.raw_queue.put(b"lo")
    raw_window._drain_queues()

    assert raw_window.display_edit.toPlainText() == "hello"


def test_raw_mode_hex_view_renders_every_byte_uppercase(raw_window, controller):
    _select(raw_window.receive_mode_combo, "HEX")
    controller.raw_queue.put(b"\x00\xab\xff")
    raw_window._drain_queues()

    assert raw_window.display_edit.toPlainText() == "00 AB FF"


def test_raw_mode_keeps_split_multibyte_char_pending(raw_window, controller):
    # "中" = E4 B8 AD：前半到达时不得输出替换字符，补齐后才显示。
    controller.raw_queue.put(b"\xe4\xb8")
    raw_window._drain_queues()
    assert "\ufffd" not in raw_window.display_edit.toPlainText()
    assert raw_window.display_edit.toPlainText() == ""

    controller.raw_queue.put(b"\xad")
    raw_window._drain_queues()
    assert raw_window.display_edit.toPlainText() == "中"


def test_raw_mode_renders_with_gbk_encoding(raw_window, controller):
    _select(raw_window.encoding_combo, "GBK")
    controller.raw_queue.put("中".encode("gbk"))
    raw_window._drain_queues()

    assert raw_window.display_edit.toPlainText() == "中"


def test_raw_mode_switching_text_hex_and_encoding_redraws_history(raw_window, controller):
    controller.raw_queue.put(b"AB")
    raw_window._drain_queues()

    _select(raw_window.receive_mode_combo, "HEX")
    assert raw_window.display_edit.toPlainText() == "41 42"
    _select(raw_window.receive_mode_combo, "文本")
    assert raw_window.display_edit.toPlainText() == "AB"


# ---------- 与日志 / 事件的隔离 ----------


def test_raw_mode_writes_no_rx_log(raw_window, controller, tmp_path):
    controller.raw_queue.put(b"hello")
    raw_window._drain_queues()

    assert _log_text(tmp_path) == []


def test_raw_mode_produces_no_receive_events(raw_window, controller):
    controller.received_queue.put(FakeEvent(1_700_000_000_000, b"X", b"X\r\n"))
    controller.raw_queue.put(b"hello")
    raw_window._drain_queues()

    # 只剩原始字节内容，事件通道没有被原始模式消费
    assert raw_window.display_edit.toPlainText() == "hello"


def test_framed_mode_still_labels_events_with_timestamps(window, controller):
    controller.received_queue.put(FakeEvent(1_700_000_000_125, b"X", b"X\r\n"))
    window._drain_queues()

    text = window.display_edit.toPlainText()
    assert text.startswith("[")
    assert text.endswith("X\n")


def test_switching_between_modes_keeps_both_histories(window, controller):
    controller.received_queue.put(FakeEvent(1_700_000_000_000, b"E", b"E\r\n"))
    window._drain_queues()
    assert "E" in window.display_edit.toPlainText()

    _select(window.parse_mode_combo, RAW_MODE)
    controller.raw_queue.put(b"raw")
    window._drain_queues()
    assert window.display_edit.toPlainText() == "raw"

    _select(window.parse_mode_combo, FRAMED_MODE)
    assert "E" in window.display_edit.toPlainText()
    _select(window.parse_mode_combo, RAW_MODE)
    assert window.display_edit.toPlainText() == "raw"


def test_raw_bytes_never_become_events_after_switching_to_framed(
    raw_window, controller, tmp_path
):
    controller.raw_queue.put(b"A\r\n")
    raw_window._drain_queues()

    _select(raw_window.parse_mode_combo, FRAMED_MODE)
    raw_window._drain_queues()

    assert _log_text(tmp_path) == []
    assert raw_window.display_edit.toPlainText() == ""


# ---------- 清空 ----------


def test_clear_drops_raw_history_and_pending_raw_queue(raw_window, controller):
    controller.raw_queue.put(b"first")
    raw_window._drain_queues()
    controller.raw_queue.put(b"pending")

    raw_window._on_clear()

    assert raw_window.display_edit.toPlainText() == ""
    controller.raw_queue.put(b"after")
    raw_window._drain_queues()
    assert raw_window.display_edit.toPlainText() == "after"


def test_clear_drops_both_event_and_raw_histories(window, controller):
    controller.received_queue.put(FakeEvent(1_700_000_000_000, b"E", b"E\r\n"))
    window._drain_queues()
    _select(window.parse_mode_combo, RAW_MODE)
    controller.raw_queue.put(b"raw")
    window._drain_queues()

    window._on_clear()
    _select(window.parse_mode_combo, FRAMED_MODE)

    assert window.display_edit.toPlainText() == ""


def test_clear_never_touches_log_files(window, controller, tmp_path):
    controller.received_queue.put(FakeEvent(1_700_000_000_000, b"E", b"E\r\n"))
    window._drain_queues()
    before = _log_text(tmp_path)
    assert before  # 已有一条 RX 记录

    window._on_clear()

    assert _log_text(tmp_path) == before
