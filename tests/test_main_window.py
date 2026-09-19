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
from dataclasses import dataclass
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QStatusBar  # noqa: E402

from paimon_assistant.receive_log import ReceiveLogService  # noqa: E402
from paimon_assistant.theme import COLORS  # noqa: E402

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
    "follow_latest_button",
]


@dataclass(frozen=True)
class FakeEvent:
    """按 duck-typing 契约构造的接收事件（received_at_ms / payload / raw_frame）。"""

    received_at_ms: int
    payload: bytes
    raw_frame: bytes


def _event(text: str, *, encoding: str = "utf-8", ms: int = 0) -> FakeEvent:
    """构造一个文本负载的完整事件（原始帧 = 载荷 + \\r\\n）。"""
    payload = text.encode(encoding)
    return FakeEvent(ms, payload, payload + b"\r\n")


class FakeController:
    """内联 fake：记录 open/close/write 调用，提供接收/错误队列。"""

    def __init__(self, ports=("COM3", "COM4")):
        self.ports = list(ports)
        self.received_queue = queue.Queue()
        self.diagnostic_queue = queue.Queue()
        self.raw_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.opened_settings = []  # 每次 open 收到的 SerialSettings
        self.close_calls = 0
        self.writes = []  # write(bytes) 记录
        self.list_ports_calls = 0
        self.open_exc = None  # 若设置，open() 抛出该异常
        self.reset_calls = 0  # reset_receive_session() 调用次数

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

    def set_raw_mode(self, enabled):
        pass

    def reset_receive_session(self):
        """模拟真实控制器：递增代次并按新队列丢弃待处理旧事件（REQ §10.1）。"""
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


# ---------- 布局与串口参数折叠行 ----------


def _rect_in_window(window, widget):
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def test_window_initial_and_minimum_sizes(window):
    assert window.size() == QSize(1080, 680)
    assert window.minimumSize() == QSize(760, 480)


def test_serial_parameters_fold_without_resetting_values(window, qapp):
    window.show()
    qapp.processEvents()
    button = window.serial_parameters_button
    row = window.serial_parameters_row
    assert button.isCheckable()
    assert not button.isChecked()
    assert row.isHidden()
    assert button.text() == "串口参数 8N1"

    button.click()
    assert row.isVisibleTo(window)
    window.data_bits_combo.setCurrentText("7")
    assert button.text() == "串口参数 7N1"
    window.parity_combo.setCurrentText("E")
    assert button.text() == "串口参数 7E1"
    window.stop_bits_combo.setCurrentText("1.5")
    assert button.text() == "串口参数 7E1.5"

    button.click()
    assert row.isHidden()
    assert button.text() == "串口参数 7E1.5"
    button.click()
    assert window.data_bits_combo.currentText() == "7"
    assert window.parity_combo.currentText() == "E"
    assert window.stop_bits_combo.currentText() == "1.5"


def test_folded_parameters_reach_controller_and_stay_locked_when_open(
    window, controller
):
    window.serial_parameters_button.click()
    window.data_bits_combo.setCurrentText("5")
    window.parity_combo.setCurrentText("M")
    window.stop_bits_combo.setCurrentText("2")
    window.serial_parameters_button.click()
    _open(window)

    settings = controller.opened_settings[-1]
    assert (settings.data_bits, settings.parity, settings.stop_bits) == (5, "M", 2)
    assert window.serial_parameters_button.text() == "串口参数 5M2"
    assert window.serial_parameters_button.isEnabled()
    window.serial_parameters_button.click()
    assert not window.serial_parameters_row.isHidden()
    controls = (window.data_bits_combo, window.parity_combo, window.stop_bits_combo)
    assert all(not control.isEnabled() for control in controls)
    window.open_button.click()
    assert all(control.isEnabled() for control in controls)
    assert window.serial_parameters_button.text() == "串口参数 5M2"


@pytest.mark.parametrize("expanded", [False, True])
def test_parameter_fold_and_tab_navigation_work_with_keyboard(
    window, qtbot, expanded
):
    window.show()
    window.activateWindow()
    button = window.serial_parameters_button
    button.setFocus()
    qtbot.waitUntil(button.hasFocus)
    if expanded:
        qtbot.keyClick(button, Qt.Key.Key_Space)
    assert button.isChecked() == expanded
    assert window.serial_parameters_row.isVisibleTo(window) == expanded

    window.encoding_combo.setFocus()
    qtbot.keyClick(window.encoding_combo, Qt.Key.Key_Tab)
    expected = window.data_bits_combo if expanded else window.receive_mode_combo
    qtbot.waitUntil(expected.hasFocus)
    if expanded:
        qtbot.keyClick(expected, Qt.Key.Key_Tab)
        qtbot.waitUntil(window.parity_combo.hasFocus)
        qtbot.keyClick(window.parity_combo, Qt.Key.Key_Tab)
        qtbot.waitUntil(window.stop_bits_combo.hasFocus)
        qtbot.keyClick(window.stop_bits_combo, Qt.Key.Key_Tab)
        qtbot.waitUntil(window.receive_mode_combo.hasFocus)


@pytest.mark.parametrize("size", [(760, 480), (1080, 680)])
@pytest.mark.parametrize("expanded", [False, True])
def test_reorganized_controls_fit_without_overlap(window, qapp, size, expanded):
    window.resize(*size)
    window.serial_parameters_button.setChecked(expanded)
    window.show()
    qapp.processEvents()
    assert window.size() == QSize(*size)
    central = _rect_in_window(window, window.centralWidget())
    names = PUBLIC_WIDGETS + [
        "serial_parameters_button", "parse_mode_combo", "timestamp_checkbox",
        "log_dir_button",
    ]
    parameters = {"data_bits_combo", "parity_combo", "stop_bits_combo"}
    rectangles = []
    for name in names:
        widget = getattr(window, name)
        if name in parameters and not expanded:
            assert not widget.isVisibleTo(window)
            continue
        assert widget.isVisibleTo(window), name
        rect = _rect_in_window(window, widget)
        assert central.contains(rect), name
        for other_name, other_rect in rectangles:
            assert not rect.intersects(other_rect), (name, other_name)
        rectangles.append((name, rect))

    rect = lambda name: _rect_in_window(window, getattr(window, name))
    assert rect("open_button").top() == rect("port_combo").top()
    assert rect("open_button").bottom() < rect("receive_mode_combo").top()
    assert rect("encoding_combo").bottom() < rect("receive_mode_combo").top()
    assert rect("receive_mode_combo").bottom() < rect("display_edit").top()
    assert rect("display_edit").bottom() < rect("send_edit").top()
    assert rect("send_mode_combo").top() == rect("send_edit").top()
    assert rect("send_mode_combo").right() < rect("send_edit").left()
    assert rect("send_edit").right() < rect("send_button").left()


def test_connection_bar_reflows_without_changing_inputs_or_focus(window, qapp, qtbot):
    window.show()
    window.activateWindow()
    window.baud_combo.setEditText("123456")
    window.encoding_combo.setCurrentText("GBK")
    window.send_edit.setText("pending send")
    window.send_edit.setFocus()
    qtbot.waitUntil(window.send_edit.hasFocus)
    for width, stacked in ((1080, False), (760, True), (1080, False)):
        window.resize(width, 680)
        qapp.processEvents()
        assert window.width() == width
        port = _rect_in_window(window, window.port_combo)
        encoding = _rect_in_window(window, window.encoding_combo)
        assert (encoding.top() > port.top()) == stacked
        assert window.baud_combo.currentText() == "123456"
        assert window.encoding_combo.currentText() == "GBK"
        assert window.send_edit.text() == "pending send"
        assert window.send_edit.hasFocus()


def test_status_bar_shows_current_facts_below_send_controls(window, qapp):
    window.show()
    qapp.processEvents()
    bar = window.findChild(QStatusBar)
    assert bar is not None
    assert bar.isVisibleTo(window)
    assert window.connection_status_label.text() == "未连接"
    assert "日志已启用" in window.receive_status_label.text()
    assert _rect_in_window(window, bar).top() > _rect_in_window(window, window.send_edit).bottom()


# ---------- 当前连接事实与唯一主操作 ----------


def _assert_primary(window, expected):
    buttons = (window.open_button, window.send_button)
    assert [button for button in buttons if button.property("primary")] == [expected]


def test_status_uses_opened_parameters_and_clears_on_close(window, controller):
    _assert_primary(window, window.open_button)
    window.baud_combo.setEditText("460800")
    window.data_bits_combo.setCurrentText("7")
    window.parity_combo.setCurrentText("E")
    window.stop_bits_combo.setCurrentText("1.5")
    _open(window)
    _assert_primary(window, window.send_button)
    expected = "已连接 COM3 · 460800 / 7E1.5"
    assert window.connection_status_label.text() == expected

    # 参数下拉框不能代表已建立的连接：轮询会移除暂时消失的端口。
    controller.ports = ["COM4"]
    window._monitor.tick()
    assert window.port_combo.currentText() != "COM3"
    window.parse_mode_combo.setCurrentText("原始字节")
    assert window.connection_status_label.text() == expected
    label = window.connection_status_label
    label.ensurePolished()
    assert label.palette().color(QPalette.ColorRole.WindowText) == QColor(COLORS["success"])

    window.open_button.click()
    assert label.text() == "未连接"
    _assert_primary(window, window.open_button)
    label.ensurePolished()
    assert label.palette().color(QPalette.ColorRole.WindowText) == QColor(COLORS["secondary"])
    assert not window.send_button.isEnabled()


@pytest.mark.parametrize("cause", ["open_failure", "read_failure", "write_failure", "port_lost"])
def test_failures_restore_disconnected_status_and_primary(
    window, controller, dialogs, monkeypatch, cause
):
    if cause == "open_failure":
        controller.open_exc = OSError("端口被占用")
        window.open_button.click()
    else:
        _open(window)
        if cause == "read_failure":
            controller.error_queue.put(OSError("读失败"))
            window._drain_queues()
        elif cause == "write_failure":
            def fail_write(data):
                raise OSError("写失败")
            monkeypatch.setattr(controller, "write", fail_write)
            window.send_edit.setText("hello")
            window.send_button.click()
        else:
            controller.ports = []
            window._monitor.tick()
            window._monitor.tick()
    assert window.connection_status_label.text() == "未连接"
    _assert_primary(window, window.open_button)
    assert dialogs["critical"] or dialogs["warning"]


def test_reopen_replaces_connection_status_without_old_port(window):
    _open(window)
    window.open_button.click()
    window.port_combo.setCurrentText("COM4")
    window.baud_combo.setEditText("9600")
    _open(window)
    assert window.connection_status_label.text() == "已连接 COM4 · 9600 / 8N1"
    _assert_primary(window, window.send_button)


def test_receive_diagnostic_is_hidden_until_error_and_hidden_after_clear(window, controller):
    assert window.receive_error_label.isHidden()
    controller.diagnostic_queue.put("接收帧超过 1 MiB，已丢弃")
    window._drain_queues()
    assert not window.receive_error_label.isHidden()
    window.clear_button.click()
    assert window.receive_error_label.text() == ""
    assert window.receive_error_label.isHidden()


@pytest.mark.parametrize("raw", [False, True])
def test_status_labels_fit_small_window_after_connection(window, qapp, raw):
    window.resize(760, 480)
    window.show()
    _open(window)
    if raw:
        window.parse_mode_combo.setCurrentText("原始字节")
    qapp.processEvents()
    bar = _rect_in_window(window, window.statusBar())
    connection = _rect_in_window(window, window.connection_status_label)
    receive = _rect_in_window(window, window.receive_status_label)
    assert window.size() == QSize(760, 480)
    assert bar.contains(connection)
    assert bar.contains(receive)
    assert not connection.intersects(receive)
    for label in (window.connection_status_label, window.receive_status_label):
        assert label.height() >= label.heightForWidth(label.width())


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


def test_drain_received_queue_shows_event_text(window, controller):
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("你好"))
    controller.received_queue.put(_event("world"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "你好\nworld\n"


def test_switch_to_hex_rerenders_history_uppercase_space_separated(window, controller):
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(
        FakeEvent(0, b"\x01\x02\xab\xff", b"\x01\x02\xab\xff\r\n")
    )
    window._drain_queues()
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == "01 02 AB FF 0D 0A\n"


def test_switch_to_gbk_rerenders_history(window, controller):
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("你好", encoding="gbk"))
    window._drain_queues()
    _select(window.encoding_combo, "GBK")
    assert window.display_edit.toPlainText() == "你好\n"


# ---------- 自动滚动 ----------


def _feed_many_lines(window, controller, count=300):
    """把窗口缩小并喂入大量事件，保证内容超出视口。"""
    window.resize(400, 200)
    window.show()
    for i in range(count):
        controller.received_queue.put(_event(f"line {i}"))
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
    """跟随开启时切换文本/HEX 或编码仍保持末尾可见。"""
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


def test_follow_latest_pauses_when_user_scrolls_and_resumes_on_click(qtbot, window, controller):
    _feed_many_lines(window, controller)
    button = window.follow_latest_button
    sb = window.display_edit.verticalScrollBar()
    assert button.isChecked()
    assert button.text() == "跟随最新"

    paused_value = max(0, sb.maximum() - 3)
    sb.setSliderPosition(paused_value)  # 模拟用户拖动滚动条
    qtbot.waitUntil(lambda: not button.isChecked())
    assert button.text() == "回到最新"

    controller.received_queue.put(_event("paused-line"))
    window._drain_queues()
    assert not button.isChecked()
    assert sb.value() == paused_value
    assert "paused-line" in window.display_edit.toPlainText()

    button.click()
    assert button.isChecked()
    assert button.text() == "跟随最新"
    assert sb.value() == sb.maximum()

    controller.received_queue.put(_event("latest-line"))
    window._drain_queues()
    assert sb.value() == sb.maximum()


def test_follow_button_can_pause_at_bottom_without_stopping_receive_or_log(
    qtbot, window, controller
):
    _feed_many_lines(window, controller)
    button = window.follow_latest_button
    sb = window.display_edit.verticalScrollBar()
    button.click()
    assert not button.isChecked()
    paused_value = sb.value()

    controller.received_queue.put(_event("bottom-paused"))
    window._drain_queues()
    assert sb.value() == paused_value
    assert "bottom-paused" in window.display_edit.toPlainText()


def test_follow_latest_keeps_selections_during_new_data(qtbot, window, controller):
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    sb.setSliderPosition(max(0, sb.maximum() // 2))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())
    cursor = window.display_edit.textCursor()
    cursor.setPosition(20)
    cursor.setPosition(32, cursor.MoveMode.KeepAnchor)
    window.display_edit.setTextCursor(cursor)
    selected = window.display_edit.textCursor().selectedText()
    paused_value = sb.value()

    controller.received_queue.put(_event("selection-tail"))
    window._drain_queues()
    assert not window.follow_latest_button.isChecked()
    assert sb.value() == paused_value
    assert window.display_edit.textCursor().selectedText() == selected


def test_follow_latest_remembers_pause_position_per_parse_mode(qtbot, window, controller):
    _feed_many_lines(window, controller)
    framed_scrollbar = window.display_edit.verticalScrollBar()
    framed_scrollbar.setSliderPosition(max(0, framed_scrollbar.maximum() // 3))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())
    framed_value = framed_scrollbar.value()

    _select(window.parse_mode_combo, "原始字节")
    controller.raw_queue.put(b"raw-" + b"x" * 4000)
    window._drain_queues()
    raw_scrollbar = window.display_edit.verticalScrollBar()
    raw_scrollbar.setSliderPosition(max(0, raw_scrollbar.maximum() // 4))
    raw_value = raw_scrollbar.value()
    _select(window.parse_mode_combo, "按 \\r\\n 分帧")
    assert not window.follow_latest_button.isChecked()
    assert window.display_edit.verticalScrollBar().value() == framed_value
    _select(window.parse_mode_combo, "原始字节")
    assert not window.follow_latest_button.isChecked()
    assert window.display_edit.verticalScrollBar().value() == raw_value


def test_follow_latest_reanchors_to_bottom_after_following_resize(qtbot, window, controller):
    _feed_many_lines(window, controller)
    window.resize(800, 240)
    window.show()
    window.display_edit.verticalScrollBar().setValue(
        window.display_edit.verticalScrollBar().maximum()
    )
    window.resize(400, 200)
    qtbot.wait(50)
    scrollbar = window.display_edit.verticalScrollBar()
    assert window.follow_latest_button.isChecked()
    assert scrollbar.value() == scrollbar.maximum()


def test_follow_latest_rerender_keeps_pause_and_clears_incompatible_selection(
    qtbot, window, controller
):
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    sb.setSliderPosition(max(0, sb.maximum() // 2))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())
    cursor = window.display_edit.textCursor()
    cursor.setPosition(20)
    cursor.setPosition(32, cursor.MoveMode.KeepAnchor)
    window.display_edit.setTextCursor(cursor)
    _select(window.receive_mode_combo, "HEX")
    assert not window.follow_latest_button.isChecked()
    assert sb.value() < sb.maximum()
    _select(window.encoding_combo, "GBK")
    assert not window.follow_latest_button.isChecked()
    assert sb.value() < sb.maximum()


def test_clear_resets_follow_latest_and_discards_pause(qtbot, window, controller):
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    sb.setSliderPosition(max(0, sb.maximum() // 2))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())
    window.clear_button.click()
    assert window.follow_latest_button.isChecked()
    assert window.follow_latest_button.text() == "跟随最新"
    assert window.display_edit.toPlainText() == ""
    controller.received_queue.put(_event("after-clear"))
    window._drain_queues()
    assert sb.value() == sb.maximum()


def test_follow_latest_first_visit_to_unrecorded_mode_shows_end_but_stays_paused(
    qtbot, window, controller
):
    """A12：暂停时首次进入没有位置记录的模式，显示该模式末尾但仍保持暂停。"""
    _feed_many_lines(window, controller)
    framed_sb = window.display_edit.verticalScrollBar()
    qtbot.waitUntil(lambda: framed_sb.maximum() > 0, timeout=2000)
    framed_sb.setSliderPosition(max(0, framed_sb.maximum() // 3))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())

    # 原始字节历史先积累数据，但从未作为显示模式出现过（无位置记录）。
    controller.raw_queue.put(b"raw-" + b"x" * 4000)
    window._drain_queues()
    _select(window.parse_mode_combo, "原始字节")

    raw_sb = window.display_edit.verticalScrollBar()
    assert raw_sb.maximum() > 0
    assert not window.follow_latest_button.isChecked()
    assert window.follow_latest_button.text() == "回到最新"
    assert raw_sb.value() == raw_sb.maximum()


def test_follow_latest_paused_state_survives_height_resize(qtbot, window, controller):
    """A13：暂停态在窗口尺寸变化后不自动恢复跟随，也不跳到末尾。"""
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    sb.setSliderPosition(max(0, sb.maximum() // 2))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())
    paused_value = sb.value()

    window.resize(400, 160)  # 只缩高度：内容更多行，活动位置应保持
    qtbot.wait(50)

    assert not window.follow_latest_button.isChecked()
    assert window.follow_latest_button.text() == "回到最新"
    assert sb.value() == paused_value
    assert sb.value() < sb.maximum()


def test_paused_scroll_still_writes_events_to_log(qtbot, mw, controller, tmp_path):
    """A14：暂停跟随不影响日志写入，事件照常落盘。"""
    fixed_ms = 1_789_795_153_723
    log_dir = tmp_path / "logs"
    log_service = ReceiveLogService(
        log_dir,
        date_from_ms=lambda _ms: date(2026, 9, 19),
    )
    window = mw.MainWindow(controller=controller, log_service=log_service)
    qtbot.addWidget(window)
    window.timestamp_checkbox.setChecked(False)
    _feed_many_lines(window, controller)
    sb = window.display_edit.verticalScrollBar()
    qtbot.waitUntil(lambda: sb.maximum() > 0, timeout=2000)
    sb.setSliderPosition(max(0, sb.maximum() // 2))
    qtbot.waitUntil(lambda: not window.follow_latest_button.isChecked())

    event = _event("logged-while-paused", ms=fixed_ms)
    controller.received_queue.put(event)
    window._drain_queues()

    assert "logged-while-paused" in window.display_edit.toPlainText()
    log_text = (log_dir / "2026-09-19.txt").read_text(encoding="utf-8")
    assert event.raw_frame.hex(" ").upper() in log_text


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


def test_clear_calls_reset_receive_session_and_drops_old_state(window, controller):
    """清空必须以控制器会话重置为唯一线性化点（REQ-0003 §10.1）。"""
    window._timer.stop()
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("shown"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "shown\n"
    controller.received_queue.put(_event("pending-not-drained"))
    window.receive_error_label.setText("接收帧超过 1 MiB，已丢弃")

    window.clear_button.click()

    assert controller.reset_calls == 1
    assert window.display_edit.toPlainText() == ""
    assert window.receive_error_label.text() == ""

    # 清空后切换显示设置不复活旧事件；新数据进入新会话并正常显示
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == ""
    controller.received_queue.put(_event("new"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "6E 65 77 0D 0A\n"


def test_clear_clears_display_and_history(window, controller):
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("abc"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "abc\n"

    window.clear_button.click()
    assert window.display_edit.toPlainText() == ""
    # 历史已清：切到 HEX 不会复活旧数据
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == ""


def test_clear_discards_queued_events_not_yet_drained(window, controller):
    """清空点击时，receive 队列中尚未被 drain 取走的旧事件必须一并丢弃。"""
    window._timer.stop()  # 停掉 50ms QTimer，保证测试确定性
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("old-data"))
    window.clear_button.click()
    window._drain_queues()
    assert window.display_edit.toPlainText() == ""


def test_clear_keeps_data_arriving_after_click(window, controller):
    """清空后新到达的数据不能被吞掉。"""
    window._timer.stop()
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("old-data"))
    window.clear_button.click()
    controller.received_queue.put(_event("new-data"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "new-data\n"


def test_clear_discards_only_old_queued_events(window, controller):
    """混合场景：点击时已在队列的旧事件丢弃，点击后到达的新事件保留。"""
    window._timer.stop()
    window.timestamp_checkbox.setChecked(False)
    controller.received_queue.put(_event("old-1"))
    controller.received_queue.put(_event("old-2"))
    window.clear_button.click()
    controller.received_queue.put(_event("new"))
    window._drain_queues()
    assert window.display_edit.toPlainText() == "new\n"


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
