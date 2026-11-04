"""Small durable SQLite-backed mapping for pipeline task snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
import datetime
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from schemas import EvidenceEvent, PipelineStatus, PipelineTaskResponse


class SQLiteTaskStore(MutableMapping[str, PipelineTaskResponse]):
    """Persist task snapshots locally while preserving the mapping API used by the API layer."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_snapshots (task_id TEXT PRIMARY KEY, files_json TEXT NOT NULL, "
                "FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_events (task_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(task_id, sequence), "
                "FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pipeline_jobs ("
                "task_id TEXT PRIMARY KEY, chat_message TEXT NOT NULL, status TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, "
                "lease_until TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE)"
            )
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pipeline_jobs)")}
            if "cancel_requested" not in job_columns:
                connection.execute(
                    "ALTER TABLE pipeline_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def __getitem__(self, task_id: str) -> PipelineTaskResponse:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT payload FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return PipelineTaskResponse.model_validate_json(row["payload"])

    def __setitem__(self, task_id: str, task: PipelineTaskResponse) -> None:
        if task.task_id != task_id:
            raise ValueError("Task mapping key must match task.task_id")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, payload) VALUES(?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET payload = excluded.payload",
                (task_id, task.model_dump_json()),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )

    def __delitem__(self, task_id: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise KeyError(task_id)

    def __iter__(self) -> Iterator[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall()
        return iter([row["task_id"] for row in rows])

    def __len__(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
        return int(row["count"])

    def save_snapshot(self, task_id: str, files: dict[str, str]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO task_snapshots(task_id, files_json) VALUES(?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET files_json = excluded.files_json",
                (task_id, json.dumps(files, ensure_ascii=False)),
            )

    def get_snapshot(self, task_id: str) -> dict[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT files_json FROM task_snapshots WHERE task_id = ?", (task_id,)
            ).fetchone()
        return json.loads(row["files_json"]) if row else None

    def get_events(self, task_id: str, limit: int = 100, offset: int = 0) -> list[EvidenceEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_events WHERE task_id = ? ORDER BY sequence LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        return [EvidenceEvent.model_validate_json(row["payload"]) for row in rows]

    @staticmethod
    def _utc_now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def create_task(self, task: PipelineTaskResponse, files: dict[str, str], max_attempts: int = 3) -> None:
        now = self._utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_id = task.task_id
            connection.execute("INSERT INTO tasks(task_id, payload) VALUES(?, ?)", (task_id, task.model_dump_json()))
            connection.executemany(
                "INSERT INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            connection.execute(
                "INSERT INTO task_snapshots(task_id, files_json) VALUES(?, ?)",
                (task_id, json.dumps(files, ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO pipeline_jobs(task_id, chat_message, status, max_attempts, created_at, updated_at) "
                "VALUES(?, ?, 'QUEUED', ?, ?, ?)",
                (task_id, task.bug_report, max_attempts, now, now),
            )

    def claim_job(self, lease_seconds: int = 60) -> dict | None:
        now = datetime.datetime.now(datetime.timezone.utc)
        now_text = now.isoformat()
        lease_until = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE pipeline_jobs SET status='FAILED', error='Worker lease expired after maximum attempts', updated_at=?, lease_until=NULL "
                "WHERE status='RUNNING' AND lease_until < ? AND attempts >= max_attempts",
                (now_text, now_text),
            )
            expired = connection.execute(
                "SELECT jobs.task_id, tasks.payload FROM pipeline_jobs AS jobs "
                "JOIN tasks ON tasks.task_id=jobs.task_id "
                "WHERE jobs.status='FAILED' AND jobs.error='Worker lease expired after maximum attempts'"
            ).fetchall()
            for expired_row in expired:
                task = PipelineTaskResponse.model_validate_json(expired_row["payload"])
                if task.status == PipelineStatus.FAILED:
                    continue
                event_time = self._utc_now()
                task.status = PipelineStatus.FAILED
                task.current_stage = "FAILED"
                task.error = {
                    "failed_stage": "WORKER_RECOVERY",
                    "error_type": "LeaseExpired",
                    "message": "Worker lease expired after maximum attempts.",
                    "is_quota_error": False,
                    "timestamp": event_time,
                }
                task.evidence_timeline.append(EvidenceEvent(
                    timestamp=event_time,
                    stage="WORKER_RECOVERY",
                    status="FAILED",
                    details=task.error,
                ))
                connection.execute(
                    "UPDATE tasks SET payload=? WHERE task_id=?",
                    (task.model_dump_json(), expired_row["task_id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                    (expired_row["task_id"], len(task.evidence_timeline) - 1, task.evidence_timeline[-1].model_dump_json()),
                )
            row = connection.execute(
                "SELECT task_id, chat_message, attempts FROM pipeline_jobs "
                "WHERE attempts < max_attempts AND (status='QUEUED' OR (status='RUNNING' AND lease_until < ?)) "
                "ORDER BY created_at LIMIT 1",
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE pipeline_jobs SET status='RUNNING', attempts=attempts+1, lease_until=?, updated_at=?, error=NULL "
                "WHERE task_id=?",
                (lease_until, now_text, row["task_id"]),
            )
            return {"task_id": row["task_id"], "chat_message": row["chat_message"], "attempt": row["attempts"] + 1}

    def heartbeat_job(self, task_id: str, lease_seconds: int = 60) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        lease_until = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE pipeline_jobs SET lease_until=?, updated_at=? WHERE task_id=? AND status IN ('RUNNING', 'CANCEL_REQUESTED')",
                (lease_until, now.isoformat(), task_id),
            )

    def finish_job(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE pipeline_jobs SET status=?, error=?, lease_until=NULL, updated_at=? WHERE task_id=?",
                (status, error, self._utc_now(), task_id),
            )

    def get_job(self, task_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM pipeline_jobs WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def job_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM pipeline_jobs GROUP BY status").fetchall()
        return {row["status"].lower(): int(row["count"]) for row in rows}

    def cancel_job(self, task_id: str) -> str | None:
        now = self._utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested FROM pipeline_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "QUEUED":
                connection.execute(
                    "UPDATE pipeline_jobs SET status='CANCELLED', updated_at=? WHERE task_id=?",
                    (now, task_id),
                )
                return "CANCELLED"
            if row["status"] == "RUNNING":
                connection.execute(
                    "UPDATE pipeline_jobs SET cancel_requested=1, updated_at=? WHERE task_id=?",
                    (now, task_id),
                )
                return "CANCEL_REQUESTED"
            if row["status"] == "CANCEL_REQUESTED":
                return "CANCEL_REQUESTED"
            return row["status"]

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status, cancel_requested FROM pipeline_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] == "CANCELLED"))

    def retry_job(self, task_id: str, reset_task: PipelineTaskResponse) -> tuple[bool, str]:
        now = self._utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, attempts, max_attempts FROM pipeline_jobs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return False, "No queued job exists for this task."
            if row["status"] != "FAILED":
                return False, f"Cannot retry a job in status '{row['status']}'."
            if row["attempts"] >= row["max_attempts"]:
                return False, "Task has reached its maximum retry count."
            connection.execute(
                "UPDATE tasks SET payload=? WHERE task_id=?",
                (reset_task.model_dump_json(), task_id),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(reset_task.evidence_timeline)],
            )
            connection.execute(
                "UPDATE pipeline_jobs SET status='QUEUED', cancel_requested=0, error=NULL, updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            return True, ""
