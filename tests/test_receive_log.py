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
