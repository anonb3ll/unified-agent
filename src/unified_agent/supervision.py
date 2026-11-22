"""FirstMate-style zero-token event-driven supervision (task #1028).

FirstMate supervises a crew with a bash watcher that wakes the liaison only when
a human decision matters — completion, block, or needs-decision — and stays
silent for routine progress. unified-agent already mirrors every task status
change onto ``agent.task.updated``; that channel is too noisy for a COS wake.

This module is the zero-token classifier + wake emitter:

* ``classify_task_transition`` — pure; no I/O, no LLM.
* ``maybe_emit_supervisor_wake`` — durable ``agent.supervisor.wake`` only when
  the transition is captain-relevant; silent for routine progress.
* ``drain_supervisor_wakes`` — cursor replay for briefing/cadence consumers so
  they resume from events instead of polling the task board.

Wakes are appended at the mutation site (``task_update``), not by a periodic
poll loop. Gated by ``UA_EVENT_SUPERVISION`` (default on).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from . import config, events

logger = logging.getLogger("unified_agent.supervision")

CHANNEL_SUPERVISOR_WAKE = events.CHANNEL_SUPERVISOR_WAKE

WakeReason = Literal["completion", "block", "needs-decision"]
DecisionKind = Literal["wake", "silence"]

# Captain-relevant terminals (FirstMate: done / blocked / needs-decision).
# ``waiting_review`` is the hub's needs-decision posture — a human must act.
_WAKE_BY_STATUS: dict[str, WakeReason] = {
    "done": "completion",
    "blocked": "block",
    "waiting_review": "needs-decision",
}

# Explicit routine statuses — progress the crew handles without waking COS.
_ROUTINE_STATUSES = frozenset({"open", "doing", "cancelled"})


@dataclass(frozen=True)
class WakeDecision:
    """Classifier result for one status transition."""

    kind: DecisionKind
    reason: WakeReason | Literal["routine", "no-change", "unknown"]
    old_status: str | None
    new_status: str | None

    @property
    def should_wake(self) -> bool:
        return self.kind == "wake"


def event_supervision_enabled() -> bool:
    """Whether supervisor wakes are emitted (``UA_EVENT_SUPERVISION=0`` disables)."""
    return config.event_supervision_enabled()


def classify_task_transition(
    old_status: str | None,
    new_status: str | None,
) -> WakeDecision:
    """Classify a task status transition — pure, zero-token.

    Wakes only when the *new* status is captain-relevant and differs from the
    old status. Routine progress (``open`` / ``doing`` / ``cancelled``),
    no-ops, and unknown statuses stay silent.
    """
    old = (old_status or "").strip() or None
    new = (new_status or "").strip() or None
    if new is None or new == old:
        return WakeDecision(
            kind="silence",
            reason="no-change",
            old_status=old,
            new_status=new,
        )
    wake_reason = _WAKE_BY_STATUS.get(new)
    if wake_reason is not None:
        return WakeDecision(
            kind="wake",
            reason=wake_reason,
            old_status=old,
            new_status=new,
        )
    if new in _ROUTINE_STATUSES:
        return WakeDecision(
            kind="silence",
            reason="routine",
            old_status=old,
            new_status=new,
        )
    return WakeDecision(
        kind="silence",
        reason="unknown",
        old_status=old,
        new_status=new,
    )


def maybe_emit_supervisor_wake(
    *,
    task_id: int,
    old_status: str | None,
    new_status: str | None,
    title: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any] | None:
    """Emit ``agent.supervisor.wake`` when the transition needs a human decision.

    Returns the wake payload (including ``event_ulid``) on emit, or ``None``
    when classified as silence / disabled. Never raises — a wake failure must
    not break ``task_update``.
    """
    if not event_supervision_enabled():
        return None
    decision = classify_task_transition(old_status, new_status)
    if not decision.should_wake:
        return None

    tid = int(task_id)
    reason = decision.reason
    assert reason in ("completion", "block", "needs-decision")
    stamp = (updated_at or "").strip() or "nostamp"
    dedupe_key = f"supervisor-wake:{tid}:{decision.new_status}:{stamp}"
    payload: dict[str, Any] = {
        "task_id": tid,
        "old_status": decision.old_status,
        "new_status": decision.new_status,
        "reason": reason,
        "title": (title or "")[:200] or None,
        "project": project,
    }
    try:
        ulid = events.record_event(
            CHANNEL_SUPERVISOR_WAKE,
            payload=payload,
            originator=agent,
            dedupe_key=dedupe_key,
        )
    except Exception:
        logger.debug(
            "supervisor wake emit failed for task %s (%s→%s)",
            tid,
            decision.old_status,
            decision.new_status,
            exc_info=True,
        )
        return None
    out = dict(payload)
    out["event_ulid"] = ulid
    out["channel"] = CHANNEL_SUPERVISOR_WAKE
    return out


def drain_supervisor_wakes(
    *,
    since: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Cursor-replay pending supervisor wakes for briefing/cadence (no poll).

    Pass back ``cursor`` as ``since`` on the next drain. Empty result with a
    nonzero ``source_event_count`` means an idle window, not a missing writer.
    """
    cap = max(1, min(int(limit), 500))
    rows = events.query_events(
        channels=[CHANNEL_SUPERVISOR_WAKE],
        since=since,
        limit=cap,
    )
    wakes = [_wake_from_row(row) for row in rows]
    liveness = events.event_log_liveness(channels=[CHANNEL_SUPERVISOR_WAKE])
    return {
        "wakes": wakes,
        "count": len(wakes),
        "cursor": rows[-1]["ulid"] if rows else since,
        "channel": CHANNEL_SUPERVISOR_WAKE,
        **liveness,
    }


def recent_supervisor_wakes(*, limit: int = 50) -> dict[str, Any]:
    """Newest supervisor wakes in chronological order (briefing footer)."""
    cap = max(1, min(int(limit), 200))
    rows = events.query_recent_events(channels=[CHANNEL_SUPERVISOR_WAKE], limit=cap)
    wakes = [_wake_from_row(row) for row in rows]
    return {
        "wakes": wakes,
        "count": len(wakes),
        "channel": CHANNEL_SUPERVISOR_WAKE,
    }


def _wake_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "ulid": row["ulid"],
        "created_at": row.get("created_at"),
        "originator": row.get("originator"),
        "task_id": payload.get("task_id"),
        "old_status": payload.get("old_status"),
        "new_status": payload.get("new_status"),
        "reason": payload.get("reason"),
        "title": payload.get("title"),
        "project": payload.get("project"),
    }
