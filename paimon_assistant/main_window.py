"""PySide6 main window for Paimon Assistant.

Received bytes are drained from the controller in timed batches. Text mode
uses an incremental decoder and HEX mode formats only newly arrived bytes;
the complete raw history is re-rendered only when mode or encoding changes.
"""

from __future__ import annotations

import queue

from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
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

from .codec import IncrementalTextDecoder, encode_text, format_hex, parse_hex_input
from .config import BAUD_RATES, SerialSettings
from .receive_buffer import ReceiveBuffer
from .serial_controller import SerialController

TEXT_MODE = "文本"
HEX_MODE = "HEX"

_PARITY_ITEMS = ["N", "E", "O", "M", "S"]


class MainWindow(QMainWindow):
    """极简串口助手主窗口：枚举/打开/关闭/收发/清空。"""

    def __init__(self, controller=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("派蒙助手")
        self.controller = controller if controller is not None else SerialController()

        self._is_open = False
        self._receive_buffer = ReceiveBuffer()
        self._hex_has_content = False  # 显示区已有 HEX 内容（决定追加时的空格）

        self._build_ui()
        self._connect_signals()

        # 增量解码器：只对“新到达字节”解码，避免每次全量重渲染
        self._decoder = IncrementalTextDecoder(self._current_encoding())

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._drain_queues)
        self._timer.start()

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
        self.open_button = QPushButton("打开")
        self.clear_button = QPushButton("清空")
        for label, widget in (
            ("接收", self.receive_mode_combo),
            ("发送", self.send_mode_combo),
            ("编码", self.encoding_combo),
        ):
            row2.addWidget(QLabel(label))
            row2.addWidget(widget)
        row2.addWidget(self.open_button)
        row2.addWidget(self.clear_button)
        row2.addStretch(1)
        root.addLayout(row2)

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
        self.send_button.clicked.connect(self._on_send_clicked)
        self.send_edit.returnPressed.connect(self._on_send_clicked)
        self.receive_mode_combo.currentIndexChanged.connect(self._re_render)
        self.encoding_combo.currentIndexChanged.connect(self._re_render)

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
            self.refresh_button,
        ):
            widget.setEnabled(not open_state)
        self.send_button.setEnabled(open_state)

    def _close_connection(self) -> None:
        try:
            self.controller.close()
        except Exception:
            pass
        self._set_open_state(False)

    # --------------------------------------------------------- enumeration

    def _refresh_ports(self) -> None:
        if self._is_open:
            return
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

    # ------------------------------------------------------------- receive

    def _drain_queues(self) -> None:
        """批量取出接收/错误队列，逐块解码追加，避免每字节刷新。"""
        received_queue = self.received_queue
        error_queue = self.error_queue

        chunks = []
        while True:
            try:
                chunks.append(received_queue.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self._append_received(b"".join(chunks))

        errors = []
        while True:
            try:
                errors.append(error_queue.get_nowait())
            except queue.Empty:
                break
        if errors:
            self._handle_error(errors[-1])

    @property
    def received_queue(self):
        return self.controller.received_queue

    @property
    def error_queue(self):
        return self.controller.error_queue

    def _append_received(self, data: bytes) -> None:
        self._receive_buffer.append(data)
        if self.receive_mode_combo.currentText() == TEXT_MODE:
            text = self._decoder.decode(data)
            if text:
                self._append_display(text)
        else:  # HEX：只格式化新到达字节，全量渲染仅在切换时发生
            rendered = format_hex(data)
            if rendered:
                if self._hex_has_content:
                    rendered = " " + rendered
                self._append_display(rendered)
                self._hex_has_content = True

    def _append_display(self, text: str) -> None:
        cursor = self.display_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.display_edit.setTextCursor(cursor)
        self.display_edit.insertPlainText(text)
        # 追加后始终跟随末尾，保证最新接收内容可见
        self._scroll_to_end(self.display_edit)

    @staticmethod
    def _scroll_to_end(edit) -> None:
        """把显示区滚动条拨到末尾（maximum 随内容同步更新）。"""
        sb = edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _re_render(self) -> None:
        """模式/编码切换时全量重渲染历史（仅此时 O(n)）。

        文本模式用新解码器对全部 raw 历史重放但不 flush：尾部不完整多字节
        序列留在 decoder pending，后续新字节到达时仍能拼出完整字符；HEX
        模式全量 format，切回文本时再从全部 raw 重建 pending。
        """
        mode = self.receive_mode_combo.currentText()
        encoding = self._current_encoding()
        try:
            decoder = IncrementalTextDecoder(encoding)
        except ValueError:
            return
        self._hex_has_content = False
        raw = self._receive_buffer.raw()
        # 重渲染前记录是否位于末尾：setPlainText 会把滚动条重置到顶部
        was_at_end = self.display_edit.verticalScrollBar().maximum() > 0 and (
            self.display_edit.verticalScrollBar().value()
            >= self.display_edit.verticalScrollBar().maximum()
        )
        try:
            if mode == TEXT_MODE:
                # 重放全部历史但不 flush：截断的尾部序列保持 pending
                text = decoder.decode(raw)
            else:
                text = self._receive_buffer.render("hex", encoding)
        except ValueError:
            return
        self._decoder = decoder
        self.display_edit.setPlainText(text)
        if was_at_end:
            self._scroll_to_end(self.display_edit)
        if mode == HEX_MODE and text:
            self._hex_has_content = True

    def _handle_error(self, err) -> None:
        if not self._is_open:
            return
        message = err if isinstance(err, str) else str(err)
        self._close_connection()
        QMessageBox.critical(self, "串口错误", message)

    # --------------------------------------------------------------- clear

    def _on_clear(self) -> None:
        # 丢弃点击时 receive 队列中尚未被 drain 取走的旧字节，避免随后
        # QTimer/_drain_queues 把清空前数据重新显示；只取此刻可取的项，
        # 点击后新到达的数据不受影响。
        received_queue = self.received_queue
        while True:
            try:
                received_queue.get_nowait()
            except queue.Empty:
                break
        self._receive_buffer.clear()
        try:
            self._decoder = IncrementalTextDecoder(self._current_encoding())
        except ValueError:
            pass
        self._hex_has_content = False
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
        self._close_connection()
        super().closeEvent(event)
