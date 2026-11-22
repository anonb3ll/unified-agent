"""Public-path tenant rejection precedes approvals and preserves run authority."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_operating_profiles import (
    _as_org,
    as_identity,
    claim,
    human,
    write_policy,
)
from test_operating_profiles import (
    env as env,
)

from unified_agent import config, operating_profiles, reviewer, server, storage
from unified_agent.tools import approvals, plan, tasks
from unified_agent.webui import api


def _notes(task_id):
    conn = storage.connect()
    try:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def test_plan_approval_scan_never_returns_a_foreign_task(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    plan_id = plan.plan_create("Tenant A private plan", ["secret step"])["plan_id"]
    _as_org(monkeypatch, "org_b")
    tasks.task_add("Tenant B private task", notes=f"[waiting-for: plan-approval]\nplan: {plan_id}")
    _as_org(monkeypatch, "org_a")
    assert reviewer.review_plan_approvals(dry_run=True)["results"] == []


def test_untenanted_plan_approval_scan_does_not_poison_a_tenant_gate(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    plan_id = plan.plan_create("Tenant A plan", ["step"])["plan_id"]
    task_id = tasks.task_add("A", notes=f"[waiting-for: plan-approval]\nplan: {plan_id}")["id"]
    before = _notes(task_id)
    monkeypatch.delenv("UA_EDITION")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    assert reviewer.review_plan_approvals()["results"] == []
    assert _notes(task_id) == before


def test_plan_approval_note_rechecks_task_tenant(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    plan_id = plan.plan_create("A", ["step"])["plan_id"]
    task_id = tasks.task_add("A", notes=f"[waiting-for: plan-approval]\nplan: {plan_id}")["id"]
    before = _notes(task_id)
    original = reviewer._plan_fingerprint

    def move_task(data):
        conn = storage.connect()
        conn.execute("UPDATE tasks SET org_id='org_b' WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
        return original(data)

    monkeypatch.setattr(reviewer, "_plan_fingerprint", move_task)
    assert reviewer.review_plan_approvals()["results"] == []
    assert _notes(task_id) == before


@pytest.mark.parametrize("operation", ["operating_run_set", "operating_run_clear"])
@pytest.mark.parametrize("target", ["unknown", "foreign"])
def test_mcp_operating_preflight_precedes_outer_approval(tmp_path, monkeypatch, operation, target):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    task_id = tasks.task_add("A")["id"] if target == "foreign" else 999999
    _as_org(monkeypatch, "org_b")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    params = {"task_id": task_id}
    if operation == "operating_run_set":
        params["profile"] = "crew"
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="unknown task"):
            server.REGISTERED_TOOLS["goal_manage"](operation, params)
        assert approvals.approval_pending()["pending"] == []
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("operation", ["set", "clear"])
@pytest.mark.parametrize("target", ["unknown", "foreign"])
async def test_http_operating_preflight_precedes_outer_approval(
    tmp_path, monkeypatch, operation, target
):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    task_id = tasks.task_add("A")["id"] if target == "foreign" else 999999
    _as_org(monkeypatch, "org_b")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    body = {"op": operation, "task_id": task_id}
    if operation == "set":
        body["profile"] = "crew"
    with pytest.raises(ToolError, match="unknown task"):
        await api.operating_action_payload("run", body, "Desktop_Agent2")
    assert approvals.approval_pending()["pending"] == []


@pytest.mark.parametrize("operation", ["update", "link", "unlink"])
def test_foreign_sprint_preflight_precedes_inner_approval(tmp_path, monkeypatch, operation):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    sprint_id = human(operating_profiles.sprint_add, "A")["id"]
    _as_org(monkeypatch, "org_b")
    task_id = tasks.task_add("B")["id"]
    token = as_identity("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="unknown sprint"):
            if operation == "update":
                operating_profiles.sprint_update(sprint_id, name="new")
            else:
                operating_profiles.sprint_link_task(
                    sprint_id, task_id, unlink=operation == "unlink"
                )
        assert approvals.approval_pending()["pending"] == []
    finally:
        config.agent_identity_var.reset(token)


def test_foreign_sprint_denial_rolls_back_initial_claim_and_preserves_own_policy(
    tmp_path, monkeypatch
):
    write_policy(tmp_path, monkeypatch)
    _as_org(monkeypatch, "org_a")
    foreign = human(operating_profiles.sprint_add, "A", profile="crew")["id"]
    _as_org(monkeypatch, "org_b")
    task_id = tasks.task_add("B")["id"]
    human(
        operating_profiles.run_settings_set,
        task_id,
        overrides={"delivery": {"mode": "local-only"}, "budget": {"max_tokens": 500}},
    )
    conn = storage.connect()
    conn.execute(
        "INSERT INTO sprint_tasks(task_id,sprint_id,linked_by,linked_at) VALUES(?,?,?,?)",
        (task_id, foreign, "Desktop_Person", storage.now()),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ToolError, match="unknown sprint"):
        claim(task_id, "Desktop_Agent2")
    with pytest.raises(ToolError, match="unknown sprint"):
        operating_profiles.settings_for_launch(task_id, "Desktop_Agent2")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "open" and row["claimed_by"] is None
        assert row["current_run_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 1
    finally:
        conn.close()
