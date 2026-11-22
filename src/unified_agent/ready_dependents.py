"""Tell a dependent's owner when its last prerequisite closes (task #2454).

Task dependencies (``task_dependencies``) used to be display only: a dependent
became ready when its last prerequisite closed, and its owner found out only by
looking. This module acts on that moment. It informs; it never claims,
dispatches or changes the dependent's status.

**Chokepoint.** Every task status writer already passes through
``run_records.queue_completion_return`` inside its own transaction (task #1751):
``task_update`` and the merge closeout reach it through
``record_transition_in_tx``, and ``run_records.complete_direct`` (close-on-merge,
the merge orchestrator, the reviewer auto-close) calls it from its own write.
That hook calls :func:`record_on_connection`, so a close and its readiness
records commit together or not at all.

**Readiness** is the ``task_list(frontier=True)`` rule: the dependent is active
(not ``done`` or ``cancelled``) and every explicit prerequisite row is ``done``
or ``cancelled``. A ``doing``, ``blocked`` or ``waiting_review`` dependent
counts, as it does in the frontier. Only ``task_dependencies`` rows count, never
text in notes (docs/work-graph.md, "no inferred edges").

**Exactly once.** When a prerequisite moves into ``done``/``cancelled`` from an
active status, each dependent that is now ready gets one ``agent.task.ready``
event whose ``dedupe_key`` is the readiness generation: the dependent id, a
digest of its prerequisite ids, and its *epoch*, the ULID of the last
``agent.task.ready.closed`` mark written when the dependent itself closed. A
retried close, a second closer, a reopen and reclose of a prerequisite, or two
prerequisites closing in the same instant all compute the same key, so the
event (and the message derived from it) is written once. The generation moves
only when the dependent's own readiness changes: a new prerequisite edge
changes the digest, and closing then reopening the dependent writes a new epoch.

**Delivery.** The event is the outbox. After commit, ``run_records.flush``
calls :func:`drain_pending` for the events it wrote; the queue-hygiene tick
drains whatever a crash left behind. A send is an ordinary ``inform`` hub
message from the service sender ``hub-graph`` with ``message_id``
``task-ready-<event ulid>``, so a resend after a lost acknowledgement is a
recorded duplicate. ``expects_reply`` is set so the existing wake path rings an
idle session that opted in; the dead-letter sweep skips hub service senders. A
refused send (an owner the roster no longer knows, a comms policy) or a
dependent that stopped being ready before the send is recorded once on
``agent.task.ready.skipped`` and never retried.

**Owner.** The dependent's stored ``next_owner``, else its claimant, else the
agent its lane names, else nobody. A ready dependent with nobody is recorded
with ``owner: null`` and counted by ``queue_health`` as ``ready_unowned``.

**Tenant.** A dependent is considered only in the closing task's org partition.

``UA_READY_DEPENDENT_NOTIFY=0`` turns all of this off without a deploy.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage, task_ownership
from .messaging import delivery

log = logging.getLogger(__name__)

CHANNEL = "agent.task.ready"
CLOSED_CHANNEL = "agent.task.ready.closed"
SKIPPED_CHANNEL = "agent.task.ready.skipped"
SENDER = delivery.READY_NOTICE_SENDER
MESSAGE_ID_PREFIX = delivery.READY_NOTICE_ID_PREFIX
CLOSED_STATUSES = ("done", "cancelled")
#: An event not sent within this window is left alone: a day-old notice is
#: noise, and turning the flag on must not page owners about old closes.
DELIVERY_WINDOW_HOURS = 24
DRAIN_LIMIT = 50

_READY_KEY = "task-ready"
_CLOSED_KEY = "task-ready-closed"
_SKIPPED_KEY = "task-ready-skipped"


def enabled() -> bool:
    return config.ready_dependent_notify_enabled()


def _org_key(org: str | None) -> str:
    return org or "-"


def _closed_prefix(org: str | None, task_id: int) -> str:
    return f"{_CLOSED_KEY}:{_org_key(org)}:{int(task_id)}:"


def _epoch(conn: Any, org: str | None, task_id: int) -> str:
    """The ULID of the dependent's last own-close mark, or ``"0"``.

    A range on the unique ``dedupe_key`` index; ``;`` is the character after
    ``:``, so the range is exactly the keys with this prefix.
    """
    prefix = _closed_prefix(org, task_id)
    row = conn.execute(
        "SELECT ulid FROM event_log WHERE dedupe_key >= ? AND dedupe_key < ? "
        "ORDER BY ROWID DESC LIMIT 1",
        (prefix, prefix[:-1] + ";"),
    ).fetchone()
    return str(row["ulid"]) if row is not None else "0"


def _digest(ids: Iterable[int]) -> str:
    text = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def resolve_owner(task: Any) -> tuple[str | None, str | None]:
    """``(owner, source)``: next_owner, else claimant, else the lane's agent, else nobody."""
    row = dict(task)
    ownership = task_ownership.read(row)
    if ownership.next_owner:
        return ownership.next_owner, "next_owner"
    if ownership.claimant:
        return ownership.claimant, "claimant"
    lane = config.canonical_task_lane(row.get("lane"))
    if lane and lane not in config.TASK_SYSTEM_LANES and config.is_known_agent_label(lane):
        return lane, "lane"
    return None, None


def _prerequisites(conn: Any, task_id: int) -> list[Any]:
    # The frontier's rule: a dependency row whose prerequisite row is gone
    # does not hold the dependent back (the JOIN drops it).
    return conn.execute(
        "SELECT p.id, p.status FROM task_dependencies d "
        "JOIN tasks p ON p.id = d.depends_on_task_id WHERE d.task_id=? ORDER BY p.id",
        (int(task_id),),
    ).fetchall()


def _has_prerequisites(conn: Any, task_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM task_dependencies WHERE task_id=? LIMIT 1", (int(task_id),)
        ).fetchone()
        is not None
    )


def record_on_connection(
    conn: Any,
    *,
    task_id: int,
    old_status: str | None,
    new_status: str | None,
    revision: str,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Record the readiness transitions one close causes, in the caller's transaction.

    Returns ``(ulid, publish_payload)`` for each new event that names an owner
    (the ones :func:`drain_pending` sends). Never raises: the work runs in a
    savepoint, and a failure is logged and rolled back without failing the
    close.
    """
    if not enabled():
        return []
    if new_status not in CLOSED_STATUSES or old_status in CLOSED_STATUSES:
        return []
    conn.execute("SAVEPOINT ready_dependents")
    try:
        written = _record(conn, int(task_id), new_status, revision)
        conn.execute("RELEASE SAVEPOINT ready_dependents")
        return written
    except Exception:  # noqa: BLE001 - a notice must never undo the close
        conn.execute("ROLLBACK TO SAVEPOINT ready_dependents")
        conn.execute("RELEASE SAVEPOINT ready_dependents")
        log.exception("ready-dependent records failed for task %s", task_id)
        return []


def _record(
    conn: Any, task_id: int, new_status: str, revision: str
) -> list[tuple[str, dict[str, Any] | None]]:
    closing = conn.execute("SELECT org_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if closing is None:
        return []
    org = closing["org_id"]
    if _has_prerequisites(conn, task_id):
        # This task is itself a dependent: closing it ends its readiness, so a
        # later reopen starts a new generation.
        events.record_event_once_on_connection(
            conn,
            CLOSED_CHANNEL,
            payload={"task_id": task_id, "status": new_status},
            originator=SENDER,
            dedupe_key=f"{_closed_prefix(org, task_id)}{revision}",
        )
    dependents = conn.execute(
        "SELECT t.* FROM task_dependencies d JOIN tasks t ON t.id = d.task_id "
        "WHERE d.depends_on_task_id=? AND t.org_id IS ? "
        "AND t.status NOT IN ('done', 'cancelled') ORDER BY t.id",
        (task_id, org),
    ).fetchall()
    written: list[tuple[str, dict[str, Any] | None]] = []
    for dependent in dependents:
        prerequisites = _prerequisites(conn, dependent["id"])
        if any(p["status"] not in CLOSED_STATUSES for p in prerequisites):
            continue
        ids = [int(p["id"]) for p in prerequisites]
        cancelled = [int(p["id"]) for p in prerequisites if p["status"] == "cancelled"]
        epoch = _epoch(conn, org, dependent["id"])
        owner, source = resolve_owner(dependent)
        key = f"{_READY_KEY}:{_org_key(org)}:{int(dependent['id'])}:{_digest(ids)}:{epoch}"
        ulid, created, publish = events.record_event_once_on_connection(
            conn,
            CHANNEL,
            payload={
                "task_id": int(dependent["id"]),
                "status": dependent["status"],
                "prerequisite_ids": ids,
                "cancelled_prerequisite_ids": cancelled,
                "closed_by_task_id": task_id,
                "owner": owner,
                "owner_source": source,
                "epoch": epoch,
            },
            originator=SENDER,
            dedupe_key=key,
        )
        if created and owner:
            written.append((ulid, publish))
    return written


# --- delivery -------------------------------------------------------------------


def message_id(ulid: str) -> str:
    return f"{MESSAGE_ID_PREFIX}{ulid}"


def render_body(payload: dict[str, Any]) -> str:
    """Ids and statuses only; never a title or note text."""
    task_id = int(payload["task_id"])
    cancelled = set(payload.get("cancelled_prerequisite_ids") or [])
    done = [i for i in payload.get("prerequisite_ids") or [] if i not in cancelled]
    parts = []
    if done:
        parts.append("done: " + ", ".join(f"#{i}" for i in done))
    if cancelled:
        parts.append("cancelled: " + ", ".join(f"#{i}" for i in sorted(cancelled)))
    body = f"Task #{task_id} is ready: all of its prerequisites are closed ({'; '.join(parts)})."
    if cancelled:
        body += (
            " A cancelled prerequisite did not deliver its work, so check the plan "
            "before you start."
        )
    if payload.get("status") == "blocked":
        body += (
            " The task is still blocked: its prerequisites are closed, so check what else holds it."
        )
    body += " This notice changes nothing on the task; claim or update it when you pick it up."
    return body


def _still_ready(conn: Any, task_id: int, org: str | None) -> bool:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id=? AND org_id IS ?", (task_id, org)
    ).fetchone()
    if row is None or row["status"] in CLOSED_STATUSES:
        return False
    return all(p["status"] in CLOSED_STATUSES for p in _prerequisites(conn, task_id))


def _skip(ulid: str, payload: dict[str, Any], reason: str) -> bool:
    return bool(
        events.record_event_once(
            SKIPPED_CHANNEL,
            {"event_ulid": ulid, "task_id": payload.get("task_id"), "reason": reason[:200]},
            originator=SENDER,
            dedupe_key=f"{_SKIPPED_KEY}:{ulid}",
        )[1]
    )


def _pending(
    conn: Any, tenant: str | None, since: str, ulids: list[str] | None, limit: int
) -> list[Any]:
    params: list[Any] = [CHANNEL, tenant, since]
    narrow = ""
    if ulids:
        narrow = f" AND e.ulid IN ({','.join('?' * len(ulids))})"
        params.extend(ulids)
    params.extend([MESSAGE_ID_PREFIX, f"{_SKIPPED_KEY}:", max(1, int(limit))])
    return conn.execute(
        "SELECT e.ulid, e.payload FROM event_log e "
        "WHERE e.channel=? AND e.org_id IS ? AND e.created_at >= ? "
        f"AND json_extract(e.payload, '$.owner') IS NOT NULL{narrow} "
        "AND NOT EXISTS (SELECT 1 FROM message_index i WHERE i.org_id IS e.org_id "
        "AND i.message_id = ? || e.ulid) "
        "AND NOT EXISTS (SELECT 1 FROM event_log s WHERE s.dedupe_key = ? || e.ulid) "
        "ORDER BY e.ROWID LIMIT ?",
        params,
    ).fetchall()


def drain_pending(
    *,
    ulids: Iterable[str] | None = None,
    now: datetime | None = None,
    limit: int = DRAIN_LIMIT,
) -> dict[str, Any]:
    """Send the ready notices not yet sent or skipped. Never raises.

    ``ulids`` narrows the pass to the events one commit wrote (the post-commit
    drain); without it the pass covers the caller's tenant (the hygiene tick).
    """
    result: dict[str, Any] = {"enabled": enabled(), "sent": [], "skipped": [], "failed": []}
    if not result["enabled"]:
        return result
    narrowed = list(dict.fromkeys(ulids)) if ulids is not None else None
    if narrowed is not None and not narrowed:
        return result
    try:
        from .messaging import send as messaging_send

        tenant = edition.tenant_org_id()
        current = now or datetime.now(UTC)
        since = (current - timedelta(hours=DELIVERY_WINDOW_HOURS)).isoformat(timespec="seconds")
        conn = storage.connect()
        try:
            rows = _pending(conn, tenant, since, narrowed, limit)
            work = []
            for row in rows:
                payload = json.loads(row["payload"])
                ready = _still_ready(conn, int(payload["task_id"]), tenant)
                work.append((str(row["ulid"]), payload, ready))
        finally:
            conn.close()
        for ulid, payload, ready in work:
            if not ready:
                _skip(ulid, payload, "no longer ready")
                result["skipped"].append(ulid)
                continue
            try:
                messaging_send.send_ready_notice(
                    event_ulid=ulid,
                    recipient=str(payload["owner"]),
                    body=render_body(payload),
                    task_id=int(payload["task_id"]),
                )
            except ToolError as exc:
                # A refusal names an address or a policy; retrying cannot change it.
                _skip(ulid, payload, f"refused: {type(exc).__name__}: {exc}")
                result["skipped"].append(ulid)
                continue
            except Exception as exc:  # noqa: BLE001 - left pending for the next pass
                log.warning("ready notice %s not sent: %s", ulid, type(exc).__name__)
                result["failed"].append(ulid)
                continue
            result["sent"].append(ulid)
    except Exception as exc:  # noqa: BLE001 - every drain point is best-effort
        log.exception("ready-dependent drain failed")
        result["error"] = type(exc).__name__
    return result


# --- queue view -----------------------------------------------------------------


def ready_unowned_count(conn: Any, tenant_clause: str, params: Iterable[Any]) -> int:
    """Unparked active tasks with prerequisites, all closed, and nobody to move them.

    The live view of what :func:`record_on_connection` records as ``owner:
    null``. ``tenant_clause`` fences ``tasks`` (unqualified column names).
    """
    rows = conn.execute(
        "SELECT * FROM tasks WHERE archived_at IS NULL "
        f"AND status NOT IN ('done', 'cancelled') AND {tenant_clause} "
        "AND EXISTS (SELECT 1 FROM task_dependencies d WHERE d.task_id = tasks.id) "
        "AND NOT EXISTS (SELECT 1 FROM task_dependencies d "
        "JOIN tasks p ON p.id = d.depends_on_task_id "
        "WHERE d.task_id = tasks.id AND p.status NOT IN ('done', 'cancelled')) "
        "AND COALESCE(TRIM(next_owner), '') = '' AND COALESCE(TRIM(claimed_by), '') = ''",
        tuple(params),
    ).fetchall()
    return sum(1 for row in rows if resolve_owner(row)[0] is None)
