"""Harness usage from executor runs reaches budgets and cycles (task #1917).

The executor launched Claude Code and Codex runs without reporting what they
cost, so every such run counted only ``run_reserve_usd`` against a budget and
fed no tokens or cost to its autonomous cycle. These tests cover the three
links: each backend parses usage from its own output, the parsed usage is
normalized into the ``run_usage`` fields ``run_records`` validates, and the
daemon reports it on the run's heartbeat and to the bound cycle.

The Codex and Agy streams reuse the shapes recorded in
``test_executor_backends.py``. The Claude ``result`` event follows the
documented ``claude -p --output-format stream-json`` result message
(``total_cost_usd``, ``usage`` with ``cache_*_input_tokens``, ``modelUsage``);
the parser reads it defensively, so a missing field yields ``None``, never 0.
"""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import patch

import pytest

from unified_agent import config, run_records, storage
from unified_agent.executor import backends
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import cycles as cycles_tool
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import tasks as tasks_tool

CLAUDE_STREAM = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "claude-opus-4-7",
                "tools": ["Bash"],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "working"}],
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "num_turns": 2,
                "total_cost_usd": 0.0123,
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 3000,
                    "output_tokens": 50,
                },
                "modelUsage": {"claude-opus-4-7": {"costUSD": 0.0123}},
            }
        ),
    ]
)

CODEX_STREAM = "\n".join(
    [
        "Reading additional input from stdin...",
        json.dumps({"type": "thread.started", "thread_id": "019fa5d7-374f"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_4", "type": "agent_message", "text": "done"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 13661,
                    "cached_input_tokens": 12000,
                    "output_tokens": 5,
                },
            }
        ),
    ]
)


# --- parsing -----------------------------------------------------------------


class TestClaudeUsage:
    def test_result_event_carries_cost_tokens_and_model(self):
        usage = backends.parse_claude_stream_json(CLAUDE_STREAM)["usage"]
        assert usage == {
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            # Total prompt tokens: uncached input plus cache writes and reads.
            "input_tokens": 3210,
            "output_tokens": 50,
            "cache_read_tokens": 3000,
            "cache_write_tokens": 200,
            "cost_usd": 0.0123,
        }

    def test_result_without_cost_leaves_cost_unknown(self):
        stream = json.dumps(
            {
                "type": "result",
                "result": "done",
                "is_error": False,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        )
        usage = backends.parse_claude_stream_json(stream)["usage"]
        assert usage["cost_usd"] is None
        assert (usage["input_tokens"], usage["output_tokens"]) == (7, 3)

    def test_interrupted_run_sums_assistant_usage_once_per_message(self):
        # A killed run has no result event. Claude Code repeats one message's
        # usage on every content-block event; count each message id once.
        msg = {"id": "msg_1", "content": [], "usage": {"input_tokens": 100, "output_tokens": 5}}
        later = {
            "id": "msg_1",
            "content": [],
            "usage": {"input_tokens": 100, "output_tokens": 9, "cache_read_input_tokens": 40},
        }
        second = {"id": "msg_2", "content": [], "usage": {"input_tokens": 30, "output_tokens": 1}}
        stream = "\n".join(
            json.dumps({"type": "assistant", "message": m}) for m in (msg, later, second)
        )
        usage = backends.parse_claude_stream_json(stream)["usage"]
        assert usage["input_tokens"] == 170
        assert usage["output_tokens"] == 10
        assert usage["cache_read_tokens"] == 40
        assert usage["cost_usd"] is None

    def test_no_usage_anywhere_is_none(self):
        stream = json.dumps({"type": "result", "result": "done", "is_error": False})
        assert backends.parse_claude_stream_json(stream)["usage"] is None

    @pytest.mark.parametrize("bad", [-1, "12", True, float("nan"), float("inf")])
    def test_malformed_numbers_are_unknown_not_zero(self, bad):
        stream = json.dumps(
            {
                "type": "result",
                "result": "x",
                "total_cost_usd": bad,
                "usage": {"input_tokens": bad, "output_tokens": 4},
            }
        )
        usage = backends.parse_claude_stream_json(stream)["usage"]
        assert usage["cost_usd"] is None
        assert usage["input_tokens"] is None
        assert usage["output_tokens"] == 4


class TestCodexUsage:
    def test_turn_completed_usage_is_parsed(self):
        usage = backends.parse_codex_jsonl(CODEX_STREAM)["usage"]
        assert usage == {
            "provider": "openai",
            "model": None,
            # Codex's input_tokens already includes the cached part.
            "input_tokens": 13661,
            "output_tokens": 5,
            "cache_read_tokens": 12000,
            "cache_write_tokens": None,
            "cost_usd": None,
        }

    def test_turns_are_summed(self):
        stream = "\n".join(
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": n, "output_tokens": 1}})
            for n in (10, 20)
        )
        usage = backends.parse_codex_jsonl(stream)["usage"]
        assert (usage["input_tokens"], usage["output_tokens"]) == (30, 2)

    def test_empty_usage_is_none(self):
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        assert backends.parse_codex_jsonl(stream)["usage"] is None


def test_agy_result_usage_is_parsed():
    stream = json.dumps(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "3",
                "usage": {"input_tokens": 51805, "output_tokens": 985},
            },
        }
    )
    usage = backends.parse_agy_stream_json(stream)["usage"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (51805, 985)
    assert usage["cost_usd"] is None


def test_resumed_invocations_sum_and_unknown_cost_stays_unknown():
    # Agy auto-resume runs several invocations; their spend adds up. One
    # unpriced invocation makes the total cost unknown, never a partial sum.
    a = {"provider": "anthropic", "input_tokens": 10, "output_tokens": 1, "cost_usd": 0.5}
    b = {"provider": None, "input_tokens": 5, "output_tokens": None, "cost_usd": None}
    total = backends.add_harness_usage(a, b)
    assert (total["input_tokens"], total["output_tokens"]) == (15, 1)
    assert total["cost_usd"] is None
    assert total["provider"] == "anthropic"
    assert backends.add_harness_usage(None, b) is b
    priced = backends.add_harness_usage(a, {**a, "cost_usd": 0.25})
    assert priced["cost_usd"] == pytest.approx(0.75)


class TestToRunUsage:
    def test_keeps_only_validated_fields_and_fills_the_model(self):
        harness = backends.parse_codex_jsonl(CODEX_STREAM)["usage"]
        out = backends.to_run_usage(harness, model="gpt-6-luna")
        assert out == {
            "provider": "openai",
            "model": "gpt-6-luna",
            "input_tokens": 13661,
            "output_tokens": 5,
        }
        # The result is what task_claim / task_update accept as run_usage.
        assert run_records.validate_usage(out)["input_tokens"] == 13661

    def test_parsed_model_wins_over_the_fallback(self):
        harness = backends.parse_claude_stream_json(CLAUDE_STREAM)["usage"]
        out = backends.to_run_usage(harness, model="something-else")
        assert out["model"] == "claude-opus-4-7"
        assert out["cost_usd"] == pytest.approx(0.0123)

    @pytest.mark.parametrize("harness", [None, {}, {"model": "m"}, "junk"])
    def test_nothing_measured_is_none(self, harness):
        assert backends.to_run_usage(harness, model="m") is None


# --- backends carry usage in their result -----------------------------------


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(["x"], returncode, stdout, "")


def test_claude_backend_run_returns_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    with patch.object(
        backends, "run_popen_with_kill_check", return_value=_completed(CLAUDE_STREAM)
    ):
        res = backends.ClaudeBackend(claude_bin="claude").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent1",
            timeout_s=5,
        )
    assert res["status"] == "success"
    assert res["usage"]["cost_usd"] == pytest.approx(0.0123)


def test_codex_backend_killed_run_keeps_partial_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    exc = backends.KillSwitchTriggered(CODEX_STREAM, "")
    with patch.object(backends, "run_popen_with_kill_check", side_effect=exc):
        res = backends.CodexBackend(codex_bin="codex").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
    assert res["status"] == "killed"
    assert res["usage"]["input_tokens"] == 13661


def test_codex_backend_timeout_keeps_partial_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path))
    exc = subprocess.TimeoutExpired(["codex"], 5, output=CODEX_STREAM, stderr="")
    with patch.object(backends, "run_popen_with_kill_check", side_effect=exc):
        res = backends.CodexBackend(codex_bin="codex").run(
            prompt="p",
            cwd=".",
            session_dir="s",
            session_id="sid",
            agent_label="Desktop_Agent2",
            timeout_s=5,
        )
    assert res["status"] == "timeout"
    assert res["usage"]["output_tokens"] == 5


# --- the daemon reports it ---------------------------------------------------


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


def _pi_result(usage: dict | None, status: str = "success") -> dict:
    return {
        "status": status,
        "text": "ok" if status == "success" else "",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": 0 if status == "success" else 1,
        "error": None if status == "success" else "boom",
        "usage": usage,
    }


HARNESS_USAGE = {
    "provider": "anthropic",
    "model": None,
    "input_tokens": 1200,
    "output_tokens": 300,
    "cache_read_tokens": 1000,
    "cache_write_tokens": None,
    "cost_usd": 0.42,
}


def _delegate() -> int:
    return delegate_tool.delegate(
        task_spec="harness usage", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]


def _current_run(task_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_reports_harness_usage_on_the_run(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(HARNESS_USAGE)
    task_id = _delegate()
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    run = run_records.get_run(_current_run(task_id))
    assert run["usage_reported"] is True
    assert run["usage"]["cost_usd"] == pytest.approx(0.42)
    assert run["usage"]["input_tokens"] == 1200
    assert run["usage"]["output_tokens"] == 300
    assert run["usage"]["provider"] == "anthropic"
    # The harness did not name a model, so the daemon's resolved run model fills it.
    assert run["usage"]["model"] == mock_pi.call_args.kwargs["model"]


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_reports_usage_for_a_failed_run_too(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(HARNESS_USAGE, status="error")
    task_id = _delegate()
    run_id = None

    real_retry = ExecutorDaemon._retry_delegation_to_pending

    def capture(self, *args, **kwargs):
        nonlocal run_id
        run_id = _current_run(task_id)
        return real_retry(self, *args, **kwargs)

    with patch.object(ExecutorDaemon, "_retry_delegation_to_pending", capture):
        ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    assert run_id is not None
    run = run_records.get_run(run_id)
    assert run["usage"]["cost_usd"] == pytest.approx(0.42)


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_records_unknown_attempt_when_the_harness_measured_nothing(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(None)
    task_id = _delegate()
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()
    run = run_records.get_run(_current_run(task_id))
    assert run["usage_reported"] is True
    assert run["usage"]["cost_complete"] is False
    assert run["usage"]["cost_usd"] is None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_feeds_tokens_and_cost_to_the_bound_cycle(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(HARNESS_USAGE)
    task_id = _delegate()
    cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"]["id"]
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]
    assert cycle["used_tokens"] == 1500
    assert cycle["used_cost_cents"] == pytest.approx(42.0)


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_harness_cost_can_block_the_cycle(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(HARNESS_USAGE)
    task_id = _delegate()
    cycle_id = cycles_tool.autonomous_cycle_start(
        "Desktop_agent3", task_id=task_id, budgets={"cost_cents": 10}
    )["cycle"]["id"]
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()
    cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]
    assert cycle["state"] == "blocked"
    assert "cost_cents" in (cycle["stop_reason"] or "")


def test_report_run_usage_logs_a_budget_stop(executor_db, caplog):
    class Hub:
        authoritative_run_checks = False

        def __init__(self):
            self.calls = []

        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"id": 7, "budget_stop": "budget hard stop: over", "budget_warnings": ["w"]}

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    daemon.hub_client = Hub()
    with caplog.at_level(logging.WARNING, logger="unified_agent.executor.daemon"):
        res = daemon._report_run_usage(7, "sp-1", {"cost_usd": 1.0}, expected_lane="x")
    assert res["budget_stop"] == "budget hard stop: over"
    [(name, args)] = daemon.hub_client.calls
    assert name == "task_claim"
    assert args["run_usage"] == {"cost_usd": 1.0}
    assert args["expected_run_id"] == "sp-1"
    assert args["expected_lane"] == "x"
    assert "budget hard stop: over" in caplog.text


def test_report_run_usage_never_raises(executor_db):
    class Hub:
        authoritative_run_checks = False

        def call_tool(self, name, arguments):
            raise RuntimeError("hub down")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    daemon.hub_client = Hub()
    assert daemon._report_run_usage(7, "sp-1", {"cost_usd": 1.0}) is None


def test_usage_survives_transient_rpc_failure_and_daemon_restart(executor_db):
    class Hub:
        authoritative_run_checks = False

        def __init__(self, fail):
            self.fail = fail
            self.calls = []

        def call_tool(self, name, args):
            self.calls.append((name, args))
            if self.fail:
                raise RuntimeError("temporary hub outage")
            return {"id": args["id"]}

    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.hub_client = Hub(True)
    assert first._report_run_usage(7, "original-run", {"cost_usd": 4.0}) is None
    assert len(list(first._usage_spool_dir().glob("*.json"))) == 1
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    restarted.hub_client = Hub(False)
    restarted._retry_pending_run_usage()
    assert restarted.hub_client.calls[0][1]["expected_run_id"] == "original-run"
    assert restarted.hub_client.calls[0][1]["run_usage"] == {"cost_usd": 4.0}
    assert list(first._usage_spool_dir().glob("*.json")) == []


def test_old_run_refusal_preserves_snapshot_and_never_changes_run_fence(executor_db):
    class Hub:
        authoritative_run_checks = False

        def call_tool(self, name, args):
            assert args["expected_run_id"] == "old-run"
            raise RuntimeError("run was superseded")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    daemon.hub_client = Hub()
    daemon._report_run_usage(7, "old-run", {"cost_usd": 4.0})
    daemon._retry_pending_run_usage()
    assert len(list(daemon._usage_spool_dir().glob("*.json"))) == 1


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_daemon_retries_failed_usage_before_successful_finish(mock_pi, executor_db):
    mock_pi.return_value = _pi_result(HARNESS_USAGE)
    task_id = _delegate()
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    real = daemon.hub_client.call_tool
    failed = False

    def transient(name, args):
        nonlocal failed
        if name == "task_claim" and args.get("run_usage") and not failed:
            failed = True
            raise RuntimeError("temporary hub outage")
        return real(name, args)

    daemon.hub_client.call_tool = transient
    daemon.poll_once()
    assert failed
    assert run_records.get_run(_current_run(task_id))["usage"]["cost_usd"] == pytest.approx(0.42)
    assert not list(daemon._usage_spool_dir().glob("*.json"))


def test_failed_old_snapshots_do_not_starve_later_usage(executor_db):
    class Hub:
        authoritative_run_checks = False

        def __init__(self):
            self.calls = []

        def call_tool(self, name, args):
            self.calls.append(args["expected_run_id"])
            raise RuntimeError("old run refused")

    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    daemon.hub_client = Hub()
    for i in range(25):
        daemon._persist_run_usage(
            {
                "id": i,
                "agent": "Desktop_agent3",
                "expected_run_id": f"run-{i}",
                "run_usage": {"cost_usd": 1.0},
            }
        )
    daemon._retry_pending_run_usage()
    daemon._retry_pending_run_usage()
    assert len(set(daemon.hub_client.calls)) == 25


def test_attempt_totals_survive_restart_without_replay_charges(executor_db):
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    assert (
        first._accumulate_run_usage(
            1, "original", "attempt-a", {"cost_usd": 3, "input_tokens": 100}
        )["cost_usd"]
        == 3
    )
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    assert (
        restarted._accumulate_run_usage(
            1, "original", "attempt-a", {"cost_usd": 3, "input_tokens": 100}
        )["cost_usd"]
        == 3
    )
    total = restarted._accumulate_run_usage(
        1, "original", "attempt-b", {"cost_usd": 1, "input_tokens": 20}
    )
    assert total == {"cost_usd": 4, "input_tokens": 120, "cost_complete": True}


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_real_retry_keeps_earlier_spend(mock_pi, executor_db):
    mock_pi.side_effect = [
        _pi_result(HARNESS_USAGE, status="error"),
        _pi_result({**HARNESS_USAGE, "cost_usd": 0.1, "input_tokens": 10}),
    ]
    task_id = _delegate()
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    assert first.poll_once()["backoff_active"]
    run_id = _current_run(task_id)
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    restarted.backoff_base_s = 0
    restarted.poll_once()
    assert _current_run(task_id) == run_id
    usage = run_records.get_run(run_id)["usage"]
    assert usage["cost_usd"] == pytest.approx(0.52)
    assert usage["input_tokens"] == 1210


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_budget_stop_never_queues_retry(mock_pi, executor_db, tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "budgets:\n  - name: hard-stop\n    agents: [Desktop_agent3]\n    period: month\n    limit_usd: 0.1\n    run_reserve_usd: 0\n    action: block\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    mock_pi.return_value = _pi_result(HARNESS_USAGE, status="error")
    _delegate()
    result = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()
    assert result["failed_terminal"] is True
    assert "backoff_active" not in result
    assert mock_pi.call_count == 1


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_cancelled_child_usage_reaches_closed_run_and_killed_cycle(mock_pi, executor_db):
    task_id = _delegate()
    cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"]["id"]
    original_run = None

    def cancel(**kwargs):
        nonlocal original_run
        original_run = _current_run(task_id)
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            tasks_tool.task_update(task_id, status="cancelled", expected_run_id=original_run)
            cycles_tool.autonomous_cycle_kill(cycle_id, "Desktop_agent3", "operator cancelled")
        finally:
            config.agent_identity_var.reset(token)
        return _pi_result(HARNESS_USAGE, status="killed")

    mock_pi.side_effect = cancel
    result = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()
    assert result["status"] == "run_not_current"
    assert run_records.get_run(original_run)["usage"]["cost_usd"] == pytest.approx(0.42)
    cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]
    assert (
        cycle["state"] == "killed"
        and cycle["stop_reason"] == "killed by Desktop_agent3: operator cancelled"
    )
    assert cycle["used_tokens"] == 1500 and cycle["used_cost_cents"] == pytest.approx(42)


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_replayed_budget_stop_blocks_retry_before_backend(
    mock_pi, executor_db, tmp_path, monkeypatch
):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "budgets:\n  - name: hard-stop\n    agents: [Desktop_agent3]\n    period: month\n    limit_usd: 0.1\n    run_reserve_usd: 0\n    action: block\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    mock_pi.return_value = _pi_result(HARNESS_USAGE, status="error")
    _delegate()
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    real = first.hub_client.call_tool

    def outage(name, args):
        if name == "task_claim" and args.get("usage_only"):
            raise RuntimeError("usage RPC outage")
        return real(name, args)

    first.hub_client.call_tool = outage
    assert first.poll_once()["backoff_active"]
    assert len(list(first._usage_spool_dir().glob("*.json"))) == 1
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    restarted.backoff_base_s = 0
    result = restarted.poll_once()
    assert result["status"] == "blocked_by_budget"
    assert mock_pi.call_count == 1
    assert not list(first._usage_spool_dir().glob("*.json"))


def test_cycle_usage_replays_once_after_rpc_response_loss(executor_db):
    task_id = _delegate()
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        run_id = tasks_tool.task_claim(task_id, "Desktop_agent3")["current_run_id"]
        cycle_id = cycles_tool.autonomous_cycle_start("Desktop_agent3", task_id=task_id)["cycle"][
            "id"
        ]
    finally:
        config.agent_identity_var.reset(token)
    args = {
        "cycle_id": cycle_id,
        "agent": "Desktop_agent3",
        "usage_run_id": run_id,
        "usage_id": "one-attempt",
        "tokens": 100,
        "cost_cents": 42,
    }
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    real = first.hub_client.call_tool

    def lost_response(name, arguments):
        real(name, arguments)
        raise RuntimeError("response lost after commit")

    first.hub_client.call_tool = lost_response
    assert first._report_cycle_usage(args) is None
    assert len(list((first._usage_spool_dir() / "cycles").glob("*.json"))) == 1
    ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)._retry_pending_run_usage()
    cycle = cycles_tool.autonomous_cycle_get(cycle_id)["cycle"]
    assert cycle["used_tokens"] == 100 and cycle["used_cost_cents"] == 42
    assert not list((first._usage_spool_dir() / "cycles").glob("*.json"))


@pytest.mark.parametrize("priced_first", [False, True])
def test_mixed_attempts_retain_known_cost_and_unknown_reserve_after_restart(
    executor_db, priced_first
):
    task_id = _delegate()
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        run_id = tasks_tool.task_claim(task_id, "Desktop_agent3")["current_run_id"]
    finally:
        config.agent_identity_var.reset(token)
    unpriced = {"input_tokens": 100, "output_tokens": 0}
    priced = {"input_tokens": 20, "output_tokens": 0, "cost_usd": 1}
    first_usage, second_usage = (priced, unpriced) if priced_first else (unpriced, priced)
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    total = first._accumulate_run_usage(task_id, run_id, "attempt-1", first_usage)
    first._report_run_usage(task_id, run_id, total)
    # Exercise a prior heartbeat whose numeric cache would otherwise hide
    # later pricing uncertainty. Neither heartbeat nor finish clears False.
    if priced_first:
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            tasks_tool.task_claim(
                task_id, "Desktop_agent3", expected_run_id=run_id, run_usage={"cost_usd": 1}
            )
        finally:
            config.agent_identity_var.reset(token)
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    total = restarted._accumulate_run_usage(task_id, run_id, "attempt-2", second_usage)
    assert total["input_tokens"] == 120
    assert total["cost_usd"] == 1 and total["cost_complete"] is False
    restarted._report_run_usage(task_id, run_id, total)
    restarted._report_run_usage(task_id, run_id, total)
    from unified_agent import budgets

    policy = budgets.BudgetPolicy("mixed", ("Desktop_agent3",), "month", 100, run_reserve_usd=2)
    for state in ("live", "expired", "cancelled", "terminal-correction"):
        token = config.agent_identity_var.set("Desktop_agent3")
        try:
            if state == "expired":
                conn = storage.connect()
                try:
                    conn.execute(
                        "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                        (task_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
            elif state == "cancelled":
                tasks_tool.task_update(
                    task_id,
                    status="cancelled",
                    expected_run_id=run_id,
                    run_usage={"cost_usd": 1, "cost_complete": True},
                )
            elif state == "terminal-correction":
                restarted._report_run_usage(
                    task_id, run_id, {**total, "cost_usd": 0.5, "cost_complete": True}
                )
        finally:
            config.agent_identity_var.reset(token)
        conn = storage.connect()
        try:
            spend = budgets.agent_spend(conn, "Desktop_agent3", policy)
        finally:
            conn.close()
        usage = run_records.get_run(run_id)["usage"]
        assert usage["cost_usd"] == 1 and usage["cost_complete"] is False
        assert spend["reserved_usd"] == 2 and spend["committed_usd"] == 3
        if state in ("cancelled", "terminal-correction"):
            assert spend["unknown_cost_runs"] == 1 and spend["priced_runs"] == 0
            assert spend["api_cost_usd"] == 1 and spend["open_run_cost_usd"] == 0
            rollup = budgets.cost_rollup(agent="Desktop_agent3")
            assert rollup["totals"]["api_cost_usd"] == 1
            assert rollup["totals"]["unknown_cost_runs"] == 1


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_accumulation_persistence_error_keeps_lifecycle_cleanup_and_stops_retry(
    mock_pi, executor_db
):
    mock_pi.return_value = _pi_result(HARNESS_USAGE)
    task_id = _delegate()
    daemon = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    with patch.object(daemon, "_accumulate_run_usage", side_effect=OSError("disk full")):
        result = daemon.poll_once()
    assert result["status"] == "accounting_persistence_failed"
    assert result["failed_terminal"] is True and "backoff_active" not in result
    assert run_records.get_run(_current_run(task_id))["usage"]["cost_complete"] is False
    conn = storage.connect()
    try:
        delegation = conn.execute(
            "SELECT status FROM delegations WHERE task_id=?", (task_id,)
        ).fetchone()
        session = conn.execute(
            "SELECT status, exit_status FROM agent_sessions WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT finished_at FROM executor_attempts WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert delegation["status"] == "failed"
    assert (
        session["status"] == "failed" and session["exit_status"] == "accounting_persistence_failed"
    )
    assert attempt["finished_at"] is not None


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_failed_totals_write_reports_computed_known_spend_and_finalizes_retry(mock_pi, executor_db):
    mock_pi.side_effect = [
        _pi_result({**HARNESS_USAGE, "cost_usd": 3}, status="error"),
        _pi_result({**HARNESS_USAGE, "cost_usd": 1}),
    ]
    task_id = _delegate()
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    assert first.poll_once()["backoff_active"]
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    restarted.backoff_base_s = 0
    real_persist = restarted._persist_usage_payload

    def disk_error(arguments, target):
        if target.parent.name == "totals":
            raise OSError("totals write failed")
        return real_persist(arguments, target)

    with patch.object(restarted, "_persist_usage_payload", side_effect=disk_error):
        result = restarted.poll_once()
    assert result["status"] == "accounting_persistence_failed" and result["failed_terminal"]
    usage = run_records.get_run(_current_run(task_id))["usage"]
    assert usage["cost_usd"] == 4 and usage["cost_complete"] is False
    assert mock_pi.call_count == 2


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_no_telemetry_attempt_survives_restart_before_priced_retry(mock_pi, executor_db):
    mock_pi.side_effect = [
        _pi_result(None, status="error"),
        _pi_result({**HARNESS_USAGE, "cost_usd": 1}),
    ]
    task_id = _delegate()
    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    assert first.poll_once()["backoff_active"]
    run_id = _current_run(task_id)
    assert run_records.get_run(run_id)["usage"]["cost_complete"] is False
    restarted = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    restarted.backoff_base_s = 0
    restarted.poll_once()
    assert _current_run(task_id) == run_id
    usage = run_records.get_run(run_id)["usage"]
    assert usage["cost_usd"] == 1 and usage["cost_complete"] is False
    from unified_agent import budgets

    policy = budgets.BudgetPolicy("absent", ("Desktop_agent3",), "month", 100, run_reserve_usd=2)
    conn = storage.connect()
    try:
        spend = budgets.agent_spend(conn, "Desktop_agent3", policy)
    finally:
        conn.close()
    assert spend["committed_usd"] == 3 and spend["reserved_usd"] == 2
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        tasks_tool.task_update(task_id, status="cancelled", expected_run_id=run_id)
    finally:
        config.agent_identity_var.reset(token)
    rollup = budgets.cost_rollup(agent="Desktop_agent3")
    assert rollup["totals"]["api_cost_usd"] == 1
    assert rollup["totals"]["unknown_cost_runs"] == 1
