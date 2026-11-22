"""Authenticated HTTP write contracts for the typed SDK (#1282)."""

import secrets
from contextlib import contextmanager

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage
from unified_agent.tools.approvals import ApprovalPendingError
from unified_agent.webui import routes


@contextmanager
def _as_verified(label: str):
    """Act as an authenticated caller. Since #869, approving in enforced mode
    needs a verified human identity; a ``resolved_by`` string is not enough."""
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


AGENT_TOKEN = f"agent-{secrets.token_urlsafe(8)}"
HUMAN_TOKEN = f"human-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"Desktop_Agent2:{AGENT_TOKEN},Desktop_Person:{HUMAN_TOKEN}"
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=routes.web_routes()))
    with TestClient(app) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTaskWrites:
    def test_create_uses_registered_tool_and_verified_identity(self, client, monkeypatch):
        seen = {}

        def task_add(**kwargs):
            seen.update(kwargs)
            from unified_agent import config

            seen["verified_identity"] = config.verified_agent_identity()
            return {"id": 912}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", task_add)
        response = client.post(
            "/api/tasks",
            headers=_bearer(AGENT_TOKEN),
            json={"title": "SDK task", "priority": "high", "project": "unified-agent"},
        )

        assert response.status_code == 201
        assert response.json() == {"id": 912}
        assert seen == {
            "title": "SDK task",
            "priority": "high",
            "project": "unified-agent",
            "verified_identity": "Desktop_Agent2",
        }

    def test_create_requires_auth_even_when_request_is_well_formed(self, client):
        response = client.post("/api/tasks", json={"title": "anonymous task"})
        assert response.status_code == 401

    def test_create_rejects_caller_supplied_identity(self, client, monkeypatch):
        invoked = False

        def task_add(**_kwargs):
            nonlocal invoked
            invoked = True
            return {"id": 1}

        monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", task_add)
        response = client.post(
            "/api/tasks",
            headers=_bearer(AGENT_TOKEN),
            json={"title": "SDK task", "agent": "Desktop_Person"},
        )
        assert response.status_code == 400
        assert not invoked

    def test_gated_create_returns_pending_approval_without_execution(self, client, monkeypatch):
        monkeypatch.setitem(
            server.REGISTERED_TOOLS,
            "task_add",
            lambda **_kwargs: (_ for _ in ()).throw(
                ApprovalPendingError("queued", approval_id=914)
            ),
        )
        response = client.post(
            "/api/tasks", headers=_bearer(AGENT_TOKEN), json={"title": "approval task"}
        )
        assert response.status_code == 202
        assert response.json()["approval_id"] == 914
        assert response.json()["status"] == "pending"

    def test_update_forwards_sdk_task_update_fields_and_returns_tool_result(
        self, client, monkeypatch
    ):
        from unified_agent import config

        seen = {}

        async def update_task(**kwargs):
            seen.update(kwargs)
            seen["verified_identity"] = config.verified_agent_identity()
            return {"id": 17, "project": "platform", "due_at": "2030-01-01T00:00:00Z"}

        monkeypatch.setattr(routes.api, "update_task", update_task)
        body = {
            "id": 17,
            "status": "doing",
            "title": "Typed update",
            "parent_id": 4,
            "project": "platform",
            "expected_project": "backlog",
            "domain": None,
            "lane": "platform",
            "due_at": "2030-01-01T00:00:00Z",
            "clear_due_at": False,
            "evidence_ref": "https://example.test/evidence/17",
            "idempotency_key": "sdk-update-17",
        }
        response = client.post("/api/action/task", headers=_bearer(AGENT_TOKEN), json=body)

        assert response.status_code == 200
        assert response.json() == {
            "id": 17,
            "project": "platform",
            "due_at": "2030-01-01T00:00:00Z",
        }
        assert seen == {
            "task_id": 17,
            "actor": "Desktop_Agent2",
            "status": "doing",
            "title": "Typed update",
            "parent_id": 4,
            "project": "platform",
            "expected_project": "backlog",
            "domain": None,
            "lane": "platform",
            "due_at": "2030-01-01T00:00:00Z",
            "clear_due_at": False,
            "evidence_ref": "https://example.test/evidence/17",
            "idempotency_key": "sdk-update-17",
            "verified_identity": "Desktop_Agent2",
        }

    @pytest.mark.parametrize("status", ["waiting_review", "cancelled"])
    def test_update_accepts_all_sdk_task_statuses(self, client, monkeypatch, status):
        async def update_task(**kwargs):
            return {"id": kwargs["task_id"], "status": kwargs["status"]}

        monkeypatch.setattr(routes.api, "update_task", update_task)
        response = client.post(
            "/api/action/task",
            headers=_bearer(AGENT_TOKEN),
            json={"id": 17, "status": status},
        )

        assert response.status_code == 200
        assert response.json() == {"id": 17, "status": status}

    def test_gated_update_returns_pending_approval_without_execution(self, client, monkeypatch):
        async def update_task(**_kwargs):
            raise ApprovalPendingError("queued", approval_id=918)

        monkeypatch.setattr(routes.api, "update_task", update_task)
        body = {"id": 17, "status": "doing", "expected_project": "platform"}
        response = client.post("/api/action/task", headers=_bearer(AGENT_TOKEN), json=body)

        assert response.status_code == 202
        assert response.json() == {
            "approval_id": 918,
            "status": "pending",
            "code": "approval_required",
            "tool": "task_update",
            "payload": {"id": 17, "status": "doing", "expected_project": "platform"},
        }


class TestApprovalWrites:
    def test_denied_request_returns_typed_decision_not_error(self, client, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        body = {"tool": "run_cmd", "payload": {"command": "rm -rf build"}}
        first = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
        client.post(
            "/api/action/approval",
            headers=_bearer(HUMAN_TOKEN),
            json={"id": first.json()["approval_id"], "decision": "deny", "reason": "no"},
        )
        again = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
        assert again.status_code == 200
        assert again.json()["status"] == "denied"

    def test_omitted_payload_defaults_to_empty_object(self, client, monkeypatch):
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        response = client.post(
            "/api/approvals/request", headers=_bearer(AGENT_TOKEN), json={"tool": "run_cmd"}
        )
        assert response.status_code == 202
        assert response.json()["payload"] == {}

    def test_concurrent_identical_requests_share_one_pending_row(self, monkeypatch):
        import threading

        from unified_agent.tools import approvals

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        storage.init_db()
        ids: list[int] = []
        barrier = threading.Barrier(8)

        def request():
            barrier.wait()
            result = approvals.check_approval_gate(
                "run_cmd", {"command": "deploy"}, requested_by="Desktop_Agent2", request_only=True
            )
            ids.append(result["approval_id"])

        threads = [threading.Thread(target=request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(ids)) == 1

    def test_http_request_is_reused_and_an_approved_pass_is_not_consumed(self, client, monkeypatch):
        from unified_agent.tools import approvals

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        body = {"tool": "run_cmd", "payload": {"command": "git status"}}
        first = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
        second = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
        assert first.status_code == 202
        assert second.status_code == 202
        approval_id = first.json()["approval_id"]
        assert second.json()["approval_id"] == approval_id
        assert approvals.get_approval(approval_id)["requested_by"] == "Desktop_Agent2"

        resolved = client.post(
            "/api/action/approval",
            headers=_bearer(HUMAN_TOKEN),
            json={"id": approval_id, "decision": "approve"},
        )
        assert resolved.status_code == 200
        approved = client.post("/api/approvals/request", headers=_bearer(AGENT_TOKEN), json=body)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approvals.get_approval(approval_id)["status"] == "approved"

    def test_agent_cannot_resolve_approval(self, client, monkeypatch):
        invoked = False

        async def resolve_approval(**_kwargs):
            nonlocal invoked
            invoked = True
            return {"id": 9, "status": "approved"}

        monkeypatch.setattr(routes.api, "resolve_platform_approval", resolve_approval)
        response = client.post(
            "/api/action/approval",
            headers=_bearer(AGENT_TOKEN),
            json={"id": 9, "decision": "approve"},
        )
        assert response.status_code == 403
        assert not invoked

    def test_human_resolution_uses_registered_tool_wrapper(self, client, monkeypatch):
        seen = {}

        async def approval_resolve(**kwargs):
            from unified_agent import config

            seen.update(kwargs)
            seen["verified_identity"] = config.verified_agent_identity()
            return {"id": 9, "status": "approved", "resolved_by": "Desktop_Person"}

        monkeypatch.setattr(routes.api, "resolve_platform_approval", approval_resolve)
        response = client.post(
            "/api/action/approval",
            headers=_bearer(HUMAN_TOKEN),
            json={"id": 9, "decision": "approve"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        assert seen["verified_identity"] == "Desktop_Person"
        assert seen["actor"] == "Desktop_Person"

    def test_request_only_does_not_consume_an_approved_pass(self, client, monkeypatch):
        from unified_agent.tools import approvals

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        pending = approvals.check_approval_gate(
            "run_cmd", {"command": "git status"}, requested_by="Desktop_Agent2", request_only=True
        )
        assert pending["status"] == "pending"
        with _as_verified("Desktop_Person"):
            approvals.approval_resolve(
                pending["approval_id"], "approve", resolved_by="Desktop_Person"
            )
        current = approvals.check_approval_gate(
            "run_cmd", {"command": "git status"}, requested_by="Desktop_Agent2", request_only=True
        )
        assert current["status"] == "approved"
        assert approvals.get_approval(pending["approval_id"])["status"] == "approved"

    def test_request_gates_but_does_not_execute(self, client, monkeypatch):
        from unified_agent import object_model

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        seen = {}

        def request_gate(tool, payload, requested_by=None, force=False, request_only=False):
            seen.update(
                tool=tool,
                payload=payload,
                requested_by=requested_by,
                force=force,
                request_only=request_only,
            )
            return {"gated": True, "approval_id": 42, "status": "pending"}

        fake_object = type("Model", (), {"writer": lambda self, _action: request_gate})()
        monkeypatch.setattr(object_model, "by_kind", lambda _kind: fake_object)
        response = client.post(
            "/api/approvals/request",
            headers=_bearer(AGENT_TOKEN),
            json={"tool": "run_cmd", "payload": {"command": "git status"}},
        )
        assert response.status_code == 202
        assert response.json() == {
            "approval_id": 42,
            "status": "pending",
            "tool": "run_cmd",
            "payload": {"command": "git status"},
        }
        assert seen["request_only"] is True
        assert seen["requested_by"] == "Desktop_Agent2"


class TestEvidenceWrites:
    def test_append_uses_registered_writer_and_verified_originator(self, client, monkeypatch):
        from unified_agent import object_model

        seen = {}

        def append(channel, *, payload=None, originator=None, **kwargs):
            seen.update(channel=channel, payload=payload, originator=originator, **kwargs)
            return "01JSDKWRITE0000000000000000", True

        monkeypatch.setattr(
            object_model,
            "by_kind",
            lambda _kind: type(
                "Model",
                (),
                {"profile_tool": "event_log_query", "writer": lambda self, _action: append},
            )(),
        )
        response = client.post(
            "/api/evidence",
            headers=_bearer(AGENT_TOKEN),
            json={
                "channel": "sdk.evidence",
                "payload": {"task_id": 9, "result": "verified"},
                "dedupe_key": "sdk:task-9",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json() == {"event_ulid": "01JSDKWRITE0000000000000000", "inserted": True}
        assert seen == {
            "channel": "sdk.evidence",
            "payload": {"task_id": 9, "result": "verified"},
            "originator": "Desktop_Agent2",
            "dedupe_key": "sdk:task-9",
        }

    def test_append_requires_auth(self, client):
        response = client.post("/api/evidence", json={"channel": "sdk.evidence", "payload": {}})
        assert response.status_code == 401

    def test_http_append_persists_verified_originator(self, client):
        from unified_agent import events

        response = client.post(
            "/api/evidence",
            headers=_bearer(AGENT_TOKEN),
            json={"channel": "sdk.evidence", "payload": {"task_id": 9}},
        )
        assert response.status_code == 201
        stored = next(
            row
            for row in events.query_events(channels=["sdk.evidence"])
            if row["ulid"] == response.json()["event_ulid"]
        )
        assert stored["originator"] == "Desktop_Agent2"
        assert stored["payload"] == {"task_id": 9}


class TestCombinedGates:
    def test_policy_approval_is_not_requeued_by_the_generic_gate(self, client, monkeypatch):
        """A write gated by both a policy rule and UA_APPROVAL_TOOLS uses ONE pass."""
        from unified_agent import safety
        from unified_agent.tools import approvals
        from unified_agent.webui import api

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "evidence_event.append")
        policy_payload = {"channel": "sdk.test", "action": "deploy", "agent": "Desktop_Agent2"}

        def policy_gate(tool, payload, originator=None):
            # Same shape as safety.check_policy_gate: its payload carries
            # action/agent, so it never matches the generic gate's key.
            approvals.check_approval_gate(tool, policy_payload, requested_by=originator, force=True)

        monkeypatch.setattr(safety, "check_policy_gate", policy_gate)
        with pytest.raises(ApprovalPendingError) as pending:
            policy_gate("evidence_event.append", {}, originator="Desktop_Agent2")
        with _as_verified("Operator"):
            approvals.approval_resolve(pending.value.approval_id, "approve", resolved_by="Operator")

        wrote = {}

        class _Writer:
            def writer(self, action):
                return lambda **kwargs: wrote.update(kwargs) or {"ok": True}

            profile_tool = "event_log_query"

        monkeypatch.setattr(api.object_model, "by_kind", lambda kind: _Writer())
        result = api._registered_platform_write(
            "evidence_event",
            "append",
            actor="Desktop_Agent2",
            operation="evidence_event.append",
            production=False,
            channel="sdk.test",
        )
        assert result == {"ok": True}
        assert wrote == {"channel": "sdk.test"}
        assert approvals.approval_pending()["pending"] == []
