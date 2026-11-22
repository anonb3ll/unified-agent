"""Lifecycle observation vocabulary (Task #482)."""

from __future__ import annotations

from enum import StrEnum

CHANNEL_LIFECYCLE_PREFIX = "agent.lifecycle"


class LifecycleKind(StrEnum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PROMPT_METADATA = "prompt_metadata"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    COMPACTION = "compaction"
    STOP = "stop"
    HANDOFF = "handoff"


LIFECYCLE_KINDS = frozenset(k.value for k in LifecycleKind)

# Client payloads may carry these keys; they are dropped, not redacted-in-place,
# so a resume packet can never reconstruct a transcript or prompt.
EXCLUDED_PAYLOAD_KEYS = frozenset(
    {
        "transcript",
        "transcript_path",
        "transcript_uri",
        "prompt",
        "messages",
        "system",
        "developer",
        "raw_prompt",
        "completion",
        "authorization",
        "token",
        "api_key",
        "password",
        "secret",
    }
)
