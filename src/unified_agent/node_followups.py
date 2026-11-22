"""Operator follow-ups into a live ACP node session (task #1740, I10).

The Sessions pane can send a follow-up to a running harness session in one of
two modes:

``queue``
    Delivered after the current turn ends: ua-node sends it as the next
    ``session/prompt`` in the same ACP session, one follow-up per turn, in the
    order the hub accepted them (``seq``).
``steer``
    Redirect now. ua-node interrupts the running turn and sends the message
    as the next prompt. ACP v1 has no mid-turn input, so the default path is
    ``session/cancel`` then ``session/prompt`` (emulated). An agent that
    advertises the ``_session/steering`` extension at initialize gets the
    message injected into the running turn instead; the path taken is recorded
    on the delivery event.

The hub row is the source of truth for what the operator asked for; the node
reports what it did through the ordinary ``/api/node/events`` stream
(``followup_delivered`` / ``followup_dropped``). Status moves
``queued -> claimed`` (the node fetched it) ``-> delivered | dropped``.

Idempotency: every follow-up carries a client-chosen ``idempotency_key``,
unique per item. A retried submit with the same key and the same payload
returns the first row (``duplicate: true``) and records nothing new; the same
key with a different payload is refused, so a key cannot be reused to slip a
second message in.

Approval (task #1872): a follow-up from an agent identity passes the
``node_dispatch`` approval gate on its own text. One that is
operator-authenticated (sent with the operator's credential: the
``UA_OPERATOR_LABEL`` identity on a bearer, OAuth, cookie or WorkOS channel)
is queued without a second approval: the HTTP route decides that from the
authenticated request (``routes._operator_recovery``) and passes
``operator=True``; nothing a caller sends can set it. That proves which
credential sent the request, not that a person typed it. The row and its
``followup_queued`` event record which path admitted it
(``operator_authored``, ``approval``).

A follow-up never changes what the session may do: it runs in the same ACP
session, under the same permission relay as the first turn.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import acp_matrix, storage, table_schemas
from .node_registry import NodeError, _load, _owned_node, _row, validate_node_name

MODES = frozenset({"queue", "steer"})
STATUSES = frozenset({"queued", "claimed", "delivered", "dropped"})
OPEN_STATUSES = ("queued", "claimed")
#: Feature flag a ua-node advertises in its registered facts once it polls
#: ``/api/node/followups``. A node without it would never deliver, so the hub
#: refuses follow-ups for it instead of queueing into a void.
NODE_FEATURE = "acp_followups"

MAX_BODY_CHARS = 20_000
MAX_OPEN_PER_ITEM = 20
MAX_CLAIM_PER_CALL = 20
#: ``approval`` values on a row and its ``followup_queued`` event: passed the
#: ``node_dispatch`` gate, or sent with the operator's credential and not gated.
#: ``operator_authored`` on the same row and event means only that: the request
#: authenticated as ``UA_OPERATOR_LABEL`` on an operator channel. It is not
#: evidence that a human typed the text.
APPROVAL_GATE_PASSED = "gate_passed"
APPROVAL_NOT_REQUIRED_OPERATOR = "not_required_operator"
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_ITEM_RE = re.compile(r"^nd-[0-9a-f]{16}$")
_FOLLOWUP_RE = re.compile(r"^nf-[0-9a-f]{16}$")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the follow-up table (DDL lives in core ``table_schemas``)."""
    table_schemas.node_followups(conn)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record(kind: str, payload: dict[str, Any], originator: str) -> None:
    from . import node_queue

    node_queue._record(kind, payload, originator)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "followup_id": row["id"],
        "item_id": row["item_id"],
        "seq": row["seq"],
        "mode": row["mode"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "operator_authored": bool(row["operator_authored"]),
        "approval": row["approval"],
    }


def _validate_submit(item_id: Any, mode: Any, body: Any, key: Any) -> tuple[str, str, str, str]:
    item_key = str(item_id or "").strip()
    if not _ITEM_RE.fullmatch(item_key):
        raise NodeError("item_id is malformed")
    if mode not in MODES:
        raise NodeError("mode must be 'queue' or 'steer'")
    if not isinstance(body, str) or not body.strip():
        raise NodeError("body is required")
    if len(body) > MAX_BODY_CHARS:
        raise NodeError(f"body exceeds {MAX_BODY_CHARS} characters")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise NodeError("idempotency_key must be 8-128 characters of [A-Za-z0-9_.:-]")
    return item_key, str(mode), body, key


def _node_supports_followups(conn: sqlite3.Connection, node: str) -> bool:
    row = conn.execute("SELECT facts_json FROM nodes WHERE node=?", (node,)).fetchone()
    facts = _load(row["facts_json"]) if row is not None else None
    features = facts.get("node_features") if isinstance(facts, dict) else None
    return isinstance(features, list) and NODE_FEATURE in features


def _attribution_check(
    conn: sqlite3.Connection, item: dict[str, Any], actor: str, *, operator: bool
) -> None:
    """No identity takeover: only the session's own identities may steer it.

    Allowed: the identity that started the item (``created_by``), the agent the
    session runs as (``profile_agent``, else the device ``agent``), or the
    verified human operator on a recovery channel (``routes._operator_recovery``).
    A profile that merely exposes the route's tools is not enough. When the
    item's task is claimed by another label, that label owns the work: refused.
    """
    if operator:
        return
    allowed = {item.get("created_by"), item.get("profile_agent"), item.get("agent")} - {None, ""}
    if actor not in allowed:
        raise NodeError(
            f"item {item['id']!r} was started by {item.get('created_by')} as "
            f"{item.get('profile_agent') or item.get('agent')}; only those identities or the "
            "operator may send it follow-ups",
            status=403,
        )
    if item.get("task_id") is not None:
        task = conn.execute(
            "SELECT claimed_by FROM tasks WHERE id=?", (item["task_id"],)
        ).fetchone()
        holder = task["claimed_by"] if task is not None else None
        if holder and holder != actor:
            raise NodeError(
                f"task #{item['task_id']} is claimed by {holder}; follow-ups are theirs to send",
                status=403,
            )


def _key_held_by_another_approval(
    conn: sqlite3.Connection, item_key: str, key: str, actor: str
) -> bool:
    """True when another identity's open follow-up approval names this item and key.

    The key belongs to whoever asked first: queueing it for someone else (for
    example the operator, who needs no approval) would strand that approval and
    turn its holder's retry into a read of a row they did not send (task #1872).
    """
    from . import node_queue

    row = conn.execute(
        "SELECT 1 FROM approvals WHERE tool=? AND status IN ('pending', 'approved') "
        "AND COALESCE(requested_by, '') != ? "
        "AND json_extract(payload, '$.followup.item_id')=? "
        "AND json_extract(payload, '$.followup.idempotency_key')=? LIMIT 1",
        (node_queue.NODE_DISPATCH_TOOL, actor, item_key, key),
    ).fetchone()
    return row is not None


def _admit(
    conn: sqlite3.Connection,
    item_key: str,
    mode: str,
    text: str,
    key: str,
    *,
    actor: str,
    operator: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(duplicate_row, None)`` for a known key, else ``(None, item)`` once every
    check passes. Raises NodeError for a request that could never be queued.
    Attribution comes first, so another identity cannot even replay a key."""
    item = _row(conn.execute("SELECT * FROM node_dispatch WHERE id=?", (item_key,)).fetchone())
    if item is None:
        raise NodeError(f"item {item_key!r} does not exist", status=404)
    _attribution_check(conn, item, actor, operator=operator)
    existing = _row(
        conn.execute(
            "SELECT * FROM node_followups WHERE item_id=? AND idempotency_key=?",
            (item_key, key),
        ).fetchone()
    )
    if existing is not None:
        if existing["created_by"] != actor:
            # A key is one sender's: another identity reusing it would either
            # read back a row it did not send or strand its own pending
            # approval behind someone else's follow-up (task #1872).
            raise NodeError(
                "idempotency_key was already used by another identity on this item; "
                "choose a new key",
                status=409,
            )
        if existing["mode"] != mode or existing["body"] != text:
            raise NodeError(
                "idempotency_key was already used for a different follow-up", status=409
            )
        return existing, None
    if _key_held_by_another_approval(conn, item_key, key, actor):
        raise NodeError(
            "idempotency_key is waiting on another identity's approval on this item; "
            "choose a new key",
            status=409,
        )
    harness = acp_matrix.acp_backend_name(item["backend"])
    if harness not in acp_matrix.ACP_HARNESSES:
        raise NodeError(
            f"backend {item['backend']!r} runs one prompt per dispatch; "
            "follow-ups need an acp:<harness> session",
            status=409,
        )
    if item["status"] not in ("queued", "claimed"):
        raise NodeError(
            f"item {item_key!r} is {item['status']}; follow-ups reach a live session "
            "only (use Start again for a new one)",
            status=409,
        )
    if item["cancel_requested"]:
        raise NodeError(f"item {item_key!r} is being cancelled", status=409)
    if mode == "steer" and item["status"] != "claimed":
        raise NodeError(
            "nothing to steer yet: the session has not started; queue the follow-up",
            status=409,
        )
    if not _node_supports_followups(conn, item["node"]):
        raise NodeError(
            f"node {item['node']!r} runs a ua-node without follow-up support; "
            "upgrade it to send follow-ups",
            status=409,
        )
    open_count = conn.execute(
        "SELECT COUNT(*) FROM node_followups WHERE item_id=? AND status IN (?, ?)",
        (item_key, *OPEN_STATUSES),
    ).fetchone()[0]
    if open_count >= MAX_OPEN_PER_ITEM:
        raise NodeError(
            f"item {item_key!r} already has {open_count} undelivered follow-ups", status=429
        )
    return None, item


def _check_followup_gate(item: dict[str, Any], mode: str, text: str, key: str, actor: str) -> None:
    """The dispatch approval gate, applied to this follow-up's own text.

    A follow-up is new prompt text for the device, so it passes the same
    ``node_dispatch`` prod-action gate (scope, revocation, one-shot human
    approval of the exact payload) the dispatch itself passed. The original
    approval covered only the initial prompt. The payload names the item and
    the follow-up, so neither a dispatch approval nor another follow-up's
    approval can be replayed for it. Raises ``ApprovalPendingError`` until a
    human approves this exact payload.
    """
    from . import config, node_queue, safety

    payload = {
        "node": item["node"],
        "backend": item["backend"],
        "cwd": item["cwd"],
        "task_id": item["task_id"],
        "run_id": item["run_id"],
        "followup": {"item_id": item["id"], "mode": mode, "idempotency_key": key},
        "prompt": text,
    }
    if item.get("hermes_profile") is not None:
        payload.update(
            hermes_profile=item["hermes_profile"], profile_agent=item.get("profile_agent")
        )
    token = config.agent_identity_var.set(actor)
    try:
        safety.check_prod_action_gate(node_queue.NODE_DISPATCH_TOOL, payload)
    finally:
        config.agent_identity_var.reset(token)


def _check_operator(actor: str) -> None:
    """Defence in depth for the ungated path: ``operator=True`` must name the operator.

    The route sets ``operator`` only from ``routes._operator_recovery`` (the
    ``UA_OPERATOR_LABEL`` identity on an operator auth channel). This re-checks the
    label here, so a future caller that passes ``operator=True`` for an agent
    identity is refused rather than ungated.
    """
    from . import config

    # ``operator_label()`` can never name an agent: ``config.operator_label_problem``
    # refuses a UA_OPERATOR_LABEL that is a catalogued agent or a configured worker.
    if actor != config.operator_label():
        raise NodeError(
            "only a request authenticated as the operator skips the follow-up gate", status=403
        )


def _check_authority(actor: str) -> None:
    """The identity half of the ``node_dispatch`` gate, for every sender, first.

    ``safety.check_prod_action_authority`` is the revocation and
    ``UA_AGENT_SCOPES`` part of the gate. ``submit`` runs it before admission
    on both paths, so a revoked or de-scoped sender is refused (``ToolError``,
    answered 403) even when retrying a key that already has a row: a duplicate
    is never read back by someone who has lost the authority to send it. The
    operator path then skips only the approval; the agent path's gate runs the
    same check again with the approval.
    """
    from . import config, node_queue, safety

    token = config.agent_identity_var.set(actor)
    try:
        safety.check_prod_action_authority(node_queue.NODE_DISPATCH_TOOL)
    finally:
        config.agent_identity_var.reset(token)


def submit(
    item_id: Any,
    *,
    mode: Any,
    body: Any,
    idempotency_key: Any,
    actor: str,
    operator: bool = False,
) -> dict[str, Any]:
    """Accept one follow-up for a live ACP item. Idempotent on the key.

    ``operator`` is True only for a request authenticated as the operator
    (``UA_OPERATOR_LABEL`` on a recovery channel), decided by the HTTP route
    from the authenticated request (``routes._operator_recovery``), never from
    the request body. It proves the credential, not that a person typed the
    text. It does two things: the operator identity may follow up on any item
    (everyone else must be one of the item's own identities), and its
    follow-up is queued without a second approval (task #1872), recorded on
    the row and event as ``operator_authored: true,
    approval: "not_required_operator"``.

    Order, on both paths: the operator label check (operator path only), then
    authority (revocation, then ``UA_AGENT_SCOPES``), then admission, then the
    approval gate (agent path only). A revoked or de-scoped sender is refused
    even for a key that already has a row. For everyone else, every check runs
    before the approval gate, so an approval is never consumed by a request
    that could not have queued; a retry of an already-queued key by a sender
    who still holds authority returns that row without asking for approval
    again. The gate raises
    ``tools.approvals.ApprovalPendingError`` until a human approves the exact
    follow-up; the caller re-sends the same request (same key) once approved.
    """
    item_key, mode_s, text, key = _validate_submit(item_id, mode, body, idempotency_key)
    if operator:
        _check_operator(actor)
    # Authority before admission, on both paths (task #1872): revocation and
    # scope must refuse even a retry that would answer a duplicate.
    _check_authority(actor)
    conn = storage.connect()
    try:
        existing, item = _admit(conn, item_key, mode_s, text, key, actor=actor, operator=operator)
    finally:
        conn.close()
    if existing is not None:
        return {**_public(existing), "duplicate": True}
    assert item is not None
    if operator:
        # Sent with the operator's credential: operator decision 1B (task
        # #1872) lets that identity's follow-ups skip the second approval.
        # Revocation and scope were checked above, before admission.
        approval = APPROVAL_NOT_REQUIRED_OPERATOR
    else:
        _check_followup_gate(item, mode_s, text, key, actor)
        approval = APPROVAL_GATE_PASSED
    as_of = _now()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Re-check under the write lock: the item may have ended, or a racing
        # request with the same key may have inserted first. The approval was
        # consumed by the gate above, so an item that ended in that window
        # answers 409 here with its approval spent; a new follow-up on a new
        # dispatch needs a new approval anyway.
        existing, item = _admit(conn, item_key, mode_s, text, key, actor=actor, operator=operator)
        if existing is not None:
            conn.commit()
            return {**_public(existing), "duplicate": True}
        assert item is not None
        seq = (
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM node_followups WHERE item_id=?", (item_key,)
            ).fetchone()[0]
            + 1
        )
        row = {
            "id": f"nf-{secrets.token_hex(8)}",
            "item_id": item_key,
            "seq": seq,
            "mode": mode_s,
            "body": text,
            "idempotency_key": key,
            "status": "queued",
            "created_by": actor,
            "created_at": as_of,
            "updated_at": as_of,
            "operator_authored": 1 if operator else 0,
            "approval": approval,
        }
        conn.execute(
            "INSERT INTO node_followups (id, item_id, seq, mode, body, idempotency_key, "
            "status, created_by, created_at, updated_at, operator_authored, approval) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.values()),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _record(
        "followup_queued",
        {
            "node": item["node"],
            "item_id": item_key,
            "followup_id": row["id"],
            "seq": seq,
            "mode": mode_s,
            "status": "queued",
            "operator_authored": bool(operator),
            "approval": approval,
            # The operator's own text, whole (bounded by MAX_BODY_CHARS), so the
            # transcript shows exactly what was sent.
            "text": text,
        },
        actor,
    )
    return {**_public(row), "duplicate": False}


MAX_ACK_PER_CALL = 50


def claim_for_node(
    node: Any,
    item_id: Any,
    *,
    identity: str,
    device_node: str | None = None,
    ack: Any = None,
) -> dict[str, Any]:
    """Hand the node its unacknowledged follow-ups for one claimed item, in ``seq`` order.

    Two-phase, so a lost reply loses nothing: a poll returns every ``queued``
    row (up to ``MAX_CLAIM_PER_CALL``) without changing it, and the node lists
    the ids it has received in ``ack`` on its next poll. Only an acknowledged
    row moves to ``claimed``. A reply that never arrived is simply answered
    again; the node dedupes by follow-up id. Delivery itself is reported on the
    event stream.
    """
    name = validate_node_name(node)
    item_key = str(item_id or "")
    if not _ITEM_RE.fullmatch(item_key):
        raise NodeError("item_id is malformed")
    if ack is None:
        ack = []
    if (
        not isinstance(ack, list)
        or len(ack) > MAX_ACK_PER_CALL
        or not all(isinstance(a, str) and _FOLLOWUP_RE.fullmatch(a) for a in ack)
    ):
        raise NodeError(f"ack must be a list of at most {MAX_ACK_PER_CALL} follow-up ids")
    as_of = _now()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _owned_node(conn, name, identity, device_node)
        item = _row(
            conn.execute(
                "SELECT status FROM node_dispatch WHERE id=? AND node=?", (item_key, name)
            ).fetchone()
        )
        if item is None:
            raise NodeError(f"item {item_key!r} is not queued for node {name!r}", status=404)
        if item["status"] != "claimed":
            raise NodeError(f"item {item_key!r} is {item['status']}, not claimed", status=409)
        for fid in ack:
            conn.execute(
                "UPDATE node_followups SET status='claimed', claimed_at=?, updated_at=? "
                "WHERE id=? AND item_id=? AND status='queued'",
                (as_of, as_of, fid, item_key),
            )
        rows = [
            _row(r)
            for r in conn.execute(
                "SELECT * FROM node_followups WHERE item_id=? AND status='queued' "
                "ORDER BY seq LIMIT ?",
                (item_key, MAX_CLAIM_PER_CALL),
            ).fetchall()
        ]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "item_id": item_key,
        "followups": [
            {"id": r["id"], "seq": r["seq"], "mode": r["mode"], "body": r["body"]} for r in rows
        ],
    }


def _bounded(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def apply_node_event(item_id: str, kind: str, data: dict[str, Any]) -> bool:
    """Fold a node's ``followup_delivered`` / ``followup_dropped`` event into the row.

    Bound to ``item_id`` (already validated as this node's claimed item by the
    caller), so a node cannot settle another dispatch's follow-up. Returns
    whether a row changed. Best-effort: the event itself is already durable.
    """
    followup_id = str(data.get("followup_id") or "")
    if not _FOLLOWUP_RE.fullmatch(followup_id):
        return False
    as_of = _now()
    try:
        conn = storage.connect()
        try:
            if kind == "followup_delivered":
                cur = conn.execute(
                    "UPDATE node_followups SET status='delivered', delivered_at=?, turn_id=?, "
                    "path=?, updated_at=? WHERE id=? AND item_id=? AND status IN (?, ?)",
                    (
                        as_of,
                        _bounded(data.get("turn_id"), 40),
                        _bounded(data.get("path"), 40),
                        as_of,
                        followup_id,
                        item_id,
                        *OPEN_STATUSES,
                    ),
                )
            elif kind == "followup_dropped":
                cur = conn.execute(
                    "UPDATE node_followups SET status='dropped', reason=?, updated_at=? "
                    "WHERE id=? AND item_id=? AND status IN (?, ?)",
                    (
                        _bounded(data.get("reason"), 200),
                        as_of,
                        followup_id,
                        item_id,
                        *OPEN_STATUSES,
                    ),
                )
            else:
                return False
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


_TERMINAL_ITEM_STATUSES = ("finished", "failed", "killed", "cancelled")


def _drop_reason(item_status: str) -> str:
    return "item_cancelled" if item_status == "cancelled" else "session_ended_before_delivery"


def sweep_ended(*, actor: str, node: str | None = None, item_id: str | None = None) -> list[str]:
    """Drop every undelivered follow-up whose dispatch item has ended, with events.

    Called after each path that ends an item commits: a node receipt, a claim
    pass (cancelled-before-start, lease-expired and policy-denied items), a
    governed cancel of a queued item, and the coordinator's lease reclaim. A
    row the node fetched but never reported delivered is dropped too: the hub
    cannot claim it reached the session. Each drop is recorded as a
    ``followup_dropped`` event so the transcript shows it, exactly once even
    when two sweeps race (the per-row update is conditional). Runs on its own
    connection, never inside the caller's transaction. Best-effort: it never
    raises into the path that ended the item.
    """
    clauses = [
        "f.status IN (?, ?)",
        f"d.status IN ({', '.join('?' * len(_TERMINAL_ITEM_STATUSES))})",
    ]
    params: list[Any] = [*OPEN_STATUSES, *_TERMINAL_ITEM_STATUSES]
    if node is not None:
        clauses.append("d.node=?")
        params.append(node)
    if item_id is not None:
        clauses.append("f.item_id=?")
        params.append(item_id)
    as_of = _now()
    dropped: list[dict[str, Any]] = []
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT f.id, f.item_id, f.status AS row_status, d.node, d.status AS item_status "
                "FROM node_followups f JOIN node_dispatch d ON d.id=f.item_id "
                "WHERE " + " AND ".join(clauses) + " ORDER BY f.item_id, f.seq",
                params,
            ).fetchall()
            for r in rows:
                # A claimed row was acknowledged by the node, which may have sent
                # it before the session ended: the hub cannot say it was not.
                reason = (
                    "delivery_unconfirmed"
                    if r["row_status"] == "claimed"
                    else _drop_reason(r["item_status"])
                )
                cur = conn.execute(
                    "UPDATE node_followups SET status='dropped', reason=?, updated_at=? "
                    "WHERE id=? AND status IN (?, ?)",
                    (reason, as_of, r["id"], *OPEN_STATUSES),
                )
                if cur.rowcount:
                    dropped.append(
                        {
                            "node": r["node"],
                            "item_id": r["item_id"],
                            "followup_id": r["id"],
                            "reason": reason,
                        }
                    )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    for payload in dropped:
        _record("followup_dropped", payload, actor)
    # The same end-of-item paths close an adopted session whose item never ran
    # (task #1741): it would otherwise read running with no receipt to end it.
    try:
        from . import node_resume

        node_resume.close_ended_adoptions(item_id=item_id)
    except Exception:  # noqa: BLE001 - best-effort, like the sweep itself
        pass
    return [d["followup_id"] for d in dropped]


def list_for_item(item_id: str) -> list[dict[str, Any]]:
    """Every follow-up of one item, oldest first, without bodies."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM node_followups WHERE item_id=? ORDER BY seq", (item_id,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        row = _row(r)
        out.append(
            {
                **_public(row),
                "delivered_at": row["delivered_at"],
                "turn_id": row["turn_id"],
                "path": row["path"],
                "reason": row["reason"],
            }
        )
    return out
