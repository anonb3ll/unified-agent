"""Control Center v2 work routes (tasks #2466, #2469).

Plans (one ``POST /api/action/plan/<action>`` route per plan_manage action, and
the joint read ``GET /api/plans/{plan_id}/joint``), task claim, delete and
archive, session end, and (task #2480) plan structure, goal edits and the
operator's session end, each over its registered tool or platform writer. The real
scoped app runs behind the hub auth middleware with the fixture identities of
``test_route_scopes``.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, TOKENS, _bearer, _client
from test_route_scopes import _isolated as _isolated

from unified_agent import config, profiles, registry, server
from unified_agent.tools import approvals, tasks
from unified_agent.webui import cc_work, route_scopes, routes

DEV = "Desktop_Agent2"

#: Every write route with a valid body and the tool its approval gate names.
WRITES = {
    "/api/action/plan/create": ({"goal": "g", "steps": ["a"]}, "plan_manage"),
    "/api/action/plan/joint/recover": ({"plan_id": "p"}, "plan_manage"),
    "/api/action/task/claim": ({"id": "{task}"}, "task_claim"),
    "/api/action/task/delete": ({"id": "{task}"}, "task_delete"),
    "/api/action/task": ({"id": "{task}", "archived": True}, "task_update"),
    "/api/action/session/end": ({"session_id": "{session}"}, "session_end"),
    "/api/action/plan/step-add": ({"plan_id": "p", "description": "x"}, "plan_manage"),
    "/api/action/goal/edit": ({"goal_id": 1, "title": "t"}, "goal_manage"),
}


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _fill(body: dict, task_id: int, session_id: str) -> dict:
    swap = {"{task}": task_id, "{session}": session_id}
    return {k: swap.get(v, v) if isinstance(v, str) else v for k, v in body.items()}


def _task(task_id: int) -> dict | None:
    from unified_agent import storage

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _approve(approval_id: int) -> None:
    token = config.agent_identity_var.set(OPERATOR)
    try:
        approvals.approval_resolve(approval_id, "approve", resolved_by=OPERATOR)
    finally:
        config.agent_identity_var.reset(token)


# --- plans ----------------------------------------------------------------------------

JOINT_OFFICE = "cc-joint"


def test_plan_lifecycle_one_route_per_action(client):
    headers = _bearer(DEV)

    def plan(action, **body):
        return client.post(f"/api/action/plan/{action}", headers=headers, json=body)

    created = plan("create", goal="ship it", steps=["build", "verify"])
    assert created.status_code == 200, created.text
    plan_id = created.json()["plan_id"]
    assert plan("update", plan_id=plan_id, step_idx=0, status="doing").status_code == 200
    assert plan("checkpoint", plan_id=plan_id, note="half way").status_code == 200
    assert plan("resume", plan_id=plan_id).json()["plan"]["id"] == plan_id
    assert plan("claim", plan_id=plan_id, step_idx=1).status_code == 200
    assert plan("handoff", plan_id=plan_id, step_idx=1, note="done here").status_code == 200
    assert plan("retry", plan_id=plan_id, step_idx=1, reason="flaky").status_code == 200
    closed = plan("close", plan_id=plan_id, reason="delivered elsewhere")
    assert closed.status_code == 200, closed.text
    assert closed.json()["plan"]["status"] == "abandoned"
    other = plan("create", goal="throwaway", steps=["x"]).json()["plan_id"]
    assert plan("delete", plan_id=other).status_code == 200
    assert plan("delete", plan_id=other).status_code == 404


def test_plan_author_is_the_verified_identity(client):
    from unified_agent.tools import plan as plan_tool

    created = client.post(
        "/api/action/plan/create",
        headers=_bearer(OPERATOR),
        json={"goal": "g", "steps": ["a"]},
    )
    assert created.status_code == 200, created.text
    assert plan_tool.plan_get(created.json()["plan_id"])["plan"]["author"] == OPERATOR


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/plan/create", {"goal": "g"}),
        ("/api/action/plan/create", {"goal": "g", "steps": ["a"], "action": "create"}),
        ("/api/action/plan/update", {"plan_id": "p", "step_idx": -1}),
        ("/api/action/plan/close", {"plan_id": "p", "reason": "x" * 4_001}),
        ("/api/action/plan/joint/review", {"plan_id": "p", "decision": "accept"}),
    ],
)
def test_bad_plan_input_is_400(client, path, body):
    res = client.post(path, headers=_bearer(DEV), json=body)
    assert res.status_code == 400, res.text


def test_each_plan_route_declares_exactly_its_action():
    from unified_agent.tools.plan import _PLAN_ACTIONS

    for action in cc_work.PLAN_ACTIONS:
        path = cc_work._plan_path(action)
        scope = route_scopes.scope_for("POST", path)
        assert scope.actions == (("plan_manage", _PLAN_ACTIONS[action].__name__),), path
        assert scope.operator and scope.csrf and not scope.tools
    review = route_scopes.scope_for("POST", "/api/action/plan")
    assert review.actions == (("plan_manage", "plan_review"),)
    joint = route_scopes.scope_for("GET", "/api/plans/{plan_id}/joint")
    assert joint.actions == (("plan_manage", "plan_joint_get"),)


@pytest.fixture
def joint_plan(tmp_path, monkeypatch) -> str:
    """A plan bound to an office whose lead is DEV and whose member is Laptop_Agent5."""
    from unified_agent.goals import goal_add
    from unified_agent.task_comments import append_comment
    from unified_agent.tools import plan as plan_tool

    registry_file = tmp_path / "org.yaml"
    registry_file.write_text(
        "schema_version: 1\noffices:\n"
        f"  - id: {JOINT_OFFICE}\n    name: CC joint\n    lead: {DEV}\n"
        f"    members: [{DEV}, Laptop_Agent5]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(registry_file))
    token = config.agent_identity_var.set(DEV)
    try:
        goal = goal_add("Plan together", author=DEV)
        task = tasks.task_add(title="joint thread")
        plan_id = plan_tool.plan_create("decompose", ["a", "b"], author=DEV)["plan_id"]
        comment = append_comment(task["id"], "bind", author=DEV, refs={"plan_id": plan_id})
    finally:
        config.agent_identity_var.reset(token)
    client = _client(routes.web_routes())
    bound = client.post(
        "/api/action/plan/joint/bind",
        headers=_bearer(DEV),
        json={
            "plan_id": plan_id,
            "goal_id": goal["goal"]["id"],
            "org_unit_id": JOINT_OFFICE,
            "comment_id": comment["comment"]["id"],
        },
    )
    assert bound.status_code == 200, bound.text
    return plan_id


def test_joint_read_names_the_caller_and_refuses_outsiders(client, joint_plan):
    res = client.get(f"/api/plans/{joint_plan}/joint", headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["revision"] == 0 and view["pending"] is None
    assert view["caller"] == {"identity": DEV, "role": "lead"}
    assert {m["identity"] for m in view["team"]} == {DEV, "Laptop_Agent5"}
    # The operator passes the profile ceiling, not the office-membership rule.
    for method, path in (
        ("GET", f"/api/plans/{joint_plan}/joint"),
        ("POST", "/api/action/plan/joint/recover"),
    ):
        refused = client.request(
            method, path, headers=_bearer(OPERATOR), json={"plan_id": joint_plan}
        )
        assert refused.status_code == 400, refused.text
        assert "not a member of org unit" in refused.json()["error"]
    # The joint read and the plain plan read do not shadow each other.
    assert client.get("/api/plans/nope/joint", headers=_bearer(DEV)).status_code == 404
    plain = client.get(f"/api/plans/{joint_plan}", headers=_bearer(DEV))
    assert plain.status_code == 200 and "team" not in plain.json()


def test_stale_joint_revision_is_a_409_conflict(client, joint_plan):
    """A proposal that lost the revision race says so: 409, not a bad request."""
    from unified_agent.task_comments import append_comment

    thread = next(t for t in tasks.task_list(status="all")["tasks"] if t["title"] == "joint thread")

    def propose(step: str):
        token = config.agent_identity_var.set(DEV)
        try:
            comment = append_comment(thread["id"], step, author=DEV, refs={"plan_id": joint_plan})
        finally:
            config.agent_identity_var.reset(token)
        return client.post(
            "/api/action/plan/joint/propose",
            headers=_bearer(DEV),
            json={
                "plan_id": joint_plan,
                "expected_revision": 0,
                "steps": [step],
                "comment_id": comment["comment"]["id"],
            },
        )

    first = propose("kept")
    assert first.status_code == 200, first.text
    lost = propose("lost")
    assert lost.status_code == 409, lost.text
    body = lost.json()
    assert body["code"] == "conflict"
    assert (body["expected_revision"], body["current_revision"]) == (0, 1)


# --- approval replay ------------------------------------------------------------------


def test_identical_request_after_approval_runs_the_parked_write(client, monkeypatch):
    """The UI replays a parked write: the same body rebuilds the same approval payload.

    memory_save also takes ``author`` from the signed-in identity, so this proves an
    identity-derived argument is rebuilt identically on the repeat.
    """
    from unified_agent import embeddings
    from unified_agent.tools import memory

    monkeypatch.setattr(
        embeddings, "generate_embedding", lambda _t: (_ for _ in ()).throw(RuntimeError("off"))
    )
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "memory_save")
    body = {"content": "parked note", "title": "replay"}
    parked = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert parked.status_code == 202, parked.text
    _approve(parked.json()["approval_id"])
    ran = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert ran.status_code == 200, ran.text
    assert memory.memory_get(ran.json()["id"])["author"] == DEV
    again = client.post("/api/action/memory/save", headers=_bearer(DEV), json=body)
    assert again.status_code == 202  # the pass was one-shot


# --- tasks ----------------------------------------------------------------------------


def test_task_claim_is_for_the_verified_identity(client):
    task = tasks.task_add(title="claim me")
    res = client.post("/api/action/task/claim", headers=_bearer(DEV), json={"id": task["id"]})
    assert res.status_code == 200, res.text
    assert _task(task["id"])["claimed_by"] == DEV


def test_task_delete_keeps_its_approval_gate(client):
    task = tasks.task_add(title="delete me")
    body = {"id": task["id"]}
    parked = client.post("/api/action/task/delete", headers=_bearer(DEV), json=body)
    assert parked.status_code == 202, parked.text
    assert parked.json()["status"] == "approval_pending"
    assert parked.json()["code"] == "approval_required"
    assert _task(task["id"]) is not None
    _approve(parked.json()["approval_id"])
    deleted = client.post("/api/action/task/delete", headers=_bearer(DEV), json=body)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    gone = client.post("/api/action/task/delete", headers=_bearer(DEV), json=body)
    _approve(gone.json()["approval_id"])
    again = client.post("/api/action/task/delete", headers=_bearer(DEV), json=body)
    assert again.status_code == 404


def test_task_archive_and_unarchive_through_action_task(client):
    task = tasks.task_add(title="archive me")
    for flag in (True, False):
        res = client.post(
            "/api/action/task", headers=_bearer(OPERATOR), json={"id": task["id"], "archived": flag}
        )
        assert res.status_code == 200, res.text
        assert bool(_task(task["id"]).get("archived_at")) is flag


# --- LangGraph and sessions ----------------------------------------------------------------


def test_session_end_closes_only_the_callers_own_session(client):
    own = registry.start_session(DEV, "interactive")
    other = registry.start_session("Laptop_Agent5", "interactive")
    refused = client.post(
        "/api/action/session/end", headers=_bearer(DEV), json={"session_id": other}
    )
    assert refused.status_code == 400
    assert "owned by" in refused.json()["error"]
    assert registry.get_session(other)["ended_at"] is None
    ended = client.post(
        "/api/action/session/end",
        headers=_bearer(DEV),
        json={"session_id": own, "status": "completed", "summary": "done"},
    )
    assert ended.status_code == 200 and ended.json()["closed"] is True
    missing = client.post("/api/action/session/end", headers=_bearer(DEV), json={"session_id": "x"})
    assert missing.status_code == 404


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("path", list(WRITES))
def test_unauthenticated_and_public_profile_are_refused(client, path):
    body = _fill(WRITES[path][0], 1, "s")
    assert client.post(path, json=body).status_code == 401
    denied = client.post(path, headers=_bearer("server_agent8"), json=body)
    assert denied.status_code == 403
    assert "unavailable to this profile" in denied.json()["error"]


@pytest.mark.parametrize("path", [*WRITES, "/api/action/session/operator-end"])
def test_cookie_session_write_needs_the_csrf_token(path):
    session = _client([r for r in routes.web_routes() if r.path == "/api/session"])
    login = session.post("/api/session", json={"token": TOKENS["Laptop_Agent5"]})
    assert login.status_code == 200
    browser = TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=routes.web_routes())),
        base_url="http://127.0.0.1",
        cookies=dict(session.cookies),
    )
    body = WRITES.get(path, ({"session_id": "s", "reason": "r"}, None))[0]
    res = browser.post(path, json=_fill(body, 1, "s"))
    assert res.status_code == 403
    assert "CSRF" in res.json()["error"]


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/plan/create", {"goal": "g", "steps": ["a"], "author": "x"}),
        ("/api/action/plan/claim", {"plan_id": "p", "step_idx": 0, "agent": "x"}),
        ("/api/action/plan/joint/recover", {"plan_id": "p", "author": "x"}),
        ("/api/action/task/claim", {"id": 1, "agent": "Laptop_Agent5"}),
        ("/api/action/goal/close", {"goal_id": 1, "author": "Laptop_Agent5"}),
    ],
)
def test_identity_named_in_the_body_must_be_the_verified_one(client, path, body):
    res = client.post(path, headers=_bearer(DEV), json=body)
    assert res.status_code == 400
    assert "authenticated identity" in res.json()["error"]


@pytest.mark.parametrize("path", list(WRITES))
def test_approval_gate_answers_202(client, path, monkeypatch):
    body, tool = WRITES[path]
    task = tasks.task_add(title="gated")
    session = registry.start_session(DEV, "interactive")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", tool)
    res = client.post(path, headers=_bearer(DEV), json=_fill(body, task["id"], session))
    assert res.status_code == 202, res.text
    payload = res.json()
    assert payload["status"] in {"approval_pending", "pending"}
    assert payload["code"] == "approval_required"
    assert payload["approval_id"] in {row["id"] for row in approvals.approval_pending()["pending"]}
    assert payload.get("tool", tool) == tool


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/task/claim", {"id": "1"}),
        ("/api/action/task/claim", {"id": 1, "colour": "red"}),
        ("/api/action/task/delete", {}),
        ("/api/action/session/end", {"session_id": "s", "status": "paused"}),
        ("/api/action/task", {"id": 1, "archived_flag": True}),
        ("/api/action/plan/reorder", {"plan_id": "p"}),
        ("/api/action/plan/step-add", {"plan_id": "p", "description": "x", "position": "0"}),
        ("/api/action/goal/close", {"goal_id": 1, "status": "open"}),
        ("/api/action/goal/unlink", {"task_id": 1}),
    ],
)
def test_bad_input_is_400(client, path, body):
    assert client.post(path, headers=_bearer(DEV), json=body).status_code == 400


@pytest.mark.parametrize("path", ["/api/action/plan/create", "/api/action/task/claim"])
def test_malformed_json_is_400(client, path):
    assert client.post(path, headers=_bearer(DEV), content=b"{oops").status_code == 400
    assert client.post(path, headers=_bearer(DEV), json=[1]).status_code == 400


# --- whoami org -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "edition,org,expected",
    [
        ("enterprise", "org_fixture", ("org_fixture", "partitioned")),
        ("enterprise", "", (None, "unpartitioned")),
        ("core", "org_fixture", (None, "unpartitioned")),
    ],
)
def test_whoami_reports_the_tenant_org(client, monkeypatch, edition, org, expected):
    monkeypatch.setenv("UA_EDITION", edition)
    for key in ("WORKOS_ORGANIZATION_ID", "UA_HUB_WORKOS_ORGANIZATION_ID"):
        monkeypatch.delenv(key, raising=False)
    if org:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)
    res = client.get("/api/whoami", headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["org"], body["org_mode"]) == expected
    assert body["agent"] == DEV and "csrf" in body and "api_version" in body


# --- review round 1 -------------------------------------------------------------------


def test_string_archived_cannot_start_a_parked_task(client):
    """Codex #1507 blocker: "false" passed admission but left archived_at set."""
    task = tasks.task_add(title="parked")
    tasks.task_update(id=task["id"], archived=True)
    res = client.post(
        "/api/action/task",
        headers=_bearer(OPERATOR),
        json={"id": task["id"], "status": "doing", "archived": "false", "worktree": False},
    )
    assert res.status_code == 400, res.text
    row = _task(task["id"])
    assert row["status"] == "open" and row["archived_at"] is not None


@pytest.mark.parametrize("field", ["archived", "untrusted", "worktree", "clear_due_at"])
def test_action_task_boolean_fields_must_be_booleans(client, field):
    task = tasks.task_add(title="typed flags", due_at="2027-01-01")
    res = client.post(
        "/api/action/task", headers=_bearer(OPERATOR), json={"id": task["id"], field: "false"}
    )
    assert res.status_code == 400, res.text
    assert field in res.json()["error"]
    row = _task(task["id"])
    assert row["untrusted"] == 0 and row["due_at"] is not None and row["archived_at"] is None


def test_api_doc_names_the_identity_argument_of_every_write_route():
    """Codex #1507: the doc claimed every route's approval was bound to its caller.

    Only routes with an identity argument bind the signed-in identity into the
    approval payload; the doc's table must say which, route by route.
    """
    import re
    from pathlib import Path

    from unified_agent.webui import cc_knowledge

    doc = (Path(__file__).resolve().parents[1] / "docs/control-center-api.md").read_text()
    table = doc.split("<!-- identity-args:start -->")[1].split("<!-- identity-args:end -->")[0]
    documented = {
        path: cell.split(";")[0].split(" (")[0].strip("` ")
        for path, cell in re.findall(r"\| `POST (\S+)` \| ([^|]+) \|", table)
    }
    actual = {}
    from unified_agent.webui import cc_agents, cc_runs

    for route in [*cc_knowledge.ROUTES, *cc_work.ROUTES, *cc_runs.ROUTES, *cc_agents.ROUTES]:
        if "POST" in route.methods:
            args = getattr(route.endpoint, "identity_args", ())
            actual[route.path] = ", ".join(args) or "none"
    assert documented == actual


# --- task #2480: plan structure, goals, operator session end ---------------------------


def test_plan_structure_routes(client, joint_plan):
    headers = _bearer(DEV)

    def plan(path, **body):
        return client.post(f"/api/action/plan/{path}", headers=headers, json=body)

    pid = plan("create", goal="ship", steps=["a", {"description": "b", "depends_on": [0]}]).json()[
        "plan_id"
    ]
    added = plan("step-add", plan_id=pid, description="first", position=0)
    assert added.status_code == 200, added.text
    assert [s["depends_on"] for s in added.json()["steps"]] == [[], [], [1]]
    assert plan("step-edit", plan_id=pid, step_idx=0, description="zero").status_code == 200
    refused = plan("reorder", plan_id=pid, order=[2, 1, 0])
    assert refused.status_code == 400 and "ahead of step" in refused.json()["error"]
    moved = plan("reorder", plan_id=pid, order=[1, 2, 0])
    assert [s["description"] for s in moved.json()["steps"]] == ["a", "b", "zero"]
    assert plan("step-remove", plan_id=pid, step_idx=2).status_code == 200
    stamp = plan("edit", plan_id=pid, title="Ship v2", description="why").json()["plan"]
    assert (stamp["title"], stamp["description"]) == ("Ship v2", "why")
    fresh = plan("edit", plan_id=pid, title="x", expected_revision=stamp["revision"])
    assert fresh.status_code == 200, fresh.text
    stale = plan("edit", plan_id=pid, title="y", expected_revision=stamp["revision"])
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"
    assert plan("step-add", plan_id="nope", description="x").status_code == 404
    joint = plan("step-add", plan_id=joint_plan, description="sneak in")
    assert joint.status_code == 400 and "joint_propose" in joint.json()["error"]


def test_goal_routes_for_the_operator(client):
    from unified_agent.goals import goal_add, goal_id_for_task

    headers = _bearer(OPERATOR)
    token = config.agent_identity_var.set(OPERATOR)
    try:
        gid = goal_add("Run the fleet", author=OPERATOR)["goal"]["id"]
    finally:
        config.agent_identity_var.reset(token)
    task = tasks.task_add(title="serve the goal")

    def goal(action, **body):
        return client.post(f"/api/action/goal/{action}", headers=headers, json=body)

    edited = goal("edit", goal_id=gid, title="Run the whole fleet", statement="all hosts")
    assert edited.status_code == 200, edited.text
    assert edited.json()["goal"]["title"] == "Run the whole fleet"
    assert goal("link", task_id=task["id"], goal_id=gid).status_code == 200
    assert goal_id_for_task(task["id"]) == gid
    assert goal("unlink", task_id=task["id"], goal_id=gid).json()["unlinked"] is True
    assert goal("close", goal_id=gid, status="achieved", reason="done").status_code == 200
    again = goal("close", goal_id=gid)
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    assert goal("reopen", goal_id=gid).json()["goal"]["status"] == "open"
    assert goal("edit", goal_id=99_999, title="x").status_code == 404
    assert goal("link", task_id=99_999, goal_id=gid).status_code == 404


def test_operator_end_is_the_operators_and_records_the_owner(client):
    from unified_agent import events

    session = registry.start_session(DEV, "interactive")
    body = {"session_id": session, "reason": "stuck in a loop"}
    for who in (DEV, "Laptop_Agent5"):
        denied = client.post("/api/action/session/operator-end", headers=_bearer(who), json=body)
        assert denied.status_code == 403, denied.text
    assert registry.get_session(session)["ended_at"] is None
    ended = client.post("/api/action/session/operator-end", headers=_bearer(OPERATOR), json=body)
    assert ended.status_code == 200, ended.text
    assert ended.json()["owner"] == DEV and ended.json()["ended_by"] == OPERATOR
    assert registry.get_session(session)["status"] == "abandoned"
    [event] = events.query_events(channels=["session.operator_ended"])
    assert event["originator"] == OPERATOR and event["payload"]["owner"] == DEV
    repeat = client.post("/api/action/session/operator-end", headers=_bearer(OPERATOR), json=body)
    assert repeat.status_code == 409
    missing = {"session_id": "nope", "reason": "x"}
    res = client.post("/api/action/session/operator-end", headers=_bearer(OPERATOR), json=missing)
    assert res.status_code == 404
    for bad in ({"session_id": session}, {**body, "operator": DEV}, {**body, "extra": 1}):
        res = client.post("/api/action/session/operator-end", headers=_bearer(OPERATOR), json=bad)
        assert res.status_code == 400, bad
    assert client.post("/api/action/session/operator-end", json=body).status_code == 401


def test_operator_end_parks_on_the_approval_gate(client, monkeypatch):
    """The governed adapter's approval gate runs for session.operator_end. The operator
    asked, so separation of duties keeps the operator from approving it."""
    session = registry.start_session(DEV, "interactive")
    body = {"session_id": session, "reason": "stuck"}
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "session.operator_end")
    path = "/api/action/session/operator-end"
    parked = client.post(path, headers=_bearer(OPERATOR), json=body)
    assert parked.status_code == 202, parked.text
    assert parked.json()["code"] == "approval_required"
    assert parked.json()["tool"] == "session.operator_end"
    [row] = [
        r
        for r in approvals.approval_pending()["pending"]
        if r["id"] == parked.json()["approval_id"]
    ]
    assert row["requested_by"] == OPERATOR
    with pytest.raises(Exception, match="separation of duties"):
        _approve(parked.json()["approval_id"])
    assert client.post(path, headers=_bearer(OPERATOR), json=body).status_code == 202
    assert registry.get_session(session)["ended_at"] is None


def test_plan_claim_after_a_reorder_is_a_409(client):
    """Review #1516: the browser's claim carries the step id it read."""
    headers = _bearer(DEV)
    pid = client.post(
        "/api/action/plan/create", headers=headers, json={"goal": "g", "steps": ["a", "b", "c"]}
    ).json()["plan_id"]
    step_b = client.get(f"/api/plans/{pid}", headers=headers).json()["steps"][1]
    moved = client.post(
        "/api/action/plan/reorder", headers=headers, json={"plan_id": pid, "order": [0, 2, 1]}
    )
    assert moved.status_code == 200, moved.text
    stale = client.post(
        "/api/action/plan/claim",
        headers=headers,
        json={"plan_id": pid, "step_idx": 1, "expected_step_id": step_b["id"]},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"


def test_plan_create_with_a_key_creates_one_plan_per_caller(client, monkeypatch):
    """Task #2516: an Idempotency-Key on plan create replays the first answer, binds
    the key to the body, scopes it to the caller, and never re-runs an unknown outcome."""
    from unified_agent.tools import plan as plan_tool
    from unified_agent.webui import cc_knowledge

    path = "/api/action/plan/create"
    body = {"goal": "keyed", "steps": ["a"]}

    def post(who, payload, key):
        return client.post(path, headers={**_bearer(who), "Idempotency-Key": key}, json=payload)

    first = post(DEV, body, "plan-1")
    assert first.status_code == 200, first.text
    assert post(DEV, body, "plan-1").json() == first.json()
    clash = post(DEV, {**body, "goal": "other"}, "plan-1")
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    theirs = post(OPERATOR, body, "plan-1")
    assert theirs.status_code == 200 and theirs.json()["plan_id"] != first.json()["plan_id"]
    goals = [p["goal"] for p in plan_tool.plan_list(status="all", limit=50)["plans"]]
    assert goals.count("keyed") == 2
    assert post(DEV, {"goal": "g"}, "plan-bad").status_code == 400  # refused, not stored
    assert post(DEV, {"goal": "g", "steps": ["a"]}, "plan-bad").status_code == 200
    real_call = cc_knowledge.call

    async def dies(*_args, **_kwargs):
        raise RuntimeError("the hub died mid-write")

    monkeypatch.setattr(cc_knowledge, "call", dies)
    lost = TestClient(client.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert (
        lost.post(
            path, headers={**_bearer(DEV), "Idempotency-Key": "plan-lost"}, json=body
        ).status_code
        == 500
    )
    monkeypatch.setattr(cc_knowledge, "call", real_call)
    unknown = post(DEV, body, "plan-lost")
    assert unknown.status_code == 409 and unknown.json()["code"] == "outcome_unknown"


def test_task_subtree_returns_every_descendant_in_any_status(client):
    """Task #2516: the board's tree read includes old done and archived descendants
    at any depth, depth-first, and nothing of another tenant or outside the root."""
    root = tasks.task_add(title="epic")["id"]
    child = tasks.task_add(title="child", parent_id=root)["id"]
    grandchild = tasks.task_add(title="grandchild", parent_id=child)["id"]
    old = tasks.task_add(title="old done", parent_id=root)["id"]
    tasks.task_add(title="unrelated")
    from unified_agent import storage

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at='2020-01-01', archived_at='2020-02-01' "
            "WHERE id=?",
            (old,),
        )
        conn.commit()
    finally:
        conn.close()
    res = client.get(f"/api/tasks/{root}/subtree", headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["root"]["id"] == root and body["complete"] is True
    assert [(t["id"], t["depth"]) for t in body["descendants"]] == [
        (child, 1),
        (grandchild, 2),
        (old, 1),
    ]
    assert body["descendants"][2]["status"] == "done" and body["count"] == 3
    assert client.get("/api/tasks/999999/subtree", headers=_bearer(DEV)).status_code == 404


def test_a_keyed_write_that_failed_after_it_ran_is_never_run_again(client, monkeypatch):
    """Review #1538: a ledger write that fails after the tool ran answered 400, the
    receipt was dropped, and the repeat made a second plan. The receipt now follows
    the execution phase: after the boundary it is outcome_unknown whatever the status;
    a park before it is dropped, so the approved repeat runs once."""
    from unified_agent import gateway
    from unified_agent.tools import plan as plan_tool

    path = "/api/action/plan/create"
    body = {"goal": "ledger-lost", "steps": ["a"]}

    def post(key, payload=body):
        return client.post(path, headers={**_bearer(DEV), "Idempotency-Key": key}, json=payload)

    real_record = gateway.record_call_result

    def ledger_down(tool_name, call, error, *args, **kwargs):
        if tool_name == "plan_manage" and error is None:
            raise gateway.LedgerWriteError("decision event could not be written")
        return real_record(tool_name, call, error, *args, **kwargs)

    monkeypatch.setattr(gateway, "record_call_result", ledger_down)
    assert post("ledger-1").status_code == 400
    monkeypatch.setattr(gateway, "record_call_result", real_record)
    again = post("ledger-1")
    assert again.status_code == 409 and again.json()["code"] == "outcome_unknown", again.text
    goals = [p["goal"] for p in plan_tool.plan_list(status="all", limit=50)["plans"]]
    assert goals.count("ledger-lost") == 1
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "plan_manage")
    gated = {"goal": "gated", "steps": ["a"]}
    parked = post("gate-1", gated)
    assert parked.status_code == 202, parked.text
    _approve(parked.json()["approval_id"])
    ran = post("gate-1", gated)
    assert ran.status_code == 200, ran.text
    assert post("gate-1", gated).json() == ran.json()


def test_task_subtree_refuses_a_tree_over_its_node_budget_or_with_a_cycle(client, monkeypatch):
    """Review #1538: a chain deeper than the budget stopped at the depth bound and
    answered complete:true. One node budget now counts every node visited, and a
    cycle or an overflow is a coded refusal, never a partial tree."""
    from unified_agent import storage

    monkeypatch.setattr(tasks, "SUBTREE_MAX", 3)
    chain = [tasks.task_add(title="c0")["id"]]
    for n in range(1, 5):
        chain.append(tasks.task_add(title=f"c{n}", parent_id=chain[-1])["id"])
    deep = client.get(f"/api/tasks/{chain[0]}/subtree", headers=_bearer(DEV))
    assert deep.status_code == 422 and deep.json()["code"] == "subtree_too_large", deep.text
    ok = client.get(f"/api/tasks/{chain[1]}/subtree", headers=_bearer(DEV)).json()
    assert ok["count"] == 3 and ok["complete"] is True
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET parent_id=? WHERE id=?", (chain[3], chain[2]))
        conn.commit()
    finally:
        conn.close()
    loop = client.get(f"/api/tasks/{chain[2]}/subtree", headers=_bearer(DEV))
    assert loop.status_code == 422 and loop.json()["code"] == "subtree_cycle", loop.text
