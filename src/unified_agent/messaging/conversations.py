"""Conversation views over the mailbox index, for the Control Center Messages pane (task #1864).

Read-only: nothing here changes read, ack or answered state.

Scope is the ``message_list`` rule (``messaging.send``), applied through the same
helpers so the two cannot drift:

* the operator login and the chief-of-staff profile see every conversation;
* any other verified caller sees only messages it sent or received, and mail
  addressed to one of its sessions stays hidden, as ``message_list`` without a
  ``session_id`` hides it. A conversation is listed when at least one message in
  it is visible, and its counts and preview cover only the visible messages;
* a caller with no verified identity is not restricted (what ``message_list``
  allows it). The HTTP routes refuse such a caller before reaching here.

A message counts as read when its recipient read it as a label or through any of
its sessions (a ``message_reads`` receipt). ``message_list`` reports only the
label's own read state; the pane shows the operator whether the recipient has
seen the message at all, so it folds the receipts in.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import storage
from . import index as message_index
from .schema import MAX_LIST_LIMIT
from .send import _viewer, list_messages

#: Conversations returned per call, at most.
MAX_CONVERSATIONS = 100
#: Distinct participant labels reported per conversation, at most.
MAX_PARTICIPANTS = 20
CONVERSATION_FILTERS = ("all", "unread", "waiting")

# Read by the recipient label (the row) or by any of its sessions (a receipt). A
# receipt counts only when its session is registered to the recipient label in
# the message's tenant (#1979): message ids repeat across tenants.
_RECIPIENT_RECEIPT = (
    "SELECT 1 FROM message_reads r JOIN agent_sessions s ON s.id = r.session_id "
    "WHERE r.message_id = i.message_id AND s.agent = i.recipient AND s.org_id IS i.org_id"
)
_UNREAD = f"(i.read_at IS NULL AND NOT EXISTS ({_RECIPIENT_RECEIPT}))"
_WAITING = f"(i.expects_reply = 1 AND {message_index.ANSWERED_BY} IS NULL)"


def _scope() -> tuple[str | None, bool, list[str], list[Any]]:
    """``(viewer label, restricted, WHERE clauses, params)`` for the caller's visible mail.

    The same filters ``list_messages`` applies with no ``to_agent``/``from_agent``
    and no ``session_id``: a restricted caller is limited to mail it is party to,
    and any verified caller reads as its own mailbox, which hides mail addressed to
    one of its sessions. An unauthenticated caller gets only the tenant fence
    (#1979), which every caller gets.
    """
    viewer = _viewer()
    if viewer.identity is None:
        return None, False, ["i.org_id IS ?"], [message_index.tenant()]
    restricted = not viewer.unrestricted
    where, params = message_index._where(
        recipient=None,
        sender=None,
        party=viewer.identity if restricted else None,
        conversation_id=None,
        act=None,
        message_id=None,
        task_id=None,
        delegation_id=None,
        after_seq=None,
        unread_only=False,
        waiting_on_reply=False,
        mailbox=(viewer.identity, None),
    )
    return viewer.identity, restricted, [where.removeprefix(" WHERE ")], params


def _sql_where(clauses: list[str]) -> str:
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def list_conversations(
    *,
    participant: str | None = None,
    filter: str = "all",
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    """Conversations the caller may see, most recent activity first.

    Each row carries its participants (labels and sessions), message ``total``,
    ``unread`` (visible messages their recipient has not read), ``unread_mine``
    (those addressed to the caller), ``waiting`` (``expects_reply`` messages not
    yet answered by their recipient) and a preview of the newest visible message.
    ``participant`` keeps conversations in which that label sent or received a
    visible message (their counts still cover every visible message). ``filter``
    is ``all``, ``unread`` (``unread > 0``) or ``waiting`` (``waiting > 0``).

    Paging: ``next_before_seq`` (set when more conversations exist) is the
    ``before_seq`` of the next page, which lists conversations whose latest
    visible message is older than it. A conversation that gets a new message
    moves to the top, so a later page never repeats one an earlier page showed.
    """
    if filter not in CONVERSATION_FILTERS:
        raise ValueError(f"filter must be one of {CONVERSATION_FILTERS}")
    cap = max(1, min(int(limit), MAX_CONVERSATIONS))
    me, restricted, clauses, params = _scope()
    scope = "own" if restricted else "all"
    who = (participant or "").strip() or None
    # Conditions on whole conversations, so counts and previews stay complete.
    having: list[str] = []
    having_params: list[Any] = []
    if who is not None:
        having.append("SUM(CASE WHEN i.sender = ? OR i.recipient = ? THEN 1 ELSE 0 END) > 0")
        having_params.extend([who, who])
    if filter != "all":
        having.append(f"{filter} > 0")
    if before_seq is not None:
        having.append("last_seq < ?")
        having_params.append(int(before_seq))
    having_sql = (" HAVING " + " AND ".join(having)) if having else ""
    where = _sql_where(clauses)
    conn = storage.connect()
    try:
        groups = conn.execute(
            "SELECT i.conversation_id AS conversation_id, COUNT(*) AS total, "
            "COALESCE(MAX(i.event_seq), 0) AS last_seq, MAX(i.event_ulid) AS last_ulid, "
            f"SUM(CASE WHEN {_UNREAD} THEN 1 ELSE 0 END) AS unread, "
            f"SUM(CASE WHEN i.recipient = ? AND {_UNREAD} THEN 1 ELSE 0 END) AS unread_mine, "
            f"SUM(CASE WHEN {_WAITING} THEN 1 ELSE 0 END) AS waiting "
            f"FROM message_index i{where} GROUP BY i.conversation_id{having_sql} "
            "ORDER BY last_seq DESC, last_ulid DESC LIMIT ?",
            [me, *params, *having_params, cap + 1],
        ).fetchall()
        truncated = len(groups) > cap
        groups = groups[:cap]
        ids = [g["conversation_id"] for g in groups]
        latest = _latest_messages(conn, ids, clauses, params)
        people = _participants(conn, ids, clauses, params)
    finally:
        conn.close()
    conversations = []
    for g in groups:
        cid = g["conversation_id"]
        labels, sessions = people.get(cid, ([], []))
        conversations.append(
            {
                "conversation_id": cid,
                "participants": labels,
                "sessions": sessions,
                "total": int(g["total"]),
                "unread": int(g["unread"] or 0),
                "unread_mine": int(g["unread_mine"] or 0),
                "waiting": int(g["waiting"] or 0),
                "last_seq": int(g["last_seq"] or 0),
                "last": latest.get(cid),
            }
        )
    return {
        "viewer": me,
        "scope": scope,
        "filter": filter,
        "participant": who,
        "conversations": conversations,
        "truncated": truncated,
        # A page that ends in conversations with no seq (legacy pruned rows) has no
        # cursor below it: before_seq must be at least 1.
        "next_before_seq": (
            conversations[-1]["last_seq"] if truncated and conversations[-1]["last_seq"] else None
        ),
    }


def _in_clause(ids: list[str]) -> str:
    return "i.conversation_id IN (" + ",".join("?" for _ in ids) + ")"


def _latest_messages(
    conn: sqlite3.Connection, ids: list[str], clauses: list[str], params: list[Any]
) -> dict[str, dict[str, Any]]:
    """The newest visible message of each conversation, with a body preview."""
    if not ids:
        return {}
    where = _sql_where([*clauses, _in_clause(ids)])
    rows = conn.execute(
        "SELECT * FROM (SELECT i.conversation_id, i.message_id, i.sender, i.recipient, i.act, "
        "i.created_at, i.event_seq, i.expects_reply, i.target_session, i.sender_session, "
        "substr(ltrim(json_extract(e.payload, '$.body'), ' ' || char(9, 10, 13)), 1, 1000) "
        "AS body, ROW_NUMBER() OVER (PARTITION BY i.conversation_id "
        "ORDER BY i.event_seq DESC, i.event_ulid DESC) AS rn "
        f"FROM message_index i LEFT JOIN event_log e ON e.ulid = i.event_ulid{where}) "
        "WHERE rn = 1",
        [*params, *ids],
    ).fetchall()
    return {
        r["conversation_id"]: {
            "message_id": r["message_id"],
            "seq": r["event_seq"],
            "act": r["act"],
            "from_agent": r["sender"],
            "to_agent": r["recipient"],
            "from_session": r["sender_session"],
            "to_session": r["target_session"],
            "expects_reply": bool(r["expects_reply"]),
            "created_at": r["created_at"],
            "preview": message_index.preview_of(r["body"]),
        }
        for r in rows
    }


def _participants(
    conn: sqlite3.Connection, ids: list[str], clauses: list[str], params: list[Any]
) -> dict[str, tuple[list[str], list[str]]]:
    """Labels and session ids seen on each conversation's visible messages, first seen first."""
    if not ids:
        return {}
    where = _sql_where([*clauses, _in_clause(ids)])
    rows = conn.execute(
        "SELECT i.conversation_id, i.sender, i.recipient, i.sender_session, i.target_session "
        f"FROM message_index i{where} ORDER BY i.event_seq, i.event_ulid",
        [*params, *ids],
    ).fetchall()
    out: dict[str, tuple[list[str], list[str]]] = {}
    for r in rows:
        labels, sessions = out.setdefault(r["conversation_id"], ([], []))
        for label in (r["sender"], r["recipient"]):
            if label and label not in labels and len(labels) < MAX_PARTICIPANTS:
                labels.append(label)
        for sid in (r["sender_session"], r["target_session"]):
            if sid and sid not in sessions and len(sessions) < MAX_PARTICIPANTS:
                sessions.append(sid)
    return out


def conversation_thread(
    conversation_id: str,
    *,
    limit: int = MAX_LIST_LIMIT,
    after_seq: int | None = None,
    before_seq: int | None = None,
) -> dict[str, Any] | None:
    """The visible messages of one conversation, oldest first, or None if none is visible.

    Rows are ``message_list`` rows (same scope, same fields), with read receipts
    from the recipient's sessions folded into ``read_at`` and ``status``. Without a
    cursor the newest ``limit`` messages come back; with ``after_seq``, the oldest
    ``limit`` after it; with ``before_seq``, the newest ``limit`` before it (the
    "load earlier" page). ``has_older`` says whether visible messages older than
    the page exist, and ``oldest_seq`` is the ``before_seq`` that fetches them.
    None means "no such conversation" and "not yours" alike, so a probe learns
    nothing about other agents' mail. Messages whose event was pruned before the
    index carried a seq have no position, so only the newest page can show them.
    """
    if after_seq is not None and before_seq is not None:
        raise ValueError("pass after_seq or before_seq, not both")
    cap = max(1, min(int(limit), MAX_LIST_LIMIT))
    _, _, clauses, params = _scope()
    in_conversation = [*clauses, "i.conversation_id = ?"]
    scoped = [*params, conversation_id]
    cursor = after_seq
    if before_seq is not None:
        cursor = _page_floor(in_conversation, scoped, before_seq, cap)
    listed = list_messages(conversation_id=conversation_id, limit=cap, after_seq=cursor)
    rows = listed["messages"]
    if before_seq is not None:
        rows = [r for r in rows if r["seq"] is not None and r["seq"] < before_seq]
    if not rows and after_seq is None and before_seq is None:
        return None
    _fold_session_reads(rows)
    participants: list[str] = []
    for row in rows:
        for label in (row["from_agent"], row["to_agent"]):
            if label and label not in participants:
                participants.append(label)
    seqs = [r["seq"] for r in rows if r["seq"] is not None]
    oldest = min(seqs) if seqs else None
    return {
        "conversation_id": conversation_id,
        "participants": participants,
        "messages": rows,
        "total": len(rows),
        "next_seq": listed["next_seq"],
        "oldest_seq": oldest,
        "has_older": oldest is not None and _has_older(in_conversation, scoped, oldest),
    }


def _page_floor(clauses: list[str], params: list[Any], before_seq: int, cap: int) -> int:
    """The exclusive ``after_seq`` whose next ``cap`` rows end just below ``before_seq``."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT i.event_seq AS seq FROM message_index i"
            f"{_sql_where([*clauses, 'i.event_seq < ?'])} "
            "ORDER BY i.event_seq DESC LIMIT 1 OFFSET ?",
            [*params, before_seq, cap - 1],
        ).fetchone()
    finally:
        conn.close()
    # Fewer than ``cap`` older rows: start from the beginning and trim above the cursor.
    return int(row["seq"]) - 1 if row is not None else 0


def _has_older(clauses: list[str], params: list[Any], seq: int) -> bool:
    conn = storage.connect()
    try:
        row = conn.execute(
            f"SELECT 1 FROM message_index i{_sql_where([*clauses, 'i.event_seq < ?'])} LIMIT 1",
            [*params, seq],
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _fold_session_reads(rows: list[dict[str, Any]]) -> None:
    """Mark rows read when a session of the recipient label holds a read receipt.

    A receipt from a session registered to any other label is ignored.
    """
    pending = [r["message_id"] for r in rows if not r["read_at"]]
    if not pending:
        return
    conn = storage.connect()
    try:
        marks = ",".join("?" for _ in pending)
        receipts = {
            r["message_id"]: r["first_read"]
            for r in conn.execute(
                "SELECT r.message_id, MIN(r.read_at) AS first_read FROM message_reads r "
                "JOIN message_index m ON m.message_id = r.message_id AND m.org_id IS ? "
                "JOIN agent_sessions s ON s.id = r.session_id AND s.agent = m.recipient "
                "AND s.org_id IS m.org_id "
                f"WHERE r.message_id IN ({marks}) GROUP BY r.message_id",
                [message_index.tenant(), *pending],
            ).fetchall()
        }
    finally:
        conn.close()
    for row in rows:
        stamp = receipts.get(row["message_id"])
        if stamp and not row["read_at"]:
            row["read_at"] = stamp
            if row["status"] == "delivered":
                row["status"] = "read"
