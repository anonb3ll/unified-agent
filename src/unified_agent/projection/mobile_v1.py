"""Validator for the bounded mobile.v1 projection.

This module is contract-first and side-effect free. It does not fetch hub data,
send notifications, or authorize actions. The iPhone adapter will be responsible
for producing this shape after an authenticated read.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = "mobile.v1"
MAX_WATCH_BYTES = 8 * 1024
MAX_IPHONE_BYTES = 32 * 1024
MAX_PROJECTION_ID = 64
MAX_REFERENCE = 128
MAX_SAFE_TITLE = 64
MAX_SAFE_SUMMARY = 96
MAX_SOURCE_HOST = 64
MAX_HUB_ORIGIN = 32
MAX_ITEMS = 20

FRESHNESS = frozenset({"live", "stale", "offline", "error"})
SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = MappingProxyType({name: index for index, name in enumerate(SEVERITIES)})
HEALTH_STATES = frozenset({"healthy", "degraded", "unavailable", "unknown"})
ATTENTION_KINDS = frozenset(
    {
        "review_wait",
        "plan_approval_wait",
        "human_merge_wait",
        "blocked_task",
        "executor_degraded",
        "inconsistent_completion",
        "approval",
        "anomaly",
        "stale_worktree",
        "stale_claim",
        "identity_drift",
        "stale_agent",
        "autonomous_cycle_stopped",
    }
)
SAFE_TITLES = MappingProxyType(
    {
        "review_wait": "Review needed",
        "plan_approval_wait": "Plan approval needed",
        "human_merge_wait": "Human merge needed",
        "blocked_task": "Task blocked",
        "executor_degraded": "Executor degraded",
        "inconsistent_completion": "Review state needs attention",
        "approval": "Human decision needed",
        "anomaly": "System anomaly",
        "stale_worktree": "Stale work detected",
        "stale_claim": "Stale claim detected",
        "identity_drift": "Identity check needed",
        "stale_agent": "Agent connection stale",
        "autonomous_cycle_stopped": "Automation stopped",
    }
)
# Hub attention kinds shown on the wrist under an existing kind (task #1672).
# Deployed Watch clients reject a projection that carries a kind outside their
# compiled allowlist (MobileProjection.swift), so a new hub kind maps onto a
# generic one rather than extending the wire contract.
MOBILE_KIND_ALIASES = MappingProxyType({"evidence_gap": "anomaly"})


def mobile_kind(kind: str) -> str:
    """The mobile.v1 kind a hub attention kind is shown as."""
    return MOBILE_KIND_ALIASES.get(kind, kind)


SAFE_NOTIFICATION_TITLE = "Unified-agent needs attention"
_PROJECTION_ID = re.compile(r"^mp_[A-Za-z0-9._-]{1,61}$")
_ITEM_REF = re.compile(r"^attention:[a-z0-9_]+:[A-Za-z0-9._-]{1,96}$")
_DEEP_LINK = re.compile(r"^ua://control-center(?:/[a-z0-9_./:-]{1,110})?$")


def mobile_item_ref(kind: str, source_subject: object) -> str:
    """The opaque ``attention:<kind>:<subject>`` reference the Watch sees.

    One derivation shared by :func:`build_mobile_projection` and the attention
    acknowledgement filter (task #779) so a wrist ``item_ref`` round-trips to
    the same hub-side item it names.
    """
    subj = str(source_subject if source_subject not in (None, "") else "item")
    subj_clean = re.sub(r"[^A-Za-z0-9._-]", "_", subj).strip("._-") or "item"
    ref = f"attention:{kind}:{subj_clean}"[:MAX_REFERENCE]
    if not _ITEM_REF.fullmatch(ref):
        ref = f"attention:{kind}:item"
    return ref


class MobileProjectionError(ValueError):
    """Raised when an untrusted mobile projection violates mobile.v1."""


@dataclass(frozen=True)
class MobileSource:
    source_host: str
    hub_origin: str
    surface: str


@dataclass(frozen=True)
class MobileAttentionItem:
    item_ref: str
    kind: str
    severity: str
    age_s: int | None
    safe_title: str
    deep_link_ref: str


@dataclass(frozen=True)
class MobileAttention:
    total: int
    highest_severity: str
    items: tuple[MobileAttentionItem, ...]


@dataclass(frozen=True)
class MobileHealth:
    state: str
    safe_summary: str


@dataclass(frozen=True)
class MobileNotification:
    event_ref: str
    safe_title: str
    deep_link_ref: str


@dataclass(frozen=True)
class MobileFallback:
    surface: str
    deep_link_ref: str


@dataclass(frozen=True)
class MobileProjection:
    schema_version: str
    projection_id: str
    generated_at: str
    expires_at: str
    freshness: str
    source: MobileSource
    attention: MobileAttention
    health: MobileHealth
    notification: MobileNotification | None
    error: str | None
    fallback: MobileFallback


def parse_mobile_projection(raw: object, *, max_bytes: int = MAX_IPHONE_BYTES) -> MobileProjection:
    """Parse and validate one untrusted mobile.v1 projection.

    Pass ``MAX_WATCH_BYTES`` at the Watch boundary and leave the default for
    the larger iPhone projection boundary.
    """
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_IPHONE_BYTES
    ):
        raise MobileProjectionError("max_bytes must be a positive limit no larger than iPhone")
    body = _decode(raw)
    _exact_keys(
        body,
        {
            "schema_version",
            "projection_id",
            "generated_at",
            "expires_at",
            "freshness",
            "source",
            "attention",
            "health",
            "notification",
            "error",
            "fallback",
        },
        "top-level",
    )
    if body.get("schema_version") != SCHEMA_VERSION:
        raise MobileProjectionError("schema_version must be mobile.v1")

    projection_id = _string(body, "projection_id", MAX_PROJECTION_ID)
    if not _PROJECTION_ID.fullmatch(projection_id):
        raise MobileProjectionError("projection_id is not a valid opaque reference")

    generated_at = _timestamp(body, "generated_at")
    expires_at = _timestamp(body, "expires_at")
    if _parse_timestamp(expires_at) <= _parse_timestamp(generated_at):
        raise MobileProjectionError("expires_at must follow generated_at")

    freshness = body.get("freshness")
    if freshness not in FRESHNESS:
        raise MobileProjectionError("freshness is invalid")

    source = _parse_source(body.get("source"))
    attention = _parse_attention(body.get("attention"))
    health = _parse_health(body.get("health"))
    notification = _parse_notification(body.get("notification"))

    error = body.get("error")
    if error is not None:
        error = _string_value(error, "error", 64)
        if error not in {"hub_unavailable", "auth_required", "invalid_projection", "offline"}:
            raise MobileProjectionError("error code is not allowlisted")

    fallback = _parse_fallback(body.get("fallback"))
    result = MobileProjection(
        schema_version=SCHEMA_VERSION,
        projection_id=projection_id,
        generated_at=generated_at,
        expires_at=expires_at,
        freshness=freshness,
        source=source,
        attention=attention,
        health=health,
        notification=notification,
        error=error,
        fallback=fallback,
    )
    encoded = json.dumps(to_wire(result), separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > max_bytes:
        boundary = "Watch" if max_bytes == MAX_WATCH_BYTES else "iPhone"
        raise MobileProjectionError(f"projection exceeds the {boundary} size limit")
    return result


def to_wire(projection: MobileProjection) -> dict[str, Any]:
    """Return a plain JSON-compatible mobile.v1 mapping."""
    if not isinstance(projection, MobileProjection):
        raise MobileProjectionError("expected a MobileProjection")
    wire = asdict(projection)
    wire["attention"]["items"] = list(wire["attention"]["items"])
    return wire


def _decode(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MobileProjectionError(f"projection is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise MobileProjectionError("projection must be a JSON object")
    return raw


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise MobileProjectionError(f"{label} must be an object")
    missing = expected - set(value)
    unexpected = set(value) - expected
    if missing:
        raise MobileProjectionError(f"{label} is missing {sorted(missing)}")
    if unexpected:
        raise MobileProjectionError(f"unexpected field in {label}: {sorted(unexpected)}")


def _string(value: dict[str, Any], key: str, limit: int) -> str:
    return _string_value(value.get(key), key, limit)


def _string_value(value: object, key: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MobileProjectionError(f"{key} must be a non-empty string")
    if len(value) > limit:
        raise MobileProjectionError(f"{key} exceeds {limit} characters")
    return value


def _timestamp(value: dict[str, Any], key: str) -> str:
    raw = _string(value, key, 40)
    try:
        parsed = _parse_timestamp(raw)
    except ValueError as exc:
        raise MobileProjectionError(f"{key} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MobileProjectionError(f"{key} must include a timezone")
    return raw


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_source(raw: object) -> MobileSource:
    if not isinstance(raw, dict):
        raise MobileProjectionError("source must be an object")
    _exact_keys(raw, {"source_host", "hub_origin", "surface"}, "source")
    surface = _string(raw, "surface", 32)
    if surface != "unified-agent":
        raise MobileProjectionError("source surface is not allowlisted")
    return MobileSource(
        source_host=_string(raw, "source_host", MAX_SOURCE_HOST),
        hub_origin=_string(raw, "hub_origin", MAX_HUB_ORIGIN),
        surface=surface,
    )


def _parse_attention(raw: object) -> MobileAttention:
    if not isinstance(raw, dict):
        raise MobileProjectionError("attention must be an object")
    _exact_keys(raw, {"total", "highest_severity", "items"}, "attention")
    total = raw.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 10_000:
        raise MobileProjectionError("attention total must be a bounded non-negative integer")
    highest = raw.get("highest_severity")
    if highest not in {*SEVERITIES, "none"}:
        raise MobileProjectionError("highest_severity is invalid")
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or len(items_raw) > MAX_ITEMS:
        raise MobileProjectionError("attention items must be a bounded list")
    items = tuple(_parse_item(item) for item in items_raw)
    if len(items) != total:
        raise MobileProjectionError("attention total must equal item count")
    expected = (
        "none"
        if not items
        else min((item.severity for item in items), key=SEVERITY_RANK.__getitem__)
    )
    if highest != expected:
        raise MobileProjectionError("highest_severity does not match items")
    return MobileAttention(total=total, highest_severity=highest, items=items)


def _parse_item(raw: object) -> MobileAttentionItem:
    if not isinstance(raw, dict):
        raise MobileProjectionError("attention item must be an object")
    _exact_keys(
        raw,
        {"item_ref", "kind", "severity", "age_s", "safe_title", "deep_link_ref"},
        "attention item",
    )
    item_ref = _string(raw, "item_ref", MAX_REFERENCE)
    if not _ITEM_REF.fullmatch(item_ref):
        raise MobileProjectionError("item_ref is not an opaque attention reference")
    kind = _string(raw, "kind", 40)
    if kind not in ATTENTION_KINDS:
        raise MobileProjectionError("attention kind is not allowlisted")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise MobileProjectionError("attention severity is invalid")
    age = raw.get("age_s")
    if age is not None and (
        isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 31_536_000
    ):
        raise MobileProjectionError("age_s must be a bounded non-negative integer or null")
    safe_title = _string(raw, "safe_title", MAX_SAFE_TITLE)
    if safe_title != SAFE_TITLES[kind]:
        raise MobileProjectionError("attention safe_title is not allowlisted")
    deep_link = _deep_link(raw.get("deep_link_ref"))
    return MobileAttentionItem(item_ref, kind, severity, age, safe_title, deep_link)


def _parse_health(raw: object) -> MobileHealth:
    if not isinstance(raw, dict):
        raise MobileProjectionError("health must be an object")
    _exact_keys(raw, {"state", "safe_summary"}, "health")
    state = _string(raw, "state", 16)
    if state not in HEALTH_STATES:
        raise MobileProjectionError("health state is invalid")
    summary = _string(raw, "safe_summary", MAX_SAFE_SUMMARY)
    if summary not in {
        "Service available",
        "Service degraded",
        "Service unavailable",
        "Service status unknown",
    }:
        raise MobileProjectionError("health safe_summary is not allowlisted")
    return MobileHealth(state, summary)


def _parse_notification(raw: object) -> MobileNotification | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MobileProjectionError("notification must be an object or null")
    _exact_keys(raw, {"event_ref", "safe_title", "deep_link_ref"}, "notification")
    event_ref = _string(raw, "event_ref", MAX_REFERENCE)
    if not _ITEM_REF.fullmatch(event_ref):
        raise MobileProjectionError("notification event_ref is invalid")
    safe_title = _string(raw, "safe_title", MAX_SAFE_TITLE)
    if safe_title != SAFE_NOTIFICATION_TITLE:
        raise MobileProjectionError("notification safe_title is not allowlisted")
    return MobileNotification(event_ref, safe_title, _deep_link(raw.get("deep_link_ref")))


def _parse_fallback(raw: object) -> MobileFallback:
    if not isinstance(raw, dict):
        raise MobileProjectionError("fallback must be an object")
    _exact_keys(raw, {"surface", "deep_link_ref"}, "fallback")
    surface = _string(raw, "surface", 16)
    if surface not in {"iphone", "web"}:
        raise MobileProjectionError("fallback surface is invalid")
    return MobileFallback(surface, _deep_link(raw.get("deep_link_ref")))


def _deep_link(value: object) -> str:
    ref = _string_value(value, "deep_link_ref", MAX_REFERENCE)
    if not _DEEP_LINK.fullmatch(ref):
        raise MobileProjectionError("deep_link_ref must be an opaque ua reference")
    return ref


def build_mobile_projection(
    attention_data: dict[str, Any] | None = None,
    health_data: dict[str, Any] | None = None,
    *,
    source_host: str | None = None,
    hub_origin: str | None = None,
    freshness: str = "live",
    error_code: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int = 300,
) -> MobileProjection:
    """Construct and return a validated MobileProjection from internal hub data."""
    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    generated_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now_dt + timedelta(seconds=max(10, ttl_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")

    proj_id_raw = f"mp_{int(now_dt.timestamp())}_{uuid.uuid4().hex[:8]}"
    projection_id = proj_id_raw[:MAX_PROJECTION_ID]

    source_host_val = (source_host or "local").strip()[:MAX_SOURCE_HOST] or "local"
    hub_origin_val = (hub_origin or "local").strip()[:MAX_HUB_ORIGIN] or "local"
    source = MobileSource(
        source_host=source_host_val,
        hub_origin=hub_origin_val,
        surface="unified-agent",
    )

    items_list: list[MobileAttentionItem] = []
    if attention_data and isinstance(attention_data, dict):
        raw_items = attention_data.get("items", [])
        if isinstance(raw_items, list):
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                kind = mobile_kind(str(raw_item.get("kind", "")))
                if kind not in ATTENTION_KINDS:
                    continue
                severity = str(raw_item.get("severity", "medium"))
                if severity not in SEVERITIES:
                    severity = "medium"
                age_raw = raw_item.get("age_s")
                age_s: int | None = None
                if isinstance(age_raw, int) and 0 <= age_raw <= 31_536_000:
                    age_s = age_raw
                safe_title = SAFE_TITLES[kind]

                item_ref = mobile_item_ref(
                    kind, raw_item.get("source_subject", raw_item.get("item_ref"))
                )

                deep_link = f"ua://control-center/attention/{kind}"
                items_list.append(
                    MobileAttentionItem(
                        item_ref=item_ref,
                        kind=kind,
                        severity=severity,
                        age_s=age_s,
                        safe_title=safe_title,
                        deep_link_ref=deep_link,
                    )
                )
                if len(items_list) >= MAX_ITEMS:
                    break

    total = len(items_list)
    highest_severity = (
        "none"
        if not items_list
        else min((item.severity for item in items_list), key=SEVERITY_RANK.__getitem__)
    )
    attention = MobileAttention(
        total=total,
        highest_severity=highest_severity,
        items=tuple(items_list),
    )

    # Health
    health_state = "unknown"
    if health_data and isinstance(health_data, dict):
        state_in = str(health_data.get("state", health_data.get("status", ""))).lower()
        if state_in in HEALTH_STATES:
            health_state = state_in
        elif health_data.get("summary", {}).get("status") in HEALTH_STATES:
            health_state = health_data["summary"]["status"]
        elif health_data.get("error") or health_data.get("errors"):
            health_state = "degraded"
        else:
            health_state = "healthy"
    elif health_data is not None:
        health_state = "healthy"

    safe_summary_map = {
        "healthy": "Service available",
        "degraded": "Service degraded",
        "unavailable": "Service unavailable",
        "unknown": "Service status unknown",
    }
    safe_summary = safe_summary_map.get(health_state, "Service status unknown")
    health = MobileHealth(state=health_state, safe_summary=safe_summary)

    # Notification
    notification: MobileNotification | None = None
    if highest_severity in ("critical", "high") and items_list:
        top_item = min(items_list, key=lambda it: SEVERITY_RANK[it.severity])
        notification = MobileNotification(
            event_ref=top_item.item_ref,
            safe_title=SAFE_NOTIFICATION_TITLE,
            deep_link_ref="ua://control-center",
        )

    # Error code
    valid_error: str | None = None
    if error_code in {"hub_unavailable", "auth_required", "invalid_projection", "offline"}:
        valid_error = error_code

    freshness_val = freshness if freshness in FRESHNESS else "live"

    return MobileProjection(
        schema_version=SCHEMA_VERSION,
        projection_id=projection_id,
        generated_at=generated_at,
        expires_at=expires_at,
        freshness=freshness_val,
        source=source,
        attention=attention,
        health=health,
        notification=notification,
        error=valid_error,
        fallback=MobileFallback(surface="iphone", deep_link_ref="ua://control-center"),
    )
