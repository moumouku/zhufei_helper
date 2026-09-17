"""Tests for paimon_assistant.receive_log.ReceiveLogService (no Qt).

Contract (docs/requirements/REQ-0003-serial-receive-event-timestamp-log.md):
- §7.1/§7.2 daily ``<YYYY-MM-DD>.txt`` files in
  ``%LOCALAPPDATA%\\PaimonAssistant\\logs\\``, UTF-8, one line per event:
  ``[HH:mm:ss.SSS] RX <RAW_FRAME_HEX>``.
- §7.3/§7.4 the first directory/open/write/flush failure raises
  ``ReceiveLogError``, is logged with the standard library logger and fuses
  the run; later ``write_event()`` calls return ``False`` without any
  filesystem access.
- §8 retention: delete only strictly named ``YYYY-MM-DD.txt`` regular files
  older than ``today - 30d``; delete failures only get logged.

No Qt, no real user directory and no real system clock: the log directory,
the milliseconds clock and the file operations are injected.
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

from paimon_assistant.receive_log import ReceiveLogError, ReceiveLogService


def local_ms(at: datetime) -> int:
    """Milliseconds since epoch as if ``at`` were a local wall-clock time."""
    return int(at.timestamp() * 1000)


def event_at(at: datetime, raw_frame: bytes, payload: bytes = b"") -> SimpleNamespace:
    """ReceivedEvent stub: the service only reads these three attributes."""
    return SimpleNamespace(
        received_at_ms=local_ms(at), payload=payload, raw_frame=raw_frame
    )


class FakeClock:
    """Injectable ``now_ms`` source; tests move it by assigning ``at``."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> int:
        return local_ms(self.at)


def log_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def dated_file(log_dir: Path, day: date, text: str = "[00:00:00.000] RX 41 0D 0A\n") -> Path:
    """Pre-create a ``YYYY-MM-DD.txt`` file the way earlier runs would."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{day.isoformat()}.txt"
    path.write_text(text, encoding="utf-8")
    return path


class FakeFile:
    """Injectable file object recording the written text and every flush."""

    def __init__(self, flush_error: Optional[Exception] = None) -> None:
        self.text = ""
        self.flushes = 0
        self.flush_error = flush_error

    def write(self, text: str) -> None:
        self.text += text

    def flush(self) -> None:
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error

    def __enter__(self) -> "FakeFile":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class RecordingOpener:
    """Injectable opener: records every path and returns ``file`` or raises."""

    def __init__(
        self, *, file: Optional[FakeFile] = None, error: Optional[Exception] = None
    ) -> None:
        self.calls: List[Path] = []
        self.file = file if file is not None else FakeFile()
        self.error = error

    def __call__(self, path: Path) -> FakeFile:
        self.calls.append(Path(path))
        if self.error is not None:
            raise self.error
        return self.file


class RecordingRemover:
    """Injectable remover: records deletions, fails for the given file names."""

    def __init__(self, *, error_for: Optional[List[str]] = None) -> None:
        self.removed: List[Path] = []
        self.error_for = list(error_for or [])

    def __call__(self, path: Path) -> None:
        if path.name in self.error_for:
            raise PermissionError(f"locked by another process: {path.name}")
        self.removed.append(Path(path))
        os.remove(path)


class TestWriteEventLines:
    def test_first_write_creates_missing_directory_and_date_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"Hello\r\n")
        )

        assert written is True
        assert log_lines(log_dir / "2026-05-17.txt") == [
            "[09:12:03.125] RX 48 65 6C 6C 6F 0D 0A"
        ]

    def test_empty_payload_frame_records_crlf_hex(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"\r\n", payload=b"")
        )

        assert log_lines(log_dir / "2026-05-17.txt") == [
            "[09:12:03.125] RX 0D 0A"
        ]

    def test_events_are_appended_in_arrival_order(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))
        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 400000), b"B\r\n"))

        assert log_lines(log_dir / "2026-05-17.txt") == [
            "[09:12:03.125] RX 41 0D 0A",
            "[09:12:03.400] RX 42 0D 0A",
        ]

    def test_events_after_local_midnight_open_new_date_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        clock = FakeClock(datetime(2026, 5, 17, 23, 59, 59))
        service = ReceiveLogService(log_dir, now_ms=clock)
        service.write_event(
            event_at(datetime(2026, 5, 17, 23, 59, 59, 900000), b"A\r\n")
        )

        clock.at = datetime(2026, 5, 18, 0, 0, 1)
        service.write_event(event_at(datetime(2026, 5, 18, 0, 0, 1, 100000), b"B\r\n"))

        assert log_lines(log_dir / "2026-05-17.txt") == [
            "[23:59:59.900] RX 41 0D 0A"
        ]
        assert log_lines(log_dir / "2026-05-18.txt") == [
            "[00:00:01.100] RX 42 0D 0A"
        ]

    def test_write_flushes_the_written_line(self, tmp_path):
        file = FakeFile()
        service = ReceiveLogService(
            tmp_path / "logs",
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=RecordingOpener(file=file),
        )

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))

        assert file.text == "[09:12:03.125] RX 41 0D 0A\n"
        assert file.flushes == 1


class TestEnsureDirectory:
    def test_creates_missing_directory(self, tmp_path):
        log_dir = tmp_path / "nested" / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.ensure_directory()

        assert log_dir.is_dir()

    def test_failure_raises_receive_log_error_and_is_logged(self, tmp_path, caplog):
        blocked = tmp_path / "logs"
        blocked.write_text("not a directory", encoding="utf-8")
        service = ReceiveLogService(
            blocked, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            with pytest.raises(ReceiveLogError, match="logs"):
                service.ensure_directory()

        assert [record.levelno for record in caplog.records] == [logging.ERROR]


class TestRetention:
    def test_first_write_removes_log_older_than_30_days(self, tmp_path):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        recent = dated_file(log_dir, date(2026, 5, 10))
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))

        assert not stale.exists()
        assert recent.exists()

    def test_keeps_the_30_day_boundary_and_removes_the_31st_day(self, tmp_path):
        log_dir = tmp_path / "logs"
        keep = dated_file(log_dir, date(2026, 4, 17))
        drop = dated_file(log_dir, date(2026, 4, 16))
        future = dated_file(log_dir, date(2026, 6, 1))
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))

        assert keep.exists()
        assert future.exists()
        assert not drop.exists()

    @pytest.mark.parametrize(
        "today, keep_day, drop_day",
        [
            # 跨年：2026-01-05 的截止日为 2025-12-06
            (date(2026, 1, 5), date(2025, 12, 6), date(2025, 12, 5)),
            # 闰年二月：2024-03-01 的截止日为 2024-01-31
            (date(2024, 3, 1), date(2024, 1, 31), date(2024, 1, 30)),
            # 平年二月：2026-03-01 的截止日为 2026-01-30
            (date(2026, 3, 1), date(2026, 1, 30), date(2026, 1, 29)),
        ],
    )
    def test_retention_boundary_across_month_year_and_leap_year(
        self, tmp_path, today, keep_day, drop_day
    ):
        log_dir = tmp_path / "logs"
        boundary = dated_file(log_dir, keep_day)
        stale = dated_file(log_dir, drop_day)
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(today.year, today.month, today.day, 12, 0, 0)),
        )

        service.cleanup()

        assert boundary.exists()  # 恰为 today - 30 天：保留
        assert not stale.exists()  # 第 31 天前：删除

    def test_ignores_non_target_entries_and_never_uses_mtime(self, tmp_path):
        log_dir = tmp_path / "logs"
        old_name = dated_file(log_dir, date(2026, 4, 1))
        notes = log_dir / "notes.txt"
        notes.write_text("keep me", encoding="utf-8")
        compact = log_dir / "20260401.txt"
        compact.write_text("keep me", encoding="utf-8")
        loose = log_dir / "2026-1-1.txt"
        loose.write_text("keep me", encoding="utf-8")
        subdir = log_dir / "2020-01-01.txt"
        subdir.mkdir()
        recent_mtime = datetime(2026, 5, 17, 9, 0).timestamp()
        ancient_mtime = datetime(2020, 1, 1).timestamp()
        os.utime(old_name, (recent_mtime, recent_mtime))
        recent_name = dated_file(log_dir, date(2026, 5, 10))
        os.utime(recent_name, (ancient_mtime, ancient_mtime))
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))

        assert notes.exists()
        assert compact.exists()
        assert loose.exists()
        assert subdir.is_dir()
        assert recent_name.exists()
        assert not old_name.exists()

    def test_retention_runs_again_only_when_the_local_date_advances(self, tmp_path):
        log_dir = tmp_path / "logs"
        clock = FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        service = ReceiveLogService(log_dir, now_ms=clock)
        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3), b"A\r\n"))
        dropped_in_later = dated_file(log_dir, date(2026, 4, 1))

        service.write_event(event_at(datetime(2026, 5, 17, 10, 0, 0), b"B\r\n"))

        assert dropped_in_later.exists()

        clock.at = datetime(2026, 5, 18, 0, 0, 1)
        service.write_event(event_at(datetime(2026, 5, 18, 0, 0, 1), b"C\r\n"))

        assert not dropped_in_later.exists()
        assert log_lines(log_dir / "2026-05-18.txt") == [
            "[00:00:01.000] RX 43 0D 0A"
        ]

    def test_delete_failure_is_logged_and_spares_the_rest(self, tmp_path, caplog):
        log_dir = tmp_path / "logs"
        locked = dated_file(log_dir, date(2026, 4, 1))
        removable = dated_file(log_dir, date(2026, 4, 2))
        remover = RecordingRemover(error_for=["2026-04-01.txt"])
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            remover=remover,
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            written = service.write_event(
                event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
            )

        assert written is True
        assert locked.exists()
        assert not removable.exists()
        assert remover.removed == [removable]
        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert "2026-04-01.txt" in caplog.text


class TestStartupCleanup:
    def test_cleanup_removes_expired_logs_without_any_event_or_write(self, tmp_path):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        recent = dated_file(log_dir, date(2026, 5, 10))
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.cleanup()

        assert not stale.exists()
        assert recent.exists()
        # 启动清理本身不创建/不写入当天日志文件
        assert not (log_dir / "2026-05-17.txt").exists()

    def test_cleanup_skips_missing_directory_without_creating_it(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.cleanup()

        assert not log_dir.exists()

    def test_cleanup_spares_non_target_entries_subdirectories_and_boundary(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 16))
        boundary = dated_file(log_dir, date(2026, 4, 17))
        notes = log_dir / "notes.txt"
        notes.write_text("keep", encoding="utf-8")
        compact = log_dir / "20260416.txt"
        compact.write_text("keep", encoding="utf-8")
        subdir = log_dir / "2020-01-01.txt"
        subdir.mkdir()
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.cleanup()

        assert not stale.exists()
        assert boundary.exists()
        assert notes.exists()
        assert compact.exists()
        assert subdir.is_dir()

    def test_startup_cleanup_is_not_repeated_on_the_same_day_first_write(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        clock = FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        service = ReceiveLogService(log_dir, now_ms=clock)

        service.cleanup()
        created_after_cleanup = dated_file(log_dir, date(2026, 4, 1))

        service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n"))

        assert created_after_cleanup.exists()  # 同一天不重复全目录扫描

        clock.at = datetime(2026, 5, 18, 0, 0, 1)
        service.write_event(event_at(datetime(2026, 5, 18, 0, 0, 1), b"B\r\n"))

        assert not created_after_cleanup.exists()  # 跨日后首次写入前再清理（§8.2）
        assert log_lines(log_dir / "2026-05-18.txt") == [
            "[00:00:01.000] RX 42 0D 0A"
        ]

    def test_cleanup_listing_failure_is_logged_and_does_not_raise(
        self, tmp_path, monkeypatch, caplog
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        def _raise_permission_error(self):
            raise PermissionError("日志目录无法列举")

        with monkeypatch.context() as mp:
            mp.setattr(Path, "iterdir", _raise_permission_error)
            with caplog.at_level(
                logging.ERROR, logger="paimon_assistant.receive_log"
            ):
                service.cleanup()  # 启动辅助操作：不得向调用方抛出

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert "日志目录无法列举" in caplog.text


class TestDateConversionInjection:
    def test_injected_date_conversion_decides_the_log_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            date_from_ms=lambda ms: date(2032, 1, 2),
        )

        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
        )

        assert written is True
        assert log_lines(log_dir / "2032-01-02.txt") == [
            "[09:12:03.125] RX 41 0D 0A"
        ]
        assert not (log_dir / "2026-05-17.txt").exists()


class TestFailureIsolation:
    def test_first_write_failure_raises_and_logs_the_original_error(
        self, tmp_path, caplog
    ):
        service = ReceiveLogService(
            tmp_path / "logs",
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=RecordingOpener(error=PermissionError("disk is full")),
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            with pytest.raises(ReceiveLogError, match="disk is full"):
                service.write_event(
                    event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
                )

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert "disk is full" in caplog.text

    def test_directory_creation_failure_raises_receive_log_error(
        self, tmp_path, caplog
    ):
        blocked = tmp_path / "logs"
        blocked.write_text("not a directory", encoding="utf-8")
        service = ReceiveLogService(
            blocked, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            with pytest.raises(ReceiveLogError):
                service.write_event(
                    event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
                )

        assert [record.levelno for record in caplog.records] == [logging.ERROR]

    def test_flush_failure_raises_receive_log_error(self, tmp_path):
        service = ReceiveLogService(
            tmp_path / "logs",
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=RecordingOpener(file=FakeFile(flush_error=OSError("cannot flush"))),
        )

        with pytest.raises(ReceiveLogError, match="cannot flush"):
            service.write_event(
                event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
            )

    def test_non_oserror_write_failure_is_normalized_and_fuses(
        self, tmp_path, caplog
    ):
        log_dir = tmp_path / "logs"
        opener = RecordingOpener(error=RuntimeError("日志句柄异常"))
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=opener,
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            with pytest.raises(ReceiveLogError, match="日志句柄异常"):
                service.write_event(
                    event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
                )

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert "日志句柄异常" in caplog.text
        assert opener.calls == [log_dir / "2026-05-17.txt"]

        # 熔断：第二次直接返回 False，不再访问文件系统
        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 4, 0), b"B\r\n")
        )
        assert written is False
        assert opener.calls == [log_dir / "2026-05-17.txt"]

    def test_injected_time_conversion_failure_is_normalized_and_fuses(
        self, tmp_path, caplog
    ):
        def _broken_conversion(ms):
            raise ValueError("时间转换失败")

        opener = RecordingOpener()
        service = ReceiveLogService(
            tmp_path / "logs",
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            date_from_ms=_broken_conversion,
            opener=opener,
        )

        with caplog.at_level(logging.ERROR, logger="paimon_assistant.receive_log"):
            with pytest.raises(ReceiveLogError, match="时间转换失败"):
                service.write_event(
                    event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
                )

        assert [record.levelno for record in caplog.records] == [logging.ERROR]
        assert "时间转换失败" in caplog.text
        assert opener.calls == []  # 转换失败：熔断前未访问文件系统

        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 4, 0), b"B\r\n")
        )
        assert written is False
        assert opener.calls == []

    def test_circuit_breaker_returns_false_and_stops_touching_the_filesystem(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        opener = RecordingOpener(error=PermissionError("disk is full"))
        remover = RecordingRemover()
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=opener,
            remover=remover,
        )
        with pytest.raises(ReceiveLogError):
            service.write_event(
                event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
            )
        opened_before = list(opener.calls)
        removed_before = list(remover.removed)
        dropped_in_later = dated_file(log_dir, date(2026, 4, 2))

        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 4, 0), b"B\r\n")
        )

        assert not stale.exists()
        assert written is False
        assert opener.calls == opened_before
        assert remover.removed == removed_before
        assert dropped_in_later.exists()


class TestCleanupFailureIsolation:
    """审查 W1：清理阶段失败不得熔断本运行的日志写入（§7.4.2 对比 §8.8）。"""

    def test_retention_listing_failure_during_write_does_not_fuse_logging(
        self, tmp_path, monkeypatch, caplog
    ):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
        )
        real_iterdir = Path.iterdir
        state = {"fail": True}

        def flaky_iterdir(self):
            if self == log_dir and state["fail"]:
                raise PermissionError("directory listing denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

        with caplog.at_level(logging.ERROR):
            written = service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3), b"A\r\n"))

        assert written is True  # 清理列举失败不得熔断正常写入
        assert log_lines(log_dir / "2026-05-17.txt") == ["[09:12:03.000] RX 41 0D 0A"]
        assert any("listing" in record.message.lower() for record in caplog.records)

    def test_retention_is_retried_after_a_listing_failure(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )
        real_iterdir = Path.iterdir
        state = {"fail": True}

        def flaky_iterdir(self):
            if self == log_dir and state["fail"]:
                raise PermissionError("directory listing denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

        service.cleanup()  # 失败：不得标记当天已完成清理（§8.8 留待下一次清理）
        state["fail"] = False
        service.cleanup()

        assert not stale.exists()

    def test_cleanup_listing_failure_at_startup_does_not_fuse_writes(
        self, tmp_path, monkeypatch
    ):
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        opener = RecordingOpener()
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            opener=opener,
        )
        state = {"fail": True}
        real_iterdir = Path.iterdir

        def flaky_iterdir(self):
            if self == log_dir and state["fail"]:
                raise PermissionError("directory listing denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky_iterdir)

        service.cleanup()
        state["fail"] = False

        assert service.write_event(event_at(datetime(2026, 5, 17, 9, 12, 3), b"A\r\n")) is True
        assert not stale.exists()  # 启动清理失败后，首次写入仍会重新清理

    def test_cleanup_skips_when_the_log_path_is_a_regular_file(self, tmp_path):
        log_path = tmp_path / "logs"
        log_path.write_text("not a directory", encoding="utf-8")
        service = ReceiveLogService(
            log_path, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.cleanup()  # 不抛异常，也不把该文件当目录处理

        assert log_path.read_text(encoding="utf-8") == "not a directory"


class TestRetentionTargets:
    """§8.3 只处理日志目录中名称严格匹配 ``YYYY-MM-DD.txt`` 的普通文件。"""

    def test_retention_skips_symlinked_date_files(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = tmp_path / "target.txt"
        target.write_text("keep", encoding="utf-8")
        link = log_dir / "2026-04-01.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is not permitted on this platform")
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        service.cleanup()

        assert link.is_symlink(), "符号链接不是普通文件，不得作为清理目标"
        assert target.read_text(encoding="utf-8") == "keep"

    def test_retention_skips_non_regular_date_entries_without_symlink_privilege(
        self, tmp_path, monkeypatch
    ):
        """上面那条需要创建链接的特权；这里用可控的 lstat 固定同一分支。"""
        log_dir = tmp_path / "logs"
        stale = dated_file(log_dir, date(2026, 4, 1))
        remover = RecordingRemover()
        service = ReceiveLogService(
            log_dir,
            now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)),
            remover=remover,
        )
        real_lstat = Path.lstat
        link_mode = stat.S_IFLNK | 0o777

        def fake_lstat(self):
            if self == stale:
                return os.stat_result((link_mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", fake_lstat)

        service.cleanup()

        assert remover.removed == []
        assert stale.exists()


class TestEmptyLogDirArgument:
    def test_empty_string_falls_back_to_the_default_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        service = ReceiveLogService(
            "", now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3))
        )

        assert service.log_dir == tmp_path / "local" / "PaimonAssistant" / "logs"


class TestMidnightAndSubSecondBoundaries:
    def test_late_event_for_the_previous_day_does_not_consume_the_new_day_cleanup(
        self, tmp_path
    ):
        log_dir = tmp_path / "logs"
        clock = FakeClock(datetime(2026, 5, 17, 23, 59, 30))
        service = ReceiveLogService(log_dir, now_ms=clock)
        service.cleanup()  # 标记 2026-05-17 已清理

        clock.at = datetime(2026, 5, 18, 0, 0, 1)
        service.write_event(
            event_at(datetime(2026, 5, 17, 23, 59, 59), b"late\r\n")
        )
        stale = dated_file(log_dir, date(2026, 4, 1))
        service.write_event(event_at(datetime(2026, 5, 18, 0, 0, 2), b"new\r\n"))

        assert log_lines(log_dir / "2026-05-17.txt") == ["[23:59:59.000] RX 6C 61 74 65 0D 0A"]
        assert not stale.exists()

    def test_sub_second_boundary_events_use_their_own_local_date(self, tmp_path):
        log_dir = tmp_path / "logs"
        service = ReceiveLogService(
            log_dir, now_ms=FakeClock(datetime(2026, 5, 18, 12, 0, 0))
        )
        last_second = int(datetime(2026, 5, 17, 23, 59, 59).timestamp() * 1000)

        service.write_event(
            SimpleNamespace(
                received_at_ms=last_second + 999, payload=b"", raw_frame=b"A\r\n"
            )
        )
        service.write_event(
            SimpleNamespace(
                received_at_ms=last_second + 1000, payload=b"", raw_frame=b"B\r\n"
            )
        )

        assert log_lines(log_dir / "2026-05-17.txt") == ["[23:59:59.999] RX 41 0D 0A"]
        assert log_lines(log_dir / "2026-05-18.txt") == ["[00:00:00.000] RX 42 0D 0A"]


class TestDefaultLogDirectory:
    def test_defaults_to_local_app_data_paimon_logs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        service = ReceiveLogService(now_ms=FakeClock(datetime(2026, 5, 17, 9, 12, 3)))

        written = service.write_event(
            event_at(datetime(2026, 5, 17, 9, 12, 3, 125000), b"A\r\n")
        )

        assert written is True
        assert log_lines(tmp_path / "PaimonAssistant" / "logs" / "2026-05-17.txt") == [
            "[09:12:03.125] RX 41 0D 0A"
        ]
