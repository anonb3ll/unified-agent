"""Validator for the bounded ``mobile-action.v1`` request/result envelopes.

Contract-first and side-effect free, exactly like :mod:`mobile_v1`. This module
parses and validates the envelopes the iPhone relay exchanges with the hub for
task #779 (Watch slice 2 — safe actions). It never fetches hub data, resolves
an ``item_ref``, evaluates policy, or records an event; the webui api layer
does all of that. See ``docs/mobile-action-contract-v1.md``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

REQUEST_SCHEMA_VERSION = "mobile-action.v1"
RESULT_SCHEMA_VERSION = "mobile-action-result.v1"

MAX_REQUEST_BYTES = 2 * 1024
MAX_ACTION_ID = 64
MAX_PROJECTION_ID = 64
MAX_ITEM_REF = 128
MAX_REASON = 32

# v1 allowlist — one safe verb. `redirect`/`dispatch` land in a later slice and
# route through the approval queue, not this auto-apply path.
ACTIONS = frozenset({"acknowledge"})

RESULT_STATUSES = frozenset({"applied", "noop", "rejected"})
REJECT_REASONS = frozenset(
    {
        "unknown_action",
        "stale_request",
        "policy_denied",
        "invalid_envelope",
    }
)

# HTTP status for each rejection reason (applied/noop are always 200).
REJECT_HTTP_STATUS = {
    "invalid_envelope": 400,
    "unknown_action": 400,
    "stale_request": 400,
    "policy_denied": 403,
}

# Default replay window: an intent older than this (or too far in the future) is
# refused. Kept small — the wrist tap and the hub call are seconds apart.
REPLAY_WINDOW_S = 600
FUTURE_SKEW_S = 60

_ACTION_ID = re.compile(r"^ma_[A-Za-z0-9._-]{1,61}$")
_PROJECTION_ID = re.compile(r"^mp_[A-Za-z0-9._-]{1,61}$")
_ITEM_REF = re.compile(r"^attention:[a-z0-9_]+:[A-Za-z0-9._-]{1,96}$")


class MobileActionError(ValueError):
    """Raised when an untrusted mobile-action envelope violates the contract."""


@dataclass(frozen=True)
class MobileActionRequest:
    schema_version: str
    action_id: str
    action: str
    item_ref: str
    projection_id: str
    issued_at: str


@dataclass(frozen=True)
class MobileActionResult:
    schema_version: str
    action_id: str
    status: str
    reason: str | None
    audit_ref: str | None
    acknowledged_until: str | None


def parse_mobile_action(
    raw: object,
    *,
    now: datetime | None = None,
    max_bytes: int = MAX_REQUEST_BYTES,
    replay_window_s: int = REPLAY_WINDOW_S,
) -> MobileActionRequest:
    """Parse and validate one untrusted ``mobile-action.v1`` request.

    Raises :class:`MobileActionError` on any violation — fail closed, no
    partial acceptance. ``now`` is injectable for deterministic tests.
    """
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_REQUEST_BYTES
    ):
        raise MobileActionError("max_bytes must be a positive limit within the envelope cap")

    body = _decode(raw, max_bytes)
    _exact_keys(
        body,
        {
            "schema_version",
            "action_id",
            "action",
            "item_ref",
            "projection_id",
            "issued_at",
        },
        "top-level",
    )
    if body.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise MobileActionError("schema_version must be mobile-action.v1")

    action_id = _string(body, "action_id", MAX_ACTION_ID)
    if not _ACTION_ID.fullmatch(action_id):
        raise MobileActionError("action_id is not a valid opaque nonce")

    action = _string(body, "action", 32)
    if action not in ACTIONS:
        raise MobileActionError("action is not allowlisted")

    item_ref = _string(body, "item_ref", MAX_ITEM_REF)
    if not _ITEM_REF.fullmatch(item_ref):
        raise MobileActionError("item_ref is not an opaque attention reference")

    projection_id = _string(body, "projection_id", MAX_PROJECTION_ID)
    if not _PROJECTION_ID.fullmatch(projection_id):
        raise MobileActionError("projection_id is not a valid opaque reference")

    issued_at = _timestamp(body, "issued_at")
    now_dt = _as_utc(now or datetime.now(UTC))
    issued_dt = _parse_timestamp(issued_at)
    age_s = (now_dt - issued_dt).total_seconds()
    window = (
        replay_window_s
        if isinstance(replay_window_s, int) and replay_window_s > 0
        else REPLAY_WINDOW_S
    )
    if age_s > window or age_s < -FUTURE_SKEW_S:
        raise MobileActionError("issued_at is outside the replay window")

    return MobileActionRequest(
        schema_version=REQUEST_SCHEMA_VERSION,
        action_id=action_id,
        action=action,
        item_ref=item_ref,
        projection_id=projection_id,
        issued_at=issued_at,
    )


def build_action_result(
    action_id: str,
    status: str,
    *,
    reason: str | None = None,
    audit_ref: str | None = None,
    acknowledged_until: str | None = None,
) -> dict[str, Any]:
    """Construct a validated ``mobile-action-result.v1`` wire mapping."""
    if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
        raise MobileActionError("action_id is not a valid opaque nonce")
    if status not in RESULT_STATUSES:
        raise MobileActionError("status is not a valid result status")
    if status == "rejected":
        if reason not in REJECT_REASONS:
            raise MobileActionError("a rejected result requires an allowlisted reason")
        audit_ref = None
        acknowledged_until = None
    else:
        if reason is not None:
            raise MobileActionError("only a rejected result carries a reason")
    if reason is not None and len(reason) > MAX_REASON:
        raise MobileActionError("reason exceeds the bound")
    if audit_ref is not None and (not isinstance(audit_ref, str) or len(audit_ref) > MAX_ITEM_REF):
        raise MobileActionError("audit_ref must be a bounded string")
    if acknowledged_until is not None:
        # Round-trip through the timestamp validator.
        acknowledged_until = _validated_timestamp(acknowledged_until)

    result = MobileActionResult(
        schema_version=RESULT_SCHEMA_VERSION,
        action_id=action_id,
        status=status,
        reason=reason,
        audit_ref=audit_ref,
        acknowledged_until=acknowledged_until,
    )
    return asdict(result)


def parse_mobile_action_result(raw: object) -> MobileActionResult:
    """Parse a ``mobile-action-result.v1`` mapping (used by the Swift-mirroring tests)."""
    body = _decode(raw, MAX_REQUEST_BYTES)
    _exact_keys(
        body,
        {
            "schema_version",
            "action_id",
            "status",
            "reason",
            "audit_ref",
            "acknowledged_until",
        },
        "result top-level",
    )
    if body.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise MobileActionError("schema_version must be mobile-action-result.v1")
    action_id = _string(body, "action_id", MAX_ACTION_ID)
    if not _ACTION_ID.fullmatch(action_id):
        raise MobileActionError("action_id is not a valid opaque nonce")
    status = body.get("status")
    if status not in RESULT_STATUSES:
        raise MobileActionError("status is not a valid result status")
    reason = body.get("reason")
    if reason is not None and (reason not in REJECT_REASONS):
        raise MobileActionError("reason is not allowlisted")
    audit_ref = body.get("audit_ref")
    if audit_ref is not None and not isinstance(audit_ref, str):
        raise MobileActionError("audit_ref must be a string or null")
    ack_until = body.get("acknowledged_until")
    if ack_until is not None:
        ack_until = _validated_timestamp(ack_until)
    return MobileActionResult(
        schema_version=RESULT_SCHEMA_VERSION,
        action_id=action_id,
        status=status,
        reason=reason,
        audit_ref=audit_ref,
        acknowledged_until=ack_until,
    )


def new_action_id(now: datetime | None = None) -> str:
    """Mint an ``action_id`` nonce (used by the iPhone relay / tests)."""
    now_dt = _as_utc(now or datetime.now(UTC))
    return f"ma_{int(now_dt.timestamp())}_{uuid.uuid4().hex[:8]}"[:MAX_ACTION_ID]


# --- helpers (kept local to mirror mobile_v1's self-contained posture) -------


def _decode(raw: object, max_bytes: int) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="strict")
    if isinstance(raw, str):
        if len(raw.encode()) > max_bytes:
            raise MobileActionError("envelope exceeds the size limit")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MobileActionError(f"envelope is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise MobileActionError("envelope must be a JSON object")
    encoded = json.dumps(raw, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > max_bytes:
        raise MobileActionError("envelope exceeds the size limit")
    return raw


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise MobileActionError(f"{label} must be an object")
    missing = expected - set(value)
    unexpected = set(value) - expected
    if missing:
        raise MobileActionError(f"{label} is missing {sorted(missing)}")
    if unexpected:
        raise MobileActionError(f"unexpected field in {label}: {sorted(unexpected)}")


def _string(value: dict[str, Any], key: str, limit: int) -> str:
    got = value.get(key)
    if not isinstance(got, str) or not got.strip():
        raise MobileActionError(f"{key} must be a non-empty string")
    if len(got) > limit:
        raise MobileActionError(f"{key} exceeds {limit} characters")
    return got


def _timestamp(value: dict[str, Any], key: str) -> str:
    raw = _string(value, key, 40)
    return _validated_timestamp(raw, key)


def _validated_timestamp(raw: str, key: str = "timestamp") -> str:
    try:
        parsed = _parse_timestamp(raw)
    except ValueError as exc:
        raise MobileActionError(f"{key} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MobileActionError(f"{key} must include a timezone")
    return raw


def _parse_timestamp(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def acknowledged_until(now: datetime | None = None, ttl_s: int = 14400) -> str:
    """The ``acknowledged_until`` timestamp for an ack recorded at ``now``."""
    now_dt = _as_utc(now or datetime.now(UTC))
    ttl = ttl_s if isinstance(ttl_s, int) and ttl_s > 0 else 14400
    return (now_dt + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
