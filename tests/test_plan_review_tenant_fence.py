"""plan_review only reads and rewrites its own plan's tenant's tasks (task #2045).

#2042 fenced the plan read, but the gate lookup behind ``plan_review`` and
``plan_preview`` matched tasks by the plan id in their notes across every tenant,
and the notes rewrite was keyed by task id alone. A tenant-B task naming tenant
A's plan id could be approved, marked changes-requested, block A's review as a
second gate, or flip A's preview to approved. The task reads and the rewrite are
now fenced on the plan row's tenant.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.reviewer import WAITING_FOR_PLAN_APPROVAL
from unified_agent.tools import plan as plan_tools
from unified_agent.tools.tasks import task_add, task_update

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


@pytest.fixture
def untenanted(monkeypatch):
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)


def _gate_task(plan_id: str, marker: str = WAITING_FOR_PLAN_APPROVAL) -> int:
    task_id = int(task_add("approve-my-plan", notes=f"{marker}\nplan: {plan_id}")["id"])
    task_update(task_id, status="doing", worktree=False)
    return task_id


def _notes(task_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()["notes"]
    finally:
        conn.close()


def _review(action: str, plan_id: str, task_id: int | None = None) -> dict:
    return plan_tools.plan_review(f"plan-review v1 {action} {plan_id}", task_id)


def _two_tenants(as_org) -> tuple[str, int, int]:
    """Tenant A's plan and gate task, plus a tenant-B gate task naming A's plan."""
    as_org(ORG_A)
    plan_id = plan_tools.plan_create("Tenant A plan", ["a"])["plan_id"]
    a_task = _gate_task(plan_id)
    as_org(ORG_B)
    b_task = _gate_task(plan_id)
    as_org(ORG_A)
    return plan_id, a_task, b_task


@pytest.mark.parametrize("action", ["approve", "changes"])
def test_review_cannot_rewrite_a_foreign_task_named_by_id(as_org, action):
    plan_id, _, b_task = _two_tenants(as_org)
    before = _notes(b_task)
    with pytest.raises(ToolError, match="waiting-for: plan-approval"):
        _review(action, plan_id, b_task)
    assert _notes(b_task) == before


@pytest.mark.parametrize("action", ["approve", "changes"])
def test_foreign_task_is_not_a_second_gate(as_org, action):
    plan_id, a_task, b_task = _two_tenants(as_org)
    before = _notes(b_task)
    out = _review(action, plan_id)  # pre-fence: "has 2 tasks waiting ... pass task_id"
    assert out["applied"] is True and int(out["task_id"]) == a_task
    assert _notes(b_task) == before
    assert _notes(a_task) != f"{WAITING_FOR_PLAN_APPROVAL}\nplan: {plan_id}"


def test_foreign_approved_task_does_not_mark_the_preview_approved(as_org):
    as_org(ORG_A)
    plan_id = plan_tools.plan_create("Tenant A plan", ["a"])["plan_id"]
    as_org(ORG_B)
    _gate_task(plan_id, marker="[plan: approved @ 2026-01-01]")
    as_org(ORG_A)
    assert plan_tools.plan_preview(plan_id)["approved"] is False


def test_same_tenant_review_is_unchanged(as_org):
    as_org(ORG_A)
    plan_id = plan_tools.plan_create("Tenant A plan", ["a"])["plan_id"]
    task_id = _gate_task(plan_id)
    out = _review("approve", plan_id, task_id)
    assert out["applied"] is True and out["approved"] is True
    notes = _notes(task_id)
    assert WAITING_FOR_PLAN_APPROVAL not in notes and "[plan: approved @" in notes
    assert _review("approve", plan_id)["already"] is True
    assert plan_tools.plan_preview(plan_id)["approved"] is True


def test_untenanted_hub_review_works_and_skips_tenanted_tasks(untenanted, monkeypatch):
    plan_id = plan_tools.plan_create("Private plan", ["a"])["plan_id"]
    task_id = _gate_task(plan_id)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", ORG_A)
    tenant_task = _gate_task(plan_id)
    before = _notes(tenant_task)
    monkeypatch.delenv("UA_EDITION")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    with pytest.raises(ToolError, match="waiting-for: plan-approval"):
        _review("approve", plan_id, tenant_task)
    out = _review("approve", plan_id)
    assert out["applied"] is True and int(out["task_id"]) == task_id
    assert "[plan: approved @" in _notes(task_id)
    assert _notes(tenant_task) == before


@pytest.mark.parametrize("action", ["approve", "changes"])
def test_review_keeps_a_note_appended_after_the_gate_read(untenanted, monkeypatch, action):
    """The notes rewrite re-reads under the writer lock; a concurrent append survives."""
    plan_id = plan_tools.plan_create("Raced plan", ["a"])["plan_id"]
    task_id = _gate_task(plan_id)
    snapshot = plan_tools._gate_tasks_for_plan

    def gate_then_concurrent_append(pid, org_id):
        rows = snapshot(pid, org_id)
        task_update(task_id, append_note="CONCURRENT-EVIDENCE", worktree=False)
        return rows

    monkeypatch.setattr(plan_tools, "_gate_tasks_for_plan", gate_then_concurrent_append)
    assert _review(action, plan_id, task_id)["applied"] is True
    assert "CONCURRENT-EVIDENCE" in _notes(task_id)
