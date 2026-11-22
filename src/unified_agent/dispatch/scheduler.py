"""Durable approved-dispatch scheduler / Kanban poller (task #550).

One tick: reclaim stale leases → dead-letter exhausted retries → find only
explicitly approved ready work → atomically ``claim_and_dispatch`` each.
Opt-in via ``UA_DISPATCH_SCHEDULER_ENABLED`` (default off). Never enables
Hermes cron or Kanban timers by itself; never sends external notifications.
Wall-clock and subprocess are injectable / stubbed; persistence is real.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import config, storage
from ..tools import tasks as tasks_tool
from . import coordinator
from .launch import LaunchBoundary, scheduler_launch_boundary

ACTOR_SCHEDULER = "dispatch-scheduler"
DISPATCH_NOTE_PREFIX = "[dispatch:"

OUTCOME_STATES = frozenset({"finished", "failed", "retryable", "blocked", "cancelled"})
TERMINAL_OUTCOMES = frozenset({"finished", "failed", "blocked", "cancelled"})
FOLLOW_UP_CHECKS = ("tests", "ci", "review", "live-health")


def _row(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _append_dispatch_note(task_id: int, body: str) -> None:
    """Operator-visible task note; never carries secrets or prompt bodies."""
    line = f"{DISPATCH_NOTE_PREFIX} {body}]"
    tasks_tool.task_update(task_id, append_note=line)


def _dispatch_outcome_noted(notes: str, *, state: str, run_id: str) -> bool:
    """True when one dispatch note line records both the outcome and run id."""
    state_marker = f"{DISPATCH_NOTE_PREFIX} {state}"
    run_marker = f"run={run_id}"
    for line in notes.splitlines():
        if state_marker in line and run_marker in line:
            return True
    return False


def _task_needs_finished_bookkeeping(task_id: int, run_id: str) -> bool:
    """Return whether a finished run still needs to close its task row.

    Duplicate terminal reports for a historical run must not re-close a task
    that was reopened for follow-up work. Only replay when this run's outcome
    note is still missing and no newer live dispatch run owns the task.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, completed_at, notes FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        notes = row["notes"] or ""
        if _dispatch_outcome_noted(notes, state="finished", run_id=run_id):
            return False
        if row["status"] == "done" and row["completed_at"] is not None:
            return False
        live = conn.execute(
            "SELECT run_id FROM dispatch_runs WHERE task_id=? AND "
            f"({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) LIMIT 1",
            (task_id,),
        ).fetchone()
        if live is not None and live["run_id"] != run_id:
            return False
        return row["status"] != "done" or row["completed_at"] is None
    finally:
        conn.close()


def _record_terminal_bookkeeping(
    run: dict[str, Any],
    state: str,
    *,
    actor: str,
    failure_reason: str | None,
    evidence_ref: str | None,
    append_task_note: bool,
    expected_task_updated_at: str | None = None,
) -> None:
    """Replay post-transaction bookkeeping after a retry sees a terminal run.

    The run/delegation transaction commits before task/worktree, transition,
    and note writes. A transient SQLite failure in that second phase must not
    turn the next retry's ``already_terminal`` response into a false success.
    """
    task_id = run.get("task_id")
    if state == "finished" and task_id is not None:
        conn = storage.connect()
        try:
            task_row = conn.execute(
                "SELECT status, completed_at, notes, updated_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        if (
            expected_task_updated_at is None
            and task_row is not None
            and task_row["status"] != "done"
        ):
            touched, finished = _ts(task_row["updated_at"]), _ts(run.get("updated_at"))
            if touched is None or finished is None or touched > finished:
                return  # a duplicate historical report cannot close replanned work
        if task_row is not None and not _dispatch_outcome_noted(
            task_row["notes"] or "", state=state, run_id=str(run["run_id"])
        ):
            # Notes and close share one guarded task transaction; a note failure
            # cannot leave an un-retryable task already marked done.
            lines = []
            if append_task_note:
                bits = [f"finished @ {storage.now()}", f"run={run['run_id']}"]
                attempts = coordinator._delegation_attempts(run.get("delegation_id"))
                if attempts is not None:
                    bits.append(f"attempts={attempts}")
                if config.current_source_host():
                    bits.append(f"host={config.current_source_host()}")
                if evidence_ref:
                    bits.append(f"evidence={evidence_ref[:200]}")
                lines.append(f"{DISPATCH_NOTE_PREFIX} {' '.join(bits)}]")
            lines.append(
                f"[follow-up: pending run={run['run_id']} checks={','.join(FOLLOW_UP_CHECKS)}]"
            )
            with tasks_tool.dispatch_completion_guard(str(run["run_id"])):
                tasks_tool.task_update(
                    int(task_id),
                    status="done",
                    evidence_ref=evidence_ref,
                    actor=actor or run.get("actor") or run.get("agent_label"),
                    append_note="\n".join(lines),
                    expected_updated_at=expected_task_updated_at or task_row["updated_at"],
                )

    coordinator._record_transition(
        actor=actor,
        task_id=task_id,
        delegation_id=run.get("delegation_id"),
        agent_label=run.get("agent_label"),
        run_id=run.get("run_id"),
        state=state,
        failure_reason=failure_reason,
        evidence_ref=evidence_ref,
        dedupe_key=f"dispatch-outcome:{run.get('run_id')}:{state}",
    )

    if append_task_note and task_id is not None:
        conn = storage.connect()
        try:
            row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
            notes = (row["notes"] if row else None) or ""
        finally:
            conn.close()
        if not _dispatch_outcome_noted(notes, state=state, run_id=str(run.get("run_id") or "")):
            ts = storage.now()
            bits = [f"{state} @ {ts}", f"run={run.get('run_id')}"]
            attempts = coordinator._delegation_attempts(run.get("delegation_id"))
            if attempts is not None:
                bits.append(f"attempts={attempts}")
            if failure_reason:
                bits.append(f"reason={failure_reason[:120]}")
            if evidence_ref:
                bits.append(f"evidence={evidence_ref[:200]}")
            host = config.current_source_host()
            if host:
                bits.append(f"host={host}")
            _append_dispatch_note(int(task_id), " ".join(bits))

    if state == "finished" and task_id is not None:
        _record_follow_up(int(task_id), str(run.get("run_id") or ""), evidence_ref)


def _record_follow_up(task_id: int, run_id: str, evidence_ref: str | None) -> None:
    """Record the bounded post-dispatch checklist exactly once per run."""
    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        notes = (row["notes"] if row else None) or ""
    finally:
        conn.close()
    marker = f"[follow-up: pending run={run_id}"
    if marker in notes:
        return
    bits = [
        "pending",
        f"run={run_id}",
        f"checks={','.join(FOLLOW_UP_CHECKS)}",
    ]
    if evidence_ref:
        bits.append(f"evidence={evidence_ref[:200]}")
    host = config.current_source_host()
    if host:
        bits.append(f"host={host}")
    tasks_tool.task_update(task_id, append_note="[follow-up: " + " ".join(bits) + "]")


def _note_quota_skip(task_id: int, result: dict[str, Any]) -> None:
    """One operator-visible note per exhaustion observation, not per tick.

    A lane can stay exhausted across many ticks; without dedupe each poll
    would append another identical note. The dedupe key includes the
    observation timestamp so a *new* exhaustion event (after recovery) still
    posts a fresh note.
    """
    lane = result.get("quota_lane") or "unknown"
    observed = result.get("quota_observed_at") or "unknown-observation"
    marker = f"quota_exhausted lane={lane} observed={observed}"
    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        notes = (row["notes"] if row else None) or ""
    finally:
        conn.close()
    if marker in notes:
        return
    evidence = result.get("quota_evidence")
    bit = f" evidence={str(evidence)[:120]}" if evidence else ""
    _append_dispatch_note(task_id, f"skipped @ {storage.now()} {marker}{bit}")


def list_pending_delegation_candidates(*, limit: int = 50) -> list[dict[str, Any]]:
    """Pending or reclaimable-looking delegations (pre-ready filter)."""
    lim = max(1, min(limit, 200))
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM delegations WHERE status='pending' ORDER BY id ASC LIMIT ?",
            (lim,),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def list_ready_candidates(*, limit: int | None = None) -> list[dict[str, Any]]:
    """Delegations that pass the authoritative ``evaluate_ready`` predicate."""
    lim = limit if limit is not None else config.dispatch_scheduler_batch_limit()
    lim = max(1, min(lim, 50))
    ready: list[dict[str, Any]] = []
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    offset = 0
    page_size = max(lim * 4, 25)
    # Look beyond the requested result count so office-ranked candidates later
    # in the pending queue can outrank bench candidates at the front.
    # Accepted bound (Devin F4 / PR #698): each tick scans at most 2000 pending
    # rows from the front of the queue (ORDER BY id ASC) and keeps ≤500 ranked
    # ready candidates. Rows past that window are invisible this tick — there
    # is no FIFO fallback for them. Intentional for scheduler latency; widen
    # only with a measured backlog need.
    ranked_limit = min(500, max(lim * 10, 100))
    scan_limit = 2000
    scanned = 0
    from .. import org_registry

    offices = org_registry.load_registry()
    bench = org_registry.find_office("bench", offices)
    bench_members = set(bench["members"]) if bench else set()
    conn = storage.connect()
    try:
        parent_by_id = {
            row["id"]: row["parent_id"]
            for row in conn.execute("SELECT id, parent_id FROM tasks").fetchall()
        }
        while len(ranked) < ranked_limit and scanned < scan_limit:
            query_limit = min(page_size, scan_limit - scanned)
            rows = conn.execute(
                "SELECT * FROM delegations WHERE status='pending' ORDER BY id ASC LIMIT ? OFFSET ?",
                (query_limit, offset),
            ).fetchall()
            if not rows:
                break
            scanned += len(rows)
            for drow in rows:
                verdict = coordinator.evaluate_ready(
                    delegation_id=drow["id"], expected_lane=drow["to_agent"]
                )
                if not verdict.get("ready"):
                    continue
                candidate = {
                    "task_id": verdict["task"]["id"],
                    "delegation_id": verdict["delegation"]["id"],
                    "agent_label": verdict["agent_label"],
                    "reason": verdict["reason"],
                }
                ancestor = verdict["task"].get("parent_id")
                seen_ancestors: set[int] = set()
                office_id = None
                while ancestor is not None and ancestor not in seen_ancestors:
                    seen_ancestors.add(ancestor)
                    office_id = org_registry.office_for_epic(ancestor, offices)
                    if office_id is not None:
                        break
                    ancestor = parent_by_id.get(ancestor)
                if office_id is None:
                    rank = 0
                else:
                    office = org_registry.find_office(office_id, offices)
                    if verdict["agent_label"] in office["members"]:
                        rank = 0
                    elif verdict["agent_label"] in bench_members:
                        rank = 1
                    else:
                        rank = 2
                ranked.append((rank, len(ranked), candidate))
                if len(ranked) >= ranked_limit:
                    break
            if len(rows) < query_limit:
                break
            offset += len(rows)
    finally:
        conn.close()
    ready.extend(candidate for _, _, candidate in sorted(ranked, key=lambda item: item[:2])[:lim])
    return ready


def record_run_outcome(
    run_id: str,
    state: str,
    *,
    actor: str = ACTOR_SCHEDULER,
    failure_reason: str | None = None,
    evidence_ref: str | None = None,
    append_task_note: bool = True,
    guard_sql: str | None = None,
    guard_params: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """Record finished / failed / retryable / blocked on a dispatch run.

    Terminal outcomes clear the live slot (delegation → completed/failed) so a
    later tick cannot double-launch. ``retryable`` releases the delegation back
    to ``pending`` for reclaim. Unknown states refuse without mutation.

    ``guard_sql``/``guard_params`` (task #1950) are ANDed into the run's
    compare-and-swap inside this function's write transaction, so a caller's
    binding check and the transition are one atomic step. A refused guard
    returns ``guard_refused`` and writes nothing.
    """
    if state not in OUTCOME_STATES:
        return {
            "ok": False,
            "reason": "invalid_outcome_state",
            "run_id": run_id,
            "state": state,
        }

    run = coordinator.get_run(run_id)
    if run is None:
        return {"ok": False, "reason": "unknown_run", "run_id": run_id, "state": state}

    if run["state"] in TERMINAL_OUTCOMES and run["state"] == state:
        _record_terminal_bookkeeping(
            run,
            state,
            actor=actor,
            failure_reason=failure_reason,
            evidence_ref=evidence_ref,
            append_task_note=append_task_note,
        )
        return {
            "ok": False,
            "reason": "already_terminal",
            "run_id": run_id,
            "state": run["state"],
        }

    if run["state"] in TERMINAL_OUTCOMES and run["state"] != state:
        return {
            "ok": False,
            "reason": "already_terminal",
            "run_id": run_id,
            "state": run["state"],
        }

    ts = storage.now()
    failure_class = None
    if state in {"failed", "retryable", "blocked"} and failure_reason:
        from ..quota_observe import classify_failure_reason

        failure_class = classify_failure_reason(failure_reason)
    elif state in {"failed", "retryable", "blocked"}:
        from ..quota_observe import FAILURE_CODE_ERROR

        failure_class = FAILURE_CODE_ERROR
    conn = storage.connect()
    try:
        # Task #1916 (audit O3): the superseding-run check and the terminal
        # write share one write transaction. On separate connections a newer
        # run could be launched between the check and the write.
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute(
            "SELECT run_id FROM dispatch_runs WHERE task_id=? AND "
            f"({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) "
            "ORDER BY created_at DESC LIMIT 1",
            (run["task_id"],),
        ).fetchone()
        if live is not None and live["run_id"] != run_id:
            conn.rollback()
            return {
                "ok": False,
                "reason": "superseded_run",
                "run_id": run_id,
                "state": run["state"],
            }
        res = coordinator.transition_run(
            conn,
            run_id,
            target_state=state,
            expected_states=("claimed", "launched", "running", "retryable"),
            failure_reason=(failure_reason or None),
            failure_class=failure_class,
            evidence_ref=evidence_ref,
            clear_lease=True,
            require_cancellation_unlocked=True,
            guard_sql=guard_sql,
            guard_params=guard_params,
        )
        if not res["ok"] and res.get("reason") == "guard_refused":
            conn.rollback()
            return {
                "ok": False,
                "reason": "guard_refused",
                "run_id": run_id,
                "state": res.get("state"),
            }
        if not res["ok"]:
            conn.commit()
            current = coordinator.get_run(run_id)
            if current and current["state"] in TERMINAL_OUTCOMES and current["state"] == state:
                _record_terminal_bookkeeping(
                    current,
                    state,
                    actor=actor,
                    failure_reason=failure_reason,
                    evidence_ref=evidence_ref,
                    append_task_note=append_task_note,
                )
            reason = (
                "already_terminal"
                if (current and current["state"] in TERMINAL_OUTCOMES)
                else res.get("reason", "already_terminal")
            )
            return {
                "ok": False,
                "reason": reason,
                "run_id": run_id,
                "state": current["state"] if current else None,
            }

        if state == "finished":
            # Task updates retain microseconds. Preserve finish precision too,
            # so a genuine later edit in this same second is never re-closed.
            conn.execute(
                "UPDATE dispatch_runs SET updated_at=? WHERE run_id=?",
                (datetime.now(UTC).isoformat(), run_id),
            )
        delegation_id = run.get("delegation_id")
        if delegation_id is not None:
            if state == "finished":
                conn.execute(
                    "UPDATE delegations SET status='completed', completed_at=?, updated_at=? "
                    "WHERE id=? AND status IN ('pending','in_flight')",
                    (ts, ts, delegation_id),
                )
                coordinator._consume_approval_pass(
                    conn,
                    task_id=int(run["task_id"]),
                    delegation_id=int(delegation_id),
                    run_id=run_id,
                    terminal=True,
                )
            elif state in {"failed", "blocked"}:
                conn.execute(
                    "UPDATE delegations SET status='failed', completed_at=?, updated_at=? "
                    "WHERE id=? AND status IN ('pending','in_flight')",
                    (ts, ts, delegation_id),
                )
                if state == "blocked":
                    coordinator._consume_approval_pass(
                        conn,
                        task_id=int(run["task_id"]),
                        delegation_id=int(delegation_id),
                        run_id=run_id,
                        terminal=True,
                    )
            elif state == "cancelled":
                conn.execute(
                    "UPDATE delegations SET status='cancelled', completed_at=?, updated_at=? "
                    "WHERE id=? AND status IN ('pending','in_flight')",
                    (ts, ts, delegation_id),
                )
            elif state == "retryable":
                conn.execute(
                    "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? "
                    "WHERE id=? AND status='in_flight'",
                    (ts, delegation_id),
                )
        conn.commit()
    finally:
        conn.close()

    run = coordinator.get_run(run_id) or run
    _record_terminal_bookkeeping(
        run,
        state,
        actor=actor,
        failure_reason=failure_reason,
        evidence_ref=evidence_ref,
        append_task_note=append_task_note,
    )

    return {
        "ok": True,
        "run_id": run_id,
        "state": state,
        "task_id": run["task_id"],
        "delegation_id": run.get("delegation_id"),
        "failure_reason": failure_reason,
        "evidence_ref": evidence_ref,
    }


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def reconcile_finished_runs(*, actor: str = ACTOR_SCHEDULER, limit: int = 200) -> dict[str, Any]:
    """Close tasks whose dispatch run finished but whose done write was lost.

    Task #1916 (audit O3): ``record_run_outcome`` commits the run and
    delegation, then closes the task in a second write. A crash between the two
    left the task open behind a finished run with nothing to retry it. This
    replays the same bookkeeping a duplicate report would, and only when it is
    safe: the run's ``finished`` outcome note is missing, no newer live run
    owns the task, and the task was not touched after the run finished (so a
    task someone reopened or re-planned is never closed by an old run).
    Idempotent; per-row failures are reported, never raised.
    """
    conn = storage.connect()
    try:
        conn.create_function(
            "dispatch_finished_noted",
            2,
            lambda notes, rid: _dispatch_outcome_noted(notes or "", state="finished", run_id=rid),
        )
        conn.create_function(
            "dispatch_task_untouched",
            2,
            lambda touched, finished: bool(
                _ts(touched) and _ts(finished) and _ts(touched) <= _ts(finished)
            ),
        )
        rows = conn.execute(
            "SELECT r.*, t.updated_at AS task_updated_at, t.status AS task_status FROM dispatch_runs r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE r.state='finished' "
            "AND NOT dispatch_finished_noted(t.notes, r.run_id) "
            "AND (t.status='done' OR dispatch_task_untouched(t.updated_at, r.updated_at)) "
            "AND NOT EXISTS (SELECT 1 FROM dispatch_runs newer WHERE newer.task_id=r.task_id "
            f"AND newer.run_id != r.run_id AND ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE})) "
            "ORDER BY r.updated_at DESC, r.run_id LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()

    healed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in rows:
        run = _row(row)
        task_updated_at = run.pop("task_updated_at", None)
        task_touched = _ts(task_updated_at)
        run_finished = _ts(run.get("updated_at"))
        task_status = run.pop("task_status", None)
        if task_status != "done" and (
            task_touched is None or run_finished is None or task_touched > run_finished
        ):
            continue
        try:
            _record_terminal_bookkeeping(
                run,
                "finished",
                actor=actor,
                failure_reason=None,
                evidence_ref=run.get("evidence_ref"),
                append_task_note=True,
                expected_task_updated_at=task_updated_at,
            )
        except Exception as exc:  # noqa: BLE001 - retried on the next pass
            errors.append(
                {
                    "run_id": run["run_id"],
                    "task_id": run["task_id"],
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
            continue
        healed.append({"run_id": run["run_id"], "task_id": run["task_id"]})
    return {"healed": healed, "errors": errors}


def dead_letter_exhausted(
    *,
    max_attempts: int | None = None,
    actor: str = ACTOR_SCHEDULER,
) -> dict[str, Any]:
    """Block pending delegations that have exhausted the retry budget.

    Dead-letter is operator-visible (``blocked`` run + task note + failed
    delegation). Never auto-relaunches these rows.
    """
    ceiling = max_attempts if max_attempts is not None else config.dispatch_scheduler_max_attempts()
    ceiling = max(1, ceiling)
    blocked: list[dict[str, Any]] = []
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM delegations WHERE status='pending' AND attempts>=?",
            (ceiling,),
        ).fetchall()
        candidates = [_row(r) for r in rows]
    finally:
        conn.close()

    for drow in candidates:
        conn = storage.connect()
        try:
            has_dispatch = conn.execute(
                "SELECT 1 FROM dispatch_runs WHERE delegation_id=? LIMIT 1",
                (drow["id"],),
            ).fetchone()
        finally:
            conn.close()
        if has_dispatch is None:
            continue
        # Prefer the latest retryable/failed run for this delegation; else mint
        # a synthetic blocked row so operators still see an index entry.
        runs = coordinator.list_runs(task_id=drow["task_id"], limit=20)
        target = next(
            (
                r
                for r in runs
                if r.get("delegation_id") == drow["id"] and r["state"] in {"retryable", "failed"}
            ),
            None,
        )
        reason = f"max_attempts_exceeded:{ceiling}"
        if target is not None:
            out = record_run_outcome(
                target["run_id"],
                "blocked",
                actor=actor,
                failure_reason=reason,
            )
            if out.get("ok"):
                blocked.append(
                    {
                        "run_id": target["run_id"],
                        "task_id": drow["task_id"],
                        "delegation_id": drow["id"],
                        "attempts": drow["attempts"],
                    }
                )
            continue

        # No prior run — refuse via evaluate path is enough; still mark
        # delegation failed + task note so Kanban does not keep polling.
        ts = storage.now()
        run_id = f"deadletter-{drow['task_id']}-{drow['id']}"
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO dispatch_runs "
                "(run_id, task_id, delegation_id, agent_label, state, actor, "
                "lease_owner, lease_expires_at, failure_reason, evidence_ref, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'blocked', ?, NULL, NULL, ?, NULL, ?, ?)",
                (
                    run_id,
                    drow["task_id"],
                    drow["id"],
                    drow["to_agent"],
                    actor,
                    reason,
                    ts,
                    ts,
                ),
            )
            conn.execute(
                "UPDATE delegations SET status='failed', completed_at=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (ts, ts, drow["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        coordinator._record_transition(
            actor=actor,
            task_id=drow["task_id"],
            delegation_id=drow["id"],
            agent_label=drow["to_agent"],
            run_id=run_id,
            state="blocked",
            failure_reason=reason,
            dedupe_key=f"dispatch-deadletter:{drow['id']}",
        )
        _append_dispatch_note(
            int(drow["task_id"]),
            f"blocked @ {ts} run={run_id} reason={reason}",
        )
        blocked.append(
            {
                "run_id": run_id,
                "task_id": drow["task_id"],
                "delegation_id": drow["id"],
                "attempts": drow["attempts"],
            }
        )

    return {"blocked_count": len(blocked), "blocked": blocked}


@dataclass
class DispatchScheduler:
    """Poller over the approved-dispatch ready set.

    Inject ``clock`` and ``launch`` for tests. Production enablement is
    ``UA_DISPATCH_SCHEDULER_ENABLED=1`` only — ``force=True`` is for operator
    one-shots / integration tests and does not flip the env gate permanently.
    """

    launch: LaunchBoundary | None = None
    clock: Callable[[], float] = field(default_factory=lambda: time.time)
    actor: str = ACTOR_SCHEDULER
    max_attempts: int | None = None
    batch_limit: int | None = None
    lease_ttl_s: float | None = None
    # Injectable quota projection (task #1251, C2): one build per tick, shared
    # by every claim in the batch. None builds it fresh inside the tick.
    quota_status: dict[str, Any] | None = None

    @staticmethod
    def enabled() -> bool:
        return config.dispatch_scheduler_enabled()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "actor": self.actor,
            "max_attempts": self.max_attempts or config.dispatch_scheduler_max_attempts(),
            "batch_limit": self.batch_limit or config.dispatch_scheduler_batch_limit(),
            "lease_ttl_s": self.lease_ttl_s
            if self.lease_ttl_s is not None
            else config.dispatch_scheduler_lease_ttl_s(),
            "now": datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
            "ready_count": len(list_ready_candidates(limit=50)) if self.enabled() else None,
        }

    def _launch_boundary(self) -> LaunchBoundary:
        return self.launch or scheduler_launch_boundary()

    def tick(self, *, force: bool = False) -> dict[str, Any]:
        """One scheduler poll. No-op when disabled unless ``force=True``."""
        started = self.clock()
        if not self.enabled() and not force:
            return {
                "status": "disabled",
                "reason": "UA_DISPATCH_SCHEDULER_ENABLED=0",
                "launched": [],
                "skipped": [],
                "reclaimed": [],
                "dead_lettered": [],
                "tick_started_at": datetime.fromtimestamp(started, tz=UTC).isoformat(),
            }

        lease_ttl = (
            self.lease_ttl_s
            if self.lease_ttl_s is not None
            else config.dispatch_scheduler_lease_ttl_s()
        )
        limit = self.batch_limit or config.dispatch_scheduler_batch_limit()
        ceiling = self.max_attempts or config.dispatch_scheduler_max_attempts()

        reclaim = coordinator.reclaim_expired_leases(actor=self.actor)

        # Task #602: reclaim terminates overran processes itself, because every
        # caller needs that and not just this one — claim_and_dispatch() also
        # reclaims and discards the result, which would otherwise leave a run
        # marked terminal with its process still working. The tick only records
        # what happened; re-killing here would signal a pid reclaim already
        # reaped, and by then that number may belong to someone else.
        for item in reclaim.get("overran") or []:
            task_id = item.get("task_id")
            if task_id is None:
                continue
            _append_dispatch_note(
                int(task_id),
                f"runtime ceiling exceeded @ {storage.now()} "
                f"run={item.get('run_id') or 'none'} pid={item.get('pid')} "
                f"{item.get('terminated') or 'not_terminated'}",
            )

        # Node-backed runs that expired while CLAIMED by a node are failures
        # (outcome unknown), not retries — reclaim already settled their queue
        # items and runs; the tick only reports them.
        for item in reclaim.get("node_expired") or []:
            task_id = item.get("task_id")
            if task_id is None:
                continue
            _append_dispatch_note(
                int(task_id),
                f"failed @ {storage.now()} run={item.get('run_id') or 'none'} "
                f"reason={item.get('failure_reason') or 'lease_expired_outcome_unknown'} "
                f"node={item.get('node')}",
            )

        for item in reclaim.get("reclaimed") or []:
            task_id = item.get("task_id")
            if task_id is None:
                continue
            reason = item.get("failure_reason") or "lease_expired"
            run_id = item.get("run_id") or "none"
            _append_dispatch_note(
                int(task_id),
                f"retryable @ {storage.now()} run={run_id} reason={reason}",
            )

        dead = dead_letter_exhausted(max_attempts=ceiling, actor=self.actor)
        finished_heal = reconcile_finished_runs(actor=self.actor)

        # C2 (task #1251): one quota projection per tick; every claim in the
        # batch consults it before the atomic claim so quota-exhausted lanes
        # are skipped rather than launched into a doomed run.
        from ..quota_observe import build_quota_status

        quota_status = self.quota_status if self.quota_status is not None else build_quota_status()

        launched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for cand in list_ready_candidates(limit=limit):
            # Exhausted attempts should already be dead-lettered; skip defensively.
            attempts = coordinator._delegation_attempts(cand["delegation_id"])
            if attempts is not None and attempts >= ceiling:
                skipped.append({**cand, "reason": "max_attempts_exceeded"})
                continue

            result = coordinator.claim_and_dispatch(
                task_id=cand["task_id"],
                delegation_id=cand["delegation_id"],
                actor=self.actor,
                launch=self._launch_boundary(),
                lease_ttl_s=lease_ttl,
                expected_lane=cand["agent_label"],
                quota_status=quota_status,
            )
            entry = {
                "task_id": cand["task_id"],
                "delegation_id": cand["delegation_id"],
                "agent_label": cand["agent_label"],
                **result,
            }
            if result.get("launched"):
                launched.append(entry)
                attempts = coordinator._delegation_attempts(cand["delegation_id"])
                attempt_bit = f" attempts={attempts}" if attempts is not None else ""
                _append_dispatch_note(
                    cand["task_id"],
                    f"launched @ {storage.now()} run={result['run_id']} "
                    f"agent={cand['agent_label']}{attempt_bit}",
                )
            elif result.get("reason") == "launch_failed":
                skipped.append(entry)
                if result.get("run_id"):
                    _append_dispatch_note(
                        cand["task_id"],
                        f"retryable @ {storage.now()} run={result['run_id']} reason=launch_failed",
                    )
            elif result.get("reason") == "quota_exhausted":
                skipped.append(entry)
                _note_quota_skip(cand["task_id"], result)
            else:
                skipped.append(entry)

        finished = self.clock()
        return {
            "status": "ok",
            "forced": force and not self.enabled(),
            "enabled": self.enabled(),
            "launched": launched,
            "skipped": skipped,
            "reclaimed": reclaim.get("reclaimed") or [],
            "reclaimed_count": reclaim.get("reclaimed_count", 0),
            "dead_lettered": dead.get("blocked") or [],
            "dead_lettered_count": dead.get("blocked_count", 0),
            "node_expired": reclaim.get("node_expired") or [],
            "node_expired_count": reclaim.get("node_expired_count", 0),
            # Expired claims held because their spawn may still be in flight
            # (#2512); each was warned about once, with its settle route.
            "spawn_unsettled": reclaim.get("spawn_unsettled") or [],
            "spawn_unsettled_count": reclaim.get("spawn_unsettled_count", 0),
            "finished_reconciled": finished_heal["healed"],
            "tick_started_at": datetime.fromtimestamp(started, tz=UTC).isoformat(),
            "tick_finished_at": datetime.fromtimestamp(finished, tz=UTC).isoformat(),
            "duration_s": round(finished - started, 4),
        }


def scheduler_tick(
    *,
    force: bool = False,
    launch: LaunchBoundary | None = None,
    clock: Callable[[], float] | None = None,
    actor: str = ACTOR_SCHEDULER,
    max_attempts: int | None = None,
    batch_limit: int | None = None,
    lease_ttl_s: float | None = None,
    quota_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Module-level entry used by CLI / future cron wrappers."""
    sched = DispatchScheduler(
        launch=launch,
        clock=clock or time.time,
        actor=actor,
        max_attempts=max_attempts,
        batch_limit=batch_limit,
        lease_ttl_s=lease_ttl_s,
        quota_status=quota_status,
    )
    return sched.tick(force=force)
