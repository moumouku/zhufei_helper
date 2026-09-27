"""Qt-free chart math for the oscilloscope page (REQ-0005 issue 017).

The page renders the waveform from the session's retained raw samples: this
module only computes *how* to draw them for one view. It never owns, mutates
or caches the raw samples themselves, so every repaint (new data, manual
viewport, resize) can rebuild from the original buffer and restore full detail
when the user zooms back in.

Downsampling keeps, per time bucket, the local minimum and maximum in time
order. High-frequency spikes therefore survive pixel-width reduction, and the
session's gap segmentation is preserved because buckets are computed inside
each existing segment.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .oscilloscope import INT32_MAX, INT32_MIN

#: 无数据或无可见通道时的合法默认视图：非零、非反向且可见。
DEFAULT_X_MIN = 0.0
DEFAULT_X_MAX = 1.0
DEFAULT_Y_MIN = -1.0
DEFAULT_Y_MAX = 1.0

#: 单点/常值时间数据的默认 X 跨度（秒），保证视口不为零宽。
MIN_VISIBLE_X_SPAN = 1.0

#: ``(relative_seconds, value)``；value 始终是原始 int32，不插值。
Point = tuple[float, int]
Segment = list[Point]
Segments = Sequence[Sequence[Point]]


def build_chart_segments(
    segments: Segments, x_min: float, x_max: float, pixel_width: int
) -> list[Segment]:
    """Clip a channel's gap segments to the X view and bucket them per pixel.

    ``segments`` is ``OscilloscopeSession.channel_segments`` shaped data: one
    list per real curve piece, already split at short-frame gaps. Each piece
    keeps its own bucket run, so a gap never gets connected. When the view is
    wide enough (``len(visible) <= pixel_width``) every raw point is passed
    through unchanged, which is how zooming in restores original detail.
    """
    if pixel_width <= 0 or x_max <= x_min:
        return _clip_segments(segments, x_min, x_max)

    bucket_width = (x_max - x_min) / pixel_width
    result: list[Segment] = []
    for segment in segments:
        visible = [point for point in segment if x_min <= point[0] <= x_max]
        if not visible:
            continue
        if len(visible) <= pixel_width:
            result.append(visible)
            continue
        result.append(_bucket_min_max(visible, x_min, bucket_width, pixel_width))
    return result


def _clip_segments(segments: Segments, x_min: float, x_max: float) -> list[Segment]:
    return [
        [point for point in segment if x_min <= point[0] <= x_max]
        for segment in segments
        if any(x_min <= point[0] <= x_max for point in segment)
    ]


def _bucket_min_max(
    visible: list[Point], x_min: float, bucket_width: float, pixel_width: int
) -> Segment:
    """每个像素桶保留 (最早, 最晚) 情形下的最小/最大值，按时间排序。"""
    bucketed: Segment = []
    current_index: int | None = None
    lowest: Point | None = None
    highest: Point | None = None
    for point in visible:
        index = min(int((point[0] - x_min) / bucket_width), pixel_width - 1)
        if index != current_index:
            if current_index is not None:
                bucketed.extend(_ordered_min_max(lowest, highest))
            current_index = index
            lowest = highest = point
            continue
        if point[1] < lowest[1]:  # type: ignore[index]
            lowest = point
        if point[1] > highest[1]:  # type: ignore[index]
            highest = point
    bucketed.extend(_ordered_min_max(lowest, highest))
    return bucketed


def _ordered_min_max(lowest: Point | None, highest: Point | None) -> Segment:
    if lowest is None or highest is None:
        return []
    if lowest is highest:  # 同一桶内的同一个点
        return [lowest]
    if lowest[0] <= highest[0]:
        return [lowest, highest]
    return [highest, lowest]


def fit_x_range(samples: Iterable[object]) -> tuple[float, float]:
    """X range covering every retained sample; never zero-width.

    A single sample (or several identical times) is centered in a
    ``MIN_VISIBLE_X_SPAN`` window, clamped to ``>= 0`` because relative time
    starts at ``T+0``. Empty data yields the legal default view.
    """
    times = [float(getattr(item, "relative_seconds")) for item in samples]
    if not times:
        return DEFAULT_X_MIN, DEFAULT_X_MAX
    low, high = min(times), max(times)
    if high - low >= MIN_VISIBLE_X_SPAN:
        return low, high
    start = (low + high) / 2 - MIN_VISIBLE_X_SPAN / 2
    start = max(DEFAULT_X_MIN, start)
    return start, start + MIN_VISIBLE_X_SPAN


def fit_y_range(
    channel_segments: Iterable[Segments], x_min: float, x_max: float
) -> tuple[float, float]:
    """Y range of the given channels' raw values inside the X view.

    Callers pass only enabled channels, so disabled curves never influence the
    scale. Constant data is widened by ±1; the result never leaves the signed
    32-bit domain, which keeps extreme int32 values visible without pinning
    ordinary signals to the full int32 range.
    """
    values = [
        value
        for segments in channel_segments
        for segment in segments
        for relative_seconds, value in segment
        if x_min <= relative_seconds <= x_max
    ]
    if not values:
        return DEFAULT_Y_MIN, DEFAULT_Y_MAX
    low, high = min(values), max(values)
    if low == high:
        low, high = low - 1, high + 1
    return max(INT32_MIN, low), min(INT32_MAX, high)


def clamp_x_range(
    x_min: float, x_max: float, retained_min: float, retained_max: float
) -> tuple[float, float]:
    """Intersect a requested X view with the retained data window.

    Used by manual zoom/pan: a request that misses the retained window falls
    back to the whole retained window so the view is never empty or inverted.
    """
    low = max(x_min, retained_min)
    high = min(x_max, retained_max)
    if high - low <= 0:
        return retained_min, retained_max
    return low, high


def keep_x_span_in_range(
    x_min: float, x_max: float, retained_min: float, retained_max: float
) -> tuple[float, float]:
    """Shift an existing X view back into the retained window, keeping width.

    Used when 180-second eviction passes the view's left edge: the reading
    position keeps its span and only slides forward to still-retained history.
    """
    low, high = x_min, x_max
    if low < retained_min:
        high += retained_min - low
        low = retained_min
    if high > retained_max:
        low -= high - retained_max
        high = retained_max
    low = max(low, retained_min)
    if high <= low:
        high = min(retained_max, low + MIN_VISIBLE_X_SPAN)
        if high <= low:
            low, high = retained_min, retained_max
    return low, high
