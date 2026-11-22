"""Devin executor backend contract tests (task #1159)."""

import subprocess

import pytest

from unified_agent import config
from unified_agent.executor import backends


def test_devin_argv_uses_narrow_edit_permission_mode(monkeypatch):
    monkeypatch.delenv("UA_DEVIN_PERMISSION_MODE", raising=False)

    argv = backends.DevinBackend(devin_bin="devin").build_command("fix it")

    assert argv == ["devin", "--permission-mode", "accept-edits", "--print=fix it"]


def test_devin_argv_resolves_model_override_and_env(monkeypatch):
    monkeypatch.setenv("UA_DEVIN_MODEL", "opus")
    backend = backends.DevinBackend(devin_bin="devin")

    assert backend.build_command("fix it") == [
        "devin",
        "--permission-mode",
        "accept-edits",
        "--model",
        "opus",
        "--print=fix it",
    ]
    assert backend.build_command("fix it", model="codex") == [
        "devin",
        "--permission-mode",
        "accept-edits",
        "--model",
        "codex",
        "--print=fix it",
    ]


def test_devin_dangerous_mode_is_refused(monkeypatch):
    from unified_agent.dispatch.harness_sandbox import HarnessSandboxUnavailable

    monkeypatch.setenv("UA_DEVIN_PERMISSION_MODE", "dangerous")

    with pytest.raises(HarnessSandboxUnavailable, match="harness_sandbox_unavailable:devin"):
        backends.DevinBackend(devin_bin="devin").build_command("run it")


def test_devin_label_selects_native_backend_and_model_family(monkeypatch):
    monkeypatch.delenv("UA_EXECUTOR_BACKEND", raising=False)
    monkeypatch.setenv("UA_MODEL_DEVIN", "claude-sonnet-4")

    assert backends.backend_name_for_label("Desktop_Agent7") == "devin"
    assert backends.get_backend(agent_label="Desktop_Agent7").name == "devin"
    assert config.agent_model_env_key("Desktop_Agent7") == "UA_MODEL_DEVIN"
    assert config.resolve_run_model("Desktop_Agent7") == "claude-sonnet-4"


def test_devin_run_uses_the_task_worktree(monkeypatch, tmp_path):
    worktree = tmp_path / "task-1159"
    worktree.mkdir()
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent7:dummy-devin-token")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "ambient-wrong-token")
    monkeypatch.setenv("UA_EXECUTOR_TOKEN", "dummy-executor-token")
    captured = {}

    def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["stdin"] = stdin
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, "done\n", "")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_run)
    result = backends.DevinBackend(devin_bin="devin").run(
        prompt="fix it",
        cwd="task-1159",
        session_dir=".worktrees/sessions",
        session_id="task-1159-session",
        agent_label="Desktop_Agent7",
        timeout_s=30,
    )

    assert captured["cwd"] == str(worktree)
    assert captured["argv"][-1] == "--print=fix it"
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["env"]["UA_AGENT_BEARER_TOKEN"] == "dummy-devin-token"
    assert captured["env"]["UA_AGENT_BEARER_TOKEN"] != "ambient-wrong-token"
    assert "UA_AGENT_TOKENS" not in captured["env"]
    assert "UA_EXECUTOR_TOKEN" not in captured["env"]
    assert result["status"] == "success"
    assert result["text"] == "done"
