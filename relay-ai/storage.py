"""Small durable SQLite-backed mapping for pipeline task snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import datetime
from collections.abc import Iterator, MutableMapping
from decimal import Decimal
from pathlib import Path

from schemas import EvidenceEvent, PipelineStatus, PipelineTaskResponse


class SQLiteTaskStore(MutableMapping[str, PipelineTaskResponse]):
    """Persist task snapshots locally while preserving the mapping API used by the API layer."""

    BASELINE_SCHEMA_VERSION = 9
    SCHEMA_VERSION = 10
    MIGRATIONS = {10: "source_snapshot_sha256"}

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # SQLite DDL is transactional when an explicit transaction is active.
            # Serialize startup upgrades and avoid leaving a partially upgraded DB.
            connection.execute("BEGIN IMMEDIATE")
            existing_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            recorded_version = 0
            if existing_migrations:
                recorded_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()["version"]
                if recorded_version > self.SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema version {recorded_version} is newer than supported version "
                        f"{self.SCHEMA_VERSION}. Upgrade the application before opening this database."
                    )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, owner_id TEXT, updated_at TEXT, status TEXT, payload TEXT NOT NULL)"
            )
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "owner_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
            if "updated_at" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")
            if "status" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN status TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_owner_status_updated ON tasks(owner_id, status, updated_at DESC)")
            for task_row in connection.execute("SELECT task_id, payload FROM tasks WHERE updated_at IS NULL OR status IS NULL"):
                legacy_task = PipelineTaskResponse.model_validate_json(task_row["payload"])
                connection.execute(
                    "UPDATE tasks SET owner_id=?, updated_at=?, status=? WHERE task_id=?",
                    (legacy_task.owner_id, legacy_task.updated_at, legacy_task.status.value, task_row["task_id"]),
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
                "CREATE TABLE IF NOT EXISTS stage_observations (task_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "stage TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms REAL NOT NULL, isolation_mode TEXT, "
                "PRIMARY KEY(task_id, sequence), FOREIGN KEY(task_id, sequence) "
                "REFERENCES task_events(task_id, sequence) ON DELETE CASCADE)"
            )
            for event_row in connection.execute("SELECT task_id, sequence, payload FROM task_events"):
                observation = self._stage_observation(event_row["payload"])
                if observation:
                    connection.execute(
                        "INSERT OR IGNORE INTO stage_observations(task_id, sequence, stage, outcome, duration_ms, isolation_mode) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (event_row["task_id"], event_row["sequence"], *observation),
                    )
            connection.execute("CREATE INDEX IF NOT EXISTS stage_observations_stage ON stage_observations(stage, outcome)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_usage ("
                "task_id TEXT NOT NULL, sequence INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
                "stage TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, candidate_tokens INTEGER NOT NULL, "
                "total_tokens INTEGER NOT NULL, created_at TEXT NOT NULL, input_usd_per_million TEXT, "
                "output_usd_per_million TEXT, estimated_cost_usd TEXT, pricing_label TEXT, PRIMARY KEY(task_id, sequence), "
                "FOREIGN KEY(task_id, sequence) REFERENCES task_events(task_id, sequence) ON DELETE CASCADE)"
            )
            usage_columns = {row["name"] for row in connection.execute("PRAGMA table_info(provider_usage)")}
            for column in ("input_usd_per_million", "output_usd_per_million", "estimated_cost_usd", "pricing_label"):
                if column not in usage_columns:
                    connection.execute(f"ALTER TABLE provider_usage ADD COLUMN {column} TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pipeline_jobs ("
                "task_id TEXT PRIMARY KEY, chat_message TEXT NOT NULL, status TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, "
                "lease_until TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_attempts ("
                "task_id TEXT NOT NULL, attempt INTEGER NOT NULL, queue_wait_ms REAL NOT NULL, "
                "started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, "
                "PRIMARY KEY(task_id, attempt), FOREIGN KEY(task_id) REFERENCES pipeline_jobs(task_id) ON DELETE CASCADE)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS job_attempts_outcome ON job_attempts(outcome)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_actions ("
                "action_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, actor TEXT NOT NULL, "
                "action TEXT NOT NULL, outcome TEXT NOT NULL, notes TEXT, details_json TEXT NOT NULL, "
                "idempotency_key TEXT, request_hash TEXT, response_json TEXT, "
                "created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE)"
            )
            action_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_actions)")}
            if "idempotency_key" not in action_columns:
                connection.execute("ALTER TABLE task_actions ADD COLUMN idempotency_key TEXT")
            if "response_json" not in action_columns:
                connection.execute("ALTER TABLE task_actions ADD COLUMN response_json TEXT")
            if "request_hash" not in action_columns:
                connection.execute("ALTER TABLE task_actions ADD COLUMN request_hash TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS rate_limit_buckets ("
                "bucket_key TEXT PRIMARY KEY, window_started REAL NOT NULL, request_count INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_actions_task_created ON task_actions(task_id, action_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS task_actions_idempotency "
                "ON task_actions(task_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pipeline_jobs)")}
            if "cancel_requested" not in job_columns:
                connection.execute(
                    "ALTER TABLE pipeline_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            # Existing databases did not have executable migration records. The
            # idempotent schema setup above upgrades them to the v9 baseline.
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (self.BASELINE_SCHEMA_VERSION, self._utc_now()),
            )
            for version in range(self.BASELINE_SCHEMA_VERSION + 1, self.SCHEMA_VERSION + 1):
                if version > recorded_version and version in self.MIGRATIONS:
                    self._apply_migration(connection, version)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (version, self._utc_now()),
                )
            snapshot_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_snapshots)")}
            if "content_sha256" not in snapshot_columns:
                raise RuntimeError("SQLite schema is marked current but source snapshot integrity column is missing.")

    def _apply_migration(self, connection: sqlite3.Connection, version: int) -> None:
        if version == 10:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_snapshots)")}
            if "content_sha256" not in columns:
                connection.execute(
                    "ALTER TABLE task_snapshots ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''"
                )
            rows = connection.execute(
                "SELECT task_id, files_json FROM task_snapshots WHERE content_sha256 = ''"
            ).fetchall()
            for snapshot_row in rows:
                connection.execute(
                    "UPDATE task_snapshots SET content_sha256=? WHERE task_id=?",
                    (self._snapshot_digest(snapshot_row["files_json"]), snapshot_row["task_id"]),
                )
            return
        raise RuntimeError(f"No SQLite migration is registered for schema version {version}.")

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
                "INSERT INTO tasks(task_id, owner_id, updated_at, status, payload) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET owner_id=excluded.owner_id, updated_at=excluded.updated_at, status=excluded.status, payload=excluded.payload",
                (task_id, task.owner_id, task.updated_at, task.status.value, task.model_dump_json()),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            self._store_stage_observations(connection, task)

    def update_task_if_present(self, task: PipelineTaskResponse) -> bool:
        """Update an existing task without allowing concurrent deletion to resurrect it."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET owner_id=?, updated_at=?, status=?, payload=? WHERE task_id=?",
                (task.owner_id, task.updated_at, task.status.value, task.model_dump_json(), task.task_id),
            )
            if cursor.rowcount == 0:
                return False
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task.task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            self._store_stage_observations(connection, task)
            return True

    def __delitem__(self, task_id: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise KeyError(task_id)

    def delete_task_if_stopped(self, task_id: str) -> bool:
        """Delete a task only if its queue job remains inactive at commit time."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task_exists is None:
                connection.rollback()
                raise KeyError(task_id)
            stored_task = connection.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            task = PipelineTaskResponse.model_validate_json(stored_task["payload"])
            if task.repository_id and task.repository_operation_state in {"APPLYING", "APPLIED", "COMMITTING", "COMMITTED"}:
                connection.rollback()
                return False
            job = connection.execute(
                "SELECT status FROM pipeline_jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if job and job["status"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                connection.rollback()
                return False
            cursor = connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(task_id)
            connection.commit()
            return True

    def __iter__(self) -> Iterator[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall()
        return iter([row["task_id"] for row in rows])

    def __len__(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
        return int(row["count"])

    def list_tasks_page(
        self,
        owner_id: str,
        include_legacy: bool,
        limit: int,
        offset: int,
        search: str | None = None,
        status: str | None = None,
    ) -> list[PipelineTaskResponse]:
        clauses = ["(owner_id=? OR owner_id IS NULL)" if include_legacy else "owner_id=?"]
        params: list[object] = [owner_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            clauses.append("instr(lower(payload), lower(?)) > 0")
            params.append(search)
        params.extend((limit, offset))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM tasks WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, task_id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [PipelineTaskResponse.model_validate_json(row["payload"]) for row in rows]

    def count_tasks_matching(
        self, owner_id: str, include_legacy: bool,
        search: str | None = None, status: str | None = None,
    ) -> int:
        clauses = ["(owner_id=? OR owner_id IS NULL)" if include_legacy else "owner_id=?"]
        params: list[object] = [owner_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            clauses.append("instr(lower(payload), lower(?)) > 0")
            params.append(search)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM tasks WHERE {' AND '.join(clauses)}", params
            ).fetchone()
        return int(row["count"])

    def retention_candidates(self, cutoff: str) -> list[dict[str, str]]:
        """List old terminal tasks, excluding operations bound to configured repositories."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT tasks.task_id, tasks.status, tasks.updated_at, tasks.payload FROM tasks "
                "JOIN pipeline_jobs ON pipeline_jobs.task_id=tasks.task_id "
                "WHERE tasks.updated_at < ? AND tasks.status IN ('FAILED','CANCELLED','REJECTED','COMMITTED') "
                "AND pipeline_jobs.status IN ('FAILED','CANCELLED','COMPLETED') "
                "ORDER BY tasks.updated_at, tasks.task_id",
                (cutoff,),
            ).fetchall()
        candidates = []
        for row in rows:
            task = PipelineTaskResponse.model_validate_json(row["payload"])
            if task.repository_id is None:
                candidates.append({key: row[key] for key in ("task_id", "status", "updated_at")})
        return candidates

    def delete_retained_task(self, task_id: str, cutoff: str) -> bool:
        """Recheck terminal status and age inside the same transaction as cascading deletion."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task_row is None or PipelineTaskResponse.model_validate_json(task_row["payload"]).repository_id is not None:
                return False
            eligible = connection.execute(
                "SELECT 1 FROM tasks JOIN pipeline_jobs USING(task_id) WHERE tasks.task_id=? AND tasks.updated_at < ? "
                "AND tasks.status IN ('FAILED','CANCELLED','REJECTED','COMMITTED') "
                "AND pipeline_jobs.status IN ('FAILED','CANCELLED','COMPLETED')",
                (task_id, cutoff),
            ).fetchone()
            if eligible is None:
                return False
            connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            return True

    def save_snapshot(self, task_id: str, files: dict[str, str]) -> None:
        serialized = json.dumps(files, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_snapshots(task_id, files_json, content_sha256) VALUES(?, ?, ?)",
                (task_id, serialized, self._snapshot_digest(serialized)),
            )
            existing = connection.execute(
                "SELECT files_json, content_sha256 FROM task_snapshots WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(task_id)
            if existing["files_json"] != serialized or existing["content_sha256"] != self._snapshot_digest(serialized):
                raise ValueError("A task source snapshot is immutable once it has been stored.")

    def get_snapshot(self, task_id: str) -> dict[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT files_json, content_sha256 FROM task_snapshots WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row and row["content_sha256"] != self._snapshot_digest(row["files_json"]):
            raise RuntimeError(f"Stored source snapshot integrity check failed for task {task_id}.")
        return json.loads(row["files_json"]) if row else None

    def get_events(self, task_id: str, limit: int = 100, offset: int = 0) -> list[EvidenceEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_events WHERE task_id = ? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        return [EvidenceEvent.model_validate_json(row["payload"]) for row in rows]

    def count_events(self, task_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["count"])

    def record_provider_usage(
        self,
        task_id: str,
        sequence: int,
        provider: str,
        model: str,
        stage: str,
        prompt_tokens: int,
        candidate_tokens: int,
        total_tokens: int,
        input_usd_per_million: str | None = None,
        output_usd_per_million: str | None = None,
        estimated_cost_usd: str | None = None,
        pricing_label: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO provider_usage(task_id, sequence, provider, model, stage, prompt_tokens, "
                "candidate_tokens, total_tokens, created_at, input_usd_per_million, output_usd_per_million, "
                "estimated_cost_usd, pricing_label) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, sequence, provider, model, stage, prompt_tokens, candidate_tokens, total_tokens,
                 self._utc_now(), input_usd_per_million, output_usd_per_million, estimated_cost_usd, pricing_label),
            )

    def provider_usage_totals(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(candidate_tokens), 0) AS candidate_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens FROM provider_usage"
            ).fetchone()
        return {key: int(row[key]) for key in ("prompt_tokens", "candidate_tokens", "total_tokens")}

    def provider_cost_totals(self) -> dict[str, int | str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT estimated_cost_usd FROM provider_usage").fetchall()
        priced = [Decimal(row["estimated_cost_usd"]) for row in rows if row["estimated_cost_usd"] is not None]
        return {
            "estimated_cost_usd": str(sum(priced, Decimal(0))),
            "priced_events": len(priced),
            "unpriced_events": len(rows) - len(priced),
        }

    @staticmethod
    def _stage_observation(payload: str) -> tuple[str, str, float, str | None] | None:
        try:
            event = json.loads(payload)
            details = event.get("details") or {}
            duration = details.get("duration_ms", details.get("elapsed_ms"))
            if not isinstance(duration, (int, float)) or duration < 0:
                return None
            stage = str(event.get("stage", ""))
            status = str(event.get("status", "")).upper()
            if status not in {"COMPLETED", "FAILED"}:
                return None
            outcome = "failed" if status == "FAILED" else "completed"
            isolation = None
            if stage == "SANDBOX_TEST_EXECUTION":
                outcome = "passed" if details.get("tests_passed") is True else "failed"
                isolation = details.get("isolation_mode")
            return stage, outcome, float(duration), str(isolation) if isolation else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _store_stage_observations(cls, connection: sqlite3.Connection, task: PipelineTaskResponse) -> None:
        for sequence, event in enumerate(task.evidence_timeline):
            observation = cls._stage_observation(event.model_dump_json())
            if observation:
                connection.execute(
                    "INSERT OR IGNORE INTO stage_observations(task_id, sequence, stage, outcome, duration_ms, isolation_mode) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (task.task_id, sequence, *observation),
                )

    def stage_metrics(self) -> tuple[list[dict], list[dict]]:
        buckets = (100, 500, 1000, 5000, 15000, 60000)
        bucket_sql = ", ".join(
            f"SUM(CASE WHEN duration_ms <= {int(bound)} THEN 1 ELSE 0 END) AS le_{int(bound)}"
            for bound in buckets
        )
        with self._lock, self._connect() as connection:
            stages = connection.execute(
                "SELECT stage, COUNT(*) AS count, SUM(CASE WHEN outcome='failed' THEN 1 ELSE 0 END) AS failures, "
                f"SUM(duration_ms) AS duration_sum, {bucket_sql} FROM stage_observations GROUP BY stage"
            ).fetchall()
            sandbox = connection.execute(
                "SELECT COALESCE(isolation_mode, 'unknown') AS mode, outcome, COUNT(*) AS count "
                "FROM stage_observations WHERE stage='SANDBOX_TEST_EXECUTION' GROUP BY isolation_mode, outcome"
            ).fetchall()
        return [dict(row) for row in stages], [dict(row) for row in sandbox]

    def record_action(
        self,
        task_id: str,
        action: str,
        actor: str,
        outcome: str,
        notes: str | None = None,
        details: dict | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        response: PipelineTaskResponse | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_actions(task_id, actor, action, outcome, notes, details_json, "
                "idempotency_key, request_hash, response_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, actor, action, outcome, notes,
                    json.dumps(details or {}, ensure_ascii=False), idempotency_key, request_hash,
                    response.model_dump_json() if response else None, self._utc_now(),
                ),
            )

    def get_action_by_idempotency_key(self, task_id: str, key: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT actor, action, request_hash, response_json FROM task_actions WHERE task_id=? AND idempotency_key=?",
                (task_id, key),
            ).fetchone()
        return dict(row) if row else None

    def get_actions(self, task_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT action_id, task_id, actor, action, outcome, notes, details_json, created_at "
                "FROM task_actions WHERE task_id=? ORDER BY action_id DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["details"] = json.loads(record.pop("details_json"))
            records.append(record)
        return records

    def count_actions(self, task_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM task_actions WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _utc_now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _snapshot_digest(serialized: str) -> str:
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def create_task(self, task: PipelineTaskResponse, files: dict[str, str], max_attempts: int = 3) -> None:
        now = self._utc_now()
        serialized_files = json.dumps(files, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_id = task.task_id
            connection.execute(
                "INSERT INTO tasks(task_id, owner_id, updated_at, status, payload) VALUES(?, ?, ?, ?, ?)",
                (task_id, task.owner_id, task.updated_at, task.status.value, task.model_dump_json()),
            )
            connection.executemany(
                "INSERT INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            connection.execute(
                "INSERT INTO task_snapshots(task_id, files_json, content_sha256) VALUES(?, ?, ?)",
                (task_id, serialized_files, self._snapshot_digest(serialized_files)),
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
            terminal_success = "'AWAITING_HUMAN_APPROVAL','REVIEWED','APPROVED','APPLIED','COMMITTED','REJECTED'"
            connection.execute(
                "UPDATE pipeline_jobs SET status='COMPLETED', error=NULL, lease_until=NULL, updated_at=? "
                "WHERE status='RUNNING' AND lease_until < ? AND task_id IN "
                f"(SELECT task_id FROM tasks WHERE status IN ({terminal_success}))",
                (now_text, now_text),
            )
            connection.execute(
                "UPDATE pipeline_jobs SET status='FAILED', error='Task failure persisted before worker acknowledgement', "
                "lease_until=NULL, updated_at=? WHERE status='RUNNING' AND lease_until < ? AND task_id IN "
                "(SELECT task_id FROM tasks WHERE status='FAILED')",
                (now_text, now_text),
            )
            connection.execute(
                "UPDATE pipeline_jobs SET status='CANCELLED', cancel_requested=0, error=NULL, lease_until=NULL, updated_at=? "
                "WHERE status='RUNNING' AND lease_until < ? AND (cancel_requested=1 OR task_id IN "
                "(SELECT task_id FROM tasks WHERE status='CANCELLED'))",
                (now_text, now_text),
            )
            connection.execute(
                "UPDATE pipeline_jobs SET status='FAILED', error='Worker lease expired after maximum attempts', updated_at=?, lease_until=NULL "
                "WHERE status='RUNNING' AND lease_until < ? AND attempts >= max_attempts",
                (now_text, now_text),
            )
            connection.execute(
                "UPDATE job_attempts SET outcome=CASE (SELECT status FROM pipeline_jobs WHERE task_id=job_attempts.task_id) "
                "WHEN 'COMPLETED' THEN 'completed' WHEN 'CANCELLED' THEN 'cancelled' ELSE 'failed' END, finished_at=? "
                "WHERE outcome IS NULL AND EXISTS (SELECT 1 FROM pipeline_jobs WHERE task_id=job_attempts.task_id "
                "AND attempts=job_attempts.attempt AND updated_at=? AND status IN ('COMPLETED','FAILED','CANCELLED'))",
                (now_text, now_text),
            )
            cancelled_rows = connection.execute(
                "SELECT jobs.task_id, tasks.payload FROM pipeline_jobs AS jobs JOIN tasks ON tasks.task_id=jobs.task_id "
                "WHERE jobs.status='CANCELLED' AND jobs.updated_at=? AND tasks.status NOT IN ('CANCELLED','FAILED')",
                (now_text,),
            ).fetchall()
            for cancelled_row in cancelled_rows:
                task = PipelineTaskResponse.model_validate_json(cancelled_row["payload"])
                task.transition_to(PipelineStatus.CANCELLED, recovery=True)
                task.current_stage = "CANCELLED"
                task.updated_at = now_text
                task.evidence_timeline.append(EvidenceEvent(
                    timestamp=now_text,
                    stage="WORKER_RECOVERY",
                    status="CANCELLED",
                    actor="system",
                    correlation_id=cancelled_row["task_id"],
                    details={"reason": "Worker lease expired after cancellation."},
                ))
                connection.execute(
                    "UPDATE tasks SET payload=?, updated_at=?, status=? WHERE task_id=?",
                    (task.model_dump_json(), now_text, task.status.value, cancelled_row["task_id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                    (cancelled_row["task_id"], len(task.evidence_timeline) - 1, task.evidence_timeline[-1].model_dump_json()),
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
                task.transition_to(PipelineStatus.FAILED)
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
                    actor="system",
                    correlation_id=expired_row["task_id"],
                    details=task.error,
                ))
                connection.execute(
                    "UPDATE tasks SET payload=?, updated_at=?, status=? WHERE task_id=?",
                    (task.model_dump_json(), task.updated_at, task.status.value, expired_row["task_id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                    (expired_row["task_id"], len(task.evidence_timeline) - 1, task.evidence_timeline[-1].model_dump_json()),
                )
            row = connection.execute(
                "SELECT jobs.task_id, jobs.chat_message, jobs.attempts, jobs.status, jobs.created_at, jobs.updated_at, "
                "jobs.lease_until, tasks.payload AS task_payload FROM pipeline_jobs AS jobs "
                "JOIN tasks ON tasks.task_id=jobs.task_id "
                "WHERE jobs.attempts < jobs.max_attempts AND (jobs.status='QUEUED' OR (jobs.status='RUNNING' AND jobs.lease_until < ?)) "
                "ORDER BY jobs.created_at LIMIT 1",
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "RUNNING":
                connection.execute(
                    "UPDATE job_attempts SET outcome='lease_expired', finished_at=? WHERE task_id=? AND attempt=? AND outcome IS NULL",
                    (now_text, row["task_id"], row["attempts"]),
                )
            connection.execute(
                "UPDATE pipeline_jobs SET status='RUNNING', attempts=attempts+1, lease_until=?, updated_at=?, error=NULL "
                "WHERE task_id=?",
                (lease_until, now_text, row["task_id"]),
            )
            waiting_since = row["created_at"] if row["attempts"] == 0 else (
                row["lease_until"] if row["status"] == "RUNNING" else row["updated_at"]
            )
            queue_wait_ms = max(
                0.0,
                (now - datetime.datetime.fromisoformat(waiting_since)).total_seconds() * 1000,
            )
            attempt = row["attempts"] + 1
            connection.execute(
                "INSERT INTO job_attempts(task_id, attempt, queue_wait_ms, started_at) VALUES(?, ?, ?, ?)",
                (row["task_id"], attempt, queue_wait_ms, now_text),
            )
            task = PipelineTaskResponse.model_validate_json(row["task_payload"])
            task.transition_to(PipelineStatus.RUNNING, recovery=row["attempts"] > 0)
            task.current_stage = "WORKER_CLAIMED"
            task.updated_at = now_text
            task.evidence_timeline.append(EvidenceEvent(
                timestamp=now_text,
                stage="WORKER_CLAIM",
                status="RUNNING",
                actor="system",
                correlation_id=row["task_id"],
                details={"attempt": attempt},
            ))
            connection.execute(
                "UPDATE tasks SET updated_at=?, status=?, payload=? WHERE task_id=?",
                (task.updated_at, task.status.value, task.model_dump_json(), row["task_id"]),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                (row["task_id"], len(task.evidence_timeline) - 1, task.evidence_timeline[-1].model_dump_json()),
            )
            return {"task_id": row["task_id"], "chat_message": row["chat_message"], "attempt": attempt}

    def heartbeat_job(self, task_id: str, attempt: int, lease_seconds: int = 60) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc)
        now_text = now.isoformat()
        lease_until = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE pipeline_jobs SET lease_until=?, updated_at=? WHERE task_id=? AND attempts=? AND lease_until>? AND status IN ('RUNNING', 'CANCEL_REQUESTED')",
                (lease_until, now_text, task_id, attempt, now_text),
            )
        return cursor.rowcount == 1

    def finish_job(self, task_id: str, attempt: int, status: str, error: str | None = None) -> bool:
        now = self._utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE pipeline_jobs SET status=?, error=?, lease_until=NULL, updated_at=? WHERE task_id=? AND attempts=? AND lease_until>? AND status IN ('RUNNING', 'CANCEL_REQUESTED')",
                (status, error, now, task_id, attempt, now),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE job_attempts SET finished_at=?, outcome=? WHERE task_id=? AND attempt=?",
                    (now, status.lower(), task_id, attempt),
                )
        return cursor.rowcount == 1

    def job_attempt_metrics(self) -> tuple[list[dict], list[dict]]:
        bounds = (100, 500, 1000, 5000, 15000, 60000)
        bucket_sql = ", ".join(
            f"SUM(CASE WHEN queue_wait_ms <= {int(bound)} THEN 1 ELSE 0 END) AS le_{int(bound)}"
            for bound in bounds
        )
        with self._lock, self._connect() as connection:
            histogram = connection.execute(
                "SELECT COUNT(*) AS count, SUM(queue_wait_ms) AS wait_sum, "
                f"{bucket_sql} FROM job_attempts"
            ).fetchone()
            outcomes = connection.execute(
                "SELECT COALESCE(outcome, 'running') AS outcome, COUNT(*) AS count "
                "FROM job_attempts GROUP BY COALESCE(outcome, 'running')"
            ).fetchall()
        return dict(histogram), [dict(row) for row in outcomes]

    def save_worker_task(self, task_id: str, attempt: int, task: PipelineTaskResponse) -> bool:
        """Persist progress only while this worker still owns the active job lease."""
        if task.task_id != task_id:
            raise ValueError("Task mapping key must match task.task_id")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._utc_now()
            lease = connection.execute(
                "SELECT 1 FROM pipeline_jobs WHERE task_id=? AND attempts=? AND lease_until>? AND status IN ('RUNNING', 'CANCEL_REQUESTED')",
                (task_id, attempt, now),
            ).fetchone()
            if lease is None:
                return False
            connection.execute(
                "UPDATE tasks SET owner_id=?, updated_at=?, status=?, payload=? WHERE task_id=?",
                (task.owner_id, task.updated_at, task.status.value, task.model_dump_json(), task_id),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            self._store_stage_observations(connection, task)
            return True

    def finish_worker_task(
        self, task_id: str, attempt: int, task: PipelineTaskResponse,
        job_status: str, error: str | None = None,
    ) -> bool:
        """Atomically persist the final task state and close the attempt under its lease."""
        if task.task_id != task_id:
            raise ValueError("Task mapping key must match task.task_id")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._utc_now()
            job = connection.execute(
                "SELECT cancel_requested FROM pipeline_jobs WHERE task_id=? AND attempts=? AND lease_until>? "
                "AND status IN ('RUNNING','CANCEL_REQUESTED')",
                (task_id, attempt, now),
            ).fetchone()
            if job is None:
                return False
            if job["cancel_requested"] and task.status != PipelineStatus.CANCELLED:
                task.transition_to(PipelineStatus.CANCELLED)
                task.current_stage = "CANCELLED"
                task.updated_at = now
                task.evidence_timeline.append(EvidenceEvent(
                    timestamp=now,
                    stage="CANCELLATION",
                    status="CANCELLED",
                    actor="system",
                    correlation_id=task_id,
                    details={"reason": "Cancellation was requested before completion."},
                ))
                job_status = "CANCELLED"
                error = None
            cursor = connection.execute(
                "UPDATE pipeline_jobs SET status=?, error=?, lease_until=NULL, updated_at=? "
                "WHERE task_id=? AND attempts=? AND lease_until>? AND status IN ('RUNNING','CANCEL_REQUESTED')",
                (job_status, error, now, task_id, attempt, now),
            )
            if cursor.rowcount != 1:
                return False
            task.updated_at = task.updated_at or now
            connection.execute(
                "UPDATE tasks SET owner_id=?, updated_at=?, status=?, payload=? WHERE task_id=?",
                (task.owner_id, task.updated_at, task.status.value, task.model_dump_json(), task_id),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO task_events(task_id, sequence, payload) VALUES(?, ?, ?)",
                [(task_id, index, event.model_dump_json()) for index, event in enumerate(task.evidence_timeline)],
            )
            self._store_stage_observations(connection, task)
            connection.execute(
                "UPDATE job_attempts SET finished_at=?, outcome=? WHERE task_id=? AND attempt=?",
                (now, job_status.lower(), task_id, attempt),
            )
            return True

    def get_job(self, task_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM pipeline_jobs WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def job_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM pipeline_jobs GROUP BY status").fetchall()
        return {row["status"].lower(): int(row["count"]) for row in rows}

    def schema_version(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)

    def consume_rate_limit(self, bucket_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Atomically consume a fixed-window request token; return allow and retry-after seconds."""
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_started, request_count FROM rate_limit_buckets WHERE bucket_key=?",
                (bucket_key,),
            ).fetchone()
            if row is None or now - row["window_started"] >= window_seconds:
                connection.execute(
                    "INSERT INTO rate_limit_buckets(bucket_key, window_started, request_count) VALUES(?, ?, 1) "
                    "ON CONFLICT(bucket_key) DO UPDATE SET window_started=excluded.window_started, request_count=1",
                    (bucket_key, now),
                )
                connection.execute(
                    "DELETE FROM rate_limit_buckets WHERE window_started < ?",
                    (now - (window_seconds * 10),),
                )
                return True, 0
            if row["request_count"] >= limit:
                retry_after = max(1, int(window_seconds - (now - row["window_started"])))
                return False, retry_after
            connection.execute(
                "UPDATE rate_limit_buckets SET request_count=request_count+1 WHERE bucket_key=?",
                (bucket_key,),
            )
            return True, 0

    def cancel_job(self, task_id: str) -> str | None:
        now = self._utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT jobs.status, jobs.cancel_requested, tasks.status AS task_status "
                "FROM pipeline_jobs AS jobs JOIN tasks ON tasks.task_id=jobs.task_id WHERE jobs.task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            active_statuses = {
                "RECEIVED", "QUEUED", "RUNNING", "TRIAGING", "TRIAGED", "INVESTIGATING",
                "INVESTIGATED", "PATCHED", "SANDBOX_RUNNING", "SANDBOX_TESTED", "REVIEWING",
            }
            if row["status"] == "QUEUED":
                if row["task_status"] not in {"RECEIVED", "QUEUED"}:
                    return "COMPLETED"
                connection.execute(
                    "UPDATE pipeline_jobs SET status='CANCELLED', updated_at=? WHERE task_id=?",
                    (now, task_id),
                )
                return "CANCELLED"
            if row["status"] == "RUNNING":
                if row["task_status"] not in active_statuses:
                    return "COMPLETED"
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
                "UPDATE tasks SET payload=?, updated_at=?, status=? WHERE task_id=?",
                (reset_task.model_dump_json(), reset_task.updated_at, reset_task.status.value, task_id),
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
