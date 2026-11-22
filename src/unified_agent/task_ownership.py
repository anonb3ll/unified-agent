"""Who owns a task's next move: the stored record, its transitions, and safe_wait (#2388).

Four columns on ``tasks`` hold the record, and this is the only module that
knows what they mean. ``table_schemas.task_ownership`` owns their DDL and the
one-time backfill.

``next_owner``            the agent responsible for the next move; NULL when none
                          is recorded
``waiting_on``            what that move is waiting for
``terminal_predicate``    the condition that ends the task
``ownership_generation``  counts ownership changes; starts at 0

Ownership is advisory, never a lock. A claim that meets live ownership gets
:func:`safe_wait` back and writes nothing. Only a real lease, or a stored owner
backed by a declared wait or pending monitor that has not ended, is live, so a
stale ``next_owner`` can never wedge a task. Liveness classification still reads
``claimant``, the holder of the lease.

A successor can replace live ownership only on a server-recorded failure
(:func:`predecessor_failure`). A bare second claim stays a ``safe_wait``. A
steward release needs no special case: it ends ownership, so nothing is live.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config

TEXT_MAX_CHARS = 500
CHANNEL = "agent.task.ownership"
# A run that ended one of these ways left its work undone. ``blocked`` and
# ``handed_off`` are a live owner's own decisions.
FAILED_RUN_OUTCOMES = frozenset({"abandoned", "cancelled", "released"})
_BUMP = "ownership_generation=ownership_generation+1"
# SET clauses for the writes that end ownership: closing a task, releasing its claim.
END_OWNERSHIP = ("next_owner=NULL", _BUMP)


@dataclass(frozen=True)
class Ownership:
    """One task's ownership record. Blank stored text reads as ``None``."""

    claimant: str
    next_owner: str | None
    waiting_on: str | None
    terminal_predicate: str | None
    generation: int


def _text(value: Any) -> str | None:
    return (value or "").strip() or None


def read(task: Any) -> Ownership:
    """The ownership record of a task row (a mapping or ``sqlite3.Row``).

    A row selected without the stored columns reads as having no stored owner,
    so callers holding a partial row get the ``claimed_by`` derivation.
    """
    row = dict(task)
    return Ownership(
        claimant=_text(row.get("claimed_by")) or "",
        next_owner=_text(row.get("next_owner")),
        waiting_on=_text(row.get("waiting_on")),
        terminal_predicate=_text(row.get("terminal_predicate")),
        generation=int(row.get("ownership_generation") or 0),
    )


def lease_active(task: Any, now: str) -> bool:
    """Whether the row's claim holds a live lease at ISO timestamp ``now``.

    A lease is expired at its expiry instant (``expires_at <= now``), the
    boundary admission, the claim CAS, stale listing and WIP all share. A claim
    with no expiry is held forever.
    """
    row = dict(task)
    if not row.get("claimed_by"):
        return False
    expires = row.get("lock_expires_at")
    return not expires or now < expires


def safe_wait(
    task: Mapping[str, Any] | Any,
    caller: str,
    *,
    now: str,
    wait_until: str | None = None,
    monitor_at: str | None = None,
) -> dict[str, Any] | None:
    """The wait to return instead of claiming, or ``None`` when the claim may proceed.

    Pure: reads only its arguments. Timestamps are stored UTC ISO strings, which
    compare in order. ``wait_until`` is the end of the task's declared wait,
    ``monitor_at`` its pending monitor. A wait ending at ``now`` is over.
    """
    row = dict(task)
    record = read(row)
    if lease_active(row, now):
        if row["claimed_by"] == caller:
            return None
        until = row.get("lock_expires_at") or None
    else:
        if not record.next_owner or record.next_owner == caller:
            return None
        until = next((at for at in (wait_until, monitor_at) if at and now < at), None)
        if until is None:
            return None
    return {
        "owner": record.next_owner or record.claimant,
        "waiting_on": record.waiting_on,
        "until": until,
        "generation": record.generation,
        "claimed_by": record.claimant or None,
    }


def predecessor_failure(
    task: Mapping[str, Any] | Any, end: Mapping[str, Any] | None, *, acted_at: str | None
) -> dict[str, Any] | None:
    """The recorded failure that lets a successor replace live ownership, or ``None``.

    Pure. ``end`` is the finish record of the task's latest run and ``acted_at``
    when its wait or monitor last changed. The owner's own run must have ended
    without completing; a hold set after that end shows the owner is still acting.
    """
    record = read(task)
    owner = record.next_owner or record.claimant
    if not owner or end is None:
        return None
    if end.get("agent") != owner or end.get("outcome") not in FAILED_RUN_OUTCOMES:
        return None
    finished_at = str(end.get("finished_at") or "")
    if acted_at is not None and acted_at > finished_at:
        return None
    return {
        "predecessor": owner,
        "run_id": end.get("run_id"),
        "outcome": end.get("outcome"),
        "finished_at": finished_at,
        "generation": record.generation,
    }


# Report only (task #2388): nothing acts on these. Precedence is table order.
SLEEPING = "sleeping"  # a wait or monitor still running: the owner said when it returns
ZOMBIE = "zombie"  # one half of a claim survived: a live lease with its run over, or the reverse
DEAD = "dead"  # in doing with no live lease and no open run: nobody will finish it
SHELVED = "shelved"  # an owner's wait or monitor ran out and nobody picked it back up
VOID = "void"  # an owner is recorded, with no claim and no hold behind it
STATES = (SLEEPING, ZOMBIE, DEAD, SHELVED, VOID)


@dataclass(frozen=True)
class StuckFacts:
    """What the classifier reads. ``lease``: live, lapsed, none. ``hold``: live,
    expired, none. ``run``: open, ended, none."""

    status: str
    owner: str | None
    lease: str
    hold: str
    run: str


_STATE_TABLE = (
    (SLEEPING, lambda f: f.hold == "live"),
    (
        ZOMBIE,
        lambda f: (
            (f.lease == "live" and f.run == "ended") or (f.lease == "lapsed" and f.run == "open")
        ),
    ),
    (DEAD, lambda f: f.status == "doing" and f.lease != "live" and f.run != "open"),
    (SHELVED, lambda f: bool(f.owner) and f.lease != "live" and f.hold == "expired"),
    (VOID, lambda f: bool(f.owner) and f.lease == "none" and f.hold == "none"),
)


def classify(facts: StuckFacts) -> str | None:
    """The first table row that holds, or ``None`` for work with nothing wrong."""
    return next((state for state, holds in _STATE_TABLE if holds(facts)), None)


def stuck_facts(
    task: Any, *, now: str, wait_until: str | None, monitor_at: str | None, run: str
) -> StuckFacts:
    """Build the classifier's input from a task row and its hold, at ISO ``now``."""
    row = dict(task)
    lease = "none" if not row.get("claimed_by") else "live" if lease_active(row, now) else "lapsed"
    holds = [at for at in (wait_until, monitor_at) if at]
    hold = "none" if not holds else "live" if any(now < at for at in holds) else "expired"
    return StuckFacts(str(row.get("status")), read(row).next_owner, lease, hold, run)


def is_safe_wait(result: Any) -> bool:
    """True for the ``task_claim`` answer that means *not claimed*.

    It is a returned dict with an ``id``, so a caller that only checks for a
    dict or an id reads it as a claim. Every consumer must check this first.
    """
    return isinstance(result, dict) and "safe_wait" in result


def describe_wait(wait: Mapping[str, Any]) -> str:
    """One human line naming a ``safe_wait``, for logs and refusals."""
    until = wait.get("until") or "no expiry"
    waiting_on = f", waiting on {wait['waiting_on']}" if wait.get("waiting_on") else ""
    return f"held by {wait.get('owner')} until {until}{waiting_on}"


def after_claim(task: Any, agent: str, *, renewal: bool) -> tuple[Any, Any, int]:
    """``(next_owner, waiting_on, generation)`` to store with a successful claim."""
    row = dict(task)
    generation = int(row.get("ownership_generation") or 0)
    if renewal:
        return row.get("next_owner"), row.get("waiting_on"), generation
    return agent, None, generation + 1


def _bounded(name: str, value: Any) -> str | None:
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    text = value.strip()
    if len(text) > TEXT_MAX_CHARS:
        raise ToolError(f"{name} must be at most {TEXT_MAX_CHARS} characters")
    return text or None


def normalize_update(
    *, next_owner: Any, waiting_on: Any, terminal_predicate: Any
) -> dict[str, str | None] | None:
    """Validate ``task_update``'s ownership parameters; ``None`` when none was passed.

    Returns the columns being changed. An empty string clears to NULL. Raises
    ``ToolError`` before any write.
    """
    spec: dict[str, str | None] = {}
    if next_owner is not None:
        owner = _bounded("next_owner", next_owner)
        if owner is not None and not config.is_known_agent_label(owner):
            raise ToolError(config.unknown_agent_label_error("next_owner", owner))
        spec["next_owner"] = owner
    if waiting_on is not None:
        spec["waiting_on"] = _bounded("waiting_on", waiting_on)
    if terminal_predicate is not None:
        spec["terminal_predicate"] = _bounded("terminal_predicate", terminal_predicate)
    return spec or None


def update_sets(
    task: Any, spec: Mapping[str, str | None], *, ended: bool
) -> tuple[list[str], list[Any]]:
    """SET clauses and values for a normalized spec.

    The generation counts owner changes only. ``ended`` means this same write
    applies :data:`END_OWNERSHIP`, which already settles ``next_owner``.
    """
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in spec.items():
        if column == "next_owner":
            if ended or value == read(task).next_owner:
                continue
            clauses.append(_BUMP)
        clauses.append(f"{column}=?")
        values.append(value)
    return clauses, values
