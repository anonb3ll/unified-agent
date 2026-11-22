"""Task liveness invariant (task #1749, Parity 25).

No task should sit in ``doing``, or ``open`` in an agent's lane, with nobody
responsible for the next move. This module names that responsibility and
surfaces the tasks that have none. It classifies and reports; it never guesses
an owner, never reassigns, never completes and never touches a live claim.

Three parts:

* :func:`action_path` is a pure classifier. Given one task row, the current
  time and a :class:`LivenessContext` gathered in batch by
  :func:`gather_context`, it returns the first action path that holds:

  ==================  ======================================================
  ``run``             a live claim lease, or an open run (a live dispatch
                      run, a claimed node item, an unfinished executor
                      attempt inside its lease, or an unclaimed ``doing``
                      row's self-pickup run inside the implicit lease or
                      whose run agent was seen on the hub within
                      ``UA_HYGIENE_LIVE_CLAIMANT_MINUTES``)
  ``review_participant``  ``waiting_review``, or a live ``[waiting-for: review]``
  ``approval``        a pending approval whose payload names the task
  ``blocked_on:<ids>``  prerequisites (``task_dependencies``) not yet terminal
  ``declared_wait``   a declared wait whose ``until`` is still in the future
  ``monitor``         a one-shot monitor due in the future
  ``human_owner``     an ``operator-only``/``interactive-only`` lane, or claimed
                      by the operator's own label
  ``queued``          a pending delegation, a retryable dispatch run, a queued
                      node item, an unassigned task in the shared pool, or an
                      ``open`` task in the lane of an agent seen on the hub
                      within ``AGENT_LANE_SEEN_HOURS``
  ``NONE``            none of the above
  ==================  ======================================================

  Terminal tasks and ``tracking`` tasks classify as ``out_of_scope``.

* :func:`run_liveness_sweep` runs inside the queue-hygiene pass (the review
  cron tick), not as a daemon. ``UA_LIVENESS_SWEEP`` picks the mode: ``off``
  does nothing; ``report`` (the default) records one ``agent.task.liveness``
  event per ``NONE`` task per UTC day, which the anomaly read surfaces in the
  exception digest; ``enforce`` also moves the task to ``blocked`` with a
  recovery note. In scope: ``doing`` tasks, and ``open`` tasks whose lane is
  an agent label. Parked (archived) rows are skipped.

* One-shot monitors and declared waits are written by ``task_update`` into
  the ``task_liveness`` table. A due monitor fires once: a hub message to the
  claimant, or a digest entry when nobody holds the task. Both are bounded to
  ``DECLARED_WAIT_MAX`` ahead and need a UTC offset. Only the claimant, the
  lane's agent when nobody holds the task, a lifecycle operator or the human
  operator may set or clear them (:func:`authorize_update`). A chain of waits
  declared back to back is tracked from its first declaration; once it runs
  past ``WAIT_CHAIN_MAX_DAYS`` the sweep records one digest entry per UTC day
  without changing the classification.

Retry exhaustion: a task whose last ``SELFPICKUP_MAX_ABANDONED`` self-pickup
runs (within ``STREAK_WINDOW``) all ended ``abandoned`` because the claimant's
lease lapsed is escalated the way
the dispatch scheduler dead-letters a delegation: a digest entry, and in
``enforce`` mode ``blocked`` plus a recovery note. Native dispatch runs already
count retries on ``delegations.attempts`` and dead-letter through
``dispatch.scheduler.dead_letter_exhausted``; ua-node items never retry (an
expired claim is failed for the operator to reconcile), so neither needs a
second counter here.

Approval linkage reads the payload keys ``task_id`` and ``parent_task_id``
only. A payload that names the task under a generic ``id`` key is deliberately
not matched: for most gated tools ``id`` names a different object.

Thresholds are deliberately simple constants; tuning them belongs to the
operating-profile work (#1732).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_lifecycle,
    config,
    events,
    registry,
    run_records,
    storage,
    task_notes,
    task_ownership,
)

logger = logging.getLogger(__name__)

CHANNEL = "agent.task.liveness"
# Attribution for enforce moves and the sender of monitor wakes. A closed
# roster (UA_AGENT_LABELS) must list it for the wake message to be accepted;
# otherwise the wake falls back to a digest entry.
ACTOR = "liveness-sweep"

RUN = "run"
QUEUED = "queued"
REVIEW_PARTICIPANT = "review_participant"
APPROVAL = "approval"
MONITOR = "monitor"
HUMAN_OWNER = "human_owner"
BLOCKED_ON = "blocked_on"
DECLARED_WAIT = "declared_wait"
NONE = "NONE"
OUT_OF_SCOPE = "out_of_scope"
KINDS = (
    RUN,
    QUEUED,
    REVIEW_PARTICIPANT,
    APPROVAL,
    MONITOR,
    HUMAN_OWNER,
    BLOCKED_ON,
    DECLARED_WAIT,
    NONE,
)

# A task written within this window is not swept: a task just moved into
# doing or into an agent's lane gets the same hour an unclaimed doing row's
# implicit lease gives it (config.UNCLAIMED_DOING_LEASE_SECONDS).
SWEEP_GRACE = timedelta(seconds=config.UNCLAIMED_DOING_LEASE_SECONDS)
# A declared wait or a monitor is bounded: one that would silence the
# invariant for longer than this is refused.
DECLARED_WAIT_MAX = timedelta(days=14)
# Waits declared back to back form a chain. One running longer than this is
# reported once per UTC day; it still classifies as declared_wait.
WAIT_CHAIN_MAX_DAYS = 28
# An open task in an agent's own lane is that agent's backlog (queued) while
# the agent was seen on the hub within this many hours; after that, nobody is
# going to pick it up and it is NONE.
AGENT_LANE_SEEN_HOURS = 24
# Consecutive abandoned self-pickup runs that count as exhausted retries. Same
# default as the dispatch scheduler's UA_DISPATCH_SCHEDULER_MAX_ATTEMPTS.
SELFPICKUP_MAX_ABANDONED = 3
# Only self-pickup finishes this recent count toward a streak.
STREAK_WINDOW = timedelta(days=30)
# An unfinished executor attempt has no timeout of its own; it runs under the
# executor's task lease (the default claim lease, renewed while it works).
EXECUTOR_ATTEMPT_LEASE = timedelta(seconds=config.TASK_CLAIM_DEFAULT_LEASE_SECONDS)
# How long a digest entry stays in the anomaly read.
DIGEST_WINDOW = timedelta(hours=24)
TEXT_MAX_CHARS = 500

_TERMINAL = ("done", "cancelled")
_HUMAN_LANES = ("operator-only", "interactive-only")
_CHUNK = 500
_DISPATCH_LIVE = ("claimed", "launched", "running")
# The lease-lapse finish of a takeover ("claimed after the lease lapsed"), as
# written before finishes carried a reason_code.
_LAPSE_REASON = "claimed after the lease lapsed"
# Bounded holds (task #2388): each monitor or declared wait an agent sets is one
# hold on that task. The count is of holds set, so clearing one refunds nothing.
HOLD_CHANNEL = "agent.task.liveness.hold"
# Waits bound to a named deployment (task #2414). "hub" or a registered ua-node.
DEPLOYMENT_CHANNEL = "agent.task.deployment_wait"
HUB_TARGET = "hub"
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
HOLD_QUOTA = 3
HOLD_WINDOW = timedelta(hours=1)


def _clock() -> datetime:
    """Clock seam (tests monkeypatch this)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ActionPath:
    """Who is responsible for a task's next move, and why."""

    kind: str
    detail: str

    @property
    def base(self) -> str:
        """``blocked_on:<ids>`` reduced to ``blocked_on``; other kinds unchanged."""
        return self.kind.split(":", 1)[0]


@dataclass(frozen=True)
class LivenessContext:
    """Facts about other tables, gathered in batch so the classifier stays pure.

    Every mapping is keyed by task id. ``open_runs`` and ``queued`` carry a
    short description; ``approvals`` and ``prerequisites`` carry ids;
    ``monitors`` maps to the pending ``monitor_at``; ``waits`` to
    ``(reason, until)``; ``wait_since`` to the start of the task's wait chain.
    ``live_run_agents`` maps an unclaimed ``doing`` task to its open run's
    agent when that agent was seen on the hub after the run opened, within
    the live-claimant window. ``seen_lane_agents`` holds the lane agents seen
    within ``AGENT_LANE_SEEN_HOURS``.
    """

    open_runs: Mapping[int, str] = field(default_factory=dict)
    queued: Mapping[int, str] = field(default_factory=dict)
    approvals: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    prerequisites: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    monitors: Mapping[int, str] = field(default_factory=dict)
    waits: Mapping[int, tuple[str, str]] = field(default_factory=dict)
    wait_since: Mapping[int, str] = field(default_factory=dict)
    live_run_agents: Mapping[int, str] = field(default_factory=dict)
    seen_lane_agents: frozenset[str] = frozenset()
    human_labels: frozenset[str] = frozenset()


def parse_ts(value: Any) -> datetime | None:
    """ISO timestamp to an aware UTC datetime; naive values are UTC. None if unusable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def is_assignee_lane(lane: str | None) -> bool:
    """True for a lane that names an agent rather than a shared or system lane."""
    name = config.canonical_task_lane(lane)
    return bool(name) and name not in config.TASK_SYSTEM_LANES


def in_sweep_scope(task: Mapping[str, Any]) -> bool:
    """``doing`` tasks and ``open`` tasks in an agent's lane, unparked, not tracking."""
    if task.get("archived_at"):
        return False
    lane = config.canonical_task_lane(task.get("lane"))
    if lane in config.TASK_UNCLAIMABLE_LANES:
        return False
    if task.get("status") == "doing":
        return True
    return task.get("status") == "open" and is_assignee_lane(lane)


def action_path(task: Mapping[str, Any], *, now: datetime, context: LivenessContext) -> ActionPath:
    """Classify one task. Pure: reads only ``task``, ``now`` and ``context``.

    ``task`` needs ``id``, ``status``, ``lane``, ``claimed_by``,
    ``lock_expires_at``, ``current_run_id``, ``updated_at`` and ``notes``. The
    first matching path wins, in the order of the table in the module
    docstring.
    """
    task_id = int(task["id"])
    status = task.get("status")
    lane = config.canonical_task_lane(task.get("lane"))
    if status in _TERMINAL:
        return ActionPath(OUT_OF_SCOPE, f"task is {status}")
    if lane in config.TASK_UNCLAIMABLE_LANES:
        return ActionPath(OUT_OF_SCOPE, f"{lane} lane is not a unit of work")

    claimant = task_ownership.read(task).claimant
    lease = parse_ts(task.get("lock_expires_at"))
    if claimant and (lease is None or lease > now):
        until = f"until {_iso(lease)}" if lease is not None else "with no lease expiry"
        return ActionPath(RUN, f"live claim by {claimant} {until}")
    if task_id in context.open_runs:
        return ActionPath(RUN, context.open_runs[task_id])
    run_id = task.get("current_run_id")
    if status == "doing" and not claimant and run_id:
        touched = parse_ts(task.get("updated_at"))
        if touched is not None and touched > now - SWEEP_GRACE:
            return ActionPath(RUN, f"open self-pickup run {run_id} inside the implicit lease")
        run_agent = context.live_run_agents.get(task_id)
        if run_agent:
            return ActionPath(
                RUN, f"open self-pickup run {run_id}; its agent {run_agent} is seen on the hub"
            )

    if status == "waiting_review":
        return ActionPath(REVIEW_PARTICIPANT, "waiting_review")
    if task_notes.live_review_request_end(task.get("notes")) is not None:
        return ActionPath(REVIEW_PARTICIPANT, "[waiting-for: review]")

    approvals = context.approvals.get(task_id)
    if approvals:
        return ActionPath(
            APPROVAL, "pending approval " + ", ".join(f"#{a}" for a in sorted(approvals))
        )

    prerequisites = context.prerequisites.get(task_id)
    if prerequisites:
        ids = ",".join(str(p) for p in sorted(prerequisites))
        return ActionPath(f"{BLOCKED_ON}:{ids}", f"waiting on task(s) {ids}")

    wait = context.waits.get(task_id)
    if wait is not None:
        until = parse_ts(wait[1])
        if until is not None and until > now:
            return ActionPath(DECLARED_WAIT, f"{wait[0]} (until {_iso(until)})")

    monitor_at = parse_ts(context.monitors.get(task_id))
    if monitor_at is not None and monitor_at > now:
        return ActionPath(MONITOR, f"monitor due {_iso(monitor_at)}")

    if lane in _HUMAN_LANES:
        return ActionPath(HUMAN_OWNER, f"{lane} lane")
    if claimant and claimant in context.human_labels:
        return ActionPath(HUMAN_OWNER, f"claimed by the operator ({claimant})")

    if task_id in context.queued:
        return ActionPath(QUEUED, context.queued[task_id])
    if status == "open" and not is_assignee_lane(lane):
        return ActionPath(QUEUED, f"shared pool ({lane or 'default'} lane)")
    if status == "open" and lane in context.seen_lane_agents:
        return ActionPath(
            QUEUED, f"backlog of {lane}, seen on the hub within {AGENT_LANE_SEEN_HOURS}h"
        )

    if status == "doing":
        who = (
            f"claim by {claimant} lapsed at {_iso(lease)}"
            if claimant and lease is not None
            else "no claimant"
        )
        return ActionPath(
            NONE,
            f"doing with {who}, and no open run, queued dispatch, review, approval, "
            "prerequisite, declared wait or monitor",
        )
    unseen = (
        f", whose agent was not seen on the hub for {AGENT_LANE_SEEN_HOURS}h,"
        if status == "open"
        else ""
    )
    return ActionPath(
        NONE,
        f"{status} in lane {lane}{unseen} with no live claim, queued dispatch, review, "
        "approval, prerequisite, declared wait or monitor",
    )


# --- batch context ----------------------------------------------------------


def _chunks(ids: list[Any]) -> Iterable[list[Any]]:
    for start in range(0, len(ids), _CHUNK):
        yield ids[start : start + _CHUNK]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _first(target: dict[int, str], task_id: Any, text: str) -> None:
    if task_id is not None:
        target.setdefault(int(task_id), text)


def _live_attempt(r: Mapping[str, Any], now: datetime) -> bool:
    """An unfinished executor attempt counts only while something bounds it.

    It has no timeout of its own, so it is live while it is younger than the
    executor's task lease, while the task's claim is live, or while its
    delegation's claim is (``in_flight`` and claimed within the scheduler's
    lease TTL, the coordinator's own orphan-reclaim bound).
    """
    started = parse_ts(r["started_at"])
    if started is not None and started > now - EXECUTOR_ATTEMPT_LEASE:
        return True
    lock = parse_ts(r["lock_expires_at"])
    if (r["claimed_by"] or "").strip() and (lock is None or lock > now):
        return True
    if r["delegation_status"] != "in_flight":
        return False
    claimed = parse_ts(r["delegation_attempt_at"])
    ttl = timedelta(seconds=config.dispatch_scheduler_lease_ttl_s())
    return claimed is not None and claimed > now - ttl


def gather_context(
    conn: sqlite3.Connection, task_ids: Iterable[int], *, now: datetime
) -> LivenessContext:
    """Read every fact :func:`action_path` needs for ``task_ids`` in batch queries."""
    ids = sorted({int(t) for t in task_ids})
    open_runs: dict[int, str] = {}
    queued: dict[int, str] = {}
    approvals: dict[int, set[int]] = {}
    prerequisites: dict[int, list[int]] = {}
    monitors: dict[int, str] = {}
    waits: dict[int, tuple[str, str]] = {}
    wait_since: dict[int, str] = {}
    # Unclaimed doing rows with an open self-pickup run: {id: (run_id, agent)}.
    unclaimed_runs: dict[int, tuple[str, str | None]] = {}
    lane_agents: set[str] = set()
    tables = (
        "dispatch_runs",
        "node_dispatch",
        "executor_attempts",
        "delegations",
        "task_dependencies",
        "task_liveness",
    )
    has = {name: _has_table(conn, name) for name in tables}
    now_iso = _iso(now)
    delegation_cols = (
        "d.status AS delegation_status, COALESCE(d.last_attempt_at, d.updated_at) "
        "AS delegation_attempt_at FROM executor_attempts a JOIN tasks t ON t.id = a.task_id "
        "LEFT JOIN delegations d ON d.id = a.delegation_id"
        if has["delegations"]
        else "NULL AS delegation_status, NULL AS delegation_attempt_at "
        "FROM executor_attempts a JOIN tasks t ON t.id = a.task_id"
    )
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT id, status, lane, claimed_by, current_run_id, current_run_agent "
            f"FROM tasks WHERE id IN ({marks})",
            chunk,
        ):
            if r["status"] == "open" and is_assignee_lane(r["lane"]):
                lane_agents.add(config.canonical_task_lane(r["lane"]))
            elif (
                r["status"] == "doing"
                and not (r["claimed_by"] or "").strip()
                and r["current_run_id"]
            ):
                unclaimed_runs[int(r["id"])] = (r["current_run_id"], r["current_run_agent"])
        if has["dispatch_runs"]:
            for r in conn.execute(
                f"SELECT run_id, task_id, state FROM dispatch_runs WHERE task_id IN ({marks}) "
                "AND state IN ('claimed','launched','running','retryable') ORDER BY created_at",
                chunk,
            ):
                if r["state"] in _DISPATCH_LIVE:
                    _first(open_runs, r["task_id"], f"dispatch run {r['run_id']} ({r['state']})")
                else:
                    _first(queued, r["task_id"], f"retryable dispatch run {r['run_id']}")
        if has["node_dispatch"]:
            for r in conn.execute(
                f"SELECT id, task_id, status, lease_expires_at FROM node_dispatch "
                f"WHERE task_id IN ({marks}) AND status IN ('queued','claimed') "
                "AND receipt_json IS NULL ORDER BY created_at",
                chunk,
            ):
                if r["status"] == "queued":
                    _first(queued, r["task_id"], f"queued node item {r['id']}")
                elif (r["lease_expires_at"] or "") > now_iso:
                    _first(open_runs, r["task_id"], f"claimed node item {r['id']}")
        if has["executor_attempts"]:
            for r in conn.execute(
                "SELECT a.id, a.task_id, a.agent, a.started_at, t.claimed_by, "
                f"t.lock_expires_at, {delegation_cols} "
                f"WHERE a.task_id IN ({marks}) AND a.finished_at IS NULL ORDER BY a.id",
                chunk,
            ):
                if _live_attempt(r, now):
                    _first(open_runs, r["task_id"], f"executor attempt {r['id']} by {r['agent']}")
        if has["delegations"]:
            for r in conn.execute(
                f"SELECT id, task_id, to_agent FROM delegations WHERE task_id IN ({marks}) "
                "AND status='pending' ORDER BY id",
                chunk,
            ):
                _first(queued, r["task_id"], f"pending delegation #{r['id']} to {r['to_agent']}")
        if has["task_dependencies"]:
            for r in conn.execute(
                "SELECT d.task_id, d.depends_on_task_id FROM task_dependencies d "
                "JOIN tasks p ON p.id = d.depends_on_task_id "
                f"WHERE d.task_id IN ({marks}) AND p.status NOT IN ('done','cancelled')",
                chunk,
            ):
                prerequisites.setdefault(int(r["task_id"]), []).append(int(r["depends_on_task_id"]))
        if has["task_liveness"]:
            for r in conn.execute(
                "SELECT task_id, monitor_at, wait_reason, wait_until, wait_first_declared_at "
                f"FROM task_liveness WHERE task_id IN ({marks})",
                chunk,
            ):
                if r["monitor_at"]:
                    monitors[int(r["task_id"])] = r["monitor_at"]
                if r["wait_until"]:
                    waits[int(r["task_id"])] = (r["wait_reason"] or "", r["wait_until"])
                    if r["wait_first_declared_at"]:
                        wait_since[int(r["task_id"])] = r["wait_first_declared_at"]

    # The open run's record names its agent (when the cache does not) and the
    # instant it opened: activity must come after that to count as the agent
    # acting, the same rule queue hygiene applies after a claim.
    run_opened: dict[int, datetime | None] = {}
    if unclaimed_runs:
        for chunk in _chunks(sorted(unclaimed_runs)):
            for r in conn.execute(
                "SELECT json_extract(payload, '$.task_id') AS task_id, "
                "json_extract(payload, '$.run_id') AS run_id, "
                "json_extract(payload, '$.agent') AS agent, "
                "json_extract(payload, '$.claimed_at') AS claimed_at "
                "FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
                f"AND json_extract(payload, '$.task_id') IN ({','.join('?' * len(chunk))})",
                (run_records.CHANNEL, run_records.KIND_CLAIMED, *chunk),
            ):
                task_id = int(r["task_id"])
                run_id, agent = unclaimed_runs[task_id]
                if r["run_id"] != run_id:
                    continue
                unclaimed_runs[task_id] = (run_id, agent or r["agent"])
                run_opened[task_id] = parse_ts(r["claimed_at"])

    labels = lane_agents | {a for _, a in unclaimed_runs.values() if a}
    seen: dict[str, tuple[str | None, str | None]] = {}
    for chunk in _chunks(sorted(labels)):
        for r in conn.execute(
            "SELECT label, last_seen, last_seen_via FROM agents "
            f"WHERE label IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            seen[r["label"]] = (r["last_seen"], r["last_seen_via"])

    lane_since = now - timedelta(hours=AGENT_LANE_SEEN_HOURS)
    seen_lane_agents = frozenset(
        label
        for label in lane_agents
        if label in seen and registry.seen_acting(*seen[label], since=lane_since)
    )
    run_since = now - registry.live_agent_window()
    live_run_agents = {
        task_id: agent
        for task_id, (_, agent) in unclaimed_runs.items()
        if agent
        and agent in seen
        and registry.seen_acting(*seen[agent], since=run_since, after=run_opened.get(task_id))
    }

    # Pending approvals are few; parse their payloads here rather than trust
    # every row to hold valid JSON for json_extract.
    wanted = set(ids)
    for r in conn.execute("SELECT id, payload FROM approvals WHERE status='pending'"):
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("task_id", "parent_task_id"):
            try:
                ref = int(payload.get(key))
            except (TypeError, ValueError):
                continue
            if ref in wanted:
                approvals.setdefault(ref, set()).add(int(r["id"]))

    return LivenessContext(
        open_runs=open_runs,
        queued=queued,
        approvals={k: tuple(sorted(v)) for k, v in approvals.items()},
        prerequisites={k: tuple(sorted(v)) for k, v in prerequisites.items()},
        monitors=monitors,
        waits=waits,
        wait_since=wait_since,
        live_run_agents=live_run_agents,
        seen_lane_agents=seen_lane_agents,
        human_labels=frozenset(config.human_identity_labels() | config.operator_aliases()),
    )


def _lease_lapse(reason_code: Any, reason: Any) -> bool:
    """An ``abandoned`` finish caused by the claimant's lease lapsing."""
    return reason_code == run_records.REASON_LEASE_EXPIRED or reason == _LAPSE_REASON


def abandoned_streaks(
    conn: sqlite3.Connection, task_ids: Iterable[int], *, now: datetime | None = None
) -> dict[int, tuple[int, str]]:
    """Trailing run of lease-lapse abandonments per task: ``{id: (n, last_run)}``.

    Read from the run ledger itself (``selfpickup.finished`` events of the last
    ``STREAK_WINDOW``). Only an ``abandoned`` finish whose lease lapsed counts
    (``reason_code`` ``lease_expired``, which a takeover after the lapse also
    writes). Other abandonments (a finish that was not recorded, a task that
    re-entered ``doing``, a repaired completion, a terminated agent) neither
    count nor reset. Any other outcome resets the count, so only consecutive
    lapses since the last real outcome count as retries.
    """
    wanted = {int(t) for t in task_ids}
    streaks: dict[int, tuple[int, str]] = {}
    if not wanted:
        return streaks
    cutoff = _iso((now or _clock()) - STREAK_WINDOW)
    for r in conn.execute(
        "SELECT json_extract(payload, '$.task_id') AS task_id, "
        "json_extract(payload, '$.outcome') AS outcome, "
        "json_extract(payload, '$.run_id') AS run_id, "
        "json_extract(payload, '$.reason_code') AS reason_code, "
        "json_extract(payload, '$.reason') AS reason "
        "FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND created_at >= ? ORDER BY rowid",
        (run_records.CHANNEL, run_records.KIND_FINISHED, cutoff),
    ):
        try:
            task_id = int(r["task_id"])
        except (TypeError, ValueError):
            continue
        if task_id not in wanted:
            continue
        if r["outcome"] == "abandoned":
            if not _lease_lapse(r["reason_code"], r["reason"]):
                continue
            count = streaks.get(task_id, (0, ""))[0]
            streaks[task_id] = (count + 1, str(r["run_id"] or ""))
        else:
            streaks.pop(task_id, None)
    return streaks


# --- task_update parameters ---------------------------------------------------


def _parse_arg(value: Any, name: str) -> datetime:
    """An ISO datetime with a UTC offset, as ``task_list(created_since=...)`` takes."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ToolError(f"{name} must be an ISO 8601 datetime, e.g. 2026-10-09T15:00:00Z") from None
    if parsed.tzinfo is None:
        raise ToolError(
            f"{name} must include a UTC offset (storage is UTC; a naive time is ambiguous)"
        )
    return parsed.astimezone(UTC)


def _text(value: Any, name: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ToolError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise ToolError(f"{name} must not be empty")
        return None
    if len(text) > TEXT_MAX_CHARS:
        raise ToolError(f"{name} must be at most {TEXT_MAX_CHARS} characters")
    return text


def normalize_update(
    *,
    monitor_at: str | None,
    monitor_note: str | None,
    clear_monitor: bool,
    declared_wait: dict | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Validate the liveness parameters of one ``task_update`` call.

    Returns None when none was passed, else ``{"monitor": (at, note) | None,
    "wait": (reason, until, next_step, deployment) | None}`` with only the keys being changed (None
    clears). Raises ``ToolError`` before any write.
    """
    if monitor_at is None and monitor_note is None and not clear_monitor and declared_wait is None:
        return None
    now = now or _clock()
    spec: dict[str, Any] = {}
    if clear_monitor:
        if monitor_at is not None or monitor_note is not None:
            raise ToolError("pass monitor_at or clear_monitor=True, not both")
        spec["monitor"] = None
    elif monitor_at is not None:
        at = _parse_arg(monitor_at, "monitor_at")
        if at > now + DECLARED_WAIT_MAX:
            raise ToolError(
                f"monitor_at may be at most {DECLARED_WAIT_MAX.days} days ahead; "
                "set a new monitor when it fires"
            )
        spec["monitor"] = (_iso(at), _text(monitor_note, "monitor_note", required=False))
    elif monitor_note is not None:
        raise ToolError("monitor_note needs monitor_at on the same call")

    if declared_wait is not None:
        if not isinstance(declared_wait, dict):
            raise ToolError('declared_wait must be {"reason": ..., "until": ISO} or {} to clear')
        if not declared_wait:
            spec["wait"] = None
        else:
            allowed = {"reason", "until", "next_step", "deployment"}
            if not {"reason", "until"} <= set(declared_wait) <= allowed:
                raise ToolError(
                    'declared_wait takes exactly "reason" and "until", plus optional '
                    '"next_step" and "deployment"'
                )
            reason = _text(declared_wait.get("reason"), "declared_wait.reason", required=True)
            until = _parse_arg(declared_wait.get("until"), "declared_wait.until")
            if until <= now:
                raise ToolError("declared_wait.until must be in the future")
            if until > now + DECLARED_WAIT_MAX:
                raise ToolError(
                    f"declared_wait.until may be at most {DECLARED_WAIT_MAX.days} days ahead; "
                    "declare a new wait when it lapses"
                )
            next_step = _text(
                declared_wait.get("next_step"), "declared_wait.next_step", required=False
            )
            deployment = _deployment(declared_wait.get("deployment"))
            spec["wait"] = (reason, _iso(until), next_step, deployment)
    return spec


def deployment_wait_blocker(conn: sqlite3.Connection, task_id: int) -> str | None:
    """Why a merge must not close this task yet, or None (task #2429).

    A live deployment wait says the work is merged but not yet running where it
    has to. An expired one has been surfaced as owed and holds nothing.
    """
    row = conn.execute(
        "SELECT wait_deploy_target, wait_deploy_sha FROM task_liveness "
        "WHERE task_id=? AND wait_deploy_sha IS NOT NULL AND wait_until > ?",
        (int(task_id), _iso(datetime.now(UTC))),
    ).fetchone()
    if row is None:
        return None
    return f"waiting on deployment of {row['wait_deploy_sha'][:12]} to {row['wait_deploy_target']}"


def _deployment(raw: Any) -> tuple[str, str] | None:
    """``(target, sha)`` of a deployment wait, or None. Raises before any write."""
    from . import node_registry

    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"target", "sha"}:
        raise ToolError('declared_wait.deployment takes exactly "target" and "sha"')
    target, sha = raw["target"], raw["sha"]
    if not isinstance(sha, str) or _FULL_SHA.fullmatch(sha) is None:
        raise ToolError(
            "declared_wait.deployment.sha must be a full commit SHA "
            "(40-character, or 64 for SHA-256)"
        )
    if target != HUB_TARGET and target not in node_registry.read_nodes():
        raise ToolError(
            f"unknown deployment target {target!r}: use {HUB_TARGET!r} or a registered ua-node"
        )
    return target, sha


def _hold_summary(spec: Mapping[str, Any]) -> tuple[str, str]:
    """``(what, until)`` of the hold a normalized spec sets."""
    if spec.get("wait") is not None:
        reason, until, _, _ = spec["wait"]
        return reason, until
    at, note = spec["monitor"]
    return note or "monitor", at


def _charge_hold(
    conn: sqlite3.Connection, task_id: int, spec: Mapping[str, Any], actor: str | None
) -> None:
    """Count one hold against ``(task, actor)``, or refuse the fourth in the window.

    Inside the caller's transaction, so a refused call writes nothing and two
    concurrent holds are counted in order. The human operator is not limited.
    """
    if (spec.get("monitor") is None and spec.get("wait") is None) or (
        not actor or config.is_human_identity(actor)
    ):
        return
    prior: list[tuple[str, dict[str, Any]]] = []
    for row in conn.execute(
        "SELECT payload, created_at FROM event_log WHERE channel=? AND created_at >= ? "
        "ORDER BY rowid",
        (HOLD_CHANNEL, _iso(_clock() - HOLD_WINDOW)),
    ):
        payload = json.loads(row["payload"] or "{}")
        if payload.get("task_id") == task_id and payload.get("agent") == actor:
            prior.append((row["created_at"], payload))
    if len(prior) >= HOLD_QUOTA:
        named = "; ".join(f"{at}: {p.get('what')} (until {p.get('until')})" for at, p in prior)
        raise ToolError(
            f"task {task_id}: {actor} set {len(prior)} holds in the last "
            f"{int(HOLD_WINDOW.total_seconds() // 60)} minutes, the limit: {named}. "
            "Do the next step, or hand the task on."
        )
    what, until = _hold_summary(spec)
    # Read back from the log only; nothing subscribes, so no socket publish.
    events.record_event_once_on_connection(
        conn,
        HOLD_CHANNEL,
        {"task_id": task_id, "agent": actor, "what": what, "until": until},
        originator=actor,
    )


def _get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def authorize_update(
    task: Mapping[str, Any], actor: str | None, *, ownership: bool = False
) -> None:
    """Refuse a monitor, declared-wait or ownership change from a caller without authority.

    Allowed: the task's claimant; when nobody holds the task, the agent its
    lane names or the agent whose self-pickup run is on it; a lifecycle operator
    (``UA_LIFECYCLE_OPERATORS``); and the human operator's label. ``task``
    needs ``claimed_by`` and ``lane`` (``current_run_agent`` when present).

    ``ownership=True`` applies the same rule to ``next_owner``, ``waiting_on``
    and ``terminal_predicate`` (#2388): the stored owner counts as a holder
    alongside the claimant, and a self-pickup run's agent gains no authority.
    """
    subject = "this task's ownership" if ownership else "this task's monitor or declared wait"
    who = (actor or "").strip()
    if not who:
        raise ToolError(
            f"changing {subject} needs an attributed caller (verified identity or actor)"
            if ownership
            else "setting or clearing a monitor or declared wait needs an attributed caller "
            "(verified identity or actor)"
        )
    claimant = (task["claimed_by"] or "").strip()
    stored_owner = task_ownership.read(task).next_owner if ownership else None
    lane = config.canonical_task_lane(task["lane"])
    holders = [name for name in (claimant, stored_owner) if name]
    if holders:
        if who in holders:
            return
    else:
        # Unclaimed: the lane's agent, or the agent whose self-pickup run is on it.
        run_agent = "" if ownership else (_get(task, "current_run_agent") or "").strip()
        if (is_assignee_lane(lane) and who == lane) or (run_agent and who == run_agent):
            return
    if who == config.operator_label() or who in agent_lifecycle.operators():
        return
    named = " or ".join(holders)
    owner = (
        f"its {'owner' if ownership else 'claimant'} {named}"
        if holders
        else "the agent its lane names"
    )
    verb = "change" if ownership else "set or clear"
    raise ToolError(
        f"{who} may not {verb} {subject}; only {owner}, a lifecycle operator or the human "
        "operator may"
    )


def apply_update(
    conn: sqlite3.Connection, task_id: int, spec: Mapping[str, Any], *, actor: str | None
) -> dict[str, Any]:
    """Write a normalized spec inside the caller's transaction; return the new state."""
    _charge_hold(conn, task_id, spec, actor)
    now_iso = storage.now()
    conn.execute(
        "INSERT OR IGNORE INTO task_liveness (task_id, updated_at) VALUES (?, ?)",
        (task_id, now_iso),
    )
    if "monitor" in spec:
        at, note = spec["monitor"] if spec["monitor"] is not None else (None, None)
        conn.execute(
            "UPDATE task_liveness SET monitor_at=?, monitor_note=?, monitor_set_by=?, "
            "updated_at=? WHERE task_id=?",
            (at, note, actor if at else None, now_iso, task_id),
        )
    if "wait" in spec:
        reason, until, next_step, deployment = (
            spec["wait"] if spec["wait"] is not None else (None, None, None, None)
        )
        target, sha = deployment if deployment is not None else (None, None)
        first = None
        if until is not None:
            # A wait declared while the previous one is still live extends the
            # chain; after a clear or an expiry a new chain starts now.
            now = _clock()
            prior = conn.execute(
                "SELECT wait_until, wait_first_declared_at FROM task_liveness WHERE task_id=?",
                (task_id,),
            ).fetchone()
            prior_until = parse_ts(prior["wait_until"])
            unbroken = prior_until is not None and prior_until > now
            first = (prior["wait_first_declared_at"] if unbroken else None) or _iso(now)
        conn.execute(
            "UPDATE task_liveness SET wait_reason=?, wait_until=?, wait_next_step=?, "
            "wait_deploy_target=?, wait_deploy_sha=?, "
            "wait_set_by=?, wait_first_declared_at=?, updated_at=? WHERE task_id=?",
            (
                reason,
                until,
                next_step,
                target,
                sha,
                actor if until else None,
                first,
                now_iso,
                task_id,
            ),
        )
    return state(conn, task_id)


def state(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    """The task's monitor and declared wait as returned by ``task_update``."""
    row = conn.execute("SELECT * FROM task_liveness WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return {"monitor": None, "declared_wait": None, "monitor_attempts": 0}
    return {
        "monitor": (
            {"at": row["monitor_at"], "note": row["monitor_note"], "set_by": row["monitor_set_by"]}
            if row["monitor_at"]
            else None
        ),
        "declared_wait": (
            {
                "reason": row["wait_reason"],
                "until": row["wait_until"],
                "next_step": row["wait_next_step"],
                "deployment": (
                    {"target": row["wait_deploy_target"], "sha": row["wait_deploy_sha"]}
                    if row["wait_deploy_sha"]
                    else None
                ),
                "set_by": row["wait_set_by"],
                "first_declared_at": row["wait_first_declared_at"],
            }
            if row["wait_until"]
            else None
        ),
        "monitor_attempts": row["monitor_attempts"],
        "monitor_fired_at": row["monitor_fired_at"],
        "monitor_result": row["monitor_result"],
    }


# --- sweep ------------------------------------------------------------------------


def _record(payload: dict[str, Any], dedupe_key: str) -> bool:
    """Append one digest entry; False when this dedupe key was already recorded."""
    return events.record_event_once(CHANNEL, payload, originator=ACTOR, dedupe_key=dedupe_key)[1]


def _wake_claimant(task: Mapping[str, Any], fired_for: str, note: str | None) -> str:
    """Send the one wake message. Returns the message id; raises on refusal."""
    from .messaging.send import send_message

    body = f"Monitor due on task #{task['id']} ({fired_for})"
    if note:
        body += f": {note}"
    body += ". Check on it, then update the task: claim, declare a wait, or set a new monitor."
    sent = send_message(
        act="inform",
        to_agent=task["claimed_by"],
        body=body,
        from_agent=ACTOR,
        task_id=int(task["id"]),
        message_id=f"liveness-monitor-{task['id']}-{fired_for}",
    )
    return str(sent.get("message_id") or "")


def _deployed_sha(target: str, nodes: Mapping[str, Any], now: datetime) -> str | None:
    """The commit ``target`` runs now, or None when it cannot say.

    The hub's is the commit its running process started from, not its checkout's
    HEAD. A node's is the ``code_sha`` it reports, counted only while it is online.
    """
    from . import hub_runtime, node_registry

    if target == HUB_TARGET:
        record = hub_runtime.read_hub_start()
        if record is None or not hub_runtime.pid_alive(record.get("pid")):
            return None
        return record.get("sha")
    info = nodes.get(target)
    if info is None or node_registry.node_status(info, now) != "online":
        return None
    return info.get("code_sha")


def _announce_deployment(row: Mapping[str, Any]) -> None:
    """Tell the claimant once. A refused message leaves the event as the record."""
    from .messaging.send import send_message

    if not (row["claimed_by"] or "").strip():
        return
    try:
        send_message(
            act="inform",
            to_agent=row["claimed_by"],
            body=(
                f"Task #{row['task_id']}: {row['wait_deploy_target']} now runs "
                f"{row['wait_deploy_sha'][:12]}, so its deployment wait is cleared. "
                "Verify the change live, then update the task."
            ),
            from_agent=ACTOR,
            task_id=int(row["task_id"]),
            message_id=f"liveness-deployment-{row['task_id']}-{row['wait_until']}",
        )
    except Exception as exc:  # noqa: BLE001 - the wait is already cleared and recorded
        logger.warning("deployment wait notice refused: %s", type(exc).__name__)


def clear_reached_deployments(*, now: datetime, dry_run: bool = False) -> list[dict[str, Any]]:
    """Clear each deployment wait whose named target reports its exact SHA.

    An exact match only: a merge, another target or a different commit leaves the
    wait in place, and it surfaces in ``queue_health`` once it expires.
    """
    from . import node_registry

    conn = storage.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT l.task_id, l.wait_deploy_target, l.wait_deploy_sha, l.wait_until, "
                "t.claimed_by FROM task_liveness l JOIN tasks t ON t.id = l.task_id "
                "WHERE l.wait_deploy_sha IS NOT NULL AND t.archived_at IS NULL "
                "AND t.status NOT IN ('done','cancelled') ORDER BY l.task_id"
            )
        ]
    finally:
        conn.close()
    if not rows:
        return []
    nodes = node_registry.read_nodes()
    reached: list[dict[str, Any]] = []
    for row in rows:
        target, sha = row["wait_deploy_target"], row["wait_deploy_sha"]
        if _deployed_sha(target, nodes, now) != sha:
            continue
        entry = {"task_id": row["task_id"], "target": target, "sha": sha}
        if dry_run:
            reached.append(entry)
            continue
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            won = conn.execute(
                "UPDATE task_liveness SET wait_reason=NULL, wait_until=NULL, wait_next_step=NULL, "
                "wait_deploy_target=NULL, wait_deploy_sha=NULL, wait_set_by=NULL, "
                "wait_first_declared_at=NULL, updated_at=? "
                "WHERE task_id=? AND wait_deploy_target=? AND wait_deploy_sha=? AND wait_until=?",
                (_iso(now), row["task_id"], target, sha, row["wait_until"]),
            ).rowcount
            if won:
                events.record_event_once_on_connection(
                    conn,
                    DEPLOYMENT_CHANNEL,
                    {"kind": "reached", **entry},
                    originator=ACTOR,
                    dedupe_key=f"deployment-wait:{row['task_id']}:{target}:{sha}:{row['wait_until']}",
                )
            conn.commit()
        finally:
            conn.close()
        if won:
            _announce_deployment(row)
            reached.append(entry)
    return reached


def fire_due_monitors(*, now: datetime, dry_run: bool = False) -> list[dict[str, Any]]:
    """Fire every monitor due at ``now``, each at most once.

    The monitor is cleared and its attempt counted in one transaction *before*
    delivery, and only by the caller whose compare-and-set wins, so a crash or
    a concurrent sweep can lose a wake but never send a second one.
    """
    now_iso = _iso(now)
    conn = storage.connect()
    try:
        due = [
            dict(r)
            for r in conn.execute(
                "SELECT l.task_id, l.monitor_at, l.monitor_note, t.id, t.status, t.claimed_by, "
                "t.updated_at FROM task_liveness l JOIN tasks t ON t.id = l.task_id "
                "WHERE l.monitor_at IS NOT NULL AND l.monitor_at <= ? ORDER BY l.monitor_at",
                (now_iso,),
            )
        ]
    finally:
        conn.close()
    fired: list[dict[str, Any]] = []
    for task in due:
        task_id = int(task["task_id"])
        entry: dict[str, Any] = {"task_id": task_id, "monitor_at": task["monitor_at"]}
        if dry_run:
            fired.append({**entry, "delivery": "dry_run"})
            continue
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            won = conn.execute(
                "UPDATE task_liveness SET monitor_at=NULL, monitor_note=NULL, "
                "monitor_attempts=monitor_attempts+1, monitor_fired_at=?, monitor_fired_for=?, "
                "monitor_result='firing', updated_at=? WHERE task_id=? AND monitor_at=?",
                (now_iso, task["monitor_at"], now_iso, task_id, task["monitor_at"]),
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        if not won:
            continue
        claimant = (task["claimed_by"] or "").strip()
        error = None
        if task["status"] in _TERMINAL:
            delivery = "skipped"
            result = f"skipped: task {task['status']}"
        elif claimant:
            try:
                message_id = _wake_claimant(task, task["monitor_at"], task["monitor_note"])
                delivery, result = "message", f"message to {claimant} {message_id}".strip()
            except Exception as exc:  # noqa: BLE001 - fall back to the digest
                error = f"{type(exc).__name__}: {exc}"[:200]
                delivery, result = "digest", f"digest (message refused: {error})"
        else:
            delivery, result = "digest", "digest (task unclaimed)"
        if delivery != "skipped":
            _record(
                {
                    "kind": "monitor_fired",
                    "task_id": task_id,
                    "monitor_at": task["monitor_at"],
                    "note": task["monitor_note"],
                    "claimed_by": claimant or None,
                    "delivery": delivery,
                    "error": error,
                    "task_updated_at": task["updated_at"],
                },
                f"liveness:monitor:{task_id}:{task['monitor_at']}",
            )
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE task_liveness SET monitor_result=? WHERE task_id=?", (result, task_id)
            )
            conn.commit()
        finally:
            conn.close()
        fired.append({**entry, "delivery": delivery, "result": result})
    return fired


def _block(task: Mapping[str, Any], note: str, errors: list[dict[str, Any]]) -> bool:
    """Move one task to blocked through task_update, fenced on the revision read."""
    from .tools import tasks as tasks_tool

    try:
        tasks_tool.task_update(
            int(task["id"]),
            status="blocked",
            append_note=note,
            actor=ACTOR,
            expected_updated_at=task["updated_at"],
        )
    except Exception as exc:  # noqa: BLE001 - one task never fails the sweep
        errors.append({"task_id": int(task["id"]), "error": f"{type(exc).__name__}: {exc}"[:200]})
        return False
    return True


def run_liveness_sweep(
    *, mode: str | None = None, now: datetime | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Fire due monitors, then classify every in-scope task and surface the stalls.

    Idempotent: digest entries are deduplicated per task per UTC day (per
    abandoned run for retry exhaustion), a fired monitor is cleared, and an
    enforced task leaves scope once it is ``blocked``. Never completes,
    reassigns or touches a task with a live claim or open run.
    """
    mode = mode or config.liveness_sweep_mode()
    result: dict[str, Any] = {
        "mode": mode,
        "dry_run": dry_run,
        "checked": 0,
        "monitors": [],
        "none": [],
        "reported": [],
        "blocked": [],
        "retries_exhausted": [],
        "wait_chains": [],
        "deployments": [],
        "errors": [],
    }
    if mode == "off":
        return result
    now = now or _clock()
    result["monitors"] = fire_due_monitors(now=now, dry_run=dry_run)
    result["deployments"] = clear_reached_deployments(now=now, dry_run=dry_run)

    conn = storage.connect()
    try:
        tasks = [
            dict(r)
            for r in conn.execute(
                "SELECT id, status, lane, claimed_by, lock_expires_at, current_run_id, "
                "current_run_agent, updated_at, archived_at, notes FROM tasks "
                "WHERE archived_at IS NULL AND status IN ('open','doing') ORDER BY id"
            )
        ]
        ids = [t["id"] for t in tasks]
        context = gather_context(conn, ids, now=now)
        streaks = abandoned_streaks(conn, ids, now=now)
    finally:
        conn.close()

    day = now.astimezone(UTC).date().isoformat()
    stamp = _iso(now)
    for task in tasks:
        task_id = int(task["id"])
        path = action_path(task, now=now, context=context)
        if in_sweep_scope(task):
            result["checked"] += 1
        since = parse_ts(context.wait_since.get(task_id))
        if (
            path.kind == DECLARED_WAIT
            and in_sweep_scope(task)
            and since is not None
            and now - since > timedelta(days=WAIT_CHAIN_MAX_DAYS)
        ):
            # Reported, not reclassified: the wait still names who moves next.
            detail = (
                f"waits declared back to back since {_iso(since)} "
                f"({(now - since).days} days, over {WAIT_CHAIN_MAX_DAYS}); {path.detail}"
            )
            result["wait_chains"].append({"task_id": task_id, "detail": detail})
            if not dry_run and _record(
                {
                    "kind": "wait_chain",
                    "task_id": task_id,
                    "status": task["status"],
                    "lane": task["lane"],
                    "detail": detail,
                    "mode": mode,
                    "day": day,
                    "task_updated_at": task["updated_at"],
                },
                f"liveness:wait_chain:{task_id}:{day}",
            ):
                result["reported"].append(task_id)
        if path.kind in (RUN, OUT_OF_SCOPE):
            continue
        streak, last_run = streaks.get(task_id, (0, ""))
        if streak >= SELFPICKUP_MAX_ABANDONED and path.base in (QUEUED, NONE):
            detail = (
                f"{streak} consecutive self-pickup runs abandoned (lease lapsed), "
                f"last_run={last_run}"
            )
            result["retries_exhausted"].append({"task_id": task_id, "detail": detail})
            if dry_run:
                continue
            if _record(
                {
                    "kind": "retries_exhausted",
                    "task_id": task_id,
                    "status": task["status"],
                    "lane": task["lane"],
                    "detail": detail,
                    "mode": mode,
                    "task_updated_at": task["updated_at"],
                },
                f"liveness:retries:{task_id}:{last_run}",
            ):
                result["reported"].append(task_id)
            marker = f"last_run={last_run}"
            if mode == "enforce" and marker not in (task["notes"] or ""):
                note = (
                    f"[liveness: retries exhausted @ {stamp}] {detail}; "
                    "recovery: fix the cause, then reopen it or claim it"
                )
                if _block(task, note, result["errors"]):
                    result["blocked"].append(task_id)
            continue

        if path.kind != NONE or not in_sweep_scope(task):
            continue
        touched = parse_ts(task["updated_at"])
        if touched is not None and touched > now - SWEEP_GRACE:
            continue
        result["none"].append({"task_id": task_id, "detail": path.detail})
        if dry_run:
            continue
        if _record(
            {
                "kind": "no_action_path",
                "task_id": task_id,
                "status": task["status"],
                "lane": task["lane"],
                "claimed_by": task["claimed_by"],
                "detail": path.detail,
                "mode": mode,
                "day": day,
                "task_updated_at": task["updated_at"],
            },
            f"liveness:none:{task_id}:{day}",
        ):
            result["reported"].append(task_id)
        if mode == "enforce":
            note = (
                f"[liveness: no action path @ {stamp}] {path.detail}; "
                "recovery: claim it, declare a wait, or set a monitor"
            )
            if _block(task, note, result["errors"]):
                result["blocked"].append(task_id)
    return result


# --- digest read ----------------------------------------------------------------


def digest_findings(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    """Recent liveness entries as anomaly findings (rule ``task_liveness``).

    One finding per task: its latest entry within ``DIGEST_WINDOW``. An entry
    drops out once the task is terminal or has changed since it was recorded
    (someone acted on it; an enforced move is reported as a blocked task
    instead). Monitor wakes delivered as a message are not listed.
    """
    now = now or _clock()
    cutoff = _iso(now - DIGEST_WINDOW)
    latest: dict[int, tuple[dict[str, Any], str]] = {}
    for r in conn.execute(
        "SELECT payload, created_at FROM event_log WHERE channel=? AND created_at >= ? "
        "ORDER BY rowid",
        (CHANNEL, cutoff),
    ):
        try:
            payload = json.loads(r["payload"] or "{}")
            task_id = int(payload["task_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if payload.get("kind") == "monitor_fired" and payload.get("delivery") != "digest":
            continue
        latest[task_id] = (payload, r["created_at"])
    if not latest:
        return []
    current: dict[int, Any] = {}
    ids = sorted(latest)
    for chunk in _chunks(ids):
        for r in conn.execute(
            f"SELECT id, status, updated_at FROM tasks WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            current[int(r["id"])] = r
    findings = []
    for task_id in ids:
        payload, created_at = latest[task_id]
        row = current.get(task_id)
        if row is None or row["status"] in _TERMINAL:
            continue
        if row["updated_at"] != payload.get("task_updated_at"):
            continue
        kind = payload.get("kind")
        if kind == "monitor_fired":
            detail = f"monitor due {payload.get('monitor_at')}"
            if payload.get("note"):
                detail += f": {payload['note']}"
            if payload.get("error"):
                detail += f" (wake message refused: {payload['error']})"
        elif kind == "retries_exhausted":
            detail = f"retries exhausted: {payload.get('detail')}"
        elif kind == "wait_chain":
            detail = f"declared wait chain: {payload.get('detail')}"
        else:
            detail = f"no action path: {payload.get('detail')}"
        findings.append(
            {
                "rule": "task_liveness",
                "subject": f"task:{task_id}",
                "severity": "warning",
                "detail": detail,
                "created_at": created_at,
            }
        )
    return findings
