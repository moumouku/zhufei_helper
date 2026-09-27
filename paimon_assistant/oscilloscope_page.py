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
from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .oscilloscope import (
    OscilloscopeConnectionBoundary,
    OscilloscopeSession,
    format_connection_boundary_line,
    format_frame_line,
)
from .theme import CHANNEL_COLORS, COLORS, SIZES, data_font


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
        self._rendering_history = False
        #: 页面已送入数据区的模型记录数；用于检测模型是否淘汰了旧记录。
        self._records_rendered = 0
        #: 页面已送入绘图 series 的模型采样点数；用于检测模型是否淘汰了旧点。
        self._samples_rendered = 0
        #: “跟随最新”默认开启；用户上翻后暂停，只有点击“回到最新”才恢复。
        self._follow_latest = True
        self._user_scroll_pending = False
        self._scroll_settle_generation = 0
        self._scroll_anchor_value = None
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------- public

    @property
    def is_receiving(self) -> bool:
        return self._receiving

    def begin_acquisition(self, origin_ns=None) -> None:
        """First successful start establishes ``T+0``; later starts keep it.

        ``origin_ns`` is captured before the connection opens, so the first
        framed boundary can never precede the origin (issue 014).
        """
        self.session.begin_acquisition(origin_ns)
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

    def show_connection_boundary(self, settings, at_ns=None) -> None:
        """恢复接收且参数变化时，在数据区插入连接边界记录（REQ-0005 §11.3）。"""
        record = self.session.note_connection_boundary(settings, at_ns)
        self._sync_display([record])

    def consume_events(self, events) -> None:
        """完整帧 → 数据区记录；合法帧另产生采样、图表点和通道最新值。

        数据区文本以模型保留记录为准：模型只追加时增量显示；一旦按 180 秒
        窗口淘汰旧记录，就从模型重渲，显示区不会继续积累窗口外文本。
        """
        new_records = [self.session.consume(event) for event in events]
        new_legal = [record for record in new_records if record.values is not None]
        self._sync_display(new_records)
        self._sync_chart(new_legal)
        self._fit_axes()

    def _sync_display(self, new_records) -> None:
        """模型只追加时保留现有文本和阅读位置；发生淘汰时从模型重渲。"""
        if self.session.record_count != self._records_rendered + len(new_records):
            self._render_history()
            return
        for record in new_records:
            self.display_edit.appendPlainText(self._format_record(record))
        self._records_rendered += len(new_records)
        if self._follow_latest:
            self._scroll_to_end()
            self._begin_scroll_settle()

    def _sync_chart(self, new_legal) -> None:
        """模型只追加时按增量续点；一旦模型淘汰了旧采样就从模型重建。"""
        if self._samples_rendered + len(new_legal) != self.session.sample_count:
            self._rebuild_chart_series()
        else:
            for record in new_legal:
                self._append_sample(record.relative_seconds, record.values)
        self._samples_rendered = self.session.sample_count

    def _rebuild_chart_series(self) -> None:
        """用模型保留的缺口分段点重建全部 series，使图表与模型一致。

        通道最新值和通道栏行从模型恢复；通道开关状态保留，只看模型实际
        保留的点，不保留任何已被 180 秒窗口淘汰的绘制点。
        """
        for series_list in self.channel_series.values():
            for series in series_list:
                self.chart.removeSeries(series)
                series.deleteLater()
        self.channel_series = {}
        self.ch1_series = None
        for index in range(self.session.channel_count):
            self._ensure_channel_row(index)
            latest = self.session.channel_latest_value(index)
            if latest is not None:
                self._update_channel_value(f"CH{index + 1}", latest)
            first_series = None
            for points in self.session.channel_segments(index):
                series = self._add_channel_segment(index)
                if first_series is None:
                    first_series = series
                for relative_seconds, value in points:
                    series.append(float(relative_seconds), float(value))
            if first_series is None:
                first_series = self._add_channel_segment(index)
            if index == 0:
                self.ch1_series = first_series
        if self.ch1_series is None:
            self.ch1_series = self._add_channel_segment(0)

    def _render_history(self) -> None:
        """从模型当前保留的记录重渲数据区（记录淘汰或清空后调用）。

        重渲尊重跟随状态：跟随开启滚动到末尾；暂停时按旧文本前缀或比例
        保留阅读位置，数据继续进入模型和文本但不强制移动视口。
        """
        if self._rendering_history:
            return
        anchor = self._capture_display_anchor()
        self._rendering_history = True
        try:
            text = "\n".join(
                self._format_record(record) for record in self.session.records
            )
            self.display_edit.setPlainText(text)
            self._records_rendered = self.session.record_count
            self._restore_display_anchor(anchor, text)
            self._begin_scroll_settle()
        finally:
            self._rendering_history = False

    @staticmethod
    def _format_record(record) -> str:
        if isinstance(record, OscilloscopeConnectionBoundary):
            return format_connection_boundary_line(record)
        return format_frame_line(record)

    def clear_acquisition(self) -> None:
        """清空波形/新建采集：重置时间原点、历史、图表和通道栏。"""
        was_receiving = self._receiving
        self.session.reset()
        self._records_rendered = 0
        self._samples_rendered = 0
        if was_receiving:
            # 活动接收清空立即以清空时刻重设 T+0；后续完整帧按新原点计时。
            self.session.begin_acquisition()
        self.display_edit.clear()
        self._reset_channels()
        self.axis_x.setRange(0.0, 1.0)
        self.axis_y.setRange(-1.0, 1.0)
        self.show_diagnostic("")
        self._set_follow_latest(True)
        self._scroll_anchor_value = None

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
        self.follow_button = QPushButton("跟随最新")
        self.follow_button.setObjectName("oscilloscope_follow_button")
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        self.state_label = QLabel("未开始")
        self.state_label.setObjectName("oscilloscope_state_label")
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.follow_button)
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
        #: 通道下标（0 起）→ 缺口分段的 QLineSeries 列表。
        self.channel_series: dict[int, list[QLineSeries]] = {}
        self.channel_rows: dict[str, QWidget] = {}
        self.channel_checks: dict[str, QCheckBox] = {}
        self.channel_labels: dict[str, QLabel] = {}
        self.ch1_series = self._add_channel_segment(0)
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
        self.channel_layout.addStretch(1)
        # 最小宽度保留设计下限；宽度随内容增长，避免裁剪 int32 值（通道名称与值都必须可见）。
        self.channel_panel.setMinimumWidth(140)
        content.addWidget(self.channel_panel)
        root.addLayout(content, 1)

    def _reset_channels(self) -> None:
        """清空通道推断、通道栏和绘制分段，重建一个空的 CH1 兼容 series。"""
        for series_list in self.channel_series.values():
            for series in series_list:
                self.chart.removeSeries(series)
                series.deleteLater()
        self.channel_series = {}
        for row in self.channel_rows.values():
            self.channel_layout.removeWidget(row)
            row.deleteLater()
        self.channel_rows.clear()
        self.channel_checks.clear()
        self.channel_labels.clear()
        self.ch1_series = self._add_channel_segment(0)

    def _connect_signals(self) -> None:
        self.start_button.clicked.connect(self._on_start_clicked)
        self.clear_button.clicked.connect(self.clear_requested.emit)
        self.follow_button.clicked.connect(self._on_follow_clicked)
        scrollbar = self.display_edit.verticalScrollBar()
        scrollbar.actionTriggered.connect(self._on_display_scroll_action)
        scrollbar.valueChanged.connect(self._on_display_scroll_changed)

    def _on_start_clicked(self) -> None:
        if self._receiving:
            self.stop_requested.emit()
        else:
            self.start_requested.emit()

    # ------------------------------------------------------- follow latest

    def _set_follow_latest(self, enabled: bool, *, scroll_to_end: bool = False) -> None:
        self._follow_latest = bool(enabled)
        with QSignalBlocker(self.follow_button):
            self.follow_button.setChecked(self._follow_latest)
        self.follow_button.setText("跟随最新" if self._follow_latest else "回到最新")
        if scroll_to_end:
            self._scroll_to_end()

    def _on_follow_clicked(self, checked: bool) -> None:
        self._set_follow_latest(checked, scroll_to_end=checked)

    def _on_display_scroll_action(self, _action: int) -> None:
        """记录真实用户滚动意图；程序性 ``setValue`` 不触发此信号。"""
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
        scrollbar = self.display_edit.verticalScrollBar()
        with QSignalBlocker(scrollbar):
            scrollbar.setValue(scrollbar.maximum())

    def _apply_anchor_value(self, value: int) -> None:
        scrollbar = self.display_edit.verticalScrollBar()
        with QSignalBlocker(scrollbar):
            scrollbar.setValue(max(0, min(value, scrollbar.maximum())))

    def _capture_display_anchor(self) -> dict:
        scrollbar = self.display_edit.verticalScrollBar()
        return {
            "value": scrollbar.value(),
            "ratio": (
                scrollbar.value() / scrollbar.maximum()
                if scrollbar.maximum()
                else 1.0
            ),
            "text": self.display_edit.toPlainText(),
        }

    def _restore_display_anchor(self, anchor: dict, text: str) -> None:
        if self._follow_latest:
            self._scroll_anchor_value = None
            self._scroll_to_end()
            return
        scrollbar = self.display_edit.verticalScrollBar()
        if text.startswith(anchor["text"]):
            value = min(anchor["value"], scrollbar.maximum())
        else:
            value = (
                round(anchor["ratio"] * scrollbar.maximum())
                if scrollbar.maximum()
                else 0
            )
        self._apply_anchor_value(value)
        self._scroll_anchor_value = value

    def _begin_scroll_settle(self) -> None:
        """本轮事件循环结束后落定最终滚动位置，纠正 Qt 延迟布局。"""
        self._scroll_settle_generation += 1
        generation = self._scroll_settle_generation
        QTimer.singleShot(0, lambda: self._finish_scroll_settle(generation))

    def _finish_scroll_settle(self, generation: int) -> None:
        if generation != self._scroll_settle_generation:
            return
        if self._follow_latest:
            self._scroll_to_end()
        elif self._scroll_anchor_value is not None:
            self._apply_anchor_value(self._scroll_anchor_value)

    # ------------------------------------------------------------ channels

    def _append_sample(self, relative_seconds: float, values: tuple[int, ...]) -> None:
        for index, value in enumerate(values):
            name = f"CH{index + 1}"
            self._ensure_channel_row(index)
            self._update_channel_value(name, value)
            self._append_channel_point(index, relative_seconds, value)

    def _add_channel_segment(self, index: int) -> QLineSeries:
        """为一个通道追加一段真实的 QLineSeries，并接管其坐标轴。"""
        series = QLineSeries()
        series.setName(f"CH{index + 1}")
        series.setPen(QPen(QColor(CHANNEL_COLORS[index]), 2))
        series.setPointsVisible(True)  # 单点分段也必须可见
        check = self.channel_checks.get(f"CH{index + 1}")
        if check is not None:
            series.setVisible(check.isChecked())
        self.chart.addSeries(series)
        series.attachAxis(self.axis_x)
        series.attachAxis(self.axis_y)
        self.channel_series.setdefault(index, []).append(series)
        return series

    def _append_channel_point(self, index: int, relative_seconds: float, value: int) -> None:
        """按模型的分段数决定续接当前分段还是开始新分段（缺口断线）。"""
        segments = self.channel_series.setdefault(index, [])
        if len(segments) < self.session.channel_segment_count(index):
            self._add_channel_segment(index)
        segments[-1].append(float(relative_seconds), float(value))

    def _ensure_channel_row(self, index: int) -> None:
        """发现新通道时建立通道栏行：默认勾选的开关 + 名称/最新值标签。"""
        name = f"CH{index + 1}"
        if name in self.channel_checks:
            return
        row = QWidget()
        row.setObjectName(f"channel_{name}_row")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(SIZES["spacing"])
        check = QCheckBox()
        check.setObjectName(f"channel_{name}_check")
        check.setAccessibleName(name)
        check.setChecked(True)
        label = QLabel("")
        label.setObjectName(f"channel_{name}_label")
        label.setTextFormat(Qt.PlainText)
        check.toggled.connect(
            lambda visible, channel=index: self._set_channel_visible(channel, visible)
        )
        row_layout.addWidget(check)
        row_layout.addWidget(label)
        row_layout.addStretch(1)
        self.channel_layout.insertWidget(self.channel_layout.count() - 1, row)
        self.channel_rows[name] = row
        self.channel_checks[name] = check
        self.channel_labels[name] = label

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
        self.channel_labels[name].setText(f"{name}  {value}")

    def _set_channel_visible(self, index: int, visible: bool) -> None:
        """开关只控制绘制：采样、最新值和缺口分段照常维护。"""
        for series in self.channel_series.get(index, []):
            series.setVisible(visible)
