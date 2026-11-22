"""The claude_cli leg: a headless Claude Code session on the subscription (task #2025).

Every test runs a fake `claude` executable written to tmp_path, never the real
CLI: conftest stubs the PATH/home search, and these tests point
UA_CLAUDE_CLI_BIN at the fake. The fake records each invocation (argv, cwd,
the cwd's contents, environment, stdin, the system-prompt file) to a JSONL
file. `claude --help` answers per FAKE_CLAUDE_HELP, `claude auth status --json`
per FAKE_CLAUDE_AUTH, and a completion (`-p`) per FAKE_CLAUDE_MODE.
"""

import json
import math
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from unified_agent import config, consolidate, llm_client, storage

# Captured at import, before conftest's isolated_env stubs it out per test.
_REAL_SEARCH = llm_client._search_claude_cli
_REAL_ENV_ALLOW = llm_client._CLAUDE_CLI_ENV_ALLOW
# The fake reads its instructions from these; the leg's allowlist would drop
# them, so the fixture lets exactly these through as well.
_FAKE_KEYS = frozenset(
    {
        "FAKE_CLAUDE_RECORD",
        "FAKE_CLAUDE_MODE",
        "FAKE_CLAUDE_AUTH",
        "FAKE_CLAUDE_HELP",
        "FAKE_CLAUDE_MODELS",
        "FAKE_CLAUDE_CHILD_PID",
        "FAKE_CLAUDE_RESULT",
    }
)

PRIVATE_PROMPT = "PRIVATE-NOTE-BODY: the quarterly numbers are not public yet"
PRIVATE_SYSTEM = "SYSTEM-SECRET: adjudicate these notes tersely"

_FAKE_CLAUDE = """#!{python}
import json, os, stat, subprocess, sys, time

argv = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
auth = os.environ.get("FAKE_CLAUDE_AUTH", "subscription")
record = {{
    "argv": argv,
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(".")),
    "env": dict(os.environ),
}}
if "--system-prompt-file" in argv:
    path = argv[argv.index("--system-prompt-file") + 1]
    record["system_file"] = path
    record["system_text"] = open(path, encoding="utf-8").read()
    record["system_mode"] = stat.S_IMODE(os.stat(path).st_mode)

def save(rec):
    with open(os.environ["FAKE_CLAUDE_RECORD"], "a") as fh:
        fh.write(json.dumps(rec) + "\\n")

if argv == ["--help"]:
    save(record)
    help_mode = os.environ.get("FAKE_CLAUDE_HELP", "full")
    if help_mode == "fail":
        sys.exit(1)
    flags = ["-p, --print", "--model <model>", "--output-format <format>",
             "--tools <tools...>", "--strict-mcp-config", "--no-session-persistence",
             "--setting-sources <sources>", "--safe-mode", "--disable-slash-commands",
             "--permission-prompts <mode>", "--system-prompt-file <file>"]
    if help_mode == "no_safe_mode":
        flags.remove("--safe-mode")
    elif help_mode == "no_permission_prompts":
        flags.remove("--permission-prompts <mode>")
    elif help_mode == "lookalike":
        flags = [f.replace("--safe-mode", "--safe-mode-legacy") for f in flags]
    print("Usage: claude [options] [command] [prompt]")
    print("Options:")
    for flag in flags:
        print("  " + flag)
    sys.exit(0)

if argv[:2] == ["auth", "status"]:
    save(record)
    base = {{"email": "someone@example.invalid", "orgId": "org-SECRET-ID",
            "orgName": "Secret Org Name", "configDirectory": "/x"}}
    if auth == "subscription":
        out = {{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
               "subscriptionType": "max", **base}}
    elif auth == "console":
        out = {{"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty",
               "subscriptionType": None, **base}}
    elif auth == "api_key":
        out = {{"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty", **base}}
    elif auth == "bedrock":
        out = {{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock", **base}}
    elif auth == "logged_out":
        out = {{"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}}
    elif auth == "garbage":
        print("not json")
        sys.exit(0)
    elif auth == "fail":
        sys.exit(1)
    print(json.dumps(out))
    sys.exit(0)

record["stdin"] = sys.stdin.read()
save(record)
usage = {{"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 100,
         "cache_creation_input_tokens": 7}}
requested = argv[argv.index("--model") + 1] if "--model" in argv else "unknown"
models = os.environ.get("FAKE_CLAUDE_MODELS", requested)
model_usage = {{}} if models == "none" else {{m: {{"inputTokens": 1}} for m in models.split(",")}}
if mode == "ok":
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": "hi from claude", "usage": usage, "total_cost_usd": 0.0042}}
    if model_usage:
        out["modelUsage"] = model_usage
elif mode == "fenced_json":
    body = '{{"verdict": "DUPLICATE", "reason": "same fact"}}'
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": "```json\\n" + body + "\\n```", "usage": usage}}
elif mode == "result_env":
    # The raw `result` string comes verbatim from FAKE_CLAUDE_RESULT (#2022).
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": os.environ["FAKE_CLAUDE_RESULT"], "usage": usage}}
elif mode == "not_json_reply":
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": "VERDICT: DUPLICATE", "usage": usage}}
elif mode == "login":
    out = {{"type": "result", "subtype": "success", "is_error": True,
           "result": "Not logged in - Please run /login. " + record["stdin"]}}
    print(json.dumps(out))
    sys.exit(1)
elif mode == "usage_limit":
    out = {{"type": "result", "subtype": "success", "is_error": True,
           "result": "Claude AI usage limit reached"}}
elif mode == "empty":
    out = {{"type": "result", "subtype": "success", "is_error": False, "result": "  "}}
elif mode == "garbage":
    print("not json at all " + record["stdin"])
    sys.exit(0)
elif mode == "deep_json":
    sys.stdout.write("[" * 200000 + "]" * 200000)
    sys.exit(0)
elif mode == "oversized":
    sys.stdout.write('{{"result": "' + "x" * (2 * 1024 * 1024) + '"}}')
    sys.exit(0)
elif mode == "stderr_flood":
    sys.stderr.write("e" * (512 * 1024))
    sys.stderr.flush()
    time.sleep(60)
    sys.exit(0)
elif mode == "early_exit":
    # Print a valid result, leave a child holding the inherited stdout, exit.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    with open(os.environ["FAKE_CLAUDE_CHILD_PID"], "w") as fh:
        fh.write(str(child.pid))
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": "hi from claude", "usage": usage, "modelUsage": model_usage}}
    print(json.dumps(out))
    sys.stdout.flush()
    os._exit(0)
elif mode == "background_child":
    # A descendant with its stdio detached from the CLI's pipes, so EOF on
    # them arrives at once and the run ends normally while it lives on.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with open(os.environ["FAKE_CLAUDE_CHILD_PID"], "w") as fh:
        fh.write(str(child.pid))
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": "hi from claude", "usage": usage, "modelUsage": model_usage}}
    print(json.dumps(out))
    sys.stdout.flush()
    os._exit(0)
elif mode == "nested_result":
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "result": '{{"a":' * 50000 + "1" + "}}" * 50000, "usage": usage}}
elif mode == "crash":
    print("error: unknown option " + record["stdin"], file=sys.stderr)
    sys.exit(2)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_CLAUDE_CHILD_PID"], "w") as fh:
        fh.write(str(child.pid))
    time.sleep(60)
    sys.exit(0)
print(json.dumps(out))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Point the leg at a recording fake `claude`; returns a reader for its calls."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(_FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    record = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("UA_CLAUDE_CLI_BIN", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "subscription")
    monkeypatch.setenv("FAKE_CLAUDE_HELP", "full")
    monkeypatch.setattr(llm_client, "_CLAUDE_CLI_ENV_ALLOW", _REAL_ENV_ALLOW | _FAKE_KEYS)

    def calls() -> list[dict]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines() if line]

    calls.script = script  # type: ignore[attr-defined]
    calls.completions = lambda: [c for c in calls() if c["argv"][:1] == ["-p"]]  # type: ignore[attr-defined]
    calls.auth = lambda: [c for c in calls() if c["argv"][:2] == ["auth", "status"]]  # type: ignore[attr-defined]
    calls.help = lambda: [c for c in calls() if c["argv"] == ["--help"]]  # type: ignore[attr-defined]
    return calls


def _only_claude_cli(monkeypatch):
    monkeypatch.setattr(
        llm_client, "_PROVIDER_CHAIN", [("claude_cli", llm_client._call_claude_cli)]
    )


def _fake_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://example.invalid")
    )


def _openrouter_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: _fake_response(
            {"choices": [{"message": {"content": "hi from openrouter"}}]}
        ),
    )


class TestInvocation:
    def test_argv_flags_prompt_on_stdin_and_parsed_result(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        result = llm_client.complete(PRIVATE_PROMPT, system=PRIVATE_SYSTEM)
        route = result.pop("route")
        assert result == {
            "text": "hi from claude",
            "provider": "claude_cli",
            "model": llm_client.DEFAULT_CLAUDE_CLI_MODEL,
        }
        assert route[0]["target"] == "claude_cli" and route[0]["ok"] is True

        [call] = fake_claude.completions()
        argv = call["argv"]
        system_file = call["system_file"]
        assert argv == [
            "-p",
            "--model",
            "claude-haiku-5-5",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources",
            "local",
            "--safe-mode",
            "--disable-slash-commands",
            "--permission-prompts",
            "none",
            "--system-prompt-file",
            system_file,
        ]
        assert "--bare" not in argv
        assert "--system-prompt" not in argv
        sources = argv[argv.index("--setting-sources") + 1].split(",")
        assert "user" not in sources and "project" not in sources
        # The prompt travels on stdin and the system prompt in a 0600 file,
        # never argv (ps exposure, arg length).
        assert call["stdin"] == PRIVATE_PROMPT
        assert not any(PRIVATE_PROMPT in arg or PRIVATE_SYSTEM in arg for arg in argv)
        assert call["system_text"] == PRIVATE_SYSTEM
        assert call["system_mode"] == 0o600
        assert not Path(system_file).exists()

    def test_model_override_from_env(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("UA_CLAUDE_CLI_MODEL", "claude-sonnet-5-5")
        result = llm_client.complete("prompt")
        assert result["model"] == "claude-sonnet-5-5"
        argv = fake_claude.completions()[0]["argv"]
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"

    def test_memory_pin_uses_native_haiku_without_openrouter_fallback(
        self, fake_claude, monkeypatch
    ):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("UA_MEMORY_PROVIDER", "claude_cli")
        monkeypatch.setenv("UA_MEMORY_MODEL", "claude-haiku-5-5")
        monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-only-not-used")
        result = llm_client.complete("prompt", memory_agent=True)
        assert result["provider"] == "claude_cli"
        assert result["model"] == "claude-haiku-5-5"
        assert [attempt["target"] for attempt in result["route"]] == ["claude_cli"]
        argv = fake_claude.completions()[0]["argv"]
        assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"

    def test_default_system_prompt_replaces_the_agent_prompt(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        llm_client.complete("prompt")
        assert fake_claude.completions()[0]["system_text"] == llm_client.DEFAULT_CLAUDE_CLI_SYSTEM

    def test_runs_in_an_empty_private_dir_under_ua_home_that_is_removed(
        self, fake_claude, monkeypatch
    ):
        _only_claude_cli(monkeypatch)
        llm_client.complete("prompt")
        [call] = fake_claude.completions()
        cwd = Path(call["cwd"])
        base = (config.data_home() / "tmp" / "claude-cli").resolve()
        assert cwd.resolve().is_relative_to(base)
        # The system-prompt file sits beside the cwd, not in it.
        assert call["cwd_entries"] == []
        assert Path(call["system_file"]).parent == cwd.parent
        assert not cwd.parent.exists()
        assert stat.S_IMODE(base.stat().st_mode) == 0o700

    def test_refuses_a_run_dir_inside_a_git_checkout(self, fake_claude, tmp_path, monkeypatch):
        repo = tmp_path / "checkout"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("UA_HOME", str(repo / "ua_home"))
        _openrouter_answers(monkeypatch)
        result = llm_client.complete("prompt")
        assert result["provider"] == "openrouter"
        by_target = {a["target"]: a for a in result["route"]}
        assert by_target["claude_cli"]["error"] == "UnsafeWorkdir"
        assert fake_claude() == []

    def test_child_env_is_a_minimal_allowlist(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        leaked = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://proxy.example",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_SIMPLE": "1",
            "CLAUDECODE": "1",
            "UA_AGENT_TOKENS": "label:secret-token",
            "SLACK_BOT_TOKEN": "xoxb-secret",
            "OPENROUTER_API_KEY": "sk-or-secret",
            "GEMINI_API_KEY": "gem-secret",
            "UNRELATED_SETTING": "x",
            # A `claude setup-token` credential reports authMethod
            # oauth_token, which the preflight refuses: not forwarded.
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
        }
        for key, value in leaked.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg/claude")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("TMPDIR", "/shared/tmp")
        llm_client.complete("prompt")
        calls = fake_claude()
        # The --help probe and the auth preflight see the same env.
        assert [c["argv"][:1] for c in calls] == [["--help"], ["auth"], ["-p"]]
        for call in calls:
            env = call["env"]
            assert not set(leaked) & set(env)
            extra = {k for k in env if not k.startswith("LC_")} - _REAL_ENV_ALLOW
            # macOS injects this harmless encoding marker into child processes;
            # keep it out of the provider allowlist while recognizing it here.
            assert extra <= {"TMPDIR", "__CF_USER_TEXT_ENCODING"} | _FAKE_KEYS, extra
            assert env["CLAUDE_CONFIG_DIR"] == "/cfg/claude"
            assert env["LANG"] == "en_US.UTF-8" and env["LC_ALL"] == "en_US.UTF-8"
            assert "HOME" in env and "PATH" in env
            # TMPDIR is the run's own private dir, not the shared one.
            tmpdir = Path(env["TMPDIR"])
            assert tmpdir.parent == Path(call["cwd"]).parent
            assert tmpdir.name == "tmp"

    def test_oauth_token_env_is_not_forwarded(self):
        """`claude setup-token` logins report authMethod oauth_token, which the
        preflight refuses; forwarding the token would only invite that."""
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in _REAL_ENV_ALLOW
        env = llm_client.claude_cli_child_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": "/bin"})
        assert env == {"PATH": "/bin"}

    def test_relative_bin_resolves_to_an_absolute_path(self, fake_claude, tmp_path, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("UA_CLAUDE_CLI_BIN", "bin/claude")
        resolved = llm_client.resolve_claude_cli()
        assert resolved == str(fake_claude.script)
        assert Path(resolved).is_absolute()
        assert llm_client.complete("prompt")["provider"] == "claude_cli"

    def test_records_usage_with_null_cost(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        llm_client.complete("prompt")
        conn = storage.connect()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
        finally:
            conn.close()
        [row] = rows
        assert row["provider"] == "claude_cli"
        assert row["model"] == llm_client.DEFAULT_CLAUDE_CLI_MODEL
        # Input counts cache reads and writes too (12 + 100 + 7).
        # total_cost_usd is an API-equivalent estimate; the preflight proved
        # subscription auth, so nothing was charged per call.
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (119, 5, None)


class TestModelEvidence:
    """The recorded model comes from the CLI's own report, matched exactly."""

    def test_exactly_the_requested_model_is_recorded_as_itself(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "claude-haiku-5-5")
        assert llm_client.complete("prompt")["model"] == "claude-haiku-5-5"

    def test_a_mix_is_recorded_as_the_mix_never_as_the_requested_model(
        self, fake_claude, monkeypatch
    ):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "claude-sonnet-5-5,claude-haiku-5-5")
        result = llm_client.complete("prompt")
        assert result["model"] == "claude-haiku-5-5+claude-sonnet-5-5"
        conn = storage.connect()
        try:
            [row] = conn.execute("SELECT model FROM usage_events").fetchall()
        finally:
            conn.close()
        assert row["model"] == "claude-haiku-5-5+claude-sonnet-5-5"

    @pytest.mark.parametrize(
        "reported", ["claude-opus-5-5", "claude-haiku-5-5-20261001", "claude-haiku-5"]
    )
    def test_a_report_without_the_exact_id_fails_closed(self, fake_claude, monkeypatch, reported):
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", reported)
        _openrouter_answers(monkeypatch)
        result = llm_client.complete("prompt")
        assert result["provider"] == "openrouter"
        failed = result["route"][0]
        assert (failed["error"], failed["reason"]) == ("ModelMismatch", "model mismatch")

    def test_an_alias_is_not_evidence_for_a_full_id(self, fake_claude, monkeypatch):
        monkeypatch.setenv("UA_CLAUDE_CLI_MODEL", "haiku")
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "claude-haiku-5-5")
        _openrouter_answers(monkeypatch)
        assert llm_client.complete("prompt")["route"][0]["reason"] == "model mismatch"

    def test_no_model_evidence_is_recorded_as_unknown(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "none")
        assert llm_client.complete("prompt")["model"] == "unknown"
        conn = storage.connect()
        try:
            [row] = conn.execute("SELECT model FROM usage_events").fetchall()
        finally:
            conn.close()
        assert row["model"] == "unknown"

    @pytest.mark.parametrize("reported", ["none", "claude-haiku-5-5,claude-sonnet-5-5"])
    def test_pinned_model_requires_exact_single_model_evidence(
        self, fake_claude, monkeypatch, reported
    ):
        monkeypatch.setenv("UA_CLAUDE_CLI_MODEL", "claude-haiku-5-5")
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", reported)
        _openrouter_answers(monkeypatch)

        result = llm_client.complete("prompt")

        assert result["provider"] == "openrouter"
        failed = result["route"][0]
        assert (failed["error"], failed["reason"]) == ("ModelMismatch", "model mismatch")
        assert len(fake_claude.completions()) == 1

    @pytest.mark.parametrize("reported", ["none", "claude-haiku-5-5,claude-sonnet-5-5"])
    def test_model_override_requires_exact_single_model_evidence(
        self, fake_claude, monkeypatch, reported
    ):
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", reported)
        assert (
            llm_client._call_claude_cli("prompt", None, model_override="claude-haiku-5-5") is None
        )
        assert len(fake_claude.completions()) == 1


def _usage_rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
    finally:
        conn.close()


class TestRejectedCompletionUsage:
    """Task #2025 review: a completion the leg rejects still used subscription
    capacity. The ledger keeps it, with the model the CLI reported, so a later
    paid fallback cannot hide it; an accepted completion is recorded once."""

    def test_schema_rejected_completion_records_its_usage(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "not_json_reply")
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        assert exc_info.value.route[0]["reason"] == "schema mismatch"
        [row] = _usage_rows()
        # The fake reports no modelUsage in this mode: no evidence is "unknown".
        assert (row["provider"], row["model"]) == ("claude_cli", "unknown")
        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (119, 5, None)

    def test_model_mismatch_records_usage_under_the_reported_model(self, fake_claude, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "claude-opus-5-5")
        _openrouter_answers(monkeypatch)
        result = llm_client.complete("prompt")
        assert result["provider"] == "openrouter"
        rows = {row["provider"]: row for row in _usage_rows()}
        assert set(rows) == {"claude_cli", "openrouter"}
        cli = rows["claude_cli"]
        assert cli["model"] == "claude-opus-5-5"
        assert (cli["input_tokens"], cli["output_tokens"], cli["cost_usd"]) == (119, 5, None)

    def test_pinned_mixed_evidence_rejection_records_the_mix(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("UA_CLAUDE_CLI_MODEL", "claude-haiku-5-5")
        monkeypatch.setenv("FAKE_CLAUDE_MODELS", "claude-haiku-5-5,claude-sonnet-5-5")
        with pytest.raises(llm_client.LLMConnectionError):
            llm_client.complete("prompt")
        [row] = _usage_rows()
        assert row["model"] == "claude-haiku-5-5+claude-sonnet-5-5"

    def test_error_result_without_usage_adds_no_row(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit")
        with pytest.raises(llm_client.LLMConnectionError):
            llm_client.complete("prompt")
        assert _usage_rows() == []

    def test_accepted_schema_completion_is_recorded_exactly_once(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "fenced_json")
        result = llm_client.complete(
            "prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA
        )
        assert result["provider"] == "claude_cli"
        [row] = _usage_rows()
        assert (row["input_tokens"], row["output_tokens"]) == (119, 5)


class TestSubscriptionPreflight:
    @pytest.mark.parametrize(
        "auth", ["console", "api_key", "bedrock", "logged_out", "garbage", "fail"]
    )
    def test_non_subscription_auth_fails_closed_without_a_completion(
        self, fake_claude, monkeypatch, auth
    ):
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", auth)
        _openrouter_answers(monkeypatch)
        result = llm_client.complete(PRIVATE_PROMPT)
        assert result["provider"] == "openrouter"
        failed = result["route"][0]
        assert failed["target"] == "claude_cli"
        assert failed["reason"] == "claude_cli_not_subscription"
        assert fake_claude.completions() == []
        assert [c["argv"] for c in fake_claude.auth()] == [["auth", "status", "--json"]]
        dumped = json.dumps(result["route"])
        assert "SECRET" not in dumped and "example.invalid" not in dumped

    def test_generation_preflight_is_fresh_for_each_request(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        llm_client.complete("one")
        llm_client.complete("two")
        assert len(fake_claude.auth()) == 2
        assert len(fake_claude.completions()) == 2

    def test_diagnostic_preflight_remains_cached(self, fake_claude):
        base = llm_client._claude_cli_run_base()
        assert llm_client._claude_cli_auth(str(fake_claude.script), base)["ok"]
        assert llm_client._claude_cli_auth(str(fake_claude.script), base)["ok"]
        assert len(fake_claude.auth()) == 1

    def test_generation_rejects_keychain_auth_change_without_file_change(
        self, fake_claude, monkeypatch, tmp_path
    ):
        _only_claude_cli(monkeypatch)
        config_dir = tmp_path / "claude-profile"
        config_dir.mkdir()
        credentials = config_dir / ".credentials.json"
        credentials.write_text('{"fixture": "unchanged"}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "subscription")

        assert llm_client._call_claude_cli("first", None) is not None
        original_credentials = credentials.read_text()
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "console")
        assert llm_client._call_claude_cli("second", None) is None

        assert credentials.read_text() == original_credentials
        assert len(fake_claude.auth()) == 2
        assert len(fake_claude.completions()) == 1

    def test_cached_failure_expires_sooner_than_success(self):
        assert llm_client.CLAUDE_CLI_AUTH_FAIL_TTL_S < llm_client.CLAUDE_CLI_AUTH_TTL_S <= 600

    def test_auth_cache_is_separated_by_home_and_cli_profile(self, fake_claude, tmp_path):
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "UA_CLAUDE_CLI_BIN": str(fake_claude.script),
            "FAKE_CLAUDE_RECORD": os.environ["FAKE_CLAUDE_RECORD"],
            "FAKE_CLAUDE_HELP": "full",
        }
        first = {
            **base_env,
            "HOME": str(tmp_path / "home-one"),
            "UA_HOME": str(tmp_path / "ua-one"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "profile-one"),
            "FAKE_CLAUDE_AUTH": "subscription",
        }
        second = {
            **base_env,
            "HOME": str(tmp_path / "home-two"),
            "UA_HOME": str(tmp_path / "ua-two"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "profile-two"),
            "FAKE_CLAUDE_AUTH": "console",
        }
        first_base = llm_client._claude_cli_run_base(first)
        second_base = llm_client._claude_cli_run_base(second)
        assert llm_client._claude_cli_auth(str(fake_claude.script), first_base, first)["ok"]
        assert not llm_client._claude_cli_auth(str(fake_claude.script), second_base, second)["ok"]
        assert len(fake_claude.auth()) == 2

    def test_auth_cache_invalidates_after_in_place_credential_change(self, fake_claude, tmp_path):
        config_dir = tmp_path / "claude-profile"
        config_dir.mkdir()
        credentials = config_dir / ".credentials.json"
        credentials.write_text('{"fixture": 1}')
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path / "home"),
            "UA_HOME": str(tmp_path / "ua"),
            "UA_CLAUDE_CLI_BIN": str(fake_claude.script),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "FAKE_CLAUDE_RECORD": os.environ["FAKE_CLAUDE_RECORD"],
            "FAKE_CLAUDE_HELP": "full",
            "FAKE_CLAUDE_AUTH": "subscription",
        }
        base = llm_client._claude_cli_run_base(env)
        assert llm_client._claude_cli_auth(str(fake_claude.script), base, env)["ok"]
        credentials.write_text('{"fixture": 2}')
        os.utime(credentials, ns=(time.time_ns(), time.time_ns()))
        changed = {**env, "FAKE_CLAUDE_AUTH": "console"}
        assert not llm_client._claude_cli_auth(str(fake_claude.script), base, changed)["ok"]
        assert len(fake_claude.auth()) == 2

    def test_cache_is_bounded(self, fake_claude, monkeypatch):
        for i in range(llm_client._CLAUDE_AUTH_CACHE_MAX + 5):
            with llm_client._claude_auth_lock:
                if len(llm_client._claude_auth_cache) >= llm_client._CLAUDE_AUTH_CACHE_MAX:
                    llm_client._claude_auth_cache.clear()
                llm_client._claude_auth_cache[f"/fake/{i}"] = (time.monotonic(), {"ok": True})
        base = llm_client._claude_cli_run_base()
        llm_client._claude_cli_auth(str(fake_claude.script), base)
        assert len(llm_client._claude_auth_cache) <= llm_client._CLAUDE_AUTH_CACHE_MAX

    def test_auth_facts_keep_no_identity_fields(self, fake_claude):
        facts = llm_client._claude_cli_auth(
            str(fake_claude.script), llm_client._claude_cli_run_base()
        )
        assert facts["ok"] is True
        assert "SECRET" not in json.dumps(facts)
        assert "example.invalid" not in json.dumps(facts)
        assert facts["auth_method"] == "claude.ai"
        assert facts["subscription_type"] == "max"


class TestCapabilityProbe:
    """`claude --help` must list the isolation flags the argv relies on."""

    @pytest.mark.parametrize(
        "help_mode", ["no_safe_mode", "no_permission_prompts", "lookalike", "fail"]
    )
    def test_unsupported_cli_fails_closed_without_a_completion(
        self, fake_claude, monkeypatch, help_mode
    ):
        monkeypatch.setenv("FAKE_CLAUDE_HELP", help_mode)
        _openrouter_answers(monkeypatch)
        result = llm_client.complete(PRIVATE_PROMPT)
        assert result["provider"] == "openrouter"
        failed = result["route"][0]
        assert failed["target"] == "claude_cli"
        assert failed["error"] == "UnsupportedCLI"
        assert failed["reason"] == llm_client.CLAUDE_CLI_UNSUPPORTED_REASON
        assert fake_claude.completions() == []

    def test_probe_runs_once_per_binary(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        llm_client.complete("one")
        llm_client.complete("two")
        assert [c["argv"] for c in fake_claude.help()] == [["--help"]]
        assert len(fake_claude.completions()) == 2

    def test_a_replaced_binary_is_probed_again(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        llm_client.complete("one")
        script = fake_claude.script
        script.write_text(script.read_text() + "\n# upgraded\n")
        llm_client.complete("two")
        assert len(fake_claude.help()) == 2

    def test_doctor_row_is_down_for_an_unsupported_cli(self, fake_claude, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_HELP", "no_safe_mode")
        env = dict(os.environ)
        env["UA_CLAUDE_CLI_BIN"] = str(fake_claude.script)
        leg = {entry["leg"]: entry for entry in llm_client.leg_status(env=env)}["claude_cli"]
        assert (leg["configured"], leg["reachable"]) == (True, False)
        assert llm_client.CLAUDE_CLI_UNSUPPORTED_REASON in leg["detail"]
        assert "--safe-mode" in leg["detail"]
        assert fake_claude.completions() == []


class TestJsonMode:
    def test_schema_goes_into_the_system_prompt_and_fence_is_removed(
        self, fake_claude, monkeypatch
    ):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "fenced_json")
        result = llm_client.complete(
            "prompt",
            system="Adjudicate.",
            response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA,
        )
        assert json.loads(result["text"]) == {"verdict": "DUPLICATE", "reason": "same fact"}
        system = fake_claude.completions()[0]["system_text"]
        assert system.startswith("Adjudicate.")
        assert '"required": ["verdict", "reason"]' in system

    def test_deeply_nested_result_is_a_schema_mismatch_not_a_crash(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "nested_result")
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        [attempt] = exc_info.value.route
        assert (attempt["error"], attempt["reason"]) == ("SchemaMismatch", "schema mismatch")

    def test_reply_missing_the_schema_falls_through(self, fake_claude, monkeypatch):
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "not_json_reply")
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", response_schema=llm_client.ADJUDICATION_VERDICT_SCHEMA)
        [attempt] = exc_info.value.route
        assert (attempt["error"], attempt["reason"]) == ("SchemaMismatch", "schema mismatch")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('```json\n{"a": 1}\n```', '{"a": 1}'),
            ('```\n{"a": 1}\n```', '{"a": 1}'),
            ('  ```json\n{"a": 1}\n```  ', '{"a": 1}'),
            ('{"a": 1}', '{"a": 1}'),
            ("```python\nx\n```", "```python\nx\n```"),
            ("```json", "```json"),
        ],
    )
    def test_fence_stripping(self, text, expected):
        assert llm_client._strip_json_fence(text) == expected

    def test_fence_stripping_is_linear_on_adversarial_input(self):
        text = "```json\n" + "\n" * 200_000 + "X"
        started = time.monotonic()
        llm_client._strip_json_fence(text)
        assert time.monotonic() - started < 0.5


class TestFailuresFallThrough:
    @pytest.mark.parametrize(
        ("mode", "error", "reason"),
        [
            ("login", "ClaudeCLIError", "auth rejected"),
            ("usage_limit", "ClaudeCLIError", "rate limited"),
            ("empty", "EmptyResponse", "empty reply"),
            ("garbage", "MalformedResponse", "malformed reply"),
            ("deep_json", "MalformedResponse", "malformed reply"),
            ("oversized", "OversizedResponse", "malformed reply"),
            ("stderr_flood", "OversizedResponse", "malformed reply"),
            ("crash", "ClaudeCLIExit", "request failed"),
        ],
    )
    def test_cli_failure_falls_through_to_openrouter(
        self, fake_claude, monkeypatch, mode, error, reason
    ):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        _openrouter_answers(monkeypatch)
        result = llm_client.complete(PRIVATE_PROMPT)
        assert result["provider"] == "openrouter"
        failed, succeeded = result["route"]
        assert failed == {
            "target": "claude_cli",
            "ok": False,
            "elapsed_s": failed["elapsed_s"],
            "error": error,
            "reason": reason,
        }
        assert succeeded["target"] == "openrouter"
        # The CLI's output can echo the prompt; none of it reaches the route.
        assert "PRIVATE-NOTE-BODY" not in json.dumps(result["route"])

    def test_missing_binary_is_unconfigured_and_falls_through(self, monkeypatch):
        monkeypatch.delenv("UA_CLAUDE_CLI_BIN", raising=False)
        _openrouter_answers(monkeypatch)
        result = llm_client.complete("prompt")
        assert result["provider"] == "openrouter"
        by_target = {a["target"]: a for a in result["route"]}
        assert by_target["claude_cli"]["error"] == "Unconfigured"
        assert by_target["claude_cli"]["reason"] == llm_client.CLAUDE_CLI_MISSING_REASON

    def test_explicit_bin_that_is_not_executable_reads_as_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_CLAUDE_CLI_BIN", str(tmp_path / "nope" / "claude"))
        assert llm_client.resolve_claude_cli() is None

    def test_timeout_kills_the_process_group_and_falls_through(
        self, fake_claude, tmp_path, monkeypatch
    ):
        child_pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
        monkeypatch.setenv("FAKE_CLAUDE_CHILD_PID", str(child_pid_file))
        monkeypatch.setenv("UA_CLAUDE_CLI_TIMEOUT_S", "1.5")
        _openrouter_answers(monkeypatch)
        started = time.monotonic()
        result = llm_client.complete("prompt")
        assert time.monotonic() - started < 20
        assert result["provider"] == "openrouter"
        by_target = {a["target"]: a for a in result["route"]}
        assert by_target["claude_cli"]["error"] == "TimeoutExpired"
        assert by_target["claude_cli"]["reason"] == "timeout"
        _assert_dies(int(child_pid_file.read_text()))

    def test_a_descendant_holding_the_pipe_cannot_outlast_the_deadline(
        self, fake_claude, tmp_path, monkeypatch
    ):
        """The CLI prints a result and exits at once, but a child it left
        behind keeps stdout open for 3 s. The one deadline covers end-of-file
        too: the call times out on time and the child is killed."""
        child_pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "early_exit")
        monkeypatch.setenv("FAKE_CLAUDE_CHILD_PID", str(child_pid_file))
        monkeypatch.setenv("UA_CLAUDE_CLI_TIMEOUT_S", "0.5")
        _openrouter_answers(monkeypatch)
        # Warm the auth cache so only the completion is timed.
        llm_client._claude_cli_auth(str(fake_claude.script), llm_client._claude_cli_run_base())
        started = time.monotonic()
        result = llm_client.complete("prompt")
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, elapsed
        by_target = {a["target"]: a for a in result["route"]}
        assert by_target["claude_cli"]["reason"] == "timeout"
        assert result["provider"] == "openrouter"
        _assert_dies(int(child_pid_file.read_text()))

    @pytest.mark.parametrize("exc_type", [OverflowError, KeyboardInterrupt])
    def test_any_abnormal_exit_kills_the_process_group(
        self, fake_claude, tmp_path, monkeypatch, exc_type
    ):
        child_pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
        monkeypatch.setenv("FAKE_CLAUDE_CHILD_PID", str(child_pid_file))
        real_wait = subprocess.Popen.wait
        state = {"raised": False}

        def interrupting_wait(self, timeout=None):
            # Interrupt the completion's wait loop once the CLI has spawned
            # its grandchild; the reap after the kill uses the real wait.
            if not state["raised"] and child_pid_file.exists():
                state["raised"] = True
                raise exc_type("interrupted")
            return real_wait(self, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)
        _openrouter_answers(monkeypatch)
        if exc_type is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                llm_client.complete("prompt")
        else:
            result = llm_client.complete("prompt")
            assert result["provider"] == "openrouter"
            by_target = {a["target"]: a for a in result["route"]}
            assert by_target["claude_cli"]["error"] == "OverflowError"
            assert by_target["claude_cli"]["reason"] == "request failed"
        assert state["raised"]
        _assert_dies(int(child_pid_file.read_text()))


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
class TestSuccessfulRunCleanup:
    def test_a_backgrounded_descendant_dies_after_a_successful_run(
        self, fake_claude, tmp_path, monkeypatch
    ):
        """The CLI answers and exits 0, leaving a child with its stdio
        detached (so the pipes reach EOF at once). The run still ends by
        killing the process group, so the child does not outlive it."""
        child_pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "background_child")
        monkeypatch.setenv("FAKE_CLAUDE_CHILD_PID", str(child_pid_file))
        _only_claude_cli(monkeypatch)
        result = llm_client.complete("prompt")
        assert result["provider"] == "claude_cli"
        assert result["text"] == "hi from claude"
        _assert_dies(int(child_pid_file.read_text()))


class TestTimeoutSetting:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("abc", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("inf", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("-inf", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("nan", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("0", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("-5", llm_client.CLAUDE_CLI_TIMEOUT_S),
            ("45", 45.0),
            ("100000", llm_client.CLAUDE_CLI_MAX_TIMEOUT_S),
        ],
    )
    def test_timeout_is_finite_positive_and_clamped(self, monkeypatch, raw, expected):
        monkeypatch.setenv("UA_CLAUDE_CLI_TIMEOUT_S", raw)
        value = llm_client._claude_cli_timeout_s()
        assert math.isfinite(value)
        assert value == expected
        assert llm_client.CLAUDE_CLI_MAX_TIMEOUT_S <= 600


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # A killed process awaiting its reaper is a zombie: dead for this test.
        return stat_path.read_text().split(") ", 1)[1][:1] != "Z"
    except (OSError, IndexError):
        return True


def _assert_dies(pid: int) -> None:
    """The grandchild the CLI spawned died with its process group."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.01)
    assert not _alive(pid)


class TestDefaultChain:
    def test_default_order_is_claude_cli_then_openrouter_only(self):
        assert [name for name, _ in llm_client._PROVIDER_CHAIN] == ["claude_cli", "openrouter"]

    def test_no_local_model_in_the_default_chain(self):
        assert "ollama" not in {name for name, _ in llm_client._PROVIDER_CHAIN}
        assert llm_client._call_ollama not in {fn for _, fn in llm_client._PROVIDER_CHAIN}

    def test_openrouter_default_model_is_the_pinned_id(self):
        assert llm_client.DEFAULT_OPENROUTER_MODEL == "deepseek/deepseek-v4.1-flash"

    def test_ai_studio_is_not_in_the_default_chain(self):
        assert "ai_studio" not in {name for name, _ in llm_client._PROVIDER_CHAIN}
        assert llm_client._call_ai_studio not in {fn for _, fn in llm_client._PROVIDER_CHAIN}

    def test_ai_studio_is_never_called_when_every_leg_fails(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        def boom(*args, **kwargs):
            raise AssertionError("gemini_ask must not run on the default chain")

        monkeypatch.setattr(llm_client.gemini_tool, "gemini_ask", boom)
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: (_ for _ in ()).throw(
                httpx.ConnectError("refused", request=httpx.Request("POST", url))
            ),
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt")
        assert [a["target"] for a in exc_info.value.route] == ["claude_cli", "openrouter"]

    def test_route_target_metadata(self):
        assert llm_client.ROUTE_TARGETS["claude_cli"]["cost_tier"] == "subscription"
        assert llm_client.ROUTE_TARGETS["claude_cli"]["local"] is False

    def test_subscription_pool_still_sits_immediately_before_openrouter(self, monkeypatch):
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_ENABLED", "1")
        monkeypatch.setenv("UA_SUBSCRIPTION_POOL_URL", "http://127.0.0.1:8317/v1")
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kwargs: (_ for _ in ()).throw(
                httpx.ConnectError("refused", request=httpx.Request("POST", url))
            ),
        )
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            llm_client.complete("prompt", subscription_pool=True)
        assert [a["target"] for a in exc_info.value.route] == [
            "claude_cli",
            "subscription_pool",
            "openrouter",
        ]


class TestBinaryResolution:
    def _exe(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_path_hit_wins(self, tmp_path):
        exe = self._exe(tmp_path / "pathdir" / "claude")
        found = _REAL_SEARCH({"PATH": str(exe.parent), "HOME": str(tmp_path / "home")})
        assert found == str(exe)

    def test_minimal_launchagent_path_falls_back_to_home_local_bin(self, tmp_path):
        home = tmp_path / "home"
        exe = self._exe(home / ".local" / "bin" / "claude")
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        found = _REAL_SEARCH({"PATH": str(empty), "HOME": str(home)})
        assert found == str(exe)

    @pytest.mark.parametrize("path", ["relbin", ".", ":", "relbin::.", "::"])
    def test_relative_and_empty_path_entries_are_not_searched(self, tmp_path, monkeypatch, path):
        """`claude` in the current dir (empty or "." entry) or a relative dir
        names whatever happens to sit where the hub was started: never used."""
        self._exe(tmp_path / "relbin" / "claude")
        self._exe(tmp_path / "claude")
        monkeypatch.chdir(tmp_path)
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert _REAL_SEARCH({"PATH": path, "HOME": str(tmp_path / "home")}) is None

    def test_absolute_entries_beside_relative_ones_are_still_searched(self, tmp_path, monkeypatch):
        self._exe(tmp_path / "relbin" / "claude")
        exe = self._exe(tmp_path / "absbin" / "claude")
        monkeypatch.chdir(tmp_path)
        found = _REAL_SEARCH({"PATH": f"relbin::.:{exe.parent}", "HOME": str(tmp_path / "home")})
        assert found == str(exe)

    def test_doctor_nightly_probe_uses_the_scheduled_path_not_the_hubs(self, tmp_path, monkeypatch):
        """A claude only on the hub's augmented PATH is not on the nightly
        job's minimal PATH, so the nightly row must not call it usable."""
        from unified_agent import sleep_pass

        monkeypatch.setattr(llm_client, "_search_claude_cli", _REAL_SEARCH)
        hub_only = self._exe(tmp_path / "hub-only-bin" / "claude")
        monkeypatch.setenv("PATH", f"{hub_only.parent}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        env = sleep_pass.scheduled_env()
        assert str(hub_only.parent) not in env["PATH"]
        assert env["HOME"] == str(tmp_path / "empty-home")
        found = llm_client.resolve_claude_cli(env)
        assert found != str(hub_only)

    def test_doctor_auth_probe_uses_supplied_service_environment(
        self, fake_claude, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient-profile"))
        service_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path / "service-home"),
            "UA_HOME": str(tmp_path / "service-ua"),
            "UA_CLAUDE_CLI_BIN": str(fake_claude.script),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "service-profile"),
            "FAKE_CLAUDE_RECORD": os.environ["FAKE_CLAUDE_RECORD"],
            "FAKE_CLAUDE_HELP": "full",
            "FAKE_CLAUDE_AUTH": "subscription",
        }
        leg = {entry["leg"]: entry for entry in llm_client.leg_status(env=service_env)}[
            "claude_cli"
        ]
        assert leg["reachable"] is True
        for probe in [*fake_claude.help(), *fake_claude.auth()]:
            assert probe["env"]["HOME"] == service_env["HOME"]
            assert probe["env"]["CLAUDE_CONFIG_DIR"] == service_env["CLAUDE_CONFIG_DIR"]

    def test_fallback_list_covers_homebrew(self):
        assert "/opt/homebrew/bin/claude" in llm_client.CLAUDE_CLI_FALLBACK_PATHS
        assert "~/.local/bin/claude" in llm_client.CLAUDE_CLI_FALLBACK_PATHS

    def test_bin_env_is_inherited_by_the_scheduled_pass(self):
        from unified_agent import sleep_pass

        assert "UA_CLAUDE_CLI_BIN" in sleep_pass._HUB_LOCAL_MODEL_KEYS
        assert "UA_CLAUDE_CLI_MODEL" in sleep_pass._HUB_LOCAL_MODEL_KEYS
        # One pinned OpenRouter model for the hub and the nightly pass; the
        # OpenRouter key itself is a secret and is never inherited.
        assert "UA_OPENROUTER_MODEL" in sleep_pass._HUB_LOCAL_MODEL_KEYS
        assert "OPENROUTER_API_KEY" not in sleep_pass._HUB_LOCAL_MODEL_KEYS

    def test_bin_env_is_in_the_hermeticity_tuple(self):
        assert "UA_CLAUDE_CLI_BIN" in llm_client.PROVIDER_CHAIN_ENV_VARS
        assert "GEMINI_API_KEY" not in llm_client.PROVIDER_CHAIN_ENV_VARS


class TestDoctorProbe:
    def _legs(self, env) -> dict:
        merged = dict(os.environ)
        merged.update(env)
        return {leg["leg"]: leg for leg in llm_client.leg_status(env=merged)}

    def _no_http(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            lambda url, **kwargs: (_ for _ in ()).throw(
                httpx.ConnectError("refused", request=httpx.Request("GET", url))
            ),
        )

    def test_probe_never_runs_a_completion(self, fake_claude, monkeypatch):
        self._no_http(monkeypatch)
        leg = self._legs({"UA_CLAUDE_CLI_BIN": str(fake_claude.script)})["claude_cli"]
        assert leg["configured"] is True
        assert leg["explicit"] is True
        assert leg["reachable"] is True
        assert "authMethod=claude.ai" in leg["detail"]
        assert "subscriptionType=max" in leg["detail"]
        assert "SECRET" not in json.dumps(leg) and "example.invalid" not in json.dumps(leg)
        assert leg["caveat"] == llm_client.CLAUDE_CLI_LOGIN_CAVEAT
        # The flag probe and the login check; never a completion.
        assert [call["argv"] for call in fake_claude()] == [
            ["--help"],
            ["auth", "status", "--json"],
        ]

    def test_console_login_is_down(self, fake_claude, monkeypatch):
        self._no_http(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "console")
        leg = self._legs({"UA_CLAUDE_CLI_BIN": str(fake_claude.script)})["claude_cli"]
        assert leg["configured"] is True
        assert leg["reachable"] is False
        assert "authMethod=console" in leg["detail"]
        assert "SECRET" not in json.dumps(leg)

    def test_missing_binary_is_unconfigured(self, monkeypatch):
        self._no_http(monkeypatch)
        leg = self._legs({})["claude_cli"]
        assert (leg["configured"], leg["explicit"], leg["reachable"]) == (False, False, None)
        assert llm_client.CLAUDE_CLI_MISSING_REASON in leg["detail"]

    def test_legs_listed_in_chain_order_without_ai_studio(self, monkeypatch):
        self._no_http(monkeypatch)
        names = [leg["leg"] for leg in llm_client.leg_status(env={"GEMINI_API_KEY": "k"})]
        assert names == ["claude_cli", "openrouter", "subscription_pool"]

    def test_system_doctor_chain_check_never_runs_a_completion(self, fake_claude, monkeypatch):
        from unified_agent.tools import doctor

        self._no_http(monkeypatch)
        monkeypatch.setattr(doctor, "_sleep_pass_installed", lambda: False)
        check = doctor._llm_provider_chain_check()
        assert "claude_cli:up" in check["detail"]
        assert check["status"] == "LIVE"
        assert fake_claude.completions() == []
        assert fake_claude.auth()


_GATE_NOTES = [
    {"id": "9846126b6aa6", "title": "A", "content": "x", "embedding": b"a"},
    {"id": "a13d8ec16f31", "title": "B", "content": "y", "embedding": b"b"},
]
_FOUR_FIELDS = (
    '{"verdict":"CONTRADICTION","reason":"Consistent.","self_consistent":true,'
    '"conflicting_attributes":[]}'
)


class TestStructuredGateSeesNormalizedText:
    """Adapter-to-gate boundary for the #2022 downgrade (PR #1213 review P3).

    With a response schema the claude_cli leg strips one ```json (or bare ```)
    fence and surrounding whitespace before validating and returning the text.
    The sleep-pass gate (`consolidate.structured_self_consistency`) reads that
    normalized `response["text"]`, so its one-object, 1000-character contract
    applies to the normalized text, not the raw CLI `result`. Normalization
    removes only the fence lines and whitespace, so nothing the gate cannot
    see is dropped: any other text keeps the reply out of the downgrade.
    """

    def _review(self, monkeypatch, raw: str) -> dict:
        _only_claude_cli(monkeypatch)
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "result_env")
        monkeypatch.setenv("FAKE_CLAUDE_RESULT", raw)
        return consolidate._review_cluster(_GATE_NOTES)

    @pytest.mark.parametrize(
        "raw",
        [
            "```json\n" + _FOUR_FIELDS + "\n```",
            "```\n" + _FOUR_FIELDS + "\n```",
            # Over the cap raw, under it once the whitespace is stripped.
            " " * 600 + "```json\n" + " " * 600 + _FOUR_FIELDS + "\n" * 300 + "```" + " " * 300,
        ],
        ids=["json-fence", "bare-fence", "whitespace-padded-over-cap"],
    )
    def test_a_fenced_four_field_reply_is_downgraded_after_normalization(
        self, fake_claude, monkeypatch, raw
    ):
        # The raw CLI result alone would not pass the strict gate ...
        assert consolidate.structured_self_consistency(raw, note_count=2) is False
        # ... the adapter's normalized text does, and that is what is judged.
        assert llm_client._strip_json_fence(raw) == _FOUR_FIELDS
        review = self._review(monkeypatch, raw)
        assert review["provider"] == "claude_cli"
        assert review["verdict"] == consolidate.VERDICT_NEEDS_REVIEW
        assert review["judge_verdict"] == "CONTRADICTION"
        assert review["downgrade_reason"] == consolidate.STRUCTURED_DOWNGRADE_REASON

    def test_a_fenced_reply_over_the_cap_after_normalization_keeps_contradiction(
        self, fake_claude, monkeypatch
    ):
        body = json.loads(_FOUR_FIELDS) | {"reason": "x" * consolidate._REPLY_MAX_CHARS}
        raw = "```json\n" + json.dumps(body) + "\n```"
        review = self._review(monkeypatch, raw)
        assert review["verdict"] == "CONTRADICTION"
        assert "downgrade_reason" not in review

    @pytest.mark.parametrize(
        "raw",
        [
            # Text after the closing fence stays in the normalized text and
            # fails the schema, so the leg rejects the reply.
            "```json\n" + _FOUR_FIELDS + "\n```\nBut A requires approval.\n```",
            # Only a json or bare fence is stripped.
            "```python\n" + _FOUR_FIELDS + "\n```",
            # Text before the fence is not stripped.
            "Here you go:\n```json\n" + _FOUR_FIELDS + "\n```",
        ],
        ids=["trailing-text", "other-language", "leading-text"],
    )
    def test_a_fence_with_other_text_is_never_downgraded(self, fake_claude, monkeypatch, raw):
        with pytest.raises(llm_client.LLMConnectionError) as exc_info:
            self._review(monkeypatch, raw)
        assert exc_info.value.route[0]["reason"] == "schema mismatch"
