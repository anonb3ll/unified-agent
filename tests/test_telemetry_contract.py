"""Tests for normalized telemetry event contract, privacy redaction, and fixtures (Task #480)."""

import json

from unified_agent.telemetry.fixtures import (
    FIXTURE_SCENARIOS,
    generate_fixture,
)
from unified_agent.telemetry.producers import (
    producer_gap_report,
)
from unified_agent.telemetry.redaction import (
    BOUNDED_PAYLOAD_LIMIT,
    BOUNDED_STRING_LIMIT,
    redact_sensitive_content,
    sanitize_metadata,
)
from unified_agent.telemetry.schema import (
    CHANNEL_TELEMETRY_PREFIX,
    SpanKind,
    SpanOutcome,
    TelemetrySpan,
    UsageMetrics,
    validate_telemetry_event,
)


def test_telemetry_span_validation_success():
    span = TelemetrySpan(
        trace_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        span_id="01ARZ3NDEKTSV4RRFFQ69G5FA1",
        kind=SpanKind.TOOL,
        name="fs_read",
        agent="Desktop_agent3",
        task_id=480,
        outcome=SpanOutcome.SUCCESS,
        duration_ms=45.2,
        usage=UsageMetrics(tokens_in=120, tokens_out=40, cost_usd=0.00015),
        metadata={"path": "src/unified_agent/storage.py"},
    )
    data = span.to_dict()
    assert data["trace_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert data["kind"] == "tool"
    assert data["outcome"] == "success"
    assert data["usage"]["tokens_total"] == 160
    assert data["duration_ms"] == 45.2
    assert validate_telemetry_event(data) is True


def test_telemetry_outcomes_enumeration():
    expected_outcomes = {
        "success",
        "failure",
        "retry",
        "fallback",
        "quarantined",
        "cooldown",
        "killed",
        "waiting_review",
        "unknown",
    }
    actual_outcomes = {o.value for o in SpanOutcome}
    assert actual_outcomes == expected_outcomes


def test_redaction_secrets_and_keys():
    raw_text = (
        "Encountered key AIzaSyD1234567890abcdef and token Bearer sk-ant-api03-1234567890 in error"
    )
    redacted = redact_sensitive_content(raw_text)
    assert "AIzaSyD1234567890abcdef" not in redacted
    assert "sk-ant-api03-1234567890" not in redacted
    assert "[REDACTED_SECRET]" in redacted or "[REDACTED" in redacted


def test_bounded_string_and_payload_limits():
    oversized_text = "x" * 2000
    bounded = redact_sensitive_content(oversized_text)
    assert len(bounded) <= BOUNDED_STRING_LIMIT

    oversized_metadata = {f"k{i}": "v" * 200 for i in range(50)}
    sanitized = sanitize_metadata(oversized_metadata)
    serialized = json.dumps(sanitized)
    assert len(serialized.encode("utf-8")) <= BOUNDED_PAYLOAD_LIMIT


def test_nested_metadata_is_redacted_and_bounded_with_one_key():
    sanitized = sanitize_metadata(
        {"details": {"headers": [{"authorization": "Bearer " + "s" * 80}] * 100}},
        max_payload_bytes=128,
    )

    assert len(json.dumps(sanitized).encode("utf-8")) <= 128
    assert "Bearer " not in json.dumps(sanitized)


def test_deterministic_fixtures_generation():
    assert set(FIXTURE_SCENARIOS) == {
        "success",
        "failure",
        "retry",
        "fallback",
        "quarantined",
        "cooldown",
    }
    for scenario in FIXTURE_SCENARIOS:
        fixture = generate_fixture(scenario)
        assert fixture["scenario"] == scenario
        assert "spans" in fixture
        assert len(fixture["spans"]) > 0
        for span in fixture["spans"]:
            assert validate_telemetry_event(span) is True


def test_channel_mapping_and_compat():
    span = TelemetrySpan(
        trace_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        span_id="01ARZ3NDEKTSV4RRFFQ69G5FA2",
        kind=SpanKind.RUN,
        name="task_execution",
        agent="Desktop_agent3",
        session_id="sess-abc",
        parent_span_id="01ARZ3NDEKTSV4RRFFQ69G5FA1",
        outcome=SpanOutcome.FAILURE,
        error_kind="TimeoutError",
        error_message="Connection timed out after 30s",
    )
    event_dict = span.to_event_log_entry()
    assert event_dict["channel"].startswith(CHANNEL_TELEMETRY_PREFIX)
    assert event_dict["channel"] == "agent.telemetry.run"
    assert event_dict["originator"] == "Desktop_agent3"
    assert event_dict["episode_id"] is None
    assert event_dict["causal_parent_id"] is None
    assert event_dict["payload"]["parent_span_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FA1"
    assert event_dict["payload"]["session_id"] == "sess-abc"


def test_usage_non_dict_is_rejected():
    base = {
        "trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "span_id": "01ARZ3NDEKTSV4RRFFQ69G5FA1",
        "kind": "tool",
        "name": "fs_read",
        "outcome": "success",
    }
    assert validate_telemetry_event({**base, "usage": None}) is False
    assert validate_telemetry_event({**base, "usage": "not-a-dict"}) is False
    assert validate_telemetry_event({**base, "usage": 42}) is False
    assert validate_telemetry_event({**base, "usage": {"tokens_in": 1, "tokens_out": 2}}) is True


def test_sensitive_metadata_keys_are_fully_redacted():
    sanitized = sanitize_metadata(
        {
            "path": "src/ok.py",
            "api_key": "literally-any-value",
            "nested": {"authorization": "plain-text-header", "count": 3},
            "password": 12345,
        }
    )
    assert sanitized["path"] == "src/ok.py"
    assert sanitized["api_key"] == "[REDACTED_SECRET]"
    assert sanitized["password"] == "[REDACTED_SECRET]"
    assert sanitized["nested"]["authorization"] == "[REDACTED_SECRET]"
    assert sanitized["nested"]["count"] == 3


def test_fixture_trace_ids_are_unique_across_scenarios():
    traces = {generate_fixture(scenario)["trace_id"] for scenario in FIXTURE_SCENARIOS}
    assert len(traces) == len(FIXTURE_SCENARIOS)
    # Historical collision: failure and fallback both started with "FA".
    assert generate_fixture("failure")["trace_id"] != generate_fixture("fallback")["trace_id"]


def test_producer_gap_report():
    report = producer_gap_report()
    assert "producers" in report
    assert "gaps" in report
    assert len(report["producers"]) >= 3  # storage.record_agent_event, executor, monitor
    assert len(report["gaps"]) > 0


# --- task #2255: allowlisted export boundary ----------------------------------

_RAW_SECRET = "sk-ant-api03-" + "Q" * 40


def _boundary_span(**changes) -> TelemetrySpan:
    base = {
        "trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "span_id": "01ARZ3NDEKTSV4RRFFQ69G5FB1",
        "kind": SpanKind.TOOL,
        "name": "governed_tool",
        "outcome": SpanOutcome.FAILURE,
        "error_kind": "gateway_exhausted",
        "error_message": f"upstream said: {_RAW_SECRET} while reading /home/someone/notes.md",
        "metadata": {
            "approval_id": 42,
            "tool_name": "fs_read",
            "attempt": 2,
            "provider_chain": ["ollama", "ai_studio"],
            "operation_key": f"op {_RAW_SECRET}",
            "arguments": {"path": "/home/someone/notes.md", "token": _RAW_SECRET},
            "result": "the whole file body",
            "path": "/home/someone/notes.md",
        },
    }
    return TelemetrySpan(**(base | changes))


def test_export_keeps_only_allowlisted_scalar_metadata(monkeypatch):
    monkeypatch.delenv("UA_TELEMETRY_FREEFORM_METADATA", raising=False)
    meta = _boundary_span().to_dict()["metadata"]
    assert meta["approval_id"] == 42
    assert meta["tool_name"] == "fs_read"
    assert meta["attempt"] == 2
    assert meta["provider_chain"] == ["ollama", "ai_studio"]
    # Arguments, results and free-text paths are not exported; only their count.
    assert not {"arguments", "result", "path"} & set(meta)
    assert meta["_dropped"] == 3
    # An allowlisted value carrying a secret becomes an opaque reference: stable
    # within the process, and not a plain hash anyone could confirm a guess with.
    import hashlib

    ref = meta["operation_key"]
    assert ref.startswith("secret-ref:")
    assert _boundary_span().to_dict()["metadata"]["operation_key"] == ref
    for value in (f"op {_RAW_SECRET}", _RAW_SECRET):
        assert hashlib.sha256(value.encode()).hexdigest()[:16] not in ref
    # A list or mapping under a scalar key is dropped, never stringified.
    nested = _boundary_span(metadata={"tool_name": {"x": 1}, "attempt": [1]}).to_dict()
    assert nested["metadata"] == {"_dropped": 2}


def test_free_text_error_message_needs_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("UA_TELEMETRY_FREEFORM_METADATA", raising=False)
    data = _boundary_span().to_dict()
    assert data["error_kind"] == "gateway_exhausted"
    assert data["error_message"] is None
    monkeypatch.setenv("UA_TELEMETRY_FREEFORM_METADATA", "1")
    data = _boundary_span().to_dict()
    # The opt-in restores the earlier redacted, bounded free-form export.
    assert "upstream said" in data["error_message"]
    assert data["metadata"]["path"] == "/home/someone/notes.md"
    assert data["metadata"]["arguments"]["token"] == "[REDACTED_SECRET]"


def test_synthetic_secret_corpus_never_reaches_any_sink(monkeypatch):
    from unified_agent.telemetry.otel import spans_to_otlp_json

    corpus = [
        _RAW_SECRET,
        "AIzaSyD" + "a" * 32,
        "ghp_" + "b" * 36,
        # Assembled at runtime so the synthetic token is never a source literal.
        "Bearer " + ".".join(("eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxIn0", "c2ln" * 6)),
    ]
    for opt_in in ("", "1"):
        monkeypatch.setenv("UA_TELEMETRY_FREEFORM_METADATA", opt_in)
        for raw in corpus:
            span = _boundary_span(
                error_message=f"failed with {raw}",
                metadata={"operation_key": raw, "tool_name": f"t {raw}", "arguments": raw},
            )
            sinks = [
                span.to_dict(),
                span.to_event_log_entry(),
                spans_to_otlp_json([span]),
            ]
            for sink in sinks:
                assert raw not in json.dumps(sink), (opt_in, raw)
