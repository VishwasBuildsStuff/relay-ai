"""
Unit tests for Pydantic v2 schemas in relay-ai/schemas.py.
"""

import pytest
from pydantic import ValidationError
from schemas import (
    InvestigationAndPatchResult,
    PipelineStatus,
    PipelineTaskResponse,
    ReportRequest,
    ReviewResult,
    SandboxExecutionResult,
    TaskActionRequest,
    TriageResult,
)


def test_pipeline_status_enum():
    """Verify the explicit pipeline and human-action lifecycle states exist."""
    expected_states = {
        "RECEIVED", "TRIAGED", "INVESTIGATED", "PATCHED",
        "SANDBOX_TESTED", "REVIEWED", "AWAITING_HUMAN_APPROVAL", "APPROVED",
        "APPLIED", "COMMITTED", "REJECTED", "CANCELLED", "FAILED",
    }
    actual_states = {s.value for s in PipelineStatus}
    assert actual_states == expected_states


def test_report_request_validation():
    """Ensure ReportRequest validates minimum chat_message length and forbids extra fields."""
    valid = ReportRequest(
        chat_message="Fix the off-by-one error in calculator",
        codebase={"calc.py": "def add(a, b): return a + b\n"}
    )
    assert valid.chat_message.startswith("Fix")

    # Short message should fail
    with pytest.raises(ValidationError):
        ReportRequest(chat_message="err", codebase={"calc.py": ""})

    # Empty codebase should fail
    with pytest.raises(ValidationError):
        ReportRequest(chat_message="Valid length message", codebase={})


def test_triage_result_schema():
    """Ensure TriageResult enforces severity choices."""
    triage = TriageResult(
        title="Off-by-one index error",
        severity="high",
        acceptance_criteria=["Index 3 returns tier 3", "Index 4 falls back to default"],
        rationale="Blocks paying tier 3 users from service access"
    )
    assert triage.severity == "high"

    with pytest.raises(ValidationError):
        TriageResult(
            title="Invalid",
            severity="super_critical",  # Invalid literal
            acceptance_criteria=["test"]
        )


def test_investigation_and_patch_schema():
    """Ensure confidence score bounds and unified diff requirements."""
    res = InvestigationAndPatchResult(
        buggy_file="utils/limiter.py",
        affected_function="check_limit",
        root_cause="Comparison used <= instead of <",
        confidence_score=0.95,
        files_changed=["utils/limiter.py"],
        unified_diff="diff --git a/utils/limiter.py b/utils/limiter.py\n",
        summary_of_changes="Fix boundary check"
    )
    assert res.confidence_score == 0.95

    # Out of bounds confidence score (> 1.0)
    with pytest.raises(ValidationError):
        InvestigationAndPatchResult(
            buggy_file="utils/limiter.py",
            affected_function="check_limit",
            root_cause="Bad logic",
            confidence_score=1.5,
            files_changed=["utils/limiter.py"],
            unified_diff="diff --git a/a b/b\n",
            summary_of_changes="Fix",
        )


def test_sandbox_result_schema():
    """Ensure SandboxExecutionResult captures telemetry."""
    res = SandboxExecutionResult(
        test_command="pytest -v",
        exit_code=0,
        tests_passed=True,
        stdout="2 passed in 0.05s",
        stderr="",
        execution_time_ms=120.5,
        isolation_mode="subprocess_dev",
        applied_patch_cleanly=True,
    )
    assert res.tests_passed is True
    assert res.isolation_mode == "subprocess_dev"


def test_reviewer_result_schema():
    """Ensure ReviewResult validates recommendation and security assessment."""
    review = ReviewResult(
        security_assessment="PASS",
        recommendation="APPROVE",
        review_summary="Clean patch with 100% test pass rate.",
        regression_risks=[],
        side_effects=[],
    )
    assert review.security_assessment == "PASS"

    with pytest.raises(ValidationError):
        ReviewResult(
            security_assessment="UNKNOWN",  # Invalid
            recommendation="APPROVE",
            review_summary="test"
        )


def test_task_action_request_schema():
    """Ensure TaskActionRequest only accepts valid action names."""
    valid_actions = ["approve", "reject", "apply", "commit", "cancel", "retry"]
    for act in valid_actions:
        req = TaskActionRequest(action=act)
        assert req.action == act

    with pytest.raises(ValidationError):
        TaskActionRequest(action="deploy")  # Invalid action
