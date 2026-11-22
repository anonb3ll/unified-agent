"""Task #1251 (Epic C, C2): quota-aware dispatch gate.

The dispatch path consults the ``model_quota_status`` projection
(``quota_observe.build_quota_status``) before claiming a delegation: a lane
whose observed availability is ``unavailable`` refuses the claim — no run is
minted, no attempt is burned, the delegation stays ``pending``, and the
refusal is recorded on the transition ledger with a reason. ``available``
*and* ``unknown`` lanes dispatch exactly as before: absent data is not a
block — only positive exhaustion evidence gates. The scheduler tick builds
the projection once and threads it through every claim in the batch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import events, storage
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.dispatch.report import report_outcome
from unified_agent.tools import delegate, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
    storage.init_db()
    return tmp_path


def _spec(title: str = "Quota gate fixture") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/dispatch/coordinator.py\n"
        "### Definition of Done\n- quota-exhausted lanes refuse dispatch\n"
        "### TDD Constraints\n- real persistence, stubbed launch\n"
        "### Boundaries\n- no claim/lock semantic changes\n"
    )


def _delegated(*, to_agent: str = "Desktop_Agent2") -> dict:
    result = delegate.delegate(
        task_spec=_spec(),
        from_agent="Desktop_Agent5",
        to_agent=to_agent,
        callback="none",
    )
    return {"delegation_id": result["delegation_id"], "task_id": result["task_id"]}


def _approve(task_id: int, *, ts: str = "2026-09-22T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")


def _quota_status(**lane_availability: str) -> dict:
    quotas = [
        {
            "lane": lane,
            "provider": lane,
            "label": lane,
            "availability": availability,
            "source": "test",
            "observed_at": "2026-09-22T00:00:00Z" if availability == "unavailable" else None,
            "evidence": f"{lane} quota exhausted" if availability == "unavailable" else None,
            "failure_class": (
                "provider_quota_exhausted" if availability == "unavailable" else None
            ),
        }
        for lane, availability in lane_availability.items()
    ]
    return {"quotas": quotas, "checked_at": "2026-09-22T00:00:00Z"}


def _transitions() -> list[dict]:
    rows = events.query_events(channels=["agent.dispatch.transition"], limit=200)
    return [r["payload"] for r in rows]


def _delegation_row(delegation_id: int) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, attempts FROM delegations WHERE id=?", (delegation_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_quota_exhausted_lane_refuses_dispatch(db):
    packed = _delegated(to_agent="Desktop_Agent2")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(
        launch=launch,
        force=True,
        quota_status=_quota_status(codex="unavailable", claude="unknown"),
    )

    assert out["launched"] == []
    assert launch.calls == []
    skipped = [s for s in out["skipped"] if s["task_id"] == packed["task_id"]]
    assert skipped, out
    assert skipped[0]["reason"] == "quota_exhausted"
    assert skipped[0]["quota_lane"] == "codex"
    # The claim was refused before the atomic UPDATE: still pending, and no
    # attempt was consumed — the delegation retries when quota recovers.
    row = _delegation_row(packed["delegation_id"])
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    # The refusal is recorded on the transition ledger with its reason.
    refused = [t for t in _transitions() if t["state"] == "refused"]
    assert any(t["failure_reason"] and "quota_exhausted" in t["failure_reason"] for t in refused), (
        _transitions()
    )


def test_healthy_quota_dispatches_unchanged(db):
    packed = _delegated(to_agent="Desktop_Agent2")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(
        launch=launch,
        force=True,
        quota_status=_quota_status(codex="available"),
    )

    assert len(out["launched"]) == 1
    assert out["launched"][0]["task_id"] == packed["task_id"]
    assert launch.calls


def test_unknown_availability_does_not_block(db):
    """'unknown' means no exhaustion evidence — never treated as a block."""
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(
        launch=launch,
        force=True,
        quota_status=_quota_status(claude="unknown", codex="unavailable"),
    )

    assert len(out["launched"]) == 1
    assert out["launched"][0]["task_id"] == packed["task_id"]


def test_other_lane_exhaustion_does_not_block(db):
    """Exhaustion on lane A must not starve lane B."""
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(
        launch=launch,
        force=True,
        quota_status=_quota_status(codex="unavailable", agy="unavailable"),
    )

    assert len(out["launched"]) == 1
    assert launch.calls


def test_claim_and_dispatch_quota_gate_direct(db):
    """The gate lives in claim_and_dispatch so operator/UI dispatch is covered."""
    packed = _delegated(to_agent="Desktop_Agent2")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    result = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        delegation_id=packed["delegation_id"],
        launch=launch,
        quota_status=_quota_status(codex="unavailable"),
    )

    assert result["launched"] is False
    assert result["reason"] == "quota_exhausted"
    assert result["run_id"] is None
    assert launch.calls == []
    assert _delegation_row(packed["delegation_id"])["status"] == "pending"


def test_quota_skip_records_task_note_once(db):
    """The skip is operator-visible on the task, deduped per observation."""
    packed = _delegated(to_agent="Desktop_Agent2")
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()
    status = _quota_status(codex="unavailable")

    scheduler.scheduler_tick(launch=launch, force=True, quota_status=status)
    scheduler.scheduler_tick(launch=launch, force=True, quota_status=status)

    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (packed["task_id"],)).fetchone()
    finally:
        conn.close()
    notes = row["notes"]
    assert "quota_exhausted" in notes
    assert notes.count("quota_exhausted") == 1


def test_stale_exhaustion_observation_unblocks_lane(db, monkeypatch):
    """Recovery: exhaustion evidence older than UA_QUOTA_EXHAUSTION_TTL_S must
    not keep a lane unavailable forever — the observation ages out to
    ``unknown`` and dispatch proceeds (subscriptions reset hourly/weekly)."""
    first = _delegated(to_agent="Desktop_Agent1")
    _approve(first["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(launch=launch, force=True)
    assert [entry["task_id"] for entry in out["launched"]] == [first["task_id"]]
    run_id = out["launched"][0]["run_id"]

    # The run dies on provider quota → fresh evidence blocks the lane.
    monkeypatch.setenv("UA_DISPATCH_RUN_ID", run_id)
    monkeypatch.setenv("UA_DISPATCH_AGENT", "Desktop_Agent1")
    assert report_outcome("failed", failure_reason="HTTP 429: usage limit has been reached")["ok"]

    second = _delegated(to_agent="Desktop_Agent1")
    _approve(second["task_id"])
    blocked = scheduler.scheduler_tick(launch=launch, force=True)
    assert not any(e["task_id"] == second["task_id"] for e in blocked["launched"]), blocked

    # Age the observation past the TTL → the gate reopens without new evidence.
    stale_ts = (datetime.now(UTC) - timedelta(seconds=25 * 3600)).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET updated_at=? WHERE run_id=?", (stale_ts, run_id))
        conn.commit()
    finally:
        conn.close()

    recovered = scheduler.scheduler_tick(launch=launch, force=True)
    assert any(e["task_id"] == second["task_id"] for e in recovered["launched"]), recovered


def test_gate_open_logs_lane_and_reason(db, caplog):
    """A fail-open must be visible: one log line naming the lane + why."""
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve(packed["task_id"])

    with caplog.at_level(logging.INFO):
        out = coordinator.claim_and_dispatch(
            task_id=packed["task_id"],
            delegation_id=packed["delegation_id"],
            launch=RecordingLaunchBoundary(),
            quota_status=_quota_status(claude="unknown"),
        )

    assert out["launched"] is True
    assert any(
        "quota gate open" in record.getMessage() and "lane=claude" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_executor_launch_scripts_carry_memory_ceiling():
    """C2 contract: both Mac executor launch scripts set an address-space
    ceiling (RLIMIT_AS — the only memory limit macOS enforces), env-tunable,
    with the restart-on-breach posture documented in comments."""
    from pathlib import Path

    from export_guard import require_exported_paths

    root = Path(__file__).resolve().parents[1]
    scripts = (
        root / "deploy" / "mac-hub" / "run-executor-agy.sh",
        root / "deploy" / "mac-hub" / "run-executor-codex.sh",
    )
    require_exported_paths(root, *scripts)
    for script in scripts:
        text = script.read_text()
        assert "ulimit" in text, f"{script.name}: no ulimit"
        assert "UA_EXECUTOR_MEM_CEILING_KB" in text, f"{script.name}: ceiling not env-tunable"
        assert "KeepAlive" in text, f"{script.name}: restart posture undocumented"
        # Pin the shipped default (16 GiB in KB) so a silent change fails loudly.
        assert "16777216" in text, f"{script.name}: default ceiling changed without test update"
