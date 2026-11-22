"""Scout success path must reach the shared success return (task #1542).

The scout branch of ``_execute_claimed_task`` skips the daemon commit, so the
shared success return used to read an undefined ``commit_res`` and raise
``UnboundLocalError`` *after* the delegation/evidence callbacks had already
run. A delivered scout was then reported as a failed attempt.
"""

import os
import subprocess
from pathlib import Path

import pytest
from product_repo_env import point_default_project_at

from unified_agent import storage
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.scout import report_path_from_text
from unified_agent.tools import delegate as delegate_tool


@pytest.fixture(autouse=True)
def executor_env(isolated_env, monkeypatch):
    """Standalone executor: no live hub, in-process fallback, isolated DB."""
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    # Closed port so the outcome never depends on a hub on the developer's box.
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    storage._connection = None
    storage.init_db()
    yield
    if storage._connection:
        storage._connection.close()
        storage._connection = None


class _Backend:
    name = "codex"
    session_kind = "codex_run"

    def __init__(self, *, text, write, scout=False):
        self.text = text
        self.write = write
        self.scout = scout
        self.calls = []
        self.reports = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        text = self.text
        if self.scout:
            # Behave like a correct backend: report at the exact attempt-specific
            # path the prompt demands (task #1528), not a fixed one.
            report = report_path_from_text(kwargs["prompt"])
            self.reports.append(report)
            text = f"{text}\n[scout: report={report}]\n"
        if self.write:
            cwd = Path(kwargs["cwd"])
            root = cwd if cwd.is_absolute() else Path(os.environ["UA_WORKSPACE"]) / cwd
            (root / "child.txt").write_text("child change\n")
        return {
            "status": "success",
            "text": text,
            "tools_used": [],
            "files_modified": ["child.txt"] if self.write else [],
            "events_count": 1,
            "exit_code": 0,
            "error": None,
        }


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _workspace_repo(workspace):
    _git("init", "-b", "main", cwd=workspace)
    _git("config", "user.name", "Test User", cwd=workspace)
    _git("config", "user.email", "test@example.com", cwd=workspace)
    (workspace / "initial.txt").write_text("hello")
    _git("add", "initial.txt", cwd=workspace)
    _git("commit", "-m", "feat: initial commit", cwd=workspace)
    point_default_project_at(workspace)


def _daemon(backend, monkeypatch):
    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False, backend=backend)
    counts = {"completed": 0, "failed": 0, "journal": 0}

    real_completed = daemon._mark_delegation_completed
    real_failed = daemon._mark_delegation_failed
    real_call_tool = daemon.hub_client.call_tool

    def completed(*args, **kwargs):
        counts["completed"] += 1
        return real_completed(*args, **kwargs)

    def failed(*args, **kwargs):
        counts["failed"] += 1
        return real_failed(*args, **kwargs)

    def call_tool(name, arguments=None, *args, **kwargs):
        if name == "vault_journal":
            counts["journal"] += 1
        return real_call_tool(name, arguments, *args, **kwargs)

    monkeypatch.setattr(daemon, "_mark_delegation_completed", completed)
    monkeypatch.setattr(daemon, "_mark_delegation_failed", failed)
    monkeypatch.setattr(daemon.hub_client, "call_tool", call_tool)
    return daemon, counts


def _row(table, task_id):
    conn = storage.connect()
    try:
        key = "id" if table == "tasks" else "task_id"
        return conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (task_id,)).fetchall()
    finally:
        conn.close()


def test_successful_scout_completes_without_a_commit(isolated_env, monkeypatch):
    _workspace_repo(isolated_env)
    backend = _Backend(text="scouted", write=False, scout=True)
    daemon, counts = _daemon(backend, monkeypatch)
    created = delegate_tool.delegate(
        task_spec="[task-shape: scout]\n\nInvestigate the login flow.",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_Agent2",
        callback="none",
    )
    task_id = created["task_id"]

    res = daemon.poll_once()

    # Truthful successful return, and no commit reported or created.
    assert res is not None
    assert res["status"] == "success"
    assert res["waiting_for_review"] is True
    assert res["commit"] is None
    worktree = isolated_env / res["worktree"]
    assert _git("rev-list", "--count", "main..HEAD", cwd=worktree).stdout.strip() == "0"

    # Evidence points at the report, not a SHA; scout markers are stamped.
    (task,) = _row("tasks", task_id)
    assert len(backend.reports) == 1 and backend.reports[0]
    assert task["evidence_ref"] == f"vault:{backend.reports[0]}"
    assert "[scout: no-pr]" in task["notes"]

    # Completion callbacks ran exactly once; nothing was reported as failed.
    assert counts == {"completed": 1, "failed": 0, "journal": 1}
    delegation = delegate_tool.delegate_get(created["delegation_id"])["delegation"]
    assert delegation["status"] == "completed"
    assert delegate_tool.delegate_list(status="failed")["delegations"] == []

    # Exactly one attempt row, typed 'scout', finished as the daemon's success (task #1543:
    # the executor_attempts CHECK used to reject 'scout', so scout runs left no
    # trust-ladder evidence at all), and no second execution on the next poll.
    attempts = _row("executor_attempts", task_id)
    assert len(attempts) == 1
    assert attempts[0]["task_type"] == "scout"
    assert attempts[0]["status"] == "success"
    assert attempts[0]["outcome"] == "waiting_review"  # daemon success outcome
    assert attempts[0]["finished_at"] is not None
    assert daemon.consecutive_failures == 0
    assert daemon.poll_once() is None
    assert len(backend.calls) == 1


def test_ordinary_coding_success_still_returns_its_commit(isolated_env, monkeypatch):
    _workspace_repo(isolated_env)
    backend = _Backend(text="ok", write=True)
    daemon, counts = _daemon(backend, monkeypatch)
    task_id = delegate_tool.delegate(
        task_spec="[code-task]\n\nChange a file.",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_Agent2",
        callback="none",
    )["task_id"]

    res = daemon.poll_once()

    worktree = isolated_env / res["worktree"]
    sha = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    assert res["status"] == "success"
    assert res["commit"] == {"status": "committed", "reason": None, "sha": sha}
    assert _git("rev-list", "--count", "main..HEAD", cwd=worktree).stdout.strip() == "1"
    (task,) = _row("tasks", task_id)
    assert task["evidence_ref"] == sha
    assert counts == {"completed": 1, "failed": 0, "journal": 1}
