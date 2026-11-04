"""HTTP API and executable demo for RELAY AI."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from orchestrator import RelayOrchestrator

app = FastAPI(title="RELAY AI", version="1.0.0")
TASKS_DB: dict[str, dict] = {}
ORCHESTRATORS: dict[str, RelayOrchestrator] = {}


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_message: str
    codebase: dict[str, str]


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["APPROVE", "REJECT"]
    reviewer_comments: str = ""


@app.post("/tasks/report")
@app.post("/api/v1/tasks/report", include_in_schema=False)
def report_issue(request: ReportRequest) -> dict:
    task_id = str(uuid.uuid4())
    try:
        orchestrator = RelayOrchestrator(task_id, request.chat_message, request.codebase)
        ORCHESTRATORS[task_id] = orchestrator
        state = orchestrator.execute_pipeline()
        TASKS_DB[task_id] = state
        return state
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/tasks/{task_id}")
@app.get("/api/v1/tasks/{task_id}", include_in_schema=False)
def get_task(task_id: str) -> dict:
    state = TASKS_DB.get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return state


@app.post("/tasks/{task_id}/approve")
@app.post("/api/v1/tasks/{task_id}/approve", include_in_schema=False)
def approve_task(task_id: str, request: ApprovalRequest) -> dict:
    state = TASKS_DB.get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if state["status"] != "AWAITING_HUMAN_APPROVAL":
        raise HTTPException(status_code=409, detail=f"Task is {state['status']}; approval is unavailable")
    if request.action == "APPROVE":
        state["status"] = "APPROVED"
        report = (f"Task {task_id} approved by human reviewer. "
                  f"{state['developer_patch']['summary_of_changes']} "
                  f"Tests passed: {state['sandbox_result']['tests_passed']}. "
                  f"Reviewer comments: {request.reviewer_comments or 'None'}")
        state["resolution_report"] = report
        state["evidence_timeline"].append({"stage": "human_approval", "detail": report})
    else:
        state["status"] = "REJECTED"
        detail = request.reviewer_comments or "Rejected by human reviewer"
        state["evidence_timeline"].append({"stage": "human_approval", "detail": detail})
    return state


if __name__ == "__main__":
    # Run with GEMINI_API_KEY set and the project's API dependencies installed.
    # This invokes the actual HTTP endpoints in-process, including human approval.
    import json
    from fastapi.testclient import TestClient

    demo_codebase = {
        "checkout.py": '''def checkout(subtotal: float, discount: float) -> float:\n    """Return the payable amount after applying a discount."""\n    discounted = subtotal - discount\n    return discounted - discount  # bug: discount is applied twice\n''',
        "tests/test_checkout.py": '''from checkout import checkout\n\ndef test_discount_is_applied_once():\n    assert checkout(100, 10) == 90\n''',
    }
    demo_message = (
        "Checkout applies the discount twice. For subtotal 100 and discount 10, "
        "the payable amount should be 90, but it is 80. Fix checkout.py and keep the test passing."
    )
    client = TestClient(app)
    created = client.post("/tasks/report", json={
        "chat_message": demo_message, "codebase": demo_codebase,
    })
    if created.status_code != 200:
        raise SystemExit(f"Demo task creation failed ({created.status_code}): {created.text}")
    task = created.json()
    print("Pipeline outputs:\n" + json.dumps(task, indent=2))
    if task["status"] == "AWAITING_HUMAN_APPROVAL":
        approved = client.post(f"/tasks/{task['task_id']}/approve", json={
            "action": "APPROVE", "reviewer_comments": "Demo approval",
        })
        print("Approval response:\n" + json.dumps(approved.json(), indent=2))
    else:
        print(f"Human approval not available; pipeline ended in {task['status']}.")
