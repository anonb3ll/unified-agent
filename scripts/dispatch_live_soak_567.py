#!/usr/bin/env python3
"""Bounded Mac-hub soak for UA #567. Stub launch only. No secrets printed.

Two-phase operator flow (human approval is never written by this script):

1. ``setup`` — create labeled soak children under #567 (approved child stays
   unapproved; denied child gets a ``[plan: denied @ …]`` marker).
2. Human steward appends ``[plan: approved @ <iso-ts>]`` to the approved child
   **or** resolves the exact ``dispatch.claim`` approval row.
3. ``tick`` — task-pinned ``claim_and_dispatch`` on the approved child only;
   verifies denied sibling never launches and a second claim is a no-op.

Abort before any DB mutation when ``UA_DISPATCH_SCHEDULER_ENABLED=0`` unless
``--force`` is passed explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

os.environ.setdefault("UA_HOME", os.path.expanduser("~/.unified_agent"))

from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.scheduler import record_run_outcome
from unified_agent.tools import delegate, tasks

TS = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
PARENT = 567
OK_TITLE_SNIPPET = "live soak: one approved"
DENIED_TITLE_SNIPPET = "live soak: denied sibling"


def _spec(title: str, dod_lines: list[str], extra_boundary: str) -> str:
    """Build a delegate task_spec block for a labeled soak child."""
    dod = "\n".join(f"- {line}" for line in dod_lines)
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- Mac hub dispatch_runs / delegations\n"
        "- unified-agent-dispatch-scheduler tick (stub spawn)\n"
        f"### Definition of Done\n{dod}\n"
        "### TDD Constraints\n"
        "- Stub: process launch (no claude/codex/agy)\n"
        "- Must be real: task, delegation, approval marker, claim, events\n"
        "### Boundaries\n"
        "- Must not restart the hub or edit Red-tier config\n"
        f"- {extra_boundary}\n"
    )


def _scheduler_gate_ok(*, force: bool) -> tuple[bool, str | None]:
    """Fail closed when the kill switch is off unless the operator passed --force."""
    if force:
        return True, None
    if os.environ.get("UA_DISPATCH_SCHEDULER_ENABLED", "0") != "1":
        return False, "scheduler_disabled"
    if not scheduler.DispatchScheduler.enabled():
        return False, "scheduler_disabled"
    return True, None


def _find_child(*, snippet: str, parent_id: int | None = None) -> dict | None:
    """Return the first child task under ``parent_id`` whose title contains ``snippet``."""
    pid = PARENT if parent_id is None else parent_id
    for t in tasks.task_list(parent_id=pid, status="all", limit=50).get("tasks") or []:
        title = t.get("title") or ""
        if snippet in title:
            return t
    return None


def _finalize_invocation_runs(*claims: dict) -> list[dict]:
    """Record terminal failure for every run minted during this tick invocation."""
    outcomes: list[dict] = []
    seen: set[str] = set()
    for claim in claims:
        run_id = claim.get("run_id")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        outcome = record_run_outcome(
            run_id,
            "failed",
            actor="dispatch-smoke",
            failure_reason="ua-567-soak-acceptance-failed",
            evidence_ref="ua-567-mac-live-soak-abort",
            append_task_note=False,
        )
        outcomes.append({"run_id": run_id, **outcome})
    return outcomes


def _cancel_soak_children(
    *,
    ok: dict,
    denied: dict,
    reason: str,
    created_fresh: dict[str, bool] | None = None,
) -> None:
    """Cancel soak-labeled children when a failed tick must not leave ready work behind.

    When ``created_fresh`` is set, only children created in the current invocation are
    cancelled so retry paths can reuse pre-existing soak tasks from a prior ``setup``.
    """
    note = f"[soak-abort: {reason} @ {TS}]"
    if created_fresh is None or created_fresh.get("ok"):
        tasks.task_update(ok["task_id"], status="cancelled", append_note=note)
    if created_fresh is None or created_fresh.get("denied"):
        tasks.task_update(denied["task_id"], status="cancelled", append_note=note)


def _finished_run_for_task(task_id: int) -> dict | None:
    """Return the newest finished dispatch run for ``task_id``, if any."""
    for run in coordinator.list_runs(task_id=task_id, limit=20):
        if run.get("state") == "finished":
            return run
    return None


def _delegation_id_for_task(task_id: int) -> int | None:
    """Look up the newest delegation row for ``task_id``, if any."""
    from unified_agent import storage

    conn = storage.connect()
    try:
        drow = conn.execute(
            "SELECT id FROM delegations WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(drow["id"]) if drow is not None else None


def _ensure_soak_children() -> tuple[dict, dict, dict[str, bool]]:
    """Create or reuse the approved/denied soak pair; report which rows were minted now."""
    created_fresh = {"ok": False, "denied": False}
    ok_existing = _find_child(snippet=OK_TITLE_SNIPPET)
    if ok_existing is not None:
        delegation_id = _delegation_id_for_task(ok_existing["id"])
        if delegation_id is None:
            raise RuntimeError("existing_soak_missing_delegation")
        ok = {"task_id": ok_existing["id"], "delegation_id": delegation_id}
    else:
        created_fresh["ok"] = True
        ok = delegate.delegate(
            task_spec=_spec(
                "UA #567 live soak: one approved stub dispatch",
                [
                    "Scheduler tick produces exactly one observable dispatch run_id",
                    "The run returns to finished after the soak records the outcome",
                    "A second tick never mints a second run for the same child",
                ],
                "Must not launch a real developer CLI",
            ),
            from_agent="Desktop_Agent4",
            to_agent="Desktop_Agent1",
            callback="none",
            priority="low",
            parent_task_id=PARENT,
            project="unified-agent",
        )

    denied_existing = _find_child(snippet=DENIED_TITLE_SNIPPET)
    if denied_existing is not None:
        delegation_id = _delegation_id_for_task(denied_existing["id"])
        if delegation_id is None:
            raise RuntimeError("existing_denied_soak_missing_delegation")
        denied = {"task_id": denied_existing["id"], "delegation_id": delegation_id}
    else:
        created_fresh["denied"] = True
        denied = delegate.delegate(
            task_spec=_spec(
                "UA #567 live soak: denied sibling never launches",
                [
                    "Scheduler tick refuses this denied child and never creates a dispatch run",
                    "No worker process is spawned for the denied sibling",
                ],
                "Must not be claimed or launched",
            ),
            from_agent="Desktop_Agent4",
            to_agent="Desktop_Agent1",
            callback="none",
            priority="low",
            parent_task_id=PARENT,
            project="unified-agent",
        )
        tasks.task_update(denied["task_id"], append_note=f"[plan: denied @ {TS}]")

    return ok, denied, created_fresh


def cmd_setup() -> int:
    """Phase 1: mint soak children; approved child stays unapproved until a human steward acts."""
    gate_ok, gate_reason = _scheduler_gate_ok(force=False)
    if not gate_ok:
        print(json.dumps({"ok": False, "reason": gate_reason}))
        return 1

    ready_before = scheduler.list_ready_candidates(limit=50)
    if ready_before:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "ready_set_not_empty",
                    "ready_task_ids": [r["task_id"] for r in ready_before],
                }
            )
        )
        return 2

    try:
        ok, denied, _created_fresh = _ensure_soak_children()
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 6

    verdict = coordinator.evaluate_ready(task_id=ok["task_id"])
    print(
        json.dumps(
            {
                "ok": True,
                "phase": "setup",
                "ok_task_id": ok["task_id"],
                "ok_delegation_id": ok["delegation_id"],
                "denied_task_id": denied["task_id"],
                "denied_delegation_id": denied["delegation_id"],
                "approved_ready": bool(verdict.get("ready")),
                "approval_reason": verdict.get("reason"),
                "next_step": (
                    "Human steward must append [plan: approved @ <iso-ts>] or "
                    "approval_resolve the dispatch.claim row, then run: tick"
                ),
            }
        )
    )
    return 0 if verdict.get("ready") else 7


def cmd_tick(*, force: bool) -> int:
    """Phase 2: task-pinned claim/dispatch after human approval; finalize runs on any failure."""
    gate_ok, gate_reason = _scheduler_gate_ok(force=force)
    if not gate_ok:
        print(json.dumps({"ok": False, "reason": gate_reason}))
        return 1

    ok_existing = _find_child(snippet=OK_TITLE_SNIPPET)
    if ok_existing is None:
        print(
            json.dumps({"ok": False, "reason": "missing_soak_children", "hint": "run setup first"})
        )
        return 8

    delegation_id = _delegation_id_for_task(ok_existing["id"])
    if delegation_id is None:
        print(json.dumps({"ok": False, "reason": "existing_soak_missing_delegation"}))
        return 6

    ok = {"task_id": ok_existing["id"], "delegation_id": delegation_id}
    denied_existing = _find_child(snippet=DENIED_TITLE_SNIPPET)
    if denied_existing is None:
        print(
            json.dumps(
                {"ok": False, "reason": "missing_denied_soak_child", "hint": "run setup first"}
            )
        )
        return 8
    denied_delegation_id = _delegation_id_for_task(denied_existing["id"])
    if denied_delegation_id is None:
        print(json.dumps({"ok": False, "reason": "existing_denied_soak_missing_delegation"}))
        return 6
    denied = {"task_id": denied_existing["id"], "delegation_id": denied_delegation_id}

    finished = _finished_run_for_task(ok["task_id"])
    if finished is not None:
        obs = coordinator.get_run_observability(finished["run_id"])
        print(
            json.dumps(
                {
                    "ok": True,
                    "reason": "soak_already_complete",
                    "ok_task_id": ok["task_id"],
                    "run_id": finished["run_id"],
                    "obs_state": (obs or {}).get("state"),
                    "hint": "Bounded soak already finished; no second launch attempted",
                },
                default=str,
            )
        )
        return 0

    verdict = coordinator.evaluate_ready(task_id=ok["task_id"])
    if not verdict.get("ready"):
        approval_reason = verdict.get("reason")
        top_reason = (
            "awaiting_human_approval"
            if approval_reason == "missing_approval_marker"
            else (approval_reason or "not_ready")
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": top_reason,
                    "approval_reason": approval_reason,
                    "ok_task_id": ok["task_id"],
                    "hint": (
                        "Append [plan: approved @ <iso-ts>] via a human steward or "
                        "approval_resolve before re-running tick"
                    ),
                }
            )
        )
        return 7

    ready = scheduler.list_ready_candidates(limit=50)
    ready_ids = [r["task_id"] for r in ready]
    if ready_ids != [ok["task_id"]]:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "ready_mismatch",
                    "expected": [ok["task_id"]],
                    "ready_task_ids": ready_ids,
                    "ok_task_id": ok["task_id"],
                    "denied_task_id": denied["task_id"],
                    "hint": (
                        "Clear unrelated ready delegations and re-run tick; "
                        "existing soak children are preserved for retry"
                    ),
                }
            )
        )
        return 3

    first = coordinator.claim_and_dispatch(
        task_id=ok["task_id"],
        delegation_id=ok["delegation_id"],
    )
    second = coordinator.claim_and_dispatch(
        task_id=ok["task_id"],
        delegation_id=ok["delegation_id"],
    )
    denied_runs = coordinator.list_runs(task_id=denied["task_id"], limit=20)

    launched = bool(first.get("launched"))
    summary = {
        "ok_task_id": ok["task_id"],
        "ok_delegation_id": ok["delegation_id"],
        "denied_task_id": denied["task_id"],
        "denied_delegation_id": denied["delegation_id"],
        "first_launched": launched,
        "first_reason": first.get("reason"),
        "run_id": first.get("run_id"),
        "second_launched": bool(second.get("launched")),
        "second_reason": second.get("reason"),
        "denied_run_count": len(denied_runs),
        "forced": force,
        "scheduler_enabled": scheduler.DispatchScheduler.enabled(),
    }

    if (
        not launched
        or first.get("reason") not in {None, "ok"}
        or second.get("launched")
        or denied_runs
    ):
        summary["finalized_runs"] = _finalize_invocation_runs(first, second)
        _cancel_soak_children(ok=ok, denied=denied, reason="acceptance_failed")
        summary["ok"] = False
        summary["reason"] = "acceptance_failed"
        summary["cleanup"] = "soak_children_cancelled"
        print(json.dumps(summary, default=str))
        return 4

    run_id = first["run_id"]
    finish = record_run_outcome(
        run_id,
        "finished",
        actor="dispatch-smoke",
        evidence_ref="ua-567-mac-live-soak-stub",
    )
    obs = coordinator.get_run_observability(run_id)
    summary["ok"] = bool(finish.get("ok"))
    summary["finish_state"] = finish.get("state")
    summary["obs_state"] = (obs or {}).get("state")
    summary["obs_agent"] = (obs or {}).get("agent_label")
    summary["obs_retry_count"] = (obs or {}).get("retry_count")
    print(json.dumps(summary, default=str))
    return 0 if summary["ok"] and summary.get("obs_state") == "finished" else 5


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``setup`` prepares children; ``tick`` runs the bounded dispatch proof."""
    parser = argparse.ArgumentParser(description="UA #567 Mac hub dispatch live soak")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("setup", "tick"),
        default="tick",
        help="setup creates children; tick requires prior human approval (default: tick)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow tick when UA_DISPATCH_SCHEDULER_ENABLED=0 (operator one-shot only)",
    )
    args = parser.parse_args(argv)

    if args.command == "setup":
        return cmd_setup()
    return cmd_tick(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
