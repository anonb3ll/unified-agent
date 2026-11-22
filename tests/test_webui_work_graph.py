"""GET /api/work-graph (task #1460): route wiring for the work_graph projection.

The projection logic itself (edges, ready/blocked, pagination, unknowns) is
covered by tests/test_work_graph.py against the function directly; this file
only pins the HTTP surface — status codes, query params, and the frozen route
inventory.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import profiles, storage, webui
from unified_agent.goals import goal_add, link_task
from unified_agent.tools.tasks import task_add
from unified_agent.webui import routes as routes_module


def _client(monkeypatch) -> TestClient:
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.DEVELOPER)
    storage.init_db()
    app = Starlette(routes=webui.web_routes())
    return TestClient(app)


def test_root_page_over_http(monkeypatch):
    client = _client(monkeypatch)
    goal_add("A root goal", author="Desktop_Agent1")

    res = client.get("/api/work-graph")
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == {"root_goals": True}
    assert any(n["kind"] == "goal" for n in body["nodes"])


def test_goal_scoped_over_http(monkeypatch):
    client = _client(monkeypatch)
    goal = goal_add("Ship it", author="Desktop_Agent1")
    task = task_add(title="do the work")
    link_task(task["id"], goal["goal"]["id"], author="Desktop_Agent1")

    res = client.get(f"/api/work-graph?goal_id={goal['goal']['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["scope"]["goal_id"] == goal["goal"]["id"]
    task_ids = {n["task_id"] for n in body["nodes"] if n["kind"] == "task"}
    assert task["id"] in task_ids
    assert "next_cursor" in body
    assert "revision" in body
    assert "unknowns" in body


def test_unknown_goal_id_is_404(monkeypatch):
    client = _client(monkeypatch)
    res = client.get("/api/work-graph?goal_id=999999")
    assert res.status_code == 404


def test_invalid_goal_id_is_400(monkeypatch):
    client = _client(monkeypatch)
    res = client.get("/api/work-graph?goal_id=not-a-number")
    assert res.status_code == 400


def test_requires_each_projected_reader(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.OPERATOR)

    goal = goal_add("Root", author="Desktop_Agent1")
    res = client.get(f"/api/work-graph?goal_id={goal['goal']['id']}")

    # Missing optional readers redact their object families instead of
    # failing the whole view; the gap is reported as an unknown.
    assert res.status_code == 200
    fields = {item["field"] for item in res.json()["unknowns"]}
    assert {"dispatch_report_self", "plan_get"} <= fields
    assert not any(n["kind"] in {"run", "plan"} for n in res.json()["nodes"])


def test_plan_reader_uses_effective_consolidated_action(monkeypatch):
    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.OPERATOR)
    operator_readers = routes_module._work_graph_readers()
    assert isinstance(operator_readers, tuple)
    assert operator_readers[3] is False  # task_list allowed; plan_get denied

    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.DEVELOPER)
    developer_readers = routes_module._work_graph_readers()
    assert isinstance(developer_readers, tuple)
    assert developer_readers[3] is True  # plan_get is allowed through plan_manage


def test_agent_metadata_uses_effective_consolidated_reader(monkeypatch):
    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.OPERATOR)
    operator_readers = routes_module._work_graph_readers()
    assert isinstance(operator_readers, tuple)
    assert operator_readers[4] is False

    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.DEVELOPER)
    developer_readers = routes_module._work_graph_readers()
    assert isinstance(developer_readers, tuple)
    assert developer_readers[4] is True


def test_session_http_routes_require_session_reader(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(routes_module.profiles, "current", lambda: profiles.PUBLIC)
    for path in ("/api/sessions", "/api/sessions/missing", "/api/sessions/missing/crew"):
        res = client.get(path)
        assert res.status_code == 403
        assert "session_list" in res.json()["error"]


def test_oversized_goal_scope_returns_413(monkeypatch):
    client = _client(monkeypatch)
    goal = goal_add("Root", author="Desktop_Agent1")
    goal_add("Child", parent_goal_id=goal["goal"]["id"], author="Desktop_Agent1")
    graph_module = __import__("unified_agent.work_graph", fromlist=["work_graph"])
    monkeypatch.setattr(graph_module, "MAX_GOAL_SCOPE", 1)

    res = client.get(f"/api/work-graph?goal_id={goal['goal']['id']}")

    assert res.status_code == 413
    assert "goal subtree exceeds maximum scope" in res.json()["error"]
