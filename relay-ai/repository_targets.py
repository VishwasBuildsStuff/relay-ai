"""Allow-listed local Git repository targets for explicit apply/commit actions."""

from __future__ import annotations

import json
import fcntl
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class RepositoryTargetError(ValueError):
    """Raised when a configured target cannot be used safely."""


@dataclass(frozen=True)
class RepositoryState:
    repository_id: str
    branch: str
    revision: str
    clean: bool


class RepositoryTargetRegistry:
    """Resolve user-selected IDs only through a trusted operator configuration."""

    def __init__(self, raw_config: str | None = None, lock_root: Path | None = None):
        self._targets: dict[str, Path] = {}
        self._lock_root = lock_root or Path(tempfile.gettempdir()) / "relay-ai-target-locks"
        if not raw_config:
            return
        try:
            configured: Any = json.loads(raw_config)
        except json.JSONDecodeError as error:
            raise RuntimeError("RELAY_REPOSITORIES_JSON must be a JSON object of target IDs to local paths.") from error
        if not isinstance(configured, dict) or len(configured) > 50:
            raise RuntimeError("RELAY_REPOSITORIES_JSON must contain at most 50 repository targets.")
        for repository_id, raw_path in configured.items():
            if not isinstance(repository_id, str) or not _TARGET_ID.fullmatch(repository_id):
                raise RuntimeError("Repository target IDs must be 1..64 letters, digits, dots, underscores, or hyphens.")
            if not isinstance(raw_path, str) or not raw_path:
                raise RuntimeError(f"Repository target {repository_id!r} must map to a local directory path.")
            path = Path(raw_path).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise RuntimeError(f"Configured repository target {repository_id!r} does not exist.") from error
            if not resolved.is_dir():
                raise RuntimeError(f"Configured repository target {repository_id!r} is not a directory.")
            repository_root = Path(str(self._git(resolved, ["rev-parse", "--show-toplevel"])).strip()).resolve(strict=True)
            if repository_root != resolved:
                raise RuntimeError(f"Configured repository target {repository_id!r} must name the Git repository root.")
            self._targets[repository_id] = resolved

    @staticmethod
    def _git(path: Path, args: list[str], *, binary: bool = False) -> str | bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args], capture_output=True, text=not binary,
                timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            raise RepositoryTargetError(f"Git operation failed: {detail}") from error
        return result.stdout

    def _path(self, repository_id: str) -> Path:
        try:
            configured_path = self._targets[repository_id]
        except KeyError as error:
            raise RepositoryTargetError("Repository target is not configured.") from error
        try:
            current_path = configured_path.resolve(strict=True)
            current_root = Path(str(self._git(current_path, ["rev-parse", "--show-toplevel"])).strip()).resolve(strict=True)
        except (OSError, RuntimeError, RepositoryTargetError) as error:
            raise RepositoryTargetError("Configured repository target is no longer available.") from error
        if current_path != configured_path or current_root != configured_path:
            raise RepositoryTargetError("Configured repository target path changed after service startup.")
        return configured_path

    def list_targets(self) -> list[dict[str, Any]]:
        result = []
        for repository_id in sorted(self._targets):
            state = self.inspect(repository_id)
            result.append({"repository_id": repository_id, "branch": state.branch, "revision": state.revision, "clean": state.clean})
        return result

    def inspect(self, repository_id: str) -> RepositoryState:
        path = self._path(repository_id)
        branch = str(self._git(path, ["branch", "--show-current"])).strip()
        revision = str(self._git(path, ["rev-parse", "HEAD"])).strip()
        porcelain = str(self._git(path, ["status", "--porcelain=v1", "--untracked-files=all"])).strip()
        return RepositoryState(repository_id, branch, revision, not porcelain)

    def path(self, repository_id: str) -> Path:
        return self._path(repository_id)

    @contextmanager
    def target_lock(self, repository_id: str):
        """Serialize RELAY apply/commit actions across API processes on this host."""
        self._path(repository_id)
        if self._lock_root.is_symlink():
            raise RepositoryTargetError("Repository operation lock directory must not be a symlink.")
        self._lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._lock_root, 0o700)
        lock_path = self._lock_root / f"{repository_id}.lock"
        if lock_path.is_symlink():
            raise RepositoryTargetError("Repository operation lock file must not be a symlink.")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def require_clean_base(self, repository_id: str, expected_revision: str | None = None) -> RepositoryState:
        state = self.inspect(repository_id)
        if not state.clean:
            raise RepositoryTargetError("Target repository has local changes; commit or remove them before applying a patch.")
        if expected_revision is not None and state.revision != expected_revision:
            raise RepositoryTargetError("Target repository has moved from the task's recorded base revision.")
        return state

    def verify_snapshot_files(self, repository_id: str, revision: str, codebase: dict[str, str], files: list[str]) -> None:
        path = self._path(repository_id)
        for relative_path in files:
            if relative_path not in codebase:
                raise RepositoryTargetError(f"Changed file {relative_path!r} is absent from the submitted snapshot.")
            target_bytes = self._git(path, ["show", f"{revision}:{relative_path}"], binary=True)
            if target_bytes != codebase[relative_path].encode("utf-8"):
                raise RepositoryTargetError(f"Submitted snapshot does not match target base for {relative_path!r}.")

    def diff_digest(self, repository_id: str) -> str:
        import hashlib

        patch = self._git(self._path(repository_id), ["diff", "--binary", "--no-ext-diff"], binary=True)
        return hashlib.sha256(patch).hexdigest()

    def recover_applied_patch(self, repository_id: str, revision: str, files: list[str], unified_diff: str) -> bool:
        """Recognize a complete Git-applied patch after a crash between apply and persistence."""
        state = self.inspect(repository_id)
        if state.revision != revision:
            raise RepositoryTargetError("Target repository base revision changed during patch recovery.")
        if state.clean:
            return False
        raw_status = self._git(
            self._path(repository_id), ["status", "--porcelain=v1", "-z", "--untracked-files=all"], binary=True
        )
        changed: set[str] = set()
        for record in raw_status.split(b"\0"):
            if not record:
                continue
            if len(record) < 4 or record[:2] != b" M":
                raise RepositoryTargetError("Target repository has unexpected changes during patch recovery.")
            changed.add(record[3:].decode("utf-8", "strict"))
        if changed != set(files):
            raise RepositoryTargetError("Target repository changes do not match the approved patch during recovery.")
        patch_file: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".patch", delete=False) as handle:
                handle.write(unified_diff if unified_diff.endswith("\n") else unified_diff + "\n")
                patch_file = Path(handle.name)
            result = subprocess.run(
                ["git", "-C", str(self._path(repository_id)), "apply", "--reverse", "--check", "--recount", str(patch_file)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RepositoryTargetError("Could not verify the applied patch during recovery.") from error
        finally:
            if patch_file:
                patch_file.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RepositoryTargetError("Target repository changes do not match the approved patch during recovery.")
        return True

    def verify_applied_patch(self, repository_id: str, revision: str, files: list[str], digest: str) -> None:
        state = self.inspect(repository_id)
        if state.revision != revision:
            raise RepositoryTargetError("Target repository base revision changed after patch application.")
        raw_status = self._git(
            self._path(repository_id), ["status", "--porcelain=v1", "-z", "--untracked-files=all"], binary=True
        )
        changed: set[str] = set()
        for record in raw_status.split(b"\0"):
            if not record:
                continue
            if len(record) < 4 or record[:2] != b" M":
                raise RepositoryTargetError("Target repository has staged, untracked, or unexpected changes.")
            changed.add(record[3:].decode("utf-8", "strict"))
        if changed != set(files):
            raise RepositoryTargetError("Target repository changes no longer match the approved patch files.")
        if self.diff_digest(repository_id) != digest:
            raise RepositoryTargetError("Target repository patch changed after approval.")

    def commit_patch(self, repository_id: str, revision: str, files: list[str], digest: str, message: str) -> str:
        state = self.inspect(repository_id)
        if state.revision != revision:
            parent = str(self._git(self._path(repository_id), ["rev-parse", "HEAD^"])).strip()
            recovered_message = str(self._git(self._path(repository_id), ["show", "-s", "--format=%B", "HEAD"])).rstrip("\n")
            recovered_files = self._git(
                self._path(repository_id), ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"], binary=True
            )
            recovered_diff = self._git(
                self._path(repository_id), ["diff", "--binary", "--no-ext-diff", f"{revision}..HEAD"], binary=True
            )
            import hashlib

            if (
                state.clean and parent == revision and recovered_message == message
                and set(recovered_files.decode("utf-8", "strict").split("\0")) - {""} == set(files)
                and hashlib.sha256(recovered_diff).hexdigest() == digest
            ):
                return state.revision
            raise RepositoryTargetError("Target repository advanced after patch application; refusing a second commit.")
        path = self._path(repository_id)
        try:
            self.verify_applied_patch(repository_id, revision, files, digest)
            self._git(path, ["add", "--", *files])
        except RepositoryTargetError:
            if not self._verify_staged_patch(repository_id, revision, files, digest):
                raise
        self._git(path, [
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=RELAY AI", "-c", "user.email=relay-ai@localhost",
            "commit", "-m", message,
        ])
        return str(self._git(path, ["rev-parse", "HEAD"])).strip()

    def _verify_staged_patch(self, repository_id: str, revision: str, files: list[str], digest: str) -> bool:
        """Resume a commit whose approved files were staged before an interrupted commit."""
        if self.inspect(repository_id).revision != revision:
            return False
        path = self._path(repository_id)
        raw_status = self._git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], binary=True)
        changed: set[str] = set()
        for record in raw_status.split(b"\0"):
            if not record:
                continue
            if len(record) < 4 or record[:2] != b"M ":
                return False
            changed.add(record[3:].decode("utf-8", "strict"))
        if changed != set(files):
            return False
        staged = self._git(path, ["diff", "--cached", "--binary", "--no-ext-diff"], binary=True)
        unstaged = self._git(path, ["diff", "--binary", "--no-ext-diff"], binary=True)
        import hashlib

        return hashlib.sha256(staged).hexdigest() == digest and not unstaged
