"""Normalized telemetry event schema and span contracts (Task #480)."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .redaction import redact_sensitive_content, sanitize_metadata

CHANNEL_TELEMETRY_PREFIX = "agent.telemetry"

#: Explicit opt-in to the earlier free-form export (task #2255): every
#: metadata key and the error message, still redacted and bounded.
FREEFORM_METADATA_ENV = "UA_TELEMETRY_FREEFORM_METADATA"

#: The only metadata a span exports by default, each as one scalar. Tool
#: arguments, results, paths and other free text are not on the list.
EXPORTED_METADATA_KEYS = frozenset(
    {
        # run and graph identity
        "run_id",
        "graph_name",
        "graph_version",
        "definition_digest_prefix",
        "task_id",
        "claimed_by",
        "expected_profile",
        "budget_revision",
        # governed operations
        "operation_key",
        "idempotency_key_prefix",
        "tool_name",
        "approval_id",
        "approval_status",
        "input_digest",
        "output_digest",
        "result_digest",
        "payload_digest",
        "duplicate",
        "recovered",
        "retry_class",
        "transient_attempt_count",
        "checkpoint_sequence",
        "attempt",
        "max_retries",
        "cooldown_seconds",
        "provider",
        "model",
        "provider_chain",
        "policy",
        "quarantine_action",
        # authority bindings, exported only when a producer actually knows them
        "policy_version",
        "policy_hash",
        "device_key_fingerprint",
        "artifact_sha",
    }
)
#: Allowlisted keys whose value is a short list of scalar strings.
_LIST_KEYS = frozenset({"provider_chain"})
_MAX_EXPORT_STR = 128
_MAX_EXPORT_LIST = 10
_SECRET_REF_KEY = secrets.token_bytes(32)
_DROP = object()


def freeform_export_enabled() -> bool:
    return os.environ.get(FREEFORM_METADATA_ENV, "").strip() == "1"


def secret_ref(value: str) -> str:
    """An opaque, process-local reference to a secret-bearing value.

    Keyed with a random per-process key, so equal values correlate within
    one process while nobody can confirm a guessed secret against the ref,
    as they could against a plain hash.
    """
    digest = hmac.new(_SECRET_REF_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"secret-ref:{digest[:16]}"


def _export_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP
    if isinstance(value, str):
        if redact_sensitive_content(value, max_len=len(value) + 1) != value:
            return secret_ref(value)
        return value[:_MAX_EXPORT_STR]
    return _DROP


def _export_value(key: str, value: Any) -> Any:
    if key in _LIST_KEYS and isinstance(value, list | tuple):
        items = [_export_scalar(item) for item in value[:_MAX_EXPORT_LIST]]
        if any(item is _DROP or not isinstance(item, str) for item in items):
            return _DROP
        return items
    return _export_scalar(value)


def export_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    """The span metadata that may leave the process (task #2255).

    By default only ``EXPORTED_METADATA_KEYS`` with scalar values are kept,
    and a secret-bearing string becomes a ``secret_ref``. Everything else is
    dropped and counted in ``_dropped``, without its name or value. With
    ``UA_TELEMETRY_FREEFORM_METADATA=1`` the earlier redacted free-form
    export applies instead.
    """
    if not meta:
        return {}
    if freeform_export_enabled():
        return sanitize_metadata(meta)
    out: dict[str, Any] = {}
    dropped = 0
    for key, value in meta.items():
        exported = _export_value(key, value) if key in EXPORTED_METADATA_KEYS else _DROP
        if exported is _DROP:
            dropped += 1
        else:
            out[key] = exported
    if dropped:
        out["_dropped"] = dropped
    return out


def export_error_message(message: str | None) -> str | None:
    """Free-text error detail is exported only under the free-form opt-in."""
    if not message or not freeform_export_enabled():
        return None
    return redact_sensitive_content(message)


class SpanKind(StrEnum):
    RUN = "run"
    MODEL = "model"
    TOOL = "tool"
    EXECUTOR = "executor"
    CHAIN = "chain"


class SpanOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    RETRY = "retry"
    FALLBACK = "fallback"
    QUARANTINED = "quarantined"
    COOLDOWN = "cooldown"
    KILLED = "killed"
    WAITING_REVIEW = "waiting_review"
    UNKNOWN = "unknown"


@dataclass
class UsageMetrics:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    cost_usd: float | None = None

    def __post_init__(self):
        if self.tokens_total == 0 and (self.tokens_in > 0 or self.tokens_out > 0):
            self.tokens_total = self.tokens_in + self.tokens_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_total,
            "cost_usd": round(self.cost_usd, 6) if self.cost_usd is not None else None,
        }


@dataclass
class TelemetrySpan:
    trace_id: str
    span_id: str
    kind: SpanKind | str
    name: str
    outcome: SpanOutcome | str = SpanOutcome.UNKNOWN
    parent_span_id: str | None = None
    agent: str | None = None
    session_id: str | None = None
    task_id: int | None = None
    delegation_id: int | None = None
    model: str | None = None
    provider: str | None = None
    duration_ms: float | None = None
    usage: UsageMetrics = field(default_factory=UsageMetrics)
    error_kind: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind_val = self.kind.value if isinstance(self.kind, SpanKind) else str(self.kind)
        outcome_val = (
            self.outcome.value if isinstance(self.outcome, SpanOutcome) else str(self.outcome)
        )
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": kind_val,
            "name": str(self.name)[:128],
            "outcome": outcome_val,
            "agent": self.agent,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "delegation_id": self.delegation_id,
            "model": self.model,
            "provider": self.provider,
            "duration_ms": round(self.duration_ms, 2) if self.duration_ms is not None else None,
            "usage": self.usage.to_dict() if isinstance(self.usage, UsageMetrics) else self.usage,
            "error_kind": str(self.error_kind)[:64] if self.error_kind else None,
            "error_message": export_error_message(self.error_message),
            "metadata": export_metadata(self.metadata),
        }

    def to_event_log_entry(self) -> dict[str, Any]:
        kind_val = self.kind.value if isinstance(self.kind, SpanKind) else str(self.kind)
        channel = f"{CHANNEL_TELEMETRY_PREFIX}.{kind_val}"
        # Leave episode_id / causal_parent_id unset so record_event uses the
        # live episode context; parent_span_id stays in the payload only.
        return {
            "channel": channel,
            "payload": self.to_dict(),
            "originator": self.agent,
            "episode_id": None,
            "causal_parent_id": None,
        }


def validate_telemetry_event(data: dict[str, Any]) -> bool:
    """Validate that a dictionary conforms to the TelemetrySpan schema."""
    required = ("trace_id", "span_id", "kind", "name", "outcome")
    for field_name in required:
        if field_name not in data or not data[field_name]:
            return False

    valid_kinds = {k.value for k in SpanKind}
    if data["kind"] not in valid_kinds:
        return False

    valid_outcomes = {o.value for o in SpanOutcome}
    if data["outcome"] not in valid_outcomes:
        return False

    if "usage" in data:
        if not isinstance(data["usage"], dict):
            return False
        if not isinstance(data["usage"].get("tokens_in", 0), int):
            return False
        if not isinstance(data["usage"].get("tokens_out", 0), int):
            return False

    return True
