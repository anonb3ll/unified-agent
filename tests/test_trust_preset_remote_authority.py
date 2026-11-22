"""A remote executor must poll current hub admission, never its stale local DB."""

from unittest.mock import Mock

import pytest

from unified_agent import config, storage
from unified_agent.executor import daemon
from unified_agent.tools import tasks


@pytest.fixture
def stale_run(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent-policy.yaml"))
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
    storage.init_db()
    task = tasks.task_add(title="Stale local executor copy")
    claim = tasks.task_claim(task["id"], "Desktop_agent3")
    return task["id"], claim["current_run_id"]


def remote_client(monkeypatch, history):
    monkeypatch.delenv("UA_EXECUTOR_ALLOW_DIRECT", raising=False)
    client = daemon.HubClient(
        agent_label="server_agent3",
        hub_url="http://hub.invalid/mcp",
        bearer_token="test-only-synthetic-token",
    )
    client._call_over_hub = Mock(return_value=history)
    return client


def history_for(run_id, admission=True):
    return {
        "runs": [
            {
                "run_id": run_id,
                "state": "open",
                "cancel_requested": False,
                "native_execution_allowed": admission,
            }
        ]
    }


def test_remote_revocation_overrules_valid_stale_local_database(stale_run, monkeypatch):
    task_id, run_id = stale_run
    history = history_for(run_id)
    hub = remote_client(monkeypatch, history)
    assert daemon._cancel_requested_for_task(task_id, None, run_id) is False
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is False
    history["runs"][0]["native_execution_allowed"] = False
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is True
    assert hub._call_over_hub.call_count == 2
    assert hub._call_over_hub.call_args.args == (
        "observability_manage",
        {"action": "run_history", "params": {"task_id": task_id, "limit": 1}},
    )


@pytest.mark.parametrize("admission", [None, "true", 1, {}, False])
def test_remote_poll_requires_boolean_admission(stale_run, monkeypatch, admission):
    task_id, run_id = stale_run
    history = history_for(run_id, admission)
    if admission is None:
        del history["runs"][0]["native_execution_allowed"]
    hub = remote_client(monkeypatch, history)
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is True


@pytest.mark.parametrize("history", [{}, {"runs": []}, {"runs": [None]}, {"runs": "invalid"}])
def test_remote_poll_invalid_history_stops_without_local_read(stale_run, monkeypatch, history):
    task_id, run_id = stale_run
    hub = remote_client(monkeypatch, history)
    monkeypatch.setattr(storage, "connect", Mock(side_effect=AssertionError("must poll hub")))
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is True


def test_remote_poll_outage_cannot_use_client_fallback(stale_run, monkeypatch):
    task_id, run_id = stale_run
    hub = remote_client(monkeypatch, history_for(run_id))
    # Configured remote authority forbids general-tool fallback as well.
    assert hub.allow_fallback is False
    hub._call_over_hub.side_effect = RuntimeError("hub unavailable")
    hub._fallback_or_raise = Mock(side_effect=AssertionError("no admission fallback"))
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is True
    hub._fallback_or_raise.assert_not_called()


def test_explicit_standalone_mode_retains_local_authority(stale_run, monkeypatch):
    task_id, run_id = stale_run
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    hub = daemon.HubClient(
        agent_label="Desktop_agent3",
        hub_url="http://127.0.0.1:1/mcp",
        bearer_token="test-only-synthetic-token",
    )
    hub._call_over_hub = Mock(side_effect=AssertionError("standalone reads local store"))
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is False
    hub._call_over_hub.assert_not_called()


def test_legacy_direct_flag_cannot_disable_explicit_remote_authority(stale_run, monkeypatch):
    task_id, run_id = stale_run
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    hub = daemon.HubClient(
        agent_label="Desktop_agent3",
        hub_url="http://hub.invalid/mcp",
        bearer_token="test-only-synthetic-token",
    )
    hub._call_over_hub = Mock(return_value=history_for(run_id, False))
    assert hub.authoritative_run_checks is True
    assert daemon._cancel_requested_for_task(task_id, hub, run_id) is True
    hub._call_over_hub.assert_called_once()


def claimed_delegation():
    return {
        "id": 7,
        "task_id": 42,
        "claim_generation": 3,
        "task_spec": "Work without native containment",
        "from_agent": "Desktop_Agent5",
    }


@pytest.mark.parametrize("admission", [None, "true", 1, {}, False])
def test_remote_prelaunch_requires_admission_before_artifacts(isolated_env, monkeypatch, admission):
    monkeypatch.delenv("UA_EXECUTOR_ALLOW_DIRECT", raising=False)
    backend = Mock()
    executor = daemon.ExecutorDaemon(
        agent_label="Desktop_agent3",
        hub_url="http://hub.invalid/mcp",
        bearer_token="test-only-synthetic-token",
        backend=backend,
    )
    executor._mark_delegation_failed = Mock()
    executor.hub_client.call_tool = Mock(return_value={})
    executor.task_worktree = Mock(side_effect=AssertionError("must refuse before artifacts"))
    executor._cycle_gate_for_task = Mock(side_effect=AssertionError("native gate must be first"))
    claim = {"current_run_id": "sp-remote", "native_execution_allowed": admission}
    if admission is None:
        del claim["native_execution_allowed"]
    out = executor._execute_claimed_task(claimed_delegation(), claim)
    assert out["status"] == "blocked_by_trust_preset"
    assert out["cleanup_error"] is None
    executor._mark_delegation_failed.assert_called_once()
    assert executor._mark_delegation_failed.call_args.args[3] == 3
    assert executor.hub_client.call_tool.call_args.args[0] == "task_update"
    assert executor.hub_client.call_tool.call_args.args[1]["expected_run_id"] == "sp-remote"
    executor._cycle_gate_for_task.assert_not_called()
    executor.task_worktree.assert_not_called()
    backend.run.assert_not_called()


def test_standalone_prelaunch_preserves_old_claim_compatibility(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent-policy.yaml"))
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    executor = daemon.ExecutorDaemon(
        agent_label="Desktop_agent3",
        hub_url="http://127.0.0.1:1/mcp",
        bearer_token="test-only-synthetic-token",
        backend=Mock(),
    )
    executor._mark_delegation_failed = Mock()
    executor._cycle_gate_for_task = Mock(side_effect=RuntimeError("reached cycle admission"))
    monkeypatch.setattr(daemon.evidence, "record_executor_event", Mock())
    out = executor._execute_claimed_task(claimed_delegation(), {"current_run_id": "sp-standalone"})
    assert executor.hub_client.authoritative_run_checks is False
    assert out["status"] == "blocked_by_cycle_gate_unverifiable"
    executor._cycle_gate_for_task.assert_called_once_with(42)


@pytest.mark.parametrize("hub_result", [False, "unavailable"])
def test_remote_true_claim_requires_current_http_preflight_before_artifacts(
    isolated_env, monkeypatch, tmp_path, hub_result
):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent-policy.yaml"))
    # A legacy fallback switch cannot turn locally supplied claim metadata into
    # authoritative permission for a configured non-loopback hub.
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    backend = Mock()
    executor = daemon.ExecutorDaemon(
        agent_label="Desktop_agent3",
        hub_url="http://hub.invalid/mcp",
        bearer_token="test-only-synthetic-token",
        backend=backend,
    )
    executor._mark_delegation_failed = Mock()
    executor.hub_client.call_tool = Mock(return_value={})
    executor.hub_client._call_over_hub = Mock(return_value=history_for("sp-remote", hub_result))
    if hub_result == "unavailable":
        executor.hub_client._call_over_hub.side_effect = RuntimeError("hub unavailable")
    executor.task_worktree = Mock(side_effect=AssertionError("must refuse before artifacts"))
    executor._cycle_gate_for_task = Mock(
        side_effect=AssertionError("native preflight must be first")
    )
    out = executor._execute_claimed_task(
        claimed_delegation(),
        {"current_run_id": "sp-remote", "native_execution_allowed": True},
    )
    assert out["status"] == "blocked_by_trust_preset"
    assert executor.hub_client.call_tool.call_args.args[1]["expected_run_id"] == "sp-remote"
    executor.hub_client._call_over_hub.assert_called_once()
    executor._cycle_gate_for_task.assert_not_called()
    executor.task_worktree.assert_not_called()
    backend.run.assert_not_called()


@pytest.mark.parametrize("agent", ["Desktop_agent3", "server_agent3"])
def test_remote_task_claim_never_provisions_through_direct_fallback(
    isolated_env, monkeypatch, agent
):
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    hub = daemon.HubClient(
        agent_label=agent,
        hub_url="http://hub.invalid/mcp",
        bearer_token="test-only-synthetic-token",
    )
    hub._call_over_hub = Mock(side_effect=RuntimeError("hub unavailable"))
    hub._direct_tool_fallback = Mock(return_value={"native_execution_allowed": True})
    with pytest.raises(RuntimeError, match="Failed to communicate"):
        hub.call_tool("task_claim", {"id": 42, "agent": agent})
    hub._direct_tool_fallback.assert_not_called()


def test_explicit_standalone_keeps_direct_task_claim_fallback(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    hub = daemon.HubClient(
        agent_label="Desktop_agent3",
        hub_url="http://127.0.0.1:1/mcp",
        bearer_token="test-only-synthetic-token",
    )
    hub._call_over_hub = Mock(side_effect=RuntimeError("no standalone HTTP service"))
    hub._direct_tool_fallback = Mock(return_value={"current_run_id": "sp-standalone"})
    assert hub.call_tool("task_claim", {"id": 42, "agent": "Desktop_agent3"}) == {
        "current_run_id": "sp-standalone"
    }
    hub._direct_tool_fallback.assert_called_once()
