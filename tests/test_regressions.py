"""TDD Red 回归子任务 D：跨功能回归测试（仅测试，禁止生产实现）。

只允许创建/修改本文件；禁止改动任何生产模块与其他测试文件。

覆盖两条跨功能回归（REQ-0003 事件化接收链路）：

1) SerialController 旧 reader 隔离
   自定义序列 factory：第一连接的 serial.read() 永久阻塞，且 close()/
   cancel_read() 均无法解除阻塞；设置 controller._JOIN_TIMEOUT=0.01 后
   close() 必须限时返回并立刻 reopen 第二个 serial；第二连接必须能正常
   产出一个完整 ReceivedEvent。随后释放第一连接让它返回一整帧：旧 reader
   一旦把该帧投进 received_queue 即判定失败（新契约下断言不泄漏任何
   ReceivedEvent，而不是裸字节）。测试清理必须让全部 reader 线程终止。

2) 跨读取块的半个 UTF-8 字符
   旧版从 MainWindow 侧直接向 received_queue 投裸字节，验证界面 decoder
   在 HEX/文本切换间保留 pending。新架构下界面只消费完整 ReceivedEvent，
   半字符不可能到达渲染层（renderer 永远拿到完整 payload），因此等价契约
   下沉到控制器层：半个 UTF-8 字符留在分帧器尾部，不产生事件、不泄漏裸
   字节；补齐 `\r\n` 后产生恰好一个完整事件。

运行：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_regressions.py -q
"""

import queue
import sys
import threading
import time
from pathlib import Path

# Make the project root importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paimon_assistant.receive_framer import ReceivedEvent  # noqa: E402
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
    在 gate 上。``parks`` 记录进入阻塞读的次数，测试据此确认上一块已被
    分帧器处理完（reader 只有处理完才会进入下一次 read）。
    """

    def __init__(self, port, script=(), block=False, **kwargs):
        self.port = port
        self.script = list(script)
        self.block = block
        self.closed = False
        self.blocked = threading.Event()  # 当前 read 是否正阻塞在 gate 上
        self._gate = threading.Event()
        self.reads = 0
        self.parks = 0

    def read(self, n=1):
        if self.block:
            self.parks += 1
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


def drain_events(q, quiet=0.5, total=3.0):
    """取空事件队列，直到连续 ``quiet`` 秒无新事件（有界，绝不无限等待）。"""
    deadline = time.monotonic() + total
    events = []
    while time.monotonic() < deadline:
        try:
            item = q.get(timeout=max(0.0, min(quiet, deadline - time.monotonic())))
        except queue.Empty:
            return events
        events.append(item)
    return events


def wait_until(predicate, timeout=2.0):
    """有界等待 predicate 成真；返回最终值。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


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
    # 返回一整帧，再次 read 抛异常让旧 reader 有机会真正退出（清理用）。
    serial_a = GatedSerial(
        port="A", script=[b"old\r\n", OSError("device removed")], block=True
    )
    serial_b = GatedSerial(port="B", script=[b"new\r\n"], block=False)
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

        # 立刻 reopen 第二个 serial：第二连接必须能正常产出事件
        ctrl.open(make_settings("B"))
        thread_b = ctrl._reader
        got = drain_events(ctrl.received_queue, quiet=0.2, total=2.0)
        assert [(e.payload, e.raw_frame) for e in got] == [(b"new", b"new\r\n")], (
            f"第二连接应产出一个 payload=b'new' 的事件，实际 {got!r}"
        )

        # 释放第一连接：旧 reader 一旦把旧帧投进队列即失败
        serial_a.release()
        leaked = drain_events(ctrl.received_queue, quiet=0.5, total=2.0)
        assert leaked == [], f"旧 reader 泄漏了 {leaked!r} 进 received_queue"
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
# 回归 2：跨读取块的半个 UTF-8 字符（事件契约下沉到控制器）
# ---------------------------------------------------------------------------


def test_partial_utf8_frame_waits_for_terminator_without_leaking_bytes():
    """半个 UTF-8 字符不产生事件；补齐 `\\r\\n` 后产生一个完整事件。"""
    utf8 = "中".encode("utf-8")  # b"\xe4\xb8\xad"
    assert len(utf8) == 3
    serial = GatedSerial(
        port="A", script=[utf8[:2], utf8[2:] + b"\r\n"], block=True
    )
    factory = SequentialFactory([serial])
    ctrl = SerialController(serial_factory=factory, port_lister=lambda: [])
    try:
        ctrl.open(make_settings("A"))
        assert serial.blocked.wait(1.0), "reader 从未阻塞在 read() 上"

        serial.release()  # 前 2 字节：半个字符
        assert wait_until(lambda: serial.parks >= 2), "前半字符未被分帧器处理完"
        assert ctrl.received_queue.empty(), "截断序列不应产生事件"

        serial.release()  # 最后 1 字节 + 结束符：一个完整事件
        assert wait_until(lambda: serial.parks >= 3), "后半字符未被分帧器处理"
        event = ctrl.received_queue.get(timeout=2.0)
        assert isinstance(event, ReceivedEvent)
        assert event.payload == utf8
        assert event.raw_frame == utf8 + b"\r\n"
        assert ctrl.received_queue.empty()
    finally:
        serial.release()
        try:
            ctrl.close()
        except Exception:
            pass
