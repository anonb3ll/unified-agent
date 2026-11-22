"""Tests for Red Team QA tool."""

from unified_agent import storage
from unified_agent.tools import red_team
from unified_agent.tools.red_team import red_team_qa


def test_red_team_qa_basic(monkeypatch, tmp_path):
    # Mock security_audit, python_lint_and_format, and subprocess.run to isolate unit test
    monkeypatch.setattr(red_team, "security_audit", lambda path: {"status": "PASS"})
    monkeypatch.setattr(red_team, "python_lint_and_format", lambda path: {"fixed": 0})

    class FakeCompletedProcess:
        returncode = 0
        stdout = "10 passed"
        stderr = ""

    monkeypatch.setattr(red_team.subprocess, "run", lambda *args, **kwargs: FakeCompletedProcess())

    res = red_team_qa(path=".")
    assert res["verdict"] == "PASS"
    assert res["security_status"] == "PASS"
    assert res["tests_status"] == "PASS"


def test_red_team_qa_with_task_annotation(monkeypatch):
    monkeypatch.setattr(red_team, "security_audit", lambda path: {"status": "PASS"})
    monkeypatch.setattr(red_team, "python_lint_and_format", lambda path: {"fixed": 0})

    class FakeCompletedProcess:
        returncode = 0
        stdout = "10 passed"
        stderr = ""

    monkeypatch.setattr(red_team.subprocess, "run", lambda *args, **kwargs: FakeCompletedProcess())

    # Create task in DB
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (888, 'QA Test Task', 'initial notes', 'doing', 'med', '2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    res = red_team_qa(path=".", task_id=888)
    assert res["verdict"] == "PASS"
    assert res["task_annotated"] is True

    # Verify task notes updated
    conn = storage.connect()
    try:
        cur = conn.execute("SELECT notes FROM tasks WHERE id=888")
        notes = cur.fetchone()["notes"]
        assert "[red-team-qa: PASS @" in notes
    finally:
        conn.close()
