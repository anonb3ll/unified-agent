from datetime import UTC, datetime, timedelta

import pytest
from test_dispatch_approval_request import _child, _request
from test_dispatch_grant_recovery import _approved
from test_sdk_http_writes import client as client

from unified_agent import storage
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import approvals


def _marker(child, kind="approved"):
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET notes=notes || ? WHERE id=?",
            (f"\n[plan: {kind} @ 2026-10-01T00:00:00Z]", child["task_id"]),
        )


@pytest.mark.parametrize("precise_run", [False, True])
def test_terminal_outcome_must_not_spend_same_second_new_grant(client, precise_run):
    child = _child()
    original = _approved(client, child)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    rounded = datetime.now(UTC).isoformat(timespec="seconds")
    run_time = (
        datetime.fromisoformat(rounded).replace(microsecond=100000).isoformat()
        if precise_run
        else rounded
    )
    with storage.connect() as conn:
        conn.execute(
            "UPDATE dispatch_runs SET created_at=? WHERE run_id=?", (run_time, result["run_id"])
        )
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,'approved',resolved_by,?,?,?,org_id
            FROM approvals WHERE id=?""",
            (rounded, rounded, rounded, original),
        )
        newer = cur.lastrowid
    assert scheduler.record_run_outcome(result["run_id"], "finished")["ok"]
    assert approvals.get_approval(newer)["status"] == "approved"


@pytest.mark.parametrize("kind", ["denied", "expired"])
def test_blocking_plan_marker_must_refuse_new_request_without_row(client, kind):
    child = _child()
    _marker(child, kind)
    with storage.connect() as conn:
        before = conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
    response = _request(client, child)
    assert response.status_code == 400, response.text
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == before


@pytest.mark.parametrize("status", ["pending", "consumed", "expired"])
def test_explicit_unusable_grant_cannot_be_ready_via_old_marker(client, status):
    child = _child()
    grant = _approved(client, child)
    _marker(child)
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (status, grant))
    verdict = coordinator.evaluate_ready(task_id=child["task_id"])
    assert not verdict["ready"], verdict
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"]
    assert not recording.calls


@pytest.mark.parametrize("latest", ["revoked", "expired", "consumed"])
def test_latest_revoked_duplicate_requires_fresh_pending_decision(client, latest):
    child = _child()
    old = _approved(client, child)
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,?,resolved_by,created_at,updated_at,resolved_at,org_id
            FROM approvals WHERE id=?""",
            (latest, old),
        )
    response = _request(client, child)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["approval_id"] > old
    assert approvals.get_approval(old)["status"] == "approved"
    assert not coordinator.evaluate_ready(task_id=child["task_id"])["ready"]


def test_expired_approved_grant_cannot_use_old_marker(client):
    child = _child()
    grant = _approved(client, child)
    _marker(child)
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET resolved_at=? WHERE id=?", (past, grant))
    assert not coordinator.evaluate_ready(task_id=child["task_id"])["ready"]


@pytest.mark.parametrize("latest", ["pending", "approved", "denied"])
def test_request_reuses_latest_actionable_duplicate_only(client, latest):
    child = _child()
    old = _approved(client, child)
    with storage.connect() as conn:
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,?,resolved_by,created_at,updated_at,resolved_at,org_id
            FROM approvals WHERE id=?""",
            (latest, old),
        )
        latest_id = cur.lastrowid
        before = conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
    response = _request(client, child)
    assert response.status_code == (202 if latest == "pending" else 200), response.text
    assert response.json()["status"] == latest
    assert response.json()["approval_id"] == latest_id
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == before
    assert approvals.get_approval(old)["status"] == "approved"


@pytest.mark.parametrize("change", ["plan_denial", "foreign_grant", "foreign_task"])
def test_locked_request_rechecks_changed_authority_before_insert(client, monkeypatch, change):
    child = _child()
    grant = _approved(client, child)
    original = approvals._request_dispatch_approval

    def changed(payload, requested_by):
        with storage.connect() as conn:
            if change == "plan_denial":
                conn.execute(
                    "UPDATE tasks SET notes=notes || ? WHERE id=?",
                    ("\n[plan: denied @ 2026-10-01T00:00:00Z]", child["task_id"]),
                )
            elif change == "foreign_grant":
                conn.execute("UPDATE approvals SET org_id='org_other' WHERE id=?", (grant,))
            else:
                conn.execute("UPDATE tasks SET org_id='org_other' WHERE id=?", (child["task_id"],))
        return original(payload, requested_by)

    monkeypatch.setattr(approvals, "_request_dispatch_approval", changed)
    response = _request(client, child)
    assert response.status_code == 400, response.text
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["pending", "consumed"])
def test_old_plan_marker_cannot_launch_with_unusable_grant(client, status):
    child = _child()
    grant = _approved(client, child)
    _marker(child)
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (status, grant))
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"]
    assert not recording.calls
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM dispatch_runs").fetchone()[0] == 0
