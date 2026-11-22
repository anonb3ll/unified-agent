"""Attention-item acknowledgements — the one safe mutation the Watch can drive.

An acknowledgement is a TTL'd, operator-visible suppression of a single
attention item. It is recorded as an append-only ``mobile.attention.ack`` event
(tamper-evident, retained, replay-safe via ``dedupe_key``) — no schema
migration, no new table. ``attention_payload`` filters items whose ref carries
a non-expired ack. See ``docs/mobile-action-contract-v1.md``.

Acknowledging says "I've seen it", never "it's handled": the underlying task /
approval / cycle is untouched.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from .. import events
from .mobile_v1 import mobile_item_ref, mobile_kind

ACK_CHANNEL = "mobile.attention.ack"
DEFAULT_TTL_S = 14400  # 4 hours
# How far back to scan for acks. Older acks have expired by definition.
_SCAN_LIMIT = 500


def ack_ttl_seconds() -> int:
    """Acknowledgement lifetime, overridable via ``UA_ATTENTION_ACK_TTL_S``."""
    raw = os.environ.get("UA_ATTENTION_ACK_TTL_S")
    if raw is None:
        return DEFAULT_TTL_S
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_TTL_S
    return val if val > 0 else DEFAULT_TTL_S


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return _as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_acknowledgement(
    item_ref: str,
    *,
    action: str,
    acked_by: str,
    action_id: str,
    now: datetime | None = None,
    ttl_s: int | None = None,
) -> tuple[str, str, bool]:
    """Record one acknowledgement event.

    Returns ``(ulid, acknowledged_until, created)``. ``created`` is False when
    ``action_id`` was already seen — the same ULID comes back and no second row
    is written (replay protection is the event log's ``dedupe_key``).
    """
    now_dt = _as_utc(now or datetime.now(UTC))
    ttl = ttl_s if isinstance(ttl_s, int) and ttl_s > 0 else ack_ttl_seconds()
    acknowledged_until = _iso(now_dt + timedelta(seconds=ttl))
    payload = {
        "item_ref": item_ref,
        "action": action,
        "acked_by": acked_by,
        "acknowledged_until": acknowledged_until,
        "recorded_at": _iso(now_dt),
    }
    ulid, created = events.record_event_once(
        ACK_CHANNEL,
        payload=payload,
        originator=acked_by,
        dedupe_key=action_id,
    )
    return ulid, acknowledged_until, created


def active_acknowledgements(now: datetime | None = None) -> dict[str, str]:
    """Map of ``item_ref`` -> ``acknowledged_until`` for non-expired acks.

    Rows come back oldest-first, so a later ack for the same ref wins (re-acking
    extends the TTL). Malformed payloads are skipped, never fatal — a bad row
    must not un-suppress everything.
    """
    now_dt = _as_utc(now or datetime.now(UTC))
    try:
        rows = events.query_events(channels=[ACK_CHANNEL], limit=_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 - suppression is best-effort, never fatal
        return {}

    active: dict[str, str] = {}
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        ref = payload.get("item_ref")
        until = payload.get("acknowledged_until")
        if not isinstance(ref, str) or not isinstance(until, str):
            continue
        try:
            until_dt = _as_utc(datetime.fromisoformat(until.replace("Z", "+00:00")))
        except ValueError:
            continue
        if until_dt <= now_dt:
            active.pop(ref, None)
        else:
            active[ref] = until
    return active


def filter_acknowledged(
    items: list[dict], now: datetime | None = None
) -> tuple[list[dict], list[str]]:
    """Split attention items into ``(surfaced, suppressed_item_refs)``."""
    active = active_acknowledgements(now)
    if not active:
        return items, []
    kept: list[dict] = []
    suppressed: list[str] = []
    for item in items:
        kind = str(item.get("kind", ""))
        subject = item.get("source_subject") or item.get("item_ref")
        # A wrist ack names the mobile kind (task #1672 aliases); a web ack may
        # name the hub kind. Either one suppresses the item.
        refs = {mobile_item_ref(kind, subject), mobile_item_ref(mobile_kind(kind), subject)}
        hit = next((ref for ref in sorted(refs) if ref in active), None)
        if hit is not None:
            suppressed.append(hit)
        else:
            kept.append(item)
    return kept, suppressed
