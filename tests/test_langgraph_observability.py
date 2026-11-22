"""Acceptance tests for the LangGraph observability and evaluation seam."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from unified_agent import config
from unified_agent.langgraph_observability import (
    ExporterDecision,
    InMemoryEvaluationSink,
    LangGraphObservability,
    LangSmithTraceExporter,
    NullTraceExporter,
    TraceExporterError,
    langsmith_export_enabled,
)
from unified_agent.langgraph_pilot import PilotEvent, PilotRun
from unified_agent.telemetry.schema import SpanKind, SpanOutcome, validate_telemetry_event

RUN_ID = "01JQ9Z6Y5K7M2N4P6R8T0V2X4Z"
DEFINITION = "a" * 64


def _run() -> PilotRun:
    return PilotRun(
        run_id=RUN_ID,
        task_id=42,
        task_updated_at="2026-08-26T11:00:00.000000+00:00",
        claimed_by="Desktop_Agent4",
        claim_expires_at="2026-08-26T12:00:00+00:00",
        expected_profile="developer",
        required_capabilities=("web_search",),
        budget_revision="budget-v1",
        trace_id="trace-observability",
        query="bounded observability query",
    )


def test_project_events_emit_redacted_correlated_spans():
    emitted: list = []
    sink = InMemoryEvaluationSink()
    obs = LangGraphObservability(
        evaluation=sink,
        exporter=NullTraceExporter(),
        emit=lambda span: emitted.append(span) or span.span_id,
    )
    events = [
        PilotEvent("node_completed", "intake", {"input_digest": "d" * 64}),
        PilotEvent(
            "approval_interrupted",
            "governed_tool",
            {
                "approval_id": "9",
                "tool_name": "web_search",
                "operation_key": "read-evidence",
                "arguments": {"query": "SECRET=abcdefghijklmnop"},
            },
        ),
        PilotEvent(
            "run_completed",
            None,
            {"duplicate": False, "output_digest": "c" * 64, "recovered": False},
        ),
    ]

    spans = obs.project_events(
        _run(),
        events,
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )

    assert len(spans) >= 3
    assert all(validate_telemetry_event(span.to_dict()) for span in spans)
    assert spans[0].kind == SpanKind.RUN
    assert spans[0].outcome == SpanOutcome.SUCCESS
    assert spans[0].trace_id == "trace-observability"
    assert spans[0].task_id == 42
    assert spans[0].metadata["definition_digest_prefix"] == DEFINITION[:12]
    assert spans[0].metadata["run_id"] == RUN_ID
    child = next(span for span in spans if span.kind == SpanKind.TOOL)
    assert child.parent_span_id == spans[0].span_id
    assert child.outcome == SpanOutcome.WAITING_REVIEW
    blob = str([span.to_dict() for span in spans])
    assert "abcdefghijklmnop" not in blob
    assert "SECRET=" not in blob or "REDACTED" in blob or "arguments" not in blob
    assert any(record["metric"] == "run_completed" for record in sink.records)
    assert any(record["metric"] == "approval_interrupted" for record in sink.records)
    assert len(emitted) == len(spans)


def test_denial_projects_failure_and_evaluation_metric():
    sink = InMemoryEvaluationSink()
    obs = LangGraphObservability(evaluation=sink, emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "run_denied",
                None,
                {"reason": "token=sk-abcdefghijklmnop", "duplicate": False},
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert spans[0].outcome == SpanOutcome.FAILURE
    assert spans[0].error_kind == "authority_denied"
    assert "abcdefghijklmnop" not in (spans[0].error_message or "")
    assert sink.records[-1]["metric"] == "run_denied"


def test_run_failed_projects_failure_and_evaluation_metric():
    sink = InMemoryEvaluationSink()
    obs = LangGraphObservability(evaluation=sink, emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "run_failed",
                None,
                {"reason": "gateway exhausted retry budget", "duplicate": False},
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert len(spans) == 1
    assert spans[0].outcome == SpanOutcome.FAILURE
    assert spans[0].error_kind == "gateway_exhausted"
    assert sink.records[-1]["metric"] == "run_failed"


def test_verified_identity_wins_over_agent_argument():
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        obs = LangGraphObservability(emit=lambda _span: None)
        spans = obs.project_events(
            _run(),
            [PilotEvent("run_completed", None, {"duplicate": False, "output_digest": "e" * 64})],
            graph_name="ua.langgraph.pilot",
            graph_version="1",
            definition_digest=DEFINITION,
            agent="Spoofed_Agent",
        )
        assert spans[0].agent == "Desktop_Agent2"
    finally:
        config.agent_identity_var.reset(token)


def test_governed_tool_success_projects_tool_span():
    obs = LangGraphObservability(emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "node_completed",
                "governed_tool",
                {
                    "operation_key": "read-evidence",
                    "idempotency_key": "ua-lg:test:governed_tool:read-evidence",
                    "result_digest": "a" * 64,
                },
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    tool_span = next(span for span in spans if span.kind == SpanKind.TOOL)
    assert tool_span.outcome == SpanOutcome.SUCCESS
    assert tool_span.name == "langgraph:governed_tool"


def test_governed_tool_denied_projects_tool_failure():
    obs = LangGraphObservability(emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "node_completed",
                "governed_tool",
                {
                    "operation_key": "read-evidence",
                    "outcome": "denied",
                    "reason": "token=sk-abcdefghijklmnop",
                },
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    tool_span = next(span for span in spans if span.kind == SpanKind.TOOL)
    assert tool_span.outcome == SpanOutcome.FAILURE
    assert tool_span.error_kind == "authority_denied"
    assert "abcdefghijklmnop" not in (tool_span.error_message or "")


def test_governed_tool_failed_projects_tool_failure():
    obs = LangGraphObservability(emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "node_completed",
                "governed_tool",
                {
                    "operation_key": "read-evidence",
                    "outcome": "failed",
                    "reason": "gateway exhausted retry budget",
                },
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    tool_span = next(span for span in spans if span.kind == SpanKind.TOOL)
    assert tool_span.outcome == SpanOutcome.FAILURE
    assert tool_span.error_kind == "gateway_exhausted"


def test_repeated_node_events_get_distinct_span_ids():
    obs = LangGraphObservability(emit=lambda _span: None)
    events = [
        PilotEvent("node_completed", "intake", {"input_digest": "d" * 64}),
        PilotEvent("node_completed", "intake", {"input_digest": "e" * 64}),
    ]
    spans = obs.project_events(
        _run(),
        events,
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    child_ids = [span.span_id for span in spans if span.kind == SpanKind.CHAIN]
    assert len(child_ids) == 2
    assert len(set(child_ids)) == 2


def test_resume_batches_get_distinct_approval_span_ids():
    obs = LangGraphObservability(emit=lambda _span: None)
    first_batch = [
        PilotEvent(
            "approval_interrupted",
            "governed_tool",
            {"approval_id": "101", "tool_name": "web_search"},
        )
    ]
    second_batch = [
        PilotEvent(
            "approval_interrupted",
            "governed_tool",
            {"approval_id": "202", "tool_name": "web_search"},
        )
    ]
    first_spans = obs.project_events(
        _run(),
        first_batch,
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    second_spans = obs.project_events(
        _run(),
        second_batch,
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    first_tool = next(span for span in first_spans if span.kind == SpanKind.TOOL)
    second_tool = next(span for span in second_spans if span.kind == SpanKind.TOOL)
    assert first_tool.span_id != second_tool.span_id


def test_run_denied_duplicate_label_propagates_to_evaluation():
    sink = InMemoryEvaluationSink()
    obs = LangGraphObservability(evaluation=sink, emit=lambda _span: None)
    obs.project_events(
        _run(),
        [PilotEvent("run_denied", None, {"duplicate": True, "reason": "already denied"})],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert sink.records[-1]["labels"]["duplicate"] == "True"


def test_transient_attempt_count_preserved_in_span_metadata():
    obs = LangGraphObservability(emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [
            PilotEvent(
                "run_failed",
                None,
                {
                    "duplicate": False,
                    "reason": "gateway exhausted retry budget",
                    "transient_attempt_count": 3,
                },
            )
        ],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert spans[0].metadata["transient_attempt_count"] == 3


def test_langsmith_exporter_requires_env_and_decision(monkeypatch):
    decision = ExporterDecision(
        approved_by="Operator",
        purpose="bounded pilot evaluation only",
        retention_days=7,
        cost_cap_usd=5.0,
    )
    monkeypatch.delenv("UA_LANGGRAPH_LANGSMITH_EXPORT", raising=False)
    assert langsmith_export_enabled() is False
    with pytest.raises(RuntimeError, match="UA_LANGGRAPH_LANGSMITH_EXPORT=1"):
        LangSmithTraceExporter(decision=decision)

    monkeypatch.setenv("UA_LANGGRAPH_LANGSMITH_EXPORT", "1")
    assert langsmith_export_enabled() is True
    sent: list[list[dict]] = []
    exporter = LangSmithTraceExporter(
        decision=decision,
        transport=lambda payload: sent.append(payload) or len(payload),
    )
    obs = LangGraphObservability(exporter=exporter, emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        [PilotEvent("run_completed", None, {"duplicate": False, "output_digest": "e" * 64})],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert sent and len(sent[0]) == len(spans)
    assert "query" not in str(sent)


def test_evaluation_failure_does_not_abort_lazy_event_stream():
    class FailingSink:
        def __init__(self) -> None:
            self.calls = 0

        def record(self, **kwargs) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("metrics backend unavailable")

    sink = FailingSink()

    def event_stream():
        yield PilotEvent("node_completed", "intake", {"input_digest": "d" * 64})
        yield PilotEvent(
            "run_completed",
            None,
            {"duplicate": False, "output_digest": "e" * 64},
        )

    obs = LangGraphObservability(evaluation=sink, emit=lambda _span: None)
    spans = obs.project_events(
        _run(),
        event_stream(),
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert spans[0].outcome == SpanOutcome.SUCCESS
    assert len(spans) >= 2


def test_exporter_failure_returns_projected_spans():
    emitted: list = []

    class FailingExporter:
        def export(self, spans) -> int:
            raise TraceExporterError("langsmith outage")

    obs = LangGraphObservability(
        exporter=FailingExporter(),
        emit=lambda span: emitted.append(span) or span.span_id,
    )
    spans = obs.project_events(
        _run(),
        [PilotEvent("run_completed", None, {"duplicate": False, "output_digest": "e" * 64})],
        graph_name="ua.langgraph.pilot",
        graph_version="1",
        definition_digest=DEFINITION,
        agent="Desktop_Agent4",
    )
    assert len(spans) == 1
    assert spans[0].outcome == SpanOutcome.SUCCESS
    assert len(emitted) == 1


def test_langsmith_buffer_evicts_oldest_when_transport_disabled(monkeypatch):
    decision = ExporterDecision(
        approved_by="Operator",
        purpose="bounded pilot evaluation only",
        retention_days=7,
        cost_cap_usd=5.0,
    )
    monkeypatch.setenv("UA_LANGGRAPH_LANGSMITH_EXPORT", "1")
    exporter = LangSmithTraceExporter(decision=decision, max_buffered_spans=2)
    obs = LangGraphObservability(exporter=exporter, emit=lambda _span: None)
    for _ in range(3):
        obs.project_events(
            _run(),
            [PilotEvent("run_completed", None, {"duplicate": False, "output_digest": "e" * 64})],
            graph_name="ua.langgraph.pilot",
            graph_version="1",
            definition_digest=DEFINITION,
            agent="Desktop_Agent4",
        )
    assert len(exporter._buffer) == 2
