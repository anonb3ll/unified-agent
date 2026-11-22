"""CI regression and contract test for scripts/mcp2_smoke.py (UA #867).

Ensures CI automatically catches any protocol drift, annotation discrepancies,
progress failure, subscription disruption, or cancellation/resume regression.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_mcp2_smoke_script_runs_clean():
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "mcp2_smoke.py")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"MCP 2.x smoke suite failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    assert "=== MCP 2.x Smoke Verification Suite [PASS] ===" in result.stdout
    assert "0 failed" in result.stdout


def test_mcp2_smoke_json_output():
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "mcp2_smoke.py"), "--json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"MCP 2.x smoke suite JSON failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["checks_failed"] == 0
    assert payload["checks_passed"] >= 5
    assert len(payload["checks"]) >= 5
    checks_map = {c["check"]: c for c in payload["checks"]}
    assert checks_map["tool_annotations_and_schemas"]["status"] == "PASS"
    assert checks_map["progress_emission"]["status"] == "PASS"
    assert checks_map["live_resource_subscriptions"]["status"] == "PASS"
    assert checks_map["governed_cancellation_and_resumption"]["status"] == "PASS"
    assert checks_map["health_and_doctor_telemetry"]["status"] == "PASS"


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
        [sys.executable, str(_REPO_ROOT / "scripts" / "mcp2_smoke.py")],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout
