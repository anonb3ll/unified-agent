"""Durable approval-decision receipts back to the requester (task #2394).

The #2303 delivery paths (``approval_slack``, ``approval_webhook``,
``approval_push``) tell the *human approver* that a request exists and post a
follow-up under the operator's Slack card. None of them tells the *requesting
agent* that its request was decided; today the requester has to poll
``approval_pending``. This module is that missing leg, built on the same
outbox idea as ``approval_slack_outbox`` but aimed at a different audience.

Protocol (the order is the point):

1. ``record_intent(conn, approval_id, decision, requester)`` writes a delivery
   intent on the caller's connection, inside the decision's own transaction
   (preferred: a rolled-back decision takes its intent with it) or committed
   just before it.
2. ``deliver(approval_id)`` claims each intent for that approval, re-reads the
   decision and the requester from the ``approvals`` table (the intent's copy
   is never trusted), appends one receipt to ``event_log`` on channel
   ``agent.approval.receipt`` under the permanent idempotency key
   ``approval-receipt:<approval_id>:<decision>``, hands it to the requester
   through an injectable ``notify`` callable, and deletes the intent only after
   the append has committed and ``notify`` has returned.
3. ``sweep()`` finishes intents a crash left behind. Every step is idempotent:
   a re-run reuses the same receipt row (same ULID), so delivery to the
   requester is at-least-once and the receipt id is the requester's dedupe key.
4. ``sweep()`` first reconciles (task #2484). The gate writes the intent behind
   a savepoint, so a failed intent insert leaves a committed decision with no
   intent row, which a sweep that reads only the intent table never sees. The
   reconcile step reads this tenant's ``approvals`` rows instead: a decision
   the row's status still implies, proven by its ``approval-resolution`` event,
   with neither an intent nor a receipt event, gets its intent re-created and
   is delivered in the same pass. The receipt key is unchanged, so a replay
   finds the receipt and does nothing.

   How far back it reads is a stored watermark, not a moving window (task
   #2494): every decision resolved before the watermark is known to be settled.
   The watermark only moves past a decision once that decision has its intent
   or its receipt, so one that cannot be repaired yet stays in reach however
   long that takes. It is the ``(decided_at, ulid)`` of the last decision the
   pass got through (task #2497), so the next pass resumes strictly after it and
   a page full of settled decisions at one timestamp is still left behind. The
   first pass on a store has no watermark and starts ``RECONCILE_FIRST_RUN_S``
   back, which bounds the back-fill on a database that predates receipts.

A decision counts as committed when the approval row still carries that status
or the ``approval-resolution:<id>:<status>`` ledger event exists (it commits in
the same transaction as the status change, so an approval that was later
consumed or revoked still confirms). An intent whose decision never committed
is kept for ``ABANDON_AFTER_S`` (the decision may still be in flight) and then
dropped without a receipt.

The receipt payload carries ids and the decision only: never the tool's
arguments, the request body, a reason string or any secret.

The approval gate itself is untouched; the call sites live in the gate's owner
modules and are wired separately.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from . import config, edition, events, storage

logger = logging.getLogger(__name__)

TABLE = "approval_receipt_intents"
CHANNEL = "agent.approval.receipt"
ORIGINATOR = "approval-receipts"
# An intent whose decision has not committed is kept this long before it is
# dropped as abandoned: the decision transaction may still be running.
ABANDON_AFTER_S = 600.0
# A claim older than this belongs to a deliverer that died mid-delivery.
STALE_CLAIM_S = 300.0
SWEEP_LIMIT = 200
# Where the first reconcile pass on a store starts when no watermark is stored
# yet. It bounds the back-fill: a database that predates receipts gets late
# receipts for the last day of decisions, not for all of them. After that the
# stored watermark decides, and no decision behind it is ever unsettled.
RECONCILE_FIRST_RUN_S = 86400.0
# The watermark never moves past decisions younger than this: one may still be
# committing, and its event may sort before another already seen.
RECONCILE_SETTLE_S = 60.0
# An idle pass rewrites the watermark only when it can move this far, so a
# quiet store does not take a write on every queue-hygiene pass.
RECONCILE_ADVANCE_S = 3600.0
# Decisions one pass reads from the watermark on, settled ones included.
RECONCILE_SCAN = 500
_WATERMARK_KEY = "approval_receipts.reconciled_through"
# Separates the two fields of a stored keyset watermark: the decision's
# ``decided_at`` and its resolution event's ulid (task #2497).
_CURSOR_SEP = "\x1f"

_DECISIONS = {
    "approve": "approved",
    "approved": "approved",
    "deny": "denied",
    "denied": "denied",
    "revoke": "revoked",
    "revoked": "revoked",
}

Notify = Callable[[Mapping[str, Any]], str | None]


def _now() -> float:
    return time.time()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the intent table. Idempotent and self-contained (no SCHEMA_VERSION
    bump). ``execute``, not ``executescript``: this runs inside ``init_db``'s
    transaction."""
    conn.execute(
        f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  approval_id INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('approved','denied','revoked')),
  requester TEXT,
  created_epoch REAL NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  claimed_epoch REAL,
  org_id TEXT,
  PRIMARY KEY (approval_id, decision)
)
"""
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_created ON {TABLE}(org_id, created_epoch)"
    )


def normalize_decision(decision: str) -> str:
    """Map a resolve verb or a resulting status to the stored status form."""
    status = _DECISIONS.get(str(decision or "").strip().lower())
    if status is None:
        raise ValueError(
            f"decision must be one of approve/deny/revoke (or their statuses), got {decision!r}"
        )
    return status


def receipt_key(approval_id: int, decision: str) -> str:
    """The permanent idempotency key for one decision's receipt."""
    return f"approval-receipt:{int(approval_id)}:{normalize_decision(decision)}"


def record_intent(
    conn: sqlite3.Connection,
    approval_id: int,
    decision: str,
    requester: str | None = None,
    *,
    now: float | None = None,
) -> None:
    """Write one delivery intent on the caller's connection. Does not commit.

    Call it inside the decision's transaction (after the guarded UPDATE has
    won), or commit it before the decision write. A second intent for the same
    approval and decision is a no-op. ``requester`` is a hint for operators
    reading the table; ``deliver`` re-reads the real one from ``approvals``.
    """
    conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} (approval_id, decision, requester, created_epoch, "
        "created_at, org_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(approval_id),
            normalize_decision(decision),
            requester,
            _now() if now is None else float(now),
            storage.now(),
            edition.tenant_org_id(),
        ),
    )


def pending_sender(receipt: Mapping[str, Any]) -> str:
    """A ``notify`` that pushes nothing (kept for callers that only want the event).

    The receipt is already durable on the event log, so the requester can read
    it from ``agent.approval.receipt``; this only reports that no push went out.
    """
    return "pending_sender"


def message_id_for(approval_id: int, decision: str) -> str:
    """The receipt message's fixed id, so a redelivery is a duplicate (task #2435)."""
    from .messaging.send import APPROVAL_NOTICE_ID_PREFIX

    return f"{APPROVAL_NOTICE_ID_PREFIX}{int(approval_id)}-{decision}"


def receipt_body(receipt: Mapping[str, Any]) -> str:
    """Ids and the decision only: never the tool, its arguments or a reason."""
    return (
        f"Your approval request #{receipt['approval_id']} was {receipt['decision']}. "
        f"Receipt {receipt['receipt_id']} on {CHANNEL}."
    )


def hub_message_sender(receipt: Mapping[str, Any]) -> str:
    """Default ``notify`` (task #2435): one hub ``inform`` to the requesting agent.

    Sent by the in-process service sender ``hub-approvals`` under a fixed id, so
    a retry after a crash is a duplicate, not a second message. A requester that
    is no agent mailbox (the operator, a service, an unknown label) gets no
    message and the intent is still finished: the receipt event is the record.
    A refusal by the messaging layer is final for the same reason. Anything else
    (the store is unavailable) raises, so the intent is kept for ``sweep``.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .messaging.send import APPROVAL_NOTICE_SENDER, send_service_notice

    requester = str(receipt.get("requester") or "")
    if config.is_human_identity(requester) or not config.is_known_agent_label(requester):
        return "no_agent_mailbox"
    try:
        sent = send_service_notice(
            sender=APPROVAL_NOTICE_SENDER,
            message_id=message_id_for(receipt["approval_id"], receipt["decision"]),
            recipient=requester,
            body=receipt_body(receipt),
            task_id=None,
        )
    except ToolError as exc:
        logger.warning(
            "approval receipt message for #%s refused: %s",
            receipt.get("approval_id"),
            str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__,
        )
        return "send_refused"
    return "duplicate" if sent.get("duplicate") else "messaged"


# ------------------------------------------------------------------ internals


def _claim(approval_id: int, decision: str, org: str | None, now: float) -> bool:
    conn = storage.connect()
    try:
        cur = conn.execute(
            f"UPDATE {TABLE} SET claimed_epoch=? WHERE approval_id=? AND decision=? "
            "AND org_id IS ? AND (claimed_epoch IS NULL OR claimed_epoch < ?)",
            (now, approval_id, decision, org, now - STALE_CLAIM_S),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _release(approval_id: int, decision: str, org: str | None, error: str | None) -> None:
    conn = storage.connect()
    try:
        if error is None:
            conn.execute(
                f"UPDATE {TABLE} SET claimed_epoch=NULL "
                "WHERE approval_id=? AND decision=? AND org_id IS ?",
                (approval_id, decision, org),
            )
        else:
            conn.execute(
                f"UPDATE {TABLE} SET claimed_epoch=NULL, attempts=attempts+1, last_error=? "
                "WHERE approval_id=? AND decision=? AND org_id IS ?",
                (error[:64], approval_id, decision, org),
            )
        conn.commit()
    finally:
        conn.close()


def _delete(approval_id: int, decision: str, org: str | None) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            f"DELETE FROM {TABLE} WHERE approval_id=? AND decision=? AND org_id IS ?",
            (approval_id, decision, org),
        )
        conn.commit()
    finally:
        conn.close()


def _owner_read(approval_id: int, decision: str, org: str | None) -> tuple[dict | None, str | None]:
    """Re-read the approval row and the resolution event that proves ``decision``.

    Returns ``(row, resolution_event_id)``. ``row`` is None when the approval is
    missing or belongs to another tenant. The decision is committed when the
    row still carries that status or the resolution event exists.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, requested_by, status FROM approvals WHERE id=? AND org_id IS ?",
            (approval_id, org),
        ).fetchone()
        if row is None:
            return None, None
        event = conn.execute(
            "SELECT ulid FROM event_log WHERE dedupe_key=?",
            (f"approval-resolution:{approval_id}:{decision}",),
        ).fetchone()
    finally:
        conn.close()
    return dict(row), (event["ulid"] if event is not None else None)


def _deliver_intent(intent: Mapping[str, Any], notify: Notify, now: float) -> dict[str, Any]:
    approval_id = int(intent["approval_id"])
    decision = str(intent["decision"])
    org = intent["org_id"]
    result: dict[str, Any] = {"approval_id": approval_id, "decision": decision}
    if not _claim(approval_id, decision, org, now):
        return {**result, "outcome": "claimed"}
    try:
        row, resolution_id = _owner_read(approval_id, decision, org)
        committed = row is not None and (row["status"] == decision or resolution_id is not None)
        if not committed:
            if row is not None and now - float(intent["created_epoch"]) < ABANDON_AFTER_S:
                _release(approval_id, decision, org, None)
                return {**result, "outcome": "in_flight"}
            _delete(approval_id, decision, org)
            return {**result, "outcome": "abandoned"}
        requester = row["requested_by"]
        key = receipt_key(approval_id, decision)
        receipt_id, _ = events.record_event_once(
            CHANNEL,
            payload={
                "approval_id": approval_id,
                "decision": decision,
                "requester": requester,
                "resolution_event_id": resolution_id,
            },
            originator=ORIGINATOR,
            notify=config.event_bus_enabled(),
            dedupe_key=key,
        )
        result["receipt_id"] = receipt_id
        if requester:
            sent = notify(
                {
                    "receipt_id": receipt_id,
                    "key": key,
                    "approval_id": approval_id,
                    "decision": decision,
                    "requester": requester,
                }
            )
            outcome = str(sent or "delivered")[:32]
        else:
            outcome = "no_requester"
        # Only now, with the receipt committed and handed over, does the
        # intent go. A crash before this line leaves it for sweep().
        _delete(approval_id, decision, org)
        return {**result, "outcome": outcome}
    except Exception as exc:
        error = type(exc).__name__
        logger.warning(
            "approval receipt for #%s (%s) not delivered: %s", approval_id, decision, error
        )
        try:
            _release(approval_id, decision, org, error)
        except Exception:
            logger.warning("approval receipt claim release failed for #%s", approval_id)
        return {**result, "outcome": "failed", "error": error}


def _intents(where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE org_id IS ? AND {where} "
            "ORDER BY created_epoch, rowid LIMIT ?",
            (edition.tenant_org_id(), *params, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# Every decision of this tenant resolved at or after the watermark, settled or
# not, oldest first. Driven from the approval rows: the row's status must still
# imply the decision (an approved pass may since have been consumed, revoked or
# expired), so a stray ledger row alone never produces a receipt; the
# resolution event supplies the proof and the time. ``updated_at`` only grows,
# so it narrows the scan without hiding an older decision. Both probes and the
# join use unique keys.
#
# The watermark is a ``(decided_at, ulid)`` keyset (task #2497), so resuming is
# strictly after the last decision read and a page full of settled decisions at
# one timestamp still progresses. ``_RECONCILE_SQL_LEGACY`` is the inclusive form
# for a watermark written before keysets, which carried only a timestamp; the
# first pass that reads one upgrades it, and every pass after resumes strictly.
_RECONCILE_SELECT = f"""
SELECT a.id AS approval_id, d.decision AS decision, a.requested_by AS requester,
  e.created_at AS decided_at, e.ulid AS decided_ulid,
  EXISTS (
    SELECT 1 FROM {TABLE} i WHERE i.approval_id = a.id AND i.decision = d.decision
  ) OR EXISTS (
    SELECT 1 FROM event_log r
    WHERE r.dedupe_key = 'approval-receipt:' || a.id || ':' || d.decision
  ) AS settled
FROM approvals a
JOIN (SELECT 'approved' AS decision UNION ALL SELECT 'denied' UNION ALL SELECT 'revoked') d
  ON d.decision = a.status
  OR (d.decision = 'approved' AND a.status IN ('consumed', 'revoked', 'expired'))
JOIN event_log e ON e.dedupe_key = 'approval-resolution:' || a.id || ':' || d.decision
WHERE a.org_id IS ? AND a.updated_at >= ?
"""
_RECONCILE_SQL = (
    _RECONCILE_SELECT + "  AND (e.created_at > ? OR (e.created_at = ? AND e.ulid > ?))\n"
    "ORDER BY e.created_at, e.ulid LIMIT ?\n"
)
# A timestamp-only watermark: resume at that second, inclusively. Reading one
# earlier decision again is harmless (it is settled), and the pass stores a
# keyset, so this form runs once per store.
_RECONCILE_SQL_LEGACY = (
    _RECONCILE_SELECT + "  AND e.created_at >= ?\nORDER BY e.created_at, e.ulid LIMIT ?\n"
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _parse_watermark(value: str | None) -> tuple[str, str] | None:
    """``(decided_at, ulid)`` from a stored watermark, or None when there is none.

    A watermark written before keysets (task #2497) carries the timestamp only;
    its ulid is empty, which reads as "resume at that second, inclusively".
    """
    if not value:
        return None
    stamp, _, ulid = value.partition(_CURSOR_SEP)
    return stamp, ulid


def _format_watermark(stamp: str, ulid: str = "") -> str:
    return f"{stamp}{_CURSOR_SEP}{ulid}"


def _cap(cursor: tuple[str, str], hold: str) -> tuple[str, str]:
    """``cursor`` never past the settle margin."""
    return (hold, "") if cursor[0] > hold else cursor


def _should_write(stored: str | None, target: tuple[str, str], *, idle: bool) -> bool:
    """Whether ``target`` moves the watermark enough to be stored.

    Before the first write, always. After one, an idle pass (nothing to repair
    and no full page) stores a move only when it is at least
    ``RECONCILE_ADVANCE_S``, so a quiet store does not take a write on every
    queue-hygiene pass. A full page or a pass that repaired something always
    stores its progress, because that progress is what leaves the page behind
    (task #2497).
    """
    current = _parse_watermark(stored)
    if current is None:
        return True
    if target <= current:
        return False
    if not idle:
        return True
    return datetime.fromisoformat(target[0]).timestamp() >= (
        datetime.fromisoformat(current[0]).timestamp() + RECONCILE_ADVANCE_S
    )


def _watermark(conn: sqlite3.Connection, org: str | None) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"{_WATERMARK_KEY}:{org or ''}",)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _reconcile(now: float, limit: int) -> list[dict[str, Any]]:
    """Re-create the intent of each committed decision that lost it (tasks #2484, #2494).

    Returns the intents written, ready for ``_deliver_intent``. The first read
    takes no lock; a pass with nothing to repair and no watermark to move
    writes nothing. Otherwise the read is repeated under the write lock and
    the intents are inserted in that transaction, so two concurrent sweeps
    cannot both re-create one.

    The watermark is the ``(decided_at, ulid)`` of the last decision the pass
    got through (task #2497), so the next pass resumes strictly after it and a
    page full of settled decisions at one timestamp is still left behind. A
    full page always stores its progress; only an idle pass that reached the
    settle margin is throttled.

    Each intent is written behind its own savepoint. One that fails is logged
    and left: the others still go through, and the watermark stops in front of
    the failed decision, so every later pass finds it again.
    """
    org = edition.tenant_org_id()
    hold = _iso(now - RECONCILE_SETTLE_S)
    conn = storage.connect()
    try:
        stored = _watermark(conn, org)
        cursor = _parse_watermark(stored)
        floor = cursor[0] if cursor is not None else _iso(now - RECONCILE_FIRST_RUN_S)
        after = cursor[1] if cursor is not None else ""
        if after:
            sql = _RECONCILE_SQL
            params: tuple = (org, floor, floor, floor, after, RECONCILE_SCAN)
        else:
            sql = _RECONCILE_SQL_LEGACY
            params = (org, floor, floor, RECONCILE_SCAN)
        rows = conn.execute(sql, params).fetchall()
        if stored is not None and all(row["settled"] for row in rows):
            # Nothing to repair. A full page leaves at its last row (progress is
            # the point); an idle pass that reached the settle margin moves to ``hold``
            # only when it can move ``RECONCILE_ADVANCE_S``.
            target = (rows[-1]["decided_at"], rows[-1]["decided_ulid"]) if rows else (hold, "")
            if not _should_write(stored, _cap(target, hold), idle=len(rows) < RECONCILE_SCAN):
                return []
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(sql, params).fetchall()
            written: list[sqlite3.Row] = []
            through: tuple[str, str] | None = None
            blocked = False
            for row in rows:
                if not row["settled"]:
                    if len(written) >= limit:
                        blocked = True
                        continue
                    conn.execute("SAVEPOINT reconcile_intent")
                    try:
                        record_intent(
                            conn, row["approval_id"], row["decision"], row["requester"], now=now
                        )
                        conn.execute("RELEASE reconcile_intent")
                        written.append(row)
                    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                        conn.execute("ROLLBACK TO reconcile_intent")
                        conn.execute("RELEASE reconcile_intent")
                        logger.warning(
                            "approval receipt reconcile could not write the intent for "
                            "approval %s: %s",
                            row["approval_id"],
                            type(exc).__name__,
                        )
                        blocked = True
                        continue
                if not blocked:
                    through = (row["decided_at"], row["decided_ulid"])
            if not blocked:
                through = _cap(
                    through if through is not None and len(rows) >= RECONCILE_SCAN else (hold, ""),
                    hold,
                )
            elif through is None and stored is None:
                # Bootstrap: record where this store's reconcile starts, so a
                # decision that cannot be repaired yet is not left behind a
                # later first-run floor (tasks #2494, #2497).
                through = (floor, "")
            if through is not None:
                target = _cap(through, hold)
                if _should_write(stored, target, idle=False):
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (f"{_WATERMARK_KEY}:{org or ''}", _format_watermark(*target)),
                    )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return [
        {
            "approval_id": row["approval_id"],
            "decision": row["decision"],
            "org_id": org,
            "created_epoch": now,
        }
        for row in written
    ]


# ------------------------------------------------------------------ public


def deliver(
    approval_id: int,
    decision: str | None = None,
    *,
    notify: Notify | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Deliver this tenant's intents for one approval, oldest first.

    Call it after the decision's transaction has committed. Never raises for a
    delivery failure: each intent reports an ``outcome`` (``delivered`` or the
    string ``notify`` returned, ``no_requester``, ``in_flight``, ``abandoned``,
    ``claimed`` or ``failed`` with an ``error`` type name). A failed intent
    stays for ``sweep``.
    """
    send = notify or hub_message_sender
    ts = _now() if now is None else float(now)
    if decision is None:
        intents = _intents("approval_id=?", (int(approval_id),), SWEEP_LIMIT)
    else:
        intents = _intents(
            "approval_id=? AND decision=?",
            (int(approval_id), normalize_decision(decision)),
            1,
        )
    return [_deliver_intent(intent, send, ts) for intent in intents]


def sweep(
    now: float | None = None,
    older_than_s: float = 60.0,
    *,
    notify: Notify | None = None,
    limit: int = SWEEP_LIMIT,
) -> dict[str, int]:
    """Deliver every intent older than ``older_than_s``. Idempotent.

    Before that it reconciles: a committed decision of this tenant since the
    stored watermark that has neither an intent nor a receipt gets its intent
    re-created and is delivered in this pass, whatever its age (nothing else
    can be delivering an intent that did not exist). A reconcile failure is
    logged and the existing intents are still swept.

    Returns ``{"scanned": n, <outcome>: count, ...}``, plus ``"reconciled": n``
    when the pass re-created any intent.
    """
    send = notify or hub_message_sender
    ts = _now() if now is None else float(now)
    cap = max(1, int(limit))
    try:
        reconciled = _reconcile(ts, cap)
    except Exception as exc:
        logger.warning("approval receipt reconcile failed: %s", type(exc).__name__)
        reconciled = []
    intents = _intents("created_epoch <= ?", (ts - float(older_than_s),), cap)
    listed = {(intent["approval_id"], intent["decision"]) for intent in intents}
    intents += [i for i in reconciled if (i["approval_id"], i["decision"]) not in listed]
    counts: Counter[str] = Counter()
    for intent in intents:
        counts[_deliver_intent(intent, send, ts)["outcome"]] += 1
    result = {"scanned": len(intents), **dict(counts)}
    if reconciled:
        result["reconciled"] = len(reconciled)
    return result
