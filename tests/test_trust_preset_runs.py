"""Real run evidence and lifecycle atomicity for configured trust presets."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, run_records, storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import delegate, tasks


@pytest.fixture(autouse=True)
def db(isolated_env, monkeypatch):
    storage.init_db()
    # Trusted orchestration can claim a context provisioned before selection.
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)


def bind(tmp_path, monkeypatch, agent="Desktop_Agent2", preset="inspection"):
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps({"trust_presets": {"agents": {agent: preset}}}))
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    return path


def delegated():
    result = delegate.delegate(
        task_spec="Inspect the assigned workspace",
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent1",
        callback="none",
    )
    tasks.task_update(result["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    return result


def test_claim_snapshot_is_folded_and_immutable_after_policy_change(tmp_path, monkeypatch):
    tid = tasks.task_add("Evidence")["id"]
    path = bind(tmp_path, monkeypatch)
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        claimed = tasks.task_claim(tid, "Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)
    run = run_records.get_run(claimed["current_run_id"])
    evidence = run["trust_preset"]
    assert evidence["preset"] == "inspection"
    assert evidence["agent"] == "Desktop_Agent2"
    assert len(evidence["policy_fingerprint"]) == 64
    assert evidence["enforcement"] == {"mediated_tools": True, "native_runtime": False}
    path.write_text('{"trust_presets":{"default":"configured-workspace"}}')
    assert run_records.run_history(task_id=tid)["runs"][0]["trust_preset"] == evidence


def test_invalid_policy_fails_initial_claim_before_lifecycle_commit(tmp_path, monkeypatch):
    tid = tasks.task_add("Invalid policy")["id"]
    path = bind(tmp_path, monkeypatch, preset="invented-mode")
    with pytest.raises(ToolError):
        tasks.task_claim(tid, "Desktop_Agent2")
    row = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid)
    assert row["status"] == "open" and row["claimed_by"] is None
    assert run_records.run_history(task_id=tid)["runs"] == []
    assert path.exists()


def test_selected_preset_requires_successful_run_recorder(tmp_path, monkeypatch):
    tid = tasks.task_add("Recorder failure")["id"]
    bind(tmp_path, monkeypatch)
    monkeypatch.setattr(run_records, "record_claim_in_tx", lambda *_a, **_k: None)
    with pytest.raises(ToolError, match="trust evidence recording failed"):
        tasks.task_claim(tid, "Desktop_Agent2")
    row = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid)
    assert row["status"] == "open" and row["current_run_id"] is None


def test_bound_claim_refuses_implicit_native_worktree_before_commit(tmp_path, monkeypatch):
    tid = tasks.task_add("Must provision first")["id"]
    bind(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: False)
    calls = []
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *_a, **_k: calls.append(1))
    with pytest.raises(ToolError):
        tasks.task_claim(tid, "Desktop_Agent2")
    assert calls == []
    row = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid)
    assert row["status"] == "open" and row["claimed_by"] is None


def test_cancellation_remains_recordable_when_live_policy_becomes_invalid(tmp_path, monkeypatch):
    tid = tasks.task_add("Cancel despite config fault")["id"]
    path = bind(tmp_path, monkeypatch)
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        claimed = tasks.task_claim(tid, "Desktop_Agent2")
        path.write_text("trust_presets: {default: invalid}")
        tasks.task_update(tid, request_cancel=True, expected_run_id=claimed["current_run_id"])
    finally:
        config.agent_identity_var.reset(token)
    run = run_records.get_run(claimed["current_run_id"])
    assert run["cancel_requested"] is True
    assert run["trust_preset"]["preset"] == "inspection"


def test_governed_recording_boundary_preserves_target_and_originator_snapshot(
    tmp_path, monkeypatch
):
    packed = delegated()
    path = bind(tmp_path, monkeypatch, agent="Desktop_Agent1")
    path.write_text(
        json.dumps(
            {
                "trust_presets": {
                    "agents": {
                        "Desktop_Agent1": "inspection",
                        "Desktop_Agent5": "configured-workspace",
                    }
                }
            }
        )
    )
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True and len(launch.calls) == 1
    folded = coordinator.get_run_observability(out["run_id"])
    assert folded["trust_preset"]["preset"] == "inspection"
    assert folded["originator_trust_preset"]["agent"] == "Desktop_Agent5"
    assert folded["originator_trust_preset"]["preset"] == "configured-workspace"
    claimed = next(t for t in folded["transitions"] if t["state"] == "claimed")
    assert claimed["trust_preset"] == folded["trust_preset"]


def test_invalid_policy_never_mints_dispatch_or_invokes_recording_boundary(tmp_path, monkeypatch):
    packed = delegated()
    bind(tmp_path, monkeypatch, agent="Desktop_Agent1", preset="invalid")
    launch = RecordingLaunchBoundary()
    with pytest.raises(ToolError):
        coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert launch.calls == []
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM dispatch_runs").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, attempts FROM delegations WHERE id=?", (packed["delegation_id"],)
        ).fetchone()
        assert row["status"] == "pending" and row["attempts"] == 0
    finally:
        conn.close()


def test_unbound_run_omits_trust_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    tid = tasks.task_add("Unbound")["id"]
    claimed = tasks.task_claim(tid, "Desktop_Agent2")
    assert "trust_preset" not in run_records.get_run(claimed["current_run_id"])
    packed = delegated()
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    assert out["launched"] is True
    assert "trust_preset" not in coordinator.get_run_observability(out["run_id"])
    assert all(
        "trust_preset" not in e["payload"]
        for e in events.query_events(channels=[coordinator.CHANNEL_DISPATCH_TRANSITION], limit=100)
    )


@pytest.mark.parametrize("bound_agent", ["Desktop_Agent1", "Desktop_Agent5"])
def test_hub_claim_and_history_expose_target_or_originator_native_denial(
    bound_agent, tmp_path, monkeypatch
):
    packed = delegated()
    bind(tmp_path, monkeypatch, agent=bound_agent)
    claimed = tasks.task_claim(packed["task_id"], "Desktop_Agent1")
    assert claimed["native_execution_allowed"] is False
    key = "trust_preset" if bound_agent == "Desktop_Agent1" else "originator_trust_preset"
    assert claimed[key]["agent"] == bound_agent
    run = run_records.get_run(claimed["current_run_id"])
    assert run["native_execution_allowed"] is False
    assert run[key] == claimed[key]
    assert run["originator_agent"] == "Desktop_Agent5"


@pytest.mark.parametrize("bound_agent", ["Desktop_Agent1", "Desktop_Agent5"])
def test_hub_live_revocation_changes_admission_without_rewriting_historical_snapshot(
    bound_agent, tmp_path, monkeypatch
):
    packed = delegated()
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    claimed = tasks.task_claim(packed["task_id"], "Desktop_Agent1")
    assert claimed["native_execution_allowed"] is True
    assert run_records.get_run(claimed["current_run_id"])["native_execution_allowed"] is True
    path = bind(tmp_path, monkeypatch, agent=bound_agent)
    live = run_records.run_history(task_id=packed["task_id"])["runs"][0]
    assert live["native_execution_allowed"] is False
    assert "trust_preset" not in live and "originator_trust_preset" not in live
    path.write_text("trust_presets: {default: invalid}")
    broken = run_records.get_run(claimed["current_run_id"])
    assert broken["native_execution_allowed"] is False
    assert broken["state"] == "open" and broken["cancel_requested"] is False


def test_bound_originator_refuses_native_context_before_claim_commit(tmp_path, monkeypatch):
    packed = delegated()
    bind(tmp_path, monkeypatch, agent="Desktop_Agent5")
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: False)
    ensure = []
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *_a, **_k: ensure.append(1))
    with pytest.raises(ToolError):
        tasks.task_claim(packed["task_id"], "Desktop_Agent1")
    assert ensure == []
    assert run_records.run_history(task_id=packed["task_id"])["runs"] == []


def test_terminal_old_delegation_does_not_bind_an_unrelated_new_selfpickup(tmp_path, monkeypatch):
    packed = delegated()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE delegations SET status='completed' WHERE id=?", (packed["delegation_id"],)
        )
        conn.commit()
    finally:
        conn.close()
    bind(tmp_path, monkeypatch, agent="Desktop_Agent5")
    claimed = tasks.task_claim(packed["task_id"], "Desktop_Agent1")
    assert claimed["native_execution_allowed"] is True
    assert "originator_trust_preset" not in claimed
    run = run_records.get_run(claimed["current_run_id"])
    assert "originator_agent" not in run
    assert run["native_execution_allowed"] is True
