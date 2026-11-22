"""MCP 2.x runtime health, version-skew, and reconnect telemetry (UA #866).

Collects bounded runtime telemetry from the MCP 2.x connection lifecycle:
- Negotiated protocol version
- Client/server capability set
- Transport mode
- Subscription counts (board, tasks, runs)
- Progress liveness (emitted events, last timestamp, active state)
- Reconnect attempts, successes, and failures
- Version-skew failures with actionable diagnostics

Provides read-only doctor/health classification:
- 'healthy': modern protocol negotiated, capabilities aligned, no version-skew failures, reconnects clean.
- 'degraded': older protocol version, recovered reconnect incidents, or stale snapshots detected.
- 'incompatible': version-skew failure, unsupported protocol version, or capability rejection.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from mcp.types import DEFAULT_NEGOTIATED_VERSION, LATEST_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

MAX_HISTORY_ENTRIES = 20


class McpHealthStatus(StrEnum):
    """Runtime health classification for the MCP 2.x service."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class McpTelemetryStore:
    negotiated_protocol_version: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    client_capabilities: dict[str, Any] = field(default_factory=dict)
    server_capabilities: dict[str, Any] = field(
        default_factory=lambda: {
            "tools": True,
            "tool_annotations": True,
            "structured_outputs": True,
            "elicitation": True,
            "progress": True,
            "resources": True,
            "resource_subscriptions": True,
        }
    )
    transport_mode: str = "streamable_http"
    subscriptions: dict[str, int] = field(
        default_factory=lambda: {"total": 0, "board": 0, "tasks": 0, "runs": 0}
    )
    progress_events_emitted: int = 0
    last_progress_at: str | None = None
    progress_active: bool = False
    reconnect_attempts: int = 0
    reconnect_successes: int = 0
    reconnect_failures: int = 0
    last_reconnect_at: str | None = None
    stale_snapshots_count: int = 0
    last_session_id: str | None = None
    last_agent_label: str | None = None
    session_touches_count: int = 0
    version_skew_failures: list[dict[str, Any]] = field(default_factory=list)
    recent_reconnects: list[dict[str, Any]] = field(default_factory=list)
    recent_stale_snapshots: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_telemetry = McpTelemetryStore()


def reset_telemetry() -> None:
    """Reset telemetry to pristine state (for test isolation)."""
    global _telemetry
    with _lock:
        _telemetry = McpTelemetryStore()


def record_connection(
    *,
    client_name: str,
    client_version: str | None = None,
    protocol_version: str | None = None,
    client_capabilities: dict[str, Any] | None = None,
    transport_mode: str = "streamable_http",
) -> None:
    """Record an established client connection and capability handshake."""
    with _lock:
        _telemetry.client_name = client_name
        _telemetry.client_version = client_version
        _telemetry.negotiated_protocol_version = protocol_version or str(LATEST_PROTOCOL_VERSION)
        if client_capabilities is not None:
            _telemetry.client_capabilities = {
                k: bool(v)
                if isinstance(v, (bool, int))
                else (v if isinstance(v, dict) else str(v)[:50])
                for k, v in client_capabilities.items()
                if "token" not in k.lower() and "secret" not in k.lower() and "key" not in k.lower()
            }
        _telemetry.transport_mode = transport_mode


def record_progress(
    progress: float, total: float | None = None, message: str | None = None
) -> None:
    """Record a progress notification milestone."""
    with _lock:
        _telemetry.progress_events_emitted += 1
        _telemetry.last_progress_at = _now_iso()
        _telemetry.progress_active = total is None or progress < total


def record_subscription_counts(
    *,
    total: int,
    **resource_counts: int,
) -> None:
    """Record active subscription counts by registry resource key."""
    with _lock:
        counts = {key: max(0, int(value)) for key, value in resource_counts.items()}
        for legacy_key in ("board", "tasks", "runs"):
            counts.setdefault(legacy_key, 0)
        _telemetry.subscriptions = {"total": max(0, total), **counts}


def record_reconnect(*, attempt: int = 1, success: bool = True, detail: str = "") -> None:
    """Record a reconnect event and its resolution."""
    ts = _now_iso()
    with _lock:
        _telemetry.reconnect_attempts += max(1, attempt)
        if success:
            _telemetry.reconnect_successes += 1
        else:
            _telemetry.reconnect_failures += 1
        _telemetry.last_reconnect_at = ts
        event = {
            "timestamp": ts,
            "attempt": attempt,
            "success": success,
            "detail": detail[:200],
        }
        _telemetry.recent_reconnects.append(event)
        if len(_telemetry.recent_reconnects) > MAX_HISTORY_ENTRIES:
            _telemetry.recent_reconnects.pop(0)


def record_stale_snapshot(
    *, uri: str, client_rev: str | None = None, server_rev: str | None = None
) -> None:
    """Record a detected stale resource snapshot during reconnect reconciliation."""
    ts = _now_iso()
    with _lock:
        _telemetry.stale_snapshots_count += 1
        entry = {
            "timestamp": ts,
            "uri": uri[:100],
            "client_revision": str(client_rev)[:50] if client_rev else None,
            "server_revision": str(server_rev)[:50] if server_rev else None,
        }
        _telemetry.recent_stale_snapshots.append(entry)
        if len(_telemetry.recent_stale_snapshots) > MAX_HISTORY_ENTRIES:
            _telemetry.recent_stale_snapshots.pop(0)


def record_version_skew(
    *,
    client_name: str,
    client_version: str | None = None,
    requested_version: str | None = None,
    reason: str = "",
) -> None:
    """Record a version skew incident (unsupported protocol or capability mismatch)."""
    ts = _now_iso()
    with _lock:
        entry = {
            "timestamp": ts,
            "client_name": client_name[:50],
            "client_version": str(client_version)[:50] if client_version else "unknown",
            "requested_version": str(requested_version)[:50] if requested_version else "none",
            "reason": reason[:200],
        }
        _telemetry.version_skew_failures.append(entry)
        if len(_telemetry.version_skew_failures) > MAX_HISTORY_ENTRIES:
            _telemetry.version_skew_failures.pop(0)


def record_session_touch(session_id: str | None, agent_label: str | None = None) -> None:
    """Record an observed HTTP middleware request with session/agent identity."""
    with _lock:
        _telemetry.session_touches_count += 1
        if session_id:
            _telemetry.last_session_id = session_id[:100]
        if agent_label:
            _telemetry.last_agent_label = agent_label[:50]


def evaluate_health() -> tuple[McpHealthStatus, list[str]]:
    """Evaluate current MCP 2.x runtime health and actionable evidence."""
    with _lock:
        skew_failures = list(_telemetry.version_skew_failures)
        reconnect_fails = _telemetry.reconnect_failures
        stale_count = _telemetry.stale_snapshots_count
        proto = _telemetry.negotiated_protocol_version
        reconnect_attempts = _telemetry.reconnect_attempts

    evidence: list[str] = []

    if skew_failures:
        for f in skew_failures[-3:]:
            c_name = f["client_name"]
            c_ver = f["client_version"]
            req_ver = f["requested_version"]
            reason = f["reason"]
            evidence.append(
                f"Version skew: client {c_name} ({c_ver}) requested {req_ver} — {reason}"
            )
        return McpHealthStatus.INCOMPATIBLE, evidence

    if reconnect_fails > 0:
        evidence.append(
            f"Reconnect degradation: {reconnect_fails} reconnect failures recorded out of {reconnect_attempts} attempts"
        )
        return McpHealthStatus.DEGRADED, evidence

    if stale_count > 0:
        evidence.append(
            f"Stale snapshots: {stale_count} out-of-sync snapshot events detected and reconciled"
        )
        return McpHealthStatus.DEGRADED, evidence

    if proto is not None and proto < str(DEFAULT_NEGOTIATED_VERSION):
        evidence.append(
            f"Legacy protocol negotiated: {proto} is older than target {DEFAULT_NEGOTIATED_VERSION}"
        )
        return McpHealthStatus.DEGRADED, evidence

    return McpHealthStatus.HEALTHY, evidence


def get_telemetry_snapshot() -> dict[str, Any]:
    """Return a sanitized, bounded telemetry snapshot distinguishing health states."""
    with _lock:
        data = asdict(_telemetry)

    status, evidence = evaluate_health()

    return {
        "status": status.value,
        "negotiated_protocol_version": data["negotiated_protocol_version"],
        "target_protocol_version": str(LATEST_PROTOCOL_VERSION),
        "client": {
            "name": data["client_name"],
            "version": data["client_version"],
            "capabilities": data["client_capabilities"],
        },
        "server_capabilities": data["server_capabilities"],
        "transport_mode": data["transport_mode"],
        "subscriptions": data["subscriptions"],
        "progress": {
            "events_emitted": data["progress_events_emitted"],
            "last_event_at": data["last_progress_at"],
            "active": data["progress_active"],
        },
        "reconnect": {
            "attempts": data["reconnect_attempts"],
            "successes": data["reconnect_successes"],
            "failures": data["reconnect_failures"],
            "last_reconnect_at": data["last_reconnect_at"],
            "recent_events": data["recent_reconnects"],
        },
        "version_skew": {
            "failures_count": len(data["version_skew_failures"]),
            "recent_failures": data["version_skew_failures"],
        },
        "session": {
            "last_id": data["last_session_id"],
            "last_agent": data["last_agent_label"],
            "touches_count": data["session_touches_count"],
        },
        "stale_snapshots": {
            "count": data["stale_snapshots_count"],
            "recent_events": data["recent_stale_snapshots"],
        },
        "actionable_evidence": evidence,
    }
