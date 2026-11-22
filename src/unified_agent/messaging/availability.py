"""Is this recipient able to receive right now (task #2392).

A label send is durable: it is written to the mailbox and read whenever the
recipient next looks, and an idle session can be woken and even reopened. So a
quiet recipient is not an unavailable one, and this module never treats
quietness as a refusal.

Four verdicts, and only one of them blocks a send:

* ``available`` -- a live heartbeat (agent online or idle), an open session, or
  an online node for the label, so delivery reaches it now;
* ``degraded`` -- the hub knows the label but nothing of it is live now; the
  message is stored and waits, which the sender should hear;
* ``unknown`` -- no evidence either way, or the resolver itself failed. This
  fails open: an unreadable liveness store must not refuse mail;
* ``unavailable`` -- the label is not an addressable identity in this tenant at
  all, which addressing already refuses. It is reported so an office fan-out
  names the member it cannot write.

This is a read. It writes no liveness, records no event and reaps nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import config, node_registry, registry, storage

AVAILABLE = "available"
DEGRADED = "degraded"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

_OPEN = ("starting", "running", "waiting")
# A heartbeat at or above this rank proves the recipient is reachable now.
_LIVE_AGENT = frozenset({"online", "idle"})
_NODE_RANK = ("online", "idle", "stale", "offline", "unknown")


@dataclass(frozen=True)
class Preflight:
    """One recipient's readiness, with the reading that produced it."""

    label: str
    verdict: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"agent": self.label, "availability": self.verdict, "reason": self.reason}

    @property
    def warning(self) -> str | None:
        """The sentence to hand the sender, or None when the send reaches it now."""
        if self.verdict in (AVAILABLE, UNAVAILABLE):
            return None
        if self.verdict == DEGRADED:
            return (
                f"{self.label} is not live right now ({self.reason}); "
                "the message is stored and will wait for it"
            )
        return (
            f"could not confirm that {self.label} can receive ({self.reason}); "
            "the message is stored anyway"
        )


def _node_statuses(label: str, now: datetime) -> list[str]:
    listed = node_registry.read_nodes()
    return [
        node_registry.node_status(info, now=now)
        for info in listed.values()
        if info.get("agent") == label
    ]


def preflight(conn: sqlite3.Connection, label: str, *, now: datetime | None = None) -> Preflight:
    """Read one recipient's availability. Never raises: a bad read is ``unknown``."""
    reference = now or datetime.now(UTC)
    name = str(label or "").strip()
    if not name:
        return Preflight("", UNAVAILABLE, "no recipient label")
    try:
        row = conn.execute("SELECT last_seen FROM agents WHERE label=?", (name,)).fetchone()
        open_sessions = conn.execute(
            f"SELECT COUNT(*) FROM agent_sessions WHERE agent=? AND status IN "
            f"({','.join('?' for _ in _OPEN)})",
            (name, *_OPEN),
        ).fetchone()[0]
        nodes = _node_statuses(name, reference)
    except (sqlite3.Error, OSError, ValueError) as exc:
        # Fail open: an unreadable liveness source cannot refuse someone's mail.
        return Preflight(name, UNKNOWN, f"resolver failed ({type(exc).__name__})")
    agent = registry.derive_status(row["last_seen"], now=reference) if row is not None else None
    if open_sessions or agent in _LIVE_AGENT or "online" in nodes:
        return Preflight(name, AVAILABLE, "live now")
    if row is not None or nodes:
        detail = f"agent {agent}" if agent is not None else "no heartbeat lived"
        if nodes:
            best = next(status for status in _NODE_RANK if status in nodes)
            detail += f", node {best}"
        return Preflight(name, DEGRADED, detail)
    if not config.agent_label_is_well_formed(name):
        return Preflight(name, UNAVAILABLE, "not an addressable identity in this tenant")
    return Preflight(name, UNKNOWN, "no liveness record yet")


def preflight_label(label: str, *, now: datetime | None = None) -> Preflight:
    """``preflight`` on its own short-lived connection. Never raises."""
    try:
        conn = storage.connect()
    except Exception as exc:  # noqa: BLE001 - an unreadable store fails open
        return Preflight(
            str(label or "").strip(), UNKNOWN, f"resolver failed ({type(exc).__name__})"
        )
    try:
        return preflight(conn, label, now=now)
    finally:
        conn.close()


def reject_reason(result: Preflight) -> str:
    """The refusal text for a verdict that must not be written, or an empty string."""
    if result.verdict != UNAVAILABLE:
        return ""
    return f"{result.label!r} cannot receive mail: {result.reason}"
