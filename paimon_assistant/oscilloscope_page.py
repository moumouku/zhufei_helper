"""Qt waveform page for the issue 012 tracer slice.

The page owns its receive start/stop control, its own frame data area, a
QtCharts CH1 view, a channel panel and its own non-modal diagnostics. All
serial/framing/protocol work lives outside: the page only reacts to
``start_requested`` / ``stop_requested`` / ``clear_requested`` and to complete
``ReceivedEvent`` frames handed in by the main window while this page is the
only receive consumer.
"""

from __future__ import annotations

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .oscilloscope import OscilloscopeSession, format_frame_line
from .theme import COLORS, SIZES, data_font


class OscilloscopePage(QWidget):
    """波形页：独立开始/停止、数据显示区、图表、通道区和诊断区。"""

    start_requested = Signal()
    stop_requested = Signal()
    clear_requested = Signal()

    def __init__(self, session: OscilloscopeSession, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("oscilloscope_page")
        self.session = session
        self._receiving = False
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------- public

    @property
    def is_receiving(self) -> bool:
        return self._receiving

    def begin_acquisition(self) -> None:
        """First successful start establishes ``T+0``; later starts keep it."""
        self.session.begin_acquisition()
        self._receiving = True
        self.start_button.setText("停止接收")
        self.state_label.setText("接收中")

    def end_acquisition(self) -> None:
        """Stop receiving but keep every displayed frame and sample."""
        self._receiving = False
        self.start_button.setText("开始接收")
        self.state_label.setText("已停止")

    def show_diagnostic(self, text: str) -> None:
        self.diagnostic_label.setText(text or "")
        self.diagnostic_label.setVisible(bool(text))

    def show_log_error(self, text: str) -> None:
        self.log_error_label.setText(text or "")
        self.log_error_label.setVisible(bool(text))

    def consume_events(self, events) -> None:
        """完整帧 → 数据区记录；合法帧另产生采样、图表点和通道最新值。"""
        for event in events:
            record = self.session.consume(event)
            self.display_edit.appendPlainText(format_frame_line(record))
            if record.values is not None:
                self._append_sample(record.relative_seconds, record.values[0])
        self._fit_axes()

    def clear_acquisition(self) -> None:
        """清空波形/新建采集：重置时间原点、历史、图表和通道栏。"""
        self.session.reset()
        self.display_edit.clear()
        self.ch1_series.clear()
        for label in self.channel_labels.values():
            self.channel_layout.removeWidget(label)
            label.deleteLater()
        self.channel_labels.clear()
        self.axis_x.setRange(0.0, 1.0)
        self.axis_y.setRange(-1.0, 1.0)
        self.show_diagnostic("")

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SIZES["spacing"])

        toolbar = QHBoxLayout()
        toolbar.setSpacing(SIZES["spacing"])
        self.start_button = QPushButton("开始接收")
        self.start_button.setObjectName("oscilloscope_start_button")
        self.clear_button = QPushButton("清空波形")
        self.clear_button.setObjectName("oscilloscope_clear_button")
        self.state_label = QLabel("未开始")
        self.state_label.setObjectName("oscilloscope_state_label")
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.state_label)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.diagnostic_label = QLabel("")
        self.diagnostic_label.setObjectName("oscilloscope_diagnostic_label")
        self.diagnostic_label.setStyleSheet(f"color: {COLORS['error']};")
        self.diagnostic_label.setTextFormat(Qt.PlainText)
        self.diagnostic_label.setWordWrap(True)
        self.diagnostic_label.setVisible(False)
        root.addWidget(self.diagnostic_label)

        self.log_error_label = QLabel("")
        self.log_error_label.setObjectName("oscilloscope_log_error_label")
        self.log_error_label.setStyleSheet(f"color: {COLORS['error']};")
        self.log_error_label.setTextFormat(Qt.PlainText)
        self.log_error_label.setWordWrap(True)
        self.log_error_label.setVisible(False)
        root.addWidget(self.log_error_label)

        content = QHBoxLayout()
        content.setSpacing(SIZES["spacing"])
        self.display_edit = QPlainTextEdit()
        self.display_edit.setObjectName("oscilloscope_display_edit")
        self.display_edit.setFont(data_font())
        self.display_edit.setReadOnly(True)
        self.display_edit.setPlaceholderText("等待完整帧，对端需以 \\r\\n 结束的整数载荷")
        content.addWidget(self.display_edit, 1)

        self.chart = QChart()
        self.chart.setBackgroundBrush(QColor(COLORS["receive"]))
        self.chart.legend().setVisible(True)
        self.chart.legend().setLabelColor(QColor(COLORS["text"]))
        self.ch1_series = QLineSeries()
        self.ch1_series.setName("CH1")
        self.ch1_series.setPen(QPen(QColor(COLORS["accent"]), 2))
        self.chart.addSeries(self.ch1_series)
        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("T+ s")
        self.axis_x.setRange(0.0, 1.0)
        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("值")
        self.axis_y.setRange(-1.0, 1.0)
        for axis in (self.axis_x, self.axis_y):
            axis.setLabelsColor(QColor(COLORS["secondary"]))
            axis.setTitleBrush(QColor(COLORS["secondary"]))
            axis.setGridLineColor(QColor(COLORS["divider"]))
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.ch1_series.attachAxis(self.axis_x)
        self.ch1_series.attachAxis(self.axis_y)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setObjectName("oscilloscope_chart_view")
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content.addWidget(self.chart_view, 2)

        self.channel_panel = QWidget()
        self.channel_panel.setObjectName("oscilloscope_channel_panel")
        self.channel_layout = QVBoxLayout(self.channel_panel)
        self.channel_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_layout.setSpacing(SIZES["spacing"])
        channel_title = QLabel("通道")
        self.channel_layout.addWidget(channel_title)
        self.channel_labels: dict[str, QLabel] = {}
        self.channel_layout.addStretch(1)
        self.channel_panel.setFixedWidth(140)
        content.addWidget(self.channel_panel)
        root.addLayout(content, 1)

    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self._on_start_clicked)
        self.clear_button.clicked.connect(self.clear_requested.emit)

    def _on_start_clicked(self) -> None:
        if self._receiving:
            self.stop_requested.emit()
        else:
            self.start_requested.emit()

    # ------------------------------------------------------------ channels

    def _append_sample(self, relative_seconds: float, value: int) -> None:
        self.ch1_series.append(relative_seconds, value)
        self._update_channel_value("CH1", value)

    def _fit_axes(self) -> None:
        """最小可见范围：保证已采集点落在视口内且常值数据仍可见。"""
        samples = self.session.samples
        if not samples:
            return
        x_max = max(sample.relative_seconds for sample in samples)
        values = [value for sample in samples for value in sample.values]
        y_min, y_max = min(values), max(values)
        if y_min == y_max:
            y_min, y_max = y_min - 1, y_max + 1
        self.axis_x.setRange(0.0, max(x_max * 1.05, 1e-3))
        self.axis_y.setRange(y_min, y_max)

    def _update_channel_value(self, name: str, value: int) -> None:
        label = self.channel_labels.get(name)
        if label is None:
            label = QLabel(f"{name}  {value}")
            label.setObjectName(f"channel_{name}_label")
            self.channel_layout.insertWidget(self.channel_layout.count() - 1, label)
            self.channel_labels[name] = label
            return
        label.setText(f"{name}  {value}")
