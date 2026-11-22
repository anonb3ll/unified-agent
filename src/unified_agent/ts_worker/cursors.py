"""Durable cursor state for the task-space (Discord/Telegram) chat worker.

Mirrors the zulip cursor schema's separation of concerns: the worker stores
only the last message/update id it has durably handled per surface/channel;
tokens remain env-only, never SQLite, and no credential material ever lands
in these rows.
"""

from __future__ import annotations

from .. import table_schemas
from ..storage import connect, now

_TS_SCHEMA = table_schemas.TS_CURSORS


def get_ts_cursor(surface: str, channel: str) -> dict[str, object] | None:
    """Return the durable cursor for one surface/channel pair, if present."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT surface, channel, last_message_id, updated_at "
            "FROM ts_cursors WHERE surface=? AND channel=?",
            (surface, channel),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def list_ts_cursors() -> list[dict[str, object]]:
    """Return all durable task-space cursors in stable surface/channel order."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT surface, channel, last_message_id, updated_at "
            "FROM ts_cursors ORDER BY surface, channel"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def save_ts_cursor(surface: str, channel: str, last_message_id: int) -> dict[str, object]:
    """Advance a task-space cursor monotonically after successful handling.

    The upsert refuses to move a cursor backwards, mirroring
    :func:`save_zulip_cursor`. Callers persist the event first and advance the
    cursor second, so a crash between the two replays safely through the event
    log's dedupe key.
    """
    if not isinstance(surface, str) or not surface.strip():
        raise ValueError("surface must be a non-empty string")
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel must be a non-empty string")
    if isinstance(last_message_id, bool) or not isinstance(last_message_id, int):
        raise ValueError("last_message_id must be an integer")
    if last_message_id < 0:
        raise ValueError("last_message_id must be non-negative")

    timestamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO ts_cursors (surface, channel, last_message_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(surface, channel) DO UPDATE SET "
            "last_message_id=MAX(last_message_id, excluded.last_message_id), "
            "updated_at=excluded.updated_at",
            (surface, channel, int(last_message_id), timestamp),
        )
        conn.commit()
        row = conn.execute(
            "SELECT surface, channel, last_message_id, updated_at "
            "FROM ts_cursors WHERE surface=? AND channel=?",
            (surface, channel),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)
