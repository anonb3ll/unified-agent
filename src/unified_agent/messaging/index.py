"""Derived per-recipient mailbox index over the ``agent.message.*`` event log (task #1859).

The event log stays the source of truth: every message is still one
``agent.message.<act>`` row, and bodies are read from it. ``message_index`` is a
derived table with one row per message, written in the same SQLite transaction
as the event append, so the two cannot disagree after a failed send. It adds
what an event-log replay cannot give cheaply: a lookup by recipient that never
drops older mail, a gap-free paging cursor, per-recipient read and ack state, and
reply ("answered") tracking.

Paging cursor (task #1870). Each row carries ``event_seq``, its event's
``event_log`` ROWID: the order rows were *committed* in. Mail is listed and
acked by that seq, never by ULID. A writer that commits late can land a row whose
ULID sorts before one a consumer has already passed, so a ULID cursor can skip
it, and an ack-through-ULID can mark it acked unseen (see ``events.query_events``
and ``events.seq_for_ulid_cursor``). A seq cursor cannot: a late row always has a
higher seq than anything committed before it. The ULID ``since`` cursor was
retired before any consumer used it; ``messaging.send`` refuses it.

Read state (``read_at``, ``acked_at``) lives only in this table. It is derived
operational state, not evidence: nothing here rewrites or deletes an event.

Session addressing (task #1860). A message names a recipient label and, optionally,
one ``target_session``. Label mail (``target_session`` NULL) is for every session of
the label; session mail is for that session alone. Read state follows the address:

* session mail has exactly one reader, so its ``read_at``/``acked_at`` stay on the
  ``message_index`` row;
* label mail read or acked *through a session* gets a ``message_reads`` receipt keyed
  ``(message_id, session_id)`` and leaves the row alone, so it stays unread for the
  label's other sessions;
* label mail read or acked with no session (the pre-#1860 call shape) sets the row's
  own ``read_at``/``acked_at``, which means "read by the label" and counts as read
  for every session. Backfilled history relies on this: it comes in already read.

A sender may also record its own session (``sender_session``, exposed as
``from_session``), which is what lets a reply go back to that session alone.

Tenancy (task #1979). Each row carries the ``org_id`` of the tenant that sent it
(``edition.tenant_org_id()``, the same stamp as its event). A message id is unique
per tenant, not globally: the unique key is ``(message_id, COALESCE(org_id, ''))``,
so the id space matches the per-tenant ``message_send`` receipt. Every read and
write here is fenced with ``org_id IS <tenant>``, so another tenant's mail reads
as absent. The untenanted private hub stamps and fences NULL, which is every row
it has, so nothing changes there.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import edition, storage
from .schema import CHANNEL_MESSAGE_PREFIX, MAX_LIST_LIMIT

_BACKFILL_MARKER = "message_index_backfill"
_BACKFILL_BATCH = 500
# Backfilled mail older than this comes in already read, so the first inbox
# check after deploy surfaces recent mail, not the fleet's whole history.
BACKFILL_UNREAD_WINDOW = timedelta(days=3)

_TABLE = """
CREATE TABLE IF NOT EXISTS message_index (
  message_id TEXT NOT NULL,
  recipient TEXT NOT NULL,
  sender TEXT NOT NULL,
  act TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  reply_to TEXT,
  expects_reply INTEGER NOT NULL DEFAULT 0,
  task_id INTEGER,
  delegation_id INTEGER,
  event_ulid TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read_at TEXT,
  acked_at TEXT,
  target_session TEXT,
  sender_session TEXT,
  event_seq INTEGER,
  org_id TEXT
)"""
_DDL = (
    _TABLE
    + """;
CREATE TABLE IF NOT EXISTS message_reads (
  message_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  read_at TEXT NOT NULL,
  acked_at TEXT,
  PRIMARY KEY (message_id, session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_index_event ON message_index(event_ulid);
CREATE INDEX IF NOT EXISTS idx_message_index_recipient
  ON message_index(recipient, event_ulid);
CREATE INDEX IF NOT EXISTS idx_message_index_sender ON message_index(sender, event_ulid);
CREATE INDEX IF NOT EXISTS idx_message_index_conversation
  ON message_index(conversation_id, event_ulid);
CREATE INDEX IF NOT EXISTS idx_message_index_reply_to
  ON message_index(reply_to) WHERE reply_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_index_unread
  ON message_index(recipient) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_message_index_task
  ON message_index(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_index_delegation
  ON message_index(delegation_id) WHERE delegation_id IS NOT NULL;
"""
)
#: Unique per tenant (#1979). Also serves lookups by ``message_id`` (leftmost column).
_TENANT_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_message_index_tenant_id "
    "ON message_index(message_id, COALESCE(org_id, ''))"
)
#: Columns of the pre-#1979 table, in order; ``org_id`` is the one it lacks.
_LEGACY_COLUMNS = (
    "message_id, recipient, sender, act, conversation_id, reply_to, expects_reply, "
    "task_id, delegation_id, event_ulid, created_at, read_at, acked_at, target_session, "
    "sender_session, event_seq"
)


def tenant() -> str | None:
    """The tenant every row here is stamped with and fenced by (task #1979)."""
    return edition.tenant_org_id()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``message_index`` (idempotent) and backfill it until that is recorded.

    Called from ``storage._migrate()``. History is replayed from the event log
    until the ``message_index_backfill`` marker in ``meta`` exists; the live write
    path keeps the index current afterwards. The marker is written in the same
    transaction as the backfilled rows, so a startup that stops part-way rolls
    back both and the next startup runs the backfill again.
    """
    existed = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_index'"
        ).fetchone()
        is not None
    )
    if not existed:
        # executescript commits the new table on its own, outside the backfill
        # transaction. Drop any marker first and commit it, so a replay that
        # stops part-way leaves no marker and the next start runs it again.
        conn.execute("DELETE FROM meta WHERE key=?", (_BACKFILL_MARKER,))
        conn.commit()
    conn.executescript(_DDL)
    # target_session arrived with #1860: add it to a table the #1859 release created.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(message_index)").fetchall()}
    if "target_session" not in columns:
        conn.execute("ALTER TABLE message_index ADD COLUMN target_session TEXT")
    if "sender_session" not in columns:
        conn.execute("ALTER TABLE message_index ADD COLUMN sender_session TEXT")
    # event_seq arrived with #1870 (see the module docstring).
    if "event_seq" not in columns:
        conn.execute("ALTER TABLE message_index ADD COLUMN event_seq INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_index_target "
        "ON message_index(target_session) WHERE target_session IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_index_recipient_seq "
        "ON message_index(recipient, event_seq)"
    )
    _rebuild_with_tenant_key(conn)
    conn.execute(_TENANT_UNIQUE)
    # Partition index (#1433, task #2326): _TENANT_UNIQUE leads on an
    # expression, which an org_id lookup cannot use.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_index_org_id ON message_index(org_id)")
    done = conn.execute("SELECT 1 FROM meta WHERE key=?", (_BACKFILL_MARKER,)).fetchone()
    if done is None:
        backfill(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (_BACKFILL_MARKER, storage.now()),
        )
    repair_event_seq(conn)


def _rebuild_with_tenant_key(conn: sqlite3.Connection) -> None:
    """Replace a global ``message_id`` PRIMARY KEY with the per-tenant key, once (#1979).

    SQLite cannot drop a PRIMARY KEY in place, so a table created before #1979
    gets the copy-and-rename rebuild in one explicit transaction: an error
    anywhere rolls back to the original table. Each copied row takes the
    ``org_id`` of its own event; a row whose event was pruned stays NULL
    (untenanted). The indexes the DROP removed are recreated here; the caller
    adds the per-tenant unique index.
    """
    info = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(message_index)")}
    if not info.get("message_id"):
        if "org_id" not in info:
            conn.execute("ALTER TABLE message_index ADD COLUMN org_id TEXT")
        return
    rebuild_table = _TABLE.replace("IF NOT EXISTS message_index", "message_index__rebuild")
    try:
        conn.executescript(
            f"""
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS message_index__rebuild;
            {rebuild_table};
            INSERT INTO message_index__rebuild ({_LEGACY_COLUMNS}, org_id)
              SELECT {_LEGACY_COLUMNS},
                (SELECT e.org_id FROM event_log e WHERE e.ulid = message_index.event_ulid)
              FROM message_index;
            DROP TABLE message_index;
            ALTER TABLE message_index__rebuild RENAME TO message_index;
            COMMIT;
            """
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.executescript(_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_index_target "
        "ON message_index(target_session) WHERE target_session IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_index_recipient_seq "
        "ON message_index(recipient, event_seq)"
    )


def repair_event_seq(conn: sqlite3.Connection) -> int:
    """Fill ``event_seq`` on rows that lack it from the event's ROWID; return rows set.

    Covers rows indexed before the column existed. Idempotent: it touches only
    rows with a NULL ``event_seq``. A row whose event retention has already
    pruned has nothing to join and stays NULL; cursor paging by seq skips it
    (it is older than anything the log still holds), and it still shows in the
    newest-window read.
    """
    cur = conn.execute(
        "UPDATE message_index SET event_seq = "
        "(SELECT e.ROWID FROM event_log e WHERE e.ulid = message_index.event_ulid) "
        "WHERE event_seq IS NULL"
    )
    return max(cur.rowcount, 0)


def backfill(conn: sqlite3.Connection) -> int:
    """Index every ``agent.message.*`` event that has no row yet; return rows added.

    Idempotent: ``INSERT OR IGNORE`` keyed on ``message_id`` (and the unique
    ``event_ulid``), so a second run adds nothing. Historical mail has no read
    state: mail newer than ``BACKFILL_UNREAD_WINDOW`` comes in unread, and older
    mail comes in read (stamped with the backfill time), so an inbox hook does
    not replay weeks of history. Events whose payload lacks a message id, sender
    or recipient are skipped.
    """
    stamped = datetime.now(UTC)
    read_cutoff = (stamped - BACKFILL_UNREAD_WINDOW).isoformat(timespec="seconds")
    read_stamp = stamped.isoformat(timespec="seconds")
    added = 0
    cursor = ""
    like = f"{CHANNEL_MESSAGE_PREFIX}.%"
    while True:
        rows = conn.execute(
            "SELECT ROWID AS seq, ulid, payload, created_at, org_id FROM event_log "
            "WHERE channel LIKE ? AND ulid > ? ORDER BY ulid LIMIT ?",
            (like, cursor, _BACKFILL_BATCH),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            cursor = row["ulid"]
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            mid = str(payload.get("message_id") or "").strip()
            sender = str(payload.get("from_agent") or "").strip()
            recipient = str(payload.get("to_agent") or "").strip()
            if not (mid and sender and recipient):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO message_index (message_id, recipient, sender, act, "
                "conversation_id, reply_to, expects_reply, task_id, delegation_id, "
                "event_ulid, created_at, read_at, target_session, sender_session, event_seq, "
                "org_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    recipient,
                    sender,
                    str(payload.get("act") or ""),
                    str(payload.get("conversation_id") or row["ulid"]),
                    str(payload.get("reply_to") or "") or None,
                    1 if payload.get("expects_reply") else 0,
                    _int_or_none(payload.get("task_id")),
                    _int_or_none(payload.get("delegation_id")),
                    row["ulid"],
                    row["created_at"],
                    read_stamp if str(row["created_at"] or "") < read_cutoff else None,
                    str(payload.get("to_session") or "").strip() or None,
                    str(payload.get("from_session") or "").strip() or None,
                    int(row["seq"]),
                    row["org_id"],
                ),
            )
            added += cur.rowcount
    return added


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def insert_on_connection(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient: str,
    sender: str,
    act: str,
    conversation_id: str,
    reply_to: str | None,
    expects_reply: bool,
    task_id: int | None,
    delegation_id: int | None,
    event_ulid: str,
    created_at: str,
    target_session: str | None = None,
    sender_session: str | None = None,
    event_seq: int | None = None,
) -> None:
    """Add one index row on the caller's open transaction. Never commits.

    ``event_seq`` is the event's ``event_log`` ROWID. Pass it when the caller
    already has it; otherwise it is read back from the event, which the caller's
    transaction has already appended.
    """
    if event_seq is None:
        found = conn.execute("SELECT ROWID AS seq FROM event_log WHERE ulid=?", (event_ulid,))
        row = found.fetchone()
        event_seq = int(row["seq"]) if row is not None else None
    conn.execute(
        "INSERT INTO message_index (message_id, recipient, sender, act, conversation_id, "
        "reply_to, expects_reply, task_id, delegation_id, event_ulid, created_at, "
        "target_session, sender_session, event_seq, org_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id,
            recipient,
            sender,
            act,
            conversation_id,
            reply_to,
            1 if expects_reply else 0,
            task_id,
            delegation_id,
            event_ulid,
            created_at,
            target_session,
            sender_session,
            event_seq,
            tenant(),
        ),
    )


def get_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    """The caller's tenant's row for ``message_id``; another tenant's reads as None."""
    return conn.execute(
        "SELECT * FROM message_index WHERE message_id=? AND org_id IS ?", (message_id, tenant())
    ).fetchone()


def unread_count(
    agent: str, conn: sqlite3.Connection | None = None, session_id: str | None = None
) -> int:
    """Unread messages in ``agent``'s mailbox: one indexed query.

    With no ``session_id`` that is the label's mailbox: label mail whose row is
    unread. With one it is that session's: label mail the session has no receipt
    for, plus mail addressed to the session, minus anything the label read.
    """
    own = conn is None
    conn = conn or storage.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM message_index i "
            f"LEFT JOIN message_reads r ON {_RECEIPT_JOIN} WHERE {_UNREAD_MAILBOX}",
            (session_id, agent, session_id, tenant()),
        ).fetchone()
        return int(row["n"])
    finally:
        if own:
            conn.close()


#: The unread-mailbox rule, shared by ``unread_count`` and ``inbox_summary``. Bind
#: ``(session_id, agent, session_id, tenant)``: the receipt join takes the first, the WHERE
#: the rest.
_RECEIPT_JOIN = "r.message_id = i.message_id AND r.session_id = ?"
_UNREAD_MAILBOX = (
    "i.recipient = ? AND i.read_at IS NULL AND r.read_at IS NULL "
    "AND (i.target_session IS NULL OR i.target_session = ?) AND i.org_id IS ?"
)
#: Partial index over unread rows by recipient: a mailbox full of read mail costs nothing.
_UNREAD_INDEX = "idx_message_index_unread"
#: Characters of a body's first line kept as a preview.
PREVIEW_CHARS = 160
#: Hard cap on ``inbox_summary`` rows, whatever the caller asks for.
SUMMARY_MAX_LIMIT = 20
# Enough of a body to find its first line without loading a whole large message.
_PREVIEW_SCAN_CHARS = 1000


def preview_of(body: Any) -> str:
    """First non-empty line of ``body``, cut to ``PREVIEW_CHARS`` characters."""
    if not isinstance(body, str):
        return ""
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line[:PREVIEW_CHARS]


def inbox_summary(
    conn: sqlite3.Connection,
    agent: str,
    session_id: str | None = None,
    *,
    limit: int = 5,
    after_seq: int | None = None,
) -> dict[str, Any]:
    """A read-only digest of ``agent``'s unread mail, newest first. Two indexed queries.

    Unread follows ``unread_count``: label mail (minus anything the label or this
    session read) plus mail addressed to ``session_id``. It never writes. ``unread``
    and ``newest_seq`` cover all unread mail; ``messages`` is the newest ``limit``
    (at most ``SUMMARY_MAX_LIMIT``) with ``seq > after_seq`` when that is given.
    It is a snapshot, not a pager: it returns no resume cursor, because a page of
    the newest mail says nothing about older unread mail below it. Use
    ``newest_seq`` to tell whether mail arrived since a previous snapshot, and
    ``message_inbox`` (``after_seq`` / ``next_seq``) to read mail in order.
    """
    cap = max(1, min(int(limit), SUMMARY_MAX_LIMIT))
    totals = conn.execute(
        "SELECT COUNT(*) AS n, MAX(i.event_seq) AS newest "
        f"FROM message_index i INDEXED BY {_UNREAD_INDEX} "
        f"LEFT JOIN message_reads r ON {_RECEIPT_JOIN} WHERE {_UNREAD_MAILBOX}",
        (session_id, agent, session_id, tenant()),
    ).fetchone()
    params: list[Any] = [_PREVIEW_SCAN_CHARS, session_id, agent, session_id, tenant()]
    after_sql = ""
    if after_seq is not None:
        after_sql = " AND i.event_seq > ?"
        params.append(int(after_seq))
    rows = conn.execute(
        "SELECT i.message_id, i.event_seq, i.sender, i.act, i.conversation_id, i.task_id, "
        "i.expects_reply, i.created_at, "
        "substr(ltrim(json_extract(e.payload, '$.body'), ' ' || char(9, 10, 13)), 1, ?) "
        "AS body "
        f"FROM message_index i INDEXED BY {_UNREAD_INDEX} "
        f"LEFT JOIN message_reads r ON {_RECEIPT_JOIN} "
        "LEFT JOIN event_log e ON e.ulid = i.event_ulid "
        f"WHERE {_UNREAD_MAILBOX}{after_sql} "
        "ORDER BY i.event_seq DESC, i.event_ulid DESC LIMIT ?",
        [*params, cap],
    ).fetchall()
    messages = [
        {
            "message_id": r["message_id"],
            "seq": r["event_seq"],
            "from_agent": r["sender"],
            "act": r["act"],
            "conversation_id": r["conversation_id"],
            "task_id": r["task_id"],
            "expects_reply": bool(r["expects_reply"]),
            "created_at": r["created_at"],
            "preview": preview_of(r["body"]),
        }
        for r in rows
    ]
    return {
        "unread": int(totals["n"]),
        "newest_seq": totals["newest"] if totals["newest"] is not None else 0,
        "messages": messages,
    }


# A reply counts as an answer only when it goes from the message's recipient back
# to its sender: a follow-up by the sender, or a reply addressed to a third agent,
# must not clear "waiting on reply". A SQL fragment over alias ``i``; public so the
# delivery sweep and the conversation reads share one definition.
ANSWERED_BY = (
    "(SELECT r.message_id FROM message_index r "
    "WHERE r.reply_to = i.message_id AND r.sender = i.recipient "
    "AND r.recipient = i.sender AND r.org_id IS i.org_id "
    "ORDER BY r.event_ulid LIMIT 1)"
)


def _where(
    *,
    recipient: str | None,
    sender: str | None,
    party: str | None,
    conversation_id: str | None,
    act: str | None,
    message_id: str | None,
    task_id: int | None,
    delegation_id: int | None,
    after_seq: int | None,
    unread_only: bool,
    waiting_on_reply: bool,
    mailbox: tuple[str, str | None] | None = None,
) -> tuple[str, list[Any]]:
    # Every read is fenced to the caller's tenant (#1979).
    clauses: list[str] = ["i.org_id IS ?"]
    params: list[Any] = [tenant()]
    for column, value in (
        ("i.recipient", recipient),
        ("i.sender", sender),
        ("i.conversation_id", conversation_id),
        ("i.act", act),
        ("i.message_id", message_id),
        ("i.task_id", task_id),
        ("i.delegation_id", delegation_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if party is not None:
        clauses.append("(i.recipient = ? OR i.sender = ?)")
        params.extend([party, party])
    if after_seq is not None:
        clauses.append("i.event_seq > ?")
        params.append(int(after_seq))
    if unread_only:
        clauses.append("i.read_at IS NULL AND r.read_at IS NULL")
    if mailbox is not None:
        # Acting as the mailbox owner: mail addressed to the owner's label is
        # visible when it is label mail or addressed to this very session.
        # Mail the owner sent, and mail to other labels, is not affected.
        clauses.append("(i.recipient != ? OR i.target_session IS NULL OR i.target_session = ?)")
        params.extend(mailbox)
    if waiting_on_reply:
        clauses.append(f"i.expects_reply = 1 AND {ANSWERED_BY} IS NULL")
    return " WHERE " + " AND ".join(clauses), params


def query(
    conn: sqlite3.Connection,
    *,
    recipient: str | None = None,
    sender: str | None = None,
    party: str | None = None,
    conversation_id: str | None = None,
    act: str | None = None,
    message_id: str | None = None,
    task_id: int | None = None,
    delegation_id: int | None = None,
    after_seq: int | None = None,
    unread_only: bool = False,
    waiting_on_reply: bool = False,
    mailbox: tuple[str, str | None] | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Index rows joined to their event, oldest first, in commit (``event_seq``) order.

    With ``after_seq`` (an exclusive cursor) the oldest ``limit`` rows after it
    come back, so a poller can page forward without skipping a late-committed
    row. Without it the newest ``limit`` rows come back, still oldest first. ``party`` limits to mail the
    named agent sent or received. ``body``/``hop``/``event_id`` come from the
    event row; when retention pruned it they are ``None`` and ``body_pruned`` is
    true.

    ``mailbox=(label, session_id)`` applies the session-addressing rule for a
    caller acting as that mailbox's owner (see the module docstring), and
    ``session_id`` selects whose read receipts fill ``read_at``/``acked_at`` and
    ``unread_only``. Pass them together to read a session's inbox.
    """
    where, params = _where(
        recipient=recipient,
        sender=sender,
        party=party,
        conversation_id=conversation_id,
        act=act,
        message_id=message_id,
        task_id=task_id,
        delegation_id=delegation_id,
        after_seq=after_seq,
        unread_only=unread_only,
        waiting_on_reply=waiting_on_reply,
        mailbox=mailbox,
    )
    cap = max(1, min(int(limit), MAX_LIST_LIMIT))
    order = "ASC" if after_seq is not None else "DESC"
    sql = (
        "SELECT i.*, COALESCE(i.read_at, r.read_at) AS eff_read_at, "
        "COALESCE(i.acked_at, r.acked_at) AS eff_acked_at, "
        f"e.channel AS channel, e.payload AS payload, {ANSWERED_BY} AS answered_by "
        "FROM message_index i "
        "LEFT JOIN message_reads r ON r.message_id = i.message_id AND r.session_id = ? "
        "LEFT JOIN event_log e ON e.ulid = i.event_ulid"
        f"{where} ORDER BY i.event_seq {order}, i.event_ulid {order} LIMIT ?"
    )
    rows = conn.execute(sql, [session_id, *params, cap]).fetchall()
    out = [_public(row) for row in rows]
    if after_seq is None:
        out.reverse()
    return out


def count(conn: sqlite3.Connection, **filters: Any) -> int:
    """How many index rows match the same filters ``query`` takes (no limit)."""
    keys = (
        "recipient",
        "sender",
        "party",
        "conversation_id",
        "act",
        "message_id",
        "task_id",
        "delegation_id",
        "after_seq",
        "unread_only",
        "waiting_on_reply",
        "mailbox",
    )
    where, params = _where(**{key: filters.get(key) for key in keys})  # type: ignore[arg-type]
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM message_index i "
        "LEFT JOIN message_reads r ON r.message_id = i.message_id AND r.session_id = ?"
        f"{where}",
        [filters.get("session_id"), *params],
    ).fetchone()
    return int(row["n"])


def _public(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    pruned = row["payload"] is None
    if not pruned:
        try:
            loaded = json.loads(row["payload"])
            payload = loaded if isinstance(loaded, dict) else {}
        except (TypeError, ValueError):
            payload = {}
    answered_by = row["answered_by"]
    if answered_by:
        status = "answered"
    elif row["eff_read_at"]:
        status = "read"
    else:
        status = "delivered"
    return {
        "seq": row["event_seq"],
        "ulid": row["event_ulid"],
        "channel": row["channel"] or f"{CHANNEL_MESSAGE_PREFIX}.{row['act']}",
        "created_at": row["created_at"],
        "message_id": row["message_id"],
        "act": row["act"],
        "conversation_id": row["conversation_id"],
        "reply_to": row["reply_to"],
        "from_agent": row["sender"],
        "to_agent": row["recipient"],
        "hop": payload.get("hop"),
        "body": payload.get("body"),
        "task_id": row["task_id"],
        "delegation_id": row["delegation_id"],
        "event_id": payload.get("event_id"),
        "expects_reply": bool(row["expects_reply"]),
        "to_session": row["target_session"],
        "from_session": row["sender_session"],
        "read_at": row["eff_read_at"],
        "acked_at": row["eff_acked_at"],
        "answered": bool(answered_by),
        "answered_by": answered_by,
        "status": status,
        "body_pruned": pruned,
        # Typed handoff fields (task #2390), only on a handoff that carried them.
        **({"handoff": payload["handoff"]} if isinstance(payload.get("handoff"), dict) else {}),
    }


def _read_stamp() -> str:
    """UTC read time with the same precision/format as wake admission.

    Inbox and ack callers hold the SQLite writer lock before this sample, so
    the stamp describes the committed read's side of an admission. Keep the
    fractional precision; rounding to seconds loses same-second causal order.
    Auto formatting also keeps exact whole-second equality equal to admission,
    instead of comparing '.000000' lexically after an otherwise identical time.
    """
    return datetime.now(UTC).isoformat()


def mark_read(
    conn: sqlite3.Connection,
    recipient: str,
    message_ids: list[str],
    session_id: str | None = None,
) -> tuple[int, str]:
    """Mark ``recipient``'s own unread mail among ``message_ids`` as read.

    Without ``session_id`` this is the label's read: it sets ``read_at`` on label
    mail (session mail is never touched, since a sessionless caller cannot see it).
    With one it is that session's read: mail addressed to the session gets
    ``read_at`` on its row, and label mail gets a ``message_reads`` receipt that
    leaves the row, and so the label's other sessions, unread.

    Returns ``(rows newly marked, the timestamp used)``.
    """
    stamp = _read_stamp()
    org = tenant()
    total = 0
    for start in range(0, len(message_ids), 500):
        chunk = message_ids[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        if session_id is None:
            cur = conn.execute(
                f"UPDATE message_index SET read_at=? WHERE recipient=? AND org_id IS ? "
                f"AND read_at IS NULL AND target_session IS NULL AND message_id IN ({marks})",
                [stamp, recipient, org, *chunk],
            )
            total += cur.rowcount
            continue
        cur = conn.execute(
            f"UPDATE message_index SET read_at=? WHERE recipient=? AND org_id IS ? "
            f"AND read_at IS NULL AND target_session=? AND message_id IN ({marks})",
            [stamp, recipient, org, session_id, *chunk],
        )
        total += cur.rowcount
        cur = conn.execute(
            f"INSERT OR IGNORE INTO message_reads (message_id, session_id, read_at) "
            f"SELECT message_id, ?, ? FROM message_index WHERE recipient=? AND org_id IS ? "
            f"AND read_at IS NULL AND target_session IS NULL AND message_id IN ({marks})",
            [session_id, stamp, recipient, org, *chunk],
        )
        total += cur.rowcount
    return total, stamp


def ack(
    conn: sqlite3.Connection,
    recipient: str,
    *,
    message_id: str | None = None,
    through_seq: int | None = None,
    session_id: str | None = None,
    include_pruned: bool = False,
) -> int:
    """Ack ``recipient``'s own mail: one message, or everything up to a seq cursor.

    ``through_seq`` acks only rows whose ``event_seq`` is at or below it, so a
    late-committed row (higher seq, older ULID) is never acked by a cursor that
    was taken before it landed. Rows with no ``event_seq`` (their event was
    pruned before the column existed) have no position and are matched only with
    ``include_pruned``, which also acks them: they are older than anything the
    log still holds.

    An ack also reads the message. The first ack time is kept, so acking twice
    is a no-op. Scope follows ``mark_read``: with no ``session_id`` the label acks
    label mail on the row; with one the session acks its own mail on the row and
    label mail through a receipt, which leaves the label's other sessions' unread
    and unacked. Returns the number of rows newly acked.
    """
    stamp = _read_stamp()
    if message_id is not None:
        pick = "{p}message_id=?"
    elif include_pruned:
        pick = "({p}event_seq <= ? OR {p}event_seq IS NULL)"
    else:
        pick = "{p}event_seq <= ?"
    key = message_id if message_id is not None else through_seq
    scope = "target_session IS NULL" if session_id is None else "target_session=?"
    scope_args = [] if session_id is None else [session_id]
    cur = conn.execute(
        "UPDATE message_index SET acked_at=COALESCE(acked_at, ?), "
        "read_at=COALESCE(read_at, ?) "
        f"WHERE recipient=? AND org_id IS ? AND {pick.format(p='')} AND acked_at IS NULL "
        f"AND {scope}",
        [stamp, stamp, recipient, tenant(), key, *scope_args],
    )
    total = cur.rowcount
    if session_id is not None:
        # Label mail the label has not acked: record the session's own ack.
        total += _ack_label_mail_for_session(conn, recipient, session_id, pick, key, stamp)
    return total


def _ack_label_mail_for_session(
    conn: sqlite3.Connection,
    recipient: str,
    session_id: str,
    pick: str,
    key: str | int | None,
    stamp: str,
) -> int:
    pending = conn.execute(
        "SELECT i.message_id FROM message_index i "
        "LEFT JOIN message_reads r ON r.message_id = i.message_id AND r.session_id = ? "
        f"WHERE i.recipient=? AND i.org_id IS ? AND i.target_session IS NULL "
        f"AND i.acked_at IS NULL AND r.acked_at IS NULL AND {pick.format(p='i.')}",
        (session_id, recipient, tenant(), key),
    ).fetchall()
    for row in pending:
        conn.execute(
            "INSERT INTO message_reads (message_id, session_id, read_at, acked_at) "
            "VALUES (?,?,?,?) ON CONFLICT(message_id, session_id) "
            "DO UPDATE SET acked_at=excluded.acked_at",
            (row["message_id"], session_id, stamp, stamp),
        )
    return len(pending)
