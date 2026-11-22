"""Deterministic fixtures for telemetry scenarios (Task #480)."""

from __future__ import annotations

from typing import Any

from .schema import SpanKind, SpanOutcome, TelemetrySpan, UsageMetrics

FIXTURE_SCENARIOS = (
    "success",
    "failure",
    "retry",
    "fallback",
    "quarantined",
    "cooldown",
)


def generate_fixture(scenario: str) -> dict[str, Any]:
    """Generate a deterministic telemetry trace fixture for a given scenario."""
    # scenario[:2] collides (failure/fallback both "FA"); use a padded slug.
    slug = scenario.replace("_", "")[:8].upper().ljust(8, "X")
    trace_id = f"01ARZ3NDEKTSV4RRFFQ{slug}"

    if scenario == "success":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.RUN,
                name="task_execution",
                agent="Desktop_agent3",
                task_id=480,
                outcome=SpanOutcome.SUCCESS,
                duration_ms=1250.0,
                usage=UsageMetrics(tokens_in=500, tokens_out=250, cost_usd=0.0015),
            ).to_dict(),
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}02",
                parent_span_id=f"{trace_id}01",
                kind=SpanKind.TOOL,
                name="fs_read",
                agent="Desktop_agent3",
                outcome=SpanOutcome.SUCCESS,
                duration_ms=15.4,
                metadata={"path": "src/unified_agent/storage.py"},
            ).to_dict(),
        ]
    elif scenario == "failure":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.TOOL,
                name="run_cmd",
                agent="Desktop_agent3",
                outcome=SpanOutcome.FAILURE,
                duration_ms=300.0,
                error_kind="PermissionDeniedError",
                error_message="Access denied when writing to protected path /etc/passwd",
            ).to_dict(),
        ]
    elif scenario == "retry":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.MODEL,
                name="llm_generate",
                agent="Desktop_agent3",
                outcome=SpanOutcome.RETRY,
                duration_ms=450.0,
                error_kind="RateLimitError",
                error_message="HTTP 429 Too Many Requests; backing off 1.5s",
                metadata={"attempt": 1, "max_retries": 3},
            ).to_dict(),
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}02",
                parent_span_id=f"{trace_id}01",
                kind=SpanKind.MODEL,
                name="llm_generate",
                agent="Desktop_agent3",
                outcome=SpanOutcome.SUCCESS,
                duration_ms=820.0,
                usage=UsageMetrics(tokens_in=300, tokens_out=150, cost_usd=0.0008),
                metadata={"attempt": 2},
            ).to_dict(),
        ]
    elif scenario == "fallback":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.MODEL,
                name="ollama_local",
                agent="Desktop_agent3",
                outcome=SpanOutcome.FALLBACK,
                duration_ms=2100.0,
                error_kind="ConnectionError",
                error_message="Ollama local daemon unavailable on port 11434; routing to ai_studio",
                metadata={"provider_chain": ["ollama", "ai_studio", "openrouter"]},
            ).to_dict(),
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}02",
                parent_span_id=f"{trace_id}01",
                kind=SpanKind.MODEL,
                name="ai_studio_gemini",
                agent="Desktop_agent3",
                outcome=SpanOutcome.SUCCESS,
                duration_ms=640.0,
                usage=UsageMetrics(tokens_in=600, tokens_out=300, cost_usd=0.0009),
                metadata={"provider": "ai_studio", "model": "gemini-flash-latest"},
            ).to_dict(),
        ]
    elif scenario == "quarantined":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.EXECUTOR,
                name="executor_dispatch",
                agent="Desktop_Agent2",
                task_id=468,
                outcome=SpanOutcome.QUARANTINED,
                duration_ms=12.0,
                error_kind="SecurityQuarantine",
                error_message="Task flagged untrusted=1; attempted execution of disallowed shell command",
                metadata={"policy": "sandbox_enforcement", "quarantine_action": "halt"},
            ).to_dict(),
        ]
    elif scenario == "cooldown":
        spans = [
            TelemetrySpan(
                trace_id=trace_id,
                span_id=f"{trace_id}01",
                kind=SpanKind.EXECUTOR,
                name="executor_rate_gate",
                agent="Desktop_Agent1",
                outcome=SpanOutcome.COOLDOWN,
                duration_ms=5.0,
                error_kind="CooldownActive",
                error_message="Provider quota exhausted; active cooldown period until 2026-08-20T01:15:00Z",
                metadata={"cooldown_seconds": 1800, "provider": "anthropic"},
            ).to_dict(),
        ]
    else:
        raise ValueError(f"Unknown fixture scenario: {scenario}")

    return {
        "scenario": scenario,
        "trace_id": trace_id,
        "spans": spans,
    }
