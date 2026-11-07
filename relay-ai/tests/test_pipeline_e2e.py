"""Deterministic end-to-end verification of the queue, pipeline, review, and Git actions."""

import time
import subprocess

from fastapi.testclient import TestClient

import main
from providers import FakeStructuredProvider
from schemas import (
    InvestigationAndPatchResult,
    PipelineStatus,
    ReviewResult,
    SandboxExecutionResult,
    TriageResult,
)
from storage import SQLiteTaskStore


def test_submit_pipeline_review_apply_and_commit(tmp_path, monkeypatch):
    store = SQLiteTaskStore(tmp_path / "e2e.sqlite3")
    monkeypatch.setattr(main, "tasks_db", store)
    monkeypatch.setattr(main.pipeline_worker, "store", store)
    monkeypatch.setattr(main, "API_TOKEN", None)
    monkeypatch.setattr(main, "AUTH_USERS", {})
    monkeypatch.setattr(main, "AUTH_ROLES", {})
    monkeypatch.setattr(main, "_auth_roles_configured", False)
    applied_root = tmp_path / "applied"
    monkeypatch.setattr(main, "APPLIED_ROOT", applied_root)

    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(left, right):\n"
        "-    return left - right\n"
        "+    return left + right\n"
    )
    monkeypatch.setattr(main.orchestrator, "provider", FakeStructuredProvider({
        TriageResult: TriageResult(
            title="Fix addition result",
            severity="medium",
            acceptance_criteria=["add returns the sum of its arguments"],
            rationale="The function currently subtracts its inputs.",
        ),
        InvestigationAndPatchResult: InvestigationAndPatchResult(
            buggy_file="app.py",
            affected_function="add",
            root_cause="The implementation uses subtraction instead of addition.",
            confidence_score=1.0,
            files_changed=["app.py"],
            unified_diff=patch,
            summary_of_changes="Use addition in add.",
        ),
        ReviewResult: ReviewResult(
            security_assessment="PASS",
            recommendation="APPROVE",
            review_summary="The patch is scoped and sandbox evidence passes.",
        ),
    }))
    monkeypatch.setattr(
        main.orchestrator.sandbox,
        "run_tests",
        lambda **_kwargs: SandboxExecutionResult(
            test_command="pytest -q",
            exit_code=0,
            tests_passed=True,
            stdout="1 passed",
            stderr="",
            execution_time_ms=1.0,
            isolation_mode="docker",
            applied_patch_cleanly=True,
        ),
    )

    with TestClient(main.app) as client:
        submitted = client.post(
            "/api/v1/tasks",
            json={
                "chat_message": "Fix add so it returns the sum of both inputs.",
                "codebase": {"app.py": "def add(left, right):\n    return left - right\n"},
            },
        )
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()["task_id"]
        assert submitted.json()["status"] == PipelineStatus.QUEUED.value

        deadline = time.monotonic() + 8
        task = submitted.json()
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/tasks/{task_id}")
            assert response.status_code == 200, response.text
            task = response.json()
            if task["status"] in {PipelineStatus.AWAITING_HUMAN_APPROVAL.value, PipelineStatus.FAILED.value}:
                break
            time.sleep(0.05)

        assert task["status"] == PipelineStatus.AWAITING_HUMAN_APPROVAL.value, task
        assert task["sandbox_result"]["tests_passed"] is True
        assert task["reviewer"]["recommendation"] == "APPROVE"
        events_response = client.get(f"/api/v1/tasks/{task_id}/events?limit=100")
        assert events_response.status_code == 200
        stages = {event["stage"] for event in events_response.json()}
        assert {
            "INGESTION",
            "STAGE_A_TRIAGE",
            "STAGE_B_INVESTIGATION_AND_DEV",
            "SANDBOX_TEST_EXECUTION",
            "STAGE_C_REVIEW",
        } <= stages

        approved = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            json={"action": "approve", "notes": "End-to-end release drill"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == PipelineStatus.APPROVED.value

        applied = client.post(f"/api/v1/tasks/{task_id}/actions", json={"action": "apply"})
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == PipelineStatus.APPLIED.value
        workspace = applied_root / task_id
        assert (workspace / "app.py").read_text(encoding="utf-8") == (
            "def add(left, right):\n    return left + right\n"
        )

        committed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            json={"action": "commit", "commit_message": "fix: return the sum"},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["status"] == PipelineStatus.COMMITTED.value
        assert committed.json()["is_committed"] is True
        assert committed.json()["commit_hash"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert committed.json()["commit_hash"] == head
        assert store.count_actions(task_id) == 3
