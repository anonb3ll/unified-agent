"""Supervised long commands on a ua-node (task #2413).

An agent starts a long test or build as a node item with ``backend="command"``.
The command runs on the node, not in the agent's session, so it keeps running
when that session dies. When it ends, the owner gets one hub message with the
exit status, duration and log pointer, and a later session of the same agent
can adopt the item while it is still running.

Storage. ``node_dispatch`` is never given new columns (every extension is a
side table, see ``table_schemas``). ``node_command`` holds the argv, the owner
and the current fence; ``node_command_notice`` is the owner-message outbox.

Fence. Each claim mints a fresh ``fence_token`` for that attempt and sends it
in the claim payload; the receipt must echo it. Completion is one compare-and-
set on (id, attempts, status='claimed', receipt NULL) with the token checked in
the same transaction, so a writer from an earlier claim, or one that never held
the token, is refused with ``stale_fence`` (HTTP 409) and an audit event.

Owner message. ``send_message`` opens its own transaction, so the receipt
transaction writes an intent row keyed (item_id, attempt, terminal); after the
commit the hub sends label mail to the owner from the service sender
``hub-node`` under a message id derived from that key, so a retried delivery is
a duplicate, never a second message. Queue hygiene retries unsent intents.
Label mail, not session mail: the session that started the command may be dead.

Safety. Running argv on a device is a new execution capability, so it is off by
default twice over: a node runs a command only when it advertises the
``command`` backend (``UA_NODE_BACKENDS``) and ``argv[0]`` is in its
``UA_NODE_COMMANDS`` allowlist. The hub path keeps the ``node_dispatch``
approval gate, with the argv in the approved payload.
"""

from __future__ import annotations

import json
import logging
import secrets
import shlex
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import events, storage

log = logging.getLogger(__name__)

BACKEND = "command"
MAX_ARGS = 64
MAX_ARG_CHARS = 4096
#: The file the node writes the command's combined output to, in its session dir.
LOG_NAME = "command.log"
#: How long a claimed command may run past its lease, or an owner message stay
#: unsent, before the doctor reports it.
UNSETTLED_GRACE_S = 15 * 60


class CommandError(ValueError):
    """A malformed command or a refused adoption; ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Side tables, idempotent; no column edit to ``node_dispatch``."""
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_command (
  item_id TEXT PRIMARY KEY NOT NULL,
  argv_json TEXT NOT NULL,
  owner_agent TEXT NOT NULL,
  owner_session TEXT,
  fence_attempt INTEGER,
  fence_token TEXT,
  exit_code INTEGER,
  log_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""
    )
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS node_command_notice (
  item_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  terminal TEXT NOT NULL,
  owner_agent TEXT NOT NULL,
  body TEXT NOT NULL,
  task_id INTEGER,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  message_id TEXT,
  PRIMARY KEY (item_id, attempt, terminal)
)
"""
    )


def validate_argv(argv: Any) -> list[str]:
    """An argv list of strings; never a shell string."""
    if isinstance(argv, str) or not isinstance(argv, list) or not argv:
        raise CommandError("command must be a non-empty argv list, never a shell string")
    if len(argv) > MAX_ARGS:
        raise CommandError(f"command has more than {MAX_ARGS} arguments")
    out = []
    for arg in argv:
        if not isinstance(arg, str) or "\x00" in arg or len(arg) > MAX_ARG_CHARS:
            raise CommandError(
                f"every command argument must be a string under {MAX_ARG_CHARS} characters"
            )
        out.append(arg)
    if not out[0].strip():
        raise CommandError("command[0] must name a program")
    return out


def describe(argv: list[str]) -> str:
    """The item's ``prompt``: a readable one-line description of the command."""
    return f"command: {shlex.join(argv)}"[:2000]


def insert(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    argv: list[str],
    owner_agent: str,
    owner_session: str | None,
    as_of: str,
) -> None:
    """Record the command beside its new ``node_dispatch`` row (same transaction)."""
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO node_command (item_id, argv_json, owner_agent, owner_session, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, json.dumps(argv), owner_agent, owner_session, as_of, as_of),
    )


def mint_fence(conn: sqlite3.Connection, item_id: str, attempt: int, as_of: str) -> dict:
    """Bind a fresh token to this claim attempt; returns the claim-payload fields."""
    ensure_schema(conn)
    token = secrets.token_hex(16)
    conn.execute(
        "UPDATE node_command SET fence_attempt=?, fence_token=?, updated_at=? WHERE item_id=?",
        (attempt, token, as_of, item_id),
    )
    row = conn.execute("SELECT argv_json FROM node_command WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        raise CommandError(f"command item {item_id} has no command row", status=409)
    return {"argv": json.loads(row["argv_json"]), "fence_token": token, "log_name": LOG_NAME}


def check_fence(
    conn: sqlite3.Connection, item_id: str, *, attempt: int, token: Any
) -> dict[str, Any]:
    """The command row when ``token`` holds the fence for ``attempt``; else ``stale_fence``."""
    row = conn.execute("SELECT * FROM node_command WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        raise CommandError(f"command item {item_id} has no command row", status=409)
    if (
        not isinstance(token, str)
        or row["fence_token"] is None
        or not secrets.compare_digest(token, row["fence_token"])
        or row["fence_attempt"] != attempt
    ):
        raise CommandError(
            f"stale fence: this writer does not hold claim {attempt} of {item_id}",
            status=409,
            code="stale_fence",
        )
    return dict(row)


def _duration_s(started: Any, finished: Any) -> int | None:
    try:
        a = datetime.fromisoformat(str(started))
        b = datetime.fromisoformat(str(finished))
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds()))


def notice_body(item: dict[str, Any], command: dict[str, Any], receipt: dict[str, Any]) -> str:
    """Status, exit code, duration and where the log is; never the output itself."""
    argv = json.loads(command["argv_json"])
    duration = _duration_s(receipt.get("started_at"), receipt.get("finished_at"))
    took = f" after {duration}s" if duration is not None else ""
    reason = f" ({receipt['reason']})" if receipt.get("reason") else ""
    where = receipt.get("log_path") or f"{LOG_NAME} in the item's session dir on {item['node']}"
    truncated = " (truncated at the node's log cap)" if receipt.get("truncated") else ""
    capture = receipt.get("log_capture")
    capture_note = ""
    if isinstance(capture, dict):
        capture_note = f" Log capture {capture['state']}; snapshot at command completion."
        if capture.get("state_path"):
            capture_note += f" Relay state: {capture['state_path']}."
    adopted = f" Owner session: {command['owner_session']}." if command.get("owner_session") else ""
    return (
        f"Command {item['id']} on {item['node']} {receipt['status']}{reason}{took}, "
        f"exit code {receipt.get('exit_code')}: {describe(argv)[:300]}. "
        f"Log: {where}{truncated}."
        f"{capture_note}"
        f"{adopted} Read it with coordination_manage('node_command_status', "
        f"{{'item_id': '{item['id']}'}})."
    )


def record_completion(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    command: dict[str, Any],
    receipt: dict[str, Any],
    as_of: str,
) -> None:
    """Inside the receipt transaction: the exit facts and one owner-message intent."""
    conn.execute(
        "UPDATE node_command SET exit_code=?, log_path=?, updated_at=? WHERE item_id=?",
        (receipt.get("exit_code"), receipt.get("log_path"), as_of, item["id"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO node_command_notice (item_id, attempt, terminal, owner_agent, "
        "body, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item["id"],
            int(command["fence_attempt"]),
            receipt["status"],
            command["owner_agent"],
            notice_body(item, command, receipt),
            item.get("task_id"),
            as_of,
        ),
    )


def notice_id(item_id: str, attempt: int, terminal: str) -> str:
    from .messaging import delivery

    return f"{delivery.NODE_NOTICE_ID_PREFIX}{item_id}-{attempt}-{terminal}"


def deliver_notices(*, item_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Send unsent owner messages; idempotent by derived message id. Never raises."""
    from .messaging import delivery
    from .messaging.send import send_service_notice

    result: dict[str, Any] = {"sent": [], "errors": []}
    try:
        conn = storage.connect()
        try:
            ensure_schema(conn)
            sql = "SELECT * FROM node_command_notice WHERE sent_at IS NULL"
            params: list[Any] = []
            if item_id is not None:
                sql += " AND item_id=?"
                params.append(item_id)
            rows = [
                dict(r)
                for r in conn.execute(
                    sql + " ORDER BY created_at LIMIT ?", (*params, limit)
                ).fetchall()
            ]
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - the intent stays for the next pass
        result["errors"].append(f"read: {type(exc).__name__}")
        return result
    for row in rows:
        mid = notice_id(row["item_id"], row["attempt"], row["terminal"])
        try:
            send_service_notice(
                sender=delivery.NODE_NOTICE_SENDER,
                message_id=mid,
                recipient=row["owner_agent"],
                body=row["body"],
                task_id=row["task_id"],
            )
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE node_command_notice SET sent_at=?, message_id=? "
                    "WHERE item_id=? AND attempt=? AND terminal=? AND sent_at IS NULL",
                    (storage.now(), mid, row["item_id"], row["attempt"], row["terminal"]),
                )
                conn.commit()
            finally:
                conn.close()
            result["sent"].append(mid)
        except Exception as exc:  # noqa: BLE001 - retried by the next pass
            result["errors"].append(f"{mid}: {type(exc).__name__}")
    return result


def _command_row(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT c.*, d.node, d.status, d.attempts, d.cwd, d.created_by, d.task_id, "
        "d.created_at AS queued_at, d.claimed_at, d.completed_at, d.lease_expires_at, d.receipt_json "
        "FROM node_command c JOIN node_dispatch d ON d.id = c.item_id WHERE c.item_id=?",
        (item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Everything but the fence token, which only the claiming node holds."""
    receipt = json.loads(row.get("receipt_json") or "{}")
    return {
        "item_id": row["item_id"],
        "node": row["node"],
        "status": row["status"],
        "attempt": row["attempts"],
        "argv": json.loads(row["argv_json"]),
        "cwd": row["cwd"],
        "owner_agent": row["owner_agent"],
        "owner_session": row["owner_session"],
        "exit_code": row["exit_code"],
        "log_path": row["log_path"],
        "log_capture": receipt.get("log_capture"),
        "task_id": row["task_id"],
        "queued_at": row["queued_at"],
        "claimed_at": row["claimed_at"],
        "completed_at": row["completed_at"],
    }


def _may_read(row: dict[str, Any], caller: str | None) -> bool:
    from . import config

    if caller is None:
        # Argv can carry secrets: an unverified caller reads nothing (review #1569).
        return False
    return caller in (row["owner_agent"], row["created_by"]) or config.is_human_identity(caller)


def status(item_id: str, *, caller: str | None) -> dict[str, Any]:
    conn = storage.connect()
    try:
        row = _command_row(conn, str(item_id or ""))
    finally:
        conn.close()
    if row is None or not _may_read(row, caller):
        raise CommandError(f"no command item {item_id!r} visible to you", status=404)
    return _public(row)


def adopt(item_id: str, *, caller: str | None, session_id: str | None) -> dict[str, Any]:
    """Rebind the owner session of an unfinished command; never the attempt or token.

    Only the owner label itself (a later session of the same agent) or a
    lifecycle operator may adopt. ``session_id`` must be one of the owner's
    sessions when given; ``None`` makes the owner the label alone. Operator
    recovery never transfers the command to another agent.
    """
    from . import agent_lifecycle, registry

    if caller is None:
        raise CommandError("adopting a command needs a verified identity", status=403)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _command_row(conn, str(item_id or ""))
        operator = caller in agent_lifecycle.operators()
        if row is None or (not operator and not _may_read(row, caller)):
            raise CommandError(f"no command item {item_id!r} visible to you", status=404)
        if caller != row["owner_agent"] and not operator:
            raise CommandError(
                f"only {row['owner_agent']} or a lifecycle operator may adopt {item_id}",
                status=403,
            )
        if row["status"] not in ("queued", "claimed"):
            raise CommandError(f"command {item_id} already {row['status']}", status=409)
        if session_id is not None:
            session = registry.get_session(session_id)
            if session is None or session["agent"] != row["owner_agent"]:
                raise CommandError(
                    f"session {session_id!r} is not a session of {row['owner_agent']}",
                    status=400,
                )
        ts = storage.now()
        conn.execute(
            "UPDATE node_command SET owner_session=?, updated_at=? WHERE item_id=?",
            (session_id, ts, row["item_id"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    try:
        events.record_event(
            "agent.node",
            {
                "kind": "command_adopted",
                "item_id": row["item_id"],
                "owner_agent": row["owner_agent"],
                "from_session": row["owner_session"],
                "to_session": session_id,
            },
            originator=caller,
        )
    except Exception:  # noqa: BLE001 - the row is the source of truth
        pass
    return {**_public({**row, "owner_session": session_id}), "adopted": True}


def unsettled(now: datetime | None = None, grace_s: int = UNSETTLED_GRACE_S) -> dict[str, Any]:
    """Claimed commands past lease plus grace, and owner messages unsent past grace."""
    ref = now or datetime.now(UTC)
    cutoff = (ref.timestamp()) - grace_s
    conn = storage.connect()
    try:
        ensure_schema(conn)
        overdue = [
            r["item_id"]
            for r in conn.execute(
                "SELECT c.item_id, d.lease_expires_at FROM node_command c "
                "JOIN node_dispatch d ON d.id = c.item_id WHERE d.status='claimed'"
            ).fetchall()
            if _ts(r["lease_expires_at"]) is not None and _ts(r["lease_expires_at"]) < cutoff
        ]
        unsent = [
            notice_id(r["item_id"], r["attempt"], r["terminal"])
            for r in conn.execute(
                "SELECT item_id, attempt, terminal, created_at FROM node_command_notice "
                "WHERE sent_at IS NULL"
            ).fetchall()
            if _ts(r["created_at"]) is not None and _ts(r["created_at"]) < cutoff
        ]
        conn.commit()
    finally:
        conn.close()
    return {"overdue": overdue, "unsent": unsent}


def _ts(value: Any) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# --- coordination_manage actions ---------------------------------------------------


def _tool_call(fn: Any) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    from .node_registry import NodeError

    try:
        return fn()
    except (NodeError, CommandError) as exc:
        raise ToolError(str(exc)) from exc


def node_command_start(
    node: str,
    argv: list[str],
    cwd: str,
    timeout_s: int | None = None,
    task_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Start a supervised command on ``node``; the caller is its owner.

    ``argv`` is a list, never a shell string. It goes through the
    ``node_dispatch`` approval gate with the argv in the approved payload, and
    runs only on a node that allowlists ``argv[0]``. ``session_id`` names the
    caller's own session; the completion message goes to the caller's label
    either way, so it survives that session.
    """
    from . import config, node_queue, registry

    actor = config.verified_agent_identity()

    def run() -> dict[str, Any]:
        if not actor:
            raise CommandError("starting a command needs a verified identity", status=403)
        clean = validate_argv(argv)
        if session_id is not None:
            session = registry.get_session(session_id)
            if session is None or session["agent"] != actor:
                raise CommandError(f"session {session_id!r} is not a session of {actor}")
        item = node_queue.request_dispatch(
            node,
            actor=str(actor),
            backend=BACKEND,
            prompt=describe(clean),
            cwd=cwd,
            timeout_s=timeout_s,
            task_id=task_id,
            command={"argv": clean, "owner_agent": str(actor), "owner_session": session_id},
        )
        return {**item, "owner_agent": actor, "owner_session": session_id, "argv": clean}

    return _tool_call(run)


def node_command_status(item_id: str) -> dict:
    """One command's state, owner, exit code and log pointer (owner or starter only)."""
    from . import config

    return _tool_call(lambda: status(item_id, caller=config.verified_agent_identity()))


def node_command_adopt(item_id: str, session_id: str | None = None) -> dict:
    """Make ``session_id`` (a session of the owner label) the command's owner session."""
    from . import config

    return _tool_call(
        lambda: adopt(item_id, caller=config.verified_agent_identity(), session_id=session_id)
    )
