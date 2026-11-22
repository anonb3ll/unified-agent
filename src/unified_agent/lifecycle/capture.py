"""Optional, fire-and-forget lifecycle observation ingest (Task #482)."""

from __future__ import annotations

from typing import Any

from .. import config, events, storage
from ..telemetry.redaction import sanitize_metadata
from .schema import (
    CHANNEL_LIFECYCLE_PREFIX,
    EXCLUDED_PAYLOAD_KEYS,
    LIFECYCLE_KINDS,
)

_TOOL = "lifecycle_record"


def record_lifecycle_observation(
    *,
    kind: str,
    session_id: str | None = None,
    task_id: int | None = None,
    agent: str | None = None,
    tool: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record one sanitized lifecycle observation. Never raises.

    Duplicate ``idempotency_key`` values return the first committed result
    without writing a second ``event_log`` row.
    """
    try:
        return _record(
            kind=kind,
            session_id=session_id,
            task_id=task_id,
            agent=agent,
            tool=tool,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except Exception:
        return {"accepted": False, "reason": "capture_failed"}


def _record(
    *,
    kind: str,
    session_id: str | None,
    task_id: int | None,
    agent: str | None,
    tool: str | None,
    payload: dict[str, Any] | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    kind_val = str(kind or "").strip()
    if kind_val not in LIFECYCLE_KINDS:
        return {"accepted": False, "reason": "unknown_kind"}
    if not config.lifecycle_capture_enabled():
        return {"accepted": False, "reason": "disabled"}
    if kind_val in config.lifecycle_exclude_kinds():
        return {"accepted": False, "reason": "excluded"}
    tool_name = str(tool).strip() if tool else None
    if tool_name and tool_name in config.lifecycle_exclude_tools():
        return {"accepted": False, "reason": "excluded"}

    body = _sanitize_payload(payload)
    event_payload: dict[str, Any] = {
        "kind": kind_val,
        "session_id": session_id,
        "task_id": task_id,
        "tool": tool_name,
        "payload": body,
    }
    result_base = {
        "accepted": True,
        "duplicate": False,
        "kind": kind_val,
        "session_id": session_id,
        "task_id": task_id,
    }

    idem_key = _idempotency_key(session_id, kind_val, idempotency_key)
    if idem_key:
        conn = storage.connect()
        try:
            if not storage.try_claim_idempotency(conn, idem_key, _TOOL):
                cached = storage.get_idempotency_record(conn, idem_key, _TOOL) or {}
                return {**cached, "duplicate": True}
            conn.commit()

            ulid = events.record_event(
                channel=f"{CHANNEL_LIFECYCLE_PREFIX}.{kind_val}",
                payload=event_payload,
                originator=agent,
                episode_id=session_id,
                notify=True,
            )
            result = {**result_base, "event_id": ulid}
            storage.record_idempotency(conn, idem_key, _TOOL, result)
            conn.commit()
            return result
        finally:
            conn.close()

    ulid = events.record_event(
        channel=f"{CHANNEL_LIFECYCLE_PREFIX}.{kind_val}",
        payload=event_payload,
        originator=agent,
        episode_id=session_id,
        notify=True,
    )
    return {**result_base, "event_id": ulid}


def _idempotency_key(session_id: str | None, kind: str, client_key: str | None) -> str | None:
    if not client_key or not str(client_key).strip():
        return None
    sid = session_id or "_"
    return f"lifecycle:{sid}:{kind}:{str(client_key).strip()}"


def _strip_excluded_keys(value: Any) -> Any:
    """Recursively drop EXCLUDED_PAYLOAD_KEYS (case-insensitive) from dicts/lists."""
    if isinstance(value, dict):
        return {
            str(key): _strip_excluded_keys(item)
            for key, item in value.items()
            if str(key).lower() not in EXCLUDED_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [_strip_excluded_keys(item) for item in value]
    return value


def _sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    stripped = _strip_excluded_keys(payload)
    if not isinstance(stripped, dict):
        return {}
    return sanitize_metadata(stripped)
