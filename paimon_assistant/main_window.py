"""PySide6 main window for Paimon Assistant.

Complete RX events are drained from the controller in timed batches and kept in
an in-memory history; the display is rendered from that history with the current
mode / encoding / timestamp settings.
Ports are polled every second via PortMonitor and the combo box is updated
by diff (add/remove only), sharing one path with the manual refresh button.
"""

from __future__ import annotations

import os
import queue

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .codec import encode_text, parse_hex_input
from .config import BAUD_RATES, SerialSettings
from .port_monitor import PortMonitor
from .receive_framer import ReceivedEvent
from .receive_log import ReceiveLogError, ReceiveLogService
from .receive_renderer import render_events
from .serial_controller import SerialController

TEXT_MODE = "文本"
HEX_MODE = "HEX"

_PARITY_ITEMS = ["N", "E", "O", "M", "S"]


def _default_log_dir_opener(log_dir) -> None:
    """用 Windows 资源管理器打开日志目录（os.startfile 仅 Windows 提供）。"""
    os.startfile(str(log_dir))


class MainWindow(QMainWindow):
    """极简串口助手主窗口：枚举/打开/关闭/收发/清空。"""

    def __init__(
        self, controller=None, parent=None, *, log_service=None, log_dir_opener=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("派蒙助手")
        self.controller = controller if controller is not None else SerialController()
        self._log_service = (
            log_service if log_service is not None else ReceiveLogService()
        )
        self._log_dir_opener = (
            log_dir_opener if log_dir_opener is not None else _default_log_dir_opener
        )
        self._log_error_shown = False

        # 端口列表监测：与轮询/手动刷新共用同一差量更新路径
        self._monitor = PortMonitor(
            port_lister=self.controller.list_ports,
            on_added=self._on_ports_added,
            on_removed=self._on_ports_removed,
            on_lost=self._on_connected_port_lost,
        )

        self._is_open = False
        self._event_history: list[ReceivedEvent] = []

        self._build_ui()
        self._connect_signals()

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._drain_queues)
        self._timer.start()

        # 端口列表轮询：约 1 秒一次，差量更新（与手动刷新共用同一路径）
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._monitor.tick)
        self._poll_timer.start()

        self._set_open_state(False)
        self._refresh_ports()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 第一行：端口 / 波特率 / 数据位 / 校验 / 停止位
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(120)
        self.refresh_button = QPushButton("刷新")
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems([str(r) for r in BAUD_RATES])
        default_baud = 115200 if 115200 in BAUD_RATES else BAUD_RATES[-1]
        self.baud_combo.setCurrentText(str(default_baud))
        self.data_bits_combo = QComboBox()
        self.data_bits_combo.addItems(["5", "6", "7", "8"])
        self.data_bits_combo.setCurrentText("8")
        self.parity_combo = QComboBox()
        self.parity_combo.addItems(_PARITY_ITEMS)
        self.parity_combo.setCurrentText("N")
        self.stop_bits_combo = QComboBox()
        self.stop_bits_combo.addItems(["1", "1.5", "2"])
        self.stop_bits_combo.setCurrentText("1")
        for label, widget in (
            ("端口", self.port_combo),
            ("波特率", self.baud_combo),
            ("数据位", self.data_bits_combo),
            ("校验", self.parity_combo),
            ("停止位", self.stop_bits_combo),
        ):
            row1.addWidget(QLabel(label))
            row1.addWidget(widget)
        row1.addWidget(self.refresh_button)
        row1.addStretch(1)
        root.addLayout(row1)

        # 第二行：接收/发送模式、编码、打开、清空
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.receive_mode_combo = QComboBox()
        self.receive_mode_combo.addItems([TEXT_MODE, HEX_MODE])
        self.send_mode_combo = QComboBox()
        self.send_mode_combo.addItems([TEXT_MODE, HEX_MODE])
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(["UTF-8", "GBK"])
        self.timestamp_checkbox = QCheckBox("时间戳")
        self.timestamp_checkbox.setObjectName("timestamp_checkbox")
        self.timestamp_checkbox.setChecked(True)
        self.open_button = QPushButton("打开")
        self.clear_button = QPushButton("清空")
        self.log_dir_button = QPushButton("日志目录")
        self.log_dir_button.setObjectName("log_dir_button")
        for label, widget in (
            ("接收", self.receive_mode_combo),
            ("发送", self.send_mode_combo),
            ("编码", self.encoding_combo),
        ):
            row2.addWidget(QLabel(label))
            row2.addWidget(widget)
        row2.addWidget(self.open_button)
        row2.addWidget(self.timestamp_checkbox)
        row2.addWidget(self.clear_button)
        row2.addWidget(self.log_dir_button)
        row2.addStretch(1)
        root.addLayout(row2)

        # 接收区上方：分帧诊断提示（红色、非模态，默认空文本）
        self.receive_error_label = QLabel("")
        self.receive_error_label.setObjectName("receive_error_label")
        self.receive_error_label.setStyleSheet("color: red")
        root.addWidget(self.receive_error_label)

        # 日志错误提示（红色、非模态，默认空文本）
        self.log_error_label = QLabel("")
        self.log_error_label.setObjectName("log_error_label")
        self.log_error_label.setStyleSheet("color: red")
        root.addWidget(self.log_error_label)

        # 第三行：滚动显示区
        self.display_edit = QPlainTextEdit()
        self.display_edit.setReadOnly(True)
        root.addWidget(self.display_edit, 1)

        # 第四行：发送
        row4 = QHBoxLayout()
        row4.setSpacing(6)
        self.send_edit = QLineEdit()
        self.send_button = QPushButton("发送")
        row4.addWidget(self.send_edit, 1)
        row4.addWidget(self.send_button)
        root.addLayout(row4)

        self.resize(760, 480)

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self._refresh_ports)
        self.open_button.clicked.connect(self._on_open_clicked)
        self.clear_button.clicked.connect(self._on_clear)
        self.log_dir_button.clicked.connect(self._on_log_dir_clicked)
        self.send_button.clicked.connect(self._on_send_clicked)
        self.send_edit.returnPressed.connect(self._on_send_clicked)
        self.receive_mode_combo.currentIndexChanged.connect(self._render_history)
        self.encoding_combo.currentIndexChanged.connect(self._render_history)
        self.timestamp_checkbox.toggled.connect(self._render_history)

        self.refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.clear_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        )
        self.send_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self._open_icon = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogOpenButton
        )
        self._close_icon = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogCloseButton
        )

    # ------------------------------------------------------------ helpers

    def _current_encoding(self) -> str:
        return self.encoding_combo.currentText().lower()

    @staticmethod
    def _port_name(item) -> str:
        """兼容 str / SerialPortInfo / pyserial ListPortInfo。"""
        if isinstance(item, str):
            return item
        for attr in ("port", "device"):
            value = getattr(item, attr, None)
            if value:
                return str(value)
        return str(item)

    def _set_open_state(self, open_state: bool) -> None:
        self._is_open = open_state
        self.open_button.setText("关闭" if open_state else "打开")
        self.open_button.setIcon(self._close_icon if open_state else self._open_icon)
        for widget in (
            self.port_combo,
            self.baud_combo,
            self.data_bits_combo,
            self.parity_combo,
            self.stop_bits_combo,
        ):
            widget.setEnabled(not open_state)
        # Refresh remains available while connected so the disabled list can
        # still be updated without changing the active serial link.
        self.refresh_button.setEnabled(True)
        self.send_button.setEnabled(open_state)

    def _close_connection(self) -> None:
        self._monitor.clear_connected()
        try:
            self.controller.close()
        except Exception:
            pass
        self._set_open_state(False)

    # --------------------------------------------------------- enumeration

    def _refresh_ports(self) -> None:
        """手动「刷新」：与轮询共用 monitor 的差量更新路径。

        启动时下拉框仍为空，先让 monitor 建立初始化快照（首轮 tick 不产生
        事件），再全量填充一次，保证启动后插入的端口能在下一轮被发现。
        """
        self._monitor.tick()
        if self.port_combo.count() == 0:
            self._fill_port_combo()

    def _fill_port_combo(self) -> None:
        """启动初始填充：全量枚举并填充下拉框，默认选中第一个。"""
        try:
            entries = list(self.controller.list_ports())
        except Exception:
            entries = []
        names = [self._port_name(e) for e in entries]
        current = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(names)
        if current and current in names:
            self.port_combo.setCurrentText(current)
        elif names:
            self.port_combo.setCurrentIndex(0)

    # -------------------------------------------------- 差量事件处理

    def _on_ports_added(self, ports) -> None:
        current = self.port_combo.currentText()
        added = []
        for name in ports:
            if self.port_combo.findText(name) == -1:
                self.port_combo.addItem(name)
                added.append(name)
        if not added:
            return

        # PortMonitor updates its snapshot before callbacks. This also handles
        # an old selection removed in the same tick as a new port is added.
        if (
            not current
            or self.port_combo.findText(current) == -1
            or (
                self._monitor._last is not None
                and current not in self._monitor._last
            )
        ):
            self.port_combo.setCurrentText(added[0])

    def _on_ports_removed(self, ports) -> None:
        current = self.port_combo.currentText()
        current_removed = False
        for name in ports:
            index = self.port_combo.findText(name)
            if index == -1:
                continue
            if name == current:
                current_removed = True
            self.port_combo.removeItem(index)
        if current_removed:
            self.port_combo.setCurrentIndex(-1)

    def _on_connected_port_lost(self, port: str) -> None:
        if not self._is_open:
            return
        self._close_connection()
        QMessageBox.warning(self, "串口已拔出", "串口已拔出，连接已关闭")

    # -------------------------------------------------------- open / close

    def _on_open_clicked(self) -> None:
        if self._is_open:
            self._close_connection()
            return
        try:
            port = self.port_combo.currentText().strip()
            if not port:
                raise ValueError("请选择要打开的串口")
            settings = SerialSettings(
                port=port,
                baudrate=int(self.baud_combo.currentText().strip()),
                data_bits=int(self.data_bits_combo.currentText()),
                parity=self.parity_combo.currentText(),
                stop_bits=float(self.stop_bits_combo.currentText()),
            )
            self.controller.open(settings)
        except Exception as exc:
            self._set_open_state(False)
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        self._set_open_state(True)
        self._monitor.set_connected(port)

    # ------------------------------------------------------------- receive

    def _drain_queues(self) -> None:
        """批量取出接收事件/分帧诊断/串口错误。

        事件先进历史并按当前设置重渲显示，再按同一顺序写入日志；
        诊断显示到 ``receive_error_label``，串口错误沿用关闭+弹窗路径。
        """
        received_queue = self.received_queue
        error_queue = self.error_queue

        events = []
        while True:
            try:
                events.append(received_queue.get_nowait())
            except queue.Empty:
                break
        if events:
            self._event_history.extend(events)
            self._render_history()
            self._write_events_to_log(events)

        # 分帧诊断（超长帧）独立队列；旧 controller 未提供时跳过。
        diagnostic_queue = getattr(self.controller, "diagnostic_queue", None)
        if diagnostic_queue is not None:
            diagnostics = []
            while True:
                try:
                    diagnostics.append(diagnostic_queue.get_nowait())
                except queue.Empty:
                    break
            if diagnostics:
                self.receive_error_label.setText(str(diagnostics[-1]))

        errors = []
        while True:
            try:
                errors.append(error_queue.get_nowait())
            except queue.Empty:
                break
        if errors:
            self._handle_error(errors[-1])

    def _write_events_to_log(self, events) -> None:
        """显示完成后，按事件顺序逐条交给日志服务（每个事件最多一次）。

        首次失败（抛 ``ReceiveLogError`` 或熔断后返回 ``False``）在
        ``log_error_label`` 显示固定提示；之后不重复提示、不缓存、不重试。
        """
        for event in events:
            try:
                written = self._log_service.write_event(event)
            except ReceiveLogError:
                written = False
            if written or self._log_error_shown:
                continue
            self._log_error_shown = True
            self.log_error_label.setText("日志写入失败，请检查磁盘空间或权限")

    @property
    def received_queue(self):
        return self.controller.received_queue

    @property
    def error_queue(self):
        return self.controller.error_queue

    @staticmethod
    def _scroll_to_end(edit) -> None:
        """把显示区滚动条拨到末尾（maximum 随内容同步更新）。"""
        sb = edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _render_history(self) -> None:
        """用当前显示模式/编码/时间戳开关重渲全部事件历史。"""
        mode = "hex" if self.receive_mode_combo.currentText() == HEX_MODE else "text"
        text = render_events(
            self._event_history,
            mode,
            self._current_encoding(),
            self.timestamp_checkbox.isChecked(),
        )
        self.display_edit.setPlainText(text)
        self._scroll_to_end(self.display_edit)

    def _handle_error(self, err) -> None:
        if not self._is_open:
            return
        message = err if isinstance(err, str) else str(err)
        self._close_connection()
        QMessageBox.critical(self, "串口错误", message)

    # --------------------------------------------------------- log entry

    def _on_log_dir_clicked(self) -> None:
        """先确保日志目录存在，再用注入的打开器打开目录本身。

        创建或打开失败只更新错误标签，不影响接收链路与串口连接。
        """
        try:
            self._log_service.ensure_directory()
            self._log_dir_opener(self._log_service.log_dir)
        except Exception as exc:
            self.log_error_label.setText(f"日志目录打开失败：{exc}")

    # --------------------------------------------------------------- clear

    def _on_clear(self) -> None:
        # 丢弃点击时 receive 队列中尚未 drain 的旧事件，避免随后 QTimer 把
        # 清空前的事件重新显示；点击后新到达的事件不受影响。
        received_queue = self.received_queue
        while True:
            try:
                received_queue.get_nowait()
            except queue.Empty:
                break
        self._event_history.clear()
        self.receive_error_label.clear()
        self.display_edit.clear()

    # --------------------------------------------------------------- send

    def _on_send_clicked(self) -> None:
        if not self._is_open:
            return
        mode = self.send_mode_combo.currentText()
        text = self.send_edit.text()
        try:
            if mode == HEX_MODE:
                data = parse_hex_input(text)
            else:
                data = encode_text(text, self._current_encoding())
        except ValueError as exc:
            QMessageBox.warning(self, "发送失败", str(exc))
            return
        try:
            self.controller.write(data)
        except Exception as exc:
            self._close_connection()
            QMessageBox.critical(self, "发送失败", str(exc))

    # ----------------------------------------------------------- lifecycle

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._poll_timer.stop()
        self._close_connection()
        super().closeEvent(event)
