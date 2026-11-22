"""In-process state machine for one bug-worker run.

Task DB status remains authoritative for queue visibility; this tracks the
worker phase within a single claim→handoff cycle.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

WORKER_STATES = (
    "queued",
    "claimed",
    "reproducing",
    "patching",
    "validating",
    "waiting_review",
    "blocked",
    "escalated",
)

TERMINAL_WORKER_STATES = {"waiting_review", "blocked", "escalated"}

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"claimed", "blocked"},
    "claimed": {"reproducing", "blocked", "escalated"},
    "reproducing": {"patching", "blocked", "escalated"},
    "patching": {"validating", "blocked", "escalated"},
    "validating": {"waiting_review", "blocked", "escalated"},
    "waiting_review": set(),
    "blocked": set(),
    "escalated": set(),
}


def validate_transition(current: str, new: str) -> None:
    if current not in WORKER_STATES:
        raise ToolError(f"unknown worker state {current!r}")
    if new not in WORKER_STATES:
        raise ToolError(f"unknown worker state {new!r}")
    if new not in TRANSITIONS.get(current, set()):
        raise ToolError(f"invalid worker transition {current!r} -> {new!r}")


def transition_worker_state(current: str, new: str) -> str:
    validate_transition(current, new)
    return new
