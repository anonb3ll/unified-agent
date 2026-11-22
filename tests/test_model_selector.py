"""Model selector: config defaults, [model:] override, registry/evidence persistence."""

from __future__ import annotations

import logging

from unified_agent import config, registry, storage
from unified_agent.executor import evidence


class TestSanitizeModelId:
    def test_accepts_common_ids(self):
        assert config.sanitize_model_id("gpt-5.5") == "gpt-5.5"
        assert config.sanitize_model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"
        assert (
            config.sanitize_model_id("claude-opus-4-8[context=1m,effort=high]")
            == "claude-opus-4-8[context=1m,effort=high]"
        )
        assert config.sanitize_model_id("openai/gpt-4o") == "openai/gpt-4o"

    def test_empty_and_none(self):
        assert config.sanitize_model_id(None) is None
        assert config.sanitize_model_id("") is None
        assert config.sanitize_model_id("   ") is None

    def test_invalid_warns_and_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert config.sanitize_model_id("bad model!", source="UA_MODEL_CURSOR") is None
        assert "UA_MODEL_CURSOR" in caplog.text
        assert "not a valid model id" in caplog.text


class TestAgentModelDefault:
    def test_unset_returns_none(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        assert config.agent_model_default("Desktop_Agent4") is None
        assert config.agent_model_default("Desktop_Agent2") is None
        assert config.agent_model_default("Desktop_agent3") is None
        assert config.agent_model_default("pi_worker") is None

    def test_family_env_wins(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UA_MODEL_CURSOR", "composer-2")
        monkeypatch.setenv("UA_MODEL_DEFAULT", "fallback-model")
        assert config.agent_model_default("Desktop_Agent4") == "composer-2"
        assert config.agent_model_default("Laptop_Agent4") == "composer-2"
        assert config.agent_model_default("server_agent4") == "composer-2"
        # Other families fall through to UA_MODEL_DEFAULT.
        assert config.agent_model_default("Desktop_Agent2") == "fallback-model"

    def test_per_family_envs(self, monkeypatch):
        monkeypatch.setenv("UA_MODEL_CODEX", "gpt-5.5-codex")
        monkeypatch.setenv("UA_MODEL_AGY", "gemini-3.6-flash-high")
        monkeypatch.setenv("UA_MODEL_PI", "openai/gpt-4o")
        assert config.agent_model_default("Desktop_Agent2") == "gpt-5.5-codex"
        assert config.agent_model_default("server_agent3") == "gemini-3.6-flash-high"
        assert config.agent_model_default("pi") == "openai/gpt-4o"
        assert config.agent_model_default("pi_worker") == "openai/gpt-4o"

    def test_invalid_family_env_falls_through_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("UA_MODEL_CURSOR", "not a model!!!")
        monkeypatch.setenv("UA_MODEL_DEFAULT", "safe-default")
        with caplog.at_level(logging.WARNING):
            assert config.agent_model_default("Desktop_Agent4") == "safe-default"
        assert "UA_MODEL_CURSOR" in caplog.text

    def test_unknown_family_uses_global_default_only(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UA_MODEL_DEFAULT", "shared")
        assert config.agent_model_default("Desktop_Agent1") == "shared"
        assert config.agent_model_default("Desktop_Agent5") == "shared"

    def test_configured_model_defaults_snapshot(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UA_MODEL_CURSOR", "composer-2")
        monkeypatch.setenv("UA_MODEL_DEFAULT", "fallback")
        snap = config.configured_model_defaults()
        assert snap["UA_MODEL_CURSOR"] == "composer-2"
        assert snap["UA_MODEL_DEFAULT"] == "fallback"
        assert snap["UA_MODEL_CODEX"] is None
        assert snap["UA_MODEL_CLAUDE"] is None

    def test_claude_family_env(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UA_MODEL_CLAUDE", "sonnet-5")
        assert config.agent_model_default("Desktop_Agent1") == "sonnet-5"
        assert config.agent_model_default("server_agent1") == "sonnet-5"

    def test_selector_seeds_canonical_fleet_ids(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        # Run resolution stays None when unset (byte-for-byte).
        assert config.agent_model_default("Desktop_Agent4") is None
        # Selector lands pre-populated with the operator's fleet seeds.
        defaults = config.model_defaults_for_selector()
        assert defaults["UA_MODEL_CURSOR"] == "auto"
        assert defaults["UA_MODEL_CODEX"] == "gpt-6-luna"
        assert defaults["UA_MODEL_AGY"] == "gemini-3.8-flash-high"
        assert defaults["UA_MODEL_CLAUDE"] == "sonnet-5"
        opts = config.model_selector_options()
        assert "auto" in opts
        assert "gpt-6-luna" in opts
        assert "gemini-3.8-flash-high" in opts
        assert "sonnet-5" in opts
        assert "cursor-grok-4.6-high-fast" in opts

    def test_selector_env_overrides_seed(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UA_MODEL_CURSOR", "composer-2")
        defaults = config.model_defaults_for_selector()
        assert defaults["UA_MODEL_CURSOR"] == "composer-2"
        assert "composer-2" in config.model_selector_options()


class TestParseModelOverride:
    def test_absent(self):
        assert config.parse_model_override("do the thing") is None
        assert config.parse_model_override(None) is None

    def test_extracts_last_marker(self):
        spec = "Objective\n[model: gpt-5.5]\nmore\n[model: composer-2]\n"
        assert config.parse_model_override(spec) == "composer-2"

    def test_invalid_marker_ignored(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert config.parse_model_override("[model: bad id!]") is None
        assert "[model:]" in caplog.text


class TestResolveRunModel:
    def test_override_beats_default(self, monkeypatch):
        monkeypatch.setenv("UA_MODEL_CURSOR", "default-cursor")
        assert (
            config.resolve_run_model("Desktop_Agent4", override="override-model")
            == "override-model"
        )

    def test_default_beats_backend_fallback(self, monkeypatch):
        monkeypatch.setenv("UA_MODEL_CODEX", "from-ua-model")
        assert (
            config.resolve_run_model("Desktop_Agent2", backend_fallback="from-ua-codex-model")
            == "from-ua-model"
        )

    def test_backend_fallback_when_unset(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        assert config.resolve_run_model("Desktop_Agent2", backend_fallback="gpt-5.5") == "gpt-5.5"


class TestRegistryAndEvidenceModel:
    def test_start_session_persists_model(self):
        sid = registry.start_session(
            "Desktop_Agent4", "cursor_run", model="composer-2", task_id=None
        )
        assert sid is not None
        row = registry.list_sessions(reap=False)[0]
        assert row["model"] == "composer-2"

    def test_start_attempt_persists_model(self):
        from unified_agent.tools import tasks as tasks_tool

        task = tasks_tool.task_add(title="model evidence")
        attempt_id = evidence.start_attempt(
            task_id=task["id"],
            delegation_id=None,
            agent="Desktop_Agent2",
            backend="codex",
            model="gpt-5.5-codex",
        )
        assert attempt_id is not None
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT model FROM executor_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["model"] == "gpt-5.5-codex"


class TestBackendArgvModel:
    def test_codex_uses_run_model_override(self, monkeypatch):
        from unified_agent.executor import backends

        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("UA_CODEX_MODEL", raising=False)
        backend = backends.CodexBackend(model="gpt-5.5")
        argv = backend.build_command("p", "/tmp/wt", model="override-model")
        assert argv[argv.index("--model") + 1] == "override-model"

    def test_agy_passes_model_flag(self, monkeypatch):
        from unified_agent.executor import backends

        monkeypatch.delenv("UA_AGY_MODEL", raising=False)
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        backend = backends.AgyBackend()
        argv = backend.build_command("prompt", model="gemini-flash")
        assert argv[argv.index("--model") + 1] == "gemini-flash"

    def test_cursor_passes_model_flag(self, monkeypatch):
        from unified_agent.dispatch import harness_sandbox
        from unified_agent.executor import backends

        monkeypatch.delenv("UA_CURSOR_SKIP_PERMISSIONS", raising=False)
        backend = backends.CursorBackend(cursor_bin="agent")
        argv = backend.build_command(
            "fix the cache",
            cwd="/tmp",
            model="composer-2",
            sandbox_posture=harness_sandbox.resolve("cursor"),
        )
        assert argv[argv.index("--sandbox") + 1] == "enabled"
        assert "--trust" in argv
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "composer-2"

    def test_headless_argv_codex_includes_model(self):
        from unified_agent.dispatch import routing

        argv = routing.headless_argv("codex", "codex", prompt="p", cwd="/tmp", model="gpt-5.5")
        assert argv[argv.index("--model") + 1] == "gpt-5.5"

    def test_claude_passes_model_flag(self):
        from unified_agent.dispatch import routing

        argv = routing.headless_argv("claude", "claude", prompt="p", model="sonnet-5")
        assert argv[argv.index("--model") + 1] == "sonnet-5"

    def test_unset_keeps_codex_legacy_default(self, monkeypatch):
        from unified_agent.executor import backends

        monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
        monkeypatch.delenv("UA_CODEX_MODEL", raising=False)
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        backend = backends.CodexBackend()
        argv = backend.build_command("p", "/tmp/wt")
        assert argv[argv.index("--model") + 1] == backends.DEFAULT_CODEX_MODEL


class TestDispatchSpecModel:
    def test_dispatch_spec_embeds_model_marker(self):
        from unified_agent.webui.contracts import DispatchRequest, dispatch_spec

        req = DispatchRequest(
            agent="Desktop_Agent4",
            prompt="do the thing",
            task_id=None,
            context=(),
            project="unified-agent",
            model="auto",
        )
        spec = dispatch_spec(req)
        assert "[model: auto]" in spec
        assert config.parse_model_override(spec) == "auto"

    def test_dispatch_routes_payload_marks_retired_scheduler(self, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
        from unified_agent.webui import api

        monkeypatch.setattr(api.monitor, "_service_probe", lambda *_: {"LoadState": "not-found"})
        payload = api.dispatch_routes_payload()
        assert payload["scheduler"] == {
            "enabled": False,
            "real_spawn": False,
            "status": "retired",
            "mode": "retired",
        }

    def test_dispatch_routes_payload_flags_reloaded_retired_tick(self, monkeypatch):
        monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
        monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
        from unified_agent.webui import api

        monkeypatch.setattr(
            api.monitor,
            "_service_probe",
            lambda *_: {"LoadState": "loaded", "ActiveState": "active"},
        )
        payload = api.dispatch_routes_payload()
        assert payload["scheduler"] == {
            "enabled": True,
            "real_spawn": True,
            "status": "misconfigured_active",
            "mode": "native_cli",
        }
        assert all(route["scheduler_spawn"]["enabled"] for route in payload["routes"])

    def test_dispatch_routes_payload_includes_seeds(self, monkeypatch):
        for key in config._MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        from unified_agent.webui import api

        payload = api.dispatch_routes_payload()
        assert payload["model_defaults"]["UA_MODEL_CODEX"] == "gpt-6-luna"
        assert "sonnet-5" in payload["model_options"]
        assert "cursor-grok-4.6-high-fast" in payload["model_options"]
