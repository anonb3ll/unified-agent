"""One read of whether a lease owner is still live (task #2395, slice 1).

Liveness is already recorded in three places. This module reads them and
does nothing else:

* agent last-seen, graded by :func:`registry.derive_status` — the same rule
  ``mcp_live_state`` publishes for an agent;
* node heartbeats, graded by :func:`node_registry.node_status`;
* the task's action path, from :func:`task_liveness.action_path`.

A stale claim lease is only a candidate. It never decides the outcome. The
probe answers ``active``, ``terminal``, ``absent`` or ``unknown``. ``active``
and ``unknown`` keep the owner; ``unknown`` also carries a warning. A terminal
reading needs a record: the task is done or cancelled, or on a lapsed ``doing``
claim the owner's own run is recorded as over (a failed self-pickup end, a
cancel request, a failed dispatch run or executor attempt). A quiet agent or
node is not a recorded end, and neither is the age of a lease. One more record
counts, but only while the agent is not seen live and no node of it is online:
every hub session the owner had is recorded as ended (completed, failed or
reaped as abandoned), the last one after this claim was taken, and none is open.
That covers an interactive session that died without writing anything about
its run. ``absent`` means the owner has no agent row and no node at all. This function never writes, so a
terminal or absent result does not clear a claim.

The reaper in :mod:`unified_agent.lease_reaper` is the writer that follows
this read. Later slices own the interrupted-work witness and any
``agent.run.stalled`` event. The executor stall timeout (1800s, against the
300s wall clock) is not consulted here.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import node_registry, registry, run_records, task_liveness, task_ownership

logger = logging.getLogger(__name__)

ACTIVE = "active"
TERMINAL = "terminal"
ABSENT = "absent"
UNKNOWN = "unknown"
OUTCOMES = (ACTIVE, TERMINAL, ABSENT, UNKNOWN)

# derive_status buckets that are positive evidence the agent was just seen.
# stale and offline are quiet, not a recorded end, so they stay inconclusive.
_LIVE_AGENT = frozenset({"online", "idle"})
# The task statuses task_liveness classifies as out of scope because the work ended.
_TERMINAL_TASK = frozenset({"done", "cancelled"})
_NODE_RANK = ("online", "idle", "stale", "offline", "unknown")
# A lapsed doing claim whose owner's run is recorded as over.
_RUN_ENDED = "ended"
# A lapsed doing claim whose owner has no open hub session, all ended after the claim.
_SESSIONS_ENDED = "sessions_ended"
_OPEN_SESSION = ("starting", "running", "waiting")
_FAILED_DISPATCH = frozenset({"failed", "cancelled", "expired"})
_FAILED_ATTEMPT = frozenset({"failure", "killed"})
_TASK_SQL = (
    "SELECT id, status, lane, claimed_by, claimed_at, lock_expires_at, current_run_id, "
    "current_run_agent, cancel_requested, next_owner, ownership_generation, updated_at, notes "
    "FROM tasks WHERE id=?"
)


@dataclass(frozen=True)
class OwnerProbe:
    """The three sources, reduced to one outcome. ``sources`` is (name, reading)."""

    outcome: str
    owner: str
    keep_owner: bool
    warning: str | None
    candidate: bool
    sources: tuple[tuple[str, str], ...]

    def source(self, name: str) -> str:
        for key, value in self.sources:
            if key == name:
                return value
        raise KeyError(name)


def probe_owner(
    conn: sqlite3.Connection,
    owner: str,
    *,
    task_id: int | None = None,
    now: datetime | None = None,
) -> OwnerProbe:
    """Read one owner's liveness. ``owner`` is the agent label on the lease.

    ``task_id`` selects the claim to judge. Without it, the task source is
    absent and only the agent and node readings apply. ``now`` is the clock;
    tests pass a fixed instant so a quiet reading does not depend on the wall.
    """
    label = str(owner or "").strip()
    if not label:
        raise ValueError("owner is required")
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    agent = _agent_fact(conn, label, reference)
    node = _node_fact(label, reference)
    task, candidate = _task_fact(conn, label, task_id, reference)
    outcome = _outcome(agent, node, task)
    warning = _warning(label, outcome, candidate, agent, node, task)
    if outcome == UNKNOWN:
        logger.warning("%s", warning)
    return OwnerProbe(
        outcome=outcome,
        owner=label,
        keep_owner=outcome in (ACTIVE, UNKNOWN),
        warning=warning,
        candidate=candidate,
        sources=(("agent", agent), ("node", node), ("task", task)),
    )


def _outcome(agent: str, node: str, task: str) -> str:
    """A recorded end first, then positive evidence.

    A done task, or a recorded end of the owner's run, is terminal whoever is
    online: an agent label is shared by its sessions, so a live owner says
    nothing about this claim. The task row was read to reach that reading, and a
    failed read never becomes absent or terminal.
    """
    if task in (TERMINAL, _RUN_ENDED):
        return TERMINAL
    if agent in _LIVE_AGENT or node == "online" or task == "run":
        return ACTIVE
    if "unreadable" in (agent, node, task):
        return UNKNOWN
    if task == _SESSIONS_ENDED:
        # Weaker than a run record: it holds only with no live agent or node.
        return TERMINAL
    if agent == ABSENT and node == ABSENT and task in (ABSENT, "lapsed"):
        return ABSENT
    return UNKNOWN


def _warning(
    owner: str, outcome: str, candidate: bool, agent: str, node: str, task: str
) -> str | None:
    if outcome != UNKNOWN:
        return None
    lease = " a stale lease is a candidate only;" if candidate else ""
    return (
        f"owner kept:{lease} {owner} is not shown active, terminal, or absent "
        f"(agent {agent}, node {node}, task {task})"
    )


def _agent_fact(conn: sqlite3.Connection, label: str, now: datetime) -> str:
    try:
        row = conn.execute("SELECT last_seen FROM agents WHERE label=?", (label,)).fetchone()
    except sqlite3.Error:
        return "unreadable"
    if row is None:
        return ABSENT
    return registry.derive_status(row["last_seen"], now=now)


def _node_fact(label: str, now: datetime) -> str:
    """The best heartbeat among this agent's nodes, via the shared node reader.

    ``read_nodes`` applies the heartbeat floor, and ``node_status`` is online
    only while that beat is inside the node live window. A node that is merely
    idle, stale, offline or unknown is not active.
    """
    try:
        listed = node_registry.read_nodes()
    except sqlite3.Error:
        return "unreadable"
    statuses = [
        node_registry.node_status(info, now=now)
        for info in listed.values()
        if info.get("agent") == label
    ]
    if not statuses:
        return ABSENT
    for status in _NODE_RANK:
        if status in statuses:
            return status
    return "unknown"


def _task_fact(
    conn: sqlite3.Connection,
    label: str,
    task_id: int | None,
    now: datetime,
) -> tuple[str, bool]:
    """``(reading, candidate)``. A lapsed lease is a candidate, not a verdict.

    ``run`` only when this owner holds a live claim or the task's action path
    is still a run of theirs (an open dispatch, node item or executor attempt).
    Someone else's live claim does not make this owner active.
    """
    if task_id is None:
        return ABSENT, False
    try:
        row = conn.execute(_TASK_SQL, (task_id,)).fetchone()
    except sqlite3.Error:
        return "unreadable", False
    if row is None:
        return ABSENT, False
    task = _row(row)
    status = task.get("status")
    claimant = (task.get("claimed_by") or "").strip()
    lease = task_liveness.parse_ts(task.get("lock_expires_at"))
    candidate = (
        status not in _TERMINAL_TASK and claimant == label and lease is not None and lease <= now
    )
    if status in _TERMINAL_TASK:
        return TERMINAL, False
    try:
        context = task_liveness.gather_context(conn, [int(task["id"])], now=now)
        path = task_liveness.action_path(task, now=now, context=context)
        if claimant == label and path.base == task_liveness.RUN:
            return "run", candidate
        if candidate and int(task["id"]) not in context.queued and _run_is_over(conn, task, label):
            return _RUN_ENDED, True
        if candidate and _sessions_all_ended(conn, task, label):
            return _SESSIONS_ENDED, True
    except sqlite3.Error:
        return "unreadable", candidate
    if candidate:
        return "lapsed", True
    return "quiet", False


def _run_is_over(conn: sqlite3.Connection, task: dict[str, Any], label: str) -> bool:
    """Whether this claim's run is recorded as ended without finishing the work.

    Only records count, and only records from this claim: an end older than
    ``claimed_at`` belongs to an earlier claim. No open or queued run exists when
    this is asked (the caller checked), so a retry still pending is not an end.
    """
    task_id = int(task["id"])
    if task.get("cancel_requested") and (task.get("current_run_agent") or label) == label:
        return True
    claimed_at = task_liveness.parse_ts(task.get("claimed_at"))

    def this_claim(stamp: Any) -> bool:
        if claimed_at is None:
            return True
        at = task_liveness.parse_ts(stamp)
        return at is not None and at >= claimed_at

    acted = conn.execute(
        "SELECT updated_at FROM task_liveness WHERE task_id=?", (task_id,)
    ).fetchone()
    end = run_records.latest_run_end(conn, task_id)
    failed = task_ownership.predecessor_failure(
        task, end, acted_at=acted["updated_at"] if acted else None
    )
    if failed is not None and this_claim(failed["finished_at"]):
        return True
    dispatch = conn.execute(
        "SELECT state, updated_at FROM dispatch_runs WHERE task_id=? AND agent_label=? "
        "ORDER BY created_at DESC, run_id DESC LIMIT 1",
        (task_id, label),
    ).fetchone()
    if dispatch is not None and dispatch["state"] in _FAILED_DISPATCH:
        if this_claim(dispatch["updated_at"]):
            return True
    attempt = conn.execute(
        "SELECT outcome, finished_at FROM executor_attempts WHERE task_id=? AND agent=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, label),
    ).fetchone()
    return (
        attempt is not None
        and attempt["outcome"] in _FAILED_ATTEMPT
        and attempt["finished_at"] is not None
        and this_claim(attempt["finished_at"])
    )


def _sessions_all_ended(conn: sqlite3.Connection, task: dict[str, Any], label: str) -> bool:
    """Every hub session of ``label`` is recorded ended, the last after this claim.

    Needs ``claimed_at``: without it there is no way to tell an end that came
    after the claim from one that came before it.
    """
    claimed_at = task_liveness.parse_ts(task.get("claimed_at"))
    if claimed_at is None:
        return False
    marks = ",".join("?" * len(_OPEN_SESSION))
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(status IN ({marks})), 0) AS open_n, "
        "MAX(ended_at) AS last_end FROM agent_sessions WHERE agent=?",
        (*_OPEN_SESSION, label),
    ).fetchone()
    if row is None or not row["n"] or row["open_n"]:
        return False
    last_end = task_liveness.parse_ts(row["last_end"])
    return last_end is not None and last_end >= claimed_at


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
