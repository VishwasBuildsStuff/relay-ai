"""
Integration tests for FastAPI endpoints in relay-ai/main.py.
"""

import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from main import app, tasks_db
from schemas import (
    InvestigationAndPatchResult,
    PipelineStatus,
    PipelineTaskResponse,
    SandboxExecutionResult,
    TriageResult,
    ReviewResult,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_root_endpoint(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "RELAY AI" in res.json()["status"]


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "python_version" in data


def test_scenarios_endpoint(client):
    res = client.get("/scenarios")
    assert res.status_code == 200
    scenarios = res.json()
    assert len(scenarios) >= 3
    assert any(s["id"] == "scenario_off_by_one" for s in scenarios)


def test_approval_apply_commit_lifecycle(client, monkeypatch, tmp_path):
    """
    Tests the strict decoupled sequence:
    AWAITING_HUMAN_APPROVAL -> APPROVE -> APPLY -> COMMIT
    Verifies that applying or committing prematurely raises HTTP 400.
    """
    task_id = "test-task-lifecycle-123"
    dummy_task = PipelineTaskResponse(
        task_id=task_id,
        status=PipelineStatus.AWAITING_HUMAN_APPROVAL,
        current_stage="AWAITING_HUMAN_APPROVAL",
        bug_report="Test issue",
        created_at="2026-11-04T00:00:00Z",
        updated_at="2026-11-04T00:00:00Z",
        triage=TriageResult(
            title="Test Bug",
            severity="medium",
            acceptance_criteria=["Criterion 1"]
        ),
        investigation_patch=InvestigationAndPatchResult(
            buggy_file="module.py",
            affected_function="func",
            root_cause="Bad logic",
            confidence_score=0.9,
            files_changed=["module.py"],
            unified_diff=(
                "diff --git a/module.py b/module.py\n"
                "--- a/module.py\n"
                "+++ b/module.py\n"
                "@@ -1 +1 @@\n"
                "-before\n"
                "+after\n"
            ),
            summary_of_changes="Fix"
        ),
        sandbox_result=SandboxExecutionResult(
            test_command="pytest",
            exit_code=0,
            tests_passed=True,
            stdout="OK",
            stderr="",
            execution_time_ms=10.0,
            isolation_mode="subprocess_dev",
            applied_patch_cleanly=True
        ),
        reviewer=ReviewResult(
            security_assessment="PASS",
            recommendation="APPROVE",
            review_summary="Patch is safe and tests passed.",
        ),
        is_approved=None,
        is_applied=False,
        is_committed=False,
    )
    monkeypatch.setattr("main.APPLIED_ROOT", tmp_path / "applied")
    tasks_db[task_id] = dummy_task
    tasks_db.save_snapshot(task_id, {"module.py": "before\n"})

    # 1. Attempting to apply before approval must fail (400)
    apply_fail = client.post(f"/tasks/{task_id}/action", json={"action": "apply"})
    assert apply_fail.status_code == 400

    # 2. Attempting to commit before apply must fail (400)
    commit_fail = client.post(f"/tasks/{task_id}/action", json={"action": "commit"})
    assert commit_fail.status_code == 400

    # 3. Approve action
    appr_res = client.post(f"/tasks/{task_id}/action", json={"action": "approve", "notes": "LGTM"})
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"
    assert appr_res.json()["is_approved"] is True

    # 4. Apply action
    apply_res = client.post(f"/tasks/{task_id}/action", json={"action": "apply"})
    assert apply_res.status_code == 200
    assert apply_res.json()["is_applied"] is True

    # 5. Commit action
    commit_res = client.post(f"/tasks/{task_id}/action", json={"action": "commit", "commit_message": "fix: resolved test bug"})
    assert commit_res.status_code == 200
    assert commit_res.json()["is_committed"] is True
    assert commit_res.json()["commit_hash"] is not None
    assert commit_res.json()["status"] == "COMMITTED"


def test_reject_action(client):
    """Verifies that rejecting a task updates status to REJECTED."""
    task_id = "test-task-reject-456"
    dummy_task = PipelineTaskResponse(
        task_id=task_id,
        status=PipelineStatus.AWAITING_HUMAN_APPROVAL,
        current_stage="AWAITING_HUMAN_APPROVAL",
        bug_report="Test reject",
        created_at="2026-11-04T00:00:00Z",
        updated_at="2026-11-04T00:00:00Z",
        is_approved=None,
    )
    tasks_db[task_id] = dummy_task

    res = client.post(f"/tasks/{task_id}/action", json={"action": "reject", "notes": "Patch introduces regressions"})
    assert res.status_code == 200
    assert res.json()["status"] == "REJECTED"
    assert res.json()["is_approved"] is False
