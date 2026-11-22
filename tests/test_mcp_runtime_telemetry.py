"""Unit tests for MCP 2.x runtime telemetry and doctor health evaluation (UA #866)."""

import pytest

from unified_agent import mcp_live_state, mcp_progress, mcp_runtime_telemetry
from unified_agent.tools import doctor
from unified_agent.tools.doctor import system_doctor
from unified_agent.tools.mcp_health import mcp_health_telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry(monkeypatch):
    monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "")
    monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])
    mcp_runtime_telemetry.reset_telemetry()
    yield
    mcp_runtime_telemetry.reset_telemetry()


def _by_capability(checks: list[dict]) -> dict:
    return {c["capability"]: c for c in checks}


def test_fresh_state_telemetry_and_doctor():
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "healthy"
    assert snapshot["negotiated_protocol_version"] is None
    assert snapshot["target_protocol_version"] == "2026-07-28"
    assert snapshot["progress"]["events_emitted"] == 0
    assert snapshot["reconnect"]["attempts"] == 0
    assert snapshot["actionable_evidence"] == []

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "DORMANT"
    assert "no active MCP 2.x client session" in mcp_check["detail"]
    assert mcp_check["gate"] == "MCP 2.x protocol negotiation"


def test_healthy_negotiated_connection():
    mcp_runtime_telemetry.record_connection(
        client_name="claude-desktop",
        client_version="1.2.3",
        protocol_version="2026-07-28",
        client_capabilities={"tools": True, "prompts": False},
        transport_mode="streamable_http",
    )
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "healthy"
    assert snapshot["negotiated_protocol_version"] == "2026-07-28"
    assert snapshot["client"]["name"] == "claude-desktop"
    assert snapshot["client"]["version"] == "1.2.3"
    assert snapshot["client"]["capabilities"] == {"tools": True, "prompts": False}
    assert snapshot["transport_mode"] == "streamable_http"

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "LIVE"
    assert "protocol 2026-07-28 negotiated with claude-desktop" in mcp_check["detail"]


def test_version_skew_incompatible():
    mcp_runtime_telemetry.record_version_skew(
        client_name="ancient-client",
        client_version="0.0.1",
        requested_version="2023-01-01",
        reason="Unsupported protocol version",
    )
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "incompatible"
    assert snapshot["version_skew"]["failures_count"] == 1
    assert len(snapshot["actionable_evidence"]) == 1
    assert "Version skew" in snapshot["actionable_evidence"][0]

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "MISCONFIGURED"
    assert mcp_check["detail"].startswith("incompatible:")
    assert "ancient-client" in mcp_check["detail"]


def test_reconnect_failure_degraded():
    mcp_runtime_telemetry.record_connection(client_name="agy", protocol_version="2026-07-28")
    mcp_runtime_telemetry.record_reconnect(attempt=1, success=False, detail="stream hung")
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "degraded"
    assert snapshot["reconnect"]["failures"] == 1
    assert len(snapshot["actionable_evidence"]) == 1
    assert "1 reconnect failures" in snapshot["actionable_evidence"][0]

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "MISCONFIGURED"
    assert mcp_check["detail"].startswith("degraded:")


def test_stale_snapshot_degraded():
    mcp_runtime_telemetry.record_connection(client_name="agy", protocol_version="2026-07-28")
    mcp_runtime_telemetry.record_stale_snapshot(
        uri="unified-agent://board", client_rev="rev-1", server_rev="rev-5"
    )
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "degraded"
    assert snapshot["stale_snapshots"]["count"] == 1
    assert len(snapshot["actionable_evidence"]) == 1
    assert "Stale snapshots: 1" in snapshot["actionable_evidence"][0]

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "MISCONFIGURED"
    assert mcp_check["detail"].startswith("degraded:")


def test_legacy_protocol_degraded():
    mcp_runtime_telemetry.record_connection(
        client_name="legacy-client",
        protocol_version="2024-11-05",
    )
    snapshot = mcp_health_telemetry()
    assert snapshot["status"] == "degraded"
    assert any("Legacy protocol negotiated" in ev for ev in snapshot["actionable_evidence"])

    checks = _by_capability(system_doctor()["checks"])
    mcp_check = checks["mcp2_runtime_telemetry"]
    assert mcp_check["status"] == "MISCONFIGURED"
    assert "Legacy protocol negotiated" in mcp_check["detail"]


def test_metric_queue_bounds():
    for i in range(30):
        mcp_runtime_telemetry.record_reconnect(attempt=i + 1, success=False, detail=f"fail-{i}")
        mcp_runtime_telemetry.record_stale_snapshot(
            uri=f"uri://{i}", client_rev="1", server_rev="2"
        )
        mcp_runtime_telemetry.record_version_skew(
            client_name=f"client-{i}", requested_version="2020-01-01", reason=f"too old {i}"
        )

    snapshot = mcp_health_telemetry()
    assert len(snapshot["reconnect"]["recent_events"]) == 20
    assert len(snapshot["stale_snapshots"]["recent_events"]) == 20
    assert len(snapshot["version_skew"]["recent_failures"]) == 20
    # Counters still track total
    assert snapshot["reconnect"]["failures"] == 30
    assert snapshot["stale_snapshots"]["count"] == 30


def test_secret_redaction_in_capabilities():
    mcp_runtime_telemetry.record_connection(
        client_name="test-client",
        client_capabilities={
            "resources": True,
            "secret_token": "super-secret-pass",
            "anthropic_api_key": "sk-ant-123456789",
            "bearer_token": "bearer123",
            "experimental": {"nested_flag": True},
        },
    )
    snapshot = mcp_health_telemetry()
    caps = snapshot["client"]["capabilities"]
    assert "resources" in caps
    assert "secret_token" not in caps
    assert "anthropic_api_key" not in caps
    assert "bearer_token" not in caps
    assert caps["experimental"] == {"nested_flag": True}


def test_progress_telemetry_integration():
    mcp_progress.report_progress(25, 100, "quarter done")
    snapshot = mcp_health_telemetry()
    assert snapshot["progress"]["events_emitted"] == 1
    assert snapshot["progress"]["active"] is True
    assert snapshot["progress"]["last_event_at"] is not None

    mcp_progress.report_progress(100, 100, "all done")
    snapshot2 = mcp_health_telemetry()
    assert snapshot2["progress"]["events_emitted"] == 2
    assert snapshot2["progress"]["active"] is False


def test_subscription_counts():
    mcp_runtime_telemetry.record_subscription_counts(total=5, board=1, tasks=3, runs=1)
    snapshot = mcp_health_telemetry()
    assert snapshot["subscriptions"] == {
        "total": 5,
        "board": 1,
        "tasks": 3,
        "runs": 1,
    }


def test_subscription_counts_include_registry_resource_kinds():
    mcp_runtime_telemetry.record_subscription_counts(total=3, plans=2, approvals=1)
    assert mcp_health_telemetry()["subscriptions"] == {
        "total": 3,
        "board": 0,
        "tasks": 0,
        "runs": 0,
        "plans": 2,
        "approvals": 1,
    }


def test_active_subscription_counts_derive_unlisted_registry_kinds():
    class _Bus:
        _listeners = {
            "unified-agent://plans/p-1": {object(), object()},
            "unified-agent://approvals/2": {object()},
        }

    class _Server:
        _subscriptions = _Bus()

    mcp_live_state.update_subscription_telemetry(_Server())
    assert mcp_health_telemetry()["subscriptions"] == {
        "total": 3,
        "board": 0,
        "tasks": 0,
        "runs": 0,
        "plan": 2,
        "approval": 1,
    }


def test_session_touch_middleware_tracking():
    mcp_runtime_telemetry.record_session_touch(
        session_id="sess-xyz-123", agent_label="Desktop_agent3"
    )
    snapshot = mcp_health_telemetry()
    assert snapshot["session"]["last_id"] == "sess-xyz-123"
    assert snapshot["session"]["last_agent"] == "Desktop_agent3"
    assert snapshot["session"]["touches_count"] >= 1
