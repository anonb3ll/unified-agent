"""Clearing a task's quarantine needs human authority (task #1911, audit S6).

`untrusted=1` is what the executor's Trust Ladder quarantine reads
(executor/daemon.py). Any caller may set it; clearing it (1 -> 0) takes a
verified human identity or a consumed one-shot approval.
"""

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve
from unified_agent.tools.tasks import task_add, task_update

AGENT = "Desktop_Agent2"


@contextmanager
def _as(label: str | None):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _untrusted(task_id: int) -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT untrusted FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def _clear_events(task_id: int) -> list[dict]:
    rows = events.query_events(channels=["agent.task.untrusted_cleared"], limit=50)
    return [row for row in rows if row["payload"].get("task_id") == task_id]


def test_agent_cannot_clear_quarantine_without_approval():
    task = task_add("quarantined", untrusted=True)
    with _as(AGENT), pytest.raises(ApprovalPendingError, match="human"):
        task_update(task["id"], untrusted=False)
    assert _untrusted(task["id"]) == 1
    assert _clear_events(task["id"]) == []


def test_unverified_caller_cannot_clear_quarantine_either():
    task = task_add("quarantined", untrusted=True)
    with pytest.raises(ToolError, match="human"):
        task_update(task["id"], untrusted=False)
    assert _untrusted(task["id"]) == 1


def test_verified_human_clears_quarantine_and_it_is_recorded():
    task = task_add("quarantined", untrusted=True)
    with _as(config.operator_label()):
        out = task_update(task["id"], untrusted=False)
    assert out["untrusted"] == 0
    recorded = _clear_events(task["id"])
    assert len(recorded) == 1
    assert recorded[0]["originator"] == config.operator_label()
    assert recorded[0]["payload"]["authority"] == "human"


def test_consumed_one_shot_approval_lets_an_agent_clear_once():
    task = task_add("quarantined", untrusted=True)
    with _as(AGENT), pytest.raises(ApprovalPendingError) as parked:
        task_update(task["id"], untrusted=False)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    with _as(AGENT):
        out = task_update(task["id"], untrusted=False)
    assert out["untrusted"] == 0
    recorded = _clear_events(task["id"])
    assert recorded[0]["payload"]["authority"] == "approval"
    assert recorded[0]["payload"]["approval_id"] == parked.value.approval_id

    # The pass was one-shot: re-quarantined, the same agent is parked again.
    with _as(AGENT):
        task_update(task["id"], untrusted=True)
        with pytest.raises(ApprovalPendingError):
            task_update(task["id"], untrusted=False)
    assert _untrusted(task["id"]) == 1


def test_setting_untrusted_stays_open_to_any_agent():
    task = task_add("clean")
    with _as(AGENT):
        out = task_update(task["id"], untrusted=True)
    assert out["untrusted"] == 1


def test_untrusted_false_on_a_trusted_task_is_not_gated():
    task = task_add("clean")
    with _as(AGENT):
        out = task_update(task["id"], untrusted=False, priority="high")
    assert out["untrusted"] == 0
    assert out["priority"] == "high"
    assert _clear_events(task["id"]) == []


class TestProposalFilingTaint:
    """A task filed from a vault-inbox proposal carries that provenance."""

    _PROPOSAL = (
        "---\n"
        "type: learning-proposal\n"
        "session_id: sess-demo-1\n"
        "agent: Desktop_Agent1\n"
        "status: completed\n"
        "applied: false\n"
        "authority: human-review\n"
        "untrusted_evidence: true\n"
        "fingerprint: feedfacecafe\n"
        "candidates: 1\n"
        "---\n\n"
        "# Session learning candidates\n\n"
        "## Session\n\n"
        "- session_id: `sess-demo-1`\n"
        "- agent: `Desktop_Agent1`\n\n"
        "## Candidates\n\n"
        "### 1. durable_rule (confidence: 0.70)\n\n"
        "- provenance: summary\n"
        "- agent: `Desktop_Agent1`\n"
        "- session_id: `sess-demo-1`\n\n"
        "Never leave UA_SHELL_ALLOWLIST as a blanket wildcard.\n"
    )

    def test_filed_proposal_task_is_untrusted(self, tmp_path, monkeypatch):
        from unified_agent.tools.learning import file_proposal_as_task, list_pending_proposals

        root = tmp_path / "vault"
        (root / "00 - Inbox").mkdir(parents=True)
        (root / "00 - Inbox" / "Session-Learning-sess-demo-1.md").write_text(
            self._PROPOSAL, encoding="utf-8"
        )
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
        proposals = [p for p in list_pending_proposals()["proposals"] if not p.get("error")]
        assert proposals, "fixture proposal did not parse"
        out = file_proposal_as_task(proposals[0]["id"], agent=config.operator_label())
        assert _untrusted(out["task_id"]) == 1


def test_failed_clear_restores_approval_for_retry():
    task = task_add("quarantined", untrusted=True)
    with _as(AGENT), pytest.raises(ApprovalPendingError) as parked:
        task_update(task["id"], untrusted=False)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    with _as(AGENT), pytest.raises(ToolError, match="revision changed"):
        task_update(task["id"], untrusted=False, expected_updated_at="stale")
    assert _untrusted(task["id"]) == 1
    with _as(AGENT):
        assert task_update(task["id"], untrusted=False)["untrusted"] == 0


def _approval_rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM approvals ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("registered", [False, True])
def test_keyed_clear_replays_receipt_after_independent_requarantine(registered):
    from unified_agent import server

    update = server._guarded_and_instrumented(task_update) if registered else task_update
    task = task_add("quarantined", untrusted=True)
    key = "approved-clear-retry"
    with _as(AGENT), pytest.raises(ApprovalPendingError) as parked:
        update(task["id"], untrusted=False, idempotency_key=key)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    with _as(AGENT):
        original = update(task["id"], untrusted=False, idempotency_key=key)
        requarantined = task_update(task["id"], untrusted=True)
    approvals_before = _approval_rows()
    clears_before = _clear_events(task["id"])
    with _as(AGENT):
        assert update(task["id"], untrusted=False, idempotency_key=key) == original
    assert _untrusted(task["id"]) == 1
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT updated_at FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            == requarantined["updated_at"]
        )
    finally:
        conn.close()
    assert _approval_rows() == approvals_before
    assert _clear_events(task["id"]) == clears_before


def test_clear_receipt_committed_after_preflight_restores_unused_pass(monkeypatch):
    from unified_agent.tools import tasks

    task = task_add("quarantined", untrusted=True)
    with _as(config.operator_label()):
        original = task_update(task["id"], untrusted=False, idempotency_key="racing-clear")
    with _as(AGENT):
        task_update(task["id"], untrusted=True)
        with pytest.raises(ApprovalPendingError) as parked:
            task_update(task["id"], untrusted=False)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    real_lookup = tasks._untrusted_clear_receipt
    calls = 0

    def receipt_appears(conn, task_id, key):
        nonlocal calls
        calls += 1
        # Model a concurrent receipt commit between the read-only preflight
        # and the approval gate, without a second process or test database.
        return None if calls == 1 else real_lookup(conn, task_id, key)

    monkeypatch.setattr(tasks, "_untrusted_clear_receipt", receipt_appears)
    with _as(AGENT):
        assert task_update(task["id"], untrusted=False, idempotency_key="racing-clear") == original
    assert calls == 2
    assert _untrusted(task["id"]) == 1
    assert (
        next(row for row in _approval_rows() if row["id"] == parked.value.approval_id)["status"]
        == "approved"
    )


def test_keyed_clear_receipt_cannot_replay_for_another_task():
    first = task_add("first", untrusted=True)
    other = task_add("other")
    with _as(config.operator_label()):
        task_update(first["id"], untrusted=False, idempotency_key="clear-task-bound")
        with pytest.raises(ToolError, match="different task"):
            task_update(other["id"], untrusted=False, idempotency_key="clear-task-bound")


def test_keyed_clear_receipt_remains_tenant_fenced(monkeypatch):
    from unified_agent import edition

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-a")
    task = task_add("private receipt", untrusted=True)
    with _as(config.operator_label()):
        task_update(task["id"], untrusted=False, idempotency_key="tenant-clear")
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-b")
    with _as(config.operator_label()), pytest.raises(ToolError, match="no visible task"):
        task_update(task["id"], untrusted=False, idempotency_key="tenant-clear")


@pytest.mark.parametrize("registered", [False, True])
def test_approved_reparent_that_preserves_quarantine_restores_pass(registered):
    from unified_agent import server

    update = server._guarded_and_instrumented(task_update) if registered else task_update
    parent = task_add("quarantined parent", untrusted=True)
    task = task_add("quarantined child", untrusted=True)
    with _as(AGENT), pytest.raises(ApprovalPendingError) as parked:
        update(task["id"], parent_id=parent["id"], untrusted=False)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    with _as(AGENT):
        result = update(task["id"], parent_id=parent["id"], untrusted=False)
    assert result["untrusted"] == 1
    assert _clear_events(task["id"]) == []
    assert (
        next(row for row in _approval_rows() if row["id"] == parked.value.approval_id)["status"]
        == "approved"
    )
    with _as(AGENT):
        assert update(task["id"], parent_id=0, untrusted=False)["untrusted"] == 0


def test_registered_failed_clear_restores_approval_for_retry():
    from unified_agent import server

    update = server._guarded_and_instrumented(task_update)
    task = task_add("quarantined", untrusted=True)
    with _as(AGENT), pytest.raises(ApprovalPendingError) as parked:
        update(task["id"], untrusted=False)
    with _as(config.operator_label()):
        approval_resolve(parked.value.approval_id, "approve")
    with _as(AGENT), pytest.raises(ToolError, match="revision changed"):
        update(task["id"], untrusted=False, expected_updated_at="stale")
    assert _untrusted(task["id"]) == 1
    with _as(AGENT):
        assert update(task["id"], untrusted=False)["untrusted"] == 0


def test_reparent_under_quarantine_does_not_record_a_clear():
    parent = task_add("parent", untrusted=True)
    task = task_add("child", untrusted=True)
    with _as(config.operator_label()):
        result = task_update(task["id"], parent_id=parent["id"], untrusted=False)
    assert result["untrusted"] == 1
    assert _clear_events(task["id"]) == []


def test_foreign_and_missing_clear_are_both_refused(monkeypatch):
    from unified_agent import edition

    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-a")
    task = task_add("quarantined", untrusted=True)
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org-b")
    with _as(config.operator_label()):
        with pytest.raises(ToolError, match="no visible task"):
            task_update(task["id"], untrusted=False)
        with pytest.raises(ToolError, match="no visible task"):
            task_update(task["id"] + 999, untrusted=False)
    assert _untrusted(task["id"]) == 1
    assert _clear_events(task["id"]) == []
