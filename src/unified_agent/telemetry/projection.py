"""Read-only projections for the telemetry waterfall and exception digest.

The projection deliberately keeps event-log identifiers as evidence references instead
of returning raw event payloads.  That gives the Command Center a useful audit trail
without creating a second prompt or secret display surface.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from .. import events
from .schema import SpanOutcome, validate_telemetry_event

TELEMETRY_CHANNEL = "agent.telemetry.*"
EXCEPTION_OUTCOMES = frozenset(
    {
        SpanOutcome.FAILURE.value,
        SpanOutcome.RETRY.value,
        SpanOutcome.FALLBACK.value,
        SpanOutcome.QUARANTINED.value,
        SpanOutcome.COOLDOWN.value,
        SpanOutcome.KILLED.value,
        SpanOutcome.WAITING_REVIEW.value,
        SpanOutcome.UNKNOWN.value,
    }
)
DEFAULT_LATENCY_THRESHOLD_MS = 2_000.0
DEFAULT_COST_THRESHOLD_USD = 0.01
DEFAULT_FALLBACK_STORM_COUNT = 3
DEFAULT_REPEATED_FAILURE_COUNT = 2


def _evidence(event: dict[str, Any]) -> dict[str, Any]:
    """Return the minimum live reference needed to inspect the original event."""
    return {
        "ulid": event.get("ulid"),
        "channel": event.get("channel"),
        "created_at": event.get("created_at"),
    }


def _telemetry_spans(events_in: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for event in events_in:
        if not str(event.get("channel", "")).startswith("agent.telemetry."):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not validate_telemetry_event(payload):
            continue
        span = dict(payload)
        span["evidence_ref"] = _evidence(event)
        spans.append(span)
    spans.sort(
        key=lambda span: (
            span["evidence_ref"].get("created_at") or "",
            span["evidence_ref"].get("ulid") or "",
        )
    )
    return spans


def _sum_usage(spans: Iterable[dict[str, Any]]) -> dict[str, Any]:
    tokens_in = tokens_out = tokens_total_only = 0
    cost = 0.0
    has_cost = False
    for span in spans:
        usage = span.get("usage") or {}
        tin = int(usage.get("tokens_in", 0) or 0)
        tout = int(usage.get("tokens_out", 0) or 0)
        ttotal = int(usage.get("tokens_total", 0) or 0)
        if tin or tout:
            tokens_in += tin
            tokens_out += tout
        elif ttotal:
            # Provider reported a total without a split — count once.
            tokens_total_only += ttotal
        if usage.get("cost_usd") is not None:
            cost += float(usage["cost_usd"])
            has_cost = True
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out + tokens_total_only,
        "cost_usd": round(cost, 6) if has_cost else None,
    }


def project_telemetry(
    events_in: Iterable[dict[str, Any]],
    *,
    trace_id: str | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    """Build a bounded trace waterfall from normalized telemetry events."""
    spans = _telemetry_spans(events_in)
    if trace_id is not None:
        spans = [span for span in spans if span.get("trace_id") == trace_id]
    if task_id is not None:
        matching_traces = {span.get("trace_id") for span in spans if span.get("task_id") == task_id}
        spans = [span for span in spans if span.get("trace_id") in matching_traces]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        grouped[span["trace_id"]].append(span)

    traces = []
    for current_trace, trace_spans in grouped.items():
        duration = sum(float(span.get("duration_ms") or 0) for span in trace_spans)
        outcomes = Counter(str(span.get("outcome", "unknown")) for span in trace_spans)
        traces.append(
            {
                "trace_id": current_trace,
                "task_ids": sorted(
                    {span["task_id"] for span in trace_spans if span.get("task_id") is not None}
                ),
                "agent": next(
                    (span.get("agent") for span in trace_spans if span.get("agent")), None
                ),
                "duration_ms": round(duration, 2),
                "outcomes": dict(sorted(outcomes.items())),
                "spans": trace_spans,
                "evidence_refs": [span["evidence_ref"] for span in trace_spans],
            }
        )
    traces.sort(key=lambda trace: trace["trace_id"])

    usage = _sum_usage(spans)
    return {
        "traces": traces,
        "spans": spans,
        "summary": {
            "traces": len(traces),
            "spans": len(spans),
            "duration_ms": round(sum(float(span.get("duration_ms") or 0) for span in spans), 2),
            **usage,
        },
        "evidence_refs": [span["evidence_ref"] for span in spans],
    }


def build_exception_digest(
    events_in: Iterable[dict[str, Any]],
    *,
    expected_trace_ids: Iterable[str] | None = None,
    latency_threshold_ms: float = DEFAULT_LATENCY_THRESHOLD_MS,
    cost_threshold_usd: float = DEFAULT_COST_THRESHOLD_USD,
    fallback_storm_count: int = DEFAULT_FALLBACK_STORM_COUNT,
    repeated_failure_count: int = DEFAULT_REPEATED_FAILURE_COUNT,
) -> dict[str, Any]:
    """Return exception-only telemetry, omitting normal successful spans."""
    spans = _telemetry_spans(events_in)
    exceptions: list[dict[str, Any]] = []

    def add(
        kind: str, span: dict[str, Any] | None, message: str, severity: str = "medium", **extra: Any
    ) -> None:
        item = {
            "kind": kind,
            "severity": severity,
            "message": message,
            **extra,
        }
        if span is not None:
            item.update(
                {
                    "trace_id": span.get("trace_id"),
                    "span_id": span.get("span_id"),
                    "name": span.get("name"),
                    "outcome": span.get("outcome"),
                    "task_id": span.get("task_id"),
                    "evidence_ref": span.get("evidence_ref"),
                }
            )
        exceptions.append(item)

    for span in spans:
        outcome = str(span.get("outcome", "unknown"))
        if outcome in EXCEPTION_OUTCOMES:
            message = span.get("error_kind") or f"telemetry outcome: {outcome}"
            add(
                outcome,
                span,
                str(message),
                "high" if outcome in {"failure", "quarantined", "killed"} else "medium",
            )
        duration = span.get("duration_ms")
        if duration is not None and float(duration) >= latency_threshold_ms:
            add(
                "latency_anomaly",
                span,
                f"latency {float(duration):g}ms exceeds {latency_threshold_ms:g}ms",
                "medium",
            )
        cost = (span.get("usage") or {}).get("cost_usd")
        if cost is not None and float(cost) >= cost_threshold_usd:
            add(
                "cost_anomaly",
                span,
                f"cost ${float(cost):.4f} exceeds ${cost_threshold_usd:.4f}",
                "medium",
            )

    failures = Counter(
        (span.get("trace_id"), span.get("name"))
        for span in spans
        if span.get("outcome") == SpanOutcome.FAILURE.value
    )
    for (current_trace, name), count in failures.items():
        if count >= repeated_failure_count:
            span = next(
                span
                for span in spans
                if span.get("trace_id") == current_trace and span.get("name") == name
            )
            add("repeated_failure", span, f"{count} failures for {name}", "high", count=count)

    # Storms are per-trace (not batch-wide): three fallbacks across unrelated
    # traces are independent; three in one trace are a cascade.
    fallback_by_trace = Counter(
        span.get("trace_id") for span in spans if span.get("outcome") == SpanOutcome.FALLBACK.value
    )
    for current_trace, fallback_count in fallback_by_trace.items():
        if fallback_count >= fallback_storm_count:
            span = next(
                span
                for span in spans
                if span.get("trace_id") == current_trace
                and span.get("outcome") == SpanOutcome.FALLBACK.value
            )
            add(
                "fallback_storm",
                span,
                f"{fallback_count} fallback spans observed in trace {current_trace}",
                "high",
                count=fallback_count,
            )

    seen_traces = {span.get("trace_id") for span in spans}
    for missing in sorted(set(expected_trace_ids or ()) - seen_traces):
        add(
            "missing_telemetry",
            None,
            f"no telemetry received for trace {missing}",
            "high",
            trace_id=missing,
        )

    normal_spans_omitted = sum(span.get("outcome") == SpanOutcome.SUCCESS.value for span in spans)
    return {
        "exceptions": exceptions,
        "total": len(exceptions),
        "normal_spans_omitted": normal_spans_omitted,
        "scanned_spans": len(spans),
        "evidence_refs": [item["evidence_ref"] for item in exceptions if item.get("evidence_ref")],
    }


def telemetry_payload(
    *,
    trace_id: str | None = None,
    task_id: int | None = None,
    limit: int = 500,
    expected_trace_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Read the durable telemetry channel and return both Command Center views."""
    # Newest-first window so limit=N is the live tail, then chronological for display.
    event_rows = events.query_events(
        channels=[TELEMETRY_CHANNEL],
        limit=max(1, min(int(limit), 500)),
        newest_first=True,
    )
    event_rows = list(reversed(event_rows))
    if trace_id is not None:
        event_rows = [
            event
            for event in event_rows
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("trace_id") == trace_id
        ]
    if task_id is not None:
        matching_traces = {
            event["payload"].get("trace_id")
            for event in event_rows
            if isinstance(event.get("payload"), dict) and event["payload"].get("task_id") == task_id
        }
        event_rows = [
            event
            for event in event_rows
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("trace_id") in matching_traces
        ]
    waterfall = project_telemetry(event_rows, trace_id=trace_id, task_id=task_id)
    digest = build_exception_digest(event_rows, expected_trace_ids=expected_trace_ids)
    return {
        "waterfall": waterfall,
        "exception_digest": digest,
        "cursor": event_rows[-1].get("ulid") if event_rows else None,
        "source": {"channel": TELEMETRY_CHANNEL, "event_count": len(event_rows)},
    }


# Short aliases for callers that use the product vocabulary rather than the module name.
build_waterfall = project_telemetry
build_telemetry_payload = telemetry_payload
