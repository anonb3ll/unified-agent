"""Route contract for /api/graph-canvas (task #807).

GET is an approval-free read (guard plane shared with every /api read); PUT is
a CSRF-gated mutation that stores the scene verbatim, attributed to the
verified identity. The route shares storage with the graph_canvas_read/write
MCP tools, so a write through one surface is visible to the other.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import graph_canvas

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
#: PUT carries the graph_canvas_write ceiling (task #1865): chief of staff holds it,
#: the developer default does not.
WRITER = "Laptop_Agent5"


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("Laptop_Agent5")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{WRITER}:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth(**kw):
    return {"Authorization": f"Bearer {TOKEN}", **kw}


class TestGraphCanvasRoute:
    def test_unknown_board_shape_is_400(self, client):
        res = client.get("/api/graph-canvas", params={"board": "../etc/passwd"})
        assert res.status_code == 400

    def test_get_unwritten_board_is_exists_false(self, client):
        res = client.get("/api/graph-canvas", params={"board": "t807-empty"}, headers=_auth())
        assert res.status_code == 200
        body = res.json()
        assert body["exists"] is False
        assert body["scene"] == {}

    def test_put_then_get_round_trip(self, client):
        scene = {
            "type": "excalidraw",
            "version": 2,
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "Ship it"},
            ],
        }
        put = client.put(
            "/api/graph-canvas",
            params={"board": "t807-ui"},
            headers=_auth(),
            json=scene,
        )
        assert put.status_code == 200
        assert put.json()["saved"] is True
        assert put.json()["updated_by"] == WRITER

        got = client.get("/api/graph-canvas", params={"board": "t807-ui"}, headers=_auth())
        assert got.status_code == 200
        body = got.json()
        assert body["exists"] is True
        assert body["scene"] == scene

    def test_put_rejects_non_object_scene(self, client):
        res = client.put(
            "/api/graph-canvas",
            params={"board": "t807-bad"},
            headers=_auth(),
            json=[1, 2, 3],
        )
        assert res.status_code == 400

    def test_put_rejects_non_array_elements(self, client):
        res = client.put(
            "/api/graph-canvas",
            params={"board": "t807-bad"},
            headers=_auth(),
            json={"elements": {"nope": True}},
        )
        assert res.status_code == 400

    def test_put_rejects_oversized_element_count(self, client):
        scene = {"elements": [{"id": f"n{i}", "type": "rectangle"} for i in range(1001)]}
        res = client.put(
            "/api/graph-canvas",
            params={"board": "t807-big"},
            headers=_auth(),
            json=scene,
        )
        assert res.status_code == 400

    def test_put_requires_auth_in_enforce_mode(self, monkeypatch):
        # In enforce mode an unauthenticated PUT must be refused before storage runs.
        monkeypatch.setenv("UA_AGENT_TOKENS", f"{WRITER}:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()

        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            res = c.put("/api/graph-canvas", params={"board": "t807-x"}, json={})
            assert res.status_code == 401

    def test_shared_storage_visible_to_mcp_tool(self, client):
        scene = {
            "elements": [
                {"id": "n1", "type": "rectangle"},
                {"id": "t1", "type": "text", "containerId": "n1", "text": "Shared"},
            ]
        }
        client.put(
            "/api/graph-canvas",
            params={"board": "t807-shared"},
            headers=_auth(),
            json=scene,
        )
        parsed = graph_canvas.graph_canvas_read(board_id="t807-shared")
        assert parsed["exists"] is True
        titles = [t["title"] for t in parsed["parsed"]["task_suggestions"]]
        assert titles == ["Shared"]

    def test_cookie_put_requires_csrf(self, client):
        login = client.post("/api/session", json={"token": TOKEN})
        assert login.status_code == 200
        csrf = login.json()["csrf"]
        scene = {"elements": []}

        missing = client.put("/api/graph-canvas", params={"board": "t807-csrf"}, json=scene)
        assert missing.status_code == 403
        assert "CSRF" in missing.json()["error"]

        ok = client.put(
            "/api/graph-canvas",
            params={"board": "t807-csrf"},
            headers={"x-ua-csrf": csrf},
            json=scene,
        )
        assert ok.status_code == 200
        assert ok.json()["saved"] is True
        assert ok.json()["updated_by"] == WRITER

    def test_mcp_tool_write_visible_via_route(self, client):
        from unified_agent import config as ua_config

        token = ua_config.agent_identity_var.set("Desktop_agent3")
        try:
            graph_canvas.graph_canvas_write(board_id="t807-tool", scene={"elements": []})
        finally:
            ua_config.agent_identity_var.reset(token)
        got = client.get("/api/graph-canvas", params={"board": "t807-tool"}, headers=_auth())
        assert got.status_code == 200
        assert got.json()["updated_by"] == "Desktop_agent3"


def test_put_refuses_a_profile_without_graph_canvas_write(monkeypatch):
    """The developer default lacks graph_canvas_write over MCP, so HTTP refuses it too."""
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent2:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        res = c.put(
            "/api/graph-canvas",
            json={"elements": []},
            headers=_auth(),
        )
        assert res.status_code == 403
        assert "graph_canvas_write" in res.json()["error"]
        assert c.get("/api/graph-canvas", headers=_auth()).status_code == 200
