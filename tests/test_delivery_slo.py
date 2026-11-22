"""Pure Delivery SLO computation (task #1259, Epic C #1188).

Synthetic-data tests only — `delivery_slo.py` is a pure module with no I/O.
`monitor.delivery_slo()` (the DB-querying wrapper) is covered separately in
`test_monitor.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from unified_agent.delivery_slo import (
    DISPATCH_SUCCESS_TARGET,
    ci_green_rate,
    compute_delivery_slo,
    dispatch_success_rate,
    format_delivery_slo_line,
    p90_cycle_time,
    unattributed_completions,
)

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _run(state: str, created_days_ago: float = 1.0) -> dict:
    return {"state": state, "created_at": _iso(created_days_ago)}


def _task(
    *,
    claimed_days_ago: float | None,
    completed_days_ago: float,
    created_days_ago: float | None = None,
    claimed_by: str | None = "Desktop_Agent1",
) -> dict:
    return {
        "claimed_by": claimed_by,
        "claimed_at": _iso(claimed_days_ago) if claimed_days_ago is not None else None,
        "created_at": _iso(created_days_ago) if created_days_ago is not None else None,
        "completed_at": _iso(completed_days_ago),
    }


class TestDispatchSuccessRate:
    def test_empty_window_is_unavailable(self):
        m = dispatch_success_rate([], window_days=7.0, now=NOW)
        assert m.status == "unavailable"
        assert m.value is None
        assert m.numerator == 0
        assert m.denominator == 0
        assert m.detail

    def test_mixed_terminal_states_computes_rate(self):
        runs = [
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("finished"),
            _run("failed"),
        ]
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.numerator == 9
        assert m.denominator == 10
        assert m.value == 0.9
        assert m.status == "ok"
        assert DISPATCH_SUCCESS_TARGET == 0.90

    def test_below_target_is_breach(self):
        runs = [_run("finished")] * 5 + [_run("failed")] * 5
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.value == 0.5
        assert m.status == "breach"

    def test_rounded_rate_below_target_stays_breach(self):
        runs = [_run("finished")] * 89999 + [_run("failed")] * 10001
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.value == 0.9
        assert m.status == "breach"

    def test_retryable_and_refused_excluded_from_denominator(self):
        runs = [_run("finished"), _run("retryable"), _run("refused")]
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.denominator == 1
        assert m.numerator == 1
        assert m.value == 1.0

    def test_in_flight_states_excluded(self):
        runs = [_run("claimed"), _run("launched"), _run("running")]
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.status == "unavailable"

    def test_outside_window_excluded(self):
        runs = [_run("finished", created_days_ago=30.0)]
        m = dispatch_success_rate(runs, window_days=7.0, now=NOW)
        assert m.status == "unavailable"

    def test_zero_denominator_never_divides(self):
        m = dispatch_success_rate([_run("claimed")], window_days=7.0, now=NOW)
        assert m.value is None


class TestP90CycleTime:
    def test_empty_window_is_unavailable(self):
        m = p90_cycle_time([], window_days=7.0, now=NOW)
        assert m.status == "unavailable"
        assert m.value is None

    def test_p90_over_synthetic_durations(self):
        # 10 tasks, cycle times 1h..10h (claimed_at N hours before completed_at).
        tasks = []
        for hours in range(1, 11):
            completed = NOW
            claimed = NOW - timedelta(hours=hours)
            tasks.append(
                {
                    "claimed_by": "x",
                    "claimed_at": claimed.isoformat(),
                    "created_at": claimed.isoformat(),
                    "completed_at": completed.isoformat(),
                }
            )
        m = p90_cycle_time(tasks, window_days=7.0, now=NOW)
        assert m.status == "ok"
        assert m.numerator == 10
        assert m.denominator == 10
        # Linear-interpolation p90 of [1..10] is 9.1.
        assert m.value == 9.1
        assert m.detail is None

    def test_missing_claimed_at_falls_back_to_created_at(self):
        t = _task(claimed_days_ago=None, created_days_ago=2.0, completed_days_ago=0.0)
        m = p90_cycle_time([t], window_days=7.0, now=NOW)
        assert m.status == "ok"
        assert m.value == 48.0  # 2 days = 48h
        assert "created_at" in (m.detail or "")

    def test_missing_all_timestamps_excluded_not_zeroed(self):
        t = {
            "claimed_by": "x",
            "claimed_at": None,
            "created_at": None,
            "completed_at": _iso(0.0),
        }
        m = p90_cycle_time([t], window_days=7.0, now=NOW)
        # considered (denominator) counts it as in-window, but no duration sample.
        assert m.denominator == 1
        assert m.numerator == 0
        assert m.status == "unavailable"

    def test_outside_window_excluded(self):
        t = _task(claimed_days_ago=31.0, completed_days_ago=30.0)
        m = p90_cycle_time([t], window_days=7.0, now=NOW)
        assert m.status == "unavailable"


class TestUnattributedCompletions:
    def test_empty_window_is_unavailable(self):
        m = unattributed_completions([], window_days=7.0, now=NOW)
        assert m.status == "unavailable"
        assert m.value is None

    def test_zero_unattributed_is_ok(self):
        tasks = [_task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by="Desktop_Agent1")]
        m = unattributed_completions(tasks, window_days=7.0, now=NOW)
        assert m.status == "ok"
        assert m.value == 0.0
        assert m.numerator == 0
        assert m.denominator == 1

    def test_unattributed_present_is_breach(self):
        tasks = [
            _task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by="Desktop_Agent1"),
            _task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by=None),
            _task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by="   "),
        ]
        m = unattributed_completions(tasks, window_days=7.0, now=NOW)
        assert m.status == "breach"
        assert m.numerator == 2
        assert m.denominator == 3
        assert m.value == 2.0

    def test_outside_window_excluded(self):
        t = _task(claimed_days_ago=31.0, completed_days_ago=30.0, claimed_by=None)
        m = unattributed_completions([t], window_days=7.0, now=NOW)
        assert m.status == "unavailable"


class TestCiGreenRate:
    def test_no_provider_is_unavailable_with_reason(self):
        m = ci_green_rate(None, window_days=7.0)
        assert m.status == "unavailable"
        assert m.value is None
        assert m.detail == "no CI data source configured"

    def test_custom_unavailable_reason_passthrough(self):
        m = ci_green_rate(None, window_days=7.0, unavailable_reason="gh CI provider errored: boom")
        assert m.detail == "gh CI provider errored: boom"

    def test_empty_list_is_unavailable_not_zero(self):
        m = ci_green_rate([], window_days=7.0)
        assert m.status == "unavailable"
        assert m.value is None

    def test_computes_green_rate_from_conclusions(self):
        runs = [{"conclusion": "success"}] * 8 + [{"conclusion": "failure"}] * 2
        m = ci_green_rate(runs, window_days=7.0)
        assert m.status == "ok"
        assert m.numerator == 8
        assert m.denominator == 10
        assert m.value == 0.8

    def test_state_field_fallback(self):
        runs = [{"state": "success"}, {"state": "failure"}]
        m = ci_green_rate(runs, window_days=7.0)
        assert m.numerator == 1
        assert m.denominator == 2


class TestComputeDeliverySlo:
    def test_all_empty_is_uniformly_unavailable(self):
        result = compute_delivery_slo(window_days=7.0, now=NOW)
        assert result["window_days"] == 7.0
        for key in (
            "dispatch_success_rate",
            "p90_cycle_time_hours",
            "unattributed_completions",
            "ci_green_rate",
        ):
            assert result[key]["status"] == "unavailable"
            assert result[key]["value"] is None

    def test_non_positive_window_days_falls_back_to_default(self):
        result = compute_delivery_slo(window_days=-1.0, now=NOW)
        assert result["window_days"] == 7.0

    def test_full_shape_with_data(self):
        runs = [_run("finished")] * 9 + [_run("failed")]
        tasks = [
            _task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by="Desktop_Agent1")
            for _ in range(9)
        ] + [_task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by=None)]
        ci_runs = [{"conclusion": "success"}] * 4 + [{"conclusion": "failure"}]
        result = compute_delivery_slo(
            dispatch_runs=runs, tasks=tasks, ci_runs=ci_runs, window_days=7.0, now=NOW
        )
        assert result["dispatch_success_rate"]["value"] == 0.9
        assert result["dispatch_success_rate"]["status"] == "ok"
        assert result["unattributed_completions"]["numerator"] == 1
        assert result["unattributed_completions"]["status"] == "breach"
        assert result["ci_green_rate"]["value"] == 0.8
        assert result["generated_at"] == NOW.isoformat()


class TestFormatDeliverySloLine:
    def test_all_unavailable_renders_na(self):
        result = compute_delivery_slo(window_days=7.0, now=NOW)
        line = format_delivery_slo_line(result)
        assert line == (
            "Delivery SLO (7d): dispatch n/a · p90 n/a · unattributed n/a · CI green n/a"
        )

    def test_full_data_renders_all_fields(self):
        runs = [_run("finished")] * 9 + [_run("failed")]
        tasks = [
            _task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by="Desktop_Agent1")
            for _ in range(9)
        ] + [_task(claimed_days_ago=1.0, completed_days_ago=0.0, claimed_by=None)]
        ci_runs = [{"conclusion": "success"}] * 4 + [{"conclusion": "failure"}]
        result = compute_delivery_slo(
            dispatch_runs=runs, tasks=tasks, ci_runs=ci_runs, window_days=7.0, now=NOW
        )
        line = format_delivery_slo_line(result)
        assert line.startswith("Delivery SLO (7d): dispatch 90% · p90 ")
        assert "unattributed 1" in line
        assert "CI green 80%" in line
