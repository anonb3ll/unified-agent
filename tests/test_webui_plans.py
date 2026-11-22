"""Plans pane Control Center routes (task #1117).

Ground truth is the real plan store + CSRF machinery — list/detail/review are
asserted through the HTTP surface, not stubs. The generic platform object
reader cannot list plans and gates developer HTTP on legacy ``plan_get``, so
these dedicated routes wrap ``plan_manage`` for authenticated sessions.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.reviewer import WAITING_FOR_PLAN_APPROVAL
from unified_agent.tools.plan import plan_create, plan_manage
from unified_agent.tools.tasks import task_add, task_update

TOKEN = f"tok-plans-{secrets.token_urlsafe(8)}"


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


def _login(client: TestClient) -> str:
    res = client.post("/api/session", json={"token": TOKEN})
    assert res.status_code == 200, res.text
    return res.json()["csrf"]


def _gate_task(plan_id: str) -> int:
    notes = f"{WAITING_FOR_PLAN_APPROVAL}\nplan: {plan_id}"
    task_id = task_add("approve-my-plan", notes=notes)["id"]
    task_update(task_id, status="doing", worktree=False)
    return task_id


def _task_notes(task_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()["notes"]
    finally:
        conn.close()


class TestPlansReadRoutes:
    def test_list_returns_created_plans(self, client):
        created = plan_create(
            "Ship plans pane",
            [
                {"description": "list", "owner": "Desktop_Agent4"},
                {"description": "detail", "owner": "Desktop_Agent1"},
            ],
            author="Desktop_Agent4",
        )
        pid = created["plan_id"]

        res = client.get("/api/plans", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert "plans" in body
        match = next(p for p in body["plans"] if p["id"] == pid)
        assert match["goal"] == "Ship plans pane"
        assert match["status"] == "active"
        assert match["author"] == "Desktop_Agent4"
        assert match["needs_human"] is False

    def test_list_requires_auth(self, client):
        assert client.get("/api/plans").status_code == 401

    def test_detail_renders_steps_owners_and_status(self, client):
        created = plan_create(
            "Detail view",
            [
                {"description": "design", "owner": "Desktop_agent3"},
                {"description": "build", "owner": "Desktop_Agent1", "depends_on": [0]},
            ],
            author="Desktop_Agent5",
        )
        pid = created["plan_id"]

        res = client.get(f"/api/plans/{pid}", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["plan"]["id"] == pid
        assert body["plan"]["goal"] == "Detail view"
        assert body["plan"]["status"] == "active"
        assert body["plan"]["author"] == "Desktop_Agent5"
        assert len(body["steps"]) == 2
        assert body["steps"][0]["description"] == "design"
        assert body["steps"][0]["owner"] == "Desktop_agent3"
        assert body["steps"][0]["status"] == "open"
        assert body["steps"][1]["owner"] == "Desktop_Agent1"
        assert "outputs" in body["steps"][0]
        assert "next_step_idx" in body

    def test_detail_unknown_plan_is_404(self, client):
        res = client.get("/api/plans/does-not-exist", headers=_auth())
        assert res.status_code == 404, res.text
        assert res.json()["error"]


class TestPlansReviewAction:
    def test_cookie_post_requires_csrf(self, client):
        pid = plan_create("g", ["a"])["plan_id"]
        tid = _gate_task(pid)
        csrf = _login(client)

        missing = client.post(
            "/api/action/plan",
            json={"intent": f"plan-review v1 approve {pid}", "task_id": tid},
        )
        assert missing.status_code == 403, missing.text
        assert "CSRF" in missing.json()["error"]

        ok = client.post(
            "/api/action/plan",
            headers={"x-ua-csrf": csrf},
            json={"intent": f"plan-review v1 approve {pid}", "task_id": tid},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["applied"] is True
        assert body["action"] == "approve"
        assert WAITING_FOR_PLAN_APPROVAL not in _task_notes(tid)
        assert "[plan: approved @" in _task_notes(tid)

    def test_invalid_transition_surfaces_as_error(self, client):
        """A plan_manage('review') refusal must be visible, not silent success."""
        pid = plan_create("g", ["a"])["plan_id"]
        # No gate task — approve must refuse.
        res = client.post(
            "/api/action/plan",
            headers=_auth(),
            json={"intent": f"plan-review v1 approve {pid}"},
        )
        assert res.status_code == 400, res.text
        err = res.json()["error"]
        assert err
        assert "waiting-for: plan-approval" in err or "not on the" in err

        notes_blob = " ".join(
            t.get("notes") or ""
            for t in __import__("unified_agent.tools.tasks", fromlist=["task_list"]).task_list(
                status="all"
            )["tasks"]
        )
        assert "[plan: approved @" not in notes_blob

    def test_bearer_review_approve_happy_path(self, client):
        pid = plan_create("g", ["a", "b"])["plan_id"]
        tid = _gate_task(pid)
        res = client.post(
            "/api/action/plan",
            headers=_auth(),
            json={"intent": f"plan-review v1 approve {pid}", "task_id": tid},
        )
        assert res.status_code == 200, res.text
        assert res.json()["applied"] is True
        # Same underlying store the MCP tool mutates.
        second = plan_manage("review", {"intent": f"plan-review v1 approve {pid}", "task_id": tid})
        assert second["already"] is True
