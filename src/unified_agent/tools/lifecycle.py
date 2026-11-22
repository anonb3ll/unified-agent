"""MCP tools for sanitized lifecycle capture and session resume packets (Task #482)."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config
from ..lifecycle import handoff as session_handoff
from ..lifecycle.capture import record_lifecycle_observation
from ..lifecycle.packet import build_session_packet
from ..lifecycle.schema import LifecycleKind

_log = logging.getLogger(__name__)


def lifecycle_record(
    kind: str,
    session_id: str | None = None,
    task_id: int | None = None,
    agent: str | None = None,
    tool: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Record one sanitized lifecycle observation (start/end/tool/compaction/stop/handoff).

    Optional and fire-and-forget: disabled capture, exclusions, and storage
    failures return ``accepted: false`` rather than raising. Duplicate
    ``idempotency_key`` values are idempotent. Observations are historical
    context, never instruction or approval authority. Transcripts and raw
    prompts are dropped, not stored.
    """
    label = config.verified_agent_identity() or agent
    result = record_lifecycle_observation(
        kind=kind,
        session_id=session_id,
        task_id=task_id,
        agent=label,
        tool=tool,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if (
        str(kind or "").strip() == LifecycleKind.COMPACTION.value
        and session_id
        and not result.get("duplicate")
    ):
        # A compaction starts a new context epoch (task #2397). Capture must
        # never break, so a foreign or unknown session only skips the bump.
        try:
            epoch = session_handoff.bump_epoch(session_id, reason="compaction", agent=label)
        except Exception as exc:  # noqa: BLE001 - fire-and-forget capture
            _log.info("lifecycle_record: context epoch not advanced: %s", type(exc).__name__)
        else:
            result = {**result, "context_epoch": epoch["epoch"]}
    return result


def session_packet(
    task_id: int | None = None,
    session_id: str | None = None,
    agent: str | None = None,
) -> dict:
    """Build a bounded resume packet from existing control-plane state.

    Derived from the task, worktree, delegations, approvals, recent events,
    relevant memory, and prior handoff — never from a raw transcript. The
    packet is labeled historical context and does not override instructions
    or approval state. Two agents asking for the same task receive the same
    comparable body.
    """
    if task_id is None and not session_id:
        raise ToolError("pass task_id or session_id")
    label = config.verified_agent_identity() or agent
    return build_session_packet(task_id=task_id, session_id=session_id, agent=label)
