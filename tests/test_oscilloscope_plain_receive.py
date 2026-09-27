"""波形页接收正文显示修订：只显示收到的原始内容。

用户修订覆盖旧的“每行相对时间 + payload="..." + 解析状态”契约：
接收区只显示完整帧的原始载荷文本，相对时间保留在波形轴与悬停读值上；
协议失败提示、连接边界提示都移到接收区之外的非模态标签。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paimon_assistant.oscilloscope import (  # noqa: E402
    OscilloscopeConnectionBoundary,
    OscilloscopeSession,
)
from paimon_assistant.oscilloscope_page import OscilloscopePage  # noqa: E402
from paimon_assistant.serial_controller import SerialSettings  # noqa: E402
from test_oscilloscope_lifecycle import BoundaryEvent, StepClock  # noqa: E402

ORIGIN_NS = 1_000_000_000


def make_page(qtbot) -> tuple[OscilloscopePage, StepClock]:
    clock = StepClock()
    page = OscilloscopePage(OscilloscopeSession(monotonic_ns=clock.mono))
    qtbot.addWidget(page)
    page.begin_acquisition()
    return page, clock


def event_at_ns(relative_ns: int, payload: bytes) -> BoundaryEvent:
    """一条在采集原点后 ``relative_ns`` 纳秒完成边界识别的完整帧事件。"""
    return BoundaryEvent(
        ORIGIN_NS + relative_ns, 1_700_000_000_000, payload, payload + b"\r\n"
    )


def settings(port="COM7", baudrate=115200, data_bits=8, parity="N", stop_bits=1):
    return SerialSettings(
        port=port,
        baudrate=baudrate,
        data_bits=data_bits,
        parity=parity,
        stop_bits=stop_bits,
    )


def test_legal_frame_body_shows_only_exact_received_text(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events([event_at_ns(250_000_000, b"12,34")])

    text = page.display_edit.toPlainText()
    assert text == "12,34"
    assert "T+" not in text
    assert "payload=" not in text
    assert '"' not in text
    assert "解析" not in text


def test_body_keeps_printable_spellings_and_escapes_nonprinting_bytes(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events([event_at_ns(250_000_000, b'1"2\\3\t4')])

    assert page.display_edit.toPlainText() == '1"2\\3\\x094'


def test_invalid_frame_keeps_raw_body_and_puts_protocol_failure_outside(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events([event_at_ns(250_000_000, b"1 2")])

    assert page.display_edit.toPlainText() == "1 2"
    assert page.session.samples == []
    assert len(page.session.records) == 1
    assert page.session.records[0].parse_ok is False
    assert "解析失败" in page.protocol_status_label.text()
    assert page.protocol_status_label.isVisibleTo(page)


def test_valid_frame_clears_only_prior_protocol_failure(qtbot):
    page, _clock = make_page(qtbot)
    page.show_diagnostic("超长帧已丢弃")

    page.consume_events([event_at_ns(0, b"bad")])
    assert "解析失败" in page.protocol_status_label.text()

    page.consume_events([event_at_ns(250_000_000, b"12")])

    assert page.protocol_status_label.text() == ""
    assert not page.protocol_status_label.isVisibleTo(page)
    assert page.diagnostic_label.text() == "超长帧已丢弃"
    assert page.diagnostic_label.isVisibleTo(page)


def test_empty_frame_keeps_its_line_and_following_valid_frame_clears_failure(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [event_at_ns(250_000_000, b""), event_at_ns(500_000_000, b"12")]
    )

    assert [record.payload for record in page.session.records] == [b"", b"12"]
    assert page.display_edit.toPlainText() == "\n12"
    assert page.protocol_status_label.text() == "", (
        "空帧之后的合法帧清除最近的协议失败提示"
    )


def test_empty_frame_as_latest_keeps_protocol_failure_visible(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events([event_at_ns(250_000_000, b"")])

    assert len(page.session.records) == 1
    assert page.session.records[0].payload == b""
    assert page.display_edit.toPlainText() == ""
    assert len(page._display_records) == 1, "空帧仍占据自己的显示记录"
    assert page.display_edit.document().blockCount() == 1
    assert "解析失败" in page.protocol_status_label.text()


def test_connection_boundary_updates_label_not_receive_body(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events([event_at_ns(0, b"12")])

    page.show_connection_boundary(
        settings("COM4", 460800, 7, "E", 2), at_ns=ORIGIN_NS + 4_000_000_000
    )

    record = page.session.records[-1]
    assert isinstance(record, OscilloscopeConnectionBoundary)
    assert record.relative_seconds == pytest.approx(4.0)
    assert record.settings.port == "COM4"
    assert page.display_edit.toPlainText() == "12", "连接边界不得插入接收正文"
    assert [r.payload for r in page._display_records] == [b"12"], (
        "显示记录只包含完整帧，边界不占接收区行"
    )
    label = page.connection_boundary_label.text()
    assert "连接边界" in label
    assert "COM4" in label and "460800" in label and "7E2" in label
    assert "T+" not in label, "连接边界提示不得显示相对时间"
    assert page.connection_boundary_label.isVisibleTo(page)

    page.consume_events([event_at_ns(4_500_000_000, b"13")])

    assert page.display_edit.toPlainText() == "12\n13"
    assert [r.payload for r in page._display_records] == [b"12", b"13"]


def test_boundary_after_invalid_frame_keeps_protocol_failure_visible(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events([event_at_ns(0, b"1 2")])

    page.show_connection_boundary(settings(), at_ns=ORIGIN_NS + 1_000_000_000)

    assert "解析失败" in page.protocol_status_label.text(), (
        "连接边界不是解析帧，不得改变协议状态"
    )
    assert page.display_edit.toPlainText() == "1 2"


def test_decimal_payload_spellings_are_shown_verbatim(qtbot):
    page, _clock = make_page(qtbot)

    page.consume_events(
        [event_at_ns(0, b"0.96,32"), event_at_ns(250_000_000, b"8.00")]
    )

    assert page.display_edit.toPlainText() == "0.96,32\n8.00", (
        "接收正文保持原始文本拼写，不重排或重新格式化数值"
    )


def test_clear_acquisition_clears_boundary_notice(qtbot):
    page, _clock = make_page(qtbot)
    page.show_connection_boundary(settings(), at_ns=ORIGIN_NS + 1_000_000_000)
    assert page.connection_boundary_label.isVisibleTo(page)

    page.clear_acquisition()

    assert page.connection_boundary_label.text() == ""
    assert not page.connection_boundary_label.isVisibleTo(page)
    assert page.display_edit.toPlainText() == ""


def test_boundary_eviction_rerenders_only_frames(qtbot):
    page, _clock = make_page(qtbot)
    page.consume_events([event_at_ns(0, b"1")])
    page.show_connection_boundary(settings(), at_ns=ORIGIN_NS + 1_000_000_000)

    page.consume_events([event_at_ns(240_000_000_000, b"2")])

    assert page.display_edit.toPlainText() == "2", (
        "淘汰重渲后正文只包含窗口内的完整帧"
    )
    assert [r.payload for r in page._display_records] == [b"2"]
    assert "连接边界" in page.connection_boundary_label.text()
