"""Goals carry a tenant, and goal reads and writes are fenced (task #1999).

Before this, ``goals`` had no ``org_id``: a tenanted caller could read
another tenant's goals by id or in a list, hang a sub-goal under one, and
link its own task to it.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import goals, storage, work_graph
from unified_agent.tools import tasks as task_tools

ORG_A = "org_a"
ORG_B = "org_b"
AUTHOR = "Desktop_Agent4"


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


def _goal_org(goal_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT org_id FROM goals WHERE id=?", (goal_id,)).fetchone()[0]
    finally:
        conn.close()


def _add(title: str, **kwargs) -> int:
    return int(goals.goal_add(title, author=AUTHOR, **kwargs)["goal"]["id"])


# --- stamping ----------------------------------------------------------------------


def test_goal_is_stamped_with_the_writing_tenant(as_org):
    as_org(ORG_A)
    goal_id = _add("Tenant A mission")
    assert _goal_org(goal_id) == ORG_A


def test_untenanted_hub_leaves_goal_org_id_null(untenanted):
    goal_id = _add("Private hub mission")
    assert _goal_org(goal_id) is None
    assert goals.get_goal(goal_id)["goal"]["id"] == goal_id


# --- reads -------------------------------------------------------------------------


def test_foreign_goal_is_unknown_by_id(as_org):
    as_org(ORG_A)
    goal_id = _add("Tenant A mission")
    as_org(ORG_B)
    assert goals.get_goal(goal_id) is None
    with pytest.raises(ToolError, match="unknown goal"):
        goals.goal_manage("get", {"goal_id": goal_id})


def test_list_and_tree_show_only_own_tenant_goals(as_org):
    as_org(ORG_A)
    a_root = _add("Tenant A mission")
    _add("Tenant A step", parent_goal_id=a_root)
    as_org(ORG_B)
    b_root = _add("Tenant B mission")

    listed = {g["id"] for g in goals.goal_manage("list")["goals"]}
    assert listed == {b_root}
    tree = goals.goal_manage("tree")["tree"]
    assert [node["id"] for node in tree] == [b_root]
    assert tree[0]["children"] == []


def test_untenanted_caller_does_not_see_tenant_goals(as_org, monkeypatch):
    as_org(ORG_A)
    a_goal = _add("Tenant A mission")
    monkeypatch.delenv("UA_EDITION")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    assert goals.get_goal(a_goal) is None
    assert goals.goal_list()["goals"] == []


# --- writes ------------------------------------------------------------------------


def test_cannot_add_a_sub_goal_under_a_foreign_goal(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A mission")
    as_org(ORG_B)
    with pytest.raises(ToolError, match="unknown parent goal"):
        _add("Hijack", parent_goal_id=a_goal)


def test_cannot_link_own_task_to_a_foreign_goal(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A mission")
    as_org(ORG_B)
    b_task = task_tools.task_add(title="Tenant B work")["id"]
    with pytest.raises(ToolError, match="unknown goal"):
        goals.link_task(b_task, a_goal, author=AUTHOR)
    assert goals.goal_id_for_task(b_task) is None


def test_cannot_link_a_foreign_task_or_read_its_why_chain(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A mission")
    a_task = task_tools.task_add(title="Tenant A work")["id"]
    goals.link_task(a_task, a_goal, author=AUTHOR)
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    with pytest.raises(ToolError, match="unknown task"):
        goals.link_task(a_task, b_goal, author=AUTHOR)
    assert goals.goal_id_for_task(a_task) is None
    with pytest.raises(ToolError, match="unknown task"):
        goals.why_chain(a_task)
    assert goals.append_why_chain("plain", a_task) == "plain"
    as_org(ORG_A)
    assert goals.goal_id_for_task(a_task) == a_goal
    assert [g["id"] for g in goals.why_chain(a_task)["chain"]] == [a_goal]


def test_legacy_cross_tenant_link_yields_no_why_chain(as_org):
    """A link written before the fence must not leak the foreign goal's title."""
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission")
    as_org(ORG_B)
    b_task = task_tools.task_add(title="Tenant B work")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_goal_links (task_id, goal_id, linked_by, linked_at) VALUES (?,?,?,?)",
            (b_task, a_goal, AUTHOR, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    chain = goals.why_chain(b_task)
    assert chain["goal_id"] is None
    assert chain["chain"] == []
    assert goals.append_why_chain("plain", b_task) == "plain"


def _legacy_link(task_id: int, goal_id: int) -> None:
    """Write a task_goal_links row directly, as a pre-fence hub could have."""
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_goal_links (task_id, goal_id, linked_by, linked_at) VALUES (?,?,?,?)",
            (task_id, goal_id, AUTHOR, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_cross_tenant_link_does_not_disclose_the_foreign_goal_id(as_org):
    """goal_id_for_task fences the goal too, not only the task (PR #1133 review)."""
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission")
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    b_task = task_tools.task_add(title="Tenant B work")["id"]
    _legacy_link(b_task, a_goal)
    assert goals.goal_id_for_task(b_task) is None
    with pytest.raises(ToolError) as excinfo:
        goals.link_task(b_task, b_goal, author=AUTHOR)
    assert f"goal {a_goal}" not in str(excinfo.value)
    as_org(ORG_A)
    # The foreign tenant still cannot see B's task, so no id crosses either way.
    assert goals.goal_id_for_task(b_task) is None


def test_project_move_does_not_disclose_a_legacy_foreign_goal(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission", project="browser")
    as_org(ORG_B)
    b_task = task_tools.task_add(title="Tenant B work")["id"]
    _legacy_link(b_task, a_goal)
    moved = task_tools.task_update(b_task, project="sample-store")
    assert moved["project"] == "sample-store"


def test_legacy_foreign_parent_is_not_named_in_ancestry_errors(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission")
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    conn = storage.connect()
    try:
        conn.execute("UPDATE goals SET parent_goal_id=? WHERE id=?", (a_goal, b_goal))
        conn.commit()
    finally:
        conn.close()
    for call in (
        lambda: goals.get_goal(b_goal),
        lambda: work_graph.work_graph(goal_id=b_goal),
    ):
        with pytest.raises(ToolError) as excinfo:
            call()
        assert f"goal: {a_goal}" not in str(excinfo.value)
        assert "not visible" in str(excinfo.value)


# --- work graph --------------------------------------------------------------------


def _graph_goal_ids(payload: dict) -> set[int]:
    return {int(n["goal_id"]) for n in payload["nodes"] if n["kind"] == "goal"}


def _graph_task_ids(payload: dict) -> set[int]:
    return {int(n["task_id"]) for n in payload["nodes"] if n["kind"] == "task"}


def test_work_graph_root_page_shows_only_own_tenant_goals(as_org):
    as_org(ORG_A)
    a_root = _add("Tenant A secret mission")
    as_org(ORG_B)
    b_root = _add("Tenant B mission")
    payload = work_graph.work_graph()
    assert _graph_goal_ids(payload) == {b_root}
    assert "secret" not in str(payload)
    as_org(ORG_A)
    assert _graph_goal_ids(work_graph.work_graph()) == {a_root}


def test_work_graph_root_page_cursor_does_not_page_into_foreign_goals(as_org):
    as_org(ORG_B)
    b_first = _add("Tenant B first")
    as_org(ORG_A)
    for i in range(3):
        _add(f"Tenant A mission {i}")
    as_org(ORG_B)
    b_last = _add("Tenant B last")
    page = work_graph.work_graph(page_size=1)
    assert _graph_goal_ids(page) == {b_first}
    rest = work_graph.work_graph(cursor=page["next_cursor"], page_size=1)
    assert _graph_goal_ids(rest) == {b_last}
    assert rest["next_cursor"] is None


def test_work_graph_foreign_goal_id_is_unknown(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission")
    as_org(ORG_B)
    with pytest.raises(ToolError, match="unknown goal"):
        work_graph.work_graph(goal_id=a_goal)


def test_work_graph_subgraph_drops_legacy_foreign_sub_goals_and_tasks(as_org):
    """Rows written before the fence may hang a foreign goal or task under ours."""
    as_org(ORG_A)
    a_task = task_tools.task_add(title="Tenant A secret work")["id"]
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    b_task = task_tools.task_add(title="Tenant B work")["id"]
    goals.link_task(b_task, b_goal, author=AUTHOR)
    _legacy_link(a_task, b_goal)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO goals (title, statement, parent_goal_id, project, status, created_by, "
            "created_at, updated_at, org_id) VALUES ('Tenant A secret sub-goal', '', ?, "
            "'unified-agent', 'open', ?, ?, ?, ?)",
            (b_goal, AUTHOR, storage.now(), storage.now(), ORG_A),
        )
        conn.commit()
    finally:
        conn.close()
    payload = work_graph.work_graph(goal_id=b_goal)
    assert _graph_goal_ids(payload) == {b_goal}
    assert _graph_task_ids(payload) == {b_task}
    assert "secret" not in str(payload)


# --- migration ---------------------------------------------------------------------


def test_existing_goals_take_their_tenant_from_the_created_event(as_org):
    as_org(ORG_A)
    a_goal = _add("Tenant A mission")
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_goals_org_id")
        conn.execute("ALTER TABLE goals DROP COLUMN org_id")
        conn.execute(
            "INSERT INTO goals (title, statement, project, status, created_by, created_at, "
            "updated_at) VALUES ('No event', '', 'unified-agent', 'open', ?, ?, ?)",
            (AUTHOR, storage.now(), storage.now()),
        )
        orphan = conn.execute("SELECT MAX(id) FROM goals").fetchone()[0]
        storage._add_goal_org_id(conn)
        storage._add_goal_org_id(conn)  # idempotent
        conn.commit()
        rows = dict(conn.execute("SELECT id, org_id FROM goals").fetchall())
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(goals)").fetchall()}
    finally:
        conn.close()
    assert rows == {a_goal: ORG_A, b_goal: ORG_B, orphan: None}
    assert "idx_goals_org_id" in indexes


# --- legacy foreign parents (PR #1133 review round 3) ------------------------------


def _legacy_parent(child_id: int, parent_id: int) -> None:
    """Point a goal at a parent directly, as a pre-fence hub could have."""
    conn = storage.connect()
    try:
        conn.execute("UPDATE goals SET parent_goal_id=? WHERE id=?", (parent_id, child_id))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def legacy_foreign_child(as_org):
    """Tenant B's goal parented to tenant A's goal, with a B child of its own."""
    as_org(ORG_A)
    a_goal = _add("Tenant A secret mission")
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    b_child = _add("Tenant B step", parent_goal_id=b_goal)
    _legacy_parent(b_goal, a_goal)
    return a_goal, b_goal, b_child


def test_goal_list_redacts_a_legacy_foreign_parent(legacy_foreign_child):
    a_goal, b_goal, b_child = legacy_foreign_child
    for listed in (goals.goal_list()["goals"], goals.goal_manage("list")["goals"]):
        by_id = {g["id"]: g for g in listed}
        assert set(by_id) == {b_goal, b_child}
        assert by_id[b_goal]["parent_goal_id"] is None
        assert by_id[b_child]["parent_goal_id"] == b_goal
        assert str(a_goal) not in {str(g["parent_goal_id"]) for g in listed}


def test_goal_tree_roots_a_child_of_a_legacy_foreign_parent(legacy_foreign_child):
    a_goal, b_goal, b_child = legacy_foreign_child
    tree = goals.goal_manage("tree")["tree"]
    assert [node["id"] for node in tree] == [b_goal]
    assert tree[0]["parent_goal_id"] is None
    assert [child["id"] for child in tree[0]["children"]] == [b_child]
    assert tree[0]["children"][0]["parent_goal_id"] == b_goal


def test_goal_list_keeps_an_own_tenant_parent_outside_the_project_filter(as_org):
    """Redaction is by tenant visibility, not by the project filter."""
    as_org(ORG_B)
    parent = _add("Tenant B mission", project="browser")
    child = _add("Tenant B step", project="sample-store")
    _legacy_parent(child, parent)
    listed = goals.goal_list(project="sample-store")["goals"]
    assert [(g["id"], g["parent_goal_id"]) for g in listed] == [(child, parent)]
    tree = goals.goal_tree(project="sample-store")["tree"]
    assert [(n["id"], n["parent_goal_id"]) for n in tree] == [(child, parent)]


def test_work_graph_root_page_roots_a_child_of_a_legacy_foreign_parent(legacy_foreign_child):
    a_goal, b_goal, _b_child = legacy_foreign_child
    payload = work_graph.work_graph()
    nodes = {n["goal_id"]: n for n in payload["nodes"] if n["kind"] == "goal"}
    assert set(nodes) == {b_goal}
    assert nodes[b_goal]["parent_goal_id"] is None
    assert "secret" not in str(payload)


def test_goals_api_tree_redacts_a_legacy_foreign_parent(legacy_foreign_child, monkeypatch):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server
    from unified_agent.webui.routes import web_routes

    a_goal, b_goal, b_child = legacy_foreign_child
    token = f"tok-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{AUTHOR}:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        resp = client.get("/api/goals", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    tree = resp.json()["tree"]
    assert [(n["id"], n["parent_goal_id"]) for n in tree] == [(b_goal, None)]
    assert [(c["id"], c["parent_goal_id"]) for c in tree[0]["children"]] == [(b_child, b_goal)]


# --- plans (task #2014) ------------------------------------------------------------


def _bind_plan(plan_id: str, goal_id: int) -> None:
    """Write a joint goal binding checkpoint directly, as a pre-fence hub could have."""
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
            "VALUES (?, 1, ?, 'joint binding', ?)",
            (plan_id, f'{{"joint":{{"goal_id":{goal_id},"revision":0}}}}', storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def _plan_org(plan_id: str):
    conn = storage.connect()
    try:
        return conn.execute("SELECT org_id FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    finally:
        conn.close()


def test_plan_is_stamped_and_work_graph_drops_a_foreign_bound_plan(as_org):
    from unified_agent.tools.plan import plan_create

    as_org(ORG_A)
    a_plan = plan_create("Tenant A secret plan", ["inspect"], author=AUTHOR)["plan_id"]
    assert _plan_org(a_plan) == ORG_A
    as_org(ORG_B)
    b_goal = _add("Tenant B mission")
    b_plan = plan_create("Tenant B plan", ["inspect"], author=AUTHOR)["plan_id"]
    assert _plan_org(b_plan) == ORG_B
    _bind_plan(a_plan, b_goal)
    _bind_plan(b_plan, b_goal)
    payload = work_graph.work_graph(goal_id=b_goal)
    plans = {n["plan_id"] for n in payload["nodes"] if n["kind"] == "plan"}
    assert plans == {b_plan}
    assert [e["from"] for e in payload["edges"] if e["kind"] == "plan_goal"] == [f"plan:{b_plan}"]
    assert "secret" not in str(payload)


def test_untenanted_hub_leaves_plan_org_id_null(untenanted):
    from unified_agent.tools.plan import plan_create

    goal_id = _add("Private hub mission")
    plan_id = plan_create("Private plan", ["inspect"], author=AUTHOR)["plan_id"]
    assert _plan_org(plan_id) is None
    _bind_plan(plan_id, goal_id)
    payload = work_graph.work_graph(goal_id=goal_id)
    assert {n["plan_id"] for n in payload["nodes"] if n["kind"] == "plan"} == {plan_id}


def test_plan_org_id_migration_is_additive_and_idempotent(as_org):
    from unified_agent.tools.plan import plan_create

    as_org(ORG_A)
    plan_id = plan_create("Tenant A plan", ["inspect"], author=AUTHOR)["plan_id"]
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_plans_org_id")
        conn.execute("ALTER TABLE plans DROP COLUMN org_id")
        storage._add_plan_org_id(conn)
        storage._add_plan_org_id(conn)  # idempotent
        conn.commit()
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(plans)").fetchall()}
    finally:
        conn.close()
    # No tenant source predates the column, so an existing plan stays untenanted.
    assert _plan_org(plan_id) is None
    assert "idx_plans_org_id" in indexes
