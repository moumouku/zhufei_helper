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

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .codec import IncrementalTextDecoder, encode_text, format_hex, parse_hex_input
from .config import BAUD_RATES, SerialSettings
from .port_monitor import PortMonitor
from .receive_buffer import ReceiveBuffer
from .receive_framer import ReceivedEvent
from .receive_log import ReceiveLogError, ReceiveLogService
from .receive_renderer import render_events
from .serial_controller import SerialController
from .theme import COLORS, SIZES, apply_theme, data_font, set_primary

TEXT_MODE = "文本"
HEX_MODE = "HEX"

#: 接收解析模式（REQ-0004 §3.1）：默认严格 `\r\n` 分帧，可选原始字节。
FRAMED_MODE = "按 \\r\\n 分帧"
RAW_MODE = "原始字节"

_PARITY_ITEMS = ["N", "E", "O", "M", "S"]
_LOG_WRITE_FAILED_TEXT = "日志写入失败，请检查磁盘空间或权限"


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
        # REQ-0003 §8.1：程序启动时执行一次日志保留清理。
        # cleanup() 自身不创建目录、不抛异常，启动路径无需额外防御。
        self._log_service.cleanup()
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
        self._active_settings: SerialSettings | None = None
        self._event_history: list[ReceivedEvent] = []
        self._raw_history = ReceiveBuffer()
        self._follow_latest = True
        self._rendering_history = False
        self._user_scroll_pending = False
        self._scroll_settle_generation = 0
        self._scroll_positions = {FRAMED_MODE: None, RAW_MODE: None}
        self._displayed_mode = FRAMED_MODE

        self._build_ui()
        apply_theme(self)
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

        self._update_receive_mode_ui()
        self._set_open_state(False)
        self._refresh_ports()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        self._root_layout = root = QVBoxLayout(central)
        margin = SIZES["page_margin"]
        root.setContentsMargins(margin, margin, margin, margin)
        root.setSpacing(SIZES["spacing"])
        self._build_connection_bar(root)

        # 接收工具栏：显示方式、解析、时间戳，以及接收区操作。
        receive_row = QHBoxLayout()
        receive_row.setSpacing(SIZES["spacing"])
        self.receive_mode_combo = QComboBox()
        self.receive_mode_combo.addItems([TEXT_MODE, HEX_MODE])
        self.parse_mode_combo = QComboBox()
        self.parse_mode_combo.setObjectName("parse_mode_combo")
        self.parse_mode_combo.addItems([FRAMED_MODE, RAW_MODE])
        self.timestamp_checkbox = QCheckBox("时间戳")
        self.timestamp_checkbox.setObjectName("timestamp_checkbox")
        self.timestamp_checkbox.setChecked(True)
        self.follow_latest_button = QPushButton("跟随最新")
        self.follow_latest_button.setObjectName("follow_latest_button")
        self.follow_latest_button.setCheckable(True)
        self.follow_latest_button.setChecked(True)
        self.clear_button = QPushButton("清空接收")
        self.log_dir_button = QPushButton("日志目录")
        self.log_dir_button.setObjectName("log_dir_button")
        for label, widget in (
            ("接收", self.receive_mode_combo),
            ("解析", self.parse_mode_combo),
        ):
            receive_row.addWidget(QLabel(label))
            receive_row.addWidget(widget)
        receive_row.addWidget(self.timestamp_checkbox)
        receive_row.addWidget(self.follow_latest_button)
        receive_row.addStretch(1)
        receive_row.addWidget(self.clear_button)
        receive_row.addWidget(self.log_dir_button)
        root.addLayout(receive_row)

        # 日志错误靠近日志入口，接收诊断紧邻接收区；空提示不占高度。
        self.log_error_label = QLabel("")
        self.log_error_label.setObjectName("log_error_label")
        self.log_error_label.setAlignment(Qt.AlignRight)
        self.receive_error_label = QLabel("")
        self.receive_error_label.setObjectName("receive_error_label")
        for label in (self.log_error_label, self.receive_error_label):
            label.setStyleSheet(f"color: {COLORS['error']};")
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setVisible(False)
            root.addWidget(label)

        # 接收区占据剩余空间。
        self.display_edit = QPlainTextEdit()
        self.display_edit.setFont(data_font())
        self.display_edit.setReadOnly(True)
        root.addWidget(self.display_edit, 1)

        # 发送模式紧邻输入框；编码仍与接收共用。
        send_row = QHBoxLayout()
        send_row.setSpacing(SIZES["spacing"])
        self.send_mode_combo = QComboBox()
        self.send_mode_combo.addItems([TEXT_MODE, HEX_MODE])
        self.send_edit = QLineEdit()
        self.send_edit.setFont(data_font())
        self.send_button = QPushButton("发送")
        send_row.addWidget(QLabel("发送"))
        send_row.addWidget(self.send_mode_combo)
        send_row.addWidget(self.send_edit, 1)
        send_row.addWidget(self.send_button)
        root.addLayout(send_row)

        status_bar = QStatusBar(self)
        status_bar.setObjectName("status_bar")
        self.connection_status_label = QLabel()
        self.connection_status_label.setObjectName("connection_status_label")
        self.receive_status_label = QLabel()
        self.receive_status_label.setObjectName("receive_status_label")
        self.receive_status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for label in (self.connection_status_label, self.receive_status_label):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            status_bar.addPermanentWidget(label, 1)
        self.setStatusBar(status_bar)
        self._set_tab_order()
        self.setMinimumSize(760, 480)
        self.resize(1080, 680)

    def _build_connection_bar(self, root: QVBoxLayout) -> None:
        self._connection_main = QWidget()
        main_row = QHBoxLayout(self._connection_main)
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(SIZES["spacing"])
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(120)
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems([str(r) for r in BAUD_RATES])
        default_baud = 115200 if 115200 in BAUD_RATES else BAUD_RATES[-1]
        self.baud_combo.setCurrentText(str(default_baud))
        self.open_button = QPushButton("打开")
        for label, widget in (("端口", self.port_combo), ("波特率", self.baud_combo)):
            main_row.addWidget(QLabel(label))
            main_row.addWidget(widget)
        main_row.addWidget(self.open_button)

        self._connection_options = QWidget()
        options_row = QHBoxLayout(self._connection_options)
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setSpacing(SIZES["spacing"])
        self.serial_parameters_button = QPushButton()
        self.serial_parameters_button.setObjectName("serial_parameters_button")
        self.serial_parameters_button.setCheckable(True)
        self.refresh_button = QPushButton("刷新")
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(["UTF-8", "GBK"])
        options_row.addWidget(self.serial_parameters_button)
        options_row.addWidget(self.refresh_button)
        options_row.addSpacing(SIZES["group_spacing"] - SIZES["spacing"])
        options_row.addWidget(QLabel("收发文本编码"))
        options_row.addWidget(self.encoding_combo)

        self._connection_layout = QGridLayout()
        self._connection_layout.setContentsMargins(0, 0, 0, 0)
        self._connection_layout.setHorizontalSpacing(SIZES["group_spacing"])
        self._connection_layout.setVerticalSpacing(SIZES["spacing"])
        self._connection_layout.setColumnStretch(0, 1)
        self._connection_layout.addWidget(self._connection_main, 0, 0, Qt.AlignLeft)
        self._connection_layout.addWidget(self._connection_options, 1, 0, Qt.AlignLeft)
        self._connection_stacked = True
        root.addLayout(self._connection_layout)

        self.serial_parameters_row = QWidget()
        self.serial_parameters_row.setObjectName("serial_parameters_row")
        parameters_row = QHBoxLayout(self.serial_parameters_row)
        parameters_row.setContentsMargins(0, 0, 0, 0)
        parameters_row.setSpacing(SIZES["spacing"])
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
            ("数据位", self.data_bits_combo),
            ("校验", self.parity_combo),
            ("停止位", self.stop_bits_combo),
        ):
            parameters_row.addWidget(QLabel(label))
            parameters_row.addWidget(widget)
        parameters_row.addStretch(1)
        root.addWidget(self.serial_parameters_row)
        self.serial_parameters_row.setVisible(False)

    def _set_tab_order(self) -> None:
        controls = (
            self.port_combo, self.baud_combo, self.open_button,
            self.serial_parameters_button, self.refresh_button, self.encoding_combo,
            self.data_bits_combo, self.parity_combo, self.stop_bits_combo,
            self.receive_mode_combo, self.parse_mode_combo, self.timestamp_checkbox,
            self.follow_latest_button, self.clear_button, self.log_dir_button,
            self.display_edit,
            self.send_mode_combo, self.send_edit, self.send_button,
        )
        for previous, following in zip(controls, controls[1:]):
            QWidget.setTabOrder(previous, following)

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
        self.follow_latest_button.clicked.connect(self._on_follow_latest_clicked)
        scrollbar = self.display_edit.verticalScrollBar()
        scrollbar.actionTriggered.connect(self._on_display_scroll_action)
        scrollbar.valueChanged.connect(self._on_display_scroll_changed)
        self.parse_mode_combo.currentIndexChanged.connect(self._on_parse_mode_changed)
        self.serial_parameters_button.toggled.connect(self._on_serial_parameters_toggled)
        for combo in (self.data_bits_combo, self.parity_combo, self.stop_bits_combo):
            combo.currentTextChanged.connect(self._update_serial_summary)

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
        self._on_serial_parameters_toggled(False)
        self._update_serial_summary()

    def _on_serial_parameters_toggled(self, expanded: bool) -> None:
        focused = self.focusWidget()
        if not expanded and focused and self.serial_parameters_row.isAncestorOf(focused):
            self.serial_parameters_button.setFocus()
        self.serial_parameters_row.setVisible(expanded)
        arrow = QStyle.StandardPixmap.SP_ArrowDown if expanded else QStyle.StandardPixmap.SP_ArrowRight
        self.serial_parameters_button.setIcon(self.style().standardIcon(arrow))
        self.serial_parameters_button.setToolTip("收起串口参数" if expanded else "展开串口参数")

    def _update_serial_summary(self) -> None:
        summary = (
            self.data_bits_combo.currentText()
            + self.parity_combo.currentText()
            + self.stop_bits_combo.currentText()
        )
        self.serial_parameters_button.setText(f"串口参数 {summary}")
        self._update_connection_layout()

    def _update_connection_layout(self) -> None:
        """按控件实际宽度换行，只移动分组，不重建控件或改变参数。"""
        margins = self._root_layout.contentsMargins()
        available = self.contentsRect().width() - margins.left() - margins.right()
        required = (
            self._connection_main.sizeHint().width()
            + self._connection_options.sizeHint().width()
            + self._connection_layout.horizontalSpacing()
        )
        stacked = available < required
        if stacked == self._connection_stacked:
            return
        self._connection_stacked = stacked
        self._connection_layout.removeWidget(self._connection_main)
        self._connection_layout.removeWidget(self._connection_options)
        if stacked:
            self._connection_layout.addWidget(self._connection_main, 0, 0, 1, 2, Qt.AlignLeft)
            self._connection_layout.addWidget(self._connection_options, 1, 0, 1, 2, Qt.AlignLeft)
        else:
            self._connection_layout.addWidget(self._connection_main, 0, 0, Qt.AlignLeft)
            self._connection_layout.addWidget(self._connection_options, 0, 1, Qt.AlignRight)

    def resizeEvent(self, event) -> None:
        self._begin_scroll_settle(reapply_anchor=False)
        super().resizeEvent(event)
        if hasattr(self, "_connection_layout"):
            self._update_connection_layout()

    def _begin_scroll_settle(self, *, reapply_anchor: bool) -> None:
        """在本轮事件循环结束后落定滚动位置。

        Qt 的延迟布局与光标可见性调整会在 ``resizeEvent`` / ``setPlainText``
        之后异步修改滚动条，可能把跟随态拉离末尾、或把暂停态拉回顶部；这些
        程序性变化必须由本次落定纠正。只有最新一次请求生效（generation 语义）。
        """
        self._scroll_settle_generation += 1
        generation = self._scroll_settle_generation
        QTimer.singleShot(
            0, lambda: self._finish_scroll_settle(generation, reapply_anchor)
        )

    def _finish_scroll_settle(self, generation: int, reapply_anchor: bool) -> None:
        if generation != self._scroll_settle_generation:
            return
        if self._follow_latest:
            self._scroll_to_end()
        elif reapply_anchor:
            anchor = self._scroll_positions.get(self._displayed_mode)
            if anchor is not None:
                self._apply_anchor_value(anchor["value"])

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

    def _update_status(self) -> None:
        """从当前连接与日志服务重算，不以历史错误提示作为状态来源。"""
        settings = self._active_settings
        if self._is_open and settings is not None:
            serial_format = f"{settings.data_bits}{settings.parity}{settings.stop_bits:g}"
            connection = f"已连接 {settings.port} · {settings.baudrate} / {serial_format}"
            connection_color = COLORS["success"]
        else:
            connection = "未连接"
            connection_color = COLORS["secondary"]
        self.connection_status_label.setText(connection)
        self.connection_status_label.setToolTip(connection)
        self.connection_status_label.setStyleSheet(f"color: {connection_color};")

        mode = self.parse_mode_combo.currentText()
        log_color = COLORS["secondary"]
        if mode == RAW_MODE:
            log_state = "不记录日志"
        elif self._log_service.failed:
            log_state = "日志写入失败"
            log_color = COLORS["error"]
        else:
            log_state = "日志已启用"
        self.receive_status_label.setText(f"{mode} · {log_state}")
        self.receive_status_label.setStyleSheet(f"color: {log_color};")
        self.receive_status_label.setToolTip(
            _LOG_WRITE_FAILED_TEXT if log_color == COLORS["error"] else ""
        )

    def _update_receive_mode_ui(self) -> None:
        raw_mode = self.parse_mode_combo.currentText() == RAW_MODE
        self.timestamp_checkbox.setEnabled(not raw_mode)
        self.timestamp_checkbox.setToolTip(
            "原始字节模式不生成接收事件，因此没有逐帧时间戳" if raw_mode else ""
        )
        self.display_edit.setPlaceholderText(
            "收到即显示 · 不记录日志" if raw_mode else
            "等待完整帧，对端需以 \\r\\n 结束；未知协议可切换到原始字节。"
        )

    def _set_open_state(self, open_state: bool) -> None:
        self._is_open = open_state
        if not open_state:
            self._active_settings = None
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
        primary, secondary = (
            (self.send_button, self.open_button) if open_state else
            (self.open_button, self.send_button)
        )
        set_primary(secondary, False)
        set_primary(primary, True)
        self._update_status()

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
        self._active_settings = settings
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

        # 原始字节通道（REQ-0004 §3.4）：两个通道的队列都取干净，避免切模式时
        # 另一个通道的待处理数据被遗漏（它仍会进入各自的历史）。
        raw_chunks = []
        while True:
            try:
                raw_chunks.append(self.raw_queue.get_nowait())
            except queue.Empty:
                break
        if raw_chunks:
            for chunk in raw_chunks:
                self._raw_history.append(chunk)
            self._render_history()

        # 分帧诊断（超长帧）独立队列；控制器契约保证该队列存在（REQ-0003 §5.1.6）。
        diagnostic_queue = self.controller.diagnostic_queue
        diagnostics = []
        while True:
            try:
                diagnostics.append(diagnostic_queue.get_nowait())
            except queue.Empty:
                break
        if diagnostics:
            self.receive_error_label.setText(str(diagnostics[-1]))
            self.receive_error_label.setVisible(bool(self.receive_error_label.text()))

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
            self.log_error_label.setText(_LOG_WRITE_FAILED_TEXT)
            self.log_error_label.setVisible(True)
        self._update_status()

    @property
    def received_queue(self):
        return self.controller.received_queue

    @property
    def raw_queue(self):
        return self.controller.raw_queue

    @property
    def error_queue(self):
        return self.controller.error_queue

    def _display_mode(self) -> str:
        return RAW_MODE if self.parse_mode_combo.currentText() == RAW_MODE else FRAMED_MODE

    def _set_follow_latest(self, enabled: bool, *, scroll_to_end: bool = False) -> None:
        self._follow_latest = bool(enabled)
        with QSignalBlocker(self.follow_latest_button):
            self.follow_latest_button.setChecked(self._follow_latest)
        self.follow_latest_button.setText("跟随最新" if self._follow_latest else "回到最新")
        if scroll_to_end:
            self._scroll_to_end()

    def _on_follow_latest_clicked(self, checked: bool) -> None:
        self._set_follow_latest(checked, scroll_to_end=checked)

    def _on_display_scroll_action(self, _action: int) -> None:
        """记录一次真实的用户滚动意图（滚轮/拖动/翻页/键盘）。

        程序性 ``setValue``（含 Qt 延迟布局自己调整滚动条）不会发出
        ``actionTriggered``，因此只有这个标志能证明“用户上翻”。
        """
        self._user_scroll_pending = True
        QTimer.singleShot(0, self._expire_user_scroll_pending)

    def _expire_user_scroll_pending(self) -> None:
        self._user_scroll_pending = False

    def _on_display_scroll_changed(self, value: int) -> None:
        if self._rendering_history or not self._user_scroll_pending:
            return
        self._user_scroll_pending = False
        if value < self.display_edit.verticalScrollBar().maximum():
            self._scroll_settle_generation += 1  # 取消待落定的程序性复位
            self._set_follow_latest(False)

    def _scroll_to_end(self) -> None:
        """把显示区滚动条拨到末尾（maximum 随内容同步更新）。"""
        sb = self.display_edit.verticalScrollBar()
        with QSignalBlocker(sb):
            sb.setValue(sb.maximum())

    def _apply_anchor_value(self, value: int) -> None:
        scrollbar = self.display_edit.verticalScrollBar()
        with QSignalBlocker(scrollbar):
            scrollbar.setValue(max(0, min(value, scrollbar.maximum())))

    def _capture_display_anchor(self) -> dict:
        scrollbar = self.display_edit.verticalScrollBar()
        cursor = self.display_edit.textCursor()
        return {
            "value": scrollbar.value(),
            "maximum": scrollbar.maximum(),
            "ratio": (scrollbar.value() / scrollbar.maximum()) if scrollbar.maximum() else 1.0,
            "selection": (cursor.selectionStart(), cursor.selectionEnd())
            if cursor.hasSelection()
            else None,
            "text": self.display_edit.toPlainText(),
        }

    def _restore_display_anchor(self, anchor: dict | None, text: str) -> None:
        if self._follow_latest or anchor is None:
            self._scroll_to_end()
            return
        scrollbar = self.display_edit.verticalScrollBar()
        if anchor.get("text") is not None and text.startswith(anchor["text"]):
            value = min(anchor["value"], scrollbar.maximum())
            compatible = True
        else:
            value = round(anchor["ratio"] * scrollbar.maximum()) if scrollbar.maximum() else 0
            compatible = False
        self._apply_anchor_value(value)
        selection = anchor.get("selection") if compatible else None
        if selection:
            document_end = max(0, self.display_edit.document().characterCount() - 1)
            start = max(0, min(selection[0], document_end))
            end = max(start, min(selection[1], document_end))
            if end > start:
                cursor = self.display_edit.textCursor()
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                self.display_edit.setTextCursor(cursor)

    def _on_parse_mode_changed(self) -> None:
        """切换接收解析模式：通知控制器、同步时间戳控件、按新模式重渲（§3.3）。

        已积累的两种历史都不清空；切换只影响后续数据如何进入链路。
        """
        raw_mode = self.parse_mode_combo.currentText() == RAW_MODE
        self.controller.set_raw_mode(raw_mode)
        self._update_receive_mode_ui()
        self._update_status()
        self._render_history()

    def _render_history(self) -> None:
        """按当前设置重渲，并尊重“跟随最新”或暂停时的阅读锚点。"""
        if self._rendering_history:
            return
        mode_name = self._display_mode()
        if self._displayed_mode == mode_name:
            anchor = self._capture_display_anchor()
        else:
            self._scroll_positions[self._displayed_mode] = self._capture_display_anchor()
            anchor = self._scroll_positions[mode_name]
        self._rendering_history = True
        try:
            mode = "hex" if self.receive_mode_combo.currentText() == HEX_MODE else "text"
            encoding = self._current_encoding()
            if mode_name == RAW_MODE:
                text = self._render_raw_history(mode, encoding)
            else:
                text = render_events(
                    self._event_history,
                    mode,
                    encoding,
                    self.timestamp_checkbox.isChecked(),
                )
            self.display_edit.setPlainText(text)
            self._restore_display_anchor(anchor, text)
            self._scroll_positions[mode_name] = self._capture_display_anchor()
            self._displayed_mode = mode_name
            self._begin_scroll_settle(reapply_anchor=True)
        finally:
            self._rendering_history = False

    def _render_raw_history(self, mode: str, encoding: str) -> str:
        """原始字节模式：整段字节流按当前模式渲染，不分帧、不加时间戳（§3.2）。

        文本模式重放全部历史但不 flush，使尾部残缺的多字节序列保持待定，
        不会提前输出替换字符。
        """
        if mode == "hex":
            return self._raw_history.render("hex", encoding)
        return IncrementalTextDecoder(encoding).decode(self._raw_history.raw())

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
        else:
            self.log_error_label.setText(
                _LOG_WRITE_FAILED_TEXT if self._log_service.failed else ""
            )
        self.log_error_label.setVisible(bool(self.log_error_label.text()))
        self._update_status()

    # --------------------------------------------------------------- clear

    def _on_clear(self) -> None:
        # 清空的唯一线性化点（REQ-0003 §10.1）：reset_receive_session() 在会话锁内
        # 递增代次、重置分帧器（丢弃未完成尾部与超长状态）并用新队列丢弃点击时
        # 尚未 drain 的旧事件。未打开时也安全，不会创建串口连接。
        self.controller.reset_receive_session()
        self._event_history.clear()
        self._raw_history.clear()
        self.receive_error_label.clear()
        self.receive_error_label.setVisible(False)
        self.display_edit.clear()
        self._scroll_positions = {FRAMED_MODE: None, RAW_MODE: None}
        self._displayed_mode = self._display_mode()
        self._set_follow_latest(True)

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
