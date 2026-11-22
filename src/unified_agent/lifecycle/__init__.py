"""Sanitized lifecycle capture and session resume packets (Task #482).

Captured observations are historical context, never authority. System,
developer, and user instructions plus approval state remain authoritative.
There is no second memory store: observations go to ``event_log`` and the
resume packet is derived from existing task, delegation, approval, session,
event, and memory rows.
"""

from .capture import record_lifecycle_observation
from .packet import PACKET_SCHEMA, build_session_packet
from .schema import (
    CHANNEL_LIFECYCLE_PREFIX,
    LIFECYCLE_KINDS,
    LifecycleKind,
)

__all__ = [
    "CHANNEL_LIFECYCLE_PREFIX",
    "LIFECYCLE_KINDS",
    "LifecycleKind",
    "PACKET_SCHEMA",
    "build_session_packet",
    "record_lifecycle_observation",
]
