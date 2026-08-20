"""MainWindow 行为测试。

固定 API 契约（来自任务书）：
- ``paimon_assistant.main_window.MainWindow(controller=...)``
- 公开控件属性：port_combo / baud_combo / data_bits_combo / parity_combo /
  stop_bits_combo / receive_mode_combo / encoding_combo / display_edit /
  clear_button / open_button / refresh_button / send_edit / send_mode_combo / send_button
- controller 契约（fake 内联定义）：``list_ports() -> [str]``、
  ``open(settings)``（settings 含 port/baudrate/bytesize/parity/stopbits）、
  ``close()``、``write(bytes)``、``received_queue`` / ``error_queue``。
- 允许测试直接调用 ``window._drain_queues()`` 形成确定性断言。
- 按钮文案约定：打开态 "打开"，打开成功/运行中 "关闭"。

运行：``QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_main_window.py -q``
"""

import importlib
import os
import queue

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

PUBLIC_WIDGETS = [
    "port_combo",
    "baud_combo",
    "data_bits_combo",
    "parity_combo",
    "stop_bits_combo",
    "receive_mode_combo",
    "encoding_combo",
    "display_edit",
    "clear_button",
    "open_button",
    "refresh_button",
    "send_edit",
    "send_mode_combo",
    "send_button",
]


class FakeController:
    """内联 fake：记录 open/close/write 调用，提供接收/错误队列。"""

    def __init__(self, ports=("COM3", "COM4")):
        self.ports = list(ports)
        self.received_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.opened_settings = []  # 每次 open 收到的 SerialSettings
        self.close_calls = 0
        self.writes = []  # write(bytes) 记录
        self.list_ports_calls = 0
        self.open_exc = None  # 若设置，open() 抛出该异常

    def list_ports(self):
        self.list_ports_calls += 1
        return list(self.ports)

    def open(self, settings):
        self.opened_settings.append(settings)
        if self.open_exc is not None:
            raise self.open_exc
        return True

    def close(self):
        self.close_calls += 1

    def write(self, data: bytes):
        self.writes.append(bytes(data))


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


@pytest.fixture
def controller():
    return FakeController()


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


@pytest.fixture
def window(qtbot, mw, controller):
    win = mw.MainWindow(controller=controller)
    qtbot.addWidget(win)
    return win


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


def _open(window):
    window.open_button.click()
    assert window.open_button.text() == "关闭"


def _combo_items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


# ---------- 窗口与控件 ----------


def test_window_title(window):
    assert window.windowTitle() == "派蒙助手"


def test_public_widget_attributes_exist(window):
    for name in PUBLIC_WIDGETS:
        assert getattr(window, name) is not None, f"缺少公开属性 {name}"


# ---------- 端口枚举 ----------


def test_startup_auto_enumeration(window, controller):
    assert controller.list_ports_calls >= 1
    assert _combo_items(window.port_combo) == ["COM3", "COM4"]
    assert window.port_combo.currentText() == "COM3"


def test_refresh_re_enumerates_ports(window, controller):
    controller.ports = ["COM7"]
    before = controller.list_ports_calls
    window.refresh_button.click()
    assert controller.list_ports_calls == before + 1
    assert "COM7" in _combo_items(window.port_combo)


# ---------- 端口轮询与差量更新 ----------


def test_poll_timer_interval_1000ms_and_active(window):
    """轮询定时器约 1 秒驱动 monitor.tick()。"""
    assert window._poll_timer.interval() == 1000
    assert window._poll_timer.isActive()


def test_new_port_appears_without_refresh(window, controller):
    """新插入的端口不点「刷新」，下一次 tick 即出现在列表中。"""
    controller.ports = ["COM3", "COM4", "COM9"]
    window._monitor.tick()
    assert _combo_items(window.port_combo) == ["COM3", "COM4", "COM9"]


def test_removed_port_disappears_without_refresh(window, controller):
    """被拔掉的端口下一次 tick 即从列表消失。"""
    controller.ports = ["COM4"]
    window._monitor.tick()
    assert _combo_items(window.port_combo) == ["COM4"]


def test_same_snapshot_tick_keeps_list_unchanged(window, controller):
    before = _combo_items(window.port_combo)
    window._monitor.tick()
    assert _combo_items(window.port_combo) == before


def test_refresh_uses_diff_path_without_rebuild(window, controller):
    """手动刷新走差量路径：未变化的项不被 clear/rebuild，保留对象数据。"""
    window.port_combo.setItemData(0, "keep-me")
    before = controller.list_ports_calls
    window.refresh_button.click()
    assert controller.list_ports_calls == before + 1
    assert _combo_items(window.port_combo) == ["COM3", "COM4"]
    assert window.port_combo.itemData(0) == "keep-me"


def test_list_updates_while_open_and_combo_stays_disabled(window, controller):
    """连接打开期间列表数据仍更新，下拉框保持禁用。"""
    _open(window)
    controller.ports = ["COM3", "COM9"]
    window._monitor.tick()
    assert _combo_items(window.port_combo) == ["COM3", "COM9"]
    assert not window.port_combo.isEnabled()
    assert window.refresh_button.isEnabled()
    assert window.open_button.text() == "关闭"


# ---------- REQ-0002 selection and removal policies ----------


def test_auto_selects_first_added_port_when_selection_is_empty(window, controller):
    window.port_combo.setCurrentIndex(-1)
    controller.ports = ["COM3", "COM4", "COM9", "COM10"]
    window._monitor.tick()

    assert _combo_items(window.port_combo) == ["COM3", "COM4", "COM9", "COM10"]
    assert window.port_combo.currentText() == "COM9"
    assert controller.opened_settings == []


def test_preserves_valid_selection_when_port_is_added(window, controller):
    _select(window.port_combo, "COM4")
    controller.ports = ["COM3", "COM4", "COM9"]
    window._monitor.tick()

    assert _combo_items(window.port_combo) == ["COM3", "COM4", "COM9"]
    assert window.port_combo.currentText() == "COM4"
    assert controller.opened_settings == []


def test_selects_first_new_port_when_selected_port_is_removed_same_tick(
    window, controller
):
    assert window.port_combo.currentText() == "COM3"
    controller.ports = ["COM4", "COM9", "COM10"]
    window._monitor.tick()

    assert _combo_items(window.port_combo) == ["COM4", "COM9", "COM10"]
    assert window.port_combo.currentText() == "COM9"
    assert controller.opened_settings == []


def test_removed_unselected_port_is_silent(window, controller, dialogs):
    _select(window.port_combo, "COM4")
    controller.ports = ["COM4"]
    window._monitor.tick()

    assert _combo_items(window.port_combo) == ["COM4"]
    assert window.port_combo.currentText() == "COM4"
    assert dialogs["warning"] == []


def test_removed_selected_unconnected_port_leaves_selection_empty(
    window, controller, dialogs
):
    _select(window.port_combo, "COM3")
    controller.ports = ["COM4"]
    window._monitor.tick()

    assert _combo_items(window.port_combo) == ["COM4"]
    assert window.port_combo.currentIndex() == -1
    assert window.port_combo.currentText() == ""
    assert controller.opened_settings == []
    assert dialogs["warning"] == []


# ---------- REQ-0002 connected-port debounce ----------


def test_connected_port_loss_closes_once_warns_once_and_keeps_diff_removal(
    window, controller, dialogs
):
    _open(window)
    controller.ports = ["COM4"]

    window._monitor.tick()
    assert controller.close_calls == 0
    assert dialogs["warning"] == []
    assert _combo_items(window.port_combo) == ["COM4"]
    assert window.open_button.text() == "关闭"

    window._monitor.tick()
    assert controller.close_calls == 1
    assert len(dialogs["warning"]) == 1
    assert dialogs["warning"][0][0][2] == "串口已拔出，连接已关闭"
    assert window.open_button.text() == "打开"
    assert window.port_combo.isEnabled()

    window._monitor.tick()
    assert controller.close_calls == 1
    assert len(dialogs["warning"]) == 1


def test_connected_port_recovery_does_not_close_or_warn(window, controller, dialogs):
    _open(window)
    controller.ports = []
    window._monitor.tick()
    controller.ports = ["COM3"]
    window._monitor.tick()

    assert controller.close_calls == 0
    assert dialogs["warning"] == []

    controller.ports = []
    window._monitor.tick()
    window._monitor.tick()
    assert controller.close_calls == 1
    assert len(dialogs["warning"]) == 1


def test_manual_close_clears_loss_tracking(window, controller, dialogs):
    _open(window)
    window.open_button.click()
    assert controller.close_calls == 1

    controller.ports = []
    window._monitor.tick()
    window._monitor.tick()
    assert controller.close_calls == 1
    assert dialogs["warning"] == []


def test_read_error_closes_without_lost_warning(window, controller, dialogs):
    _open(window)
    controller.error_queue.put(OSError("串口异常断开"))
    window._drain_queues()
    assert controller.close_calls == 1
    assert len(dialogs["critical"]) == 1

    controller.ports = []
    window._monitor.tick()
    window._monitor.tick()
    assert dialogs["warning"] == []


# ---------- 默认参数 ----------


def test_baud_editable_default_115200(window):
    assert window.baud_combo.isEditable()
    assert window.baud_combo.currentText() == "115200"


def test_advanced_defaults_8n1(window):
    assert window.data_bits_combo.currentText() == "8"
    assert window.parity_combo.currentText() == "N"
    assert window.stop_bits_combo.currentText() == "1"


def test_default_modes_and_encoding(window):
    assert window.receive_mode_combo.currentText() == "文本"
    assert window.send_mode_combo.currentText() == "文本"
    assert window.encoding_combo.currentText() == "UTF-8"


# ---------- 打开 / 关闭 ----------


def test_open_passes_settings_and_disables_config(window, controller):
    window.baud_combo.setEditText("460800")  # 手动输入任意波特率
    _select(window.data_bits_combo, "8")
    _select(window.parity_combo, "N")
    _select(window.stop_bits_combo, "1")
    window.open_button.click()

    assert len(controller.opened_settings) == 1
    s = controller.opened_settings[0]
    assert s.port == "COM3"
    assert s.baudrate == 460800
    assert s.bytesize == 8
    assert s.parity == "N"
    assert s.stopbits == 1

    # 打开后：按钮变“关闭”，配置控件禁用
    assert window.open_button.text() == "关闭"
    assert not window.port_combo.isEnabled()
    assert not window.baud_combo.isEnabled()
    assert not window.data_bits_combo.isEnabled()
    assert not window.parity_combo.isEnabled()
    assert not window.stop_bits_combo.isEnabled()


def test_open_failure_shows_critical_and_survives(window, controller, dialogs):
    controller.open_exc = OSError("端口被占用")
    window.open_button.click()

    assert len(dialogs["critical"]) == 1
    # 不崩：仍处于关闭状态，可再次打开
    assert window.open_button.text() == "打开"
    assert window.port_combo.isEnabled()

    controller.open_exc = None
    window.open_button.click()
    assert len(controller.opened_settings) == 2
    assert window.open_button.text() == "关闭"


def test_close_restores_state(window, controller):
    _open(window)
    assert not window.port_combo.isEnabled()

    window.open_button.click()  # 再次点击即关闭
    assert controller.close_calls == 1
    assert window.open_button.text() == "打开"
    assert window.port_combo.isEnabled()
    assert window.baud_combo.isEnabled()
    assert window.data_bits_combo.isEnabled()
    assert window.parity_combo.isEnabled()
    assert window.stop_bits_combo.isEnabled()


# ---------- 接收显示 ----------


def test_drain_received_queue_shows_text(window, controller):
    controller.received_queue.put("你好".encode("utf-8"))
    controller.received_queue.put(b" world")
    window._drain_queues()
    assert window.display_edit.toPlainText() == "你好 world"


def test_switch_to_hex_rerenders_history_uppercase_space_separated(window, controller):
    controller.received_queue.put(b"\x01\x02\xab\xff")
    window._drain_queues()
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == "01 02 AB FF"


def test_switch_to_gbk_rerenders_history(window, controller):
    controller.received_queue.put("你好".encode("gbk"))
    window._drain_queues()
    _select(window.encoding_combo, "GBK")
    assert window.display_edit.toPlainText() == "你好"


# ---------- 自动滚动 ----------


def _feed_many_lines(window, controller, count=300):
    """把窗口缩小并追加大量带换行文本，保证内容超出视口。"""
    window.resize(400, 200)
    window.show()
    text = "".join(f"line {i}\n" for i in range(count))
    controller.received_queue.put(text.encode("utf-8"))
    window._drain_queues()


def test_append_scrolls_to_end(qtbot, window, controller):
    """追加足够多带换行文本后，滚动条必须位于末尾。"""
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    # 场景有效性：内容确实超出视口，否则断言无意义
    assert sb.maximum() > 0
    assert sb.value() == sb.maximum()


def test_rerender_keeps_end_visible(qtbot, window, controller):
    """切换文本/HEX 或编码导致全量重渲染后，仍保持末尾可见。"""
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    sb.setValue(sb.maximum())  # 用户当前位于末尾

    _select(window.receive_mode_combo, "HEX")
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    assert sb.value() == sb.maximum()

    _select(window.receive_mode_combo, "文本")
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    assert sb.value() == sb.maximum()

    _select(window.encoding_combo, "GBK")
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    assert sb.value() == sb.maximum()


# ---------- 发送 ----------


def test_send_text_utf8_bytes(window, controller):
    _open(window)
    window.send_edit.setText("你好")
    window.send_button.click()
    assert controller.writes == ["你好".encode("utf-8")]


def test_send_text_gbk_bytes(window, controller):
    _open(window)
    _select(window.encoding_combo, "GBK")
    window.send_edit.setText("你好")
    window.send_button.click()
    assert controller.writes == ["你好".encode("gbk")]


def test_send_hex_bytes_space_and_comma_separated(window, controller):
    _open(window)
    _select(window.send_mode_combo, "HEX")
    window.send_edit.setText("48 65 6C 6C 6F")
    window.send_button.click()
    assert controller.writes == [b"Hello"]

    window.send_edit.setText("48,65 6C")
    window.send_button.click()
    assert controller.writes == [b"Hello", b"Hel"]


def test_invalid_hex_warns_and_no_write(window, controller, dialogs):
    _open(window)
    _select(window.send_mode_combo, "HEX")
    window.send_edit.setText("GG 01")
    window.send_button.click()
    assert len(dialogs["warning"]) == 1
    assert controller.writes == []


def test_empty_hex_warns_and_no_write(window, controller, dialogs):
    _open(window)
    _select(window.send_mode_combo, "HEX")
    window.send_edit.setText("")
    window.send_button.click()
    assert len(dialogs["warning"]) == 1
    assert controller.writes == []


# ---------- 清空 / 错误队列 ----------


def test_clear_clears_display_and_history(window, controller):
    controller.received_queue.put(b"abc")
    window._drain_queues()
    assert window.display_edit.toPlainText() == "abc"

    window.clear_button.click()
    assert window.display_edit.toPlainText() == ""
    # 历史已清：切到 HEX 不会复活旧数据
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == ""


def test_clear_discards_queued_bytes_not_yet_drained(window, controller):
    """清空点击时，receive 队列中尚未被 drain 取走的旧字节必须一并丢弃。"""
    window._timer.stop()  # 停掉 50ms QTimer，保证测试确定性
    controller.received_queue.put(b"old-data")
    window.clear_button.click()
    window._drain_queues()
    assert window.display_edit.toPlainText() == ""


def test_clear_keeps_data_arriving_after_click(window, controller):
    """清空后新到达的数据不能被吞掉。"""
    window._timer.stop()
    controller.received_queue.put(b"old-data")
    window.clear_button.click()
    controller.received_queue.put(b"new-data")
    window._drain_queues()
    assert window.display_edit.toPlainText() == "new-data"


def test_clear_discards_only_old_queued_bytes(window, controller):
    """混合场景：点击时已在队列的旧字节丢弃，点击后到达的新字节保留。"""
    window._timer.stop()
    controller.received_queue.put(b"old-1")
    controller.received_queue.put(b"old-2")
    window.clear_button.click()
    controller.received_queue.put(b"new")
    window._drain_queues()
    assert window.display_edit.toPlainText() == "new"


def test_error_queue_shows_critical_and_restores_closed_state(window, controller, dialogs):
    _open(window)
    assert window.open_button.text() == "关闭"
    assert not window.port_combo.isEnabled()

    controller.error_queue.put(OSError("串口异常断开"))
    window._drain_queues()

    assert len(dialogs["critical"]) == 1
    # 恢复关闭状态
    assert window.open_button.text() == "打开"
    assert window.port_combo.isEnabled()
    assert window.baud_combo.isEnabled()
