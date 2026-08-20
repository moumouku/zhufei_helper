"""TDD Red 回归子任务 D：跨功能回归测试（仅测试，禁止生产实现）。

只允许创建/修改本文件；禁止改动任何生产模块与其他测试文件。

覆盖两条回归：

1) SerialController 旧 reader 隔离
   自定义序列 factory：第一连接的 serial.read() 永久阻塞，且 close()/
   cancel_read() 均无法解除阻塞；设置 controller._JOIN_TIMEOUT=0.01 后
   close() 必须限时返回并立刻 reopen 第二个 serial；第二连接必须能正常
   产出 b"new"。随后释放第一连接让它返回 b"old"：旧 reader 一旦把
   b"old" 投进 received_queue 即判定失败（当前实现因 reader 使用可变
   self._stop、阻塞 read 返回后未复查而泄漏）。测试清理必须让全部
   reader 线程终止。

2) MainWindow UTF-8 跨块 + 模式切换
   offscreen QApplication/qtbot + 内联 fake controller：先入队 UTF-8
   “中”(E4 B8 AD) 的前 2 字节并 drain，切 HEX，再切回文本，然后入队
   最后 1 字节并 drain：最终 display 必须恰好为“中”。原始历史与
   decoder pending state 必须在模式切换间保留；当前实现每次 _re_render
   都新建解码器并按块补全，会把截断序列渲染成替换字符 U+FFFD。

运行：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_regressions.py -q
"""

import importlib
import os
import queue
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# Make the project root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.serial_controller import (  # noqa: E402
    SerialController,
    SerialSettings,
)


# ---------------------------------------------------------------------------
# 回归 1：SerialController 旧 reader 隔离
# ---------------------------------------------------------------------------


class GatedSerial:
    """pyserial 兼容 fake：read() 可阻塞直到测试 release() 放行。

    close()/（缺失的）cancel_read() 均不能解除阻塞中的 read()。
    script: 每次 read 依次返回的项（bytes 或 Exception 实例，Exception
    直接抛出）；script 耗尽后返回 b""。block=True 时每次 read 都先阻塞
    在 gate 上。
    """

    def __init__(self, port, script=(), block=False, **kwargs):
        self.port = port
        self.script = list(script)
        self.block = block
        self.closed = False
        self.blocked = threading.Event()  # 当前 read 是否正阻塞在 gate 上
        self._gate = threading.Event()
        self.reads = 0

    def read(self, n=1):
        if self.block:
            self.blocked.set()
            self._gate.wait()
            self._gate.clear()
            self.blocked.clear()
        self.reads += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        self.closed = True

    def release(self):
        self._gate.set()


class SequentialFactory:
    """自定义序列 factory：按调用顺序逐个返回预置的 serial 实例。"""

    def __init__(self, serials):
        self.serials = list(serials)
        self.calls = []

    def __call__(self, *args, **kwargs):
        inst = self.serials.pop(0)
        self.calls.append((args, kwargs))
        return inst


def make_settings(port):
    return SerialSettings(
        port=port, baudrate=9600, bytesize=8, parity="N", stopbits=1
    )


def drain_quiet(q, quiet=0.5, total=3.0):
    """取空队列，直到连续 ``quiet`` 秒无新数据（有界，绝不无限等待）。"""
    deadline = time.monotonic() + total
    buf = b""
    while time.monotonic() < deadline:
        try:
            item = q.get(timeout=max(0.0, min(quiet, deadline - time.monotonic())))
        except queue.Empty:
            return buf
        buf += bytes(item)
    return buf


def wait_threads_dead(threads, timeout=2.0):
    """有界等待所有线程终止；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.is_alive() for t in threads):
            return True
        time.sleep(0.01)
    return False


def test_old_reader_is_isolated_after_reopen():
    # 第一连接：read() 永久阻塞，close/cancel_read 均不能解除；释放后先
    # 返回 b"old"，再次 read 抛异常让旧 reader 有机会真正退出（清理用）。
    serial_a = GatedSerial(
        port="A", script=[b"old", OSError("device removed")], block=True
    )
    serial_b = GatedSerial(port="B", script=[b"new"], block=False)
    factory = SequentialFactory([serial_a, serial_b])
    ctrl = SerialController(serial_factory=factory, port_lister=lambda: [])
    ctrl._JOIN_TIMEOUT = 0.01  # close() 不得被卡死的 reader 拖住

    thread_a = None
    thread_b = None
    try:
        ctrl.open(make_settings("A"))
        thread_a = ctrl._reader
        assert serial_a.blocked.wait(1.0), "reader A 从未阻塞在 read() 上"

        ctrl.close()  # 无法解除 serial_a 的阻塞，join 限时返回
        assert not ctrl.is_open
        assert thread_a.is_alive(), "reader A 应仍阻塞在 read() 中"

        # 立刻 reopen 第二个 serial：第二连接必须能正常产出数据
        ctrl.open(make_settings("B"))
        thread_b = ctrl._reader
        got = drain_quiet(ctrl.received_queue, quiet=0.2, total=2.0)
        assert got == b"new", f"第二连接应产出 b'new'，实际 {got!r}"

        # 释放第一连接：旧 reader 一旦把 b"old" 投进队列即失败
        serial_a.release()
        leaked = drain_quiet(ctrl.received_queue, quiet=0.5, total=2.0)
        assert leaked == b"", f"旧 reader 把 {leaked!r} 泄漏进了 received_queue"
    finally:
        serial_a.release()  # 解除阻塞，让旧 reader 走到退出路径
        try:
            ctrl.close()
        except Exception:
            pass
    # 清理后所有 reader 线程必须终止（仅在主体通过时验证，避免掩盖 Red）
    assert wait_threads_dead(
        [t for t in (thread_a, thread_b) if t is not None]
    ), "close()/清理后仍有 reader 线程存活"


# ---------------------------------------------------------------------------
# 回归 2：MainWindow UTF-8 跨块 + 模式切换
# ---------------------------------------------------------------------------


class InlineFakeController:
    """内联 fake controller：MainWindow 所需的最小契约。"""

    def __init__(self):
        self.ports = ["COM3"]
        self.received_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.close_calls = 0
        self.writes = []

    def list_ports(self):
        return list(self.ports)

    def open(self, settings):
        return True

    def close(self):
        self.close_calls += 1

    def write(self, data):
        self.writes.append(bytes(data))


@pytest.fixture
def mw():
    return importlib.import_module("paimon_assistant.main_window")


def _select(combo, text):
    idx = combo.findText(text)
    assert idx != -1, f"组合框缺少选项 {text!r}"
    combo.setCurrentIndex(idx)
    assert combo.currentText() == text


def test_utf8_split_across_mode_switch_preserves_pending(qtbot, mw):
    controller = InlineFakeController()
    window = mw.MainWindow(controller=controller)
    qtbot.addWidget(window)

    data = "中".encode("utf-8")  # b"\xe4\xb8\xad"
    assert len(data) == 3

    # 前 2 字节：跨块第一半，decoder 保留 pending，不输出残缺字符
    controller.received_queue.put(data[:2])
    window._drain_queues()
    assert window.display_edit.toPlainText() == "", "截断序列不应产生输出"

    # 切 HEX：历史按原始字节渲染（原始历史保留）
    _select(window.receive_mode_combo, "HEX")
    assert window.display_edit.toPlainText() == "E4 B8"

    # 切回文本：decoder pending state 必须保留，不得出现 U+FFFD 替换字符
    _select(window.receive_mode_combo, "文本")
    assert window.display_edit.toPlainText() == "", (
        "模式切换丢失了 pending 状态（出现替换字符）"
    )

    # 补最后 1 字节：跨块 + 模式切换后仍能拼出完整字符
    controller.received_queue.put(data[2:])
    window._drain_queues()
    assert window.display_edit.toPlainText() == "中", (
        f"最终显示应为“中”，实际 {window.display_edit.toPlainText()!r}"
    )
