"""E2E Tests for Requirement R1: Approval Ledger, Repository Bindings, and Doctor Audits.

Authoritative source:
- ORIGINAL_REQUEST.md § R1
- orchestrator_2/PROJECT.md § M1
- explorer_survey_batch_1/handoff.md
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import project_bindings
from unified_agent.tools import approvals, doctor

from .conftest import identity_context, resolve_human_approval


class TestR1ApprovalLedgerAndDoctor:
    """Tier 1 (Feature Coverage) and Tier 2 (Boundary & Corner Cases) for R1."""

    # -------------------------------------------------------------------------
    # Tier 1: Feature Coverage (Happy Path)
    # -------------------------------------------------------------------------

    def test_r1_approval_pending_creation_and_listing(self):
        """Feature: Gated tool call creates pending approval record and raises ApprovalPendingError."""
        payload = {"command": "deploy-production --fleet", "dry_run": False}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )

        app_id = exc_info.value.approval_id
        assert app_id > 0

        # Query pending approvals
        pending_list = approvals.approval_pending()
        assert "pending" in pending_list
        assert len(pending_list["pending"]) >= 1
        found = next((item for item in pending_list["pending"] if item["id"] == app_id), None)
        assert found is not None
        assert found["tool"] == "shell.run_cmd"
        assert found["requested_by"] == "Desktop_Agent7"
        assert found["status"] == "pending"

    def test_r1_approval_resolution_by_authorized_human(self):
        """Feature: Authorized human (Desktop_Person) resolves pending approval to 'approved'."""
        payload = {"action": "push_release", "tag": "v2.0.0"}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )

        app_id = exc_info.value.approval_id

        # Human resolves approval
        res = resolve_human_approval(app_id, "approve", approver="Desktop_Person")
        assert res["status"] == "approved"
        assert res["id"] == app_id

        # Verification in store
        record = approvals.get_approval(app_id)
        assert record["status"] == "approved"
        assert record["resolved_by"] == "Desktop_Person"
        assert record["resolved_at"] is not None

    def test_r1_approval_invocation_consumption_one_shot(self):
        """Feature: Approved one-shot pass is consumed on execution and cannot be re-used."""
        payload = {"action": "database_migration", "step": 1}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "storage.migrate",
                payload,
                requested_by="Desktop_Agent2",
                force=True,
            )
        app_id = exc_info.value.approval_id
        resolve_human_approval(app_id, "approve")

        # First consumption under approval_invocation
        with approvals.approval_invocation():
            receipt = approvals.check_approval_gate(
                "storage.migrate",
                payload,
                requested_by="Desktop_Agent2",
                force=True,
            )
            assert receipt["gated"] is True
            assert receipt["approval_id"] == app_id

        # Record should now be consumed
        record = approvals.get_approval(app_id)
        assert record["status"] == "consumed"

        # Subsequent invocation must NOT reuse spent pass; raises new ApprovalPendingError
        with pytest.raises(approvals.ApprovalPendingError) as exc_info_2:
            approvals.check_approval_gate(
                "storage.migrate",
                payload,
                requested_by="Desktop_Agent2",
                force=True,
            )
        assert exc_info_2.value.approval_id != app_id

    def test_r1_project_repo_binding_lifecycle(self):
        """Feature: Project-to-repository binding CRUD and normalization (task #1425)."""
        # Bind project
        res = project_bindings.bind(
            project="sample-store",
            repo="https://github.com/anonb3ll/sample-store.git",
            bound_by="Desktop_Person",
        )
        bound = res["binding"]
        assert bound["project"] == "sample-store"
        assert bound["repo"] == "anonb3ll/sample-store"
        assert bound["bound_by"] == "Desktop_Person"

        # Get binding
        current = project_bindings.get("sample-store")
        assert current is not None
        assert current["binding"] is not None
        assert current["binding"]["repo"] == "anonb3ll/sample-store"

        # List bindings
        all_bindings = project_bindings.list_bindings()
        assert "projects" in all_bindings
        sm_entry = next(
            (p for p in all_bindings["projects"] if p["project"] == "sample-store"), None
        )
        assert sm_entry is not None
        assert sm_entry["binding"]["repo"] == "anonb3ll/sample-store"

        # Unbind project
        unbound = project_bindings.unbind("sample-store", unbound_by="Desktop_Person")
        assert unbound["unbound"] is True
        assert unbound["previous"]["repo"] == "anonb3ll/sample-store"

        # Verify unbound (returns None for non-default projects)
        assert project_bindings.get("sample-store")["binding"] is None

    def test_r1_doctor_system_audit_clean_pass(self):
        """Feature: System doctor runs complete audit cleanly without raising unhandled exceptions."""
        result = doctor.system_doctor()
        assert isinstance(result, dict)
        assert "checks" in result
        assert "summary" in result
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) > 0

    # -------------------------------------------------------------------------
    # Tier 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r1_approval_denial_blocks_retries(self):
        """Boundary: Human denial marks approval 'denied' and causes immediate ToolError on retry."""
        payload = {"command": "rm -rf /tmp/test-boundary"}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent4",
                force=True,
            )
        app_id = exc_info.value.approval_id

        # Human denies request with explicit reason
        with identity_context("Desktop_Person"):
            approvals.approval_resolve(app_id, "deny", reason="Unsafe directory removal")

        record = approvals.get_approval(app_id)
        assert record["status"] == "denied"
        assert record["reason"] == "Unsafe directory removal"

        # Retry with identical payload immediately raises ToolError citing denial
        with pytest.raises(ToolError, match="was DENIED: Unsafe directory removal"):
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent4",
                force=True,
            )

    def test_r1_unauthorized_approver_cannot_approve(self):
        """Boundary: An unverified or non-human actor cannot approve approval requests."""
        payload = {"op": "privilege_escalation"}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "admin.escalate",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
        app_id = exc_info.value.approval_id

        # Developer trying to approve its own or peer request should fail separation of duties / authority
        with (
            identity_context("Desktop_Agent7"),
            pytest.raises((ToolError, PermissionError)),
        ):
            approvals.approval_resolve(app_id, "approve")

    def test_r1_invalid_project_binding_repo_rejected(self):
        """Boundary: Disallowed host or malformed repo string is rejected."""
        with pytest.raises(ToolError, match="is not allowed"):
            project_bindings.bind(
                project="unified-agent",
                repo="https://malicious-phishing.com/evil/agent.git",
                bound_by="Desktop_Person",
            )

        with pytest.raises(ToolError, match="unknown project stream"):
            project_bindings.bind(
                project="../traversal",
                repo="https://github.com/anonb3ll/agent",
                bound_by="Desktop_Person",
            )

    def test_r1_bare_tool_entry_parks_without_restoring_policy_pass(self):
        """Boundary: Bare tool call raising ApprovalPendingError marks pre-execution failure."""
        payload = {"command": "git push origin main"}
        receipts_ref = None
        with (
            pytest.raises(approvals.ApprovalPendingError),
            approvals.approval_invocation() as receipts,
        ):
            receipts_ref = receipts
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent2",
                force=True,
            )
        assert receipts_ref is not None
        assert approvals.failed_before_execution(receipts_ref) is True

    def test_r1_approval_payload_canonicalization_variance(self):
        """Boundary: Equivalent payloads with differing key order or whitespace map to identical approval."""
        payload1 = {"alpha": 1, "beta": "two", "tags": ["a", "b"]}
        payload2 = {"tags": ["a", "b"], "beta": "two", "alpha": 1}

        # Request with payload1
        with pytest.raises(approvals.ApprovalPendingError) as exc1:
            approvals.check_approval_gate(
                "test_tool", payload1, requested_by="Desktop_Agent7", force=True
            )
        app_id_1 = exc1.value.approval_id

        # Request with payload2 should deduplicate to the same pending approval ID
        with pytest.raises(approvals.ApprovalPendingError) as exc2:
            approvals.check_approval_gate(
                "test_tool", payload2, requested_by="Desktop_Agent7", force=True
            )
        app_id_2 = exc2.value.approval_id

        assert app_id_1 == app_id_2
