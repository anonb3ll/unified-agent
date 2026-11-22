"""Batch notification digest engine to prevent iMessage notification fatigue.

Buffers non-critical notifications in SQLite and flushes them in structured digests
or when a batch threshold is reached.
"""

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, storage
from .notify import notify_as


def notify(message: str) -> dict:
    """Deliver a flushed digest to the operator.

    Entries were already checked against each queuing agent's comms policy
    in batch_notify, so the flush is a system send (task #1764): judging the
    combined digest by whoever happens to call flush_digest would refuse and
    then delete other agents' queued notifications.
    """
    return notify_as(message, sender=None, route="digest_flush")


_DIGEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL DEFAULT 'general',
  message TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT 'Desktop_agent3',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_notifications_cat ON pending_notifications(category);
"""


def _init_digest_db(conn) -> None:
    conn.executescript(_DIGEST_SCHEMA)
    conn.commit()


def batch_notify(
    messages: list[str],
    category: str = "general",
    agent: str = "Desktop_agent3",
    flush_now: bool = False,
    max_batch_size: int = 5,
) -> dict[str, Any]:
    """Queue one or more notification messages into the pending digest buffer.

    If ``flush_now=True`` or the total pending messages in this category reaches
    ``max_batch_size``, the buffer is combined into a single digest and sent via iMessage.

    Args:
        messages: List of non-empty message strings to queue.
        category: Notification category (e.g. 'general', 'drift', 'audit', 'task_completion').
        agent: Label of the agent queuing the messages.
        flush_now: Force an immediate digest flush via notify().
        max_batch_size: Number of queued messages that triggers an auto-flush (default 5).

    Returns:
        {queued: int, total_pending: int, flushed: bool, delivery_result: dict | None}
    """
    if not messages:
        raise ToolError("messages list must not be empty")

    valid_messages = [m.strip() for m in messages if m and m.strip()]
    if not valid_messages:
        raise ToolError("all messages in list were empty")
    from ..operating_profiles import check_operator_contact

    check_operator_contact(config.verified_agent_identity(), route="batch_notify")

    now_iso = datetime.now(UTC).isoformat()
    conn = storage.connect()
    try:
        _init_digest_db(conn)
        for msg in valid_messages:
            conn.execute(
                "INSERT INTO pending_notifications (category, message, agent, created_at) "
                "VALUES (?, ?, ?, ?)",
                (category, msg, agent, now_iso),
            )
        conn.commit()

        cur = conn.execute(
            "SELECT COUNT(*) FROM pending_notifications WHERE category=?",
            (category,),
        )
        total_pending = cur.fetchone()[0]
    finally:
        conn.close()

    should_flush = flush_now or (total_pending >= max_batch_size)
    delivery_result = None

    if should_flush:
        delivery_result = flush_digest(category=category)

    return {
        "queued": len(valid_messages),
        "total_pending": total_pending if not should_flush else 0,
        "flushed": should_flush,
        "delivery_result": delivery_result,
    }


def flush_digest(category: str | None = None) -> dict[str, Any]:
    """Flush queued pending notifications into a combined iMessage digest.

    Args:
        category: Optional category filter. If None, flushes all pending categories.

    Returns:
        {flushed_count: int, delivered: bool, error: str | None}
    """
    conn = storage.connect()
    try:
        _init_digest_db(conn)
        if category:
            cur = conn.execute(
                "SELECT id, category, message, agent, created_at FROM pending_notifications "
                "WHERE category=? ORDER BY id ASC",
                (category,),
            )
        else:
            cur = conn.execute(
                "SELECT id, category, message, agent, created_at FROM pending_notifications "
                "ORDER BY id ASC"
            )
        rows = cur.fetchall()

        if not rows:
            return {
                "flushed_count": 0,
                "delivered": False,
                "error": "No pending notifications to flush",
            }

        ids_to_delete = [r["id"] for r in rows]

        # Format combined digest message
        lines = [f"📬 Agent Notification Digest ({len(rows)} alerts):", ""]
        for r in rows:
            cat_label = f"[{r['category']}]" if r["category"] != "general" else ""
            lines.append(f"• {cat_label} [{r['agent']}]: {r['message']}")

        digest_text = "\n".join(lines)

        # Attempt delivery via notify()
        try:
            res = notify(digest_text)
            delivered = res.get("delivered", False)
            error = res.get("error")
        except Exception as e:
            delivered = False
            error = str(e)

        # Clear flushed rows from DB
        placeholders = ",".join("?" for _ in ids_to_delete)
        conn.execute(
            f"DELETE FROM pending_notifications WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        conn.commit()

        return {
            "flushed_count": len(rows),
            "delivered": delivered,
            "error": error,
        }
    finally:
        conn.close()
