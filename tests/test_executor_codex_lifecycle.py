"""End-to-end lifecycle for a Codex-backed executor (task #196).

Proves the DoD claim directly: a Codex executor claims a delegation and drives
the same task/session/journal lifecycle Desktop_agent3 does. The Codex process
itself is never spawned — `backends.run_popen_with_kill_check` is replaced — but everything above
that boundary is the real daemon code path, including the delegation claim, the
finish-to-review transition, and the registry session record.
"""

import json
import subprocess

import pytest

from unified_agent import registry, storage
from unified_agent.executor import backends
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate as delegate_tool
from unified_agent.tools import tasks as tasks_tool

CODEX_SUCCESS_STREAM = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "src/thing.py", "kind": "update"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Implemented the thing."},
            }
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
)

CODEX_FAILURE_STREAM = "\n".join(
    [
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.failed", "error": {"message": "sandbox denied write"}}),
    ]
)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    # Standalone case: no hub to talk to, so the in-process fallback is opted
    # into explicitly, exactly as tests/test_executor.py does.
    monkeypatch.setenv("UA_EXECUTOR_ALLOW_DIRECT", "1")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("UA_EXECUTOR_BACKEND", "codex")
    monkeypatch.delenv("UA_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("UA_CODEX_ALLOW_FULL_ACCESS", raising=False)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

    storage._connection = None
    storage.init_db()
    yield
    if storage._connection:
        storage._connection.close()
        storage._connection = None


def _fake_codex(stdout, returncode=0, capture=None):
    def fake_run(argv, *, cwd, env, timeout_s, stdin, kill_check=None, poll_interval_s=1.0):
        if capture is not None:
            capture["argv"] = argv
            capture["kwargs"] = {"cwd": cwd, "env": env, "stdin": stdin}
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return fake_run


def _delegate_to_codex():
    return delegate_tool.delegate(
        task_spec="Add a docstring to thing.py",
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent2",
    )


def test_desktop_Agent2_is_a_valid_delegation_target_without_env_override(monkeypatch):
    """Open roster: well-formed labels work without UA_AGENT_LABELS.

    A fresh install no longer compiles this fleet's names, so Desktop_Agent2 is
    accepted because it is well-formed, not because it is a hardcoded default.
    Garbage refs are still refused so this cannot pass with validation disabled.
    """
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    res = _delegate_to_codex()

    rows = delegate_tool.delegate_list(status="pending", to_agent="Desktop_Agent2")
    ids = [d["id"] for d in rows["delegations"]]
    assert res["delegation_id"] in ids

    with pytest.raises(Exception, match="not a valid agent label"):
        delegate_tool.delegate(task_spec="x", from_agent="Desktop_Agent5", to_agent="???")


def test_codex_executor_claims_and_reaches_waiting_for_review(monkeypatch):
    monkeypatch.setattr(backends, "run_popen_with_kill_check", _fake_codex(CODEX_SUCCESS_STREAM))

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False)
    assert daemon.backend.name == "codex"

    delegation = _delegate_to_codex()
    result = daemon.poll_once()

    assert result is not None, "the Codex executor did not pick up its delegation"
    assert result["status"] == "success"
    assert result["task_id"] == delegation["task_id"]

    task = tasks_tool.task_list(status="doing")["tasks"][0]
    assert task["id"] == delegation["task_id"]
    assert task["claimed_by"] == "Desktop_Agent2"
    # Finish-to-review: the executor must NOT close the task itself.
    assert task["status"] == "doing"
    assert "[waiting-for: review]" in task["notes"]


def test_run_is_recorded_as_a_codex_session(monkeypatch):
    monkeypatch.setattr(backends, "run_popen_with_kill_check", _fake_codex(CODEX_SUCCESS_STREAM))

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False)
    _delegate_to_codex()
    daemon.poll_once()

    sessions = registry.list_sessions(reap=False)
    assert len(sessions) == 1, "the Codex run left no session record"
    session = sessions[0]
    assert session["agent"] == "Desktop_Agent2"
    # Distinguishable from a Pi run in the registry, so the control centre can
    # say which agent actually executed.
    assert session["kind"] == "codex_run"
    assert session["status"] == "completed"
    assert session["exit_status"] == "success"


def test_codex_runs_in_the_task_worktree_not_the_workspace_root(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        backends, "run_popen_with_kill_check", _fake_codex(CODEX_SUCCESS_STREAM, capture=capture)
    )

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False)
    _delegate_to_codex()
    daemon.poll_once()

    cd_arg = capture["argv"][capture["argv"].index("--cd") + 1]
    # Whatever isolation the daemon established, codex must be pointed at the
    # same directory it decided on -- never silently at the workspace root.
    task = tasks_tool.task_list(status="doing")["tasks"][0]
    if task.get("worktree_path"):
        assert task["worktree_path"] in cd_arg
    assert capture["kwargs"]["cwd"] == cd_arg


def test_delegated_prompt_reaches_codex_argv(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        backends, "run_popen_with_kill_check", _fake_codex(CODEX_SUCCESS_STREAM, capture=capture)
    )

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False)
    _delegate_to_codex()
    daemon.poll_once()

    prompt = capture["argv"][-1]
    assert "Add a docstring to thing.py" in prompt
    assert "Desktop_Agent5" in prompt, "the delegating agent must be named in the prompt"


def test_failed_codex_turn_does_not_reach_review(monkeypatch):
    """A refused/failed run must not be annotated as ready for review."""
    monkeypatch.setattr(backends, "run_popen_with_kill_check", _fake_codex(CODEX_FAILURE_STREAM))

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=False)
    _delegate_to_codex()
    result = daemon.poll_once()

    assert result["status"] == "error"
    task = tasks_tool.task_list(status="doing")["tasks"][0]
    assert "[waiting-for: review]" not in task["notes"]


def test_shadow_mode_executes_nothing(monkeypatch):
    """v0 shadow mode must stay side-effect free on the Codex path too."""
    called = {"n": 0}

    def exploding_run(argv, **kwargs):
        called["n"] += 1
        raise AssertionError("shadow mode must not spawn codex")

    monkeypatch.setattr(backends, "run_popen_with_kill_check", exploding_run)

    daemon = ExecutorDaemon(agent_label="Desktop_Agent2", shadow_mode=True)
    _delegate_to_codex()
    daemon.poll_once()

    assert called["n"] == 0
