"""Qt waveform page for the issue 012 tracer slice.

The page owns its receive start/stop control, its own frame data area, a
QtCharts CH1 view, a channel panel and its own non-modal diagnostics. All
serial/framing/protocol work lives outside: the page only reacts to
``start_requested`` / ``stop_requested`` / ``clear_requested`` and to complete
``ReceivedEvent`` frames handed in by the main window while this page is the
only receive consumer.
"""

from __future__ import annotations

import math

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
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
    INT32_MAX,
    INT32_MIN,
    OscilloscopeConnectionBoundary,
    OscilloscopeSession,
    format_connection_boundary_line,
    format_frame_line,
    format_relative_seconds,
)
from .oscilloscope_chart import (
    DEFAULT_X_MAX,
    DEFAULT_X_MIN,
    DEFAULT_Y_MAX,
    DEFAULT_Y_MIN,
    build_chart_segments,
    clamp_x_range,
    fit_x_range,
    fit_y_range,
    keep_x_span_in_range,
    shift_range,
    zoom_range,
)
from .theme import CHANNEL_COLORS, COLORS, SIZES, data_font

#: 可见 +/- 按钮与滚轮的每级缩放倍率；factor<1 为放大（跨度变小）。
ZOOM_STEP = 1.25

#: 悬停邻近判定阈值（屏幕/视口像素）；附近必须有真实原始采样点。
HOVER_DISTANCE_PX = 8.0

#: 事件修饰键以 int 信号传递（PySide6 的 KeyboardModifier 不可直接 int()）。
_CTRL_MODIFIER = Qt.KeyboardModifier.ControlModifier.value
_SHIFT_MODIFIER = Qt.KeyboardModifier.ShiftModifier.value


class OscilloscopeChartView(QChartView):
    """图表视图：把原生滚轮/鼠标/尺寸事件转成页面可测的交互信号。

    不继承 QGraphicsView 的默认滚轮滚动，所有交互都围绕图表轴坐标处理；
    页面负责坐标映射和轴夹紧，信号只携带 viewport 像素位置与修饰键。
    """

    wheel_zoom_requested = Signal(float, float, float, int)
    pan_started = Signal(float, float, int)
    pointer_moved = Signal(float, float)
    pan_finished = Signal()
    pointer_left = Signal()
    view_resized = Signal()

    def __init__(self, chart, parent=None) -> None:
        super().__init__(chart, parent)
        # 无按键悬停也要收到 mouseMoveEvent；viewport 是实际接收事件的子控件。
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        delta = float(event.angleDelta().y())
        if delta:
            position = event.position()
            self.wheel_zoom_requested.emit(
                delta, position.x(), position.y(), event.modifiers().value
            )
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            position = event.position()
            self.pan_started.emit(
                position.x(), position.y(), event.modifiers().value
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        position = event.position()
        self.pointer_moved.emit(position.x(), position.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.pan_finished.emit()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.pointer_left.emit()
        super().leaveEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        # 真实鼠标离开通常由 viewport 收到 Leave；leaveEvent 只覆盖视图本身。
        if event.type() == QEvent.Type.Leave:
            self.pointer_left.emit()
        return super().viewportEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.view_resized.emit()


class OscilloscopePage(QWidget):
    """波形页：独立开始/停止、数据显示区、图表、通道区和诊断区。"""

    start_requested = Signal()
    stop_requested = Signal()
    clear_requested = Signal()
    resource_failed = Signal(str)

    def __init__(self, session: OscilloscopeSession, parent=None, *, chart_builder=None) -> None:
        super().__init__(parent)
        self.setObjectName("oscilloscope_page")
        self.session = session
        self._receiving = False
        self._rendering_history = False
        #: 页面已送入数据区的模型记录数；用于检测模型是否淘汰了旧记录。
        self._records_rendered = 0
        #: “跟随最新”默认开启；用户上翻后暂停，只有点击“回到最新”才恢复。
        self._follow_latest = True
        #: 图表是否跟随最新数据；手动缩放/平移后关闭，与数据区跟随互相独立。
        self._chart_following = True
        #: 用户手动选择的 X 宽度；None 表示跟随态适配全部保留范围。
        self._manual_x_span: float | None = None
        #: 首个有效帧到达时是否还需要适配初始 Y；清空/新建采集重置。
        self._initial_y_pending = True
        #: 左键拖动状态：起始修饰键决定平移哪些轴，上一图表值用于增量。
        self._dragging = False
        self._drag_axes = (False, False)
        self._drag_last_value = None
        #: 绘图资源失败后保持原始历史但不再重试绘制；由主窗口负责关闭串口。
        self._resource_failed = False
        #: 可注入的绘图函数，测试资源失败路径时不依赖真实内存耗尽。
        self._chart_builder = (
            chart_builder if chart_builder is not None else build_chart_segments
        )
        self._user_scroll_pending = False
        self._scroll_settle_generation = 0
        self._scroll_anchor_value = None
        self._chart_rerender_pending = False
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
        """完整帧 → 数据区记录；合法帧另产生采样、绘图点和通道最新值。

        数据区文本以模型保留记录为准：模型只追加时增量显示；一旦按 180 秒
        窗口淘汰旧记录，就从模型重渲，显示区不会继续积累窗口外文本。
        波形每次都由视口驱动从模型重新生成，绘图 series 不是原始存储。
        """
        new_records = [self.session.consume(event) for event in events]
        new_legal = [record for record in new_records if record.values is not None]
        self._sync_display(new_records)
        for record in new_legal:
            for index, value in enumerate(record.values):
                self._ensure_channel_row(index)
                self._update_channel_value(f"CH{index + 1}", value)
        self._sync_view_for_data()
        self._fit_initial_y(new_legal)
        self._render_chart_guarded()

    # ------------------------------------------------------- view access

    @property
    def chart_following(self) -> bool:
        """图表是否实时跟随最新数据；手动缩放/平移后为 ``False``。"""
        return self._chart_following

    def x_view_range(self) -> tuple[float, float]:
        return (self.axis_x.min(), self.axis_x.max())

    def y_view_range(self) -> tuple[float, float]:
        return (self.axis_y.min(), self.axis_y.max())

    def zoom_x(self, factor: float, anchor: float | None = None) -> None:
        """围绕图表 X 坐标 ``anchor`` 缩放 X 轴；缺省以视口中心为中心。

        ``factor < 1`` 放大（跨度变小）。结果始终夹紧在仍保留的时间窗内，
        非零且非反向；只要应用就退出实时跟随。
        """
        if not math.isfinite(factor) or factor <= 0.0:
            return
        x_min, x_max = self.x_view_range()
        samples = self.session.samples
        retained = (
            fit_x_range(samples) if samples else (DEFAULT_X_MIN, DEFAULT_X_MAX)
        )
        if anchor is None:
            anchor = (x_min + x_max) / 2.0
        if not math.isfinite(anchor):
            return
        new_x = zoom_range(x_min, x_max, factor, anchor, retained[0], retained[1])
        if new_x[1] <= new_x[0]:
            return
        self.axis_x.setRange(*new_x)
        self._manual_x_span = new_x[1] - new_x[0]
        self._chart_following = False
        self._set_history_hint(False)
        self._render_chart_guarded()

    def zoom_y(self, factor: float, anchor: float | None = None) -> None:
        """围绕图表 Y 坐标 ``anchor`` 缩放 Y 轴；夹紧在 int32 硬范围。"""
        if not math.isfinite(factor) or factor <= 0.0:
            return
        y_min, y_max = self.y_view_range()
        if anchor is None:
            anchor = (y_min + y_max) / 2.0
        if not math.isfinite(anchor):
            return
        new_y = zoom_range(y_min, y_max, factor, anchor, INT32_MIN, INT32_MAX)
        if new_y[1] <= new_y[0]:
            return
        self.axis_y.setRange(*new_y)
        self._initial_y_pending = False
        self._chart_following = False
        self._render_chart_guarded()

    def auto_scale(self) -> None:
        """一次性把 X 适配保留采样范围，并只按可见 X 内开启通道计算 Y。"""
        x_min, x_max = fit_x_range(self.session.samples)
        enabled_segments = [
            self.session.channel_segments(index)
            for index in range(self.session.channel_count)
            if self._channel_enabled(index)
        ]
        self.axis_x.setRange(x_min, x_max)
        self.axis_y.setRange(*fit_y_range(enabled_segments, x_min, x_max))
        self._manual_x_span = None
        self._initial_y_pending = False
        self._set_history_hint(False)
        self._render_chart_guarded()

    def return_to_latest(self) -> None:
        """把 X 视口移回最新保留数据并恢复实时跟随；不隐式缩放 Y。

        保留用户手动选定的 X 宽度（§8.6 的“移动”而不是自动缩放全部）；
        从未手动选过宽度时仍适配全部保留范围。
        """
        self._chart_following = True
        samples = self.session.samples
        if samples:
            retained = fit_x_range(samples)
            if self._manual_x_span is None:
                self.axis_x.setRange(*retained)
            else:
                shifted = keep_x_span_in_range(
                    retained[1] - self._manual_x_span, retained[1], *retained
                )
                self.axis_x.setRange(*shifted)
        self._set_history_hint(False)
        self._render_chart_guarded()

    def set_view_range(self, x_min, x_max, y_min=None, y_max=None) -> None:
        """手动设置视口（供缩放/平移使用）；合法范围外的请求被忽略。

        X 被夹紧到仍保留的采样窗口，Y 被夹紧到 int32 硬范围；只要成功
        应用就退出实时跟随，后续新数据不会把视口强制移回。
        """
        try:
            requested_x = (float(x_min), float(x_max))
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(value) for value in requested_x):
            return
        if requested_x[1] <= requested_x[0]:
            return
        if (y_min is None) != (y_max is None):
            return
        samples = self.session.samples
        retained = fit_x_range(samples) if samples else (DEFAULT_X_MIN, DEFAULT_X_MAX)
        new_x = clamp_x_range(requested_x[0], requested_x[1], *retained)
        if new_x[1] <= new_x[0]:
            return
        if y_min is not None:
            try:
                requested_y = (float(y_min), float(y_max))
            except (TypeError, ValueError):
                return
            if not all(math.isfinite(value) for value in requested_y):
                return
            y_low = max(INT32_MIN, requested_y[0])
            y_high = min(INT32_MAX, requested_y[1])
            if y_high <= y_low:
                return
            self.axis_y.setRange(y_low, y_high)
            self._initial_y_pending = False
        self.axis_x.setRange(*new_x)
        self._manual_x_span = new_x[1] - new_x[0]
        self._chart_following = False
        self._set_history_hint(False)
        self._render_chart_guarded()

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

    def _sync_view_for_data(self) -> None:
        """新数据后的 X 视口：跟随则适配/滑动保留范围，手动则夹紧不越界。"""
        samples = self.session.samples
        if not samples:
            return
        retained = fit_x_range(samples)
        if self._chart_following:
            if self._manual_x_span is None:
                self.axis_x.setRange(*retained)
            else:
                shifted = keep_x_span_in_range(
                    retained[1] - self._manual_x_span, retained[1], *retained
                )
                self.axis_x.setRange(*shifted)
            self._set_history_hint(False)
            return
        current = self.x_view_range()
        shifted = keep_x_span_in_range(current[0], current[1], *retained)
        if shifted != current:
            self.axis_x.setRange(*shifted)
            self._set_history_hint(True)
        else:
            self._set_history_hint(False)

    def _fit_initial_y(self, new_legal) -> None:
        """首个有效帧到达时把 Y 适配到当前视口内开启通道的真实值。

        保证默认 Y[-1,1] 之外的常规首次采样可见；只做一次，清空/新建
        采集后重置。用户显式选过 Y 时不覆盖。
        """
        if not self._initial_y_pending or not new_legal:
            return
        x_min, x_max = self.x_view_range()
        enabled_segments = [
            self.session.channel_segments(index)
            for index in range(self.session.channel_count)
            if self._channel_enabled(index)
        ]
        self.axis_y.setRange(*fit_y_range(enabled_segments, x_min, x_max))
        self._initial_y_pending = False

    def _render_chart_guarded(self) -> None:
        """绘图失败不丢原始历史：停页并上抛信号，由主窗口关闭连接。

        数据/视口/尺寸变化后旧悬停读值可能已不对应指针位置，必须在
        重绘时隐藏，避免误解为当前位置数据。
        """
        self._hide_hover()
        if self._resource_failed:
            return
        try:
            self._render_chart()
        except Exception as error:  # Qt/内存资源失败必须停页保数据
            self._on_resource_failed(error)

    def _on_resource_failed(self, error: BaseException) -> None:
        self._resource_failed = True
        self._receiving = False
        self.start_button.setText("开始接收")
        self.state_label.setText("已停止：绘图资源不足")
        self.show_diagnostic(
            f"绘图资源不足，已停止波形接收；已采集的原始数据仍保留（{error}）"
        )
        self.resource_failed.emit(str(error))

    def _render_chart(self) -> None:
        """按当前可见 X、图表像素宽度及通道开关从原始采样重生成 series。

        模型保留的记录数/采样数只增或按窗口淘汰，绘图不持有唯一原始数据；
        每个通道的每个缺口分段独立成段，隐藏通道的 series 不显示但仍保留
        开关身份，重新勾选后从原始历史恢复。
        """
        x_min, x_max = self.x_view_range()
        pixel_width = self._chart_pixel_width()
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
            rendered = self._chart_builder(
                self.session.channel_segments(index), x_min, x_max, pixel_width
            )
            first_series = None
            for points in rendered or [[]]:
                series = self._add_channel_segment(index)
                if first_series is None:
                    first_series = series
                for relative_seconds, value in points:
                    series.append(float(relative_seconds), float(value))
            for series in self.channel_series[index][1:]:
                # 缺口分段只是同一通道的曲线延续，图例只保留一个身份条目。
                for marker in self.chart.legend().markers(series):
                    marker.setVisible(False)
            if index == 0:
                self.ch1_series = first_series
        if self.ch1_series is None:
            self.ch1_series = self._add_channel_segment(0)

    def _chart_pixel_width(self) -> int:
        """当前绘图区像素宽度；布局未完成时退回控件宽度，保证不为零。"""
        area = self.chart.plotArea()
        width = area.width() if area.width() > 0 else self.chart_view.width()
        return max(1, int(width))

    def _channel_enabled(self, index: int) -> bool:
        check = self.channel_checks.get(f"CH{index + 1}")
        return check is None or check.isChecked()

    def _set_history_hint(self, visible: bool) -> None:
        self.history_hint_label.setVisible(bool(visible))

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

    def clear_acquisition(self, origin_ns: int | None = None) -> None:
        """清空波形/新建采集：重置时间原点、历史、图表和通道栏。

        ``origin_ns`` 是控制器会话锁内捕获的新采集原点（issue 019）。提供时
        直接使用，不再读页面时钟。页面独立使用（无控制器）且正在接收时，
        退回本页时钟读取一次，保持既有行为。
        """
        was_receiving = self._receiving
        self.session.reset()
        self._records_rendered = 0
        # 显式新建采集才解除绘图资源失败状态；清理资源诊断与旧悬停读值。
        self._resource_failed = False
        if origin_ns is not None:
            self.session.begin_acquisition(origin_ns)
        elif was_receiving:
            self.session.begin_acquisition()
        self.display_edit.clear()
        self._reset_channels()
        self.axis_x.setRange(DEFAULT_X_MIN, DEFAULT_X_MAX)
        self.axis_y.setRange(DEFAULT_Y_MIN, DEFAULT_Y_MAX)
        self.show_diagnostic("")
        self._set_follow_latest(True)
        self._chart_following = True
        self._manual_x_span = None
        self._initial_y_pending = True
        self._set_history_hint(False)
        self._scroll_anchor_value = None
        # 使清空前排队的滚动/图表落定失效，避免旧锚点重新定位新采集。
        self._scroll_settle_generation += 1
        self._user_scroll_pending = False
        self._dragging = False
        self._drag_axes = (False, False)
        self._drag_last_value = None
        self._hide_hover()

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
        self.auto_scale_button = QPushButton("自动缩放")
        self.auto_scale_button.setObjectName("oscilloscope_auto_scale_button")
        self.chart_latest_button = QPushButton("回到最新")
        self.chart_latest_button.setObjectName("oscilloscope_chart_latest_button")
        self.x_zoom_in_button = QPushButton("X+")
        self.x_zoom_in_button.setObjectName("oscilloscope_x_zoom_in_button")
        self.x_zoom_in_button.setToolTip("X 轴放大")
        self.x_zoom_out_button = QPushButton("X-")
        self.x_zoom_out_button.setObjectName("oscilloscope_x_zoom_out_button")
        self.x_zoom_out_button.setToolTip("X 轴缩小")
        self.y_zoom_in_button = QPushButton("Y+")
        self.y_zoom_in_button.setObjectName("oscilloscope_y_zoom_in_button")
        self.y_zoom_in_button.setToolTip("Y 轴放大")
        self.y_zoom_out_button = QPushButton("Y-")
        self.y_zoom_out_button.setObjectName("oscilloscope_y_zoom_out_button")
        self.y_zoom_out_button.setToolTip("Y 轴缩小")
        self.state_label = QLabel("未开始")
        self.state_label.setObjectName("oscilloscope_state_label")
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.follow_button)
        toolbar.addWidget(self.auto_scale_button)
        toolbar.addWidget(self.chart_latest_button)
        toolbar.addWidget(self.x_zoom_in_button)
        toolbar.addWidget(self.x_zoom_out_button)
        toolbar.addWidget(self.y_zoom_in_button)
        toolbar.addWidget(self.y_zoom_out_button)
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

        #: 历史淘汰越过当前 X 视口时的非模态状态（REQ-0005 §8.8）。
        self.history_hint_label = QLabel("历史正在滚动淘汰")
        self.history_hint_label.setObjectName("oscilloscope_history_hint_label")
        self.history_hint_label.setStyleSheet(f"color: {COLORS['warning']};")
        self.history_hint_label.setTextFormat(Qt.PlainText)
        self.history_hint_label.setVisible(False)
        root.addWidget(self.history_hint_label)

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
        self.chart_view = OscilloscopeChartView(self.chart)
        self.chart_view.setObjectName("oscilloscope_chart_view")
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.hover_label = QLabel(self.chart_view)
        self.hover_label.setObjectName("oscilloscope_hover_label")
        self.hover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hover_label.setFont(data_font())
        self.hover_label.setStyleSheet(
            f"background-color: {COLORS['panel']}; color: {COLORS['text']};"
            f" border: 1px solid {COLORS['border']}; padding: 2px 6px;"
        )
        self.hover_label.setVisible(False)
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
        self.auto_scale_button.clicked.connect(self.auto_scale)
        self.chart_latest_button.clicked.connect(self.return_to_latest)
        self.x_zoom_in_button.clicked.connect(lambda: self.zoom_x(1.0 / ZOOM_STEP))
        self.x_zoom_out_button.clicked.connect(lambda: self.zoom_x(ZOOM_STEP))
        self.y_zoom_in_button.clicked.connect(lambda: self.zoom_y(1.0 / ZOOM_STEP))
        self.y_zoom_out_button.clicked.connect(lambda: self.zoom_y(ZOOM_STEP))
        self.chart_view.wheel_zoom_requested.connect(self._on_wheel_zoom_requested)
        self.chart_view.pan_started.connect(self._on_pan_started)
        self.chart_view.pointer_moved.connect(self._on_pointer_moved)
        self.chart_view.pan_finished.connect(self._on_pan_finished)
        self.chart_view.pointer_left.connect(self._hide_hover)
        self.chart_view.view_resized.connect(self._schedule_chart_rerender)
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

    def _update_channel_value(self, name: str, value: int) -> None:
        self.channel_labels[name].setText(f"{name}  {value}")

    def _set_channel_visible(self, index: int, visible: bool) -> None:
        """开关只控制绘制：采样、最新值和缺口分段照常维护。"""
        for series in self.channel_series.get(index, []):
            series.setVisible(visible)
        # 悬停中的通道被关闭（或没有通道开启）时旧读值立即失效。
        self._hide_hover()

    def _on_wheel_zoom_requested(
        self, delta: float, viewport_x: float, viewport_y: float, modifiers: int
    ) -> None:
        """滚轮以指针所在图表坐标为锚点；默认 X，Ctrl 只缩放 Y。"""
        anchor = self._viewport_value(viewport_x, viewport_y)
        if anchor is None:
            return
        factor = 1.0 / ZOOM_STEP if delta > 0 else ZOOM_STEP
        if modifiers & _CTRL_MODIFIER:
            self.zoom_y(factor, anchor.y())
        else:
            self.zoom_x(factor, anchor.x())

    @staticmethod
    def _axes_for_modifiers(modifiers: int) -> tuple[bool, bool]:
        """默认同时平移 X/Y；Shift 只 X，Ctrl 只 Y。"""
        ctrl = bool(modifiers & _CTRL_MODIFIER)
        shift = bool(modifiers & _SHIFT_MODIFIER)
        if ctrl and not shift:
            return False, True
        if shift and not ctrl:
            return True, False
        return True, True

    def _on_pan_started(
        self, viewport_x: float, viewport_y: float, modifiers: int
    ) -> None:
        self._dragging = True
        self._drag_axes = self._axes_for_modifiers(modifiers)
        self._drag_last_value = self._viewport_value(viewport_x, viewport_y)
        self._hide_hover()

    def _on_pointer_moved(self, viewport_x: float, viewport_y: float) -> None:
        if not self._dragging:
            self._update_hover(viewport_x, viewport_y)
            return
        if self._drag_last_value is None:
            self._drag_last_value = self._viewport_value(viewport_x, viewport_y)
            return
        current = self._viewport_value(viewport_x, viewport_y)
        if current is None:
            return
        x_delta = self._drag_last_value.x() - current.x()
        y_delta = self._drag_last_value.y() - current.y()
        self._drag_last_value = current
        if x_delta == 0.0 and y_delta == 0.0:
            return
        self._apply_pan(x_delta, y_delta)

    def _on_pan_finished(self) -> None:
        self._dragging = False
        self._drag_axes = (False, False)
        self._drag_last_value = None

    def _apply_pan(self, x_delta: float, y_delta: float) -> None:
        """按起始修饰键平移对应轴；X 夹紧保留窗口，Y 夹紧 int32。"""
        pan_x, pan_y = self._drag_axes
        if pan_x:
            x_min, x_max = self.x_view_range()
            samples = self.session.samples
            retained = (
                fit_x_range(samples) if samples else (DEFAULT_X_MIN, DEFAULT_X_MAX)
            )
            new_x = keep_x_span_in_range(
                x_min + x_delta, x_max + x_delta, retained[0], retained[1]
            )
            if new_x[1] > new_x[0]:
                self.axis_x.setRange(*new_x)
                self._manual_x_span = new_x[1] - new_x[0]
        if pan_y:
            y_min, y_max = self.y_view_range()
            new_y = shift_range(y_min, y_max, y_delta, INT32_MIN, INT32_MAX)
            if new_y[1] > new_y[0]:
                self.axis_y.setRange(*new_y)
                self._initial_y_pending = False
        self._chart_following = False
        self._set_history_hint(False)
        self._render_chart_guarded()

    def _update_hover(self, viewport_x: float, viewport_y: float) -> None:
        """在最靠近指针的可见通道真实原始采样上显示整帧读值。

        邻近距离在屏幕/视口尺度上计算，但返回值只能来自 ``session.samples``
        的原始点；短帧缺失通道不生成虚构读数。没有开启通道、指针远离
        任何有效原始点时隐藏。
        """
        if not self._any_channel_enabled():
            self._hide_hover()
            return
        chart_position = self._viewport_chart_position(viewport_x, viewport_y)
        if chart_position is None:
            self._hide_hover()
            return
        best_sample = None
        best_distance = HOVER_DISTANCE_PX
        for sample in self.session.samples:
            values = sample.values
            for index in range(min(len(values), self.session.channel_count)):
                if not self._channel_enabled(index):
                    continue
                pixel = self.chart.mapToPosition(
                    QPointF(sample.relative_seconds, float(values[index])),
                    self.ch1_series,
                )
                distance = math.hypot(
                    pixel.x() - chart_position.x(), pixel.y() - chart_position.y()
                )
                if distance <= best_distance:
                    best_distance = distance
                    best_sample = sample
        if best_sample is None:
            self._hide_hover()
            return
        self.hover_label.setText(self._format_hover(best_sample))
        self._position_hover_label(viewport_x, viewport_y)
        self.hover_label.setVisible(True)

    def _hide_hover(self) -> None:
        self.hover_label.setVisible(False)
        self.hover_label.clear()

    def _any_channel_enabled(self) -> bool:
        return any(check.isChecked() for check in self.channel_checks.values())

    @staticmethod
    def _format_hover(sample) -> str:
        """``T+... s`` 加该帧实际存在的全部通道和值。"""
        fields = "  ".join(
            f"CH{index + 1}={value}" for index, value in enumerate(sample.values)
        )
        return f"{format_relative_seconds(sample.relative_seconds)}  {fields}"

    def _position_hover_label(self, viewport_x: float, viewport_y: float) -> None:
        self.hover_label.resize(self.hover_label.sizeHint())
        size = self.hover_label.size()
        viewport = self.chart_view.viewport()
        margin = 12
        left = int(viewport_x) + margin
        top = int(viewport_y) + margin
        if left + size.width() > viewport.width():
            left = max(0, int(viewport_x) - margin - size.width())
        if top + size.height() > viewport.height():
            top = max(0, int(viewport_y) - margin - size.height())
        self.hover_label.move(left, top)

    def _viewport_chart_position(self, viewport_x: float, viewport_y: float):
        """QChartView viewport 坐标 → QChart 图元坐标；布局未就绪时返回 None。"""
        area = self.chart.plotArea()
        if area.width() <= 0.0 or area.height() <= 0.0:
            return None
        point = QPoint(int(round(viewport_x)), int(round(viewport_y)))
        return self.chart.mapFromScene(self.chart_view.mapToScene(point))

    def _viewport_value(self, viewport_x: float, viewport_y: float):
        """viewport 坐标处的图表值坐标；无法映射时返回 None。"""
        chart_position = self._viewport_chart_position(viewport_x, viewport_y)
        if chart_position is None or self.ch1_series is None:
            return None
        return self.chart.mapToValue(chart_position, self.ch1_series)

    def _schedule_chart_rerender(self) -> None:
        if self._chart_rerender_pending:
            return
        self._chart_rerender_pending = True
        QTimer.singleShot(0, self._finish_chart_rerender)

    def _finish_chart_rerender(self) -> None:
        self._chart_rerender_pending = False
        self._render_chart_guarded()
