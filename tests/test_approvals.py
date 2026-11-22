"""Tests for the opt-in global approval queue (task #62 · C).

The gate is a hub-side chokepoint: UA_APPROVAL_TOOLS (env, global) decides
what is gated — agents cannot opt out. Default (unset) must leave every tool's
behavior untouched.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.tools import approvals as approval_tools
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
    get_approval,
)
from unified_agent.tools.shell import run_cmd


class TestGateOptIn:
    def test_ungated_by_default(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        assert check_approval_gate("run_cmd", {"command": "echo hi"}) == {"gated": False}
        assert approval_pending()["pending"] == []

    def test_default_run_cmd_unchanged(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("echo hello")
        assert res["exit_code"] == 0
        assert "hello" in res["stdout"]
        # no queue rows were created as a side effect
        assert approval_pending()["pending"] == []

    def test_unlisted_tool_not_gated(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "memory_delete")
        assert check_approval_gate("run_cmd", {"command": "echo hi"}) == {"gated": False}


class TestQueueLifecycle:
    def test_gated_call_parks_and_lists(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError, match="queued"):
            check_approval_gate("run_cmd", {"command": "echo hi"}, requested_by="Desktop_agent3")
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        assert pending[0]["tool"] == "run_cmd"
        assert pending[0]["payload"] == {"command": "echo hi"}
        assert pending[0]["requested_by"] == "Desktop_agent3"

    def test_retry_reuses_pending_row(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        for _ in range(3):
            with pytest.raises(ApprovalPendingError):
                check_approval_gate("run_cmd", {"command": "echo hi"})
        assert len(approval_pending()["pending"]) == 1

    def test_approve_is_one_shot_pass(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo hi"})
        rid = approval_pending()["pending"][0]["id"]
        row = approval_resolve(rid, "approve", resolved_by="Operator")
        assert row["status"] == "approved"
        # identical retried call consumes the pass and proceeds
        res = check_approval_gate("run_cmd", {"command": "echo hi"})
        assert res == {"gated": True, "approval_id": rid}
        # the pass is one-shot: a second identical call queues again
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo hi"})

    def test_approve_requires_a_different_human(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo agent"}, requested_by="Desktop_agent3")
        rid = approval_pending()["pending"][0]["id"]
        with pytest.raises(ToolError, match="named approver"):
            approval_resolve(rid, "approve")
        with pytest.raises(ToolError, match="human identity"):
            approval_resolve(rid, "approve", resolved_by="Desktop_Agent2")
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(ToolError, match="human identity"):
                approval_resolve(rid, "approve", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(token)
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo self"}, requested_by="Desktop_Person")
        self_id = approval_pending()["pending"][1]["id"]
        with pytest.raises(ToolError, match="separation of duties"):
            approval_resolve(self_id, "approve", resolved_by="Desktop_Person")
        with pytest.raises(ToolError, match="separation of duties"):
            approval_resolve(self_id, "approve", resolved_by="Operator")
        alias = config.agent_identity_var.set("Operator")
        try:
            with pytest.raises(ToolError, match="separation of duties"):
                approval_resolve(self_id, "approve", resolved_by="someone-else")
        finally:
            config.agent_identity_var.reset(alias)
        approval_resolve(self_id, "deny", reason="withdrawn")
        row = approval_resolve(rid, "approve", resolved_by="Desktop_Person")
        assert row["status"] == "approved"
        assert row["resolved_by"] == "Desktop_Person"
        assert row["requested_by"] == "Desktop_agent3"

    def test_enforced_auth_rejects_caller_supplied_human_name(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo agent"}, requested_by="Desktop_agent3")
        rid = approval_pending()["pending"][0]["id"]
        with pytest.raises(ToolError, match="verified human identity"):
            approval_resolve(rid, "approve", resolved_by="Desktop_Person")
        assert approval_pending()["pending"][0]["id"] == rid
        verified = config.agent_identity_var.set("Desktop_Person")
        try:
            approved = approval_resolve(rid, "approve", resolved_by="spoofed")
        finally:
            config.agent_identity_var.reset(verified)
        assert approved["resolved_by"] == "Desktop_Person"

    def test_verified_identity_is_recorded_over_caller_value(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo identity"})
        rid = approval_pending()["pending"][0]["id"]
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            row = approval_resolve(rid, "approve", resolved_by="spoofed-operator")
        finally:
            config.agent_identity_var.reset(token)
        assert row["resolved_by"] == "Desktop_Person"

    def test_approval_matches_exact_payload_only(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo hi"})
        rid = approval_pending()["pending"][0]["id"]
        approval_resolve(rid, "approve", resolved_by="Desktop_Person")
        # a different payload does not consume the pass
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "rm -rf /"})
        assert len(approval_pending()["approved_unconsumed"]) == 1

    def test_deny_blocks_retries_with_reason(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo hi"})
        rid = approval_pending()["pending"][0]["id"]
        approval_resolve(rid, "deny", reason="not while prod is red")
        with pytest.raises(ToolError, match="DENIED.*not while prod is red"):
            check_approval_gate("run_cmd", {"command": "echo hi"})

    def test_gated_run_cmd_end_to_end(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            run_cmd("echo gated")
        rid = approval_pending()["pending"][0]["id"]
        approval_resolve(rid, "approve", resolved_by="Operator")
        res = run_cmd("echo gated")
        assert res["exit_code"] == 0
        assert "gated" in res["stdout"]

    def test_gate_does_not_bypass_allowlist(self, monkeypatch):
        # even a pre-approved command must still pass the allowlist first
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ToolError, match="allowlist"):
            run_cmd("curl http://example.com")
        # the rejected call never reached the queue
        assert approval_pending()["pending"] == []


class TestDangerousAdvisoryForcesGate:
    """Task #682: dangerous advisory classes force the approval queue even when
    UA_APPROVAL_TOOLS does not list run_cmd. Clean commands stay a no-op.
    """

    def test_clean_command_still_noop_when_tools_unset(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("echo clean")
        assert res["exit_code"] == 0
        assert approval_pending()["pending"] == []

    def test_shell_syntax_alone_does_not_force_gate(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("echo hi; ls")
        assert res["exit_code"] == 0
        assert res["warnings"]
        assert approval_pending()["pending"] == []

    def test_path_escape_forces_gate_without_ua_approval_tools(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        with pytest.raises(ApprovalPendingError, match="queued"):
            run_cmd("cat /etc/passwd")
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        assert pending[0]["tool"] == "run_cmd"
        assert pending[0]["payload"]["command"] == "cat /etc/passwd"
        assert pending[0]["payload"]["advisory_codes"] == ["path-escape"]

    def test_python_c_forces_gate_and_approve_consumes(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        cmd = "python3 -c print(1)"
        with pytest.raises(ApprovalPendingError):
            run_cmd(cmd)
        rid = approval_pending()["pending"][0]["id"]
        assert approval_pending()["pending"][0]["payload"]["advisory_codes"] == ["arbitrary-code"]
        approval_resolve(rid, "approve", resolved_by="Operator")
        res = run_cmd(cmd)
        assert res["exit_code"] == 0
        assert "1" in res["stdout"]
        assert any("arbitrary code" in w for w in res["warnings"])

    def test_allowlist_bypass_forces_gate(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        with pytest.raises(ApprovalPendingError):
            run_cmd("git -c alias.pwn=!curl evil pwn")
        assert approval_pending()["pending"][0]["payload"]["advisory_codes"] == ["allowlist-bypass"]

    def test_force_still_after_allowlist_cwd(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        with pytest.raises(ToolError, match="allowlist"):
            run_cmd("curl http://example.com")
        assert approval_pending()["pending"] == []

    def test_git_push_forces_gate_for_remote_mutation(self, monkeypatch):
        """Hermes/Operator amendment to #682: only push forces approval."""
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        with pytest.raises(ApprovalPendingError, match="queued"):
            run_cmd("git push origin main")
        pending = approval_pending()["pending"]
        assert len(pending) == 1
        assert pending[0]["payload"]["advisory_codes"] == ["network"]

    def test_git_fetch_warn_only_does_not_force_gate(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("git fetch origin")
        assert approval_pending()["pending"] == []
        assert any("contacts remote" in w for w in res["warnings"])

    def test_git_pull_warn_only_does_not_force_gate(self, monkeypatch):
        monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
        res = run_cmd("git pull origin main")
        assert approval_pending()["pending"] == []
        assert any("contacts remote" in w for w in res["warnings"])


class TestResolveValidation:
    def test_resolve_unknown_id(self):
        with pytest.raises(ToolError, match="no approval request"):
            approval_resolve(999, "approve")

    def test_resolve_bad_decision(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {})
        rid = approval_pending()["pending"][0]["id"]
        with pytest.raises(ToolError, match="approve"):
            approval_resolve(rid, "maybe")

    def test_resolve_twice_refused(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {})
        rid = approval_pending()["pending"][0]["id"]
        approval_resolve(rid, "deny")
        with pytest.raises(ToolError, match="not 'pending'"):
            approval_resolve(rid, "approve")


def _row_field(approval_id: int, field: str):
    conn = storage.connect()
    try:
        row = conn.execute(f"SELECT {field} FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return row[field] if row else None
    finally:
        conn.close()


def _status_of(approval_id: int) -> str | None:
    return _row_field(approval_id, "status")


def _reason_of(approval_id: int) -> str:
    return _row_field(approval_id, "reason") or ""


def _age_approval(approval_id: int, hours: float) -> None:
    """Backdate a pass's grant time. Time is injected — no sleeping to cross a TTL."""
    granted = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at=?, created_at=? WHERE id=?",
            (granted, granted, approval_id),
        )
        conn.commit()
    finally:
        conn.close()


def _queue_and_approve(monkeypatch, command="echo hi") -> tuple[int, dict]:
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    payload = {"command": command}
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
    approval_id = approval_pending()["pending"][0]["id"]
    approval_resolve(approval_id, "approve", resolved_by="Operator")
    return approval_id, payload


def _pause_next_approval_read(monkeypatch, prefix: str):
    """Pause one approval read and return events for a deterministic interleave."""
    original = storage.connect
    lock = Lock()
    paused = False
    selected = Event()
    release = Event()

    class Connection:
        def __init__(self):
            self.inner = original()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            nonlocal paused
            cursor = self.inner.execute(sql, *args)
            should_pause = False
            if sql.startswith(prefix):
                with lock:
                    if not paused:
                        paused = True
                        should_pause = True
            if should_pause:
                selected.set()
                assert release.wait(timeout=5)
            return cursor

    monkeypatch.setattr(storage, "connect", Connection)
    return selected, release


def _capture_expected_error(fn):
    try:
        return fn()
    except (ApprovalPendingError, ToolError) as exc:
        return exc


def test_consume_then_revoke_cas_preserves_consumed(monkeypatch):
    approval_id, payload = _queue_and_approve(monkeypatch)
    selected, release = _pause_next_approval_read(monkeypatch, "SELECT * FROM approvals WHERE id=?")
    with ThreadPoolExecutor(max_workers=1) as pool:
        revoke = pool.submit(
            _capture_expected_error, lambda: approval_resolve(approval_id, "revoke")
        )
        assert selected.wait(timeout=5)
        consumed = check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
        release.set()
        revoked = revoke.result()
    assert consumed["gated"] is True
    assert isinstance(revoked, ToolError)
    assert _status_of(approval_id) == "consumed"


def test_revoke_then_consume_cas_preserves_revoked(monkeypatch):
    approval_id, payload = _queue_and_approve(monkeypatch)
    selected, release = _pause_next_approval_read(
        monkeypatch, "SELECT * FROM approvals WHERE tool=?"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        consume = pool.submit(
            _capture_expected_error,
            lambda: check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3"),
        )
        assert selected.wait(timeout=5)
        revoked = approval_resolve(approval_id, "revoke")
        release.set()
        consumed = consume.result()
    assert revoked["status"] == "revoked"
    assert isinstance(consumed, ApprovalPendingError)
    assert _status_of(approval_id) == "revoked"


@pytest.mark.parametrize("decisions", [("approve", "deny"), ("deny", "approve")])
def test_concurrent_resolvers_have_one_compare_and_swap_winner(monkeypatch, decisions):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": "echo resolve-race"})
    approval_id = approval_pending()["pending"][0]["id"]
    selected, release = _pause_next_approval_read(monkeypatch, "SELECT * FROM approvals WHERE id=?")

    def resolve(decision):
        return _capture_expected_error(
            lambda: approval_resolve(approval_id, decision, resolved_by="Operator")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolve, decisions[0])
        assert selected.wait(timeout=5)
        second = resolve(decisions[1])
        release.set()
        first = first.result()
    assert isinstance(first, ToolError)
    assert isinstance(second, dict)
    assert _status_of(approval_id) == {"approve": "approved", "deny": "denied"}[decisions[1]]


def test_two_consumers_have_one_cas_winner(monkeypatch):
    approval_id, payload = _queue_and_approve(monkeypatch)
    selected, release = _pause_next_approval_read(
        monkeypatch, "SELECT * FROM approvals WHERE tool=?"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            _capture_expected_error,
            lambda: check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3"),
        )
        assert selected.wait(timeout=5)
        second = _capture_expected_error(
            lambda: check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
        )
        release.set()
        first = first.result()
    assert isinstance(second, dict)
    assert isinstance(first, ApprovalPendingError)
    assert _status_of(approval_id) == "consumed"


def test_expiry_cas_does_not_overwrite_changed_status(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
    approval_id, payload = _queue_and_approve(monkeypatch)
    _age_approval(approval_id, hours=25)
    selected, release = _pause_next_approval_read(
        monkeypatch, "SELECT * FROM approvals WHERE tool=?"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            _capture_expected_error,
            lambda: check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3"),
        )
        assert selected.wait(timeout=5)
        conn = storage.connect()
        try:
            conn.execute("UPDATE approvals SET status='consumed' WHERE id=?", (approval_id,))
            conn.commit()
        finally:
            conn.close()
        release.set()
        result = first.result()
    assert isinstance(result, ApprovalPendingError)
    assert _status_of(approval_id) == "consumed"


class TestRevocation:
    """An approved-but-unconsumed pass had no revocation path at all.

    approval_resolve accepted only 'pending' rows, so a granted pass sat in the
    queue as a standing one-shot authorization — approval_pending surfaced it
    under approved_unconsumed precisely because it is a liability, while
    offering no way to act on it. Two such passes from 2026-07-19 were still
    live a week later. See task #171.
    """

    def test_revoked_pass_does_not_authorize_the_call(self, monkeypatch):
        approval_id, payload = _queue_and_approve(monkeypatch)

        revoked = approval_resolve(approval_id, "revoke", resolved_by="Operator", reason="stale")
        assert revoked["status"] == "revoked"
        assert revoked["resolved_by"] == "Operator"
        assert revoked["resolved_at"] is not None

        # The identical call must NOT execute — it re-queues for a fresh decision.
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")

    def test_revoked_row_stays_in_the_audit_trail(self, monkeypatch):
        approval_id, payload = _queue_and_approve(monkeypatch)
        approval_resolve(approval_id, "revoke", resolved_by="Operator", reason="superseded")

        conn = storage.connect()
        try:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        finally:
            conn.close()
        assert row is not None, "revocation must not delete the row"
        assert row["status"] == "revoked"
        assert row["reason"] == "superseded"

    def test_revoke_is_not_a_permanent_deny(self, monkeypatch):
        """The human withdrew one grant; they did not refuse the action forever."""
        approval_id, payload = _queue_and_approve(monkeypatch)
        approval_resolve(approval_id, "revoke", resolved_by="Operator")

        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
        # ...and the fresh request is approvable again.
        new_id = approval_pending()["pending"][0]["id"]
        assert new_id != approval_id
        approval_resolve(new_id, "approve", resolved_by="Operator")
        assert (
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")["gated"] is True
        )

    def test_cannot_revoke_a_pending_row(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": "echo hi"})
        pending_id = approval_pending()["pending"][0]["id"]

        with pytest.raises(ToolError, match="only an approved-but-unconsumed pass"):
            approval_resolve(pending_id, "revoke")

    def test_cannot_revoke_a_consumed_pass(self, monkeypatch):
        approval_id, payload = _queue_and_approve(monkeypatch)
        check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")  # consumes it

        with pytest.raises(ToolError, match="not 'approved'"):
            approval_resolve(approval_id, "revoke")

    def test_approve_on_an_approved_row_points_at_revoke(self, monkeypatch):
        approval_id, _ = _queue_and_approve(monkeypatch)
        with pytest.raises(ToolError, match="use decision='revoke'"):
            approval_resolve(approval_id, "deny")

    def test_rejects_unknown_decision(self):
        with pytest.raises(ToolError, match="must be 'approve', 'deny' or 'revoke'"):
            approval_resolve(1, "maybe")


class TestPassExpiry:
    """A pass with no expiry is a permanent grant, contradicting the standing
    rule that approvals are not durable (task #171)."""

    def test_expired_pass_does_not_authorize_the_call(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, payload = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=25)

        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")

    def test_expired_row_still_reports_why(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, payload = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=25)
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")

        conn = storage.connect()
        try:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "expired", "expiry must not delete or silently drop the row"
        assert "expired unconsumed" in row["reason"]
        assert "24" in row["reason"]

    def test_pass_inside_the_ttl_still_works(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, payload = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=1)

        assert check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3") == {
            "gated": True,
            "approval_id": approval_id,
        }

    def test_undateable_pass_fails_closed(self, monkeypatch):
        """A pass of unknown age must not authorize anything."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        approval_id, payload = _queue_and_approve(monkeypatch)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE approvals SET resolved_at=NULL, created_at='not-a-timestamp' WHERE id=?",
                (approval_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")

    def test_misconfigured_ttl_falls_back_rather_than_disabling_expiry(self, monkeypatch):
        for bad in ("0", "-5", "not-a-number"):
            monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", bad)
            assert config.approval_pass_ttl_hours() == config.APPROVAL_PASS_TTL_HOURS_DEFAULT

    def test_pending_shows_remaining_ttl(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, _ = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=4)

        res = approval_pending()
        row = res["approved_unconsumed"][0]
        assert res["pass_ttl_hours"] == 24
        assert row["expired"] is False
        assert 19.5 < row["expires_in_hours"] < 20.5
        assert row["expires_at"] is not None

    def test_a_stale_pass_moves_out_of_the_actionable_list(self, monkeypatch):
        """SUPERSEDES test_pending_flags_a_stale_pass (task #596).

        That test asserted a stale pass stays in `approved_unconsumed` flagged
        `expired: True`. The intent was right — an unconsumed pass is a
        liability and must stay visible — but the effect was that inert rows
        piled up in the list an operator reads to decide what to act on. The
        only reaper (`check_approval_gate`) runs when a matching gated call
        arrives, and with `UA_APPROVAL_TOOLS` empty none ever did, so by the
        time the gate went live on 2026-08-25 there were 14 of them.

        They are still reported, and still revocable — just under their own key
        and out of the actionable set. Nothing is retired here: reaping on a
        read would make listing the queue mutate it, and would expire a pass
        before a human could revoke it.
        """
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, _ = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=200)

        res = approval_pending()
        assert res["approved_unconsumed"] == [], "an expired pass authorizes nothing"
        row = res["expired_passes"][0]
        assert row["id"] == approval_id
        assert row["expired"] is True
        assert row["expires_in_hours"] < 0

        # Listing did not mutate: the row is still 'approved', so the #171
        # revocation path still works on it.
        assert _status_of(approval_id) == "approved"

    def test_a_live_pass_stays_in_the_actionable_list(self, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        approval_id, _ = _queue_and_approve(monkeypatch)
        _age_approval(approval_id, hours=4)

        res = approval_pending()
        assert [r["id"] for r in res["approved_unconsumed"]] == [approval_id]
        assert res["expired_passes"] == []

    def test_a_live_pass_survives_a_window_full_of_expired_rows(self, monkeypatch):
        """Task #1449: the live/expired split runs *after* the query, so the
        caller's LIMIT used to truncate live passes out of the response.

        `approval_pending()` read the approved bucket as
        `... WHERE status='approved' ORDER BY id LIMIT ?` and only then split
        live from expired in Python. With enough old approved rows the OLDEST
        fill the window and the live pass — the only row an operator can act on
        — never reaches the caller. Measured on the live hub: `limit=25`
        returned no live pass while 43 inert ones sat in the table; only the
        default `limit=100` surfaced them.

        Ordering by grant time fixes it: expiry is `granted_at + TTL`, so a
        live pass can never be older than an expired one, and truncation can
        only drop inert rows.
        """
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        for hours in (200, 150, 100):
            stale_id, _ = _queue_and_approve(monkeypatch, command=f"echo stale {hours}")
            _age_approval(stale_id, hours=hours)
        live_id, _ = _queue_and_approve(monkeypatch, command="echo live")
        _age_approval(live_id, hours=1)

        res = approval_pending(limit=2)
        assert [r["id"] for r in res["approved_unconsumed"]] == [live_id], (
            "a live pass must never be truncated out by older inert rows"
        )
        assert len(res["expired_passes"]) == 2, "the bucket is still capped"

    def test_a_live_pass_beyond_any_scan_bound_still_surfaces(self, monkeypatch):
        """Devin review, PR #803: a finite *scan* bound hides old live grants.

        An intermediate revision of the #1449 fix read the newest 2,000 approved
        rows and only then split live from expired. With 2,001 live grants the
        oldest is excluded even though it is still approved and consumable by
        check_approval_gate — the queue simply cannot show it. Classifying
        before bounding removes the failure mode outright.

        This is a guard against reintroducing such a bound, not a reproduction
        of the #1449 LIMIT bug (that is the test above): it fails only if a
        finite pre-split bound comes back, which is why the grant count here is
        deliberately past the 2,000 the review named.
        """
        monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "24")
        oldest_id, _ = _queue_and_approve(monkeypatch, command="echo oldest")
        now_ts = datetime.now(UTC).isoformat()
        conn = storage.connect()
        try:
            conn.executemany(
                "INSERT INTO approvals (tool, payload, requested_by, status, "
                "created_at, updated_at, resolved_at) "
                "VALUES (?, ?, ?, 'approved', ?, ?, ?)",
                [
                    (
                        "run_cmd",
                        json.dumps({"command": f"echo filler {i}"}),
                        "Desktop_agent3",
                        now_ts,
                        now_ts,
                        now_ts,
                    )
                    for i in range(2100)
                ],
            )
            conn.commit()
        finally:
            conn.close()

        res = approval_pending(limit=500)
        ids = [r["id"] for r in res["approved_unconsumed"]]
        assert oldest_id in ids, "the oldest live grant must not be pushed out by a bound"
        assert ids[0] == oldest_id, "oldest-first ordering is preserved"
        assert len(ids) == 500, "the caller's limit still bounds the returned list"

    def test_a_bulky_expired_payload_is_summarised_not_reproduced(self, monkeypatch):
        """Delegation payloads carry a full multi-KB task_spec. Fourteen of
        them dominated one response while authorizing nothing."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        huge = "x" * 5000
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": huge, "cwd": "."})
        approval_id = approval_pending()["pending"][0]["id"]
        approval_resolve(approval_id, "approve", resolved_by="Operator")
        _age_approval(approval_id, hours=200)

        row = approval_pending()["expired_passes"][0]
        assert len(row["payload"]["command"]) < 400
        assert "5000 chars, elided" in row["payload"]["command"]
        assert row["payload"]["cwd"] == ".", "short fields pass through untouched"

    def test_a_pending_request_is_never_elided(self, monkeypatch):
        """Elision is only for inert rows. A request awaiting a decision must
        be readable in full — that is the thing the human is judging."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        huge = "y" * 5000
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": huge})
        assert approval_pending()["pending"][0]["payload"]["command"] == huge

    def test_the_two_live_2026_07_19_passes_are_revocable(self, monkeypatch):
        """The concrete rows that motivated #171: run_cmd passes approved
        2026-07-19 and never consumed, inert only because UA_APPROVAL_TOOLS was
        empty. Both go live the moment run_cmd is gated again."""
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        ids = []
        for command in ("python3 scripts/hook_pre_push.py", "pytest"):
            approval_id, _ = _queue_and_approve(monkeypatch, command=command)
            _age_approval(approval_id, hours=168)  # 7 days, as they were
            ids.append(approval_id)

        for approval_id in ids:
            assert (
                approval_resolve(approval_id, "revoke", resolved_by="Operator")["status"]
                == "revoked"
            )

        assert approval_pending()["approved_unconsumed"] == []


class TestApprovalRequestEvents:
    @pytest.mark.parametrize("verified", ["Desktop_Agent2", None])
    def test_structured_request_uses_verified_identity_without_payload(self, monkeypatch, verified):
        monkeypatch.setenv("UA_EVENT_BUS", "1")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        payload = {"command": "synthetic-sensitive-command", "token": "fixture-secret-value"}
        token = config.agent_identity_var.set(verified)
        try:
            result = check_approval_gate(
                "run_cmd", payload, requested_by="claimed-peer", request_only=True
            )
        finally:
            config.agent_identity_var.reset(token)
        emitted = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])
        assert len(emitted) == 1
        assert emitted[0]["payload"] == {
            "kind": "approval",
            "ref_id": str(result["approval_id"]),
            "summary": "queued: run_cmd",
            "tool": "run_cmd",
            "requested_by": verified,
            "run_id": None,
        }
        assert emitted[0]["originator"] == verified
        conn = storage.connect()
        try:
            activity = conn.execute(
                "SELECT agent FROM agent_events WHERE kind='approval'"
            ).fetchone()
            assert activity["agent"] == verified
        finally:
            conn.close()
        assert "fixture-secret-value" not in json.dumps(emitted)
        assert "synthetic-sensitive-command" not in json.dumps(emitted)
        # The event is only a hint: authoritative detail still comes from the queue.
        row = approval_pending()["pending"][0]
        assert row["payload"] == payload
        assert row["status"] == "pending"
        assert row["requested_by"] == "claimed-peer"  # legacy queue contract
        assert events.verify_event_chain()["verified"] is True
        # The same requester retrying reuses its own row instead of adding one.
        check_approval_gate("run_cmd", payload, requested_by="claimed-peer", request_only=True)
        assert len(events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])) == 1

    @pytest.mark.parametrize(
        "tool",
        [
            "evidence_event.append",
            "acp_permission",
            "autonomous_cycle:merge",
            "autonomous_cycle:deploy",
        ],
    )
    def test_internal_tool_identifiers_are_supported(self, monkeypatch, tool):
        monkeypatch.setenv("UA_EVENT_BUS", "1")
        check_approval_gate(tool, {}, force=True, request_only=True)
        emitted = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])
        assert emitted[0]["payload"]["tool"] == tool

    @pytest.mark.parametrize(
        "tool", ["run_cmd\nfixture-secret-value", "https://secret.invalid", "x" * 129, ""]
    )
    def test_unsafe_tool_text_is_not_copied_into_notification(self, monkeypatch, tool):
        monkeypatch.setenv("UA_EVENT_BUS", "1")
        check_approval_gate(tool, {}, force=True, request_only=True)
        emitted = events.query_events(channels=[events.CHANNEL_APPROVAL_REQUESTED])
        assert emitted[0]["payload"]["tool"] is None
        assert emitted[0]["payload"]["summary"] == "queued"
        assert emitted[0]["payload"]["requested_by"] is None
        conn = storage.connect()
        try:
            assert (
                conn.execute("SELECT summary FROM agent_events").fetchone()["summary"] == "queued"
            )
        finally:
            conn.close()


class TestResolutionOnChainedLedger:
    """Task #1909: a resolution is recorded on the hash-chained event_log in the
    same transaction as the status change. Before this it reached the chain
    only through the best-effort agent_events mirror, so a resolution could
    commit with no ledger record at all."""

    def _queue(self, monkeypatch, command="echo ledger"):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", {"command": command}, requested_by="Desktop_agent3")
        return approval_pending()["pending"][0]["id"]

    def _resolution_events(self):
        return events.query_events(channels=["agent.approval.resolved"], limit=100)

    def test_approve_appends_chained_resolution_event(self, monkeypatch):
        import hashlib

        rid = self._queue(monkeypatch)
        row = approval_resolve(rid, "approve", resolved_by="Operator")
        evs = self._resolution_events()
        assert len(evs) == 1
        ev = evs[0]
        payload = ev["payload"]
        assert payload["approval_id"] == rid
        assert payload["decision"] == "approve"
        assert payload["status"] == "approved"
        assert payload["prior_status"] == "pending"
        assert payload["tool"] == "run_cmd"
        assert payload["resolved_by"] == "Operator"
        assert payload["resolver_verified"] is None  # unauthenticated test caller
        assert payload["resolved_at"] == row["resolved_at"]
        expected = hashlib.sha256(
            json.dumps({"command": "echo ledger"}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert payload["payload_sha256"] == expected
        assert ev["entry_hash"]
        assert events.verify_event_chain()["status"] == "valid"

    def test_verified_resolver_is_recorded(self, monkeypatch):
        rid = self._queue(monkeypatch)
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            approval_resolve(rid, "deny", reason="no")
        finally:
            config.agent_identity_var.reset(token)
        ev = self._resolution_events()[0]
        assert ev["payload"]["resolver_verified"] == "Desktop_Agent1"
        assert ev["payload"]["resolved_by"] == "Desktop_Agent1"
        assert ev["originator"] == "Desktop_Agent1"
        assert "reason" not in ev["payload"]  # free text stays off the ledger

    def test_revoke_appends_its_own_event(self, monkeypatch):
        rid = self._queue(monkeypatch)
        approval_resolve(rid, "approve", resolved_by="Operator")
        approval_resolve(rid, "revoke")
        statuses = [e["payload"]["status"] for e in self._resolution_events()]
        assert sorted(statuses) == ["approved", "revoked"]

    def test_ledger_append_failure_rolls_back_status_change(self, monkeypatch):
        rid = self._queue(monkeypatch)
        original = events.record_event_once_on_connection

        def boom(*args, **kwargs):
            raise RuntimeError("injected ledger failure")

        monkeypatch.setattr(events, "record_event_once_on_connection", boom)
        with pytest.raises(RuntimeError, match="injected ledger failure"):
            approval_resolve(rid, "approve", resolved_by="Operator")
        monkeypatch.setattr(events, "record_event_once_on_connection", original)
        assert _status_of(rid) == "pending"
        assert self._resolution_events() == []
        # The rolled-back request can still be resolved normally afterwards.
        assert approval_resolve(rid, "approve", resolved_by="Operator")["status"] == "approved"
        assert len(self._resolution_events()) == 1

    def test_agent_event_row_still_written(self, monkeypatch):
        rid = self._queue(monkeypatch)
        approval_resolve(rid, "deny", resolved_by="Operator")
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT summary FROM agent_events WHERE kind='approval' AND ref_id=? "
                "ORDER BY id DESC LIMIT 1",
                (str(rid),),
            ).fetchone()
        finally:
            conn.close()
        assert row["summary"] == "denied: run_cmd"


class TestOperatorSelfApproval:
    """Task #2500: the operator's own write for a named tool is a recorded pass.

    On a one-human hub an approval needs a different person, so a write the
    operator makes from their own browser could never be approved. For the two
    actions that dead-ended that way the operator's own verified submission is
    recorded and spent instead. The pass stays one-shot, keeps its audit row and
    ledger event, and is unreachable from an agent credential.
    """

    def _self_approval_events(self) -> list[dict]:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT payload, originator FROM event_log WHERE channel=? "
                "ORDER BY created_at DESC",
                ("agent.approval.self_approval",),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"payload": json.loads(row["payload"]), "originator": row["originator"]} for row in rows
        ]

    def test_operator_self_approval_passes_and_is_recorded(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        result = check_approval_gate(
            "task_delete", {"task_id": 7}, requested_by="Operator", force=True
        )
        assert result["gated"] is True
        assert result["approval_id"] > 0
        # The row is the audit trail: approved by the operator himself, with the
        # reason written down and the pass spent by this call.
        row = get_approval(result["approval_id"])
        assert row["status"] == "consumed"
        assert row["resolved_by"] == "Operator"
        assert "operator self-approval" in row["reason"]
        # Nothing is left waiting for a person who does not exist.
        assert approval_pending()["pending"] == []
        # The ledger names the tool, the operator and the reason.
        recorded = self._self_approval_events()
        assert len(recorded) == 1
        assert recorded[0]["payload"]["tool"] == "task_delete"
        assert recorded[0]["payload"]["operator"] == "Operator"
        assert "task #2500" in recorded[0]["payload"]["reason"]

    def test_another_connection_cannot_spend_the_operators_minted_pass(self, monkeypatch):
        # Pause at SQLite's consumption boundary, not a fake gate: a second
        # connection runs the real agent gate if the minted row is visible.
        connect = storage.connect
        observed = []
        stolen = []
        probe_errors = []

        def at_consumption(sql):
            if not sql.startswith("UPDATE approvals SET status='consumed'"):
                return
            try:
                observer = connect()
                try:
                    visible = observer.execute(
                        "SELECT id FROM approvals WHERE tool='task_delete' AND status='approved'"
                    ).fetchall()
                finally:
                    observer.close()
                observed.append([row["id"] for row in visible])
                if visible:
                    identity = config.agent_identity_var.set("Desktop_Agent2")
                    try:
                        stolen.append(
                            check_approval_gate(
                                "task_delete",
                                {"task_id": 7},
                                requested_by="Desktop_Agent2",
                                force=True,
                            )
                        )
                    finally:
                        config.agent_identity_var.reset(identity)
            except Exception as exc:
                # sqlite trace callbacks suppress exceptions; surface any probe
                # failure in the test rather than mistaking it for isolation.
                probe_errors.append(exc)

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            if config.verified_agent_identity() == "Desktop_Person":
                conn.set_trace_callback(at_consumption)
            return conn

        monkeypatch.setattr(storage, "connect", traced_connect)
        identity = config.agent_identity_var.set("Desktop_Person")
        try:
            try:
                result = check_approval_gate(
                    "task_delete", {"task_id": 7}, requested_by="Desktop_Person", force=True
                )
            except ApprovalPendingError:
                result = None
        finally:
            config.agent_identity_var.reset(identity)

        assert probe_errors == []
        assert observed == [[]], f"minted pass exposed to another connection: {observed}"
        assert stolen == []
        assert result is not None
        assert get_approval(result["approval_id"])["status"] == "consumed"
        assert len(self._self_approval_events()) == 1
        identity = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(ApprovalPendingError):
                check_approval_gate(
                    "task_delete", {"task_id": 7}, requested_by="Desktop_Agent2", force=True
                )
        finally:
            config.agent_identity_var.reset(identity)

    def test_the_self_approved_pass_is_one_shot(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        first = check_approval_gate(
            "task_delete", {"task_id": 7}, requested_by="Operator", force=True
        )
        second = check_approval_gate(
            "task_delete", {"task_id": 7}, requested_by="Operator", force=True
        )
        assert first["approval_id"] != second["approval_id"]
        assert get_approval(first["approval_id"])["status"] == "consumed"
        assert get_approval(second["approval_id"])["status"] == "consumed"

    def test_langgraph_governed_call_is_covered(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        result = check_approval_gate(
            "langgraph_governed_call", {"graph": "g1"}, requested_by="Operator", force=True
        )
        assert result["gated"] is True

    def test_an_agent_cannot_self_approve(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate(
                "task_delete", {"task_id": 7}, requested_by="Desktop_Agent2", force=True
            )
        assert len(approval_pending()["pending"]) == 1
        assert self._self_approval_events() == []

    def test_naming_the_operator_does_not_help_an_agent(self, monkeypatch):
        # The pass is keyed off the transport-verified identity, never the label
        # a caller writes into requested_by.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("task_delete", {"task_id": 7}, requested_by="Operator", force=True)
        assert self._self_approval_events() == []

    def test_the_operator_is_not_self_approved_for_an_unlisted_tool(self, monkeypatch):
        # Separation of duties stays for production release and config changes.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate(
                "work_product_release", {"path": "out.pdf"}, requested_by="Operator", force=True
            )
        assert self._self_approval_events() == []

    def test_request_only_never_self_approves(self, monkeypatch):
        # A route that only queues a request must still queue it.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        result = check_approval_gate(
            "task_delete", {"task_id": 7}, requested_by="Operator", force=True, request_only=True
        )
        assert result["status"] == "pending"


class TestPassBoundToRequester:
    """#2492: a one-shot pass belongs to the requester that queued it.

    The gate matched an approved row on tool, payload and tenant alone, so a
    second eligible caller making the identical call spent the first caller's
    pass. Nothing here re-implements a guard: the pass keeps its one-shot CAS,
    its TTL and its human resolver, and gains the requester it was asked for.
    """

    def _park_and_approve(self, monkeypatch, who: str | None, command: str = "echo hi"):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        payload = {"command": command}
        with pytest.raises(ApprovalPendingError) as parked:
            check_approval_gate("run_cmd", payload, requested_by=who)
        approval_id = parked.value.approval_id
        # Resolve as the anonymous harness identity, the way a human's own
        # console does; the tests set the consuming identity afterwards.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
        approval_resolve(approval_id, "approve", resolved_by="Operator")
        return approval_id, payload

    def test_another_verified_caller_cannot_spend_the_pass(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")

        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        with pytest.raises(ApprovalPendingError) as parked:
            check_approval_gate("run_cmd", payload, requested_by="Desktop_Agent2")

        assert parked.value.approval_id != approval_id
        # A's pass is neither spent nor retired by B's attempt.
        assert get_approval(approval_id)["status"] == "approved"
        assert [row["requested_by"] for row in approval_pending()["pending"]] == ["Desktop_Agent2"]

    def test_the_requester_still_spends_its_own_pass(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")
        assert check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3") == {
            "gated": True,
            "approval_id": approval_id,
        }
        assert get_approval(approval_id)["status"] == "consumed"

    def test_an_unidentified_caller_cannot_spend_a_named_pass(self, monkeypatch):
        monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload)
        assert get_approval(approval_id)["status"] == "approved"

    def test_a_row_that_named_nobody_stays_unbound(self, monkeypatch):
        # The in-process path where the tool supplied no requester label: there
        # is nobody to bind to, so the pass stays spendable. The registered
        # wrapper always names the verified identity, so this is not the shape
        # a gated tool call has on the hub.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
        approval_id, payload = self._park_and_approve(monkeypatch, None)
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        assert check_approval_gate("run_cmd", payload, requested_by="Desktop_Agent2") == {
            "gated": True,
            "approval_id": approval_id,
        }

    def test_the_resolving_human_can_spend_the_pass(self, monkeypatch):
        # The human-in-the-loop shape: an agent asks, a person resolves, and
        # that person's own session spends the pass (elicitation, the GitHub
        # App check, Control Center writes).
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Operator")
        assert check_approval_gate("run_cmd", payload, requested_by="Operator") == {
            "gated": True,
            "approval_id": approval_id,
        }
        assert get_approval(approval_id)["status"] == "consumed"

    def test_an_on_behalf_of_spend_is_explicit(self, monkeypatch):
        # The org-proposal apply: the pass names the proposer and was resolved
        # by one human, while a different caller applies it. Only the explicit
        # on_behalf_of names the pass's own requester; otherwise refused.
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        with pytest.raises(ApprovalPendingError):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_Agent2")
        assert check_approval_gate(
            "run_cmd", payload, requested_by="Desktop_Agent2", on_behalf_of="Desktop_agent3"
        ) == {"gated": True, "approval_id": approval_id}
        assert get_approval(approval_id)["status"] == "consumed"

    def test_a_denial_still_stops_every_identical_call(self, monkeypatch):
        # Denials stay action-scoped on purpose: the human refused that exact
        # call, so another caller asking the same thing is refused too rather
        # than re-queued. Only positive authority is requester-bound.
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        payload = {"command": "echo hi"}
        with pytest.raises(ApprovalPendingError) as parked:
            check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
        approval_resolve(
            parked.value.approval_id, "deny", resolved_by="Operator", reason="not today"
        )
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        with pytest.raises(ToolError, match="DENIED"):
            check_approval_gate("run_cmd", payload, requested_by="Desktop_Agent2")

    def test_the_consumption_cas_pins_the_row_requester(self, monkeypatch):
        """A row re-pointed at another requester after selection is not spent."""
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        approval_id, payload = self._park_and_approve(monkeypatch, "Desktop_agent3")
        canon = approval_tools._canonical_payload(payload)
        conn = storage.connect()
        try:
            selected = dict(
                conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            )
            conn.execute(
                "UPDATE approvals SET requested_by='Desktop_Agent2' WHERE id=?", (approval_id,)
            )
            conn.commit()
            spent = approval_tools._consume_approved(
                conn,
                "run_cmd",
                canon,
                selected,
                "Desktop_agent3",
                None,
                None,
                {},
                ("run_cmd", canon, None),
            )
        finally:
            conn.close()
        assert spent is None
        assert get_approval(approval_id)["status"] == "approved"

    def test_the_invocation_cache_key_is_caller_scoped(self, monkeypatch):
        payload = {"command": "echo hi"}
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_agent3")
        agy = approval_tools._receipt_key("run_cmd", payload, None)
        monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
        assert agy != approval_tools._receipt_key("run_cmd", payload, None)

    def test_recovery_never_returns_another_callers_consumption(self):
        """Recovery reads this caller's own consumption event, not merely the grant."""
        binding = {
            "actor": "Desktop_Agent2",
            "scope_key": "task-2492-recovery",
            "digest": "digest",
            "org_id": None,
            "subject": "agent:Desktop_Agent2",
        }
        conn = storage.connect()
        try:
            events.record_event(
                approval_tools._CONSUMPTION_CHANNEL,
                payload={
                    "approval_id": 424242,
                    "tool": "task_update",
                    "binding": binding,
                    "generation": "g",
                    "grant": {},
                },
                originator="Desktop_agent3",
            )
            receipts: dict = {}
            with approval_tools.selected_task_binding(
                lambda: binding, lambda arguments: "digest", lambda connection: None
            ):
                recovered = approval_tools._recover_task_consumption(
                    conn,
                    "task_update",
                    "{}",
                    ("task_update", "{}", (None, "Desktop_Agent2")),
                    receipts,
                    {"active": True, "cache": {}},
                )
        finally:
            conn.close()
        assert recovered is None
        assert receipts == {}
