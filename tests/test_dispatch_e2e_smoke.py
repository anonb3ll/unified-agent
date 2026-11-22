"""End-to-end approved-dispatch smoke + observability (task #551).

Real task / delegation / approval / event / dispatch_runs persistence and real
routing + claim. Only the worker process is faked
(``FakeWorkerProcessSpawner``). Scheduler stays opt-in (``force=True`` or
env gate); no production enablement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unified_agent import events, storage
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import RoutedLaunchBoundary
from unified_agent.dispatch.worker import (
    FakeWorkerProcessSpawner,
    complete_smoke_run_from_dir,
    evidence_path_for,
    started_path_for,
)
from unified_agent.tools import delegate, tasks

REQUIRED_OPERATOR_FIELDS = (
    "task_id",
    "agent_label",
    "run_id",
    "state",
    "last_error",
    "retry_count",
    "evidence_location",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    storage.init_db()
    return tmp_path


def _spec(title: str = "Dispatch e2e smoke") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/dispatch/\n"
        "- tests/test_dispatch_e2e_smoke.py\n"
        "### Definition of Done\n"
        "- approved delegation reaches fake worker once\n"
        "- denied/unknown refuse; duplicate ticks do not double-launch\n"
        "### TDD Constraints\n"
        "- Stub: fake bounded worker process only\n"
        "- Must be real: task/delegation/approval/event persistence and claim\n"
        "### Boundaries\n"
        "- Must not enable production dispatch or edit Red-tier config\n"
    )


def _delegated(*, to_agent: str = "Desktop_Agent1", callback: str = "memory") -> dict:
    result = delegate.delegate(
        task_spec=_spec(),
        from_agent="Desktop_Agent5",
        to_agent=to_agent,
        callback=callback,
    )
    return {
        "delegation_id": result["delegation_id"],
        "task_id": result["task_id"],
        "memory_id": result.get("memory_id"),
    }


def _approve(task_id: int, *, ts: str = "2026-08-24T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")


def _deny(task_id: int, *, ts: str = "2026-08-24T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: denied @ {ts}]")


def _transitions() -> list[dict]:
    rows = events.query_events(channels=["agent.dispatch.transition"], limit=200)
    return [r["payload"] for r in rows]


def _fake_launch(evidence_dir):
    spawner = FakeWorkerProcessSpawner(evidence_dir=evidence_dir)
    return RoutedLaunchBoundary(spawner=spawner), spawner


def test_e2e_approved_dispatch_smoke_with_callback_and_observability(db):
    evidence_dir = db / "fake-worker"
    packed = _delegated(callback="memory")
    _approve(packed["task_id"])
    launch, spawner = _fake_launch(evidence_dir)

    first = scheduler.scheduler_tick(force=True, launch=launch, batch_limit=5)
    assert first["status"] == "ok"
    assert first["forced"] is True
    assert len(first["launched"]) == 1
    run_id = first["launched"][0]["run_id"]
    assert first["launched"][0]["agent_label"] == "Desktop_Agent1"
    assert len(spawner.calls) == 1
    assert spawner.calls[0]["agent_label"] == "Desktop_Agent1"
    assert started_path_for(evidence_dir, run_id).is_file()

    run = coordinator.get_run(run_id)
    assert run is not None
    assert run["state"] == "launched"

    # Duplicate tick must not double-launch.
    second = scheduler.scheduler_tick(force=True, launch=launch, batch_limit=5)
    assert second["launched"] == []
    assert len(spawner.calls) == 1

    done = complete_smoke_run_from_dir(run_id, evidence_dir)
    assert done["ok"] is True

    finished = coordinator.get_run(run_id)
    assert finished["state"] == "finished"
    assert finished["evidence_ref"]
    assert (db / "fake-worker" / f"{run_id}.evidence.json").is_file()

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"])
    assert task["status"] == "done"
    assert task.get("evidence_ref")
    notes = task.get("notes") or ""
    assert "[dispatch: launched" in notes
    assert "[dispatch: finished" in notes
    assert f"run={run_id}" in notes
    assert "attempts=" in notes

    conn = storage.connect()
    try:
        drow = conn.execute(
            "SELECT status, callback_result, attempts FROM delegations WHERE id=?",
            (packed["delegation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert drow["status"] == "completed"
    assert drow["callback_result"]
    assert int(drow["attempts"] or 0) >= 1

    obs = coordinator.get_run_observability(run_id)
    assert obs is not None
    for field in REQUIRED_OPERATOR_FIELDS:
        assert field in obs
    assert obs["task_id"] == packed["task_id"]
    assert obs["agent_label"] == "Desktop_Agent1"
    assert obs["run_id"] == run_id
    assert obs["state"] == "finished"
    assert obs["retry_count"] >= 1
    assert obs["evidence_location"] == finished["evidence_ref"]
    assert obs["last_error"] is None
    assert any("finished" in ln for ln in obs["dispatch_notes"])

    payloads = _transitions()
    claimed = next(p for p in payloads if p.get("run_id") == run_id and p.get("state") == "claimed")
    launched = next(
        p for p in payloads if p.get("run_id") == run_id and p.get("state") == "launched"
    )
    finished_ev = next(
        p for p in payloads if p.get("run_id") == run_id and p.get("state") == "finished"
    )
    for edge in (claimed, launched, finished_ev):
        assert edge["task_id"] == packed["task_id"]
        assert edge["agent_label"] == "Desktop_Agent1"
        assert edge["run_id"] == run_id
        assert "retry_count" in edge
        assert edge["retry_count"] is not None
    assert finished_ev.get("evidence_ref")


def test_e2e_denied_approval_never_launches(db):
    evidence_dir = db / "fake-worker-denied"
    packed = _delegated()
    _deny(packed["task_id"])
    launch, spawner = _fake_launch(evidence_dir)

    out = scheduler.scheduler_tick(force=True, launch=launch)
    assert out["launched"] == []
    assert spawner.calls == []
    assert coordinator.list_runs(task_id=packed["task_id"]) == []

    direct = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        delegation_id=packed["delegation_id"],
        launch=launch,
    )
    assert direct["launched"] is False
    assert direct["reason"] == "denied"
    assert spawner.calls == []
    assert any(
        p.get("state") == "refused" and p.get("failure_reason") == "denied" for p in _transitions()
    )


def test_e2e_unknown_agent_never_launches(db, monkeypatch):
    evidence_dir = db / "fake-worker-unknown"
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_Agent5")
    # Inject an unknown roster label past delegate validation by writing the row.
    packed = _delegated(to_agent="Desktop_Agent1")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE delegations SET to_agent=? WHERE id=?",
            ("NotARealAgent_XYZ", packed["delegation_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    # Keep task notes approved so the refusal is specifically unknown_agent.
    _approve(packed["task_id"])
    launch, spawner = _fake_launch(evidence_dir)

    ready = scheduler.list_ready_candidates(limit=20)
    assert packed["task_id"] not in {r["task_id"] for r in ready}

    out = scheduler.scheduler_tick(force=True, launch=launch)
    assert out["launched"] == []
    assert spawner.calls == []

    direct = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        delegation_id=packed["delegation_id"],
        launch=launch,
    )
    assert direct["launched"] is False
    assert direct["reason"] == "unknown_agent"
    assert spawner.calls == []


def test_e2e_duplicate_ticks_do_not_double_launch(db):
    evidence_dir = db / "fake-worker-dup"
    packed = _delegated()
    _approve(packed["task_id"])
    launch, spawner = _fake_launch(evidence_dir)

    a = scheduler.scheduler_tick(force=True, launch=launch)
    b = scheduler.scheduler_tick(force=True, launch=launch)
    c = scheduler.scheduler_tick(force=True, launch=launch)

    assert len(a["launched"]) == 1
    assert b["launched"] == []
    assert c["launched"] == []
    assert len(spawner.calls) == 1
    assert len(coordinator.list_runs(task_id=packed["task_id"])) == 1


def test_fake_worker_resolves_relative_evidence_dir(db, monkeypatch):
    """Relative evidence_dir must not double-prefix under subprocess cwd."""
    monkeypatch.chdir(db)
    packed = _delegated()
    _approve(packed["task_id"])
    spawner = FakeWorkerProcessSpawner(evidence_dir=Path("evidence"))
    launch = RoutedLaunchBoundary(spawner=spawner)

    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True
    run_id = out["run_id"]
    assert started_path_for(spawner.evidence_dir, run_id).is_file()
    assert evidence_path_for(spawner.evidence_dir, run_id).is_file()
