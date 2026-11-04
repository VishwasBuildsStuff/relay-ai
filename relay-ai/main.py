"""
RELAY AI — FastAPI Pipeline Service
Local API for autonomous bug remediation, bounded ZIP ingestion,
sandbox test execution, and human-reviewed task-copy Git operations.
"""

from __future__ import annotations

import io
import os
import secrets
import shutil
import subprocess
import sys
import uuid
import zipfile
import datetime
from pathlib import Path, PurePosixPath
from typing import Dict, List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.responses import JSONResponse

from orchestrator import RelayOrchestrator
from schemas import (
    EvidenceEvent,
    PipelineStatus,
    PipelineTaskResponse,
    ReportRequest,
    TaskActionRequest,
)
from storage import SQLiteTaskStore
from worker import PipelineWorker

tasks_db = SQLiteTaskStore(os.environ.get("RELAY_DB_PATH", str(Path(__file__).with_name("relay_tasks.sqlite3"))))
orchestrator = RelayOrchestrator()
pipeline_worker = PipelineWorker(tasks_db, orchestrator)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 25 * 1024 * 1024
APPLIED_ROOT = Path(os.environ.get("RELAY_APPLIED_ROOT", str(Path(__file__).with_name(".relay_applied"))))

app = FastAPI(
    title="RELAY AI Autonomous Engineering Pipeline",
    version="1.0.0",
    description="Transforms natural-language bug reports into verified, sandbox-tested Git patches with human approval.",
    lifespan=pipeline_worker.lifespan,
)

# Restrict browser origins; configure RELAY_CORS_ORIGINS for deployed clients.
cors_origins = [
    origin.strip()
    for origin in os.environ.get("RELAY_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if origin.strip()
]
API_TOKEN = os.environ.get("RELAY_API_TOKEN")


@app.middleware("http")
async def optional_api_token(request: Request, call_next):
    """Enforce a shared bearer token when configured for a non-local deployment."""
    if API_TOKEN and request.method != "OPTIONS":
        authorization = request.headers.get("Authorization", "")
        provided = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(provided, API_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Valid bearer token required."})
    return await call_next(request)


# Add CORS after the token middleware so token failures still carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _new_task(task_id: str, request: ReportRequest) -> PipelineTaskResponse:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    task = PipelineTaskResponse(
        task_id=task_id,
        status=PipelineStatus.RECEIVED,
        current_stage="QUEUED",
        bug_report=request.chat_message,
        created_at=now,
        updated_at=now,
        evidence_timeline=[],
    )
    task.evidence_timeline.append(EvidenceEvent(
        timestamp=now,
        stage="INGESTION",
        status="QUEUED",
        details={"files_count": len(request.codebase)},
    ))
    return task


@app.get("/")
def read_root():
    """Root status endpoint."""
    return {"status": "RELAY AI Pipeline active", "docs": "/docs"}


@app.get("/health")
def health_check():
    """System health check and runtime capabilities."""
    git_version = "unknown"
    try:
        git_res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=2)
        if git_res.returncode == 0:
            git_version = git_res.stdout.strip()
    except Exception:
        pass

    return {
        "status": "healthy",
        "python_version": sys.version.split()[0],
        "git_version": git_version,
        "docker_available": orchestrator.sandbox.docker_available,
        "sandbox_image_available": orchestrator.sandbox.sandbox_image_available,
        "queue": tasks_db.job_counts(),
        "active_tasks_count": sum(
            task.status not in (
                PipelineStatus.APPROVED, PipelineStatus.APPLIED, PipelineStatus.COMMITTED,
                PipelineStatus.REJECTED, PipelineStatus.CANCELLED, PipelineStatus.FAILED,
            )
            for task in (tasks_db[task_id] for task_id in tasks_db)
        ),
    }


@app.get("/ready")
def readiness_check():
    """Reports whether this instance can currently run the full remediation pipeline."""
    missing = []
    if orchestrator.provider is None:
        missing.append("GEMINI_API_KEY is not configured")
    if not orchestrator.sandbox.sandbox_image_available and not orchestrator.sandbox.allow_unsafe_subprocess:
        missing.append("isolated Docker sandbox image is unavailable")
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing": missing})
    return {"status": "ready"}


@app.post("/tasks/report", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_report_json(request: ReportRequest):
    """
    Programmatic endpoint: Accepts natural language bug description + dictionary of codebase files.
    Persists a job and returns immediately; the durable worker executes the pipeline.
    """
    task_id = str(uuid.uuid4())
    task = _new_task(task_id, request)
    tasks_db.create_task(task, request.codebase)
    return task


@app.post("/tasks/submit", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_repository_zip(
    bug_report: str = Form(..., description="Natural-language description of the defect."),
    file: UploadFile = File(..., description="ZIP archive containing the target Python repository."),
):
    """
    ZIP upload endpoint: Unpacks repository archive in-memory with zip-slip validation,
    extracts codebase files, and triggers the autonomous remediation pipeline.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip archives are supported.")

    codebase: Dict[str, str] = {}
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP upload exceeds the 25 MiB limit.")

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            members = z.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise HTTPException(status_code=413, detail="ZIP contains more than 500 entries.")
            expanded_size = 0
            seen_paths = set()
            for member in members:
                filename = member.filename.replace("\\", "/")
                normalized_name = filename.rstrip("/") if member.is_dir() else filename
                path = PurePosixPath(normalized_name)
                mode = (member.external_attr >> 16) & 0o170000
                if path.is_absolute() or any(part in ("..", ".", "") for part in normalized_name.split("/")):
                    raise HTTPException(status_code=400, detail="ZIP contains an unsafe path.")
                if member.is_dir():
                    continue
                if mode == 0o120000:
                    raise HTTPException(status_code=400, detail="ZIP symlinks are not supported.")
                if normalized_name in seen_paths:
                    raise HTTPException(status_code=400, detail="ZIP contains duplicate paths.")
                seen_paths.add(normalized_name)
                if member.file_size > MAX_ARCHIVE_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"ZIP member exceeds 2 MiB limit: {filename}")
                expanded_size += member.file_size
                if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail="ZIP expands beyond the 25 MiB limit.")

                # Read text files only (ignore binary / pycache)
                if any(part in {"__pycache__", ".git", ".pytest_cache", ".venv"} for part in path.parts):
                    continue

                try:
                    file_bytes = z.read(member)
                    codebase[normalized_name] = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    # Skip binary files safely
                    continue
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP archive.")

    if not codebase:
        raise HTTPException(status_code=400, detail="Archive contains no readable source code files.")

    report_req = ReportRequest(chat_message=bug_report, codebase=codebase)
    task_id = str(uuid.uuid4())
    task = _new_task(task_id, report_req)
    tasks_db.create_task(task, report_req.codebase)
    return task


@app.get("/tasks/{task_id}", response_model=PipelineTaskResponse)
def get_task(task_id: str):
    """Retrieves current task state, generated patch, test evidence, and review verdict."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    return tasks_db[task_id]


@app.get("/tasks/{task_id}/patch")
def download_task_patch(task_id: str):
    """Downloads the proposed unified diff for local inspection or application."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    task = tasks_db[task_id]
    if not task.investigation_patch or not task.investigation_patch.unified_diff:
        raise HTTPException(status_code=404, detail="Task does not have a proposed patch.")
    return Response(
        content=task.investigation_patch.unified_diff,
        media_type="text/x-diff",
        headers={"Content-Disposition": f'attachment; filename="relay-{task_id}.patch"'},
    )


@app.get("/tasks/{task_id}/events", response_model=List[EvidenceEvent])
def list_task_events(task_id: str, limit: int = 100, offset: int = 0):
    """Returns the append-only persisted task event history."""
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if limit < 1 or limit > 500 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be 1..500 and offset must be non-negative.")
    return tasks_db.get_events(task_id, limit=limit, offset=offset)


@app.get("/tasks", response_model=List[PipelineTaskResponse])
def list_tasks(limit: int = 50, offset: int = 0):
    """Lists recent task snapshots for the local dashboard."""
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be 1..100 and offset must be non-negative.")
    all_tasks = [tasks_db[task_id] for task_id in tasks_db]
    all_tasks.sort(key=lambda item: item.updated_at, reverse=True)
    return all_tasks[offset:offset + limit]


@app.post("/tasks/{task_id}/action", response_model=PipelineTaskResponse)
def handle_task_action(task_id: str, action_req: TaskActionRequest):
    """
    Decoupled Human-in-the-Loop Gateway:
    - 'approve': Authorizes a verified proposal.
    - 'reject': Rejects proposed patch.
    - 'apply': Applies to the durable, task-owned repository snapshot.
    - 'commit': Creates a real Git commit in that task-owned snapshot.
    - 'cancel': Cancels queued work or requests cancellation at a stage boundary.
    - 'retry': Re-queues failed work, up to three attempts.
    """
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    task = tasks_db[task_id]
    action = action_req.action

    if action == "cancel":
        job_status = tasks_db.cancel_job(task_id)
        if job_status is None:
            raise HTTPException(status_code=409, detail="Task has no cancellable pipeline job.")
        if job_status not in ("CANCELLED", "CANCEL_REQUESTED"):
            raise HTTPException(status_code=409, detail=f"Cannot cancel job in status '{job_status}'.")
        if job_status == "CANCELLED":
            task.status = PipelineStatus.CANCELLED
            task.current_stage = "CANCELLED"
        task.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        orchestrator._log_event(task, "CANCELLATION", job_status)
        tasks_db[task_id] = task
        return task

    if action == "retry":
        if task.status != PipelineStatus.FAILED:
            raise HTTPException(status_code=409, detail=f"Cannot retry task in status '{task.status}'.")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        reset_task = PipelineTaskResponse(
            task_id=task_id,
            status=PipelineStatus.RECEIVED,
            current_stage="QUEUED",
            bug_report=task.bug_report,
            created_at=task.created_at,
            updated_at=now,
            evidence_timeline=[
                *task.evidence_timeline,
                EvidenceEvent(timestamp=now, stage="RETRY", status="QUEUED"),
            ],
        )
        retried, retry_error = tasks_db.retry_job(task_id, reset_task)
        if not retried:
            raise HTTPException(status_code=409, detail=retry_error)
        return reset_task

    if action == "approve":
        if task.status not in (PipelineStatus.AWAITING_HUMAN_APPROVAL, PipelineStatus.REVIEWED):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve task in status '{task.status}'. Must be AWAITING_HUMAN_APPROVAL."
            )
        if not task.sandbox_result or not task.sandbox_result.tests_passed or not task.sandbox_result.applied_patch_cleanly:
            raise HTTPException(
                status_code=400,
                detail="Cannot approve without a cleanly applied patch and passing sandbox tests."
            )
        if not task.reviewer or task.reviewer.security_assessment != "PASS" or task.reviewer.recommendation != "APPROVE":
            raise HTTPException(
                status_code=400,
                detail="Cannot approve unless the reviewer reports security PASS and recommends APPROVE."
            )
        task.is_approved = True
        task.status = PipelineStatus.APPROVED
        task.current_stage = "APPROVED"
        orchestrator._log_event(task, "HUMAN_APPROVAL", "APPROVED", {"notes": action_req.notes})

    elif action == "reject":
        if task.status != PipelineStatus.AWAITING_HUMAN_APPROVAL:
            raise HTTPException(status_code=400, detail=f"Cannot reject task in status '{task.status}'.")
        task.is_approved = False
        task.status = PipelineStatus.REJECTED
        task.current_stage = "REJECTED"
        orchestrator._log_event(task, "HUMAN_APPROVAL", "REJECTED", {"notes": action_req.notes})

    elif action == "apply":
        if not task.is_approved:
            raise HTTPException(
                status_code=400,
                detail="Cannot apply patch to working copy before human approval."
            )
        if not task.investigation_patch or not task.investigation_patch.unified_diff:
            raise HTTPException(status_code=400, detail="No proposed patch exists to apply.")
        if task.is_applied:
            raise HTTPException(status_code=409, detail="Patch has already been applied to this task workspace.")
        codebase = tasks_db.get_snapshot(task_id)
        if not codebase:
            raise HTTPException(status_code=409, detail="The task source snapshot is unavailable.")

        target_dir = APPLIED_ROOT / task_id
        if target_dir.exists():
            raise HTTPException(status_code=409, detail="A task workspace already exists at this location.")
        target_created = False
        try:
            in_scope, scope_error = orchestrator.sandbox.validate_patch_scope(
                task.investigation_patch.unified_diff,
                task.investigation_patch.files_changed,
            )
            if not in_scope:
                raise HTTPException(status_code=400, detail=f"Patch rejected by scope validation: {scope_error}")
            APPLIED_ROOT.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(exist_ok=False)
            target_created = True
            orchestrator.sandbox._populate_workspace(target_dir, codebase)
            orchestrator.sandbox._init_git_repo(target_dir)
            valid, patch_error = orchestrator.sandbox.validate_patch_syntax(
                target_dir, task.investigation_patch.unified_diff
            )
            if not valid:
                raise HTTPException(status_code=400, detail=f"Patch no longer applies cleanly: {patch_error}")
            applied, patch_error = orchestrator.sandbox.apply_patch(
                target_dir, task.investigation_patch.unified_diff
            )
            if not applied:
                raise HTTPException(status_code=400, detail=f"Patch application failed: {patch_error}")
        except HTTPException:
            if target_created:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        except Exception as error:
            if target_created:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Could not prepare task workspace: {error}") from error

        task.is_applied = True
        task.status = PipelineStatus.APPLIED
        task.current_stage = "PATCH_APPLIED_TO_TASK_WORKSPACE"
        orchestrator._log_event(task, "REPOSITORY_APPLY", "APPLIED", {
            "files": task.investigation_patch.files_changed,
            "workspace": "task-owned repository snapshot",
        })

    elif action == "commit":
        if not task.is_applied:
            raise HTTPException(
                status_code=400,
                detail="Cannot commit patch before it has been applied to repository."
            )
        if task.is_committed:
            raise HTTPException(status_code=409, detail="Task workspace already has a commit.")
        target_dir = APPLIED_ROOT / task_id
        commit_msg = action_req.commit_message or (
            f"fix({task.investigation_patch.buggy_file}): {task.triage.title if task.triage else 'auto remediation'}\n\n"
            f"Approved-By: Human Reviewer\nTask-ID: {task.task_id}"
        )
        try:
            subprocess.run(["git", "add", "--all"], cwd=target_dir, capture_output=True, text=True, timeout=10, check=True)
            subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", commit_msg],
                cwd=target_dir, capture_output=True, text=True, timeout=15, check=True,
            )
            commit_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target_dir, capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise HTTPException(status_code=500, detail=f"Git commit failed: {error}") from error
        task.is_committed = True
        task.status = PipelineStatus.COMMITTED
        task.commit_hash = commit_hash
        task.commit_message = commit_msg
        task.current_stage = "COMMITTED"
        orchestrator._log_event(task, "GIT_COMMIT", "COMMITTED", {
            "commit_hash": commit_hash,
            "commit_message": commit_msg,
        })

    tasks_db[task_id] = task
    return task


@app.get("/scenarios")
def get_benchmark_scenarios():
    """
    Returns 3 standardized benchmark bug scenarios for instant hackathon demonstrations:
    1. Off-by-one index error in payment tier calculator (passes tests when fixed).
    2. Regression bug with failing test assertions.
    3. Security vulnerability (e.g. unsafe eval).
    """
    return [
        {
            "id": "scenario_off_by_one",
            "name": "Scenario 1: Off-by-One Tier Index Crash",
            "description": "IndexError when user requests the highest tier limit in rate_limiter.py",
            "category": "functional_bug",
            "bug_report": "The rate limiter raises IndexError: list index out of range when checking tier 3 users at maximum capacity.",
            "codebase": {
                "rate_limiter.py": (
                    "TIER_LIMITS = [100, 500, 1000, 5000]\n\n"
                    "def get_tier_limit(tier_index: int) -> int:\n"
                    "    # BUG: Off-by-one boundary check <= instead of <\n"
                    "    if tier_index <= len(TIER_LIMITS):\n"
                    "        return TIER_LIMITS[tier_index]\n"
                    "    return TIER_LIMITS[0]\n"
                ),
                "test_rate_limiter.py": (
                    "from rate_limiter import get_tier_limit, TIER_LIMITS\n\n"
                    "def test_valid_tiers():\n"
                    "    assert get_tier_limit(0) == 100\n"
                    "    assert get_tier_limit(1) == 500\n"
                    "    assert get_tier_limit(2) == 1000\n"
                    "    assert get_tier_limit(3) == 5000\n\n"
                    "def test_out_of_bounds_fallback():\n"
                    "    assert get_tier_limit(4) == 100\n"
                    "    assert get_tier_limit(99) == 100\n"
                ),
            },
        },
        {
            "id": "scenario_tax_rounding",
            "name": "Scenario 2: Floating Point Invoice Rounding Error",
            "description": "Invoice item tax calculation accumulates precision error instead of decimal rounding.",
            "category": "edge_case_defect",
            "bug_report": "Tax calculations on multi-item invoices do not round to 2 decimal places, causing bank settlement mismatch.",
            "codebase": {
                "invoicing.py": (
                    "def calculate_total_with_tax(subtotal: float, tax_rate: float) -> float:\n"
                    "    # BUG: Missing round to 2 decimal places\n"
                    "    return subtotal + (subtotal * tax_rate)\n"
                ),
                "test_invoicing.py": (
                    "from invoicing import calculate_total_with_tax\n\n"
                    "def test_invoice_calculation():\n"
                    "    assert calculate_total_with_tax(10.05, 0.0825) == 10.88\n"
                    "    assert calculate_total_with_tax(100.0, 0.05) == 105.00\n"
                ),
            },
        },
        {
            "id": "scenario_unsafe_eval",
            "name": "Scenario 3: Security Regression (Unsafe Expression Evaluation)",
            "description": "Math expression parser uses eval() allowing arbitrary code execution.",
            "category": "security_vulnerability",
            "bug_report": "Security vulnerability: expression parser uses unsafe python eval() on user input.",
            "codebase": {
                "calculator.py": (
                    "def evaluate_math_expression(expr: str) -> float:\n"
                    "    # SECURITY RISK: eval used on untrusted input\n"
                    "    return float(eval(expr))\n"
                ),
                "test_calculator.py": (
                    "from calculator import evaluate_math_expression\n\n"
                    "def test_math_safe():\n"
                    "    assert evaluate_math_expression('2 + 2') == 4.0\n"
                    "    assert evaluate_math_expression('10 * 5') == 50.0\n"
                ),
            },
        },
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
