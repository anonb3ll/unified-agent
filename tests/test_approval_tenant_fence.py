"""Tenant (org_id) fence on approval reads, resolves and gate lookups (task #1793).

Approval writes stamp ``edition.tenant_org_id()``, but reads and the gate's
pass/denial lookups did not filter by it: one tenant could list, read or
resolve another tenant's request, and another tenant's approved pass or
denial for the same tool and payload applied to this tenant's call. Another
tenant's row must now read exactly like a missing one. The untenanted private
hub (tenant ``None``) keeps matching NULL rows via ``IS``.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools import approvals
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)

ORG_A = "org_a"
ORG_B = "org_b"
CALL = ("run_cmd", {"command": "echo tenant"})


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _request(org_switch, org: str) -> int:
    org_switch(org)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(*CALL)
    return approval_pending()["pending"][0]["id"]


def _ids(bucket: str) -> set[int]:
    return {row["id"] for row in approval_pending()[bucket]}


def test_reads_of_another_tenants_request_read_like_missing(as_org):
    a_id = _request(as_org, ORG_A)
    as_org(ORG_B)
    assert approvals.get_approval(a_id) is None
    assert approvals.get_approvals([a_id]) == {}
    assert a_id not in _ids("pending")
    as_org(ORG_A)
    assert approvals.get_approval(a_id)["id"] == a_id
    assert set(approvals.get_approvals([a_id])) == {a_id}
    assert _ids("pending") == {a_id}


def test_resolve_of_another_tenants_request_reads_like_missing(as_org):
    a_id = _request(as_org, ORG_A)
    as_org(ORG_B)
    with pytest.raises(ToolError) as foreign:
        approval_resolve(a_id, "approve", resolved_by=config.operator_label())
    with pytest.raises(ToolError) as missing:
        approval_resolve(99999, "approve", resolved_by=config.operator_label())
    assert str(foreign.value) == str(missing.value).replace("99999", str(a_id))
    as_org(ORG_A)
    assert approvals.get_approval(a_id)["status"] == "pending"


def test_another_tenants_pass_does_not_authorize_this_tenants_call(as_org):
    a_id = _request(as_org, ORG_A)
    approval_resolve(a_id, "approve", resolved_by=config.operator_label())
    # Same tool and payload from org B: queues its own request, never spends A's pass.
    b_id = _request(as_org, ORG_B)
    assert b_id != a_id
    as_org(ORG_A)
    assert approvals.get_approval(a_id)["status"] == "approved"
    assert check_approval_gate(*CALL) == {"gated": True, "approval_id": a_id}


def test_another_tenants_denial_does_not_deny_this_tenants_call(as_org):
    a_id = _request(as_org, ORG_A)
    approval_resolve(a_id, "deny", resolved_by=config.operator_label(), reason="org a says no")
    assert approvals.lookup_denied_approval(*CALL) == (a_id, "org a says no")
    as_org(ORG_B)
    assert approvals.lookup_denied_approval(*CALL) is None
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(*CALL)


def test_another_tenants_pending_row_does_not_absorb_this_tenants_request(as_org):
    a_id = _request(as_org, ORG_A)
    b_id = _request(as_org, ORG_B)
    assert b_id != a_id
    assert _ids("pending") == {b_id}


def test_untenanted_hub_sees_only_untenanted_rows(as_org, monkeypatch):
    a_id = _request(as_org, ORG_A)
    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(*CALL)
    plain = _ids("pending")
    assert a_id not in plain and len(plain) == 1
    assert approvals.get_approval(a_id) is None
