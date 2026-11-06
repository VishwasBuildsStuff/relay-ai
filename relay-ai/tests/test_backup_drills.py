"""Isolated backup/restore and freshness-alert condition rehearsal."""

import os
import time

from backup import check_latest_backup, create_backup, restore_backup, verify_backup
from schemas import PipelineStatus, PipelineTaskResponse
from storage import SQLiteTaskStore


def test_backup_restore_and_stale_archive_signal(tmp_path, monkeypatch):
    database = tmp_path / "live.sqlite3"
    applied = tmp_path / "applied"
    applied.mkdir()
    workspace = applied / "task-copy"
    workspace.mkdir()
    (workspace / "result.txt").write_text("preserved", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    store = SQLiteTaskStore(database)
    now = "2026-11-06T00:00:00+00:00"
    task = PipelineTaskResponse(
        task_id="backup-drill",
        status=PipelineStatus.QUEUED,
        current_stage="QUEUED",
        bug_report="restore this task",
        created_at=now,
        updated_at=now,
    )
    store.create_task(task, {"source.py": "print('source')\n"})
    monkeypatch.setenv("RELAY_DB_PATH", str(database))
    monkeypatch.setenv("RELAY_APPLIED_ROOT", str(applied))

    archive = create_backup(backup_dir)
    assert verify_backup(archive)["sqlite_integrity"] == "ok"
    restored = restore_backup(archive, tmp_path / "isolated-restore")
    restored_store = SQLiteTaskStore(restored / "relay_tasks.sqlite3")
    assert restored_store["backup-drill"].bug_report == "restore this task"
    assert restored_store.get_snapshot("backup-drill") == {"source.py": "print('source')\n"}
    assert (restored / "relay_applied" / "task-copy" / "result.txt").read_text(encoding="utf-8") == "preserved"

    assert check_latest_backup(backup_dir, 1)["ok"] is True
    stale = time.time() - 48 * 60 * 60
    os.utime(archive, (stale, stale))
    result = check_latest_backup(backup_dir, 26)
    assert result["ok"] is False
    assert result["age_hours"] > result["max_age_hours"]
