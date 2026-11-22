import json
import subprocess
from pathlib import Path

import pytest

from unified_agent import codex_llm_client as client


def stream(text='{"verdict":"DUPLICATE","reason":"same"}', model=None, usage=None):
    events = [{"type": "thread.started", "thread_id": "fake"}]
    if model:
        events[0]["model"] = model
    events += [
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {
            "type": "turn.completed",
            "usage": usage or {"input_tokens": 20, "cached_input_tokens": 15, "output_tokens": 5},
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # The adapter ships disabled (#2026). These tests exercise the guarded
    # implementation, so they lift the guard explicitly for this test only.
    monkeypatch.setattr(client, "TOOL_DENY_ALL_SUPPORTED", True)
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    env = {"HOME": str(tmp_path), "PATH": str(binary.parent), "UA_CODEX_CLI_BIN": str(binary)}
    calls = []

    def runner(invocation):
        calls.append(invocation)
        if "login" in invocation.argv:
            return client.CLIResult(0, "", "Logged in using ChatGPT")
        return client.CLIResult(0, stream(), "")

    return env, tmp_path / "runs", calls, runner


def test_subscription_invocation_and_private_prompt_transport(setup):
    env, base, calls, run = setup
    result = client.CodexAdapter().complete(
        "PRIVATE-USER", system="PRIVATE-SYSTEM", run=run, base_dir=base, env=env
    )
    assert result.ok and result.provider == "codex_cli"
    assert result.requested_model == "gpt-6-luna" and result.model == "unknown"
    assert len(calls) == 2
    argv = calls[-1].argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert "--ephemeral" in argv and "--json" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in argv
    assert 'model_provider="openai"' in argv
    assert "features.shell_tool=false" in argv
    assert "features.unified_exec=false" in argv
    assert "features.hooks=false" in argv and "features.plugins=false" in argv
    assert "features.apps=false" in argv and "features.multi_agent=false" in argv
    assert not any("PRIVATE" in arg for arg in argv)
    assert calls[-1].stdin == "PRIVATE-USER"
    assert calls[-1].env == calls[0].env
    assert not calls[-1].cwd.exists()


def test_minimal_environment_and_auth_snapshot(setup):
    env, base, calls, run = setup
    for key in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENROUTER_API_KEY",
        "UA_AGENT_TOKENS",
        "SLACK_BOT_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        env[key] = "synthetic-secret"
    env["CODEX_HOME"] = str(base.parent / ".codex")
    client.CodexAdapter().complete("prompt", run=run, base_dir=base, env=env)
    for invocation in calls:
        assert invocation.env["CODEX_HOME"] == env["CODEX_HOME"]
        assert not any("KEY" in key or "TOKEN" in key for key in invocation.env)
        assert "synthetic-secret" not in repr(invocation)


@pytest.mark.parametrize(
    "status", ["Logged in using an API key: SECRET", "Not logged in", "unrecognized"]
)
def test_non_chatgpt_auth_never_generates(setup, status):
    env, base, calls, _ = setup

    def run(inv):
        calls.append(inv)
        return client.CLIResult(0, "", status)

    result = client.CodexAdapter().complete("prompt", run=run, base_dir=base, env=env)
    assert not result.ok and result.error == "not_subscription"
    assert len(calls) == 1 and "SECRET" not in repr(result)


def test_auth_is_fresh_for_every_generation(setup):
    env, base, calls, run = setup
    adapter = client.CodexAdapter()
    adapter.complete("a", run=run, base_dir=base, env=env)
    adapter.complete("b", run=run, base_dir=base, env=env)
    assert len([c for c in calls if "login" in c.argv]) == 2


@pytest.mark.parametrize("raw", ["inf", "nan", "-1", "bogus", "0"])
def test_timeout_invalid_values_are_bounded(setup, raw):
    env, base, calls, run = setup
    env["UA_CODEX_CLI_TIMEOUT_S"] = raw
    client.CodexAdapter().complete("a", run=run, base_dir=base, env=env)
    assert calls[-1].timeout_s == client.DEFAULT_TIMEOUT_S


def test_timeout_hard_cap(setup):
    env, base, calls, run = setup
    env["UA_CODEX_CLI_TIMEOUT_S"] = "1000000000"
    client.CodexAdapter().complete("a", run=run, base_dir=base, env=env)
    assert calls[-1].timeout_s == client.MAX_TIMEOUT_S


def test_pinned_haiku_excludes_codex_without_auth_or_generation(setup):
    env, base, calls, run = setup
    result = client.CodexAdapter().complete(
        "p", pinned_model="claude-haiku-5-5", run=run, base_dir=base, env=env
    )
    assert not result.ok and result.error == "model_pin_excludes_codex" and not calls


@pytest.mark.parametrize(
    "model",
    ["claude-haiku-5-5", "anthropic/claude-haiku-5.5", "openrouter/gpt-6-luna", "bad\nmodel"],
)
def test_non_native_model_rejected_before_generation(setup, model):
    env, base, calls, run = setup
    env["UA_CODEX_CLI_MODEL"] = model
    result = client.CodexAdapter().complete("p", run=run, base_dir=base, env=env)
    assert not result.ok and result.error == "invalid_model" and not calls


def test_observed_model_and_cache_tokens_are_truthful():
    result = client.parse_result(client.CLIResult(0, stream(model="gpt-6-luna"), ""), "gpt-6-luna")
    assert result.ok and result.model == "gpt-6-luna"
    assert (
        result.input_tokens == 20 and result.cached_input_tokens == 15 and result.output_tokens == 5
    )
    assert result.cost_usd is None


def test_different_observed_model_fails_closed():
    result = client.parse_result(client.CLIResult(0, stream(model="gpt-6-sol"), ""), "gpt-6-luna")
    assert not result.ok and result.error == "model_mismatch"


def test_native_pin_needs_actual_model_evidence(setup):
    env, base, _, run = setup
    result = client.CodexAdapter().complete(
        "p", pinned_model="gpt-6-luna", run=run, base_dir=base, env=env
    )
    assert not result.ok and result.error == "model_evidence_missing"


def test_schema_file_is_private_and_validated(setup):
    env, base, calls, original = setup
    schema = {
        "type": "object",
        "properties": {"verdict": {"enum": ["DUPLICATE"]}, "reason": {"type": "string"}},
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    }

    def run(inv):
        if "--output-schema" in inv.argv:
            path = Path(inv.argv[inv.argv.index("--output-schema") + 1])
            assert path.stat().st_mode & 0o777 == 0o600
            assert json.loads(path.read_text()) == schema
        return original(inv)

    result = client.CodexAdapter().complete(
        "p", response_schema=schema, run=run, base_dir=base, env=env
    )
    assert result.ok


def test_schema_mismatch_and_nested_text_fail_closed():
    for text in ('{"wrong":1}', '{"x":' + "[" * 20000 + "0" + "]" * 20000 + "}"):
        result = client.parse_result(
            client.CLIResult(0, stream(text), ""),
            "gpt-6-luna",
            {"type": "object", "required": ["verdict"]},
        )
        assert not result.ok and result.error == "schema_mismatch"


@pytest.mark.parametrize("stdout", ["not-json", '{"type":', "[" * 20000])
def test_malformed_stream_fails_closed(stdout):
    assert (
        client.parse_result(client.CLIResult(0, stdout, ""), "gpt-6-luna").error
        == "malformed_reply"
    )


def test_tool_events_are_not_accepted_as_a_completion():
    payload = (
        json.dumps(
            {"type": "item.completed", "item": {"type": "command_execution", "command": "private"}}
        )
        + "\n"
        + stream()
    )
    result = client.parse_result(client.CLIResult(0, payload, ""), "gpt-6-luna")
    assert not result.ok and result.error == "unexpected_tool"


def test_quota_failure_cools_down_without_repeated_auth_or_generation(setup):
    env, base, calls, original = setup
    now = [10.0]

    def run(inv):
        if "login" in inv.argv:
            return original(inv)
        calls.append(inv)
        return client.CLIResult(
            1,
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "You have hit your usage limit. PRIVATE-PROMPT"},
                }
            ),
            "",
        )

    adapter = client.CodexAdapter(clock=lambda: now[0], cooldown_s=1800)
    first = adapter.complete("p", run=run, base_dir=base, env=env)
    assert first.error == "capacity" and "PRIVATE" not in repr(first)
    second = adapter.complete("p", run=run, base_dir=base, env=env)
    assert second.error == "capacity_cooldown" and len(calls) == 2
    now[0] = 1811
    assert adapter.complete("p", run=original, base_dir=base, env=env).ok


def test_runner_timeout_is_classified_without_prompt_disclosure(setup):
    env, base, _, _ = setup

    def run(inv):
        raise subprocess.TimeoutExpired("private-command", 1, output="PRIVATE")

    result = client.CodexAdapter().complete("p", run=run, base_dir=base, env=env)
    assert not result.ok and result.error == "timeout" and "PRIVATE" not in repr(result)


def test_cancellation_propagates_and_private_directory_is_removed(setup):
    env, base, calls, _ = setup

    def run(inv):
        calls.append(inv)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        client.CodexAdapter().complete("p", run=run, base_dir=base, env=env)
    assert not calls[0].cwd.exists()


def test_missing_binary_and_unsafe_base_do_not_spawn(setup):
    env, base, calls, run = setup
    env["UA_CODEX_CLI_BIN"] = str(base / "missing")
    assert (
        client.CodexAdapter().complete("p", run=run, base_dir=base, env=env).error
        == "missing_binary"
    )
    assert not calls


def test_conflicting_auth_status_fails_closed(setup):
    env, base, calls, _ = setup

    def run(inv):
        calls.append(inv)
        return client.CLIResult(0, "Logged in using ChatGPT\nLogged in using an API key", "")

    result = client.CodexAdapter().complete("p", run=run, base_dir=base, env=env)
    assert result.error == "not_subscription" and len(calls) == 1


def test_unexpected_observed_provider_model_fails_closed():
    result = client.parse_result(
        client.CLIResult(0, stream(model="claude-haiku-5-5"), ""), "gpt-6-luna"
    )
    assert result.error == "model_mismatch"


def test_remote_schema_reference_is_not_fetched(monkeypatch):
    import urllib.request

    def never_fetch(*args, **kwargs):
        raise AssertionError("no remote schema reads")

    monkeypatch.setattr(urllib.request, "urlopen", never_fetch)
    result = client.parse_result(
        client.CLIResult(0, stream(), ""), "gpt-6-luna", {"$ref": "https://example.invalid/private"}
    )
    assert result.error == "schema_mismatch"


def test_unknown_usage_is_not_fabricated_as_zero():
    result = client.parse_result(
        client.CLIResult(0, stream(usage={"input_tokens": -1, "output_tokens": True}), ""),
        "gpt-6-luna",
    )
    assert result.ok and result.input_tokens is None and result.output_tokens is None


def test_unsafe_repository_ancestry_refused_before_auth(setup):
    env, base, calls, run = setup
    (base.parent / ".git").mkdir()
    result = client.CodexAdapter().complete("p", run=run, base_dir=base, env=env)
    assert result.error == "unsafe_workdir" and not calls


def test_capacity_error_code_sets_backoff():
    raw = json.dumps({"type": "turn.failed", "error": {"code": "insufficient_quota"}})
    result = client.parse_result(client.CLIResult(1, raw, ""), "gpt-6-luna")
    assert result.error == "capacity"


def test_shipped_guard_is_closed():
    assert client.TOOL_DENY_ALL_SUPPORTED is False
    assert client.DISABLED_REASON == "tool_deny_all_unsupported"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"system": "s", "response_schema": {"type": "object"}},
        {"pinned_model": "gpt-6-luna"},
    ],
)
def test_disabled_adapter_refuses_before_any_process_is_spawned(tmp_path, monkeypatch, kwargs):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    env = {"HOME": str(tmp_path), "PATH": str(binary.parent), "UA_CODEX_CLI_BIN": str(binary)}
    spawned = []

    def forbidden(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("the disabled adapter must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(client, "_binary", forbidden)

    def runner(invocation):
        spawned.append(invocation)
        raise AssertionError("the disabled adapter must not call its runner")

    base = tmp_path / "runs"
    result = client.CodexAdapter().complete("p", run=runner, base_dir=base, env=env, **kwargs)
    assert not result.ok and result.error == client.DISABLED_REASON
    assert result.text is None and not spawned and not base.exists()


@pytest.mark.parametrize(
    "stderr", ["ERROR: usage limit reached", "You've hit your usage limit. Try again later."]
)
def test_stderr_only_usage_limit_is_capacity_and_cools_down(setup, stderr):
    env, base, _, original = setup
    now = [10.0]
    generations = []

    def run(inv):
        if "login" in inv.argv:
            return original(inv)
        generations.append(inv)
        return client.CLIResult(1, "", stderr + " PRIVATE-PROMPT")

    adapter = client.CodexAdapter(clock=lambda: now[0], cooldown_s=60)
    first = adapter.complete("p", run=run, base_dir=base, env=env)
    assert first.error == "capacity" and "PRIVATE" not in repr(first)
    second = adapter.complete("p", run=run, base_dir=base, env=env)
    assert second.error == "capacity_cooldown" and len(generations) == 1


def test_stderr_without_usage_limit_stays_provider_error():
    for stderr in ("ERROR: rate limit reached, retry in 2s", "ERROR: connection reset"):
        result = client.parse_result(client.CLIResult(1, "", stderr), "gpt-6-luna")
        assert result.error == "provider_error"


def test_stderr_usage_text_does_not_fail_a_completed_turn():
    result = client.parse_result(
        client.CLIResult(0, stream(), "warning: usage limit reached soon"), "gpt-6-luna"
    )
    assert result.ok


DRAFT4_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "number",
    "minimum": 2,
    "exclusiveMinimum": True,
}


def test_non_2020_12_schema_dialect_is_rejected_before_any_call(setup):
    env, base, calls, run = setup
    result = client.CodexAdapter().complete(
        "p", response_schema=DRAFT4_SCHEMA, run=run, base_dir=base, env=env
    )
    assert not result.ok and result.error == "unsupported_schema_dialect" and not calls


def test_parse_result_rejects_the_same_dialect_as_preflight():
    result = client.parse_result(client.CLIResult(0, stream("3"), ""), "gpt-6-luna", DRAFT4_SCHEMA)
    assert result.error == "unsupported_schema_dialect"


def test_declared_2020_12_schema_is_accepted(setup):
    env, base, calls, run = setup
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["verdict"],
    }
    result = client.CodexAdapter().complete(
        "p", response_schema=schema, run=run, base_dir=base, env=env
    )
    assert result.ok and any("--output-schema" in c.argv for c in calls)


def _capacity_run(original, generations):
    def run(inv):
        if "login" in inv.argv:
            return original(inv)
        generations.append(inv.env["CODEX_HOME"])
        return client.CLIResult(
            1, json.dumps({"type": "turn.failed", "error": {"code": "insufficient_quota"}}), ""
        )

    return run


def test_cooldown_cap_evicts_expired_entries_before_active_ones(setup):
    env, base, _, original = setup
    calls = []
    now = [0.0]
    adapter = client.CodexAdapter(clock=lambda: now[0], cooldown_s=100)
    run = _capacity_run(original, calls)

    def fail(index):
        assert (
            adapter.complete(
                "p", run=run, base_dir=base, env={**env, "CODEX_HOME": f"/h/{index}"}
            ).error
            == "capacity"
        )

    fail(0)  # expires at 100
    now[0] = 50.0
    for index in range(1, 32):
        fail(index)  # expire at 150
    now[0] = 120.0
    fail(32)  # 33rd context: only the expired one may go
    calls.clear()
    for index in range(1, 33):
        blocked = adapter.complete(
            "p", run=run, base_dir=base, env={**env, "CODEX_HOME": f"/h/{index}"}
        )
        assert blocked.error == "capacity_cooldown"
    assert not calls


def test_cooldown_cap_evicts_only_the_oldest_active_entry(setup):
    env, base, _, original = setup
    calls = []
    now = [0.0]
    adapter = client.CodexAdapter(clock=lambda: now[0], cooldown_s=300)
    run = _capacity_run(original, calls)
    for index in range(33):
        now[0] = float(index)
        result = adapter.complete(
            "p", run=run, base_dir=base, env={**env, "CODEX_HOME": f"/h/{index}"}
        )
        assert result.error == "capacity"
    calls.clear()
    for index in range(1, 33):
        blocked = adapter.complete(
            "p", run=run, base_dir=base, env={**env, "CODEX_HOME": f"/h/{index}"}
        )
        assert blocked.error == "capacity_cooldown"
    assert not calls
    oldest = adapter.complete("p", run=run, base_dir=base, env={**env, "CODEX_HOME": "/h/0"})
    assert oldest.error == "capacity" and calls == ["/h/0"]


def test_generic_jsonl_error_with_stderr_usage_limit_is_capacity(setup):
    env, base, _, original = setup
    now = [10.0]
    generations = []

    def run(inv):
        if "login" in inv.argv:
            return original(inv)
        generations.append(inv)
        return client.CLIResult(
            1,
            json.dumps({"type": "turn.failed", "error": {"message": "request failed"}}),
            "ERROR: usage limit reached PRIVATE-PROMPT",
        )

    adapter = client.CodexAdapter(clock=lambda: now[0], cooldown_s=60)
    first = adapter.complete("p", run=run, base_dir=base, env=env)
    assert first.error == "capacity" and "PRIVATE" not in repr(first)
    second = adapter.complete("p", run=run, base_dir=base, env=env)
    assert second.error == "capacity_cooldown" and len(generations) == 1


def test_generic_jsonl_error_without_stderr_usage_limit_stays_provider_error():
    raw = json.dumps({"type": "turn.failed", "error": {"message": "request failed"}})
    result = client.parse_result(client.CLIResult(1, raw, "ERROR: connection reset"), "gpt-6-luna")
    assert result.error == "provider_error"


@pytest.mark.parametrize("declared", [[], {}, 4, None, "http://json-schema.org/draft-07/schema#"])
def test_malformed_or_unsupported_schema_dialect_is_refused_everywhere(setup, declared):
    env, base, calls, run = setup
    schema = {"$schema": declared, "type": "object"}
    result = client.CodexAdapter().complete(
        "p", response_schema=schema, run=run, base_dir=base, env=env
    )
    assert not result.ok and result.error == "unsupported_schema_dialect" and not calls
    parsed = client.parse_result(client.CLIResult(0, stream(), ""), "gpt-6-luna", schema)
    assert parsed.error == "unsupported_schema_dialect"
