"""Unit tests for self-organization toggle, scope-as-membership, and proposals (task #1431)."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from org_test_helpers import installation_org as installation_org

from unified_agent import config, object_model, platform_readers, storage
from unified_agent.self_organization import (
    APPLIED,
    PROPOSED,
    REJECTED,
    REVIEWED,
    ScopeEscapeError,
    SelfOrganizationDisabledError,
    apply_org_proposal,
    get_org_proposal,
    is_self_organization_enabled,
    list_org_proposals,
    org_proposal,
    propose_org_change,
    reject_org_proposal,
    review_org_proposal,
    set_self_organization_toggle,
)
from unified_agent.tools import approvals

pytestmark = pytest.mark.usefixtures("installation_org")


def test_self_organization_default_is_enabled(tmp_path, monkeypatch):
    """Proposing is always on by default when no control_flags key is set."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    assert is_self_organization_enabled() is True


def test_toggle_off_suppresses_proposals(tmp_path, monkeypatch):
    """Setting control_flags key self_organization to false suppresses proposals."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    # Toggle off
    toggle_res = set_self_organization_toggle(
        False, reason="Maintenance window", set_by="Desktop_Person"
    )
    assert toggle_res["enabled"] is False
    assert is_self_organization_enabled() is False

    # Proposing must be refused
    with pytest.raises(SelfOrganizationDisabledError, match="disabled via control_flags"):
        propose_org_change(
            title="Add triage lane",
            proposal_type="lane",
            scope="platform-engineering",
            rationale="Handle triage",
            proposed_changes={"lane": "triage"},
            author="Desktop_agent3",
        )

    # Via unified tool also refused
    with pytest.raises(SelfOrganizationDisabledError, match="disabled via control_flags"):
        org_proposal(
            "propose",
            {
                "title": "Add triage lane",
                "proposal_type": "lane",
                "scope": "platform-engineering",
                "rationale": "Handle triage",
                "proposed_changes": {"lane": "triage"},
                "author": "Desktop_agent3",
            },
        )

    # Toggle back on
    set_self_organization_toggle(True, reason="Maintenance complete", set_by="Desktop_Person")
    assert is_self_organization_enabled() is True

    # Now proposing succeeds
    prop = propose_org_change(
        title="Add triage lane",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Handle triage",
        proposed_changes={"lane": "triage"},
        author="Desktop_agent3",
    )
    assert prop["id"].startswith("orgprop-")
    assert prop["status"] == PROPOSED


def test_proposals_carry_required_metadata(tmp_path, monkeypatch):
    """Proposals from agents carry author, scope, rationale, and proposed_changes."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    prop = propose_org_change(
        title="Dynamic routine for queue grooming",
        proposal_type="routine",
        scope="platform-engineering",
        rationale="Automate queue grooming for platform tasks",
        proposed_changes={"schedule": "0 9 * * 1-5", "action": "groom_queue"},
        author="Desktop_agent3",
    )

    assert prop["author"] == "Desktop_agent3"
    assert prop["scope"] == "platform-engineering"
    assert prop["rationale"] == "Automate queue grooming for platform tasks"
    assert prop["proposed_changes"] == {"schedule": "0 9 * * 1-5", "action": "groom_queue"}
    assert prop["status"] == PROPOSED
    assert prop["approval_id"] is not None

    # Retrieve from storage
    stored = get_org_proposal(prop["id"])
    assert stored is not None
    assert stored["id"] == prop["id"]
    assert stored["title"] == "Dynamic routine for queue grooming"
    assert stored["proposed_changes"] == {"schedule": "0 9 * * 1-5", "action": "groom_queue"}


def test_scope_escape_is_denied(tmp_path, monkeypatch):
    """Scope-as-membership (task #704): an agent may not propose change outside its member offices."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    # Desktop_agent3 is in platform-engineering, but NOT in product-engineering or chief-of-staff
    with pytest.raises(ScopeEscapeError, match="scope escape denied"):
        propose_org_change(
            title="Reassign epic 1186",
            proposal_type="routing",
            scope="product-engineering",
            rationale="Re-route epic",
            proposed_changes={"epic": 1186},
            author="Desktop_agent3",
        )

    with pytest.raises(ScopeEscapeError, match="scope escape denied"):
        propose_org_change(
            title="Alter dispatch schedule",
            proposal_type="routing",
            scope="chief-of-staff",
            rationale="Change queue cadence",
            proposed_changes={"cadence": "hourly"},
            author="Desktop_agent3",
        )

    # Unknown office
    with pytest.raises(ToolError, match="unknown org unit / office scope"):
        propose_org_change(
            title="Invalid office change",
            proposal_type="role",
            scope="non-existent-office",
            rationale="Some rationale",
            proposed_changes={},
            author="Desktop_agent3",
        )

    # Human identity (Desktop_Person) has global authority across any office
    human_prop = propose_org_change(
        title="CEO strategic office realignment",
        proposal_type="role",
        scope="product-engineering",
        rationale="Realign product focus",
        proposed_changes={"focus": "enterprise"},
        author="Desktop_Person",
    )
    assert human_prop["scope"] == "product-engineering"
    assert human_prop["author"] == "Desktop_Person"


def test_applying_is_strictly_gated_by_human_approval(tmp_path, monkeypatch):
    """Applying is never automatic — it strictly requires human/policy approval gate."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    # The human's bearer, so approving org_proposal_apply (high evidence tier,
    # task #1672) can carry its signed producer stamp.
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")
    storage.init_db()

    prop = propose_org_change(
        title="Add platform routine",
        proposal_type="routine",
        scope="platform-engineering",
        rationale="Nightly lint pass",
        proposed_changes={"routine": "nightly-lint"},
        author="Desktop_agent3",
    )
    pid = prop["id"]
    aid = prop["approval_id"]
    assert aid is not None

    # Proposing agent cannot self-apply (separation of duties)
    with pytest.raises(ToolError, match="separation of duties"):
        apply_org_proposal(pid, applied_by="Desktop_agent3")

    # Another agent cannot apply without an approved human gate
    with pytest.raises(ToolError, match="not 'approved'; a human must resolve it"):
        apply_org_proposal(pid, applied_by="Desktop_Agent2")

    # An agent cannot approve the approval gate row either
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="human identity"):
            approvals.approval_resolve(aid, "approve", resolved_by="Desktop_Agent2")
    finally:
        config.agent_identity_var.reset(token)

    # Proposing agent attempting to approve own proposal fails separation of duties
    operator_token = config.agent_identity_var.set("Desktop_Person")
    try:
        # A human resolves the approval gate
        approvals.approval_resolve(
            aid,
            "approve",
            resolved_by="Desktop_Person",
            reason="Approved organizational routine",
        )
    finally:
        config.agent_identity_var.reset(operator_token)

    # Now applying consumes the one-shot pass and succeeds
    applied = apply_org_proposal(pid, applied_by="Desktop_Person")
    assert applied["ok"] is True
    assert applied["status"] == APPLIED
    assert applied["applied_by"] == "Desktop_Person"

    stored = get_org_proposal(pid)
    assert stored["status"] == APPLIED
    assert stored["applied_by"] == "Desktop_Person"
    assert stored["applied_at"] is not None

    # Second apply must fail
    with pytest.raises(ToolError, match="already been applied"):
        apply_org_proposal(pid, applied_by="Desktop_Person")


def test_reject_proposal(tmp_path, monkeypatch):
    """A proposal can be rejected, which marks status=rejected and denies pending approval."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    prop = propose_org_change(
        title="Experimental delegation rule",
        proposal_type="delegation",
        scope="platform-engineering",
        rationale="Test experimental delegation",
        proposed_changes={"rule": "auto-delegate"},
        author="Desktop_agent3",
    )
    pid = prop["id"]
    aid = prop["approval_id"]

    rejected = reject_org_proposal(pid, rejected_by="Desktop_Person", reason="Too risky")
    assert rejected["ok"] is True
    assert rejected["status"] == REJECTED
    assert rejected["rejected_by"] == "Desktop_Person"

    stored = get_org_proposal(pid)
    assert stored["status"] == REJECTED
    assert stored["resolution_reason"] == "Too risky"

    # Linked approval row should be denied
    app_row = approvals.get_approval(aid)
    assert app_row["status"] == "denied"

    # Cannot apply a rejected proposal
    with pytest.raises(ToolError, match="was rejected and cannot be applied"):
        apply_org_proposal(pid, applied_by="Desktop_Person")


def test_review_proposal(tmp_path, monkeypatch):
    """A proposal can transition from proposed to reviewed."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    prop = propose_org_change(
        title="Lane restructure",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Clean up lanes",
        proposed_changes={"lanes": ["fast-track"]},
        author="Desktop_agent3",
    )
    pid = prop["id"]

    rev = review_org_proposal(pid, reviewed_by="Desktop_Agent2", notes="Looks plausible")
    assert rev["ok"] is True
    assert rev["status"] == REVIEWED

    stored = get_org_proposal(pid)
    assert stored["status"] == REVIEWED
    assert stored["reviewed_by"] == "Desktop_Agent2"


def test_platform_reader_and_object_model(tmp_path, monkeypatch):
    """Platform reader read_org_proposal serves the registered ObjectType."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    prop = propose_org_change(
        title="Read model check",
        proposal_type="role",
        scope="platform-engineering",
        rationale="Verify platform reader",
        proposed_changes={"role": "tester"},
        author="Desktop_agent3",
    )
    pid = prop["id"]

    ot = object_model.by_kind("org_proposal")
    assert ot.served is True
    assert ot.uri_template == "unified-agent://org-proposals/{proposal_id}"
    uri = ot.render(pid)
    assert uri == f"unified-agent://org-proposals/{pid}"

    # Reader via platform_readers
    read_data = platform_readers.read_org_proposal(pid)
    assert read_data["uri"] == uri
    assert read_data["org_proposal"]["id"] == pid
    assert read_data["org_proposal"]["title"] == "Read model check"

    # Non-existent ID raises ResourceNotFoundError
    with pytest.raises(ResourceNotFoundError):
        platform_readers.read_org_proposal("orgprop-0000000000000000")


def test_unified_org_proposal_tool_actions(tmp_path, monkeypatch):
    """The unified org_proposal tool dispatches all actions correctly."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    # Toggle action
    t_status = org_proposal("toggle")
    assert t_status["enabled"] is True

    # Propose action
    created = org_proposal(
        "propose",
        {
            "title": "Unified action proposal",
            "proposal_type": "routing",
            "scope": "platform-engineering",
            "rationale": "Testing unified tool dispatch",
            "proposed_changes": {"routing": "test"},
            "author": "Desktop_agent3",
        },
    )
    pid = created["id"]
    assert pid.startswith("orgprop-")

    # List action
    listed = org_proposal("list", {"scope": "platform-engineering"})
    assert listed["count"] == 1
    assert listed["proposals"][0]["id"] == pid

    # Get action
    got = org_proposal("get", {"proposal_id": pid})
    assert got["proposal"]["id"] == pid

    # Invalid action
    with pytest.raises(ToolError, match="action must be one of"):
        org_proposal("invalid_action")


@pytest.mark.anyio
async def test_webui_org_proposals_endpoints(tmp_path, monkeypatch):
    """Test webui api payloads and HTTP routing for org proposals."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent.webui import api
    from unified_agent.webui.routes import web_routes

    prop = propose_org_change(
        title="WebUI payload check",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Check webui api",
        proposed_changes={"lane": "triage"},
        author="Desktop_agent3",
    )
    pid = prop["id"]

    # Test api.org_proposals_payload
    payload = await api.org_proposals_payload()
    assert payload["count"] == 1
    assert payload["proposals"][0]["id"] == pid

    # Test api.org_proposal_detail
    detail = await api.org_proposal_detail(pid)
    assert "proposal" in detail
    assert detail["proposal"]["id"] == pid

    detail_missing = await api.org_proposal_detail("orgprop-missing")
    assert "error" in detail_missing

    # Test HTTP routes via TestClient
    app = Starlette(routes=web_routes())
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get("/api/org-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["proposals"][0]["id"] == pid

    resp_detail = client.get(f"/api/org-proposals/{pid}")
    assert resp_detail.status_code == 200
    assert resp_detail.json()["proposal"]["id"] == pid

    resp_404 = client.get("/api/org-proposals/orgprop-notfound")
    assert resp_404.status_code == 404


def test_plain_text_proposal_approval_and_application(isolated_env):
    """Verify that proposals with raw string proposed_changes can be approved and applied."""
    prop = propose_org_change(
        title="Add a triage lane",
        proposal_type="routing",
        scope="platform-engineering",
        rationale="Improve incident triage",
        proposed_changes="Add a triage lane to handle high-urgency alarms",
        author="Desktop_Agent2",
    )
    pid = prop["proposal_id"]
    approval_id = prop["approval_id"]
    assert approval_id is not None

    # Approve proposal as human
    approvals.approval_resolve(
        int(approval_id),
        "approve",
        resolved_by="Desktop_Person",
    )

    # Human applies proposal
    applied = apply_org_proposal(pid, applied_by="Desktop_Person")
    assert applied["ok"] is True
    assert applied["status"] == "applied"

    fetched = get_org_proposal(pid)
    assert fetched["status"] == "applied"
    assert fetched["proposed_changes"] == {"raw": "Add a triage lane to handle high-urgency alarms"}


def test_verified_identity_spoofing_prevented(isolated_env):
    """Verify that an authenticated agent cannot submit or apply as another author."""
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        # Cannot propose as a different author
        with pytest.raises(ToolError, match="cannot submit proposals as author"):
            org_proposal(
                "propose",
                {
                    "title": "Unauthorized Change",
                    "proposal_type": "lane",
                    "scope": "platform-engineering",
                    "rationale": "Test spoofing",
                    "proposed_changes": {"lane": "test"},
                    "author": "Desktop_Person",
                },
            )

        # Can propose as self
        res = org_proposal(
            "propose",
            {
                "title": "Legit Change",
                "proposal_type": "lane",
                "scope": "platform-engineering",
                "rationale": "Test legit",
                "proposed_changes": {"lane": "test"},
            },
        )
        pid = res["proposal_id"]

        # Cannot apply as Operator
        with pytest.raises(ToolError, match="cannot apply proposals as 'Desktop_Person'"):
            org_proposal(
                "apply",
                {
                    "proposal_id": pid,
                    "applied_by": "Desktop_Person",
                },
            )
    finally:
        config.agent_identity_var.reset(token)


def test_toggle_permissions_and_string_values(isolated_env):
    """Verify human-only toggle authorization and proper string boolean parsing."""
    # Agent caller is refused
    with pytest.raises(ToolError, match="requires human authorization"):
        org_proposal("toggle", {"enabled": "false", "set_by": "Desktop_Agent2"})

    # Human caller with string "false" disables proposing
    res = org_proposal("toggle", {"enabled": "false", "set_by": "Desktop_Person"})
    assert res["enabled"] is False
    assert is_self_organization_enabled() is False

    with pytest.raises(SelfOrganizationDisabledError):
        propose_org_change(
            title="Disabled",
            proposal_type="lane",
            scope="platform-engineering",
            rationale="Test",
            proposed_changes={},
            author="Desktop_Agent2",
        )

    # Human caller with string "true" re-enables proposing
    res2 = org_proposal("toggle", {"enabled": "true", "set_by": "Desktop_Person"})
    assert res2["enabled"] is True
    assert is_self_organization_enabled() is True


def test_atomic_cas_and_terminal_states(isolated_env):
    """Verify that terminal states (applied, rejected) cannot be overwritten."""
    prop = propose_org_change(
        title="Terminal test",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Testing CAS",
        proposed_changes={},
        author="Desktop_Agent2",
    )
    pid = prop["proposal_id"]

    # Reject the proposal
    reject_org_proposal(pid, rejected_by="Desktop_Person", reason="Not feasible")
    current = get_org_proposal(pid)
    assert current["status"] == "rejected"

    # Trying to apply rejected proposal must fail
    with pytest.raises(ToolError, match="was rejected and cannot be applied"):
        apply_org_proposal(pid, applied_by="Desktop_Person")

    # Trying to review rejected proposal must fail
    with pytest.raises(ToolError, match="cannot mark as reviewed"):
        review_org_proposal(pid, reviewed_by="Desktop_Person")

    # Trying to reject already rejected proposal must fail
    with pytest.raises(ToolError, match="already in terminal state"):
        reject_org_proposal(pid, rejected_by="Desktop_Person")


def test_rejection_revokes_approved_pass(isolated_env):
    """Verify that rejecting a proposal with an approved pass revokes that pass."""
    prop = propose_org_change(
        title="Revoke pass test",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Testing pass revocation",
        proposed_changes={"lane": "revokable"},
        author="Desktop_Agent2",
    )
    pid = prop["proposal_id"]
    approval_id = int(prop["approval_id"])

    # Approve the pass
    approvals.approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")
    app_before = approvals.get_approval(approval_id)
    assert app_before["status"] == "approved"

    # Reject the proposal -> should revoke the unconsumed approved pass
    reject_org_proposal(pid, rejected_by="Desktop_Person", reason="Decision changed")
    app_after = approvals.get_approval(approval_id)
    assert app_after["status"] == "revoked"


def test_multi_tenant_isolation(isolated_env):
    """Verify tenant org_id scoping in get and list."""
    prop1 = propose_org_change(
        title="Tenant A Change",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="For tenant A",
        proposed_changes={},
        author="Desktop_Agent2",
    )
    pid1 = prop1["proposal_id"]

    # Update row directly to set org_id to 'tenant-a'
    conn = storage.connect()
    try:
        conn.execute("UPDATE org_proposals SET org_id = 'tenant-a' WHERE id = ?", (pid1,))
        conn.commit()
    finally:
        conn.close()

    # Query with tenant-a
    found_a = get_org_proposal(pid1, org_id="tenant-a")
    assert found_a is not None
    assert found_a["id"] == pid1

    # Query with tenant-b -> should be hidden
    found_b = get_org_proposal(pid1, org_id="tenant-b")
    assert found_b is None

    # List with tenant-b -> should not include prop1
    list_b = list_org_proposals(org_id="tenant-b")
    assert not any(p["id"] == pid1 for p in list_b["proposals"])


def test_trust_preset_read_allowlist():
    """Verify org_proposal list and get actions are recognized as read-only by trust presets."""
    from unified_agent.trust_presets import is_read_only_call

    assert is_read_only_call("org_proposal", {"action": "list"}) is True
    assert (
        is_read_only_call("org_proposal", {"action": "get", "params": {"proposal_id": "test"}})
        is True
    )
    assert is_read_only_call("org_proposal", {"action": "propose"}) is False
    assert is_read_only_call("org_proposal", {"action": "apply"}) is False


def test_human_aliases_separation_of_duties(isolated_env):
    """Verify that human aliases (Operator vs Desktop_Person) cannot bypass separation of duties."""
    prop = propose_org_change(
        title="Alias test",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Testing human aliases",
        proposed_changes={},
        author="Desktop_Person",
    )
    pid = prop["id"]

    # Desktop_Person authored this proposal; Operator must not be able to self-apply it
    with pytest.raises(ToolError, match="separation of duties"):
        apply_org_proposal(pid, applied_by="Operator")


def test_review_notes_survive_decision(isolated_env):
    """Verify that review_notes are durably preserved when proposal is applied or rejected."""
    prop = propose_org_change(
        title="Notes test",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Testing review notes",
        proposed_changes={},
        author="Desktop_Agent2",
    )
    pid = prop["id"]
    approval_id = int(prop["approval_id"])

    # Review with specific notes
    review_org_proposal(pid, reviewed_by="Desktop_Agent5", notes="Architecturally sound")
    reviewed = get_org_proposal(pid)
    assert reviewed["status"] == "reviewed"
    assert reviewed["review_notes"] == "Architecturally sound"

    # Approve approval gate
    approvals.approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")

    # Apply with resolution reason
    applied = apply_org_proposal(pid, applied_by="Desktop_Person", reason="Executed migration")
    assert applied["status"] == "applied"

    fetched = get_org_proposal(pid)
    assert fetched["status"] == "applied"
    assert fetched["review_notes"] == "Architecturally sound"
    assert fetched["resolution_reason"] == "Executed migration"


def test_tenant_null_proposals_isolated(isolated_env):
    """Verify that a specific tenant query does not return legacy NULL-org proposals."""
    prop = propose_org_change(
        title="Global NULL Org Proposal",
        proposal_type="lane",
        scope="platform-engineering",
        rationale="Legacy null tenant",
        proposed_changes={},
        author="Desktop_Agent2",
    )
    pid = prop["id"]

    # Confirm prop has NULL org_id
    stored = get_org_proposal(pid, org_id=None)
    assert stored is not None
    assert stored["org_id"] is None

    # Query with tenant-a must NOT return this NULL proposal
    tenant_lookup = get_org_proposal(pid, org_id="tenant-a")
    assert tenant_lookup is None

    tenant_list = list_org_proposals(org_id="tenant-a")
    assert not any(p["id"] == pid for p in tenant_list["proposals"])


def test_claimed_human_identity_cannot_toggle(isolated_env, monkeypatch):
    """Verify that an agent process cannot toggle by claiming a human set_by label."""
    monkeypatch.setenv("UA_AGENT_LABEL", "Desktop_agent3")
    with pytest.raises(
        ToolError, match="cannot toggle self-organization; human authorization required"
    ):
        org_proposal("toggle", {"enabled": "false", "set_by": "Desktop_Person"})
