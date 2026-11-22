"""Capacity ceilings are recorded apart from lease expiry (task #1841).

A run that stopped because its harness hit a plan usage limit used to finish
as a bare lease variant ("lease expired; released by queue hygiene"), which
hid the real cause. A run that carries a capacity signal (a heartbeat or a
finishing ``run_usage`` with ``capacity.limit_reached``, or an executor that
saw the harness report a usage limit) now finishes with
``reason_code: "capacity_ceiling"``. Without a signal it stays a lease
variant: nothing is guessed. A low ``remaining_pct`` on an open run is
surfaced in ``runs_in_flight`` before the run dies.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, monitor, queue_hygiene, run_records, storage
from unified_agent.executor import backends
from unified_agent.quota_observe import FAILURE_PROVIDER_QUOTA_EXHAUSTED, classify_failure_reason
from unified_agent.tools import doctor
from unified_agent.tools.tasks import task_add, task_claim, task_update

AGENT = "Desktop_Agent1"
OTHER = "Desktop_Agent2"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _exec(sql: str, params: tuple = ()) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _past(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _claimed() -> int:
    tid = task_add("capacity", lane="dispatch-ready")["id"]
    with _as(AGENT):
        task_claim(tid, AGENT, expected_lane="dispatch-ready")
    return tid


def _heartbeat(tid: int, usage: dict) -> None:
    with _as(AGENT):
        task_claim(tid, AGENT, expected_lane="dispatch-ready", run_usage=usage)


def _lapse(tid: int) -> None:
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(7200), tid))
    # No agent row: the owner probes absent, so hygiene releases the claim.
    _exec("DELETE FROM agents WHERE 1=?", (1,))
    # A dead session's label is not live: hygiene must release, not renew.
    _exec("UPDATE agents SET last_seen=? WHERE label=?", (_past(86400), AGENT))


def _last_run(tid: int) -> dict:
    return run_records.run_history(task_id=tid)["runs"][0]


LIMIT_HIT = {
    "capacity": {
        "limit_reached": True,
        "source": "claude_cli",
        "detail": "Claude AI usage limit reached",
    }
}


# --- validation -----------------------------------------------------------------


def test_capacity_is_an_accepted_run_usage_field():
    usage = run_records.validate_usage(
        {
            "capacity": {
                "limit_reached": False,
                "remaining_pct": 12.5,
                "resets_at": "2026-10-02T10:00:00+00:00",
                "source": "claude_cli",
                "detail": "5-hour window",
            }
        }
    )
    assert usage["capacity"] == {
        "limit_reached": False,
        "remaining_pct": 12.5,
        "resets_at": "2026-10-02T10:00:00+00:00",
        "source": "claude_cli",
        "detail": "5-hour window",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"capacity": "exhausted"},
        {"capacity": {"limit_reached": "yes"}},
        {"capacity": {"remaining_pct": 101}},
        {"capacity": {"remaining_pct": -1}},
        {"capacity": {"remaining_pct": float("nan")}},
        {"capacity": {"remaining_pct": True}},
        {"capacity": {"plan": "max"}},
        {"capacity": {"resets_at": 5}},
    ],
)
def test_bad_capacity_is_refused(bad):
    with pytest.raises(ToolError, match="capacity"):
        run_records.validate_usage(bad)


# --- finish reason: capacity vs lease ---------------------------------------------


def test_queue_hygiene_release_without_signal_stays_a_lease_variant(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _claimed()
    _lapse(tid)
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    run = _last_run(tid)
    assert run["outcome"] == "abandoned"
    assert run["reason"] == "lease expired; released by queue hygiene"
    assert run["reason_code"] == "lease_expired"


def test_queue_hygiene_release_with_limit_signal_records_capacity_ceiling(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _lapse(tid)
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    run = _last_run(tid)
    assert run["outcome"] == "abandoned"
    assert run["reason_code"] == "capacity_ceiling"
    assert run["reason"].startswith("capacity ceiling: Claude AI usage limit reached")
    assert "lease expired" not in run["reason"].split(";")[0]
    assert run["usage"]["capacity"]["limit_reached"] is True


def test_low_remaining_capacity_alone_is_not_recorded_as_a_ceiling(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _claimed()
    _heartbeat(tid, {"capacity": {"limit_reached": False, "remaining_pct": 3}})
    _lapse(tid)
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    run = _last_run(tid)
    assert run["reason_code"] == "lease_expired"
    assert run["reason"] == "lease expired; released by queue hygiene"


def test_doctor_stale_lock_reclaim_with_limit_signal_records_capacity_ceiling():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _lapse(tid)
    doctor.reclaim_stale_task_locks()
    run = _last_run(tid)
    assert run["finished_by"] == "system-doctor"
    assert run["reason_code"] == "capacity_ceiling"


def test_doctor_stale_lock_reclaim_without_signal_stays_lease():
    tid = _claimed()
    _lapse(tid)
    doctor.reclaim_stale_task_locks()
    run = _last_run(tid)
    assert run["reason"] == "lease expired; stale lock reclaimed by system_doctor"
    assert run["reason_code"] == "lease_expired"


def test_takeover_after_lapse_with_limit_signal_records_capacity_ceiling():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as(OTHER):
        task_claim(tid, OTHER, expected_lane="dispatch-ready")
    prior = run_records.run_history(task_id=tid)["runs"][1]
    assert prior["agent"] == AGENT
    assert prior["outcome"] == "abandoned"
    assert prior["reason_code"] == "capacity_ceiling"


def test_takeover_after_lapse_without_signal_is_lease_expired():
    tid = _claimed()
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as(OTHER):
        task_claim(tid, OTHER, expected_lane="dispatch-ready")
    prior = run_records.run_history(task_id=tid)["runs"][1]
    assert prior["reason"] == "claimed after the lease lapsed"
    assert prior["reason_code"] == "lease_expired"


def test_release_reporting_a_limit_records_capacity_ceiling():
    tid = _claimed()
    with _as(AGENT):
        task_update(tid, status="open", run_usage=LIMIT_HIT)
    run = _last_run(tid)
    assert run["outcome"] == "released"
    assert run["reason_code"] == "capacity_ceiling"


def test_completion_is_never_relabelled_as_a_capacity_ceiling():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    with _as(AGENT):
        task_update(tid, status="done")
    run = _last_run(tid)
    assert run["outcome"] == "completed"
    assert run["reason_code"] is None


def test_operator_termination_is_not_relabelled():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _exec(
        "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
        "lock_expires_at=NULL WHERE id=?",
        (tid,),
    )
    run_records.record_abandoned(
        tid, agent=AGENT, closed_by="agent_lifecycle", reason="agent terminated: operator"
    )
    run = _last_run(tid)
    assert run["reason"] == "agent terminated: operator"
    assert run["reason_code"] is None


# --- warning before the run dies -----------------------------------------------


def test_runs_in_flight_warns_on_low_remaining_capacity():
    low = _claimed()
    ok = _claimed()
    _heartbeat(low, {"capacity": {"remaining_pct": 5, "resets_at": "2026-10-02T10:00:00Z"}})
    _heartbeat(ok, {"capacity": {"remaining_pct": 80}})
    view = monitor.observability_manage("runs_in_flight")
    warnings = view["capacity_warnings"]
    assert [w["task_id"] for w in warnings] == [low]
    [w] = warnings
    assert w["agent"] == AGENT
    assert w["remaining_pct"] == 5
    assert w["resets_at"] == "2026-10-02T10:00:00Z"
    assert w["limit_reached"] is False


def test_runs_in_flight_warns_when_limit_reached():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    [w] = run_records.runs_in_flight()["capacity_warnings"]
    assert w["task_id"] == tid
    assert w["limit_reached"] is True


def test_runs_in_flight_has_no_capacity_warnings_without_signals():
    _claimed()
    assert run_records.runs_in_flight()["capacity_warnings"] == []


# --- harness signal -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached|1759400000",
        "5-hour limit reached ∙ resets 3am",
        "You've hit your limit · resets 10am (Europe/Dublin)",
        "Weekly limit reached",
    ],
)
def test_claude_cli_usage_limit_messages_classify_as_quota(text):
    assert classify_failure_reason(text) == FAILURE_PROVIDER_QUOTA_EXHAUSTED


def test_capacity_from_failure_only_for_usage_limit_text():
    hit = backends.capacity_from_failure("Claude AI usage limit reached|1759400000")
    assert hit == {
        "limit_reached": True,
        "source": "executor",
        "detail": "Claude AI usage limit reached|1759400000",
    }
    assert backends.capacity_from_failure("Traceback: KeyError") is None
    assert backends.capacity_from_failure(None) is None


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
def test_executor_records_a_harness_usage_limit_on_the_run(mock_pi, executor_db):
    from unified_agent.executor.daemon import ExecutorDaemon
    from unified_agent.tools import delegate as delegate_tool

    mock_pi.return_value = {
        "status": "error",
        "text": "",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": 1,
        "error": "Claude AI usage limit reached|1759400000",
        "usage": None,
    }
    task_id = delegate_tool.delegate(
        task_spec="capacity", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]
    run_id = None
    real_retry = ExecutorDaemon._retry_delegation_to_pending

    def capture(self, *args, **kwargs):
        nonlocal run_id
        conn = storage.connect()
        try:
            run_id = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        return real_retry(self, *args, **kwargs)

    with patch.object(ExecutorDaemon, "_retry_delegation_to_pending", capture):
        ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False).poll_once()

    assert run_id is not None
    run = run_records.get_run(run_id)
    assert run["usage"]["capacity"]["limit_reached"] is True
    assert run["usage"]["capacity"]["source"] == "executor"


# --- executor spool: the latest attempt decides capacity -------------------------

LIMIT = {"limit_reached": True, "source": "executor", "detail": "Claude AI usage limit reached"}


def _daemon():
    from unified_agent.executor.daemon import ExecutorDaemon

    return ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)


def test_spool_success_after_a_limit_clears_it(executor_db):
    d = _daemon()
    first = d._accumulate_run_usage(1, "run", "a1", {"capacity": LIMIT})
    assert first["capacity"] == LIMIT
    total = _daemon()._accumulate_run_usage(
        1, "run", "a2", {"cost_usd": 1, "input_tokens": 10}, succeeded=True
    )
    assert total["capacity"]["limit_reached"] is False
    assert total["capacity"]["source"] == "executor"
    assert total["cost_usd"] == 1 and total["input_tokens"] == 10


def test_spool_later_attempt_without_capacity_does_not_resurrect_a_cleared_limit(executor_db):
    d = _daemon()
    d._accumulate_run_usage(1, "run", "a1", {"capacity": LIMIT})
    d._accumulate_run_usage(1, "run", "a2", {}, succeeded=True)
    total = d._accumulate_run_usage(1, "run", "a3", {"input_tokens": 5})
    assert total["capacity"]["limit_reached"] is False
    # Replaying the first attempt's id keeps its place in the order.
    replay = d._accumulate_run_usage(1, "run", "a1", {"capacity": LIMIT})
    assert replay["capacity"]["limit_reached"] is False


def test_spool_later_capacity_report_replaces_an_earlier_one(executor_db):
    d = _daemon()
    d._accumulate_run_usage(1, "run", "a1", {"capacity": LIMIT})
    later = {"limit_reached": True, "source": "executor", "detail": "Weekly limit reached"}
    total = d._accumulate_run_usage(1, "run", "a2", {"capacity": later})
    assert total["capacity"] == later


def test_spool_later_failure_without_a_limit_signal_clears_the_old_limit(executor_db):
    # The later attempt ran and stopped for another reason: the run's latest
    # stop was not the ceiling, so a lapse must not read as capacity_ceiling.
    d = _daemon()
    d._accumulate_run_usage(1, "run", "a1", {"capacity": LIMIT})
    total = d._accumulate_run_usage(1, "run", "a2", {"input_tokens": 3})
    assert total["capacity"]["limit_reached"] is False


def test_spool_without_any_capacity_reports_none(executor_db):
    d = _daemon()
    d._accumulate_run_usage(1, "run", "a1", {"input_tokens": 3})
    total = d._accumulate_run_usage(1, "run", "a2", {"input_tokens": 4}, succeeded=True)
    assert "capacity" not in total
    assert total == {"input_tokens": 7, "cost_complete": False}


def test_cleared_spool_report_clears_the_hub_run_so_a_lapse_stays_lease(executor_db, monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = task_add("spool", lane="dispatch-ready")["id"]
    with _as("Desktop_agent3"):
        run_id = task_claim(tid, "Desktop_agent3", expected_lane="dispatch-ready")["current_run_id"]
    d = _daemon()
    d._report_run_usage(
        tid, run_id, d._accumulate_run_usage(tid, run_id, "a1", {"capacity": LIMIT})
    )
    assert run_records.get_run(run_id)["usage"]["capacity"]["limit_reached"] is True
    d._report_run_usage(tid, run_id, d._accumulate_run_usage(tid, run_id, "a2", {}, succeeded=True))
    assert run_records.get_run(run_id)["usage"]["capacity"]["limit_reached"] is False
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(7200), tid))
    _exec("DELETE FROM agents WHERE label=?", ("Desktop_agent3",))
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    run = run_records.get_run(run_id)
    assert run["outcome"] == "abandoned"
    assert run["reason_code"] == "lease_expired"


# --- review follow-ups ------------------------------------------------------------


def test_partial_capacity_heartbeat_keeps_an_earlier_reached_limit(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _heartbeat(tid, {"capacity": {"remaining_pct": 0, "resets_at": "2026-10-02T10:00:00Z"}})
    run = _last_run(tid)
    assert run["usage"]["capacity"]["limit_reached"] is True
    assert run["usage"]["capacity"]["remaining_pct"] == 0
    _lapse(tid)
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert _last_run(tid)["reason_code"] == "capacity_ceiling"


def test_explicit_limit_reached_false_clears_an_earlier_limit(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    _heartbeat(tid, {"capacity": {"limit_reached": False, "remaining_pct": 90}})
    _lapse(tid)
    queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert _last_run(tid)["reason_code"] == "lease_expired"


def test_transient_rate_limit_is_not_a_capacity_ceiling():
    assert backends.capacity_from_failure("HTTP 429: rate limit reached, retry in 20s") is None
    assert backends.capacity_from_failure("rate_limit reached") is None


def test_run_failure_capacity_reads_quota_text_behind_a_generic_error():
    res = {
        "status": "error",
        "error": "claude produced no result event",
        "text": "Working on it.\nWeekly limit reached ∙ resets Mon 9am",
    }
    hit = backends.capacity_from_run_result(res)
    assert hit is not None and hit["limit_reached"] is True
    assert "Weekly limit reached" in hit["detail"]


def test_run_failure_capacity_ignores_limit_words_early_in_long_output():
    prose = "We should record when the usage limit reached message appears. " + "x" * 2000
    res = {"status": "error", "error": "exit 1", "text": prose}
    assert backends.capacity_from_run_result(res) is None


def test_run_failure_capacity_is_none_on_success():
    res = {"status": "success", "error": None, "text": "Claude AI usage limit reached"}
    assert backends.capacity_from_run_result(res) is None


def test_capacity_warning_says_when_the_run_is_no_longer_running():
    tid = _claimed()
    _heartbeat(tid, LIMIT_HIT)
    [live] = run_records.runs_in_flight()["capacity_warnings"]
    assert live["run_state"] == "running"
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    [lapsed] = run_records.runs_in_flight()["capacity_warnings"]
    assert lapsed["run_state"] == "abandoned"
    assert lapsed["abandon_reason"] == "lease_expired"
