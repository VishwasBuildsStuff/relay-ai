"""Verify the retained-job completion SLI and Prometheus exposition."""

import datetime
import sqlite3

from fastapi.testclient import TestClient

import main
from schemas import PipelineStatus, PipelineTaskResponse
from storage import SQLiteTaskStore


def _queued_task(task_id):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return PipelineTaskResponse(
        task_id=task_id,
        status=PipelineStatus.QUEUED,
        current_stage="QUEUED",
        bug_report="Measure pipeline completion SLI",
        created_at=now,
        updated_at=now,
    )


def _finish(store, task_id, task_status, job_status):
    claim = store.claim_job()
    assert claim is not None and claim["task_id"] == task_id
    task = store[task_id]
    task.status = task_status
    task.current_stage = task_status.value
    assert store.finish_worker_task(task_id, claim["attempt"], task, job_status)


def test_pipeline_completion_sli_counts_review_failure_cancel_and_active(tmp_path, monkeypatch):
    store = SQLiteTaskStore(tmp_path / "sli.sqlite3")
    store.create_task(_queued_task("reviewed"), {"app.py": "pass"})
    _finish(store, "reviewed", PipelineStatus.AWAITING_HUMAN_APPROVAL, "COMPLETED")

    store.create_task(_queued_task("late-failure"), {"app.py": "pass"})
    with sqlite3.connect(store.database_path) as connection:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        connection.execute("UPDATE pipeline_jobs SET created_at=? WHERE task_id=?", (old, "late-failure"))
    _finish(store, "late-failure", PipelineStatus.FAILED, "FAILED")

    store.create_task(_queued_task("cancelled"), {"app.py": "pass"})
    assert store.cancel_job("cancelled") == "CANCELLED"
    store.create_task(_queued_task("active"), {"app.py": "pass"})

    store.create_task(_queued_task("overdue-active"), {"app.py": "pass"})
    with sqlite3.connect(store.database_path) as connection:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        connection.execute("UPDATE pipeline_jobs SET created_at=? WHERE task_id=?", (old, "overdue-active"))

    store.create_task(_queued_task("outside-window"), {"app.py": "pass"})
    with sqlite3.connect(store.database_path) as connection:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)).isoformat()
        connection.execute("UPDATE pipeline_jobs SET created_at=? WHERE task_id=?", (old, "outside-window"))
    _finish(store, "outside-window", PipelineStatus.AWAITING_HUMAN_APPROVAL, "COMPLETED")

    counts = store.pipeline_completion_sli(main.PIPELINE_COMPLETION_SLO_MS)
    assert counts == {
        "accepted": 5,
        "reviewed": 1,
        "failed": 1,
        "cancelled": 1,
        "in_progress": 2,
        "within_slo": 1,
        "late": 2,
        "eligible": 3,
    }

    monkeypatch.setattr(main, "tasks_db", store)
    monkeypatch.setattr(main, "API_TOKEN", None)
    monkeypatch.setattr(main, "AUTH_USERS", {})
    response = TestClient(main.app).get("/metrics")
    assert response.status_code == 200
    assert 'relay_pipeline_completion_sli_jobs{state="accepted"} 5' in response.text
    assert 'relay_pipeline_completion_sli_jobs{state="within_slo"} 1' in response.text
    assert 'relay_pipeline_completion_sli_ratio 0.333333' in response.text
