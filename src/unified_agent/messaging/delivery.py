"""Message delivery facts and the dead-letter notice (task #2391).

A message row used to say ``delivered`` the moment it was stored. That is one
fact of four, and the others are what a sender needs to know:

* **admitted**: the hub committed it to the recipient's mailbox (``created_at``).
* **appended**: a wake rang a live session of the recipient after it arrived.
  It comes from the wake receipts (``session_wake_offers``): an offer covering
  the message's ``seq`` with outcome ``delivered`` is ``yes``; one the node
  could not confirm (``delivery_uncertain``) is ``unknown``. An unknown append
  is verified by a later read and is never re-sent automatically.
* **read**: a session of the recipient read or acked it.
* **terminal**: it ended. ``answered`` (the recipient replied), ``acked``, or
  ``dead_letter``: mail that asked for attention (a request, query or handoff,
  or ``expects_reply``) went unread for ``UA_MESSAGE_DEAD_LETTER_HOURS``.

``delivery.state`` is the furthest of these. The legacy ``status`` field keeps
its values for existing readers.

The dead-letter sweep runs in queue hygiene. It records each dead letter once on
``agent.message.dead_letter`` and, in ``notify`` mode (the default), sends the
sender one ``inform`` naming the message by id. The notice never carries the
original body and the message is never re-sent; the sender decides. A notice is
an ``inform`` without ``expects_reply``, so it can never dead-letter itself.
Only mail admitted inside the sweep window is considered, and each pass sends
at most ``SWEEP_CAP`` notices, so a backlog cannot flood anyone.

Stranded session mail (task #2547). Session mail is private to its session, so
mail left unread when that session ends is invisible to every other session of
the label. The stranded sweep finds it (``agent.message.stranded``, once per
message), tells the recipient label once per ended session which ids are
waiting and how to read them, and tells each sender once. Neither notice quotes
a body: the mail stays where it was, readable by the same label through
``session_id``. Stranded mail is not dead-lettered as well.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import events, storage
from . import index as message_index

log = logging.getLogger(__name__)

DEAD_LETTER_CHANNEL = "agent.message.dead_letter"
#: The in-process sender of delivery notices. Never accepted from a caller.
NOTICE_SENDER = "hub-delivery"
NOTICE_ID_PREFIX = "delivery-"
#: The in-process sender of ready-dependent notices (task #2454). Never accepted
#: from a caller either.
READY_NOTICE_SENDER = "hub-graph"
READY_NOTICE_ID_PREFIX = "task-ready-"
#: The in-process sender of approval-receipt messages (task #2435). Never
#: accepted from a caller either.
APPROVAL_NOTICE_SENDER = "hub-approvals"
APPROVAL_NOTICE_ID_PREFIX = "approval-receipt-"
#: The in-process sender of stranded-mail notices (task #2547). Never accepted
#: from a caller either.
STRANDED_NOTICE_SENDER = "hub-mailbox"
STRANDED_NOTICE_ID_PREFIX = "stranded-"
STRANDED_CHANNEL = "agent.message.stranded"
#: Session statuses after which session mail can no longer be read by its session.
ENDED_SESSION_STATUSES = ("completed", "failed", "abandoned")
#: How many message ids one stranded notice names before summarising the rest.
STRANDED_LIST_MAX = 10
#: The in-process sender of supervised-command completions (task #2413). Never
#: accepted from a caller either.
NODE_NOTICE_SENDER = "hub-node"
NODE_NOTICE_ID_PREFIX = "node-command-"
#: Every hub service sender and the message-id prefix reserved for it.
SERVICE_SENDERS = {
    NOTICE_SENDER: NOTICE_ID_PREFIX,
    READY_NOTICE_SENDER: READY_NOTICE_ID_PREFIX,
    APPROVAL_NOTICE_SENDER: APPROVAL_NOTICE_ID_PREFIX,
    STRANDED_NOTICE_SENDER: STRANDED_NOTICE_ID_PREFIX,
    NODE_NOTICE_SENDER: NODE_NOTICE_ID_PREFIX,
}
ATTENTION_ACTS = ("request", "query", "handoff")
DEFAULT_TTL_HOURS = 24.0
#: Mail older than this is never swept, so turning the sweep on does not page
#: senders about a historical backlog.
DEFAULT_WINDOW_HOURS = 48.0
SWEEP_CAP = 20
MODES = ("off", "report", "notify")

ADMITTED, APPENDED, READ = "admitted", "appended", "read"
ANSWERED, ACKED, DEAD_LETTER = "answered", "acked", "dead_letter"
STRANDED = "stranded"


def _hours(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def ttl_hours() -> float:
    return _hours("UA_MESSAGE_DEAD_LETTER_HOURS", DEFAULT_TTL_HOURS)


def window_hours() -> float:
    return max(_hours("UA_MESSAGE_DEAD_LETTER_WINDOW_HOURS", DEFAULT_WINDOW_HOURS), ttl_hours())


def mode() -> str:
    raw = os.environ.get("UA_MESSAGE_DEAD_LETTER", "notify").strip().lower()
    return raw if raw in MODES else "notify"


def dead_letter_key(message_id: str) -> str:
    return f"dead-letter:{message_id}"


def stranded_mode() -> str:
    raw = os.environ.get("UA_MESSAGE_STRANDED", "notify").strip().lower()
    return raw if raw in MODES else "notify"


def stranded_key(message_id: str) -> str:
    return f"stranded:{message_id}"


# --- facts ----------------------------------------------------------------------


def _appends(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, tuple[str, Any]]:
    """``message_id -> (appended, appended_at)`` from the wake receipts.

    One query per recipient. Label mail counts a wake to any session of the
    recipient; session mail counts only that session.
    """
    out: dict[str, tuple[str, Any]] = {}
    by_recipient: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("seq") is not None:
            by_recipient.setdefault(row["to_agent"], []).append(row)
    for recipient, mail in by_recipient.items():
        low = min(int(r["seq"]) for r in mail)
        offers = conn.execute(
            "SELECT o.session_id, o.offered_seq, o.outcome, o.reason, o.recorded_at "
            "FROM session_wake_offers o JOIN agent_sessions s ON s.id = o.session_id "
            "WHERE s.agent = ? AND s.org_id IS ? AND o.offered_seq >= ? "
            "AND o.recorded_at IS NOT NULL ORDER BY o.recorded_at",
            (recipient, message_index.tenant(), low),
        ).fetchall()
        for row in mail:
            seq = int(row["seq"])
            target = row.get("to_session")
            covering = [
                o
                for o in offers
                if o["offered_seq"] >= seq and (target is None or o["session_id"] == target)
            ]
            delivered = [o for o in covering if o["outcome"] == "delivered"]
            if delivered:
                out[row["message_id"]] = ("yes", delivered[0]["recorded_at"])
            elif any(o["reason"] == "delivery_uncertain" for o in covering):
                out[row["message_id"]] = ("unknown", None)
            else:
                out[row["message_id"]] = ("no", None)
    return out


def _dead_letters(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, str]:
    keys = [dead_letter_key(mid) for mid in ids]
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key, created_at FROM event_log WHERE channel = ? "
        f"AND dedupe_key IN ({marks}) AND org_id IS ?",
        (DEAD_LETTER_CHANNEL, *keys, message_index.tenant()),
    ).fetchall()
    return {row["dedupe_key"].removeprefix("dead-letter:"): row["created_at"] for row in rows}


def _stranded(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, str]:
    keys = [stranded_key(mid) for mid in ids]
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key, created_at FROM event_log WHERE channel = ? "
        f"AND dedupe_key IN ({marks}) AND org_id IS ?",
        (STRANDED_CHANNEL, *keys, message_index.tenant()),
    ).fetchall()
    return {row["dedupe_key"].removeprefix("stranded:"): row["created_at"] for row in rows}


def attach_facts(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``delivery`` to each listed row, in place; returns ``rows``."""
    if not rows:
        return rows
    try:
        appends = _appends(conn, rows)
        dead = _dead_letters(conn, (r["message_id"] for r in rows))
        stranded = _stranded(conn, (r["message_id"] for r in rows))
    except sqlite3.Error as exc:  # an old store without the wake tables
        log.debug("delivery facts unavailable: %s", exc)
        appends, dead, stranded = {}, {}, {}
    for row in rows:
        appended, appended_at = appends.get(row["message_id"], ("no", None))
        if row.get("answered_by"):
            terminal, terminal_at = ANSWERED, None
        elif row.get("acked_at"):
            terminal, terminal_at = ACKED, row["acked_at"]
        elif row["message_id"] in dead:
            terminal, terminal_at = DEAD_LETTER, dead[row["message_id"]]
        elif row["message_id"] in stranded and not row.get("read_at"):
            # Read after it was reported: the reader found it, so it is read.
            terminal, terminal_at = STRANDED, stranded[row["message_id"]]
        else:
            terminal, terminal_at = None, None
        if terminal in (ANSWERED, ACKED):
            state = terminal
        elif row.get("read_at"):
            state = READ
        elif terminal in (DEAD_LETTER, STRANDED):
            state = terminal
        elif appended == "yes":
            state = APPENDED
        else:
            state = ADMITTED
        row["delivery"] = {
            "state": state,
            "admitted_at": row.get("created_at"),
            "appended": appended,
            "appended_at": appended_at,
            "read_at": row.get("read_at"),
            "terminal": terminal,
            "terminal_at": terminal_at,
        }
    return rows


# --- dead letters ---------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _candidates(
    conn: sqlite3.Connection, now: datetime, cap: int, *, notify: bool
) -> list[sqlite3.Row]:
    """Unread attention mail past its TTL and inside the window, oldest first.

    In notify mode a message stays a candidate until its notice exists, so a
    notice that failed after the fact was recorded is retried on the next pass
    (the fact itself is deduplicated). In report mode the recorded fact ends it.
    """
    done = (
        "NOT EXISTS (SELECT 1 FROM message_index n WHERE n.org_id IS i.org_id "
        f"AND n.message_id = '{NOTICE_ID_PREFIX}' || i.message_id)"
        if notify
        else "NOT EXISTS (SELECT 1 FROM event_log e WHERE e.channel = ? "
        "AND e.dedupe_key = 'dead-letter:' || i.message_id)"
    )
    oldest = _iso(now - timedelta(hours=window_hours()))
    newest = _iso(now - timedelta(hours=ttl_hours()))
    acts = ",".join("?" * len(ATTENTION_ACTS))
    return conn.execute(
        "SELECT i.message_id, i.sender, i.recipient, i.act, i.task_id, i.created_at, "
        "i.event_seq, i.target_session FROM message_index i "
        "WHERE i.org_id IS ? AND i.created_at >= ? AND i.created_at <= ? "
        f"AND (i.act IN ({acts}) OR i.expects_reply = 1) "
        "AND i.read_at IS NULL AND i.acked_at IS NULL "
        f"AND i.sender NOT IN ({','.join('?' * len(SERVICE_SENDERS))}) "
        "AND NOT EXISTS (SELECT 1 FROM message_reads r WHERE r.message_id = i.message_id) "
        f"AND {message_index.ANSWERED_BY} IS NULL AND {done} "
        # Stranded mail already told its sender (task #2547): no second notice.
        "AND NOT EXISTS (SELECT 1 FROM event_log st WHERE st.channel = ? "
        "AND st.dedupe_key = 'stranded:' || i.message_id AND st.org_id IS i.org_id) "
        "ORDER BY i.event_seq LIMIT ?",
        (
            message_index.tenant(),
            oldest,
            newest,
            *ATTENTION_ACTS,
            *SERVICE_SENDERS,
            *(() if notify else (DEAD_LETTER_CHANNEL,)),
            STRANDED_CHANNEL,
            cap,
        ),
    ).fetchall()


def notice_body(row: dict[str, Any], appended: str, hours: float) -> str:
    """Ids and counts only: the original body is never quoted back."""
    wake = {"yes": "a wake reached a live session", "unknown": "a wake was unconfirmed"}.get(
        appended, "no wake reached a live session"
    )
    task = f" (task #{row['task_id']})" if row.get("task_id") else ""
    return (
        f"Your {row['act']} {row['message_id']} to {row['recipient']}{task} was not read "
        f"within {hours:g}h; {wake}. It was not re-sent: re-send it, route it to someone "
        "else, or close the work it was for."
    )


def sweep_dead_letters(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
    cap: int = SWEEP_CAP,
) -> dict[str, Any]:
    """Record unread attention mail past its TTL as dead letters and tell the sender.

    Idempotent: a message dead-letters once (``dedupe_key``) and its notice has
    a fixed ``message_id``. Never raises for one message; the rest still run.
    """
    current = mode()
    if current == "notify" and not notify:
        # The caller sends no notifications this pass: record facts only. A
        # recorded fact without a notice stays a candidate for a later pass.
        current = "report"
    result: dict[str, Any] = {"mode": current, "dead_letters": [], "notices": 0, "errors": []}
    if current == "off":
        return result
    ref = now or datetime.now(UTC)
    hours = ttl_hours()
    conn = storage.connect()
    try:
        rows = [dict(r) for r in _candidates(conn, ref, cap, notify=current == "notify")]
        listed = [
            {
                "message_id": r["message_id"],
                "to_agent": r["recipient"],
                "to_session": r["target_session"],
                "seq": r["event_seq"],
            }
            for r in rows
        ]
        appends = _appends(conn, listed)
    finally:
        conn.close()
    for row in rows:
        mid = row["message_id"]
        appended = appends.get(mid, ("no", None))[0]
        entry = {
            "message_id": mid,
            "from_agent": row["sender"],
            "to_agent": row["recipient"],
            "act": row["act"],
            "appended": appended,
        }
        result["dead_letters"].append(entry)
        if dry_run:
            continue
        try:
            events.record_event_once(
                DEAD_LETTER_CHANNEL,
                {**entry, "task_id": row["task_id"], "ttl_hours": hours, "mode": current},
                originator=NOTICE_SENDER,
                dedupe_key=dead_letter_key(mid),
            )
        except Exception as exc:  # noqa: BLE001 - one message must not stop the sweep
            result["errors"].append(f"{mid}: {type(exc).__name__}")
            continue
        if current != "notify":
            continue
        try:
            from .send import send_delivery_notice

            sent = send_delivery_notice(
                original_id=mid,
                recipient=row["sender"],
                body=notice_body(row, appended, hours),
                task_id=row["task_id"],
            )
            if not sent.get("duplicate"):
                result["notices"] += 1
        except Exception as exc:  # noqa: BLE001 - the fact is recorded; the notice can retry
            result["errors"].append(f"{mid}: notice {type(exc).__name__}")
    return result


# --- stranded session mail (task #2547) ------------------------------------------


def _stranded_candidates(conn: sqlite3.Connection, now: datetime, cap: int) -> list[sqlite3.Row]:
    """Unread session mail whose target session has ended, oldest first.

    Inside the sweep window only, so turning this on does not page anyone about
    old history. A recorded fact ends a message; a revived session's mail is live
    again and drops out on its own. Mail whose sender already got a dead-letter
    notice is skipped, so one message never gets both notices in either sweep
    order (task #2551). The test is the notice, not the dead-letter fact: in
    report mode the fact is recorded with no notice, and the stranded notice is
    then the only one anybody gets (task #2555).
    """
    oldest = _iso(now - timedelta(hours=window_hours()))
    ended = ", ".join("?" * len(ENDED_SESSION_STATUSES))
    return conn.execute(
        "SELECT i.message_id, i.sender, i.recipient, i.act, i.task_id, i.event_seq, "
        "i.target_session, s.status AS session_status FROM message_index i "
        "JOIN agent_sessions s ON s.id = i.target_session AND s.org_id IS i.org_id "
        f"WHERE i.org_id IS ? AND s.status IN ({ended}) AND i.created_at >= ? "
        "AND i.read_at IS NULL AND i.acked_at IS NULL "
        f"AND {message_index.ANSWERED_BY} IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM event_log e WHERE e.channel = ? "
        "AND e.dedupe_key = 'stranded:' || i.message_id AND e.org_id IS i.org_id) "
        "AND NOT EXISTS (SELECT 1 FROM message_index n WHERE n.org_id IS i.org_id "
        f"AND n.message_id = '{NOTICE_ID_PREFIX}' || i.message_id) "
        "ORDER BY i.event_seq LIMIT ?",
        (
            message_index.tenant(),
            *ENDED_SESSION_STATUSES,
            oldest,
            STRANDED_CHANNEL,
            cap,
        ),
    ).fetchall()


def _listing(rows: list[dict[str, Any]], *, who: str) -> str:
    """``who`` is ``from`` (name each sender) or ``to`` (name the recipient)."""
    party = "sender" if who == "from" else "recipient"

    def one(r: dict[str, Any]) -> str:
        task = f", task #{r['task_id']}" if r.get("task_id") else ""
        return f"{r['message_id']} ({r['act']} {who} {r[party]}{task})"

    shown = "; ".join(one(r) for r in rows[:STRANDED_LIST_MAX])
    more = len(rows) - STRANDED_LIST_MAX
    return shown + (f"; and {more} more" if more > 0 else "")


def stranded_recipient_body(session_id: str, status: str, rows: list[dict[str, Any]]) -> str:
    """Ids, acts and senders only: no body is quoted."""
    recipient = rows[0]["recipient"]
    return (
        f"{len(rows)} message(s) addressed to your session {session_id} were not read "
        f"before it ended ({status}): {_listing(rows, who='from')}. They were not "
        f"re-sent. Read them with message_list(to_agent='{recipient}', "
        f"session_id='{session_id}', unread_only=True), and ack them with "
        f"coordination_manage('message_ack', {{'session_id': '{session_id}', 'through': <seq>}})."
    )


def stranded_sender_body(session_id: str, status: str, rows: list[dict[str, Any]]) -> str:
    """Ids and acts only: no body is quoted."""
    recipient = rows[0]["recipient"]
    return (
        f"Your {len(rows)} message(s) to {recipient}'s session {session_id} were not "
        f"read before that session ended ({status}): {_listing(rows, who='to')}. They "
        f"were not re-sent: send again to the label {recipient}, or close the work "
        "they were for."
    )


def _shared_task(rows: list[dict[str, Any]]) -> int | None:
    tasks = {r.get("task_id") for r in rows}
    return tasks.pop() if len(tasks) == 1 else None


def sweep_stranded(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
    cap: int = SWEEP_CAP,
) -> dict[str, Any]:
    """Record unread mail left in an ended session and tell its label and sender.

    One notice to the recipient label per ended session and one to each sender,
    with ids derived from the session and its newest stranded message, so a
    retried pass is a duplicate, never a second notice. A group's facts are
    recorded only after its notices went out, so a failed notice is retried on
    the next pass. Never raises for one group; the rest still run.
    """
    current = stranded_mode()
    if current == "notify" and not notify:
        current = "report"
    result: dict[str, Any] = {"mode": current, "stranded": [], "notices": 0, "errors": []}
    if current == "off":
        return result
    conn = storage.connect()
    try:
        rows = [dict(r) for r in _stranded_candidates(conn, now or datetime.now(UTC), cap)]
    finally:
        conn.close()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["target_session"], []).append(row)
        result["stranded"].append(
            {
                "message_id": row["message_id"],
                "from_agent": row["sender"],
                "to_agent": row["recipient"],
                "to_session": row["target_session"],
                "act": row["act"],
                "session_status": row["session_status"],
            }
        )
    if dry_run:
        return result
    from .send import send_service_notice

    for sid, mail in groups.items():
        status = mail[0]["session_status"]
        stamp = f"{STRANDED_NOTICE_ID_PREFIX}{sid}-{max(int(r['event_seq']) for r in mail)}"
        if current == "notify":
            try:
                notices = [
                    (mail[0]["recipient"], stamp, stranded_recipient_body(sid, status, mail), mail)
                ]
                by_sender: dict[str, list[dict[str, Any]]] = {}
                for r in mail:
                    if r["sender"] not in SERVICE_SENDERS:
                        by_sender.setdefault(r["sender"], []).append(r)
                for sender, theirs in sorted(by_sender.items()):
                    body = stranded_sender_body(sid, status, theirs)
                    notices.append((sender, f"{stamp}-{sender}", body, theirs))
                for recipient, mid, body, about in notices:
                    sent = send_service_notice(
                        sender=STRANDED_NOTICE_SENDER,
                        message_id=mid,
                        recipient=recipient,
                        body=body,
                        task_id=_shared_task(about),
                    )
                    if not sent.get("duplicate"):
                        result["notices"] += 1
            except Exception as exc:  # noqa: BLE001 - this group retries on the next pass
                result["errors"].append(f"{sid}: notice {type(exc).__name__}")
                continue
        for r in mail:
            try:
                events.record_event_once(
                    STRANDED_CHANNEL,
                    {
                        "message_id": r["message_id"],
                        "from_agent": r["sender"],
                        "to_agent": r["recipient"],
                        "to_session": sid,
                        "act": r["act"],
                        "task_id": r["task_id"],
                        "session_status": status,
                        "mode": current,
                    },
                    originator=STRANDED_NOTICE_SENDER,
                    dedupe_key=stranded_key(r["message_id"]),
                )
            except Exception as exc:  # noqa: BLE001 - one message must not stop the sweep
                result["errors"].append(f"{r['message_id']}: {type(exc).__name__}")
    return result
