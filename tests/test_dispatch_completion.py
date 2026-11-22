"""Completion reporting for a real spawned session (task #601).

The gap this closes: before it, ``record_run_outcome`` had exactly two callers
— ``worker.py`` (the #551 smoke worker, which "never execs real agent CLIs")
and the ``max_attempts_exceeded`` dead-letter path. A run started by
``CliProcessSpawner`` reached ``launched`` and stopped there.
"""

from __future__ import annotations

import pytest

from unified_agent import storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RoutedLaunchBoundary, StubProcessSpawner
from unified_agent.dispatch.report import (
    NotDispatchedError,
    report_exit,
    report_outcome,
    resolve_actor,
    resolve_run_id,
    state_for_exit_code,
)
from unified_agent.tools import delegate, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("UA_DISPATCH_RUN_ID", raising=False)
    monkeypatch.delenv("UA_DISPATCH_AGENT", raising=False)
    storage.init_db()
    return tmp_path


def _spec() -> str:
    return (
        "### Objective\nCompletion reporting\n"
        "### Scope\n- src/unified_agent/dispatch/\n"
        "### Definition of Done\n- run reaches a terminal state\n"
        "### TDD Constraints\n- Must be real: run persistence\n"
        "### Boundaries\n- Must not enable production dispatch\n"
    )


def _launched_run(agent: str = "Desktop_Agent1") -> dict:
    """An approved delegation carried to state 'launched' — where a real spawn
    leaves it today."""
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


# ── the gap ─────────────────────────────────────────────────────────────────


def test_a_launched_run_stays_launched_until_something_reports(db):
    """Baseline. Documents the exact defect #601 exists to fix: dispatch leaves
    a run at 'launched' and no code path advances it."""
    run = _launched_run()
    assert coordinator.get_run(run["run_id"])["state"] == "launched"


# ── identity resolution ─────────────────────────────────────────────────────


def test_run_id_comes_from_the_spawn_environment(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", "run-from-env")
    assert resolve_run_id() == "run-from-env"
    assert resolve_run_id("explicit-wins") == "explicit-wins"


def test_refuses_rather_than_inventing_a_run_id(db, monkeypatch):
    """A human running the CLI by hand must not silently report against some
    other agent's run."""
    monkeypatch.delenv("UA_DISPATCH_RUN_ID", raising=False)
    with pytest.raises(NotDispatchedError, match="not started by approved dispatch"):
        resolve_run_id()
    for blank in ("", "   "):
        monkeypatch.setenv("UA_DISPATCH_RUN_ID", blank)
        with pytest.raises(NotDispatchedError):
            resolve_run_id()


def test_actor_falls_back_to_none_not_a_made_up_identity(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_AGENT", "Desktop_agent3")
    assert resolve_actor() == "Desktop_agent3"
    assert resolve_actor("Desktop_Agent2") == "Desktop_Agent2"
    monkeypatch.delenv("UA_DISPATCH_AGENT", raising=False)
    assert resolve_actor() is None


# ── the fix ─────────────────────────────────────────────────────────────────


def test_self_report_takes_a_launched_run_terminal(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", run["agent"])

    result = report_outcome("finished", evidence_ref="https://github.com/x/y/pull/1")
    assert result["ok"] is True

    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished"
    assert row["evidence_ref"] == "https://github.com/x/y/pull/1"


def _live_runs() -> int:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatch_runs "
            "WHERE state IN ('claimed','launched','running')"
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def test_the_run_leaves_the_live_set_so_a_later_one_can_start(db, monkeypatch):
    """A terminal run must free its slot.

    Two things depend on this. The partial unique index
    ``idx_dispatch_runs_live_task`` blocks any further run for the task while
    one is live, and PR #290's ``UA_DISPATCH_MAX_LIVE_RUNS`` ceiling counts the
    same set host-wide — so a run stuck at 'launched' permanently consumes
    both. Counted here with plain SQL rather than #290's ``live_run_count`` so
    this branch does not depend on an unmerged one."""
    run = _launched_run()
    assert _live_runs() == 1
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_outcome("finished")["ok"] is True
    assert _live_runs() == 0


def test_failure_is_recorded_with_a_reason(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_outcome("failed", failure_reason="tests failed")["ok"] is True
    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "failed"
    assert row["failure_reason"] == "tests failed"


# ── supervisor path (exit status) ───────────────────────────────────────────


def test_exit_code_mapping_treats_unobserved_as_failure():
    """A process we never saw exit must not be recorded as having completed its
    work. Signal-terminated children (negative returncode) are failures too."""
    assert state_for_exit_code(0) == "finished"
    for bad in (1, 2, 127, -9, -15, None):
        assert state_for_exit_code(bad) == "failed", bad


def test_report_exit_records_the_code_in_the_reason(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_exit(3, detail="claude -p")["ok"] is True
    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "failed"
    assert "exit_code:3" in row["failure_reason"]
    assert "claude -p" in row["failure_reason"]


def test_report_exit_zero_is_finished_with_no_failure_reason(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_exit(0)["ok"] is True
    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished"
    assert not row["failure_reason"]


def test_unobserved_exit_says_so_rather_than_printing_none(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_exit(None)["ok"] is True
    assert "not observed" in coordinator.get_run(run["run_id"])["failure_reason"]


# ── idempotency: both reporters firing is the design working ────────────────


def test_a_duplicate_report_is_refused_without_mutating(db, monkeypatch):
    """The supervisor and the agent both reporting is expected, not a bug. The
    second one must not write a second edge or change the recorded outcome."""
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert report_outcome("finished", evidence_ref="first")["ok"] is True

    second = report_outcome("failed", failure_reason="supervisor saw exit 1")
    assert second["ok"] is False
    assert second["reason"] == "already_terminal"

    row = coordinator.get_run(run["run_id"])
    assert row["state"] == "finished", "the first terminal outcome must stand"
    assert row["evidence_ref"] == "first"


def test_an_unknown_run_is_refused(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", "run-does-not-exist")
    result = report_outcome("finished")
    assert result["ok"] is False
    assert result["reason"] == "unknown_run"


def test_an_invalid_state_is_refused(db, monkeypatch):
    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    result = report_outcome("definitely-not-a-state")
    assert result["ok"] is False
    assert result["reason"] == "invalid_outcome_state"
    assert coordinator.get_run(run["run_id"])["state"] == "launched"


# ── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_report_needs_no_arguments_inside_a_dispatched_session(db, monkeypatch, capsys):
    from unified_agent.dispatch.cli import main

    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    monkeypatch.setenv("UA_DISPATCH_AGENT", run["agent"])

    assert main(["report", "finished"]) == 0
    assert coordinator.get_run(run["run_id"])["state"] == "finished"
    assert "recorded" in capsys.readouterr().out


def test_cli_report_outside_a_dispatched_session_exits_2(db, monkeypatch, capsys):
    from unified_agent.dispatch.cli import main

    monkeypatch.delenv("UA_DISPATCH_RUN_ID", raising=False)
    assert main(["report", "finished"]) == 2
    assert "not started by approved dispatch" in capsys.readouterr().err


def test_cli_report_exits_1_on_a_refusal_without_crashing(db, monkeypatch, capsys):
    from unified_agent.dispatch.cli import main

    run = _launched_run()
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run["run_id"])
    assert main(["report", "finished"]) == 0
    assert main(["report", "finished"]) == 1
    assert "already_terminal" in capsys.readouterr().err
