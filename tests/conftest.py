"""REQ-0003 测试会话公共设施。

本文件只做两件事，都不涉及生产代码改动：

1. 会话级 autouse fixture：把 ``LOCALAPPDATA`` 重定向到临时目录，使任何测试
   （含 subprocess 冒烟测试，它继承 ``os.environ``）都不可能读写真实用户目录
   ``%LOCALAPPDATA%\\PaimonAssistant\\logs\\``（REQ §14.5、§16.4）。
2. 会话级护栏：会话前后对真实用户日志目录做**只读**快照，发现变化即报错，
   防止未来再次出现"测试写真实用户目录"。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

import pytest

#: 导入时（尚未被任何 monkeypatch 重定向）解析出的真实用户日志目录。
#: 护栏只用它做只读快照，自身绝不创建/删除/修改该目录。
_REAL_LOG_DIR = Path(
    os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
) / "PaimonAssistant" / "logs"

#: (目录是否存在, 目录 mtime_ns, ((条目名, 大小, mtime_ns), ...))
_Snapshot = Tuple[bool, Optional[int], Tuple[Tuple[str, int, int], ...]]


def _snapshot(log_dir: Path) -> _Snapshot:
    """只读快照：目录是否存在、目录 mtime_ns、各条目名字/大小/mtime_ns。"""
    if not log_dir.is_dir():
        return (False, None, ())
    stat = log_dir.stat()
    entries = tuple(
        (child.name, child.stat().st_size, child.stat().st_mtime_ns)
        for child in sorted(log_dir.iterdir())
    )
    return (True, stat.st_mtime_ns, entries)


@pytest.fixture(scope="session", autouse=True)
def isolated_user_log_dir(tmp_path_factory):
    """把日志用户目录重定向到临时目录，并守护真实目录未被改动。"""
    before = _snapshot(_REAL_LOG_DIR)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("local_app_data")))
    yield
    monkeypatch.undo()
    after = _snapshot(_REAL_LOG_DIR)
    if before != after:
        pytest.exit(
            "测试改动了真实用户日志目录（违反 REQ-0003 §14.5 隔离要求）：\n"
            f"  目录：{_REAL_LOG_DIR}\n"
            f"  会话前：{before!r}\n"
            f"  会话后：{after!r}",
            returncode=1,
        )
