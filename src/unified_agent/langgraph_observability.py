"""LangGraph observability and evaluation seam (task #588).

Projects pilot stream events into Unified-Agent ``TelemetrySpan`` records and
an optional evaluation sink. External exporters (including LangSmith) are sinks
only: they never authorize effects, mutate tasks, or become a source of truth.
LangSmith stays disabled unless both an explicit privacy/cost decision and the
opt-in environment flag are present.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config
from .langgraph_pilot import PilotEvent, PilotRun
from .telemetry.producers import emit_telemetry_span
from .telemetry.redaction import redact_sensitive_content, sanitize_metadata
from .telemetry.schema import SpanKind, SpanOutcome, TelemetrySpan, UsageMetrics

_LANGSMITH_ENV = "UA_LANGGRAPH_LANGSMITH_EXPORT"
_DEFINITION_PREFIX_LEN = 12
_DEFAULT_BUFFERED_SPANS = 256


class TraceExporterError(Exception):
    """Expected external exporter failure; must not abort pilot projection."""


class EvaluationSink(Protocol):
    """Bounded evaluation hook; never reads or writes authority state."""

    def record(
        self,
        *,
        run_id: str,
        task_id: int,
        metric: str,
        value: float | str | bool,
        labels: dict[str, str] | None = None,
    ) -> None: ...


@dataclass
class InMemoryEvaluationSink:
    """Test/local evaluation recorder with a fixed capacity."""

    max_records: int = 256
    records: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        run_id: str,
        task_id: int,
        metric: str,
        value: float | str | bool,
        labels: dict[str, str] | None = None,
    ) -> None:
        if not metric or len(metric) > 64:
            raise ValueError("metric must be a non-empty name ≤64 characters")
        entry = sanitize_metadata(
            {
                "run_id": run_id,
                "task_id": task_id,
                "metric": metric,
                "value": value,
                "labels": dict(labels or {}),
            }
        )
        self.records.append(entry)
        if len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]


@dataclass(frozen=True)
class ExporterDecision:
    """Explicit privacy/cost decision required before any external export."""

    approved_by: str
    purpose: str
    retention_days: int
    cost_cap_usd: float

    def validate(self) -> None:
        if not self.approved_by.strip():
            raise ValueError("exporter decision requires an approver")
        if not self.purpose.strip() or len(self.purpose) > 200:
            raise ValueError("exporter decision purpose must be 1-200 characters")
        if self.retention_days < 1 or self.retention_days > 365:
            raise ValueError("retention_days must be in 1..365")
        if self.cost_cap_usd < 0:
            raise ValueError("cost_cap_usd must be non-negative")


class TraceExporter(Protocol):
    """Optional external sink. Must never be treated as authoritative state."""

    def export(self, spans: Sequence[TelemetrySpan]) -> int: ...


class NullTraceExporter:
    """Default exporter: accepts spans and discards them."""

    def export(self, spans: Sequence[TelemetrySpan]) -> int:
        return 0


@dataclass
class LangSmithTraceExporter:
    """Opt-in LangSmith sink gated by env + explicit decision.

    This class records bounded, already-redacted span dictionaries locally for
    verification. It does not open a network client unless ``transport`` is
    injected by a separately reviewed integration.
    """

    decision: ExporterDecision
    transport: Callable[[list[dict[str, Any]]], int] | None = None
    max_buffered_spans: int = _DEFAULT_BUFFERED_SPANS
    _buffer: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.decision.validate()
        if self.max_buffered_spans < 0:
            raise ValueError("max_buffered_spans must be non-negative")
        if os.environ.get(_LANGSMITH_ENV, "").strip() != "1":
            raise RuntimeError(
                f"LangSmith export requires {_LANGSMITH_ENV}=1 after an explicit "
                "privacy/cost decision; default remains disabled"
            )

    def export(self, spans: Sequence[TelemetrySpan]) -> int:
        payload = [sanitize_metadata(span.to_dict()) for span in spans]
        if self.transport is not None:
            try:
                return int(self.transport(payload))
            except TraceExporterError:
                raise
            except Exception as exc:
                raise TraceExporterError(str(exc)) from exc
        self._buffer.extend(payload)
        if len(self._buffer) > self.max_buffered_spans:
            del self._buffer[: len(self._buffer) - self.max_buffered_spans]
        return len(payload)


@dataclass
class LangGraphObservability:
    """Projects pilot events into TelemetrySpan records and evaluation metrics."""

    evaluation: EvaluationSink | None = None
    exporter: TraceExporter = field(default_factory=NullTraceExporter)
    emit: Callable[[TelemetrySpan], str | None] = field(default=emit_telemetry_span)

    def project_events(
        self,
        run: PilotRun,
        events: Iterable[PilotEvent],
        *,
        graph_name: str,
        graph_version: str,
        definition_digest: str,
        agent: str | None = None,
    ) -> list[TelemetrySpan]:
        run.validate()
        identity = config.verified_agent_identity() or agent or run.claimed_by
        spans: list[TelemetrySpan] = []
        parent_span_id = _stable_span_id(run.trace_id, "run")
        started = time.perf_counter()
        run_span = TelemetrySpan(
            trace_id=run.trace_id,
            span_id=parent_span_id,
            kind=SpanKind.RUN,
            name=f"langgraph:{graph_name}",
            outcome=SpanOutcome.UNKNOWN,
            agent=identity,
            task_id=run.task_id,
            metadata=_run_metadata(run, graph_name, graph_version, definition_digest),
        )
        spans.append(run_span)

        final_outcome = SpanOutcome.UNKNOWN
        for index, event in enumerate(events):
            if _is_terminal_run_event(event):
                _apply_terminal_event(run_span, event)
            else:
                child = self._span_for_event(
                    run=run,
                    event=event,
                    parent_span_id=parent_span_id,
                    agent=identity,
                    graph_name=graph_name,
                    graph_version=graph_version,
                    definition_digest=definition_digest,
                    event_index=index,
                )
                if child is not None:
                    spans.append(child)
            final_outcome = _outcome_for_event(event, final_outcome)
            self._record_eval_safe(run, event)

        run_span.outcome = final_outcome
        run_span.duration_ms = (time.perf_counter() - started) * 1000.0
        for span in spans:
            self.emit(span)
        self._export_safe(spans)
        return spans

    def _record_eval_safe(self, run: PilotRun, event: PilotEvent) -> None:
        try:
            self._record_eval(run, event)
        except Exception:
            # Evaluation sinks are advisory; never abort lazy pilot streams.
            return

    def _export_safe(self, spans: Sequence[TelemetrySpan]) -> None:
        try:
            self.exporter.export(spans)
        except TraceExporterError:
            return

    def _span_for_event(
        self,
        *,
        run: PilotRun,
        event: PilotEvent,
        parent_span_id: str,
        agent: str,
        graph_name: str,
        graph_version: str,
        definition_digest: str,
        event_index: int,
    ) -> TelemetrySpan | None:
        kind, outcome = _kind_and_outcome(event)
        if kind is None:
            return None
        node_key = event.node_key or event.kind
        metadata = _run_metadata(run, graph_name, graph_version, definition_digest)
        metadata.update(_bounded_event_metadata(event))
        error_kind, error_message = _error_fields_for_event(event)
        return TelemetrySpan(
            trace_id=run.trace_id,
            span_id=_stable_span_id(run.trace_id, _span_label_for_event(event, event_index)),
            parent_span_id=parent_span_id,
            kind=kind,
            name=f"langgraph:{node_key}",
            outcome=outcome,
            agent=agent,
            task_id=run.task_id,
            metadata=metadata,
            usage=UsageMetrics(),
            error_kind=error_kind,
            error_message=error_message,
        )

    def _record_eval(self, run: PilotRun, event: PilotEvent) -> None:
        if self.evaluation is None:
            return
        if event.kind == "run_completed":
            self.evaluation.record(
                run_id=run.run_id,
                task_id=run.task_id,
                metric="run_completed",
                value=True,
                labels={"duplicate": str(bool(event.payload.get("duplicate")))},
            )
        elif event.kind == "run_denied":
            self.evaluation.record(
                run_id=run.run_id,
                task_id=run.task_id,
                metric="run_denied",
                value=True,
                labels={"duplicate": str(bool(event.payload.get("duplicate")))},
            )
        elif event.kind == "approval_interrupted":
            self.evaluation.record(
                run_id=run.run_id,
                task_id=run.task_id,
                metric="approval_interrupted",
                value=True,
            )
        elif event.kind == "run_failed":
            self.evaluation.record(
                run_id=run.run_id,
                task_id=run.task_id,
                metric="run_failed",
                value=True,
                labels={"duplicate": str(bool(event.payload.get("duplicate")))},
            )


def langsmith_export_enabled() -> bool:
    """Return whether the LangSmith opt-in flag is set (still needs a decision)."""
    return os.environ.get(_LANGSMITH_ENV, "").strip() == "1"


def _run_metadata(
    run: PilotRun,
    graph_name: str,
    graph_version: str,
    definition_digest: str,
) -> dict[str, Any]:
    return sanitize_metadata(
        {
            "run_id": run.run_id,
            "graph_name": graph_name,
            "graph_version": graph_version,
            "definition_digest_prefix": definition_digest[:_DEFINITION_PREFIX_LEN],
            "task_id": run.task_id,
            "claimed_by": run.claimed_by,
            "expected_profile": run.expected_profile,
            "budget_revision": run.budget_revision,
            "input_digest": run.input_digest,
        }
    )


def _bounded_event_metadata(event: PilotEvent) -> dict[str, Any]:
    payload = event.payload or {}
    allowed = {
        "operation_key": payload.get("operation_key"),
        "idempotency_key_prefix": _prefix(payload.get("idempotency_key")),
        "approval_id": payload.get("approval_id"),
        "approval_status": payload.get("approval_status"),
        "output_digest": payload.get("output_digest"),
        "result_digest": payload.get("result_digest"),
        "payload_digest": payload.get("payload_digest"),
        "duplicate": payload.get("duplicate"),
        "recovered": payload.get("recovered"),
        "retry_class": payload.get("retry_class"),
        "transient_attempt_count": payload.get("transient_attempt_count"),
        "checkpoint_sequence": payload.get("checkpoint_sequence"),
        "tool_name": payload.get("tool_name"),
    }
    return sanitize_metadata({k: v for k, v in allowed.items() if v is not None})


def _kind_and_outcome(
    event: PilotEvent,
) -> tuple[SpanKind | None, SpanOutcome]:
    if event.kind == "node_completed":
        node_key = event.node_key or event.kind
        if node_key == "governed_tool":
            return SpanKind.TOOL, _governed_tool_outcome(event)
        return SpanKind.CHAIN, SpanOutcome.SUCCESS
    if event.kind == "approval_interrupted":
        return SpanKind.TOOL, SpanOutcome.WAITING_REVIEW
    return None, SpanOutcome.UNKNOWN


def _governed_tool_outcome(event: PilotEvent) -> SpanOutcome:
    payload = event.payload or {}
    outcome = payload.get("outcome")
    if outcome in {"denied", "failed"}:
        return SpanOutcome.FAILURE
    return SpanOutcome.SUCCESS


def _error_fields_for_event(event: PilotEvent) -> tuple[str | None, str | None]:
    if event.kind != "node_completed" or (event.node_key or event.kind) != "governed_tool":
        return None, None
    payload = event.payload or {}
    outcome = payload.get("outcome")
    if outcome not in {"denied", "failed"}:
        return None, None
    error_kind = "authority_denied" if outcome == "denied" else "gateway_exhausted"
    reason = payload.get("reason")
    error_message = redact_sensitive_content(str(reason), max_len=200) if reason else None
    return error_kind, error_message


def _is_terminal_run_event(event: PilotEvent) -> bool:
    return event.kind in {"run_completed", "run_denied", "run_failed"}


def _apply_terminal_event(run_span: TelemetrySpan, event: PilotEvent) -> None:
    """Project terminal run events onto the parent run span only."""
    run_span.metadata.update(_bounded_event_metadata(event))
    if event.kind == "run_denied":
        run_span.error_kind = "authority_denied"
        reason = event.payload.get("reason")
        if reason:
            run_span.error_message = redact_sensitive_content(str(reason), max_len=200)
    elif event.kind == "run_failed":
        run_span.error_kind = "gateway_exhausted"
        reason = event.payload.get("reason")
        if reason:
            run_span.error_message = redact_sensitive_content(str(reason), max_len=200)


def _outcome_for_event(event: PilotEvent, current: SpanOutcome) -> SpanOutcome:
    if event.kind == "run_completed":
        return SpanOutcome.SUCCESS
    if event.kind == "run_denied":
        return SpanOutcome.FAILURE
    if event.kind == "run_failed":
        return SpanOutcome.FAILURE
    if event.kind == "approval_interrupted":
        return SpanOutcome.WAITING_REVIEW
    return current


def _span_label_for_event(event: PilotEvent, event_index: int) -> str:
    """Build a stable span label that stays unique across resume batches."""
    node_key = event.node_key or event.kind
    payload = event.payload or {}
    approval_id = payload.get("approval_id")
    if approval_id is not None:
        return f"{event.kind}:{node_key}:approval:{approval_id}"
    checkpoint_sequence = payload.get("checkpoint_sequence")
    if checkpoint_sequence is not None:
        return f"{event.kind}:{node_key}:checkpoint:{checkpoint_sequence}"
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key is not None:
        return f"{event.kind}:{node_key}:idempotency:{idempotency_key}"
    return f"{event.kind}:{node_key}:{event_index}"


def _stable_span_id(trace_id: str, label: str) -> str:
    digest = hashlib.sha256(f"{trace_id}:{label}".encode()).hexdigest()
    return digest[:16]


def _prefix(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:16]


def new_span_id() -> str:
    """Return a random span id for callers that need an unbound child."""
    return uuid.uuid4().hex[:16]


__all__ = [
    "EvaluationSink",
    "ExporterDecision",
    "InMemoryEvaluationSink",
    "LangGraphObservability",
    "LangSmithTraceExporter",
    "NullTraceExporter",
    "TraceExporter",
    "TraceExporterError",
    "langsmith_export_enabled",
    "new_span_id",
]
