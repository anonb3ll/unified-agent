"""Real HTTP request/resolution and native dispatch persistence; launch is isolated."""

from datetime import UTC, datetime, timedelta

import pytest
from test_dispatch_coordinator import _spec
from test_sdk_http_writes import AGENT_TOKEN, HUMAN_TOKEN, _as_verified, _bearer
from test_sdk_http_writes import client as client

from unified_agent import config, storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import approvals, delegate


def _child():
    return delegate.delegate(
        task_spec=_spec("Existing child execution approval"),
        from_agent="Desktop_Agent2",
        to_agent="Desktop_Agent1",
        callback="none",
    )


def _body(child):
    return {
        "tool": "dispatch_claim",
        "payload": coordinator.dispatch_approval_payload(
            task_id=child["task_id"], delegation_id=child["delegation_id"]
        ),
    }


def _request(client, child):
    return client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=_body(child))


def _counts():
    with storage.connect() as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "delegations", "dispatch_runs", "approvals")
        )


def test_existing_child_request_human_resolution_and_exactly_one_native_launch(client):
    child, other = _child(), _child()
    before = _counts()
    first = _request(client, child)
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["payload"] == _body(child)["payload"]
    assert result["status"] == "pending"
    assert _request(client, child).json()["approval_id"] == result["approval_id"]
    assert _counts() == (*before[:3], before[3] + 1)
    row = approvals.get_approval(result["approval_id"])
    assert row["requested_by"] == "Desktop_Agent2"
    assert row["resolved_by"] is None
    assert coordinator.evaluate_ready(task_id=child["task_id"])["reason"] == "unapproved"
    assert (
        coordinator.evaluate_ready(task_id=other["task_id"])["reason"] == "missing_approval_marker"
    )
    with storage.connect() as conn:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (child["task_id"],)).fetchone()[
            0
        ]
    assert "[plan: approved @" not in notes

    agent_decision = client.post(
        "/api/action/approval",
        headers=_bearer(AGENT_TOKEN),
        json={"id": result["approval_id"], "decision": "approve"},
    )
    assert agent_decision.status_code == 403
    human = client.post(
        "/api/action/approval",
        headers=_bearer(HUMAN_TOKEN),
        json={"id": result["approval_id"], "decision": "approve"},
    )
    assert human.status_code == 200, human.text
    granted = approvals.get_approval(result["approval_id"])
    assert granted["resolved_by"] == "Desktop_Person"
    assert granted["resolved_at"]
    again = _request(client, child)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "approved"
    assert approvals.get_approval(result["approval_id"])["status"] == "approved"
    preview = client.get(
        "/api/dispatch-stack",
        headers=_bearer(AGENT_TOKEN),
        params={"task_ids": child["task_id"]},
    )
    assert preview.status_code == 200
    assert preview.json()["next_task_id"] == child["task_id"]
    launch = RecordingLaunchBoundary()
    dispatched = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=launch)
    assert dispatched["launched"] is True
    duplicate = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=launch)
    assert duplicate["reason"] == "already_claimed"
    assert len(launch.calls) == 1
    assert approvals.get_approval(result["approval_id"])["status"] == "consumed"
    assert _request(client, child).status_code == 400
    assert _counts()[:3] == (*before[:2], before[2] + 1)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "delete"},
        {"task_id": True},
        {"task_id": 1.2},
        {"task_id": "1"},
        {"delegation_id": 0},
        {"resolved_by": "Desktop_Person"},
    ],
)
def test_dispatch_request_rejects_noncanonical_or_identity_fields(client, change):
    child = _child()
    body = _body(child)
    body["payload"].update(change)
    before = _counts()
    response = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
    assert response.status_code == 400
    assert _counts() == before


def test_existing_pair_mismatch_and_unknown_tool_refuse_without_request(client):
    child, other = _child(), _child()
    body = _body(child)
    body["payload"]["delegation_id"] = other["delegation_id"]
    before = _counts()
    assert (
        client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body).status_code
        == 400
    )
    body["tool"] = "unregistered_arbitrary_tool"
    assert (
        client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body).status_code
        == 400
    )
    assert _counts() == before


@pytest.mark.parametrize("table", ["tasks", "delegations"])
@pytest.mark.parametrize("foreign_org", [None, "org_foreign"])
def test_existing_child_resource_org_must_be_proven(client, monkeypatch, table, foreign_org):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    child = _child()
    row_id = child["task_id"] if table == "tasks" else child["delegation_id"]
    with storage.connect() as conn:
        conn.execute(f"UPDATE {table} SET org_id=? WHERE id=?", (foreign_org, row_id))
    before = _counts()
    assert _request(client, child).status_code == 400
    assert _counts() == before


def test_foreign_matching_approval_cannot_be_reused(client, monkeypatch):
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    first = _request(client, child)
    assert first.status_code == 202, first.text
    with storage.connect() as conn:
        conn.execute(
            "UPDATE approvals SET org_id='org_foreign' WHERE id=?", (first.json()["approval_id"],)
        )
    assert _request(client, child).status_code == 400


@pytest.mark.parametrize("decision", ["deny", "revoke", "expire"])
def test_existing_dispatch_request_preserves_denial_revocation_and_ttl(client, decision):
    child = _child()
    first = _request(client, child)
    assert first.status_code == 202, first.text
    approval_id = first.json()["approval_id"]
    resolution = "deny" if decision == "deny" else "approve"
    assert (
        client.post(
            "/api/action/approval",
            headers=_bearer(HUMAN_TOKEN),
            json={"id": approval_id, "decision": resolution},
        ).status_code
        == 200
    )
    if decision == "revoke":
        with _as_verified("Desktop_Person"):
            approvals.approval_resolve(approval_id, "revoke")
    if decision == "expire":
        old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        with storage.connect() as conn:
            conn.execute("UPDATE approvals SET resolved_at=? WHERE id=?", (old, approval_id))
    assert not coordinator.evaluate_ready(task_id=child["task_id"])["ready"]
    retry = _request(client, child)
    assert retry.status_code == (200 if decision == "deny" else 202), retry.text
    assert retry.json()["status"] == ("denied" if decision == "deny" else "pending")
    assert _counts()[2] == 0


def test_bound_workos_org_mismatch_refuses_before_queue(client, monkeypatch):
    from unified_agent.webui.dispatch_approval import request_existing_dispatch_approval

    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    tokens = config.bind_workos_request_auth(
        identity="Desktop_Agent2",
        subject="fixture-subject",
        organization_id="org_foreign",
        session_id="fixture-session",
        expires_at=datetime.now(UTC).timestamp() + 300,
    )
    before = _counts()
    try:
        with pytest.raises(ValueError, match="organization"):
            request_existing_dispatch_approval(
                _body(child)["payload"], requested_by="Desktop_Agent2"
            )
    finally:
        config.reset_workos_request_auth(*tokens)
    assert _counts() == before


def test_consumption_failure_rolls_back_claim_and_leaves_original_grant_usable(client, monkeypatch):
    child = _child()
    first = _request(client, child)
    assert first.status_code == 202, first.text
    approval_id = first.json()["approval_id"]
    assert (
        client.post(
            "/api/action/approval",
            headers=_bearer(HUMAN_TOKEN),
            json={"id": approval_id, "decision": "approve"},
        ).status_code
        == 200
    )
    consume = coordinator._consume_approval_pass

    def fail_consumption(*_args, **_kwargs):
        raise RuntimeError("isolated consumption fault")

    monkeypatch.setattr(coordinator, "_consume_approval_pass", fail_consumption)
    launch = RecordingLaunchBoundary()
    with pytest.raises(RuntimeError, match="consumption fault"):
        coordinator.claim_and_dispatch(task_id=child["task_id"], launch=launch)
    assert launch.calls == []
    assert _counts()[2] == 0
    assert approvals.get_approval(approval_id)["status"] == "approved"
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, claim_generation FROM delegations WHERE id=?",
            (child["delegation_id"],),
        ).fetchone()
    assert tuple(row) == ("pending", 0, 0)
    monkeypatch.setattr(coordinator, "_consume_approval_pass", consume)
    assert coordinator.claim_and_dispatch(task_id=child["task_id"], launch=launch)["launched"]
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_request_preserves_agent_access_read_only_refusal(client, monkeypatch):
    child = _child()
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "1")
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    before = _counts()
    response = _request(client, child)
    assert response.status_code == 403, response.text
    assert _counts() == before
