"""Fail-closed approval-to-dispatch coordinator (tasks #548–#549).

Consumes an operator-approved delegation exactly once, mints a durable run_id,
and records transitions on the existing event log. Refuses denied / expired /
cancelled / unapproved / malformed / unknown-agent / not-dispatchable inputs
with an operator-visible reason. Crash mid-launch leaves a reclaimable lease
without double execution. Default launch boundary resolves named-agent routes
(#549) while stubbing external process spawn.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import (
    agent_lifecycle,
    config,
    edition,
    events,
    node_queue,
    profiles,
    storage,
    task_holds,
    trust_presets,
)
from ..dispatch_reads import (  # noqa: F401 - dispatch-run readers; re-exported here
    APPROVED_MARKER_RE,
    DENIED_MARKER_RE,
    DISPATCH_APPROVAL_TOOL,
    EXPIRED_MARKER_RE,
    MALFORMED_APPROVED_RE,
    _approval_row_status,
    _canonical_payload,
    _delegation_attempts,
    _last_marker_pos,
    _operator_notes_for_approval,
    _retry_count_for_run,
    _row,
    dispatch_approval_payload,
    dispatch_approval_row,
    dispatch_approval_selection,
    dispatch_plan_blocker,
    dispatch_recovery_run,
    list_runs,
)
from ..dispatch_states import LIVE_STATES  # noqa: F401 - also read as coordinator.LIVE_STATES
from . import supervise
from .launch import LaunchBoundary, scheduler_launch_boundary
from .routing import DISPATCHABLE_LABELS

logger = logging.getLogger(__name__)

CHANNEL_DISPATCH_TRANSITION = "agent.dispatch.transition"
ACTOR_COORDINATOR = "dispatch-coordinator"

TERMINAL_TASK = frozenset({"done", "cancelled"})
DEAD_DELEGATION = frozenset({"cancelled", "completed", "failed"})

DEFAULT_LEASE_TTL_S = 300.0


def _pid_from_evidence(evidence_ref: str | None) -> int | None:
    """Pid recorded in an evidence_ref, or None.

    Delegates to ``supervise.parse_pid_token`` so there is exactly one parser
    for this format. The original implementation here used ``split(":", 1)``,
    which silently returned None once task #602 extended the token to
    ``pid:<n>:<start>`` — a live agent then read as having no pid at all, and
    the liveness protections keyed off it never fired.
    """
    pid, _start = supervise.parse_pid_token(evidence_ref)
    return pid


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _new_run_id(task_id: int) -> str:
    return f"run-{task_id}-{secrets.token_hex(4)}"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _record_transition(
    *,
    actor: str,
    task_id: int | None,
    delegation_id: int | None,
    agent_label: str | None,
    run_id: str | None,
    state: str,
    failure_reason: str | None = None,
    evidence_ref: str | None = None,
    retry_count: int | None = None,
    dedupe_key: str | None = None,
    termination_outcome: str | None = None,
    trust_snapshot: dict[str, Any] | None = None,
    originator_snapshot: dict[str, Any] | None = None,
) -> str:
    ts = storage.now()
    attempts = retry_count if retry_count is not None else _delegation_attempts(delegation_id)
    payload = {
        "actor": actor,
        "task_id": task_id,
        "delegation_id": delegation_id,
        "agent_label": agent_label,
        "run_id": run_id,
        "timestamp": ts if ts.endswith("Z") else ts.replace("+00:00", "Z"),
        "state": state,
        "failure_reason": failure_reason,
        "evidence_ref": evidence_ref,
        # Operator field (task #551): mirrors delegations.attempts at transition time.
        "retry_count": attempts,
        "host": config.current_source_host(),
    }
    if trust_snapshot is not None:
        payload["trust_preset"] = trust_snapshot
    if originator_snapshot is not None:
        payload["originator_trust_preset"] = originator_snapshot
    # Task #1221: only present for cancellation transitions. "confirmed" or
    # "unknown" — never render a cancel event as a confirmed stop unless this
    # says so.
    if termination_outcome is not None:
        payload["termination_outcome"] = termination_outcome
    return events.record_event(
        CHANNEL_DISPATCH_TRANSITION,
        payload=payload,
        originator=actor,
        dedupe_key=dedupe_key,
    )


def _load_task(conn, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(row) if row else None


def _has_cancelled_ancestor(conn, task: dict) -> bool:
    """Return whether any parent in the task's full lineage is cancelled."""
    parent_id = task.get("parent_id")
    seen = {int(task["id"])}
    while parent_id is not None:
        parent_id = int(parent_id)
        if parent_id in seen:
            return True
        seen.add(parent_id)
        parent = _load_task(conn, parent_id)
        if parent is None:
            return True
        if parent.get("status") == "cancelled":
            return True
        parent_id = parent.get("parent_id")
    return False


def _load_delegation(
    conn,
    *,
    delegation_id: int | None = None,
    task_id: int | None = None,
) -> dict | None:
    if delegation_id is not None:
        row = conn.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone()
        return _row(row) if row else None
    if task_id is not None:
        row = conn.execute(
            "SELECT * FROM delegations WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _row(row) if row else None
    return None


#: Run states a dispatch run reaches only after its launch boundary succeeded.
LAUNCHED_RUN_STATES = ("launched", "running", "finished", "failed", "blocked", "denied")
#: Run states that never reached the launch boundary's success (without a
#: ``dispatch-launched`` event): a launch key bound to such a run may be rebound
#: by a retry of the same task (#2501).
_UNLAUNCHED_RUN_STATES = ("retryable", "refused", "expired", "cancelled")


def launch_key_run(conn, launch_key: str) -> dict | None:
    """The run a Control Center launch key is bound to, with its task and state (#2501).

    ``launched`` is True when the run reached launch: a launched state, a
    ``dispatch-launched`` event (a launched run later cancelled or expired), or a
    recorded spawn (``spawn_outcome='spawned'``, even when the launcher lost its
    lease afterwards). ``spawn_unsettled`` is True while a spawn started without
    a recorded outcome: the launcher may still be spawning (review #1521 pass 4).
    """
    row = conn.execute(
        "SELECT k.launch_key, k.task_id, k.run_id, k.spawn_started_at, k.spawn_outcome, "
        "r.state, r.delegation_id "
        "FROM dispatch_launch_keys k LEFT JOIN dispatch_runs r ON r.run_id = k.run_id "
        "WHERE k.launch_key=?",
        (launch_key,),
    ).fetchone()
    if row is None:
        return None
    bound = dict(row)
    bound["spawn_unsettled"] = (
        bound["spawn_started_at"] is not None and bound["spawn_outcome"] is None
    )
    launched = bound["state"] in LAUNCHED_RUN_STATES or bound["spawn_outcome"] == "spawned"
    if not launched and bound["state"] is not None:
        event = conn.execute(
            "SELECT 1 FROM event_log WHERE dedupe_key=?", (f"dispatch-launched:{bound['run_id']}",)
        ).fetchone()
        launched = event is not None
    bound["launched"] = launched
    return bound


def _launch_key_refusal(conn, launch_key: str, task_id: int) -> dict | None:
    """Why ``launch_key`` may not mint a run for ``task_id`` now, or None (#2501).

    Called under the claim's write lock. A key is free, or bound to a run of the
    same task that never reached launch (a retry may replace it). Any other bound
    run, of any task, holds the key: one launch, one job, across hub workers. So
    does a run whose spawn started without a recorded outcome, whatever its state
    (a reclaimed lease does not stop a stalled spawn): ``launch_unsettled``.
    """
    bound = launch_key_run(conn, launch_key)
    if bound is None:
        return None
    reusable = (
        bound["task_id"] == task_id
        and not bound["launched"]
        and not bound["spawn_unsettled"]
        and (bound["state"] is None or bound["state"] in _UNLAUNCHED_RUN_STATES)
    )
    if reusable:
        return None
    if bound["task_id"] != task_id:
        reason = "launch_key_bound"
    elif bound["spawn_unsettled"] and not bound["launched"]:
        reason = "launch_unsettled"
    else:
        reason = "already_claimed"
    return {"launched": False, "reason": reason, "run_id": bound["run_id"]}


def _bind_launch_key(conn, launch_key: str, task_id: int, run_id: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO dispatch_launch_keys (launch_key, task_id, run_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(launch_key) DO UPDATE SET "
        "task_id=excluded.task_id, run_id=excluded.run_id, spawn_started_at=NULL, "
        "spawn_outcome=NULL, updated_at=excluded.updated_at",
        (launch_key, task_id, run_id, ts, ts),
    )


#: Every dispatch run's spawn mark lives in ``dispatch_launch_keys`` (#2512): a
#: keyed launch's row is filed under its Control Center key, an unkeyed dispatch's
#: under its own per-run key, ``run:<run_id>``. One fence, one in-flight registry
#: and one operator settlement then cover both. Caller keys may not use the prefix.
RUN_SPAWN_KEY_PREFIX = "run:"

#: SQL: the ``dispatch_runs`` row's spawn started without a recorded outcome.
_SPAWN_UNSETTLED_SQL = (
    "EXISTS (SELECT 1 FROM dispatch_launch_keys k WHERE k.run_id = dispatch_runs.run_id "
    "AND k.spawn_started_at IS NOT NULL AND k.spawn_outcome IS NULL)"
)


def _spawn_key(run_id: str, launch_key: str | None) -> str:
    """The key a run's spawn mark is filed under: its launch key, else ``run:<run_id>``."""
    return launch_key or f"{RUN_SPAWN_KEY_PREFIX}{run_id}"


#: Suffix of a resume's spawn mark, ``run:<run_id>:resume`` (#2527).
_RESUME_SPAWN_KEY_SUFFIX = ":resume"


def _resume_spawn_key(run_id: str) -> str:
    """The key a resume of ``run_id`` files its spawn mark under (#2527).

    A resume never re-arms the claim's own mark (a Control Center key, or
    ``run:<run_id>``): a keyed claim's ``spawn_outcome='spawned'`` is that key's
    launch evidence (``launch_key_run``), and the claim's ``dispatch-launched``
    event proves only the claim's spawn. Every resume of the run re-arms this
    one mark instead, so the claim's record stays as it was.
    """
    return f"{RUN_SPAWN_KEY_PREFIX}{run_id}{_RESUME_SPAWN_KEY_SUFFIX}"


def _arm_resume_spawn(conn, run_id: str, task_id: int, ts: str) -> str | None:
    """Arm the resume's spawn mark in the resume's transaction; its key, or None.

    Inserts ``run:<run_id>:resume``, or resets it when an earlier resume's
    outcome is recorded. A mark still unsettled is never cleared: None, and the
    caller refuses (``launch_unsettled``) by rolling its transaction back.
    """
    key = _resume_spawn_key(run_id)
    cur = conn.execute(
        "INSERT INTO dispatch_launch_keys (launch_key, task_id, run_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(launch_key) DO UPDATE SET "
        "task_id=excluded.task_id, spawn_started_at=NULL, spawn_outcome=NULL, "
        "spawn_launcher=NULL, updated_at=excluded.updated_at "
        "WHERE dispatch_launch_keys.run_id=excluded.run_id "
        "AND (dispatch_launch_keys.spawn_started_at IS NULL "
        "OR dispatch_launch_keys.spawn_outcome IS NOT NULL)",
        (key, task_id, run_id, ts, ts),
    )
    return key if cur.rowcount == 1 else None


def _run_spawn_unsettled(conn, run_id: str) -> bool:
    """The run's spawn started and no outcome is recorded: it may still be spawning."""
    row = conn.execute(
        f"SELECT 1 FROM dispatch_runs WHERE run_id=? AND {_SPAWN_UNSETTLED_SQL}", (run_id,)
    ).fetchone()
    return row is not None


def _holding_unsettled_spawns(conn, task_id: int | None = None) -> list[dict[str, Any]]:
    """Runs whose unsettled spawn holds their task, oldest first (#2512).

    A run whose spawn started without a recorded outcome holds its task, whatever
    its state (a reclaimed lease or a cancellation does not stop a stalled
    spawn), until the outcome is recorded or ``settle_unsettled_spawn`` proves
    the launcher and the job gone. A run that reached launch (a launched state or
    a ``dispatch-launched`` event) or handed its job to a node (``node:``
    evidence, whose queue reports the outcome) has nothing left to spawn.
    ``stuck_since`` is when the run stopped being covered by a live claim: its
    lease expiry while it is claimed, else the spawn's start.
    """
    where = "k.spawn_started_at IS NOT NULL AND k.spawn_outcome IS NULL"
    params: tuple[Any, ...] = ()
    if task_id is not None:
        where += " AND k.task_id=?"
        params = (task_id,)
    rows = conn.execute(
        "SELECT k.run_id, k.task_id, k.spawn_started_at, k.launch_key, r.state, "
        "r.evidence_ref, r.lease_expires_at FROM dispatch_launch_keys k "
        f"LEFT JOIN dispatch_runs r ON r.run_id = k.run_id WHERE {where} "
        "ORDER BY k.spawn_started_at",
        params,
    ).fetchall()
    held: list[dict[str, Any]] = []
    for row in rows:
        if row["state"] in LAUNCHED_RUN_STATES or (row["evidence_ref"] or "").startswith("node:"):
            continue
        # The claim's ``dispatch-launched`` event proves the claim's spawn, never
        # a later resume's (#2527): a resume of a run that once launched is held.
        if not row["launch_key"].endswith(_RESUME_SPAWN_KEY_SUFFIX):
            launched = conn.execute(
                "SELECT 1 FROM event_log WHERE dedupe_key=?",
                (f"dispatch-launched:{row['run_id']}",),
            ).fetchone()
            if launched is not None:
                continue
        stuck_since = (
            row["lease_expires_at"]
            if row["state"] == "claimed" and row["lease_expires_at"]
            else row["spawn_started_at"]
        )
        held.append(
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "state": row["state"],
                "keyed": not row["launch_key"].startswith(RUN_SPAWN_KEY_PREFIX),
                "spawn_started_at": row["spawn_started_at"],
                "lease_expires_at": row["lease_expires_at"],
                "stuck_since": stuck_since,
            }
        )
    return held


def _task_spawn_refusal(conn, task_id: int) -> dict | None:
    """A run of ``task_id`` that may still be spawning, as a refusal, or None (#2512).

    Called under the claim's write lock, for keyed and unkeyed dispatch alike;
    the holding rule is ``_holding_unsettled_spawns``.
    """
    held = _holding_unsettled_spawns(conn, task_id)
    if held:
        return {"launched": False, "reason": "launch_unsettled", "run_id": held[0]["run_id"]}
    return None


#: An unsettled spawn past its lease longer than this degrades ``system_doctor``.
UNSETTLED_SPAWN_GRACE_S = 15 * 60

#: The operator's recovery for an unsettled spawn (``webui.cc_launch.settle_spawn``).
SETTLE_SPAWN_ROUTE = "POST /api/dispatch-runs/{run_id}/settle-spawn"


def unsettled_spawns(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every run whose unsettled spawn holds its task, with how long it has (#2512).

    Read-only. Each row adds ``past_lease_s`` (seconds since ``stuck_since``,
    negative while a claim's lease is still live) and ``settle`` (the operator
    route). Feeds the scheduler tick, the queue-hygiene pass and the
    ``dispatch_unsettled_spawns`` doctor row.
    """
    at = now or datetime.now(UTC)
    conn = storage.connect()
    try:
        held = _holding_unsettled_spawns(conn)
    finally:
        conn.close()
    for item in held:
        since = _parse_ts(item["stuck_since"])
        item["past_lease_s"] = int((at - since).total_seconds()) if since else None
        item["settle"] = SETTLE_SPAWN_ROUTE.format(run_id=item["run_id"])
    return held


def warn_unsettled_spawn(item: dict[str, Any], *, actor: str) -> bool:
    """Tell the operator, once per run, that an unsettled spawn holds its task (#2512).

    Writes a task note naming the run and the settle route, then one
    ``agent.health.alert`` event (deduplicated by run, so the supervisor wake
    loop, the Zulip bridge and the cloud mirror see it once). The note is
    written only while that event is absent, and its text is fixed per run, so
    a retry after a crash between the two re-delivers the same note, which the
    task store drops as a duplicate. Returns True when this call published.
    """
    run_id, task_id = item["run_id"], item.get("task_id")
    dedupe_key = f"dispatch-spawn-unsettled:{run_id}"
    conn = storage.connect()
    try:
        seen = conn.execute("SELECT 1 FROM event_log WHERE dedupe_key=?", (dedupe_key,)).fetchone()
    finally:
        conn.close()
    if seen is not None:
        return False
    settle = SETTLE_SPAWN_ROUTE.format(run_id=run_id)
    summary = (
        f"dispatch run {run_id} started its spawn with no recorded outcome, so task "
        f"#{task_id} is held: nothing reclaims or respawns it. If its launcher crashed or "
        f"its spawn raised, an operator settles it with {settle} (refused until the "
        "launcher and the job are proven gone)."
    )
    if task_id is not None:
        try:
            from ..tools import tasks as tasks_tool

            tasks_tool.task_update(
                int(task_id), append_note=f"[dispatch: spawn-unsettled run={run_id}] {summary}"
            )
        except Exception:  # noqa: BLE001 - the event below still reaches the operator
            logger.warning("dispatch run %s: unsettled-spawn note failed", run_id, exc_info=True)
    _event, won = events.record_event_once(
        events.CHANNEL_HEALTH_ALERT,
        payload={
            "kind": "dispatch_spawn_unsettled",
            "ref_id": str(task_id) if task_id is not None else None,
            "task_id": task_id,
            "run_id": run_id,
            "summary": summary,
            "settle": settle,
        },
        originator=actor,
        dedupe_key=dedupe_key,
    )
    return won


def publish_unsettled_spawn_warnings(
    *, actor: str, dry_run: bool = False, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Warn once for every unsettled spawn past its lease; return them all (#2512).

    The queue-hygiene pass calls this, since it is the hub's periodic sweep. A dry
    run publishes nothing. Each returned row carries ``warned``: whether this call
    published its warning.
    """
    held = unsettled_spawns(now=now)
    for item in held:
        item["warned"] = False
        if dry_run or item["past_lease_s"] is None or item["past_lease_s"] < 0:
            continue
        try:
            item["warned"] = warn_unsettled_spawn(item, actor=actor)
        except Exception:  # noqa: BLE001 - the next sweep retries
            logger.warning("unsettled-spawn warning failed for %s", item["run_id"], exc_info=True)
    return held


def _fence_spawn(launch_key: str, run_id: str, actor: str) -> bool:
    """Mark the run's spawn as started, only while this launcher holds it.

    ``launch_key`` is the run's spawn key (``_spawn_key``): a keyed launch's key,
    or ``run:<run_id>`` for an unkeyed dispatch (#2512).

    One transaction, immediately before the spawn: the run is still ``claimed``
    by ``actor`` under a live lease, the key is still bound to it and no spawn
    started yet. A launcher that lost any of these spawns nothing (review #1521
    pass 4); one that passed leaves a mark no other launcher can rebind past
    until ``_settle_spawn`` records the outcome.

    The run is registered in ``_SPAWNS_IN_FLIGHT`` before the mark commits (review
    #1521 pass 6), so from the moment the mark exists this process reads as a
    launcher still spawning; a failed fence unregisters it.
    """
    launcher = _launcher_token()  # read before the transaction: it may call ``ps``
    now = datetime.now(UTC).isoformat()
    with _SPAWN_LOCK:
        _SPAWNS_IN_FLIGHT.add(run_id)
    fenced = False
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE dispatch_launch_keys SET spawn_started_at=?, spawn_launcher=?, updated_at=? "
            "WHERE launch_key=? AND run_id=? AND spawn_started_at IS NULL AND EXISTS ("
            "SELECT 1 FROM dispatch_runs WHERE run_id=? AND state='claimed' "
            "AND lease_owner=? AND lease_expires_at > ?)",
            (now, launcher, now, launch_key, run_id, run_id, actor, now),
        )
        conn.commit()
        fenced = cur.rowcount == 1
        return fenced
    finally:
        conn.close()
        if not fenced:
            _unregister_spawn(run_id)


def _fence_holds(launch_key: str, run_id: str) -> bool:
    """This run's spawn mark is still unsettled and its spawn key still bound to the run."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM dispatch_launch_keys WHERE launch_key=? AND run_id=? "
            "AND spawn_started_at IS NOT NULL AND spawn_outcome IS NULL",
            (launch_key, run_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _unregister_spawn(run_id: str) -> None:
    with _SPAWN_LOCK:
        _SPAWNS_IN_FLIGHT.discard(run_id)


#: Runs this process fenced and whose spawn outcome is not yet recorded (review
#: #1521 passes 5-6). Read and written only under ``_SPAWN_LOCK``.
_SPAWNS_IN_FLIGHT: set[str] = set()
_SPAWN_LOCK = threading.Lock()


_LAUNCHER_TOKENS: dict[int, str] = {}


def _launcher_token() -> str:
    """This process as a launcher: ``pid:<n>:<start>`` (a recycled pid never matches)."""
    pid = os.getpid()
    if pid not in _LAUNCHER_TOKENS:
        _LAUNCHER_TOKENS[pid] = supervise.pid_token(pid)
    return _LAUNCHER_TOKENS[pid]


def settle_unsettled_spawn(run_id: str, *, actor: str) -> dict[str, Any]:
    """Record a run's unsettled spawn as ``failed``, once it provably did nothing.

    Keyed or unkeyed alike (#2512): every run's spawn mark is a
    ``dispatch_launch_keys`` row, found here by ``run_id``.

    The operator's recovery for a launcher that crashed after ``_fence_spawn``
    (review #1521 pass 5). It refuses unless both are proven gone:

    * the launcher: its process token names a process that is not running (or a
      recycled pid), or it is this process and the spawn is not in flight here;
    * the job: the run has no recorded process, or that process is not running,
      and the run is not node-routed (a node reports its own outcome) nor
      launched; a still-claimed run's lease has expired.

    The outcome is written in one transaction that re-checks the key is still
    bound to this run with its spawn unsettled. Returns ``{"settled": True, ...}``
    or ``{"settled": False, "reason": ...}``.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT k.launch_key, k.spawn_launcher, r.state, r.evidence_ref, r.lease_expires_at "
            "FROM dispatch_launch_keys k LEFT JOIN dispatch_runs r ON r.run_id = k.run_id "
            "WHERE k.run_id=? AND k.spawn_started_at IS NOT NULL AND k.spawn_outcome IS NULL",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"settled": False, "run_id": run_id, "reason": "not_unsettled"}
    launcher = row["spawn_launcher"]
    launcher_pid, _start = supervise.parse_pid_token(launcher)
    here = launcher_pid == os.getpid()
    if not here and (launcher is None or supervise.pid_is_ours(launcher)):
        return {"settled": False, "run_id": run_id, "reason": "launcher_may_be_spawning"}
    evidence = row["evidence_ref"] or ""
    if evidence.startswith("node:"):
        return {"settled": False, "run_id": run_id, "reason": "node_reports_its_outcome"}
    if row["state"] in LAUNCHED_RUN_STATES or supervise.pid_is_ours(evidence or None):
        return {"settled": False, "run_id": run_id, "reason": "job_may_be_running"}
    expires = _parse_ts(row["lease_expires_at"])
    if row["state"] == "claimed" and (expires is None or expires > datetime.now(UTC)):
        return {"settled": False, "run_id": run_id, "reason": "lease_still_live"}
    # This process's own launcher is checked and the outcome written under the
    # lock its fence registers under: no spawn of it can be in flight meanwhile.
    with _SPAWN_LOCK:
        if here and run_id in _SPAWNS_IN_FLIGHT:
            return {"settled": False, "run_id": run_id, "reason": "launcher_may_be_spawning"}
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE dispatch_launch_keys SET spawn_outcome='failed', updated_at=? "
                "WHERE launch_key=? AND run_id=? AND spawn_started_at IS NOT NULL "
                "AND spawn_outcome IS NULL",
                (storage.now(), row["launch_key"], run_id),
            )
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount != 1:
        return {"settled": False, "run_id": run_id, "reason": "not_unsettled"}
    logger.info("dispatch run %s: unsettled spawn settled as failed by %s", run_id, actor)
    return {"settled": True, "run_id": run_id, "spawn_outcome": "failed"}


def _settle_spawn(launch_key: str, run_id: str, outcome: str) -> None:
    """Record the run's spawn outcome (``spawned`` or ``failed``) under its spawn key."""
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_launch_keys SET spawn_outcome=?, updated_at=? "
            "WHERE launch_key=? AND run_id=?",
            (outcome, storage.now(), launch_key, run_id),
        )
        conn.commit()
    finally:
        conn.close()


#: ``_fenced_launch`` spawned nothing: the fence was never taken (the launcher no
#: longer holds a live claim on the run, or its mark is not armed).
FENCE_REFUSED = "fence_refused"
#: ``_fenced_launch`` spawned nothing: the fence was taken, then settled from
#: under it (or its re-check failed) before the spawn.
FENCE_LOST = "fence_lost"


def _fenced_launch(
    launch: LaunchBoundary,
    spawn_key: str,
    *,
    run_id: str,
    actor: str,
    task_id: int,
    agent_label: str,
    delegation_id: int,
) -> dict[str, Any] | str:
    """Spawn a claimed run's job behind its spawn fence; the boundary's result.

    The one sequence every spawn of a dispatch run takes (review #1521 passes
    4-6, #2512): fence the spawn under ``spawn_key`` (``_fence_spawn``: the run
    still claimed by ``actor`` under a live lease, the mark armed and unset),
    re-check that the fence still holds (an operator settlement in between
    leaves nothing to spawn), call the boundary, record its outcome
    (``_settle_spawn``) and only then leave the in-flight registry.

    Returns ``FENCE_REFUSED`` or ``FENCE_LOST`` when nothing was spawned. An
    exception from the boundary propagates with the outcome unrecorded, so the
    run and its task stay held until ``settle_unsettled_spawn`` proves the
    launcher and the job gone. A re-check that raises spawns nothing either.
    """
    if not _fence_spawn(spawn_key, run_id, actor):
        return FENCE_REFUSED
    try:
        holds = _fence_holds(spawn_key, run_id)
    except BaseException:
        _unregister_spawn(run_id)
        raise
    if not holds:
        _unregister_spawn(run_id)
        return FENCE_LOST
    try:
        result = launch.launch(
            run_id=run_id,
            task_id=task_id,
            agent_label=agent_label,
            delegation_id=delegation_id,
        )
    except BaseException:
        # The boundary returned (raising): this launcher is no longer spawning, and
        # the outcome stays unrecorded for the operator's settlement to prove.
        _unregister_spawn(run_id)
        raise
    # A boundary that cancelled its own late spawn still started a job.
    spawned = result.get("ok", True) or result.get("error") == "cancelled_during_launch"
    _settle_spawn(spawn_key, run_id, "spawned" if spawned else "failed")
    # Registered until the outcome is durable; a failed write keeps it held.
    _unregister_spawn(run_id)
    return result


def _live_run_for_task(conn, task_id: int) -> dict | None:
    """The run holding this task's live slot, or None.

    Task #1221: unified live-slot predicate shared with idx_dispatch_runs_live_task
    in storage.py. Any run with unconfirmed cessation (claimed/launched/running,
    cancelled with unconfirmed outcome, or blocked via runtime ceiling timeout)
    retains its live-slot reservation until cessation is verified.
    """
    row = conn.execute(
        f"SELECT * FROM dispatch_runs WHERE task_id=? AND ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) "
        "ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _row(row) if row else None


def _approval_pass_still_valid(conn, *, task_id: int, delegation_id: int) -> bool:
    """True when an approved/consumed pass for this dispatch payload is still within TTL."""
    from ..tools.approvals import _pass_expires_at

    row = dispatch_approval_row(conn, task_id=task_id, delegation_id=delegation_id)
    if row is None or row["status"] not in {"approved", "consumed"}:
        return False
    if row["status"] == "consumed" and dispatch_recovery_run(conn, row) is None:
        return False
    expires_at = _pass_expires_at(row)
    return expires_at is not None and datetime.now(UTC) < expires_at


def _task_executor_lock_active(task: dict[str, Any], now: datetime) -> bool:
    """True when the Active Executor holds a live task lock (no dispatch_runs row)."""
    claimed_by = task.get("claimed_by")
    if not claimed_by:
        return False
    lock_exp = _parse_ts(task.get("lock_expires_at"))
    if lock_exp is None:
        return True
    return now < lock_exp


def _classify_approval(
    notes: str,
    conn,
    *,
    task_id: int,
    delegation_id: int,
    delegation_task_spec: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). reason is 'ok' when approved."""
    blocker = dispatch_plan_blocker(notes, delegation_task_spec)
    if blocker:
        return False, blocker
    notes = _operator_notes_for_approval(notes, delegation_task_spec)
    approved_at = _last_marker_pos(APPROVED_MARKER_RE, notes)
    approval_status = _approval_row_status(conn, task_id=task_id, delegation_id=delegation_id)
    if approval_status is not None:
        # Explicit authority is authoritative even when legacy notes say approved.
        if approval_status == "invalid":
            return False, "invalid_approval_authority"
        if approval_status == "denied":
            return False, "denied"
        if approval_status in {"expired", "revoked"}:
            return False, "expired"
        if approval_status == "pending":
            return False, "unapproved"
        if approval_status in {"approved", "consumed"}:
            if _approval_pass_still_valid(conn, task_id=task_id, delegation_id=delegation_id):
                return True, "ok"
            return False, "expired" if approval_status == "approved" else "unapproved"
        return False, "invalid_approval_authority"

    if approved_at >= 0:
        approved_match = None
        for match in APPROVED_MARKER_RE.finditer(notes):
            if match.start() == approved_at:
                approved_match = match
                break
        if approved_match is not None and _parse_ts(approved_match.group(1).strip()):
            return True, "ok"
        return False, "malformed_approval"
    if MALFORMED_APPROVED_RE.search(notes):
        return False, "malformed_approval"

    return False, "missing_approval_marker"


def evaluate_ready(
    *,
    task_id: int | None = None,
    delegation_id: int | None = None,
    expected_project: str | None = None,
    expected_lane: str | None = None,
) -> dict[str, Any]:
    """Authoritative ready predicate from the approved-dispatch contract."""
    if task_id is None and delegation_id is None:
        return {
            "ready": False,
            "reason": "malformed",
            "task": None,
            "delegation": None,
            "agent_label": None,
        }

    conn = storage.connect()
    try:
        delegation = _load_delegation(conn, delegation_id=delegation_id, task_id=task_id)
        if delegation is None:
            return {
                "ready": False,
                "reason": "malformed",
                "task": None,
                "delegation": None,
                "agent_label": None,
            }
        task = _load_task(conn, delegation["task_id"])
        if task is None:
            return {
                "ready": False,
                "reason": "malformed",
                "task": None,
                "delegation": delegation,
                "agent_label": delegation.get("to_agent"),
            }

        if expected_project is not None and task.get("project") != expected_project:
            return {
                "ready": False,
                "reason": "project_mismatch",
                "task": task,
                "delegation": delegation,
                "agent_label": (delegation.get("to_agent") or "").strip(),
            }
        if expected_lane is not None and config.canonical_task_lane(
            task.get("lane") or "default"
        ) not in {
            *config.TASK_OPEN_POOL_LANES,
            config.canonical_task_lane(expected_lane),
        }:
            return {
                "ready": False,
                "reason": "lane_mismatch",
                "task": task,
                "delegation": delegation,
                "agent_label": (delegation.get("to_agent") or "").strip(),
            }

        if _has_cancelled_ancestor(conn, task):
            return {
                "ready": False,
                "reason": "cancelled",
                "task": task,
                "delegation": delegation,
                "agent_label": (delegation.get("to_agent") or "").strip(),
            }
        # Subtree hold (task #1747): the same check task_claim makes.
        held = task_holds.claim_block(conn, int(task["id"]))
        if held:
            return {
                "ready": False,
                "reason": "held",
                "detail": held,
                "task": task,
                "delegation": delegation,
                "agent_label": (delegation.get("to_agent") or "").strip(),
            }

        agent_label = (delegation.get("to_agent") or "").strip()
        if not config.is_known_agent_label(agent_label):
            return {
                "ready": False,
                "reason": "unknown_agent",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }
        if agent_label not in DISPATCHABLE_LABELS:
            return {
                "ready": False,
                "reason": "not_dispatchable",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        if task.get("status") in TERMINAL_TASK:
            reason = "cancelled" if task.get("status") == "cancelled" else "task_terminal"
            return {
                "ready": False,
                "reason": reason,
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        live = _live_run_for_task(conn, task["id"])
        if live is not None:
            if live.get("state") in {"cancelled", "blocked"}:
                # Task #1221 finding 1/3: an unconfirmed cancellation or unconfirmed
                # ceiling-timeout kill is not a stale lease safe to reclaim — the process
                # it tried to stop may still be running. Never let a lease-expiry read free
                # this slot; only a confirmed/reconciled termination_outcome does (see
                # finalize_cancellation / retry_cancellation / confirm_cancellation).
                return {
                    "ready": False,
                    "reason": "cessation_unconfirmed",
                    "task": task,
                    "delegation": delegation,
                    "agent_label": agent_label,
                    "live_run": live,
                }
            lease_exp = _parse_ts(live.get("lease_expires_at"))
            if lease_exp is not None and datetime.now(UTC) >= lease_exp:
                # Stale live lease — not ready until reclaim; surface as reclaimable.
                return {
                    "ready": False,
                    "reason": "lease_expired",
                    "task": task,
                    "delegation": delegation,
                    "agent_label": agent_label,
                    "live_run": live,
                }
            return {
                "ready": False,
                "reason": "already_claimed",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
                "live_run": live,
            }

        d_status = delegation.get("status")
        if d_status == "cancelled":
            return {
                "ready": False,
                "reason": "cancelled",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }
        if d_status in {"completed", "failed"}:
            return {
                "ready": False,
                "reason": "task_terminal",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        if d_status == "in_flight":
            # Orphan in_flight without a live run (crash before run insert, or
            # after reclaim of the run) — reclaimable, not ready until released.
            return {
                "ready": False,
                "reason": "lease_expired",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        if d_status != "pending":
            return {
                "ready": False,
                "reason": "malformed",
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        ok, reason = _classify_approval(
            task.get("notes") or "",
            conn,
            task_id=task["id"],
            delegation_id=delegation["id"],
            delegation_task_spec=delegation.get("task_spec"),
        )
        if not ok:
            return {
                "ready": False,
                "reason": reason,
                "task": task,
                "delegation": delegation,
                "agent_label": agent_label,
            }

        return {
            "ready": True,
            "reason": "ok",
            "task": task,
            "delegation": delegation,
            "agent_label": agent_label,
        }
    finally:
        conn.close()


def get_run(run_id: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (run_id,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def _operator_authorized(actor: str) -> bool:
    """Require a verified identity and the authenticated operator profile."""
    identity = config.verified_agent_identity()
    return bool(identity and identity == actor and profiles.current() == profiles.OPERATOR)


TERMINAL_STATES = frozenset({"finished", "failed", "denied", "expired", "blocked"})


def transition_run(
    conn: sqlite3.Connection,
    run_id: str,
    target_state: str | None = None,
    *,
    expected_states: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
    failure_reason: str | None = None,
    failure_class: str | None = None,
    evidence_ref: str | None = None,
    lease_owner: str | None = None,
    expected_lease_owner: str | None = None,
    lease_expires_at: str | None = None,
    clear_lease: bool = False,
    termination_outcome: str | None = None,
    clear_termination_outcome: bool = False,
    clear_failure_reason: bool = False,
    clear_evidence_ref: bool = False,
    require_cancellation_unlocked: bool = True,
    allow_downgrade_confirmed_cessation: bool = False,
    guard_sql: str | None = None,
    guard_params: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """Centralized transition validator and writer for dispatch runs.

    ``guard_sql`` (task #1950) is an extra boolean SQL condition on the
    ``dispatch_runs`` row, ANDed into the compare-and-swap UPDATE itself, so a
    caller's precondition (e.g. "the run is still bound to this node item")
    holds at the instant of the write rather than at an earlier read. A row
    that fails it is left untouched and reported as ``guard_refused``.

    Task #1221: routes every dispatch_runs state mutation through a single
    governed path enforcing:
    1. Cancellation lock: once termination_outcome is non-NULL (requested,
       unknown, confirmed), state mutation is disallowed for ordinary writers
       (record_run_outcome, reclaim_expired_leases, lease renewal).
    2. Transition validity: only allowed transitions between states.
    3. Terminal immutability: terminal rows cannot be mutated except by resume_run.
    4. Atomic compare-and-swap update against SQLite.
    """
    row = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "unknown_run", "run_id": run_id}
    current = _row(row)
    cur_state = current["state"]
    cur_outcome = current.get("termination_outcome")

    # 1. Cancellation lock check
    if require_cancellation_unlocked and cur_outcome is not None:
        return {
            "ok": False,
            "reason": (
                "cancellation_in_progress" if cur_state != "cancelled" else "already_terminal"
            ),
            "run_id": run_id,
            "state": cur_state,
            "termination_outcome": cur_outcome,
        }

    # 2. Terminal state check: if currently in a terminal state, report already_terminal
    # unless expected_states explicitly includes this terminal state (e.g. resume_run).
    if cur_state in TERMINAL_STATES and (
        expected_states is None or cur_state not in expected_states
    ):
        return {
            "ok": False,
            "reason": "already_terminal",
            "run_id": run_id,
            "state": cur_state,
        }

    # 3. Expected states check
    if expected_states is not None and cur_state not in expected_states:
        return {
            "ok": False,
            "reason": "invalid_transition",
            "run_id": run_id,
            "state": cur_state,
            "target_state": target_state,
            "expected_states": list(expected_states),
        }

    # 4. Expected lease owner check
    if expected_lease_owner is not None and current.get("lease_owner") != expected_lease_owner:
        return {
            "ok": False,
            "reason": "lease_lost",
            "run_id": run_id,
            "state": cur_state,
        }

    # 5. Cancellation confirmation check:
    # If currently cancelled and already confirmed, ignore redundant finalization
    # unless allow_downgrade_confirmed_cessation is explicitly set (late spawn race).
    if (
        cur_state == "cancelled"
        and cur_outcome == "confirmed"
        and target_state == "cancelled"
        and not allow_downgrade_confirmed_cessation
    ):
        return {
            "ok": True,
            "run_id": run_id,
            "state": cur_state,
            "termination_outcome": cur_outcome,
            "already_finalized": True,
        }

    # 6. Build atomic update
    set_clauses = ["updated_at=?"]
    params: list[Any] = [storage.now()]

    new_state = target_state if target_state is not None else cur_state
    if target_state is not None:
        set_clauses.append("state=?")
        params.append(target_state)

    if failure_reason is not None:
        set_clauses.append("failure_reason=?")
        params.append(failure_reason[:200])
    elif clear_failure_reason:
        set_clauses.append("failure_reason=NULL")

    if failure_class is not None:
        set_clauses.append("failure_class=?")
        params.append(failure_class)

    if evidence_ref is not None:
        set_clauses.append("evidence_ref=?")
        params.append(evidence_ref)
    elif clear_evidence_ref:
        set_clauses.append("evidence_ref=NULL")

    if lease_owner is not None:
        set_clauses.append("lease_owner=?")
        params.append(lease_owner)

    if clear_lease:
        set_clauses.append("lease_expires_at=NULL")
    elif lease_expires_at is not None:
        set_clauses.append("lease_expires_at=?")
        params.append(lease_expires_at)

    if clear_termination_outcome:
        set_clauses.append("termination_outcome=NULL")
    elif termination_outcome is not None:
        set_clauses.append("termination_outcome=?")
        params.append(termination_outcome)

    where_clauses = ["run_id=?", "state=?"]
    where_params = [run_id, cur_state]

    if expected_lease_owner is not None:
        where_clauses.append("lease_owner=?")
        where_params.append(expected_lease_owner)

    if require_cancellation_unlocked:
        where_clauses.append("termination_outcome IS NULL")
    else:
        # For cancellation transitions: matches legacy NULL, requested, or unknown (or confirmed if allow_downgrade)
        if cur_outcome is None:
            where_clauses.append("termination_outcome IS NULL")
        else:
            where_clauses.append("termination_outcome=?")
            where_params.append(cur_outcome)

    if guard_sql is not None:
        where_clauses.append(f"({guard_sql})")
        where_params.extend(guard_params)

    sql = f"UPDATE dispatch_runs SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
    all_params = params + where_params

    cur = conn.execute(sql, all_params)
    if cur.rowcount == 0:
        current_now = conn.execute(
            "SELECT * FROM dispatch_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        row_now = _row(current_now) if current_now else None
        guard_failed = guard_sql is not None and (
            row_now is not None
            and row_now.get("state") == cur_state
            and conn.execute(
                f"SELECT 1 FROM dispatch_runs WHERE run_id=? AND ({guard_sql})",
                (run_id, *guard_params),
            ).fetchone()
            is None
        )
        return {
            "ok": False,
            "reason": "guard_refused" if guard_failed else "state_race",
            "run_id": run_id,
            "state": row_now.get("state") if row_now else None,
            "termination_outcome": row_now.get("termination_outcome") if row_now else None,
        }

    return {
        "ok": True,
        "run_id": run_id,
        "state": new_state,
        "termination_outcome": (
            None if clear_termination_outcome else (termination_outcome or cur_outcome)
        ),
        "previous_state": cur_state,
    }


def finalize_cancellation(
    run: dict[str, Any],
    *,
    actor: str,
    reason: str,
    termination: str | None,
    cessation: str,
    allow_downgrade_confirmed_cessation: bool = False,
    guard_sql: str | None = None,
    guard_params: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """Commit a cancellation's outcome, guarded on the pending-intent marker.

    Shared by ``cancel_run``, the launch-race path in ``launch.py`` (finding
    2), and the operator reconcile path (``retry_cancellation`` /
    ``confirm_cancellation``, finding 1/3). ``guard_sql``/``guard_params`` are
    passed to ``transition_run``'s compare-and-swap (task #1950); a refused
    guard writes nothing.
    """
    rid = run["run_id"]
    conn = storage.connect()
    try:
        ts = storage.now()
        downgrade = allow_downgrade_confirmed_cessation or actor == "dispatch-launch"
        res = transition_run(
            conn,
            rid,
            target_state="cancelled",
            expected_states=("claimed", "launched", "running", "cancelled", "blocked"),
            failure_reason=reason[:200],
            termination_outcome=cessation,
            clear_lease=(cessation == "confirmed"),
            require_cancellation_unlocked=False,
            allow_downgrade_confirmed_cessation=downgrade,
            guard_sql=guard_sql,
            guard_params=guard_params,
        )
        if not res["ok"] and res.get("reason") == "guard_refused":
            conn.commit()
            return {
                "ok": False,
                "reason": "guard_refused",
                "run_id": rid,
                "state": res.get("state"),
            }
        if not res["ok"]:
            current = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
            conn.commit()
            cur_row = _row(current) if current else None
            if cur_row is None:
                return {"ok": False, "reason": "unknown_run", "run_id": rid}
            if cur_row.get("state") != "cancelled":
                return {
                    "ok": False,
                    "reason": "already_terminal",
                    "run_id": rid,
                    "state": cur_row.get("state"),
                }
            return {
                "ok": True,
                "run_id": rid,
                "state": cur_row["state"],
                "termination": termination,
                "cessation": cur_row.get("termination_outcome"),
                "already_finalized": True,
            }
        if res.get("already_finalized"):
            conn.commit()
            return {
                "ok": True,
                "run_id": rid,
                "state": res["state"],
                "termination": termination,
                "cessation": res.get("termination_outcome"),
                "already_finalized": True,
            }
        delegation_id = run.get("delegation_id")
        if delegation_id is not None:
            conn.execute(
                "UPDATE delegations SET status='cancelled', completed_at=?, updated_at=? "
                "WHERE id=? AND status IN ('pending','in_flight')",
                (ts, ts, delegation_id),
            )
        conn.commit()
    finally:
        conn.close()

    _record_transition(
        actor=actor,
        task_id=run.get("task_id"),
        delegation_id=run.get("delegation_id"),
        agent_label=run.get("agent_label"),
        run_id=rid,
        state="cancelled",
        failure_reason=reason[:200],
        evidence_ref=run.get("evidence_ref"),
        termination_outcome=cessation,
        # Keep repeated writes of the same outcome idempotent, but retain an
        # unknown -> confirmed reconciliation as a distinct audit event.
        dedupe_key=f"dispatch-cancel:{cessation}:{rid}",
    )
    # `termination` is the raw supervise.py outcome (e.g. "terminated",
    # "refused", "identity_mismatch"); `cessation` is the reviewed claim about
    # it ("confirmed" or "unknown"). A caller must not read `ok: True` as proof
    # the process stopped — check `cessation`.
    return {
        "ok": True,
        "run_id": rid,
        "state": "cancelled",
        "termination": termination,
        "cessation": cessation,
    }


def cancel_run(run_id: str, *, actor: str, reason: str = "cancelled") -> dict[str, Any]:
    """Cancel one live dispatch run, terminating only its identity-checked process.

    Task #1221: "we recorded a cancellation", "we attempted a stop", and "the
    work stopped" are three different claims, each durable on its own:

    1. Cancellation *intent* is persisted (``termination_outcome='requested'``)
       atomically, before termination is attempted. The run's ``state`` is
       left untouched (still ``claimed``/``launched``/``running``) at this
       point — it already occupies the task's live slot via the ordinary
       live-run checks, so nothing extra is needed to reserve it. A crash
       between this write and the termination attempt below leaves a
       reconcilable row (finding 3): ``reconcile_pending_cancellations`` (run
       from ``reclaim_expired_leases``) or an operator's
       ``retry_cancellation``/``confirm_cancellation`` picks it back up —
       reclaim's ordinary lease-expiry path never silently promotes it to
       ``retryable``.
    2. The intent write doubles as a compare-and-swap against a launch that
       has not yet persisted its spawned pid (finding 2):
       ``launch._persist_spawn_pid`` only writes ``evidence_ref`` while
       ``termination_outcome IS NULL``. Whichever of the two guarded writes
       commits first "wins"; the loser is responsible for actually stopping
       the real process — either this function terminates the freshly
       recorded evidence, or the launch boundary discovers its write was
       refused and kills the child it just spawned itself. Either way no
       process is left unaccounted for.
    3. Termination is attempted, and its outcome classified, before the row is
       committed ``cancelled``. See ``supervise.classify_termination_outcome``
       for the confirmed/unknown split and ``resume_run`` /
       ``_live_run_for_task`` for the guards a non-confirmed outcome keeps
       enabled (finding 1: an unconfirmed cancellation still occupies the
       task's live slot, via both the partial unique index and
       ``_live_run_for_task``).

    Duplicate and late cancellation are safe no-ops; terminal success/failure
    is preserved.
    """
    rid = (run_id or "").strip()
    if not rid:
        return {"ok": False, "reason": "missing_run_id", "run_id": rid}
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "unknown_run", "run_id": rid}
        run = _row(row)
        if run["state"] not in LIVE_STATES:
            return {"ok": False, "reason": "already_terminal", "run_id": rid, "state": run["state"]}
        if run.get("termination_outcome") is not None:
            # Another cancel (or the launch-race path) already recorded
            # intent for this run — never issue a second concurrent signal.
            return {
                "ok": False,
                "reason": "cancellation_in_progress",
                "run_id": rid,
                "state": run["state"],
                "termination_outcome": run.get("termination_outcome"),
            }

        res = transition_run(
            conn,
            rid,
            target_state=None,
            termination_outcome="requested",
            expected_states=LIVE_STATES,
            require_cancellation_unlocked=True,
        )
        if not res["ok"]:
            conn.commit()
            current = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
            cur_row = _row(current) if current else None
            if cur_row is None:
                return {"ok": False, "reason": "unknown_run", "run_id": rid}
            if cur_row.get("termination_outcome") == "requested":
                return {
                    "ok": False,
                    "reason": "cancellation_in_progress",
                    "run_id": rid,
                    "state": cur_row["state"],
                    "termination_outcome": "requested",
                }
            return {
                "ok": False,
                "reason": "already_terminal",
                "run_id": rid,
                "state": cur_row["state"],
            }
        conn.commit()
    finally:
        conn.close()

    _record_transition(
        actor=actor,
        task_id=run.get("task_id"),
        delegation_id=run.get("delegation_id"),
        agent_label=run.get("agent_label"),
        run_id=rid,
        state="cancel_requested",
        failure_reason=reason[:200],
        evidence_ref=run.get("evidence_ref"),
        termination_outcome="requested",
        dedupe_key=f"dispatch-cancel-requested:{rid}",
    )

    # Reread for the freshest evidence_ref: a spawn may have persisted a real
    # pid between the row read above and the intent commit — see finding 2 in
    # the docstring. `_persist_spawn_pid`'s own guard means that write can only
    # have landed if it beat the UPDATE above, so it is safe to trust here.
    conn = storage.connect()
    try:
        fresh = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    if fresh is not None:
        run = _row(fresh)

    try:
        termination: str | None = supervise.terminate_ours(run.get("evidence_ref"))
    except Exception as exc:  # defensive: an unexpected raise must not be silent success
        logger.warning("terminate_ours raised during cancel_run(%s): %s", rid, exc)
        termination = None
    cessation = supervise.classify_termination_outcome(termination)

    return finalize_cancellation(
        run, actor=actor, reason=reason, termination=termination, cessation=cessation
    )


def reconcile_pending_cancellations(*, actor: str = ACTOR_COORDINATOR) -> dict[str, Any]:
    """Finish cancellations whose intent was recorded but never finalized.

    Task #1221 finding 3: a coordinator crash between persisting
    ``termination_outcome='requested'`` and finishing termination must not
    leave the run stuck, nor let the ordinary lease-expiry path in
    ``reclaim_expired_leases`` silently promote it to ``retryable`` — that
    would drop the unconfirmed-cessation guard ``resume_run`` and
    ``_live_run_for_task`` rely on. This re-attempts termination against the
    row's current evidence and finalizes it, exactly as ``cancel_run`` would
    have. Safe to call repeatedly; a row already finalized is left alone by
    ``finalize_cancellation``'s own guard.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE termination_outcome='requested'"
        ).fetchall()
        pending = [_row(r) for r in rows]
    finally:
        conn.close()

    resolved: list[dict[str, Any]] = []
    for run in pending:
        rid = run["run_id"]
        try:
            termination = supervise.terminate_ours(run.get("evidence_ref"))
        except Exception as exc:
            logger.warning("terminate_ours raised during reconcile(%s): %s", rid, exc)
            termination = None
        cessation = supervise.classify_termination_outcome(termination)
        result = finalize_cancellation(
            run,
            actor=actor,
            reason="reconciled_cancellation",
            termination=termination,
            cessation=cessation,
        )
        resolved.append(
            {
                "run_id": rid,
                "termination": termination,
                "cessation": cessation,
                "result": result,
            }
        )
    return {"resolved_count": len(resolved), "resolved": resolved}


def retry_cancellation(run_id: str, *, actor: str) -> dict[str, Any]:
    """Operator path: re-attempt termination for a stuck unconfirmed cancellation.

    Task #1221 finding 1/3 require an operator path to clear a run that keeps
    holding its task's live slot because cessation was never confirmed — this
    is that path for the case where retrying the real termination might now
    succeed (the process finally exited, a transient permission issue
    cleared, etc). Safe to call repeatedly.
    """
    rid = (run_id or "").strip()
    if not _operator_authorized(actor):
        return {"ok": False, "reason": "operator_authorization_required", "run_id": rid}
    run = get_run(rid)
    if run is None:
        return {"ok": False, "reason": "unknown_run", "run_id": rid}
    if run["state"] not in {"cancelled", "blocked"} or run.get("termination_outcome") not in {
        "requested",
        "unknown",
        None,
    }:
        return {
            "ok": False,
            "reason": "not_reconcilable",
            "run_id": rid,
            "state": run["state"],
            "termination_outcome": run.get("termination_outcome"),
        }
    try:
        termination = supervise.terminate_ours(run.get("evidence_ref"))
    except Exception as exc:
        logger.warning("terminate_ours raised during retry_cancellation(%s): %s", rid, exc)
        termination = None
    cessation = supervise.classify_termination_outcome(termination)
    return finalize_cancellation(
        run, actor=actor, reason="retry_cancellation", termination=termination, cessation=cessation
    )


def confirm_cancellation(
    run_id: str, *, actor: str, note: str = "operator_confirmed"
) -> dict[str, Any]:
    """Operator override: assert a stuck cancellation's target is verified gone.

    Task #1221 finding 1/3: an unconfirmed cancellation must not be a state
    nobody can clear. When the target is verified gone by means outside
    ``terminate_ours``'s reach (the host it ran on was rebuilt, an operator
    confirmed it dead by hand), this releases the slot the same way a
    genuinely confirmed cancellation would. Never called automatically —
    unlike ``retry_cancellation``, this does not itself attempt termination.
    """
    rid = (run_id or "").strip()
    if not _operator_authorized(actor):
        return {"ok": False, "reason": "operator_authorization_required", "run_id": rid}
    run = get_run(rid)
    if run is None:
        return {"ok": False, "reason": "unknown_run", "run_id": rid}
    if (
        run["state"] not in {"cancelled", "blocked"}
        or run.get("termination_outcome") == "confirmed"
    ):
        return {
            "ok": False,
            "reason": "not_reconcilable",
            "run_id": rid,
            "state": run["state"],
            "termination_outcome": run.get("termination_outcome"),
        }
    return finalize_cancellation(
        run,
        actor=actor,
        reason=f"operator_override:{note}"[:200],
        termination="operator_confirmed",
        cessation="confirmed",
    )


def resume_run(
    run_id: str,
    *,
    actor: str,
    launch: LaunchBoundary | None = None,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> dict[str, Any]:
    """Resume a previously cancelled or retryable dispatch run under governance.

    Fail-closed: returns ok=False if:
    - the run does not exist
    - the run is already terminal in an un-resumable state (finished, denied, expired)
    - the run is currently active (claimed, launched, running)
    - the underlying task is finished/cancelled or has a cancelled ancestor
    - the underlying task is held: a subtree hold, or interrupted work (#2482)
    - the underlying task lacks valid approval
    - another run is already active for the same task
    - a run of the task has a spawn without a recorded outcome (#2512)

    The relaunch is fenced like a claim's (#2527): the resume arms its own spawn
    mark, ``run:<run_id>:resume``, in the transaction that claims the run, then
    spawns through ``_fenced_launch``. The claim's mark is left as it was.
    """
    rid = (run_id or "").strip()
    if not rid:
        return {"ok": False, "reason": "missing_run_id", "run_id": rid}
    launch = launch or scheduler_launch_boundary()
    # A subtree hold released on its own policy inside this transaction is
    # published once the transaction commits (task #1747).
    hold_events: list[dict[str, Any] | None] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "unknown_run", "run_id": rid}
        run = _row(row)
        current_state = run["state"]
        if current_state in {"finished", "denied", "expired"}:
            return {
                "ok": False,
                "reason": f"terminal_{current_state}",
                "run_id": rid,
                "state": current_state,
            }
        if current_state in {"claimed", "launched", "running"}:
            return {"ok": False, "reason": "already_active", "run_id": rid, "state": current_state}
        if current_state not in {"cancelled", "failed", "retryable"}:
            return {
                "ok": False,
                "reason": f"unresumable_state_{current_state}",
                "run_id": rid,
                "state": current_state,
            }
        # Task #1221: a cancelled run may not have a confirmed-stopped process
        # behind it — termination could have been refused, hit
        # identity_mismatch, or never completed before a crash. Resuming in
        # that case would overlap a second run onto a process nothing has
        # verified is gone. `failed`/`retryable` rows never carry this
        # ambiguity: they are only reached via a self-report from the process
        # itself, or via reclaim's own confirmed-termination branch — so the
        # gate applies only to `cancelled`. Unset (legacy rows predating this
        # column) fails closed as unknown, not confirmed.
        if current_state == "cancelled" and run.get("termination_outcome") != "confirmed":
            return {
                "ok": False,
                "reason": "cessation_unconfirmed",
                "run_id": rid,
                "state": current_state,
                "termination_outcome": run.get("termination_outcome"),
            }
        # Task #1327 review: a node-routed run failed by lease expiry never had
        # its outcome reconciled — a lost node heartbeat does not prove the
        # backend stopped, so a resumed second run could overlap the first.
        if current_state == "failed" and run.get("failure_reason") == (
            "lease_expired_outcome_unknown"
        ):
            return {
                "ok": False,
                "reason": "outcome_unknown",
                "run_id": rid,
                "state": current_state,
            }

        task = _load_task(conn, run["task_id"])
        if task is None:
            return {"ok": False, "reason": "missing_task", "run_id": rid}
        if task.get("status") in TERMINAL_TASK:
            return {
                "ok": False,
                "reason": "task_terminal",
                "run_id": rid,
                "state": task.get("status"),
            }
        if _has_cancelled_ancestor(conn, task):
            return {"ok": False, "reason": "cancelled_ancestor", "run_id": rid}
        # The check claim_and_dispatch makes (tasks #1747, #2482): a resume
        # relaunches work, so a held task refuses it too.
        held = task_holds.claim_block(conn, int(task["id"]), pending=hold_events)
        if held:
            return {"ok": False, "reason": "held", "run_id": rid, "detail": held}

        delegation = _load_delegation(
            conn, delegation_id=run.get("delegation_id"), task_id=task["id"]
        )
        if delegation is None:
            return {"ok": False, "reason": "missing_delegation", "run_id": rid}
        if delegation.get("status") == "completed":
            return {"ok": False, "reason": "delegation_completed", "run_id": rid}

        agent_label = run.get("agent_label") or delegation.get("to_agent")
        if not config.is_known_agent_label(agent_label):
            return {
                "ok": False,
                "reason": "unknown_agent",
                "run_id": rid,
                "agent_label": agent_label,
            }
        if agent_label not in DISPATCHABLE_LABELS:
            return {
                "ok": False,
                "reason": "not_dispatchable",
                "run_id": rid,
                "agent_label": agent_label,
            }

        live = _live_run_for_task(conn, task["id"])
        if live is not None and live["run_id"] != rid:
            return {
                "ok": False,
                "reason": "already_claimed",
                "run_id": rid,
                "live_run_id": live["run_id"],
            }
        # A run of this task whose spawn may still be in flight, this one
        # included, holds it against a resume as against a claim (#2512).
        unsettled = _task_spawn_refusal(conn, int(task["id"]))
        if unsettled is not None:
            return {
                "ok": False,
                "reason": "launch_unsettled",
                "run_id": rid,
                "unsettled_run_id": unsettled["run_id"],
            }

        ok, reason = _classify_approval(
            task.get("notes") or "",
            conn,
            task_id=task["id"],
            delegation_id=delegation["id"],
            delegation_task_spec=delegation.get("task_spec"),
        )
        if not ok:
            return {"ok": False, "reason": reason, "run_id": rid}

        trust_snapshot = trust_presets.snapshot(agent_label)
        originator_snapshot = trust_presets.snapshot(delegation.get("from_agent"))
        ts = storage.now()
        lease_expires = (datetime.now(UTC) + timedelta(seconds=lease_ttl_s)).isoformat()

        _consume_approval_pass(conn, task_id=task["id"], delegation_id=delegation["id"], run_id=rid)
        res = transition_run(
            conn,
            rid,
            target_state="claimed",
            expected_states=("cancelled", "failed", "retryable"),
            clear_failure_reason=True,
            clear_evidence_ref=True,
            clear_termination_outcome=True,
            lease_owner=actor,
            lease_expires_at=lease_expires,
            require_cancellation_unlocked=False,
        )
        if not res["ok"]:
            return {
                "ok": False,
                "reason": "state_race",
                "run_id": rid,
                "state": res.get("state"),
            }
        # The resume fences its own spawn as a claim does (#2527), under its own
        # mark, armed here in the transaction that decided the refusal above.
        spawn_key = _arm_resume_spawn(conn, rid, int(task["id"]), ts)
        if spawn_key is None:
            return {"ok": False, "reason": "launch_unsettled", "run_id": rid}

        conn.execute(
            "UPDATE delegations SET status='in_flight', attempts=attempts+1, "
            "claim_generation=claim_generation+1, "
            "last_attempt_at=?, updated_at=? WHERE id=? AND status != 'completed'",
            (ts, ts, delegation["id"]),
        )
        conn.commit()
        task_holds.publish(hold_events)
    finally:
        conn.close()

    _record_transition(
        actor=actor,
        task_id=task["id"],
        delegation_id=delegation["id"],
        agent_label=agent_label,
        run_id=rid,
        state="resumed",
        trust_snapshot=trust_snapshot,
        originator_snapshot=originator_snapshot,
        dedupe_key=f"dispatch-resumed:{rid}:{ts}",
    )

    # The same fenced sequence as claim_and_dispatch: a resumed launcher that
    # lost its claim spawns nothing, and a resumed spawn without a recorded
    # outcome holds the run claimed and its task against any other dispatch.
    launched = _fenced_launch(
        launch,
        spawn_key,
        run_id=rid,
        actor=actor,
        task_id=task["id"],
        agent_label=agent_label,
        delegation_id=delegation["id"],
    )
    if isinstance(launched, str):
        return {"ok": False, "reason": "lease_lost", "run_id": rid}
    result = launched
    if not result.get("ok", True):
        if result.get("error") == "cancelled_during_launch":
            # Task #1221 finding 2: the launch boundary itself found and
            # terminated a late spawn racing a concurrent cancel_run, and
            # already finalized the row via finalize_cancellation — do not
            # also mark it retryable here, that would resurrect the slot the
            # finalize call just closed.
            return {
                "ok": False,
                "reason": "cancelled_during_launch",
                "run_id": rid,
                "state": "cancelled",
                "termination": result.get("termination"),
                "cessation": result.get("cessation"),
            }
        failure = str(result.get("error") or "resume_launch_failed")[:500]
        conn = storage.connect()
        try:
            res = transition_run(
                conn,
                rid,
                target_state="retryable",
                expected_states=("claimed",),
                failure_reason=failure,
                require_cancellation_unlocked=True,
            )
            if res["ok"]:
                conn.execute(
                    "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                    (storage.now(), delegation["id"]),
                )
            conn.commit()
        finally:
            conn.close()
        _record_transition(
            actor=actor,
            task_id=task["id"],
            delegation_id=delegation["id"],
            agent_label=agent_label,
            run_id=rid,
            state="retryable",
            failure_reason=failure,
        )
        return {
            "ok": False,
            "reason": "launch_failed",
            "error": failure,
            "run_id": rid,
            "state": "retryable",
        }

    conn = storage.connect()
    try:
        res = transition_run(
            conn,
            rid,
            target_state="launched",
            expected_states=("claimed",),
            expected_lease_owner=actor,
            require_cancellation_unlocked=True,
        )
        if not res["ok"]:
            current = conn.execute("SELECT * FROM dispatch_runs WHERE run_id=?", (rid,)).fetchone()
            conn.commit()
            current_run = _row(current) if current else None
            if current_run and current_run.get("termination_outcome") is not None:
                result = {
                    "ok": False,
                    "reason": (
                        "cancelled_during_launch"
                        if current_run.get("state") == "cancelled"
                        else "cancellation_in_progress"
                    ),
                    "run_id": rid,
                    "state": current_run.get("state"),
                    "termination_outcome": current_run.get("termination_outcome"),
                }
                if current_run.get("termination_outcome") in {"confirmed", "unknown"}:
                    result["cessation"] = current_run["termination_outcome"]
                return result
            return {"ok": False, "reason": "lease_lost", "run_id": rid}
        conn.commit()
    finally:
        conn.close()

    _record_transition(
        actor=actor,
        task_id=task["id"],
        delegation_id=delegation["id"],
        agent_label=agent_label,
        run_id=rid,
        state="launched",
        dedupe_key=f"dispatch-resumed-launched:{rid}:{ts}",
    )
    spawn = result.get("spawn") or {}
    return {
        "ok": True,
        "run_id": rid,
        "state": "launched",
        "spawned": bool(result.get("spawned")),
        "pid": result.get("pid") if result.get("pid") is not None else spawn.get("pid"),
        "stub": bool(result.get("stub", True)),
    }


def get_run_observability(run_id: str) -> dict[str, Any] | None:
    """Operator view: task/agent/run/state/error/retry/evidence in one place.

    Prefer this over scraping multiple stores. Fields also appear on
    ``agent.dispatch.transition`` events and ``[dispatch: …]`` task notes.
    """
    run = get_run(run_id)
    if run is None:
        return None
    notes_lines: list[str] = []
    conn = storage.connect()
    try:
        trow = conn.execute("SELECT notes FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
        raw = (trow["notes"] if trow else None) or ""
        notes_lines = [ln.strip() for ln in raw.splitlines() if "[dispatch:" in ln]
    finally:
        conn.close()
    transitions: list[dict[str, Any]] = []
    since: str | None = None
    while True:
        batch = events.query_events(
            channels=[CHANNEL_DISPATCH_TRANSITION],
            limit=200,
            since=since,
        )
        if not batch:
            break
        for row in batch:
            payload = row.get("payload") or {}
            if payload.get("run_id") == run_id:
                transitions.append(payload)
        if len(batch) < 200:
            break
        since = batch[-1]["ulid"]
    attempts = _retry_count_for_run(transitions)
    if attempts is None:
        attempts = _delegation_attempts(run.get("delegation_id"))
    trust_evidence = {}
    for transition in transitions:
        if transition.get("state") in {"claimed", "resumed"}:
            trust_evidence = {
                key: transition[key]
                for key in ("trust_preset", "originator_trust_preset")
                if key in transition
            }
    return {
        **trust_evidence,
        "task_id": run["task_id"],
        "agent_label": run["agent_label"],
        "run_id": run["run_id"],
        "state": run["state"],
        "last_error": run.get("failure_reason"),
        "retry_count": attempts,
        "evidence_ref": run.get("evidence_ref"),
        "evidence_location": run.get("evidence_ref"),
        "delegation_id": run.get("delegation_id"),
        "actor": run.get("actor"),
        "updated_at": run.get("updated_at"),
        "dispatch_notes": notes_lines,
        "transitions": transitions,
    }


def reclaim_expired_leases(*, actor: str = ACTOR_COORDINATOR) -> dict[str, Any]:
    """Mark expired live leases ``retryable`` and release linked delegations to pending.

    Live native runs are checked against current trust policy on every tick,
    including before lease expiry. Revocation uses exact-run cancellation and
    its confirmed/unknown cessation boundary, never ordinary retry reclamation.
    Otherwise an unexpired live run is left alone; reclaim never double-launches.

    Task #1221 finding 3: a coordinator crash can leave a run at
    ``termination_outcome='requested'`` with an intent that never got
    finished. That is reconciled *first*, below, so the ordinary lease-expiry
    loop that follows never has the chance to promote such a row straight to
    ``retryable`` — which would silently drop the unconfirmed-cessation guard.
    """
    reconcile_pending_cancellations(actor=actor)

    # Task #2110: queued node work whose wait deadline passed is failed
    # (waiting_expired_node_offline) and its run closed BEFORE the lease loop
    # below, which would otherwise cancel the item and hand the run back as
    # retryable. Earlier expiries whose run close failed are retried here too.
    # A failing wait sweep must not block ordinary reclamation.
    try:
        expired_waits = node_queue.expire_waiting(actor=actor)
    except Exception:  # noqa: BLE001 - the next sweep retries
        logger.warning("node wait sweep failed during lease reclamation", exc_info=True)
        expired_waits = []

    ts = storage.now()
    now = datetime.now(UTC)
    reclaimed: list[dict[str, Any]] = []
    renewed: list[dict[str, Any]] = []
    overran: list[dict[str, Any]] = []
    node_expired: list[dict[str, Any]] = []
    trust_denied: list[dict[str, Any]] = []
    spawn_unsettled: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    node_settled = False
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE state IN ('claimed','launched','running') "
            "AND lease_expires_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            if row["termination_outcome"] is not None:
                # Reconcile above should already have resolved this; skip
                # rather than let the timeout/renew logic below touch a row
                # with an unfinished cancellation intent.
                continue
            originator = None
            if row["delegation_id"] is not None:
                delegation = conn.execute(
                    "SELECT from_agent FROM delegations WHERE id=?", (row["delegation_id"],)
                ).fetchone()
                originator = delegation["from_agent"] if delegation is not None else None
            try:
                trust_presets.require_native_allowed(row["agent_label"], originator=originator)
            except ToolError as exc:
                trust_denied.append({"run_id": row["run_id"], "denial_reason": str(exc)})
                # Preserve this live slot until cancel_run has classified a
                # real termination attempt, even when its lease already expired.
                continue
            exp = _parse_ts(row["lease_expires_at"])
            if exp is None or now < exp:
                continue
            evidence_ref = row["evidence_ref"]
            if isinstance(evidence_ref, str) and evidence_ref.startswith("node:"):
                # Node-backed run: settle its queue item first. A lost node
                # heartbeat does not prove the backend stopped, so a CLAIMED
                # node run must be reconciled — failed, never silently
                # relaunched (same contract as node_queue._fail_expired). An
                # UNCLAIMED item never ran, so the ordinary retryable path is
                # safe. A terminal/missing item means the queue already
                # settled — ordinary logic applies unchanged.
                node_verdict = node_queue.settle_expired_run(conn, row["run_id"], ts)
                if node_verdict in ("waiting", "wait_unsettled"):
                    # The wait deadline, not this lease, decides the outcome
                    # (#2110); expire_waiting above settles it when due.
                    continue
                if node_verdict is not None:
                    node_settled = True
                if node_verdict == "claimed":
                    res = transition_run(
                        conn,
                        row["run_id"],
                        target_state="failed",
                        expected_states=("claimed", "launched", "running"),
                        failure_reason="lease_expired_outcome_unknown",
                        require_cancellation_unlocked=True,
                        clear_lease=True,
                    )
                    if not res["ok"]:
                        continue
                    if row["delegation_id"] is not None:
                        conn.execute(
                            "UPDATE delegations SET status='failed', completed_at=?, updated_at=? "
                            "WHERE id=? AND status='in_flight'",
                            (ts, ts, row["delegation_id"]),
                        )
                    parts = evidence_ref.split(":", 2)
                    node_expired.append(
                        {
                            "run_id": row["run_id"],
                            "task_id": row["task_id"],
                            "delegation_id": row["delegation_id"],
                            "node": parts[1] if len(parts) > 1 else None,
                            "item_id": parts[2] if len(parts) > 2 else None,
                            "failure_reason": "lease_expired_outcome_unknown",
                        }
                    )
                    transitions.append(
                        {
                            "actor": actor,
                            "task_id": row["task_id"],
                            "delegation_id": row["delegation_id"],
                            "agent_label": row["agent_label"],
                            "run_id": row["run_id"],
                            "state": "failed",
                            "failure_reason": "lease_expired_outcome_unknown",
                            "dedupe_key": f"dispatch-reclaim:{row['run_id']}",
                        }
                    )
                    continue
                if node_verdict == "unclaimed":
                    res = transition_run(
                        conn,
                        row["run_id"],
                        target_state="retryable",
                        expected_states=("claimed", "launched", "running"),
                        failure_reason="node_unclaimed",
                        require_cancellation_unlocked=True,
                        clear_lease=True,
                    )
                    if not res["ok"]:
                        continue
                    if row["delegation_id"] is not None:
                        conn.execute(
                            "UPDATE delegations SET status='pending', last_attempt_at=NULL, "
                            "updated_at=? WHERE id=? AND status='in_flight'",
                            (ts, row["delegation_id"]),
                        )
                    reclaimed.append(
                        {
                            "run_id": row["run_id"],
                            "task_id": row["task_id"],
                            "delegation_id": row["delegation_id"],
                            "prior_state": row["state"],
                            "failure_reason": "node_unclaimed",
                        }
                    )
                    transitions.append(
                        {
                            "actor": actor,
                            "task_id": row["task_id"],
                            "delegation_id": row["delegation_id"],
                            "agent_label": row["agent_label"],
                            "run_id": row["run_id"],
                            "state": "retryable",
                            "failure_reason": "node_unclaimed",
                            "dedupe_key": f"dispatch-reclaim:{row['run_id']}",
                        }
                    )
                    continue
            live_pid = _pid_from_evidence(row["evidence_ref"])
            if row["state"] == "claimed" and live_pid is not None and _pid_alive(live_pid):
                lease_ttl = config.dispatch_scheduler_lease_ttl_s()
                new_lease = (now + timedelta(seconds=lease_ttl)).isoformat()
                transition_run(
                    conn,
                    row["run_id"],
                    target_state="launched",
                    expected_states=("claimed",),
                    lease_expires_at=new_lease,
                    require_cancellation_unlocked=True,
                )
                continue

            # Task #602: a LAUNCHED run whose process is genuinely still ours
            # and still alive is working, not stale. Before this, the liveness
            # check above applied only to 'claimed' — the narrow
            # Popen-to-launched window — so every real spawn hit the timeout
            # branch below at lease expiry (300s) and released its delegation
            # back to pending. The next tick could then launch the same task
            # alongside the agent already doing it.
            #
            # Renewal is bounded by the wall-clock ceiling so a hung agent
            # cannot hold its slot forever: past that, fall through and time it
            # out. Termination and delegation release happen after conn.close().
            if row["state"] in {"launched", "running"}:
                runtime_over = supervise.runtime_exceeded(row["created_at"], now=now)
                ours = supervise.pid_is_ours(row["evidence_ref"])
                if ours and not runtime_over:
                    lease_ttl = config.dispatch_scheduler_lease_ttl_s()
                    new_lease = (now + timedelta(seconds=lease_ttl)).isoformat()
                    res = transition_run(
                        conn,
                        row["run_id"],
                        target_state=None,
                        expected_states=("launched", "running"),
                        lease_expires_at=new_lease,
                        require_cancellation_unlocked=True,
                    )
                    if res["ok"]:
                        renewed.append(
                            {
                                "run_id": row["run_id"],
                                "task_id": row["task_id"],
                                "pid": live_pid,
                            }
                        )
                    continue
                if runtime_over and live_pid is not None and _pid_alive(live_pid):
                    entry = {
                        "run_id": row["run_id"],
                        "task_id": row["task_id"],
                        "pid": live_pid,
                        "evidence_ref": row["evidence_ref"],
                        "delegation_id": row["delegation_id"],
                        "prior_state": row["state"],
                        "agent_label": row["agent_label"],
                    }
                    if not ours:
                        # Recycled or forged pid token — never signal bare pid.
                        entry["terminated"] = "identity_mismatch"
                    overran.append(entry)
                    # Defer retryable + delegation release until after termination
                    # (or identity block) below.
                    continue

            # Launched/running expiry is an operator-visible timeout; a claim
            # that never crossed the launch boundary is a plain lease expiry.
            # A claim whose spawn started without a recorded outcome stays
            # claimed (#2512): its launcher may still be spawning, and a
            # retryable run would hand the task to a second spawn. The guard is
            # in the compare-and-swap itself, so a fence committed after the read
            # above still holds the run. Only the outcome, or the operator's
            # settle_unsettled_spawn, releases it.
            prior_state = row["state"]
            reason = "timeout" if prior_state in {"launched", "running"} else "lease_expired"
            res = transition_run(
                conn,
                row["run_id"],
                target_state="retryable",
                expected_states=("claimed", "launched", "running"),
                failure_reason=reason,
                require_cancellation_unlocked=True,
                clear_lease=True,
                guard_sql=f"state <> 'claimed' OR NOT {_SPAWN_UNSETTLED_SQL}",
            )
            if not res["ok"]:
                if res.get("reason") == "guard_refused":
                    spawn_unsettled.append(
                        {
                            "run_id": row["run_id"],
                            "task_id": row["task_id"],
                            "delegation_id": row["delegation_id"],
                            "lease_expires_at": row["lease_expires_at"],
                        }
                    )
                continue
            if row["delegation_id"] is not None:
                conn.execute(
                    "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? "
                    "WHERE id=? AND status='in_flight'",
                    (ts, row["delegation_id"]),
                )
            reclaimed.append(
                {
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "delegation_id": row["delegation_id"],
                    "prior_state": prior_state,
                    "failure_reason": reason,
                }
            )
            transitions.append(
                {
                    "actor": actor,
                    "task_id": row["task_id"],
                    "delegation_id": row["delegation_id"],
                    "agent_label": row["agent_label"],
                    "run_id": row["run_id"],
                    "state": "retryable",
                    "failure_reason": reason,
                    "dedupe_key": f"dispatch-reclaim:{row['run_id']}",
                }
            )
        # Orphan in_flight delegations with no live run (crash before insert).
        orphans = conn.execute(
            "SELECT d.* FROM delegations d "
            "LEFT JOIN dispatch_runs r ON r.delegation_id=d.id "
            "AND r.state IN ('claimed','launched','running') "
            "WHERE d.status='in_flight' AND r.run_id IS NULL"
        ).fetchall()
        for drow in orphans:
            task = _load_task(conn, drow["task_id"])
            if task is not None and _task_executor_lock_active(task, now):
                continue
            # Only reclaim when last_attempt is stale or missing — avoid yanking
            # a claim that just started in another connection.
            last = _parse_ts(drow["last_attempt_at"]) or _parse_ts(drow["updated_at"])
            orphan_stale_s = config.dispatch_scheduler_lease_ttl_s()
            if last is not None and (now - last).total_seconds() < orphan_stale_s:
                continue
            conn.execute(
                "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? "
                "WHERE id=? AND status='in_flight'",
                (ts, drow["id"]),
            )
            reclaimed.append(
                {
                    "run_id": None,
                    "task_id": drow["task_id"],
                    "delegation_id": drow["id"],
                    "orphan_delegation": True,
                }
            )
        conn.commit()
    finally:
        conn.close()

    if node_settled:
        # settle_expired_run ended node items inside the transaction above;
        # their undelivered follow-ups are dropped now, with events (#1740).
        node_queue.sweep_ended_followups(actor=actor)

    # Never hold SQLite across a blocking termination attempt. cancel_run
    # records intent first and fences the exact run against concurrent launch.
    for item in trust_denied:
        item["cancellation"] = cancel_run(
            item["run_id"], actor=actor, reason="trust_preset_native_denied"
        )

    # Terminate overran processes HERE, before releasing their delegations.
    #
    # Overran rows are collected above with `continue` so the delegation stays
    # in_flight until the process is gone. claim_and_dispatch() also calls this
    # function, so killing must happen in reclaim itself — not only in tick().
    #
    # Deliberately after `conn.close()`: terminate() blocks for the SIGTERM
    # grace period, and holding the SQLite write connection across seconds of
    # sleeping is how unrelated writes start failing on the 5s busy timeout.
    for item in overran:
        if "terminated" not in item:
            item["terminated"] = supervise.terminate_ours(item.get("evidence_ref"))
        run_id = item.get("run_id")
        if not run_id:
            continue

        outcome = item["terminated"]
        conn = storage.connect()
        try:
            ts = storage.now()
            if outcome in {"terminated", "killed", "already_gone"}:
                res = transition_run(
                    conn,
                    run_id,
                    target_state="retryable",
                    expected_states=("claimed", "launched", "running"),
                    failure_reason="timeout",
                    require_cancellation_unlocked=True,
                    clear_lease=True,
                )
                if not res["ok"]:
                    # The exit watcher already reported this run while we blocked
                    # on the SIGTERM grace period, or cancellation was requested.
                    conn.commit()
                    continue
                delegation_id = item.get("delegation_id")
                if delegation_id is not None:
                    conn.execute(
                        "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? "
                        "WHERE id=? AND status='in_flight'",
                        (ts, delegation_id),
                    )
                reclaimed.append(
                    {
                        "run_id": run_id,
                        "task_id": item.get("task_id"),
                        "delegation_id": item.get("delegation_id"),
                        "prior_state": item.get("prior_state"),
                        "failure_reason": "timeout",
                    }
                )
                transitions.append(
                    {
                        "actor": actor,
                        "task_id": item.get("task_id"),
                        "delegation_id": item.get("delegation_id"),
                        "agent_label": item.get("agent_label"),
                        "run_id": run_id,
                        "state": "retryable",
                        "failure_reason": "timeout",
                        "dedupe_key": f"dispatch-reclaim:{run_id}",
                    }
                )
            elif outcome in {"refused", "identity_mismatch", "kill_unconfirmed"}:
                if outcome == "identity_mismatch":
                    reason = "runtime_ceiling_identity_mismatch"
                elif outcome == "kill_unconfirmed":
                    # Task #1221 finding 4: SIGKILL was sent but exit was never
                    # observed — treat the same as a refused/mismatched kill,
                    # not as a stopped process.
                    reason = "runtime_ceiling_kill_unconfirmed"
                else:
                    reason = "runtime_ceiling_termination_refused"
                res = transition_run(
                    conn,
                    run_id,
                    target_state="blocked",
                    expected_states=("claimed", "launched", "running"),
                    failure_reason=reason,
                    termination_outcome=("unknown" if outcome == "kill_unconfirmed" else None),
                    require_cancellation_unlocked=True,
                    clear_lease=True,
                )
                if not res["ok"]:
                    conn.commit()
                    continue
                delegation_id = item.get("delegation_id")
                if delegation_id is not None:
                    conn.execute(
                        "UPDATE delegations SET status='failed', updated_at=? "
                        "WHERE id=? AND status='in_flight'",
                        (ts, delegation_id),
                    )
                transitions.append(
                    {
                        "actor": actor,
                        "task_id": item.get("task_id"),
                        "delegation_id": item.get("delegation_id"),
                        "agent_label": item.get("agent_label"),
                        "run_id": run_id,
                        "state": "blocked",
                        "failure_reason": reason,
                        "dedupe_key": f"dispatch-reclaim:{run_id}",
                    }
                )
            conn.commit()
        finally:
            conn.close()

    for tr in transitions:
        _record_transition(**tr)

    # Kept unsettled spawns reach the operator once each (#2512).
    for item in spawn_unsettled:
        item["settle"] = SETTLE_SPAWN_ROUTE.format(run_id=item["run_id"])
        try:
            item["warned"] = warn_unsettled_spawn(item, actor=actor)
        except Exception:  # noqa: BLE001 - the next reclaim retries
            item["warned"] = False
            logger.warning("unsettled-spawn warning failed for %s", item["run_id"], exc_info=True)

    return {
        "reclaimed_count": len(reclaimed),
        "reclaimed": reclaimed,
        # Task #602 telemetry. `renewed` is the fix working — live agents that
        # would previously have been reclaimed out from under themselves.
        # `overran` are runs past the wall-clock ceiling; each carries how its
        # termination went ('terminated' / 'killed' / 'already_gone' /
        # 'refused').
        "renewed_count": len(renewed),
        "renewed": renewed,
        "overran_count": len(overran),
        "overran": overran,
        # Node-backed runs whose claimed queue item expired outcome-unknown
        # (task #1327, I1). These are failures, never retries.
        "node_expired_count": len(node_expired),
        "node_expired": node_expired,
        # Queued items whose offline node missed their wait deadline (#2110).
        "node_wait_expired": expired_waits,
        "trust_denied_count": len(trust_denied),
        "trust_denied": trust_denied,
        # Expired claims kept because their spawn may still be in flight (#2512):
        # the outcome or the operator's settle-spawn releases each.
        "spawn_unsettled_count": len(spawn_unsettled),
        "spawn_unsettled": spawn_unsettled,
    }


def _consume_approval_pass(
    conn, *, task_id: int, delegation_id: int, run_id: str, terminal: bool = False
) -> None:
    """Consume one validated grant and bind its recovery to the winning run."""
    from ..tools.approvals import _pass_expires_at

    valid, grant = dispatch_approval_selection(conn, task_id=task_id, delegation_id=delegation_id)
    if not valid:
        if terminal:
            # Record an already executed outcome without spending invalid or
            # unrelated authority. Terminal bookkeeping never grants a launch.
            return
        raise ToolError("execution approval authority is invalid or ambiguous")
    if grant is None:
        # Genuine absence preserves the existing operator-marker contract.
        return
    if terminal:
        # Older runs spent passes during terminal bookkeeping. Current runs
        # already spent at claim/resume: do not rotate their proof or touch a
        # newer approval. Timestamps in the same second cannot establish causal
        # order when either column may have been rounded: preserve the grant.
        run = conn.execute(
            "SELECT state, created_at FROM dispatch_runs WHERE run_id=? "
            "AND task_id=? AND delegation_id=?",
            (run_id, task_id, delegation_id),
        ).fetchone()
        resolved = _parse_ts(grant["resolved_at"])
        created = _parse_ts(run["created_at"]) if run else None
        if (
            run is None
            or run["state"] not in {"finished", "blocked"}
            or grant["status"] != "approved"
            or resolved is None
            or created is None
            or resolved.replace(microsecond=0) >= created.replace(microsecond=0)
        ):
            return
    expires = _pass_expires_at(grant)
    if not terminal and (
        grant["status"] not in {"approved", "consumed"}
        or expires is None
        or datetime.now(UTC) >= expires
        or (grant["status"] == "consumed" and dispatch_recovery_run(conn, grant) is None)
    ):
        raise ToolError("execution approval changed before the winning claim")
    previous = _parse_ts(grant["updated_at"])
    generation = max(
        datetime.now(UTC),
        previous + timedelta(microseconds=1) if previous else datetime.min.replace(tzinfo=UTC),
    ).isoformat(timespec="microseconds")
    updated = conn.execute(
        "UPDATE approvals SET status='consumed', updated_at=? "
        "WHERE id=? AND org_id IS ? AND tool=? AND payload=? AND status=? "
        "AND updated_at=? AND resolved_at IS ? AND resolved_by IS ?",
        (
            generation,
            grant["id"],
            grant["org_id"],
            DISPATCH_APPROVAL_TOOL,
            grant["payload"],
            grant["status"],
            grant["updated_at"],
            grant["resolved_at"],
            grant["resolved_by"],
        ),
    )
    if updated.rowcount != 1:
        raise ToolError("execution approval changed during consumption")
    events.record_event_once_on_connection(
        conn,
        "agent.approval.consumed",
        payload={
            "approval_id": grant["id"],
            "tool": DISPATCH_APPROVAL_TOOL,
            "source": "dispatch_claim-v1",
            "task_id": task_id,
            "delegation_id": delegation_id,
            "run_id": run_id,
            "org_id": grant["org_id"],
            "generation": generation,
            "grant": {
                field: grant[field]
                for field in ("requested_by", "resolved_by", "resolved_at", "created_at")
            },
        },
        originator=config.verified_agent_identity() or ACTOR_COORDINATOR,
    )


def _quota_gate(
    agent_label: str,
    *,
    quota_status: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return exhaustion evidence when the agent's fleet lane is quota-blocked.

    Consults the ``model_quota_status`` projection (task #1251, C2): only a
    positive ``unavailable`` observation gates. ``unknown`` lanes — no
    exhaustion evidence — dispatch exactly as before; absent data is not a
    block. ``quota_status`` may be injected (the scheduler builds it once per
    tick) — None builds it fresh.
    """
    from ..quota_observe import build_quota_status, lane_for_agent

    lane = lane_for_agent(agent_label)
    if lane == "unknown":
        return None
    status = quota_status if quota_status is not None else build_quota_status()
    for row in status.get("quotas") or []:
        if row.get("lane") == lane:
            if row.get("availability") == "unavailable":
                return {
                    "lane": lane,
                    "evidence": row.get("evidence"),
                    "observed_at": row.get("observed_at"),
                    "source": row.get("source"),
                }
            logger.info(
                "quota gate open: lane=%s availability=%s source=%s observed_at=%s",
                lane,
                row.get("availability"),
                row.get("source"),
                row.get("observed_at"),
            )
            return None
    logger.info("quota gate open: lane=%s reason=no_quota_row", lane)
    return None


def claim_and_dispatch(
    *,
    task_id: int | None = None,
    delegation_id: int | None = None,
    actor: str = ACTOR_COORDINATOR,
    launch: LaunchBoundary | None = None,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    expected_project: str | None = None,
    expected_lane: str | None = None,
    quota_status: dict[str, Any] | None = None,
    launch_key: str | None = None,
) -> dict[str, Any]:
    """Atomically claim an approved delegation and mint one durable run_id.

    Returns ``{launched, reason, run_id}``. Repeated polls after a successful
    claim return ``already_claimed`` with the existing ``run_id`` and never
    invoke the launch boundary a second time.

    ``launch_key`` (task #2501): a Control Center launch's operation key. It is
    bound to the minted run in the claim's transaction (``dispatch_launch_keys``);
    while it is bound to another run (any task's, unless a same-task run that
    never reached launch) the claim is refused: ``already_claimed`` for the same
    task, ``launch_key_bound`` for another, each with the bound ``run_id``.

    Every dispatch, keyed or not, fences its spawn (#2512): an unkeyed one under
    its per-run key ``run:<run_id>``. While any run of the task has a spawn that
    started without a recorded outcome, the claim is refused
    (``launch_unsettled``, with that ``run_id``) and reclaim keeps that run
    claimed, so a stalled spawn never has a second job started beside it.
    """
    if launch_key and launch_key.startswith(RUN_SPAWN_KEY_PREFIX):
        raise ValueError(f"launch_key may not start with {RUN_SPAWN_KEY_PREFIX!r}")
    launch = launch or scheduler_launch_boundary()
    # Auto-reclaim stale leases so a crash/restart can progress without a
    # separate operator step; reclaim never launches.
    reclaim_expired_leases(actor=actor)

    verdict = evaluate_ready(
        task_id=task_id,
        delegation_id=delegation_id,
        expected_project=expected_project,
        expected_lane=expected_lane,
    )
    task = verdict.get("task")
    delegation = verdict.get("delegation")
    agent_label = verdict.get("agent_label")

    if not verdict["ready"]:
        reason = verdict["reason"]
        live = verdict.get("live_run")
        # Duplicate poll: surface the existing run_id, still no launch.
        if reason == "already_claimed" and live is not None:
            return {
                "launched": False,
                "reason": "already_claimed",
                "run_id": live["run_id"],
            }
        if reason == "cessation_unconfirmed" and live is not None:
            # Task #1221 finding 1: never reclaim-and-retry this one — surface
            # the blocking run so an operator can retry_cancellation() or
            # confirm_cancellation() it, and record a refusal for visibility.
            _record_transition(
                actor=actor,
                task_id=task["id"] if task else None,
                delegation_id=delegation["id"] if delegation else None,
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason="cessation_unconfirmed",
            )
            return {
                "launched": False,
                "reason": "cessation_unconfirmed",
                "run_id": None,
                "blocking_run_id": live["run_id"],
            }
        if reason == "lease_expired":
            # One more reclaim pass, then re-evaluate.
            reclaim_expired_leases(actor=actor)
            verdict = evaluate_ready(
                task_id=task_id,
                delegation_id=delegation_id,
                expected_project=expected_project,
                expected_lane=expected_lane,
            )
            task = verdict.get("task")
            delegation = verdict.get("delegation")
            agent_label = verdict.get("agent_label")
            if not verdict["ready"]:
                reason = verdict["reason"]
                live = verdict.get("live_run")
                if reason == "already_claimed" and live is not None:
                    return {
                        "launched": False,
                        "reason": "already_claimed",
                        "run_id": live["run_id"],
                    }
                if reason == "lease_expired" and live is not None:
                    # Reclaim keeps a claim whose spawn may still be in flight
                    # (#2512): this dispatch waits for that spawn's outcome.
                    check = storage.connect()
                    try:
                        unsettled = _run_spawn_unsettled(check, live["run_id"])
                    finally:
                        check.close()
                    if unsettled:
                        return {
                            "launched": False,
                            "reason": "launch_unsettled",
                            "run_id": live["run_id"],
                        }
                _record_transition(
                    actor=actor,
                    task_id=task["id"] if task else None,
                    delegation_id=delegation["id"] if delegation else None,
                    agent_label=agent_label,
                    run_id=None,
                    state="refused",
                    failure_reason=reason,
                )
                return {"launched": False, "reason": reason, "run_id": None}
        else:
            _record_transition(
                actor=actor,
                task_id=task["id"] if task else None,
                delegation_id=delegation["id"] if delegation else None,
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason=reason,
            )
            return {"launched": False, "reason": reason, "run_id": None}

    assert task is not None and delegation is not None and agent_label is not None

    # C2 (task #1251): refuse before the atomic claim when the target lane is
    # quota-exhausted — no run minted, no attempt burned, delegation stays
    # pending and retries once the lane recovers. Recorded on the transition
    # ledger like every other refusal.
    quota_block = _quota_gate(agent_label, quota_status=quota_status)
    if quota_block is not None:
        reason = f"quota_exhausted:lane={quota_block['lane']}"
        _record_transition(
            actor=actor,
            task_id=task["id"],
            delegation_id=delegation["id"],
            agent_label=agent_label,
            run_id=None,
            state="refused",
            failure_reason=reason,
        )
        return {
            "launched": False,
            "reason": "quota_exhausted",
            "run_id": None,
            "quota_lane": quota_block["lane"],
            "quota_evidence": quota_block.get("evidence"),
            "quota_observed_at": quota_block.get("observed_at"),
        }

    trust_snapshot = trust_presets.snapshot(agent_label)
    originator_snapshot = trust_presets.snapshot(delegation.get("from_agent"))
    run_id = _new_run_id(task["id"])
    ts = storage.now()
    lease_expires = (datetime.now(UTC) + timedelta(seconds=lease_ttl_s)).isoformat()

    # A subtree hold released on its own policy inside this transaction is
    # published once the transaction commits (task #1747).
    hold_events: list[dict[str, Any] | None] = []
    conn = storage.connect()
    try:
        # Atomic claim: pending → in_flight on the delegation row.
        cur = conn.execute(
            "UPDATE delegations SET status='in_flight', attempts=attempts+1, "
            "claim_generation=claim_generation+1, "
            "last_attempt_at=?, updated_at=? WHERE id=? AND status='pending'",
            (ts, ts, delegation["id"]),
        )
        if cur.rowcount == 0:
            conn.commit()
            live = _live_run_for_task(conn, task["id"])
            return {
                "launched": False,
                "reason": "already_claimed",
                "run_id": live["run_id"] if live else None,
            }

        # Re-validate under the write lock — approval may have been revoked or
        # the parent cancelled while we were evaluating readiness.
        fresh_task = _load_task(conn, task["id"])
        fresh_delegation = _load_delegation(conn, delegation_id=delegation["id"])
        if fresh_task is None or fresh_delegation is None:
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            return {"launched": False, "reason": "malformed", "run_id": None}
        if fresh_task.get("status") in TERMINAL_TASK:
            reason = "cancelled" if fresh_task.get("status") == "cancelled" else "task_terminal"
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            _record_transition(
                actor=actor,
                task_id=fresh_task["id"],
                delegation_id=fresh_delegation["id"],
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason=reason,
            )
            return {"launched": False, "reason": reason, "run_id": None}
        if _has_cancelled_ancestor(conn, fresh_task):
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            _record_transition(
                actor=actor,
                task_id=fresh_task["id"],
                delegation_id=fresh_delegation["id"],
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason="cancelled",
            )
            return {"launched": False, "reason": "cancelled", "run_id": None}
        if task_holds.claim_block(conn, int(fresh_task["id"]), pending=hold_events):
            # A hold placed while readiness was evaluated (task #1747).
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            _record_transition(
                actor=actor,
                task_id=fresh_task["id"],
                delegation_id=fresh_delegation["id"],
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason="held",
            )
            return {"launched": False, "reason": "held", "run_id": None}
        ok, reason = _classify_approval(
            fresh_task.get("notes") or "",
            conn,
            task_id=fresh_task["id"],
            delegation_id=fresh_delegation["id"],
            delegation_task_spec=fresh_delegation.get("task_spec"),
        )
        if not ok:
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            _record_transition(
                actor=actor,
                task_id=fresh_task["id"],
                delegation_id=fresh_delegation["id"],
                agent_label=agent_label,
                run_id=None,
                state="refused",
                failure_reason=reason,
            )
            return {"launched": False, "reason": reason, "run_id": None}
        task = fresh_task
        delegation = fresh_delegation
        key_refusal = _launch_key_refusal(conn, launch_key, int(task["id"])) if launch_key else None
        if key_refusal is None:
            # Any run of this task still spawning holds it, keyed or not (#2512).
            key_refusal = _task_spawn_refusal(conn, int(task["id"]))
        if key_refusal is not None:
            conn.execute(
                "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), "
                "last_attempt_at=NULL, updated_at=? WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            return key_refusal

        try:
            conn.execute(
                "INSERT INTO dispatch_runs "
                "(run_id, task_id, delegation_id, agent_label, state, actor, "
                "lease_owner, lease_expires_at, failure_reason, evidence_ref, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    run_id,
                    task["id"],
                    delegation["id"],
                    agent_label,
                    actor,
                    actor,
                    lease_expires,
                    ts,
                    ts,
                ),
            )
        except sqlite3.IntegrityError:
            # Unique live-run index lost the race — roll delegation back.
            conn.execute(
                "UPDATE delegations SET status='pending', "
                "attempts=MAX(0, attempts-1), last_attempt_at=NULL, updated_at=? "
                "WHERE id=? AND status='in_flight'",
                (ts, delegation["id"]),
            )
            conn.commit()
            live = _live_run_for_task(conn, task["id"])
            return {
                "launched": False,
                "reason": "already_claimed",
                "run_id": live["run_id"] if live else None,
            }
        # Every run files its spawn mark under its spawn key (#2512).
        spawn_key = _spawn_key(run_id, launch_key)
        _bind_launch_key(conn, spawn_key, int(task["id"]), run_id, ts)
        # Spend the exact existing execution pass with the winning claim/run,
        # after the locked approval recheck. Recovery continues to recognize
        # this consumed grant only inside its original lifetime.
        _consume_approval_pass(
            conn, task_id=task["id"], delegation_id=delegation["id"], run_id=run_id
        )
        conn.commit()
    finally:
        committed = not conn.in_transaction
        conn.close()
        if committed:
            task_holds.publish(hold_events)

    _record_transition(
        actor=actor,
        task_id=task["id"],
        delegation_id=delegation["id"],
        agent_label=agent_label,
        run_id=run_id,
        state="claimed",
        trust_snapshot=trust_snapshot,
        originator_snapshot=originator_snapshot,
        dedupe_key=f"dispatch-claimed:{run_id}",
    )

    # Launch boundary is outside the claim transaction on purpose: a crash here
    # leaves state=claimed with a reclaimable lease (never silent success).
    launched = _fenced_launch(
        launch,
        spawn_key,
        run_id=run_id,
        actor=actor,
        task_id=task["id"],
        agent_label=agent_label,
        delegation_id=delegation["id"],
    )
    if launched == FENCE_REFUSED:
        _record_transition(
            actor=actor,
            task_id=task["id"],
            delegation_id=delegation["id"],
            agent_label=agent_label,
            run_id=run_id,
            state="retryable",
            failure_reason="lease_lost",
        )
        return {"launched": False, "reason": "lease_lost", "run_id": run_id}
    if isinstance(launched, str):
        return {"launched": False, "reason": "lease_lost", "run_id": run_id}
    result = launched
    if not result.get("ok", True):
        if result.get("error") == "cancelled_during_launch":
            # Task #1221 finding 2: see the matching branch in resume_run —
            # the launch boundary already terminated the late spawn and
            # finalized the row; do not overwrite that with 'retryable'.
            return {
                "ok": False,
                "launched": False,
                "reason": "cancelled_during_launch",
                "run_id": run_id,
                "state": "cancelled",
                "termination": result.get("termination"),
                "cessation": result.get("cessation"),
            }
        failure = str(result.get("error") or "launch_failed")[:500]
        conn = storage.connect()
        try:
            res = transition_run(
                conn,
                run_id,
                target_state="retryable",
                expected_states=("claimed",),
                failure_reason=failure,
                require_cancellation_unlocked=True,
            )
            if res["ok"]:
                conn.execute(
                    "UPDATE delegations SET status='pending', last_attempt_at=NULL, updated_at=? "
                    "WHERE id=? AND status='in_flight'",
                    (storage.now(), delegation["id"]),
                )
            conn.commit()
        finally:
            conn.close()
        _record_transition(
            actor=actor,
            task_id=task["id"],
            delegation_id=delegation["id"],
            agent_label=agent_label,
            run_id=run_id,
            state="retryable",
            failure_reason=failure,
        )
        return {
            "launched": False,
            "reason": "launch_failed",
            "error": failure,
            "run_id": run_id,
            "spawned": False,
            "pid": None,
        }

    conn = storage.connect()
    try:
        res = transition_run(
            conn,
            run_id,
            target_state="launched",
            expected_states=("claimed",),
            expected_lease_owner=actor,
            require_cancellation_unlocked=True,
        )
        if not res["ok"]:
            current = conn.execute(
                "SELECT * FROM dispatch_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            conn.commit()
            current_run = _row(current) if current else None
            if current_run and current_run.get("termination_outcome") is not None:
                result = {
                    "ok": False,
                    "reason": (
                        "cancelled_during_launch"
                        if current_run.get("state") == "cancelled"
                        else "cancellation_in_progress"
                    ),
                    "run_id": run_id,
                    "state": current_run.get("state"),
                    "termination_outcome": current_run.get("termination_outcome"),
                }
                if current_run.get("termination_outcome") in {"confirmed", "unknown"}:
                    result["cessation"] = current_run["termination_outcome"]
                return result
            _record_transition(
                actor=actor,
                task_id=task["id"],
                delegation_id=delegation["id"],
                agent_label=agent_label,
                run_id=run_id,
                state="retryable",
                failure_reason="lease_lost",
            )
            return {"launched": False, "reason": "lease_lost", "run_id": run_id}
        conn.commit()
    finally:
        conn.close()

    _record_transition(
        actor=actor,
        task_id=task["id"],
        delegation_id=delegation["id"],
        agent_label=agent_label,
        run_id=run_id,
        state="launched",
        dedupe_key=f"dispatch-launched:{run_id}",
    )
    spawn = result.get("spawn") or {}
    return {
        "launched": True,
        "reason": "ok",
        "run_id": run_id,
        "spawned": bool(result.get("spawned")),
        "pid": result.get("pid") if result.get("pid") is not None else spawn.get("pid"),
        "stub": bool(result.get("stub", True)),
    }


# --- agent lifecycle stop (task #1723) ----------------------------------------


def lifecycle_stop_runs(agent: str, actor: str, reason: str) -> list[dict[str, Any]]:
    """Cancel every live dispatch run of ``agent`` for a lifecycle pause or terminate.

    Registered with ``agent_lifecycle`` as its ``dispatch`` stop handler, so the
    lifecycle module does not import this runtime module. One row per run;
    a failed cancel is reported as ``control='failed'``, never aborts the rest.
    """
    conn = storage.connect()
    try:
        try:
            rows = conn.execute(
                "SELECT run_id FROM dispatch_runs WHERE agent_label=? AND state IN "
                f"({','.join('?' * len(LIVE_STATES))}) "
                "AND task_id IN (SELECT id FROM tasks WHERE org_id IS ?)",
                (agent, *sorted(LIVE_STATES), edition.tenant_org_id()),
            ).fetchall()
        except Exception:  # noqa: BLE001 - table absent on a fresh store
            rows = []
    finally:
        conn.close()
    out = []
    for row in rows:
        run_id = row["run_id"]
        try:
            res = cancel_run(run_id, actor=actor, reason=reason)
            outcome = str(res.get("termination_outcome") or res.get("reason") or "requested")
            control = outcome if outcome in {"confirmed", "unknown"} else "requested"
            out.append({"path": "dispatch", "ref": run_id, "control": control, "detail": outcome})
        except Exception as exc:  # noqa: BLE001 - report, never abort the fan-out
            out.append(
                {"path": "dispatch", "ref": run_id, "control": "failed", "detail": str(exc)[:200]}
            )
    return out


agent_lifecycle.register_stop_handler("dispatch", lifecycle_stop_runs)
