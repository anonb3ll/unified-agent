"""Hand an agent conversation off to a project task (task #1426).

A Control Center conversation is the existing mailbox thread (``message_send`` /
``message_list``, task #1864), not a new chat system. Handing it off creates a
task, or links an existing one, that *references* the conversation by id. The
transcript keeps one owner: no message body is copied into task notes or any
other table, so there is no second message store to drift.

The link is recorded twice, both append-only and both pointing rather than
copying: a ``[conversation: <id>]`` line on the task, and one
``agent.conversation.handoff`` event (deduplicated per conversation, task and
participant; the creating call has its own key, so a participant that creates
and later repeats the handoff leaves two).
The task line is the durable index the Messages pane lists from; the event is
the audit record and only adds who handed off and when. An operator may prune
events, and an event write can fail after the task write committed; neither
hides a link.

Rules:

* The caller is verified and can see the conversation (the ``message_list``
  scope); another tenant's task or thread behaves like a missing one.
* Only an id-safe conversation id is written into notes: a sender chooses the
  id, so anything that could forge a note marker (``]``, newlines) is refused.
* The task write is the registered ``task_add`` / ``task_update`` called with
  the arguments a direct MCP call would pass, so it is admitted exactly as that
  call is: agent access, a rate slot, the policy gate and the
  ``UA_APPROVAL_TOOLS`` gate. An approval for one is an approval for the other;
  a parked approval writes nothing.
* Creating is claimed atomically per tenant, conversation, title, project and
  priority, so retried and concurrent identical handoffs converge on one task
  and a handoff to another project creates that project's task. A retry
  after the first one finished returns that task; a concurrent attempt that
  loses the claim is told to retry, and its retry returns the same task.
* A created task records a ``messages`` completion-return origin (task #1993)
  in the same transaction as the task: the conversation and the verified
  requester. When the task reaches done, blocked or cancelled, a short status
  is posted into the conversation (``completion_return``). Linking an existing
  task records none: that task was asked for elsewhere.
* Over MCP the caller's profile must allow ``task_add`` (create) or
  ``task_update`` (link). The HTTP route enforces its declared ceiling itself,
  which also admits the verified human operator.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import completion_return, config, edition, events, profiles, storage
from . import conversations
from .schema import CONVERSATION_ID_RE, CONVERSATION_ID_RULE

HANDOFF_CHANNEL = "agent.conversation.handoff"
MAX_TITLE = 300
_CLAIM_TOOL = "conversation_handoff"
#: A pending claim older than this belongs to a crashed attempt and may be retaken.
_STALE_CLAIM = timedelta(minutes=5)
_ID_RE = CONVERSATION_ID_RE
#: The refusal a concurrent identical handoff gets while the winner is creating.
IN_PROGRESS = "the same handoff is already in progress"


def reference(conversation_id: str) -> str:
    """The task-note line that points at a conversation."""
    return f"[conversation: {conversation_id}] handed off from the agent conversation"


def _require(tool: str) -> None:
    if not profiles.tool_is_allowed_for(profiles.current(), tool):
        raise ToolError(f"your profile may not use {tool}, which a handoff needs")


def hand_off_conversation(
    conversation_id: str,
    *,
    task_id: int | None = None,
    title: str | None = None,
    project: str | None = None,
    priority: str = "med",
) -> dict[str, Any]:
    """Create or link a task for one visible conversation; never copy its messages.

    Pass ``task_id`` to link an existing task, or ``title`` (and optionally
    ``project`` and ``priority``) to create one. Repeating the same handoff is a
    no-op that returns the same task. Requires ``task_add`` (create) or
    ``task_update`` (link) in the caller's profile; the write then passes the
    same gates, approval included, as that direct call.
    """
    _require("task_update" if task_id is not None else "task_add")
    return _hand_off(
        conversation_id, task_id=task_id, title=title, project=project, priority=priority
    )


def hand_off_for_route(
    conversation_id: str,
    *,
    task_id: int | None = None,
    title: str | None = None,
    project: str | None = None,
    priority: str = "med",
) -> dict[str, Any]:
    """The HTTP route's entry: the route has already enforced its ceiling."""
    return _hand_off(
        conversation_id, task_id=task_id, title=title, project=project, priority=priority
    )


def _hand_off(
    conversation_id: str,
    *,
    task_id: int | None,
    title: str | None,
    project: str | None,
    priority: str,
) -> dict[str, Any]:
    actor = config.verified_agent_identity()
    if not actor:
        raise ToolError("handing off a conversation needs a verified identity")
    cid = str(conversation_id or "").strip()
    if not _ID_RE.fullmatch(cid):
        # The id is written into task notes; refuse anything that could forge a
        # marker there (for example "]" or a newline followed by "[plan: ...]").
        raise ToolError(CONVERSATION_ID_RULE)
    if (task_id is None) == (title is None or not str(title).strip()):
        raise ToolError("pass exactly one of task_id (link) or title (create)")
    # Same visibility as the Messages pane: "missing" and "not yours" read alike.
    if conversations.conversation_thread(cid, limit=1) is None:
        raise ToolError("conversation not found")

    line = reference(cid)
    if task_id is not None and not _visible_task(int(task_id)):
        # task_update finds its row by id alone; fence the link to the caller's
        # tenant here so another tenant's task reads exactly like a missing one.
        raise ToolError(f"no task with id {int(task_id)}")
    if task_id is not None:
        # A verbatim repeat of an appended line is suppressed, so relinking is
        # idempotent. Appending a note changes no status, so no worktree is made.
        tid = int(_task_write("update", id=int(task_id), append_note=line)["id"])
        created = False
    else:
        clean = str(title).strip()[:MAX_TITLE]
        tid, created = _create_once(
            cid, clean, line, project=project, priority=priority, requester=actor
        )

    # One event per participant (PR #1146 review): with a shared key a
    # concurrent identical handoff by another participant could win, and the
    # requester's own evidence, which authorizes its completion notices, was
    # never written. The creating call has a key of its own (task #2183): a
    # repeat by the same participant that reads the claim result before the
    # creator writes its event would otherwise take the key, so the record said
    # "not created" and, because the answer used to follow the event, so did
    # the creator's reply. A repeat writes nothing once the creator's event is
    # recorded, so only that race can leave a second event.
    key = f"conversation-handoff:{edition.tenant_org_id() or ''}:{cid}:{tid}:{actor}"
    try:
        events.record_event_once(
            HANDOFF_CHANNEL,
            payload={"conversation_id": cid, "task_id": tid, "created": created, "by": actor},
            originator=actor,
            dedupe_key=key + ":created" if created else key,
            precondition=None if created else _skip_if_created(key + ":created"),
        )
    except _CreatorEventRecorded:
        pass
    # ``created`` is settled by the claim alone: exactly one call holds it.
    return {
        "conversation_id": cid,
        "task_id": tid,
        "created": created,
        "reference": line,
    }


class _CreatorEventRecorded(Exception):
    """The creating call's handoff event exists; a repeat records nothing."""


def _skip_if_created(created_key: str) -> Callable[[Any], None]:
    """Precondition for a repeat's event, checked inside its write transaction."""

    def check(conn: Any) -> None:
        if conn.execute("SELECT 1 FROM event_log WHERE dedupe_key=?", (created_key,)).fetchone():
            raise _CreatorEventRecorded

    return check


def _task_write(action: str, **arguments: Any) -> dict[str, Any]:
    """Call the registered ``task_add`` / ``task_update``, as a direct MCP call does.

    The call goes through ``tool_surface`` to the same governed callable the
    MCP surface calls, so it passes the same admission: agent access, the
    caller's rate slot, the policy gate and the ``UA_APPROVAL_TOOLS`` gate
    (the pattern ``workflow._governed_invoke`` uses below the adapter layer).
    Every parameter is passed with its default, as the MCP adapter passes
    them, so the gated payload is the one a direct call with these arguments
    produces and an approval for either call is the same approval. Raises
    ``ApprovalPendingError`` when a gate parks the write and
    ``PolicyViolation`` when policy refuses it; nothing is written in either
    case.
    """
    from .. import tool_surface
    from ..tools import tasks

    tool = f"task_{action}"
    bound = inspect.signature(getattr(tasks, tool)).bind(**arguments)
    bound.apply_defaults()
    return tool_surface.registry()[tool](**bound.arguments)


def _visible_task(task_id: int) -> bool:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (task_id, edition.tenant_org_id())
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _create_once(
    cid: str, title: str, line: str, *, project: str | None, priority: str, requester: str
) -> tuple[int, bool]:
    """Create the task at most once per tenant, conversation, title and options.

    The claim is taken under a write lock before the task exists, so retried
    and concurrent identical handoffs converge on one task: a retry after the
    winner finished returns its task, and a concurrent attempt that loses the
    claim is refused with "already in progress; retry in a moment". A write
    that fails or parks for approval releases the claim.

    The task is created with a ``messages`` completion-return origin
    (``requester`` is the verified caller), recorded by ``task_add`` in the
    task's own transaction, so the task never exists without it.
    """
    key = _claim_key(cid, title, project=project, priority=priority)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not storage.try_claim_idempotency(conn, key, _CLAIM_TOOL):
            row = conn.execute(
                "SELECT result_json, created_at FROM idempotency_keys WHERE key=? AND tool=?",
                (key, _CLAIM_TOOL),
            ).fetchone()
            record = json.loads(row["result_json"]) if row and row["result_json"] else {}
            if isinstance(record.get("task_id"), int):
                conn.commit()
                return record["task_id"], False
            if not _stale(row["created_at"] if row else None):
                conn.rollback()
                raise ToolError(f"{IN_PROGRESS}; retry in a moment")
            # A crashed attempt left its claim: take it over.
            conn.execute(
                "UPDATE idempotency_keys SET created_at=? WHERE key=? AND tool=?",
                (storage.now(), key, _CLAIM_TOOL),
            )
        conn.commit()
    finally:
        conn.close()
    origin = completion_return.Origin(
        surface="messages", channel=cid, thread=cid, requester=requester
    )
    try:
        with completion_return.creation_origin(origin):
            added = _task_write("add", title=title, notes=line, priority=priority, project=project)
    except BaseException:
        _release_claim(key)
        raise
    tid = int(added["id"])
    conn = storage.connect()
    try:
        # Let a failure surface rather than report success: a claim left
        # pending lets a takeover after _STALE_CLAIM create a second task.
        storage.record_idempotency_required(conn, key, _CLAIM_TOOL, {"task_id": tid})
        conn.commit()
    finally:
        conn.close()
    return tid, True


def _claim_key(cid: str, title: str, *, project: str | None = None, priority: str = "med") -> str:
    """The idempotency key one create handoff claims, per tenant.

    The destination and options are part of the key: the same title handed to
    another project (or at another priority) is a different task, not a repeat.
    """
    tenant = edition.tenant_org_id() or ""
    material = json.dumps([tenant, cid, title, project or "", priority])
    digest = hashlib.sha256(material.encode()).hexdigest()[:40]
    return f"conversation-handoff:{digest}"


def _stale(created_at: str | None) -> bool:
    try:
        stamp = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - stamp > _STALE_CLAIM


def _release_claim(key: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE key=? AND tool=? AND result_json=?",
            (key, _CLAIM_TOOL, json.dumps({"pending": True})),
        )
        conn.commit()
    finally:
        conn.close()


def conversation_handoffs(conversation_id: str) -> list[dict[str, Any]]:
    """Tasks a visible conversation was handed off to, oldest first.

    The list is derived from the durable task-note reference, not the event
    log: pruning events, or an event write that failed after the task write,
    never hides a link. ``by`` and ``at`` come from the handoff event while it
    still exists and are ``None`` once it does not.
    """
    cid = str(conversation_id or "").strip()
    if not _ID_RE.fullmatch(cid) or conversations.conversation_thread(cid, limit=1) is None:
        return []
    tenant = edition.tenant_org_id()
    conn = storage.connect()
    try:
        # instr, not LIKE: "_" and "%" in an id would be wildcards there.
        tasks = conn.execute(
            "SELECT id, title, status FROM tasks "
            "WHERE org_id IS ? AND instr(notes, ?) > 0 ORDER BY id",
            (tenant, reference(cid)),
        ).fetchall()
        audit: dict[int, tuple[Any, Any]] = {}
        for row in conn.execute(
            "SELECT json_extract(payload, '$.task_id') AS task_id, "
            "json_extract(payload, '$.by') AS by_agent, created_at FROM event_log "
            "WHERE channel=? AND org_id IS ? "
            "AND json_extract(payload, '$.conversation_id') = ? ORDER BY ROWID",
            (HANDOFF_CHANNEL, tenant, cid),
        ):
            audit.setdefault(row["task_id"], (row["by_agent"], row["created_at"]))
        return [
            {
                "task_id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "by": audit.get(task["id"], (None, None))[0],
                "at": audit.get(task["id"], (None, None))[1],
            }
            for task in tasks
        ]
    finally:
        conn.close()
