"""Memory routing fixtures: no paid generation or production state."""

import json
import os
import subprocess
import sys

import httpx
import pytest

from unified_agent import llm_client, sleep_pass


@pytest.fixture(autouse=True)
def clear_memory_env(monkeypatch):
    for key in (llm_client.MEMORY_PROVIDER_ENV, llm_client.MEMORY_MODEL_ENV):
        monkeypatch.delenv(key, raising=False)


def test_defaults_and_resolution():
    assert llm_client.memory_model_settings({}) == (None, None)
    assert llm_client.memory_model_settings({"UA_MEMORY_PROVIDER": " claude_cli "}) == (
        "claude_cli",
        None,
    )
    assert llm_client.memory_model_settings(
        {"UA_MEMORY_PROVIDER": "subscription_pool", "UA_MEMORY_MODEL": "claude-haiku-5-5"}
    ) == ("subscription_pool", "claude-haiku-5-5")


@pytest.mark.parametrize("provider", ["claude_cli", "openrouter", "subscription_pool"])
def test_blank_provider_model_setting_means_the_default(provider):
    # Task #2034: like the ordinary adapters, a whitespace-only model setting is
    # the default, not an invalid identifier.
    key = {
        "claude_cli": llm_client.CLAUDE_CLI_MODEL_ENV,
        "openrouter": llm_client.OPENROUTER_MODEL_ENV,
        "subscription_pool": llm_client.SUBSCRIPTION_POOL_MODEL_ENV,
    }[provider]
    default = llm_client._memory_effective_model(provider, None, {})
    assert llm_client._memory_effective_model(provider, None, {key: "   "}) == default
    assert llm_client._memory_effective_model(provider, "  ", {}) == default
    assert llm_client._memory_effective_model(provider, " x-model ", {}) == "x-model"


@pytest.mark.parametrize(
    "env",
    [
        {"UA_MEMORY_MODEL": "claude-haiku-5-5"},
        {"UA_MEMORY_PROVIDER": "anthropic"},
        {"UA_MEMORY_PROVIDER": "ai_studio"},
        {"UA_MEMORY_PROVIDER": "ollama"},
        {"UA_MEMORY_PROVIDER": "openrouter", "UA_MEMORY_MODEL": "bad model"},
        {"UA_MEMORY_PROVIDER": "openrouter", "UA_MEMORY_MODEL": "bad\nmodel"},
    ],
)
def test_invalid_settings(env):
    with pytest.raises(ValueError):
        llm_client.memory_model_settings(env)


def test_pinned_model_propagates_without_changing_shared_settings(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", "subscription_pool")
    monkeypatch.setenv("UA_MEMORY_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://fixture.test/v1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_MODEL", "shared-model")
    seen = []

    def post(url, **kw):
        seen.append(kw["json"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "model": kw["json"]["model"]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    result = llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
    assert result["model"] == "claude-haiku-5-5"
    assert [a["target"] for a in result["route"]] == ["subscription_pool"]
    assert seen[0]["model"] == "claude-haiku-5-5"
    assert not {"temperature", "top_p", "top_k", "thinking"} & seen[0].keys()
    assert seen[0]["messages"][-1]["role"] == "user"
    assert llm_client._call_subscription_pool("other", None)["model"] == "shared-model"


def test_unavailable_pinned_model_never_falls_back(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", "subscription_pool")
    monkeypatch.setenv("UA_MEMORY_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://fixture.test/v1")

    def post(url, **kw):
        return httpx.Response(
            404, json={"error": {"message": "model not found"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(llm_client.LLMConnectionError) as exc:
        llm_client.complete("private", subscription_pool=True, memory_agent=True)
    assert len(exc.value.route) == 1
    assert exc.value.route[0]["status"] == 404


def test_disabled_pool_remains_disabled(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", "subscription_pool")
    monkeypatch.setenv("UA_MEMORY_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "0")
    with pytest.raises(llm_client.LLMConnectionError) as exc:
        llm_client.complete("prompt", subscription_pool=True, memory_agent=True)
    assert exc.value.route[0]["error"] == "Unconfigured"


def test_persistent_settings_restart_and_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    (tmp_path / "service.env").write_text(
        "UA_MEMORY_PROVIDER=openrouter\nUA_MEMORY_MODEL=anthropic/claude-haiku-5.5\n"
    )
    (tmp_path / "sleep-pass.env").write_text(
        "UA_MEMORY_PROVIDER=claude_cli\nUA_MEMORY_MODEL=claude-haiku-5-5\n"
    )
    expected = ("claude_cli", "claude-haiku-5-5")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == expected
    # A cold process sources the same persisted scheduled file; no module cache.
    for _ in range(2):
        monkeypatch.setenv("UA_MEMORY_PROVIDER", expected[0])
        monkeypatch.setenv("UA_MEMORY_MODEL", expected[1])
        sleep_pass._inherit_hub_local_model_env()
        assert llm_client.memory_model_settings() == expected
    (tmp_path / "sleep-pass.env").write_text("")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (
        "openrouter",
        "anthropic/claude-haiku-5.5",
    )


@pytest.mark.parametrize("provider", ["openrouter"])
def test_existing_adapters_receive_override(monkeypatch, provider):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", provider)
    monkeypatch.setenv("UA_MEMORY_MODEL", "fixture-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    seen = []

    def post(url, **kw):
        seen.append(kw["json"]["model"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "model": kw["json"]["model"]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    result = llm_client.complete("prompt", memory_agent=True)
    assert seen == ["fixture-model"]
    assert result["provider"] == provider
    assert result["model"] == "fixture-model"


def test_nonmemory_consumer_ignores_memory_selection(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", "invalid")
    monkeypatch.setenv("UA_MEMORY_MODEL", "memory-only")
    monkeypatch.setattr(
        llm_client,
        "_PROVIDER_CHAIN",
        [("ollama", lambda *a, **kw: {"text": "ok", "provider": "ollama", "model": "default"})],
    )
    assert llm_client.complete("prompt")["model"] == "default"


def test_cold_process_rereads_persisted_configuration(tmp_path):
    config_file = tmp_path / "sleep-pass.env"
    env = dict(os.environ)
    # A hub interpreter may have another checkout installed; test this candidate.
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(llm_client.__file__))
    code = (
        "import json,sys; from pathlib import Path; "
        "from unified_agent.sleep_pass import parse_env_file; "
        "from unified_agent.llm_client import memory_model_settings; "
        "print(json.dumps(memory_model_settings(parse_env_file(Path(sys.argv[1])))))"
    )
    for model in ("claude-haiku-5-5", "fixture-new-model"):
        config_file.write_text(f"UA_MEMORY_PROVIDER=subscription_pool\nUA_MEMORY_MODEL={model}\n")
        result = subprocess.run(
            [sys.executable, "-c", code, str(config_file)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == ["subscription_pool", model]


def test_memory_status_probes_selected_model_and_reports_disabled_route(monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm_client, "_probe_endpoint", lambda *a, **kw: calls.append(kw) or (True, "ok")
    )
    env = {
        "UA_MEMORY_PROVIDER": "subscription_pool",
        "UA_MEMORY_MODEL": "claude-haiku-5-5",
        "UA_SUBSCRIPTION_POOL_ENABLED": "1",
        "UA_SUBSCRIPTION_POOL_URL": "http://fixture.test/v1",
    }
    legs = llm_client.leg_status(env, memory_agent=True)
    assert [leg["leg"] for leg in legs] == ["subscription_pool"]
    assert calls[-1]["expect_model"] == "claude-haiku-5-5"
    env["UA_SUBSCRIPTION_POOL_ENABLED"] = "0"
    assert llm_client.leg_status(env, memory_agent=True)[0]["configured"] is False


def test_requested_model_survives_bounded_failure_telemetry():
    route = [
        {
            "target": "subscription_pool",
            "ok": False,
            "elapsed_s": 0,
            "model": "claude-haiku-5-5",
            "error": "Unconfigured",
        }
    ]
    assert sleep_pass._bounded_llm_route(route)[0]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize(
    ("ids", "reachable"),
    [(["claude-haiku-5-5"], True), (["claude-haiku-5-5:latest"], False), (["other"], False)],
)
def test_subscription_catalog_matches_effective_model_exactly(monkeypatch, ids, reachable):
    llm_client.clear_probe_cache()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            json={"data": [{"id": model} for model in ids]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    env = {
        "UA_MEMORY_PROVIDER": "subscription_pool",
        "UA_SUBSCRIPTION_POOL_MODEL": "claude-haiku-5-5",
        "UA_SUBSCRIPTION_POOL_ENABLED": "1",
        "UA_SUBSCRIPTION_POOL_URL": "http://fixture.test/v1",
        "GEMINI_API_KEY": "fixture-key",
        "OPENROUTER_API_KEY": "fixture-key",
    }
    # Provider-only configuration must check the same model inference requests.
    legs = llm_client.leg_status(env, memory_agent=True)
    assert legs[0]["reachable"] is reachable
    assert calls == ["http://fixture.test/v1/models"]


def test_ollama_alias_cache_cannot_make_subscription_model_look_available(monkeypatch):
    monkeypatch.setenv("UA_LLM_PROBE_CACHE_S", "60")
    llm_client.clear_probe_cache()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200, json={"data": [{"id": "model:latest"}]}, request=httpx.Request("GET", url)
        ),
    )
    url = "http://fixture.test/v1/models"
    assert llm_client._probe_endpoint(url, {}, 1, expect_model="model", ollama_alias=True)[0]
    assert not llm_client._probe_endpoint(url, {}, 1, expect_model="model")[0]


def test_disabled_selection_is_visible_in_doctor(monkeypatch):
    from unified_agent.tools import doctor

    monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
    monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: True)
    monkeypatch.setattr(
        sleep_pass,
        "scheduled_env",
        lambda: {
            "UA_MEMORY_PROVIDER": "subscription_pool",
            "UA_SUBSCRIPTION_POOL_ENABLED": "0",
        },
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200, json={"data": [{"id": "llama3:8b"}]}, request=httpx.Request("GET", url)
        ),
    )
    llm_client.clear_probe_cache()
    check = doctor._llm_provider_chain_check()
    assert check["status"] == "MISCONFIGURED"
    assert "subscription_pool[sleep-pass]:unconfigured" in check["detail"]
    assert "UA_SUBSCRIPTION_POOL_ENABLED" in check["detail"]


def _openrouter_metadata_ok(url, **kw):
    """Unbilled OpenRouter metadata: the key answers and the default is listed."""
    payload = {"data": [{"id": llm_client.DEFAULT_OPENROUTER_MODEL}]}
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def test_doctor_reports_an_unusable_hub_memory_route(monkeypatch):
    """Task #2025 review: the hub pins memory calls to a disabled pool while
    OpenRouter is healthy and no sleep pass is scheduled. The general chain is
    usable, but every memory request fails as Unconfigured: not LIVE."""
    from unified_agent.tools import doctor

    monkeypatch.setattr(
        doctor,
        "_service_env",
        lambda unit: {
            "UA_MEMORY_PROVIDER": "subscription_pool",
            "UA_SUBSCRIPTION_POOL_ENABLED": "0",
            "OPENROUTER_API_KEY": "fixture-key",
        },
    )
    monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: False)
    monkeypatch.setattr(httpx, "get", _openrouter_metadata_ok)
    llm_client.clear_probe_cache()
    check = doctor._llm_provider_chain_check()
    assert check["status"] == "DEGRADED"
    assert "openrouter:up" in check["detail"]
    assert (
        "hub memory route (UA_MEMORY_PROVIDER=subscription_pool) has no usable leg"
        in (check["detail"])
    )
    assert (
        "subscription_pool[memory]:unconfigured (UA_SUBSCRIPTION_POOL_ENABLED unset)"
        in (check["detail"])
    )
    assert "[sleep-pass]" not in check["detail"]


def test_doctor_reports_a_usable_hub_memory_route_as_live(monkeypatch):
    from unified_agent.tools import doctor

    monkeypatch.setattr(
        doctor,
        "_service_env",
        lambda unit: {"UA_MEMORY_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "fixture-key"},
    )
    monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: False)
    monkeypatch.setattr(httpx, "get", _openrouter_metadata_ok)
    llm_client.clear_probe_cache()
    check = doctor._llm_provider_chain_check()
    assert check["status"] == "LIVE"
    assert "openrouter[memory]:up" in check["detail"]


def test_doctor_reports_invalid_hub_memory_settings_as_misconfigured(monkeypatch):
    from unified_agent.tools import doctor

    monkeypatch.setattr(
        doctor,
        "_service_env",
        lambda unit: {"UA_MEMORY_MODEL": "claude-haiku-5-5", "OPENROUTER_API_KEY": "fixture-key"},
    )
    monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: False)
    monkeypatch.setattr(httpx, "get", _openrouter_metadata_ok)
    llm_client.clear_probe_cache()
    check = doctor._llm_provider_chain_check()
    assert check["status"] == "MISCONFIGURED"
    assert (
        "hub memory route misconfigured: UA_MEMORY_MODEL requires UA_MEMORY_PROVIDER"
        in (check["detail"])
    )


def test_selected_cli_retains_quota_caveat():
    from unified_agent.tools import doctor

    part = doctor._leg_part(
        {
            "leg": "claude_cli",
            "reachable": True,
            "memory_selected": True,
            "detail": "claude-haiku-5-5: authMethod=claude.ai",
            "caveat": llm_client.CLAUDE_CLI_LOGIN_CAVEAT,
        }
    )
    assert "claude-haiku-5-5: authMethod=claude.ai" in part
    assert llm_client.CLAUDE_CLI_LOGIN_CAVEAT in part


@pytest.mark.parametrize("provider", ["claude_cli", "subscription_pool"])
def test_provider_only_selection_does_not_inherit_hub_memory_model(monkeypatch, tmp_path, provider):
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    (tmp_path / "service.env").write_text(
        "UA_MEMORY_PROVIDER=openrouter\nUA_MEMORY_MODEL=anthropic/claude-haiku-5.5\n"
    )
    (tmp_path / "sleep-pass.env").write_text(f"UA_MEMORY_PROVIDER={provider}\n")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (provider, None)
    monkeypatch.setenv("UA_MEMORY_PROVIDER", provider)
    sleep_pass._inherit_hub_local_model_env()
    assert "UA_MEMORY_MODEL" not in os.environ
    assert llm_client.memory_model_settings() == (provider, None)


@pytest.mark.parametrize(
    "ids,reachable",
    [
        (["anthropic/claude-haiku-5.5"], True),
        (["anthropic/other"], False),
        (["anthropic/claude-haiku-5.5:latest"], False),
    ],
)
def test_selected_openrouter_catalog_requires_exact_effective_model(monkeypatch, ids, reachable):
    llm_client.clear_probe_cache()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200,
            json={"models": [{"name": name} for name in ids]},
            request=httpx.Request("GET", url),
        ),
    )
    env = {
        "UA_MEMORY_PROVIDER": "openrouter",
        "UA_MEMORY_MODEL": "anthropic/claude-haiku-5.5",
        "OPENROUTER_API_KEY": "fixture-key",
    }
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/auth/key"):
            return httpx.Response(200, json={"data": {}}, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            json={"data": [{"id": model} for model in ids]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", get)
    legs = llm_client.leg_status(env, memory_agent=True)
    assert [leg["leg"] for leg in legs] == ["openrouter"]
    assert legs[0]["reachable"] is reachable
    assert calls == [
        "https://openrouter.ai/api/v1/auth/key",
        "https://openrouter.ai/api/v1/models",
    ]


def test_provider_only_failure_records_effective_requested_model(monkeypatch):
    monkeypatch.setenv("UA_MEMORY_PROVIDER", "subscription_pool")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_MODEL", "missing-model")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
    monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://fixture.test/v1")
    seen = []

    def post(url, **kw):
        seen.append(kw["json"]["model"])
        return httpx.Response(
            404, json={"error": {"message": "model not found"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(llm_client.LLMConnectionError) as exc:
        llm_client.complete("private", subscription_pool=True, memory_agent=True)
    assert seen == ["missing-model"]
    assert exc.value.route[0]["model"] == "missing-model"
    assert sleep_pass._bounded_llm_route(exc.value.route)[0]["model"] == "missing-model"


def test_scheduled_wrapper_reads_relocated_home_and_matches_doctor(monkeypatch, tmp_path):
    import shutil
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "code"
    (root / "deploy").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    (root / "src").symlink_to(repo / "src", target_is_directory=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    shutil.copy2(repo / "deploy/run-sleep-pass-scheduled.sh", root / "deploy/runner.sh")
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    (relocated / "sleep-pass.env").write_text(
        "UA_MEMORY_PROVIDER=claude_cli\nUA_MEMORY_MODEL=claude-haiku-5-5\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UA_HOME", str(relocated))
    monkeypatch.setenv("UA_CODE_ROOT", str(root))
    cli = root / ".venv/bin/unified-agent-sleep-pass"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json; from unified_agent import llm_client,sleep_pass\n"
        "sleep_pass._inherit_hub_local_model_env()\n"
        "print(json.dumps(llm_client.memory_model_settings()))\n"
    )
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", str(root / "deploy/runner.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == list(
        llm_client.memory_model_settings(sleep_pass.scheduled_env())
    )
    assert json.loads(result.stdout) == ["claude_cli", "claude-haiku-5-5"]


def _write_settings(path, body: str):
    path.write_text("schema_version = 1\n\n[env]\n" + body)
    path.chmod(0o600)
    return path


def test_scheduled_env_reads_the_settings_toml_memory_pin(monkeypatch, tmp_path):
    """Task #2175: a settings.toml [env] pin reaches doctor's nightly view.

    The scheduled process seeds unset keys from settings.toml at import, so
    the pin wins over service.env and loses to sleep-pass.env, as at run time.
    """
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    monkeypatch.delenv("UA_SETTINGS_FILE", raising=False)
    _write_settings(
        tmp_path / "settings.toml",
        'UA_MEMORY_PROVIDER = "claude_cli"\nUA_MEMORY_MODEL = "claude-haiku-5-5"\n',
    )
    (tmp_path / "service.env").write_text(
        "UA_MEMORY_PROVIDER=openrouter\nUA_MEMORY_MODEL=anthropic/claude-haiku-5.5\n"
    )
    (tmp_path / "sleep-pass.env").write_text("OPENROUTER_API_KEY=pass-key\n")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (
        "claude_cli",
        "claude-haiku-5-5",
    )

    # sleep-pass.env is the real environment of the scheduled job: it wins.
    (tmp_path / "sleep-pass.env").write_text(
        "UA_MEMORY_PROVIDER=subscription_pool\nUA_MEMORY_MODEL=pool-model\n"
    )
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (
        "subscription_pool",
        "pool-model",
    )

    # A provider-only pin in settings.toml does not pair with service.env's model.
    (tmp_path / "sleep-pass.env").write_text("")
    _write_settings(tmp_path / "settings.toml", 'UA_MEMORY_PROVIDER = "claude_cli"\n')
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == ("claude_cli", None)


def test_scheduled_env_honours_the_settings_file_location(monkeypatch, tmp_path):
    """sleep-pass.env's UA_SETTINGS_FILE selects (or disables) the file, as at startup."""
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    elsewhere = _write_settings(tmp_path / "custom.toml", 'UA_MEMORY_PROVIDER = "claude_cli"\n')
    _write_settings(tmp_path / "settings.toml", 'UA_MEMORY_PROVIDER = "openrouter"\n')
    (tmp_path / "sleep-pass.env").write_text(f"UA_SETTINGS_FILE={elsewhere}\n")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env())[0] == "claude_cli"
    (tmp_path / "sleep-pass.env").write_text("UA_SETTINGS_FILE=\n")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (None, None)
    (tmp_path / "sleep-pass.env").write_text("")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env())[0] == "openrouter"


def test_scheduled_env_ignores_the_doctor_process_settings_file(monkeypatch, tmp_path):
    """Task #2193: the scheduler never carries the doctor's UA_SETTINGS_FILE.

    Neither the systemd unit nor the LaunchAgent sets it, so the nightly pass
    reads <UA_HOME>/settings.toml unless sleep-pass.env names another file.
    """
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    elsewhere = _write_settings(tmp_path / "custom.toml", 'UA_MEMORY_PROVIDER = "claude_cli"\n')
    _write_settings(tmp_path / "settings.toml", 'UA_MEMORY_PROVIDER = "openrouter"\n')
    monkeypatch.setenv("UA_SETTINGS_FILE", str(elsewhere))
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env())[0] == "openrouter"
    monkeypatch.setenv("UA_SETTINGS_FILE", "")
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env())[0] == "openrouter"


def test_scheduled_env_follows_a_home_relocated_by_sleep_pass_env(monkeypatch, tmp_path):
    """Task #2196: a UA_HOME set in sleep-pass.env moves service.env and settings.toml.

    At run time the pass imports config with the relocated UA_HOME, so it
    seeds from that home's settings.toml and inherits from its service.env.
    """
    base = tmp_path / "base"
    moved = tmp_path / "moved"
    base.mkdir()
    moved.mkdir()
    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: base)
    monkeypatch.delenv("UA_SETTINGS_FILE", raising=False)
    (base / "sleep-pass.env").write_text(f"UA_HOME={moved}\n")
    (base / "service.env").write_text("UA_MEMORY_PROVIDER=claude_cli\nUA_OLLAMA_MODEL=base-model\n")
    (moved / "service.env").write_text(
        "UA_MEMORY_PROVIDER=openrouter\nUA_MEMORY_MODEL=anthropic/claude-haiku-5.5\n"
        "UA_OLLAMA_MODEL=moved-model\n"
    )

    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (
        "openrouter",
        "anthropic/claude-haiku-5.5",
    )
    assert sleep_pass.scheduled_ollama_env()["UA_OLLAMA_MODEL"] == "moved-model"

    _write_settings(base / "settings.toml", 'UA_MEMORY_PROVIDER = "subscription_pool"\n')
    _write_settings(
        moved / "settings.toml",
        'UA_MEMORY_PROVIDER = "claude_cli"\nUA_MEMORY_MODEL = "claude-haiku-5-5"\n',
    )
    assert llm_client.memory_model_settings(sleep_pass.scheduled_env()) == (
        "claude_cli",
        "claude-haiku-5-5",
    )
    assert sleep_pass.scheduled_ollama_env()[llm_client.MEMORY_PROVIDER_ENV] == "claude_cli"


def test_scheduled_wrapper_matches_doctor_with_a_settings_toml_pin(monkeypatch, tmp_path):
    """The real wrapper and scheduled_env agree when the pin lives in settings.toml."""
    import shutil
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "code"
    (root / "deploy").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    (root / "src").symlink_to(repo / "src", target_is_directory=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    shutil.copy2(repo / "deploy/run-sleep-pass-scheduled.sh", root / "deploy/runner.sh")
    home = tmp_path / "pinned_home"
    home.mkdir()
    _write_settings(
        home / "settings.toml",
        'UA_MEMORY_PROVIDER = "claude_cli"\nUA_MEMORY_MODEL = "claude-haiku-5-5"\n',
    )
    (home / "service.env").write_text(
        "UA_MEMORY_PROVIDER=openrouter\nUA_MEMORY_MODEL=anthropic/claude-haiku-5.5\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_CODE_ROOT", str(root))
    monkeypatch.delenv("UA_SETTINGS_FILE", raising=False)
    cli = root / ".venv/bin/unified-agent-sleep-pass"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json; from unified_agent import llm_client,sleep_pass\n"
        "sleep_pass._inherit_hub_local_model_env()\n"
        "print(json.dumps(llm_client.memory_model_settings()))\n"
    )
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", str(root / "deploy/runner.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["claude_cli", "claude-haiku-5-5"]
    assert json.loads(result.stdout) == list(
        llm_client.memory_model_settings(sleep_pass.scheduled_env())
    )


def test_doctor_names_the_settings_toml_pinned_sleep_pass_leg(monkeypatch, tmp_path):
    """Task #2175: the nightly rows show the one pinned leg, no OpenRouter fallback."""
    from unified_agent.tools import doctor

    monkeypatch.setattr(sleep_pass.config, "data_home", lambda: tmp_path)
    monkeypatch.delenv("UA_SETTINGS_FILE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_settings(
        tmp_path / "settings.toml",
        'UA_MEMORY_PROVIDER = "claude_cli"\nUA_MEMORY_MODEL = "claude-haiku-5-5"\n',
    )
    (tmp_path / "sleep-pass.env").write_text("OPENROUTER_API_KEY=fixture-key\n")
    monkeypatch.setattr(doctor, "_service_env", lambda unit: {})
    monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: True)
    monkeypatch.setattr(httpx, "get", _openrouter_metadata_ok)

    def cli_leg(env, *, timeout_s):
        model = env.get(llm_client.CLAUDE_CLI_MODEL_ENV) or llm_client.DEFAULT_CLAUDE_CLI_MODEL
        return {
            "leg": "claude_cli",
            "configured": True,
            "explicit": True,
            "reachable": True,
            "detail": f"{model}: authMethod=claude.ai",
        }

    monkeypatch.setattr(llm_client, "_claude_cli_leg_status", cli_leg)
    llm_client.clear_probe_cache()
    check = doctor._llm_provider_chain_check()
    detail = check["detail"]
    assert "claude_cli[sleep-pass]:up (claude-haiku-5-5: authMethod=claude.ai)" in detail
    assert "openrouter[sleep-pass]" not in detail
    assert check["status"] == "LIVE"


def test_scheduled_wrapper_matches_doctor_when_sleep_pass_env_relocates_home(monkeypatch, tmp_path):
    """The real wrapper and scheduled_env read the same relocated service.env (#2196)."""
    import shutil
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "code"
    (root / "deploy").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    (root / "src").symlink_to(repo / "src", target_is_directory=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    shutil.copy2(repo / "deploy/run-sleep-pass-scheduled.sh", root / "deploy/runner.sh")
    base = tmp_path / "base"
    moved = tmp_path / "moved"
    base.mkdir()
    moved.mkdir()
    (base / "sleep-pass.env").write_text(f"UA_HOME={moved}\n")
    (base / "service.env").write_text("UA_MEMORY_PROVIDER=subscription_pool\n")
    (moved / "service.env").write_text(
        "UA_MEMORY_PROVIDER=claude_cli\nUA_MEMORY_MODEL=claude-haiku-5-5\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UA_HOME", str(base))
    monkeypatch.setenv("UA_CODE_ROOT", str(root))
    monkeypatch.delenv("UA_SETTINGS_FILE", raising=False)
    cli = root / ".venv/bin/unified-agent-sleep-pass"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json; from unified_agent import llm_client,sleep_pass\n"
        "sleep_pass._inherit_hub_local_model_env()\n"
        "print(json.dumps(llm_client.memory_model_settings()))\n"
    )
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", str(root / "deploy/runner.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["claude_cli", "claude-haiku-5-5"]
    assert json.loads(result.stdout) == list(
        llm_client.memory_model_settings(sleep_pass.scheduled_env())
    )
