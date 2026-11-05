"""
RELAY AI — FastAPI Pipeline Service
Local API for autonomous bug remediation, bounded ZIP ingestion,
sandbox test execution, and human-reviewed task-copy Git operations.
"""

from __future__ import annotations

import io
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import stat
import uuid
import zipfile
import datetime
import json
import ipaddress
import re
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
from observability import configure_logging
from redaction import redact_sensitive_text
from path_utils import normalize_relative_path, normalize_snapshot_paths
from repository_targets import RepositoryTargetError, RepositoryTargetRegistry

configure_logging()

tasks_db = SQLiteTaskStore(os.environ.get("RELAY_DB_PATH", str(Path(__file__).with_name("relay_tasks.sqlite3"))))
orchestrator = RelayOrchestrator()
pipeline_worker = PipelineWorker(tasks_db, orchestrator)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 25 * 1024 * 1024
MAX_JSON_REQUEST_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
MAX_OTHER_REQUEST_BYTES = 1024 * 1024
APPLIED_ROOT = Path(os.environ.get("RELAY_APPLIED_ROOT", str(Path(__file__).with_name(".relay_applied"))))
REPOSITORY_TARGETS = RepositoryTargetRegistry(
    os.environ.get("RELAY_REPOSITORIES_JSON"), lock_root=APPLIED_ROOT / ".target-locks"
)

app = FastAPI(
    title="RELAY AI Autonomous Engineering Pipeline",
    version="1.0.0",
    description="Transforms natural-language bug reports into verified, sandbox-tested Git patches with human approval.",
    lifespan=pipeline_worker.lifespan,
)


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before FastAPI buffers and parses request content."""

    def __init__(self, application: ASGIApp):
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            await self.application(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_type = headers.get(b"content-type", b"").lower()
        path = scope.get("path", "")
        if path.endswith(("/upload", "/tasks/submit")) and content_type.startswith(b"multipart/form-data"):
            max_bytes = MAX_MULTIPART_REQUEST_BYTES
        elif content_type.startswith(b"application/json"):
            max_bytes = MAX_JSON_REQUEST_BYTES
        else:
            max_bytes = MAX_OTHER_REQUEST_BYTES

        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    response = JSONResponse(status_code=413, content={"detail": f"Request body exceeds the {max_bytes}-byte limit."})
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})
                await response(scope, receive, send)
                return

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_bytes:
                    raise HTTPException(status_code=413, detail=f"Request body exceeds the {max_bytes}-byte limit.")
            return message

        await self.application(scope, limited_receive, send)

# Restrict browser origins; configure RELAY_CORS_ORIGINS for deployed clients.
cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "RELAY_CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
RELAY_ENV = os.environ.get("RELAY_ENV", "development").strip().lower()
if RELAY_ENV not in {"development", "production"}:
    raise RuntimeError("RELAY_ENV must be either 'development' or 'production'.")
API_TOKEN = os.environ.get("RELAY_API_TOKEN")
try:
    AUTH_USERS = json.loads(os.environ.get("RELAY_AUTH_USERS_JSON", "{}"))
except json.JSONDecodeError as error:
    raise RuntimeError("RELAY_AUTH_USERS_JSON must contain a JSON object mapping usernames to bearer tokens.") from error
if not isinstance(AUTH_USERS, dict) or any(
    not isinstance(username, str) or not username or len(username) > 100
    or not isinstance(token, str) or not token or len(token) > 4096
    for username, token in AUTH_USERS.items()
):
    raise RuntimeError("RELAY_AUTH_USERS_JSON must map non-empty usernames to non-empty bearer tokens.")
if len(set(AUTH_USERS.values())) != len(AUTH_USERS):
    raise RuntimeError("RELAY_AUTH_USERS_JSON must not assign the same bearer token to multiple usernames.")
_auth_roles_configured = "RELAY_AUTH_ROLES_JSON" in os.environ
try:
    AUTH_ROLES = json.loads(os.environ.get("RELAY_AUTH_ROLES_JSON", "{}"))
except json.JSONDecodeError as error:
    raise RuntimeError("RELAY_AUTH_ROLES_JSON must contain a JSON object mapping usernames to roles.") from error
if not isinstance(AUTH_ROLES, dict) or any(
    not isinstance(username, str) or username not in AUTH_USERS
    or not isinstance(role, str) or role not in {"viewer", "operator"}
    for username, role in AUTH_ROLES.items()
):
    raise RuntimeError("RELAY_AUTH_ROLES_JSON must map configured usernames to 'viewer' or 'operator'.")
if RELAY_ENV == "production":
    if not (API_TOKEN or AUTH_USERS):
        raise RuntimeError("Production mode requires RELAY_API_TOKEN or RELAY_AUTH_USERS_JSON.")
    if "RELAY_CORS_ORIGINS" not in os.environ:
        raise RuntimeError("Production mode requires an explicit RELAY_CORS_ORIGINS allow-list.")
    if "*" in cors_origins:
        raise RuntimeError("Production mode does not allow wildcard CORS origins.")
    for origin in cors_origins:
        try:
            parsed_origin = urlsplit(origin)
            parsed_origin.port  # Validate malformed ports before accepting the origin.
        except ValueError as error:
            raise RuntimeError(f"Invalid production CORS origin: {origin!r}.") from error
        if (
            "*" in origin
            or any(character.isspace() for character in origin)
            or parsed_origin.scheme.lower() != "https"
            or not parsed_origin.hostname
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username is not None
            or parsed_origin.password is not None
        ):
            raise RuntimeError(
                "Production CORS origins must be bare HTTPS origins without paths, credentials, or whitespace."
            )
    if os.environ.get("RELAY_ALLOW_UNSAFE_SUBPROCESS", "false").strip().lower() == "true":
        raise RuntimeError("Production mode cannot enable RELAY_ALLOW_UNSAFE_SUBPROCESS.")
    sandbox_image = os.environ.get("RELAY_SANDBOX_IMAGE", "").strip()
    if not re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[^@\s]+@sha256:[0-9a-f]{64})", sandbox_image):
        raise RuntimeError(
            "Production mode requires RELAY_SANDBOX_IMAGE to be a SHA-256 image ID or repository digest."
        )


@app.middleware("http")
async def optional_api_token(request: Request, call_next):
    """Enforce a shared bearer token when configured for a non-local deployment."""
    if request.method != "OPTIONS" and AUTH_USERS:
        authorization = request.headers.get("Authorization", "")
        provided = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        matched_user = next(
            (username for username, expected in AUTH_USERS.items() if secrets.compare_digest(provided, expected)),
            None,
        )
        if matched_user is None:
            return JSONResponse(status_code=401, content={"detail": "Valid bearer token required."})
        request.state.actor = matched_user
        request.state.role = AUTH_ROLES.get(
            matched_user, "viewer" if _auth_roles_configured else "operator"
        )
    elif API_TOKEN and request.method != "OPTIONS":
        authorization = request.headers.get("Authorization", "")
        provided = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(provided, API_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Valid bearer token required."})
        request.state.actor = "shared-token"
        request.state.role = "operator"
    elif request.method != "OPTIONS":
        request.state.actor = "local-operator"
        request.state.role = "operator"
    if request.method not in {"GET", "HEAD", "OPTIONS"} and getattr(request.state, "role", "operator") != "operator":
        return JSONResponse(status_code=403, content={"detail": "This account has read-only access."})
    if request.method == "POST":
        if request.url.path in {"/tasks/report", "/tasks/submit", "/api/v1/tasks", "/api/v1/tasks/upload"}:
            bucket_name, limit = "task-submit", 10
        elif request.url.path.endswith(("/action", "/actions")):
            bucket_name, limit = "task-action", 60
        else:
            bucket_name, limit = None, 0
        if bucket_name:
            client_host = request.client.host if request.client else "unknown"
            client_digest = hashlib.blake2b(client_host.encode("utf-8"), digest_size=16).hexdigest()
            allowed, retry_after = tasks_db.consume_rate_limit(
                f"{client_digest}:{bucket_name}", limit=limit, window_seconds=60
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Request rate limit exceeded. Try again shortly."},
                    headers={"Retry-After": str(retry_after)},
                )
    return await call_next(request)


# Add CORS after the token middleware so token failures still carry CORS headers.
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

def _new_task(task_id: str, request: ReportRequest, owner_id: str) -> PipelineTaskResponse:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    task = PipelineTaskResponse(
        task_id=task_id,
        owner_id=owner_id,
        status=PipelineStatus.QUEUED,
        current_stage="QUEUED",
        bug_report=request.chat_message,
        created_at=now,
        updated_at=now,
        evidence_timeline=[],
    )
    if request.repository_id:
        try:
            repository_state = REPOSITORY_TARGETS.require_clean_base(request.repository_id)
        except RepositoryTargetError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        task.repository_id = repository_state.repository_id
        task.repository_base_revision = repository_state.revision
    task.evidence_timeline.append(EvidenceEvent(
        timestamp=now,
        stage="INGESTION",
        status="QUEUED",
        actor=owner_id,
        correlation_id=task_id,
        details={"files_count": len(request.codebase)},
    ))
    return task


@app.get("/")
def read_root():
    """Root status endpoint."""
    return {"status": "RELAY AI Pipeline active", "docs": "/docs"}


@app.get("/api/v1/session")
def get_session(request: Request):
    """Returns the authenticated actor and capabilities for the dashboard."""
    return {"actor": request.state.actor, "role": request.state.role}


@app.get("/api/v1/repositories")
def get_repository_targets(request: Request):
    """List operator-configured local targets without exposing their filesystem paths."""
    if request.state.role != "operator":
        return []
    try:
        return REPOSITORY_TARGETS.list_targets()
    except RepositoryTargetError as error:
        raise HTTPException(status_code=503, detail="A configured repository target could not be inspected.") from error


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
        "database_schema_version": tasks_db.schema_version(),
        "python_version": sys.version.split()[0],
        "git_version": git_version,
        "docker_available": orchestrator.sandbox.docker_available,
        "sandbox_image_available": orchestrator.sandbox.sandbox_image_available,
        "worker_running": pipeline_worker.is_running,
        "queue": tasks_db.job_counts(),
        "active_tasks_count": sum(
            task.status not in (
                PipelineStatus.APPROVED, PipelineStatus.APPLIED, PipelineStatus.COMMITTED,
                PipelineStatus.REJECTED, PipelineStatus.CANCELLED, PipelineStatus.FAILED,
            )
            for task in (tasks_db[task_id] for task_id in tasks_db)
        ),
    }


@app.get("/metrics", include_in_schema=False)
def metrics():
    """Expose a small Prometheus-compatible operational snapshot."""
    queue_counts = tasks_db.job_counts()
    task_counts: Dict[str, int] = {}
    for task_id in tasks_db:
        status_name = tasks_db[task_id].status.value.lower()
        task_counts[status_name] = task_counts.get(status_name, 0) + 1
    lines = [
        "# HELP relay_pipeline_jobs Current durable jobs by queue status.",
        "# TYPE relay_pipeline_jobs gauge",
    ]
    lines.extend(
        f'relay_pipeline_jobs{{status="{name}"}} {count}'
        for name, count in sorted(queue_counts.items())
    )
    lines.extend([
        "# HELP relay_tasks Current persisted tasks by pipeline status.",
        "# TYPE relay_tasks gauge",
    ])
    lines.extend(
        f'relay_tasks{{status="{name}"}} {count}'
        for name, count in sorted(task_counts.items())
    )
    stage_rows, sandbox_rows = tasks_db.stage_metrics()
    lines.extend([
        "# HELP relay_pipeline_stage_duration_ms Duration of completed or failed pipeline stages.",
        "# TYPE relay_pipeline_stage_duration_ms histogram",
        "# HELP relay_pipeline_stage_failures_total Failed pipeline stage executions.",
        "# TYPE relay_pipeline_stage_failures_total counter",
    ])
    histogram_bounds = ((100, "100"), (500, "500"), (1000, "1000"), (5000, "5000"), (15000, "15000"), (60000, "60000"), (None, "+Inf"))
    stage_data = {row["stage"]: row for row in stage_rows}
    for stage_name in sorted(set(stage_data) | {
        "STAGE_A_TRIAGE", "STAGE_B_INVESTIGATION_AND_DEV", "SANDBOX_TEST_EXECUTION", "STAGE_C_REVIEW"
    }):
        row = stage_data.get(stage_name, {"count": 0, "failures": 0, "duration_sum": 0, **{f"le_{bound}": 0 for bound, _ in histogram_bounds if bound is not None}})
        stage_label = str(stage_name).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        for bound, label in histogram_bounds:
            value = row["count"] if bound is None else row[f"le_{bound}"]
            lines.append(f'relay_pipeline_stage_duration_ms_bucket{{stage="{stage_label}",le="{label}"}} {value}')
        lines.extend([
            f'relay_pipeline_stage_duration_ms_sum{{stage="{stage_label}"}} {row["duration_sum"] or 0}',
            f'relay_pipeline_stage_duration_ms_count{{stage="{stage_label}"}} {row["count"]}',
            f'relay_pipeline_stage_failures_total{{stage="{stage_label}"}} {row["failures"]}',
        ])
    lines.extend([
        "# HELP relay_sandbox_executions_total Sandbox runs by isolation mode and outcome.",
        "# TYPE relay_sandbox_executions_total counter",
    ])
    sandbox_data = {(row["mode"], row["outcome"]): row["count"] for row in sandbox_rows}
    sandbox_labels = {(mode, outcome) for mode in ("docker", "subprocess_dev", "unavailable") for outcome in ("passed", "failed")}
    sandbox_labels.update(sandbox_data)
    lines.extend(
        f'relay_sandbox_executions_total{{mode="{mode}",outcome="{outcome}"}} {sandbox_data.get((mode, outcome), 0)}'
        for mode, outcome in sorted(sandbox_labels)
    )
    attempt_histogram, attempt_outcomes = tasks_db.job_attempt_metrics()
    lines.extend([
        "# HELP relay_job_queue_wait_ms Time from enqueue or retry readiness to worker claim.",
        "# TYPE relay_job_queue_wait_ms histogram",
    ])
    for bound, label in histogram_bounds:
        value = attempt_histogram["count"] if bound is None else attempt_histogram[f"le_{bound}"]
        lines.append(f'relay_job_queue_wait_ms_bucket{{le="{label}"}} {value or 0}')
    lines.extend([
        f'relay_job_queue_wait_ms_sum {attempt_histogram["wait_sum"] or 0}',
        f'relay_job_queue_wait_ms_count {attempt_histogram["count"]}',
        "# HELP relay_job_attempts Durable worker attempts by final outcome or running state.",
        "# TYPE relay_job_attempts gauge",
    ])
    attempt_data = {row["outcome"]: row["count"] for row in attempt_outcomes}
    lines.extend(
        f'relay_job_attempts{{outcome="{outcome}"}} {attempt_data.get(outcome, 0)}'
        for outcome in sorted(set(attempt_data) | {"running", "completed", "failed", "cancelled", "lease_expired"})
    )
    usage_totals = tasks_db.provider_usage_totals()
    lines.extend([
        "# HELP relay_gemini_usage_tokens Reported Gemini token usage retained with tasks.",
        "# TYPE relay_gemini_usage_tokens gauge",
    ])
    lines.extend(
        f'relay_gemini_usage_tokens{{kind="{kind}"}} {count}'
        for kind, count in usage_totals.items()
    )
    cost_totals = tasks_db.provider_cost_totals()
    lines.extend([
        "# HELP relay_gemini_estimated_cost_usd Estimated retained Gemini cost using operator-configured token rates.",
        "# TYPE relay_gemini_estimated_cost_usd gauge",
        f"relay_gemini_estimated_cost_usd {cost_totals['estimated_cost_usd']}",
        "# HELP relay_gemini_usage_events Gemini usage records retained by pricing configuration state.",
        "# TYPE relay_gemini_usage_events gauge",
        f'relay_gemini_usage_events{{pricing="configured"}} {cost_totals["priced_events"]}',
        f'relay_gemini_usage_events{{pricing="unconfigured"}} {cost_totals["unpriced_events"]}',
    ])
    lines.extend([
        "# HELP relay_worker_running Whether the local pipeline worker thread is active.",
        "# TYPE relay_worker_running gauge",
        f"relay_worker_running {1 if pipeline_worker.is_running else 0}",
        "",
    ])
    return Response("\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/ready")
def readiness_check():
    """Reports whether this instance can currently run the full remediation pipeline."""
    missing = []
    try:
        schema_version = tasks_db.schema_version()
        if schema_version != tasks_db.SCHEMA_VERSION:
            missing.append(f"SQLite schema version {schema_version} does not match expected {tasks_db.SCHEMA_VERSION}")
    except Exception:
        missing.append("SQLite task store is unavailable")
    if not pipeline_worker.is_running:
        missing.append("pipeline worker is not running")
    if orchestrator.provider is None:
        missing.append("GEMINI_API_KEY is not configured")
    if not orchestrator.sandbox.sandbox_image_available and not orchestrator.sandbox.allow_unsafe_subprocess:
        missing.append("isolated Docker sandbox image is unavailable")
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing": missing})
    return {"status": "ready"}


def _task_for_actor(task_id: str, request: Request) -> PipelineTaskResponse:
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    task = tasks_db[task_id]
    owner_id = task.owner_id
    actor = request.state.actor
    if owner_id is None:
        if AUTH_USERS:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    elif owner_id != actor:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    return task


def _persist_task_operation(task: PipelineTaskResponse) -> None:
    if not tasks_db.update_task_if_present(task):
        raise HTTPException(status_code=404, detail="Task was removed before the repository operation could continue.")


@app.post("/api/v1/tasks", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED)
@app.post("/tasks/report", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def submit_report_json(payload: ReportRequest, request: Request):
    """
    Programmatic endpoint: Accepts natural language bug description + dictionary of codebase files.
    Persists a job and returns immediately; the durable worker executes the pipeline.
    """
    task_id = str(uuid.uuid4())
    task = _new_task(task_id, payload, request.state.actor)
    tasks_db.create_task(task, payload.codebase)
    return task


@app.post("/api/v1/tasks/upload", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED)
@app.post("/tasks/submit", response_model=PipelineTaskResponse, status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def submit_repository_zip(
    request: Request,
    bug_report: str = Form(..., description="Natural-language description of the defect."),
    file: UploadFile = File(..., description="ZIP archive containing the target Python repository."),
    repository_id: Optional[str] = Form(None, description="Optional configured local Git repository target."),
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
            actual_expanded_size = 0
            seen_archive_entries = set()
            archive_file_paths = []
            for member in members:
                filename = member.filename.replace("\\", "/")
                mode = (member.external_attr >> 16) & 0o170000
                is_directory = member.is_dir()
                archive_path = filename.rstrip("/") if is_directory else filename
                try:
                    normalized_name, path_identity = normalize_relative_path(archive_path)
                except ValueError as error:
                    raise HTTPException(status_code=400, detail="ZIP contains an unsafe or non-portable path.") from error
                if path_identity in seen_archive_entries:
                    raise HTTPException(status_code=400, detail="ZIP contains duplicate normalized paths.")
                seen_archive_entries.add(path_identity)
                if is_directory:
                    if mode not in (0, stat.S_IFDIR):
                        raise HTTPException(status_code=400, detail="ZIP contains an unsupported directory entry type.")
                    continue
                if mode not in (0, stat.S_IFREG):
                    raise HTTPException(status_code=400, detail="ZIP contains an unsupported special file type.")
                archive_file_paths.append(normalized_name)
                if member.file_size > MAX_ARCHIVE_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"ZIP member exceeds 2 MiB limit: {filename}")
                expanded_size += member.file_size
                if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail="ZIP expands beyond the 25 MiB limit.")

                # Read text files only (ignore binary / pycache)
                path = PurePosixPath(normalized_name)
                if any(part.casefold() in {"__pycache__", ".git", ".pytest_cache", ".venv"} for part in path.parts):
                    continue

                try:
                    with z.open(member) as member_stream:
                        file_bytes = member_stream.read(MAX_ARCHIVE_FILE_BYTES + 1)
                    if len(file_bytes) > MAX_ARCHIVE_FILE_BYTES:
                        raise HTTPException(status_code=413, detail=f"ZIP member exceeds 2 MiB limit: {filename}")
                    if len(file_bytes) != member.file_size:
                        raise HTTPException(status_code=400, detail=f"ZIP member size is inconsistent: {filename}")
                    actual_expanded_size += len(file_bytes)
                    if actual_expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise HTTPException(status_code=413, detail="ZIP expands beyond the 25 MiB limit.")
                    codebase[normalized_name] = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    # Skip binary files safely
                    continue
            try:
                normalize_snapshot_paths({path: "" for path in archive_file_paths})
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP archive.")

    if not codebase:
        raise HTTPException(status_code=400, detail="Archive contains no readable source code files.")

    report_req = ReportRequest(chat_message=bug_report, codebase=codebase, repository_id=repository_id)
    task_id = str(uuid.uuid4())
    task = _new_task(task_id, report_req, request.state.actor)
    tasks_db.create_task(task, report_req.codebase)
    return task


@app.get("/api/v1/tasks/{task_id}", response_model=PipelineTaskResponse)
@app.get("/tasks/{task_id}", response_model=PipelineTaskResponse, include_in_schema=False)
def get_task(task_id: str, request: Request):
    """Retrieves current task state, generated patch, test evidence, and review verdict."""
    return _task_for_actor(task_id, request)


@app.get("/api/v1/tasks/{task_id}/patch")
@app.get("/tasks/{task_id}/patch", include_in_schema=False)
def download_task_patch(task_id: str, request: Request):
    """Downloads the proposed unified diff for local inspection or application."""
    task = _task_for_actor(task_id, request)
    if not task.investigation_patch or not task.investigation_patch.unified_diff:
        raise HTTPException(status_code=404, detail="Task does not have a proposed patch.")
    return Response(
        content=task.investigation_patch.unified_diff,
        media_type="text/x-diff",
        headers={"Content-Disposition": f'attachment; filename="relay-{task_id}.patch"'},
    )


@app.get(
    "/api/v1/tasks/{task_id}/events",
    response_model=List[EvidenceEvent],
    responses={200: {"headers": {"X-Total-Count": {"schema": {"type": "integer"}, "description": "Total persisted events for this task."}}}},
)
@app.get("/tasks/{task_id}/events", response_model=List[EvidenceEvent], include_in_schema=False)
def list_task_events(task_id: str, request: Request, response: Response, limit: int = 100, offset: int = 0):
    """Returns the append-only persisted task event history."""
    _task_for_actor(task_id, request)
    if limit < 1 or limit > 500 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be 1..500 and offset must be non-negative.")
    response.headers["X-Total-Count"] = str(tasks_db.count_events(task_id))
    return tasks_db.get_events(task_id, limit=limit, offset=offset)


@app.get(
    "/api/v1/tasks/{task_id}/actions",
    responses={200: {"headers": {"X-Total-Count": {"schema": {"type": "integer"}, "description": "Total persisted operator actions for this task."}}}},
)
@app.get("/tasks/{task_id}/actions", include_in_schema=False)
def list_task_actions(task_id: str, request: Request, response: Response, limit: int = 100, offset: int = 0):
    """Returns the append-only operator action audit for a task."""
    _task_for_actor(task_id, request)
    if limit < 1 or limit > 500 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be 1..500 and offset must be non-negative.")
    response.headers["X-Total-Count"] = str(tasks_db.count_actions(task_id))
    return tasks_db.get_actions(task_id, limit=limit, offset=offset)


@app.get(
    "/api/v1/tasks",
    response_model=List[PipelineTaskResponse],
    responses={200: {"headers": {"X-Total-Count": {"schema": {"type": "integer"}, "description": "Total owner-visible tasks matching the filters."}}}},
)
@app.get("/tasks", response_model=List[PipelineTaskResponse], include_in_schema=False)
def list_tasks(request: Request, response: Response, limit: int = 50, offset: int = 0, q: str | None = None, status: PipelineStatus | None = None):
    """Lists recent task snapshots for the local dashboard."""
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be 1..100 and offset must be non-negative.")
    search = q.strip() if q else None
    if search and len(search) > 100:
        raise HTTPException(status_code=400, detail="q must be at most 100 characters.")
    actor = request.state.actor
    include_legacy = not AUTH_USERS
    response.headers["X-Total-Count"] = str(tasks_db.count_tasks_matching(
        owner_id=actor, include_legacy=include_legacy, search=search, status=status.value if status else None,
    ))
    return tasks_db.list_tasks_page(
        owner_id=actor,
        include_legacy=include_legacy,
        limit=limit,
        offset=offset,
        search=search,
        status=status.value if status else None,
    )


@app.delete("/api/v1/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
@app.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def delete_task(task_id: str, request: Request):
    """Permanently delete an owned, non-running task and its task-owned repository copy."""
    task = _task_for_actor(task_id, request)
    if task.repository_id and (
        task.is_applied or task.repository_operation_state in {"APPLYING", "APPLIED", "COMMITTING", "COMMITTED"}
    ):
        raise HTTPException(
            status_code=409,
            detail="This task has changed a configured repository. Keep its audit record; deleting the task cannot undo that repository operation.",
        )
    job = tasks_db.get_job(task_id)
    if job and job["status"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
        raise HTTPException(status_code=409, detail="Cancel the active pipeline and wait for it to stop before deleting the task.")

    APPLIED_ROOT.mkdir(parents=True, exist_ok=True)
    target_dir = (APPLIED_ROOT / task_id).resolve()
    applied_root = APPLIED_ROOT.resolve()
    if not target_dir.is_relative_to(applied_root):
        raise HTTPException(status_code=400, detail="Task workspace path is invalid.")
    trash_dir = APPLIED_ROOT / f".deleting-{uuid.uuid4().hex}"
    moved_workspace = False
    if target_dir.exists():
        os.replace(target_dir, trash_dir)
        moved_workspace = True
    try:
        if not tasks_db.delete_task_if_stopped(task_id):
            raise HTTPException(
                status_code=409,
                detail="Task became active while deletion was starting; its workspace was restored. Cancel it and wait for it to stop.",
            )
    except Exception:
        if moved_workspace and trash_dir.exists():
            os.replace(trash_dir, target_dir)
        raise
    if moved_workspace:
        shutil.rmtree(trash_dir, ignore_errors=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/tasks/{task_id}/actions", response_model=PipelineTaskResponse)
@app.post("/tasks/{task_id}/action", response_model=PipelineTaskResponse, include_in_schema=False)
def handle_task_action(task_id: str, action_req: TaskActionRequest, request: Request):
    """
    Decoupled Human-in-the-Loop Gateway:
    - 'approve': Authorizes a verified proposal.
    - 'reject': Rejects proposed patch.
    - 'apply': Applies to the durable, task-owned repository snapshot.
    - 'commit': Creates a real Git commit in that task-owned snapshot.
    - 'cancel': Cancels queued work or requests cancellation at a stage boundary.
    - 'retry': Re-queues failed work, up to three attempts.
    """
    task = _task_for_actor(task_id, request)
    action = action_req.action
    idempotency_key = request.headers.get("Idempotency-Key")
    request_hash = hashlib.sha256(action_req.model_dump_json().encode("utf-8")).hexdigest()
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key):
            raise HTTPException(status_code=400, detail="Idempotency-Key must be 1..128 printable ASCII characters.")
        previous_action = tasks_db.get_action_by_idempotency_key(task_id, idempotency_key)
        if previous_action:
            if (
                previous_action["actor"] != request.state.actor
                or previous_action["action"] != action
                or previous_action["request_hash"] != request_hash
            ):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different action request.")
            if previous_action["response_json"]:
                return PipelineTaskResponse.model_validate_json(previous_action["response_json"])
            return tasks_db[task_id]

    if action == "cancel":
        job_status = tasks_db.cancel_job(task_id)
        if job_status is None:
            raise HTTPException(status_code=409, detail="Task has no cancellable pipeline job.")
        if job_status not in ("CANCELLED", "CANCEL_REQUESTED"):
            raise HTTPException(status_code=409, detail=f"Cannot cancel job in status '{job_status}'.")
        if job_status == "CANCELLED":
            task.transition_to(PipelineStatus.CANCELLED)
            task.current_stage = "CANCELLED"
            task.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            orchestrator._log_event(task, "CANCELLATION", job_status, actor=request.state.actor)
            tasks_db[task_id] = task
        else:
            task = tasks_db[task_id]
        tasks_db.record_action(
            task_id, action, request.state.actor, job_status, action_req.notes,
            idempotency_key=idempotency_key, request_hash=request_hash, response=task,
        )
        return task

    if action == "retry":
        if task.status != PipelineStatus.FAILED:
            raise HTTPException(status_code=409, detail=f"Cannot retry task in status '{task.status}'.")
        task.transition_to(PipelineStatus.QUEUED)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        reset_task = PipelineTaskResponse(
            task_id=task_id,
            owner_id=task.owner_id,
            status=PipelineStatus.QUEUED,
            current_stage="QUEUED",
            bug_report=task.bug_report,
            created_at=task.created_at,
            updated_at=now,
            repository_id=task.repository_id,
            repository_base_revision=task.repository_base_revision,
            repository_applied_digest=task.repository_applied_digest,
            repository_result_revision=task.repository_result_revision,
            repository_operation_state=task.repository_operation_state,
            evidence_timeline=[
                *task.evidence_timeline,
                EvidenceEvent(timestamp=now, stage="RETRY", status="QUEUED", actor=request.state.actor, correlation_id=task_id),
            ],
        )
        retried, retry_error = tasks_db.retry_job(task_id, reset_task)
        if not retried:
            raise HTTPException(status_code=409, detail=retry_error)
        tasks_db.record_action(
            task_id, action, request.state.actor, "QUEUED", action_req.notes,
            idempotency_key=idempotency_key, request_hash=request_hash, response=reset_task,
        )
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
        task.transition_to(PipelineStatus.APPROVED)
        task.current_stage = "APPROVED"
        orchestrator._log_event(task, "HUMAN_APPROVAL", "APPROVED", {"notes": action_req.notes}, actor=request.state.actor)

    elif action == "reject":
        if task.status != PipelineStatus.AWAITING_HUMAN_APPROVAL:
            raise HTTPException(status_code=400, detail=f"Cannot reject task in status '{task.status}'.")
        task.is_approved = False
        task.transition_to(PipelineStatus.REJECTED)
        task.current_stage = "REJECTED"
        orchestrator._log_event(task, "HUMAN_APPROVAL", "REJECTED", {"notes": action_req.notes}, actor=request.state.actor)

    elif action == "apply":
        if not task.is_approved:
            raise HTTPException(
                status_code=400,
                detail="Cannot apply patch to working copy before human approval."
            )
        if not task.investigation_patch or not task.investigation_patch.unified_diff:
            raise HTTPException(status_code=400, detail="No proposed patch exists to apply.")
        if task.is_applied:
            if task.repository_id and task.repository_operation_state == "APPLIED":
                tasks_db.record_action(
                    task_id, action, request.state.actor, task.status.value, action_req.notes,
                    {"repository_id": task.repository_id, "replayed_after_interruption": True},
                    idempotency_key=idempotency_key, request_hash=request_hash, response=task,
                )
                return task
            raise HTTPException(status_code=409, detail="Patch has already been applied to this task workspace.")
        codebase = tasks_db.get_snapshot(task_id)
        if not codebase:
            raise HTTPException(status_code=409, detail="The task source snapshot is unavailable.")

        if task.repository_id and task.repository_operation_state == "APPLYING":
            try:
                with REPOSITORY_TARGETS.target_lock(task.repository_id):
                    recovered = REPOSITORY_TARGETS.recover_applied_patch(
                        task.repository_id, task.repository_base_revision or "",
                        task.investigation_patch.files_changed, task.investigation_patch.unified_diff,
                    )
                    if recovered:
                        task.repository_applied_digest = REPOSITORY_TARGETS.diff_digest(task.repository_id)
                        task.repository_operation_state = "APPLIED"
                        task.is_applied = True
                        task.transition_to(PipelineStatus.APPLIED)
                        task.current_stage = "PATCH_APPLIED_TO_TARGET_REPOSITORY"
                        orchestrator._log_event(task, "REPOSITORY_APPLY", "APPLIED", {
                            "files": task.investigation_patch.files_changed,
                            "repository_id": task.repository_id,
                            "base_revision": task.repository_base_revision,
                            "recovered_after_interruption": True,
                        }, actor=request.state.actor)
                        _persist_task_operation(task)
                        tasks_db.record_action(
                            task_id, action, request.state.actor, task.status.value, action_req.notes,
                            {"files": task.investigation_patch.files_changed, "recovered_after_interruption": True},
                            idempotency_key=idempotency_key, request_hash=request_hash, response=task,
                        )
                        return task
            except RepositoryTargetError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

        in_scope, scope_error = orchestrator.sandbox.validate_patch_scope(
            task.investigation_patch.unified_diff,
            task.investigation_patch.files_changed,
            set(codebase),
        )
        if not in_scope:
            raise HTTPException(status_code=400, detail=f"Patch rejected by scope validation: {scope_error}")

        target_dir = REPOSITORY_TARGETS.path(task.repository_id) if task.repository_id else APPLIED_ROOT / task_id
        if not task.repository_id and target_dir.exists():
            raise HTTPException(status_code=409, detail="A task workspace already exists at this location.")
        target_created = False
        try:
            operation_lock = REPOSITORY_TARGETS.target_lock(task.repository_id) if task.repository_id else nullcontext()
            with operation_lock:
                if task.repository_id:
                    REPOSITORY_TARGETS.require_clean_base(task.repository_id, task.repository_base_revision)
                    REPOSITORY_TARGETS.verify_snapshot_files(
                        task.repository_id, task.repository_base_revision or "", codebase,
                        task.investigation_patch.files_changed,
                    )
                else:
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
                if task.repository_id:
                    task.repository_operation_state = "APPLYING"
                    _persist_task_operation(task)
                applied, patch_error = orchestrator.sandbox.apply_patch(
                    target_dir, task.investigation_patch.unified_diff
                )
                if not applied:
                    raise HTTPException(status_code=400, detail=f"Patch application failed: {patch_error}")
                if task.repository_id:
                    task.repository_applied_digest = REPOSITORY_TARGETS.diff_digest(task.repository_id)
                    task.repository_operation_state = "APPLIED"
                    task.is_applied = True
                    task.transition_to(PipelineStatus.APPLIED)
                    task.current_stage = "PATCH_APPLIED_TO_TARGET_REPOSITORY"
                    orchestrator._log_event(task, "REPOSITORY_APPLY", "APPLIED", {
                        "files": task.investigation_patch.files_changed,
                        "repository_id": task.repository_id,
                        "base_revision": task.repository_base_revision,
                        "target": "configured repository",
                    }, actor=request.state.actor)
                    _persist_task_operation(task)
                    tasks_db.record_action(
                        task_id, action, request.state.actor, task.status.value, action_req.notes,
                        {"files": task.investigation_patch.files_changed, "repository_id": task.repository_id},
                        idempotency_key=idempotency_key, request_hash=request_hash, response=task,
                    )
                    return task
        except HTTPException:
            if target_created:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        except RepositoryTargetError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            if target_created:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=redact_sensitive_text(f"Could not prepare task workspace: {error}")) from error

        task.is_applied = True
        task.transition_to(PipelineStatus.APPLIED)
        task.current_stage = "PATCH_APPLIED_TO_TASK_WORKSPACE"
        orchestrator._log_event(task, "REPOSITORY_APPLY", "APPLIED", {
            "files": task.investigation_patch.files_changed,
            "target": "task-owned repository snapshot",
        }, actor=request.state.actor)

    elif action == "commit":
        if not task.is_applied:
            raise HTTPException(
                status_code=400,
                detail="Cannot commit patch before it has been applied to repository."
            )
        if task.is_committed:
            if task.repository_id and task.repository_operation_state == "COMMITTED":
                tasks_db.record_action(
                    task_id, action, request.state.actor, task.status.value, action_req.notes,
                    {"repository_id": task.repository_id, "commit_hash": task.commit_hash, "replayed_after_interruption": True},
                    idempotency_key=idempotency_key, request_hash=request_hash, response=task,
                )
                return task
            raise HTTPException(status_code=409, detail="Task already has a commit.")
        commit_msg = (action_req.commit_message or "").strip() or (
            f"fix({task.investigation_patch.buggy_file}): {task.triage.title if task.triage else 'auto remediation'}\n\n"
            f"Approved-By: Human Reviewer\nTask-ID: {task.task_id}"
        )
        try:
            operation_lock = REPOSITORY_TARGETS.target_lock(task.repository_id) if task.repository_id else nullcontext()
            with operation_lock:
                if task.repository_id:
                    if not task.repository_base_revision or not task.repository_applied_digest or not task.investigation_patch:
                        raise RepositoryTargetError("Task is missing configured repository provenance for commit.")
                    if task.repository_operation_state == "COMMITTING" and task.commit_message != commit_msg:
                        raise RepositoryTargetError("A commit with a different message is already pending recovery.")
                    task.repository_operation_state = "COMMITTING"
                    task.commit_message = commit_msg
                    _persist_task_operation(task)
                    commit_hash = REPOSITORY_TARGETS.commit_patch(
                        task.repository_id, task.repository_base_revision,
                        task.investigation_patch.files_changed, task.repository_applied_digest, commit_msg,
                    )
                    task.repository_result_revision = commit_hash
                    task.repository_operation_state = "COMMITTED"
                    task.is_committed = True
                    task.transition_to(PipelineStatus.COMMITTED)
                    task.commit_hash = commit_hash
                    task.commit_message = commit_msg
                    task.current_stage = "COMMITTED"
                    orchestrator._log_event(task, "GIT_COMMIT", "COMMITTED", {
                        "commit_hash": commit_hash,
                        "commit_message": commit_msg,
                        "repository_id": task.repository_id,
                        "base_revision": task.repository_base_revision,
                        "result_revision": task.repository_result_revision,
                    }, actor=request.state.actor)
                    _persist_task_operation(task)
                    tasks_db.record_action(
                        task_id, action, request.state.actor, task.status.value, action_req.notes,
                        {
                            "commit_hash": task.commit_hash, "commit_message": task.commit_message,
                            "repository_id": task.repository_id, "base_revision": task.repository_base_revision,
                            "result_revision": task.repository_result_revision,
                        },
                        idempotency_key=idempotency_key, request_hash=request_hash, response=task,
                    )
                    return task
                else:
                    target_dir = APPLIED_ROOT / task_id
                    subprocess.run(["git", "add", "--all"], cwd=target_dir, capture_output=True, text=True, timeout=10, check=True)
                    subprocess.run(
                        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", commit_msg],
                        cwd=target_dir, capture_output=True, text=True, timeout=15, check=True,
                    )
                    commit_hash = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=target_dir, capture_output=True, text=True, timeout=5, check=True,
                    ).stdout.strip()
        except RepositoryTargetError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise HTTPException(status_code=500, detail=redact_sensitive_text(f"Git commit failed: {error}")) from error
        task.is_committed = True
        task.transition_to(PipelineStatus.COMMITTED)
        task.commit_hash = commit_hash
        task.commit_message = commit_msg
        task.current_stage = "COMMITTED"
        orchestrator._log_event(task, "GIT_COMMIT", "COMMITTED", {
            "commit_hash": commit_hash,
            "commit_message": commit_msg,
            "repository_id": task.repository_id,
            "base_revision": task.repository_base_revision,
            "result_revision": task.repository_result_revision or commit_hash,
        }, actor=request.state.actor)

    tasks_db[task_id] = task
    outcome = task.status.value
    details = {}
    if action == "apply" and task.investigation_patch:
        details["files"] = task.investigation_patch.files_changed
    elif action == "commit":
        details["commit_hash"] = task.commit_hash
        details["commit_message"] = task.commit_message
        if task.repository_id:
            details["repository_id"] = task.repository_id
            details["base_revision"] = task.repository_base_revision
            details["result_revision"] = task.repository_result_revision
    tasks_db.record_action(
        task_id, action, request.state.actor, outcome, action_req.notes, details,
        idempotency_key=idempotency_key, request_hash=request_hash, response=task,
    )
    return task


@app.get("/api/v1/scenarios")
@app.get("/scenarios", include_in_schema=False)
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
    bind_host = os.environ.get("RELAY_HOST", "127.0.0.1").strip()
    try:
        bind_address = ipaddress.ip_address(bind_host)
    except ValueError as error:
        raise RuntimeError("RELAY_HOST must be an IPv4 or IPv6 address literal.") from error
    if not bind_address.is_loopback and not (API_TOKEN or AUTH_USERS):
        raise RuntimeError(
            "Authentication is required when RELAY_HOST binds beyond localhost. "
            "Configure RELAY_API_TOKEN or RELAY_AUTH_USERS_JSON."
        )
    uvicorn.run("main:app", host=bind_host, port=8000, reload=False)
