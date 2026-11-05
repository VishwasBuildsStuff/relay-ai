"""
RELAY AI — Data Schemas & State Models
Strict Pydantic v2 schemas for all state transitions, AI outputs, and execution evidence.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from path_utils import normalize_snapshot_paths


class PipelineStatus(str, Enum):
    """Explicit pipeline state machine states."""
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    SANDBOX_RUNNING = "SANDBOX_RUNNING"
    REVIEWING = "REVIEWING"
    TRIAGED = "TRIAGED"
    INVESTIGATED = "INVESTIGATED"
    PATCHED = "PATCHED"
    SANDBOX_TESTED = "SANDBOX_TESTED"
    REVIEWED = "REVIEWED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


_ALLOWED_STATUS_TRANSITIONS = {
    PipelineStatus.RECEIVED: {PipelineStatus.QUEUED, PipelineStatus.RUNNING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.QUEUED: {PipelineStatus.RUNNING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.RUNNING: {PipelineStatus.TRIAGING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.TRIAGING: {PipelineStatus.TRIAGED, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.TRIAGED: {PipelineStatus.INVESTIGATING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.INVESTIGATING: {PipelineStatus.INVESTIGATED, PipelineStatus.PATCHED, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.INVESTIGATED: {PipelineStatus.PATCHED, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.PATCHED: {PipelineStatus.SANDBOX_RUNNING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.SANDBOX_RUNNING: {PipelineStatus.SANDBOX_TESTED, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.SANDBOX_TESTED: {PipelineStatus.REVIEWING, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.REVIEWING: {PipelineStatus.AWAITING_HUMAN_APPROVAL, PipelineStatus.CANCELLED, PipelineStatus.FAILED},
    PipelineStatus.REVIEWED: {PipelineStatus.AWAITING_HUMAN_APPROVAL, PipelineStatus.APPROVED, PipelineStatus.REJECTED},
    PipelineStatus.AWAITING_HUMAN_APPROVAL: {PipelineStatus.APPROVED, PipelineStatus.REJECTED, PipelineStatus.CANCELLED},
    PipelineStatus.APPROVED: {PipelineStatus.APPLIED},
    PipelineStatus.APPLIED: {PipelineStatus.COMMITTED},
    PipelineStatus.FAILED: {PipelineStatus.QUEUED},
}


SeverityLevel = Literal["low", "medium", "high", "critical"]
SecurityAssessment = Literal["PASS", "WARN", "FAIL"]
ReviewRecommendation = Literal["APPROVE", "REJECT", "REVISE"]
IsolationMode = Literal["docker", "subprocess_dev", "unavailable"]
TaskActionType = Literal["approve", "reject", "apply", "commit", "cancel", "retry"]
MAX_MODEL_INPUT_BYTES = 1_500_000


class ReportRequest(BaseModel):
    """Natural-language bug report paired with in-memory codebase dict."""
    model_config = ConfigDict(extra="forbid")

    chat_message: str = Field(
        ...,
        min_length=5,
        max_length=20_000,
        description="Natural-language bug report, webhook payload, or user issue description."
    )
    codebase: Dict[str, str] = Field(
        ...,
        min_length=1,
        description="Mapping of relative file paths to UTF-8 file contents."
    )
    repository_id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="Optional operator-configured local Git repository target.",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_snapshot(cls, obj: Any) -> Any:
        """Reject unsafe paths and oversized snapshots for both JSON and ZIP inputs."""
        if isinstance(obj, dict):
            codebase = obj.get("codebase")
            if isinstance(codebase, dict):
                if len(codebase) > 500:
                    raise ValueError("Codebase contains more than 500 files.")
                if not all(isinstance(name, str) and isinstance(content, str) for name, content in codebase.items()):
                    return obj
                codebase = normalize_snapshot_paths(codebase)
                obj = dict(obj)
                obj["codebase"] = codebase
                report = obj.get("chat_message", "")
                total_bytes = len(report.encode("utf-8")) if isinstance(report, str) else 0
                for name, content in codebase.items():
                    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
                        raise ValueError(f"File exceeds 2 MiB limit: {name!r}")
                    total_bytes += len(name.encode("utf-8")) + len(content.encode("utf-8"))
                if total_bytes > 25 * 1024 * 1024:
                    raise ValueError("Request exceeds the 25 MiB snapshot limit.")
                if total_bytes > MAX_MODEL_INPUT_BYTES:
                    raise ValueError("Report and codebase exceed the 1.5 MB model input limit.")
        return obj


class TriageResult(BaseModel):
    """Stage A: Issue classification and testable acceptance criteria."""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., description="Concise engineering issue title.")
    severity: SeverityLevel = Field(..., description="Triage severity level.")
    acceptance_criteria: List[str] = Field(
        ...,
        min_length=1,
        description="Checklist of testable criteria required for bug resolution."
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Short technical reasoning for the assigned severity."
    )


class InvestigationAndPatchResult(BaseModel):
    """Stage B: Root cause diagnosis and unified Git diff patch synthesis."""
    model_config = ConfigDict(extra="forbid")

    buggy_file: str = Field(..., description="Exact relative path to the buggy file.")
    affected_function: str = Field(..., description="Function or method where the defect manifests.")
    root_cause: str = Field(..., description="Detailed technical root cause analysis.")
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 and 1.0."
    )
    files_changed: List[str] = Field(
        ...,
        min_length=1,
        description="List of files modified by the proposed patch."
    )
    unified_diff: str = Field(
        ...,
        max_length=1_000_000,
        description="Minimal, valid Unified Git Diff (e.g. diff --git a/... b/...)."
    )
    summary_of_changes: str = Field(
        ...,
        description="Concise description of the modifications made."
    )


class SandboxExecutionResult(BaseModel):
    """Deterministic sandbox test execution result (Stage B -> Stage C)."""
    model_config = ConfigDict(extra="forbid")

    test_command: str = Field(..., description="Executed command (e.g. pytest -v).")
    exit_code: int = Field(..., description="Process exit code.")
    tests_passed: bool = Field(..., description="True if test runner succeeded with exit_code 0.")
    stdout: str = Field(default="", description="Captured standard output.")
    stderr: str = Field(default="", description="Captured standard error.")
    execution_time_ms: float = Field(default=0.0, ge=0.0, description="Duration in milliseconds.")
    isolation_mode: IsolationMode = Field(..., description="Isolation backend used.")
    applied_patch_cleanly: bool = Field(
        ...,
        description="True if git apply --check passed and the patch applied cleanly."
    )
    patch_error: Optional[str] = Field(
        default=None,
        description="Git error if the patch failed validation or application."
    )


class ReviewResult(BaseModel):
    """Stage C: Security audit, regression analysis, and human approval recommendation."""
    model_config = ConfigDict(extra="forbid")

    security_assessment: SecurityAssessment = Field(
        ...,
        description="Security verdict (PASS, WARN, or FAIL)."
    )
    recommendation: ReviewRecommendation = Field(
        ...,
        description="Recommended action for the human reviewer."
    )
    review_summary: str = Field(
        ...,
        description="Summary of the security and stability audit."
    )
    regression_risks: List[str] = Field(
        default_factory=list,
        description="Potential unintended consequences or backward incompatibility risks."
    )
    side_effects: List[str] = Field(
        default_factory=list,
        description="Identified side effects or external system impacts."
    )


class EvidenceEvent(BaseModel):
    """Append-only event with actor and task correlation metadata."""
    model_config = ConfigDict(extra="allow")

    timestamp: str
    stage: str
    status: str
    actor: str = "system"
    correlation_id: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class TaskActionRequest(BaseModel):
    """Human decision request for review, repository, cancel, or retry actions."""
    model_config = ConfigDict(extra="forbid")

    action: TaskActionType = Field(
        ...,
        description="Action to take: 'approve', 'reject', 'apply', or 'commit'."
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional reviewer notes or justification."
    )
    commit_message: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Commit message when performing 'commit' action."
    )


class PipelineTaskResponse(BaseModel):
    """Complete aggregated task lifecycle state returned by FastAPI."""
    model_config = ConfigDict(extra="allow")

    task_id: str
    owner_id: Optional[str] = None
    status: PipelineStatus
    current_stage: str
    bug_report: str
    created_at: str
    updated_at: str

    triage: Optional[TriageResult] = None
    investigation_patch: Optional[InvestigationAndPatchResult] = None
    sandbox_result: Optional[SandboxExecutionResult] = None
    reviewer: Optional[ReviewResult] = None
    error: Optional[Dict[str, Any]] = None
    evidence_timeline: List[EvidenceEvent] = Field(default_factory=list)

    is_approved: Optional[bool] = None
    is_applied: bool = False
    is_committed: bool = False
    commit_hash: Optional[str] = None
    commit_message: Optional[str] = None
    repository_id: Optional[str] = None
    repository_base_revision: Optional[str] = None
    repository_applied_digest: Optional[str] = None
    repository_result_revision: Optional[str] = None
    repository_operation_state: Optional[str] = None

    def transition_to(self, new_status: PipelineStatus, recovery: bool = False) -> None:
        """Move through the centralized task lifecycle graph; same-state updates are idempotent."""
        if self.status == new_status:
            return
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(self.status, set())
        if recovery and new_status == PipelineStatus.RUNNING and self.status in {
            PipelineStatus.TRIAGING, PipelineStatus.TRIAGED, PipelineStatus.INVESTIGATING,
            PipelineStatus.INVESTIGATED, PipelineStatus.PATCHED, PipelineStatus.SANDBOX_RUNNING,
            PipelineStatus.SANDBOX_TESTED, PipelineStatus.REVIEWING,
        }:
            allowed = {*allowed, PipelineStatus.RUNNING}
        if new_status not in allowed:
            raise ValueError(f"Invalid task status transition: {self.status.value} -> {new_status.value}{' during recovery' if recovery else ''}.")
        self.status = new_status
