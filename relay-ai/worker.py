"""Single-process worker that leases durable SQLite jobs and records pipeline progress."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from orchestrator import RelayOrchestrator
from schemas import EvidenceEvent, PipelineStatus, PipelineTaskResponse, ReportRequest
from storage import SQLiteTaskStore

logger = logging.getLogger("relay.worker")


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
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(15):
                try:
                    self.store.heartbeat_job(task_id)
                except Exception:
                    logger.exception("Failed to renew worker lease", extra={"task_id": task_id})

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"relay-lease-{task_id[:8]}", daemon=True)
        heartbeat_thread.start()
        pipeline_event_count = 0

        def persist_progress(task: PipelineTaskResponse) -> None:
            nonlocal pipeline_event_count
            previous = self.store[task_id]
            new_events = task.evidence_timeline[pipeline_event_count:]
            pipeline_event_count = len(task.evidence_timeline)
            task.created_at = previous.created_at
            task.evidence_timeline = [*previous.evidence_timeline, *new_events]
            self.store[task_id] = task

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
            if self.store.is_cancel_requested(task_id) and task.status != PipelineStatus.CANCELLED:
                task.status = PipelineStatus.CANCELLED
                task.current_stage = "CANCELLED"
                self.orchestrator._log_event(task, "CANCELLATION", "CANCELLED")
            self.store[task_id] = task
            if task.status == PipelineStatus.CANCELLED:
                self.store.finish_job(task_id, "CANCELLED")
            elif task.status == PipelineStatus.FAILED:
                self.store.finish_job(task_id, "FAILED", task.error.get("message") if task.error else "Pipeline failed")
            else:
                self.store.finish_job(task_id, "COMPLETED")
        except Exception as error:
            logger.exception("Pipeline worker failed", extra={"task_id": task_id})
            cancelled = self.store.is_cancel_requested(task_id)
            try:
                task = self.store[task_id]
                task.status = PipelineStatus.CANCELLED if cancelled else PipelineStatus.FAILED
                task.current_stage = task.status.value
                if not cancelled:
                    task.error = {
                        "failed_stage": "WORKER",
                        "error_type": error.__class__.__name__,
                        "message": str(error),
                        "is_quota_error": False,
                        "timestamp": self.orchestrator._now(),
                    }
                task.evidence_timeline.append(EvidenceEvent(
                    timestamp=self.orchestrator._now(),
                    stage="CANCELLATION" if cancelled else "WORKER",
                    status="CANCELLED" if cancelled else "FAILED",
                    details=task.error or {},
                ))
                self.store[task_id] = task
            except Exception:
                logger.exception("Could not persist worker failure", extra={"task_id": task_id})
            self.store.finish_job(task_id, "CANCELLED" if cancelled else "FAILED", None if cancelled else str(error))
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
