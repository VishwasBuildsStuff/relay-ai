"""Disposable-repository rehearsal for guarded apply, commit, and crash recovery."""

import hashlib
import json
import subprocess

import pytest

from repository_targets import RepositoryTargetError, RepositoryTargetRegistry


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _target(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RELAY Drill")
    _git(repository, "config", "user.email", "relay-drill@example.invalid")
    (repository / "changed.txt").write_text("before\n", encoding="utf-8")
    (repository / "untouched.txt").write_text("keep\n", encoding="utf-8")
    _git(repository, "add", "--", "changed.txt", "untouched.txt")
    _git(repository, "commit", "-m", "initial")
    registry = RepositoryTargetRegistry(
        json.dumps({"drill": str(repository)}), lock_root=tmp_path / "locks"
    )
    return repository, registry


def test_stale_base_is_rejected(tmp_path):
    repository, registry = _target(tmp_path)
    recorded_revision = registry.inspect("drill").revision
    (repository / "untouched.txt").write_text("external change\n", encoding="utf-8")
    _git(repository, "add", "--", "untouched.txt")
    _git(repository, "commit", "-m", "external advance")

    with pytest.raises(RepositoryTargetError, match="moved from the task's recorded base"):
        registry.require_clean_base("drill", recorded_revision)


def test_changed_file_only_commit_and_interrupted_commit_recovery(tmp_path):
    repository, registry = _target(tmp_path)
    base = registry.require_clean_base("drill").revision
    snapshot = {"changed.txt": "before\n", "untouched.txt": "keep\n"}
    registry.verify_snapshot_files("drill", base, snapshot, ["changed.txt"])
    patch = (
        "diff --git a/changed.txt b/changed.txt\n"
        "--- a/changed.txt\n"
        "+++ b/changed.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )
    patch_path = tmp_path / "approved.patch"
    patch_path.write_text(patch, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "apply", str(patch_path)], check=True)
    assert registry.recover_applied_patch("drill", base, ["changed.txt"], patch)

    digest = registry.diff_digest("drill")
    # Simulate a crash after staging but before commit. The retry must validate
    # the staged diff and finish the original approved operation.
    _git(repository, "add", "--", "changed.txt")
    result_revision = registry.commit_patch("drill", base, ["changed.txt"], digest, "fix: update one file")
    assert _git(repository, "show", "HEAD:changed.txt") == "after"
    assert _git(repository, "show", "HEAD:untouched.txt") == "keep"
    committed_files = set(_git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert committed_files == {"changed.txt"}

    # Simulate loss of the service acknowledgement after Git created the commit.
    assert registry.commit_patch("drill", base, ["changed.txt"], digest, "fix: update one file") == result_revision
