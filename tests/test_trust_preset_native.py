"""Native refusal must happen before backend, process, or context side effects."""

import json
from unittest.mock import Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.dispatch import launch
from unified_agent.executor import backends
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate, pi_runner


@pytest.fixture(autouse=True)
def db(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    storage.init_db()


def bind(tmp_path, monkeypatch, agent):
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps({"trust_presets": {"agents": {agent: "inspection"}}}))
    monkeypatch.setenv("UA_POLICY_FILE", str(path))


@pytest.mark.parametrize(
    "name",
    ["pi", "codex", "agy", "agy-interactive", "claude", "cursor", "copilot", "hermes", "devin"],
)
def test_every_concrete_backend_refuses_before_process_or_environment(name, tmp_path, monkeypatch):
    backend = backends.get_backend(name)
    bind(tmp_path, monkeypatch, "Desktop_Agent2")
    spawned = Mock(side_effect=AssertionError("process must never start"))
    child_env = Mock(side_effect=AssertionError("environment must never be prepared"))
    monkeypatch.setattr(backends, "run_popen_with_kill_check", spawned)
    monkeypatch.setattr(backends, "_add_child_cache_env", child_env)
    monkeypatch.setattr(pi_runner, "pi_run_task", spawned)
    with pytest.raises(ToolError):
        backend.run(
            prompt="test",
            cwd=".",
            session_dir="sessions",
            session_id="test",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
    spawned.assert_not_called()
    child_env.assert_not_called()


@pytest.mark.parametrize("bound_agent", ["Desktop_agent3", "Desktop_Agent5"])
def test_executor_checks_target_and_originator_before_worktree_or_backend(
    bound_agent, tmp_path, monkeypatch
):
    backend = Mock()
    daemon = ExecutorDaemon(
        agent_label="Desktop_agent3", backend=backend, hub_url="http://127.0.0.1:1/mcp"
    )
    daemon._mark_delegation_failed = Mock()
    daemon.hub_client.call_tool = Mock(return_value={})
    daemon.task_worktree = Mock(side_effect=AssertionError("must not prepare a worktree"))
    daemon._cycle_gate_for_task = Mock(side_effect=AssertionError("must stop first"))
    bind(tmp_path, monkeypatch, bound_agent)
    out = daemon._execute_claimed_task(
        {
            "id": 1,
            "task_id": 1,
            "claim_generation": 1,
            "task_spec": "Inspect",
            "from_agent": "Desktop_Agent5",
        },
        {"current_run_id": "sp-test"},
    )
    assert out["status"] == "blocked_by_trust_preset"
    assert out["cleanup_error"] is None
    assert daemon.hub_client.call_tool.call_args.args[1]["expected_run_id"] == "sp-test"
    backend.run.assert_not_called()
    daemon.task_worktree.assert_not_called()
    daemon._cycle_gate_for_task.assert_not_called()
    daemon._mark_delegation_failed.assert_called_once()


@pytest.mark.parametrize("bound_agent", ["Desktop_Agent1", "Desktop_Agent5"])
def test_real_dispatch_refuses_target_or_originator_before_route_or_spawn(
    bound_agent, tmp_path, monkeypatch
):
    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    bind(tmp_path, monkeypatch, bound_agent)
    spawner = Mock()
    boundary = launch.RoutedLaunchBoundary(spawner=spawner)
    resolver = Mock(side_effect=AssertionError("native guard must be first"))
    monkeypatch.setattr(launch, "_resolve_route_or_error", resolver)
    out = boundary.launch(
        run_id="run-1",
        task_id=packed["task_id"],
        agent_label="Desktop_Agent1",
        delegation_id=packed["delegation_id"],
    )
    assert out["ok"] is False and out["spawned"] is False
    spawner.spawn.assert_not_called()
    resolver.assert_not_called()
    assert boundary.last_request is None


def test_direct_real_spawner_refuses_before_sidecars_logs_or_env(tmp_path, monkeypatch):
    bind(tmp_path, monkeypatch, "Desktop_Agent1")
    sidecar = Mock(side_effect=AssertionError("must not write sidecar"))
    monkeypatch.setattr(launch.claude_sandbox, "write_dispatch_sidecar", sidecar)
    out = launch.CliProcessSpawner().spawn(
        {
            "agent_label": "Desktop_Agent1",
            "binary": "claude",
            "cli_backend": "claude",
            "run_id": "run-1",
            "cwd": str(tmp_path),
            "task_id": 1,
        }
    )
    assert out["ok"] is False and out["spawned"] is False
    sidecar.assert_not_called()
    assert list(tmp_path.glob("**/*.jsonl")) == []


def test_node_dispatch_refuses_before_enqueue(tmp_path, monkeypatch):
    from unified_agent import node_queue

    bind(tmp_path, monkeypatch, "Desktop_Agent1")
    route = Mock(cli_backend="claude")
    monkeypatch.setattr(launch, "_resolve_route_or_error", lambda _agent: (route, Mock(), None))
    monkeypatch.setattr(
        node_queue, "select_node_for_agent", lambda *_a: {"routed": True, "ok": True}
    )
    enqueue = Mock(side_effect=AssertionError("node must not launch"))
    monkeypatch.setattr(node_queue, "enqueue", enqueue)
    out = launch.NodeRoutedLaunchBoundary(fallback=launch.RecordingLaunchBoundary()).launch(
        run_id="run-1", task_id=1, agent_label="Desktop_Agent1", delegation_id=None
    )
    assert out["ok"] is False and out["node_routed"] is True
    enqueue.assert_not_called()


def test_unbound_backend_retains_existing_execution_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    result = {"status": "success", "text": "done"}
    run = Mock(return_value=result)
    monkeypatch.setattr(pi_runner, "pi_run_task", run)
    assert (
        backends.PiBackend().run(
            prompt="test",
            cwd=".",
            session_dir="sessions",
            session_id="test",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
        == result
    )
    run.assert_called_once()


@pytest.mark.parametrize("bound_agent", ["Desktop_agent3", "Desktop_Agent5"])
def test_executor_poll_refuses_before_claim_or_worktree_provisioning(
    bound_agent, tmp_path, monkeypatch
):
    daemon = ExecutorDaemon(
        agent_label="Desktop_agent3",
        backend=Mock(),
        shadow_mode=False,
        hub_url="http://127.0.0.1:1/mcp",
    )
    daemon.find_pending_work = Mock(
        return_value=[
            {"task_id": 1, "id": 1, "task_spec": "Inspect", "from_agent": "Desktop_Agent5"}
        ]
    )
    daemon._mark_delegation_failed = Mock()
    daemon.hub_client.call_tool = Mock(
        side_effect=AssertionError("must not request a native worktree")
    )
    bind(tmp_path, monkeypatch, bound_agent)
    assert daemon.poll_once()["status"] == "blocked_by_trust_preset"
    daemon.hub_client.call_tool.assert_not_called()
    daemon._mark_delegation_failed.assert_called_once()


def test_remote_executor_honors_hub_denial_with_absent_local_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    backend = Mock()
    daemon = ExecutorDaemon(
        agent_label="Desktop_agent3", backend=backend, hub_url="http://127.0.0.1:1/mcp"
    )
    daemon._mark_delegation_failed = Mock()
    daemon.hub_client.call_tool = Mock(return_value={})
    daemon.task_worktree = Mock(side_effect=AssertionError("must stop before local artifacts"))
    out = daemon._execute_claimed_task(
        {
            "id": 1,
            "task_id": 1,
            "claim_generation": 1,
            "task_spec": "Inspect",
            "from_agent": "Desktop_Agent5",
        },
        {
            "current_run_id": "sp-test",
            "native_execution_allowed": False,
            "native_execution_denial": "hub preset requires native containment",
        },
    )
    assert out["status"] == "blocked_by_trust_preset" and out["cleanup_error"] is None
    backend.run.assert_not_called()
    daemon.task_worktree.assert_not_called()
    daemon._mark_delegation_failed.assert_called_once()
    assert daemon.hub_client.call_tool.call_args.args[0] == "task_update"
    assert daemon.hub_client.call_tool.call_args.args[1]["expected_run_id"] == "sp-test"


def test_remote_revocation_poll_stops_when_hub_binding_changes_and_local_policy_is_absent(
    tmp_path, monkeypatch
):
    from unified_agent.executor import daemon as daemon_module

    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    history = {
        "runs": [
            {
                "run_id": "sp-test",
                "state": "open",
                "cancel_requested": False,
                "native_execution_allowed": True,
            }
        ]
    }
    hub = Mock()
    hub.call_tool.side_effect = lambda *_a: history

    def retired_store():
        raise storage.LiveStoreError("remote executor has no authoritative local store")

    monkeypatch.setattr(storage, "connect", retired_store)
    assert daemon_module._cancel_requested_for_task(1, hub, "sp-test") is False
    history["runs"][0]["native_execution_allowed"] = False
    assert daemon_module._cancel_requested_for_task(1, hub, "sp-test") is True
    hub.call_tool.side_effect = RuntimeError("hub unavailable")
    assert daemon_module._cancel_requested_for_task(1, hub, "sp-test") is True


def test_local_revocation_poll_stops_originator_binding_before_postrun_writes(
    tmp_path, monkeypatch
):
    from unified_agent.executor import daemon as daemon_module
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    packed = delegate.delegate(
        task_spec="Inspect", from_agent="Desktop_Agent5", to_agent="Desktop_agent3", callback="none"
    )
    claimed = tasks.task_claim(packed["task_id"], "Desktop_agent3")
    assert (
        daemon_module._cancel_requested_for_task(packed["task_id"], None, claimed["current_run_id"])
        is False
    )
    bind(tmp_path, monkeypatch, "Desktop_Agent5")
    assert (
        daemon_module._cancel_requested_for_task(packed["task_id"], None, claimed["current_run_id"])
        is True
    )


@pytest.mark.parametrize("restricted", ["Desktop_Agent2", "Desktop_Agent5"])
def test_unattended_daemon_preserves_stored_originator_at_concrete_spawn_seam(
    restricted, tmp_path, monkeypatch
):
    import subprocess

    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    packed = delegate.delegate(
        task_spec="Native compatibility",
        from_agent="Desktop_Agent5",
        to_agent="Desktop_agent3",
        callback="none",
    )
    taken = delegate.delegate_claim(packed["delegation_id"], "Desktop_agent3")["delegation"]
    claim = tasks.task_claim(packed["task_id"], "Desktop_agent3")
    bind(tmp_path, monkeypatch, restricted)
    # Canonical control RPCs have a verified actor only inside HubClient's
    # scoped direct dispatch. Native execution still has no ambient caller.
    monkeypatch.setattr(config, "verified_agent_identity", lambda: config.agent_identity_var.get())
    assert config.verified_agent_identity() is None
    backend = backends.CodexBackend()
    monkeypatch.setattr(backend, "_child_env", lambda _agent: {})
    spawned = Mock(return_value=subprocess.CompletedProcess([], 1, "", "test-only failure"))
    monkeypatch.setattr(backends, "run_popen_with_kill_check", spawned)
    executor = ExecutorDaemon(
        agent_label="Desktop_agent3",
        backend=backend,
        shadow_mode=False,
        hub_url="http://127.0.0.1:1/mcp",
        bearer_token="test-only-synthetic-token",
    )
    out = executor._execute_claimed_task(taken, claim)
    if restricted == "Desktop_Agent5":
        assert out["status"] == "blocked_by_trust_preset"
        spawned.assert_not_called()
    else:
        spawned.assert_called_once()
        assert out["status"] != "blocked_by_trust_preset"
    # Trusted orchestration attribution must not escape the backend call.
    assert config.verified_agent_identity() is None
    with pytest.raises(ToolError, match="verified native launch originator"):
        backend.run(
            prompt="direct",
            cwd=".",
            session_dir="sessions",
            session_id="outside-scope",
            agent_label="Desktop_agent3",
            timeout_s=5,
        )


@pytest.mark.parametrize("restricted", ["caller", "originator", "different-target"])
def test_backend_attribution_scope_keeps_current_binding_and_target_checks(
    restricted, tmp_path, monkeypatch
):
    from unified_agent import trust_presets

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    bind(tmp_path, monkeypatch, "Desktop_Agent2")
    with trust_presets._native_execution_scope("Desktop_agent3", originator="Desktop_Agent5"):
        trust_presets.require_native_allowed("Desktop_agent3")
        if restricted == "caller":
            monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
            target = "Desktop_agent3"
        elif restricted == "originator":
            bind(tmp_path, monkeypatch, "Desktop_Agent5")
            target = "Desktop_agent3"
        else:
            target = "Desktop_Agent1"
        with pytest.raises(ToolError):
            trust_presets.require_native_allowed(target)


def test_backend_attribution_scope_restores_on_exception_and_refuses_missing_originator(
    tmp_path, monkeypatch
):
    from unified_agent import trust_presets

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    bind(tmp_path, monkeypatch, "Desktop_Agent2")
    with pytest.raises(ToolError):
        with trust_presets._native_execution_scope("Desktop_agent3", originator=None):
            pytest.fail("missing stored authority must never enter scope")
    with pytest.raises(RuntimeError, match="backend raised"):
        with trust_presets._native_execution_scope("Desktop_agent3", originator="Desktop_Agent5"):
            raise RuntimeError("backend raised")
    with pytest.raises(ToolError, match="verified native launch originator"):
        trust_presets.require_native_allowed("Desktop_agent3")


def test_backend_scope_preserves_originator_when_presets_are_first_configured(
    tmp_path, monkeypatch
):
    from unified_agent import trust_presets

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing.yaml"))
    with trust_presets._native_execution_scope("Desktop_agent3", originator="Desktop_Agent5"):
        bind(tmp_path, monkeypatch, "Desktop_Agent2")
        trust_presets.require_native_allowed("Desktop_agent3")
        bind(tmp_path, monkeypatch, "Desktop_Agent5")
        with pytest.raises(ToolError, match="native containment"):
            trust_presets.require_native_allowed("Desktop_agent3")


def test_verified_self_target_without_delegator_reaches_concrete_backend(tmp_path, monkeypatch):
    import subprocess

    from unified_agent import trust_presets

    bind(tmp_path, monkeypatch, "Desktop_Agent2")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
    backend = backends.CodexBackend()
    monkeypatch.setattr(backend, "_child_env", lambda _agent: {})
    spawned = Mock(return_value=subprocess.CompletedProcess([], 1, "", "test-only failure"))
    monkeypatch.setattr(backends, "run_popen_with_kill_check", spawned)
    with trust_presets._native_execution_scope("Desktop_agent3", originator=None):
        backend.run(
            prompt="verified self pickup",
            cwd=".",
            session_dir="sessions",
            session_id="self-pickup",
            agent_label="Desktop_agent3",
            timeout_s=5,
        )
    spawned.assert_called_once()
    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    with pytest.raises(ToolError, match="verified native launch originator"):
        trust_presets.require_native_allowed("Desktop_agent3")


def test_missing_delegator_scope_cannot_attribute_a_different_target(tmp_path, monkeypatch):
    from unified_agent import trust_presets

    bind(tmp_path, monkeypatch, "Desktop_Agent2")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent5")
    with pytest.raises(ToolError):
        with trust_presets._native_execution_scope("Desktop_agent3", originator=None):
            pytest.fail("verified absence of delegation applies only to the caller's target")
