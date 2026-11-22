"""One list of everything waiting on the human operator (task #2411).

A read model over stores that already exist: no new table and no new tool. Each
row names one thing and the existing place to act on it.

The catalog is closed. Every source is a :func:`producer`, every kind is named in
:data:`DECLARED`, and :func:`payload` refuses to compose when the two differ, so
a new source cannot be added without being listed, and a listed one cannot
quietly stop being read. A producer that fails to read is reported in ``errors``
and never reads as "nothing waiting".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from . import config, edition, review_holds, storage
from .messaging import index as message_index

DECLARED = (
    "approval",
    "operator_task",
    "red_tier_hold",
    "held_pr",
    "operator_reply",
    "wake_grant",
)
LIMIT_PER_KIND = 100

_UNFINISHED = "status IN ('open','doing','waiting_review','blocked') AND archived_at IS NULL"
_RED_TIER = review_holds.RED_TIER_LIKE
_Row = dict[str, Any]
_PRODUCERS: dict[str, Callable[[sqlite3.Connection], list[_Row]]] = {}


class CatalogError(RuntimeError):
    """The producers and the declared kinds disagree."""


def producer(kind: str) -> Callable:
    def register(read: Callable[[sqlite3.Connection], list[_Row]]) -> Callable:
        _PRODUCERS[kind] = read
        return read

    return register


def _row(kind: str, subject: Any, title: str, at: Any, pane: str, **params: Any) -> _Row:
    return {
        "kind": kind,
        "subject": str(subject),
        "title": title,
        "since": at,
        "act": {"pane": pane, "params": {k: str(v) for k, v in params.items()}},
    }


def _tenant() -> str | None:
    return edition.tenant_org_id()


@producer("approval")
def _approvals(conn: sqlite3.Connection) -> list[_Row]:
    rows = conn.execute(
        "SELECT id, tool, created_at FROM approvals WHERE status='pending' AND org_id IS ? "
        "ORDER BY id LIMIT ?",
        (_tenant(), LIMIT_PER_KIND),
    ).fetchall()
    return [
        _row(
            "approval",
            r["id"],
            f"{r['tool']} is waiting for approval",
            r["created_at"],
            "approvals",
            approval=r["id"],
        )
        for r in rows
    ]


def _task_rows(conn: sqlite3.Connection, kind: str, where: str, *params: Any) -> list[_Row]:
    rows = conn.execute(
        f"SELECT id, title, updated_at FROM tasks WHERE {_UNFINISHED} AND org_id IS ? AND {where} "
        "ORDER BY id LIMIT ?",
        (_tenant(), *params, LIMIT_PER_KIND),
    ).fetchall()
    return [_row(kind, r["id"], r["title"], r["updated_at"], "tasks", task=r["id"]) for r in rows]


@producer("operator_task")
def _operator_tasks(conn: sqlite3.Connection) -> list[_Row]:
    return _task_rows(conn, "operator_task", "lane = 'operator-only'")


def _undecided_holds(conn: sqlite3.Connection, kind: str, where: str, *params: Any) -> list[_Row]:
    """Held tasks whose current hold has no operator decision (task #2514).

    A release or keep recorded through ``review_holds`` answers the hold marker it
    was made on; a new marker is a new hold and is listed again.
    """
    rows = conn.execute(
        f"SELECT id, title, updated_at, notes FROM tasks WHERE {_UNFINISHED} AND org_id IS ? "
        f"AND {where} ORDER BY id",
        (_tenant(), *params),
    ).fetchall()
    out: list[_Row] = []
    for r in rows:
        if review_holds.decided(conn, r["id"], r["notes"]) is not None:
            continue
        out.append(_row(kind, r["id"], r["title"], r["updated_at"], "tasks", task=r["id"]))
        if len(out) >= LIMIT_PER_KIND:
            break
    return out


@producer("red_tier_hold")
def _red_tier_holds(conn: sqlite3.Connection) -> list[_Row]:
    return _undecided_holds(conn, "red_tier_hold", "notes LIKE ?", _RED_TIER)


@producer("held_pr")
def _held_prs(conn: sqlite3.Connection) -> list[_Row]:
    return _undecided_holds(
        conn,
        "held_pr",
        "(notes LIKE ? OR notes LIKE ?) AND notes NOT LIKE ?",
        review_holds.HUMAN_MERGE_LIKE,
        review_holds.HELD_LIKE,
        _RED_TIER,
    )


@producer("operator_reply")
def _operator_replies(conn: sqlite3.Connection) -> list[_Row]:
    rows = message_index.query(
        conn,
        recipient=config.operator_label(),
        waiting_on_reply=True,
        limit=LIMIT_PER_KIND,
    )
    # The sender and the act only: a body is another agent's text, read in Messages.
    return [
        _row(
            "operator_reply",
            r["message_id"],
            f"{r.get('from_agent') or r.get('sender')} is waiting for your reply ({r.get('act')})",
            r.get("created_at"),
            "messages",
            conversation=r.get("conversation_id"),
        )
        for r in rows
    ]


@producer("wake_grant")
def _wake_grants(conn: sqlite3.Connection) -> list[_Row]:
    rows = conn.execute(
        "SELECT w.session_id, w.requested_at, s.agent FROM session_wake w "
        "JOIN agent_sessions s ON s.id = w.session_id "
        "WHERE w.opted_in = 1 AND w.granted_at IS NULL AND w.declined_at IS NULL "
        "AND s.status IN ('starting','running','waiting') AND s.org_id IS ? "
        "ORDER BY w.requested_at LIMIT ?",
        (_tenant(), LIMIT_PER_KIND),
    ).fetchall()
    return [
        _row(
            "wake_grant",
            r["session_id"],
            f"{r['agent']} session asks to be woken",
            r["requested_at"],
            "sessions",
            session=r["session_id"],
        )
        for r in rows
    ]


def check_catalog() -> None:
    """Raise when a producer is undeclared or a declared kind has no producer."""
    undeclared = sorted(set(_PRODUCERS) - set(DECLARED))
    missing = sorted(set(DECLARED) - set(_PRODUCERS))
    if undeclared or missing:
        raise CatalogError(f"needs_operator catalog: undeclared {undeclared}, missing {missing}")


def payload() -> dict[str, Any]:
    """``{rows, counts, errors}``; every declared kind appears in ``counts``."""
    check_catalog()
    rows: list[_Row] = []
    counts = dict.fromkeys(DECLARED, 0)
    errors: list[str] = []
    with closing(storage.connect()) as conn:
        for kind in DECLARED:
            try:
                found = _PRODUCERS[kind](conn)
            except sqlite3.Error as exc:
                errors.append(f"{kind}: could not be read ({type(exc).__name__})")
                continue
            counts[kind] = len(found)
            rows.extend(found)
    return {"rows": rows, "counts": counts, "total": len(rows), "errors": errors}
