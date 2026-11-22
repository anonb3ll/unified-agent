"""FirstMate-style crew live sessions: watch + typed intervene (task #1025).

Not a PTY/tmux bridge. Interventions reach the target agent as a typed
``message_send`` addressed to that one session (``to_session``, task #1860), so a
second session of the same agent does not see it, plus a task-note marker.
When the session has a worktree an append-only ``.ua/interventions.jsonl`` line is
also written. Nothing consumes that file for delivery (the target reads its
mailbox); it is an audit copy that ``crew_view`` echoes back as
``worktree_interventions`` so the Control Center can show what was sent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, registry, storage
from .messaging import index as message_index
from .messaging.send import send_message
from .telemetry.redaction import redact_sensitive_content

log = logging.getLogger(__name__)

# Control Center login is not a roster agent label, so map it to the
# registered human name (see config.operator_dispatch_alias).

INTERVENTION_RELPATH = Path(".ua") / "interventions.jsonl"
MAX_BODY_CHARS = 2000
CREW_CONVERSATION_PREFIX = "crew:"

_OPEN_STATUSES = frozenset({"starting", "running", "waiting"})


def crew_conversation_id(session_id: str) -> str:
    return f"{CREW_CONVERSATION_PREFIX}{session_id}"


def resolve_from_agent(label: str) -> str:
    name = (label or "").strip()
    if not name:
        raise ToolError("from_agent is required")
    return config.operator_dispatch_alias(name)


def crew_live_view(session_id: str, *, message_limit: int = 50) -> dict[str, Any]:
    """Session snapshot + recent crew / task / delegation messages."""
    sid = (session_id or "").strip()
    if not sid:
        raise ToolError("session_id is required")
    session = registry.get_session(sid)
    if session is None:
        raise ToolError(f"unknown session {sid}")

    cap = max(1, min(int(message_limit), 200))
    conversation = crew_conversation_id(sid)
    task_id = session.get("task_id")
    delegation_id = session.get("delegation_id")
    # The crew view is gated by the crew_view tool, not by mailbox ownership, so
    # it reads the index directly rather than through the caller-scoped list.
    scopes: list[dict[str, Any]] = [{"conversation_id": conversation}]
    if task_id is not None:
        scopes.append({"task_id": task_id})
    if delegation_id is not None:
        scopes.append({"delegation_id": delegation_id})
    found: dict[str, dict[str, Any]] = {}
    conn = storage.connect()
    try:
        for scope in scopes:
            for row in message_index.query(conn, limit=cap, **scope):
                found[row["ulid"]] = row
    finally:
        conn.close()
    messages = [found[key] for key in sorted(found)][-cap:]

    drop_path = _intervention_path(session.get("worktree_path"))
    return {
        "session": session,
        "live": (session.get("status") or "") in _OPEN_STATUSES,
        "conversation_id": conversation,
        "messages": messages,
        "message_total": len(messages),
        "interventions_path": str(drop_path) if drop_path is not None else None,
        "worktree_interventions": _read_worktree_interventions(drop_path, limit=cap),
    }


def intervene_on_session(
    *,
    session_id: str,
    body: str,
    from_agent: str,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Send a typed intervention to that session and record it."""
    sid = (session_id or "").strip()
    if not sid:
        raise ToolError("session_id is required")
    session = registry.get_session(sid)
    if session is None:
        raise ToolError(f"unknown session {sid}")

    text = body if isinstance(body, str) else str(body)
    text = text.strip()
    if not text:
        raise ToolError("body must be a non-empty string")
    if len(text) > MAX_BODY_CHARS:
        raise ToolError(f"body must be at most {MAX_BODY_CHARS} characters")
    text = redact_sensitive_content(text, max_len=MAX_BODY_CHARS)

    sender = resolve_from_agent(from_agent)
    recipient = (session.get("agent") or "").strip()
    if not recipient:
        raise ToolError(f"session {sid} has no agent")
    if sender == recipient:
        raise ToolError("from_agent and session agent must differ")

    conversation = crew_conversation_id(sid)
    message = send_message(
        act="request",
        to_agent=recipient,
        to_session=sid,
        from_agent=sender,
        body=text,
        conversation_id=conversation,
        message_id=message_id,
        task_id=session.get("task_id"),
        delegation_id=session.get("delegation_id"),
    )

    note_marker = f"[intervention @ {storage.now()}] session={sid} from {sender}: {text[:500]}"
    note_recorded = False
    task_id = session.get("task_id")
    if task_id is not None:
        note_recorded = _append_task_intervention_note(
            task_id=int(task_id),
            marker=note_marker,
            actor=sender,
        )

    drop_path = _intervention_path(session.get("worktree_path"))
    worktree_written = False
    if drop_path is not None and not message.get("duplicate"):
        worktree_written = _append_worktree_intervention(
            drop_path,
            {
                "ts": storage.now(),
                "session_id": sid,
                "from_agent": sender,
                "to_agent": recipient,
                "to_session": sid,
                "message_id": message.get("message_id"),
                "task_id": task_id,
                "delegation_id": session.get("delegation_id"),
                "body": text,
            },
        )

    return {
        "accepted": True,
        "duplicate": bool(message.get("duplicate")),
        "session_id": sid,
        "to_agent": recipient,
        "to_session": sid,
        "from_agent": sender,
        "conversation_id": conversation,
        "message": message,
        "note_recorded": note_recorded,
        "task_id": task_id,
        "delegation_id": session.get("delegation_id"),
        "worktree_written": worktree_written,
        "interventions_path": str(drop_path) if drop_path is not None else None,
    }


def _append_task_intervention_note(*, task_id: int, marker: str, actor: str) -> bool:
    """Append the intervention marker without minting a worktree."""
    from .tools import tasks as task_tools

    try:
        task_tools.task_update(
            id=task_id,
            append_note=marker,
            worktree=False,
            actor=actor,
        )
        return True
    except ToolError as exc:
        log.warning("crew intervene: task note append failed for #%s: %s", task_id, exc)
        return False


def _intervention_path(worktree_path: str | None) -> Path | None:
    raw = (worktree_path or "").strip()
    if not raw:
        return None
    root = Path(raw)
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    return root / INTERVENTION_RELPATH


def _append_worktree_intervention(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except OSError as exc:
        log.warning("crew intervene: worktree drop failed at %s: %s", path, exc)
        return False


def _read_worktree_interventions(path: Path | None, *, limit: int) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def crew_view(session_id: str, limit: int = 50) -> dict[str, Any]:
    """Inspect live crew status and recent messages for an active session."""
    return crew_live_view(session_id=session_id, message_limit=limit)


def crew_intervene(
    session_id: str,
    message: str,
    message_id: str | None = None,
    from_agent: str | None = None,
) -> dict[str, Any]:
    """Send a typed intervention into an active crew session."""
    import os

    from . import config

    caller = (
        config.verified_agent_identity()
        or from_agent
        or os.environ.get("UA_AGENT_LABEL")
        or os.environ.get("AGENT_LABEL")
        or ""
    )
    return intervene_on_session(
        session_id=session_id,
        body=message,
        from_agent=caller,
        message_id=message_id,
    )
