"""Structured, additive evidence for executor trust-ladder review.

This module records what happened; it does not decide whether execution is
allowed.  The vocabulary is deliberately finite so reports can count rows
without parsing task prose or human notes.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import events, storage

# Same logger name as executor.daemon, so evidence warnings land beside the
# daemon's own executor log lines.
logger = logging.getLogger("unified_agent.executor")

TASK_TYPES = ("code", "docs", "research", "scout", "review", "operations", "unknown")
EXECUTOR_OUTCOMES = (
    "success",
    "failure",
    "blocked",
    "quarantined",
    "cooldown",
    "killed",
    "waiting_review",
    "unknown",
)
DELEGATION_OUTCOMES = (
    "accepted",
    "completed",
    "failed",
    "cancelled",
    "escalated",
    "unknown",
)
APPROVAL_OUTCOMES = (
    "requested",
    "approved",
    "denied",
    "consumed",
    "revoked",
    "expired",
    "unknown",
)


def normalize(value: str | None, allowed: tuple[str, ...], default: str = "unknown") -> str:
    value = (value or "").strip().lower()
    return value if value in allowed else default


def delegation_outcome(status: str | None) -> str:
    return normalize(status, DELEGATION_OUTCOMES)


def approval_outcome(status: str | None) -> str:
    """Normalize an `approvals.status` value into the evidence vocabulary.

    The `approvals` table's real status is 'pending' (storage.py's CHECK
    constraint); APPROVAL_OUTCOMES spells the same state 'requested'. Without
    this translation every ordinary pending approval normalized to 'unknown',
    misclassifying all outstanding requests in the Control Center evidence
    (task #445 / PR #201 review).
    """
    if (status or "").strip().lower() == "pending":
        return "requested"
    return normalize(status, APPROVAL_OUTCOMES)


def executor_outcome(status: str | None, *, waiting_for_review: bool = False) -> str:
    if waiting_for_review:
        return "waiting_review"
    return normalize(status, EXECUTOR_OUTCOMES, "failure" if status else "unknown")


def start_attempt(
    *,
    task_id: int,
    delegation_id: int | None,
    agent: str,
    backend: str,
    task_type: str = "unknown",
    model: str | None = None,
) -> int | None:
    """Persist an attempt before invoking a backend; return its row id."""
    try:
        conn = storage.connect()
        try:
            cur = conn.execute(
                "INSERT INTO executor_attempts "
                "(task_id, delegation_id, agent, backend, model, task_type, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    delegation_id,
                    agent,
                    backend,
                    model,
                    normalize(task_type, TASK_TYPES),
                    storage.now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
    except Exception as exc:
        # Best-effort by design: evidence must never break execution. But a
        # swallowed failure silently dropped every scout attempt (task #1543),
        # so say so.
        logger.warning(
            "executor evidence: could not record attempt for task %s (agent=%s, "
            "backend=%s, task_type=%s): %s",
            task_id,
            agent,
            backend,
            task_type,
            exc,
        )
        return None


def finish_attempt(
    attempt_id: int | None,
    *,
    status: str | None,
    waiting_for_review: bool = False,
    detail: str | None = None,
) -> None:
    if attempt_id is None:
        return
    outcome = executor_outcome(status, waiting_for_review=waiting_for_review)
    failure_class = None
    if outcome in {"failure", "blocked", "quarantined", "cooldown", "killed"}:
        from ..quota_observe import classify_failure_reason

        failure_class = classify_failure_reason(detail)
    try:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE executor_attempts SET finished_at=?, status=?, outcome=?, detail=?, "
                "failure_class=? WHERE id=?",
                (
                    storage.now(),
                    status or "unknown",
                    outcome,
                    (detail or "")[:500] or None,
                    failure_class,
                    attempt_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def record_executor_event(
    kind: str, *, task_id: int, delegation_id: int, agent: str, reason: str
) -> None:
    """Write a durable, structured event; never changes the policy path."""
    channel = f"agent.executor.{kind}"
    try:
        events.record_event(
            channel,
            {
                "task_id": task_id,
                "delegation_id": delegation_id,
                "agent": agent,
                "reason": reason[:500],
            },
            originator=agent,
            notify=True,
        )
    except Exception:
        pass


def outcomes_snapshot() -> dict[str, Any]:
    """Read-only aggregate used by health/report consumers."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS count FROM executor_attempts GROUP BY outcome"
        ).fetchall()
        return {"attempts": {r["outcome"]: r["count"] for r in rows}}
    finally:
        conn.close()
