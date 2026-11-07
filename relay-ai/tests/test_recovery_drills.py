"""Local execution drills for durable worker recovery and bounded retries."""

import datetime
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from schemas import EvidenceEvent, PipelineStatus, PipelineTaskResponse
from storage import SQLiteTaskStore
from worker import PipelineWorker


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


def test_concurrent_worker_claims_are_unique_and_persist_ordered_events(tmp_path):
    database = str(tmp_path / "concurrent.sqlite3")
    coordinator = SQLiteTaskStore(database)
    task_ids = [f"concurrent-{index}" for index in range(8)]
    stores = [SQLiteTaskStore(database) for _ in task_ids]

    for task_id in task_ids:
        task = _queued_task(task_id)
        task.evidence_timeline.append(EvidenceEvent(
            timestamp=task.created_at,
            stage="INGESTION",
            status="QUEUED",
            actor="local-operator",
            correlation_id=task_id,
            details={"files_count": 1},
        ))
        coordinator.create_task(task, {"main.py": "print('ok')"}, max_attempts=1)

    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        claims = list(executor.map(lambda store: store.claim_job(), stores))

    claimed_ids = [claim["task_id"] for claim in claims if claim is not None]
    assert len(claimed_ids) == len(task_ids)
    assert len(set(claimed_ids)) == len(task_ids)
    assert set(claimed_ids) == set(task_ids)
    assert all(claim["attempt"] == 1 for claim in claims if claim is not None)

    for task_id in task_ids:
        events = coordinator.get_events(task_id, limit=10)
        assert [event.stage for event in events] == ["WORKER_CLAIM", "INGESTION"]
        assert coordinator.count_events(task_id) == 2

    assert all(store.claim_job() is None for store in stores)


def test_worker_failure_persists_terminal_state_and_redacted_diagnostic(tmp_path, monkeypatch):
    secret = "provider-key-123456789"
    monkeypatch.setenv("RELAY_API_TOKEN", secret)
    store = SQLiteTaskStore(tmp_path / "worker-failure.sqlite3")
    store.create_task(_queued_task("worker-failure"), {"main.py": "print('ok')"})
    job = store.claim_job()
    assert job is not None

    class FailingOrchestrator:
        async def run(self, *_args, **_kwargs):
            raise RuntimeError(f"provider request failed api_key={secret}")

        @staticmethod
        def _now():
            return datetime.datetime.now(datetime.timezone.utc).isoformat()

    PipelineWorker(store, FailingOrchestrator())._process(job)

    failed_task = store["worker-failure"]
    failed_job = store.get_job("worker-failure")
    assert failed_task.status == PipelineStatus.FAILED
    assert failed_task.error["failed_stage"] == "WORKER"
    assert secret not in failed_task.error["message"]
    assert "[REDACTED]" in failed_task.error["message"]
    assert failed_job["status"] == "FAILED"
    assert secret not in failed_job["error"]
    assert store.get_events("worker-failure", limit=1)[0].stage == "WORKER"
