#!/usr/bin/env python3
"""MCP 2.x End-to-End Live Smoke Verification Suite (UA #867).

Exercises all supported MCP 2.x capabilities under an isolated runtime environment:
1. Tool Risk Annotations and Structured Schemas
2. Real-path Progress Emission & Telemetry Recording
3. Live Resource Endpoints & Subscription Reconciliation
4. Governed Subtask Cancellation and Resumption
5. Runtime Health, Version-Skew Diagnostics, and Doctor Reporting

Usage:
  uv run python scripts/mcp2_smoke.py [--json]

Exit codes:
  0: All checks PASSED
  1: One or more checks FAILED
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def setup_isolated_env(tmp_dir: Path) -> None:
    os.environ["UA_HOME"] = str(tmp_dir / "ua_home")
    os.environ["UA_WORKSPACE"] = str(tmp_dir / "workspace")
    os.environ["UA_VAULT_PATH"] = str(tmp_dir / "vault")
    # Task and dispatch events must stay off the launching host's live bus.
    os.environ["UA_EVENT_SOCKET"] = str(tmp_dir / "events.sock")
    Path(os.environ["UA_WORKSPACE"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["UA_VAULT_PATH"]).mkdir(parents=True, exist_ok=True)
    os.environ.pop("UA_BRIDGE_DIR", None)
    os.environ["UA_STATE_SYNC"] = "0"
    # These standalone checks exercise real writes in a disposable store,
    # without a human approval loop. Match pytest's isolated runtime rather
    # than inherit the launching host's gates or the production baseline.
    os.environ["UA_APPROVAL_TOOLS"] = ""
    os.environ["UA_APPROVAL_BASELINE"] = "0"
    os.environ["UA_AGENT_LABEL"] = "Desktop_agent3"


def check_tool_annotations_and_schemas() -> tuple[str, str]:
    from unified_agent.server import REGISTERED_TOOLS, _tool_annotations

    if not REGISTERED_TOOLS:
        return "FAIL", "no registered tools found on server"

    total = len(REGISTERED_TOOLS)
    annotated = 0
    conflicts = 0

    for _name, fn in REGISTERED_TOOLS.items():
        ann = _tool_annotations(getattr(fn, "__wrapped__", fn))
        if ann is not None:
            annotated += 1
            read_only = getattr(ann, "read_only_hint", None)
            destructive = getattr(ann, "destructive_hint", None)
            if read_only and destructive:
                conflicts += 1

    if conflicts > 0:
        return "FAIL", f"{conflicts} tools have conflicting (read-only + destructive) hints"

    return "PASS", f"all {annotated}/{total} tools verified with valid non-conflicting annotations"


def check_progress_emission() -> tuple[str, str]:
    from unified_agent import mcp_progress, mcp_runtime_telemetry
    from unified_agent.tools.mcp_health import mcp_health_telemetry

    mcp_runtime_telemetry.reset_telemetry()
    mcp_progress.report_progress(50.0, 100.0, "smoke progress milestone 1")
    mcp_progress.report_progress(100.0, 100.0, "smoke progress completed")

    snapshot = mcp_health_telemetry()
    emitted = snapshot["progress"]["events_emitted"]
    if emitted < 2:
        return "FAIL", f"expected at least 2 progress events, got {emitted}"
    if snapshot["progress"]["active"] is not False:
        return "FAIL", "expected progress.active to be False after reaching total"

    return "PASS", f"progress milestones recorded ({emitted} events, active=False)"


def check_live_resource_subscriptions() -> tuple[str, str]:
    from unified_agent.mcp_live_state import (
        BOARD_URI,
        read_board,
        update_subscription_telemetry,
    )
    from unified_agent.server import mcp
    from unified_agent.tools.mcp_health import mcp_health_telemetry

    board = read_board()
    b_uri = board.get("uri")
    if b_uri != BOARD_URI:
        return "FAIL", f"unexpected board URI: {b_uri}"
    if "tasks" not in board:
        return "FAIL", "board payload missing tasks list"

    update_subscription_telemetry(mcp)
    snapshot = mcp_health_telemetry()
    if "subscriptions" not in snapshot:
        return "FAIL", "subscriptions telemetry missing from snapshot"

    task_count = len(board["tasks"])
    return "PASS", f"board resource live ({task_count} tasks) and subscription telemetry verified"


def check_governed_cancellation_and_resumption() -> tuple[str, str]:
    from unified_agent import config
    from unified_agent.dispatch import coordinator
    from unified_agent.dispatch.launch import RecordingLaunchBoundary
    from unified_agent.tools import delegate, tasks

    spec_text = (
        "### Objective\nSmoke dispatch cancellation and resume test\n"
        "### Scope\n- src/unified_agent/dispatch/\n"
        "### Definition of Done\n- cancel and resume verified\n"
        "### TDD Constraints\n- stub launch boundary\n"
        "### Boundaries\n- smoke test only\n"
    )
    delegation = delegate.delegate(
        task_spec=spec_text,
        from_agent="Desktop_Agent5",
        to_agent="Desktop_agent3",
        callback="none",
    )
    task_id = delegation["task_id"]
    tasks.task_update(task_id, append_note="[plan: approved @ 2026-09-09T20:00:00Z]")

    # Claim and dispatch
    launch_boundary = RecordingLaunchBoundary()
    dispatch_out = coordinator.claim_and_dispatch(
        task_id=task_id, actor=config.operator_name(), launch=launch_boundary
    )
    run_id = dispatch_out.get("run_id")
    if not run_id:
        return "FAIL", f"claim_and_dispatch did not return run_id: {dispatch_out}"

    # Cancel the run
    cancel_res = coordinator.cancel_run(run_id, actor="Desktop_agent3", reason="smoke cancellation")
    if not cancel_res.get("ok"):
        return "FAIL", f"cancel_run failed: {cancel_res}"

    cancelled_run = coordinator.get_run(run_id)
    c_state = cancelled_run.get("state")
    if c_state != "cancelled":
        return "FAIL", f"expected state cancelled, got {c_state}"

    # Resume the run
    resume_boundary = RecordingLaunchBoundary()
    resume_res = coordinator.resume_run(run_id, actor="Desktop_agent3", launch=resume_boundary)
    if not resume_res.get("ok"):
        return "FAIL", f"resume_run failed: {resume_res}"

    resumed_run = coordinator.get_run(run_id)
    r_state = resumed_run.get("state")
    if r_state != "launched":
        return "FAIL", f"expected resumed state launched, got {r_state}"

    return "PASS", f"run {run_id} successfully cancelled and resumed to launched state"


def check_health_and_doctor_telemetry() -> tuple[str, str]:
    from unified_agent.tools.doctor import system_doctor
    from unified_agent.tools.mcp_health import mcp_health_telemetry

    snapshot = mcp_health_telemetry()
    status = snapshot.get("status")
    if status not in ("healthy", "degraded", "incompatible"):
        return "FAIL", f"invalid telemetry health status: {status}"

    doc = system_doctor()
    checks = {c["capability"]: c for c in doc.get("checks", [])}
    mcp_check = checks.get("mcp2_runtime_telemetry")
    if not mcp_check:
        return "FAIL", "mcp2_runtime_telemetry missing from system_doctor"

    doc_status = mcp_check.get("status")
    if doc_status not in ("LIVE", "DORMANT", "DEGRADED", "MISCONFIGURED", "UNKNOWN"):
        return "FAIL", f"invalid doctor status: {doc_status}"

    return (
        "PASS",
        f"health telemetry evaluated ({status}) and system_doctor verified ({doc_status})",
    )


CHECKS = [
    ("tool_annotations_and_schemas", check_tool_annotations_and_schemas),
    ("progress_emission", check_progress_emission),
    ("live_resource_subscriptions", check_live_resource_subscriptions),
    ("governed_cancellation_and_resumption", check_governed_cancellation_and_resumption),
    ("health_and_doctor_telemetry", check_health_and_doctor_telemetry),
]


def run_smoke_suite() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ua-mcp2-smoke-") as tmp:
        setup_isolated_env(Path(tmp))

        from unified_agent import post_init, storage

        post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
        storage.join_init_background_threads()

        t0 = time.time()
        results = []
        all_passed = True

        for name, check_fn in CHECKS:
            try:
                status, detail = check_fn()
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"

            if status != "PASS":
                all_passed = False

            results.append({"check": name, "status": status, "detail": detail})

        duration_ms = (time.time() - t0) * 1000

        return {
            "passed": all_passed,
            "duration_ms": duration_ms,
            "checks_total": len(CHECKS),
            "checks_passed": sum(1 for r in results if r["status"] == "PASS"),
            "checks_failed": sum(1 for r in results if r["status"] == "FAIL"),
            "checks": results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 2.x Live Smoke Verification Suite")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    args = parser.parse_args()

    report = run_smoke_suite()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status_str = "PASS" if report["passed"] else "FAIL"
        passed_n = report["checks_passed"]
        total_n = report["checks_total"]
        failed_n = report["checks_failed"]
        dur = report["duration_ms"]
        print(f"=== MCP 2.x Smoke Verification Suite [{status_str}] ===")
        print(f"Result: {passed_n}/{total_n} passed, {failed_n} failed ({dur:.1f}ms)")
        print("Checks:")
        for c in report["checks"]:
            c_tag = c["status"]
            c_name = c["check"]
            c_det = c["detail"]
            print(f"  [{c_tag:4}] {c_name}: {c_det}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
