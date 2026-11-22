"""Task subtree holds: stop a unit of work in one action and resume it safely (task #1747).

An operator holds a root task (an epic or a sprint) and every task under it, instead
of cancelling tasks one by one. One active hold exists per root; a released hold
stays as history and a later pause creates a new record.

* **Members are a snapshot.** The hold records the root's subtree at hold time, with
  each member's status then. A task added under the root after the hold is NOT held:
  re-hold after release to include it. A member moved out of the subtree stays held.
* **Refusal.** While a hold is active, new work on a member is refused with an error
  naming the hold: ``task_claim``, ``task_update(status="doing")`` from another status,
  approved dispatch (``dispatch.coordinator``) and node dispatch (``node_queue.enqueue``,
  and ``node_queue.request_dispatch`` before its approval gate). :func:`claim_block` is
  the one check, called next to ``agent_lifecycle.claim_block`` in each.
* **Renewals.** A live renewal (the claimant re-claiming its own ``doing`` task before
  the lease lapses) is NOT refused. Refusing it would let the lease of an in-flight run
  lapse mid-run and strand the run's ability to finish and report: the executor treats
  a refused renewal as lost ownership. In-flight runs may finish; new claims, a re-claim
  after the lease lapsed, and a takeover are refused. A claim with no lease expiry
  (``lock_expires_at`` NULL) is not renewable while held, since it would renew forever.
  Stopping in-flight work is what ``cancel`` is for, and a cancelled run's renewal is
  already refused by the ``request_cancel`` flag (task #1525).
* **Interrupted work.** :func:`claim_block` also refuses a task the lease reaper left
  ``blocked`` and unowned because its owner's run never recorded a finish
  (``lease_reaper.interrupted_hold``, task #2482). That hold has no row here and no
  ``resume`` mode: an explicit ``task_claim`` by an agent with no delegation queued for
  the task ends it, as does reopening or closing the task. Everything queued is
  refused through the same call sites, plus ``dispatch_resume``.

Modes (``task_hold``):

``pause``    create the hold (idempotent: an active hold on the root is returned as is).
``cancel``   pause, then request cancellation of every member's live work, each through
             its existing path and authority rule: ``task_update(request_cancel=True)``
             on a live self-pickup run (the run's claimant or a lifecycle operator), and
             ``node_queue.cancel`` on a queued or claimed node item (the item's creator,
             the task's claimant or an operator). Escalates an active pause hold. Each
             result is reported; a refusal does not abort. Only a request this call
             made (the flag went from clear to set) is recorded as the hold's, with the
             run it targeted; a request already standing is ``already_requested`` and
             restore never clears it. Until the requests are recorded the hold is
             ``cancelling`` and restore refuses.
``resume``   release the hold. Nothing else changes.
``restore``  undo the hold's own cancel requests, then release once every one is undone.
             Members that were ``done``/``cancelled`` at hold time are never touched.
             A member whose cancelled run is still open is ``pending``: the run ledger
             keeps the request and the executor will still stop, so nothing is cleared
             and the hold stays active; restore again once the run has closed. Once it
             has, a still-set ``cancel_requested`` flag is cleared, and the member is
             reopened (``open``, claim cleared) only if that run acknowledged the cancel
             (its finish is the fenced cancel an executor makes, ``cancel_acknowledged``)
             and nothing changed the task's status since. A task its claimant cancelled
             on its own stays cancelled. Node items a cancel stopped are not restarted.

Who may release (``resume`` or ``restore``): the hold's creator, a lifecycle operator
(``UA_LIFECYCLE_OPERATORS``, as for ``request_cancel``), or the verified human operator
over HTTP (:func:`operator_release`). Without a verified identity, only the ``actor``
that created the hold. Restore also follows ``request_cancel``'s rule per member: the
cancelled run's claimant or an operator. Any other member is ``refused`` and keeps the
hold active.

Release policy. ``manual`` (default) waits for resume/restore.
``after_active_runs_finish`` releases the hold on its own once no member has a live
run: a claimed ``doing`` row whose lease has a future expiry (a NULL lease is not
live), a live dispatch run, or a queued or claimed node item. It is evaluated when a
hold is read (``task_holds``, ``task_hold_get``) and when a claim meets the hold
(:func:`claim_block`), so no daemon is involved; a release made inside a claim's
transaction is published after that transaction commits. It is refused with
``cancel`` (a cancel hold that released itself could no longer be restored), and a new
hold needs at least one live run in the subtree (on an idle subtree it would release
at once: use ``manual``).

Who may hold: a caller whose profile exposes ``task_update``; over HTTP the verified
operator too. Every hold, release, cancel and restore is an ``event_log`` row on
``agent.task.hold``. Every read and write is fenced to the caller's tenant
(``org_id IS edition.tenant_org_id()``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_lifecycle,
    config,
    edition,
    events,
    lease_reaper,
    profiles,
    run_records,
    storage,
)
from .dispatch_states import LIVE_STATES

CHANNEL = "agent.task.hold"
KIND = "task.hold"
MODES = ("pause", "cancel", "resume", "restore")
RELEASE_POLICIES = ("manual", "after_active_runs_finish")
TERMINAL_STATUSES = ("done", "cancelled")
AUTO_RELEASE_ACTOR = "task-hold:after_active_runs_finish"
#: A ``cancelling`` mark older than this is from an interrupted cancel; restore ignores it.
CANCELLING_STALE_S = 300
_REASON_MAX = 500
_LIST_MAX = 500
_TENANT_SQL = "org_id IS ?"
_NO_AUTO_RELEASE_CANCEL = (
    "release_policy after_active_runs_finish cannot be combined with cancel: a cancel "
    "hold that released itself could no longer be restored; use release_policy manual"
)


class HoldAuthorityError(ToolError):
    """The caller may not release this hold (HTTP 403)."""


# The identity an HTTP route admitted as the verified human operator (see operator_release).
_operator_grant: ContextVar[str | None] = ContextVar("task_hold_operator_grant", default=None)


@contextmanager
def operator_release(identity: str) -> Iterator[None]:
    """Let one HTTP request release holds as the verified human operator.

    A route enters this only after ``routes._operator_recovery`` held for the request.
    It is honoured only while the verified identity is ``identity`` and that identity
    is the operator label, so it never covers another caller or an MCP call.
    """
    token = _operator_grant.set(identity)
    try:
        yield
    finally:
        _operator_grant.reset(token)


def _is_operator(identity: str | None) -> bool:
    if not identity:
        return False
    if identity in agent_lifecycle.operators():
        return True
    return _operator_grant.get() == identity and identity == config.operator_label()


def _tenant() -> str | None:
    return edition.tenant_org_id()


def _actor(actor: str | None) -> str:
    identity = config.verified_agent_identity()
    if identity:
        return identity
    text = " ".join(str(actor or "").split())
    if not text:
        raise ToolError("actor is required when no verified identity is present")
    return text


def _require_authority() -> None:
    # The route's operator exemption reaches here as declared call authority.
    if not profiles.tool_is_allowed("task_update"):
        raise ToolError("task holds need task_update in the active profile")


def _release_authority(row, who: str) -> bool:
    """Refuse a release by anyone but the creator or an operator; True for an operator."""
    verified = config.verified_agent_identity()
    if verified is None:
        if who == row["created_by"]:
            return False
    elif _is_operator(verified):
        return True
    elif verified == row["created_by"]:
        return False
    raise HoldAuthorityError(
        f"{verified or who!r} may not release hold #{row['id']}: only its creator "
        f"({row['created_by']}) or an operator (UA_LIFECYCLE_OPERATORS, or the verified "
        "operator over HTTP) may"
    )


def _clean_reason(reason: str | None, *, required: bool) -> str:
    text = " ".join(str(reason or "").split())
    if required and not text:
        raise ToolError("a reason is required to hold a subtree")
    if len(text) > _REASON_MAX:
        raise ToolError(f"reason must be at most {_REASON_MAX} characters")
    return text


def _cancelling(row) -> bool:
    """Whether a cancel on this hold is still recording its requests (not stale)."""
    at = row["cancelling_at"]
    if not at:
        return False
    try:
        started = datetime.fromisoformat(str(at))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (datetime.now(UTC) - started).total_seconds() < CANCELLING_STALE_S


# --- reads ----------------------------------------------------------------------


def _hold(conn, hold_id: int):
    return conn.execute(
        f"SELECT * FROM task_holds WHERE id=? AND {_TENANT_SQL}", (int(hold_id), _tenant())
    ).fetchone()


def _active_hold_for_root(conn, root_task_id: int):
    return conn.execute(
        f"SELECT * FROM task_holds WHERE root_task_id=? AND status='active' AND {_TENANT_SQL}",
        (int(root_task_id), _tenant()),
    ).fetchone()


def _member_rows(conn, hold_id: int) -> list[Any]:
    tenant = _tenant()
    return conn.execute(
        "SELECT m.task_id, m.status_at_hold, m.cancel_requested_by_hold, m.cancelled_run_id, "
        "m.cancelled_run_agent, m.restored_at, t.status, t.claimed_by, t.lock_expires_at, "
        "t.current_run_id, t.current_run_agent, t.cancel_requested, t.notes, t.updated_at "
        "FROM task_hold_members m JOIN tasks t ON t.id = m.task_id "
        f"WHERE m.hold_id=? AND t.{_TENANT_SQL} ORDER BY m.task_id",
        (int(hold_id), tenant),
    ).fetchall()


def _optional_rows(conn, sql: str, params: tuple) -> list[Any]:
    """Rows from a table a store may not have (dispatch runs, the node queue)."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []


def _live_run_task_ids(conn, hold_id: int) -> list[int]:
    """Members with live work: a claimed ``doing`` row on a live lease, a live dispatch
    run, or a queued or claimed node item. A NULL lease is not live."""
    now = storage.now()
    tenant = _tenant()
    rows = conn.execute(
        "SELECT t.id FROM task_hold_members m JOIN tasks t ON t.id = m.task_id "
        f"WHERE m.hold_id=? AND t.{_TENANT_SQL} AND t.status='doing' "
        "AND t.claimed_by IS NOT NULL "
        "AND t.lock_expires_at IS NOT NULL AND t.lock_expires_at > ?",
        (int(hold_id), tenant, now),
    ).fetchall()
    live = {int(r["id"]) for r in rows}
    states = sorted(LIVE_STATES)
    rows = _optional_rows(
        conn,
        "SELECT DISTINCT d.task_id FROM dispatch_runs d "
        "JOIN task_hold_members m ON m.task_id = d.task_id "
        f"WHERE m.hold_id=? AND d.state IN ({','.join('?' * len(states))})",
        (int(hold_id), *states),
    )
    live |= {int(r["task_id"]) for r in rows}
    rows = _optional_rows(
        conn,
        "SELECT DISTINCT d.task_id FROM node_dispatch d "
        "JOIN task_hold_members m ON m.task_id = d.task_id "
        "WHERE m.hold_id=? AND d.status IN ('queued', 'claimed')",
        (int(hold_id),),
    )
    live |= {int(r["task_id"]) for r in rows}
    return sorted(live)


def _serialize(conn, row) -> dict[str, Any]:
    members = [
        {
            "task_id": int(m["task_id"]),
            "status_at_hold": m["status_at_hold"],
            "status": m["status"],
            "cancel_requested_by_hold": bool(m["cancel_requested_by_hold"]),
            "cancelled_run_id": m["cancelled_run_id"],
            "restored_at": m["restored_at"],
        }
        for m in _member_rows(conn, row["id"])
    ]
    return {
        "id": int(row["id"]),
        "root_task_id": int(row["root_task_id"]),
        "mode": row["mode"],
        "status": row["status"],
        "reason": row["reason"],
        "release_policy": row["release_policy"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "released_by": row["released_by"],
        "released_at": row["released_at"],
        "cancelling": row["status"] == "active" and _cancelling(row),
        "member_ids": [m["task_id"] for m in members],
        "members": members,
        "live_run_task_ids": _live_run_task_ids(conn, row["id"])
        if row["status"] == "active"
        else [],
    }


# --- writes (caller owns the transaction) ----------------------------------------


def _record(conn, who: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Append the hold event inside the caller's write transaction; publish after commit."""
    _ulid, _inserted, pending = events.record_event_once_on_connection(
        conn,
        CHANNEL,
        payload={"kind": KIND, **payload, "actor": who, "at": storage.now()},
        originator=who,
    )
    return pending


def publish(pending: list[dict[str, Any] | None]) -> None:
    """Publish hold events recorded in a transaction that has now committed."""
    for event in pending:
        if event is not None:
            events.publish(event)


def _release_in_tx(conn, row, who: str, action: str, reason: str, **extra) -> dict | None:
    cur = conn.execute(
        "UPDATE task_holds SET status='released', released_by=?, released_at=? "
        f"WHERE id=? AND status='active' AND {_TENANT_SQL}",
        (who, storage.now(), int(row["id"]), _tenant()),
    )
    if cur.rowcount != 1:
        raise ToolError(f"hold #{row['id']} changed while it was being released; retry")
    return _record(
        conn,
        who,
        {
            "action": action,
            "hold_id": int(row["id"]),
            "root_task_id": int(row["root_task_id"]),
            "reason": reason or None,
            **extra,
        },
    )


def _settle_in_tx(conn, rows) -> list[dict[str, Any] | None]:
    """Release each ``after_active_runs_finish`` hold in ``rows`` whose members are idle."""
    pending = []
    for row in rows:
        if row["status"] != "active" or row["release_policy"] != "after_active_runs_finish":
            continue
        if _live_run_task_ids(conn, row["id"]):
            continue
        pending.append(
            _release_in_tx(
                conn,
                row,
                AUTO_RELEASE_ACTOR,
                "auto_release",
                "no member has a live run (release_policy after_active_runs_finish)",
            )
        )
    return pending


def _due_holds(conn) -> list[Any]:
    rows = conn.execute(
        "SELECT * FROM task_holds WHERE status='active' "
        f"AND release_policy='after_active_runs_finish' AND {_TENANT_SQL}",
        (_tenant(),),
    ).fetchall()
    return [r for r in rows if not _live_run_task_ids(conn, r["id"])]


def _settle_due() -> None:
    """Auto-release every due hold: the read-time check.

    Takes the write lock only when a hold is due.
    """
    conn = storage.connect()
    try:
        if not _due_holds(conn):
            return
        conn.execute("BEGIN IMMEDIATE")
        pending = _settle_in_tx(conn, _due_holds(conn))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    publish(pending)


# --- the claim chokepoint ----------------------------------------------------------


def claim_block(
    conn,
    task_id: int,
    *,
    renewal: bool = False,
    pending: list[dict[str, Any] | None] | None = None,
    resume: bool = False,
) -> str | None:
    """Why ``task_id`` may not be newly claimed or dispatched right now, or None.

    ``renewal`` is a live renewal of the caller's own claim, which a hold never
    refuses (see the module docstring). A due ``after_active_runs_finish`` hold is
    released here. When ``conn`` already has a write transaction (the claim's own) the
    release joins it and its events are appended to ``pending``, which the caller
    publishes (:func:`publish`) once its transaction commits; otherwise this call
    opens, commits and publishes a transaction of its own. A store without the hold
    tables holds nothing.

    Interrupted work the lease reaper holds is refused here too (task #2482), after
    the subtree holds. ``resume`` lets that one hold pass: only ``task_claim`` sets
    it, and only for a claim that executes no queued delegation, the explicit act
    that resumes the work. It never passes a subtree hold.
    """
    if renewal:
        return None
    held = _subtree_block(conn, task_id, pending)
    if held or resume:
        return held
    return lease_reaper.interrupted_hold(conn, task_id)


def _subtree_block(conn, task_id: int, pending: list[dict[str, Any] | None] | None) -> str | None:
    """The active subtree hold over ``task_id``, after releasing any that are due."""
    try:
        rows = conn.execute(
            "SELECT h.* FROM task_holds h JOIN task_hold_members m ON m.hold_id = h.id "
            f"WHERE m.task_id=? AND h.status='active' AND h.{_TENANT_SQL} ORDER BY h.id",
            (int(task_id), _tenant()),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if not rows:
        return None
    due = [
        r
        for r in rows
        if r["release_policy"] == "after_active_runs_finish"
        and not _live_run_task_ids(conn, r["id"])
    ]
    if due:
        owns_tx = not conn.in_transaction
        if owns_tx:
            conn.execute("BEGIN IMMEDIATE")
        try:
            released = _settle_in_tx(conn, due)
            if owns_tx:
                conn.commit()
        except BaseException:
            if owns_tx:
                conn.rollback()
            raise
        if owns_tx:
            publish(released)
        elif pending is not None:
            pending.extend(released)
    due_ids = {int(r["id"]) for r in due}
    for row in rows:
        if int(row["id"]) in due_ids:
            continue
        why = f": {row['reason']}" if row["reason"] else ""
        return (
            f"task {task_id} is held by hold #{row['id']} on task #{row['root_task_id']} "
            f"({row['mode']}, by {row['created_by']}){why}; new claims are refused until "
            "the hold is released (coordination_manage task_hold mode=resume)"
        )
    return None


# --- the action ----------------------------------------------------------------------


def _visible_root(conn, root_task_id: int):
    return conn.execute(
        f"SELECT id, status FROM tasks WHERE id=? AND {_TENANT_SQL}",
        (int(root_task_id), _tenant()),
    ).fetchone()


def _create_in_tx(conn, root_task_id: int, mode: str, reason: str, policy: str, who: str):
    from .tools import tasks as tasks_tool

    subtree = tasks_tool._subtree_ids(conn, root_task_id)
    cur = conn.execute(
        "INSERT INTO task_holds (root_task_id, mode, status, reason, release_policy, "
        "created_by, created_at, org_id) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
        (int(root_task_id), mode, reason, policy, who, storage.now(), _tenant()),
    )
    hold_id = int(cur.lastrowid)
    placeholders = ",".join("?" * len(subtree))
    statuses = conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({placeholders}) AND {_TENANT_SQL}",
        (*subtree, _tenant()),
    ).fetchall()
    conn.executemany(
        "INSERT INTO task_hold_members (hold_id, task_id, status_at_hold) VALUES (?, ?, ?)",
        [(hold_id, int(r["id"]), r["status"]) for r in statuses],
    )
    return hold_id


def _node_items(conn, task_ids: list[int]) -> list[Any]:
    if not task_ids:
        return []
    return _optional_rows(
        conn,
        "SELECT d.id, d.task_id, d.status, d.created_by, t.claimed_by FROM node_dispatch d "
        "LEFT JOIN tasks t ON t.id = d.task_id "
        f"WHERE d.task_id IN ({','.join('?' * len(task_ids))}) "
        "AND d.status IN ('queued', 'claimed') ORDER BY d.task_id, d.id",
        tuple(task_ids),
    )


def _request_cancels(hold_id: int, root_task_id: int, who: str) -> list[dict[str, Any]]:
    """Request cancellation of the live work of the hold's non-terminal members.

    Self-pickup runs go through ``task_update(request_cancel=True)``, node items through
    ``node_queue.cancel``. The hold is ``cancelling`` until the outcomes are recorded.
    """
    from . import node_queue  # lazy: a declared seam (docs/architecture/layers.toml)
    from .tools import tasks as tasks_tool

    conn = storage.connect()
    try:
        members = [
            m for m in _member_rows(conn, hold_id) if m["status_at_hold"] not in TERMINAL_STATUSES
        ]
        items = _node_items(conn, [int(m["task_id"]) for m in members])
    finally:
        conn.close()
    verified = config.verified_agent_identity()
    outcomes: list[dict[str, Any]] = []
    flagged: list[tuple[str, str | None, int, int]] = []
    try:
        for m in members:
            if m["status"] != "doing" or not m["current_run_id"]:
                continue
            task_id, run_id = int(m["task_id"]), m["current_run_id"]
            base = {"task_id": task_id, "path": "run", "ref": run_id}
            if m["cancel_requested"]:
                outcomes.append(
                    {
                        **base,
                        "control": "already_requested",
                        "detail": "a cancel request was already standing; restore leaves it",
                    }
                )
                continue
            try:
                # The existing cancellation path, with its own authority rule.
                res = tasks_tool.task_update(task_id, request_cancel=True, worktree=False)
            except ToolError as exc:
                outcomes.append({**base, "control": "refused", "detail": str(exc)[:300]})
                continue
            run = run_records.get_run(run_id) or {}
            if (
                res.get("cancel_requested")
                and res.get("current_run_id") == run_id
                and run.get("cancel_requested_by") == verified
            ):
                flagged.append((run_id, m["current_run_agent"], hold_id, task_id))
                outcomes.append({**base, "control": "requested", "detail": ""})
            else:
                outcomes.append(
                    {
                        **base,
                        "control": "already_requested",
                        "detail": "another caller requested it first; restore leaves it",
                    }
                )
        operator = _is_operator(verified)
        for item in items:
            base = {"task_id": int(item["task_id"]), "path": "node", "ref": item["id"]}
            if not (
                operator or (verified and verified in (item["created_by"], item["claimed_by"]))
            ):
                outcomes.append(
                    {
                        **base,
                        "control": "refused",
                        "detail": "stopping another identity's node item is an operator action "
                        "(UA_LIFECYCLE_OPERATORS)",
                    }
                )
                continue
            try:
                res = node_queue.cancel(item["id"], actor=verified or who)
            except Exception as exc:  # noqa: BLE001 - report, never abort the fan-out
                outcomes.append({**base, "control": "failed", "detail": str(exc)[:300]})
                continue
            outcomes.append(
                {
                    **base,
                    "control": "requested" if res.get("cancel_requested") else "unchanged",
                    "detail": "will not start"
                    if item["status"] == "queued"
                    else "relayed on the node's next heartbeat",
                }
            )
    finally:
        # Always end the cancelling mark, so a failure here never wedges restore.
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "UPDATE task_hold_members SET cancel_requested_by_hold=1, cancelled_run_id=?, "
                "cancelled_run_agent=? WHERE hold_id=? AND task_id=?",
                flagged,
            )
            conn.execute(
                f"UPDATE task_holds SET cancelling_at=NULL WHERE id=? AND {_TENANT_SQL}",
                (hold_id, _tenant()),
            )
            pending = [
                _record(
                    conn,
                    who,
                    {
                        "action": "cancel_requested",
                        "hold_id": hold_id,
                        "root_task_id": int(root_task_id),
                        "outcomes": outcomes,
                    },
                )
            ]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish(pending)
    return outcomes


def _acknowledged_cancel(conn, task_id: int, finish: tuple[str, dict[str, Any]] | None) -> bool:
    """Whether ``finish`` moved the task to cancelled by acknowledging the cancel request,
    and no later status change happened on the task."""
    if finish is None:
        return False
    ulid, payload = finish
    if payload.get("to_status") != "cancelled" or not payload.get("cancel_acknowledged"):
        return False
    later = conn.execute(
        "SELECT 1 FROM event_log WHERE channel=? AND ulid > ? "
        "AND json_extract(payload, '$.task_id')=? LIMIT 1",
        (events.CHANNEL_TASK_UPDATED, ulid, int(task_id)),
    ).fetchone()
    return later is None


def _restore_in_tx(conn, row, who: str, operator: bool) -> tuple[list[dict[str, Any]], bool]:
    """Undo the hold's cancel member by member; return the outcomes and whether all are done."""
    from . import workflow_roles
    from .tools import tasks as tasks_tool

    verified = config.verified_agent_identity()
    out: list[dict[str, Any]] = []
    settled = True
    stamp = storage.now()
    for m in _member_rows(conn, row["id"]):
        if not m["cancel_requested_by_hold"] or m["status_at_hold"] in TERMINAL_STATUSES:
            continue
        task_id, run_id = int(m["task_id"]), m["cancelled_run_id"]
        base = {"task_id": task_id, "run_id": run_id}
        if m["restored_at"]:
            out.append({**base, "control": "restored", "detail": f"at {m['restored_at']}"})
            continue
        if not operator and (verified is None or verified != m["cancelled_run_agent"]):
            # request_cancel's rule: the run's claimant or an operator.
            settled = False
            out.append(
                {
                    **base,
                    "control": "refused",
                    "detail": f"{verified or who!r} may not undo the cancel on "
                    f"{m['cancelled_run_agent']!r}'s run: its claimant or an operator may",
                }
            )
            continue
        finish = run_records.finish_event_on(conn, run_id) if run_id else None
        if run_id and finish is None:
            settled = False
            out.append(
                {
                    **base,
                    "control": "pending",
                    "detail": "run still stopping; restore again once it closes",
                }
            )
            continue
        # Clearing the flag is a task change: stamp a revision (task #2087).
        cleared = conn.execute(
            "UPDATE tasks SET cancel_requested=0, updated_at=? WHERE id=? AND cancel_requested=1 "
            f"AND (current_run_id IS NULL OR current_run_id IS ?) AND {_TENANT_SQL}",
            (storage.next_task_revision(conn, task_id), task_id, run_id, _tenant()),
        ).rowcount
        reopened = 0
        failed_workflow = workflow_roles.is_failed_frontier(conn, task_id)
        if (
            not failed_workflow
            and m["status"] == "cancelled"
            and _acknowledged_cancel(conn, task_id, finish)
        ):
            line = (
                f"[hold #{row['id']} restored @ {stamp} by {who}] reopened: the hold's "
                "cancel request had cancelled it"
            )
            notes = f"{(m['notes'] or '').rstrip()}\n{line}".lstrip("\n")
            reopened = conn.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
                "lock_expires_at=NULL, notes=?, updated_at=? "
                f"WHERE id=? AND status='cancelled' AND {_TENANT_SQL}",
                (notes, tasks_tool._next_task_revision(m["updated_at"]), task_id, _tenant()),
            ).rowcount
        conn.execute(
            "UPDATE task_hold_members SET restored_at=? WHERE hold_id=? AND task_id=?",
            (stamp, int(row["id"]), task_id),
        )
        if failed_workflow:
            out.append({**base, "control": "retained", "detail": "workflow_failed"})
        elif reopened:
            out.append({**base, "control": "reopened"})
        elif cleared:
            out.append({**base, "control": "cancel_cleared"})
        else:
            out.append({**base, "control": "unchanged", "status": m["status"]})
    return out, settled


def _after_write(task_ids: list[int]) -> None:
    from .change_feed import task_changed
    from .sync import schedule_sync_to_vault

    schedule_sync_to_vault()
    for task_id in task_ids:
        task_changed(task_id)


def task_hold(
    root_task_id: int,
    mode: str = "pause",
    reason: str = "",
    release_policy: str = "manual",
    actor: str | None = None,
    hold_id: int | None = None,
) -> dict[str, Any]:
    """Hold, release, cancel or restore a task subtree in one action (task #1747).

    ``mode``: ``pause`` (reason required) holds ``root_task_id`` and its subtree as
    snapshotted now; ``cancel`` (reason required) also requests cancellation of every
    member's live self-pickup run and queued or claimed node item; ``resume`` releases;
    ``restore`` undoes the hold's own cancel requests and releases once all are undone
    (a member whose cancelled run is still open is reported ``pending`` and the hold
    stays active: restore again once the run has closed).
    ``release_policy`` (pause only): ``manual`` or ``after_active_runs_finish``, which
    needs a live run in the subtree. Re-holding an already-held root returns the
    existing hold (``changed: false``); ``cancel`` on a paused hold escalates it.
    Releasing needs the hold's creator, a lifecycle operator, or the verified operator
    over HTTP. ``hold_id``, when given, must name the root's active hold. ``actor`` is
    used only without a verified identity. Needs ``task_update`` in the active profile.
    """
    verb = str(mode or "").strip().lower()
    if verb not in MODES:
        raise ToolError(f"mode must be one of {MODES}")
    policy = str(release_policy or "manual").strip().lower()
    if policy not in RELEASE_POLICIES:
        raise ToolError(f"release_policy must be one of {RELEASE_POLICIES}")
    if verb == "cancel" and policy == "after_active_runs_finish":
        raise ToolError(_NO_AUTO_RELEASE_CANCEL)
    _require_authority()
    who = _actor(actor)
    text = _clean_reason(reason, required=verb in ("pause", "cancel"))
    storage.require_live_task_store()
    pending: list[dict[str, Any] | None] = []
    changed = True
    released = verb in ("resume", "restore")
    restored: list[dict[str, Any]] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _visible_root(conn, root_task_id) is None:
            raise ToolError(f"no task with id {root_task_id}")
        existing = _active_hold_for_root(conn, root_task_id)
        if hold_id is not None and (existing is None or int(existing["id"]) != int(hold_id)):
            raise ToolError(f"hold #{hold_id} is not the active hold on task #{root_task_id}")
        if verb in ("pause", "cancel"):
            if existing is not None and (verb == "pause" or existing["mode"] == "cancel"):
                changed = False
                hold_id = int(existing["id"])
            elif existing is not None:
                if existing["release_policy"] == "after_active_runs_finish":
                    raise ToolError(_NO_AUTO_RELEASE_CANCEL)
                hold_id = int(existing["id"])
                conn.execute(
                    f"UPDATE task_holds SET mode='cancel', cancelling_at=? WHERE id=? "
                    f"AND {_TENANT_SQL}",
                    (storage.now(), hold_id, _tenant()),
                )
            else:
                hold_id = _create_in_tx(conn, root_task_id, verb, text, policy, who)
                if verb == "cancel":
                    conn.execute(
                        "UPDATE task_holds SET cancelling_at=? WHERE id=?",
                        (storage.now(), hold_id),
                    )
                if policy == "after_active_runs_finish" and not _live_run_task_ids(conn, hold_id):
                    raise ToolError(
                        f"nothing is running in task #{root_task_id}'s subtree, so "
                        "release_policy after_active_runs_finish would release the hold at "
                        "once; hold it with release_policy manual and release it by hand"
                    )
            if changed:
                members = [m["task_id"] for m in _member_rows(conn, hold_id)]
                pending.append(
                    _record(
                        conn,
                        who,
                        {
                            "action": verb,
                            "hold_id": hold_id,
                            "root_task_id": int(root_task_id),
                            "reason": text,
                            "release_policy": policy if existing is None else None,
                            "members": members,
                            "escalated": existing is not None,
                        },
                    )
                )
        else:
            if existing is None:
                raise ToolError(f"task {root_task_id} has no active hold")
            hold_id = int(existing["id"])
            operator = _release_authority(existing, who)
            if _cancelling(existing):
                # Releasing mid fan-out would leave cancel requests on a released
                # hold that no restore could undo.
                raise ToolError(
                    f"hold #{hold_id} is still recording its cancel requests; "
                    f"{verb} once that finishes"
                )
            if verb == "restore":
                restored, released = _restore_in_tx(conn, existing, who, operator)
            if released:
                pending.append(
                    _release_in_tx(conn, existing, who, verb, text, restored=restored or None)
                )
            else:
                pending.append(
                    _record(
                        conn,
                        who,
                        {
                            "action": "restore_pending",
                            "hold_id": hold_id,
                            "root_task_id": int(root_task_id),
                            "reason": text or None,
                            "restored": restored,
                        },
                    )
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    publish(pending)
    result: dict[str, Any] = {"action": verb, "changed": changed}
    if verb == "cancel" and changed:
        result["cancel_requests"] = _request_cancels(hold_id, root_task_id, who)
    if verb in ("resume", "restore"):
        result["released"] = released
    if restored:
        result["restored"] = restored
        result["pending"] = [
            r["task_id"] for r in restored if r["control"] in ("pending", "refused")
        ]
        _after_write(
            [r["task_id"] for r in restored if r["control"] in ("reopened", "cancel_cleared")]
        )
    result["hold"] = _get(hold_id)
    return result


def task_holds(
    root_task_id: int | None = None, status: str = "active", limit: int = 200
) -> dict[str, Any]:
    """Holds in the caller's tenant, newest first, each with its members and live runs.

    ``status``: ``active`` (default), ``released`` or ``all``. ``root_task_id`` narrows
    to one root. Due ``after_active_runs_finish`` holds are released before reading.
    """
    view = str(status or "active").strip().lower()
    if view not in ("active", "released", "all"):
        raise ToolError("status must be one of ('active', 'released', 'all')")
    cap = max(1, min(int(limit), _LIST_MAX))
    _settle_due()
    where, params = [_TENANT_SQL], [_tenant()]
    if view != "all":
        where.append("status=?")
        params.append(view)
    if root_task_id is not None:
        where.append("root_task_id=?")
        params.append(int(root_task_id))
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM task_holds WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, cap + 1),
        ).fetchall()
        holds = [_serialize(conn, r) for r in rows[:cap]]
    finally:
        conn.close()
    return {"holds": holds, "truncated": len(rows) > cap}


def task_hold_get(hold_id: int) -> dict[str, Any]:
    """One hold by id, with its members and live runs (another tenant's reads as missing).

    A due ``after_active_runs_finish`` hold is released before reading.
    """
    _settle_due()
    return _get(hold_id)


def _get(hold_id: int) -> dict[str, Any]:
    conn = storage.connect()
    try:
        row = _hold(conn, hold_id)
        if row is None:
            raise ToolError(f"no task hold with id {hold_id}")
        return _serialize(conn, row)
    finally:
        conn.close()
