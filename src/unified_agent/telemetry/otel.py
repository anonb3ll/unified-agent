"""OTLP/JSON trace export without the OpenTelemetry SDK (UA #668).

Maps already-redacted ``TelemetrySpan`` records into OTLP JSON plus a
small gen-ai attribute set. Network I/O happens only when a caller injects
``transport``. This module never authorizes effects or mutates tasks.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .redaction import redact_sensitive_content
from .schema import (
    SpanKind,
    SpanOutcome,
    TelemetrySpan,
    export_error_message,
    export_metadata,
)

OTEL_EXPORT_ENV = "UA_H2_OTEL_EXPORT"
_DEFAULT_BUFFERED = 256
_SERVICE_NAME = "unified-agent"

_KIND_TO_OTEL = {
    SpanKind.RUN: 1,  # INTERNAL
    SpanKind.CHAIN: 1,
    SpanKind.MODEL: 3,  # CLIENT
    SpanKind.TOOL: 3,
    SpanKind.EXECUTOR: 3,
}

_GEN_AI_OPERATION = {
    SpanKind.RUN: "invoke_agent",
    SpanKind.CHAIN: "invoke_agent",
    SpanKind.MODEL: "chat",
    SpanKind.TOOL: "execute_tool",
    SpanKind.EXECUTOR: "execute_tool",
}


#: Authority bindings carried as OTLP attributes when a span knows them.
_BINDING_ATTRS = (
    ("approval_id", "ua.approval_id"),
    ("policy_version", "ua.policy.version"),
    ("policy_hash", "ua.policy.hash"),
    ("device_key_fingerprint", "ua.device.key_fingerprint"),
    ("artifact_sha", "ua.artifact.sha"),
)


def _hex_id(value: str, nbytes: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[: nbytes * 2]


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        typed: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int) and not isinstance(value, bool):
        typed = {"intValue": str(value)}
    elif isinstance(value, float):
        typed = {"doubleValue": value}
    else:
        typed = {"stringValue": redact_sensitive_content(str(value), max_len=256)}
    return {"key": key, "value": typed}


def _kind(span: TelemetrySpan) -> SpanKind | None:
    if isinstance(span.kind, SpanKind):
        return span.kind
    try:
        return SpanKind(str(span.kind))
    except ValueError:
        return None


def span_to_otlp(span: TelemetrySpan) -> dict[str, Any]:
    """One OTLP JSON span. Original UA ids stay in attributes."""
    kind = _kind(span)
    outcome = span.outcome.value if isinstance(span.outcome, SpanOutcome) else str(span.outcome)
    metadata = export_metadata(span.metadata)
    attrs = [
        item
        for item in (
            _attr("ua.trace_id", span.trace_id),
            _attr("ua.span_id", span.span_id),
            _attr(
                "gen_ai.operation.name",
                _GEN_AI_OPERATION.get(kind, "invoke_agent") if kind else "invoke_agent",
            ),
            _attr("gen_ai.provider.name", span.provider),
            _attr("gen_ai.request.model", span.model),
            _attr("gen_ai.agent.name", span.agent),
            _attr("ua.span.kind", kind.value if kind else str(span.kind)),
            _attr("ua.span.outcome", outcome),
            _attr("ua.task_id", span.task_id),
            _attr("error.type", span.error_kind),
            _attr("error.message", export_error_message(span.error_message)),
            *(_attr(attr, metadata.get(key)) for key, attr in _BINDING_ATTRS),
        )
        if item is not None
    ]
    usage = span.usage
    if usage is not None:
        tokens_in = getattr(usage, "tokens_in", 0)
        tokens_out = getattr(usage, "tokens_out", 0)
        if tokens_in:
            item = _attr("gen_ai.usage.input_tokens", tokens_in)
            if item:
                attrs.append(item)
        if tokens_out:
            item = _attr("gen_ai.usage.output_tokens", tokens_out)
            if item:
                attrs.append(item)
    payload: dict[str, Any] = {
        "traceId": _hex_id(span.trace_id, 16),
        "spanId": _hex_id(span.span_id, 8),
        "name": str(span.name)[:128],
        "kind": _KIND_TO_OTEL.get(kind, 1) if kind else 1,
        "attributes": attrs,
        "status": {
            "code": 2 if outcome == SpanOutcome.FAILURE.value else 1,
        },
    }
    if span.parent_span_id:
        payload["parentSpanId"] = _hex_id(span.parent_span_id, 8)
    if span.duration_ms is not None:
        dur = _attr("ua.duration_ms", float(span.duration_ms))
        if dur:
            payload["attributes"].append(dur)
    return payload


def spans_to_otlp_json(spans: Sequence[TelemetrySpan]) -> dict[str, Any]:
    """OTLP/JSON ExportTraceServiceRequest body. No SDK.

    Do not run ``sanitize_metadata`` on this tree: that helper stringifies
    nested dicts past depth 3, which would collapse OTLP ``spans``.
    Span string fields are already redacted by ``TelemetrySpan.to_dict``.
    """
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": _SERVICE_NAME},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "unified-agent.horizon2"},
                        "spans": [span_to_otlp(span) for span in spans],
                    }
                ],
            }
        ]
    }


@dataclass
class OTLPTraceExporter:
    """Opt-in OTLP/JSON sink. Default off; transport is injectable."""

    transport: Callable[[dict[str, Any]], int] | None = None
    max_buffered: int = _DEFAULT_BUFFERED
    _buffer: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_buffered < 0:
            raise ValueError("max_buffered must be non-negative")
        if os.environ.get(OTEL_EXPORT_ENV, "").strip() != "1":
            raise RuntimeError(
                f"OTLP export requires {OTEL_EXPORT_ENV}=1; default remains disabled"
            )

    def export(self, spans: Sequence[TelemetrySpan]) -> int:
        payload = spans_to_otlp_json(spans)
        if self.transport is not None:
            return int(self.transport(payload))
        self._buffer.append(payload)
        if len(self._buffer) > self.max_buffered:
            del self._buffer[: len(self._buffer) - self.max_buffered]
        return len(spans)
