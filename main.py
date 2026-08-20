"""派蒙助手应用入口（TDD Green 子任务 E）。

公共契约由 ``tests/test_entrypoint_packaging.py`` 固定：

* ``main(argv=None) -> int`` — argv 形如 sys.argv（argv[0] 为程序名）。
* 复用已有 QApplication（``QApplication.instance() or QApplication(argv)``），
  保证 pytest 进程内可重复调用。
* 设置 applicationName == "派蒙助手"、organizationName == "PaimonAssistant"。
* 创建 ``paimon_assistant.main_window.MainWindow`` 并 show()。
* ``--smoke-test``：处理待决事件后立即返回 0（不进入事件循环）。
* 普通模式：进入 Qt 事件循环并返回其退出码。
* ``if __name__ == '__main__': raise SystemExit(main())``。
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from paimon_assistant.main_window import MainWindow

APP_TITLE = "派蒙助手"
ORG_NAME = "PaimonAssistant"

# 模块级引用保活：--smoke-test 返回后窗口对象不被回收，
# 供进程内测试检查可见窗口；进程退出时自然清理。
_window: MainWindow | None = None


def main(argv: list[str] | None = None) -> int:
    """应用入口：复用/创建 QApplication，显示主窗口并进入事件循环。

    普通模式返回 ``app.exec()`` 的退出码；``--smoke-test`` 处理完待决
    事件后立即返回 0，供 CI / 冒烟测试使用。
    """
    global _window

    args = list(sys.argv if argv is None else argv)

    app = QApplication.instance() or QApplication(args)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName(ORG_NAME)

    _window = MainWindow()
    _window.show()

    if "--smoke-test" in args:
        app.processEvents()
        return 0
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
