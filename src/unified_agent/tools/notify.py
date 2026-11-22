"""Programmatic outbound iMessage push via the imessage-bridge send.ts primitive.

Gives any agent the ability to proactively notify the operator without
going through the chat loop.  The actual delivery is delegated to
``bun send.ts`` (which opens its own short-lived Spectrum connection),
so this module has no Spectrum dependency — it just shells out.
"""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Locate the send.ts script relative to the workspace
_BRIDGE_DIR_ENV = "UA_BRIDGE_DIR"
_DEFAULT_BRIDGE_DIR = "projects/imessage-bridge"

# Durable delivery-outcome log for the Notify Delivery Status operator surface
# (task #1183). Every notify() attempt — direct or via digest.flush_digest(),
# which calls notify() under the hood — is recorded here: never the message
# text itself, only success/fail metadata. This is the read side of "did the
# last send actually go out", without reading agent journals.
_DELIVERY_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_delivery_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT,
  delivered INTEGER NOT NULL,
  chars INTEGER NOT NULL,
  error TEXT,
  sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notify_delivery_log_sent_at ON notify_delivery_log(sent_at);
"""


def _record_delivery(*, delivered: bool, chars: int, error: str | None) -> None:
    """Best-effort append to the delivery log. Never raises into the caller."""
    try:
        from .. import config, storage

        try:
            agent = config.verified_agent_identity()
        except Exception:  # noqa: BLE001 - identity is optional context here
            agent = None
        conn = storage.connect()
        try:
            conn.executescript(_DELIVERY_LOG_SCHEMA)
            conn.execute(
                "INSERT INTO notify_delivery_log (agent, delivered, chars, error, sent_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, 1 if delivered else 0, chars, error, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - logging must never break a real send
        pass


def _bridge_dir() -> Path:
    """Resolve the imessage-bridge project directory.

    Prefer ``UA_BRIDGE_DIR`` when set. Otherwise use the process workspace
    (``config.process_workspace_root()``), not ``workspace_root()``. The
    latter is the per-agent writable root under ``UA_AGENT_WORKSPACES`` and
    must not supply ``send.ts``. Set ``UA_BRIDGE_DIR`` when the bridge does
    not live at ``<process workspace>/projects/imessage-bridge``.
    """
    from .. import config

    raw = os.environ.get(_BRIDGE_DIR_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return (config.process_workspace_root() / _DEFAULT_BRIDGE_DIR).resolve()


def _send_script() -> Path:
    return _bridge_dir() / "src" / "send.ts"


def _refuse_agent_supplied_bridge(script: Path) -> None:
    """Refuse to execute send.ts when an agent-bound workspace supplies it.

    When the caller's ``workspace_root()`` differs from the process workspace,
    a script that resolves inside the caller root is the agent's writable
    tree. The live hub, where those two roots are the same path, is unchanged
    and notify keeps working.
    """
    from .. import config

    try:
        caller = config.workspace_root().resolve()
    except ToolError:
        # A DENY binding has no caller workspace that could supply the script.
        return
    process = config.process_workspace_root().resolve()
    if caller == process:
        return
    try:
        resolved = script.resolve()
    except OSError as e:
        raise ToolError(f"cannot resolve send.ts: {e}") from e
    if resolved == caller or resolved.is_relative_to(caller):
        raise ToolError(
            "refusing to execute send.ts from an agent-bound workspace "
            f"({resolved}). Set {_BRIDGE_DIR_ENV} to a bridge outside that "
            "workspace."
        )


def notify(message: str, timeout_s: int = 60) -> dict:
    """Send a proactive iMessage notification to the operator via the imessage-bridge push channel.

    Use this to deliver completion alerts, review requests, anomaly warnings,
    or any agent-initiated message that should reach the operator on their phone.

    The message is delivered by ``bun send.ts`` — the same primitive used by
    cron scripts (health check, audit, briefing).  It opens a short-lived
    connection, sends one message, and exits.

    Every attempt (this call, or via ``digest.flush_digest`` which calls this
    function) is recorded to a durable delivery log — never the message text,
    only outcome metadata — readable via ``notify_delivery_status`` (task
    #1183). Logging is best-effort and never blocks or fails a real send.

    Args:
        message: The notification text (max 8000 chars, plain text).
        timeout_s: Seconds to wait before timing out (default 60, max 120).

    Returns:
        {delivered: bool, chars: int, error: str | None}
    """
    from .. import config

    sender = config.verified_agent_identity()
    # An unverified caller is a hub-internal delivery only inside an
    # ``internal_route`` block opened by in-process system code. A block
    # reached through a tool call (e.g. check_anomalies_and_notify invoked
    # over MCP) does not count: that caller is judged as itself (task #1775).
    from .. import tool_surface

    internal = None if tool_surface.in_tool_call() else _internal_route.get()
    route = "notify" if sender else (internal or "notify")
    return notify_as(message, sender=sender, route=route, timeout_s=timeout_s)


_internal_route: ContextVar[str | None] = ContextVar("ua_notify_internal_route", default=None)


@contextmanager
def internal_route(route: str) -> Iterator[None]:
    """Mark ``notify`` calls in this block as the named hub-internal delivery.

    For in-process system senders (anomaly alerts, the review pipeline, queue
    hygiene) that have no agent identity. ``route`` must be one of
    ``operating_profiles.INTERNAL_ROUTES`` or the send is judged as an
    ordinary unverified caller.
    """
    token = _internal_route.set(route)
    try:
        yield
    finally:
        _internal_route.reset(token)


def notify_as(message: str, *, sender: str | None, route: str, timeout_s: int = 60) -> dict:
    """Notify the operator on ``sender``'s behalf, under ``sender``'s comms policy.

    A funnel run's crew reaches the operator only through its liaison (task
    #1735). ``sender=None`` passes only on a hub-internal ``route`` (see
    ``operating_profiles.INTERNAL_ROUTES``); any other unverified caller is
    refused while operating profiles are configured (task #1775). A
    delegation callback passes the delegator.
    """
    from ..operating_profiles import check_operator_contact

    check_operator_contact(sender, route=route)
    result = _notify_impl(message, timeout_s)
    _record_delivery(
        delivered=bool(result.get("delivered")),
        chars=int(result.get("chars") or 0),
        error=result.get("error"),
    )
    return result


def _notify_impl(message: str, timeout_s: int = 60) -> dict:
    message = message.strip()
    if not message:
        raise ToolError("message must not be empty")
    if len(message) > 8000:
        message = message[:8000] + "\n…(truncated)"

    timeout_s = max(10, min(timeout_s, 120))

    script = _send_script()
    _refuse_agent_supplied_bridge(script)
    if not script.exists():
        raise ToolError(
            f"notification bridge is absent: send.ts not found at {script}. "
            f"Set {_BRIDGE_DIR_ENV} to the imessage-bridge project directory."
        )

    # bun must be on PATH; send.ts reads .env from its own project dir
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    # Forward Spectrum credentials if set in our env
    for key in (
        "SPECTRUM_PROJECT_ID",
        "SPECTRUM_PROJECT_SECRET",
        "NOTIFY_TARGET",
        "BRIDGE_ALLOWED_SENDERS",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val

    try:
        res = subprocess.run(
            ["bun", str(script), message],
            cwd=str(_bridge_dir()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if res.returncode != 0:
            stderr = res.stderr.strip()[-300:]
            return {
                "delivered": False,
                "chars": len(message),
                "error": f"send.ts exited {res.returncode}: {stderr}",
            }
        return {"delivered": True, "chars": len(message), "error": None}
    except FileNotFoundError:
        raise ToolError(
            "bun executable not found on system PATH. "
            "Install bun (https://bun.sh) to enable iMessage notifications."
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "delivered": False,
            "chars": len(message),
            "error": f"send.ts timed out after {timeout_s}s",
        }


def notify_delivery_status(limit: int = 20) -> dict[str, Any]:
    """Read-only recent outbound notify/iMessage delivery outcomes (task #1183).

    Operator confirmation of what ``notify`` / ``batch_notify`` actually did —
    success/fail and last-send timestamp — without reading agent journals.
    Read-only: never sends a notification itself. The send path stays exactly
    where it was (``notify`` / ``batch_notify``, MCP/approval-gated); this is
    only a projection of the durable delivery log those tools already write.

    Args:
        limit: Max rows to return, newest first (default 20, max 200).

    Returns:
        {deliveries: [{agent, delivered, chars, error, sent_at}], count,
         last_delivery: {..} | None, success_count, failure_count}
    """
    from .. import storage

    lim = max(1, min(limit, 200))
    conn = storage.connect()
    try:
        conn.executescript(_DELIVERY_LOG_SCHEMA)
        rows = conn.execute(
            "SELECT agent, delivered, chars, error, sent_at FROM notify_delivery_log "
            "ORDER BY id DESC LIMIT ?",
            (lim,),
        ).fetchall()
    finally:
        conn.close()

    deliveries = [
        {
            "agent": row["agent"],
            "delivered": bool(row["delivered"]),
            "chars": row["chars"],
            "error": row["error"],
            "sent_at": row["sent_at"],
        }
        for row in rows
    ]
    success_count = sum(1 for d in deliveries if d["delivered"])
    return {
        "deliveries": deliveries,
        "count": len(deliveries),
        "last_delivery": deliveries[0] if deliveries else None,
        "success_count": success_count,
        "failure_count": len(deliveries) - success_count,
    }
