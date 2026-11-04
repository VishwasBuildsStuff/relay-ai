"""
Unit tests for deterministic SandboxRunner in relay-ai/sandbox.py.
"""

import pytest
from sandbox import SandboxRunner, SandboxSecurityError


@pytest.fixture
def sample_codebase():
    return {
        "calculator.py": (
            "def divide(a: float, b: float) -> float:\n"
            "    # Bug: no zero check\n"
            "    return a / b\n"
        ),
        "test_calculator.py": (
            "import pytest\n"
            "from calculator import divide\n\n"
            "def test_divide_normal():\n"
            "    assert divide(10, 2) == 5.0\n\n"
            "def test_divide_zero():\n"
            "    assert divide(10, 0) == 0.0\n"
        ),
    }


@pytest.fixture
def runner(monkeypatch):
    # The test fixtures here are trusted and explicitly exercise the dev fallback.
    monkeypatch.setenv("RELAY_ALLOW_UNSAFE_SUBPROCESS", "true")
    instance = SandboxRunner()
    instance.docker_available = False
    instance.sandbox_image_available = False
    return instance


def test_sandbox_executes_pytest_cleanly(sample_codebase, runner):
    """Verify that unpatched codebase runs pytest and accurately reports test failure."""
    result = runner.run_tests(sample_codebase)

    assert result.exit_code != 0
    assert result.tests_passed is False
    assert "ZeroDivisionError" in result.stdout or "ZeroDivisionError" in result.stderr


def test_sandbox_validates_and_applies_unified_diff(sample_codebase, runner):
    """Verify that a valid unified diff applies cleanly and causes tests to pass."""
    valid_patch = (
        "diff --git a/calculator.py b/calculator.py\n"
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def divide(a: float, b: float) -> float:\n"
        "-    # Bug: no zero check\n"
        "-    return a / b\n"
        "+    if b == 0:\n"
        "+        return 0.0\n"
        "+    return a / b\n"
    )

    result = runner.run_tests(sample_codebase, unified_diff=valid_patch)
    assert result.applied_patch_cleanly is True
    assert result.tests_passed is True
    assert result.exit_code == 0


def test_sandbox_rejects_malformed_patch(sample_codebase, runner):
    """Verify that a malformed patch fails validation without corrupting repository."""
    malformed_patch = "This is not a git patch @@ invalid hunk"

    result = runner.run_tests(sample_codebase, unified_diff=malformed_patch)
    assert result.applied_patch_cleanly is False
    assert result.tests_passed is False
    assert result.patch_error is not None


def test_sandbox_security_traversal_prevention(runner):
    """Verify that path traversal attempts (zip-slip or relative escapes) raise SandboxSecurityError."""
    malicious_codebase = {
        "../../etc/passwd": "root:x:0:0:root:/root:/bin/bash"
    }

    with pytest.raises(SandboxSecurityError):
        runner.run_tests(malicious_codebase)
