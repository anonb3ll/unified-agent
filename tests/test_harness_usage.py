"""Run usage is measured with its bill, not left dark (task #1837).

M1's review found every run unknown-cost: interactive sessions never reported
usage, and executor runs reported tokens without saying whether a subscription
or an API key paid for them. These tests cover the three pieces:

* ``harness_billing`` reads how each CLI is logged in, under the child's own
  environment, and never guesses;
* ``harness_usage`` sums one harness session's model calls inside a run's
  window from the local session store, once per message;
* the executor backends and daemon put the observed ``billing`` on the run, so
  ``cost_rollup`` counts a subscription run as subscription, not API spend.

The transcript lines follow the shapes Claude Code 2.x and Codex write to
their stores (assistant ``message.usage``; ``event_msg``/``token_count`` with a
cumulative ``total_token_usage``), trimmed to the fields read.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from unified_agent import budgets, harness_billing, harness_usage, run_records, storage
from unified_agent.executor import backends
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.harness_billing import probe as real_probe
from unified_agent.tools import delegate as delegate_tool

SESSION = "a71249b6-e1b3-46d2-8c6e-5dcad1ef963d"
CODEX_SESSION = "01a1349c-6e63-73d1-b014-ea8b7fa5500e"
SINCE = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)


# --- billing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "billing"),
    [("claude.ai", "subscription"), ("api_key", "api"), ("third_party", "api"), ("other", None)],
)
def test_claude_auth_method_maps_to_billing(method, billing):
    out = json.dumps({"loggedIn": True, "authMethod": method, "apiProvider": "firstParty"})
    assert harness_billing.claude_billing(out) == billing


@pytest.mark.parametrize("out", ["", "not json", "[]", json.dumps({"loggedIn": False})])
def test_claude_status_that_says_nothing_is_unknown(out):
    assert harness_billing.claude_billing(out) is None


# Codex 0.159+ prints login status on stderr; an API key line carries a masked key.
CODEX_CHATGPT = ("", "Logged in using ChatGPT\n")
CODEX_API_KEY_STATUS = ("", "Logged in using an API key - sk-proj-***ABCD\n")


def _rollout(codex_home, session_id, provider):
    """A Codex rollout whose first record is ``session_meta``, as Codex writes it."""
    path = (
        codex_home
        / "sessions"
        / "2026"
        / "10"
        / "13"
        / f"rollout-2026-10-13T22-00-00-{session_id}.jsonl"
    )
    meta = {"id": session_id, "session_id": session_id, "cwd": "/w", "source": "exec"}
    if provider is not None:
        meta["model_provider"] = provider
    _write(path, [{"type": "session_meta", "timestamp": "2026-10-13T22:00:00Z", "payload": meta}])
    return path


def test_codex_status_maps_to_billing():
    assert harness_billing.codex_billing(CODEX_CHATGPT, {}, "openai") == "subscription"
    assert harness_billing.codex_billing(CODEX_API_KEY_STATUS, {}, "openai") == "api"
    assert harness_billing.codex_billing(("", "Not logged in\n"), {}, "openai") is None
    assert harness_billing.codex_billing(None, {}, "openai") is None


@pytest.mark.parametrize("provider", [None, "proxy", "openrouter", "ollama"])
def test_a_codex_subscription_needs_the_session_on_the_openai_provider(provider):
    # ``codex login status`` names the ChatGPT login even when the session ran
    # under ``-c model_provider="proxy"``, a profile or another config layer.
    assert harness_billing.codex_billing(CODEX_CHATGPT, {}, provider) is None


@pytest.mark.parametrize("key", ["CODEX_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"])
def test_codex_child_with_a_provider_key_is_unknown(key):
    assert harness_billing.codex_billing(CODEX_CHATGPT, {key: "x"}, "openai") is None


def test_codex_session_provider_reads_session_meta(tmp_path):
    _rollout(tmp_path, CODEX_SESSION, "proxy")
    assert harness_billing.codex_session_provider(CODEX_SESSION, codex_home=tmp_path) == "proxy"
    other = "02a1349c-6e63-73d1-b014-ea8b7fa5500e"
    _rollout(tmp_path, other, None)
    assert harness_billing.codex_session_provider(other, codex_home=tmp_path) is None
    assert harness_billing.codex_session_provider("missing", codex_home=tmp_path) is None
    assert harness_billing.codex_session_provider(None, codex_home=tmp_path) is None
    assert harness_billing.codex_session_provider("../x", codex_home=tmp_path) is None


def _proc(stdout: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], code, stdout=stdout, stderr=stderr)


def test_probe_runs_the_cli_under_the_child_env_every_time(monkeypatch):
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    calls = []
    answers = iter(
        [
            json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
            json.dumps({"loggedIn": True, "authMethod": "api_key"}),
        ]
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return _proc(next(answers))

    env = {"HOME": "/h", "PATH": "/bin"}
    with patch.object(harness_billing.subprocess, "run", side_effect=run):
        assert harness_billing.probe("claude", binary="/opt/claude", env=env) == "subscription"
        # No cached exemption: a login that changed between runs is seen.
        assert harness_billing.probe("claude", binary="/opt/claude", env=env) == "api"
    assert [argv for argv, _ in calls] == [["/opt/claude", "auth", "status"]] * 2
    assert calls[0][1] == env


def test_probe_reads_codex_status_from_stderr(monkeypatch):
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    with patch.object(
        harness_billing.subprocess, "run", return_value=_proc("", stderr=CODEX_CHATGPT[1])
    ):
        assert harness_billing.probe("codex", env={}, session_provider="openai") == "subscription"
        assert harness_billing.probe("codex", env={}) is None


@pytest.mark.parametrize(
    "outcome", [_proc("", 1), OSError("missing"), subprocess.TimeoutExpired("x", 1)]
)
def test_probe_failure_is_unknown(monkeypatch, outcome):
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    kwargs = (
        {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    )
    with patch.object(harness_billing.subprocess, "run", **kwargs):
        assert harness_billing.probe("codex", env={}) is None


def test_probe_of_an_unsupported_harness_runs_nothing(monkeypatch):
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    with patch.object(harness_billing.subprocess, "run") as run:
        assert harness_billing.probe("cursor", env={}) is None
    run.assert_not_called()


# --- Claude Code transcripts ---------------------------------------------------


def _assistant(ts: str, msg_id: str, model: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": f"req_{msg_id}",
        "message": {"id": msg_id, "model": model, "usage": usage, "content": []},
    }


def _write(path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def claude_store(tmp_path):
    store = tmp_path / "projects"
    main = store / "-home-u-dev" / f"{SESSION}.jsonl"
    _write(
        main,
        [
            # Before the window: another task's work in the same session.
            _assistant("2026-10-13T21:59:59Z", "m0", "claude-opus-5-5", {"output_tokens": 999}),
            {"type": "user", "timestamp": "2026-10-13T22:00:01Z", "message": {"content": "hi"}},
            # One message, two lines: the second carries the final output count.
            _assistant(
                "2026-10-13T22:00:02Z",
                "m1",
                "claude-opus-5-5",
                {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 1,
                },
            ),
            _assistant(
                "2026-10-13T22:00:03Z",
                "m1",
                "claude-opus-5-5",
                {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 40,
                },
            ),
            _assistant("2026-10-13T22:00:04Z", "m2", "<synthetic>", {"output_tokens": 7}),
            "not a record",
            # After the window.
            _assistant("2026-10-13T23:00:00Z", "m3", "claude-opus-5-5", {"output_tokens": 500}),
        ],
    )
    _write(
        main.parent / SESSION / "subagents" / "agent-a1.jsonl",
        [
            _assistant(
                "2026-10-13T22:00:05Z",
                "s1",
                "claude-haiku-5-5",
                {"input_tokens": 10, "output_tokens": 5},
            )
        ],
    )
    # A concurrent session in the same project is never summed in.
    _write(
        store / "-home-u-dev" / "b0000000-0000-0000-0000-000000000000.jsonl",
        [_assistant("2026-10-13T22:00:06Z", "x1", "claude-opus-5-5", {"output_tokens": 800})],
    )
    return store


def test_claude_session_window_counts_each_message_once(claude_store):
    until = datetime(2026, 10, 13, 22, 30, tzinfo=UTC)
    usage = harness_usage.measure(
        "claude", SESSION, SINCE, until, billing="subscription", claude_store=claude_store
    )
    assert usage == {
        "provider": "anthropic",
        # The session's main model produced most of the output.
        "model": "claude-opus-5-5",
        # Cache reads and writes are prompt tokens too (the #1917 convention).
        "input_tokens": 2 + 100 + 50 + 10,
        "output_tokens": 40 + 5,
        "billing": "subscription",
    }
    assert run_records.validate_usage(usage)["billing"] == "subscription"


def test_a_message_is_charged_to_the_run_it_started_in(tmp_path):
    store = tmp_path / "projects"
    _write(
        store / "-p" / f"{SESSION}.jsonl",
        [
            # Started before the claim; its final line lands after it.
            _assistant("2026-10-13T21:59:59Z", "early", "claude-opus-5-5", {"output_tokens": 2}),
            _assistant("2026-10-13T22:00:01Z", "early", "claude-opus-5-5", {"output_tokens": 40}),
            # Started inside the window; its final line lands after the finish.
            _assistant("2026-10-13T22:29:59Z", "late", "claude-opus-5-5", {"output_tokens": 3}),
            _assistant("2026-10-13T22:30:01Z", "late", "claude-opus-5-5", {"output_tokens": 70}),
        ],
    )
    until = datetime(2026, 10, 13, 22, 30, tzinfo=UTC)
    usage = harness_usage.measure("claude", SESSION, SINCE, until, claude_store=store)
    # ``early`` belongs to the previous run; ``late`` counts its final total.
    assert usage["output_tokens"] == 70
    previous = harness_usage.measure(
        "claude", SESSION, datetime(2026, 10, 13, 21, tzinfo=UTC), SINCE, claude_store=store
    )
    assert previous["output_tokens"] == 40


def test_open_window_runs_to_the_end_of_the_session(claude_store):
    usage = harness_usage.measure("claude", SESSION, SINCE, claude_store=claude_store)
    assert usage["output_tokens"] == 40 + 5 + 500


def test_window_with_no_model_calls_is_none(claude_store):
    since = datetime(2026, 10, 14, tzinfo=UTC)
    assert harness_usage.measure("claude", SESSION, since, claude_store=claude_store) is None


def test_unknown_or_unsafe_session_is_a_lookup_error(claude_store):
    for session in ("c0000000-0000-0000-0000-000000000000", "../../etc/passwd"):
        with pytest.raises(LookupError):
            harness_usage.measure("claude", session, SINCE, claude_store=claude_store)


def test_api_billed_session_reports_tokens_and_stays_unknown_cost(claude_store):
    usage = harness_usage.measure(
        "claude", SESSION, SINCE, billing="api", claude_store=claude_store
    )
    assert "cost_usd" not in usage
    assert budgets._classify(usage) == "unknown_cost"
    assert budgets._classify({**usage, "billing": "subscription"}) == "subscription"


# --- Codex rollouts ------------------------------------------------------------


def _token_count(ts: str, inp: int, cached: int, out: int) -> dict:
    total = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {"type": "token_count", "info": {"total_token_usage": total}},
    }


def test_codex_window_is_the_difference_of_cumulative_totals(tmp_path):
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "10"
        / "13"
        / f"rollout-2026-10-13T21-00-00-{CODEX_SESSION}.jsonl"
    )
    _write(
        rollout,
        [
            {
                "type": "turn_context",
                "timestamp": "2026-10-13T21:00:00Z",
                "payload": {"model": "gpt-6.1-sol"},
            },
            _token_count("2026-10-13T21:30:00Z", 1000, 400, 50),
            {
                "type": "event_msg",
                "timestamp": "2026-10-13T22:00:01Z",
                "payload": {"type": "token_count", "info": None},
            },
            _token_count("2026-10-13T22:05:00Z", 1500, 600, 80),
            # A repeated total adds nothing.
            _token_count("2026-10-13T22:06:00Z", 1500, 600, 80),
            _token_count("2026-10-13T22:10:00Z", 2000, 900, 120),
        ],
    )
    usage = harness_usage.measure("codex", CODEX_SESSION, SINCE, codex_dir=tmp_path)
    assert usage == {
        "provider": "openai",
        "model": "gpt-6.1-sol",
        "input_tokens": 1000,
        "output_tokens": 70,
    }


def test_malformed_codex_totals_are_skipped_not_zeroed(tmp_path):
    rollout = tmp_path / "sessions" / "2026" / "10" / "13" / f"rollout-x-{CODEX_SESSION}.jsonl"
    _write(
        rollout,
        [
            _token_count("2026-10-13T21:30:00Z", 100, 0, 10),
            _token_count("2026-10-13T22:05:00Z", 200, 0, 30),
            {
                "type": "event_msg",
                "timestamp": "2026-10-13T22:06:00Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {}}},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-10-13T22:07:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": -5, "output_tokens": 1}},
                },
            },
        ],
    )
    usage = harness_usage.measure("codex", CODEX_SESSION, SINCE, codex_dir=tmp_path)
    assert (usage["input_tokens"], usage["output_tokens"]) == (100, 20)
    # A window holding only a malformed total measured nothing.
    empty = tmp_path / "e" / "sessions" / "2026" / "10" / "13" / f"rollout-x-{CODEX_SESSION}.jsonl"
    _write(
        empty,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-10-13T22:06:00Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {}}},
            }
        ],
    )
    assert harness_usage.measure("codex", CODEX_SESSION, SINCE, codex_dir=tmp_path / "e") is None


# --- ua-usage ------------------------------------------------------------------


def test_cli_detects_the_claude_session_and_prints_run_usage(claude_store, monkeypatch, capsys):
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(claude_store))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    code = harness_usage.main(
        ["--since", "2026-10-13T22:00:00Z", "--until", "2026-10-13T22:30:00Z"]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["output_tokens"] == 45
    assert run_records.validate_usage(printed)["input_tokens"] == 162


def test_cli_never_infers_billing(claude_store, tmp_path, monkeypatch, capsys):
    # The stores record no login or provider per call, and today's login says
    # nothing about calls already made, so an unstated bill stays unknown.
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(claude_store))
    rollout = _rollout(tmp_path, CODEX_SESSION, "openai")
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_token_count("2026-10-13T22:05:00Z", 100, 0, 10)) + "\n")
    monkeypatch.setenv("UA_NODE_CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    with patch.object(harness_billing.subprocess, "run") as run:
        for harness, session in (("claude", SESSION), ("codex", CODEX_SESSION)):
            code = harness_usage.main(
                ["--harness", harness, "--session", session, "--since", "2026-10-13T22:00:00Z"]
            )
            assert code == 0
            printed = json.loads(capsys.readouterr().out)
            assert "billing" not in printed
            assert budgets._classify(printed) == "unknown_cost"
    run.assert_not_called()


@pytest.mark.parametrize("billing", ["api", "subscription", "local"])
def test_cli_passes_a_stated_bill_through(claude_store, monkeypatch, capsys, billing):
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(claude_store))
    code = harness_usage.main(
        [
            "--harness",
            "claude",
            "--session",
            SESSION,
            "--since",
            "2026-10-13T22:00:00Z",
            "--billing",
            billing,
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["billing"] == billing


def test_cli_without_a_session_explains_what_to_pass(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert harness_usage.main(["--since", "2026-10-13T22:00:00Z"]) == 1
    assert "--harness and --session" in capsys.readouterr().err


def test_cli_rejects_a_bad_instant(capsys):
    assert harness_usage.main(["--since", "yesterday"]) == 2


# --- executor runs carry the observed bill -------------------------------------


def test_to_run_usage_keeps_only_a_valid_billing():
    raw = {"provider": "openai", "input_tokens": 3, "output_tokens": 1}
    assert backends.to_run_usage(raw, billing="subscription")["billing"] == "subscription"
    assert "billing" not in backends.to_run_usage(raw, billing="free")
    assert "billing" not in backends.to_run_usage(raw)


CLAUDE_RESULT = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "apiKeySource": "none"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "total_cost_usd": 0.5,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )
)


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], 0, stdout=stdout, stderr="")


def test_claude_backend_marks_a_subscription_run(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    seen = {}

    def probe(harness, *, binary=None, env=None, cwd=None, session_provider=None):
        seen.update(harness=harness, binary=binary, has_env=isinstance(env, dict), cwd=cwd)
        return "subscription"

    monkeypatch.setattr(harness_billing, "probe", probe)
    with patch.object(
        backends, "run_popen_with_kill_check", return_value=_completed(CLAUDE_RESULT)
    ):
        res = backends.ClaudeBackend(claude_bin="claude").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent1",
            timeout_s=5,
        )
    assert res["billing"] == "subscription"
    assert seen == {
        "harness": "claude",
        "binary": "claude",
        "has_env": True,
        "cwd": str(tmp_path.resolve()),
    }
    usage = backends.to_run_usage(res["usage"], billing=res["billing"])
    # The CLI's own price estimate is kept, but a subscription is not API spend.
    assert usage["cost_usd"] == pytest.approx(0.5)
    assert budgets._classify(usage) == "subscription"
    assert budgets._known_usd(usage) == 0.0


@pytest.mark.parametrize(
    ("init", "billing"),
    [
        ({"apiKeySource": "none"}, "subscription"),
        ({"apiKeySource": "ANTHROPIC_API_KEY"}, None),
        ({"apiKeySource": "apiKeyHelper"}, None),
        ({}, None),
    ],
)
def test_a_claude_subscription_needs_the_run_on_the_oauth_login(
    monkeypatch, tmp_path, init, billing
):
    # ``claude auth status`` can say claude.ai while the run itself used a key
    # from project settings or a helper; the run's init event is the evidence.
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(harness_billing, "probe", lambda harness, **_: "subscription")
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5", **init},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "total_cost_usd": 0.5,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )
    )
    with patch.object(backends, "run_popen_with_kill_check", return_value=_completed(stream)):
        res = backends.ClaudeBackend(claude_bin="claude").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent1",
            timeout_s=5,
        )
    assert res.get("billing") == billing
    # The evidence field never reaches the run record.
    assert "api_key_source" not in backends.to_run_usage(res["usage"])


def test_probe_runs_in_the_child_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    with patch.object(
        harness_billing.subprocess,
        "run",
        return_value=_proc(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})),
    ) as run:
        harness_billing.probe("claude", env={}, cwd=str(tmp_path))
    assert run.call_args.kwargs["cwd"] == str(tmp_path)


def test_codex_backend_marks_a_killed_run_too(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(harness_billing, "probe", lambda harness, **_: "subscription")
    stream = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 2}}
    )
    with patch.object(
        backends, "run_popen_with_kill_check", side_effect=backends.KillSwitchTriggered(stream, "")
    ):
        res = backends.CodexBackend(codex_bin="codex").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
    assert res["status"] == "killed"
    assert res["billing"] == "subscription"


@pytest.mark.parametrize(("provider", "billing"), [("openai", "subscription"), ("proxy", None)])
def test_codex_backend_bills_by_its_own_session_provider(monkeypatch, tmp_path, provider, billing):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    codex_home = tmp_path / "codex-home"
    _rollout(codex_home, CODEX_SESSION, provider)
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    monkeypatch.setattr(
        backends.CodexBackend,
        "_child_env",
        staticmethod(lambda label: {"CODEX_HOME": str(codex_home)}),
    )
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "thread.started", "thread_id": CODEX_SESSION},
            {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 2}},
        )
    )
    with (
        patch.object(
            backends,
            "run_popen_with_kill_check",
            return_value=subprocess.CompletedProcess(["codex"], 0, stdout=stream, stderr=""),
        ),
        patch.object(
            harness_billing.subprocess, "run", return_value=_proc("", stderr=CODEX_CHATGPT[1])
        ),
    ):
        res = backends.CodexBackend(codex_bin="codex").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
    assert res.get("billing") == billing


def test_a_resumed_codex_run_has_no_billing_evidence(monkeypatch, tmp_path):
    # Codex keeps the original session_meta on resume, so an "openai" header
    # says nothing about the provider the resumed turns used.
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    codex_home = tmp_path / "codex-home"
    _rollout(codex_home, CODEX_SESSION, "openai")
    monkeypatch.setattr(harness_billing, "probe", real_probe)
    monkeypatch.setattr(
        backends.CodexBackend,
        "_child_env",
        staticmethod(lambda label: {"CODEX_HOME": str(codex_home)}),
    )
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "thread.started", "thread_id": CODEX_SESSION},
            {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 2}},
        )
    )
    with (
        patch.object(
            backends,
            "run_popen_with_kill_check",
            return_value=subprocess.CompletedProcess(["codex"], 0, stdout=stream, stderr=""),
        ),
        patch.object(
            harness_billing.subprocess, "run", return_value=_proc("", stderr=CODEX_CHATGPT[1])
        ),
    ):
        res = backends.CodexBackend(codex_bin="codex").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent2",
            timeout_s=5,
            resume_session_id=CODEX_SESSION,
        )
    assert res["usage"]["input_tokens"] == 9
    assert "billing" not in res


def test_a_run_with_no_usage_is_not_probed(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    probed = []
    monkeypatch.setattr(harness_billing, "probe", lambda *a, **k: probed.append(a) or "api")
    with patch.object(backends, "run_popen_with_kill_check", return_value=_completed("")):
        res = backends.ClaudeBackend(claude_bin="claude").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent1",
            timeout_s=5,
        )
    assert "billing" not in res
    assert probed == []


@pytest.fixture
def executor_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_EXECUTOR_BACKEND", "pi")
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    storage._connection = None
    storage.init_db()
    yield
    if storage._connection:
        storage._connection.close()
        storage._connection = None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_records_the_backend_billing_on_the_run(mock_pi, executor_db):
    mock_pi.return_value = {
        "status": "success",
        "text": "ok",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": 0,
        "error": None,
        "usage": {
            "provider": "anthropic",
            "input_tokens": 100,
            "output_tokens": 30,
            "cost_usd": 0.2,
        },
        "billing": "subscription",
    }
    task_id = delegate_tool.delegate(
        task_spec="billing", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    conn = storage.connect()
    try:
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[
            0
        ]
    finally:
        conn.close()
    run = run_records.get_run(run_id)
    assert run["usage"]["billing"] == "subscription"
    assert budgets._classify(run["usage"]) == "subscription"


@patch("unified_agent.tools.pi_runner.pi_run_task")
@pytest.mark.parametrize(("billing", "cents"), [("subscription", 0.0), ("api", 20.0), (None, 20.0)])
def test_cycle_dollars_follow_the_run_billing(mock_pi, executor_db, billing, cents):
    from unified_agent.tools import cycles as cycles_tool

    result = {
        "status": "success",
        "text": "ok",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": 0,
        "error": None,
        "usage": {
            "provider": "anthropic",
            "input_tokens": 100,
            "output_tokens": 30,
            "cost_usd": 0.2,
        },
    }
    if billing:
        result["billing"] = billing
    mock_pi.return_value = result
    task_id = delegate_tool.delegate(
        task_spec="cycle billing", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]
    cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"]["id"]
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]
    # Tokens always count; a subscription run spends no cycle dollars.
    assert cycle["used_tokens"] == 130
    assert cycle["used_cost_cents"] == pytest.approx(cents)


@pytest.mark.parametrize(
    ("bills", "expected", "classified"),
    [
        (["subscription", "subscription"], "subscription", "subscription"),
        (["api", "api"], "api", "priced"),
        # A subscription attempt never exempts another attempt's API spend.
        (["api", "subscription"], "api", "priced"),
        (["subscription", "api"], "api", "priced"),
        # An attempt without billing evidence keeps the total uncertain.
        (["subscription", None], "api", "unknown_cost"),
        ([None, None], None, "unknown_cost"),
    ],
)
def test_attempt_billing_aggregates_conservatively(executor_db, bills, expected, classified):
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    total = None
    for i, bill in enumerate(bills):
        usage = {"input_tokens": 10, "output_tokens": 2}
        if bill is not None:
            usage["cost_usd"] = 1.0
            usage["billing"] = bill
        total = daemon._accumulate_run_usage(1, "run-agg", f"attempt-{i}", usage)
    assert total.get("billing") == expected
    assert budgets._classify(total) == classified
    if "api" in bills:
        assert budgets._known_usd(total) == pytest.approx(sum(1.0 for b in bills if b))


def test_a_run_charged_as_api_is_never_exempted_by_a_later_report():
    # A replayed subscription snapshot after a mixed cumulative total.
    mixed = {"billing": "api", "cost_usd": 3.0, "input_tokens": 10}
    replayed = {"billing": "subscription", "cost_usd": 1.0, "input_tokens": 5}
    merged = run_records.merge_usage(mixed, replayed)
    assert merged["billing"] == "api"
    assert budgets._classify(merged) == "priced"
    # The other direction still upgrades to API.
    assert run_records.merge_usage(replayed, mixed)["billing"] == "api"


def test_unreadable_totals_never_leave_an_exempt_fallback(executor_db, monkeypatch):
    # Earlier API attempts cannot be reconstructed, so the current
    # subscription attempt must not exempt the run.
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    sent = []

    def broken(*a, **k):
        raise ValueError("corrupt totals")

    monkeypatch.setattr(daemon, "_accumulate_run_usage", broken)
    monkeypatch.setattr(daemon, "_report_run_usage", lambda t, r, usage, **k: sent.append(usage))
    from unittest.mock import patch as _patch

    with _patch("unified_agent.tools.pi_runner.pi_run_task") as mock_pi:
        mock_pi.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
            "usage": {
                "provider": "anthropic",
                "input_tokens": 10,
                "output_tokens": 2,
                "cost_usd": 1.0,
            },
            "billing": "subscription",
        }
        delegate_tool.delegate(
            task_spec="spool", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
        )
        daemon.poll_once()
    assert sent, "the fallback usage must still be reported"
    assert sent[0]["billing"] == "api"
    assert sent[0]["cost_complete"] is False
    assert budgets._classify(sent[0]) == "unknown_cost"


def test_an_empty_fallback_after_a_subscription_attempt_stays_charged(executor_db, monkeypatch):
    # Prior subscription attempt, then an attempt with no telemetry whose
    # totals cannot be read: the empty fallback must still revoke the
    # exemption, so the finished run keeps its reserve as unknown cost.
    delegate_tool.delegate(
        task_spec="empty fallback", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    sent = []
    real_report = daemon._report_run_usage

    def report(t, r, usage, **k):
        sent.append(dict(usage))
        return real_report(t, r, usage, **k)

    def broken(*a, **k):
        raise ValueError("corrupt totals")

    monkeypatch.setattr(daemon, "_report_run_usage", report)
    with patch("unified_agent.tools.pi_runner.pi_run_task") as mock_pi:
        mock_pi.return_value = {
            "status": "success",
            "text": "ok",
            "tools_used": [],
            "files_modified": [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
            "usage": None,
        }
        monkeypatch.setattr(daemon, "_accumulate_run_usage", broken)
        daemon.poll_once()
    assert sent and sent[0] == {"cost_complete": False, "billing": "api"}
    assert budgets._classify(sent[0]) == "unknown_cost"


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_empty_fallback_keeps_the_run_charged_with_its_reserve(
    mock_pi, executor_db, tmp_path, monkeypatch
):
    # Task #2035: end to end on the hub. A subscription attempt ($1 estimate)
    # fails and retries; the retry measures nothing and its totals cannot be
    # read. The run must stay charged: billing api, unknown cost, and
    # the reserve still committed in the budget.
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "budgets:\n"
        "  - name: agy\n"
        "    agents: [Desktop_agent3]\n"
        "    period: month\n"
        "    limit_usd: 100\n"
        "    run_reserve_usd: 3\n"
        "    action: warn\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    base = {"text": "", "tools_used": [], "files_modified": [], "events_count": 1}
    mock_pi.side_effect = [
        {
            **base,
            "status": "error",
            "exit_code": 1,
            "error": "boom",
            "usage": {
                "provider": "anthropic",
                "input_tokens": 10,
                "output_tokens": 2,
                "cost_usd": 1.0,
            },
            "billing": "subscription",
        },
        {**base, "status": "success", "text": "ok", "exit_code": 0, "error": None, "usage": None},
    ]
    task_id = delegate_tool.delegate(
        task_spec="reserve", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    first.poll_once()
    conn = storage.connect()
    try:
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[
            0
        ]
    finally:
        conn.close()
    assert run_records.get_run(run_id)["usage"]["billing"] == "subscription"

    second = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    second.backoff_base_s = 0

    def unreadable(*a, **k):
        raise ValueError("corrupt totals")

    monkeypatch.setattr(second, "_accumulate_run_usage", unreadable)
    second.poll_once()

    run = run_records.get_run(run_id)
    assert run["usage"]["billing"] == "api"
    assert run["usage"]["cost_complete"] is False
    assert budgets._classify(run["usage"]) == "unknown_cost"
    # An accounting failure holds the run open (no finish yet), so the budget's
    # open-run accounting is what must keep it charged.
    status = budgets.budget_status("Desktop_agent3")
    agy = status["budgets"][0]["agents_status"][0]
    # The unknown-cost run holds its reserve; the $1 estimate is a known lower bound.
    assert agy["committed_usd"] >= 3.0


def test_codex_output_is_charged_to_the_model_active_at_each_total(tmp_path):
    # Window 1: model-a +51, then model-b +49. Window 2: model-a +60.
    # model-a produced 111 of 160, so the run is named for it (task #2037).
    rollout = tmp_path / "sessions" / "2026" / "10" / "14" / f"rollout-x-{CODEX_SESSION}.jsonl"

    def ctx(ts, model):
        return {"type": "turn_context", "timestamp": ts, "payload": {"model": model}}

    _write(
        rollout,
        [
            _token_count("2026-10-14T11:59:00Z", 100, 0, 1000),
            ctx("2026-10-14T12:00:00Z", "model-a"),
            _token_count("2026-10-14T12:01:00Z", 200, 0, 1051),
            ctx("2026-10-14T12:02:00Z", "model-b"),
            _token_count("2026-10-14T12:03:00Z", 300, 0, 1100),
            _token_count("2026-10-14T12:15:00Z", 300, 0, 1100),
            ctx("2026-10-14T12:20:00Z", "model-a"),
            _token_count("2026-10-14T12:21:00Z", 400, 0, 1160),
        ],
    )
    t = datetime.fromisoformat
    usage = harness_usage.measure(
        "codex",
        CODEX_SESSION,
        windows=[
            (t("2026-10-14T12:00:00+00:00"), t("2026-10-14T12:10:00+00:00")),
            (t("2026-10-14T12:20:00+00:00"), t("2026-10-14T12:30:00+00:00")),
        ],
        codex_dir=tmp_path,
    )
    assert (usage["model"], usage["output_tokens"]) == ("model-a", 160)
