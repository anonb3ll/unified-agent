"""Unit tests for scripts/dispatch_live_soak_567.py (UA #567 live soak)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from unified_agent import storage
from unified_agent.tools import tasks

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dispatch_live_soak_567.py"


def _load_soak_module():
    """Import ``dispatch_live_soak_567.py`` as a module for isolated soak tests."""
    spec = importlib.util.spec_from_file_location("dispatch_live_soak_567", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _task_notes(task_id: int) -> str:
    """Return the raw notes column for a task row in the test database."""
    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return (row["notes"] or "") if row is not None else ""


@pytest.fixture
def soak_db(tmp_path, monkeypatch):
    """Load the soak script against an isolated UA_HOME and synthetic #567 parent."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    storage.init_db()
    parent = tasks.task_add(
        title="UA #567 parent",
        notes="soak test parent",
        priority="low",
    )
    soak = _load_soak_module()
    monkeypatch.setattr(soak, "PARENT", parent["id"])
    return soak


def test_scheduler_gate_blocks_when_disabled(soak_db, monkeypatch):
    """``_scheduler_gate_ok`` rejects ticks when the scheduler kill switch is off."""
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    ok, reason = soak_db._scheduler_gate_ok(force=False)
    assert not ok
    assert reason == "scheduler_disabled"


def test_scheduler_gate_allows_force_when_disabled(soak_db, monkeypatch):
    """``--force`` bypasses the scheduler kill switch for operator one-shots."""
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    ok, reason = soak_db._scheduler_gate_ok(force=True)
    assert ok
    assert reason is None


def test_setup_creates_children_without_self_approval(soak_db, capsys):
    """``setup`` mints soak children but never writes the approved marker itself."""
    code = soak_db.cmd_setup()
    out = json.loads(capsys.readouterr().out)
    assert code == 7
    assert out["ok"] is True
    assert out["phase"] == "setup"
    assert out["approved_ready"] is False
    assert "Human steward" in out["next_step"]

    assert "[plan: approved @" not in _task_notes(out["ok_task_id"])
    assert "[plan: denied @" in _task_notes(out["denied_task_id"])


def test_tick_blocks_until_human_approval(soak_db, capsys):
    """``tick`` returns ``awaiting_human_approval`` until a steward approves the child."""
    soak_db.cmd_setup()
    capsys.readouterr()
    code = soak_db.cmd_tick(force=False)
    out = json.loads(capsys.readouterr().out)
    assert code == 7
    assert out["reason"] == "awaiting_human_approval"


def test_tick_succeeds_after_human_approval(soak_db, capsys):
    """Approved soak child dispatches once; a second ``tick`` is a no-op."""
    soak_db.cmd_setup()
    setup = json.loads(capsys.readouterr().out)
    tasks.task_update(
        setup["ok_task_id"],
        append_note="[plan: approved @ 2026-08-24T00:00:00Z]",
    )
    code = soak_db.cmd_tick(force=False)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True
    assert out["first_launched"] is True
    assert out["second_launched"] is False
    assert out["denied_run_count"] == 0
    assert out["obs_state"] == "finished"

    # Second tick must not mint another approved or denied run (command-level no-op).
    code2 = soak_db.cmd_tick(force=False)
    out2 = json.loads(capsys.readouterr().out)
    assert code2 == 0
    assert out2["reason"] == "soak_already_complete"
    assert out2["run_id"] == out["run_id"]
    assert out2.get("first_launched") is not True
    assert out2.get("second_launched") is not True


def test_tick_finalizes_runs_on_acceptance_failure(soak_db, capsys, monkeypatch):
    """Acceptance failure finalizes launched runs and cancels both soak children."""
    from unified_agent.dispatch import coordinator

    soak_db.cmd_setup()
    setup = json.loads(capsys.readouterr().out)
    tasks.task_update(
        setup["ok_task_id"],
        append_note="[plan: approved @ 2026-08-24T00:00:00Z]",
    )
    real_list_runs = coordinator.list_runs

    def list_runs_with_denied_history(**kwargs):
        """Simulate a denied sibling with historical runs to trigger acceptance failure."""
        runs = real_list_runs(**kwargs)
        if kwargs.get("task_id") == setup["denied_task_id"]:
            return [{"run_id": "denied-historical", "state": "finished"}]
        return runs

    monkeypatch.setattr(coordinator, "list_runs", list_runs_with_denied_history)

    code = soak_db.cmd_tick(force=False)
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["reason"] == "acceptance_failed"
    assert out["first_launched"] is True
    assert out["finalized_runs"]
    assert out.get("cleanup") == "soak_children_cancelled"
    run_id = out["run_id"]
    obs = coordinator.get_run_observability(run_id)
    assert obs is not None
    assert obs["state"] == "failed"

    ok_child = soak_db._find_child(snippet=soak_db.OK_TITLE_SNIPPET)
    denied_child = soak_db._find_child(snippet=soak_db.DENIED_TITLE_SNIPPET)
    assert ok_child is not None and ok_child["status"] == "cancelled"
    assert denied_child is not None and denied_child["status"] == "cancelled"


def test_setup_aborts_before_mutation_when_scheduler_disabled(soak_db, monkeypatch, capsys):
    """``setup`` returns ``scheduler_disabled`` without creating approved or denied children."""
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    code = soak_db.cmd_setup()
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["reason"] == "scheduler_disabled"
    assert soak_db._find_child(snippet=soak_db.OK_TITLE_SNIPPET) is None
    assert soak_db._find_child(snippet=soak_db.DENIED_TITLE_SNIPPET) is None


def test_tick_ready_mismatch_preserves_soak_children_for_retry(soak_db, capsys):
    """Foreign ready work aborts tick but leaves soak children intact for operator retry."""
    from unified_agent.tools import delegate

    soak_db.cmd_setup()
    setup = json.loads(capsys.readouterr().out)
    tasks.task_update(
        setup["ok_task_id"],
        append_note="[plan: approved @ 2026-08-24T00:00:00Z]",
    )

    foreign = delegate.delegate(
        task_spec=(
            "### Objective\nForeign ready sibling for soak ready_mismatch test\n"
            "### Scope\n- tests/test_dispatch_live_soak.py\n"
            "### Definition of Done\n- appears in ready set alongside soak child\n"
            "### TDD Constraints\n- stub only\n"
            "### Boundaries\n- must not be claimed by soak tick\n"
        ),
        from_agent="Desktop_Agent4",
        to_agent="Desktop_Agent1",
        callback="none",
        priority="low",
    )
    tasks.task_update(
        foreign["task_id"],
        append_note="[plan: approved @ 2026-08-24T00:00:00Z]",
    )

    code = soak_db.cmd_tick(force=False)
    out = json.loads(capsys.readouterr().out)
    assert code == 3
    assert out["reason"] == "ready_mismatch"
    assert setup["ok_task_id"] in out["ready_task_ids"]
    assert foreign["task_id"] in out["ready_task_ids"]

    ok_child = soak_db._find_child(snippet=soak_db.OK_TITLE_SNIPPET)
    denied_child = soak_db._find_child(snippet=soak_db.DENIED_TITLE_SNIPPET)
    assert ok_child is not None
    assert denied_child is not None
    assert ok_child["status"] != "cancelled"
    assert denied_child["status"] != "cancelled"
