"""Grant lifecycle/tenant regressions from independent review; recording-only launches."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from test_dispatch_approval_request import _body, _child, _request
from test_sdk_http_writes import AGENT_TOKEN, HUMAN_TOKEN, _as_verified, _bearer
from test_sdk_http_writes import client as client

from unified_agent import storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import approvals


def _approved(client, child):
    requested = _request(client, child)
    assert requested.status_code == 202, requested.text
    approval_id = requested.json()["approval_id"]
    resolution = client.post(
        "/api/action/approval",
        headers=_bearer(HUMAN_TOKEN),
        json={"id": approval_id, "decision": "approve"},
    )
    assert resolution.status_code == 200, resolution.text
    return approval_id


@pytest.mark.parametrize("consume_enabled", [True, False])
def test_dispatch_grant_revoke_surviving_execution_authority_after_launch_failure(
    client, monkeypatch, consume_enabled
):
    child = _child()
    approval_id = _approved(client, child)
    if not consume_enabled:
        # Delta control: reproduce the predecessor behavior of not calling consumption.
        monkeypatch.setattr(coordinator, "_consume_approval_pass", lambda *a, **k: None)
    failed = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    assert failed["reason"] == "launch_failed"
    assert approvals.get_approval(approval_id)["status"] == (
        "consumed" if consume_enabled else "approved"
    )
    revoke_error = None
    try:
        with _as_verified("Desktop_Person"):
            approvals.approval_resolve(approval_id, "revoke")
    except Exception as exc:
        revoke_error = f"{type(exc).__name__}: {exc}"
    recording = RecordingLaunchBoundary()
    retry = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert (revoke_error, retry["launched"], len(recording.calls)) == (None, False, 0), {
        "revoke_error": revoke_error,
        "retry": retry,
        "recorded_calls": recording.calls,
    }


@pytest.mark.parametrize("foreign_org", [None, "org_foreign"])
def test_dispatch_grant_claim_does_not_consume_another_tenants_approval(
    client, monkeypatch, foreign_org
):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    approval_id = _approved(client, child)
    # Model an older/mixed-tenant row arriving after the producer's preflight.
    with storage.connect() as conn:
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,reason,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,status,reason,resolved_by,created_at,updated_at,resolved_at,?
            FROM approvals WHERE id=?""",
            (foreign_org, approval_id),
        )
        foreign_id = cur.lastrowid
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    with storage.connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id,status FROM approvals WHERE id IN (?,?)", (approval_id, foreign_id)
            )
        }
    assert statuses[foreign_id] == "approved", {"statuses": statuses, "claim": result}


def test_dispatch_grant_consumed_execution_pass_still_expires_after_failed_launch(client):
    child = _child()
    approval_id = _approved(client, child)
    failed = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    assert failed["reason"] == "launch_failed"
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET resolved_at=? WHERE id=?", (old, approval_id))
    recording = RecordingLaunchBoundary()
    retry = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not retry["launched"]
    assert recording.calls == []


def test_dispatch_grant_creation_grant_cannot_authorize_existing_child_execution(client):
    child = _child()
    receipt = approvals.check_approval_gate(
        "delegate",
        _body(child)["payload"],
        requested_by="Desktop_Agent2",
        force=True,
        request_only=True,
    )
    resolution = client.post(
        "/api/action/approval",
        headers=_bearer(HUMAN_TOKEN),
        json={"id": receipt["approval_id"], "decision": "approve"},
    )
    assert resolution.status_code == 200, resolution.text
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"] and recording.calls == []
    assert approvals.get_approval(receipt["approval_id"])["status"] == "approved"


def test_dispatch_grant_concurrent_identical_requests_share_pending_row(client):
    child = _child()
    barrier = threading.Barrier(4)

    def request_once(_):
        barrier.wait(timeout=10)
        response = _request(client, child)
        assert response.status_code == 202, response.text
        return response.json()["approval_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(request_once, range(4)))
    assert len(set(ids)) == 1


def test_dispatch_grant_concurrent_claims_spend_one_grant_and_launch_once(client):
    child = _child()
    approval_id = _approved(client, child)
    barrier = threading.Barrier(2)
    recording = RecordingLaunchBoundary()

    def claim_once(_):
        barrier.wait(timeout=10)
        return coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_once, range(2)))
    assert sum(bool(result["launched"]) for result in results) == 1, results
    assert len(recording.calls) == 1
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_dispatch_grant_http_request_identity_cannot_be_spoofed_and_human_cannot_self_approve(
    client,
):
    child = _child()
    forged = {**_body(child), "requested_by": "Desktop_Person"}
    assert (
        client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=forged).status_code
        == 400
    )
    human_request = client.post(
        "/api/approvals/request", headers=_bearer(HUMAN_TOKEN), json=_body(child)
    )
    assert human_request.status_code == 202, human_request.text
    approval_id = human_request.json()["approval_id"]
    assert approvals.get_approval(approval_id)["requested_by"] == "Desktop_Person"
    decision = client.post(
        "/api/action/approval",
        headers=_bearer(HUMAN_TOKEN),
        json={"id": approval_id, "decision": "approve"},
    )
    assert decision.status_code == 400, decision.text
    assert approvals.get_approval(approval_id)["status"] == "pending"


def test_dispatch_grant_revocation_between_preview_and_locked_claim_prevents_launch(
    client, monkeypatch
):
    child = _child()
    approval_id = _approved(client, child)
    original = coordinator._quota_gate

    def revoke_then_check(*args, **kwargs):
        with _as_verified("Desktop_Person"):
            approvals.approval_resolve(approval_id, "revoke")
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_quota_gate", revoke_then_check)
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"] and recording.calls == []
    assert approvals.get_approval(approval_id)["status"] == "revoked"


def test_consumed_completed_grant_cannot_revoke_or_authorize_another_run(client):
    child = _child()
    approval_id = _approved(client, child)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    with storage.connect() as conn:
        conn.execute(
            "UPDATE dispatch_runs SET state='finished' WHERE run_id=?", (result["run_id"],)
        )
        conn.execute(
            "UPDATE delegations SET status='pending' WHERE id=?", (child["delegation_id"],)
        )
    with _as_verified("Desktop_Person"), pytest.raises(approvals.ToolError):
        approvals.approval_resolve(approval_id, "revoke")
    recording = RecordingLaunchBoundary()
    assert not coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)[
        "launched"
    ]
    assert recording.calls == []


@pytest.mark.parametrize("alteration", ["grant", "binding", "event_superseded"])
def test_recovery_requires_original_grant_and_run_proof(client, alteration):
    child = _child()
    approval_id = _approved(client, child)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    assert result["reason"] == "launch_failed"
    with storage.connect() as conn:
        if alteration == "grant":
            changed = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
            conn.execute("UPDATE approvals SET resolved_at=? WHERE id=?", (changed, approval_id))
        elif alteration == "binding":
            conn.execute(
                "UPDATE dispatch_runs SET delegation_id=NULL WHERE run_id=?", (result["run_id"],)
            )
        else:
            from unified_agent import events

            # An unbound newer consumption supersedes the original proof;
            # preserve the append-only ledger rather than deleting evidence.
            events.record_event(
                "agent.approval.consumed",
                payload={"approval_id": approval_id, "tool": "dispatch_claim"},
            )
    recording = RecordingLaunchBoundary()
    assert not coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)[
        "launched"
    ]
    with _as_verified("Desktop_Person"), pytest.raises(approvals.ToolError):
        approvals.approval_resolve(approval_id, "revoke")
    assert recording.calls == []


def test_retry_binding_moves_to_new_run_and_remains_revocable(client):
    child = _child()
    approval_id = _approved(client, child)
    first = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    second = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    assert first["run_id"] != second["run_id"]
    with _as_verified("Desktop_Person"):
        approvals.approval_resolve(approval_id, "revoke")
    recording = RecordingLaunchBoundary()
    assert not coordinator.resume_run(second["run_id"], actor="Desktop_Person", launch=recording)[
        "ok"
    ]
    assert recording.calls == []


def test_resume_consumes_only_its_exact_grant_and_recovers_failure(client):
    child = _child()
    approval_id = _approved(client, child)
    first = coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    resumed = coordinator.resume_run(
        first["run_id"],
        actor="Desktop_Person",
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    assert resumed["reason"] == "launch_failed"
    with _as_verified("Desktop_Person"):
        approvals.approval_resolve(approval_id, "revoke")
    recording = RecordingLaunchBoundary()
    assert not coordinator.resume_run(first["run_id"], actor="Desktop_Person", launch=recording)[
        "ok"
    ]
    assert recording.calls == []


def test_agent_cannot_revoke_consumed_recovery_as_a_human(client):
    child = _child()
    approval_id = _approved(client, child)
    coordinator.claim_and_dispatch(
        task_id=child["task_id"],
        launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
    )
    with _as_verified("Desktop_Agent2"), pytest.raises(approvals.ToolError):
        approvals.approval_resolve(approval_id, "revoke")
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_legacy_consumed_grant_without_run_binding_is_closed(client):
    child = _child()
    approval_id = _approved(client, child)
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET status='consumed' WHERE id=?", (approval_id,))
    recording = RecordingLaunchBoundary()
    assert not coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)[
        "launched"
    ]
    with _as_verified("Desktop_Person"), pytest.raises(approvals.ToolError):
        approvals.approval_resolve(approval_id, "revoke")
    assert recording.calls == []


def test_claim_consumes_selected_local_grant_only(client):
    child = _child()
    original_id = _approved(client, child)
    with storage.connect() as conn:
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,reason,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,status,reason,resolved_by,created_at,updated_at,resolved_at,org_id
            FROM approvals WHERE id=?""",
            (original_id,),
        )
        latest_id = cur.lastrowid
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    assert result["launched"]
    assert approvals.get_approval(original_id)["status"] == "approved"
    assert approvals.get_approval(latest_id)["status"] == "consumed"


def test_terminal_bookkeeping_preserves_consumption_proof(client):
    from unified_agent.dispatch import scheduler

    child = _child()
    approval_id = _approved(client, child)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    generation = approvals.get_approval(approval_id)["updated_at"]
    assert scheduler.record_run_outcome(result["run_id"], "finished")["ok"]
    assert approvals.get_approval(approval_id)["updated_at"] == generation
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_terminal_bookkeeping_does_not_spend_newer_grant(client):
    from unified_agent.dispatch import scheduler

    child = _child()
    approval_id = _approved(client, child)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    newer_time = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    with storage.connect() as conn:
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,'approved',resolved_by,?,?,?,org_id
            FROM approvals WHERE id=?""",
            (newer_time, newer_time, newer_time, approval_id),
        )
        newer_id = cur.lastrowid
    assert scheduler.record_run_outcome(result["run_id"], "finished")["ok"]
    assert approvals.get_approval(newer_id)["status"] == "approved"


def test_legacy_terminal_consumption_stays_closed(client, monkeypatch):
    from unified_agent.dispatch import scheduler

    child = _child()
    approval_id = _approved(client, child)
    # This compatibility fixture proves grant resolution preceded the run,
    # rather than relying on equal timestamps rounded to the same second.
    with storage.connect() as conn:
        conn.execute(
            "UPDATE approvals SET resolved_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), approval_id),
        )
    consume = coordinator._consume_approval_pass
    monkeypatch.setattr(coordinator, "_consume_approval_pass", lambda *a, **k: None)
    result = coordinator.claim_and_dispatch(
        task_id=child["task_id"], launch=RecordingLaunchBoundary()
    )
    monkeypatch.setattr(coordinator, "_consume_approval_pass", consume)
    assert scheduler.record_run_outcome(result["run_id"], "finished")["ok"]
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    with _as_verified("Desktop_Person"), pytest.raises(approvals.ToolError):
        approvals.approval_resolve(approval_id, "revoke")


@pytest.mark.parametrize("foreign_org", [None, "org_foreign"])
@pytest.mark.parametrize("prior_decision", ["revoke", "deny"])
@pytest.mark.parametrize("path", ["claim", "resume"])
def test_dispatch_grant_ambiguous_tenant_grants_cannot_hide_refusal_behind_old_plan_marker(
    client, monkeypatch, foreign_org, prior_decision, path
):
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    original_id = _approved(client, child)
    tasks.task_update(child["task_id"], append_note="[plan: approved @ 2026-10-01T00:00:00Z]")
    with _as_verified("Desktop_Person"):
        approvals.approval_resolve(original_id, "revoke")
    # The old marker is initially subordinate to the explicit revoked grant.
    assert not coordinator.evaluate_ready(task_id=child["task_id"])["ready"]
    with storage.connect() as conn:
        conn.execute("UPDATE approvals SET org_id=? WHERE id=?", (foreign_org, original_id))
        # Keep the newest grant local and explicitly refused, as in the predecessor.
        cur = conn.execute(
            """INSERT INTO approvals
            (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
            SELECT tool,payload,requested_by,?,resolved_by,created_at,updated_at,resolved_at,'org_local'
            FROM approvals WHERE id=?""",
            ("revoked" if prior_decision == "revoke" else "denied", original_id),
        )
        local_id = cur.lastrowid
    preview = coordinator.evaluate_ready(task_id=child["task_id"])
    assert not preview["ready"]
    assert preview["reason"] == "invalid_approval_authority"
    recording = RecordingLaunchBoundary()
    if path == "claim":
        result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    else:
        # Imported/retryable run state, with no process or effect behind it.
        with storage.connect() as conn:
            conn.execute(
                """INSERT INTO dispatch_runs
                (run_id,task_id,delegation_id,agent_label,state,actor,created_at,updated_at)
                VALUES ('synthetic-marker-retry',?,?, 'Desktop_Agent1','retryable','Desktop_Agent2',?,?)""",
                (child["task_id"], child["delegation_id"], storage.now(), storage.now()),
            )
        result = coordinator.resume_run(
            "synthetic-marker-retry", actor="Desktop_Person", launch=recording
        )
    assert result["reason"] == "invalid_approval_authority"
    assert not recording.calls, {
        "result": result,
        "foreign_id": original_id,
        "local_refusal_id": local_id,
        "local_refusal": approvals.get_approval(local_id)["status"],
        "calls": recording.calls,
    }


@pytest.mark.parametrize("enterprise", [False, True])
@pytest.mark.parametrize("path", ["claim", "resume"])
def test_valid_absent_grant_retains_legacy_marker_compatibility(
    client, monkeypatch, enterprise, path
):
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise" if enterprise else "core")
    if enterprise:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    with _as_verified("Desktop_Person"):
        tasks.task_update(child["task_id"], append_note="[plan: approved @ 2026-10-01T00:00:00Z]")
    assert coordinator.evaluate_ready(task_id=child["task_id"])["ready"]
    recording = RecordingLaunchBoundary()
    if path == "claim":
        result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
        assert result["launched"]
    else:
        first = coordinator.claim_and_dispatch(
            task_id=child["task_id"],
            launch=RecordingLaunchBoundary(fail_with="synthetic_spawn_unavailable"),
        )
        result = coordinator.resume_run(first["run_id"], actor="Desktop_Person", launch=recording)
        assert result["ok"]
    assert len(recording.calls) == 1
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM approvals WHERE tool='dispatch_claim'").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("resource", ["task", "delegation"])
def test_invalid_resource_org_cannot_use_absent_grant_marker_fallback(
    client, monkeypatch, resource
):
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    tasks.task_update(child["task_id"], append_note="[plan: approved @ 2026-10-01T00:00:00Z]")
    with storage.connect() as conn:
        if resource == "task":
            conn.execute("UPDATE tasks SET org_id='org_foreign' WHERE id=?", (child["task_id"],))
        else:
            conn.execute(
                "UPDATE delegations SET org_id='org_foreign' WHERE id=?", (child["delegation_id"],)
            )
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"] and recording.calls == []
    assert result["reason"] == "invalid_approval_authority"


@pytest.mark.parametrize("foreign_org", [None, "org_foreign"])
def test_ambiguity_between_preview_and_claim_cannot_enable_old_marker(
    client, monkeypatch, foreign_org
):
    from unified_agent.tools import tasks

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_local")
    child = _child()
    approval_id = _approved(client, child)
    tasks.task_update(child["task_id"], append_note="[plan: approved @ 2026-10-01T00:00:00Z]")
    quota = coordinator._quota_gate

    def introduce_ambiguity(*args, **kwargs):
        with storage.connect() as conn:
            conn.execute(
                """INSERT INTO approvals
                (tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,org_id)
                SELECT tool,payload,requested_by,status,resolved_by,created_at,updated_at,resolved_at,?
                FROM approvals WHERE id=?""",
                (foreign_org, approval_id),
            )
        return quota(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_quota_gate", introduce_ambiguity)
    recording = RecordingLaunchBoundary()
    result = coordinator.claim_and_dispatch(task_id=child["task_id"], launch=recording)
    assert not result["launched"] and recording.calls == []
    assert result["reason"] == "invalid_approval_authority"
    assert approvals.get_approval(approval_id)["status"] == "approved"
