"""Tests for interactive agent canvas & action deck endpoints."""

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import registry, server, storage, webui

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
HUMAN_TOKEN = f"human-{secrets.token_urlsafe(8)}"
HUMAN_HEADERS = {"Authorization": f"Bearer {HUMAN_TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN},Desktop_Person:{HUMAN_TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


class TestInteractiveUI:
    def test_cycles_endpoint(self, client, monkeypatch):
        async def fake_cycles(**kwargs):
            return {"cycles": [{"id": "cycle-1", "state": "plan"}]}

        monkeypatch.setattr("unified_agent.webui.api.cycles_payload", fake_cycles)
        res = client.get("/api/cycles")
        assert res.status_code == 200
        assert res.json()["cycles"][0]["state"] == "plan"

    def test_cycle_action_requires_operation_fields(self, client):
        res = client.post(
            "/api/action/cycle",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"operation": "transition"},
        )
        assert res.status_code == 400
        assert "cycle_id" in res.json()["error"]

    def test_agent_graph_endpoint(self, client):
        res = client.get("/api/agent-graph")
        assert res.status_code == 200
        data = res.json()
        assert "nodes" in data
        assert "links" in data
        assert isinstance(data["nodes"], list)

    def test_overview_includes_agent_graph(self, client):
        res = client.get("/api/overview")
        assert res.status_code == 200
        data = res.json()
        assert "agent_graph" in data

    def test_whoami_control_enabled(self, client):
        res = client.get("/api/whoami")
        assert res.status_code == 200
        data = res.json()
        assert data.get("control_enabled") is True


class TestActionValidations:
    def test_action_task_validations(self, client):
        assert client.post("/api/action/task", content=b"invalid json").status_code == 400
        assert client.post("/api/action/task", json={"status": "doing"}).status_code == 400
        assert client.post("/api/action/task", json={"id": "not-int"}).status_code == 400
        assert (
            client.post("/api/action/task", json={"id": 1, "status": "invalid-status"}).status_code
            == 400
        )

    def test_action_approval_validations(self, client):
        assert (
            client.post(
                "/api/action/approval", headers=HUMAN_HEADERS, content=b"invalid json"
            ).status_code
            == 400
        )
        assert (
            client.post("/api/action/approval", headers=HUMAN_HEADERS, json={}).status_code == 400
        )
        assert (
            client.post(
                "/api/action/approval",
                headers=HUMAN_HEADERS,
                json={"id": 1, "decision": "invalid-dec"},
            ).status_code
            == 400
        )

    def test_action_workflow_validations(self, client):
        assert client.post("/api/action/workflow", content=b"invalid json").status_code == 400
        assert client.post("/api/action/workflow", json={}).status_code == 400
        assert client.post("/api/action/workflow", json={"name": 123}).status_code == 400
        assert (
            client.post(
                "/api/action/workflow", json={"name": "reconcile", "inputs": "string-not-dict"}
            ).status_code
            == 400
        )

    def test_action_csrf_origin_rejection(self, client):
        res = client.post(
            "/api/action/task",
            json={"id": 1, "status": "doing"},
            headers={"Origin": "http://evil.example"},
        )
        assert res.status_code == 403


class TestCookieCSRFValidation:
    def test_cookie_mutations_require_csrf_token(self, client):
        # Insert task 1 into db
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
                "VALUES (1, 'Test Task', 'notes', 'open', 'med', '2026-07-24T00:00:00', '2026-07-24T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        # 1. Login via bearer token to establish session cookie & get csrf token
        login_res = client.post("/api/session", json={"token": TOKEN})
        assert login_res.status_code == 200
        valid_csrf = login_res.json()["csrf"]

        # Verify whoami exposes csrf token
        who = client.get("/api/whoami").json()
        assert who["csrf"] == valid_csrf
        assert who["via"] == "cookie"

        # 2. Missing CSRF token -> rejected (403)
        res_missing = client.post("/api/action/task", json={"id": 1, "status": "doing"})
        assert res_missing.status_code == 403
        assert "CSRF" in res_missing.json()["error"]

        # 3. Incorrect CSRF token -> rejected (403)
        res_wrong = client.post(
            "/api/action/task",
            json={"id": 1, "status": "doing"},
            headers={"x-ua-csrf": "invalid-csrf-token"},
        )
        assert res_wrong.status_code == 403
        assert "CSRF" in res_wrong.json()["error"]

        # 4. Valid session + valid CSRF token -> accepted (200)
        res_valid = client.post(
            "/api/action/task",
            json={"id": 1, "status": "doing"},
            headers={"x-ua-csrf": valid_csrf},
        )
        assert res_valid.status_code == 200


class TestHeartbeatSkipping:
    def test_tool_call_without_identity_skips_heartbeat(self, monkeypatch):
        registry.reset_throttle()
        monkeypatch.delenv("UA_AGENT_LABEL", raising=False)
        monkeypatch.delenv("AGENT_LABEL", raising=False)

        wrapped = server._guarded_and_instrumented(lambda: {"ok": True})
        res = wrapped()
        assert res == {"ok": True}

        agent = registry.get_agent("Desktop_agent3")
        assert agent is None or agent["last_seen_via"] != "tool_call"


class TestApprovalIdValidation:
    def test_approval_id_non_integer_rejected(self, client):
        res = client.post(
            "/api/action/approval",
            headers=HUMAN_HEADERS,
            json={"id": "not-a-number", "decision": "approve"},
        )
        assert res.status_code == 400
        assert "invalid approval id" in res.json()["error"]

    def test_approval_id_dict_rejected(self, client):
        res = client.post(
            "/api/action/approval",
            headers=HUMAN_HEADERS,
            json={"id": {"nested": True}, "decision": "approve"},
        )
        assert res.status_code == 400
        assert "invalid approval id" in res.json()["error"]


class TestTaskKwargsFiltering:
    def test_unexpected_keys_rejected_not_dropped(self, client):
        """Extra keys not in task_update's signature must not cause a 500 —
        and since task #1246 they must not be silently dropped either: a typo
        like "notes_append" used to return a 200 while discarding the note.
        Unknown fields now fail closed with a 400 naming them."""
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
                "VALUES (999, 'Filter Test', 'n', 'open', 'med', '2026-07-31T00:00:00', '2026-07-31T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        res = client.post(
            "/api/action/task",
            json={"id": 999, "status": "doing", "evil_key": "pwned", "another": 42},
        )
        assert res.status_code == 400
        assert "evil_key" in res.json()["error"]
        assert "another" in res.json()["error"]


class TestGraphSubtaskLinkOrdering:
    def test_child_before_parent_still_linked(self):
        """Subtask links should be generated even if the child appears before its parent."""
        from unified_agent.webui import api

        agents_res = {"agents": []}
        # Child task (id=2, parent_id=1) appears before parent task (id=1)
        tasks_res = {
            "tasks": [
                {"id": 2, "title": "child", "status": "open", "priority": "med", "parent_id": 1},
                {"id": 1, "title": "parent", "status": "open", "priority": "med"},
            ]
        }
        delegations_res = {"delegations": []}

        import asyncio

        graph = asyncio.run(
            api.agent_graph_payload(
                agents_res, tasks_res, delegations_res, messages_res={"messages": []}
            )
        )

        subtask_links = [lnk for lnk in graph["links"] if lnk["kind"] == "subtask"]
        assert len(subtask_links) == 1
        assert subtask_links[0]["source"] == "task:1"
        assert subtask_links[0]["target"] == "task:2"


class TestAgentGraphOverlays:
    def test_message_links_omit_body_and_carry_act(self):
        from pathlib import Path

        from unified_agent import config
        from unified_agent.webui import api

        agents_res = {
            "agents": [
                {
                    "label": "Desktop_Agent4",
                    "status": "online",
                    "open_sessions": 0,
                    "active_tasks": 1,
                },
                {
                    "label": "Desktop_Agent5",
                    "status": "idle",
                    "open_sessions": 0,
                    "active_tasks": 0,
                },
            ]
        }
        tasks_res = {
            "tasks": [
                {"id": 479, "title": "umbrella", "status": "doing", "claimed_by": "Desktop_Agent4"}
            ]
        }
        messages_res = {
            "messages": [
                {
                    "from_agent": "Desktop_Agent4",
                    "to_agent": "Desktop_Agent5",
                    "act": "inform",
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "task_id": 479,
                    "body": "secret handoff text that must never reach the graph",
                }
            ]
        }
        workspaces = {
            "Desktop_Agent4": Path("/home/you/dev/unified_agent"),
            "Desktop_Agent5": config.WORKSPACE_DENIED,
        }

        import asyncio

        graph = asyncio.run(
            api.agent_graph_payload(
                agents_res,
                tasks_res,
                {"delegations": []},
                messages_res=messages_res,
                workspaces=workspaces,
            )
        )

        message_links = [lnk for lnk in graph["links"] if lnk["kind"] == "message"]
        assert len(message_links) == 1
        link = message_links[0]
        assert link["source"] == "agent:Desktop_Agent4"
        assert link["target"] == "agent:Desktop_Agent5"
        assert link["act"] == "inform"
        assert link["conversation_id"] == "conv-1"
        assert link["message_id"] == "msg-1"
        assert link["task_id"] == 479
        assert "body" not in link
        dumped = str(graph)
        assert "secret handoff text" not in dumped

        by_id = {node["id"]: node for node in graph["nodes"]}
        assert by_id["agent:Desktop_Agent4"]["workspace_binding"] == "bound"
        assert by_id["agent:Desktop_Agent4"]["workspace_root"] == "/home/you/dev/unified_agent"
        assert by_id["agent:Desktop_Agent5"]["workspace_binding"] == "denied"
        assert "workspace_root" not in by_id["agent:Desktop_Agent5"]
        for node in graph["nodes"]:
            assert "memory" not in node
            assert "notes" not in node

    def test_unbound_agent_has_no_workspace_root(self):
        import asyncio

        from unified_agent.webui import api

        graph = asyncio.run(
            api.agent_graph_payload(
                {"agents": [{"label": "Desktop_agent3", "status": "online"}]},
                {"tasks": []},
                {"delegations": []},
                messages_res={"messages": []},
                workspaces={},
            )
        )
        node = graph["nodes"][0]
        assert node["workspace_binding"] == "unbound"
        assert "workspace_root" not in node
