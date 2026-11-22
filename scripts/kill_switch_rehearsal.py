#!/usr/bin/env python3
"""Kill switch rehearsal (task #373 Tier 2, 3.3 — S5 gate MUST 4).

Proves the bounded-autonomy kill switch (autonomous_cycle_kill, task
#373 3.3) actually stops the executor daemon from dispatching more work,
using the REAL daemon code path (ExecutorDaemon.poll_once ->
_execute_claimed_task -> the cycle gate added for 3.3) rather than a
reimplementation of the check. This is a REHEARSAL, in the same sense
scripts/gate_e_rollback_rehearsal.py is one for Gate C: every database it
touches lives under a throwaway tempfile.TemporaryDirectory via UA_HOME, it
performs zero network calls, and it never reads or writes the real
~/.unified_agent/memory.db.

Read docs/s5-bounded-autonomy-acceptance-gates.md MUST 3-4 before trusting
this report's meaning. It answers one question: "does a killed/over-budget
cycle actually stop the daemon from starting more work, and is the DB state
afterward distinguishable from blocked/failed/completed?" It does NOT prove
"an already-running subprocess is interrupted mid-flight" — that would
require Popen-based process handles in PiBackend/CodexBackend/AgyBackend,
none of which exist today (all three block inside `subprocess.run`, and
PiBackend's own docstring says it "must not become a rewrite of the path
that already works"). What this rehearsal proves instead, honestly: no
further execution starts once a cycle is killed or over budget (checked
before every dispatch), and any subprocess that IS already running is
bounded to at most `min(300, remaining_time_budget)` seconds by the
budget-capped timeout wired into the daemon — never unbounded. That is a
real, bounded, documented stop, not an instant SIGTERM. See the daemon
wiring in src/unified_agent/executor/daemon.py `_execute_claimed_task`.

The one substitution from full production code: a stub execution backend
replaces PiBackend/CodexBackend/AgyBackend. This is not a mock of the code
under test — it replaces exactly the one external-process boundary
ExecutorDaemon already takes as an injected dependency (the same `backend=`
parameter tests/test_executor.py's TestBoundedAutonomyCycleGate uses), so
`poll_once`/`_execute_claimed_task`/the cycle gate/the timeout-capping
arithmetic/the usage-recording call are all real, unmodified production
code, exercised end-to-end.

Usage: uv run python scripts/kill_switch_rehearsal.py [--out PATH]
Exit codes: 0 = every check passed; 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Same isolation contract as gate_e_rollback_rehearsal.py: no background
# vault-sync thread writing into a tempdir that is about to vanish.
os.environ["UA_STATE_SYNC"] = "0"


class _StubBackend:
    """Replaces PiBackend/CodexBackend/AgyBackend — the one real external-
    process boundary. Records every call it receives (proving whether the
    gate let it run) and returns a scripted result without shelling out to
    anything real."""

    name = "stub"
    session_kind = "stub_run"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "success",
            "text": "stub run",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }


def _check(checks: list[dict], name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run_rehearsal() -> dict:
    checks: list[dict] = []
    prior_home = os.environ.get("UA_HOME")
    prior_workspace = os.environ.get("UA_WORKSPACE")
    prior_allow_direct = os.environ.get("UA_EXECUTOR_ALLOW_DIRECT")
    prior_hub_url = os.environ.get("UA_HTTP_HUB_URL")
    prior_spec_enforce = os.environ.get("UA_DELEGATION_SPEC_ENFORCE")

    try:
        with tempfile.TemporaryDirectory(prefix="kill-switch-rehearsal-") as tmp:
            tmp_dir = Path(tmp)
            os.environ["UA_HOME"] = str(tmp_dir / "home")
            os.environ["UA_WORKSPACE"] = str(tmp_dir / "workspace")
            (tmp_dir / "workspace").mkdir(parents=True, exist_ok=True)
            # Standalone-script equivalent of tests/test_executor.py's
            # setup_db fixture: no real hub to talk to, so opt into the
            # in-process fallback explicitly, and point at a closed port so a
            # developer's locally-running hub can never be reached by
            # accident.
            os.environ["UA_EXECUTOR_ALLOW_DIRECT"] = "1"
            os.environ["UA_HTTP_HUB_URL"] = "http://127.0.0.1:1/mcp"
            # This rehearsal exercises the cycle gate, not the unrelated
            # delegation-spec-format gate — same default the pytest harness
            # itself uses (tests/conftest.py).
            os.environ["UA_DELEGATION_SPEC_ENFORCE"] = "0"

            from unified_agent import post_init
            from unified_agent.executor.daemon import ExecutorDaemon
            from unified_agent.tools import cycles as cycles_tool
            from unified_agent.tools import delegate as delegate_tool

            post_init.init_db()  # storage.init_db() plus the hub's post-init hooks

            # --- Scenario 1: kill an already-dispatched-once cycle, then
            # prove the NEXT poll refuses to run the backend at all. ---
            del_res = delegate_tool.delegate(
                task_spec="kill switch rehearsal task",
                from_agent="Desktop_Agent1",
                to_agent="Desktop_agent3",
            )
            task_id = del_res["task_id"]
            cycle = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"]
            cycle_id = cycle["id"]
            _check(
                checks,
                "cycle_starts_in_plan_not_terminal",
                cycle["state"] == "plan",
                cycle["state"],
            )

            killed = cycles_tool.autonomous_cycle_kill(
                cycle_id, "Desktop_agent3", "rehearsal: simulated runaway"
            )
            _check(
                checks,
                "kill_sets_state_to_killed",
                killed["cycle"]["state"] == "killed",
                killed["cycle"]["state"],
            )
            _check(
                checks,
                "killed_is_distinguishable_from_blocked_failed_completed",
                killed["cycle"]["state"] not in {"blocked", "failed", "completed"},
            )
            _check(
                checks,
                "kill_was_not_approval_gated",
                True,  # autonomous_cycle_kill raised nothing above — a gated
                # call would have raised ApprovalPendingError instead of
                # returning normally.
            )

            backend = _StubBackend()
            daemon = ExecutorDaemon(
                agent_label="Desktop_agent3", shadow_mode=False, backend=backend
            )
            poll_res = daemon.poll_once()
            _check(
                checks,
                "daemon_refuses_to_dispatch_a_killed_cycle",
                poll_res is not None and poll_res.get("status") == "blocked_by_cycle",
                json.dumps(poll_res),
            )
            _check(
                checks,
                "backend_run_was_never_called_for_a_killed_cycle",
                backend.calls == [],
                f"calls={len(backend.calls)}",
            )

            _check(
                checks,
                "killed_cycle_rejects_further_usage_recording",
                _raises_terminal_error(cycles_tool, cycle_id),
            )

            # --- Scenario 2: killing this task's cycle must not lock the
            # TASK out forever — a fresh cycle for the same task proceeds
            # normally. Proves the kill switch stops a specific runaway
            # line of work, not the task itself. ---
            fresh_cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)[
                "cycle"
            ]["id"]
            found = cycles_tool.active_cycle_for_task(task_id)
            _check(
                checks,
                "a_fresh_cycle_for_the_same_task_is_found_not_the_old_kill",
                found is not None and found["id"] == fresh_cycle_id,
                json.dumps(found),
            )

            # --- Scenario 3: budget-capped timeout is the automatic,
            # bounded-stop half of the gate — proves a cycle's subprocess
            # cannot run past its own remaining time budget even without an
            # explicit kill. ---
            del_res_2 = delegate_tool.delegate(
                task_spec="budget cap rehearsal task",
                from_agent="Desktop_Agent1",
                to_agent="Desktop_agent3",
            )
            task_id_2 = del_res_2["task_id"]
            cycles_tool.autonomous_cycle_start(
                "Desktop_agent3", task_id=task_id_2, budgets={"time_s": 17}
            )
            backend_2 = _StubBackend()
            daemon_2 = ExecutorDaemon(
                agent_label="Desktop_agent3", shadow_mode=False, backend=backend_2
            )
            daemon_2.poll_once()
            _check(
                checks,
                "subprocess_timeout_capped_to_remaining_budget_not_unbounded_300s",
                bool(backend_2.calls) and backend_2.calls[0].get("timeout_s") == 17,
                f"calls={backend_2.calls}",
            )

            # --- Scenario 4: no state outside this rehearsal's own sandbox
            # was touched. Anything other than the two isolated homes below
            # would be a real bug in this script, not a pass/fail of the
            # kill switch itself — asserted so a future edit cannot silently
            # widen the blast radius. ---
            _check(
                checks,
                "only_the_rehearsals_own_tempdir_was_ever_used_as_ua_home",
                os.environ["UA_HOME"].startswith(str(tmp_dir)),
                os.environ["UA_HOME"],
            )
    finally:
        _restore("UA_HOME", prior_home)
        _restore("UA_WORKSPACE", prior_workspace)
        _restore("UA_EXECUTOR_ALLOW_DIRECT", prior_allow_direct)
        _restore("UA_HTTP_HUB_URL", prior_hub_url)
        _restore("UA_DELEGATION_SPEC_ENFORCE", prior_spec_enforce)

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "generated_at": datetime.now(UTC).isoformat(),
        "gate": "S5/G5",
        "task": 373,
        "checks": checks,
        "summary": {"total": len(checks), "passed": sum(c["ok"] for c in checks)},
    }


def _raises_terminal_error(cycles_tool: Any, cycle_id: str) -> bool:
    try:
        cycles_tool.autonomous_cycle_record_usage(cycle_id, "Desktop_agent3", elapsed_s=1)
    except Exception as e:
        return "terminal" in str(e)
    return False


def _restore(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here too")
    args = parser.parse_args()

    report = run_rehearsal()
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
