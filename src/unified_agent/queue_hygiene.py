"""Queue hygiene automation (task #729 / Lane A Increment A2).

Three idempotent transitions over the shared task store, run from the review
pipeline's cron tick (``unified-agent-review scan`` calls :func:`run_hygiene_pass`
before reconciling) so the queue grooms itself without a model in the loop:

1. **stale claim**: a ``doing`` task whose ``lock_expires_at`` has passed is a
   candidate. :mod:`lease_reaper`, the one writer that also serves
   ``doctor.reclaim_stale_task_locks``, probes its owner. A terminal or absent
   owner's task goes back to ``open`` with one notification; an active or
   unknown owner keeps it, and an unknown one is listed under ``leases_kept``.
2. **aging open**: an ``open`` task older than 14 days is demoted one priority
   step (high→med→low; ``low`` stays) — once.
3. **auto-archive**: a ``done`` task completed more than 30 days ago gets
   ``archived_at`` stamped — once.

A fourth rule, close-on-merge, lives in :mod:`queue_closure` and runs from the same
pass (task #1848); park and fold never run from here.
The liveness sweep (:mod:`task_liveness`, task #1749) runs last, over the
queue as the pass leaves it. The pass also expires stale per-run dispatch
settings files and sidecars (task #2191, :func:`expire_dispatch_artifacts`).

Idempotence and the no-dupe-notification rule share one mechanism: each
one-time action appends a dated marker line (``[hygiene: <kind> @ ts]``) to
the task's notes, and the pass skips any task whose notes already carry that
marker. The stale-claim marker also names the claim it released
(``claim=<claimed_at>``) and only skips that same claim, so a task claimed
again and abandoned again is released again (task #1916). Re-running the
pass therefore re-releases nothing, re-demotes nothing, re-archives nothing,
and re-notifies nothing.

Notifications are best-effort (same posture as ``reviewer._notify_best_effort``):
a broken bridge never fails the pass.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import lease_reaper, owner_liveness, registry, storage
from .tools.tasks import PRIORITIES

HYGIENE_AGING_OPEN_DAYS = 14
HYGIENE_ARCHIVE_DONE_DAYS = 30
# Live-claimant renewal (task #1337): a lapsed lease is renewed, not released,
# when the claimant was seen on the hub within this many minutes AND the task
# itself was touched within this many hours. The seen test itself lives in
# registry.seen_acting, shared with the liveness sweep (task #1749).
HYGIENE_LIVE_CLAIMANT_MINUTES = registry.LIVE_AGENT_MINUTES
HYGIENE_ACTIVE_TASK_HOURS = 24
_MIN_RENEWAL = timedelta(minutes=60)

_MARKER_BY_KIND = {
    "stale-claim": "[hygiene: stale-claim @",
    "aging-demote": "[hygiene: aging-demote @",
    "auto-archive": "[hygiene: auto-archive @",
}


def _clock() -> datetime:
    """Clock seam for aging thresholds (tests monkeypatch this)."""
    return datetime.now(UTC)


def _now_iso() -> str:
    return storage.now()


def _has_marker(notes: str | None, kind: str) -> bool:
    if not notes:
        return False
    return _MARKER_BY_KIND[kind] in notes


def _append_marker(notes: str | None, kind: str, *, claim: str | None = None) -> str:
    suffix = f" claim={claim}" if claim else ""
    marker = f"{_MARKER_BY_KIND[kind]} {_now_iso()}Z{suffix}]"
    return f"{(notes or '').rstrip()}\n{marker}" if notes else marker


def _claim_key(row) -> str:
    """Identify one claim generation (task #1916).

    ``claimed_at`` changes on every fresh claim and survives lease renewal, so
    it names the claim the release applies to. A row without it (a legacy
    ``doing`` row with only a lock) falls back to its expiry.
    """
    return str(row["claimed_at"] or row["lock_expires_at"] or "")


def _released_this_claim(notes: str | None, claim: str) -> bool:
    """True when a stale-claim marker already names this claim generation.

    A marker from an earlier claim (or a legacy marker with no ``claim=``)
    does not count: the task was released, claimed again, and abandoned again.
    """
    if not notes or not claim:
        return False
    prefix = _MARKER_BY_KIND["stale-claim"]
    tail = f" claim={claim}]"
    return any(prefix in line and tail in line for line in notes.splitlines())


def _notify_best_effort(message: str) -> bool:
    try:
        from .tools.notify import internal_route, notify

        with internal_route("queue_hygiene"):
            return bool(notify(message).get("delivered", False))
    except Exception:
        return False


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def renew_live_claims(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    """Extend lapsed leases whose claimant is demonstrably still working.

    A ``doing`` row past its ``lock_expires_at`` is renewed (caller commits) when
    its ``claimed_by`` agent acted after claiming (``last_seen`` later than
    ``claimed_at``) within ``UA_HYGIENE_LIVE_CLAIMANT_MINUTES`` and the task's
    ``updated_at`` is within ``UA_HYGIENE_ACTIVE_TASK_HOURS``. Renewal sets a
    fixed 60-minute lease and deliberately leaves ``updated_at`` alone, so an
    online-but-idle agent still loses a claim it stopped touching once the
    activity window passes. A claim whose own run is recorded as ended
    (``owner_liveness`` reads it terminal) is never renewed, however active the
    label is elsewhere (task #2483). Anything else is left for the release path.
    """
    current = now or _clock()
    live_cutoff = current - registry.live_agent_window()
    active_cutoff = current - timedelta(
        hours=_env_number("UA_HYGIENE_ACTIVE_TASK_HOURS", HYGIENE_ACTIVE_TASK_HOURS)
    )
    rows = conn.execute(
        "SELECT t.id, t.title, t.claimed_by, t.claimed_at, t.lock_expires_at, t.updated_at, "
        "a.last_seen, a.last_seen_via FROM tasks t LEFT JOIN agents a ON a.label = t.claimed_by "
        "WHERE t.status='doing' AND t.claimed_by IS NOT NULL "
        "AND t.lock_expires_at IS NOT NULL AND t.lock_expires_at <= ? "
        # Task #1525 (PR #866 review: "queue hygiene renews cancelled
        # leases"): a task whose current run has been flagged for
        # cancellation must be left to lapse, not resurrected with a fresh
        # hour-long lease just because its claimant is still active elsewhere.
        "AND t.cancel_requested = 0 "
        # Task #1848: a parked task is out of the working queue; parking must not
        # let a lapsed lease be renewed out of sight.
        "AND t.archived_at IS NULL",
        (current.isoformat(timespec="seconds"),),
    ).fetchall()
    renewed: list[dict[str, Any]] = []
    for r in rows:
        if not registry.seen_acting(
            r["last_seen"],
            r["last_seen_via"],
            since=live_cutoff,
            after=_parse_ts(r["claimed_at"]),
        ):
            continue
        touched = _parse_ts(r["updated_at"])
        if touched is None or touched < active_cutoff:
            continue
        # Task #2483: the label acting somewhere says nothing about this claim.
        # When the owner probe reads the claim's own run as ended, the lease is
        # left lapsed, so the reaper that runs next still has it as a candidate.
        probe = owner_liveness.probe_owner(conn, r["claimed_by"], task_id=r["id"], now=current)
        if probe.outcome == owner_liveness.TERMINAL:
            continue
        # Fixed-length renewal: claimed_at stays put while expiry advances, so
        # deriving the lease from their span would grow it on every pass.
        new_expiry = (current + _MIN_RENEWAL).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE tasks SET lock_expires_at=? WHERE id=? AND status='doing' AND claimed_by=?",
            (new_expiry, r["id"], r["claimed_by"]),
        )
        renewed.append(
            {
                "task_id": r["id"],
                "title": r["title"],
                "claimed_by": r["claimed_by"],
                "lock_expires_at": new_expiry,
            }
        )
    return renewed


def _stale_claim_notes(candidate: lease_reaper.Candidate) -> str | None:
    """The notes a release writes, or ``None`` when this claim was already released.

    Task #1916: the marker is per claim generation. A plain "any marker" check
    made release one-shot: a task released once, then claimed and abandoned
    again, stayed `doing` forever.
    """
    claim = _claim_key(
        {"claimed_at": candidate.claimed_at, "lock_expires_at": candidate.lock_expires_at}
    )
    if _released_this_claim(candidate.notes, claim):
        return None
    return _append_marker(candidate.notes, "stale-claim", claim=claim)


def _reap_stale_claims(
    conn, *, skip_task_ids: frozenset[int] = frozenset()
) -> tuple[lease_reaper.ReapResult, ...]:
    return lease_reaper.reap_stale_claim_leases(
        conn, now=_clock(), skip_task_ids=skip_task_ids, notes_for=_stale_claim_notes
    )


def _demote_aging_open(
    conn, *, skip_task_ids: frozenset[int] = frozenset()
) -> list[dict[str, Any]]:
    demoted: list[dict[str, Any]] = []
    cutoff = (_clock() - timedelta(days=HYGIENE_AGING_OPEN_DAYS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title, priority, notes, updated_at FROM tasks "
        "WHERE status='open' AND priority != 'low' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    for r in rows:
        if r["id"] in skip_task_ids:
            continue
        if _has_marker(r["notes"], "aging-demote"):
            continue
        idx = PRIORITIES.index(r["priority"])
        new_priority = PRIORITIES[min(idx + 1, len(PRIORITIES) - 1)]
        notes = _append_marker(r["notes"], "aging-demote")
        conn.execute(
            "UPDATE tasks SET priority=?, notes=?, updated_at=? WHERE id=?",
            (new_priority, notes, storage.next_revision(r["updated_at"]), r["id"]),
        )
        demoted.append(
            {
                "task_id": r["id"],
                "title": r["title"],
                "priority": r["priority"],
                "new_priority": new_priority,
            }
        )
    return demoted


def _archive_old_done(conn) -> list[dict[str, Any]]:
    archived: list[dict[str, Any]] = []
    cutoff = (_clock() - timedelta(days=HYGIENE_ARCHIVE_DONE_DAYS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title, notes, updated_at FROM tasks "
        "WHERE status='done' AND archived_at IS NULL AND completed_at IS NOT NULL "
        "AND completed_at < ?",
        (cutoff,),
    ).fetchall()
    for r in rows:
        # A row already carrying the marker (marker written, archive stamp
        # lost — e.g. a crash between the two older writes) still needs its
        # archived_at backfilled, but must not notify again.
        notes = r["notes"]
        if _has_marker(notes, "auto-archive"):
            notes = _append_marker(notes, "auto-archive")
            conn.execute(
                "UPDATE tasks SET archived_at=?, notes=?, updated_at=? WHERE id=?",
                (_now_iso(), notes, storage.next_revision(r["updated_at"]), r["id"]),
            )
        else:
            conn.execute(
                "UPDATE tasks SET archived_at=?, updated_at=? WHERE id=?",
                (_now_iso(), storage.next_revision(r["updated_at"]), r["id"]),
            )
        archived.append({"task_id": r["id"], "title": r["title"]})
    return archived


def _pending_finished_task_ids(conn) -> frozenset[int]:
    """Protect crash-recovery candidates from hygiene's revision changes.

    A failed or bounded reconciliation must remain eligible for the next
    pass. Read under the hygiene write lock so another completion cannot
    arrive between this check and a release/demotion.
    """
    from .dispatch import scheduler

    rows = conn.execute(
        "SELECT t.id, t.notes, t.updated_at AS task_updated_at, r.run_id, r.updated_at "
        "FROM dispatch_runs r JOIN tasks t ON t.id=r.task_id "
        "WHERE r.state='finished' AND t.status IN ('open','doing') "
        "AND NOT EXISTS (SELECT 1 FROM dispatch_runs newer WHERE newer.task_id=r.task_id "
        f"AND newer.run_id != r.run_id AND ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}))"
    ).fetchall()
    pending = set()
    for row in rows:
        touched = scheduler._ts(row["task_updated_at"])
        finished = scheduler._ts(row["updated_at"])
        if (
            touched is not None
            and finished is not None
            and touched <= finished
            and not scheduler._dispatch_outcome_noted(
                row["notes"] or "", state="finished", run_id=row["run_id"]
            )
        ):
            pending.add(row["id"])
    return frozenset(pending)


def run_hygiene_pass(*, send_notifications: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Run all three hygiene transitions. Returns per-action result lists.

    Idempotent: re-running the pass over unchanged state performs no
    transitions and sends no notifications (marker-gated). Notifications are
    batched into one best-effort message per pass.
    """
    try:
        storage.require_live_task_store()
    except storage.LiveStoreError as e:
        raise ToolError(str(e)) from e

    # Recover completion before hygiene advances updated_at. The reconciler
    # treats a later task revision as intentional replanning, so releasing or
    # demoting first would permanently hide a lost task-done write.
    dispatch_reconciled: list[dict[str, Any]] = []
    dispatch_reconcile_error: str | None = None
    if not dry_run:
        try:
            from .dispatch import scheduler as dispatch_scheduler

            healed = dispatch_scheduler.reconcile_finished_runs(actor="queue-hygiene")
            dispatch_reconciled = healed["healed"]
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            dispatch_reconcile_error = f"{type(exc).__name__}: {exc}"[:200]

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending_finished = _pending_finished_task_ids(conn)
        renewed = renew_live_claims(conn)
        reaped = _reap_stale_claims(conn, skip_task_ids=pending_finished)
        demoted = _demote_aging_open(conn, skip_task_ids=pending_finished)
        archived = _archive_old_done(conn)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    released = [
        {
            "task_id": r.task_id,
            "title": r.title,
            "claimed_by": r.owner or None,
            "expired_at": r.expired_at,
            "to_status": r.to_status,
            "interrupted_run": r.interrupted_run,
        }
        for r in reaped
        if r.action == lease_reaper.RELEASED
    ]
    leases_kept = [
        {"task_id": r.task_id, "claimed_by": r.owner, "warning": r.warning}
        for r in reaped
        if r.action == lease_reaper.KEPT and r.warning
    ]

    if not dry_run and released:
        from . import run_records

        # Task #1436: a released lapsed claim is an abandoned run. After the
        # commit, best-effort.
        for r in released:
            run_records.safe(
                run_records.record_abandoned,
                r["task_id"],
                agent=r["claimed_by"],
                closed_by="queue-hygiene",
                reason="lease expired; released by queue hygiene",
                reason_code=run_records.REASON_LEASE_EXPIRED,
                to_status=r["to_status"],
            )
        # The interrupted-work witness is already durable; this is the bus fan-out.
        lease_reaper.publish_after_commit(reaped)

    # Task #1848: work already merged closes, with the merge SHA as evidence,
    # only where exactly one PR whose head branch names the task has merged.
    # Applying is opt-in (UA_CLOSE_ON_MERGE=1) until the operator has reviewed
    # the first proposed list; otherwise every pass reports what would close.
    # Park and fold never run from here. Best-effort: a missing PR cursor never
    # fails the pass.
    closed_on_merge: list[dict[str, Any]] = []
    would_close_on_merge: list[dict[str, Any]] = []
    close_on_merge_error: str | None = None
    apply_close = os.environ.get("UA_CLOSE_ON_MERGE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        from .queue_closure import close_on_merge

        merged = close_on_merge(dry_run=dry_run or not apply_close)
        closed_on_merge = merged["closed"]
        would_close_on_merge = merged["would_close"]
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        close_on_merge_error = f"{type(exc).__name__}: {exc}"[:200]

    # Task #2394: finish approval-decision receipts a crash left behind (intents
    # older than a minute), and re-create the intent of a committed decision
    # that lost it (task #2484). Best-effort; never fails the pass.
    approval_receipts_swept: dict[str, Any] | None = None
    if not dry_run:
        try:
            from . import approval_receipts

            approval_receipts_swept = approval_receipts.sweep(older_than_s=60)
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            approval_receipts_swept = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    # Task #2393: a work-proposal item stuck in ``approving`` (a crash between
    # the approving claim and its task) recovers forward if its task exists,
    # else rolls back to pending. Best-effort; never fails the pass.
    work_proposals_recovered: dict[str, Any] | None = None
    if not dry_run:
        try:
            from . import work_proposals

            work_proposals_recovered = work_proposals.work_proposal_recover()
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            work_proposals_recovered = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2512: a dispatch run whose spawn started with no recorded outcome
    # holds its task until an operator settles it. This pass is the hub's
    # periodic sweep, so it warns once per such run past its lease (a task note
    # and an agent.health.alert event) and reports them all. Best-effort.
    try:
        from .dispatch import coordinator as dispatch_coordinator

        dispatch_spawn_unsettled: Any = dispatch_coordinator.publish_unsettled_spawn_warnings(
            actor="queue-hygiene", dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        dispatch_spawn_unsettled = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    notified = False
    if send_notifications and not dry_run and (released or demoted or archived or closed_on_merge):
        lines = []
        reopened = [r for r in released if not r["interrupted_run"]]
        interrupted = [r for r in released if r["interrupted_run"]]
        if reopened:
            lines.append(
                "stale claims released: " + ", ".join(f"#{r['task_id']}" for r in reopened)
            )
        if interrupted:
            lines.append(
                "interrupted work held as blocked (not re-run): "
                + ", ".join(f"#{r['task_id']}" for r in interrupted)
            )
        if demoted:
            lines.append(
                "aging open tasks demoted: " + ", ".join(f"#{r['task_id']}" for r in demoted)
            )
        if archived:
            lines.append(
                "old done tasks archived: " + ", ".join(f"#{r['task_id']}" for r in archived)
            )
        if closed_on_merge:
            lines.append(
                "merged work closed with its merge SHA: "
                + ", ".join(f"#{r['task_id']}" for r in closed_on_merge)
            )
        notified = _notify_best_effort("queue hygiene — " + "; ".join(lines) + ".")

    # Task #1497: governed-workflow plans whose run lost its origin authority
    # would stay 'active' forever. Best-effort; never fails the pass.
    workflow_plans: dict[str, Any] | None = None
    if not dry_run:
        try:
            from .langgraph_workflow import close_expired_workflow_plans

            workflow_plans = close_expired_workflow_plans()
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            workflow_plans = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #1749: the liveness sweep runs here, after releases and renewals, so
    # it classifies the queue as this pass leaves it. UA_LIVENESS_SWEEP picks
    # off|report|enforce (default report: digest entries only). Best-effort;
    # never fails the pass.
    try:
        from .task_liveness import run_liveness_sweep

        liveness = run_liveness_sweep(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        liveness = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2389: report what recently ended sessions left behind. Most real
    # sessions are reaped, never closed, so session_end alone would see none.
    try:
        from .session_owed import record_recently_ended

        session_owed_reports: Any = 0 if dry_run else record_recently_ended()
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        session_owed_reports = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2191: per-run Claude settings files and sidecars a killed
    # supervisor or a restart left behind expire after
    # UA_DISPATCH_SETTINGS_RETENTION_DAYS (default 7). A live run's files are
    # kept; launch logs are evidence and never expire here. Best-effort.
    try:
        dispatch_artifacts = expire_dispatch_artifacts(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        dispatch_artifacts = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2547: unread session mail left in a session that has ended is
    # invisible to the label's other sessions. Tell the recipient label once
    # per ended session and each sender once (UA_MESSAGE_STRANDED
    # off|report|notify, default notify). Best-effort; never fails the pass.
    # It runs BEFORE dead letters because the dead-letter query skips stranded
    # mail, so within one pass stranded wins. Mail dead-lettered on an earlier
    # pass is covered by the stranded query itself, which skips a message whose
    # sender already got a dead-letter notice (tasks #2551, #2555).
    try:
        from .messaging.delivery import sweep_stranded

        stranded_mail: Any = sweep_stranded(dry_run=dry_run, notify=send_notifications)
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        stranded_mail = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2391: unread attention mail past UA_MESSAGE_DEAD_LETTER_HOURS becomes a
    # dead letter, once, and its sender gets one notice (UA_MESSAGE_DEAD_LETTER
    # off|report|notify, default notify). A pass told not to send notifications
    # records facts only. Best-effort; never fails the pass.
    try:
        from .messaging.delivery import sweep_dead_letters

        dead_letters: Any = sweep_dead_letters(dry_run=dry_run, notify=send_notifications)
    except Exception as exc:  # noqa: BLE001 - hygiene must complete
        dead_letters = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2413: redeliver supervised-command completion messages whose
    # post-receipt send failed. Idempotent by derived message id. Best-effort.
    command_notices: Any = None
    if send_notifications and not dry_run:
        try:
            from .node_command import deliver_notices

            command_notices = deliver_notices()
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            command_notices = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #2399: reveal design panels past their deadline (answers stay
    # sealed until then), ingest session-end friction nudges, and re-check
    # friction fix claims whose window has passed; a fix claim closes only by
    # re-check. All three write, so a dry run skips them.
    design_panels_due: Any = None
    friction: Any = None
    if not dry_run:
        try:
            from .design_panels import reveal_due

            design_panels_due = reveal_due()
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            design_panels_due = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        try:
            from .friction_rollup import ingest_session_nudges, recheck

            ingested = ingest_session_nudges(cap=200)
            friction = {"ingested": ingested, **recheck(None)}
        except Exception as exc:  # noqa: BLE001 - hygiene must complete
            friction = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # Task #1751: the periodic drain point for completion returns (the others
    # are post-commit and start-up). A completion return is a chat message, so
    # a pass told not to send notifications leaves the outbox alone.
    # drain_pending never raises.
    completion_returns: dict[str, Any] | None = None
    if send_notifications and not dry_run:
        from .completion_return import drain_pending

        completion_returns = drain_pending()

    # Task #2454: the periodic drain point for ready-dependent notices (the
    # other is post-commit). Never raises; sends nothing when the flag is off.
    ready_notices: dict[str, Any] | None = None
    if send_notifications and not dry_run:
        from .ready_dependents import drain_pending as drain_ready

        ready_notices = drain_ready()

    return {
        "renewed": renewed,
        "released": released,
        "leases_kept": leases_kept,
        "demoted": demoted,
        "archived": archived,
        "closed_on_merge": closed_on_merge,
        "would_close_on_merge": would_close_on_merge,
        "close_on_merge_error": close_on_merge_error,
        "dispatch_reconciled": dispatch_reconciled,
        "dispatch_reconcile_error": dispatch_reconcile_error,
        "dispatch_spawn_unsettled": dispatch_spawn_unsettled,
        "workflow_plans": workflow_plans,
        "liveness": liveness,
        "session_owed_reports": session_owed_reports,
        "dispatch_artifacts": dispatch_artifacts,
        "completion_returns": completion_returns,
        "ready_notices": ready_notices,
        "dead_letters": dead_letters,
        "stranded_mail": stranded_mail,
        "command_notices": command_notices,
        "approval_receipts": approval_receipts_swept,
        "work_proposals": work_proposals_recovered,
        "design_panels": design_panels_due,
        "friction": friction,
        "notified": notified,
        "dry_run": dry_run,
    }


def expire_dispatch_artifacts(*, dry_run: bool = False) -> dict[str, Any]:
    """Expire stale dispatch settings files and sidecars (task #2191).

    The live-run set is read first: a run still holding a launch slot keeps
    its files whatever their age. When that read fails nothing is deleted.
    """
    from .dispatch import claude_sandbox

    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT run_id FROM dispatch_runs WHERE ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE})"
        ).fetchall()
    finally:
        conn.close()
    live = frozenset(str(row["run_id"]) for row in rows)
    return claude_sandbox.expire_stale_run_artifacts(live_run_ids=live, dry_run=dry_run)


# --- queue backpressure read (task #1430, defect 3) ---------------------------
#
# Leases (claimed_by / claimed_at / lock_expires_at), lanes and WIP caps
# (UA_AGENT_WIP_MAX, UA_LANE_WIP) already exist; nothing turned them into an
# operator signal. This is a read over the existing task and activity rows. It
# adds no table and no state, and unlike run_hygiene_pass it writes nothing.

_ACTIVE_STATUSES = ("open", "doing", "waiting_review", "blocked")
# `tracking` rows are never claimable (epics and trackers); every other lane is
# claimable by someone: default and dispatch-ready by anyone, an agent-named
# lane by that agent, operator-only by the operator (Devin, #814).
_UNCLAIMABLE_LANES = ("tracking",)
CONTENTION_WINDOW_DAYS = 7
OLDEST_OPEN_SIGNAL_HOURS = 72.0


def _hours(later: datetime, earlier: datetime | None) -> float | None:
    if earlier is None:
        return None
    return round(max(0.0, (later - earlier).total_seconds()) / 3600, 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def queue_pressure(now: datetime | None = None) -> dict[str, Any]:
    """Queue depth, claim age, WIP-cap pressure and contention. Read-only.

    Returns {as_of, level: ok|pressure, signals: [str], notes: [str], depth, open_by_lane,
    oldest_open: {lane: {id, age_hours}}, claims: {doing, unowned_doing, stale, no_expiry,
    oldest_claim_hours, median_claim_hours}, wip: {agent_max, agents: [{agent, doing, wip_max, at_cap}], lanes},
    contention: {window_days, tasks: [{task_id, claims, agents}]},
    intake: {active_routines, queued_fires, skipped_fires}}.

    `unowned_doing` counts `doing` rows with no `claimed_by`: work reached
    `doing` through generic task_update rather than task_claim, so no lease
    protects it (execution-substrate survey §3b). `contention` counts live
    tasks that entered `doing` under two or more agents within the window,
    a lower bar than the monitor's reassignment-thrash page.

    `level` is `pressure` only for `signals`: work in `doing` with no claimant,
    a lease past expiry, a claim with no expiry (locked forever), or a hard lane
    cap holding back waiting work. WIP counts use the claim gate's own
    predicate: expired leases do not occupy a slot. Aging
    backlog and contention are `notes`.
    """
    from . import config

    now = now or datetime.now(UTC)
    conn = storage.connect()
    try:
        depth = dict.fromkeys(_ACTIVE_STATUSES, 0)
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE archived_at IS NULL "
            f"AND status IN ({','.join('?' for _ in _ACTIVE_STATUSES)}) GROUP BY status",
            _ACTIVE_STATUSES,
        ):
            depth[row["status"]] = row["n"]

        open_by_lane: dict[str, int] = {}
        oldest_open: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT lane, COUNT(*) AS n, MIN(created_at) AS oldest FROM tasks "
            "WHERE status='open' AND archived_at IS NULL GROUP BY lane ORDER BY lane"
        ):
            open_by_lane[row["lane"]] = row["n"]
            if row["lane"] not in _UNCLAIMABLE_LANES:
                first = conn.execute(
                    "SELECT id, created_at FROM tasks WHERE status='open' AND archived_at IS NULL "
                    "AND lane=? ORDER BY created_at, id LIMIT 1",
                    (row["lane"],),
                ).fetchone()
                oldest_open[row["lane"]] = {
                    "id": first["id"],
                    "age_hours": _hours(now, _parse_ts(first["created_at"])),
                }

        doing_rows = conn.execute(
            "SELECT id, lane, claimed_by, claimed_at, lock_expires_at FROM tasks "
            "WHERE status='doing' AND archived_at IS NULL"
        ).fetchall()
        ages: list[float] = []
        stale = unowned = no_expiry = 0
        per_agent: dict[str, int] = {}
        per_lane: dict[str, int] = {}
        for row in doing_rows:
            if not row["claimed_by"]:
                unowned += 1
                continue
            age = _hours(now, _parse_ts(row["claimed_at"]))
            if age is not None:
                ages.append(age)
            lock = _parse_ts(row["lock_expires_at"])
            if lock is None:
                # task_claim treats a claim with no expiry as locked forever.
                no_expiry += 1
            elif lock < now:
                # Expired: the claim gate no longer counts it toward WIP, so
                # neither does this read (it is reported as stale instead).
                stale += 1
                continue
            per_agent[row["claimed_by"]] = per_agent.get(row["claimed_by"], 0) + 1
            per_lane[row["lane"]] = per_lane.get(row["lane"], 0) + 1

        agent_max = config.agent_wip_max()
        agents = [
            {
                "agent": agent,
                "doing": n,
                "wip_max": config.agent_wip_max_for(agent),
                "at_cap": n >= config.agent_wip_max_for(agent),
            }
            for agent, n in sorted(per_agent.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        lanes = []
        for lane, cap in sorted(config.lane_wip_limits().items()):
            running = per_lane.get(lane, 0)
            lanes.append(
                {
                    "lane": lane,
                    "doing": running,
                    "wip_max": cap["wip_max"],
                    "mode": cap["mode"],
                    "at_cap": running >= int(cap["wip_max"]),
                    "open_waiting": open_by_lane.get(lane, 0),
                }
            )

        cutoff = (now - timedelta(days=CONTENTION_WINDOW_DAYS)).isoformat(timespec="seconds")
        contended = [
            {"task_id": int(row["ref_id"]), "claims": row["claims"], "agents": row["agents"]}
            for row in conn.execute(
                "SELECT e.ref_id, COUNT(*) AS claims, COUNT(DISTINCT e.agent) AS agents "
                "FROM agent_events e JOIN tasks t ON t.id = CAST(e.ref_id AS INTEGER) "
                "WHERE e.kind='task' AND e.summary LIKE '%->doing%' AND e.ts >= ? "
                "AND t.status NOT IN ('done', 'cancelled') "
                "GROUP BY e.ref_id HAVING COUNT(DISTINCT e.agent) >= 2 "
                "ORDER BY CAST(e.ref_id AS INTEGER)",
                (cutoff,),
            )
        ]
        intake = _routine_intake(conn, cutoff)
    finally:
        conn.close()

    # `signals` are faults in the claim machinery and degrade doctor. `notes`
    # describe backlog shape (aging unclaimed work, re-claim churn): worth
    # showing, but a long backlog is not a broken queue, and a permanently
    # DEGRADED row would stop meaning anything.
    signals: list[str] = []
    notes: list[str] = []
    if unowned:
        signals.append(f"{unowned} doing task(s) without a claimant (no lease protects them)")
    if stale:
        signals.append(f"{stale} doing task(s) past an expired lease")
    if no_expiry:
        signals.append(
            f"{no_expiry} doing task(s) claimed with no lease expiry (locked indefinitely)"
        )
    for lane in lanes:
        if lane["at_cap"] and lane["mode"] == "hard" and lane["open_waiting"]:
            signals.append(
                f"lane {lane['lane']} at its hard WIP cap ({lane['doing']}/{lane['wip_max']}) "
                f"with {lane['open_waiting']} open task(s) waiting"
            )
    for lane, oldest in oldest_open.items():
        age = oldest["age_hours"]
        if lane == "dispatch-ready" and age is not None and age > OLDEST_OPEN_SIGNAL_HOURS:
            notes.append(f"dispatch-ready task #{oldest['id']} unclaimed for {age:g}h")
    if intake["queued_fires"]:
        notes.append(
            f"{intake['queued_fires']} routine fire(s) queued behind an active run "
            "(concurrency=queue)"
        )
    if intake["skipped_fires"]:
        notes.append(
            f"{intake['skipped_fires']} routine fire(s) skipped in the last "
            f"{CONTENTION_WINDOW_DAYS} days (concurrency=skip)"
        )
    if contended:
        notes.append(
            f"{len(contended)} live task(s) claimed by 2+ agents in the last "
            f"{CONTENTION_WINDOW_DAYS} days"
        )

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "level": "pressure" if signals else "ok",
        "signals": signals,
        "notes": notes,
        "depth": depth,
        "open_by_lane": open_by_lane,
        "oldest_open": oldest_open,
        "claims": {
            "doing": len(doing_rows),
            "unowned_doing": unowned,
            "stale": stale,
            "no_expiry": no_expiry,
            "oldest_claim_hours": max(ages) if ages else None,
            "median_claim_hours": _median(ages),
        },
        "wip": {"agent_max": agent_max, "agents": agents, "lanes": lanes},
        "contention": {"window_days": CONTENTION_WINDOW_DAYS, "tasks": contended},
        "intake": intake,
    }


def _routine_intake(conn, cutoff: str) -> dict[str, Any]:
    """Routine intake as queue pressure (task #1430): the routines policy
    (lane, concurrency skip|queue, catch_up) is the intake policy, so its
    outcomes are the intake signal. Read-only; zeros when routines are absent."""
    empty = {"active_routines": 0, "queued_fires": 0, "skipped_fires": 0}
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('routines','routine_runs')"
        )
    }
    if tables != {"routines", "routine_runs"}:
        return empty
    active = conn.execute("SELECT COUNT(*) AS n FROM routines WHERE paused=0").fetchone()["n"]
    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM routine_runs rr JOIN tasks t ON t.id=rr.task_id "
        "WHERE rr.status='queued' AND t.status='blocked'"
    ).fetchone()["n"]
    skipped = conn.execute(
        "SELECT COUNT(*) AS n FROM routine_runs "
        "WHERE status='skipped' AND reason='overlap' AND created_at >= ?",
        (cutoff,),
    ).fetchone()["n"]
    return {"active_routines": active, "queued_fires": queued, "skipped_fires": skipped}
