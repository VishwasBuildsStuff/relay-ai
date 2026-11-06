"""Opt-in integration drill against the pinned Docker sandbox image."""

import os

import pytest

from sandbox import SandboxRunner


pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_RUN_ISOLATION_DRILL") != "true",
    reason="Set RELAY_RUN_ISOLATION_DRILL=true to exercise the real Docker sandbox.",
)


def test_docker_runtime_enforces_sandbox_policy():
    probe = '''import os, pathlib, socket

def test_runtime_policy():
    assert os.geteuid() == 65534
    status = pathlib.Path("/proc/self/status").read_text()
    cap_eff = next(line.split()[1] for line in status.splitlines() if line.startswith("CapEff:"))
    assert int(cap_eff, 16) == 0
    assert set(os.listdir("/sys/class/net")) == {"lo"}
    mounts = pathlib.Path("/proc/mounts").read_text().splitlines()
    tmp_mount = next(line for line in mounts if " /tmp " in line)
    options = set(tmp_mount.split(" ")[3].split(","))
    assert {"noexec", "nosuid"}.issubset(options)
    assert "size=65536k" in options or "size=64m" in options
    assert pathlib.Path("/sys/fs/cgroup/pids.max").read_text().strip() == "128"
    assert int(pathlib.Path("/sys/fs/cgroup/memory.max").read_text().strip()) <= 512 * 1024 * 1024
    for target in ("/root/relay-write-check", "/workspace/relay-write-check"):
        try:
            pathlib.Path(target).write_text("blocked")
        except OSError:
            pass
        else:
            raise AssertionError(f"unexpected writable path: {target}")
    probe = socket.socket()
    probe.settimeout(1)
    assert probe.connect_ex(("1.1.1.1", 53)) != 0
    probe.close()
'''
    result = SandboxRunner(timeout_seconds=20).run_tests({"test_sandbox_policy.py": probe})
    assert result.isolation_mode == "docker"
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.tests_passed


def test_docker_runner_caps_captured_output():
    noisy_test = '''def test_noisy_failure():
    print("x" * 1_500_000)
    assert False
'''
    result = SandboxRunner(timeout_seconds=20).run_tests({"test_noisy_output.py": noisy_test})
    assert len(result.stdout.encode("utf-8")) <= 1_048_700
    assert "output truncated at 1 MiB" in result.stdout
