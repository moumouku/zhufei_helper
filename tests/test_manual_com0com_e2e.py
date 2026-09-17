"""真实 com0com 端到端验收脚本（默认跳过，不依赖真实串口即可全绿）。

REQ-0003 §14.7 / REQ-0004 §6.15：真实串口端到端只作为 Windows 手工验收，
不纳入自动化测试依赖。本文件把该手工验收固化为可重复执行的脚本：

* 默认 **跳过**（com0com 端口不存在或未显式启用时）。
* 显式启用：``PAIMON_COM0COM_E2E=1``。
* 端口可用环境变量覆盖：``PAIMON_E2E_WINDOW_PORT`` / ``PAIMON_E2E_PEER_PORT``
  （默认 ``COM17`` / ``COM19``，即本项目开发环境的 com0com 配对口）。

运行：

    QT_QPA_PLATFORM=offscreen PAIMON_COM0COM_E2E=1 \
        .venv/Scripts/python.exe -m pytest tests/test_manual_com0com_e2e.py -q

覆盖范围：
1. 分帧模式：完整帧显示、无结束符不显示、补齐结束符后显示、HEX 视图含 0D 0A。
2. 原始字节模式：无结束符立即显示、不产生 RX 日志、切回分帧模式仍正常。
3. 发送：文本不追加 0D 0A、HEX 显式 0D 0A、发送数据不进入接收通道。
4. 日志：接收事件写入真实日志目录，格式与 REQ-0003 §7.2 一致。
"""

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

serial = pytest.importorskip("serial")

from paimon_assistant.config import SerialSettings  # noqa: E402
from paimon_assistant.main_window import FRAMED_MODE, HEX_MODE, RAW_MODE, MainWindow  # noqa: E402

WINDOW_PORT = os.environ.get("PAIMON_E2E_WINDOW_PORT", "COM17")
PEER_PORT = os.environ.get("PAIMON_E2E_PEER_PORT", "COM19")
ENABLED = os.environ.get("PAIMON_COM0COM_E2E") == "1"


def _port_pair_present() -> bool:
    from serial.tools import list_ports

    names = {p.device for p in list_ports.comports()}
    return {WINDOW_PORT, PEER_PORT} <= names


pytestmark = [
    pytest.mark.skipif(
        not ENABLED,
        reason="真实串口验收默认跳过；设置 PAIMON_COM0COM_E2E=1 启用",
    ),
    pytest.mark.skipif(
        not _port_pair_present(),
        reason=f"未检测到 com0com 端口对 {WINDOW_PORT}/{PEER_PORT}",
    ),
]


def _pump(app, seconds: float) -> None:
    """处理 Qt 事件，让接收队列按 QTimer 节奏被消费。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def pair(qapp, qtbot, tmp_path):
    """打开窗口侧端口与对端端口；日志服务注入临时目录，不污染真实用户目录。"""
    from paimon_assistant.receive_log import ReceiveLogService

    peer = serial.Serial(
        PEER_PORT, 115200, timeout=0.2, bytesize=8, parity="N", stopbits=1
    )
    win = MainWindow(log_service=ReceiveLogService(tmp_path / "logs"))
    qtbot.addWidget(win)
    win.show()
    _pump(qapp, 0.3)

    win.port_combo.setCurrentText(WINDOW_PORT)
    win._on_open_clicked()
    _pump(qapp, 0.5)
    assert win._is_open, f"无法打开 {WINDOW_PORT}"

    yield qapp, win, peer, tmp_path

    win._close_connection()
    peer.close()


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"缺少选项 {text!r}"
    combo.setCurrentIndex(idx)


def _send_and_collect(qapp, peer, win, mode, text, wait=0.6):
    peer.reset_input_buffer()
    _select(win.send_mode_combo, mode)
    win.send_edit.setText(text)
    win._on_send_clicked()
    _pump(qapp, wait)
    return peer.read(64)


def test_framed_mode_displays_only_complete_frames(pair):
    """REQ-0003 §4.2.3 / §6.3：无 0D 0A 不显示，补齐后立即显示。"""
    app, win, peer, _ = pair

    peer.write(b"A\r\n")
    peer.flush()
    _pump(app, 0.6)
    assert "A" in win.display_edit.toPlainText()

    win._on_clear()
    peer.write(b"hello")
    peer.flush()
    _pump(app, 0.6)
    assert win.display_edit.toPlainText() == "", "无结束符的数据不应显示"

    peer.write(b"\r\n")
    peer.flush()
    _pump(app, 0.6)
    assert "hello" in win.display_edit.toPlainText()

    _select(win.receive_mode_combo, HEX_MODE)
    assert "68 65 6C 6C 6F 0D 0A" in win.display_edit.toPlainText()
    _select(win.receive_mode_combo, "文本")


def test_raw_mode_displays_bytes_without_terminator(pair):
    """REQ-0004 §3.2：原始字节模式到达即显示，且不写 RX 日志。"""
    app, win, peer, tmp_path = pair
    _select(win.parse_mode_combo, RAW_MODE)
    assert win.timestamp_checkbox.isEnabled() is False

    peer.write(b"hello")
    peer.flush()
    _pump(app, 0.6)

    assert win.display_edit.toPlainText() == "hello"
    assert list((tmp_path / "logs").glob("*.txt")) == [], "原始字节模式不应写 RX 日志"


def test_raw_mode_recovers_framing_when_switched_back(pair):
    """REQ-0004 §3.3：切回分帧模式后事件链路正常，两种历史互不污染。"""
    app, win, peer, tmp_path = pair
    _select(win.parse_mode_combo, RAW_MODE)
    peer.write(b"raw-bytes")
    peer.flush()
    _pump(app, 0.6)
    assert win.display_edit.toPlainText() == "raw-bytes"

    _select(win.parse_mode_combo, FRAMED_MODE)
    peer.write(b"B\r\n")
    peer.flush()
    _pump(app, 0.6)

    assert "B" in win.display_edit.toPlainText()
    assert "raw-bytes" not in win.display_edit.toPlainText()
    log_files = list((tmp_path / "logs").glob("*.txt"))
    assert len(log_files) == 1
    assert "52 41 57" not in log_files[0].read_text(encoding="utf-8")


def test_send_appends_nothing_and_never_enters_receive_path(pair):
    """REQ-0003 §11 / REQ-0004 §6.14：发送原样转发，不回环、不写日志。"""
    app, win, peer, tmp_path = pair

    assert _send_and_collect(app, peer, win, "文本", "TXT") == b"TXT"
    assert _send_and_collect(app, peer, win, HEX_MODE, "42 0D 0A") == b"B\r\n"

    assert win.display_edit.toPlainText() == ""
    assert list((tmp_path / "logs").glob("*.txt")) == []


def test_received_event_is_written_to_the_log_file(pair):
    """REQ-0003 §7.2：日志行格式 [HH:mm:ss.SSS] RX <RAW_HEX>。"""
    app, win, peer, tmp_path = pair

    peer.write(b"Hi\r\n")
    peer.flush()
    _pump(app, 0.8)

    log_files = list((tmp_path / "logs").glob("*.txt"))
    assert len(log_files) == 1
    line = log_files[0].read_text(encoding="utf-8").strip()
    assert line.endswith("RX 48 69 0D 0A")
    assert line.startswith("[") and "] " in line
