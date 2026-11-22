"""Unconfirmed child cleanup fences automatic retry (task #1950, PR #1230 review round 3).

A backend that could not confirm its stopped process group is gone returns
``cleanup_confirmed: False``. Retrying would start a second writer next to a
possible survivor, so the daemon holds the work for an operator instead.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from unified_agent import storage
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate as delegate_tool

UNCONFIRMED_ERROR = (
    "Execution stalled: no output for 1800 seconds; process group 4321 was not confirmed "
    "gone after SIGTERM and SIGKILL (...); inspect it with `ps -g 4321` and stop it before "
    "retrying this task"
)


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


def _timeout_result(*, cleanup_confirmed: bool) -> dict:
    return {
        "status": "timeout",
        "text": "",
        "tools_used": [],
        "files_modified": [],
        "events_count": 1,
        "exit_code": -1,
        "error": UNCONFIRMED_ERROR if not cleanup_confirmed else "Execution stalled",
        "cleanup_confirmed": cleanup_confirmed,
    }


def _delegate() -> int:
    return delegate_tool.delegate(
        task_spec="cleanup fence", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )["task_id"]


def _task(task_id: int) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        return {"status": row["status"], "notes": row["notes"] or ""}
    finally:
        conn.close()


def _delegation_status(task_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT status FROM delegations WHERE task_id=?", (task_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_unconfirmed_cleanup_is_held_and_never_retried(mock_pi, executor_db):
    mock_pi.return_value = _timeout_result(cleanup_confirmed=False)
    task_id = _delegate()

    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    result = first.poll_once()

    assert result["failed_terminal"] is True
    assert result["status"] == "cleanup_unconfirmed"
    assert "backoff_active" not in result
    task = _task(task_id)
    assert task["status"] == "blocked"
    assert "cleanup unconfirmed" in task["notes"]
    assert "ps -g 4321" in task["notes"]
    assert _delegation_status(task_id) == "failed"

    # No second attempt starts, however often the daemon polls.
    later = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    later.backoff_base_s = 0
    later.poll_once()
    later.poll_once()
    assert mock_pi.call_count == 1


@patch("unified_agent.tools.pi_runner.pi_run_task")
def test_confirmed_cleanup_timeout_keeps_the_retry_path(mock_pi, executor_db):
    mock_pi.return_value = _timeout_result(cleanup_confirmed=True)
    task_id = _delegate()

    first = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    first.backoff_base_s = 0
    assert first.poll_once()["backoff_active"]
    assert _delegation_status(task_id) != "failed"

    again = ExecutorDaemon(agent_label="Desktop_agent3", shadow_mode=False)
    again.backoff_base_s = 0
    again.poll_once()
    assert mock_pi.call_count == 2
