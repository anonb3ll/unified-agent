from __future__ import annotations

import pytest

from unified_agent.horizon2 import export_and_replay
from unified_agent.telemetry.otel import OTEL_EXPORT_ENV, OTLPTraceExporter, spans_to_otlp_json
from unified_agent.telemetry.schema import SpanKind, SpanOutcome, TelemetrySpan, UsageMetrics


def _span(**changes) -> TelemetrySpan:
    base = {
        "trace_id": "trace-1",
        "span_id": "span-1",
        "kind": SpanKind.MODEL,
        "name": "chat",
        "outcome": SpanOutcome.SUCCESS,
        "provider": "codex",
        "model": "gpt-test",
        "usage": UsageMetrics(tokens_in=3, tokens_out=5),
    }
    return TelemetrySpan(**(base | changes))


def test_otlp_json_maps_gen_ai_attributes_without_sdk():
    payload = spans_to_otlp_json(
        [
            _span(),
            _span(
                span_id="span-2",
                kind=SpanKind.TOOL,
                name="tool",
                outcome=SpanOutcome.FAILURE,
                parent_span_id="span-1",
                error_kind="timeout",
            ),
        ]
    )
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans[0]["traceId"]) == 32
    assert len(spans[0]["spanId"]) == 16
    keys = {item["key"] for item in spans[0]["attributes"]}
    assert "gen_ai.operation.name" in keys
    assert "gen_ai.request.model" in keys
    assert "gen_ai.usage.input_tokens" in keys
    assert spans[1]["status"]["code"] == 2
    assert spans[1]["parentSpanId"]


def test_otlp_exporter_requires_opt_in_and_uses_injected_transport(monkeypatch):
    with pytest.raises(RuntimeError, match=OTEL_EXPORT_ENV):
        OTLPTraceExporter()
    monkeypatch.setenv(OTEL_EXPORT_ENV, "1")
    seen: list[dict] = []
    exporter = OTLPTraceExporter(transport=lambda body: seen.append(body) or 2)
    assert exporter.export([_span()]) == 2
    assert seen[0]["resourceSpans"]


def test_export_and_replay_exports_then_rescores(monkeypatch):
    monkeypatch.setenv(OTEL_EXPORT_ENV, "1")
    seen: list[int] = []
    exporter = OTLPTraceExporter(
        transport=lambda body: seen.append(len(body["resourceSpans"])) or 1
    )
    result = export_and_replay(
        [
            _span(),
            _span(span_id="span-2", kind=SpanKind.TOOL, name="tool", outcome=SpanOutcome.FAILURE),
        ],
        exporter,
    )
    assert seen == [1]
    assert result.successful == result.failed == 1
    assert result.score == 0.5


def test_otlp_exports_known_authority_bindings_and_no_free_text(monkeypatch):
    # Task #2255: approval, policy, device key and artifact bindings travel
    # when known; unknown stays absent rather than guessed.
    monkeypatch.delenv("UA_TELEMETRY_FREEFORM_METADATA", raising=False)
    known = _span(
        error_kind="timeout",
        error_message="free text from a tool result",
        metadata={
            "approval_id": 7,
            "policy_version": "v12",
            "policy_hash": "a" * 64,
            "device_key_fingerprint": "SHA256:abc",
            "artifact_sha": "b" * 40,
            "arguments": {"q": "raw tool input"},
        },
    )
    [bound, unknown] = spans_to_otlp_json([known, _span(span_id="span-2")])["resourceSpans"][0][
        "scopeSpans"
    ][0]["spans"]
    attrs = {a["key"]: a["value"] for a in bound["attributes"]}
    assert attrs["ua.approval_id"] == {"intValue": "7"}
    assert attrs["ua.policy.version"] == {"stringValue": "v12"}
    assert attrs["ua.policy.hash"] == {"stringValue": "a" * 64}
    assert attrs["ua.device.key_fingerprint"] == {"stringValue": "SHA256:abc"}
    assert attrs["ua.artifact.sha"] == {"stringValue": "b" * 40}
    assert "error.message" not in attrs
    assert "raw tool input" not in str(bound)
    assert not {k for k in (a["key"] for a in unknown["attributes"]) if k.startswith("ua.policy")}
    assert "ua.approval_id" not in {a["key"] for a in unknown["attributes"]}
