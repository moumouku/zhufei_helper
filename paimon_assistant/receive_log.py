"""Receive log service: daily UTF-8 ``RX`` log files with retention cleanup.

Contract (docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md):
- §7.1/§7.2 one ``<本地日期>.txt`` file per day under
  ``%LOCALAPPDATA%\\PaimonAssistant\\logs\\``, each event appended as
  ``[HH:mm:ss.SSS] RX <RAW_FRAME_HEX>`` (uppercase, space separated) and
  flushed right away.
- §7.3/§7.4 the first directory/open/write/flush failure is logged with the
  standard library logger, raises ``ReceiveLogError`` and fuses the rest of
  the run; afterwards ``write_event()`` returns ``False`` without touching the
  filesystem, without buffering and without retrying.
- §8 retention: only strictly named ``YYYY-MM-DD.txt`` regular files older
  than ``today - 30d`` are deleted; a failed delete is only logged.

No Qt dependency. Log directory, milliseconds clock and file operations are
injectable so tests never touch the real user directory or the system clock.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, TextIO, Union

if TYPE_CHECKING:  # provided by issue 005; only needed for the type hint
    from .receive_framer import ReceivedEvent

logger = logging.getLogger(__name__)

#: 保留当天及此前 30 个日历日的日志（REQ-0003 §8.5-8.6）。
_RETENTION_DAYS = 30

#: 只有严格命名为 ``YYYY-MM-DD.txt`` 的普通文件才参与保留清理（§8.3）。
_LOG_FILE_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.txt")

#: 默认追加写入 UTF-8 文本日志；测试用注入的 opener 替换。
_open_append = partial(open, mode="a", encoding="utf-8")


def _system_now_ms() -> int:
    """Local wall-clock milliseconds since the Unix epoch."""
    return int(time.time() * 1000)


def _default_log_dir() -> Path:
    """``%LOCALAPPDATA%\\PaimonAssistant\\logs\\`` of the current user."""
    # 非常规环境下 LOCALAPPDATA 缺失时退回家目录，避免落到当前工作目录。
    local_app_data = os.environ.get("LOCALAPPDATA") or str(
        Path.home() / "AppData" / "Local"
    )
    return Path(local_app_data) / "PaimonAssistant" / "logs"


def _default_date_from_ms(ms: int) -> date:
    """本地时区下某个 epoch 毫秒时刻对应的日历日期（§7.1.3）。"""
    return datetime.fromtimestamp(ms // 1000).date()


def _log_file_date(name: str) -> Optional[date]:
    """Date encoded in a strictly named ``YYYY-MM-DD.txt`` file, else ``None``."""
    if _LOG_FILE_NAME.fullmatch(name) is None:
        return None
    try:
        return date.fromisoformat(name[: -len(".txt")])
    except ValueError:
        return None


class ReceiveLogError(Exception):
    """Raised when the receive log directory or log file cannot be written."""


class ReceiveLogService:
    """Append received events to a per-local-date log file."""

    def __init__(
        self,
        log_dir: Optional[Union[str, Path]] = None,
        *,
        now_ms: Optional[Callable[[], int]] = None,
        date_from_ms: Optional[Callable[[int], date]] = None,
        opener: Optional[Callable[[Path], TextIO]] = None,
        remover: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self._log_dir = (
            Path(log_dir) if log_dir else _default_log_dir()
        )  # 空字符串视为未指定，避免退化为当前工作目录
        self._now_ms = now_ms if now_ms is not None else _system_now_ms
        self._date_from_ms = (
            date_from_ms if date_from_ms is not None else _default_date_from_ms
        )
        self._opener = opener if opener is not None else _open_append
        self._remover = remover if remover is not None else os.remove
        self._last_cleanup_date: Optional[date] = None
        self._broken = False

    @property
    def log_dir(self) -> Path:
        """Resolved log directory (the "日志目录" entry opens this path)."""
        return self._log_dir

    def _create_directory(self) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def ensure_directory(self) -> None:
        """Create the log directory (no-op when it already exists)."""
        try:
            self._create_directory()
        except OSError as exc:
            logger.error(
                "Receive log directory creation failed: %s", self._log_dir, exc_info=True
            )
            raise ReceiveLogError(
                f"Receive log directory unavailable: {self._log_dir}"
            ) from exc

    def write_event(self, event: "ReceivedEvent") -> bool:
        """Append one ``RX`` line for ``event``; ``True`` when flushed.

        After the first failure of this run the service is fused: later calls
        return ``False`` without touching the filesystem, buffering the event
        or retrying anything (§7.4.3). 日志写入路径上的任何失败（含非
        ``OSError``）都归一为 ``ReceiveLogError`` 并熔断，避免日志故障穿透
        到串口核心链路（§7.3.8）。
        """
        if self._broken:
            return False
        try:
            seconds, millis = divmod(event.received_at_ms, 1000)
            received = datetime.fromtimestamp(seconds)
            write_date = self._date_from_ms(event.received_at_ms)
            line = (
                f"[{received:%H:%M:%S}.{millis:03d}] "
                f"RX {event.raw_frame.hex(' ').upper()}"
            )
            self._create_directory()
        except Exception as exc:
            raise self._fuse(exc) from exc
        # 保留清理是辅助操作：失败只记日志，不得熔断本运行的日志写入（§8.8）。
        self._cleanup_if_needed(write_date)
        try:
            path = self._log_dir / f"{write_date.isoformat()}.txt"
            with self._opener(path) as handle:
                handle.write(line + "\n")
                handle.flush()
        except Exception as exc:
            raise self._fuse(exc) from exc
        return True

    def _fuse(self, exc: Exception) -> ReceiveLogError:
        """熔断本次运行并返回对外报告的日志故障（§7.4.2）。"""
        self._broken = True
        logger.error(
            "Receive log write failed for %s: %s", self._log_dir, exc, exc_info=True
        )
        return ReceiveLogError(f"Receive log write failed: {exc}")

    def cleanup(self) -> None:
        """§8.1 启动清理：按保留规则删除过期日期日志，不创建目录。

        启动清理是辅助操作：目录不存在视为无文件可清理；清理失败只记日志，
        绝不向调用方抛出，以免影响应用启动。
        """
        try:
            if not self._log_dir.is_dir():
                return
            today = self._date_from_ms(self._now_ms())
            self._cleanup_if_needed(today)
        except Exception as exc:
            logger.exception(
                "Receive log startup cleanup failed for %s: %s", self._log_dir, exc
            )

    def _cleanup_if_needed(self, cleanup_date: date) -> None:
        """Once per local date (startup and after midnight), drop old logs.

        清理是辅助操作：列举、检查或删除失败只记日志，不熔断本运行的日志写入
        （§8.8）；列举失败时不标记该日期已清理，留待下一次重试。
        """
        if cleanup_date == self._last_cleanup_date:
            return
        try:
            entries = list(self._log_dir.iterdir())
        except OSError:
            logger.exception("Receive log retention listing failed: %s", self._log_dir)
            return
        self._last_cleanup_date = cleanup_date
        cutoff = self._date_from_ms(self._now_ms()) - timedelta(days=_RETENTION_DAYS)
        for path in entries:
            log_date = _log_file_date(path.name)
            if log_date is None:  # 非目标文件名（§8.3、§8.7）
                continue
            if log_date >= cutoff:  # 保留当天及此前 30 个日历日（§8.5）
                continue
            try:
                mode = path.lstat().st_mode
            except OSError:
                logger.exception("Receive log retention stat failed: %s", path)
                continue
            if not stat.S_ISREG(mode):  # 子目录、符号链接等非普通文件（§8.3）
                continue
            try:
                self._remover(path)
            except OSError:
                logger.exception("Receive log retention delete failed: %s", path)
