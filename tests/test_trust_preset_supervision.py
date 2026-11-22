"""Local coordinated native runs must obey live preset changes."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from unified_agent import storage
from unified_agent.dispatch import coordinator, supervise
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.tools import delegate, tasks


@pytest.fixture
def launched(isolated_env, tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    storage.init_db()
    packed = delegate.delegate(
        task_spec="Inspect assigned source",
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent1",
        callback="none",
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
            ("pid:review-only", out["run_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    terminate = Mock(return_value="kill_unconfirmed")
    monkeypatch.setattr(supervise, "terminate_ours", terminate)
    monkeypatch.setattr(supervise, "pid_is_ours", lambda _ref: True)
    return out["run_id"], policy, terminate


@pytest.mark.parametrize("binding", ["Desktop_Agent1", "Desktop_Agent5", "invalid"])
@pytest.mark.parametrize("expired", [False, True])
def test_current_policy_refuses_live_native_renewal_and_requests_exact_run_stop(
    launched,
    binding,
    expired,
):
    run_id, policy, terminate = launched
    if expired:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?",
                ((datetime.now(UTC) - timedelta(seconds=30)).isoformat(), run_id),
            )
            conn.commit()
        finally:
            conn.close()
    before = coordinator.get_run(run_id)
    policy.write_text(
        "trust_presets: {default: invalid}"
        if binding == "invalid"
        else json.dumps({"trust_presets": {"agents": {binding: "inspection"}}})
    )
    result = coordinator.reclaim_expired_leases()
    assert result["renewed_count"] == 0
    terminate.assert_called_once_with(before["evidence_ref"])
    after = coordinator.get_run(run_id)
    assert after["state"] == "cancelled"
    assert after["termination_outcome"] == "unknown"
    assert after["lease_expires_at"] == before["lease_expires_at"]
    assert after["failure_reason"] == "trust_preset_native_denied"


def test_confirmed_revocation_uses_existing_cancellation_classification(launched):
    run_id, policy, terminate = launched
    terminate.return_value = "terminated"
    policy.write_text(json.dumps({"trust_presets": {"default": "inspection"}}))
    coordinator.reclaim_expired_leases()
    terminate.assert_called_once()
    after = coordinator.get_run(run_id)
    assert after["termination_outcome"] == "confirmed"
    assert after["lease_expires_at"] is None


def test_unbound_unexpired_run_is_left_untouched(launched):
    run_id, _, terminate = launched
    before = coordinator.get_run(run_id)
    coordinator.reclaim_expired_leases()
    terminate.assert_not_called()
    assert coordinator.get_run(run_id) == before
