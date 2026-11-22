"""Public projection seams for the Command Center telemetry view (Task #481)."""

from unified_agent.telemetry.fixtures import generate_fixture
from unified_agent.telemetry.projection import (
    _sum_usage,
    build_exception_digest,
    project_telemetry,
    telemetry_payload,
)


def _events(*scenarios):
    events = []
    for scenario in scenarios:
        fixture = generate_fixture(scenario)
        for index, span in enumerate(fixture["spans"], start=1):
            events.append(
                {
                    "ulid": f"01ARZ3NDEKTSV4RRFFQ69{scenario[:2].upper()}{index:04d}",
                    "channel": f"agent.telemetry.{span['kind']}",
                    "payload": span,
                    "originator": span.get("agent"),
                    "created_at": f"2026-08-20T01:00:{index:02d}+00:00",
                }
            )
    return events


def test_project_telemetry_groups_waterfall_and_preserves_live_evidence():
    result = project_telemetry(_events("success"))

    assert result["summary"] == {
        "traces": 1,
        "spans": 2,
        "duration_ms": 1265.4,
        "tokens_in": 500,
        "tokens_out": 250,
        "tokens_total": 750,
        "cost_usd": 0.0015,
    }
    assert result["traces"][0]["trace_id"]
    assert [span["name"] for span in result["traces"][0]["spans"]] == [
        "task_execution",
        "fs_read",
    ]
    assert result["traces"][0]["spans"][0]["evidence_ref"] == {
        "ulid": "01ARZ3NDEKTSV4RRFFQ69SU0001",
        "channel": "agent.telemetry.run",
        "created_at": "2026-08-20T01:00:01+00:00",
    }


def test_exception_digest_omits_normal_state_and_reports_outcomes():
    result = build_exception_digest(_events("success", "failure", "retry", "fallback"))

    assert result["normal_spans_omitted"] == 4
    kinds = {item["kind"] for item in result["exceptions"]}
    assert {"failure", "retry", "fallback"} <= kinds
    assert all(item["outcome"] != "success" for item in result["exceptions"])
    assert result["total"] == len(result["exceptions"])


def test_exception_digest_preserves_task_id_for_governed_actions():
    events = _events("failure")
    events[0]["payload"]["task_id"] = 42

    failures = [
        item for item in build_exception_digest(events)["exceptions"] if item["kind"] == "failure"
    ]

    assert failures[0]["task_id"] == 42


def test_exception_digest_detects_missing_telemetry_repeated_failures_and_storms():
    events = _events("failure", "failure", "fallback", "fallback", "fallback")
    result = build_exception_digest(events, expected_trace_ids=["trace-not-seen"])

    kinds = {item["kind"] for item in result["exceptions"]}
    assert {"missing_telemetry", "repeated_failure", "fallback_storm"} <= kinds


def test_fallback_storm_is_per_trace_not_batch_wide():
    """Three fallbacks across three distinct traces must not raise a storm."""
    events = []
    for i in range(3):
        fixture = generate_fixture("fallback")
        span = dict(fixture["spans"][0])
        span["trace_id"] = f"01ARZ3NDEKTSV4RRFFQTRACE{i:02d}"
        events.append(
            {
                "ulid": f"01ARZ3NDEKTSV4RRFFQ69FB{i:04d}",
                "channel": "agent.telemetry.model",
                "payload": span,
                "originator": "Desktop_agent3",
                "created_at": f"2026-08-20T01:00:{i:02d}+00:00",
            }
        )
    result = build_exception_digest(events, fallback_storm_count=3)
    assert "fallback_storm" not in {item["kind"] for item in result["exceptions"]}

    # Same trace, three fallbacks → storm.
    same_trace = []
    for i in range(3):
        span = dict(generate_fixture("fallback")["spans"][0])
        span["span_id"] = f"{span['trace_id']}{i:02d}"
        same_trace.append(
            {
                "ulid": f"01ARZ3NDEKTSV4RRFFQ69FS{i:04d}",
                "channel": "agent.telemetry.model",
                "payload": span,
                "originator": "Desktop_agent3",
                "created_at": f"2026-08-20T02:00:{i:02d}+00:00",
            }
        )
    stormed = build_exception_digest(same_trace, fallback_storm_count=3)
    assert "fallback_storm" in {item["kind"] for item in stormed["exceptions"]}


def test_sum_usage_prefers_tokens_total_when_split_missing():
    spans = [
        {"usage": {"tokens_in": 0, "tokens_out": 0, "tokens_total": 900}},
        {"usage": {"tokens_in": 10, "tokens_out": 5, "tokens_total": 15}},
    ]
    usage = _sum_usage(spans)
    assert usage["tokens_in"] == 10
    assert usage["tokens_out"] == 5
    assert usage["tokens_total"] == 915  # 900 (total-only) + 10 + 5


def test_exception_digest_surfaces_latency_and_cost_anomalies():
    events = _events("success")
    span = events[0]["payload"]
    span["duration_ms"] = 2500
    span["usage"]["cost_usd"] = 0.02

    result = build_exception_digest(events)
    kinds = {item["kind"] for item in result["exceptions"]}
    assert {"latency_anomaly", "cost_anomaly"} <= kinds


def test_telemetry_payload_uses_newest_window():
    """limit=N must be the newest N events, not the oldest."""
    from unified_agent import events

    for i in range(5):
        events.record_event(
            channel="agent.telemetry.tool",
            payload={
                "trace_id": f"trace-{i:04d}",
                "span_id": f"span-{i:04d}",
                "kind": "tool",
                "name": f"step_{i}",
                "outcome": "success",
            },
            originator="Desktop_agent3",
        )

    payload = telemetry_payload(limit=2)
    names = [span["name"] for span in payload["waterfall"]["spans"]]
    assert names == ["step_3", "step_4"]
    assert payload["source"]["event_count"] == 2
