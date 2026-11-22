"""Executor child environments are allowlisted, not inherited (task #1910).

Audit S4: every native executor backend used to build its child environment as
``dict(os.environ)`` minus three bearer variables, so provider API keys, the
managed-secret Fernet key (``UA_MANAGED_SECRET_KEYS``) and every other hub
secret reached Claude Code, Codex, agy, Cursor, Copilot, Hermes and Devin
children. These tests drive each backend's real ``run()`` with the subprocess
boundary stubbed and assert what the child would have received.

All values are fakes.
"""

from __future__ import annotations

import subprocess

import pytest

from unified_agent import config
from unified_agent.executor import backends
from unified_agent.executor import child_env as executor_child_env
from unified_agent.tools import pi_runner

SECRET_PARENT_VARS = {
    "UA_MANAGED_SECRET_KEYS": "fake-fernet-key",
    "UA_AGENT_TOKENS": "Desktop_Agent1:fake-registry-token",
    "OPENAI_API_KEY": "fake-openai",
    "ANTHROPIC_API_KEY": "fake-anthropic",
    "GEMINI_API_KEY": "fake-gemini",
    "SLACK_BOT_TOKEN": "fake-slack",
    "GITHUB_TOKEN": "fake-github",
    "UA_EXECUTOR_TOKEN": "fake-executor-token",
    "SOME_SERVICE_SECRET": "fake-secret",
    "STRIPE_KEY": "fake-stripe",
}

REQUIRED_PARENT_VARS = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/fake-user",
    "LANG": "en_US.UTF-8",
    "TERM": "xterm-256color",
    "XDG_CONFIG_HOME": "/home/fake-user/.config",
}

NATIVE_BACKENDS = [
    "codex",
    "agy",
    "agy-interactive",
    "claude",
    "cursor",
    "copilot",
    "hermes",
    "devin",
]


@pytest.fixture
def hostile_parent(monkeypatch, tmp_path):
    for key, value in {**SECRET_PARENT_VARS, **REQUIRED_PARENT_VARS}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.delenv("UA_EXECUTOR_ENV_PASSTHROUGH", raising=False)
    monkeypatch.delenv("UA_EXECUTOR_AGENT", raising=False)
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(backends.trust_presets, "require_native_allowed", lambda _label: None)
    return tmp_path


def _capture_env(backend_name: str, monkeypatch) -> dict[str, str]:
    """Run ``backend_name`` once with the spawn stubbed; return the child env."""
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, *, cwd, env, timeout_s, stdin=None, kill_check=None, **_kw):
        captured["env"] = dict(env)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", fake_popen)

    if backend_name == "agy-interactive":
        from unified_agent.executor import agy_interactive

        def fake_session(**kwargs):
            captured["env"] = dict(kwargs["env"])
            return {
                "status": "success",
                "text": "",
                "tools_used": [],
                "files_modified": [],
                "events_count": 0,
                "exit_code": 0,
                "error": None,
            }

        monkeypatch.setattr(agy_interactive, "run_interactive_session", fake_session)
        backend = backends.AgyInteractiveBackend()
    else:
        backend = {
            "codex": backends.CodexBackend,
            "agy": backends.AgyBackend,
            "claude": backends.ClaudeBackend,
            "cursor": backends.CursorBackend,
            "copilot": backends.CopilotBackend,
            "hermes": backends.HermesBackend,
            "devin": backends.DevinBackend,
        }[backend_name]()

    backend.run(
        prompt="check the executor child env",
        cwd=".",
        session_dir=".",
        session_id="sess-1910",
        agent_label="Desktop_Agent1",
        timeout_s=5,
    )
    return captured["env"]


@pytest.mark.parametrize("backend_name", NATIVE_BACKENDS)
def test_backend_child_env_drops_parent_secrets(backend_name, hostile_parent, monkeypatch):
    env = _capture_env(backend_name, monkeypatch)

    leaked = sorted(set(SECRET_PARENT_VARS) & set(env))
    assert leaked == [], f"{backend_name} child inherited {leaked}"
    for key, value in REQUIRED_PARENT_VARS.items():
        assert env.get(key) == value, f"{backend_name} child lost {key}"
    assert env["UA_AGENT_LABEL"] == "Desktop_Agent1"
    # The bearer is bound from the registry, never inherited raw.
    assert env["UA_AGENT_BEARER_TOKEN"] == "fake-registry-token"


def test_pi_child_env_drops_parent_secrets(hostile_parent):
    """Pi already used an allowlist (tasks #1524/#1677); keep it that way."""
    env = pi_runner._build_env("Desktop_Agent1")
    leaked = sorted((set(SECRET_PARENT_VARS) - {"OPENAI_API_KEY"}) & set(env))
    assert leaked == []
    assert env["HOME"] == REQUIRED_PARENT_VARS["HOME"]
    assert "PATH" in env


@pytest.mark.parametrize(
    ("backend_name", "var", "value"),
    [
        ("codex", "CODEX_HOME", "/home/fake-user/.codex"),
        ("claude", "CLAUDE_CONFIG_DIR", "/home/fake-user/.claude"),
        ("cursor", "UA_CURSOR_TOKEN_FILE", "/home/fake-user/.cursor/unified-agent-token"),
        ("copilot", "UA_COPILOT_TOKEN_FILE", "/home/fake-user/.copilot/unified-agent-token"),
        ("hermes", "HERMES_HOME", "/home/fake-user/.hermes"),
        ("devin", "UA_DEVIN_TOKEN_FILE", "/home/fake-user/.devin/unified-agent-token"),
    ],
)
def test_backend_keeps_its_own_config_locations(
    backend_name, var, value, hostile_parent, monkeypatch
):
    monkeypatch.setenv(var, value)
    env = _capture_env(backend_name, monkeypatch)
    assert env[var] == value


def test_backend_specific_location_does_not_reach_other_backends(hostile_parent, monkeypatch):
    monkeypatch.setenv("UA_COPILOT_TOKEN_FILE", "/home/fake-user/.copilot/unified-agent-token")
    env = _capture_env("claude", monkeypatch)
    assert "UA_COPILOT_TOKEN_FILE" not in env


def test_scoped_passthrough_admits_exactly_one_key_for_one_backend(hostile_parent, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_ENV_PASSTHROUGH", "codex:OPENAI_API_KEY")

    codex_env = _capture_env("codex", monkeypatch)
    assert codex_env["OPENAI_API_KEY"] == "fake-openai"
    assert "ANTHROPIC_API_KEY" not in codex_env
    assert "UA_MANAGED_SECRET_KEYS" not in codex_env

    claude_env = _capture_env("claude", monkeypatch)
    assert "OPENAI_API_KEY" not in claude_env


def test_unscoped_passthrough_of_a_secret_name_is_refused(hostile_parent, monkeypatch):
    """A bare secret-looking name would hand the key to every backend."""
    monkeypatch.setenv("UA_EXECUTOR_ENV_PASSTHROUGH", "OPENAI_API_KEY,HTTPS_PROXY")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")

    env = _capture_env("codex", monkeypatch)
    assert "OPENAI_API_KEY" not in env
    assert env["HTTPS_PROXY"] == "http://proxy.example.test:3128"


@pytest.mark.parametrize(
    "name",
    [
        "UA_MANAGED_SECRET_KEYS",
        "UA_AGENT_TOKENS",
        "UA_EXECUTOR_TOKEN",
        "UA_AGENT_BEARER_TOKEN",
        "UA_HTTP_BEARER",
    ],
)
def test_hub_secrets_cannot_be_passed_through_even_when_scoped(name, hostile_parent, monkeypatch):
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "fake-ambient-bearer")
    monkeypatch.setenv("UA_HTTP_BEARER", "fake-http-bearer")
    monkeypatch.setenv("UA_EXECUTOR_ENV_PASSTHROUGH", f"codex:{name},{name}")

    env = executor_child_env.build_executor_child_env("codex", "Unknown_Agent")
    assert name not in env


def test_builder_accepts_explicit_parent_mapping(monkeypatch):
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_EXECUTOR_ENV_PASSTHROUGH", raising=False)
    parent = {**SECRET_PARENT_VARS, **REQUIRED_PARENT_VARS, "UA_AGENT_TOKENS": ""}
    env = executor_child_env.build_executor_child_env("claude", "Desktop_Agent1", parent=parent)
    assert not set(SECRET_PARENT_VARS) & set(env)
    assert env["PATH"] == REQUIRED_PARENT_VARS["PATH"]
    assert env["UA_AGENT_LABEL"] == "Desktop_Agent1"


OPENROUTER_BACKENDS = {"codex", "hermes"}


@pytest.mark.parametrize("backend_name", NATIVE_BACKENDS)
def test_openrouter_key_reaches_only_openrouter_capable_backends(
    backend_name, hostile_parent, monkeypatch
):
    """Executors may route through OpenRouter (Operator, 2026-10-07).

    codex (model_providers with env_key) and hermes read OPENROUTER_API_KEY;
    no other backend can use it, so it stays out of their children.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter")
    monkeypatch.setenv("UA_OPENROUTER_MODEL", "fake/model")
    env = _capture_env(backend_name, monkeypatch)
    if backend_name in OPENROUTER_BACKENDS:
        assert env.get("OPENROUTER_API_KEY") == "fake-openrouter"
        assert env.get("UA_OPENROUTER_MODEL") == "fake/model"
    else:
        assert "OPENROUTER_API_KEY" not in env
    # The OpenRouter allowance never opens other provider keys.
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize("scope", ["agy", "agy-interactive"])
def test_interactive_and_print_credential_scopes_are_distinct(scope, hostile_parent, monkeypatch):
    monkeypatch.setenv("UA_EXECUTOR_ENV_PASSTHROUGH", f"{scope}:GEMINI_API_KEY")
    for backend in ("agy", "agy-interactive"):
        env = _capture_env(backend, monkeypatch)
        assert ("GEMINI_API_KEY" in env) is (backend == scope)


@pytest.mark.parametrize("name", ["DATABASE_URL", "SSH_AUTH_SOCK"])
def test_scoped_passthrough_cannot_admit_denied_infrastructure(name, hostile_parent, monkeypatch):
    monkeypatch.setenv(name, "fake-infrastructure-credential")
    monkeypatch.setenv("UA_EXECUTOR_ENV_PASSTHROUGH", f"codex:{name}")
    assert name not in _capture_env("codex", monkeypatch)


def test_copilot_keeps_explicit_bound_bearer_mode(hostile_parent, monkeypatch):
    monkeypatch.setenv("UA_COPILOT_TOKEN_SOURCE", "env")
    assert _capture_env("copilot", monkeypatch)["UA_COPILOT_TOKEN_SOURCE"] == "env"


def test_tmux_script_replaces_ambient_server_secrets(tmp_path):
    import json
    import os
    import shlex
    import sys

    from unified_agent.executor.agy_interactive import _tmux_exec_script

    probe = "import os,json; print(json.dumps({k: os.environ.get(k) for k in ('UA_MANAGED_SECRET_KEYS','ALLOWED')}))"
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}"
    script = tmp_path / "env.sh"
    script.write_text(_tmux_exec_script(cmd, {"ALLOWED": "safe"}))
    result = subprocess.run(
        ["/bin/sh", str(script)],
        env={**os.environ, "UA_MANAGED_SECRET_KEYS": "fake-secret"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"UA_MANAGED_SECRET_KEYS": None, "ALLOWED": "safe"}
