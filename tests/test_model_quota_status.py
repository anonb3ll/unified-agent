"""Truthful model_quota_status: exhausted vs unknown vs available (task #1081)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import credential_pool, server, storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.dispatch.report import report_outcome
from unified_agent.tools import delegate, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    # Default: no Codex sessions tree unless a test points at fixtures.
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
    storage.init_db()
    return tmp_path


def _write_codex_meter(
    sessions_root,
    *,
    used_percent: float,
    window_minutes: int = 10080,
    timestamp: str | None = None,
):
    ts = timestamp if timestamp is not None else storage.now()
    day = sessions_root / "2026" / "09" / "18"
    day.mkdir(parents=True)
    path = day / "rollout-meter.jsonl"
    event = {
        "timestamp": ts,
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "prolite",
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": window_minutes,
                    "resets_in_seconds": None,
                },
            }
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return path


def _spec() -> str:
    return (
        "### Objective\nQuota status\n"
        "### Scope\n- src/unified_agent/\n"
        "### Definition of Done\n- availability derived\n"
        "### TDD Constraints\n- Must be real: observations\n"
        "### Boundaries\n- Must not probe providers\n"
    )


def _record_failed_run(agent: str, failure_reason: str, monkeypatch) -> str:
    packed = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent=agent, callback="memory"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-25T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", out["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", agent)
    assert report_outcome("failed", failure_reason=failure_reason)["ok"] is True
    return out["run_id"]


def _by_lane(result: dict) -> dict:
    return {row["lane"]: row for row in result["quotas"]}


def test_model_quota_status_is_registered():
    assert "model_quota_status" in server.REGISTERED_TOOLS


def test_lane_with_no_observations_is_unknown_with_no_source_never_available(db):
    result = credential_pool.model_quota_status()
    lanes = _by_lane(result)

    assert result["checked_at"]
    for lane in ("claude", "agy", "cursor", "codex"):
        row = lanes[lane]
        assert row["availability"] == "unknown"
        assert row["source"] == "no source"
        assert row["remaining"] == "unknown"
        assert row["availability"] != "available"


def test_lane_exhausted_via_usage_limit_failure_is_unavailable_with_evidence(db, monkeypatch):
    _record_failed_run(
        "Desktop_Agent1",
        "HTTP 429: The usage limit has been reached — quota exhausted",
        monkeypatch,
    )

    result = credential_pool.model_quota_status()
    claude = _by_lane(result)["claude"]

    assert claude["availability"] == "unavailable"
    assert claude["source"] == "dispatch_runs.failure_class"
    assert claude["observed_at"]
    assert (
        "usage limit" in (claude.get("evidence") or "").lower()
        or "quota" in (claude.get("evidence") or "").lower()
    )
    assert claude["failure_class"] == "provider_quota_exhausted"


def test_iteration_budget_failure_does_not_mark_lane_unavailable(db, monkeypatch):
    _record_failed_run(
        "Desktop_Agent1",
        "iteration budget exhausted (150/150)",
        monkeypatch,
    )

    result = credential_pool.model_quota_status()
    claude = _by_lane(result)["claude"]
    # Iteration-budget is Hermes-side, not provider quota — must not flip the lane.
    assert claude["availability"] == "unknown"
    assert claude["source"] == "no source"


def test_codex_meter_below_cap_reports_available_with_used_percent(db, monkeypatch, tmp_path):
    sessions = tmp_path / "codex-sessions"
    ts = storage.now()
    source = _write_codex_meter(sessions, used_percent=5.0, timestamp=ts)
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))

    result = credential_pool.model_quota_status()
    codex = _by_lane(result)["codex"]

    assert codex["availability"] == "available"
    assert codex["used_percent"] == 5.0
    assert codex["window_minutes"] == 10080
    assert codex["observed_at"] == ts
    assert codex["source"] == "codex_rollout_rate_limits"
    assert codex["source_path"] == str(source)
    assert codex["resets_in_seconds"] is None
    assert codex["plan_type"] == "prolite"


def test_codex_meter_at_100_percent_reports_unavailable(db, monkeypatch, tmp_path):
    sessions = tmp_path / "codex-sessions"
    _write_codex_meter(sessions, used_percent=100.0, window_minutes=300)
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))

    result = credential_pool.model_quota_status()
    codex = _by_lane(result)["codex"]

    assert codex["availability"] == "unavailable"
    assert codex["used_percent"] == 100.0
    assert codex["source"] == "codex_rollout_rate_limits"
    assert codex["resets_in_seconds"] is None


def test_registry_rows_still_surface_when_present(db, monkeypatch, tmp_path):
    credential_pool.credential_pool_register("openai", "work-cli")
    sessions = tmp_path / "codex-sessions"
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))

    result = credential_pool.model_quota_status()
    labels = {(q.get("provider"), q.get("label")) for q in result["quotas"]}
    assert ("openai", "work-cli") in labels
    registry_row = next(
        q
        for q in result["quotas"]
        if q.get("provider") == "openai" and q.get("label") == "work-cli"
    )
    # No observations for a bare registry row → unknown, never available.
    assert registry_row["availability"] == "unknown"
    assert registry_row["source"] == "no source"
    assert registry_row["remaining"] == "unknown"


def _age_newest_dispatch_exhaustion(*, age_s: float) -> None:
    """Push the newest provider_quota_exhausted dispatch_runs row into the past."""
    stale_ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET updated_at=? "
            "WHERE run_id=(SELECT run_id FROM dispatch_runs "
            "WHERE failure_class='provider_quota_exhausted' "
            "ORDER BY updated_at DESC LIMIT 1)",
            (stale_ts,),
        )
        conn.commit()
    finally:
        conn.close()


def test_stale_exhaustion_observation_degrades_to_unknown(db, monkeypatch):
    """TTL: an exhaustion observation older than UA_QUOTA_EXHAUSTION_TTL_S
    (default 24h) no longer produces unavailable — the lane recovers to
    unknown. This is the un-blocking path #1251 review required."""
    _record_failed_run(
        "Desktop_Agent1",
        "HTTP 429: The usage limit has been reached — quota exhausted",
        monkeypatch,
    )
    _age_newest_dispatch_exhaustion(age_s=48 * 3600)

    claude = _by_lane(credential_pool.model_quota_status())["claude"]

    assert claude["availability"] == "unknown"
    assert claude["stale"] is True
    assert claude["failure_class"] is None
    assert claude["observed_at"]
    assert "stale" in (claude.get("evidence") or "").lower()


def test_fresh_exhaustion_within_ttl_still_blocks(db, monkeypatch):
    """The TTL widens but does not remove the block: evidence inside the
    window still produces unavailable."""
    _record_failed_run(
        "Desktop_Agent1",
        "HTTP 429: The usage limit has been reached — quota exhausted",
        monkeypatch,
    )
    _age_newest_dispatch_exhaustion(age_s=3600)  # 1h old, inside 24h TTL

    claude = _by_lane(credential_pool.model_quota_status())["claude"]
    assert claude["availability"] == "unavailable"


def test_exhaustion_ttl_env_override(db, monkeypatch):
    """UA_QUOTA_EXHAUSTION_TTL_S stretches the freshness window."""
    _record_failed_run(
        "Desktop_Agent1",
        "HTTP 429: The usage limit has been reached — quota exhausted",
        monkeypatch,
    )
    _age_newest_dispatch_exhaustion(age_s=48 * 3600)
    monkeypatch.setenv("UA_QUOTA_EXHAUSTION_TTL_S", str(7 * 24 * 3600))

    claude = _by_lane(credential_pool.model_quota_status())["claude"]
    assert claude["availability"] == "unavailable"


def test_stale_codex_meter_degrades_to_unknown_but_keeps_reading(db, monkeypatch, tmp_path):
    """A stale rate_limits sample cannot latch the codex lane either; the
    last reading stays visible with availability=unknown."""
    sessions = tmp_path / "codex-sessions"
    old_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    _write_codex_meter(sessions, used_percent=100.0, window_minutes=300, timestamp=old_ts)
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))

    codex = _by_lane(credential_pool.model_quota_status())["codex"]

    assert codex["availability"] == "unknown"
    assert codex["stale"] is True
    assert codex["used_percent"] == 100.0
    assert codex["source"] == "codex_rollout_rate_limits"
    assert codex["failure_class"] is None


def test_research_profile_does_not_expose_model_quota_status():
    """Finding for #1081(d): research allow-list omits model_quota_status by design.

    capability_catalog is reachable; model_quota_status is not. Do not widen the
    research surface unilaterally — record the finding only.
    """
    from unified_agent import profiles

    assert profiles.tool_is_allowed_for(profiles.RESEARCH, "capability_catalog")
    assert not profiles.tool_is_allowed_for(profiles.RESEARCH, "model_quota_status")
