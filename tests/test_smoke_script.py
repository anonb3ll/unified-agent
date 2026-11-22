"""Regression coverage for scripts/smoke.py itself (System Leverage Plan
T1-6). Deliberately NOT using the isolated_env fixture / any mocking —
scripts/smoke.py manages its own env isolation (a fresh temp dir per run,
see its module docstring), and the entire point of the smoke suite is to
exercise the real installed venv. This test just proves the script itself
runs clean end-to-end and exits 0; scripts/smoke.py's own checks cover
correctness of what it's testing.
"""

import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_smoke_script_runs_clean():
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "smoke.py")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"smoke suite failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    assert "failed" in result.stdout  # sanity: the summary line printed at all
    assert "0 failed" in result.stdout


@pytest.mark.parametrize("approval_tools", [None, "delegate:task_add:task_claim:task_update"])
def test_smoke_isolates_approval_configuration(approval_tools):
    env = os.environ.copy()
    # Exercise the production default and a configured host, independently
    # of pytest's approval-baseline fixture.
    env.pop("UA_APPROVAL_BASELINE", None)
    env.pop("UA_APPROVAL_TOOLS", None)
    if approval_tools is not None:
        env["UA_APPROVAL_TOOLS"] = approval_tools
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "smoke.py")],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout


@pytest.mark.parametrize(
    ("script", "setup_name"),
    [("smoke.py", "_setup_isolated_env"), ("mcp2_smoke.py", "setup_isolated_env")],
)
def test_smoke_does_not_publish_to_inherited_event_socket(
    tmp_path, monkeypatch, script, setup_name
):
    from unified_agent import events

    inherited_socket = tmp_path / "host.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(inherited_socket))
        listener.listen()
        listener.setblocking(False)
        monkeypatch.setenv("UA_EVENT_SOCKET", str(inherited_socket))
        monkeypatch.setenv("UA_EVENT_BUS", "1")

        namespace = runpy.run_path(str(_REPO_ROOT / "scripts" / script))
        namespace[setup_name](tmp_path / "isolated")
        assert events.publish({"kind": "smoke.test", "task_id": 1}) is False
        with pytest.raises(BlockingIOError):
            listener.accept()
