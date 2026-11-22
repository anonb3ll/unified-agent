"""Append-only conversation on a task (task #1423).

Task notes stay the specification (``append_note``). A comment is a different
object: an authored utterance, optionally threaded, with mentions and
references to a plan, a decision (memory note), or a handoff (delegation).
Posted comments are not editable and have no delete route. A thread snapshot
row bumps ``revision`` so a list reader and the resource subscription see
the append.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage, table_schemas
from .telemetry.redaction import contains_session_credential, redact_session_credentials

REF_KEYS = ("plan_id", "decision_id", "handoff_id")
_MENTION_CHANNEL = "comment.mentioned"
_MAX_BODY = 8000
log = logging.getLogger(__name__)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables (the DDL lives in ``table_schemas``)."""
    table_schemas.task_comments(conn)


def _author(author: str | None) -> str:
    identity = config.verified_agent_identity()
    if identity:
        return identity
    text = (author or "").strip()
    if not text:
        raise ToolError("author is required when no verified identity is present")
    return text


def _mentions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolError("mentions must be a list of agent labels")
    labels: list[str] = []
    for item in raw:
        label = str(item).strip()
        if not label or any(ch.isspace() for ch in label) or len(label) > 64:
            raise ToolError(f"invalid mention: {item!r}")
        if label not in labels:
            labels.append(label)
    return labels


def _refs(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("refs must be an object")
    unknown = set(raw) - set(REF_KEYS)
    if unknown:
        raise ToolError(f"unknown ref keys: {sorted(unknown)}")
    cleaned: dict[str, Any] = {}
    if "plan_id" in raw and raw["plan_id"] is not None:
        plan_id = str(raw["plan_id"]).strip()
        # A plan in another tenant is unknown, never confirmed (task #2042).
        row = conn.execute(
            "SELECT id FROM plans WHERE id=? AND org_id IS ?", (plan_id, edition.tenant_org_id())
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown plan: {plan_id}")
        cleaned["plan_id"] = plan_id
    if "decision_id" in raw and raw["decision_id"] is not None:
        decision_id = str(raw["decision_id"]).strip()
        # A decision note in another tenant is unknown, never confirmed (task #1762).
        row = conn.execute(
            "SELECT id FROM memory WHERE id=? AND org_id IS ?",
            (decision_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"unknown decision: {decision_id}")
        cleaned["decision_id"] = decision_id
    if "handoff_id" in raw and raw["handoff_id"] is not None:
        try:
            handoff_id = int(raw["handoff_id"])
        except (TypeError, ValueError) as exc:
            raise ToolError("handoff_id must be an integer delegation id") from exc
        row = conn.execute("SELECT id FROM delegations WHERE id=?", (handoff_id,)).fetchone()
        if row is None:
            raise ToolError(f"unknown handoff: {handoff_id}")
        cleaned["handoff_id"] = handoff_id
    return cleaned


def _public(row: sqlite3.Row) -> dict[str, Any]:
    record = {key: row[key] for key in row.keys()}
    record["mentions"] = json.loads(record.pop("mentions") or "[]")
    record["refs"] = json.loads(record.pop("refs") or "{}")
    record["participants"] = [record["author"], *record["mentions"]]
    return record


def _notify_mentions(
    *,
    task_id: int,
    comment_id: int,
    author: str,
    mentions: list[str],
    revision: int,
) -> list[str]:
    """Tell each mentioned agent, once, with a pointer and never the body (#2392).

    A mention used to be a stored list that woke nobody. The notice is an
    ordinary ``inform`` message from the author: task id, author, comment id and
    thread revision, so untrusted thread text is not copied into a mailbox. A
    mention of the author is skipped, the fan-out is capped by the same limit as
    an office send, and a failed notice never fails the comment: it is already
    committed.
    """
    from .messaging.send import send_message

    targets = [label for label in mentions if label != author]
    sent: list[str] = []
    for label in targets:
        try:
            send_message(
                act="inform",
                to_agent=label,
                body=(
                    f"Task #{task_id}: {author} mentioned you in a comment "
                    f"(comment {comment_id}, thread revision {revision}). "
                    f'Read it with task_comment action="list" task_id={task_id}.'
                ),
                from_agent=author,
                task_id=task_id,
                message_id=f"comment-mention-{comment_id}-{label}",
            )
        except Exception as exc:  # noqa: BLE001 - the comment is already committed
            log.warning(
                "mention notice to %s for comment %s refused: %s",
                label,
                comment_id,
                type(exc).__name__,
            )
            continue
        sent.append(label)
    if sent:
        events.record_event(
            _MENTION_CHANNEL,
            payload={"task_id": int(task_id), "comment_id": comment_id, "mentioned": sent},
            originator=author,
            notify=False,
        )
    return sent


def append_comment(
    task_id: int,
    body: str,
    parent_comment_id: int | None = None,
    mentions: list[str] | None = None,
    refs: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Append one comment. A repeated idempotency key returns the original row.

    A hub session credential (``uas_...``) is redacted from the body, with a
    warning in the result, and refused in the claimed author, a mention or the
    idempotency key, before anything is written, so an agent cannot paste its
    own credential into the thread (task #1893).
    """
    text = (body or "").strip()
    if not text:
        raise ToolError("body must not be empty")
    for field, value in (
        ("author", author),
        ("mentions", mentions),
        ("idempotency_key", idempotency_key),
    ):
        if value is not None and contains_session_credential(json.dumps(value, default=str)):
            raise ToolError(f"{field} must not carry a hub session credential (uas_...)")
    text, credentials_redacted = redact_session_credentials(text)
    if len(text) > _MAX_BODY:
        raise ToolError(f"body must be at most {_MAX_BODY} characters")
    who = _author(author)
    key = (idempotency_key or "").strip() or None
    if key is not None and len(key) > 200:
        raise ToolError("idempotency_key must be at most 200 characters")
    labels = _mentions(mentions)
    cap = config.message_max_recipients()
    if len(labels) > cap:
        raise ToolError(
            f"a comment may mention at most {cap} agents (got {len(labels)}); "
            "one comment is one notice per mention"
        )
    conn = storage.connect()
    try:
        # Another tenant's task is unknown, like a missing one (task #1814).
        task = conn.execute(
            "SELECT id FROM tasks WHERE id=? AND org_id IS ?",
            (int(task_id), edition.tenant_org_id()),
        ).fetchone()
        if task is None:
            raise ToolError(f"unknown task: {task_id}")
        if key is not None:
            prior = conn.execute(
                "SELECT * FROM task_comments WHERE task_id=? AND author=? AND idempotency_key=?",
                (int(task_id), who, key),
            ).fetchone()
            if prior is not None:
                return {"comment": _public(prior), "idempotent_replay": True}
        if parent_comment_id is not None:
            parent = conn.execute(
                "SELECT id, task_id FROM task_comments WHERE id=?",
                (int(parent_comment_id),),
            ).fetchone()
            if parent is None or int(parent["task_id"]) != int(task_id):
                raise ToolError("parent comment is not on this task")
        linked = _refs(conn, refs)
        now = storage.now()
        conn.execute(
            "INSERT INTO task_comment_threads (task_id, revision, updated_at) "
            "VALUES (?, 1, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "revision = revision + 1, updated_at = excluded.updated_at",
            (int(task_id), now),
        )
        revision = int(
            conn.execute(
                "SELECT revision FROM task_comment_threads WHERE task_id=?",
                (int(task_id),),
            ).fetchone()["revision"]
        )
        try:
            cur = conn.execute(
                "INSERT INTO task_comments (task_id, parent_comment_id, author, body, "
                "mentions, refs, idempotency_key, thread_revision, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    int(task_id),
                    int(parent_comment_id) if parent_comment_id is not None else None,
                    who,
                    text,
                    json.dumps(labels),
                    json.dumps(linked),
                    key,
                    revision,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            if key is not None:
                prior = conn.execute(
                    "SELECT * FROM task_comments WHERE task_id=? AND author=? AND idempotency_key=?",
                    (int(task_id), who, key),
                ).fetchone()
                if prior is not None:
                    return {"comment": _public(prior), "idempotent_replay": True}
            raise
        comment_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM task_comments WHERE id=?", (comment_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    events.record_event(
        "comment.appended",
        payload={
            "comment_id": comment_id,
            "task_id": int(task_id),
            "thread_revision": int(row["thread_revision"]),
        },
        originator=who,
        notify=False,
    )
    out: dict[str, Any] = {"comment": _public(row), "idempotent_replay": False}
    notified = _notify_mentions(
        task_id=int(task_id),
        comment_id=comment_id,
        author=who,
        mentions=labels,
        revision=int(row["thread_revision"]),
    )
    if notified:
        out["notified_mentions"] = notified
    if credentials_redacted:
        out["warning"] = (
            "a hub session credential (uas_...) was redacted from this comment; "
            "credentials never belong in a task thread"
        )
    return out


def list_comments(task_id: int) -> dict[str, Any]:
    conn = storage.connect()
    try:
        # Another tenant's task is unknown, like a missing one (task #1814).
        task = conn.execute(
            "SELECT id, notes FROM tasks WHERE id=? AND org_id IS ?",
            (int(task_id), edition.tenant_org_id()),
        ).fetchone()
        if task is None:
            raise ToolError(f"unknown task: {task_id}")
        rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id=? ORDER BY id",
            (int(task_id),),
        ).fetchall()
        snap = conn.execute(
            "SELECT revision, updated_at FROM task_comment_threads WHERE task_id=?",
            (int(task_id),),
        ).fetchone()
    finally:
        conn.close()
    return {
        "task_id": int(task_id),
        "revision": int(snap["revision"]) if snap else 0,
        "updated_at": snap["updated_at"] if snap else None,
        "comments": [_public(row) for row in rows],
        "notes_are_spec": True,
    }


def get_comment(comment_id: int) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        # A comment has no org_id of its own; it is visible only through a
        # task in the caller's tenant (task #1814).
        row = conn.execute(
            "SELECT c.* FROM task_comments AS c JOIN tasks AS t ON t.id = c.task_id "
            "WHERE c.id=? AND t.org_id IS ?",
            (int(comment_id), edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"comment": _public(row)}


def task_comment(action: str, params: dict | None = None) -> dict[str, Any]:
    """MCP write/read for the comment thread. There is no update or delete action."""
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    name = action.strip().lower()
    if name in {"update", "delete", "edit", "remove"}:
        raise ToolError("comments are append-only")
    body = params or {}
    if not isinstance(body, dict):
        raise ToolError("params must be an object")
    if name == "append":
        return append_comment(
            task_id=int(body["task_id"]),
            body=str(body.get("body") or ""),
            parent_comment_id=body.get("parent_comment_id"),
            mentions=body.get("mentions"),
            refs=body.get("refs"),
            idempotency_key=body.get("idempotency_key"),
            author=body.get("author"),
        )
    if name == "list":
        return list_comments(int(body["task_id"]))
    if name == "get":
        found = get_comment(int(body["comment_id"]))
        if found is None:
            raise ToolError(f"unknown comment: {body.get('comment_id')}")
        return found
    raise ToolError("action must be one of append, list, get")
