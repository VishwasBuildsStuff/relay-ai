"""Typed contracts shared by the RELAY AI pipeline."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TriageSchema(StrictModel):
    task_id: str
    title: str
    summary: str
    severity: Literal["low", "medium", "high", "critical"]
    component: str
    acceptance_criteria: list[str]


class InvestigationSchema(StrictModel):
    task_id: str
    buggy_file: str
    affected_function: str
    root_cause: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    line_evidence: list[str]


class DeveloperPatchSchema(StrictModel):
    task_id: str
    target_file: str
    proposed_patch: str
    summary_of_changes: str
    potential_risks: list[str]


class SandboxResultSchema(StrictModel):
    task_id: str
    test_command: str
    exit_code: int
    stdout: str
    stderr: str
    tests_passed: bool


class ReviewerSchema(StrictModel):
    task_id: str
    security_assessment: Literal["PASS", "WARN", "FAIL"]
    review_summary: str
    recommendation: Literal["APPROVE", "REJECT", "NEEDS_REVISION"]


class OrchestratorState(StrictModel):
    task_id: str
    status: Literal[
        "RECEIVED", "TRIAGED", "INVESTIGATED", "PATCH_PROPOSED", "TESTED",
        "AWAITING_HUMAN_APPROVAL", "APPROVED", "REJECTED", "FAILED",
    ] = "RECEIVED"
    current_stage: str = "received"
    triage: TriageSchema | None = None
    investigation: InvestigationSchema | None = None
    developer_patch: DeveloperPatchSchema | None = None
    sandbox_result: SandboxResultSchema | None = None
    reviewer: ReviewerSchema | None = None
    error: dict[str, str] | None = None
    evidence_timeline: list[dict[str, str]] = Field(default_factory=list)
    resolution_report: str | None = None
