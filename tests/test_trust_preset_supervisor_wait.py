"""Detached supervisors retain authority and enforce presets without ticks."""

import io
import json
import os
import subprocess
from unittest.mock import Mock

import pytest

from unified_agent import config, storage
from unified_agent.dispatch import coordinator, supervise, supervisor_spawn
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.tools import delegate, tasks


@pytest.fixture
def run(isolated_env, tmp_path, monkeypatch, request):
    policy = tmp_path / "custom-policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    storage.init_db()
    from_agent, to_agent = getattr(request, "param", ("Desktop_Agent5", "Desktop_Agent1"))
    packed = delegate.delegate(
        task_spec="Inspect", from_agent=from_agent, to_agent=to_agent, callback="none"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-09-29T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RoutedLaunchBoundary(spawner=StubProcessSpawner())
    )
    assert out["launched"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=? WHERE run_id=?",
            ("pid:supervisor-review", out["run_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return out["run_id"], policy


@pytest.mark.parametrize("binding", ["Desktop_Agent1", "Desktop_Agent5", "invalid"])
def test_wait_rechecks_current_policy_and_cancels_exact_run_without_ticks(
    run, binding, monkeypatch
):
    run_id, policy = run
    terminate = Mock(return_value="kill_unconfirmed")
    monkeypatch.setattr(supervise, "terminate_ours", terminate)
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        assert timeout is not None and 0 < timeout <= 1.0
        if len(waits) == 1:
            policy.write_text(
                "trust_presets: {default: invalid}"
                if binding == "invalid"
                else json.dumps({"trust_presets": {"agents": {binding: "inspection"}}})
            )
            raise subprocess.TimeoutExpired("test-child", timeout)
        return 0

    proc = Mock(pid=123, wait=wait)
    supervise._wait_and_report_exit(proc, run_id)
    terminate.assert_called_once_with("pid:supervisor-review")
    after = coordinator.get_run(run_id)
    assert after["state"] == "cancelled" and after["termination_outcome"] == "unknown"
    assert after["lease_expires_at"] is not None


def test_authority_paths_come_from_parent_config_not_child_environment(tmp_path, monkeypatch):
    custom = tmp_path / "operator-policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(custom))
    captured = {}

    def popen(*args, **kwargs):
        captured["environment"] = kwargs["env"]
        proc = Mock()

        def write(raw):
            captured["payload"] = json.loads(raw)
            from pathlib import Path

            Path(kwargs["env"]["UA_SUP_PID_PATH"]).write_text("123")

        proc.stdin.write.side_effect = write
        return proc

    monkeypatch.setattr(supervise.subprocess, "Popen", popen)
    child_env = {"PATH": os.environ.get("PATH", ""), "UA_HOME": str(tmp_path / "child-home")}
    supervise.spawn_via_detached_supervisor(
        ["child"], cwd=".", env=child_env, log_path=tmp_path / "child.log", run_id="review"
    )
    authority = captured["payload"]["authority"]
    assert authority["data_home"] == str(config.data_home().resolve())
    assert authority["policy_file"] == str(custom.resolve())
    assert "UA_POLICY_FILE" not in captured["environment"]
    assert captured["environment"]["UA_HOME"] == child_env["UA_HOME"]


def test_supervisor_refuses_missing_authority_before_native_spawn(tmp_path, monkeypatch):
    payload = {
        "argv": ["child"],
        "cwd": ".",
        "log_path": str(tmp_path / "child.log"),
        "run_id": "missing-authority",
    }
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(tmp_path / "pid"))
    popen = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr(supervisor_spawn.subprocess, "Popen", popen)
    monkeypatch.setattr(supervise, "_wait_and_report_exit", lambda *_args: None)
    with pytest.raises((ValueError, RuntimeError)):
        supervisor_spawn.main()
    popen.assert_not_called()


def test_startup_binds_custom_parent_authority_without_widening_child_env(
    run, tmp_path, monkeypatch
):
    run_id, policy = run
    authority = {
        "data_home": str(config.data_home().resolve()),
        "policy_file": str(policy.resolve()),
        "policy_file_required": False,
        "agent_labels": ":".join(sorted(config.agent_labels())),
        "launch_caller": None,
    }
    payload = {
        "argv": ["child"],
        "cwd": ".",
        "log_path": str(tmp_path / "child.log"),
        "run_id": run_id,
        "authority": authority,
    }
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(tmp_path / "pid"))
    monkeypatch.setenv("UA_HOME", str(tmp_path / "child-home"))
    monkeypatch.delenv("UA_POLICY_FILE")
    popen = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr(supervisor_spawn.subprocess, "Popen", popen)
    monkeypatch.setattr(supervise, "pid_token", lambda _pid: "pid:startup-test")
    monkeypatch.setattr(supervise, "_wait_and_report_exit", lambda *_args, **_kwargs: None)
    supervisor_spawn.main()
    assert str(config.data_home()) == authority["data_home"]
    assert str(config.policy_file_path()) == authority["policy_file"]
    child = popen.call_args.kwargs["env"]
    assert child["UA_HOME"] == str(tmp_path / "child-home")
    assert "UA_POLICY_FILE" not in child and "UA_SUP_PID_PATH" not in child


def test_authority_loss_stops_only_owned_child_and_does_not_publish_success(run, monkeypatch):
    run_id, _ = run
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            monkeypatch.setattr(
                storage,
                "require_live_task_store",
                Mock(side_effect=storage.LiveStoreError("authority lost")),
            )
            raise subprocess.TimeoutExpired("child", timeout)
        return 0

    monkeypatch.setattr(supervise, "pid_token", lambda _pid: "pid:owned-token")
    cancel = Mock(side_effect=storage.LiveStoreError("authority lost"))
    monkeypatch.setattr(coordinator, "cancel_run", cancel)
    terminate = Mock(return_value="kill_unconfirmed")
    monkeypatch.setattr(supervise, "terminate_ours", terminate)
    report = Mock()
    monkeypatch.setattr(supervise, "_report_exit_resilient", report)
    supervise._wait_and_report_exit(Mock(pid=123, wait=wait), run_id)
    cancel.assert_called_once_with(
        run_id, actor="dispatch-supervisor", reason="trust_preset_native_denied"
    )
    terminate.assert_called_once_with("pid:owned-token")
    report.assert_not_called()


def test_supervisor_cancellation_race_stops_owned_child_and_corrects_false_cessation(
    run, tmp_path, monkeypatch
):
    run_id, policy = run
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET evidence_ref=NULL WHERE run_id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    authority = {
        "data_home": str(config.data_home().resolve()),
        "policy_file": str(policy.resolve()),
        "policy_file_required": False,
        "agent_labels": ":".join(sorted(config.agent_labels())),
        "launch_caller": None,
    }
    payload = {
        "argv": ["child"],
        "cwd": ".",
        "log_path": str(tmp_path / "child.log"),
        "run_id": run_id,
        "authority": authority,
    }
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(tmp_path / "pid"))
    monkeypatch.setattr(supervisor_spawn.subprocess, "Popen", Mock(return_value=Mock(pid=123)))
    monkeypatch.setattr(supervise, "pid_token", lambda _pid: "pid:owned-token")
    terminate = Mock(side_effect=["already_gone", "kill_unconfirmed"])
    monkeypatch.setattr(supervise, "terminate_ours", terminate)
    monkeypatch.setattr(supervise, "_wait_and_report_exit", lambda *_args, **_kwargs: None)
    from unified_agent.dispatch import launch

    def cancellation_wins(*_args):
        assert coordinator.cancel_run(run_id, actor="review")["cessation"] == "confirmed"
        return False

    monkeypatch.setattr(launch, "_persist_spawn_pid", cancellation_wins)
    supervisor_spawn.main()
    assert terminate.call_args_list[1].args == ("pid:owned-token",)
    after = coordinator.get_run(run_id)
    assert after["state"] == "cancelled" and after["termination_outcome"] == "unknown"
    assert after["evidence_ref"] == "pid:owned-token"


def test_parent_policy_symlink_remains_current_authority_entrypoint(tmp_path, monkeypatch):
    first = tmp_path / "first.yaml"
    first.write_text("version: 1")
    configured = tmp_path / "current-policy.yaml"
    configured.symlink_to(first)
    monkeypatch.setenv("UA_POLICY_FILE", str(configured))
    captured = {}

    def popen(*_args, **kwargs):
        proc = Mock()

        def write(raw):
            captured.update(json.loads(raw))
            from pathlib import Path

            Path(kwargs["env"]["UA_SUP_PID_PATH"]).write_text("123")

        proc.stdin.write.side_effect = write
        return proc

    monkeypatch.setattr(supervise.subprocess, "Popen", popen)
    supervise.spawn_via_detached_supervisor(
        ["child"], cwd=".", env={}, log_path=tmp_path / "child.log", run_id="review"
    )
    assert captured["authority"]["policy_file"] == str(configured.absolute())


def test_real_detached_child_stops_after_parent_policy_binding_without_ticks(run, tmp_path):
    import sys
    import time

    run_id, policy = run
    child_env = {"PATH": os.environ.get("PATH", ""), "UA_HOME": str(tmp_path / "child-home")}
    pid = supervise.spawn_via_detached_supervisor(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        env=child_env,
        log_path=tmp_path / "real-child.log",
        run_id=run_id,
    )
    own_token = supervise.pid_token(pid)
    try:
        assert supervise.pid_alive(pid)
        policy.write_text(
            json.dumps({"trust_presets": {"agents": {"Desktop_Agent1": "inspection"}}})
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            after = coordinator.get_run(run_id)
            if after["state"] == "cancelled":
                break
            time.sleep(0.01)
        assert after["state"] == "cancelled"
        assert after["failure_reason"] == "trust_preset_native_denied"
        assert after["termination_outcome"] == "confirmed"
        assert not supervise.pid_alive(pid)
    finally:
        if supervise.pid_alive(pid):
            supervise.terminate_ours(own_token)


def test_supervisor_metadata_captures_only_verified_parent_caller(tmp_path, monkeypatch):
    captured = {}

    def popen(*_args, **kwargs):
        proc = Mock()

        def write(raw):
            captured.update(json.loads(raw))
            from pathlib import Path

            Path(kwargs["env"]["UA_SUP_PID_PATH"]).write_text("123")

        proc.stdin.write.side_effect = write
        return proc

    monkeypatch.setattr(supervise.subprocess, "Popen", popen)
    caller = config.agent_identity_var.set("Desktop_Agent2")
    try:
        supervise.spawn_via_detached_supervisor(
            ["child"],
            cwd=".",
            env={"UA_AGENT_LABEL": "Desktop_agent3"},
            log_path=tmp_path / "child.log",
            run_id="review",
        )
    finally:
        config.agent_identity_var.reset(caller)
    assert captured["authority"]["launch_caller"] == "Desktop_Agent2"


@pytest.mark.parametrize("run", [("Desktop_Agent1", "Desktop_agent3")], indirect=True)
def test_real_distinct_verified_caller_revocation_cancels_exact_run(run, tmp_path):
    import sys
    import time

    run_id, policy = run
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "UA_HOME": str(tmp_path / "child-home"),
        "UA_AGENT_LABEL": "Desktop_agent3",
    }
    caller = config.agent_identity_var.set("Desktop_Agent2")
    try:
        pid = supervise.spawn_via_detached_supervisor(
            [sys.executable, "-c", "import time; time.sleep(40)"],
            cwd=str(tmp_path),
            env=child_env,
            log_path=tmp_path / "caller-child.log",
            run_id=run_id,
        )
    finally:
        config.agent_identity_var.reset(caller)
    own_token = supervise.pid_token(pid)
    try:
        policy.write_text(
            json.dumps({"trust_presets": {"agents": {"Desktop_Agent2": "inspection"}}})
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            after = coordinator.get_run(run_id)
            if after["state"] == "cancelled":
                break
            time.sleep(0.01)
        assert after["agent_label"] == "Desktop_agent3"
        assert after["state"] == "cancelled"
        assert after["failure_reason"] == "trust_preset_native_denied"
        assert after["termination_outcome"] == "confirmed"
        assert not supervise.pid_alive(pid)
    finally:
        if supervise.pid_alive(pid):
            supervise.terminate_ours(own_token)


@pytest.mark.parametrize("caller", [1, True, [], "", " Desktop_Agent2", "Desktop/Codex"])
def test_malformed_supervisor_caller_is_rejected_before_child_execution(
    tmp_path, monkeypatch, caller
):
    payload = {
        "argv": ["child"],
        "cwd": ".",
        "log_path": str(tmp_path / "child.log"),
        "run_id": "invalid-caller",
        "authority": {
            "data_home": str(tmp_path),
            "policy_file": str(tmp_path / "policy.yaml"),
            "policy_file_required": False,
            "agent_labels": "Desktop_Agent2",
            "launch_caller": caller,
        },
    }
    monkeypatch.setattr(supervisor_spawn.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("UA_SUP_PID_PATH", str(tmp_path / "pid"))
    popen = Mock()
    monkeypatch.setattr(supervisor_spawn.subprocess, "Popen", popen)
    with pytest.raises(ValueError, match="launch caller"):
        supervisor_spawn.main()
    popen.assert_not_called()
