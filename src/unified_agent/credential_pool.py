"""Hub-owned metadata registry for CLI subscription accounts."""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import post_init, storage

SUPPORTED_PROVIDERS = frozenset(
    {"anthropic", "codex", "gemini", "google", "openai", "openrouter", "ollama", "xai"}
)
_SECRET_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|auth(?:orization)?|client[_-]?secret|refresh[_-]?token|"
    r"private[_-]?key|password|secret)\s*[:=]|(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)


def _validate_text(value: str, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > 200:
        requirement = "non-empty " if required else ""
        raise ToolError(f"{field} must be a {requirement}string of at most 200 characters")
    return value.strip()


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "label": row["label"],
        "note": row["note"],
        "status": row["status"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def credential_pool_register(provider: str, label: str, note: str = "") -> dict:
    """Register provider, label, and non-secret operator metadata only."""
    provider = _validate_text(provider, "provider").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ToolError(
            f"unsupported provider {provider!r}; expected one of {sorted(SUPPORTED_PROVIDERS)}"
        )
    label = _validate_text(label, "label")
    note = _validate_text(note, "note", required=False)
    if _SECRET_RE.search(note):
        raise ToolError("note must not contain OAuth tokens, API keys, passwords, or other secrets")

    credential_id = f"cp_{uuid.uuid4().hex}"
    timestamp = storage.now()
    post_init.register_default_hooks()
    storage.init_db_once()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO credential_pool "
            "(id, provider, label, note, status, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'registered', 0, ?, ?)",
            (credential_id, provider, label, note, timestamp, timestamp),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, provider, label, note, status, active, created_at, updated_at "
            "FROM credential_pool WHERE id=?",
            (credential_id,),
        ).fetchone()
    finally:
        conn.close()
    return {"credential": _row_dict(row)}


def credential_pool_list(include_revoked: bool = False) -> dict:
    """List existing metadata; unavailable stores are explicit, revoked rows opt-in.

    Registration owns bootstrap. A read never creates or migrates the registry.
    """
    try:
        conn = storage.connect_read_only()
    except storage.LiveStoreError as exc:
        raise ToolError("credential metadata unavailable") from exc
    try:
        query = (
            "SELECT id, provider, label, note, status, active, created_at, updated_at "
            "FROM credential_pool"
        )
        if not include_revoked:
            query += " WHERE status != 'revoked'"
        rows = conn.execute(query + " ORDER BY created_at, id").fetchall()
    except sqlite3.Error as exc:
        raise ToolError("credential metadata unavailable") from exc
    finally:
        conn.close()
    credentials = [_row_dict(row) for row in rows]
    return {"credentials": credentials, "count": len(credentials)}


def credential_pool_revoke(id: str) -> dict:
    """Revoke one registry row without deleting its audit metadata."""
    id = _validate_text(id, "id")
    post_init.register_default_hooks()
    storage.init_db_once()
    timestamp = storage.now()
    conn = storage.connect()
    try:
        updated = conn.execute(
            "UPDATE credential_pool SET status='revoked', updated_at=? "
            "WHERE id=? AND status='registered'",
            (timestamp, id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, provider, label, note, status, active, created_at, updated_at "
            "FROM credential_pool WHERE id=?",
            (id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"credential pool record not found: {id}")
    return {"credential": _row_dict(row), "already_revoked": updated.rowcount == 0}


def model_quota_status() -> dict:
    """Report fleet lane availability derived from local observations.

    Sources, in preference order for Codex: the local rollout ``rate_limits``
    stream; for every lane: classified dispatch/executor failure outcomes.
    Absent data is ``unknown`` with ``source: "no source"`` — never invented
    as available, never measured by probing a provider.
    """
    from .quota_observe import build_quota_status

    return build_quota_status()
