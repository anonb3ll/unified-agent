"""First-mate supervisor loop, slice 1: inbox drain and dry-run planner (task #1918).

Design: ``docs/designs/2026-10-06-first-mate-supervisor-loop.md``.

The supervisor inbox is not a new store. It is a filtered view of ``event_log``
(the channels in ``INBOX_CHANNELS``) plus one cursor row per consumer in
``supervisor_cursors``. A deterministic watcher (no LLM) drains the view,
coalesces it and decides, per event, one of:

* ``wake``      - wake the supervisor session (coalesced to one per drain);
* ``route``     - message the task's ``claimed_by`` (CI failure, review comment);
* ``review``    - hand to the review pipeline (task reached ``waiting_review``);
* ``reconcile`` - hand to the stall reconciler (#1916).

Slice 1 only plans. ``run_once`` refuses ``dry_run=False`` and the CLI requires
``--once --dry-run``: nothing is sent, woken, launched or acknowledged, and the
cursor is never advanced by the CLI. The cursor primitives are here and tested
so slice 2 can act and then commit.

Safety posture (holds in every later slice):

* The planner copies only structured fields (task ids, enum states, ``seq``)
  into a decision. Titles, bodies, summaries and originators are data and
  never reach a decision, a message or a wake line.
* ``ACTIONS`` has no merge and no approval resolution. An approval request
  surfaces as a wake; only a human resolves it.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from . import events, storage
from .supervision import CHANNEL_SUPERVISOR_WAKE

# Producers that exist today.
CHANNEL_BUDGET = "agent.budget"  # budgets.CHANNEL; literal to avoid importing policy code
CHANNEL_APPROVAL = events.CHANNEL_APPROVAL_REQUESTED
CHANNEL_HEALTH = events.CHANNEL_HEALTH_ALERT
# Reserved for producers the design adds in later slices. Draining them now is
# harmless: no rows exist until a producer writes them.
# agent.run.stalled: lease_reaper writes run.interrupted here (task #2395).
CHANNEL_RUN_STALLED = "agent.run.stalled"  # stall reconciler, task #1916
CHANNEL_PR_CI = "agent.pr.ci"  # pr-watch CI rollup per task-linked PR head
CHANNEL_PR_REVIEW = "agent.pr.review"  # pr-watch review activity per task-linked PR

INBOX_CHANNELS: tuple[str, ...] = (
    CHANNEL_SUPERVISOR_WAKE,
    CHANNEL_BUDGET,
    CHANNEL_APPROVAL,
    CHANNEL_HEALTH,
    CHANNEL_RUN_STALLED,
    CHANNEL_PR_CI,
    CHANNEL_PR_REVIEW,
)

Action = Literal["wake", "route", "review", "reconcile"]
ACTIONS: frozenset[str] = frozenset({"wake", "route", "review", "reconcile"})

DEFAULT_CONSUMER = "first-mate"
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
DEFAULT_LOOKBACK = 1000

_CI_FAIL_STATES = frozenset({"failure", "error", "timed_out", "cancelled", "action_required"})
_REVIEW_ROUTE_STATES = {
    "commented": "review-comment",
    "changes_requested": "changes-requested",
}
_WAKE_REASONS = frozenset({"completion", "block"})


@dataclass(frozen=True)
class Decision:
    """One planned reaction. Only structured fields, never payload text."""

    action: Action
    target: str
    task_id: int | None
    reasons: tuple[str, ...]
    source_seqs: tuple[int, ...]
    task_ids: tuple[int, ...] = ()
    head_sha: str | None = None
    review_seq: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "task_id": self.task_id,
            "task_ids": list(self.task_ids),
            "reasons": list(self.reasons),
            "source_seqs": list(self.source_seqs),
            "head_sha": self.head_sha,
            "review_seq": self.review_seq,
        }


@dataclass
class _Acc:
    action: Action
    target: str
    task_id: int | None
    reasons: list[str] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    task_ids: list[int] = field(default_factory=list)
    head_sha: str | None = None
    review_seq: int | None = None

    def add(self, reason: str, seq: int, task_id: int | None) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.seqs.append(seq)
        if task_id is not None and task_id not in self.task_ids:
            self.task_ids.append(task_id)

    def freeze(self) -> Decision:
        return Decision(
            action=self.action,
            target=self.target,
            task_id=self.task_id,
            reasons=tuple(self.reasons),
            source_seqs=tuple(self.seqs),
            task_ids=tuple(self.task_ids),
            head_sha=self.head_sha,
            review_seq=self.review_seq,
        )


def _task_id(payload: dict[str, Any]) -> int | None:
    raw = payload.get("task_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdigit() and raw != "0":
        return int(raw)
    return None


def _state(payload: dict[str, Any], key: str = "state") -> str:
    raw = payload.get(key)
    return raw.strip().lower() if isinstance(raw, str) else ""


def _classify(
    event: dict[str, Any], owner_of: Callable[[int], str | None]
) -> tuple[Action, str, int | None, str] | None:
    """Map one event to ``(action, target, task_id, reason)`` or ``None`` (silence)."""
    channel = event.get("channel")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    tid = _task_id(payload)

    if channel == CHANNEL_SUPERVISOR_WAKE:
        reason = _state(payload, "reason")
        if reason == "needs-decision" and tid is not None:
            return ("review", "reviewer", tid, "waiting-review")
        if reason in _WAKE_REASONS:
            return ("wake", "supervisor", tid, reason)
        return None
    if channel == CHANNEL_BUDGET:
        kind = _state(payload, "kind")
        if kind in ("budget.warning", "budget.blocked"):
            return ("wake", "supervisor", tid, kind)
        return None
    if channel == CHANNEL_APPROVAL:
        # The approval mirror carries requests and resolutions on one channel;
        # only a request ("queued...") needs a human to look.
        if _state(payload, "summary").startswith("queued"):
            return ("wake", "supervisor", None, "approval-pending")
        return None
    if channel == CHANNEL_HEALTH:
        return ("wake", "supervisor", tid, "health-alert")
    if channel == CHANNEL_RUN_STALLED:
        return ("reconcile", "reconciler", tid, "stalled")
    if channel == CHANNEL_PR_CI:
        if _state(payload) not in _CI_FAIL_STATES:
            return None
        return _route_or_wake(tid, "ci-failure", owner_of)
    if channel == CHANNEL_PR_REVIEW:
        reason = _REVIEW_ROUTE_STATES.get(_state(payload))
        if reason is None:
            return None
        return _route_or_wake(tid, reason, owner_of)
    return None


def _route_or_wake(
    tid: int | None, reason: str, owner_of: Callable[[int], str | None]
) -> tuple[Action, str, int | None, str]:
    owner = owner_of(tid) if tid is not None else None
    if owner:
        return ("route", owner, tid, reason)
    # Nobody owns it: the supervisor decides, rather than the event vanishing.
    return ("wake", "supervisor", tid, f"unowned:{reason}")


def _head_sha(payload: dict[str, Any]) -> str | None:
    raw = payload.get("head_sha")
    return (
        raw.lower() if isinstance(raw, str) and re.fullmatch(r"[a-fA-F0-9]{40,64}", raw) else None
    )


def plan_reactions(
    rows: Iterable[dict[str, Any]],
    *,
    owner_of: Callable[[int], str | None] = lambda _tid: None,
) -> list[Decision]:
    """Pure, zero-token planner over drained inbox rows (oldest first).

    * Latest state wins per (task, head SHA) for PR CI: a failure followed by
      a success in the same drain routes nothing.
    * CI routes coalesce per task/head; each review event retains its own seq.
    * Other reviews and reconciles coalesce per (action, target, task).
    * All wake reasons coalesce into one supervisor wake per drain.
    """
    rows = list(rows)
    latest_ci: dict[tuple[int | None, str | None], int] = {}
    for row in rows:
        if row.get("channel") == CHANNEL_PR_CI:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            tid = _task_id(payload)
            latest_ci[(tid, _head_sha(payload))] = int(row["seq"])

    accs: dict[tuple[Any, ...], _Acc] = {}
    for row in rows:
        seq = int(row["seq"])
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        head_sha = _head_sha(payload)
        if row.get("channel") == CHANNEL_PR_CI:
            tid = _task_id(payload)
            if seq != latest_ci[(tid, head_sha)]:
                continue  # only the latest state for this task/head describes outstanding CI
        classified = _classify(row, owner_of)
        if classified is None:
            continue
        action, target, tid, reason = classified
        review_seq = seq if row.get("channel") == CHANNEL_PR_REVIEW else None
        key = (
            (action, target, None)
            if action == "wake"
            else (action, target, tid, head_sha, review_seq)
        )
        acc = accs.get(key)
        if acc is None:
            acc = accs[key] = _Acc(
                action=action,
                target=target,
                task_id=key[2],
                head_sha=head_sha if action != "wake" else None,
                review_seq=review_seq if action != "wake" else None,
            )
        acc.add(reason, seq, tid)

    return sorted((a.freeze() for a in accs.values()), key=lambda d: d.source_seqs[0])


# --- cursor -------------------------------------------------------------------


def load_cursor(consumer: str) -> int | None:
    """Stored ``after_seq`` for ``consumer``; ``None`` if absent or table missing."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT after_seq FROM supervisor_cursors WHERE consumer = ?", (consumer,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # Only a pre-migration schema may fall back; locked/corrupt reads must fail.
        if str(exc) != "no such table: supervisor_cursors":
            raise
        return None
    finally:
        conn.close()
    return int(row["after_seq"]) if row else None


def commit_cursor(consumer: str, after_seq: int, *, expected: int | None) -> bool:
    """Compare-and-swap the cursor. Never moves backwards.

    ``expected`` is the value this watcher read before draining (``None`` when
    absent). A second watcher that drained the same window loses the swap, so
    it must not act on its plan.
    """
    new = int(after_seq)
    if new < 0:
        raise ValueError("after_seq must be >= 0")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT after_seq FROM supervisor_cursors WHERE consumer = ?", (consumer,)
        ).fetchone()
        current = int(row["after_seq"]) if row else None
        if current != expected or (current is not None and new < current):
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO supervisor_cursors (consumer, after_seq, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(consumer) DO UPDATE SET after_seq = excluded.after_seq, "
            "updated_at = excluded.updated_at",
            (consumer, new, storage.now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


@dataclass(frozen=True)
class Batch:
    events: list[dict[str, Any]]
    expected: int | None  # the stored cursor, for commit_cursor's swap
    start_seq: int
    next_seq: int
    cursor_source: Literal["stored", "lookback", "explicit"]
    truncated: bool


def drain(
    *,
    consumer: str = DEFAULT_CONSUMER,
    limit: int = DEFAULT_LIMIT,
    lookback: int = DEFAULT_LOOKBACK,
    after_seq: int | None = None,
) -> Batch:
    """Read the inbox after the consumer's cursor. Read-only.

    With no stored cursor, starts ``lookback`` rows before the log tail, so a
    first run neither replays the whole log nor silently skips recent rows.
    When the read is not truncated, ``next_seq`` jumps to the tail read
    *before* the query, so unrelated channels are not rescanned next time.
    """
    cap = max(1, min(int(limit), MAX_LIMIT))
    stored = load_cursor(consumer)
    _ulid, tail = events.tail_cursor()
    if after_seq is not None:
        if after_seq < 0:
            raise ValueError("after_seq must be >= 0")
        start = after_seq
        source: Literal["stored", "lookback", "explicit"] = "explicit"
    elif stored is None:
        start = max(0, tail - max(0, int(lookback)))
        source = "lookback"
    else:
        start = stored
        source = "stored"
    rows = events.query_events(channels=list(INBOX_CHANNELS), after_seq=start, limit=cap)
    truncated = len(rows) >= cap
    last = int(rows[-1]["seq"]) if rows else start
    next_seq = last if truncated else max(last, tail, start)
    return Batch(
        events=rows,
        expected=stored,
        start_seq=start,
        next_seq=next_seq,
        cursor_source=source,
        truncated=truncated,
    )


def task_owner(task_id: int) -> str | None:
    """``claimed_by`` for a task, or ``None``. Read-only."""
    conn = storage.connect()
    try:
        row = conn.execute("SELECT claimed_by FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    finally:
        conn.close()
    owner = (row["claimed_by"] or "").strip() if row else ""
    return owner or None


def run_once(
    *,
    consumer: str = DEFAULT_CONSUMER,
    dry_run: bool = True,
    limit: int = DEFAULT_LIMIT,
    lookback: int = DEFAULT_LOOKBACK,
    after_seq: int | None = None,
) -> dict[str, Any]:
    """Drain and plan once. Slice 1 is dry-run only: nothing acts, nothing commits."""
    if not dry_run:
        raise ValueError("supervisor loop slice 1 is dry-run only (task #1918)")
    batch = drain(consumer=consumer, limit=limit, lookback=lookback, after_seq=after_seq)
    decisions = plan_reactions(batch.events, owner_of=task_owner)
    return {
        "consumer": consumer,
        "dry_run": True,
        "cursor_source": batch.cursor_source,
        "start_seq": batch.start_seq,
        "next_seq": batch.next_seq,
        "event_count": len(batch.events),
        "truncated": batch.truncated,
        "decisions": [d.as_dict() for d in decisions],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-supervisor",
        description=(
            "First-mate supervisor loop (task #1918). Slice 1: drain the supervisor "
            "inbox and print the wakes and routes it would make. Requires --once --dry-run."
        ),
    )
    parser.add_argument("--once", action="store_true", help="one drain, then exit")
    parser.add_argument("--dry-run", action="store_true", help="plan only; no side effects")
    parser.add_argument("--consumer", default=DEFAULT_CONSUMER)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--lookback",
        type=int,
        default=DEFAULT_LOOKBACK,
        help="event_log rows before the tail to read when the consumer has no cursor",
    )
    parser.add_argument("--after-seq", type=int, help="read-only resume from reported next_seq")
    args = parser.parse_args(argv)
    if not args.once:
        print("error: only --once is supported in this slice", file=sys.stderr)
        return 2
    if not args.dry_run:
        print(
            "error: only --dry-run is supported in this slice; nothing is routed or woken",
            file=sys.stderr,
        )
        return 2
    try:
        report = run_once(
            consumer=args.consumer,
            dry_run=True,
            limit=args.limit,
            lookback=args.lookback,
            after_seq=args.after_seq,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, do not trace
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
