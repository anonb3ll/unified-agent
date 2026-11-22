"""Failure-outcome classification for truthful lane quota status (task #1081)."""

from __future__ import annotations

import pytest

from unified_agent import storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.dispatch.report import report_outcome
from unified_agent.executor import evidence
from unified_agent.quota_observe import (
    FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED,
    FAILURE_CODE_ERROR,
    FAILURE_ITERATION_BUDGET_TIMEOUT,
    FAILURE_PROVIDER_QUOTA_EXHAUSTED,
    FAILURE_ZERO_OUTPUT_ATTEMPT,
    classify_failure_reason,
    lane_for_agent,
)
from unified_agent.tools import delegate, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    storage.init_db()
    return tmp_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "openai-codex (gpt-5.6-luna, first failover): HTTP 429: "
            "The usage limit has been reached — quota exhausted",
            FAILURE_PROVIDER_QUOTA_EXHAUSTED,
        ),
        (
            "Provider quota exhausted; active cooldown period until 2026-08-20T01:15:00Z",
            FAILURE_PROVIDER_QUOTA_EXHAUSTED,
        ),
        ("iteration budget exhausted (150/150)", FAILURE_ITERATION_BUDGET_TIMEOUT),
        ("Iteration budget exhausted", FAILURE_ITERATION_BUDGET_TIMEOUT),
        ("tests failed: AssertionError in test_foo", FAILURE_CODE_ERROR),
        ("exit_code:1 claude -p", FAILURE_CODE_ERROR),
        (
            "zero_output_attempt: backend reported success but worktree has no new commit",
            FAILURE_ZERO_OUTPUT_ATTEMPT,
        ),
        (
            "agy_teardown_resume_exhausted: still torn down after resumes; resume_count=2",
            FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED,
        ),
        ("", FAILURE_CODE_ERROR),
        (None, FAILURE_CODE_ERROR),
    ],
)
def test_classify_failure_reason_distinguishes_quota_budget_and_code(text, expected):
    assert classify_failure_reason(text) == expected


@pytest.mark.parametrize(
    ("agent", "lane"),
    [
        ("Desktop_Agent1", "claude"),
        ("server_agent1", "claude"),
        ("Laptop_Agent1", "claude"),
        ("Desktop_Agent2", "codex"),
        ("server_agent2", "codex"),
        ("Desktop_agent3", "agy"),
        ("server_agent3", "agy"),
        ("Desktop_Agent4", "cursor"),
        ("Laptop_Agent4", "cursor"),
        ("Desktop_Agent5", "hermes"),
    ],
)
def test_lane_for_agent_maps_known_labels(agent, lane):
    assert lane_for_agent(agent) == lane


def _spec() -> str:
    return (
        "### Objective\nClassify dispatch failures\n"
        "### Scope\n- src/unified_agent/\n"
        "### Definition of Done\n- failure_class persisted\n"
        "### TDD Constraints\n- Must be real: dispatch_runs.failure_class\n"
        "### Boundaries\n- Must not enable production dispatch\n"
    )


def _launched_run(agent: str = "Desktop_Agent1") -> dict:
    packed = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent=agent, callback="memory"
    )
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ 2026-08-25T00:00:00Z]")
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=RoutedLaunchBoundary(spawner=StubProcessSpawner()),
    )
    assert out["launched"] is True
    return {"run_id": out["run_id"], "task_id": packed["task_id"], "agent": agent}


def test_record_run_outcome_persists_provider_quota_failure_class(db, monkeypatch):
    run = _launched_run("Desktop_Agent1")
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", run["agent"])

    assert (
        report_outcome(
            "failed",
            failure_reason="HTTP 429: The usage limit has been reached — quota exhausted",
        )["ok"]
        is True
    )

    row = coordinator.get_run(run["run_id"])
    assert row["failure_class"] == FAILURE_PROVIDER_QUOTA_EXHAUSTED


def test_record_run_outcome_persists_iteration_budget_failure_class(db, monkeypatch):
    run = _launched_run("Desktop_Agent1")
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", run["agent"])

    assert (
        report_outcome(
            "failed",
            failure_reason="iteration budget exhausted (150/150)",
        )["ok"]
        is True
    )

    row = coordinator.get_run(run["run_id"])
    assert row["failure_class"] == FAILURE_ITERATION_BUDGET_TIMEOUT


def test_finish_attempt_persists_zero_output_failure_class(db):
    """zero_output_attempt detail feeds failure_class for the #1142 pair gate."""
    task_id = tasks.task_add(title="zero output attempt", priority="low")["id"]
    attempt_id = evidence.start_attempt(
        task_id=task_id,
        delegation_id=None,
        agent="Desktop_agent3",
        backend="agy",
        task_type="code",
    )
    assert attempt_id is not None
    evidence.finish_attempt(
        attempt_id,
        status="error",
        detail=(
            "zero_output_attempt: backend reported success but worktree has no "
            "new commit (clean tree)"
        ),
    )

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT failure_class, detail FROM executor_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["failure_class"] == FAILURE_ZERO_OUTPUT_ATTEMPT
    assert "zero_output_attempt" in row["detail"]


def test_finish_attempt_persists_failure_class_on_executor_detail(db):
    task_id = tasks.task_add(title="quota classify attempt", priority="low")["id"]
    attempt_id = evidence.start_attempt(
        task_id=task_id,
        delegation_id=None,
        agent="Desktop_Agent1",
        backend="claude",
        task_type="code",
    )
    assert attempt_id is not None
    evidence.finish_attempt(
        attempt_id,
        status="failure",
        detail="HTTP 429: The usage limit has been reached — quota exhausted",
    )

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT failure_class, detail FROM executor_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["failure_class"] == FAILURE_PROVIDER_QUOTA_EXHAUSTED


def test_lane_for_agent_resolves_exported_family_segments(monkeypatch):
    from unified_agent import config, quota_observe

    monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "Agent2", "Codex")
    monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "agent3", "agy")
    monkeypatch.setitem(config.SEGMENT_TO_FAMILY, "Agent7", "Devin")

    assert quota_observe.lane_for_agent("Desktop_Agent2") == "codex"
    assert quota_observe.lane_for_agent("Desktop_agent3") == "agy"
    assert quota_observe.lane_for_agent("Desktop_Agent7") == "unknown"
    assert quota_observe.lane_for_agent("custom-codex-worker") == "codex"
