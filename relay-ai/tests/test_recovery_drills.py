"""Local execution drills for durable worker recovery and bounded retries."""

import datetime
import sqlite3

from schemas import PipelineStatus, PipelineTaskResponse
from storage import SQLiteTaskStore


def _queued_task(task_id: str) -> PipelineTaskResponse:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return PipelineTaskResponse(
        task_id=task_id,
        status=PipelineStatus.QUEUED,
        current_stage="QUEUED",
        bug_report="Exercise worker recovery",
        created_at=now,
        updated_at=now,
    )


def _expire_lease(database: str, task_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE pipeline_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (task_id,),
        )


def test_expired_lease_recovers_after_store_reopen_and_exhausts_attempts(tmp_path):
    database = str(tmp_path / "relay.sqlite3")
    first_store = SQLiteTaskStore(database)
    first_store.create_task(_queued_task("recover-me"), {"main.py": "print('ok')"}, max_attempts=2)

    first_claim = first_store.claim_job()
    assert first_claim["attempt"] == 1
    _expire_lease(database, "recover-me")

    restarted_store = SQLiteTaskStore(database)
    second_claim = restarted_store.claim_job()
    assert second_claim["attempt"] == 2
    _expire_lease(database, "recover-me")

    assert restarted_store.claim_job() is None
    assert restarted_store.get_job("recover-me")["status"] == "FAILED"
    assert restarted_store["recover-me"].status == PipelineStatus.FAILED
    outcomes = restarted_store.job_attempt_metrics()[1]
    assert {entry["outcome"]: entry["count"] for entry in outcomes} == {
        "failed": 1,
        "lease_expired": 1,
    }


def test_cancellation_requested_during_finalization_wins_race(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.sqlite3")
    store.create_task(_queued_task("cancel-race"), {"main.py": "print('ok')"})
    claim = store.claim_job()
    assert claim is not None
    assert store.cancel_job("cancel-race") == "CANCEL_REQUESTED"

    completed = store["cancel-race"]
    assert store.finish_worker_task("cancel-race", claim["attempt"], completed, "COMPLETED")

    assert store.get_job("cancel-race")["status"] == "CANCELLED"
    assert store["cancel-race"].status == PipelineStatus.CANCELLED
    assert store.get_events("cancel-race", limit=1)[0].stage == "CANCELLATION"


def test_manual_retry_is_bounded_by_attempt_limit(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.sqlite3")
    store.create_task(_queued_task("retry-me"), {"main.py": "print('ok')"}, max_attempts=2)
    first = store.claim_job()
    assert first is not None
    failed = store["retry-me"]
    failed.transition_to(PipelineStatus.FAILED)
    failed.current_stage = "FAILED"
    assert store.save_worker_task("retry-me", first["attempt"], failed)
    assert store.finish_job("retry-me", first["attempt"], "FAILED", "transient")

    reset = store["retry-me"]
    reset.transition_to(PipelineStatus.QUEUED)
    reset.current_stage = "QUEUED"
    assert store.retry_job("retry-me", reset) == (True, "")
    second = store.claim_job()
    assert second is not None and second["attempt"] == 2
    assert store.finish_job("retry-me", second["attempt"], "FAILED", "still failing")

    retry_allowed, message = store.retry_job("retry-me", _queued_task("retry-me"))
    assert not retry_allowed
    assert "maximum retry count" in message
