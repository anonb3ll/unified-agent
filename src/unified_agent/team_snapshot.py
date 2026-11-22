"""Team snapshot and restore (task #1738).

After a reboot, a deploy or the end of the day, the operator restores a whole
team in one action and sees, per seat, what actually happened.

**Snapshot.** For each seat (a committed team spec's seats, resolved to the
seat's latest launched node item, or an explicit set of node items): label,
node, harness backend, model, working directory, claimed task and node item,
the harness session id, whether that session can be reopened (ACP
``session/resume``/``session/load`` or the harness ``--resume`` flag, as
:mod:`node_resume` decides), and the task's bounded session packet. It is one
append-only ``agent.team.snapshot`` event: no transcript text beyond the
bounded packet, with credential shapes redacted.

**Preview** is read-only and lists the planned outcome per seat:

``resumed``  the recorded harness session can be reopened on its node;
``rebuilt``  it never could, but a session packet exists: a fresh session
             starts with the packet as its prompt;
``fresh``    neither: a fresh session starts in the same place;
``failed``   the seat cannot be restored (offline node, a lease that expired
             with an unconfirmed outcome, a claim another agent holds, a seat
             that belongs to another identity, no recorded session, or a
             session that was resumable when the snapshot was taken and is not
             now), with the reason;
``live``     the seat's session, a resume of it, or the session an earlier
             restore started for it is still queued or running: nothing to do.

Only a seat whose session was never resumable may be rebuilt or started fresh;
a seat recorded as resumable is resumed or reported failed. Unless the caller
is the verified human operator (the HTTP recovery channel only), the caller
must be the seat's owner (the identity that started the node item, or the
harness session's attributed identity) or the task's current claimant, and a
task claimed by anyone else, live or expired, fails the seat. Preview applies
the same checks, so it plans what restore will do.

**Restore** carries the plan out through the existing paths, never a new one:
:func:`node_resume.resume` for ``resumed`` and :func:`node_queue.request_dispatch`
for ``rebuilt``/``fresh``. Both keep their approval gate, admission checks and
claim rules. A seat planned as ``resumed`` whose resume fails reports
``failed``; it never silently falls back to a fresh session. A seat whose
resume or dispatch waits for its approval reports ``awaiting_approval`` with
the approval id. A seat is always restored on the node it was recorded on:
moving it to another host is not offered. The outcome is recorded as one
``agent.team.restore`` event.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage
from .telemetry.redaction import redact_sensitive_content

SNAPSHOT_CHANNEL = "agent.team.snapshot"
RESTORE_CHANNEL = "agent.team.restore"
PLANNED = ("resumed", "rebuilt", "fresh", "failed", "live")
#: Restore outcomes: the planned ones plus a seat whose dispatch waits for approval.
OUTCOMES = (*PLANNED, "awaiting_approval")
MAX_SEATS = 50
MAX_PACKET_BYTES = 8_000
RESUME_PROMPT = (
    "Your session was restored after a stop. Continue the work you were doing; "
    "check the task and its notes first."
)


def _actor() -> str:
    actor = config.verified_agent_identity()
    if not actor:
        raise ToolError("team snapshots need a verified identity")
    return actor


def _bounded_packet(task_id: int | None) -> dict[str, Any] | None:
    """The task's session packet, redacted and bounded; None when there is none."""
    if task_id is None:
        return None
    from .lifecycle.packet import build_session_packet

    try:
        packet = build_session_packet(task_id=int(task_id))
    except Exception:  # noqa: BLE001 - a missing or foreign task has no packet
        return None
    encoded = redact_sensitive_content(json.dumps(packet, default=str), max_len=10**7)
    if _size(encoded) <= MAX_PACKET_BYTES:
        return json.loads(encoded)
    return _trimmed_packet(json.loads(encoded))


#: Sections a too-large packet sheds first, in order; the task and any handoff stay.
_PACKET_OPTIONAL = (
    "memory",
    "experience",
    "recent_events",
    "approvals",
    "delegations",
    "lifecycle",
    "why_chain",
    "operating_profile",
    "session",
    "worktree",
)
_PACKET_KEPT = ("schema", "labels", "generated_at", "truncated", "task", "prior_handoff")
_TASK_KEYS = ("id", "title", "status", "priority", "claimed_by", "branch", "notes")
#: Successive string bounds for the task and handoff once every optional section is gone.
_STRING_BOUNDS = (2_000, 1_000, 500, 200)


def _size(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return len(text.encode("utf-8"))


def _tail(value: Any, limit: int) -> Any:
    """Every string in ``value`` cut to its last ``limit`` characters (notes grow at the end)."""
    if isinstance(value, str):
        return value if len(value) <= limit else "…" + value[-limit:]
    if isinstance(value, dict):
        return {key: _tail(inner, limit) for key, inner in value.items()}
    if isinstance(value, list):
        return [_tail(inner, limit) for inner in value[:20]]
    return value


def _trimmed_packet(whole: dict[str, Any]) -> dict[str, Any]:
    """Trim an already-redacted packet to the byte cap, keeping the task context.

    Optional sections go first, then long strings in the task and handoff are cut
    to a bounded tail. ``schema``, ``generated_at``, the task's id, title and status
    always survive, and the packet says it was truncated.
    """
    packet = {**whole, "truncated": True}
    task = packet.get("task")
    if isinstance(task, dict):
        packet["task"] = {key: task.get(key) for key in _TASK_KEYS if key in task}
    optional = [key for key in _PACKET_OPTIONAL if key in packet]
    optional += [key for key in packet if key not in _PACKET_KEPT and key not in optional]
    for key in optional:
        if _size(packet) <= MAX_PACKET_BYTES:
            return packet
        packet.pop(key, None)
    for bound in _STRING_BOUNDS:
        if _size(packet) <= MAX_PACKET_BYTES:
            return packet
        for key in ("task", "prior_handoff"):
            if packet.get(key) is not None:
                packet[key] = _tail(packet[key], bound)
    if _size(packet) <= MAX_PACKET_BYTES:
        return packet
    # Last resort: the handoff alone is too large; the task core always fits.
    core = {key: (task or {}).get(key) for key in ("id", "title", "status")}
    return {
        "schema": packet.get("schema"),
        "labels": packet.get("labels"),
        "generated_at": packet.get("generated_at"),
        "truncated": True,
        "task": _tail(core, 200) if isinstance(task, dict) else None,
    }


def _claimant(task_id: int | None) -> tuple[str | None, str | None]:
    if task_id is None:
        return None, None
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT claimed_by, lock_expires_at FROM tasks WHERE id=? AND org_id IS ?",
            (int(task_id), edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return (row["claimed_by"], row["lock_expires_at"]) if row else (None, None)


def _seat(label: str | None, item_id: str | None) -> dict[str, Any]:
    from . import node_queue, node_resume

    item = node_queue.get_item(item_id) if item_id else None
    if item is None:
        return {"label": label, "item_id": item_id, "recorded": False}
    item["outcome_unknown"] = _outcome_unknown(item, datetime.now(UTC))
    node_resume.annotate_items([item])
    resume = item.get("resume") or {}
    return {
        "label": label or item.get("profile_agent") or item.get("agent"),
        "recorded": True,
        "item_id": item["id"],
        "item_status": item.get("status"),
        "created_by": item.get("created_by"),
        "node": item.get("node"),
        "backend": item.get("backend"),
        "model": item.get("model"),
        "cwd": item.get("cwd"),
        # The checkout real path the item was bound to (#1770); a restore
        # re-binds it so a retargeted checkout is refused at launch.
        "cwd_realpath": item.get("cwd_realpath"),
        "permission_mode": item.get("permission_mode"),
        "hermes_profile": item.get("hermes_profile"),
        "profile_agent": item.get("profile_agent"),
        "timeout_s": item.get("timeout_s"),
        "task_id": item.get("task_id"),
        "session_id": item.get("session_id"),
        "resume": {
            "available": bool(resume.get("available")),
            "mode": resume.get("mode"),
            "reason": resume.get("reason"),
        },
        "packet": _bounded_packet(item.get("task_id")),
    }


def snapshot(
    *, team: str | None = None, item_ids: list[str] | None = None, reason: str = "on_demand"
) -> dict[str, Any]:
    """Capture every seat of ``team`` (or the given node items) as one event."""
    actor = _actor()
    if (team is None) == (not item_ids):
        raise ToolError("pass exactly one of team or item_ids")
    seats: list[dict[str, Any]] = []
    if team is not None:
        from . import team_spec
        from .team_policy import load_specs

        spec = load_specs().get(str(team))
        if spec is None:
            raise ToolError(f"no committed team spec named {team!r}")
        for seat in spec["seats"][:MAX_SEATS]:
            run = team_spec._latest_run(seat["label"], spec["team"]) or {}
            seats.append(_seat(seat["label"], run.get("item_id")))
    else:
        ids = [str(i).strip() for i in item_ids or [] if str(i).strip()]
        if len(ids) > MAX_SEATS:
            raise ToolError(f"a snapshot holds at most {MAX_SEATS} seats")
        seats = [_seat(None, item_id) for item_id in dict.fromkeys(ids)]
    payload = {
        "team": team,
        "reason": str(reason)[:80],
        "by": actor,
        "taken_at": storage.now(),
        "seats": seats,
    }
    snapshot_id = events.record_event(SNAPSHOT_CHANNEL, payload=payload, originator=actor)
    return {"snapshot_id": snapshot_id, **payload}


def get_snapshot(snapshot_id: str) -> dict[str, Any]:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, payload FROM event_log WHERE ulid=? AND channel=? AND org_id IS ?",
            (str(snapshot_id), SNAPSHOT_CHANNEL, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no team snapshot {snapshot_id!r}")
    return {"snapshot_id": row["ulid"], **json.loads(row["payload"])}


def list_snapshots(limit: int = 20) -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid, payload FROM event_log WHERE channel=? AND org_id IS ? "
            "ORDER BY ROWID DESC LIMIT ?",
            (SNAPSHOT_CHANNEL, edition.tenant_org_id(), max(1, min(int(limit), 100))),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        out.append(
            {
                "snapshot_id": row["ulid"],
                "team": payload.get("team"),
                "taken_at": payload.get("taken_at"),
                "by": payload.get("by"),
                "seats": len(payload.get("seats") or []),
            }
        )
    return out


RECONCILE_REASON = (
    "the item's lease expired with an unconfirmed outcome; reconcile it before restoring"
)


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _outcome_unknown(item: dict[str, Any] | None, now: datetime) -> bool:
    """The Sessions pane's reconcile rule: a lapsed lease leaves the outcome unconfirmed.

    A claimed item whose lease has passed (the hub has not failed it yet), or one
    the hub failed for exactly that reason, may still be running on its node.
    """
    if item is None:
        return False
    receipt = item.get("receipt") or {}
    if (
        isinstance(receipt, dict)
        and receipt.get("hub_generated")
        and receipt.get("reason") == "lease_expired_outcome_unknown"
    ):
        return True
    lease = _when(item.get("lease_expires_at"))
    return item.get("status") == "claimed" and lease is not None and lease < now


def _successors() -> dict[str, str]:
    """Per recorded node item, the newest item a restore queued for it.

    Read from this tenant's recent ``agent.team.restore`` events, newest first, so
    restoring the same seat again (from this snapshot or another that recorded
    the same item) sees the work an earlier restore already started.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? AND org_id IS ? "
            "ORDER BY ROWID DESC LIMIT 200",
            (RESTORE_CHANNEL, edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for row in rows:
        for seat in json.loads(row["payload"]).get("seats") or []:
            item_id, successor = seat.get("item_id"), seat.get("new_item_id")
            if item_id and successor:
                out.setdefault(str(item_id), str(successor))
    return out


def _resume_busy(session_id: str | None) -> bool:
    """A queued or claimed item already reopens this harness session."""
    from . import node_resume

    if not session_id:
        return False
    conn = storage.connect()
    try:
        return node_resume._open_resume_item(conn, str(session_id)) is not None
    finally:
        conn.close()


def _owners(item: dict[str, Any] | None, seat: dict[str, Any]) -> set[str]:
    """Identities the seat's work is attributed to, as :func:`node_resume.resume` decides.

    The harness session's attributed identity, and the identity that started the
    node item (never for an adopted session, which stays with its adopter).
    """
    from . import node_resume

    starter = (item if item is not None else seat).get("created_by")
    sid = seat.get("session_id")
    row = None
    if sid:
        conn = storage.connect()
        try:
            row = node_resume._session_row(conn, str(sid))
        finally:
            conn.close()
    if row is None:
        return {starter} - {None}
    native = str(row.get("kind") or "").endswith("_native")
    return {row.get("agent"), None if native else starter} - {None}


def _plan_seat(
    seat: dict[str, Any],
    actor: str,
    now: datetime,
    *,
    operator: bool = False,
    successors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The outcome restore would produce for one seat; reads only.

    ``successors`` maps a recorded item to the newest item an earlier restore
    queued for it (:func:`_successors`), so a repeated restore never starts the
    same seat twice.
    """
    from . import node_queue, node_resume
    from .node_registry import list_nodes
    from .team_spec import _live

    def plan(outcome: str, reason: str | None = None) -> dict[str, Any]:
        return {**_public(seat), "planned": outcome, "reason": reason}

    def is_open(row: dict[str, Any] | None) -> bool:
        return row is not None and row.get("status") not in node_queue.TERMINAL_ITEM_STATUSES

    if not seat.get("recorded"):
        return plan("failed", "no session was recorded for this seat")
    item = node_queue.get_item(seat["item_id"])
    # Node liveness first: an item claimed by a node that went offline stays
    # claimed in the queue, and is not live work.
    node = list_nodes().get(str(seat.get("node")))
    if node is None or not _live(node, now):
        reason = f"node {seat.get('node')} is offline"
        if is_open(item):
            reason += f"; item {item['id']} is still recorded as {item.get('status')}"
        return plan("failed", reason)
    if _outcome_unknown(item, now):
        return plan("failed", RECONCILE_REASON)
    if is_open(item):
        return plan("live", "the session is still running")
    successor_id = (successors or {}).get(str(seat["item_id"]))
    successor = node_queue.get_item(successor_id) if successor_id else None
    if _outcome_unknown(successor, now):
        return plan(
            "failed",
            f"a previous restore's item {successor_id} lease expired with an unconfirmed "
            "outcome; reconcile it before restoring",
        )
    if is_open(successor):
        return plan("live", "a previous restore's session is still queued or running")
    if _resume_busy(seat.get("session_id")):
        return plan("live", "a resume of this session is already queued or running")
    claimant, _ = _claimant(seat.get("task_id"))
    owners = _owners(item, seat)
    if not operator:
        # The same rule as resume: no identity takeover. A claim held by anyone
        # else fails the seat, even an expired one (only its claimant or the
        # operator may treat it as released).
        if claimant and claimant != actor:
            return plan("failed", f"task #{seat['task_id']} is claimed by {claimant}")
        if actor not in owners and claimant != actor:
            owner = ", ".join(sorted(owners)) or "an unrecorded identity"
            return plan(
                "failed",
                f"the seat belongs to {owner}; only that identity, the task's claimant "
                "or the operator may restore it",
            )
    recorded_resumable = bool((seat.get("resume") or {}).get("available"))
    if item is not None:
        node_resume.annotate_items([item])
        resume = item.get("resume") or {}
        if resume.get("available"):
            if not operator and actor not in owners:
                # Resume reopens another identity's session; node_resume admits
                # only its owners or the operator, never the task's claimant.
                owner = ", ".join(sorted(owners)) or "an unrecorded identity"
                return plan(
                    "failed",
                    f"the session belongs to {owner}; only that identity or the "
                    "operator may resume it",
                )
            return plan("resumed", resume.get("label"))
        if recorded_resumable:
            # Never a silent fallback: a seat that could be reopened is reopened or fails.
            return plan("failed", f"the session can no longer be resumed: {resume.get('reason')}")
    elif recorded_resumable:
        return plan("failed", "the session can no longer be resumed: its node item is gone")
    if seat.get("packet"):
        return plan("rebuilt", "starts a fresh session with the task's session packet")
    return plan("fresh", "starts a fresh session in the recorded place")


def _public(seat: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "label",
        "node",
        "backend",
        "model",
        "cwd",
        "task_id",
        "item_id",
        "session_id",
    )
    public = {key: seat.get(key) for key in keys}
    public["cwd_realpath"] = _bound_realpath(seat)
    return public


def _bound_realpath(seat: dict[str, Any]) -> str | None:
    """The checkout real path a restore must bind (#1770/#2011).

    A snapshot recorded before seats carried ``cwd_realpath`` falls back to
    the original node item's binding, so an older snapshot never restores a
    bound seat unbound.
    """
    if seat.get("cwd_realpath"):
        return seat["cwd_realpath"]
    if "cwd_realpath" in seat or not seat.get("item_id"):
        return None
    from . import node_queue

    item = node_queue.get_item(seat["item_id"])
    return (item or {}).get("cwd_realpath")


def preview(snapshot_id: str) -> dict[str, Any]:
    """Planned outcome per seat for the calling agent. Never changes anything."""
    return preview_for(snapshot_id, operator=False)


def preview_for(snapshot_id: str, *, operator: bool) -> dict[str, Any]:
    """:func:`preview` for a caller whose operator standing the HTTP route verified.

    Not bound to any MCP action: ``operator`` comes only from
    ``routes._operator_recovery``.
    """
    actor = _actor()
    snap = get_snapshot(snapshot_id)
    now = datetime.now(UTC)
    successors = _successors()
    seats = [
        _plan_seat(seat, actor, now, operator=operator, successors=successors)
        for seat in snap.get("seats") or []
    ]
    return {"snapshot_id": snap["snapshot_id"], "team": snap.get("team"), "seats": seats}


def _packet_prompt(seat: dict[str, Any]) -> str:
    packet = seat.get("packet") or {}
    task = packet.get("task") if isinstance(packet.get("task"), dict) else {}
    task_id = task.get("id") or seat.get("task_id")
    heading = ""
    if task_id:
        title = f": {task['title']}" if task.get("title") else ""
        heading = f"Task #{task_id}{title}\n\n"
    # The packet is already bounded to MAX_PACKET_BYTES as compact JSON; the task
    # line comes first so no cut can lose it.
    return (
        "Your session was restored after a stop and could not be reopened, so this is a "
        "fresh session. The task's session packet follows; continue from it.\n\n"
        + heading
        + json.dumps(packet, default=str)[:MAX_PACKET_BYTES]
    )


def _fresh_prompt(seat: dict[str, Any]) -> str:
    task = f" on task #{seat['task_id']}" if seat.get("task_id") else ""
    return f"Your session was restored after a stop as a fresh session. Continue the work{task}."


def restore(snapshot_id: str) -> dict[str, Any]:
    """Carry out the preview's plan for the calling agent; honest outcome per seat.

    Restoring dispatches work on devices, so the caller's profile needs dispatch
    authority (``delegation_manage`` ``delegate``), as the HTTP route requires.
    """
    from . import profiles

    if not profiles.consolidated_action_is_allowed("delegation_manage", "delegate"):
        raise ToolError("team_restore needs dispatch authority (delegation_manage delegate)")
    return restore_for(snapshot_id, operator=False)


def restore_for(snapshot_id: str, *, operator: bool) -> dict[str, Any]:
    """:func:`restore` for a caller whose operator standing the HTTP route verified.

    Not bound to any MCP action: ``operator`` comes only from
    ``routes._operator_recovery``.
    """
    from . import node_queue, node_resume
    from .node_registry import NodeError
    from .tools.approvals import ApprovalPendingError

    actor = _actor()
    snap = get_snapshot(snapshot_id)
    now = datetime.now(UTC)
    by_item = {seat.get("item_id"): seat for seat in snap.get("seats") or []}
    results: list[dict[str, Any]] = []
    successors = _successors()
    plans = (
        _plan_seat(seat, actor, now, operator=operator, successors=successors)
        for seat in snap.get("seats") or []
    )
    for planned in plans:
        seat = by_item.get(planned.get("item_id")) or {}
        outcome = planned["planned"]
        result: dict[str, Any] = {**planned, "outcome": outcome}
        if outcome in {"failed", "live"}:
            results.append(result)
            continue
        try:
            if outcome == "resumed":
                queued = node_resume.resume(
                    seat["item_id"], prompt=RESUME_PROMPT, actor=actor, operator=operator
                )
            else:
                queued = node_queue.request_dispatch(
                    seat["node"],
                    actor=actor,
                    prompt=_packet_prompt(seat) if outcome == "rebuilt" else _fresh_prompt(seat),
                    cwd=seat["cwd"],
                    cwd_realpath=_bound_realpath(seat),
                    backend=seat["backend"],
                    model=seat.get("model"),
                    permission_mode=seat.get("permission_mode"),
                    timeout_s=seat.get("timeout_s"),
                    task_id=seat.get("task_id"),
                    hermes_profile=seat.get("hermes_profile"),
                    expected_profile_agent=seat.get("profile_agent"),
                )
            result["new_item_id"] = (queued or {}).get("id") or (queued or {}).get("item_id")
        except ApprovalPendingError as exc:
            # Queuing work on a device is approval-gated; the seat waits for it
            # and is not restored yet. ``planned`` still says what it will be.
            result["outcome"] = "awaiting_approval"
            result["awaiting_approval"] = exc.approval_id
        except (NodeError, ToolError, ValueError) as exc:
            # Never a silent fresh fallback: a failed resume is a failed seat.
            result["outcome"] = "failed"
            result["reason"] = str(exc)[:300]
        results.append(result)
    summary = {outcome: sum(1 for r in results if r["outcome"] == outcome) for outcome in OUTCOMES}
    events.record_event(
        RESTORE_CHANNEL,
        payload={
            "snapshot_id": snap["snapshot_id"],
            "team": snap.get("team"),
            "by": actor,
            "summary": summary,
            "seats": [
                {
                    key: r.get(key)
                    for key in (
                        "label",
                        "item_id",
                        "planned",
                        "outcome",
                        "reason",
                        "new_item_id",
                        "awaiting_approval",
                    )
                }
                for r in results
            ],
        },
        originator=actor,
    )
    return {"snapshot_id": snap["snapshot_id"], "summary": summary, "seats": results}
