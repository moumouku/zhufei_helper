"""TDD Red 子任务 E：入口 main.py / 打包契约测试（仅测试，无生产实现）。

固定交付契约（来自任务书；main.py / PaimonAssistant.spec / requirements-dev.txt
由后续 Green 子任务实现，本文件禁止修改以迎合实现）：

1. 根 ``main.py`` 可作为模块导入（无包结构，用 importlib 按文件加载），
   公开 ``main(argv=None) -> int``（argv 形如 sys.argv，argv[0] 为程序名）：
   - 复用已有 QApplication 实例（``QApplication.instance() or QApplication(argv)``），
     保证 pytest 进程内可重复调用；
   - 设置 applicationName == "派蒙助手"、organizationName == "PaimonAssistant"；
   - 创建 ``paimon_assistant.main_window.MainWindow`` 并 ``show()``；
   - ``main(["main.py", "--smoke-test"])`` 立即返回 0（不阻塞）；
   - 普通模式 ``main(["main.py"])`` 进入 Qt 事件循环后返回 0。
   （事件循环用 monkeypatch 替换 ``QApplication.exec`` 防止阻塞，
    窗口创建/显示用真实 offscreen MainWindow 验证，避免脆弱内部 mock。）

2. subprocess 冒烟：``QT_QPA_PLATFORM=offscreen`` 下执行
   ``.venv/Scripts/python.exe main.py --smoke-test``，60s 内退出码 0 且
   stderr/stdout 无 "Traceback"（即不崩溃、事件循环自行结束）。

3. 根 ``PaimonAssistant.spec``：存在；静态含 "main.py"、
   ``name='PaimonAssistant'``、``console=False``（或等价 ``windowed=True``）；
   且为单文件 EXE——含 ``EXE(`` 且不含 ``COLLECT``。

4. ``requirements.txt``：依赖名集合恰为 {pyserial, PySide6, PyInstaller}
   （只保留本期运行/打包实际依赖），不含 numpy / pyqtgraph。

5. ``requirements-dev.txt``：存在；以 ``-r requirements.txt`` 引用主依赖；
   含 pytest 与 pytest-qt。

运行：``.venv/Scripts/python.exe -m pytest tests/test_entrypoint_packaging.py -q``
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

APP_TITLE = "派蒙助手"
ORG_NAME = "PaimonAssistant"

SMOKE_TIMEOUT = 60  # 秒：PySide6 冷启动 + 冒烟退出


# ---------------------------------------------------------------- helpers


@pytest.fixture(scope="session")
def qapp():
    """保证进程内恰有一个 QApplication；main() 必须复用已有实例。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(["pytest"])
    return app


def _load_main_module():
    """按文件加载根 main.py（根目录非包，用 importlib 而非 import）。"""
    main_path = ROOT / "main.py"
    assert main_path.is_file(), f"缺少根目录入口 main.py：{main_path}"
    spec = importlib.util.spec_from_file_location("paimon_root_main", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))  # 让 main.py 的绝对导入（paimon_assistant）可解析
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(ROOT))
        except ValueError:
            pass
    return module


def _visible_main_windows():
    """当前可见的顶层 QMainWindow（id -> widget）。"""
    return {
        id(w): w
        for w in QApplication.topLevelWidgets()
        if isinstance(w, QMainWindow) and w.isVisible()
    }


def _created_main_windows(before):
    """main() 调用后新增的可见顶层 QMainWindow。"""
    after = _visible_main_windows()
    return [w for key, w in after.items() if key not in before]


def _fake_exec(exec_calls):
    """替换 QApplication.exec：记录调用并立即返回 0，防止测试阻塞。"""
    def _exec(self):  # noqa: ANN001 - 签名与 QApplication.exec 一致
        exec_calls.append(1)
        return 0

    return _exec


def _req_names(path: Path):
    """解析 requirements 文件中的依赖名（跳过注释、空行与 -r/-e 等旗标行）。"""
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if match:
            names.append(match.group(1).lower())
    return names


# ------------------------------------------------- 1) 根 main.py 模块契约


def test_main_module_smoke_contract(qapp, monkeypatch):
    """main(['main.py', '--smoke-test']) -> 0；应用名/组织名与窗口创建显示正确。"""
    before = _visible_main_windows()
    exec_calls = []
    monkeypatch.setattr(QApplication, "exec", _fake_exec(exec_calls))

    main_module = _load_main_module()

    # 契约：main(argv=None) -> int
    sig = inspect.signature(main_module.main)
    assert list(sig.parameters) == ["argv"], "main 仅接受 argv 参数"
    assert sig.parameters["argv"].default is None, "argv 默认值必须为 None"

    result = main_module.main(["main.py", "--smoke-test"])
    assert isinstance(result, int), "main 必须返回 int"
    assert result == 0, f"--smoke-test 应返回 0，实际 {result!r}"

    # 应用名 / 组织名
    app = QApplication.instance()
    assert app is not None
    assert app.applicationName() == APP_TITLE, (
        f"applicationName 应为 {APP_TITLE!r}，实际 {app.applicationName()!r}"
    )
    assert app.organizationName() == ORG_NAME, (
        f"organizationName 应为 {ORG_NAME!r}，实际 {app.organizationName()!r}"
    )

    # 窗口创建并显示
    created = _created_main_windows(before)
    assert created, "main() 未创建可见的主窗口"
    assert any(w.windowTitle() == APP_TITLE for w in created), (
        "创建的窗口标题不是派蒙助手"
    )

    for w in created:
        w.close()


def test_main_normal_mode_enters_event_loop(qapp, monkeypatch):
    """普通模式 main(['main.py'])：进入 Qt 事件循环（exec 被调用）后返回 0。"""
    before = _visible_main_windows()
    exec_calls = []
    monkeypatch.setattr(QApplication, "exec", _fake_exec(exec_calls))

    main_module = _load_main_module()
    result = main_module.main(["main.py"])
    assert result == 0
    assert exec_calls, "普通模式应进入 Qt 事件循环（调用 app.exec()）"

    created = _created_main_windows(before)
    assert any(w.windowTitle() == APP_TITLE for w in created), "普通模式未显示主窗口"
    for w in created:
        w.close()


# ------------------------------------------- 2) subprocess 冒烟（无 traceback）


def test_subprocess_smoke_exits_zero_without_traceback():
    """offscreen 下 .venv/Scripts/python.exe main.py --smoke-test 60s 内退出 0。"""
    assert PYTHON.is_file(), f"缺少虚拟环境解释器：{PYTHON}"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        proc = subprocess.run(
            [str(PYTHON), "main.py", "--smoke-test"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"main.py --smoke-test 在 {SMOKE_TIMEOUT}s 内未退出（事件循环未结束）")

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"退出码 {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "Traceback" not in combined, f"stderr 出现 traceback：\n{proc.stderr}"


# ----------------------------------------------------- 3) PyInstaller spec


def test_spec_file_single_file_windowed_exe():
    """PaimonAssistant.spec：main.py + name='PaimonAssistant' + windowed 单文件 EXE。"""
    spec = ROOT / "PaimonAssistant.spec"
    assert spec.is_file(), f"缺少根目录 PaimonAssistant.spec：{spec}"
    text = spec.read_text(encoding="utf-8")

    assert "main.py" in text, "spec 未引用入口 main.py"
    assert re.search(r"name\s*=\s*['\"]PaimonAssistant['\"]", text), (
        "spec 缺少 name='PaimonAssistant'"
    )
    assert re.search(r"console\s*=\s*False", text) or re.search(
        r"windowed\s*=\s*True", text
    ), "spec 必须为窗口程序（console=False 或 windowed=True）"

    assert re.search(r"\bEXE\s*\(", text), "spec 缺少 EXE(...) 目标"
    assert "COLLECT" not in text, "spec 含 COLLECT —— 不是单文件 EXE"


# ----------------------------------------------------- 4) requirements.txt


def test_requirements_only_runtime_and_packaging_deps():
    """requirements.txt 只保留 pyserial / PySide6 / PyInstaller。"""
    req = ROOT / "requirements.txt"
    assert req.is_file(), f"缺少根目录 requirements.txt：{req}"

    names = _req_names(req)
    assert names == ["pyserial", "pyside6", "pyinstaller"], (
        f"依赖名集合应为 {{pyserial, PySide6, PyInstaller}}，实际 {sorted(set(names))}"
    )
    assert "numpy" not in names, "requirements.txt 不得包含 numpy（本期未用）"
    assert "pyqtgraph" not in names, "requirements.txt 不得包含 pyqtgraph（本期未用）"


# -------------------------------------------------- 5) requirements-dev.txt


def test_requirements_dev_references_main_and_tools():
    """requirements-dev.txt：引用 requirements.txt 且含 pytest / pytest-qt。"""
    dev = ROOT / "requirements-dev.txt"
    assert dev.is_file(), f"缺少根目录 requirements-dev.txt：{dev}"
    text = dev.read_text(encoding="utf-8")

    assert re.search(r"-r\s+requirements\.txt", text), (
        "requirements-dev.txt 必须以 -r requirements.txt 引用主依赖"
    )

    names = set(_req_names(dev))
    assert "pytest" in names, "requirements-dev.txt 缺少 pytest"
    assert "pytest-qt" in names, "requirements-dev.txt 缺少 pytest-qt"
