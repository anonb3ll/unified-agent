"""Control Center query box for ranked task search (task #1419).

Ground truth is the real task_list(query=...) path through the HTTP surface
(not a stub) -- auth-gated, requires a query, and returns the same
combined_score/explanation shape the MCP tool does. Read-only: no route here
may create, update, or delete a task.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import profiles, server, storage, webui
from unified_agent.tools.tasks import task_add

TOKEN = f"tok-task-search-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_task_search_returns_ranked_results_with_explanation(client):
    task_add(title="Fix login race condition", notes="Duplicate sessions on parallel login.")
    task_add(title="Write release notes", notes="Nothing about auth here.")

    res = client.get("/api/tasks/search", params={"q": "login race"}, headers=_auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["query"] == "login race"
    assert body["truncated"] is False
    assert body["tasks"], body
    row = body["tasks"][0]
    assert "combined_score" in row
    assert "explanation" in row
    assert row["explanation"]["salience"] is None
    assert row["explanation"]["access"] is None


def test_task_search_requires_auth(client):
    assert client.get("/api/tasks/search", params={"q": "x"}).status_code == 401


def test_task_search_respects_task_list_profile_gate(client, monkeypatch):
    monkeypatch.setattr(profiles, "tool_is_allowed_for", lambda profile, name: name != "task_list")
    res = client.get("/api/tasks/search", params={"q": "login"}, headers=_auth())
    assert res.status_code == 403


def test_task_search_requires_query(client):
    res = client.get("/api/tasks/search", headers=_auth())
    assert res.status_code == 400, res.text
    assert res.json()["error"]


def test_task_search_route_is_get_only():
    from unified_agent.webui.routes import web_routes

    route = next(r for r in web_routes() if r.path == "/api/tasks/search")
    methods = {m for m in (route.methods or set()) if m != "HEAD"}
    assert methods == {"GET"}
