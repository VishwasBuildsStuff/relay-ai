"""
RELAY AI — 3-Stage Agentic Pipeline Orchestrator
Sequentially coordinates Triage (Stage A), Investigation & Development (Stage B),
Deterministic Sandbox Pytest Execution, and Security/Regression Review (Stage C).
"""

from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any, Callable, Dict, Optional

from google.genai.errors import APIError

from sandbox import SandboxRunner
from providers import GeminiStructuredProvider, StructuredProvider
from redaction import redact_sensitive_text
from pricing import configured_gemini_pricing, estimate_gemini_cost_usd
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
        self.gemini_pricing = configured_gemini_pricing()
        self.sandbox = SandboxRunner()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _log_event(
        self, task: PipelineTaskResponse, stage: str, status: str,
        details: Optional[Dict[str, Any]] = None, actor: str = "system",
    ) -> None:
        event = EvidenceEvent(
            timestamp=self._now(),
            stage=stage,
            status=status,
            actor=actor,
            correlation_id=task.task_id,
            details=details or {},
        )
        task.evidence_timeline.append(event)
        task.updated_at = self._now()

    def _call_gemini_structured(
        self,
        task: PipelineTaskResponse,
        stage: str,
        system_instruction: str,
        prompt: str,
        schema_cls: Any,
    ) -> Any:
        """
        Executes a structured JSON call using official google-genai SDK with Pydantic schema.
        Handles APIError and quota limits safely.
        """
        if not self.provider:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. Please configure GEMINI_API_KEY to run AI stages."
            )
        result = self.provider.generate(system_instruction, prompt, schema_cls)
        usage = getattr(self.provider, "last_usage", None)
        if usage is not None:
            usage_details = {
                "provider": "gemini",
                "model": self.model_name,
                "stage": stage,
                **usage,
            }
            if self.gemini_pricing is not None:
                input_rate, output_rate, label = self.gemini_pricing
                usage_details.update({
                    "input_usd_per_million": str(input_rate),
                    "output_usd_per_million": str(output_rate),
                    "estimated_cost_usd": str(estimate_gemini_cost_usd(
                        int(usage.get("prompt_tokens", 0)),
                        int(usage.get("candidate_tokens", 0)),
                        self.gemini_pricing,
                    )),
                    "pricing_label": label,
                })
            self._log_event(task, "MODEL_USAGE", "RECORDED", {
                **usage_details,
            })
        return result

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
            status=PipelineStatus.RUNNING,
            current_stage="RECEIVED",
            bug_report=request.chat_message,
            created_at=now,
            updated_at=now,
            evidence_timeline=[],
        )
        self._log_event(task, "INGESTION", "STARTED", {"files_count": len(request.codebase)})
        if on_progress:
            on_progress(task)

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "INGESTION", on_progress)

        # -------------------------------------------------------------
        # STAGE A: TRIAGE AGENT
        # -------------------------------------------------------------
        task.transition_to(PipelineStatus.TRIAGING)
        task.current_stage = "STAGE_A_TRIAGE"
        triage_started = time.perf_counter()
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
                task=task,
                stage="STAGE_A_TRIAGE",
                system_instruction="You are a senior triage engineer. Classify severity and define strict acceptance criteria.",
                prompt=triage_prompt,
                schema_cls=TriageResult,
            )
            task.triage = triage_result
            task.transition_to(PipelineStatus.TRIAGED)
            self._log_event(task, "STAGE_A_TRIAGE", "COMPLETED", {
                **triage_result.model_dump(),
                "duration_ms": round((time.perf_counter() - triage_started) * 1000, 2),
            })
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "STAGE_A_TRIAGE", e, on_progress, round((time.perf_counter() - triage_started) * 1000, 2))

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "STAGE_A_TRIAGE", on_progress)

        # -------------------------------------------------------------
        # STAGE B: INVESTIGATION & DEVELOPMENT AGENT
        # -------------------------------------------------------------
        task.transition_to(PipelineStatus.INVESTIGATING)
        task.current_stage = "STAGE_B_INVESTIGATION_AND_DEV"
        investigation_started = time.perf_counter()
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
                task=task,
                stage="STAGE_B_INVESTIGATION_AND_DEV",
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
                set(request.codebase),
            )
            if not in_scope:
                raise ValueError(f"Generated patch failed path/scope validation: {scope_error}")

            task.investigation_patch = inv_patch_result
            task.transition_to(PipelineStatus.PATCHED)
            self._log_event(task, "STAGE_B_INVESTIGATION_AND_DEV", "COMPLETED", {
                "buggy_file": inv_patch_result.buggy_file,
                "affected_function": inv_patch_result.affected_function,
                "confidence_score": inv_patch_result.confidence_score,
                "duration_ms": round((time.perf_counter() - investigation_started) * 1000, 2),
            })
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "STAGE_B_INVESTIGATION_AND_DEV", e, on_progress, round((time.perf_counter() - investigation_started) * 1000, 2))

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "STAGE_B_INVESTIGATION_AND_DEV", on_progress)

        # -------------------------------------------------------------
        # DETERMINISTIC SANDBOX TEST RUNNER (Stage B -> Stage C Bridge)
        # -------------------------------------------------------------
        task.transition_to(PipelineStatus.SANDBOX_RUNNING)
        task.current_stage = "SANDBOX_TEST_EXECUTION"
        sandbox_started = time.perf_counter()
        self._log_event(task, "SANDBOX_TEST_EXECUTION", "IN_PROGRESS", {"isolation": self.sandbox.isolation_mode})
        if on_progress:
            on_progress(task)

        try:
            sandbox_result: SandboxExecutionResult = self.sandbox.run_tests(
                codebase=request.codebase,
                unified_diff=task.investigation_patch.unified_diff,
                expected_files=task.investigation_patch.files_changed,
            )
            task.sandbox_result = sandbox_result
            task.transition_to(PipelineStatus.SANDBOX_TESTED)
            self._log_event(task, "SANDBOX_TEST_EXECUTION", "COMPLETED", {
                "exit_code": sandbox_result.exit_code,
                "tests_passed": sandbox_result.tests_passed,
                "applied_patch_cleanly": sandbox_result.applied_patch_cleanly,
                "isolation_mode": sandbox_result.isolation_mode,
                "duration_ms": round((time.perf_counter() - sandbox_started) * 1000, 2),
            })
            if on_progress:
                on_progress(task)
        except Exception as e:
            return self._handle_failure(task, "SANDBOX_TEST_EXECUTION", e, on_progress, round((time.perf_counter() - sandbox_started) * 1000, 2))

        if should_cancel and should_cancel():
            return self._handle_cancellation(task, "SANDBOX_TEST_EXECUTION", on_progress)

        # -------------------------------------------------------------
        # STAGE C: REVIEWER AGENT
        # -------------------------------------------------------------
        task.transition_to(PipelineStatus.REVIEWING)
        task.current_stage = "STAGE_C_REVIEW"
        review_started = time.perf_counter()
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
                task=task,
                stage="STAGE_C_REVIEW",
                system_instruction="You are a security auditor and release manager. Audit patch and test evidence objectively.",
                prompt=reviewer_prompt,
                schema_cls=ReviewResult,
            )
            if should_cancel and should_cancel():
                return self._handle_cancellation(task, "STAGE_C_REVIEW", on_progress)
            task.reviewer = review_result
            task.current_stage = "AWAITING_HUMAN_APPROVAL"
            task.transition_to(PipelineStatus.AWAITING_HUMAN_APPROVAL)
            self._log_event(task, "STAGE_C_REVIEW", "COMPLETED", {
                **review_result.model_dump(),
                "duration_ms": round((time.perf_counter() - review_started) * 1000, 2),
            })
            return task
        except Exception as e:
            return self._handle_failure(task, "STAGE_C_REVIEW", e, on_progress, round((time.perf_counter() - review_started) * 1000, 2))

    def _handle_cancellation(
        self,
        task: PipelineTaskResponse,
        stage: str,
        on_progress: Optional[Callable[[PipelineTaskResponse], None]],
    ) -> PipelineTaskResponse:
        task.transition_to(PipelineStatus.CANCELLED)
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
        duration_ms: Optional[float] = None,
    ) -> PipelineTaskResponse:
        """
        Gracefully handles pipeline failures or Gemini 429 quota exhaustion.
        Records stage error details without crashing the host process.
        """
        is_quota_error = False
        error_msg = redact_sensitive_text(error)
        if isinstance(error, APIError):
            if error.code == 429 or "RESOURCE_EXHAUSTED" in error_msg:
                is_quota_error = True
                error_msg = "Gemini API Quota Exhausted (429 RESOURCE_EXHAUSTED). Pipeline stopped safely."

        task.transition_to(PipelineStatus.FAILED)
        task.current_stage = "FAILED"
        task.error = {
            "failed_stage": stage,
            "error_type": error.__class__.__name__,
            "message": error_msg,
            "is_quota_error": is_quota_error,
            "timestamp": self._now(),
        }
        if duration_ms is not None:
            task.error["duration_ms"] = duration_ms
        self._log_event(task, stage, "FAILED", task.error)
        if on_progress:
            on_progress(task)
        return task
