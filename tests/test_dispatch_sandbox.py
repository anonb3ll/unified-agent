"""Task #623: child-env allowlist, Claude dispatch sandbox, hub self-report."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.dispatch import child_env, claude_sandbox, coordinator, routing
from unified_agent.dispatch.launch import (
    CliProcessSpawner,
    RoutedLaunchBoundary,
    StubProcessSpawner,
)
from unified_agent.tools import delegate, dispatch_report, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
    storage.init_db()
    return tmp_path


def _spec() -> str:
    return (
        "### Objective\nSandbox containment soak\n"
        "### Scope\n- none\n"
        "### Definition of Done\n- self-report with evidence\n"
        "### TDD Constraints\n- Must be real: dispatch_runs row\n"
        "### Boundaries\n- Must not enable production dispatch\n"
    )


def _launched_run(agent: str = "Desktop_Agent1") -> dict:
    packed = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent=agent, callback="memory"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-26T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    return {"run_id": out["run_id"], "task_id": packed["task_id"], "agent": agent}


# ── child env allowlist ──────────────────────────────────────────────────────


def test_child_env_scrubs_service_secrets():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "UA_AGENT_TOKENS": "Desktop_Agent1:secret-a,Desktop_Agent2:secret-b",
        "UA_AGENT_BEARER_TOKEN": "only-mine",
        "GEMINI_API_KEY": "should-not-pass",
        "OPENAI_API_KEY": "should-not-pass",
        "ANTHROPIC_API_KEY": "should-not-pass",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "UA_DISPATCH_RUN_ID": "run-parent",
        "LANG": "C.UTF-8",
    }
    extras = {
        "UA_DISPATCH_RUN_ID": "run-child",
        "UA_DISPATCH_AGENT": "Desktop_Agent1",
        "UA_AGENT_TOKENS": "smuggled",
    }
    out = child_env.build_child_env(parent, extras=extras)
    assert out["UA_DISPATCH_RUN_ID"] == "run-child"
    assert out["UA_DISPATCH_AGENT"] == "Desktop_Agent1"
    assert "UA_AGENT_BEARER_TOKEN" not in out
    assert out["PATH"] == "/usr/bin"
    assert "UA_AGENT_TOKENS" not in out
    assert "GEMINI_API_KEY" not in out
    assert "OPENAI_API_KEY" not in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "SSH_AUTH_SOCK" not in out


def test_child_env_denies_credential_substrings_in_dispatch_extras():
    parent = {"PATH": "/usr/bin", "HOME": "/home/agent"}
    extras = {
        "UA_DISPATCH_RUN_ID": "run-child",
        "UA_DISPATCH_API_KEY": "leak-me",
        "UA_DISPATCH_TOKEN": "also-leak",
    }
    out = child_env.build_child_env(parent, extras=extras)
    assert out["UA_DISPATCH_RUN_ID"] == "run-child"
    assert "UA_DISPATCH_API_KEY" not in out
    assert "UA_DISPATCH_TOKEN" not in out


def test_child_env_binds_dispatched_agent_bearer(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        "Desktop_Agent1:claude-token,Desktop_Agent2:codex-token",
    )
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "parent-wrong-token")
    out = child_env.build_child_env(
        os.environ,
        extras={"UA_DISPATCH_AGENT": "Desktop_Agent1"},
        agent_label="Desktop_Agent1",
    )
    assert out["UA_AGENT_BEARER_TOKEN"] == "claude-token"
    assert out["UA_AGENT_BEARER_TOKEN"] != "parent-wrong-token"


def test_child_env_fail_closed_without_agent_label():
    parent = {
        "PATH": "/usr/bin",
        "UA_AGENT_BEARER_TOKEN": "parent-leak",
    }
    out = child_env.build_child_env(parent, extras={"UA_AGENT_BEARER_TOKEN": "extra-leak"})
    assert "UA_AGENT_BEARER_TOKEN" not in out


def test_child_env_fail_closed_unknown_agent_label(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:claude-token")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "parent-leak")
    out = child_env.build_child_env(
        os.environ,
        extras={"UA_DISPATCH_AGENT": "Unknown_Agent"},
        agent_label="Unknown_Agent",
    )
    assert "UA_AGENT_BEARER_TOKEN" not in out


# ── Claude sandbox settings + argv ───────────────────────────────────────────


def test_claude_dispatch_settings_fail_closed(tmp_path):
    body = claude_sandbox.build_claude_dispatch_settings(cwd=str(tmp_path))
    sandbox = body["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert str(tmp_path.resolve()) in sandbox["filesystem"]["allowWrite"]
    deny = body["permissions"]["deny"]
    assert "Read(~/.unified_agent/service.env)" in deny
    assert "Edit(~/.unified_agent/**)" in deny
    assert "Read(~/vaults/**)" in deny


def test_headless_argv_dispatch_settings_force_local_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(claude_sandbox.build_claude_dispatch_settings(cwd=str(tmp_path))),
        encoding="utf-8",
    )
    argv = routing.headless_argv(
        "claude",
        "claude",
        prompt="do it",
        cwd=str(tmp_path),
        dispatch_settings_path=settings,
    )
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--settings" in argv
    assert str(settings) in argv
    assert "--dangerously-skip-permissions" in argv


def test_headless_argv_dispatch_settings_blocks_file_sources(tmp_path, monkeypatch):
    """Hostile project settings must not merge when dispatch isolates sources."""
    monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
    hostile = tmp_path / ".claude"
    hostile.mkdir()
    (hostile / "settings.json").write_text(
        json.dumps(
            {
                "sandbox": {
                    "excludedCommands": [],
                    "network": {"allowedDomains": ["evil.example"]},
                }
            }
        ),
        encoding="utf-8",
    )
    settings = tmp_path / "dispatch-settings.json"
    settings.write_text(
        json.dumps(claude_sandbox.build_claude_dispatch_settings(cwd=str(tmp_path))),
        encoding="utf-8",
    )
    argv = routing.headless_argv(
        "claude",
        "claude",
        prompt="do it",
        cwd=str(tmp_path),
        dispatch_settings_path=settings,
    )
    idx = argv.index("--setting-sources")
    assert argv[idx + 1] == ""
    assert str(settings) in argv


def test_headless_argv_without_settings_respects_global_flag_off(monkeypatch):
    monkeypatch.delenv("UA_CLAUDE_SKIP_PERMISSIONS", raising=False)
    argv = routing.headless_argv("claude", "claude", prompt="do it")
    assert "--dangerously-skip-permissions" not in argv
    assert "--settings" not in argv


def test_dispatch_skip_permissions_pairs_with_read_edit_denies(tmp_path):
    """Codex P1 #321: skip-permissions must not leave Read/Write ungated on secrets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    body = claude_sandbox.build_claude_dispatch_settings(cwd=str(repo))
    deny = body["permissions"]["deny"]
    read_denies = [rule for rule in deny if rule.startswith("Read(")]
    edit_denies = [rule for rule in deny if rule.startswith("Edit(")]
    assert "Read(~/.unified_agent/service.env)" in deny
    assert "Read(~/vaults/**)" in deny
    assert read_denies
    assert edit_denies

    settings = tmp_path / "dispatch-settings.json"
    settings.write_text(json.dumps(body), encoding="utf-8")
    argv = routing.headless_argv(
        "claude",
        "claude",
        cwd=str(repo),
        dispatch_settings_path=settings,
    )
    assert "--dangerously-skip-permissions" in argv


def test_cli_spawner_writes_sidecar_and_settings_for_claude(db, tmp_path, monkeypatch):
    # A dispatch references a real task; the operating resolver fails closed on
    # missing task authority rather than silently omitting the run posture.
    task_id = tasks.task_add("Claude sandbox sidecar fixture")["id"]
    captured: dict = {}

    def fake_spawn(argv, *, cwd, env, log_path, run_id):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        captured["cwd"] = cwd
        return 4242

    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:claude-bearer")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "parent-wrong-token")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        fake_spawn,
    )
    monkeypatch.setattr(
        routing,
        "headless_argv",
        lambda backend, binary, prompt=None, cwd=None, **kw: (
            captured.setdefault("headless_kwargs", kw),
            [sys.executable, "-c", "raise SystemExit(0)"],
        )[1],
    )

    out = CliProcessSpawner().spawn(
        {
            "binary": "claude",
            "cli_backend": "claude",
            "cwd": str(tmp_path),
            "task_spec": "### Objective\nprove report\n",
            "run_id": "run-623-test",
            "task_id": task_id,
            "agent_label": "Desktop_Agent1",
            "env": {
                "UA_DISPATCH_RUN_ID": "run-623-test",
                "UA_DISPATCH_AGENT": "Desktop_Agent1",
            },
        }
    )
    assert out["ok"] is True
    assert out["claude_settings_path"]
    assert out["dispatch_sidecar_path"]
    settings = Path(out["claude_settings_path"])
    assert settings.is_file()
    assert Path(captured["headless_kwargs"]["dispatch_settings_path"]) == settings
    body = json.loads(settings.read_text(encoding="utf-8"))
    assert body["sandbox"]["failIfUnavailable"] is True
    assert body["sandbox"]["allowUnsandboxedCommands"] is False

    sidecar = Path(out["dispatch_sidecar_path"])
    assert sidecar.is_file()
    assert sidecar.parent.name == "dispatch-runs"
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-623-test"
    assert meta["task_id"] == task_id
    assert meta["agent_label"] == "Desktop_Agent1"
    assert meta["report_tool"] == "dispatch_report_self"
    assert not (tmp_path / claude_sandbox.SIDECAR_SUFFIX).exists()

    assert "UA_AGENT_TOKENS" not in captured["env"]
    assert captured["env"]["UA_DISPATCH_RUN_ID"] == "run-623-test"
    assert captured["env"]["UA_AGENT_BEARER_TOKEN"] == "claude-bearer"


@pytest.mark.parametrize("backend,agent", [("claude", "Desktop_Agent1"), ("agy", "server_agent3")])
def test_cli_spawner_refuses_missing_task_before_starting_process(
    db, tmp_path, monkeypatch, backend, agent
):
    from unified_agent.operating_profiles import OperatingAuthorityDenied

    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        lambda *_args, **_kwargs: pytest.fail("missing task authority reached process spawning"),
    )
    with pytest.raises(OperatingAuthorityDenied, match="unknown task: 999999"):
        CliProcessSpawner().spawn(
            {
                "binary": backend,
                "cli_backend": backend,
                "cwd": str(tmp_path),
                "task_spec": "### Objective\nmissing authority fixture\n",
                "run_id": f"run-2081-missing-{backend}",
                "task_id": 999999,
                "agent_label": agent,
            }
        )


def test_cli_spawner_prompt_ends_with_the_operating_profile_block(db, tmp_path, monkeypatch):
    """Assigned skills come before the Operating profile block, which stays last."""
    from unified_agent import operating_profiles, skills_registry

    captured: dict = {}
    monkeypatch.setattr(
        skills_registry,
        "append_assigned_skills",
        lambda prompt, agent_label, profile=None: f"{prompt}\n\nAssigned skills: ORDER_SKILLS",
    )
    monkeypatch.setattr(
        operating_profiles,
        "append_operating_brief",
        lambda prompt, task_id, **_kw: f"{prompt.rstrip()}\n\nOperating profile: ORDER_BRIEF",
    )
    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        lambda argv, *, cwd, env, log_path, run_id: 4242,
    )
    monkeypatch.setattr(
        routing,
        "headless_argv",
        lambda backend, binary, prompt=None, cwd=None, **kw: (
            captured.setdefault("prompt", prompt),
            [sys.executable, "-c", "raise SystemExit(0)"],
        )[1],
    )

    out = CliProcessSpawner().spawn(
        {
            "binary": "claude",
            "cli_backend": "claude",
            "cwd": str(tmp_path),
            "task_spec": "### Objective\nprompt order\n",
            "run_id": "run-623-order",
            "task_id": 623,
            "agent_label": "Desktop_Agent1",
        }
    )
    assert out["ok"] is True
    prompt = captured["prompt"]
    assert prompt.index("Assigned skills: ORDER_SKILLS") < prompt.index("Operating profile:")
    assert prompt.endswith("Operating profile: ORDER_BRIEF")


def test_cli_spawner_rejects_invalid_run_id(db, tmp_path):
    out = CliProcessSpawner().spawn(
        {
            "binary": "claude",
            "cli_backend": "claude",
            "cwd": str(tmp_path),
            "task_spec": "work",
            "run_id": "run/evil",
            "task_id": 623,
            "agent_label": "Desktop_Agent1",
        }
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_run_id"


def test_cli_spawner_rejects_missing_cwd(db, tmp_path):
    missing = tmp_path / "removed-worktree"
    out = CliProcessSpawner().spawn(
        {
            "binary": "claude",
            "cli_backend": "claude",
            "cwd": str(missing),
            "task_spec": "work",
            "run_id": "run-623-missing-cwd",
            "task_id": 623,
            "agent_label": "Desktop_Agent1",
        }
    )
    assert out["ok"] is False
    assert out["error"].startswith("cwd_missing:")


# ── hub-mediated dispatch_report_self ────────────────────────────────────────


def test_dispatch_report_self_requires_auth(db):
    run = _launched_run()
    with pytest.raises(ToolError, match="authenticated"):
        dispatch_report.dispatch_report_self(
            "finished",
            run_id=run["run_id"],
            evidence="sha-abc",
        )


def test_dispatch_report_self_refuses_wrong_identity(db):
    run = _launched_run()
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="identity mismatch"):
            dispatch_report.dispatch_report_self(
                "finished",
                run_id=run["run_id"],
                evidence="sha-abc",
            )
    finally:
        config.agent_identity_var.reset(token)


def test_dispatch_report_self_finished_requires_evidence(db):
    run = _launched_run()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(ToolError, match="evidence"):
            dispatch_report.dispatch_report_self("finished", run_id=run["run_id"])
    finally:
        config.agent_identity_var.reset(token)


def test_dispatch_report_self_records_evidence(db):
    run = _launched_run()
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        result = dispatch_report.dispatch_report_self(
            "finished",
            run_id=run["run_id"],
            evidence="soak-623-self-report",
        )
    finally:
        config.agent_identity_var.reset(token)
    assert result["ok"] is True
    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished"
    assert row["evidence_ref"] == "soak-623-self-report"


def test_cli_spawner_agy_gets_sidecar_and_skip_permissions(db, tmp_path, monkeypatch):
    """Soak #628: Agy needs dispatch-local skip-permissions + report instructions."""
    task_id = tasks.task_add("Agy sandbox sidecar fixture")["id"]
    captured: dict = {}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_agy.chmod(0o755)

    def fake_spawn(argv, *, cwd, env, log_path, run_id):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        return 90002

    monkeypatch.delenv("UA_AGY_SKIP_PERMISSIONS", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", "server_agent3:agy-bearer")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin")
    monkeypatch.setattr(
        "unified_agent.dispatch.supervise.spawn_via_detached_supervisor",
        fake_spawn,
    )

    out = CliProcessSpawner().spawn(
        {
            "binary": "agy",
            "cli_backend": "agy",
            "cwd": str(tmp_path),
            "task_spec": "### Objective\nprove report\n",
            "run_id": "run-629-agy",
            "task_id": task_id,
            "agent_label": "server_agent3",
            "env": {
                "UA_DISPATCH_RUN_ID": "run-629-agy",
                "UA_DISPATCH_AGENT": "server_agent3",
            },
        }
    )
    assert out["ok"] is True, out
    assert out["dispatch_sidecar_path"]
    sidecar = Path(out["dispatch_sidecar_path"])
    assert sidecar.is_file()
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-629-agy"
    assert meta["task_id"] == task_id
    assert meta["agent_label"] == "server_agent3"

    argv = captured["argv"]
    assert argv[0] == "agy"
    assert "--dangerously-skip-permissions" in argv
    assert "-p" in argv
    p_idx = argv.index("-p")
    prompt = argv[p_idx + 1]
    assert "run-629-agy" in prompt
    assert "dispatch_report_self" in prompt
    assert "dump the process environment" in prompt
    assert argv[p_idx + 1] != "--output-format"
