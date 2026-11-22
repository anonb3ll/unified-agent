"""Self-pickup run records (task #1436).

Self-pickup is the path real work takes: an agent calls ``task_claim`` (or moves
a task to ``doing`` through ``task_update``), does the work, and moves the task
on. Until this module the platform recorded the task state but not the run, so
it could not say what was in flight, how long a run took, how it ended, or
whether an agent died mid-run (survey #1432, section 3b).

This module writes that record. It adds no scheduler, and one small table only
for heartbeat usage (below):

* **Storage** is the durable, hash-chained ``event_log`` on one channel,
  ``agent.run.selfpickup``, with two event kinds: ``selfpickup.claimed`` and
  ``selfpickup.finished``. They extend the declared ``run`` object's
  ``dispatch.*`` kinds (``object_model.py``). A run id is ``sp-<ULID>``.
* **Idempotency** uses ``event_log.dedupe_key``: one claim per
  (task, agent, claim instant) and one finish per run, so a retried write is harmless.
* **Best effort.** The recorder runs after the task transaction has committed.
  :func:`safe` swallows any failure, logs it and leaves a ``run_recorder`` row
  in ``agent_events``, so a broken recorder can never fail a claim. Missing
  coverage is still visible: :func:`runs_in_flight` lists ``doing`` tasks with no
  open run, and :func:`run_coverage` reports completed tasks with no run.

Outcomes (the vocabulary #1348, #1354 and #1465 read):

=============  ==============================================================
``completed``  the task moved to ``done``
``handed_off`` the task moved to ``waiting_review``
``blocked``    the task moved to ``blocked``
``released``   the task went back to ``open``, or the claim was released
``cancelled``  the task was cancelled
``abandoned``  another agent claimed the task after this run's lease lapsed
=============  ==============================================================

An open run is reported ``abandoned`` on read, without any write, when its lease
has lapsed, its claimant changed, or the task left ``doing`` by a path that
bypassed ``task_update``. A claimed-but-never-finished run is therefore
distinguishable from a task that was never claimed.

Usage is optional. A finishing ``task_update`` may pass ``run_usage``; every
field is present in the record either way and ``null`` when unknown, with
``usage_reported`` saying which.

A live renewal (``task_claim`` by the agent already holding the task) is the
run's heartbeat and may carry ``run_usage`` too: the run's *cumulative* usage
so far, not a delta (re-land of PR #866, task #1681). Each heartbeat replaces
the fields it reports and keeps the rest. The latest snapshot lives in the
one-row-per-run ``run_usage_heartbeats`` table, not the event log, so a
renewal every poll tick appends nothing to the hash chain. Reads overlay it on
an open run, and the run's finish event carries it: a finishing
``run_usage`` overrides the fields it reports, and an abandoned run keeps the
cost its dead agent had already reported. ``wall_s`` is normally computed; a conservative
direct repair of a missing finish leaves it null because work duration is unknown.

Authenticated usage-only reports append deduplicated accounting events and
overlay late cumulative totals on the original run, including after it closes.
They do not alter a finish outcome or task/lease.

Nothing here backfills history. Runs that started before this module deployed
appear as ``unrecorded`` until they finish; they are never fabricated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    completion_return,
    events,
    operating_policy,
    operating_profiles,
    ready_dependents,
    storage,
    table_schemas,
    trust_presets,
)

logger = logging.getLogger("unified_agent.run_records")
_TRUST_UNSET = object()

CHANNEL = "agent.run.selfpickup"
KIND_CLAIMED = "selfpickup.claimed"
KIND_FINISHED = "selfpickup.finished"
KIND_CANCEL_REQUESTED = "selfpickup.cancel_requested"
KIND_USAGE_REPORTED = "selfpickup.usage_reported"
RUN_ID_PREFIX = "sp-"

OUTCOMES = ("completed", "handed_off", "blocked", "released", "cancelled", "abandoned")
_STATUS_OUTCOME = {
    "done": "completed",
    "waiting_review": "handed_off",
    "blocked": "blocked",
    "open": "released",
    "cancelled": "cancelled",
}
# A finish with no recorded claim is still worth writing for these transitions:
# it attributes the finish and counts against coverage.
_UNMATCHED_FINISH_STATUSES = frozenset({"done", "waiting_review"})
# An open run on a task in one of these states finished without a recorded
# finish; it is reported as unclosed, not as in flight.
_NOT_IN_FLIGHT = frozenset({"done", "waiting_review", "cancelled"})
# A completed task is covered when its latest run ended one of these ways.
_COVERING_OUTCOMES = frozenset({"completed", "handed_off"})

USAGE_FIELDS: dict[str, tuple[type, ...]] = {
    "provider": (str,),
    "model": (str,),
    "input_tokens": (int,),
    "output_tokens": (int,),
    "cost_usd": (int, float),
    "cost_complete": (bool,),
    # Task #1348: how the run was paid for, where its time went, and whether a
    # subscription lane was contended (queued or rate-limited) rather than slow.
    "billing": (str,),
    "phases": (dict,),
    "contended": (bool,),
    # Task #1841: the harness's remaining plan capacity, so a run stopped by a
    # usage ceiling is recorded as that, not as a lapsed lease.
    "capacity": (dict,),
}
BILLING_VALUES = ("api", "subscription", "local")
MAX_PHASES = 20

# Task #1841: finish ``reason_code`` values. ``lease_expired`` marks every
# lease-lapse path (queue hygiene, the doctor's stale-lock reclaim, a takeover
# after the lapse); ``capacity_ceiling`` replaces it, and marks a release or
# block, when the run itself reported that its harness hit a usage limit.
REASON_LEASE_EXPIRED = "lease_expired"
REASON_CAPACITY_CEILING = "capacity_ceiling"
CAPACITY_FIELDS: dict[str, tuple[type, ...]] = {
    "limit_reached": (bool,),
    "remaining_pct": (int, float),
    "resets_at": (str,),
    "source": (str,),
    "detail": (str,),
}
# An open run at or below this share of plan capacity is listed in
# ``runs_in_flight``'s ``capacity_warnings`` before it dies.
CAPACITY_WARN_PCT = 15.0

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


# --- helpers -----------------------------------------------------------------


def _claim_key(task_id: int, agent: str | None, instant: str) -> str:
    # The agent is part of the key: two agents can claim the same task within
    # one second (a takeover after a lapsed lease), and that is two runs.
    return f"selfpickup:claimed:{task_id}:{agent or '-'}:{instant}"


def _finish_key(run_id: str) -> str:
    return f"selfpickup:finished:{run_id}"


def _cancel_key(run_id: str) -> str:
    return f"selfpickup:cancel_requested:{run_id}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _wall_s(start: str | None, end: str | None) -> float | None:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return round(max(0.0, (b - a).total_seconds()), 3)


def empty_usage() -> dict[str, Any]:
    """Every usage field, typed and null: the shape when nothing was reported."""
    return {name: None for name in USAGE_FIELDS}


def merge_usage(base: dict[str, Any] | None, top: dict[str, Any] | None) -> dict[str, Any]:
    """``base`` with every non-null field of ``top`` laid over it.

    Usage snapshots are cumulative, so a newer report replaces a field rather
    than adding to it; a field the newer report leaves null keeps its value.
    """
    out = dict(base) if base else empty_usage()
    for name, value in (top or {}).items():
        if value is None:
            continue
        if name == "billing" and out.get("billing") == "api" and value != "api":
            # A run once charged as API is never exempted by a later or
            # replayed report (task #1837): a subscription snapshot replayed
            # after a mixed cumulative total must not erase accounted spend.
            continue
        if name == "capacity" and isinstance(value, dict) and isinstance(out.get(name), dict):
            # Field by field (task #1841): a partial report such as
            # {"remaining_pct": 0} must not drop an earlier limit_reached.
            # An explicit limit_reached: false still clears it.
            out[name] = {**out[name], **{k: v for k, v in value.items() if v is not None}}
            continue
        out[name] = value
    # Partial pricing never proves every invocation was priced. Preserve
    # uncertainty through heartbeat, finish and historical overlays.
    if (base or {}).get("cost_complete") is False or (top or {}).get("cost_complete") is False:
        out["cost_complete"] = False
    return out


def ensure_schema(conn: Any) -> None:
    """Create the heartbeat usage store. Idempotent.

    The DDL lives in core (``table_schemas.run_usage_heartbeats``), which
    ``storage._migrate`` runs inside ``init_db``'s transaction.
    """
    table_schemas.run_usage_heartbeats(conn)


def backfill_open_run_cache(conn: Any) -> None:
    """Post-init hook: fill ``tasks.current_run_id`` for runs claimed before it.

    A task claimed before the cache columns existed (task #1526) stays
    'doing' with an open run in event_log but ``current_run_id`` NULL, since
    record_claim's early-return paths only fire on the *next* claim or
    renewal. Bounded to 'doing' tasks with an empty cache, so a healthy,
    already-backfilled store does no work. Runs after ``storage.init_db``
    (see ``post_init``); the caller commits.
    """
    for row in conn.execute(
        "SELECT id FROM tasks WHERE status='doing' AND current_run_id IS NULL"
    ).fetchall():
        open_run = _open_run_on(conn, row["id"])
        if open_run and open_run.get("agent") and open_run.get("run_id"):
            conn.execute(
                "UPDATE tasks SET current_run_id=?, current_run_agent=? WHERE id=?",
                (open_run["run_id"], open_run["agent"], row["id"]),
            )


def _heartbeat_usages(conn: Any, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Latest heartbeat usage per run id; empty for runs that never sent one."""
    ids = [r for r in dict.fromkeys(run_ids) if r]
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        try:
            rows = conn.execute(
                "SELECT run_id, usage_json FROM run_usage_heartbeats "
                f"WHERE run_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
        except Exception:  # noqa: BLE001 - a store predating ensure_schema has no heartbeats
            return out
        for row in rows:
            try:
                parsed = json.loads(row["usage_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                out[row["run_id"]] = merge_usage(None, parsed)
    return out


def heartbeat_revisions(conn: Any) -> dict[str, str]:
    """Run id -> revision of its stored heartbeat, for the live-state poller.

    A heartbeat appends no event, so a poller that watches only the event log
    would never see one written by another process (Devin review on PR #940).
    """
    try:
        rows = conn.execute(
            "SELECT run_id, updated_at, usage_json FROM run_usage_heartbeats"
        ).fetchall()
    except Exception:  # noqa: BLE001 - a store predating ensure_schema has no heartbeats
        return {}
    return {
        row["run_id"]: hashlib.sha256(
            f"{row['updated_at']}:{row['usage_json']}".encode()
        ).hexdigest()
        for row in rows
    }


def _delete_heartbeats(conn: Any, where: str, params: tuple | list = ()) -> None:
    try:
        conn.execute(f"DELETE FROM run_usage_heartbeats WHERE {where}", params)
    except Exception:  # noqa: BLE001 - a store predating ensure_schema has no heartbeats
        pass


def drop_heartbeats_for_tasks(conn: Any, task_ids: list[int]) -> None:
    """Delete the heartbeat rows of deleted tasks. Runs in the caller's transaction."""
    ids = [int(t) for t in task_ids]
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        _delete_heartbeats(conn, f"task_id IN ({','.join('?' * len(chunk))})", chunk)


def drop_orphan_heartbeats(conn: Any) -> None:
    """Delete heartbeat rows whose claim event is gone (an event-log prune)."""
    _delete_heartbeats(
        conn,
        "run_id NOT IN (SELECT json_extract(payload, '$.run_id') FROM event_log "
        "WHERE channel=? AND json_extract(payload, '$.kind')=?)",
        (CHANNEL, KIND_CLAIMED),
    )


def record_heartbeat_usage_in_tx(
    tx: _Tx, task_id: int, *, run_id: str, agent: str, usage: dict[str, Any]
) -> dict[str, Any]:
    """Store ``usage`` as ``run_id``'s latest cumulative snapshot; return the merge.

    Runs inside ``task_claim``'s renewal transaction. No event is appended: the
    run resource is notified through :func:`flush`, and the snapshot reaches
    the event log once, on the run's finish (see :func:`_append`).
    """
    merged = merge_usage(_heartbeat_usages(tx, [run_id]).get(run_id), usage)
    tx.execute(
        "INSERT INTO run_usage_heartbeats(run_id, task_id, agent, usage_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
        "usage_json=excluded.usage_json, updated_at=excluded.updated_at",
        (run_id, int(task_id), agent, json.dumps(merged, sort_keys=True), storage.now()),
    )
    tx.info_changed_runs.append(run_id)
    return merged


def record_accounting_usage_in_tx(
    tx: _Tx,
    task_id: int,
    *,
    run_id: str,
    agent: str,
    usage: dict[str, Any],
    expected_project: str | None = None,
    expected_lane: str | None = None,
) -> dict[str, Any]:
    """Record cumulative usage on the authenticated actor's immutable run.

    Accounting neither requires nor renews a live lease. Late usage may overlay
    a finished run, but its finish event, outcome and successor stay untouched.
    Numeric totals are monotonic so replaying an older snapshot cannot erase
    spend. The hash-chained event is durable evidence; the heartbeat table is
    also the read cache for late usage on closed runs.
    """
    claim = tx.execute(
        "SELECT payload FROM event_log WHERE channel=? "
        "AND json_extract(payload,'$.kind')=? AND json_extract(payload,'$.run_id')=?",
        (CHANNEL, KIND_CLAIMED, run_id),
    ).fetchone()
    bound = _payload(claim) if claim is not None else {}
    if bound.get("task_id") != task_id or bound.get("agent") != agent:
        raise ToolError("usage accounting requires the original run's task and authenticated actor")
    if expected_project is not None and bound.get("project") != expected_project:
        raise ToolError("usage accounting project guard does not match the original run")
    if expected_lane is not None and bound.get("lane") not in (None, "default", expected_lane):
        raise ToolError("usage accounting lane guard does not match the original run")
    finish = _finishes_for(tx, [run_id]).get(run_id)
    base = merge_usage(
        finish.get("usage") if finish else None,
        _heartbeat_usages(tx, [run_id]).get(run_id),
    )
    merged = merge_usage(base, usage)
    for field in ("input_tokens", "output_tokens", "cost_usd"):
        if base.get(field) is not None and merged.get(field) is not None:
            merged[field] = max(base[field], merged[field])
    digest = hashlib.sha256(json.dumps(merged, sort_keys=True).encode()).hexdigest()
    _append(
        tx,
        {
            "kind": KIND_USAGE_REPORTED,
            "task_id": task_id,
            "run_id": run_id,
            "agent": agent,
            "usage": merged,
            "reported_at": storage.now(),
        },
        agent,
        f"selfpickup:usage:{run_id}:{digest}",
    )
    record_heartbeat_usage_in_tx(tx, task_id, run_id=run_id, agent=agent, usage=merged)
    return merged


def validate_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Check a caller-supplied ``run_usage`` and return it normalized.

    Raises ``ToolError`` on unknown keys or wrong types. This is input
    validation, done before any task write, so a bad value is rejected rather
    than silently dropped.
    """
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ToolError("run_usage must be an object")
    unknown = sorted(set(usage) - set(USAGE_FIELDS))
    if unknown:
        raise ToolError(f"run_usage has unknown field(s) {unknown}; allowed: {list(USAGE_FIELDS)}")
    out = empty_usage()
    for name, value in usage.items():
        if value is None:
            continue
        types = USAGE_FIELDS[name]
        if name in ("contended", "cost_complete"):
            if not isinstance(value, bool):
                raise ToolError(f"run_usage.{name} must be bool")
            out[name] = value
            continue
        if isinstance(value, bool) or not isinstance(value, types):
            raise ToolError(f"run_usage.{name} must be {' or '.join(t.__name__ for t in types)}")
        if name == "billing":
            value = value.strip().lower()
            if value not in BILLING_VALUES:
                raise ToolError(f"run_usage.billing must be one of {list(BILLING_VALUES)}")
            out[name] = value
            continue
        if name == "phases":
            out[name] = _validate_phases(value)
            continue
        if name == "capacity":
            out[name] = _validate_capacity(value)
            continue
        if name in ("input_tokens", "output_tokens", "cost_usd") and (
            value < 0 or not math.isfinite(value)
        ):
            raise ToolError(f"run_usage.{name} must be a finite number >= 0")
        out[name] = value.strip()[:200] if isinstance(value, str) else value
    return out


def _validate_capacity(capacity: dict[Any, Any]) -> dict[str, Any]:
    """``{limit_reached, remaining_pct, resets_at, source, detail}``, all optional."""
    unknown = sorted(str(k) for k in set(capacity) - set(CAPACITY_FIELDS))
    if unknown:
        raise ToolError(
            f"run_usage.capacity has unknown field(s) {unknown}; allowed: {list(CAPACITY_FIELDS)}"
        )
    out: dict[str, Any] = {}
    for name, value in capacity.items():
        if value is None:
            continue
        if name == "limit_reached":
            if not isinstance(value, bool):
                raise ToolError("run_usage.capacity.limit_reached must be bool")
            out[name] = value
        elif name == "remaining_pct":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                raise ToolError("run_usage.capacity.remaining_pct must be a number from 0 to 100")
            out[name] = value
        else:
            if not isinstance(value, str):
                raise ToolError(f"run_usage.capacity.{name} must be str")
            out[name] = value.strip()[:200]
    return out


def capacity_reached(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """The run's capacity signal when it says the usage limit was reached."""
    capacity = (usage or {}).get("capacity")
    if isinstance(capacity, dict) and capacity.get("limit_reached") is True:
        return capacity
    return None


def _capacity_reason(capacity: dict[str, Any], *, after: str | None = None) -> str:
    detail = capacity.get("detail") or "harness reported its usage limit reached"
    reason = f"capacity ceiling: {detail}"
    if capacity.get("resets_at"):
        reason += f" (resets {capacity['resets_at']})"
    return f"{reason}; {after}" if after else reason


def _run_capacity(conn: Any, run_id: str, usage: dict[str, Any] | None = None):
    """The run's reached-limit signal: its last heartbeat with ``usage`` over it."""
    heartbeat = _heartbeat_usages(conn, [run_id]).get(run_id)
    return capacity_reached(merge_usage(heartbeat, usage))


def _lease_lapse_reason(conn: Any, run: dict[str, Any], reason: str) -> tuple[str, str]:
    """A lease-lapse finish: the capacity ceiling when the run reported one.

    Without a recorded signal the lease variant stands; nothing is inferred
    from duration or timing.
    """
    capacity = _run_capacity(conn, run["run_id"])
    if capacity is not None:
        return _capacity_reason(capacity, after=reason), REASON_CAPACITY_CEILING
    return reason, REASON_LEASE_EXPIRED


def _validate_phases(phases: dict[Any, Any]) -> dict[str, float]:
    """``{phase: seconds}``: short string names, finite non-negative seconds."""
    if len(phases) > MAX_PHASES:
        raise ToolError(f"run_usage.phases allows at most {MAX_PHASES} phases")
    out: dict[str, float] = {}
    for key, secs in phases.items():
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 40:
            raise ToolError("run_usage.phases keys must be non-empty names of at most 40 chars")
        if isinstance(secs, bool) or not isinstance(secs, (int, float)):
            raise ToolError(f"run_usage.phases[{key!r}] must be a number of seconds")
        if not math.isfinite(secs) or secs < 0:
            raise ToolError(f"run_usage.phases[{key!r}] must be finite and >= 0")
        out[key.strip()] = round(float(secs), 3)
    return out


def safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a recorder call; never raise.

    A failed record is logged and noted in ``agent_events`` (kind
    ``run_recorder``) so the gap is inspectable. The caller's primary write has
    already committed and must not be affected.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - recorder is best-effort by contract
        task_id = kwargs.get("task_id", args[0] if args else None)
        logger.warning("run recorder failed for task %s: %s", task_id, exc)
        storage.record_agent_event(
            "run_recorder",
            ref_id=str(task_id) if task_id is not None else None,
            summary=f"run record not written: {type(exc).__name__}: {exc}"[:200],
            mirror=False,
        )
        return None


def _payload(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}


def open_run_on(conn: Any, task_id: int) -> dict[str, Any] | None:
    """The newest self-pickup run on ``task_id`` with no finish, or None.

    Read on the caller's connection, so a writer holding the lock sees the
    same answer its update will act on.
    """
    return _open_run_on(conn, task_id)


def _open_run_on(conn: Any, task_id: int) -> dict[str, Any] | None:
    """Newest claim for ``task_id`` with no finish, or None."""
    row = conn.execute(
        "SELECT c.payload, c.created_at FROM event_log c "
        "WHERE c.channel=? AND json_extract(c.payload, '$.kind')=? "
        "AND json_extract(c.payload, '$.task_id')=? "
        "AND NOT EXISTS (SELECT 1 FROM event_log f WHERE f.dedupe_key = "
        "'selfpickup:finished:' || json_extract(c.payload, '$.run_id')) "
        "ORDER BY c.ulid DESC LIMIT 1",
        (CHANNEL, KIND_CLAIMED, int(task_id)),
    ).fetchone()
    return _payload(row) if row else None


def _trace(conn: Any, task_id: int, summary: str) -> None:
    """Queue a ``run_recorder`` note; written after the recorder's transaction."""
    conn.info_traces.append((task_id, summary[:200]))


def _bind_current_run(
    conn: Any, task_id: int, run_id: str, agent: str, *, stamp: bool = False
) -> None:
    """Point ``tasks.current_run_id`` at the freshly opened run (task #1526).

    A cheap, indexed cache of "this task's open self-pickup run", written in
    the same transaction as the claim event above. usage_ledger.resolve_run_id
    reads this column to attribute provider usage to a run in O(1), instead of
    scanning event_log. Always overwritten unconditionally: whatever run this
    task previously cached (closed, abandoned, or none) is superseded the
    moment a new one opens.

    ``cancel_requested`` is reset only when the run id changes. A renewal of
    the same run must not erase a cancellation already requested for it.

    ``stamp`` is for a transaction of the recorder's own (the standalone
    ``record_claim`` and its renewal backfill): no caller write stamps the row
    there, so a cache change carries its own revision, and only an actual
    change does (task #2087). In a caller's transaction the caller's own write
    stamps the revision.
    """
    if stamp:
        conn.execute(
            "UPDATE tasks SET current_run_id=?, current_run_agent=?, "
            "cancel_requested=CASE WHEN current_run_id=? THEN cancel_requested ELSE 0 END, "
            "updated_at=? "
            "WHERE id=? AND (current_run_id IS NOT ? OR current_run_agent IS NOT ?)",
            (
                run_id,
                agent,
                run_id,
                storage.next_task_revision(conn, task_id),
                int(task_id),
                run_id,
                agent,
            ),
        )
        return
    conn.execute(
        "UPDATE tasks SET current_run_id=?, current_run_agent=?, "
        "cancel_requested=CASE WHEN current_run_id=? THEN cancel_requested ELSE 0 END "
        "WHERE id=?",
        (run_id, agent, run_id, int(task_id)),
    )


def _unbind_current_run(conn: Any, task_id: int, run_id: str, *, stamp: bool = False) -> None:
    """Clear the cache when ``run_id`` closes, but only if it is still current.

    The ``AND current_run_id=?`` guard means a run that was already
    superseded by a newer claim (this row is stale by the time its finish is
    recorded) cannot clobber that newer run's cache entry. ``cancel_requested``
    is cleared with it (task #1525): once no run is current, no flag is live.

    ``stamp`` as for :func:`_bind_current_run`: ``record_abandoned`` and the
    standalone ``record_transition`` run after the release committed, so the
    cleared cache and cancel flag carry their own revision (task #2087).
    """
    if stamp:
        conn.execute(
            "UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL, cancel_requested=0, "
            "updated_at=? WHERE id=? AND current_run_id=?",
            (storage.next_task_revision(conn, task_id), int(task_id), run_id),
        )
        return
    conn.execute(
        "UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL, cancel_requested=0 "
        "WHERE id=? AND current_run_id=?",
        (int(task_id), run_id),
    )


def _claim_is_current(conn: Any, task_id: int, agent: str) -> bool:
    row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row is not None and row["status"] == "doing" and row["claimed_by"] == agent


def _transition_is_current(
    conn: Any,
    task_id: int,
    *,
    revision: str | None,
    old_status: str | None,
    new_status: str | None,
    claimed_by: str | None,
    released: bool,
) -> bool:
    """A notes edit may advance the row revision without changing its lifecycle."""
    row = conn.execute(
        "SELECT status, claimed_by, updated_at FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    expected_status = new_status if new_status is not None else old_status
    if row["status"] != expected_status:
        return False
    if released and row["claimed_by"] is not None:
        return False
    if new_status == "doing" and row["claimed_by"] != claimed_by:
        return False
    if revision is not None and new_status is not None and new_status != old_status:
        # Notes edits do not emit a lifecycle event. A later status cycle can
        # return to the same final state, so the row alone cannot distinguish
        # that newer transition from this delayed recorder call.
        latest = conn.execute(
            "SELECT json_extract(payload, '$.updated_at') AS revision "
            "FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.task_id')=? "
            "ORDER BY ROWID DESC LIMIT 1",
            (events.CHANNEL_TASK_UPDATED, task_id),
        ).fetchone()
        if latest is not None and latest["revision"] != revision:
            return False
    if revision is not None and row["updated_at"] != revision:
        # Only accept a newer row with the same lifecycle. A fabricated or
        # older revision must not bypass the stale-transition guard.
        current_at, transition_at = _parse_ts(row["updated_at"]), _parse_ts(revision)
        return current_at is not None and transition_at is not None and current_at > transition_at
    return True


def _latest_run_outcome(conn: Any, task_id: int) -> str | None:
    """Outcome of the task's most recent run; ``"open"`` if unfinished, None if none."""
    row = conn.execute(
        "SELECT json_extract(payload, '$.run_id') AS run_id FROM event_log "
        "WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.task_id')=? ORDER BY ulid DESC LIMIT 1",
        (CHANNEL, KIND_CLAIMED, int(task_id)),
    ).fetchone()
    if row is None or not row["run_id"]:
        return None
    fin = _finishes_for(conn, [row["run_id"]]).get(row["run_id"])
    return fin.get("outcome") if fin else "open"


def latest_run_end(conn: Any, task_id: int) -> dict[str, Any] | None:
    """The finish record of the task's most recent run; None if none or still open."""
    row = conn.execute(
        "SELECT json_extract(payload, '$.run_id') AS run_id FROM event_log "
        "WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.task_id')=? ORDER BY ulid DESC LIMIT 1",
        (CHANNEL, KIND_CLAIMED, int(task_id)),
    ).fetchone()
    if row is None or not row["run_id"]:
        return None
    return _finishes_for(conn, [row["run_id"]]).get(row["run_id"])


def _coverage_run_provenance(conn: Any, task_id: int) -> dict[str, Any]:
    """Explain the latest retained claim without changing writer outcome reads."""
    claim_row = conn.execute(
        "SELECT ulid, created_at, payload, json_extract(payload, '$.run_id') AS lookup_run_id "
        "FROM event_log "
        "WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.task_id')=? ORDER BY ulid DESC LIMIT 1",
        (CHANNEL, KIND_CLAIMED, int(task_id)),
    ).fetchone()
    claim = _payload(claim_row) if claim_row else None
    raw_run_id = claim.get("run_id") if claim else None
    # SQLite normalizes JSON booleans to integers; preserve the legacy lookup
    # exactly while keeping the raw recorded value in the provenance projection.
    run_id = claim_row["lookup_run_id"] if claim_row else None
    finish_row = None
    finish = None
    if run_id:
        finish_row = conn.execute(
            "SELECT ulid, created_at, payload FROM event_log WHERE dedupe_key=?",
            (_finish_key(run_id),),
        ).fetchone()
        candidate = _payload(finish_row) if finish_row else None
        # Match _finishes_for's payload-run-ID lookup, including legacy data.
        if candidate and candidate.get("run_id") == run_id:
            finish = candidate
        else:
            finish_row = None
    outcome = (finish.get("outcome") if finish else "open") if run_id else None
    if outcome in _COVERING_OUTCOMES:
        reason = None
    elif claim_row is None:
        reason = "no_recorded_claim_event"
    elif finish is None and (not isinstance(raw_run_id, str) or not raw_run_id):
        # A legacy non-string run ID whose finish still matched is labelled by
        # that finish's outcome below (task #1887); only an unmatched one is invalid.
        reason = "invalid_recorded_claim"
    elif outcome in {"released", "blocked", "abandoned", "cancelled", "open"}:
        reason = f"latest_run_{outcome}"
    else:
        reason = "unknown_recorded_outcome"
    return {
        "outcome": outcome,
        "reason": reason,
        "latest_claim": (
            {
                "event_ulid": claim_row["ulid"],
                "event_at": claim_row["created_at"],
                **{
                    key: claim.get(key)
                    for key in ("run_id", "agent", "claimed_at", "claim_source", "observed_from")
                },
            }
            if claim_row is not None
            else None
        ),
        "matched_finish": _coverage_finish_provenance(finish_row) if finish_row else None,
    }


def _coverage_finish_provenance(row: Any) -> dict[str, Any]:
    payload = _payload(row)
    return {
        "event_ulid": row["ulid"],
        "event_at": row["created_at"],
        **{
            key: payload.get(key)
            for key in (
                "run_id",
                "outcome",
                "reason",
                "finished_at",
                "finished_by",
                "claim_recorded",
            )
        },
    }


def _append(conn: Any, payload: dict[str, Any], originator: str | None, key: str) -> bool:
    if payload.get("kind") == KIND_FINISHED and payload.get("run_id"):
        heartbeat = _heartbeat_usages(conn, [payload["run_id"]]).get(payload["run_id"])
        if heartbeat is not None:
            # Every finish path (transition, takeover, abandon, direct repair)
            # carries the run's last heartbeat; a finishing report wins per field.
            reported = payload.get("usage") if payload.get("usage_reported") else None
            payload["usage"] = merge_usage(heartbeat, reported)
            payload["usage_reported"] = True
    _, inserted, published = events.record_event_once_on_connection(
        conn, CHANNEL, payload=payload, originator=originator, dedupe_key=key
    )
    if payload.get("kind") == KIND_FINISHED and payload.get("run_id"):
        # The finish event now carries the snapshot; the row has no reader left.
        _delete_heartbeats(conn, "run_id=?", (payload["run_id"],))
    if inserted and published is not None:
        conn.info_pending_publish.append(published)
        if payload.get("run_id"):
            conn.info_changed_runs.append(payload["run_id"])
    return inserted


def _finish_payload(
    run: dict[str, Any] | None,
    *,
    task_id: int,
    outcome: str,
    to_status: str | None,
    finished_by: str | None,
    finished_at: str,
    usage: dict[str, Any] | None,
    reason: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": KIND_FINISHED,
        "run_id": run["run_id"] if run else None,
        "task_id": int(task_id),
        "agent": run["agent"] if run else None,
        "claim_recorded": run is not None,
        "outcome": outcome,
        "to_status": to_status,
        "finished_by": finished_by,
        "finished_at": finished_at,
        "wall_s": _wall_s(run["claimed_at"], finished_at) if run else None,
        "usage": usage if usage is not None else empty_usage(),
        "usage_reported": usage is not None,
        "reason": reason,
        "reason_code": reason_code,
    }


class _Tx:
    """A connection plus the socket events to publish once it commits."""

    def __init__(self, conn: Any):
        self._conn = conn
        self.info_pending_publish: list[dict[str, Any]] = []
        self.info_changed_runs: list[str] = []
        self.info_traces: list[tuple[int, str]] = []
        # Tasks whose completion return (#1751) was queued in this transaction.
        self.info_completion_returns: list[int] = []
        # Ready-dependent events (#2454) written in this transaction, to send
        # once it commits.
        self.info_ready_notices: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def attach(conn: Any) -> _Tx:
    """Wrap an already-open write connection so a run-record write can run

    inside the *caller's* transaction (task #1525), instead of best-effort in
    a transaction of its own after the caller has already committed. The
    caller must have issued ``BEGIN IMMEDIATE`` (or equivalent) itself; this
    module never opens or closes the connection it is handed.

    Call :func:`flush` once the caller's own transaction has committed, to
    publish/notify/trace the effects this attached write queued. A caller
    that rolls back its transaction must not call :func:`flush` — the queued
    effects describe a write that never happened.
    """
    return conn if isinstance(conn, _Tx) else _Tx(conn)


def flush(tx: _Tx) -> None:
    """Publish, notify and trace the effects an :func:`attach`-ed write queued.

    Call this after the caller's own commit succeeds. Every step here is
    best-effort (mirrors :func:`_tx`'s own post-commit flush): a failure to
    publish or notify must never look like the write itself failed, since by
    the time this runs, it already durably committed.
    """
    for published in tx.info_pending_publish:
        try:
            events.publish(published)
        except Exception:  # noqa: BLE001 - fan-out is best-effort
            pass
    for task_id, summary in tx.info_traces:
        try:
            storage.record_agent_event(
                "run_recorder", ref_id=str(task_id), summary=summary, mirror=False
            )
        except Exception:  # noqa: BLE001 - post-commit tracing is best-effort
            logger.exception("run recorder trace failed for task %s", task_id)
    if tx.info_changed_runs:
        try:
            from . import change_feed

            change_feed.runs_changed(tx.info_changed_runs)
        except Exception:  # noqa: BLE001 - notification is best-effort
            pass
    for task_id in dict.fromkeys(tx.info_completion_returns):
        # Never raises; a no-op when no chat surface is configured.
        completion_return.schedule_drain(task_id=task_id)
    if tx.info_ready_notices:
        # Never raises. What it cannot send stays pending for the hygiene tick.
        ready_dependents.drain_pending(ulids=tx.info_ready_notices)


def safe_in_tx(fn: Callable[..., Any], tx: _Tx, task_id: Any, *args: Any, **kwargs: Any) -> Any:
    """Like :func:`safe`, but for a write already running inside ``tx``.

    A failure cannot be recorded via a fresh connection here — ``tx``'s
    transaction is still open, and a second connection trying to write would
    either block on or bypass that lock. Instead the failure note is queued
    onto ``tx.info_traces``, exactly like the in-band ``_trace`` calls the
    writers already make, and is written out by :func:`flush` after commit.
    An operating authority denial is part of primary write admission and
    propagates after rolling back the recorder savepoint.
    """
    pending = len(tx.info_pending_publish)
    changed = len(tx.info_changed_runs)
    traces = len(tx.info_traces)
    returns = len(tx.info_completion_returns)
    ready = len(tx.info_ready_notices)
    tx.execute("SAVEPOINT run_recorder_safe")
    try:
        result = fn(tx, task_id, *args, **kwargs)
        tx.execute("RELEASE SAVEPOINT run_recorder_safe")
        return result
    except Exception as exc:  # noqa: BLE001 - recorder is best-effort by contract
        tx.execute("ROLLBACK TO SAVEPOINT run_recorder_safe")
        tx.execute("RELEASE SAVEPOINT run_recorder_safe")
        del tx.info_pending_publish[pending:]
        del tx.info_changed_runs[changed:]
        del tx.info_traces[traces:]
        del tx.info_completion_returns[returns:]
        del tx.info_ready_notices[ready:]
        if isinstance(exc, operating_profiles.OperatingAuthorityDenied):
            # A tenant/authority refusal is an admission failure, not optional
            # telemetry. Let task_claim roll back its claim as well.
            raise
        logger.warning("run recorder failed for task %s: %s", task_id, exc)
        tx.info_traces.append(
            (task_id, f"run record not written: {type(exc).__name__}: {exc}"[:200])
        )
        return None


def _tx(fn: Callable[[Any], Any]) -> Any:
    raw = storage.connect()
    conn = _Tx(raw)
    try:
        raw.execute("BEGIN IMMEDIATE")
        result = fn(conn)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    flush(conn)
    return result


# --- writers -----------------------------------------------------------------


def run_originator(conn: Any, task_id: int, agent: str, *, active_only: bool = True) -> str | None:
    """Linked delegation attribution comes from hub rows, never client payloads."""
    status_filter = "AND status IN ('pending', 'in_flight') " if active_only else ""
    row = conn.execute(
        "SELECT from_agent FROM delegations WHERE task_id=? AND to_agent=? "
        + status_filter
        + "ORDER BY id DESC LIMIT 1",
        (int(task_id), agent),
    ).fetchone()
    return row["from_agent"] if row is not None else None


def native_execution_status(agent: str | None, originator: str | None = None) -> dict[str, Any]:
    """Live hub authority, distinct from an immutable historical snapshot.

    A broken policy must stop native execution while cancellation and history
    remain readable. The requesting viewer cannot alter the run's identities.
    """
    try:
        selected = trust_presets.snapshot(agent)
        originating = trust_presets.snapshot(originator) if originator else None
        if selected is not None or originating is not None:
            return {
                "native_execution_allowed": False,
                "native_execution_denial": "trust preset requires unsupported native containment",
            }
        return {"native_execution_allowed": True}
    except ToolError as exc:
        return {"native_execution_allowed": False, "native_execution_denial": str(exc)}


def _add_live_native_status(conn: Any, runs: list[dict[str, Any]]) -> None:
    for run in runs:
        originator = run.get("originator_agent")
        if run.get("native_authority_version") != 1:
            # Legacy events lack captured attribution. New events explicitly
            # distinguish no originating delegation from missing old evidence.
            originator = originator or run_originator(
                conn, run["task_id"], run.get("agent"), active_only=False
            )
        run.update(native_execution_status(run.get("agent"), originator))


def record_claim(
    task_id: int,
    *,
    agent: str,
    claimed_at: str,
    lock_expires_at: str | None = None,
    source: str = "task_claim",
    lane: str | None = None,
    project: str | None = None,
    renewal: bool = False,
    revision: str | None = None,
    prior_status: str | None = None,
    trust_snapshot: Any = _TRUST_UNSET,
    originator_snapshot: Any = _TRUST_UNSET,
    originator_agent: str | None = None,
) -> str | None:
    """Open a run for ``agent`` on ``task_id``; return its run id.

    A live renewal by the agent that already holds an open run writes nothing.
    A renewal with no open run (a claim made before this recorder deployed, or
    one whose claim write failed) opens a run marked ``observed_from:
    "renewal"``: it is recorded from the moment it was seen, using the task
    row's own ``claimed_at``, never backdated further.

    When a run is still open after a lapsed lease, the prior run is closed
    ``abandoned`` even if the same agent claims again.
    """
    agent = (agent or "").strip() or None
    if agent is None:
        return None
    if trust_snapshot is _TRUST_UNSET:
        trust_snapshot = trust_presets.snapshot(agent)
    if renewal:
        # The executor renews every poll tick; answer the common case with a
        # read and take the write lock only when a run must be opened.
        conn = storage.connect()
        try:
            held = _open_run_on(conn, task_id)
            cached = conn.execute(
                "SELECT current_run_id, current_run_agent FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
        finally:
            conn.close()
        if held is not None and held.get("agent") == agent:
            if cached is not None and (
                cached["current_run_id"] != held["run_id"] or cached["current_run_agent"] != agent
            ):
                # Backfill the cache for a run claimed before it existed, or
                # one whose original bind failed (task #1526). Rare, so it
                # takes the write lock and re-reads the run there: a finish
                # committed between the read above and this write must not
                # have its cleared cache restored to the closed run.
                def backfill(tx: Any) -> None:
                    current = _open_run_on(tx, task_id)
                    if current is not None and current.get("run_id") == held["run_id"]:
                        _bind_current_run(tx, task_id, held["run_id"], agent, stamp=True)

                _tx(backfill)
            return held["run_id"]

    def write(conn: Any) -> str | None:
        return _write_claim(
            conn,
            task_id,
            agent=agent,
            claimed_at=claimed_at,
            lock_expires_at=lock_expires_at,
            source=source,
            lane=lane,
            project=project,
            renewal=renewal,
            revision=revision,
            prior_status=prior_status,
            trust_snapshot=trust_snapshot,
            originator_snapshot=originator_snapshot,
            originator_agent=originator_agent,
            stamp=True,
        )

    return _tx(write)


def record_claim_in_tx(
    tx: _Tx,
    task_id: int,
    *,
    agent: str,
    claimed_at: str,
    lock_expires_at: str | None = None,
    source: str = "task_claim",
    lane: str | None = None,
    project: str | None = None,
    renewal: bool = False,
    revision: str | None = None,
    prior_status: str | None = None,
    trust_snapshot: Any = _TRUST_UNSET,
    originator_snapshot: Any = _TRUST_UNSET,
    originator_agent: str | None = None,
) -> str | None:
    """``record_claim``'s write, run inside the caller's own transaction.

    Task #1525: ``task_claim`` already holds ``BEGIN IMMEDIATE`` on ``tx`` for
    its own row update, so there is nothing to gain from the standalone
    entrypoint's renewal fast path (a second, lock-free connection) here — it
    would only add a read against a connection that is about to take the
    write lock anyway. This always runs the full write, atomically with the
    caller's task row change: a crash between the two can no longer leave a
    claimed task with an unopened run, or vice versa.
    """
    agent = (agent or "").strip() or None
    if agent is None:
        return None
    return _write_claim(
        tx,
        task_id,
        agent=agent,
        claimed_at=claimed_at,
        lock_expires_at=lock_expires_at,
        source=source,
        lane=lane,
        project=project,
        renewal=renewal,
        revision=revision,
        prior_status=prior_status,
        trust_snapshot=trust_snapshot,
        originator_snapshot=originator_snapshot,
        originator_agent=originator_agent,
    )


def _write_claim(
    conn: Any,
    task_id: int,
    *,
    agent: str,
    claimed_at: str,
    lock_expires_at: str | None,
    source: str,
    lane: str | None,
    project: str | None,
    renewal: bool,
    revision: str | None,
    prior_status: str | None,
    trust_snapshot: Any = _TRUST_UNSET,
    originator_snapshot: Any = _TRUST_UNSET,
    originator_agent: str | None = None,
    stamp: bool = False,
) -> str | None:
    if trust_snapshot is _TRUST_UNSET:
        trust_snapshot = trust_presets.snapshot(agent)
    originator_agent = originator_agent or run_originator(conn, task_id, agent)
    if originator_snapshot is _TRUST_UNSET:
        originator_snapshot = trust_presets.snapshot(originator_agent) if originator_agent else None
    now = storage.now()
    if not _claim_is_current(conn, task_id, agent):
        # The task moved on (released, re-claimed) between the claim's
        # commit and this write. Recording it now would reverse ownership.
        _trace(conn, task_id, "claim superseded before it was recorded")
        return None
    prior = _open_run_on(conn, task_id)
    if prior is not None:
        if renewal and prior.get("agent") == agent:
            # Same backfill as the renewal fast-path above: this run may
            # predate the cache (task #1526 follow-up, Devin review of PR
            # #875). Already inside the write transaction, so an
            # unconditional overwrite is as cheap as the guarded version.
            _bind_current_run(conn, task_id, prior["run_id"], agent, stamp=stamp)
            return prior["run_id"]
        if prior_status == "doing":
            reason, reason_code = _lease_lapse_reason(conn, prior, "claimed after the lease lapsed")
        else:
            reason = "closed by the next claim; its own finish was not recorded"
            reason_code = None
        _append(
            conn,
            _finish_payload(
                prior,
                task_id=task_id,
                outcome="abandoned",
                to_status="doing",
                finished_by=agent,
                finished_at=now,
                usage=None,
                reason=reason,
                reason_code=reason_code,
            ),
            agent,
            _finish_key(prior["run_id"]),
        )
    run_id = RUN_ID_PREFIX + events.new_ulid()
    payload = {
        "kind": KIND_CLAIMED,
        "run_id": run_id,
        "task_id": int(task_id),
        "agent": agent,
        "claim_source": source,
        "observed_from": "renewal" if renewal else "claim",
        "claimed_at": claimed_at or now,
        "recorded_at": now,
        "native_authority_version": 1,
        "lock_expires_at": lock_expires_at,
        "lane": lane,
        "project": project,
    }
    if trust_snapshot is not None:
        payload["trust_preset"] = trust_snapshot
    if originator_agent is not None:
        payload["originator_agent"] = originator_agent
    if originator_snapshot is not None:
        payload["originator_trust_preset"] = originator_snapshot
    # The run keeps the operating posture it started with (task #1732).
    operating = operating_profiles.snapshot_for_claim(conn, int(task_id), agent)
    if operating is not None:
        payload["operating_profile"] = operating
    # The task revision, not the second-resolution claim time, keys the
    # claim: a release and re-claim within one second are two runs.
    key = _claim_key(task_id, agent, revision or run_id)
    inserted = _append(conn, payload, agent, key)
    if inserted:
        _bind_current_run(conn, task_id, run_id, agent, stamp=stamp)
        # Record unprofiled runs too: a new claim supersedes older keyed
        # snapshots, including when the operating block has been removed.
        operating_policy.record_snapshot(conn, run_id, int(task_id), agent, operating or {})
    return run_id if inserted else None


def record_transition(
    task_id: int,
    *,
    old_status: str | None,
    new_status: str | None,
    actor: str | None,
    claimed_by: str | None,
    revision: str | None = None,
    released: bool = False,
    usage: dict[str, Any] | None = None,
    lane: str | None = None,
    project: str | None = None,
) -> dict[str, Any] | None:
    """Record what a ``task_update`` did to the task's run.

    * entering ``doing`` with no open run opens one (``claim_source:
      "task_update"``), which covers agents that skip ``task_claim``;
    * leaving ``doing``, or releasing the claim, closes the open run with the
      outcome for the new status;
    * a completion or hand-off with no open run writes a finish with
      ``claim_recorded: false`` so the gap is counted, not hidden.
    """
    changed = new_status is not None and new_status != old_status
    if not changed and not released:
        return None

    def write(conn: Any) -> dict[str, Any] | None:
        return _write_transition(
            conn,
            task_id,
            old_status=old_status,
            new_status=new_status,
            actor=actor,
            claimed_by=claimed_by,
            revision=revision,
            released=released,
            usage=usage,
            lane=lane,
            project=project,
            changed=changed,
            stamp=True,
        )

    return _tx(write)


def record_transition_in_tx(
    tx: _Tx,
    task_id: int,
    *,
    old_status: str | None,
    new_status: str | None,
    actor: str | None,
    claimed_by: str | None,
    revision: str | None = None,
    released: bool = False,
    usage: dict[str, Any] | None = None,
    lane: str | None = None,
    project: str | None = None,
    attribute_actor: bool = True,
    cancel_acknowledged: bool = False,
) -> dict[str, Any] | None:
    """``record_transition``'s write, run inside the caller's own transaction.

    ``cancel_acknowledged`` (task #1747) marks a move to ``cancelled`` that
    acknowledges a standing cancellation request on the run it closes (the
    executor's fenced ``expected_run_id`` cancel). The finish then carries
    ``cancel_acknowledged: true``, so a later reader can tell that cancel apart
    from one the claimant decided on its own.

    ``attribute_actor=False`` (task #1886) opens a run on entering ``doing``
    under the claimant only, never under ``actor``: an unverified caller's
    ``actor`` is a claim about identity, not a verified one, so it must not
    charge that label's per-agent WIP. ``actor`` is still recorded as
    ``opened_by``.

    Task #1525: ``task_update`` already holds ``BEGIN IMMEDIATE`` on ``tx``
    for its own row update. Recording the run transition there too closes the
    gap where a crash (or a concurrent write squeezed between the two
    separate transactions) could commit the task's new status without ever
    recording what happened to its run, or vice versa.
    """
    changed = new_status is not None and new_status != old_status
    if not changed and not released:
        return None
    if changed:
        queue_completion_return(
            tx, task_id, old_status=old_status, new_status=new_status, revision=revision
        )
    return _write_transition(
        tx,
        task_id,
        old_status=old_status,
        new_status=new_status,
        actor=actor,
        claimed_by=claimed_by,
        revision=revision,
        released=released,
        usage=usage,
        lane=lane,
        project=project,
        changed=changed,
        attribute_actor=attribute_actor,
        cancel_acknowledged=cancel_acknowledged,
    )


def queue_completion_return(
    conn: Any,
    task_id: int,
    *,
    old_status: str | None,
    new_status: str | None,
    revision: str | None = None,
) -> bool:
    """Queue (or supersede) the task's completion return in this transaction (#1751).

    The one outbox hook for every status writer: ``record_transition_in_tx``
    calls it, so ``task_update`` and the merge closeout queue here, and
    ``complete_direct`` (close-on-merge, the merge orchestrator, the reviewer
    auto-close) calls it from its own write. ``revision`` defaults to the
    task's ``updated_at`` as written in this transaction; ``(task_id,
    revision)`` is unique, so a repeated call for one transition queues once.
    The drain is scheduled by :func:`flush` after commit.

    The same hook records the dependents a close makes ready (#2454,
    ``ready_dependents``), so every closer reaches that too.
    """
    if new_status is None or new_status == old_status:
        return False
    if revision is None:
        row = conn.execute("SELECT updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return False
        revision = row["updated_at"]
    ready = ready_dependents.record_on_connection(
        conn,
        task_id=int(task_id),
        old_status=old_status,
        new_status=new_status,
        revision=revision,
    )
    if ready and isinstance(conn, _Tx):
        for ulid, published in ready:
            conn.info_ready_notices.append(ulid)
            if published is not None:
                conn.info_pending_publish.append(published)
    queued = completion_return.record_transition_on_connection(
        conn,
        task_id=int(task_id),
        old_status=old_status,
        new_status=new_status,
        revision=revision,
    )
    if queued and isinstance(conn, _Tx):
        conn.info_completion_returns.append(int(task_id))
    return queued


def _write_transition(
    conn: Any,
    task_id: int,
    *,
    old_status: str | None,
    new_status: str | None,
    actor: str | None,
    claimed_by: str | None,
    revision: str | None,
    released: bool,
    usage: dict[str, Any] | None,
    lane: str | None,
    project: str | None,
    changed: bool,
    attribute_actor: bool = True,
    cancel_acknowledged: bool = False,
    stamp: bool = False,
) -> dict[str, Any] | None:
    now = storage.now()
    if not _transition_is_current(
        conn,
        task_id,
        revision=revision,
        old_status=old_status,
        new_status=new_status,
        claimed_by=claimed_by,
        released=released,
    ):
        # A later write already changed the task; this transition is stale
        # and applying it now would close or open the wrong run.
        _trace(conn, task_id, "transition superseded before it was recorded")
        return None
    run = _open_run_on(conn, task_id)
    if changed and new_status == "doing":
        agent = (claimed_by or (actor if attribute_actor else None) or "").strip() or None
        if run is not None:
            if old_status == "doing" and run.get("agent") == agent:
                return {"run_id": run["run_id"], "action": "already_open"}
            # An open run while the task was not 'doing' lost its finish
            # (a bypass path or a failed write). It is not this run.
            _append(
                conn,
                _finish_payload(
                    run,
                    task_id=task_id,
                    outcome="abandoned",
                    to_status="doing",
                    finished_by=actor,
                    finished_at=now,
                    usage=None,
                    reason="task re-entered doing; this run's finish was not recorded",
                ),
                actor,
                _finish_key(run["run_id"]),
            )
        run_id = RUN_ID_PREFIX + events.new_ulid()
        payload = {
            "kind": KIND_CLAIMED,
            "run_id": run_id,
            "task_id": int(task_id),
            "agent": agent,
            "claim_source": "task_update",
            "opened_by": actor,
            "observed_from": "claim",
            "claimed_at": now,
            "recorded_at": now,
            "lock_expires_at": None,
            "lane": lane,
            "project": project,
        }
        _append(conn, payload, actor, _claim_key(task_id, agent, revision or now))
        _bind_current_run(conn, task_id, run_id, agent, stamp=stamp)
        return {"run_id": run_id, "action": "opened"}

    if changed:
        outcome = _STATUS_OUTCOME.get(new_status or "", "released")
        to_status = new_status
    else:
        outcome, to_status = "released", old_status
    if run is None:
        if not (changed and new_status in _UNMATCHED_FINISH_STATUSES):
            return None
        if _latest_run_outcome(conn, task_id) == "handed_off":
            # The run already ended at review; the later completion is task
            # state, not a second run and not a coverage gap. Any other
            # prior outcome (released, blocked, abandoned...) means this
            # completion had no recorded claim of its own.
            return None
        key = _finish_key(f"none:{task_id}:{revision or now}")
        _append(
            conn,
            _finish_payload(
                None,
                task_id=task_id,
                outcome=outcome,
                to_status=to_status,
                finished_by=actor,
                finished_at=now,
                usage=usage,
                reason="no claim was recorded for this run",
            ),
            actor,
            key,
        )
        return {"run_id": None, "action": "unmatched_finish"}
    if claimed_by and run.get("agent") != claimed_by:
        _trace(conn, task_id, "transition claimant differs from open run")
        return None
    if revision is not None:
        claim_at, transition_at = _parse_ts(run.get("claimed_at")), _parse_ts(revision)
        if claim_at is not None and transition_at is not None and claim_at > transition_at:
            _trace(conn, task_id, "transition predates open run")
            return None
    reason_code = None
    if old_status != "doing" and not released:
        outcome = "abandoned"
        reason = "finish not recorded"
    else:
        reason = None
        # Task #1841: a run that gives up its task after reporting a reached
        # usage limit ended at the capacity ceiling. Delivery outcomes and
        # cancellation are never relabelled.
        capacity = (
            _run_capacity(conn, run["run_id"], usage)
            if outcome in ("released", "blocked")
            else None
        )
        if capacity is not None:
            reason, reason_code = _capacity_reason(capacity), REASON_CAPACITY_CEILING
    finish = _finish_payload(
        run,
        task_id=task_id,
        outcome=outcome,
        to_status=to_status,
        finished_by=actor,
        finished_at=now,
        usage=usage,
        reason=reason,
        reason_code=reason_code,
    )
    if cancel_acknowledged and outcome == "cancelled":
        finish["cancel_acknowledged"] = True
    _append(conn, finish, actor, _finish_key(run["run_id"]))
    _unbind_current_run(conn, task_id, run["run_id"], stamp=stamp)
    return {"run_id": run["run_id"], "action": "closed", "outcome": outcome}


def record_abandoned(
    task_id: int,
    *,
    agent: str | None,
    closed_by: str,
    reason: str,
    reason_code: str | None = None,
    to_status: str = "open",
) -> str | None:
    """Close ``agent``'s released run on ``task_id`` as ``abandoned``; return its id.

    ``to_status`` is where the release left the task: ``open``, or ``blocked``
    when the lease reaper held interrupted work (task #2395).

    For the paths that release a lapsed claim by direct SQL rather than
    ``task_update``: queue hygiene, the doctor's stale-lock reclaim and the
    executor's startup recovery. Call it after the release has committed.

    ``reason_code=REASON_LEASE_EXPIRED`` marks a lease-lapse release. When the
    run's last heartbeat reported a reached usage limit, the finish records
    ``capacity_ceiling`` instead (task #1841); other codes are kept as given.

    Another claim can commit between the release and this write. So the open
    run is closed only when it belongs to the released claimant and the task is
    still unclaimed; a newer run (another agent's, or the same agent's re-claim)
    is never touched.
    """

    def write(conn: Any) -> str | None:
        run = _open_run_on(conn, task_id)
        if run is None or not agent or run.get("agent") != agent:
            return None
        row = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is not None and row["claimed_by"]:
            return None
        why, code = reason, reason_code
        if reason_code == REASON_LEASE_EXPIRED:
            why, code = _lease_lapse_reason(conn, run, reason)
        _append(
            conn,
            _finish_payload(
                run,
                task_id=task_id,
                outcome="abandoned",
                to_status=to_status,
                finished_by=closed_by,
                finished_at=storage.now(),
                usage=None,
                reason=why,
                reason_code=code,
            ),
            closed_by,
            _finish_key(run["run_id"]),
        )
        # Its own transaction, after the release committed (task #2087).
        _unbind_current_run(conn, task_id, run["run_id"], stamp=True)
        return run["run_id"]

    return _tx(write)


class DirectCompletionChanged(RuntimeError):
    """Transient orchestration identity mismatch; a fresh review may retry."""


# Internal orchestration scope, carried through tools.git without widening its API.
_direct_completion_scope: ContextVar[dict[str, Any] | None] = ContextVar(
    "direct_completion_scope",
    default=None,
)


@contextmanager
def direct_completion_scope(expected: dict[str, Any]):
    token = _direct_completion_scope.set(expected)
    try:
        yield
    finally:
        _direct_completion_scope.reset(token)


def scoped_direct_completion(task_id: int) -> dict[str, Any] | None:
    expected = _direct_completion_scope.get()
    return expected if expected is not None and expected["id"] == task_id else None


_COMPLETION_FIELDS = (
    "id",
    "status",
    "updated_at",
    "claimed_by",
    "current_run_id",
    "current_run_agent",
    "cancel_requested",
    "notes",
)


def _direct_completion_snapshot_on(conn: Any, row: Any) -> dict[str, Any]:
    """Snapshot a row and its ledger on the caller's owning transaction."""
    run = _open_run_on(conn, row["id"])
    return {
        **{key: row[key] for key in _COMPLETION_FIELDS},
        "open_run_id": run["run_id"] if run else None,
    }


def capture_direct_completion(task_id: int, observed: Any = None) -> dict[str, Any] | None:
    """Capture revision and ledger run in one read snapshot, before orchestration.

    An already-read orchestration row must still match; never recapture a successor.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or (
            observed is not None and any(row[key] != observed[key] for key in _COMPLETION_FIELDS)
        ):
            return None
        return _direct_completion_snapshot_on(conn, row)
    finally:
        conn.close()


def append_scoped_direct_note(task_id: int, line: str) -> bool:
    """CAS this exact own write and advance only its revision in the inherited scope."""
    expected = scoped_direct_completion(task_id)
    if expected is None:
        return False

    def write(conn: Any) -> dict | None:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or any(row[key] != expected[key] for key in _COMPLETION_FIELDS):
            return None
        run = _open_run_on(conn, task_id)
        if (run["run_id"] if run else None) != expected["open_run_id"] or row["cancel_requested"]:
            return None
        revision = storage.next_revision(row["updated_at"])
        notes = (row["notes"] or "") + "\n" + line
        conn.execute(
            "UPDATE tasks SET notes=?, updated_at=? WHERE id=?", (notes, revision, task_id)
        )
        # No successor capture: only the two fields written above can advance.
        return {**expected, "updated_at": revision, "notes": notes}

    advanced = _tx(write)
    if advanced is None:
        return False
    _direct_completion_scope.set(advanced)
    return True


def complete_direct(
    expected: dict[str, Any] | None,
    *,
    actor: str,
    note: str = "",
    blocker: Callable[[Any, Any], Any] | None = None,
    evidence_ref: str | None = None,
) -> bool:
    """CAS task completion and its run finish in one authority transaction.

    ``evidence_ref`` (task #1848), when given, replaces the stored completion
    pointer in the same write; omitted leaves it untouched.

    Missing historical handoff time is unknown: repair dangling non-doing runs
    as abandoned with null duration. Recorder failure rolls back the task write.
    """
    if expected is None:
        return False
    task_id = expected["id"]

    def write(conn: Any) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or any(row[key] != expected[key] for key in _COMPLETION_FIELDS):
            return None
        run = _open_run_on(conn, task_id)
        if (run["run_id"] if run else None) != expected["open_run_id"]:
            return None
        if row["cancel_requested"] or (blocker and blocker(conn, row)):
            return None
        from . import workflow_steps

        if workflow_steps.is_workflow_task(conn, task_id):
            return None
        now = storage.now()
        revision = storage.next_revision(row["updated_at"])
        notes = ((row["notes"] or "") + ("\n" + note if note else "")).strip()
        conn.execute(
            "UPDATE tasks SET status='done', notes=?, updated_at=?, completed_at=?, "
            "claimed_by=COALESCE(NULLIF(TRIM(claimed_by), ''), ?), "
            "evidence_ref=COALESCE(?, evidence_ref) WHERE id=?",
            (notes, revision, now, actor, evidence_ref, task_id),
        )
        queue_completion_return(
            conn, task_id, old_status=row["status"], new_status="done", revision=revision
        )
        if run is not None:
            completed = row["status"] == "doing" and (
                row["current_run_id"] == run["run_id"]
                and row["current_run_agent"] == run["agent"]
                and row["claimed_by"] == run["agent"]
            )
            payload = _finish_payload(
                run,
                task_id=task_id,
                outcome="completed" if completed else "abandoned",
                to_status="done",
                finished_by=actor,
                finished_at=now,
                usage=None,
                reason=None if completed else "finish not recorded",
            )
            if not completed:
                payload["wall_s"] = None
            if not _append(conn, payload, actor, _finish_key(run["run_id"])):
                raise RuntimeError("direct completion run record was refused")
            _unbind_current_run(conn, task_id, run["run_id"])
        elif row["status"] != "done" and _latest_run_outcome(conn, task_id) != "handed_off":
            inserted = _append(
                conn,
                _finish_payload(
                    None,
                    task_id=task_id,
                    outcome="completed",
                    to_status="done",
                    finished_by=actor,
                    finished_at=now,
                    usage=None,
                    reason="no claim was recorded for this run",
                ),
                actor,
                _finish_key(f"none:{task_id}:{events.new_ulid()}"),
            )
            if not inserted:
                raise RuntimeError("direct completion unmatched record was refused")
        completed_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _direct_completion_snapshot_on(conn, completed_row)

    advanced = _tx(write)
    if advanced is None:
        return False
    if scoped_direct_completion(task_id) == expected:
        # This is the exact row written in our transaction, never a fresh
        # capture that could adopt a successor admitted after commit.
        _direct_completion_scope.set(advanced)
    return True


# --- readers -----------------------------------------------------------------


def _fold(
    claims: list[Any],
    finishes: dict[str, Any],
    cancels: dict[str, Any] | None = None,
    heartbeats: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cancels = cancels or {}
    heartbeats = heartbeats or {}
    runs = []
    for row in claims:
        c = _payload(row)
        f = finishes.get(c.get("run_id"))
        cancel = cancels.get(c.get("run_id"))
        heartbeat = heartbeats.get(c.get("run_id"))
        runs.append(
            {
                "run_id": c.get("run_id"),
                "task_id": c.get("task_id"),
                "agent": c.get("agent"),
                "claim_source": c.get("claim_source"),
                "observed_from": c.get("observed_from"),
                "claimed_at": c.get("claimed_at"),
                "lock_expires_at": c.get("lock_expires_at"),
                "lane": c.get("lane"),
                "project": c.get("project"),
                "state": "finished" if f else "open",
                "outcome": f.get("outcome") if f else None,
                "to_status": f.get("to_status") if f else None,
                "finished_at": f.get("finished_at") if f else None,
                "finished_by": f.get("finished_by") if f else None,
                "wall_s": f.get("wall_s") if f else None,
                "usage": merge_usage(f.get("usage") if f else None, heartbeat),
                "usage_reported": bool(f and f.get("usage_reported")) or heartbeat is not None,
                "reason": f.get("reason") if f else None,
                # Task #1841: why the run ended, when a path classifies it.
                "reason_code": f.get("reason_code") if f else None,
                # Task #1525: visible the moment `request_cancel` (via
                # task_update) commits, not only on the run's next heartbeat.
                "cancel_requested": cancel is not None,
                "cancel_requested_at": cancel.get("requested_at") if cancel else None,
                "cancel_requested_by": cancel.get("requested_by") if cancel else None,
            }
        )
        for key in (
            "trust_preset",
            "originator_agent",
            "originator_trust_preset",
            "native_authority_version",
        ):
            if key in c:
                runs[-1][key] = c[key]
    return runs


def _events_for(conn: Any, run_ids: list[str], key_fn: Callable[[str], str]) -> dict[str, Any]:
    if not run_ids:
        return {}
    out: dict[str, Any] = {}
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i : i + 500]
        keys = [key_fn(r) for r in chunk]
        rows = conn.execute(
            f"SELECT payload FROM event_log WHERE dedupe_key IN ({','.join('?' * len(keys))})",
            keys,
        ).fetchall()
        for row in rows:
            p = _payload(row)
            if p.get("run_id"):
                out[p["run_id"]] = p
    return out


def _finishes_for(conn: Any, run_ids: list[str]) -> dict[str, Any]:
    return _events_for(conn, run_ids, _finish_key)


def _cancels_for(conn: Any, run_ids: list[str]) -> dict[str, Any]:
    return _events_for(conn, run_ids, _cancel_key)


def finish_event_on(conn: Any, run_id: str) -> tuple[str, dict[str, Any]] | None:
    """``(ulid, payload)`` of ``run_id``'s finish on ``conn``, or None while it is open.

    Read on the caller's connection, so a caller inside a write transaction
    sees its own view (task subtree holds, task #1747).
    """
    row = conn.execute(
        "SELECT ulid, payload FROM event_log WHERE dedupe_key=?", (_finish_key(run_id),)
    ).fetchone()
    if row is None:
        return None
    payload = _payload(row)
    return (row["ulid"], payload) if payload.get("run_id") == run_id else None


def record_cancel_request_in_tx(
    tx: _Tx, task_id: int, *, run_id: str, agent: str | None, requested_by: str
) -> bool:
    """Append the cancellation event for ``run_id`` on ``task_id`` (task #1525).

    The caller (``task_update``'s ``request_cancel=True`` path) has already
    verified the task is ``doing`` with ``run_id`` as its current run, holds
    the write lock, and is about to set (or has just set)
    ``tasks.cancel_requested=1`` in the same transaction. This only records
    the *event*, so ``get_run``/``run_history`` and the MCP run resource show
    the request immediately rather than waiting for the run's next heartbeat
    (PR #866 review: "cancellation is invisible to run subscribers"). One
    event per run — a retried request is a no-op, not a duplicate.
    """
    payload = {
        "kind": KIND_CANCEL_REQUESTED,
        "run_id": run_id,
        "task_id": int(task_id),
        "agent": agent,
        "requested_by": requested_by,
        "requested_at": storage.now(),
    }
    return _append(tx, payload, requested_by, _cancel_key(run_id))


def get_run(run_id: str) -> dict[str, Any] | None:
    """One self-pickup run by id, folded from its claim, finish and cancel events."""
    rid = (run_id or "").strip()
    if not rid.startswith(RUN_ID_PREFIX):
        return None
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.kind')=? AND json_extract(payload, '$.run_id')=?",
            (CHANNEL, KIND_CLAIMED, rid),
        ).fetchall()
        runs = _fold(
            rows,
            _finishes_for(conn, [rid]),
            _cancels_for(conn, [rid]),
            _heartbeat_usages(conn, [rid]),
        )
        _add_live_native_status(conn, runs)
    finally:
        conn.close()
    return runs[0] if runs else None


def run_history(
    task_id: int | None = None,
    agent: str | None = None,
    since: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Self-pickup runs, newest first, each with outcome, duration and usage.

    Filters: ``task_id``, ``agent`` (the claimant) and ``since`` (an ISO
    timestamp, any offset, compared as an instant against the claim time; a
    naive value is read as UTC). This is the read #1348 (cost),
    #1354 (scorecards) and #1465 (delivery SLO) consume. ``truncated`` says
    when ``limit`` cut the list.
    """
    cap = max(1, min(int(limit), MAX_LIMIT))
    where = ["channel=?", "json_extract(payload, '$.kind')=?"]
    params: list[Any] = [CHANNEL, KIND_CLAIMED]
    if task_id is not None:
        where.append("json_extract(payload, '$.task_id')=?")
        params.append(int(task_id))
    if agent:
        where.append("json_extract(payload, '$.agent')=?")
        params.append(agent.strip())
    if since:
        # Claim times are UTC second-precision strings (storage.now). Compare
        # instants, not text: normalize any offset to that form first.
        parsed = _parse_ts(since)
        if parsed is None:
            raise ToolError("since must be an ISO 8601 timestamp")
        cutoff = parsed.astimezone(UTC)
        if cutoff.microsecond:
            # Stored claim times are whole seconds; a fractional cutoff rounds
            # up so a claim earlier in that second is not admitted.
            try:
                cutoff = cutoff.replace(microsecond=0) + timedelta(seconds=1)
            except OverflowError:
                # Past the last representable second: nothing can be later.
                return {"runs": [], "count": 0, "truncated": False, "limit": cap}
        where.append("json_extract(payload, '$.claimed_at') >= ?")
        params.append(cutoff.isoformat(timespec="seconds"))
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT payload FROM event_log WHERE {' AND '.join(where)} ORDER BY ulid DESC LIMIT ?",
            [*params, cap + 1],
        ).fetchall()
        truncated = len(rows) > cap
        rows = rows[:cap]
        run_ids = [p["run_id"] for p in (_payload(r) for r in rows) if p.get("run_id")]
        runs = _fold(
            rows,
            _finishes_for(conn, run_ids),
            _cancels_for(conn, run_ids),
            _heartbeat_usages(conn, run_ids),
        )
        _add_live_native_status(conn, runs)
    finally:
        conn.close()
    return {"runs": runs, "count": len(runs), "truncated": truncated, "limit": cap}


def _abandon_reason(run: dict[str, Any], task: Any, now: str) -> str | None:
    if task is None:
        return "task_missing"
    if task["status"] != "doing":
        return "task_left_doing"
    claimant = task["claimed_by"]
    if claimant and run.get("agent") and claimant != run["agent"]:
        return "claimant_changed"
    lease = task["lock_expires_at"]
    if lease and lease <= now:
        return "lease_expired"
    return None


def unclaimed_doing_cutoff(now_ts: str) -> str:
    """Oldest ``updated_at`` at which an unclaimed ``doing`` row still counts (task #1886).

    An unclaimed ``doing`` row holds no lease, so it is treated as if it held
    a default-length claim lease from its last write
    (``config.UNCLAIMED_DOING_LEASE_SECONDS``).
    """
    from . import config

    now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    cutoff = now_dt - timedelta(seconds=config.UNCLAIMED_DOING_LEASE_SECONDS)
    return cutoff.astimezone(UTC).isoformat(timespec="seconds")


# A ``doing`` row with no claimant that still counts as live WIP (task #1886):
# not archived (``queue_pressure``'s ``unowned_doing`` predicate) and written
# within the implicit lease. Bind the ``unclaimed_doing_cutoff`` value.
UNCLAIMED_LIVE_DOING_SQL = "claimed_by IS NULL AND archived_at IS NULL AND updated_at > ?"


def live_wip_counts(conn: Any, now_ts: str, exclude_id: int | None = None) -> tuple[int, int]:
    """Live ``doing`` WIP fleet-wide as ``(claimed, unclaimed)`` (tasks #1838, #1886).

    ``claimed``: claimed rows whose lease has not expired, the per-agent WIP
    predicate across every claimant (a lapsed lease frees its slot).
    ``unclaimed``: rows that reached ``doing`` through ``task_update`` with no
    claimant. They hold no lease, so each counts until it leaves ``doing`` or
    goes ``UNCLAIMED_DOING_LEASE_SECONDS`` without a write (see
    ``unclaimed_doing_cutoff``); ``queue_pressure`` reports them as a fault.
    """
    sql = (
        "SELECT "
        "COALESCE(SUM(CASE WHEN claimed_by IS NOT NULL "
        "AND (lock_expires_at IS NULL OR lock_expires_at > ?) THEN 1 ELSE 0 END), 0) AS claimed, "
        f"COALESCE(SUM(CASE WHEN {UNCLAIMED_LIVE_DOING_SQL} THEN 1 ELSE 0 END), 0) AS unclaimed "
        "FROM tasks WHERE status='doing'"
    )
    params: list[Any] = [now_ts, unclaimed_doing_cutoff(now_ts)]
    if exclude_id is not None:
        sql += " AND id!=?"
        params.append(exclude_id)
    row = conn.execute(sql, params).fetchone()
    return int(row["claimed"]), int(row["unclaimed"])


def live_claim_count(conn: Any, now_ts: str, exclude_id: int | None = None) -> int:
    """Live ``doing`` WIP fleet-wide, the number the fleet cap reads (tasks #1838, #1886).

    Claimed rows with a live lease plus unclaimed ``doing`` rows still inside
    their implicit lease (``live_wip_counts``). Counting the unclaimed rows
    keeps ``task_update(status="doing")`` from filling the fleet past its cap
    invisibly.
    """
    return sum(live_wip_counts(conn, now_ts, exclude_id))


def has_live_dispatcher_slot(conn: Any, task_id: int) -> bool:
    """True when the dispatcher already holds a live slot for this task (task #1838).

    A live ``dispatch_runs`` row (the launch gate's own slot predicate) or a
    claimed ``node_dispatch`` item with no receipt. The dispatcher admitted that
    run against ``UA_DISPATCH_MAX_LIVE_RUNS``; the spawned child's own
    ``task_claim`` must not be refused for the same slot after launch.
    """
    row = conn.execute(
        "SELECT 1 FROM dispatch_runs WHERE task_id=? "
        f"AND ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) "
        "UNION ALL SELECT 1 FROM node_dispatch WHERE task_id=? AND status='claimed' "
        "AND receipt_json IS NULL LIMIT 1",
        (task_id, task_id),
    ).fetchone()
    return row is not None


def _claim_path(entry: dict[str, Any]) -> str:
    """Which path put the item's *current* claim in flight.

    Ledger rows are not all current: executor attempts are best-effort
    evidence whose finish can be lost, and a dispatch row can keep a launch
    slot after it was cancelled. So a launcher row decides the path only when
    it is tied to the current claim: same agent as the task's claimant, a
    dispatch row in a live state or a claimed node item, and an executor
    attempt started at or after the current self-pickup claim (the executor
    claims first, then records its attempt). Otherwise the current self-pickup
    run's ``claim_source`` decides. Rows that tie to nothing are a fallback.
    """
    from .dispatch_states import LIVE_STATES

    sources = entry["sources"]
    claimant = entry.get("claimed_by")

    def ours(agent: Any) -> bool:
        return claimant is None or agent == claimant

    runs = sources.get("self_pickup") or []
    live_runs = [r for r in runs if r["state"] == "running"]
    current = max(live_runs, key=lambda r: r.get("claimed_at") or "") if live_runs else None
    since = (current or {}).get("claimed_at") or ""
    if any(
        d.get("state") in LIVE_STATES and ours(d.get("agent_label"))
        for d in sources.get("dispatch") or []
    ) or any(ours(n.get("agent")) for n in sources.get("node") or []):
        return "dispatcher"
    if any(
        ours(x.get("agent")) and (x.get("started_at") or "") >= since
        for x in sources.get("executor") or []
    ):
        return "executor"
    pick = current or (runs[-1] if runs else None)
    if pick is not None:
        source = pick.get("claim_source")
        return "direct_claim" if source in (None, "task_claim") else str(source)
    if sources.get("dispatch") or sources.get("node"):
        return "dispatcher"
    if sources.get("executor"):
        return "executor"
    return "unrecorded"


def _capacity_warning(run: dict[str, Any]) -> dict[str, Any] | None:
    """A warning for an open run near or at its harness's usage ceiling."""
    capacity = (run.get("usage") or {}).get("capacity")
    if not isinstance(capacity, dict):
        return None
    reached = capacity.get("limit_reached") is True
    remaining = capacity.get("remaining_pct")
    low = isinstance(remaining, (int, float)) and remaining <= CAPACITY_WARN_PCT
    if not (reached or low):
        return None
    return {
        "task_id": run.get("task_id"),
        "run_id": run.get("run_id"),
        "agent": run.get("agent"),
        "run_state": "running",
        "abandon_reason": None,
        "limit_reached": reached,
        "remaining_pct": remaining,
        "resets_at": capacity.get("resets_at"),
        "source": capacity.get("source"),
        "detail": capacity.get("detail"),
        "threshold_pct": CAPACITY_WARN_PCT,
    }


def runs_in_flight() -> dict[str, Any]:
    """What is in flight right now, across every execution path.

    One item per task (plus node work with no task), each listing the ledgers
    that see it: ``self_pickup`` (this module), ``executor``
    (``executor_attempts`` with no finish), ``dispatch`` (live ``dispatch_runs``)
    and ``node`` (claimed ``node_dispatch`` items with no receipt).

    Each item's ``state`` is ``running``, ``abandoned`` (an open self-pickup run
    whose lease lapsed, whose claimant changed, or whose task left ``doing``
    unrecorded) or ``unrecorded`` (a ``doing`` task no run ledger covers).
    ``coverage`` counts the ``doing`` tasks each way so a gap is never silent,
    and ``coverage.unclosed_runs`` lists open runs whose task already reached
    ``done``/``waiting_review``/``cancelled`` without a recorded finish.

    Each item's ``claim_path`` says how it got in flight: ``dispatcher``
    (dispatch or node ledger), ``executor``, ``direct_claim`` (an agent's own
    ``task_claim``), ``task_update`` or ``unrecorded``. ``wip`` reports the
    fleet cap ``task_claim`` and ``task_update`` into ``doing`` enforce
    (tasks #1838, #1886) beside the dispatcher's live-run ceiling, with the
    counts each gate reads; ``wip.fleet.unclaimed_doing`` is the part of
    ``live_claims`` held by ``doing`` rows with no claimant.
    """
    from . import config

    now = storage.now()
    conn = storage.connect()
    try:
        claims = conn.execute(
            "SELECT c.payload FROM event_log c WHERE c.channel=? "
            "AND json_extract(c.payload, '$.kind')=? "
            "AND NOT EXISTS (SELECT 1 FROM event_log f WHERE f.dedupe_key = "
            "'selfpickup:finished:' || json_extract(c.payload, '$.run_id')) "
            "ORDER BY c.ulid",
            (CHANNEL, KIND_CLAIMED),
        ).fetchall()
        open_runs = _fold(
            claims,
            {},
            heartbeats=_heartbeat_usages(conn, [_payload(c).get("run_id") for c in claims]),
        )
        doing = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, title, status, claimed_by, claimed_at, lock_expires_at, lane, project "
                "FROM tasks WHERE status='doing'"
            ).fetchall()
        }
        task_ids = {int(r["task_id"]) for r in open_runs if r.get("task_id") is not None}
        extra = task_ids - set(doing)
        other_tasks: dict[int, Any] = {}
        if extra:
            ids = sorted(extra)
            for r in conn.execute(
                f"SELECT id, title, status, claimed_by, claimed_at, lock_expires_at, lane, project "
                f"FROM tasks WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall():
                other_tasks[r["id"]] = r
        executor = conn.execute(
            "SELECT id, task_id, delegation_id, agent, backend, model, started_at, status "
            "FROM executor_attempts WHERE finished_at IS NULL"
        ).fetchall()
        # Every row that holds a launch slot, not only the live states: a
        # cancelled run with unconfirmed termination or a runtime-ceiling block
        # keeps its slot, so it must stay visible with its actual state.
        dispatch = conn.execute(
            "SELECT run_id, task_id, agent_label, state, lease_expires_at, created_at, "
            "failure_reason, termination_outcome "
            f"FROM dispatch_runs WHERE ({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE})"
        ).fetchall()
        node = conn.execute(
            "SELECT id, node, agent, backend, run_id, task_id, status, claimed_at, "
            "lease_expires_at FROM node_dispatch "
            "WHERE claimed_at IS NOT NULL AND receipt_json IS NULL"
        ).fetchall()
        fleet_claimed, fleet_unclaimed = live_wip_counts(conn, now)
        fleet_live = fleet_claimed + fleet_unclaimed
    finally:
        conn.close()

    items: dict[Any, dict[str, Any]] = {}

    def item_for(task_id: Any, key: Any = None) -> dict[str, Any]:
        k = task_id if task_id is not None else key
        if k not in items:
            task = doing.get(task_id) or other_tasks.get(task_id) if task_id is not None else None
            items[k] = {
                "task_id": task_id,
                "title": task["title"] if task is not None else None,
                "task_status": task["status"] if task is not None else None,
                "claimed_by": task["claimed_by"] if task is not None else None,
                "lock_expires_at": task["lock_expires_at"] if task is not None else None,
                "state": "running",
                "sources": {},
            }
        return items[k]

    abandoned = 0
    unclosed: list[dict[str, Any]] = []
    capacity_warnings: list[dict[str, Any]] = []
    for run in open_runs:
        tid = run.get("task_id")
        task = doing.get(tid) or other_tasks.get(tid)
        if task is not None and task["status"] in _NOT_IN_FLIGHT:
            # The task finished by a path that wrote no run finish. It is not in
            # flight; list it so the gap stays visible.
            unclosed.append(
                {"run_id": run["run_id"], "task_id": tid, "task_status": task["status"]}
            )
            continue
        reason = _abandon_reason(run, task, now)
        warning = _capacity_warning(run)
        if warning is not None:
            # A lapsed run stays listed (it may have died at the ceiling), but
            # says it is no longer running.
            if reason:
                warning["run_state"], warning["abandon_reason"] = "abandoned", reason
            capacity_warnings.append(warning)
        entry = item_for(tid)
        entry["sources"].setdefault("self_pickup", []).append(
            {
                "run_id": run["run_id"],
                "agent": run["agent"],
                "claim_source": run["claim_source"],
                "observed_from": run["observed_from"],
                "claimed_at": run["claimed_at"],
                "state": "abandoned" if reason else "running",
                "abandon_reason": reason,
                "usage": run["usage"],
                "usage_reported": run["usage_reported"],
            }
        )
    for r in executor:
        item_for(r["task_id"], f"executor:{r['id']}")["sources"].setdefault("executor", []).append(
            {k: r[k] for k in r.keys()}
        )
    for r in dispatch:
        item_for(r["task_id"], f"dispatch:{r['run_id']}")["sources"].setdefault(
            "dispatch", []
        ).append({k: r[k] for k in r.keys()})
    for r in node:
        item_for(r["task_id"], f"node:{r['id']}")["sources"].setdefault("node", []).append(
            {k: r[k] for k in r.keys()}
        )
    for tid in doing:
        if tid not in items:
            item_for(tid)["state"] = "unrecorded"

    for entry in items.values():
        if entry["state"] == "unrecorded":
            continue
        sp = entry["sources"].get("self_pickup", [])
        live_other = any(entry["sources"].get(k) for k in ("executor", "dispatch", "node"))
        if sp and all(s["state"] == "abandoned" for s in sp) and not live_other:
            entry["state"] = "abandoned"
            abandoned += 1

    ordered = sorted(
        items.values(),
        key=lambda e: (
            {"abandoned": 0, "unrecorded": 1, "running": 2}[e["state"]],
            e["task_id"] if e["task_id"] is not None else 0,
        ),
    )
    unrecorded = [e["task_id"] for e in ordered if e["state"] == "unrecorded"]
    covered = len(doing) - len(unrecorded)
    by_path: dict[str, int] = {}
    for entry in ordered:
        entry["claim_path"] = _claim_path(entry)
        if entry["state"] == "running":
            by_path[entry["claim_path"]] = by_path.get(entry["claim_path"], 0) + 1
    soft_cap, hard_cap = config.fleet_wip_max(), config.fleet_wip_hard_max()
    if fleet_live >= hard_cap:
        fleet_state = "at_hard_cap"
    elif fleet_live > soft_cap:
        fleet_state = "over_soft_cap"
    else:
        fleet_state = "ok"
    wip = {
        "fleet": {
            "live_claims": fleet_live,
            "unclaimed_doing": fleet_unclaimed,
            "soft_cap": soft_cap,
            "hard_cap": hard_cap,
            "state": fleet_state,
            "exempt_lanes": list(config.FLEET_WIP_EXEMPT_LANES),
        },
        "dispatch": {"live_slots": len(dispatch), "cap": config.dispatch_max_live_runs()},
        "running_by_path": by_path,
    }
    return {
        "as_of": now,
        "items": ordered,
        "counts": {
            "items": len(ordered),
            "running": sum(1 for e in ordered if e["state"] == "running"),
            "abandoned": abandoned,
            "unrecorded": len(unrecorded),
            "by_source": {
                "self_pickup": len(open_runs),
                "executor": len(executor),
                "dispatch": len(dispatch),
                "node": len(node),
            },
        },
        "wip": wip,
        # Task #1841: open runs whose harness reported low or exhausted plan
        # capacity on a heartbeat, so the operator sees it before they die.
        "capacity_warnings": capacity_warnings,
        "coverage": {
            "doing_tasks": len(doing),
            "recorded": covered,
            "unrecorded": len(unrecorded),
            "unrecorded_task_ids": unrecorded,
            "unclosed_runs": unclosed,
        },
    }


COVERAGE_DETAIL_MAX = 200


def _latest_unmatched_finishes(conn: Any, task_ids: list[int]) -> dict[int, Any]:
    """Latest retained finish with a null run ID per task, in one channel scan.

    Batched for run_coverage (task #1887): a per-task lookup scanned the whole
    self-pickup channel once per missing task. The newest finish per task (by
    ULID, the event_log primary key) is chosen in SQL, so the rows fetched are
    bounded by the task count, not by a task's retained finish history.
    """
    if not task_ids:
        return {}
    rows = conn.execute(
        "SELECT ulid, created_at, payload, task_id FROM ("
        "SELECT ulid, created_at, payload, json_extract(payload, '$.task_id') AS task_id, "
        "ROW_NUMBER() OVER (PARTITION BY json_extract(payload, '$.task_id') "
        "ORDER BY ulid DESC) AS rn "
        "FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.run_id') IS NULL "
        f"AND json_extract(payload, '$.task_id') IN ({','.join('?' * len(task_ids))})"
        ") WHERE rn = 1",
        (CHANNEL, KIND_FINISHED, *task_ids),
    ).fetchall()
    return {row["task_id"]: row for row in rows}


def run_coverage(
    window_days: float = 7.0, details: bool = True, limit: int = COVERAGE_DETAIL_MAX
) -> dict[str, Any]:
    """Completed-task coverage, with additive missing-run evidence diagnostics.

    The cohort is current done task rows within the existing completion window.
    The latest retained claim's completed/handed_off finish covers a task even
    when that claim predates the window or a later unmatched completion. Empty
    percentage stays None. Missing retained evidence does not prove no historic
    claim existed; diagnostics explain this reader, not inferred execution.

    ``details=False`` omits the per-task ``missing_tasks`` list and ``limit``
    (0..200) caps it (task #1887). Neither changes the coverage numbers or
    ``reason_counts``.
    """
    days = float(window_days)
    if days <= 0:
        raise ToolError("window_days must be > 0")
    if not isinstance(details, bool):
        raise ToolError("details must be a boolean")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 0 <= limit <= COVERAGE_DETAIL_MAX
    ):
        raise ToolError(f"limit must be an integer from 0 to {COVERAGE_DETAIL_MAX}")
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = storage.connect()
    detail_limit = limit if details else 0
    reason_counts: dict[str, int] = {}
    missing_tasks: list[dict[str, Any]] = []
    recorded: set[int] = set()
    try:
        # Cohort, latest claims/finishes and reasons must describe one read
        # snapshot; a successor claim can otherwise contradict the totals.
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT id, completed_at FROM tasks "
            "WHERE status='done' AND completed_at >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        cohort = [row["id"] for row in rows]
        for row in rows:
            task_id = row["id"]
            evidence = _coverage_run_provenance(conn, task_id)
            if evidence["outcome"] in _COVERING_OUTCOMES:
                recorded.add(task_id)
                continue
            reason = evidence["reason"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if len(missing_tasks) < detail_limit:
                missing_tasks.append(
                    {
                        "task_id": task_id,
                        "completed_at": row["completed_at"],
                        "reason": reason,
                        "latest_claim": evidence["latest_claim"],
                        "matched_finish": evidence["matched_finish"],
                        "latest_unmatched_finish": None,
                    }
                )
        unmatched = _latest_unmatched_finishes(conn, [item["task_id"] for item in missing_tasks])
        for item in missing_tasks:
            found = unmatched.get(item["task_id"])
            if found is not None:
                item["latest_unmatched_finish"] = _coverage_finish_provenance(found)
        failures = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_events WHERE kind='run_recorder' AND ts >= ?",
            (cutoff,),
        ).fetchone()["n"]
    finally:
        conn.close()
    hit = [t for t in cohort if t in recorded]
    missing = [t for t in cohort if t not in recorded]
    return {
        "window_days": days,
        "since": cutoff,
        "completed": len(cohort),
        "recorded": len(hit),
        "unrecorded": len(missing),
        "pct": round(100.0 * len(hit) / len(cohort), 1) if cohort else None,
        "unrecorded_task_ids": missing[:200],
        "recorder_failures": int(failures),
        "diagnostics": {
            "reason_counts": reason_counts,
            "missing_tasks": missing_tasks,
            "returned_count": len(missing_tasks),
            "total": len(missing),
            "limit": detail_limit,
            "truncated": len(missing) > len(missing_tasks),
        },
    }
