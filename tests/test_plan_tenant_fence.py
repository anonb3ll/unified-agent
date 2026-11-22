"""Plan reads are tenant-fenced (task #2042).

#2014 stamped ``plans.org_id``; the reads stayed open. A tenanted caller could
get, list, update, delete or checkpoint another tenant's plan, read it through
the platform reader, link a comment to it, and learn of it through the joint
planning checks and the governed-workflow sweep. A foreign plan must now read
exactly like an unknown one. Checkpoints carry no tenant and are read through
their plan.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from org_test_helpers import installation_org as installation_org

from unified_agent import platform_readers, storage
from unified_agent.goals import goal_add
from unified_agent.task_comments import append_comment
from unified_agent.tools import plan as plan_tools
from unified_agent.tools.tasks import task_add

ORG_A = "org_a"
ORG_B = "org_b"
AUTHOR = "Desktop_Agent4"
OFFICE = "product-engineering"
RUN_ID = "01K0000000000000000000000A"


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


def _plan(goal: str = "Tenant A secret plan") -> str:
    return plan_tools.plan_create(goal, ["inspect", "ship"], author=AUTHOR)["plan_id"]


def _raw(plan_id: str):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        conn.close()


def _bind_joint(plan_id: str, goal_id: int) -> None:
    """A joint binding checkpoint, written directly as a pre-fence hub could have."""
    joint = {"revision": 0, "goal_id": goal_id, "org_unit_id": OFFICE, "actor": AUTHOR}
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
            "VALUES (?, 1, ?, 'joint binding', ?)",
            (plan_id, json.dumps({"joint": joint}), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


UNKNOWN = "no plan with id"

FOREIGN_CALLS = [
    ("get", lambda p: plan_tools.plan_get(p)),
    ("update", lambda p: plan_tools.plan_update(p, 0, status="done")),
    ("checkpoint", lambda p: plan_tools.plan_checkpoint(p, note="peek")),
    ("resume", lambda p: plan_tools.plan_resume(p)),
    ("resume_seq", lambda p: plan_tools.plan_resume(p, from_checkpoint=1)),
    ("retry", lambda p: plan_tools.plan_step_retry(p, 0, "again")),
    ("claim", lambda p: plan_tools.plan_step_claim(p, 0, AUTHOR)),
    ("handoff", lambda p: plan_tools.plan_handoff(p, 0, agent=AUTHOR)),
    ("close", lambda p: plan_tools.plan_close(p, "done elsewhere")),
    ("manage_get", lambda p: plan_tools.plan_manage("get", {"plan_id": p})),
]


@pytest.mark.parametrize(("name", "call"), FOREIGN_CALLS, ids=[n for n, _ in FOREIGN_CALLS])
def test_foreign_plan_reads_like_an_unknown_plan(as_org, name, call):
    as_org(ORG_A)
    a_plan = _plan()
    plan_tools.plan_checkpoint(a_plan, note="tenant A context")
    before = dict(_raw(a_plan))
    as_org(ORG_B)
    with pytest.raises(ToolError) as foreign:
        call(a_plan)
    with pytest.raises(ToolError) as unknown:
        call("feedfeedfeed")
    assert UNKNOWN in str(foreign.value)
    assert str(foreign.value).replace(a_plan, "X") == str(unknown.value).replace(
        "feedfeedfeed", "X"
    )
    assert dict(_raw(a_plan)) == before


def test_foreign_plan_delete_is_a_noop_like_an_unknown_plan(as_org):
    as_org(ORG_A)
    a_plan = _plan()
    as_org(ORG_B)
    assert plan_tools.plan_delete(a_plan) == {"deleted": False}
    assert plan_tools.plan_delete("feedfeedfeed") == {"deleted": False}
    assert _raw(a_plan) is not None
    as_org(ORG_A)
    assert plan_tools.plan_delete(a_plan) == {"deleted": True}


def test_list_shows_only_own_tenant_plans(as_org):
    as_org(ORG_A)
    a_plan = _plan()
    as_org(ORG_B)
    b_plan = _plan("Tenant B plan")
    listed = {p["id"] for p in plan_tools.plan_list(status="all")["plans"]}
    assert listed == {b_plan}
    assert a_plan not in {p["id"] for p in plan_tools.plan_list()["plans"]}


def test_platform_reader_hides_a_foreign_plan(as_org):
    as_org(ORG_A)
    a_plan = _plan()
    assert platform_readers.read_plan(a_plan)["plan"]["id"] == a_plan
    as_org(ORG_B)
    with pytest.raises(ResourceNotFoundError, match=f"unknown plan: {a_plan}"):
        platform_readers.read_plan(a_plan)


def test_comment_cannot_reference_a_foreign_plan(as_org):
    as_org(ORG_A)
    a_plan = _plan()
    as_org(ORG_B)
    task_id = int(task_add(title="Tenant B thread")["id"])
    with pytest.raises(ToolError, match=f"unknown plan: {a_plan}"):
        append_comment(task_id, "see their plan", author=AUTHOR, refs={"plan_id": a_plan})
    b_plan = _plan("Tenant B plan")
    posted = append_comment(task_id, "see my plan", author=AUTHOR, refs={"plan_id": b_plan})
    assert posted["comment"]["refs"] == {"plan_id": b_plan}


@pytest.mark.usefixtures("installation_org")
def test_foreign_joint_plan_does_not_leak_through_the_membership_check(as_org):
    as_org(ORG_A)
    a_goal = int(goal_add("Tenant A goal", author=AUTHOR)["goal"]["id"])
    a_plan = _plan()
    _bind_joint(a_plan, a_goal)
    with pytest.raises(ToolError, match="is a joint plan"):
        plan_tools.plan_resume(a_plan)  # the binding is live for its own tenant
    as_org(ORG_B)
    # Before the fence the joint checkpoint answered first, naming the org unit.
    for call in (plan_tools.plan_resume, plan_tools.plan_delete):
        try:
            result = call(a_plan)
        except ToolError as exc:
            assert UNKNOWN in str(exc) and OFFICE not in str(exc)
        else:
            assert result == {"deleted": False}
    for call in (plan_tools.plan_joint_get, plan_tools.plan_joint_recover):
        with pytest.raises(ToolError, match=UNKNOWN):
            call(a_plan, author=AUTHOR)
    assert _raw(a_plan) is not None


@pytest.mark.usefixtures("installation_org")
def test_foreign_joint_plan_cannot_be_bound_or_block_a_binding(as_org):
    as_org(ORG_B)
    b_goal = int(goal_add("Tenant B goal", author=AUTHOR)["goal"]["id"])
    as_org(ORG_A)
    a_plan = _plan()
    # A pre-fence binding of tenant A's plan to tenant B's goal.
    _bind_joint(a_plan, b_goal)
    as_org(ORG_B)
    task_id = int(task_add(title="Tenant B thread")["id"])
    comment = int(append_comment(task_id, "bind", author=AUTHOR)["comment"]["id"])
    with pytest.raises(ToolError, match=UNKNOWN):
        plan_tools.plan_joint_bind(a_plan, b_goal, OFFICE, comment_id=comment, author=AUTHOR)
    # Nor does the foreign plan read as "goal already has active joint plan <id>".
    b_plan = _plan("Tenant B plan")
    bound = plan_tools.plan_joint_bind(b_plan, b_goal, OFFICE, comment_id=comment, author=AUTHOR)
    assert bound["plan_id"] == b_plan


def test_workflow_sweep_only_sees_own_tenant_plans(as_org, tmp_path):
    from unified_agent.langgraph_workflow import PLAN_GOAL_PREFIX, close_expired_workflow_plans

    missing = tmp_path / "workflow.db"
    as_org(ORG_A)
    a_plan = _plan(PLAN_GOAL_PREFIX + RUN_ID)
    as_org(ORG_B)
    assert close_expired_workflow_plans(missing)["checked"] == 0
    as_org(ORG_A)
    swept = close_expired_workflow_plans(missing)
    assert swept["checked"] == 1
    assert [u["plan_id"] for u in swept["unverifiable"]] == [a_plan]


# --- untenanted hub ---------------------------------------------------------------


def test_untenanted_hub_plans_work_end_to_end(untenanted):
    plan_id = _plan("Private plan")
    assert _raw(plan_id)["org_id"] is None
    plan_tools.plan_checkpoint(plan_id, note="ctx")
    assert plan_tools.plan_get(plan_id)["plan"]["id"] == plan_id
    assert plan_tools.plan_resume(plan_id)["checkpoint"]["seq"] == 1
    assert plan_tools.plan_update(plan_id, 0, status="done")["step"]["status"] == "done"
    assert plan_id in {p["id"] for p in plan_tools.plan_list(status="all")["plans"]}
    assert platform_readers.read_plan(plan_id)["plan"]["id"] == plan_id
    task_id = int(task_add(title="Private thread")["id"])
    posted = append_comment(task_id, "see plan", author=AUTHOR, refs={"plan_id": plan_id})
    assert posted["comment"]["refs"] == {"plan_id": plan_id}
    assert plan_tools.plan_delete(plan_id) == {"deleted": True}


def test_untenanted_hub_does_not_see_a_tenanted_plan(as_org, monkeypatch):
    as_org(ORG_A)
    a_plan = _plan()
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    with pytest.raises(ToolError, match=UNKNOWN):
        plan_tools.plan_get(a_plan)
    assert a_plan not in {p["id"] for p in plan_tools.plan_list(status="all")["plans"]}
