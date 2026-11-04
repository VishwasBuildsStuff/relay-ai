"""Stateful, single-task RELAY AI workflow orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from schemas import (
    DeveloperPatchSchema, InvestigationSchema, OrchestratorState,
    ReviewerSchema, SandboxResultSchema, TriageSchema,
)

T = TypeVar("T", bound=BaseModel)
MODEL = "gemini-2.5-flash"


class PipelineError(RuntimeError):
    """An expected pipeline failure with stage context."""


class RelayOrchestrator:
    def __init__(self, task_id: str, chat_message: str, codebase: dict[str, str]):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY must be set to run the RELAY pipeline")
        if not isinstance(codebase, dict) or not codebase:
            raise ValueError("codebase must be a non-empty mapping of relative paths to text")
        self.client = genai.Client(api_key=api_key)
        self.task_id = task_id
        self.chat_message = chat_message
        self.codebase = self._validate_codebase(codebase)
        self.state = OrchestratorState(task_id=task_id)

    @staticmethod
    def _validate_codebase(codebase: dict[str, str]) -> dict[str, str]:
        safe: dict[str, str] = {}
        for name, content in codebase.items():
            path = PurePosixPath(name.replace("\\", "/"))
            if not name or path.is_absolute() or ".." in path.parts or not isinstance(content, str):
                raise ValueError(f"Unsafe or invalid codebase entry: {name!r}")
            safe[str(path)] = content
        return safe

    def _invoke(self, schema: type[T], instruction: str, context: dict) -> T:
        response = self.client.models.generate_content(
            model=MODEL,
            contents=(instruction + "\n\nStructured context (JSON):\n" +
                      json.dumps(context, ensure_ascii=False)),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema,
                temperature=0.1,
            ),
        )
        if not response.text:
            raise PipelineError("Model returned an empty response")
        parsed = schema.model_validate_json(response.text)
        if getattr(parsed, "task_id", self.task_id) != self.task_id:
            raise PipelineError("Model returned an output for a different task")
        return parsed

    def _record(self, stage: str, detail: str) -> None:
        self.state.current_stage = stage
        self.state.evidence_timeline.append({"stage": stage, "detail": detail})

    def _fail(self, stage: str, exc: Exception, status: str = "FAILED") -> dict:
        self.state.status = status
        self.state.current_stage = stage
        self.state.error = {"stage": stage, "message": str(exc)}
        self._record(stage, f"Pipeline stopped: {exc}")
        return self.state.model_dump(mode="json")

    def run_triage(self) -> TriageSchema:
        result = self._invoke(TriageSchema,
            "Triage the incoming engineering issue. Extract concise metadata and verifiable acceptance criteria.",
            {"task_id": self.task_id, "chat_message": self.chat_message})
        self.state.triage = result
        self.state.status = "TRIAGED"
        self._record("triage", result.summary)
        return result

    def run_investigation(self) -> InvestigationSchema:
        result = self._invoke(InvestigationSchema,
            "Inspect the supplied repository files. Identify the actual defect, function, root cause and textual line evidence. Be conservative with confidence.",
            {"triage": self.state.triage.model_dump(mode="json"), "files": self.codebase})
        self.state.investigation = result
        self.state.status = "INVESTIGATED"
        self._record("investigation", f"{result.buggy_file}: {result.root_cause}")
        if result.confidence_score < 0.7:
            raise PipelineError(f"Investigation confidence {result.confidence_score:.2f} is below 0.70")
        return result

    def run_developer(self) -> DeveloperPatchSchema:
        result = self._invoke(DeveloperPatchSchema,
            "Propose the smallest safe unified Git diff for the diagnosed bug. The diff must apply to target_file and avoid unrelated edits.",
            {"triage": self.state.triage.model_dump(mode="json"),
             "investigation": self.state.investigation.model_dump(mode="json"), "files": self.codebase})
        if result.target_file not in self.codebase:
            raise PipelineError(f"Patch targets unknown file: {result.target_file}")
        if not result.proposed_patch.startswith("--- ") or "+++ " not in result.proposed_patch:
            raise PipelineError("Developer output is not a unified diff")
        self.state.developer_patch = result
        self.state.status = "PATCH_PROPOSED"
        self._record("developer", result.summary_of_changes)
        return result

    @staticmethod
    def _apply_patch(root: Path, patch: str) -> None:
        # Restrict patch headers to workspace-relative files before invoking git.
        for line in patch.splitlines():
            if line.startswith(("--- ", "+++ ")):
                raw = line[4:].split("\t", 1)[0].split(" ", 1)[0]
                if raw != "/dev/null":
                    name = raw.removeprefix("a/").removeprefix("b/")
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts:
                        raise PipelineError("Patch contains an unsafe path")
        applied = subprocess.run(["git", "apply", "--unsafe-paths"], input=patch,
                                 text=True, capture_output=True, cwd=root, timeout=15, check=False)
        if applied.returncode:
            raise PipelineError(f"Patch could not be applied: {applied.stderr.strip()}")

    def run_sandbox_tests(self) -> SandboxResultSchema:
        with tempfile.TemporaryDirectory(prefix="relay-sandbox-") as tmp:
            root = Path(tmp).resolve()
            for name, content in self.codebase.items():
                destination = (root / Path(*PurePosixPath(name).parts)).resolve()
                if root not in destination.parents:
                    raise PipelineError("Codebase path escaped sandbox")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            self._apply_patch(root, self.state.developer_patch.proposed_patch)
            tests = [name for name in self.codebase if name.startswith("tests/") or
                     name.startswith("test_") or "/test_" in name]
            command = ["python", "-m", "pytest", "-q", *(tests or ["."])]
            try:
                run = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                     timeout=120, check=False)
                result = SandboxResultSchema(task_id=self.task_id,
                    test_command=" ".join(command), exit_code=run.returncode,
                    stdout=run.stdout[-20000:], stderr=run.stderr[-20000:],
                    tests_passed=run.returncode == 0)
            except subprocess.TimeoutExpired as exc:
                result = SandboxResultSchema(task_id=self.task_id,
                    test_command=" ".join(command), exit_code=124,
                    stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
                    stderr="pytest exceeded the 120 second timeout", tests_passed=False)
        self.state.sandbox_result = result
        self._record("sandbox", f"pytest exit code {result.exit_code}")
        if result.exit_code != 0:
            self.state.status = "FAILED"
            self.state.error = {"stage": "sandbox", "message": "Sandbox tests did not pass"}
            raise PipelineError("Sandbox tests did not pass")
        self.state.status = "TESTED"
        return result

    def run_reviewer(self) -> ReviewerSchema:
        result = self._invoke(ReviewerSchema,
            "Review the proposed diff and actual sandbox logs for security issues and regressions. Recommend approval only when supported by the evidence.",
            {"patch": self.state.developer_patch.model_dump(mode="json"),
             "sandbox": self.state.sandbox_result.model_dump(mode="json"),
             "triage": self.state.triage.model_dump(mode="json"),
             "investigation": self.state.investigation.model_dump(mode="json")})
        self.state.reviewer = result
        self._record("reviewer", f"{result.recommendation}: {result.review_summary}")
        if result.recommendation == "REJECT":
            self.state.status = "REJECTED"
        elif result.recommendation == "NEEDS_REVISION":
            self.state.status = "FAILED"
            self.state.error = {"stage": "reviewer", "message": "Reviewer requested revision"}
        else:
            self.state.status = "AWAITING_HUMAN_APPROVAL"
        return result

    def execute_pipeline(self) -> dict:
        try:
            self.run_triage()
            self.run_investigation()
            self.run_developer()
            self.run_sandbox_tests()
            self.run_reviewer()
        except Exception as exc:
            if self.state.error is None:
                self._fail(self.state.current_stage, exc)
        return self.state.model_dump(mode="json")
