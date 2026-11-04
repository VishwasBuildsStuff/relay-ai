"""
RELAY AI — 3-Stage Agentic Pipeline Orchestrator
Sequentially coordinates Triage (Stage A), Investigation & Development (Stage B),
Deterministic Sandbox Pytest Execution, and Security/Regression Review (Stage C).
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Callable, Dict, Optional

from google.genai.errors import APIError

from sandbox import SandboxRunner
from providers import GeminiStructuredProvider, StructuredProvider
from schemas import (
    EvidenceEvent,
    InvestigationAndPatchResult,
    PipelineStatus,
    PipelineTaskResponse,
    ReportRequest,
    ReviewResult,
    SandboxExecutionResult,
    TriageResult,
)


class RelayOrchestrator:
    """
    Coordinates the 3-stage AI remediation pipeline with deterministic sandbox verification.
    """

    def __init__(self, model_name: Optional[str] = None, provider: Optional[StructuredProvider] = None):
        self.model_name = model_name or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        api_key = os.environ.get("GEMINI_API_KEY")
        self.provider = provider or (GeminiStructuredProvider(api_key, self.model_name) if api_key else None)
        self.sandbox = SandboxRunner()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _log_event(self, task: PipelineTaskResponse, stage: str, status: str, details: Optional[Dict[str, Any]] = None) -> None:
        event = EvidenceEvent(
            timestamp=self._now(),
            stage=stage,
            status=status,
            details=details or {},
        )
        task.evidence_timeline.append(event)
        task.updated_at = self._now()

    def _call_gemini_structured(self, system_instruction: str, prompt: str, schema_cls: Any) -> Any:
        """
        Executes a structured JSON call using official google-genai SDK with Pydantic schema.
        Handles APIError and quota limits safely.
        """
        if not self.provider:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. Please configure GEMINI_API_KEY to run AI stages."
            )
        return self.provider.generate(system_instruction, prompt, schema_cls)

    async def run(
        self,
        task_id: str,
        request: ReportRequest,
        on_progress: Optional[Callable[[PipelineTaskResponse], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> PipelineTaskResponse:
        """
        Asynchronously executes the full 3-Stage remediation pipeline:
        Stage A (Triage) -> Stage B (Investigation & Development) ->
        Deterministic Sandbox Runner -> Stage C (Review).
        """
        now = self._now()
        task = PipelineTaskResponse(
            task_id=task_id,
            status=PipelineStatus.RECEIVED,
            current_stage="RECEIVED",
            bug_report=request.chat_message,
            created_at=now,
            updated_at=now,
            evidence_timeline=[],
        )
        self._log_event(task, "INGESTION", "RECEIVED", {"files_count": len(request.codebase)})
        if on_progress:
            on_progress(task)

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "INGESTION", on_progress)

        # -------------------------------------------------------------
        # STAGE A: TRIAGE AGENT
        # -------------------------------------------------------------
        task.current_stage = "STAGE_A_TRIAGE"
        self._log_event(task, "STAGE_A_TRIAGE", "IN_PROGRESS")
        if on_progress:
            on_progress(task)

        triage_prompt = f"""
You are the Lead Triage Engineer at RELAY AI.
Analyze the following natural-language bug report and codebase file list to classify its severity and specify actionable acceptance criteria.

[BUG REPORT]
{request.chat_message}

[CODEBASE FILES]
{json.dumps(list(request.codebase.keys()), indent=2)}

Produce a structured TriageResult with:
- title: concise technical summary
- severity: 'low', 'medium', 'high', or 'critical'
- acceptance_criteria: 2 to 4 concrete, testable conditions required to consider this bug resolved.
- rationale: short explanation for the severity
"""
        try:
            triage_result: TriageResult = self._call_gemini_structured(
                system_instruction="You are a senior triage engineer. Classify severity and define strict acceptance criteria.",
                prompt=triage_prompt,
                schema_cls=TriageResult,
            )
            task.triage = triage_result
            task.status = PipelineStatus.TRIAGED
            self._log_event(task, "STAGE_A_TRIAGE", "COMPLETED", triage_result.model_dump())
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "STAGE_A_TRIAGE", e, on_progress)

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "STAGE_A_TRIAGE", on_progress)

        # -------------------------------------------------------------
        # STAGE B: INVESTIGATION & DEVELOPMENT AGENT
        # -------------------------------------------------------------
        task.current_stage = "STAGE_B_INVESTIGATION_AND_DEV"
        self._log_event(task, "STAGE_B_INVESTIGATION_AND_DEV", "IN_PROGRESS")
        if on_progress:
            on_progress(task)

        # Prepare codebase contents for inspection
        codebase_dump = ""
        for filepath, content in request.codebase.items():
            codebase_dump += f"\n--- {filepath} ---\n{content}\n"

        investigation_prompt = f"""
You are the Lead Systems Investigator and Developer at RELAY AI.
Inspect the repository files below to diagnose the root cause of the reported bug and synthesize a minimal, valid Unified Git Diff patch.

[ISSUE TITLE]: {task.triage.title}
[SEVERITY]: {task.triage.severity}
[ACCEPTANCE CRITERIA]:
{chr(10).join(f"- {c}" for c in task.triage.acceptance_criteria)}

[USER BUG REPORT]:
{request.chat_message}

[CODEBASE FILES]:
{codebase_dump}

INSTRUCTIONS FOR UNIFIED DIFF:
1. Provide a standard Unified Git Diff format starting with `diff --git a/<filename> b/<filename>`.
2. Modify ONLY the exact lines necessary to fix the defect and satisfy acceptance criteria.
3. Ensure line counts in hunk headers `@@ -start,count +start,count @@` match the patch lines exactly.
4. Do NOT include markdown code fences (```diff) inside the unified_diff string property; provide the raw diff text.
"""
        try:
            inv_patch_result: InvestigationAndPatchResult = self._call_gemini_structured(
                system_instruction="You are a principal software engineer. Diagnose root cause and output a minimal, valid Unified Git Diff.",
                prompt=investigation_prompt,
                schema_cls=InvestigationAndPatchResult,
            )
            # Clean accidental markdown fences if returned
            clean_diff = inv_patch_result.unified_diff.strip()
            if clean_diff.startswith("```"):
                clean_diff = clean_diff.split("\n", 1)[1]
            if clean_diff.endswith("```"):
                clean_diff = clean_diff.rsplit("\n", 1)[0]
            inv_patch_result.unified_diff = clean_diff.strip()
            if not inv_patch_result.unified_diff:
                raise ValueError("Model returned an empty patch.")
            in_scope, scope_error = self.sandbox.validate_patch_scope(
                inv_patch_result.unified_diff,
                inv_patch_result.files_changed,
            )
            if not in_scope:
                raise ValueError(f"Generated patch failed path/scope validation: {scope_error}")

            task.investigation_patch = inv_patch_result
            task.status = PipelineStatus.PATCHED
            self._log_event(task, "STAGE_B_INVESTIGATION_AND_DEV", "COMPLETED", {
                "buggy_file": inv_patch_result.buggy_file,
                "affected_function": inv_patch_result.affected_function,
                "confidence_score": inv_patch_result.confidence_score,
            })
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "STAGE_B_INVESTIGATION_AND_DEV", e, on_progress)

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "STAGE_B_INVESTIGATION_AND_DEV", on_progress)

        # -------------------------------------------------------------
        # DETERMINISTIC SANDBOX TEST RUNNER (Stage B -> Stage C Bridge)
        # -------------------------------------------------------------
        task.current_stage = "SANDBOX_TEST_EXECUTION"
        self._log_event(task, "SANDBOX_TEST_EXECUTION", "IN_PROGRESS", {"isolation": self.sandbox.isolation_mode})
        if on_progress:
            on_progress(task)

        try:
            sandbox_result: SandboxExecutionResult = self.sandbox.run_tests(
                codebase=request.codebase,
                unified_diff=task.investigation_patch.unified_diff,
                test_command="pytest -p no:cacheprovider -v",
                expected_files=task.investigation_patch.files_changed,
            )
            task.sandbox_result = sandbox_result
            task.status = PipelineStatus.SANDBOX_TESTED
            self._log_event(task, "SANDBOX_TEST_EXECUTION", "COMPLETED", {
                "exit_code": sandbox_result.exit_code,
                "tests_passed": sandbox_result.tests_passed,
                "applied_patch_cleanly": sandbox_result.applied_patch_cleanly,
                "isolation_mode": sandbox_result.isolation_mode,
            })
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "SANDBOX_TEST_EXECUTION", e, on_progress)

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "SANDBOX_TEST_EXECUTION", on_progress)

        # -------------------------------------------------------------
        # STAGE C: REVIEWER AGENT
        # -------------------------------------------------------------
        task.current_stage = "STAGE_C_REVIEW"
        self._log_event(task, "STAGE_C_REVIEW", "IN_PROGRESS")
        if on_progress:
            on_progress(task)

        reviewer_prompt = f"""
You are the Principal Security & QA Auditor at RELAY AI.
Audit the proposed patch alongside real sandbox test execution evidence before presenting it for human sign-off.

[BUG SUMMARY]: {task.triage.title}
[ACCEPTANCE CRITERIA]:
{chr(10).join(f"- {c}" for c in task.triage.acceptance_criteria)}

[PROPOSED PATCH]:
{task.investigation_patch.unified_diff}

[SANDBOX TEST RESULTS]:
- Exit Code: {task.sandbox_result.exit_code}
- Tests Passed: {task.sandbox_result.tests_passed}
- Patch Applied Cleanly: {task.sandbox_result.applied_patch_cleanly}
- Standard Output:
{task.sandbox_result.stdout[:2000]}
- Standard Error:
{task.sandbox_result.stderr[:2000]}

Evaluate:
1. Security implications (injection risks, permission escalation, unsafe imports).
2. Regression risks or unhandled edge cases.
3. Recommendation:
   - "APPROVE": Tests passed, patch is clean, no security flaws.
   - "REJECT": Tests failed or security vulnerabilities detected.
   - "REVISE": Minor tweaks needed or partial test pass.
"""
        try:
            review_result: ReviewResult = self._call_gemini_structured(
                system_instruction="You are a security auditor and release manager. Audit patch and test evidence objectively.",
                prompt=reviewer_prompt,
                schema_cls=ReviewResult,
            )
            if should_cancel and should_cancel():
                return self._handle_cancellation(task, "STAGE_C_REVIEW", on_progress)
            task.reviewer = review_result
            task.current_stage = "AWAITING_HUMAN_APPROVAL"
            task.status = PipelineStatus.AWAITING_HUMAN_APPROVAL
            self._log_event(task, "STAGE_C_REVIEW", "COMPLETED", review_result.model_dump())
            if on_progress:
                on_progress(task)
            return task
        except Exception as e:
            return self._handle_failure(task, "STAGE_C_REVIEW", e, on_progress)

    def _handle_cancellation(
        self,
        task: PipelineTaskResponse,
        stage: str,
        on_progress: Optional[Callable[[PipelineTaskResponse], None]],
    ) -> PipelineTaskResponse:
        task.status = PipelineStatus.CANCELLED
        task.current_stage = "CANCELLED"
        self._log_event(task, stage, "CANCELLED")
        if on_progress:
            on_progress(task)
        return task

    def _handle_failure(
        self,
        task: PipelineTaskResponse,
        stage: str,
        error: Exception,
        on_progress: Optional[Callable[[PipelineTaskResponse], None]],
    ) -> PipelineTaskResponse:
        """
        Gracefully handles pipeline failures or Gemini 429 quota exhaustion.
        Records stage error details without crashing the host process.
        """
        is_quota_error = False
        error_msg = str(error)
        if isinstance(error, APIError):
            if error.code == 429 or "RESOURCE_EXHAUSTED" in error_msg:
                is_quota_error = True
                error_msg = "Gemini API Quota Exhausted (429 RESOURCE_EXHAUSTED). Pipeline stopped safely."

        task.status = PipelineStatus.FAILED
        task.current_stage = "FAILED"
        task.error = {
            "failed_stage": stage,
            "error_type": error.__class__.__name__,
            "message": error_msg,
            "is_quota_error": is_quota_error,
            "timestamp": self._now(),
        }
        self._log_event(task, stage, "FAILED", task.error)
        if on_progress:
            on_progress(task)
        return task
