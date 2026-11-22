"""Tests for agent_perf_samples write-rate reduction (task #925).

Covers:
- Default behavior with no env vars set (untouched, byte-for-byte prior default)
- Exclusions via UA_PERF_SAMPLE_EXCLUDE_TOOLS for hot poll tools
- Rate-threshold 1-in-N sampling with stubbed clock
- Refusal/ignoring of any policy that would drop error samples
- Compatibility with perf_summary on surviving rows
"""

from __future__ import annotations

import pytest

from unified_agent import monitor, storage


def _get_samples(tool: str | None = None) -> list[dict]:
    conn = storage.connect()
    try:
        if tool:
            return conn.execute(
                "SELECT * FROM agent_perf_samples WHERE tool=? ORDER BY id", (tool,)
            ).fetchall()
        return conn.execute("SELECT * FROM agent_perf_samples ORDER BY id").fetchall()
    finally:
        conn.close()


class TestPerfSamplingDefaultUntouched:
    def test_default_behavior_untouched_with_no_env_vars(self, isolated_env, monkeypatch):
        """With no env vars set, behavior is byte-for-byte the prior default."""
        storage.init_db()
        storage._reset_perf_sample_state()

        # Ensure no task-925 env vars are set
        monkeypatch.delenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", raising=False)
        monkeypatch.delenv("UA_PERF_SAMPLE_RATE_THRESHOLD", raising=False)
        monkeypatch.delenv("UA_PERF_SAMPLE_INTERVAL", raising=False)
        monkeypatch.delenv("UA_PERF_SAMPLE_N", raising=False)
        monkeypatch.delenv("UA_PERF_SAMPLE_RATE", raising=False)

        def dummy_ok(x: int) -> int:
            return x * 2

        def dummy_err():
            raise RuntimeError("boom_default")

        # 1. Successful instrumented call
        res = monitor.instrumented(dummy_ok)(21)
        assert res == 42

        # 2. Failing instrumented call
        with pytest.raises(RuntimeError, match="boom_default"):
            monitor.instrumented(dummy_err)()

        # 3. Direct storage.record_perf_sample OK
        storage.record_perf_sample("task_list", 12.5, ok=True, rss_bytes=1024)

        # 4. Direct storage.record_perf_sample Error
        storage.record_perf_sample(
            "task_list", 25.0, ok=False, error="timeout error", rss_bytes=2048
        )

        rows = _get_samples()
        assert len(rows) == 4

        # Verify exact columns and values
        r0 = rows[0]
        assert r0["tool"] == "dummy_ok"
        assert r0["ok"] == 1
        assert r0["error"] is None
        assert r0["latency_ms"] >= 0.0

        r1 = rows[1]
        assert r1["tool"] == "dummy_err"
        assert r1["ok"] == 0
        assert "boom_default" in r1["error"]

        r2 = rows[2]
        assert r2["tool"] == "task_list"
        assert r2["ok"] == 1
        assert r2["latency_ms"] == 12.5
        assert r2["error"] is None
        assert r2["rss_bytes"] == 1024

        r3 = rows[3]
        assert r3["tool"] == "task_list"
        assert r3["ok"] == 0
        assert r3["latency_ms"] == 25.0
        assert r3["error"] == "timeout error"
        assert r3["rss_bytes"] == 2048


class TestPerfSampleExclusions:
    def test_hot_poll_tools_stop_recording_ok_samples(self, isolated_env, monkeypatch):
        """When excluded, hot poll tools record 0 OK samples while unexcluded tools record normally."""
        storage.init_db()
        storage._reset_perf_sample_state()

        hot_tools = ["task_list", "delegation_manage", "delegate_list", "approval_pending"]
        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", ":".join(hot_tools))

        # Record multiple OK calls for each hot poll tool
        for tool_name in hot_tools:
            for _ in range(10):
                storage.record_perf_sample(tool_name, 5.0, ok=True)
                fn = lambda: "poll_result"  # noqa: E731
                fn.__name__ = tool_name
                monitor.instrumented(fn)()

        # All hot tools should have 0 rows in agent_perf_samples
        for tool_name in hot_tools:
            assert _get_samples(tool_name) == []

        # An unexcluded tool records normally
        unexcluded = "fs_read"
        storage.record_perf_sample(unexcluded, 1.5, ok=True)
        read_fn = lambda: "content"  # noqa: E731
        read_fn.__name__ = unexcluded
        monitor.instrumented(read_fn)()

        unexcluded_rows = _get_samples(unexcluded)
        assert len(unexcluded_rows) == 2
        assert all(r["ok"] == 1 for r in unexcluded_rows)

    def test_exclusions_never_drop_error_samples(self, isolated_env, monkeypatch):
        """Hot poll tools configured in exclude list MUST still record every error sample."""
        storage.init_db()
        storage._reset_perf_sample_state()

        hot_tools = ["task_list", "delegation_manage", "delegate_list", "approval_pending"]
        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", ":".join(hot_tools))

        for tool_name in hot_tools:
            # Direct storage write with error
            storage.record_perf_sample(
                tool_name, 10.0, ok=False, error=f"{tool_name} failed in test"
            )

            # Instrumented error
            def make_boom(name):
                def boom():
                    raise ValueError(f"err_{name}")

                boom.__name__ = name
                return boom

            with pytest.raises(ValueError, match=f"err_{tool_name}"):
                monitor.instrumented(make_boom(tool_name))()

            # Verify both error samples were recorded
            rows = _get_samples(tool_name)
            assert len(rows) == 2
            assert rows[0]["ok"] == 0
            assert f"{tool_name} failed in test" in rows[0]["error"]
            assert rows[1]["ok"] == 0
            assert f"err_{tool_name}" in rows[1]["error"]

    def test_exclusion_parsing_whitespace_and_empty(self, isolated_env, monkeypatch):
        """Colon-separated list handles leading/trailing whitespace and empty tokens."""
        storage.init_db()
        storage._reset_perf_sample_state()

        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", "  task_list  : : delegation_manage : ")

        storage.record_perf_sample("task_list", 1.0, ok=True)
        storage.record_perf_sample("delegation_manage", 1.0, ok=True)
        storage.record_perf_sample("other_tool", 1.0, ok=True)

        assert _get_samples("task_list") == []
        assert _get_samples("delegation_manage") == []
        assert len(_get_samples("other_tool")) == 1


class TestPerfSampleRateThreshold:
    def test_rate_threshold_sampling_with_stubbed_clock(self, isolated_env, monkeypatch):
        """1-in-N sampling drops the majority of OK samples above rate threshold."""
        storage.init_db()
        storage._reset_perf_sample_state()

        fake_time = 1000.0

        def mock_clock() -> float:
            return fake_time

        monkeypatch.setattr(storage, "_clock", mock_clock)
        monkeypatch.setenv("UA_PERF_SAMPLE_RATE_THRESHOLD", "5")
        monkeypatch.setenv("UA_PERF_SAMPLE_INTERVAL", "5")

        tool = "hot_poller"

        # First 5 calls in the 1s window (rate <= 5) are all kept
        for _ in range(5):
            storage.record_perf_sample(tool, 2.0, ok=True)

        assert len(_get_samples(tool)) == 5

        # Next 15 calls exceed threshold (rate > 5).
        # With interval=5, 1 in 5 is kept (3 out of 15).
        for _ in range(15):
            storage.record_perf_sample(tool, 2.0, ok=True)

        rows = _get_samples(tool)
        # Total kept: 5 (under threshold) + 3 (1-in-5 sampled) = 8 out of 20.
        # Majority of OK samples (12 of 20, 60%) stopped!
        assert len(rows) == 8

        # An error sample above the rate threshold is ALWAYS kept
        storage.record_perf_sample(tool, 50.0, ok=False, error="rate_overflow_error")
        err_rows = [r for r in _get_samples(tool) if r["ok"] == 0]
        assert len(err_rows) == 1
        assert err_rows[0]["error"] == "rate_overflow_error"

        # Advance clock past 1s window: rate resets
        fake_time += 10.0
        storage.record_perf_sample(tool, 2.0, ok=True)
        # Should be kept since rate is now 1 <= 5
        rows_after = _get_samples(tool)
        assert len(rows_after) == 10


class TestPolicyRefusalAndSafety:
    def test_policy_attempting_to_drop_errors_is_refused(self, isolated_env, monkeypatch):
        """Failure mode asserted: a configured policy that would drop an error sample is refused/ignored — errors are never sampled out."""
        storage.init_db()
        storage._reset_perf_sample_state()

        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", "task_list")
        monkeypatch.setenv("UA_PERF_SAMPLE_RATE_THRESHOLD", "0.001")
        monkeypatch.setenv("UA_PERF_SAMPLE_INTERVAL", "1000")
        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_ERRORS", "1")

        # should_record_perf_sample MUST return True for any error
        assert storage.should_record_perf_sample("task_list", ok=False) is True
        assert storage.should_record_perf_sample("other_tool", ok=False) is True

        storage.record_perf_sample("task_list", 5.0, ok=False, error="cannot drop")
        rows = _get_samples("task_list")
        assert len(rows) == 1
        assert rows[0]["ok"] == 0
        assert rows[0]["error"] == "cannot drop"


class TestPerfSummaryCompatibility:
    def test_perf_summary_maintains_shape_and_works_for_surviving_rows(
        self, isolated_env, monkeypatch
    ):
        """perf_summary returns the same shape and correct metrics for surviving rows."""
        storage.init_db()
        storage._reset_perf_sample_state()

        monkeypatch.setenv("UA_PERF_SAMPLE_EXCLUDE_TOOLS", "task_list")

        # task_list OK samples are excluded
        storage.record_perf_sample("task_list", 10.0, ok=True)
        storage.record_perf_sample("task_list", 20.0, ok=True)
        # task_list error sample survives
        storage.record_perf_sample("task_list", 30.0, ok=False, error="locked")

        # other_tool OK samples survive
        storage.record_perf_sample("other_tool", 10.0, ok=True, rss_bytes=1000)
        storage.record_perf_sample("other_tool", 20.0, ok=True, rss_bytes=2000)

        summary = monitor.perf_summary()

        assert set(summary.keys()) == {"window_hours", "by_tool", "memory_rss_bytes"}
        assert summary["window_hours"] == 24.0

        # task_list only has the 1 error sample
        tl = summary["by_tool"]["task_list"]
        assert tl["calls"] == 1
        assert tl["errors"] == 1
        assert tl["error_rate"] == 1.0
        assert tl["avg_latency_ms"] == 30.0

        # other_tool has 2 calls
        ot = summary["by_tool"]["other_tool"]
        assert ot["calls"] == 2
        assert ot["errors"] == 0
        assert ot["error_rate"] == 0.0
        assert ot["avg_latency_ms"] == 15.0
        assert ot["min_latency_ms"] == 10.0
        assert ot["max_latency_ms"] == 20.0

        mem = summary["memory_rss_bytes"]
        assert mem["count"] == 2
        assert mem["avg"] == 1500.0
        assert mem["min"] == 1000
        assert mem["max"] == 2000
