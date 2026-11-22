"""End-to-end delivery receipt and separate-process auth proof contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import storage
from unified_agent.audit import evidence_report
from unified_agent.dispatch import coordinator, receipt, scheduler
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import delegate, dispatch_report, tasks


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    storage.init_db()
    return tmp_path


def _spec() -> str:
    return "### Objective\nreceipt rehearsal\n### Scope\n- dispatch\n"


def _delegated() -> dict:
    result = delegate.delegate(
        task_spec=_spec(), from_agent="Desktop_Agent5", to_agent="Desktop_Agent1", callback="none"
    )
    tasks.task_update(result["task_id"], append_note="[plan: approved @ 2026-09-17T00:00:00Z]")
    return result


def test_failed_reclaim_and_success_run_is_auditable(db, monkeypatch):
    packed = _delegated()
    launch = RecordingLaunchBoundary()
    claimed = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = claimed["run_id"]

    conn = storage.connect()
    try:
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE dispatch_runs SET lease_expires_at=? WHERE run_id=?", (expired, run_id)
        )
        conn.commit()
    finally:
        conn.close()
    reclaimed = coordinator.reclaim_expired_leases()
    assert reclaimed["reclaimed_count"] == 1

    resumed = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=launch)
    assert resumed["ok"] is True
    scheduler.record_run_outcome(run_id, "finished", evidence_ref="receipt-proof")

    item = receipt.receipts_for_task(packed["task_id"])[0]
    assert item["task_id"] == packed["task_id"]
    assert item["delegation_id"] == packed["delegation_id"]
    assert item["executor_identity"] == "Desktop_Agent1"
    assert item["retry_count"] == 1
    assert sum(1 for transition in item["transitions"] if transition["state"] == "resumed") == 1
    assert item["event_cursor"]
    assert item["policy_outcome"] == "approved"
    assert item["approval_outcome"] == "approved"
    assert item["evidence_ref"] == "receipt-proof"
    assert item["final_task_state"] == "done"
    assert item["terminal"] is True

    report = evidence_report.build_evidence_report(task_id=packed["task_id"])
    assert report["records"][0]["delivery_receipts"][0]["run_id"] == run_id

    monkeypatch.setattr(
        "unified_agent.dispatch.receipt._executor_auth_proof",
        lambda: {"status": "LIVE", "authenticated": True, "probe_process": "separate"},
    )
    auth_receipt = dispatch_report.dispatch_delivery_receipt(run_id=run_id)
    assert auth_receipt["executor_auth"] == {
        "status": "LIVE",
        "authenticated": True,
        "probe_process": "separate",
    }
