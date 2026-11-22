"""Hub-observed provider usage ledger (task #1526).

Cursor's PR #860 was vetoed for wrapping every MCP tool call and resolving
"the caller's current run" with a correlated event_log scan (JSON extraction
plus a NOT EXISTS on the unindexed dedupe_key column). These tests cover the
replacement: usage_ledger records only at the four provider call sites
(llm_broker.py, llm_client.py, tools/gemini.py — covered in their own test
files — and this module's own helpers), and its run lookup is a single
indexed read of tasks.current_run_agent, never event_log.
"""

from __future__ import annotations

import time

import pytest

from unified_agent import events, run_records, storage, usage_ledger
from unified_agent.tools.tasks import task_add, task_claim, task_update


def _new_claimed_task(agent: str) -> int:
    tid = task_add(f"t-{agent}", lane="dispatch-ready")["id"]
    task_claim(tid, agent, expected_lane="dispatch-ready")
    return tid


def _rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_events").fetchall()]
    finally:
        conn.close()


class TestTokenAndMoneyHelpers:
    def test_token_count_passes_through_a_real_value(self):
        assert usage_ledger.token_count(42) == 42
        assert usage_ledger.token_count(0) == 0

    @pytest.mark.parametrize("value", [None, -1, "5", True, 1.5, float("nan"), float("inf")])
    def test_token_count_never_fabricates(self, value):
        assert usage_ledger.token_count(value) is None

    def test_money_passes_through_a_real_value(self):
        assert usage_ledger.money(0.002) == 0.002
        assert usage_ledger.money(0) == 0.0

    @pytest.mark.parametrize("value", [None, -0.01, "0.01", True, float("nan")])
    def test_money_never_fabricates(self, value):
        assert usage_ledger.money(value) is None


class TestResolveRunId:
    def test_unknown_actor_is_none(self):
        assert usage_ledger.resolve_run_id("nobody") is None
        assert usage_ledger.resolve_run_id(None) is None
        assert usage_ledger.resolve_run_id("  ") is None

    def test_one_open_run_resolves(self):
        tid = _new_claimed_task("solo-agent")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]
        assert usage_ledger.resolve_run_id("solo-agent") == run_id

    def test_two_open_runs_for_the_same_agent_resolve_to_none(self):
        """The exact ambiguity PR #860 got wrong: don't guess the newest."""
        _new_claimed_task("busy-agent")
        _new_claimed_task("busy-agent")
        assert usage_ledger.resolve_run_id("busy-agent") is None

    def test_finished_run_no_longer_resolves(self):
        tid = _new_claimed_task("finishing-agent")
        assert usage_ledger.resolve_run_id("finishing-agent") is not None
        task_update(tid, status="done", actor="finishing-agent")
        assert usage_ledger.resolve_run_id("finishing-agent") is None

    def test_never_queries_event_log(self, monkeypatch):
        """The whole point of the redesign: this lookup must not touch
        event_log at all, regardless of how big it is."""
        tid = _new_claimed_task("watched-agent")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]

        real_connect = storage.connect
        seen_sql: list[str] = []

        class _Watched:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args, **kwargs):
                seen_sql.append(sql)
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def watched_connect():
            return _Watched(real_connect())

        monkeypatch.setattr(storage, "connect", watched_connect)
        assert usage_ledger.resolve_run_id("watched-agent") == run_id
        assert seen_sql, "expected at least one query"
        assert all("event_log" not in sql for sql in seen_sql)
        assert all("tasks" in sql for sql in seen_sql)


class TestRecordObserved:
    def test_records_a_row_with_resolved_run_id(self):
        tid = _new_claimed_task("recording-agent")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]
        usage_ledger.record_observed(
            provider="ollama",
            model="m",
            input_tokens=3,
            output_tokens=1,
            cost_usd=None,
            actor="recording-agent",
        )
        [row] = _rows()
        assert row["run_id"] == run_id
        assert row["actor"] == "recording-agent"

    def test_explicit_run_id_skips_the_lookup(self):
        usage_ledger.record_observed(
            provider="ollama",
            actor="no-such-agent",
            run_id="sp-explicit",
        )
        [row] = _rows()
        assert row["run_id"] == "sp-explicit"

    def test_explicit_none_run_id_is_not_overwritten_by_a_lookup(self):
        tid = _new_claimed_task("skip-lookup-agent")
        run_records.run_history(task_id=tid)  # sanity: a run does exist
        usage_ledger.record_observed(provider="ollama", actor="skip-lookup-agent", run_id=None)
        [row] = _rows()
        assert row["run_id"] is None

    def test_missing_actor_falls_back_to_a_generic_label(self):
        usage_ledger.record_observed(provider="ollama", actor=None)
        [row] = _rows()
        assert row["actor"] == "hub"

    def test_never_raises_on_a_broken_insert(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(usage_ledger, "_insert", boom)
        usage_ledger.record_observed(provider="ollama", actor="whatever")  # must not raise
        assert _rows() == []

    def test_dedupe_key_suppresses_a_retried_insert(self):
        for _ in range(3):
            usage_ledger.record_observed(
                provider="ollama", actor="retry-agent", dedupe_key="fixed-key"
            )
        assert len(_rows()) == 1

    def test_different_dedupe_keys_are_not_suppressed(self):
        usage_ledger.record_observed(provider="ollama", actor="a", dedupe_key="k1")
        usage_ledger.record_observed(provider="ollama", actor="a", dedupe_key="k2")
        assert len(_rows()) == 2


class TestProviderCallCount:
    def test_counts_only_recorded_rows(self):
        assert usage_ledger.provider_call_count() == 0
        usage_ledger.record_observed(provider="ollama", actor="a")
        usage_ledger.record_observed(provider="openrouter", actor="a")
        assert usage_ledger.provider_call_count() == 2
        assert usage_ledger.provider_call_count(provider="ollama") == 1


class TestFlatCostRegression:
    """A coarse guard against reintroducing an event_log scan: this must not
    slow down materially as event_log grows. Generous bound (not a tight
    latency assertion) to stay non-flaky on a loaded CI runner — see
    scripts/bench_usage_ledger.py for the real benchmark with 50k rows and
    percentile numbers.
    """

    def test_resolve_run_id_cost_is_flat_as_event_log_grows(self):
        tid = _new_claimed_task("flat-cost-agent")
        run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]

        def _time_calls(n: int) -> float:
            start = time.perf_counter()
            for _ in range(n):
                assert usage_ledger.resolve_run_id("flat-cost-agent") == run_id
            return time.perf_counter() - start

        before = _time_calls(50)

        conn = storage.connect()
        try:
            now = storage.now()
            rows = [(events.new_ulid(), "bench.noise", "{}", "bench", now) for _ in range(5000)]
            conn.executemany(
                "INSERT INTO event_log (ulid, channel, payload, originator, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

        after = _time_calls(50)
        # A per-call event_log scan would grow ~linearly with row count; an
        # indexed tasks-table lookup should not move meaningfully. 5x is a
        # loose ceiling for a noisy shared CI runner, not a real budget.
        assert after < before * 5 + 0.05
