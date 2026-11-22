"""Server-enforced idempotency receipts for delegation create (task #1938).

A delegation spans several commits: the child task, the attributed memory note
and the ``delegations`` row. A client that loses the response cannot tell
which of them landed, and a blind retry used to create a second delegation.
With an ``idempotency_key`` the hub keeps one receipt per (caller, key) in the
existing ``idempotency_keys`` table and moves it through three states:

``in_progress``
    A caller holds the key while the delegation is being created. A second
    caller with the same key is refused as a conflict instead of running the
    same effects concurrently. A holder that died (hub restart mid-call) leaves
    a stale receipt; after ``STALE_AFTER_S`` the next retry takes it over.
``approval_required``
    The approval gate parked the request as ``approval_id``. The identical
    retry after a human approves consumes the one-shot pass and proceeds.
``committed``
    The delegations row and this receipt committed in one transaction, so a
    retry or a lookup returns the original result and never a second row.

The receipt binds a digest of the request. Reusing a key for a different
delegation is a conflict, never a replay of the other request's result.
The child task and memory note are created with keys derived from the receipt,
so a takeover after a crash reuses them instead of creating orphans.

``delegate_lookup`` reads a receipt without any effect: the recovery path for a
client whose response was dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import storage
from ..tool_errors import ConflictError

TOOL = "delegate"
STALE_AFTER_S = 900.0
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def validate_key(key: Any) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ToolError(
            "idempotency_key must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return key


def storage_key(actor: str, key: str) -> str:
    """The caller-scoped receipt key: another caller's key never replays yours.

    This is the caller key the shared idempotency store sees. ``_read``,
    ``_write`` and the delete add the tenant scope through ``storage``, so a
    receipt written before tenant scoping is refused, never missed.
    """
    digest = hashlib.sha256(f"{actor}\0{key}".encode()).hexdigest()
    return f"delegate-receipt:{digest}"


def derived_key(receipt_key: str, part: str) -> str:
    """Key for one inner effect (task, memory note) of a keyed delegation."""
    digest = hashlib.sha256(f"{receipt_key}\0{part}".encode()).hexdigest()
    return f"delegate-{part}:{digest}"


def request_digest(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read(conn, key: str) -> dict | None:
    try:
        row = storage.read_idempotency_row(conn, key)
    except storage.AmbiguousIdempotencyReceipt as exc:
        raise ToolError(str(exc)) from None
    if row is None:
        return None
    if row["tool"] != TOOL:
        raise ConflictError("idempotency_key is already registered for a different operation")
    try:
        record = json.loads(row["result_json"])
    except (TypeError, ValueError):
        raise ToolError("delegation receipt is unreadable; retry with a new key") from None
    if not isinstance(record, dict) or record.get("state") not in {
        "in_progress",
        "approval_required",
        "committed",
    }:
        raise ToolError("delegation receipt is unreadable; retry with a new key")
    return record


def _write(conn, key: str, record: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, tool, result_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            storage.idempotency_storage_key(key),
            TOOL,
            json.dumps(record, allow_nan=False),
            storage.now(),
        ),
    )


def _age_s(record: dict) -> float:
    try:
        since = datetime.fromisoformat(record["since"])
    except (KeyError, TypeError, ValueError):
        return STALE_AFTER_S
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return (datetime.now(UTC) - since).total_seconds()


def claim(key: str, digest: str) -> tuple[str, Any]:
    """Return ``("replay", result)`` for a committed key or ``("owner", token)``."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        record = _read(conn, key)
        if record is not None:
            if record.get("request") != digest:
                raise ConflictError(
                    "idempotency_key was already used for a different delegation request; "
                    "use a new key for a new request"
                )
            if record["state"] == "committed":
                conn.rollback()
                return "replay", record["result"]
            if record["state"] == "in_progress" and _age_s(record) < STALE_AFTER_S:
                raise ConflictError(
                    "a delegation with this idempotency_key is still in progress; "
                    "look it up before retrying"
                )
        token = secrets.token_hex(16)
        _write(
            conn,
            key,
            {
                "state": "in_progress",
                "request": digest,
                "token": token,
                "since": datetime.now(UTC).isoformat(),
            },
        )
        conn.commit()
        return "owner", token
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def park(key: str, token: str, digest: str, approval_id: int | None) -> None:
    """Record that the owner's request is waiting in the approval queue."""
    _transition(
        key,
        token,
        {"state": "approval_required", "request": digest, "approval_id": approval_id},
    )


def release(key: str, token: str) -> None:
    """Drop the owner's in-progress receipt after a refusal before commit."""
    _transition(key, token, None)


def _transition(key: str, token: str, record: dict | None) -> None:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read(conn, key)
        except ToolError:
            current = None
        if current and current["state"] == "in_progress" and current.get("token") == token:
            if record is None:
                conn.execute(
                    "DELETE FROM idempotency_keys WHERE key=? AND tool=?",
                    (storage.idempotency_storage_key(key), TOOL),
                )
            else:
                _write(conn, key, record)
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def commit_on_connection(conn, key: str, token: str, digest: str, result: dict) -> dict | None:
    """Inside the caller's write transaction: fence the owner, then record.

    Returns the committed result of a competing owner instead (the caller must
    roll back its insert), or None once this owner's receipt is recorded.
    """
    current = _read(conn, key)
    if current is not None and current["state"] == "committed":
        return current["result"]
    if current is None or current["state"] != "in_progress" or current.get("token") != token:
        raise ConflictError(
            "another request took over this idempotency_key; look it up before retrying"
        )
    _write(conn, key, {"state": "committed", "request": digest, "result": result})
    return None


def lookup(key: str) -> dict:
    """Read-only view of one receipt for recovery after a dropped response."""
    conn = storage.connect()
    try:
        record = _read(conn, key)
    finally:
        conn.close()
    if record is None:
        return {"state": "not_found"}
    view: dict[str, Any] = {"state": record["state"]}
    if record["state"] == "committed":
        view["result"] = record["result"]
    elif record["state"] == "approval_required":
        view["approval_id"] = record.get("approval_id")
    else:
        view["since"] = record.get("since")
        view["stale"] = _age_s(record) >= STALE_AFTER_S
    return view
