"""Who is due a wake: the hub half of waking an idle session (tasks #1873, #2050).

Design: ``docs/designs/2026-10-wake-idle-session.md``. Two locks, both required.

1. The request. A session launched with ``UA_WAKE=1`` registers with
   ``wake: true``; the inbox-session route records it here (``set_opt_in``). The
   hook uses the harness's own bearer, so the agent can send the same POST: a
   request is therefore only a request and never enables a wake by itself. A
   registration without ``wake`` clears it.
2. The grant. Only the verified human operator sets it (``set_grant``), through
   the operator-only ``POST /api/sessions/{id}/wake-grant`` route; no MCP tool and
   no agent or session credential reaches it. A grant is bound to one hub
   session id: it survives that session registering again, a new session needs
   a new one, and the operator revokes it the same way. Every change is on the
   event log (``CHANNEL_GRANT``) with the operator as originator.
3. The decline (task #2514). The operator may refuse a request
   (``set_decline``, ``POST /api/sessions/{id}/wake-decline``) with a reason. A
   decline is recorded (who, when, why), revokes any grant, drops the request
   from the operator inbox, and is bound to the session id: the session asking
   again, or withdrawing and asking again, does not clear it. Only a later
   grant does. ``_candidates`` skips a declined session even if a grant column
   were set behind the route's back.

The ua-node on the session's host long-polls ``POST /api/node/wake``.
``due_wakes`` applies every gate centrally and hands back ids and counts only:
``{session_id, external_session_id, unread, newest_seq, expires_at, offer_id}``.
The node types ``wake_text(unread)`` through herdr and reports the outcome
(``record_receipt``), echoing the offer's ``newest_seq`` so a receipt for a
lapsed offer never answers a newer one. Nodes check live consent and the
absolute offer deadline before every attempt; revocation clears pending offers.

Gates, in order: request and grant; an open session on the caller's host; no offer
outstanding; the retry hold after a failed or skipped offer; the circuit
breaker (``BREAKER_AFTER`` delivered wakes with no inbox read in between pause
the session until it reads mail); the floor between wakes;
the hourly cap; at least one unread ``WAKE_ACTS`` or ``expects_reply`` message
newer than the last woken set (``newest_seq`` must advance); and the trailing
debounce. Counts are computed at offer time, so mail read during the debounce
never fires a wake.

This module reads mailbox metadata only (``message_index``, ``message_reads``).
It never reads the event log, so no message text can reach a wake.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import edition, events, storage
from ..node_registry import NodeError, _owned_node, validate_node_name
from .index import _RECEIPT_JOIN, _UNREAD_MAILBOX, unread_count

_log = logging.getLogger(__name__)

#: Acts that ring the doorbell. ``inform`` and ``done`` wait for the hook,
#: unless the sender set ``expects_reply``.
WAKE_ACTS = frozenset({"request", "query", "handoff"})
#: The only text ever typed. One constant and one integer.
WAKE_TEXT = "You have {n} new hub messages; run message_inbox"

DEBOUNCE_QUIET_S = 20
DEBOUNCE_MAX_HOLD_S = 60
FLOOR_S = 600
HOURLY_CAP = 6
BREAKER_AFTER = 3
#: An offer with no receipt blocks a re-offer this long, then lapses.
OFFER_LEASE_S = 120
#: ``delivered_at`` is the node's clock; admission and "now" are the hub's. Their
#: difference on NTP-synced hosts is milliseconds, more than the time a receipt
#: takes to arrive (task #2424). The lease and consent bounds stay exact.
RECEIPT_CLOCK_SKEW_S = 5.0
#: After a failed or skipped offer (busy, focused, blocked), retry this much later.
RETRY_S = 30
#: Retries per mail set before it is given up and left to the hook.
MAX_ATTEMPTS = 20

OUTCOMES = frozenset({"delivered", "failed", "skipped"})
#: Skips left to the hook, without repeating an absent or uncertain prompt.
FINAL_SKIPS = frozenset(
    {
        "no_match",
        "herdr_missing",
        "input_state_unavailable",
        "already_delivered",
        "delivery_uncertain",
    }
)
CHANNEL_WAKE = "agent.wake"
CHANNEL_GRANT = "agent.wake.grant"
CHANNEL_REQUEST = "agent.wake.request"
CHANNEL_ATTEMPT = "agent.wake.attempt"
CHANNEL_REVIVED = "agent.wake.session_revived"
PAUSED_NO_READ = "no_inbox_read"
#: Panes one node may report on a wake poll, and the length of one session id.
MAX_LIVE_SESSIONS = 64
MAX_EXTERNAL_ID_CHARS = 512
#: The only way a session may be reopened by a pane report: the reaper closed it.
_REAPED_EXIT = "no_heartbeat"
_REASON_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
_OPEN = ("starting", "running", "waiting")


def wake_text(n: Any) -> str:
    """The wake line. ``int()`` first, so nothing but a number can be formatted in."""
    return WAKE_TEXT.format(n=int(n))


def host_key(host: Any) -> str:
    """Compare hosts by short lowercase name: ``Box.local`` and ``box`` are one host."""
    return str(host or "").strip().lower().split(".")[0]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ago(now: datetime, raw: Any) -> float | None:
    when = _ts(raw)
    return None if when is None else (now - when).total_seconds()


# --- request and grant -----------------------------------------------------------


def set_opt_in(session_id: str, wake: bool, *, conn: sqlite3.Connection | None = None) -> None:
    """Record a registration's ``wake`` flag: a request, never a grant.

    Only the inbox-session route calls this. ``wake=True`` records the request
    (idempotent, keeping the first ``requested_at``) and preserves the breaker
    across routine refreshes. A new hub session starts with new state; an
    existing session resets only after an inbox read. ``wake=False`` clears the
    request of a session that had one and creates nothing. Neither touches the
    operator's grant.
    """
    own = conn is None
    db = conn or storage.connect()
    published = None
    try:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        now = storage.now()
        before = db.execute(
            "SELECT opted_in FROM session_wake WHERE session_id=?", (session_id,)
        ).fetchone()
        changed = bool(before and before["opted_in"]) != wake
        if wake:
            db.execute(
                "INSERT INTO session_wake (session_id, opted_in, requested_at, updated_at) "
                "VALUES (?, 1, ?, ?) ON CONFLICT(session_id) DO UPDATE SET opted_in=1, "
                "requested_at=COALESCE(requested_at, excluded.requested_at), "
                "updated_at=excluded.updated_at",
                (session_id, now, now),
            )
        else:
            db.execute(
                "UPDATE session_wake SET opted_in=0, requested_at=NULL, "
                "offered_at=NULL, offered_seq=NULL, offered_id=NULL, updated_at=? "
                "WHERE session_id=? AND opted_in=1",
                (now, session_id),
            )
        if changed and not wake:
            db.execute(
                "UPDATE session_wake_offers SET invalidated_at=? WHERE session_id=? "
                "AND recorded_at IS NULL AND invalidated_at IS NULL",
                (datetime.now(UTC).isoformat(), session_id),
            )
        if changed:
            session = db.execute(
                "SELECT agent FROM agent_sessions WHERE id=? AND org_id IS ?",
                (session_id, edition.tenant_org_id()),
            ).fetchone()
            if session is not None:
                _, _, published = events.record_event_once_on_connection(
                    db,
                    CHANNEL_REQUEST,
                    {
                        "kind": "requested" if wake else "withdrawn",
                        "session_id": session_id,
                        "agent": session["agent"],
                    },
                    originator=session["agent"],
                )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        if own:
            db.close()
    if published is not None:
        events.publish(published)


def set_grant(session_id: str, grant: bool, *, actor: str) -> dict[str, Any] | None:
    """The operator grants (or revokes) wakes for one hub session; ``None`` if unknown.

    The caller has already verified ``actor`` is the human operator: only the
    operator-only wake-grant route calls this. A change and its ``CHANNEL_GRANT``
    event commit together; a call that changes nothing records nothing.
    """
    sid = str(session_id or "").strip()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT agent FROM agent_sessions WHERE id=? AND org_id IS ?",
            (sid, edition.tenant_org_id()),
        ).fetchone()
        if session is None:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT opted_in, granted_at FROM session_wake WHERE session_id=?", (sid,)
        ).fetchone()
        requested = bool(row and row["opted_in"])
        changed = bool(row and row["granted_at"]) != grant
        published = None
        if changed:
            now = storage.now()
            if grant:
                # A grant is the operator changing their mind: it clears a decline.
                conn.execute(
                    "INSERT INTO session_wake (session_id, granted_at, granted_by, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "granted_at=excluded.granted_at, granted_by=excluded.granted_by, "
                    "declined_at=NULL, declined_by=NULL, decline_reason=NULL, "
                    "updated_at=excluded.updated_at",
                    (sid, now, actor, now),
                )
            else:
                conn.execute(
                    "UPDATE session_wake_offers SET invalidated_at=? WHERE session_id=? "
                    "AND recorded_at IS NULL AND invalidated_at IS NULL",
                    (datetime.now(UTC).isoformat(), sid),
                )
                conn.execute(
                    "UPDATE session_wake SET granted_at=NULL, granted_by=NULL, "
                    "offered_at=NULL, offered_seq=NULL, offered_id=NULL, updated_at=? "
                    "WHERE session_id=?",
                    (now, sid),
                )
            _, _, published = events.record_event_once_on_connection(
                conn,
                CHANNEL_GRANT,
                {
                    "kind": "granted" if grant else "revoked",
                    "session_id": sid,
                    "agent": session["agent"],
                },
                originator=actor,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return {"session_id": sid, "granted": grant, "requested": requested, "changed": changed}


#: Longest decline reason kept.
MAX_DECLINE_REASON = 500


def set_decline(session_id: str, *, actor: str, reason: str | None) -> dict[str, Any] | None:
    """The operator declines one session's wake request; ``None`` if the session is unknown.

    The caller has already verified ``actor`` is the human operator (the
    operator-only wake-decline route). Records who, when and why, revokes any
    grant and invalidates outstanding offers, and records a ``CHANNEL_GRANT``
    ``declined`` event, all in one commit. Declining again keeps the first record.
    """
    sid = str(session_id or "").strip()
    why = (reason or "").strip()[:MAX_DECLINE_REASON] or None
    conn = storage.connect()
    published = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT agent FROM agent_sessions WHERE id=? AND org_id IS ?",
            (sid, edition.tenant_org_id()),
        ).fetchone()
        if session is None:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT opted_in, declined_at FROM session_wake WHERE session_id=?", (sid,)
        ).fetchone()
        changed = not (row and row["declined_at"])
        if changed:
            now = storage.now()
            conn.execute(
                "UPDATE session_wake_offers SET invalidated_at=? WHERE session_id=? "
                "AND recorded_at IS NULL AND invalidated_at IS NULL",
                (datetime.now(UTC).isoformat(), sid),
            )
            conn.execute(
                "INSERT INTO session_wake (session_id, declined_at, declined_by, decline_reason, "
                "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "declined_at=excluded.declined_at, declined_by=excluded.declined_by, "
                "decline_reason=excluded.decline_reason, granted_at=NULL, granted_by=NULL, "
                "offered_at=NULL, offered_seq=NULL, offered_id=NULL, "
                "updated_at=excluded.updated_at",
                (sid, now, actor, why, now),
            )
            _, _, published = events.record_event_once_on_connection(
                conn,
                CHANNEL_GRANT,
                {"kind": "declined", "session_id": sid, "agent": session["agent"]},
                originator=actor,
            )
        state = conn.execute(
            "SELECT opted_in, granted_at, declined_at, declined_by, decline_reason "
            "FROM session_wake WHERE session_id=?",
            (sid,),
        ).fetchone()
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return {
        "session_id": sid,
        "requested": bool(state["opted_in"]),
        "granted": bool(state["granted_at"]),
        "declined": True,
        "declined_at": state["declined_at"],
        "declined_by": state["declined_by"],
        "decline_reason": state["decline_reason"],
        "changed": changed,
    }


def wake_states(session_ids: list[str]) -> dict[str, dict[str, bool]]:
    """``{session_id: {requested, granted, declined}}`` for sessions with a wake row."""
    if not session_ids:
        return {}
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT session_id, opted_in, granted_at, declined_at FROM session_wake "
            f"WHERE session_id IN ({','.join('?' for _ in session_ids)})",
            list(session_ids),
        ).fetchall()
    finally:
        conn.close()
    return {
        str(r["session_id"]): {
            "requested": bool(r["opted_in"]),
            "granted": bool(r["granted_at"]),
            "declined": bool(r["declined_at"]),
        }
        for r in rows
    }


# --- gates ----------------------------------------------------------------------


def _read_since(conn: sqlite3.Connection, agent: str, session_id: str, since: str) -> bool:
    """Did this session (or its label) read any of its mail after ``since``?"""
    row = conn.execute(
        "SELECT 1 FROM message_index i LEFT JOIN message_reads r "
        f"ON {_RECEIPT_JOIN} WHERE i.recipient = ? AND i.org_id IS ? "
        "AND (i.target_session IS NULL OR i.target_session = ?) "
        "AND COALESCE(i.read_at, r.read_at) > ? LIMIT 1",
        (session_id, agent, edition.tenant_org_id(), session_id, since),
    ).fetchone()
    # ponytail: scans the label's mail history without a read_at index; only runs
    # while a breaker count is open. Add an index on read_at if it ever shows up.
    return row is not None


def _trigger(
    conn: sqlite3.Connection,
    agent: str,
    session_id: str,
    after_seq: int,
    through_seq: int = 9223372036854775807,
):
    return conn.execute(
        "SELECT COUNT(*) AS n, MAX(i.event_seq) AS newest, MIN(i.created_at) AS oldest_at, "
        "MAX(i.created_at) AS newest_at FROM message_index i "
        f"LEFT JOIN message_reads r ON {_RECEIPT_JOIN} WHERE {_UNREAD_MAILBOX} "
        f"AND (i.act IN ({','.join('?' for _ in WAKE_ACTS)}) OR i.expects_reply = 1) "
        "AND i.event_seq > ? AND i.event_seq <= ?",
        (
            session_id,
            agent,
            session_id,
            edition.tenant_org_id(),
            *sorted(WAKE_ACTS),
            after_seq,
            through_seq,
        ),
    ).fetchone()


def _budget_hold(conn, row, now) -> str | None:
    """Known delivery and conservative one-shot attempt budgets stay separate."""
    last = max(
        (t for t in (_ts(row["last_wake_at"]), _ts(row["last_attempt_at"])) if t is not None),
        default=None,
    )
    unacked = max(int(row["unacked_wakes"] or 0), int(row["unacked_attempts"] or 0))
    read_after = _ts(row["last_attempt_at"]) or _ts(row["last_wake_at"])
    if (
        unacked
        and read_after is not None
        and _read_since(conn, row["agent"], row["session_id"], read_after.isoformat())
    ):
        conn.execute(
            "UPDATE session_wake SET unacked_wakes=0, unacked_attempts=0, paused_reason=NULL WHERE session_id=?",
            (row["session_id"],),
        )
        unacked = 0
    if unacked >= BREAKER_AFTER:
        return "breaker"
    if last is not None and (now - last).total_seconds() < FLOOR_S:
        return "floor"
    for start, count in (
        ("hour_started_at", "wakes_last_hour"),
        ("attempt_hour_started_at", "attempts_last_hour"),
    ):
        hour = _ago(now, row[start])
        if hour is not None and hour < 3600 and int(row[count] or 0) >= HOURLY_CAP:
            return "hourly_cap"
    return None


def _reserve_attempt(conn, row, offer_id, now, *, identity):
    """One server-time reservation per durable generation, before any submission."""
    stamp = now.astimezone(UTC).isoformat()
    changed = conn.execute(
        "UPDATE session_wake_offers SET attempted_at=? WHERE offer_id=? AND attempted_at IS NULL",
        (stamp, offer_id),
    ).rowcount
    if not changed:
        return None
    hour = _ago(now, row["attempt_hour_started_at"])
    fresh_hour = hour is None or hour >= 3600
    unacked = int(row["unacked_attempts"] or 0) + 1
    conn.execute(
        "UPDATE session_wake SET last_attempt_at=?, attempt_hour_started_at=?, attempts_last_hour=?, "
        "unacked_attempts=?, paused_reason=? WHERE session_id=?",
        (
            stamp,
            stamp if fresh_hour else row["attempt_hour_started_at"],
            1 if fresh_hour else int(row["attempts_last_hour"] or 0) + 1,
            unacked,
            PAUSED_NO_READ if unacked >= BREAKER_AFTER else row["paused_reason"],
            row["session_id"],
        ),
    )
    _, _, published = events.record_event_once_on_connection(
        conn,
        CHANNEL_ATTEMPT,
        {"kind": "reserved", "session_id": row["session_id"], "offer_id": offer_id},
        originator=identity,
    )
    return published


def _gate(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> tuple[str, Any]:
    """``("due", (unread, newest_seq))`` or ``("hold", reason)``. May write breaker state."""
    sid, agent = row["session_id"], row["agent"]
    offered = _ago(now, row["offered_at"])
    if offered is not None and offered < OFFER_LEASE_S:
        return "hold", "offer_outstanding"
    retry = _ts(row["retry_at"])
    if retry is not None and now < retry:
        return "hold", "retry_wait"
    hold = _budget_hold(conn, row, now)
    if hold is not None:
        return "hold", hold
    mail = _trigger(conn, agent, sid, int(row["last_wake_seq"] or 0))
    if not mail["n"]:
        return "hold", "nothing_to_wake"
    quiet = _ago(now, mail["newest_at"]) or 0.0
    held = _ago(now, mail["oldest_at"]) or 0.0
    if quiet < DEBOUNCE_QUIET_S and held < DEBOUNCE_MAX_HOLD_S:
        return "hold", "debounce"
    unread = unread_count(agent, conn, sid)
    if unread <= 0:
        return "hold", "nothing_to_wake"
    return "due", (unread, int(mail["newest"]))


def _candidates(conn: sqlite3.Connection, node: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT w.*, s.agent, s.external_session_id, s.host FROM session_wake w "
        "JOIN agent_sessions s ON s.id = w.session_id WHERE w.opted_in = 1 "
        "AND w.granted_at IS NOT NULL AND w.declined_at IS NULL "
        f"AND s.status IN ({','.join('?' for _ in _OPEN)}) AND s.org_id IS ? "
        "AND s.external_session_id IS NOT NULL",
        (*_OPEN, edition.tenant_org_id()),
    ).fetchall()
    key = host_key(node)
    return [r for r in rows if key and host_key(r["host"]) == key]


def note_live_sessions(
    node: Any,
    external_ids: list[str],
    *,
    identity: str,
    device_node: str | None = None,
) -> dict[str, int]:
    """Keep granted sessions open while ``node`` still sees their panes (task #2428).

    An idle session at its prompt runs no hook, so nothing heartbeats it and the
    reaper would close it, and wakes reach only open sessions. The node that
    would deliver the wake reports which native session ids still have a pane.
    Only a session that asked for wakes, was granted them and registered on that
    node's own host is touched. A session the reaper already closed comes back;
    one that ended any other way stays ended.
    """
    name = validate_node_name(node)
    reported = {str(e) for e in external_ids}
    kept = revived = 0
    published: list[dict[str, Any]] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        key = host_key(name)
        rows = conn.execute(
            "SELECT s.id, s.agent, s.status, s.exit_status, s.external_session_id, s.host "
            "FROM session_wake w JOIN agent_sessions s ON s.id = w.session_id "
            "WHERE w.opted_in = 1 AND w.granted_at IS NOT NULL AND w.declined_at IS NULL "
            "AND s.org_id IS ? AND s.external_session_id IS NOT NULL",
            (edition.tenant_org_id(),),
        ).fetchall()
        now = storage.now()
        for row in rows:
            if row["external_session_id"] not in reported or host_key(row["host"]) != key:
                continue
            if row["status"] in _OPEN:
                conn.execute(
                    "UPDATE agent_sessions SET last_heartbeat_at=? WHERE id=?", (now, row["id"])
                )
                kept += 1
            elif row["status"] == "abandoned" and row["exit_status"] == _REAPED_EXIT:
                conn.execute(
                    "UPDATE agent_sessions SET last_heartbeat_at=?, status='running', "
                    "ended_at=NULL, exit_status=NULL WHERE id=?",
                    (now, row["id"]),
                )
                revived += 1
                _, _, event = events.record_event_once_on_connection(
                    conn,
                    CHANNEL_REVIVED,
                    {"session_id": row["id"], "agent": row["agent"], "node": name},
                    originator=identity,
                )
                if event is not None:
                    published.append(event)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for event in published:
        events.publish(event)
    return {"kept_open": kept, "revived": revived}


def due_wakes(
    node: Any,
    *,
    identity: str,
    device_node: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Sessions on ``node``'s host that are due a wake now, marked as offered.

    ``node`` must be registered to ``identity`` (and its device, once bound). Its
    name is the host: a node sees only sessions whose registered host matches.
    """
    name = validate_node_name(node)
    ref = now or datetime.now(UTC)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        out: list[dict[str, Any]] = []
        for row in _candidates(conn, name):
            verdict, value = _gate(conn, row, ref)
            if verdict == "hold":
                if value == "breaker" and row["paused_reason"] != PAUSED_NO_READ:
                    conn.execute(
                        "UPDATE session_wake SET paused_reason=? WHERE session_id=?",
                        (PAUSED_NO_READ, row["session_id"]),
                    )
                continue
            unread, newest_seq = value
            offer_id = uuid.uuid4().hex
            offered_at = ref.astimezone(UTC).isoformat()
            expires_at = (ref + timedelta(seconds=OFFER_LEASE_S)).timestamp()
            conn.execute(
                "INSERT INTO session_wake_offers "
                "(offer_id,session_id,offered_seq,offered_at,expires_at) VALUES (?,?,?,?,?)",
                (offer_id, row["session_id"], newest_seq, offered_at, expires_at),
            )
            conn.execute(
                "UPDATE session_wake SET offered_at=?, offered_seq=?, offered_id=?, updated_at=? "
                "WHERE session_id=?",
                (offered_at, newest_seq, offer_id, _iso(ref), row["session_id"]),
            )
            out.append(
                {
                    "session_id": str(row["session_id"]),
                    "external_session_id": str(row["external_session_id"]),
                    "unread": int(unread),
                    "newest_seq": int(newest_seq),
                    "expires_at": expires_at,
                    "offer_id": offer_id,
                }
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return out


def _live_offer(row, seq, ref, expires_at=None, offer_id=None) -> float:
    offered = _ts(row["offered_at"])
    if (
        offered is None
        or seq != row["offered_seq"]
        or offer_id != row["offered_id"]
        or seq <= int(row["last_wake_seq"] or 0)
        or not row["opted_in"]
        or row["granted_at"] is None
        or row["status"] not in _OPEN
    ):
        return 0.0
    deadline = (offered + timedelta(seconds=OFFER_LEASE_S)).timestamp()
    if expires_at is not None and expires_at != deadline:
        return 0.0
    return max(0.0, deadline - ref.timestamp())


def check_offer(
    node: Any,
    session_id: Any,
    *,
    seq: Any,
    expires_at: Any,
    offer_id: Any,
    identity: str,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically admit one prompt attempt and reserve its budget before typing.

    The random offer id identifies its generation: re-offering the same unread
    mail must not make a delayed old offer valid again.
    """
    name = validate_node_name(node)
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 9223372036854775807:
        raise NodeError("seq must be a non-negative integer")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not 0 < expires_at < 253402300800
    ):
        raise NodeError("expires_at must be a finite timestamp")
    if not isinstance(offer_id, str) or not re.fullmatch(r"[a-f0-9]{32}", offer_id):
        raise NodeError("offer_id must identify an offered generation")
    sid = str(session_id or "").strip()
    conn = storage.connect()
    published = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        row = conn.execute(
            "SELECT w.*, s.host, s.status, s.agent FROM session_wake w "
            "JOIN agent_sessions s ON s.id=w.session_id "
            "WHERE w.session_id=? AND s.org_id IS ?",
            (sid, edition.tenant_org_id()),
        ).fetchone()
        if row is None or host_key(row["host"]) != host_key(name):
            raise NodeError(f"session {sid!r} is not a wake session on node {name!r}", status=404)
        ref = now or datetime.now(UTC)
        remaining = _live_offer(row, seq, ref, expires_at, offer_id)
        offer = conn.execute(
            "SELECT attempted_at FROM session_wake_offers WHERE offer_id=? AND session_id=?",
            (offer_id, sid),
        ).fetchone()
        if remaining and (offer is None or offer["attempted_at"] is not None):
            remaining = 0.0
        if (
            remaining
            and not _trigger(conn, row["agent"], sid, int(row["last_wake_seq"] or 0), seq)["n"]
        ):
            remaining = 0.0
        if remaining and _budget_hold(conn, row, ref) is not None:
            remaining = 0.0
        if remaining:
            fresh = dict(
                conn.execute("SELECT * FROM session_wake WHERE session_id=?", (sid,)).fetchone()
            )
            admission_ref = now or datetime.now(UTC)
            remaining = _live_offer(row, seq, admission_ref, expires_at, offer_id)
            if remaining:
                published = _reserve_attempt(
                    conn, fresh, offer_id, admission_ref, identity=identity
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return {"session_id": sid, "valid": remaining > 0, "remaining_s": remaining}


def record_receipt(
    node: Any,
    session_id: Any,
    outcome: Any,
    reason: Any = None,
    *,
    seq: Any,
    expires_at: Any,
    offer_id: Any,
    delivered_at: Any = None,
    identity: str,
    device_node: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Account one trusted node receipt against its durable offered generation.

    A delivered result can be replayed after reconnect only if its reported
    completion was inside that known lease and before consent invalidation.
    It advances the mail cursor and floor once, without clearing another offer.
    Failed/skipped results can affect only the current live generation.
    """
    name = validate_node_name(node)
    if outcome not in OUTCOMES:
        raise NodeError(f"outcome must be one of {sorted(OUTCOMES)}")
    if reason is not None and (not isinstance(reason, str) or not _REASON_RE.fullmatch(reason)):
        raise NodeError("reason must be 1-64 chars of [a-z0-9_:.-]")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 9223372036854775807:
        raise NodeError("seq must be the offer's newest_seq, a non-negative integer")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not 0 < expires_at < 253402300800
    ):
        raise NodeError("expires_at must be the offer deadline")
    if not isinstance(offer_id, str) or not re.fullmatch(r"[a-f0-9]{32}", offer_id):
        raise NodeError("offer_id must identify an offered generation")
    if outcome == "delivered" and (
        isinstance(delivered_at, bool)
        or not isinstance(delivered_at, (int, float))
        or not 0 < delivered_at < 253402300800
    ):
        raise NodeError("delivered_at must be the prompt completion timestamp")
    sid = str(session_id or "").strip()
    ref = now or datetime.now(UTC)
    conn = storage.connect()
    published = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        row = conn.execute(
            "SELECT w.*, s.host, s.status FROM session_wake w "
            "JOIN agent_sessions s ON s.id=w.session_id WHERE w.session_id=? AND s.org_id IS ?",
            (sid, edition.tenant_org_id()),
        ).fetchone()
        if row is None or host_key(row["host"]) != host_key(name):
            raise NodeError(f"session {sid!r} is not a wake session on node {name!r}", status=404)
        offer = conn.execute(
            "SELECT * FROM session_wake_offers WHERE offer_id=? AND session_id=?",
            (offer_id, sid),
        ).fetchone()
        valid = bool(
            offer
            and offer["recorded_at"] is None
            and offer["offered_seq"] == seq
            and offer["expires_at"] == expires_at
        )
        if valid and outcome == "delivered":
            invalidated = _ts(offer["invalidated_at"])
            valid = (
                offer["attempted_at"] is not None
                and _ts(offer["attempted_at"]).timestamp() - RECEIPT_CLOCK_SKEW_S
                <= delivered_at
                < expires_at
                and delivered_at <= ref.timestamp() + RECEIPT_CLOCK_SKEW_S
                and (invalidated is None or delivered_at < invalidated.timestamp())
            )
        elif valid:
            valid = (
                bool(offer["attempted_at"])
                if reason == "delivery_uncertain"
                else bool(_live_offer(row, seq, ref, expires_at, offer_id))
            )
        if not valid:
            conn.rollback()
            return {"session_id": sid, "recorded": False, "duplicate": True}
        # A delayed old result must never move the floor/cursor backwards.
        previous = _ts(row["last_wake_at"])
        stamp = _iso(max(ref, previous) if previous is not None else ref)
        same_generation = row["offered_id"] == offer_id
        if outcome == "delivered":
            hour = _ago(ref, row["hour_started_at"])
            fresh_hour = hour is None or hour >= 3600
            unacked = int(row["unacked_wakes"] or 0) + 1
            conn.execute(
                "UPDATE session_wake SET last_wake_at=?, last_wake_seq=?, "
                "hour_started_at=?, wakes_last_hour=?, unacked_wakes=?, paused_reason=?, "
                "offered_at=CASE WHEN ? THEN NULL ELSE offered_at END, "
                "offered_seq=CASE WHEN ? THEN NULL ELSE offered_seq END, "
                "offered_id=CASE WHEN ? THEN NULL ELSE offered_id END, "
                "retry_at=NULL, attempts=0, last_outcome=?, last_reason=?, updated_at=? "
                "WHERE session_id=?",
                (
                    stamp,
                    max(seq, int(row["last_wake_seq"] or 0)),
                    stamp if fresh_hour else row["hour_started_at"],
                    1 if fresh_hour else int(row["wakes_last_hour"] or 0) + 1,
                    unacked,
                    PAUSED_NO_READ if unacked >= BREAKER_AFTER else None,
                    same_generation,
                    same_generation,
                    same_generation,
                    outcome,
                    reason,
                    stamp,
                    sid,
                ),
            )
            _, _, published = events.record_event_once_on_connection(
                conn,
                CHANNEL_WAKE,
                {
                    "kind": "delivered",
                    "session_id": sid,
                    "node": name,
                    "seq": seq,
                    "offer_id": offer_id,
                },
                originator=identity,
            )
        elif reason == "delivery_uncertain":
            # The admission already consumed the conservative budget. Preserve
            # truthful delivery counters and any different replacement offer.
            conn.execute(
                "UPDATE session_wake SET last_wake_seq=?, "
                "offered_at=CASE WHEN ? THEN NULL ELSE offered_at END, "
                "offered_seq=CASE WHEN ? THEN NULL ELSE offered_seq END, "
                "offered_id=CASE WHEN ? THEN NULL ELSE offered_id END, "
                "last_outcome=?, last_reason=?, updated_at=? WHERE session_id=?",
                (
                    max(seq, int(row["last_wake_seq"] or 0)),
                    same_generation,
                    same_generation,
                    same_generation,
                    outcome,
                    reason,
                    stamp,
                    sid,
                ),
            )
        else:
            attempts = int(row["attempts"] or 0) + 1
            give_up = reason in FINAL_SKIPS or attempts >= MAX_ATTEMPTS
            conn.execute(
                "UPDATE session_wake SET offered_at=NULL, offered_seq=NULL, offered_id=NULL, "
                "retry_at=?, attempts=?, last_wake_seq=?, last_outcome=?, last_reason=?, updated_at=? "
                "WHERE session_id=?",
                (
                    None if give_up else _iso(ref + timedelta(seconds=RETRY_S)),
                    0 if give_up else attempts,
                    max(seq, int(row["last_wake_seq"] or 0)) if give_up else row["last_wake_seq"],
                    outcome,
                    reason,
                    stamp,
                    sid,
                ),
            )
        conn.execute(
            "UPDATE session_wake_offers SET recorded_at=?, outcome=?, delivered_at=?, reason=? "
            "WHERE offer_id=?",
            (stamp, outcome, delivered_at if outcome == "delivered" else None, reason, offer_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return {"session_id": sid, "recorded": True, "duplicate": False, "outcome": outcome}


# --- doctor ---------------------------------------------------------------------

#: Session ids named in the doctor row; the count covers the rest.
DOCTOR_AWAITING_SHOWN = 5


def doctor_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the ``agent_wake`` doctor row over open sessions that requested wakes.

    ``granted`` of them can be woken; ``awaiting`` lists (up to
    ``DOCTOR_AWAITING_SHOWN``) the ids still waiting on an operator grant.
    """
    rows = conn.execute(
        "SELECT w.session_id, w.granted_at, w.paused_reason, EXISTS ("
        "SELECT 1 FROM session_wake_offers o WHERE o.session_id = w.session_id "
        "AND o.outcome = 'delivered') AS woken "
        "FROM session_wake w JOIN agent_sessions s ON s.id = w.session_id "
        f"WHERE w.opted_in = 1 AND s.status IN ({','.join('?' for _ in _OPEN)}) "
        "AND s.org_id IS ? ORDER BY w.requested_at, w.session_id",
        (*_OPEN, edition.tenant_org_id()),
    ).fetchall()
    granted = [r for r in rows if r["granted_at"]]
    awaiting = [str(r["session_id"]) for r in rows if not r["granted_at"]]
    return {
        "opted_in": len(rows),
        "granted": len(granted),
        "awaiting": len(awaiting),
        "awaiting_ids": awaiting[:DOCTOR_AWAITING_SHOWN],
        "paused": sum(1 for r in granted if r["paused_reason"] is not None),
        "woken": sum(1 for r in granted if r["woken"]),
    }
