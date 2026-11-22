"""Focused evidence contracts for trust-ladder review (task #458)."""

from unified_agent import storage
from unified_agent.executor import evidence
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import tasks as tasks_tool


def test_executor_attempt_is_persistent_and_countable():
    task = tasks_tool.task_add(title="evidence task")
    delegation = delegate_tool.delegate(
        task_spec="evidence run", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )

    attempt_id = evidence.start_attempt(
        task_id=task["id"],
        delegation_id=delegation["delegation_id"],
        agent="Desktop_agent3",
        backend="test",
        task_type="code",
    )
    evidence.finish_attempt(attempt_id, status="success", waiting_for_review=True)

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM executor_attempts WHERE id=?", (attempt_id,)).fetchone()
    finally:
        conn.close()
    assert row["task_type"] == "code"
    assert row["outcome"] == "waiting_review"
    assert row["finished_at"] is not None
    assert evidence.outcomes_snapshot()["attempts"]["waiting_review"] == 1


def test_executor_events_are_durable_and_structured():
    evidence.record_executor_event(
        "quarantine", task_id=7, delegation_id=8, agent="Desktop_agent3", reason="blocked"
    )
    rows = evidence.events.query_events(channels=["agent.executor.quarantine"])
    assert rows[-1]["payload"] == {
        "task_id": 7,
        "delegation_id": 8,
        "agent": "Desktop_agent3",
        "reason": "blocked",
    }


def test_outcome_vocabularies_normalize_unknown_values():
    assert evidence.delegation_outcome("in_flight") == "unknown"
    assert evidence.approval_outcome("denied") == "denied"
    assert evidence.executor_outcome("success", waiting_for_review=True) == "waiting_review"


def test_approval_outcome_maps_pending_to_requested():
    """The approvals table's real status is 'pending' (storage.py's CHECK
    constraint); APPROVAL_OUTCOMES spells the same state 'requested'. Every
    ordinary pending approval used to normalize to 'unknown' without this
    translation, misclassifying all outstanding requests (task #445 /
    PR #201 review)."""
    assert evidence.approval_outcome("pending") == "requested"
    assert evidence.approval_outcome("PENDING") == "requested"


def test_deleting_a_task_preserves_its_executor_attempt_evidence():
    """task_id/delegation_id are ON DELETE SET NULL, not CASCADE: deleting a
    task must not retroactively erase completed execution evidence or change
    historical trust-ladder counts (task #445 / PR #201 review)."""
    task = tasks_tool.task_add(title="evidence survives deletion")
    delegation = delegate_tool.delegate(
        task_spec="evidence run", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    attempt_id = evidence.start_attempt(
        task_id=task["id"],
        delegation_id=delegation["delegation_id"],
        agent="Desktop_agent3",
        backend="test",
    )
    evidence.finish_attempt(attempt_id, status="success")

    tasks_tool.task_delete(task["id"])

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM executor_attempts WHERE id=?", (attempt_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["task_id"] is None
    assert row["outcome"] == "success"


def test_scout_attempt_is_persisted_with_its_own_task_type():
    """'scout' is in evidence.TASK_TYPES, so it must also be storable (task
    #1543: the executor_attempts CHECK used to reject it and start_attempt
    returned None, recording nothing)."""
    task = tasks_tool.task_add(title="scout evidence task")

    attempt_id = evidence.start_attempt(
        task_id=task["id"],
        delegation_id=None,
        agent="Desktop_Agent2",
        backend="test",
        task_type="scout",
    )
    assert attempt_id is not None
    evidence.finish_attempt(attempt_id, status="success", waiting_for_review=True)

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM executor_attempts WHERE id=?", (attempt_id,)).fetchone()
    finally:
        conn.close()
    assert row["task_type"] == "scout"
    assert row["outcome"] == "waiting_review"


def test_start_attempt_stays_best_effort_but_logs_the_swallowed_error(caplog, monkeypatch):
    """A failing INSERT must never break execution (returns None) and must no
    longer vanish silently (task #1543)."""

    def boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(evidence.storage, "connect", boom)
    with caplog.at_level("WARNING", logger="unified_agent.executor"):
        result = evidence.start_attempt(
            task_id=1, delegation_id=None, agent="Desktop_Agent2", backend="test", task_type="scout"
        )
    assert result is None
    assert any(
        r.levelname == "WARNING" and "db unavailable" in r.getMessage() for r in caplog.records
    )
