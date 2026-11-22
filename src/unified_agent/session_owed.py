"""What a session still owes when it ends (task #2389). Report mode.

Three classes, none of which blocks anything:

* ``unanswered``: a reply the session took on: ``expects_reply`` mail that
  arrived during the session, was read by it or addressed to it, and has no reply;
* ``next_moves``: unfinished tasks whose stored next owner is this agent and that
  this agent does not hold;
* ``claims``: tasks this agent holds in ``doing`` that would have no action path
  once its lease stopped being renewed.

Claims and next moves belong to the agent label, which several sessions can
share, so each report counts the label's other open sessions. That count is how
false positives are measured before anything enforces this.

Almost no real session calls ``session_end``; the reaper marks them abandoned.
:func:`record_recently_ended` covers those from the hygiene sweep. One event per
session (``agent.session.owed``) holds ids and counts, never message bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from . import edition, events, storage, task_liveness
from .messaging import index as message_index

logger = logging.getLogger(__name__)

CHANNEL = "agent.session.owed"
TURN_CHANNEL = "agent.session.owed.turn"
LIST_CAP = 20
SWEEP_WINDOW = timedelta(hours=6)
SWEEP_LIMIT = 200
SUMMARY_WINDOW = timedelta(days=7)
_EPOCH = "2000-01-01T00:00:00+00:00"
_MAIL_SCAN = 200
_OPEN = ("starting", "running", "waiting")


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _unanswered(conn: sqlite3.Connection, session: sqlite3.Row) -> list[dict[str, Any]]:
    sid = session["id"]
    rows = message_index.query(
        conn,
        recipient=session["agent"],
        waiting_on_reply=True,
        mailbox=(session["agent"], sid),
        session_id=sid,
        limit=_MAIL_SCAN,
    )
    return [
        {
            "message_id": r["message_id"],
            "from_agent": r.get("from_agent"),
            "act": r.get("act"),
            "task_id": r.get("task_id"),
        }
        for r in rows
        if (r.get("created_at") or "") >= (session["started_at"] or "")
        and (r.get("to_session") == sid or r.get("read_at"))
    ]


def _next_moves(conn: sqlite3.Connection, session: sqlite3.Row) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE next_owner=? AND org_id IS ? "
        "AND status IN ('open','doing','waiting_review','blocked') AND archived_at IS NULL "
        "AND (claimed_by IS NULL OR claimed_by != ?) ORDER BY id",
        (session["agent"], session["org_id"], session["agent"]),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"]} for r in rows]


def _claims(conn: sqlite3.Connection, session: sqlite3.Row, now: datetime) -> list[dict[str, Any]]:
    tasks = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM tasks WHERE claimed_by=? AND status='doing' AND org_id IS ? "
            "AND archived_at IS NULL ORDER BY id",
            (session["agent"], session["org_id"]),
        )
    ]
    if not tasks:
        return []
    context = task_liveness.gather_context(conn, [t["id"] for t in tasks], now=now)
    # Ask what would still move the task if nobody renewed this lease.
    return [
        {"id": t["id"], "title": t["title"]}
        for t in tasks
        if task_liveness.action_path(
            {**t, "lock_expires_at": _EPOCH}, now=now, context=context
        ).base
        == task_liveness.NONE
    ]


def owed_by(
    conn: sqlite3.Connection, session: sqlite3.Row, now: datetime | None = None
) -> dict[str, Any]:
    """The report for one session row. Reads only."""
    reference = now or datetime.now(UTC)
    unanswered = _unanswered(conn, session)
    next_moves = _next_moves(conn, session)
    claims = _claims(conn, session, reference)
    others = conn.execute(
        f"SELECT COUNT(*) FROM agent_sessions WHERE agent=? AND id != ? AND org_id IS ? "
        f"AND status IN ({','.join('?' for _ in _OPEN)})",
        (session["agent"], session["id"], session["org_id"], *_OPEN),
    ).fetchone()[0]
    return {
        "unanswered": unanswered[:LIST_CAP],
        "next_moves": next_moves[:LIST_CAP],
        "claims": claims[:LIST_CAP],
        "counts": {
            "unanswered": len(unanswered),
            "next_moves": len(next_moves),
            "claims": len(claims),
        },
        "count": len(unanswered) + len(next_moves) + len(claims),
        "other_open_sessions": int(others),
        "report_only": True,
    }


def _session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, agent, status, started_at, org_id FROM agent_sessions WHERE id=?",
        (session_id,),
    ).fetchone()


def _payload(session: sqlite3.Row, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "agent": session["agent"],
        "status": session["status"],
        "counts": report["counts"],
        "count": report["count"],
        "other_open_sessions": report["other_open_sessions"],
        "message_ids": [m["message_id"] for m in report["unanswered"]],
        "task_ids": sorted({t["id"] for t in (*report["next_moves"], *report["claims"])}),
    }


def _record(session: sqlite3.Row, report: dict[str, Any]) -> bool:
    return events.record_event_once(
        CHANNEL,
        _payload(session, report),
        originator=session["agent"],
        dedupe_key=f"session-owed:{session['id']}",
    )[1]


def check_turn(agent: str, session_id: str) -> dict[str, Any] | None:
    """The report at a turn end, for the caller's own session; ``None`` if it is not theirs.

    Records one event per distinct set of owed items, so a session sitting on the
    same debt across many turns is counted once.
    """
    with closing(storage.connect()) as conn:
        session = _session(conn, session_id)
        if (
            session is None
            or session["agent"] != agent
            or session["org_id"] != edition.tenant_org_id()
        ):
            return None
        report = owed_by(conn, session)
    if report["count"]:
        payload = _payload(session, report)
        items = json.dumps([payload["message_ids"], payload["task_ids"]])
        digest = hashlib.sha256(items.encode("utf-8")).hexdigest()[:16]
        events.record_event_once(
            TURN_CHANNEL,
            payload,
            originator=agent,
            dedupe_key=f"session-owed-turn:{session_id}:{digest}",
        )
    return report


def report_session(session_id: str) -> dict[str, Any] | None:
    """Compute and record one ended session's report. Never raises."""
    try:
        with closing(storage.connect()) as conn:
            session = _session(conn, session_id)
            if session is None:
                return None
            report = owed_by(conn, session)
            _record(session, report)
            return report
    except Exception as exc:  # noqa: BLE001 - a report must never stop a session closing
        logger.warning("session_owed: report failed: %s", type(exc).__name__)
        return None


def record_recently_ended(now: datetime | None = None) -> int:
    """Report sessions that ended recently, however they ended. Returns how many are new.

    The event's dedupe key makes a repeat sweep a no-op. Scoped to this process's
    tenant, because the mailbox read is.
    """
    reference = now or datetime.now(UTC)
    with closing(storage.connect()) as conn:
        ended = conn.execute(
            "SELECT id, agent, status, started_at, org_id FROM agent_sessions "
            "WHERE status IN ('completed','failed','abandoned') AND ended_at >= ? AND org_id IS ? "
            "ORDER BY ended_at DESC LIMIT ?",
            (_iso(reference - SWEEP_WINDOW), edition.tenant_org_id(), SWEEP_LIMIT),
        ).fetchall()
        return sum(_record(row, owed_by(conn, row, reference)) for row in ended)


def summary(now: datetime | None = None) -> dict[str, int]:
    """Measurement over the last week. ``shared_label`` is the likely false-positive pool."""
    reference = now or datetime.now(UTC)
    totals = {"sessions": 0, "with_owed": 0, "items": 0, "shared_label": 0, "turn_sets": 0}
    with closing(storage.connect()) as conn:
        totals["turn_sets"] = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE channel=? AND created_at >= ? AND org_id IS ?",
            (TURN_CHANNEL, _iso(reference - SUMMARY_WINDOW), edition.tenant_org_id()),
        ).fetchone()[0]
        for row in conn.execute(
            "SELECT payload FROM event_log WHERE channel=? AND created_at >= ? AND org_id IS ?",
            (CHANNEL, _iso(reference - SUMMARY_WINDOW), edition.tenant_org_id()),
        ):
            payload = json.loads(row["payload"] or "{}")
            count = int(payload.get("count") or 0)
            totals["sessions"] += 1
            totals["items"] += count
            if count:
                totals["with_owed"] += 1
                if payload.get("other_open_sessions"):
                    totals["shared_label"] += 1
    return totals
