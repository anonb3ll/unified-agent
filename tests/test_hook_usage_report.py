"""The Claude Code usage-report hook and ``ua-usage --report`` (task #1837).

The hook remembers the run a claim opens, and when ``task_update`` ends it (or
the session ends) starts a detached ``ua-usage --report`` that records the
session's measured usage on that run. The tool reply shape is the one Claude
Code passes for an MCP tool: a list of ``{"type": "text", "text": <json>}``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

from unified_agent import harness_usage

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hook_usage_report", REPO_ROOT / "scripts" / "hook_usage_report.py"
)
hook = importlib.util.module_from_spec(spec)
sys.modules["hook_usage_report"] = hook
spec.loader.exec_module(hook)

SESSION = "a71249b6-e1b3-46d2-8c6e-5dcad1ef963d"
CLAIM = {
    "id": 1837,
    "status": "doing",
    "claimed_at": "2026-10-14T02:37:29.780020+00:00",
    "current_run_id": "sp-01M4W4B89VVDBN1M9GSWD1H060",
    "current_run_agent": "Desktop_Agent1",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("UA_NODE_CLAUDE_PROJECTS", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / ".cache" / "ua-usage-hook")
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unified-agent": {
                        "url": "http://hub.test:8765/mcp",
                        "headers": {"Authorization": "Bearer tok-abc"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "ua-usage"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("UA_USAGE_BIN", str(binary))
    spawned = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append({"argv": argv, **kwargs})

    monkeypatch.setattr(hook.subprocess, "Popen", FakePopen)
    return spawned


def _run(monkeypatch, payload) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hook.main()


def _tool(name, params, reply):
    return {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": f"mcp__unified-agent__{name}",
        "tool_input": params,
        "tool_response": [{"type": "text", "text": json.dumps(reply)}],
    }


def _arg(argv, flag):
    if flag in ("--since", "--until"):
        # The hook passes windows as --window START,END pairs; the newest
        # window is the one this report adds.
        start, end = _all_windows(argv)[-1]
        return start if flag == "--since" else end
    return argv[argv.index(flag) + 1]


def _all_windows(argv):
    return [tuple(argv[i + 1].split(",", 1)) for i, a in enumerate(argv) if a == "--window"]


def _state():
    path = hook.STATE_DIR / f"{SESSION}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _open_tasks():
    return [t for t in _state() if not t.startswith("_")]


def _windows(spawned):
    return [(_arg(x["argv"], "--since"), _arg(x["argv"], "--until")) for x in spawned]


def _disjoint(spawned):
    spans = sorted(_windows(spawned))
    return all(a[1] <= b[0] for a, b in zip(spans, spans[1:], strict=False))


def test_closing_a_claimed_run_reports_its_window(env, monkeypatch):
    assert _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM)) == 0
    assert env == []
    done = {**CLAIM, "status": "done", "current_run_id": None}
    assert _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, done)) == 0
    assert len(env) == 1
    argv = env[0]["argv"]
    assert _arg(argv, "--since") == CLAIM["claimed_at"]
    assert _arg(argv, "--run") == CLAIM["current_run_id"]
    assert _arg(argv, "--agent") == "Desktop_Agent1"
    assert _arg(argv, "--task") == "1837"
    assert _arg(argv, "--session") == SESSION
    assert "--report" in argv and "--billing" not in argv
    # The bearer reaches the child through its environment only.
    assert env[0]["env"]["UA_AGENT_BEARER_TOKEN"] == "tok-abc"
    assert env[0]["env"]["UA_HTTP_HUB_URL"] == "http://hub.test:8765/mcp"
    assert not any("tok-abc" in a for a in argv)
    assert env[0]["start_new_session"] is True
    assert _open_tasks() == []


def test_a_renewal_keeps_the_original_window(env, monkeypatch):
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    renewed = {**CLAIM, "claimed_at": "2026-10-14T03:00:00+00:00"}
    _run(monkeypatch, _tool("task_claim", {"id": 1837, "expected_run_id": "x"}, renewed))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "blocked"}, renewed))
    assert _arg(env[0]["argv"], "--since") == CLAIM["claimed_at"]


def test_usage_only_accounting_is_not_a_claim(env, monkeypatch):
    _run(monkeypatch, _tool("task_claim", {"id": 1837, "usage_only": True}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, CLAIM))
    assert env == []


def test_entering_doing_through_task_update_opens_a_window(env, monkeypatch):
    opened = {**CLAIM, "verified_caller": "Desktop_Agent1"}
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "doing"}, opened))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "waiting_review"}, opened))
    assert len(env) == 1


def test_an_update_that_keeps_the_run_does_not_report(env, monkeypatch):
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "notes": "progress"}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 99, "status": "done"}, {"id": 99}))
    assert env == []


def test_session_end_reports_every_open_run(env, monkeypatch):
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    other = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T02:50:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, other))
    end = {"session_id": SESSION, "hook_event_name": "SessionEnd", "reason": "exit"}
    assert _run(monkeypatch, end) == 0
    assert sorted(_arg(s["argv"], "--task") for s in env) == ["1837", "2034"]
    assert _open_tasks() == []


def test_a_released_claim_ends_its_window(env, monkeypatch):
    # task_update(release_claim=True) closes the run without a status change.
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    released = {**CLAIM, "current_run_id": None, "current_run_agent": None}
    _run(monkeypatch, _tool("task_update", {"id": 1837, "release_claim": True}, released))
    assert len(env) == 1 and _arg(env[0]["argv"], "--run") == CLAIM["current_run_id"]
    assert _open_tasks() == []


def test_a_newer_claim_ends_the_older_window_so_calls_count_once(env, monkeypatch):
    # One window at a time per session: calls after B's claim belong to B only.
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    later = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T02:50:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, later))
    assert len(env) == 1
    assert _arg(env[0]["argv"], "--task") == "1837"
    assert _arg(env[0]["argv"], "--until") == later["claimed_at"]
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, CLAIM))
    assert len(env) == 1  # the older run was already reported up to B's claim
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "done"}, later))
    assert [_arg(s["argv"], "--task") for s in env] == ["1837", "2034"]
    assert _arg(env[1]["argv"], "--since") == later["claimed_at"]


def test_renewing_a_superseded_run_never_reopens_covered_time(env, monkeypatch):
    # Claim A 12:00, claim B 12:10, finish B, renew A, finish A: A must not
    # report the 12:10-onward span again.
    a = {**CLAIM, "claimed_at": "2026-10-14T12:00:00+00:00"}
    b = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T12:10:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, a))
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, b))
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "done"}, b))
    _run(monkeypatch, _tool("task_claim", {"id": 1837, "expected_run_id": a["current_run_id"]}, a))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, a))
    assert _disjoint(env)
    assert _windows(env)[0] == ("2026-10-14T12:00:00+00:00", "2026-10-14T12:10:00+00:00")


def test_a_replacement_run_shares_one_boundary_with_the_old_one(env, monkeypatch):
    # A takeover reply arrives late (local now > hub claimed_at): the old window
    # ends at the replacement's claimed_at, not at the later local time.
    a = {**CLAIM, "claimed_at": "2026-10-14T12:00:00+00:00"}
    replacement = {
        **a,
        "current_run_id": "sp-01M4W4B8W9Y55N2ME01J587954",
        "claimed_at": "2026-10-14T12:10:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, a))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "notes": "x"}, replacement))
    assert _windows(env) == [("2026-10-14T12:00:00+00:00", "2026-10-14T12:10:00+00:00")]


def test_a_window_entirely_before_the_cutoff_is_not_reported(env, monkeypatch):
    late = {**CLAIM, "claimed_at": "2026-10-14T12:10:00+00:00"}
    early = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T12:00:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, late))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, late))
    # A clock running backwards cannot reopen time already reported.
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, early))
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "done"}, early))
    assert _disjoint(env)


def test_a_resumed_session_cannot_re_report_covered_time(env, monkeypatch):
    # Claim A 12:00, claim B 12:10, exit; resume and renew the still-live A.
    a = {**CLAIM, "claimed_at": "2026-10-14T12:00:00+00:00"}
    b = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T12:10:00+00:00",
    }
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, a))
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, b))
    _run(monkeypatch, {"session_id": SESSION, "hook_event_name": "SessionEnd"})
    _run(monkeypatch, _tool("task_claim", {"id": 1837, "expected_run_id": a["current_run_id"]}, a))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, a))
    assert _disjoint(env)


def test_a_run_opened_without_claimed_at_starts_when_first_seen(env, monkeypatch):
    # task_update(status="doing") opens a run without a claim time.
    unclaimed = {**CLAIM, "claimed_at": None, "verified_caller": "Desktop_Agent1"}
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "doing"}, unclaimed))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, unclaimed))
    assert len(env) == 1
    since, until = _windows(env)[0]
    assert hook._when(since) is not None and since <= until


def test_windows_keep_sub_second_precision(env, monkeypatch):
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, CLAIM))
    # A call in the closing second must not fall outside a truncated window.
    assert "." in _arg(env[0]["argv"], "--until")


def test_ua_usage_is_found_on_path_when_not_in_the_checkout(env, monkeypatch, tmp_path):
    monkeypatch.delenv("UA_USAGE_BIN")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "ua-usage"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, CLAIM))
    assert env and env[0]["argv"][0] == str(exe)


def _me(runs):
    runs.setdefault(hook.SELF, CLAIM["current_run_agent"])
    return runs


def test_concurrent_hooks_never_lose_a_remembered_run(env, monkeypatch):
    # Two claim hooks that read the same state before either writes: the
    # session lock serializes the read-modify-write.
    import threading

    started, release = threading.Event(), threading.Event()
    real_load = hook._load

    def slow_load(path):
        data = real_load(path)
        if not started.is_set():
            started.set()
            release.wait(2)
        return data

    monkeypatch.setattr(hook, "_load", slow_load)
    other = {
        **CLAIM,
        "id": 2034,
        "current_run_id": "sp-01M4W4B8QGKJZZQDRHQS6DQEG9",
        "claimed_at": "2026-10-14T02:50:00+00:00",
    }
    a = threading.Thread(
        target=lambda: hook._transact(
            SESSION, lambda runs, now: hook._remember(SESSION, _me(runs), CLAIM, now)
        )
    )
    a.start()
    started.wait(2)
    b = threading.Thread(
        target=lambda: hook._transact(
            SESSION, lambda runs, now: hook._remember(SESSION, _me(runs), other, now)
        )
    )
    b.start()
    release.set()
    a.join(5)
    b.join(5)
    state = json.loads((hook.STATE_DIR / f"{SESSION}.json").read_text())
    # B's claim is newer, so A's window was reported and B is the open run.
    assert [t for t in state if not t.startswith("_")] == ["2034"]
    assert [_arg(s["argv"], "--task") for s in env] == ["1837"]


@pytest.mark.parametrize(
    "reply",
    [
        {"id": 1837},
        {**CLAIM, "current_run_id": "not-a-run"},
        {**CLAIM, "id": "1837"},
    ],
)
def test_a_reply_without_a_usable_run_is_ignored(env, monkeypatch, reply):
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, reply))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, reply))
    assert env == []


def test_outside_claude_code_or_without_config_nothing_runs(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDECODE", "0")
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    assert not hook.STATE_DIR.exists()
    monkeypatch.setenv("CLAUDECODE", "1")
    (tmp_path / ".claude.json").unlink()
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, CLAIM))
    assert env == []


@pytest.mark.parametrize("stdin", ["", "not json", json.dumps({"session_id": "../x"})])
def test_malformed_input_exits_zero(env, monkeypatch, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert hook.main() == 0
    assert env == []


# --- ua-usage --report ---------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "projects" / "-p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-10-14T02:40:00Z",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "projects"))


ARGS = [
    "--harness",
    "claude",
    "--session",
    SESSION,
    "--since",
    "2026-10-14T02:37:29Z",
    "--report",
    "--task",
    "1837",
    "--run",
    "sp-01M4W4B89VVDBN1M9GSWD1H060",
    "--agent",
    "Desktop_Agent1",
]


def test_report_records_late_accounting_on_the_run(store, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(harness_usage, "report", lambda *a: calls.append(a) or {"id": 1837})
    assert harness_usage.main(ARGS) == 0
    task, run, agent, usage = calls[0]
    assert (task, run, agent) == (1837, "sp-01M4W4B89VVDBN1M9GSWD1H060", "Desktop_Agent1")
    assert usage["output_tokens"] == 4 and "billing" not in usage
    assert json.loads(capsys.readouterr().out) == usage


def test_report_calls_task_claim_usage_only(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, agent_label=None, allow_fallback=True, **_):
            seen.update(agent=agent_label, fallback=allow_fallback)

        def call_tool(self, name, arguments):
            seen.update(name=name, arguments=arguments)
            return {}

    from unified_agent import hub_client

    monkeypatch.setattr(hub_client, "HubClient", FakeClient)
    harness_usage.report(7, "sp-X", "Desktop_Agent1", {"input_tokens": 1})
    assert seen == {
        "agent": "Desktop_Agent1",
        "fallback": False,
        "name": "task_claim",
        "arguments": {
            "id": 7,
            "agent": "Desktop_Agent1",
            "expected_run_id": "sp-X",
            "run_usage": {"input_tokens": 1},
            "usage_only": True,
        },
    }


@pytest.mark.parametrize(
    ("exc", "shown"),
    [
        (RuntimeError("hub auth rejected for agent 'X' (HTTP 401) calling task_claim"), "HTTP 401"),
        (ValueError("Invalid header value b'Bearer sk-secret\\n'"), "ValueError"),
    ],
)
def test_report_failure_exits_three_without_a_secret(store, monkeypatch, capsys, exc, shown):
    def boom(*a):
        raise exc

    monkeypatch.setattr(harness_usage, "report", boom)
    assert harness_usage.main(ARGS) == 3
    err = capsys.readouterr().err
    assert "report failed" in err and shown in err
    # Exception text can carry the rejected Authorization value; never echo it.
    assert "sk-secret" not in err and "Bearer" not in err


def test_report_needs_its_run_coordinates(store, capsys):
    assert harness_usage.main(ARGS[:7]) == 2
    assert "--report needs" in capsys.readouterr().err


def test_the_hook_runs_on_the_oldest_supported_system_python():
    # The hook runs under the host's system python3, which is 3.9 on macOS
    # (Command Line Tools). It must parse and import there: no syntax or stdlib
    # names newer than 3.9, such as datetime.UTC (3.11).
    import ast

    source = (REPO_ROOT / "scripts" / "hook_usage_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            assert "UTC" not in {a.name for a in node.names}
        if isinstance(node, ast.Attribute) and node.attr == "UTC":
            raise AssertionError("datetime.UTC is Python 3.11+")


# --- task #2037 refinements ------------------------------------------------------


class _Clock:
    """Freezes the hook's clock; ``at`` sets the next event's time."""

    def __init__(self, monkeypatch):
        import datetime as dt

        clock = self
        self.now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)

        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now

        monkeypatch.setattr(hook, "datetime", Frozen)

    def at(self, hhmm):
        import datetime as dt

        h, m = map(int, hhmm.split(":"))
        self.now = dt.datetime(2026, 10, 14, h, m, tzinfo=dt.UTC)


def _claim(task, run, hhmm):
    return {
        "id": task,
        "status": "doing",
        "claimed_at": f"2026-10-14T{hhmm}:00+00:00",
        "current_run_id": run,
        "current_run_agent": "Desktop_Agent1",
    }


RUN_A = "sp-01M4W4B89VVDBN1M9GSWD1H060"
RUN_B = "sp-01M4W4B8QGKJZZQDRHQS6DQEG9"


def test_resumed_session_with_a_frozen_clock_reports_exact_disjoint_windows(env, monkeypatch):
    # Claim A 12:00, claim B 12:10, exit 12:20; resume and renew A at 12:25 and
    # finish it at 12:30. A gets [12:00,12:10) then, on finish, [12:20,12:30)
    # added to its earlier window, so the hub's max is the cumulative total.
    clock = _Clock(monkeypatch)
    a, b = _claim(1837, RUN_A, "12:00"), _claim(2034, RUN_B, "12:10")
    clock.at("12:00")
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, a))
    clock.at("12:10")
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, b))
    clock.at("12:20")
    _run(monkeypatch, {"session_id": SESSION, "hook_event_name": "SessionEnd"})
    clock.at("12:25")
    _run(monkeypatch, _tool("task_claim", {"id": 1837, "expected_run_id": RUN_A}, a))
    clock.at("12:30")
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "done"}, a))
    reports = [(_arg(s["argv"], "--run"), _all_windows(s["argv"])) for s in env]
    t = "2026-10-14T{}:00+00:00".format
    assert reports == [
        (RUN_A, [(t("12:00"), t("12:10"))]),
        (RUN_B, [(t("12:10"), t("12:20"))]),
        (RUN_A, [(t("12:00"), t("12:10")), (t("12:20"), t("12:30"))]),
    ]


def test_a_reply_naming_another_agents_run_never_replaces_this_sessions_window(env, monkeypatch):
    clock = _Clock(monkeypatch)
    clock.at("12:00")
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, _claim(1837, RUN_A, "12:00")))
    foreign = {**_claim(2034, RUN_B, "12:05"), "current_run_agent": "server_agent2"}
    clock.at("12:05")
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "doing"}, foreign))
    assert env == []  # our window was not cut at the foreign claim
    assert _open_tasks() == ["1837"]
    clock.at("12:30")
    _run(
        monkeypatch,
        _tool("task_update", {"id": 1837, "status": "done"}, _claim(1837, RUN_A, "12:00")),
    )
    assert _all_windows(env[0]["argv"]) == [
        ("2026-10-14T12:00:00+00:00", "2026-10-14T12:30:00+00:00")
    ]


def test_prune_drops_only_old_state_whose_session_cannot_resume(env, monkeypatch, tmp_path):
    import os
    import time

    store = tmp_path / "claude"
    (store / "projects" / "-p").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(store))
    hook.STATE_DIR.mkdir(parents=True)
    old = time.time() - hook.PRUNE_AFTER_S - 60
    gone = "aaaaaaaa-0000-0000-0000-000000000001"
    alive = "aaaaaaaa-0000-0000-0000-000000000002"
    recent = "aaaaaaaa-0000-0000-0000-000000000003"
    for sid in (gone, alive, recent):
        (hook.STATE_DIR / f"{sid}.json").write_text("{}")
        (hook.STATE_DIR / f"{sid}.lock").write_text("")
    for sid in (gone, alive):
        os.utime(hook.STATE_DIR / f"{sid}.json", (old, old))
    (store / "projects" / "-p" / f"{alive}.jsonl").write_text("")
    hook._prune()
    assert not (hook.STATE_DIR / f"{gone}.json").exists()
    assert not (hook.STATE_DIR / f"{gone}.lock").exists()
    assert (hook.STATE_DIR / f"{alive}.json").exists()
    assert (hook.STATE_DIR / f"{recent}.json").exists()
    # At most once a day: a second call within the interval does nothing.
    (hook.STATE_DIR / f"{gone}.json").write_text("{}")
    os.utime(hook.STATE_DIR / f"{gone}.json", (old, old))
    hook._prune()
    assert (hook.STATE_DIR / f"{gone}.json").exists()


def test_ua_usage_sums_disjoint_windows(tmp_path, monkeypatch, capsys):
    path = tmp_path / "projects" / "-p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)

    def msg(ts, mid, out):
        return json.dumps(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {"id": mid, "model": "claude-opus-5-5", "usage": {"output_tokens": out}},
            }
        )

    path.write_text(
        "\n".join(
            [
                msg("2026-10-14T12:05:00Z", "m1", 100),
                msg("2026-10-14T12:15:00Z", "m2", 7),
                msg("2026-10-14T12:25:00Z", "m3", 30),
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    code = harness_usage.main(
        [
            "--harness",
            "claude",
            "--session",
            SESSION,
            "--window",
            "2026-10-14T12:00:00Z,2026-10-14T12:10:00Z",
            "--window",
            "2026-10-14T12:20:00Z,2026-10-14T12:30:00Z",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["output_tokens"] == 130  # m2 is outside both


def test_ua_usage_needs_exactly_one_way_to_give_the_window(capsys):
    assert harness_usage.main(["--harness", "claude", "--session", SESSION]) == 2
    assert (
        harness_usage.main(
            [
                "--harness",
                "claude",
                "--session",
                SESSION,
                "--since",
                "2026-10-14T12:00:00Z",
                "--window",
                "2026-10-14T12:00:00Z,2026-10-14T12:10:00Z",
            ]
        )
        == 2
    )
    assert harness_usage.main(["--session", SESSION, "--window", "no-comma"]) == 2


# --- review round 1 on PR #1168 --------------------------------------------------


def test_ua_usage_refuses_overlapping_reversed_or_mixed_windows(capsys):
    import datetime as dt

    w = "2026-10-14T12:00:00Z,2026-10-14T12:10:00Z"
    base = ["--harness", "claude", "--session", SESSION]
    assert harness_usage.main([*base, "--window", w, "--window", w]) == 2
    partial = "2026-10-14T12:05:00Z,2026-10-14T12:20:00Z"
    assert harness_usage.main([*base, "--window", w, "--window", partial]) == 2
    assert harness_usage.main([*base, "--window", "2026-10-14T12:10:00Z,2026-10-14T12:00:00Z"]) == 2
    assert harness_usage.main([*base, "--window", w, "--until", "2026-10-14T13:00:00Z"]) == 2
    t = dt.datetime(2026, 10, 14, 12, tzinfo=dt.UTC)
    with pytest.raises(ValueError):
        harness_usage.measure(
            "claude", SESSION, windows=[(t, None), (t + dt.timedelta(hours=1), None)]
        )


def test_an_update_first_session_ignores_a_foreign_run_it_never_owned(env, monkeypatch):
    clock = _Clock(monkeypatch)
    own = {
        **_claim(1837, RUN_A, "12:00"),
        "claimed_at": None,
        "claimed_by": None,
        "verified_caller": "Desktop_Agent1",
    }
    clock.at("12:00")
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "doing"}, own))
    assert _open_tasks() == ["1837"]
    foreign = {
        **_claim(2034, RUN_B, "12:05"),
        "current_run_agent": "server_agent2",
        "claimed_by": None,
        "verified_caller": "Desktop_Agent1",
    }
    clock.at("12:05")
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "doing"}, foreign))
    assert env == []
    assert _open_tasks() == ["1837"]


def test_a_claimed_task_update_teaches_no_identity(env, monkeypatch):
    # task_update(status="doing") on a task someone holds opens the run for the
    # claimant, who may not be this session: with no identity yet, nothing is
    # remembered.
    clock = _Clock(monkeypatch)
    held = {
        **_claim(2034, RUN_B, "12:05"),
        "current_run_agent": "server_agent2",
        "claimed_by": "server_agent2",
    }
    clock.at("12:05")
    _run(monkeypatch, _tool("task_update", {"id": 2034, "status": "doing"}, held))
    assert _open_tasks() == []
    assert hook.SELF not in _state()


def test_a_lock_unlinked_while_held_hands_waiters_a_fresh_file(env, monkeypatch):
    import threading
    import time

    refused = threading.Event()

    class _NotingTime:
        """The hook's clock; its retry sleep means a lock attempt was refused."""

        def sleep(self, seconds):
            refused.set()
            time.sleep(seconds)

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(hook, "time", _NotingTime())
    hook.STATE_DIR.mkdir(parents=True)
    holder = hook._lock(SESSION, 0.0)
    orphan = os.fstat(holder.fileno()).st_ino
    # An unlocked handle keeps the orphan's inode allocated: tmpfs reuses a freed
    # inode number at once, so the fresh lock file could otherwise share it.
    keep_orphan = open(hook.STATE_DIR / f"{SESSION}.lock", "rb")  # noqa: SIM115
    got = {}
    waiter = threading.Thread(target=lambda: got.setdefault("lock", hook._lock(SESSION, 3.0)))
    waiter.start()
    assert refused.wait(5)  # the waiter is now blocked on the held file
    (hook.STATE_DIR / f"{SESSION}.lock").unlink()  # as _prune does, while holding
    holder.close()
    waiter.join(5)
    lock = got["lock"]
    path = hook.STATE_DIR / f"{SESSION}.lock"
    assert lock is not None
    assert os.fstat(lock.fileno()).st_ino == os.stat(path).st_ino
    assert os.fstat(lock.fileno()).st_ino != orphan  # never the unlinked file
    assert hook._lock(SESSION, 0.0) is None  # one holder at a time
    lock.close()
    keep_orphan.close()


def _stale_state(sid, runs=None):
    import time

    hook.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = hook.STATE_DIR / f"{sid}.json"
    path.write_text(json.dumps(runs or {"_cursor": "2026-09-01T00:00:00+00:00"}))
    old = time.time() - hook.PRUNE_AFTER_S - 60
    os.utime(path, (old, old))
    return path


def test_prune_skips_a_session_whose_lock_is_held(env):
    state = _stale_state(SESSION)
    held = hook._lock(SESSION, 0.0)
    hook._prune()
    held.close()
    assert state.exists()


def test_prune_honors_the_projects_override_and_the_recorded_store(env, monkeypatch, tmp_path):
    override = tmp_path / "node-projects"
    (override / "-p").mkdir(parents=True)
    recorded = tmp_path / "old-projects"
    (recorded / "-p").mkdir(parents=True)
    by_override = "bbbbbbbb-0000-0000-0000-000000000001"
    by_record = "bbbbbbbb-0000-0000-0000-000000000002"
    (override / "-p" / f"{by_override}.jsonl").write_text("")
    (recorded / "-p" / f"{by_record}.jsonl").write_text("")
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(override))
    a = _stale_state(by_override)
    b = _stale_state(by_record, {"_cursor": "2026-09-01T00:00:00+00:00", "_store": str(recorded)})
    hook._prune()
    assert a.exists() and b.exists()


def test_a_remembered_run_records_its_transcript_store(env, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "np"))
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, CLAIM))
    assert _state()[hook.STORE] == str(tmp_path / "np")


def test_no_window_is_ever_dropped(env, monkeypatch):
    clock = _Clock(monkeypatch)
    a, b = _claim(1837, RUN_A, "12:00"), _claim(2034, RUN_B, "12:00")
    for i, hhmm in enumerate(("12:00", "12:10", "12:20", "12:30", "12:40", "12:50")):
        clock.at(hhmm)
        # alternate claims so RUN_A is superseded and renewed three times
        reply = a if i % 2 == 0 else {**b, "claimed_at": f"2026-10-14T{hhmm}:00+00:00"}
        params = {"id": reply["id"]}
        if i % 2 == 0 and i:
            params["expected_run_id"] = RUN_A
        _run(monkeypatch, _tool("task_claim", params, reply))
    runs_a = [s for s in env if _arg(s["argv"], "--run") == RUN_A]
    assert len(_all_windows(runs_a[-1]["argv"])) == 3


def test_a_reassert_of_another_agents_run_teaches_only_the_verified_caller(env, monkeypatch):
    # Agent A opened an unclaimed task's run moments ago; this session (B)
    # reasserts doing on it. The reply names A's run with no claimant, but the
    # hub's verified_caller says B: B never takes A's label or A's run.
    clock = _Clock(monkeypatch)
    clock.at("12:01")
    foreign = {
        **_claim(1837, RUN_A, "12:00"),
        "claimed_at": None,
        "claimed_by": None,
        "current_run_agent": "server_agent2",
        "verified_caller": "Desktop_Agent1",
    }
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "doing"}, foreign))
    assert _state()[hook.SELF] == "Desktop_Agent1"
    assert _open_tasks() == []
    clock.at("12:05")
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, _claim(2034, RUN_B, "12:05")))
    assert _open_tasks() == ["2034"]


def test_an_older_hub_learns_identity_only_from_a_claim(env, monkeypatch):
    # Without verified_caller, a task_update reply could name anyone's run.
    clock = _Clock(monkeypatch)
    clock.at("12:00")
    unknown = {**_claim(1837, RUN_A, "12:00"), "claimed_at": None, "claimed_by": None}
    _run(monkeypatch, _tool("task_update", {"id": 1837, "status": "doing"}, unknown))
    assert hook.SELF not in _state()
    assert _open_tasks() == []
    clock.at("12:05")
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, _claim(2034, RUN_B, "12:05")))
    assert _state()[hook.SELF] == "Desktop_Agent1"
    assert _open_tasks() == ["2034"]


def test_the_verified_caller_overrides_a_label_learned_from_an_older_reply(env, monkeypatch):
    clock = _Clock(monkeypatch)
    clock.at("12:00")
    stale = {**_claim(1837, RUN_A, "12:00"), "current_run_agent": "server_agent2"}
    _run(monkeypatch, _tool("task_claim", {"id": 1837}, stale))
    assert _state()[hook.SELF] == "server_agent2"
    clock.at("12:05")
    fixed = {**_claim(2034, RUN_B, "12:05"), "verified_caller": "Desktop_Agent1"}
    _run(monkeypatch, _tool("task_claim", {"id": 2034}, fixed))
    assert _state()[hook.SELF] == "Desktop_Agent1"
    assert "2034" in _open_tasks()


def test_summed_windows_are_named_for_the_model_with_the_most_output(tmp_path, monkeypatch, capsys):
    path = tmp_path / "projects" / "-p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)

    def msg(ts, mid, model, out):
        usage = {"output_tokens": out}
        return json.dumps(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {"id": mid, "model": model, "usage": usage},
            }
        )

    lines = [
        msg("2026-10-14T12:05:00Z", "m1", "claude-opus-5-5", 900),
        msg("2026-10-14T12:25:00Z", "m2", "claude-haiku-5-5", 10),
    ]
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    code = harness_usage.main(
        [
            "--harness",
            "claude",
            "--session",
            SESSION,
            "--window",
            "2026-10-14T12:00:00Z,2026-10-14T12:10:00Z",
            "--window",
            "2026-10-14T12:20:00Z,2026-10-14T12:30:00Z",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] == "claude-opus-5-5"
    assert out["output_tokens"] == 910


def test_models_are_tallied_across_windows_before_the_run_is_named(tmp_path, monkeypatch, capsys):
    # Window 1: A=51, B=49 (A wins there). Window 2: B=60. Totals A=51, B=109.
    path = tmp_path / "projects" / "-p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)

    def msg(ts, mid, model, out):
        usage = {"output_tokens": out}
        return json.dumps(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {"id": mid, "model": model, "usage": usage},
            }
        )

    lines = [
        msg("2026-10-14T12:01:00Z", "m1", "model-a", 51),
        msg("2026-10-14T12:02:00Z", "m2", "model-b", 49),
        msg("2026-10-14T12:21:00Z", "m3", "model-b", 60),
    ]
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    code = harness_usage.main(
        [
            "--harness",
            "claude",
            "--session",
            SESSION,
            "--window",
            "2026-10-14T12:00:00Z,2026-10-14T12:10:00Z",
            "--window",
            "2026-10-14T12:20:00Z,2026-10-14T12:30:00Z",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["model"], out["output_tokens"]) == ("model-b", 160)
