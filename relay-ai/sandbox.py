"""
RELAY AI — Sandbox Test Runner
Deterministic test execution engine using Git for patch validation and application,
with Docker isolation when available and a sanitized subprocess dev-runner fallback.
"""

from __future__ import annotations

import os
import resource
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from path_utils import normalize_relative_path, normalize_snapshot_paths
from redaction import redact_sensitive_text
from schemas import IsolationMode, SandboxExecutionResult


class SandboxSecurityError(Exception):
    """Raised when an operation violates sandbox containment policies."""
    pass


class SandboxRunner:
    """
    Manages isolated workspace environments, patch application via Git,
    and deterministic test execution via pytest.
    """

    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self.docker_available = self._detect_docker()
        self.allow_unsafe_subprocess = os.environ.get("RELAY_ALLOW_UNSAFE_SUBPROCESS", "false").lower() == "true"
        self.docker_image = os.environ.get("RELAY_SANDBOX_IMAGE", "relay-ai-sandbox:python3.11")
        profile = os.environ.get("RELAY_TEST_PROFILE", "pytest").strip().lower()
        profiles = {
            "pytest": ["python", "-m", "pytest", "-p", "no:cacheprovider", "-v"],
            "unittest": ["python", "-m", "unittest", "discover", "-v"],
        }
        if profile not in profiles:
            raise ValueError(f"Unsupported RELAY_TEST_PROFILE '{profile}'. Choose one of: {', '.join(profiles)}.")
        self.test_profile = profile
        self.test_argv = profiles[profile]
        self.sandbox_image_available = self._detect_sandbox_image()

    def _detect_docker(self) -> bool:
        """Checks if Docker CLI and daemon are accessible."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return False
        try:
            res = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return res.returncode == 0
        except Exception:
            return False

    def _detect_sandbox_image(self) -> bool:
        if not self.docker_available:
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.docker_image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _read_output(stream, limit: int = 1024 * 1024) -> str:
        stream.seek(0)
        data = stream.read(limit + 1)
        clipped = len(data) > limit or stream.tell() >= limit
        text = redact_sensitive_text(data[:limit].decode("utf-8", errors="replace"))
        return text + ("\n[output truncated at 1 MiB]" if clipped else "")

    @staticmethod
    def _limit_child_file_output() -> None:
        """Cap each captured output file and disable core dumps for the child tree."""
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def _execute_bounded(self, command, cwd: str, timeout: int, env=None) -> Tuple[int, str, str]:
        """Run a process with bounded in-memory output and a hard wall-clock timeout."""
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    preexec_fn=self._limit_child_file_output if os.name == "posix" else None,
                )
                try:
                    exit_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    if command and command[0] == "docker" and "--name" in command:
                        container_name = command[command.index("--name") + 1]
                        try:
                            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=3)
                        except Exception:
                            pass
                    return 124, self._read_output(stdout_file), (
                        self._read_output(stderr_file) + f"\nExecution timed out after {timeout} seconds."
                    )
                return exit_code, self._read_output(stdout_file), self._read_output(stderr_file)
            except Exception as e:
                return 1, "", redact_sensitive_text(f"Execution failed: {str(e)}")

    @property
    def isolation_mode(self) -> IsolationMode:
        if self.sandbox_image_available:
            return "docker"
        return "subprocess_dev" if self.allow_unsafe_subprocess else "unavailable"

    def validate_patch_syntax(self, repo_dir: Path, unified_diff: str) -> Tuple[bool, Optional[str]]:
        """
        Validates the proposed unified diff using native `git apply --check`.
        Returns (is_valid, error_message).
        """
        patch_file = None
        try:
            # Ensure newline at end of patch
            clean_patch = unified_diff if unified_diff.endswith("\n") else unified_diff + "\n"
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".patch", delete=False) as handle:
                handle.write(clean_patch)
                patch_file = Path(handle.name)
            result = subprocess.run(
                ["git", "apply", "--check", "--recount", str(patch_file)],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, None
            return False, redact_sensitive_text(result.stderr or result.stdout or "git apply --check failed with non-zero exit code")
        except subprocess.TimeoutExpired:
            return False, "git apply --check timed out after 10s"
        except Exception as e:
            return False, redact_sensitive_text(f"Patch validation error: {str(e)}")
        finally:
            if patch_file and patch_file.exists():
                patch_file.unlink(missing_ok=True)

    def validate_patch_scope(
        self,
        unified_diff: str,
        declared_files: Optional[list[str]] = None,
        source_files: Optional[set[str] | list[str]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Require every patch header to name the same safe, existing source file."""
        if not isinstance(unified_diff, str):
            return False, "Patch must be UTF-8 text."
        try:
            if len(unified_diff.encode("utf-8")) > 1_000_000:
                return False, "Patch exceeds the 1 MiB limit."
        except UnicodeEncodeError:
            return False, "Patch contains invalid UTF-8 text."
        targets = set()
        try:
            lines = unified_diff.splitlines()
            diff_sections = [
                (index, line)
                for index, line in enumerate(lines)
                if line.startswith("diff --git ")
            ]
            for section_index, (index, line) in enumerate(diff_sections):
                next_diff = (
                    diff_sections[section_index + 1][0]
                    if section_index + 1 < len(diff_sections)
                    else len(lines)
                )
                metadata = lines[index + 1:next_diff]
                if any(
                    item.startswith((
                        "old mode ", "new mode ", "new file mode ", "deleted file mode ",
                        "rename from ", "rename to ", "copy from ", "copy to ",
                        "GIT binary patch", "Binary files ",
                    ))
                    for item in metadata
                ):
                    return False, "Patch may only change text contents; file modes, renames, and binary patches are not supported."
                parts = shlex.split(line)
                if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                    return False, "Malformed diff header."
                old_target, target = parts[2][2:], parts[3][2:]
                if old_target != target:
                    return False, "Patch may only modify existing files; additions, deletions, and renames are not supported."
                try:
                    normalized_target, _ = normalize_relative_path(target)
                except ValueError:
                    return False, f"Unsafe patch target path: {target!r}"
                if normalized_target != target:
                    return False, f"Patch target path is not in canonical form: {target!r}"
                if target in targets:
                    return False, f"Patch contains multiple diff sections for {target!r}."
                file_headers = [item for item in metadata if item.startswith(("--- ", "+++ "))]
                if len(file_headers) < 2:
                    return False, f"Patch is missing unified file headers for {target!r}."
                try:
                    old_header = shlex.split(file_headers[0][4:])
                    new_header = shlex.split(file_headers[1][4:])
                except ValueError:
                    return False, f"Malformed unified file headers for {target!r}."
                if old_header != [f"a/{target}"] or new_header != [f"b/{target}"]:
                    return False, f"Unified file headers do not match diff target {target!r}."
                targets.add(target)
            if not targets:
                return False, "Patch has no diff file headers."
            if len(targets) > 25:
                return False, "Patch exceeds the 25-file change limit."
            if declared_files is not None:
                declared_list = [name.replace("\\", "/") for name in declared_files]
                declared = set(declared_list)
                if len(declared) != len(declared_list) or targets != declared:
                    return False, "Diff targets do not match the declared files_changed list."
            if source_files is not None:
                normalized_sources = {name.replace("\\", "/") for name in source_files}
                missing = targets - normalized_sources
                if missing:
                    return False, f"Patch targets files outside the submitted source snapshot: {sorted(missing)}"
            return True, None
        except ValueError as error:
            return False, redact_sensitive_text(f"Could not parse diff headers: {error}")

    def apply_patch(self, repo_dir: Path, unified_diff: str) -> Tuple[bool, Optional[str]]:
        """
        Applies the unified diff directly using `git apply`.
        """
        patch_file = None
        try:
            clean_patch = unified_diff if unified_diff.endswith("\n") else unified_diff + "\n"
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".patch", delete=False) as handle:
                handle.write(clean_patch)
                patch_file = Path(handle.name)
            result = subprocess.run(
                ["git", "apply", "--recount", str(patch_file)],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, None
            return False, redact_sensitive_text(result.stderr or result.stdout or "git apply failed")
        except Exception as e:
            return False, redact_sensitive_text(f"Failed to apply patch: {str(e)}")
        finally:
            if patch_file and patch_file.exists():
                patch_file.unlink(missing_ok=True)

    def run_tests(
        self,
        codebase: Dict[str, str],
        unified_diff: Optional[str] = None,
        expected_files: Optional[list[str]] = None,
    ) -> SandboxExecutionResult:
        """
        Populates an isolated workspace, initializes Git, applies patch (if supplied),
        runs the configured allow-listed test profile, and gathers execution telemetry.
        """
        start_time = time.perf_counter()
        workspace_dir = Path(tempfile.mkdtemp(prefix="relay_sandbox_"))
        test_argv = self.test_argv
        test_command = " ".join(test_argv)

        try:
            if unified_diff and unified_diff.strip():
                in_scope, scope_error = self.validate_patch_scope(unified_diff, expected_files, set(codebase))
                if not in_scope:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000
                    return SandboxExecutionResult(
                        test_command=test_command,
                        exit_code=1,
                        tests_passed=False,
                        stdout="",
                        stderr=f"Patch rejected by scope validation: {scope_error}",
                        execution_time_ms=round(elapsed_ms, 2),
                        isolation_mode=self.isolation_mode,
                        applied_patch_cleanly=False,
                        patch_error=scope_error,
                    )
            # 1. Populate workspace safely (prevent directory traversal / zip-slip)
            self._populate_workspace(workspace_dir, codebase)

            # 2. Initialize Git repo in the sandbox for atomic patch tracking
            self._init_git_repo(workspace_dir)

            applied_cleanly = True
            patch_err = None

            # 3. Validate & Apply Patch if provided
            if unified_diff and unified_diff.strip():
                is_valid, check_err = self.validate_patch_syntax(workspace_dir, unified_diff)
                if not is_valid:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000
                    return SandboxExecutionResult(
                        test_command=test_command,
                        exit_code=1,
                        tests_passed=False,
                        stdout="",
                        stderr=f"Patch rejected by git apply --check:\n{check_err}",
                        execution_time_ms=round(elapsed_ms, 2),
                        isolation_mode=self.isolation_mode,
                        applied_patch_cleanly=False,
                        patch_error=check_err,
                    )

                applied, apply_err = self.apply_patch(workspace_dir, unified_diff)
                if not applied:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000
                    return SandboxExecutionResult(
                        test_command=test_command,
                        exit_code=1,
                        tests_passed=False,
                        stdout="",
                        stderr=f"Patch application failed:\n{apply_err}",
                        execution_time_ms=round(elapsed_ms, 2),
                        isolation_mode=self.isolation_mode,
                        applied_patch_cleanly=False,
                        patch_error=apply_err,
                    )

            # 4. Execute test command
            mode = self.isolation_mode
            if self.sandbox_image_available:
                exit_code, stdout, stderr = self._run_in_docker(workspace_dir, test_argv)
            elif self.allow_unsafe_subprocess:
                exit_code, stdout, stderr = self._run_in_subprocess(workspace_dir, test_argv)
            else:
                exit_code, stdout, stderr = 125, "", (
                    "The isolated Docker runner or prebuilt image is unavailable. Tests were not executed. "
                    "For trusted local development only, set RELAY_ALLOW_UNSAFE_SUBPROCESS=true."
                )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            tests_passed = (exit_code == 0)

            return SandboxExecutionResult(
                test_command=test_command,
                exit_code=exit_code,
                tests_passed=tests_passed,
                stdout=stdout,
                stderr=stderr,
                execution_time_ms=round(elapsed_ms, 2),
                isolation_mode=mode,
                applied_patch_cleanly=applied_cleanly,
                patch_error=patch_err,
            )

        finally:
            # Clean up temporary sandbox directory
            shutil.rmtree(workspace_dir, ignore_errors=True)

    def _populate_workspace(self, target_dir: Path, codebase: Dict[str, str]) -> None:
        """Writes files into target_dir while enforcing path containment."""
        resolved_root = target_dir.resolve()
        try:
            normalized_codebase = normalize_snapshot_paths(codebase)
        except ValueError as error:
            raise SandboxSecurityError(str(error)) from error
        for normalized, content in normalized_codebase.items():
            candidate = Path(normalized)
            file_dest = (resolved_root / candidate).resolve()
            if not file_dest.is_relative_to(resolved_root):
                raise SandboxSecurityError(f"Directory traversal detected: '{normalized}' escapes sandbox root")

            file_dest.parent.mkdir(parents=True, exist_ok=True)
            file_dest.write_text(content, encoding="utf-8")

    def _init_git_repo(self, repo_dir: Path) -> None:
        """Initializes a local Git repository and creates an initial commit."""
        subprocess.run(["git", "init"], cwd=str(repo_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["git", "config", "user.email", "relay@bot.internal"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "config", "user.name", "RELAY AI Sandbox"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["git", "commit", "-m", "Initial sandbox commit", "--allow-empty"], cwd=str(repo_dir), stdout=subprocess.DEVNULL, check=True)

    def _run_in_subprocess(self, workspace_dir: Path, command: list[str]) -> Tuple[int, str, str]:
        """
        Executes test command in a local subprocess with sanitized environment.
        Clearly labeled as 'subprocess_dev' (development fallback only).
        """
        # Security sanitization: Strip API keys, tokens, and host secrets
        # Include current sys.path in PYTHONPATH so user-installed packages (pytest) are found
        pythonpath_entries = [
            str(workspace_dir),
            "/root/.local/lib/python3.10/site-packages",
            *sys.path,
        ]
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin") + ":/root/.local/bin",
            "PYTHONPATH": ":".join(p for p in pythonpath_entries if p),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(workspace_dir),
            "USER": "sandbox",
        }
        # Explicitly ensure GEMINI_API_KEY and other sensitive variables are NOT passed
        for secret_key in ["GEMINI_API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN"]:
            safe_env.pop(secret_key, None)

        cmd_parts = list(command)
        if cmd_parts[:2] == ["python", "-m"]:
            cmd_parts[0] = sys.executable

        try:
            return self._execute_bounded(cmd_parts, str(workspace_dir), self.timeout_seconds, env=safe_env)
        except Exception as e:
            return 1, "", redact_sensitive_text(f"Execution failed: {str(e)}")

    def _run_in_docker(self, workspace_dir: Path, command: list[str]) -> Tuple[int, str, str]:
        """
        Executes test suite inside an isolated Docker container with zero network access.
        """
        docker_cmd = [
            "docker", "run", "--rm",
            "--pull=never",
            "--name", f"relay-sandbox-{uuid.uuid4().hex[:12]}",
            "--init",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128",
            "--user", "65534:65534",
            "--memory", "512m",
            "--cpus", "1.0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{workspace_dir}:/workspace:ro",
            "-w", "/workspace",
            self.docker_image,
            *command,
        ]
        try:
            return self._execute_bounded(docker_cmd, str(workspace_dir), self.timeout_seconds + 15)
        except Exception as e:
            return 1, "", redact_sensitive_text(f"Docker execution error: {str(e)}")
