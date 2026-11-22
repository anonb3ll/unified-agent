"""Producer gap analysis and telemetry bridge (Task #480)."""

from __future__ import annotations

from typing import Any

from .. import events
from .schema import TelemetrySpan


def producer_gap_report() -> dict[str, Any]:
    """Return an audit report of current telemetry producers and identified gaps."""
    return {
        "producers": [
            {
                "module": "unified_agent.storage",
                "function": "record_agent_event",
                "status": "active",
                "coverage": "High-level mutation log (task claim/update, approval request, memory save).",
            },
            {
                "module": "unified_agent.events",
                "function": "record_event",
                "status": "active",
                "coverage": "Durable ULID event bus with Unix-socket fanout.",
            },
            {
                "module": "unified_agent.executor.evidence",
                "function": "start_attempt / finish_attempt",
                "status": "active",
                "coverage": "Executor dispatch attempts, task types, and coarse outcomes.",
            },
            {
                "module": "unified_agent.llm_client",
                "function": "call_llm / gemini_ask",
                "status": "partial",
                "coverage": "Direct LLM requests; missing token usage metrics and latency span emission.",
            },
        ],
        "gaps": [
            {
                "id": "GAP-001",
                "area": "llm_usage",
                "description": "llm_client.py returns generated text but drops token count and cost metadata from API response headers.",
                "remediation_task": "P0 telemetry instrumentation follow-on (#481)",
            },
            {
                "id": "GAP-002",
                "area": "tool_spans",
                "description": "MCPServer tool registry does not wrap tool executions with start/finish duration spans.",
                "remediation_task": "P0 telemetry waterfall (#481)",
            },
            {
                "id": "GAP-003",
                "area": "workflow_subspans",
                "description": "workflow_run records top-level workflow status but lacks step-level duration and error tracing.",
                "remediation_task": "P0 lifecycle & handoff (#482)",
            },
        ],
    }


def emit_telemetry_span(span: TelemetrySpan) -> str | None:
    """Emit a TelemetrySpan to the durable event log via events.record_event."""
    entry = span.to_event_log_entry()
    try:
        return events.record_event(
            channel=entry["channel"],
            payload=entry["payload"],
            originator=entry["originator"],
            episode_id=entry["episode_id"],
            causal_parent_id=entry["causal_parent_id"],
            notify=True,
        )
    except Exception:
        return None
