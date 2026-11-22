"""Durable logical claim operations for ua-node workers (task #2136).

The problem: a worker sends a claim, the hub commits item A, and the response
is lost. Single-use device proofs correctly refuse a replay of that request,
so a later fresh claim could hand the worker item B while A stays claimed and
unobserved. This module records one durable disposition per *logical* claim
operation so the worker can reconcile instead.

The worker picks a random operation id and persists it locally BEFORE the
first send. Every request for that operation (the original and any
reconciliation) carries ``claim_op = {id, mode, issued_at}`` and is
authenticated afresh: each HTTP call still signs a new single-use proof, and an
exact proof replay is still refused before this module runs.

Every handler reads and settles its operation row inside the same
``BEGIN IMMEDIATE`` transaction that mutates the queue (``node_queue.claim_next``),
so the row is the single linearization point:

* ``pending``  the operation was admitted and has not been settled yet;
* ``bound``    it claimed exactly one item attempt, with the immutable payload
  it was handed (``snapshot_json``); it never moves to another item;
* ``no_claim`` it is closed without work. A delayed original that arrives
  afterwards finds this row and claims nothing.

A ``reconcile`` request never selects work. It returns the bound disposition,
or closes a ``pending`` or absent operation as ``no_claim`` (installing a
tombstone for an id the hub has never seen), so an original that was admitted
but not yet settled cannot commit afterwards. A ``claim`` request for an
operation that is already settled returns that same disposition.

Retention. A tombstone must outlive every original that could still arrive for
its id, or a deleted fence would reopen admission. A ``claim`` request is
admitted only while ``|now - issued_at| <= MAX_AGE_S``, and a non-bound row is
kept until ``max(created, issued_at) + TOMBSTONE_RETENTION_S`` (well past
``MAX_AGE_S``), so by the time a fence is deleted no request for that id can be
admitted again. A bound row is kept for ``BOUND_RETENTION_S`` and, past that,
only while its attempt is no longer the item's live claim. A store at capacity
refuses new operations rather than discarding unresolved ones.

This is fail-closed recovery, not an exactly-once guarantee: the hub binds at
most one item attempt per operation and never re-issues it implicitly, but
whether a backend actually ran is still established only by the worker's
receipt (or its absence, which the existing lease-expiry path reports as an
unknown outcome).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .node_registry import NodeError

#: Feature string the hub advertises at registration; a worker sends
#: ``claim_op`` only to a hub that lists it. Same string as the worker's.
FEATURE = "node_claim_op_v1"
MODES = frozenset({"claim", "reconcile"})
STATES = frozenset({"pending", "bound", "no_claim"})
#: A ``claim`` request is admitted only this close to its ``issued_at``.
MAX_AGE_S = 600
#: A pending or closed row outlives its admission window by this much.
TOMBSTONE_RETENTION_S = 3600
#: A bound row is kept at least this long, and longer while its claim is live.
BOUND_RETENTION_S = 24 * 3600
#: Rows per node; at capacity new operations are refused, never evicted.
MAX_OPS_PER_NODE = 5000
#: Expired rows deleted per claim transaction, so cleanup stays bounded.
CLEANUP_LIMIT = 50
# Plausible unix seconds: 2001-09-09 .. 2286-11-20.
_ISSUED_MIN = 1_000_000_000
_ISSUED_MAX = 9_999_999_999
_OP_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KEYS = frozenset({"id", "mode", "issued_at"})


@dataclass(frozen=True)
class ClaimOp:
    id: str
    mode: str
    issued_at: int


def parse(raw: Any) -> ClaimOp:
    """Validate a request's ``claim_op`` object; 400 on any malformed field."""
    if not isinstance(raw, dict):
        raise NodeError("claim_op must be a JSON object")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise NodeError("claim_op has unknown field(s): " + ", ".join(unknown))
    op_id, mode, issued_at = raw.get("id"), raw.get("mode"), raw.get("issued_at")
    if not isinstance(op_id, str) or not _OP_ID_RE.fullmatch(op_id):
        raise NodeError("claim_op.id must be 32 lowercase hex characters")
    if mode not in MODES:
        raise NodeError(f"claim_op.mode must be one of {sorted(MODES)}")
    if type(issued_at) is not int or not _ISSUED_MIN <= issued_at <= _ISSUED_MAX:
        raise NodeError("claim_op.issued_at must be unix seconds")
    return ClaimOp(op_id, mode, issued_at)


def load(conn: sqlite3.Connection, node: str, op_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM node_claim_ops WHERE node=? AND op_id=?", (node, op_id)
    ).fetchone()
    return {k: row[k] for k in row.keys()} if row is not None else None


def check_same_request(row: dict[str, Any], op: ClaimOp, identity: str) -> None:
    """A reused id with a different identity or ``issued_at`` is a conflict, never a claim."""
    if row["identity"] != identity or int(row["issued_at"]) != op.issued_at:
        raise NodeError("claim_op.id conflicts with an earlier claim operation", status=409)


def require_fresh(op: ClaimOp, now: int) -> None:
    if abs(now - op.issued_at) > MAX_AGE_S:
        raise NodeError(
            "claim operation is outside its admission window; check the device clock "
            "and reconcile the operation instead of claiming with it",
            status=409,
        )


def cleanup(conn: sqlite3.Connection, now: int) -> int:
    """Delete at most ``CLEANUP_LIMIT`` rows whose fence can no longer matter."""
    cur = conn.execute(
        "DELETE FROM node_claim_ops WHERE rowid IN ("
        "SELECT o.rowid FROM node_claim_ops o WHERE o.fence_expires_at < ? AND ("
        "o.state != 'bound' OR NOT EXISTS (SELECT 1 FROM node_dispatch d "
        "WHERE d.id = o.item_id AND d.status = 'claimed' AND d.attempts = o.attempt)) "
        "LIMIT ?)",
        (now, CLEANUP_LIMIT),
    )
    return cur.rowcount


def _require_capacity(conn: sqlite3.Connection, node: str) -> None:
    (count,) = conn.execute("SELECT COUNT(*) FROM node_claim_ops WHERE node=?", (node,)).fetchone()
    if count >= MAX_OPS_PER_NODE:
        raise NodeError(
            "claim operation store is at capacity for this node; retry later", status=503
        )


def insert(
    conn: sqlite3.Connection,
    node: str,
    op: ClaimOp,
    *,
    identity: str,
    device_node: str | None,
    state: str,
    reason: str | None,
    now: int,
    as_of: str,
) -> dict[str, Any]:
    """Admit a new operation as ``pending`` or install a ``no_claim`` tombstone."""
    if state not in ("pending", "no_claim"):
        raise ValueError(f"insert state {state!r}")
    _require_capacity(conn, node)
    conn.execute(
        "INSERT INTO node_claim_ops (node, op_id, identity, device_node, issued_at, state, "
        "reason, created_at, updated_at, fence_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            node,
            op.id,
            identity,
            device_node or "",
            op.issued_at,
            state,
            reason,
            as_of,
            as_of,
            max(now, op.issued_at) + TOMBSTONE_RETENTION_S,
        ),
    )
    row = load(conn, node, op.id)
    assert row is not None
    return row


def close(
    conn: sqlite3.Connection, row: dict[str, Any], *, reason: str, now: int, as_of: str
) -> dict[str, Any]:
    """Settle a ``pending`` row as terminal ``no_claim``."""
    fence = max(int(row["fence_expires_at"]), now + TOMBSTONE_RETENTION_S)
    conn.execute(
        "UPDATE node_claim_ops SET state='no_claim', reason=?, updated_at=?, "
        "fence_expires_at=? WHERE node=? AND op_id=? AND state='pending'",
        (reason, as_of, fence, row["node"], row["op_id"]),
    )
    return {**row, "state": "no_claim", "reason": reason, "updated_at": as_of}


def bind(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    now: int,
    as_of: str,
) -> dict[str, Any]:
    """Bind a ``pending`` row to the item attempt this transaction just claimed."""
    encoded = json.dumps(snapshot, sort_keys=True)
    cur = conn.execute(
        "UPDATE node_claim_ops SET state='bound', reason=NULL, item_id=?, run_id=?, attempt=?, "
        "snapshot_json=?, updated_at=?, fence_expires_at=? "
        "WHERE node=? AND op_id=? AND state='pending'",
        (
            snapshot["item_id"],
            snapshot.get("run_id"),
            int(snapshot["attempt"]),
            encoded,
            as_of,
            now + BOUND_RETENTION_S,
            row["node"],
            row["op_id"],
        ),
    )
    if cur.rowcount != 1:
        # Only reachable if the caller skipped the in-transaction state check.
        raise NodeError("claim operation is no longer pending", status=409)
    return {
        **row,
        "state": "bound",
        "reason": None,
        "item_id": snapshot["item_id"],
        "run_id": snapshot.get("run_id"),
        "attempt": int(snapshot["attempt"]),
        "snapshot_json": encoded,
        "updated_at": as_of,
    }


def snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("snapshot_json")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def disposition(
    row: dict[str, Any], *, executable: bool, refusal: str | None = None
) -> dict[str, Any]:
    """The worker-facing view of one operation: ids, state and whether to run."""
    return {
        "id": row["op_id"],
        "state": row["state"],
        "reason": refusal or row.get("reason"),
        "item_id": row.get("item_id"),
        "attempt": row.get("attempt"),
        "executable": bool(executable and row["state"] == "bound"),
    }
