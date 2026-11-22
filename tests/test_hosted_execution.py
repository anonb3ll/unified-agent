"""Hosted hub refuses local execution (task #2109).

``UA_HOSTED=1`` or a non-empty ``RAILWAY_ENVIRONMENT_ID`` is the flag. With
neither set, the same calls keep their local behavior.
"""

from __future__ import annotations

import pytest

from unified_agent.hosted_execution import (
    CODE,
    HostedExecutionError,
    is_hosted,
    refusal_result,
)
from unified_agent.tools.pi_runner import pi_run_task
from unified_agent.tools.shell import run_cmd, run_cmd_check


def _hosted(monkeypatch, *, railway: str | None = None, flag: str | None = "1") -> None:
    if flag is None:
        monkeypatch.delenv("UA_HOSTED", raising=False)
    else:
        monkeypatch.setenv("UA_HOSTED", flag)
    if railway is None:
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    else:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", railway)


def test_local_process_is_not_hosted(monkeypatch):
    _hosted(monkeypatch, flag=None)
    assert is_hosted() is False
    assert refusal_result("run_cmd") is None


def test_railway_environment_id_is_hosted_without_the_flag(monkeypatch):
    _hosted(monkeypatch, flag=None, railway="env-123")
    assert is_hosted() is True


def test_run_cmd_refuses_when_hosted_and_runs_locally(monkeypatch):
    import unified_agent.tools.shell as shell

    popped = []
    original = shell.subprocess.run

    def _boom(*_args, **_kwargs):
        popped.append(True)
        raise AssertionError("subprocess should not start")

    monkeypatch.setattr(shell.subprocess, "run", _boom)

    _hosted(monkeypatch)
    with pytest.raises(HostedExecutionError, match="node worker") as exc:
        run_cmd("echo hi")
    assert exc.value.code == CODE
    assert exc.value.data["surface"] == "run_cmd"
    assert popped == []

    checked = run_cmd_check("echo hi")
    assert checked["ok"] is False
    assert checked["verdict"] == "block"
    assert checked["findings"][0]["code"] == CODE

    _hosted(monkeypatch, flag=None)
    monkeypatch.setattr(shell.subprocess, "run", original)
    local = run_cmd("echo hi")
    assert local["exit_code"] == 0
    assert "hi" in local["stdout"]
    assert run_cmd_check("echo hi")["ok"] is True


def test_pi_run_task_refuses_before_spawn(monkeypatch):
    import unified_agent.tools.pi_runner as pi_runner

    def _boom(*_args, **_kwargs):
        raise AssertionError("pi should not spawn")

    monkeypatch.setattr(pi_runner.subprocess, "Popen", _boom)
    _hosted(monkeypatch, flag=None, railway="env-123")
    with pytest.raises(HostedExecutionError, match="pi_run_task"):
        pi_run_task("do the thing")


def test_executor_popen_refuses_when_hosted(monkeypatch):
    import unified_agent.executor.backends as backends

    def _boom(*_args, **_kwargs):
        raise AssertionError("executor should not spawn")

    monkeypatch.setattr(backends.subprocess, "Popen", _boom)
    _hosted(monkeypatch)
    with pytest.raises(HostedExecutionError, match="executor"):
        backends.run_popen_with_kill_check(["echo", "hi"], cwd=".", env={}, timeout_s=5)


def test_agy_interactive_refuses_when_hosted(monkeypatch):
    import unified_agent.executor.agy_interactive as agy

    def _boom(*_args, **_kwargs):
        raise AssertionError("agy should not spawn")

    monkeypatch.setattr(agy.subprocess, "run", _boom)
    _hosted(monkeypatch)
    with pytest.raises(HostedExecutionError, match="agy-interactive"):
        agy.run_interactive_session(agy_bin="agy", prompt="hi", cwd=".", env={}, timeout_s=5)


def test_dispatch_spawn_returns_the_typed_refusal(monkeypatch):
    import unified_agent.dispatch.supervise as supervise
    from unified_agent.dispatch.launch import CliProcessSpawner

    calls = []
    real = supervise.spawn_via_detached_supervisor

    def _record(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(supervise, "spawn_via_detached_supervisor", _record)
    _hosted(monkeypatch)
    result = CliProcessSpawner().spawn({"agent_label": "Desktop_Agent1", "run_id": "r"})
    assert result["ok"] is False
    assert result["spawned"] is False
    assert result["code"] == CODE
    assert "ua-node" in result["detail"]
    assert calls == []

    with pytest.raises(HostedExecutionError, match="dispatch"):
        real(["echo", "hi"], cwd=".", env={}, log_path="x", run_id="r")


def test_claude_cli_leg_does_not_spawn_when_hosted(monkeypatch):
    import unified_agent.llm_client as llm

    def _boom(*_args, **_kwargs):
        raise AssertionError("claude should not spawn")

    monkeypatch.setattr(llm.subprocess, "Popen", _boom)
    _hosted(monkeypatch)
    assert llm._call_claude_cli("hello", None) is None
    failure = llm._LEG_FAILURE.get()
    assert failure["error"] == "HostedExecutionError"
    assert failure["reason"] == "use a node worker"

    with pytest.raises(HostedExecutionError, match="claude -p"):
        llm._run_cli(["claude", "-p"], "hi", 1, cwd=llm.Path("."), tmpdir=llm.Path("."))
