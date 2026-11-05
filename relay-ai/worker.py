"""Single-process worker that leases durable SQLite jobs and records pipeline progress."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from orchestrator import RelayOrchestrator
from redaction import redact_sensitive_text
from schemas import EvidenceEvent, PipelineStatus, PipelineTaskResponse, ReportRequest
from storage import SQLiteTaskStore

logger = logging.getLogger("relay.worker")


class _LeaseLost(RuntimeError):
    """Raised when a worker attempt no longer owns the durable job lease."""


class PipelineWorker:
    def __init__(self, store: SQLiteTaskStore, orchestrator: RelayOrchestrator):
        self.store = store
        self.orchestrator = orchestrator
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @asynccontextmanager
    async def lifespan(self, _app):
        self.start()
        try:
            yield
        finally:
            self.stop()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="relay-pipeline-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim_job()
            except Exception:
                logger.exception("Could not claim a pipeline job")
                self._stop.wait(2)
                continue
            if job is None:
                self._stop.wait(0.75)
                continue
            self._process(job)

    def _process(self, job: dict) -> None:
        task_id = job["task_id"]
        attempt = job["attempt"]
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(15):
                try:
                    if not self.store.heartbeat_job(task_id, attempt):
                        lease_lost.set()
                        return
                except Exception:
                    logger.exception("Failed to renew worker lease", extra={"task_id": task_id})

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"relay-lease-{task_id[:8]}", daemon=True)
        heartbeat_thread.start()
        pipeline_event_count = 0

        def persist_progress(task: PipelineTaskResponse) -> None:
            nonlocal pipeline_event_count
            if lease_lost.is_set():
                raise _LeaseLost(f"Worker lease lost for task {task_id}.")
            previous = self.store[task_id]
            new_events = task.evidence_timeline[pipeline_event_count:]
            pipeline_event_count = len(task.evidence_timeline)
            task.created_at = previous.created_at
            task.owner_id = previous.owner_id
            task.repository_id = previous.repository_id
            task.repository_base_revision = previous.repository_base_revision
            task.repository_applied_digest = previous.repository_applied_digest
            task.repository_result_revision = previous.repository_result_revision
            task.repository_operation_state = previous.repository_operation_state
            task.evidence_timeline = [*previous.evidence_timeline, *new_events]
            if not self.store.save_worker_task(task_id, attempt, task):
                lease_lost.set()
                raise _LeaseLost(f"Worker lease lost for task {task_id}.")
            for offset, event in enumerate(new_events):
                if event.stage == "MODEL_USAGE":
                    details = event.details
                    self.store.record_provider_usage(
                        task_id=task_id,
                        sequence=len(previous.evidence_timeline) + offset,
                        provider=str(details.get("provider", "unknown")),
                        model=str(details.get("model", "unknown")),
                        stage=str(details.get("stage", "unknown")),
                        prompt_tokens=int(details.get("prompt_tokens", 0)),
                        candidate_tokens=int(details.get("candidate_tokens", 0)),
                        total_tokens=int(details.get("total_tokens", 0)),
                        input_usd_per_million=details.get("input_usd_per_million"),
                        output_usd_per_million=details.get("output_usd_per_million"),
                        estimated_cost_usd=details.get("estimated_cost_usd"),
                        pricing_label=details.get("pricing_label"),
                    )

        try:
            codebase = self.store.get_snapshot(task_id)
            if not codebase:
                raise RuntimeError("Task source snapshot is missing.")
            request = ReportRequest(chat_message=job["chat_message"], codebase=codebase)
            task = asyncio.run(self.orchestrator.run(
                task_id,
                request,
                on_progress=persist_progress,
                should_cancel=lambda: self.store.is_cancel_requested(task_id),
            ))
            if lease_lost.is_set():
                raise _LeaseLost(f"Worker lease lost for task {task_id}.")
            if self.store.is_cancel_requested(task_id) and task.status != PipelineStatus.CANCELLED:
                task.transition_to(PipelineStatus.CANCELLED)
                task.current_stage = "CANCELLED"
                self.orchestrator._log_event(task, "CANCELLATION", "CANCELLED")
            task.owner_id = self.store[task_id].owner_id
            previous_task = self.store[task_id]
            task.repository_id = previous_task.repository_id
            task.repository_base_revision = previous_task.repository_base_revision
            task.repository_applied_digest = previous_task.repository_applied_digest
            task.repository_result_revision = previous_task.repository_result_revision
            task.repository_operation_state = previous_task.repository_operation_state
            if task.status == PipelineStatus.CANCELLED:
                job_status, job_error = "CANCELLED", None
            elif task.status == PipelineStatus.FAILED:
                job_status, job_error = "FAILED", task.error.get("message") if task.error else "Pipeline failed"
            else:
                job_status, job_error = "COMPLETED", None
            if not self.store.finish_worker_task(task_id, attempt, task, job_status, job_error):
                lease_lost.set()
                raise _LeaseLost(f"Worker lease lost for task {task_id}.")
        except _LeaseLost as error:
            logger.warning("Discarding stale worker attempt", extra={"task_id": task_id, "attempt": attempt})
        except Exception as error:
            logger.exception("Pipeline worker failed", extra={"task_id": task_id})
            cancelled = self.store.is_cancel_requested(task_id)
            try:
                task = self.store[task_id]
                task.transition_to(PipelineStatus.CANCELLED if cancelled else PipelineStatus.FAILED)
                task.current_stage = task.status.value
                if not cancelled:
                    task.error = {
                        "failed_stage": "WORKER",
                        "error_type": error.__class__.__name__,
                        "message": redact_sensitive_text(error),
                        "is_quota_error": False,
                        "timestamp": self.orchestrator._now(),
                    }
                task.evidence_timeline.append(EvidenceEvent(
                    timestamp=self.orchestrator._now(),
                    stage="CANCELLATION" if cancelled else "WORKER",
                    status="CANCELLED" if cancelled else "FAILED",
                    actor="system",
                    correlation_id=task_id,
                    details=task.error or {},
                ))
                if not self.store.save_worker_task(task_id, attempt, task):
                    lease_lost.set()
                    logger.warning("Discarding stale worker failure", extra={"task_id": task_id, "attempt": attempt})
                    return
            except Exception:
                logger.exception("Could not persist worker failure", extra={"task_id": task_id})
            self.store.finish_job(task_id, attempt, "CANCELLED" if cancelled else "FAILED", None if cancelled else redact_sensitive_text(error))
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
