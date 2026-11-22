"""Actual transport precondition acceptance and remaining invocation baselines.

The HTTP revision field reaches the existing registered task writer and shares
its optimistic revision rule with MCP. The core remains unregistered; the
post-commit wrapper evidence probe still records a remaining integration gap.
"""

import json
import secrets

import pytest
from mcp.client import Client
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, gateway, server, storage, webui
from unified_agent.tools.tasks import task_add


@pytest.fixture
def http(monkeypatch):
    # In-memory test credential configuration only; no fleet credential is
    # issued, persisted, read from a file or transmitted outside TestClient.
    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"server_agent2:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, {"Authorization": f"Bearer {token}"}


def current_task(identifier):
    with storage.connect() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone())


@pytest.mark.asyncio
async def test_existing_registered_mcp_and_actual_http_title_replay_baseline(http):
    task = task_add("application baseline")
    args = {"id": task["id"], "title": "through MCP", "idempotency_key": "application-key"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        async with Client(server.mcp) as client:
            called = await client.session.call_tool("task_update", args)
        assert not called.is_error
        first = json.loads(called.content[0].text)
    finally:
        config.agent_identity_var.reset(identity)
    transport, headers = http
    response = transport.post("/api/action/task", json=args, headers=headers)
    assert response.status_code == 200
    assert response.json() == first
    assert current_task(task["id"])["title"] == "through MCP"


@pytest.mark.asyncio
async def test_actual_http_revision_precondition_matches_registered_mcp(http):
    task = task_add("revision route parity")
    args = {
        "id": task["id"],
        "title": "revision proof",
        "expected_updated_at": current_task(task["id"])["updated_at"],
    }
    transport, headers = http
    response = transport.post("/api/action/task", json=args, headers=headers)
    assert response.status_code == 200
    assert response.json()["title"] == "revision proof"
    # The same revision rule must reject a later new write with stale authority.
    stale = transport.post(
        "/api/action/task", json={**args, "title": "stale write"}, headers=headers
    )
    # A stale revision is a typed conflict on HTTP too (task #2056): 409 + code.
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"
    assert current_task(task["id"])["title"] == "revision proof"
    args["expected_updated_at"] = current_task(task["id"])["updated_at"]
    args["title"] = "through registered MCP"
    identity = config.agent_identity_var.set("server_agent2")
    try:
        async with Client(server.mcp) as client:
            called = await client.session.call_tool("task_update", args)
        assert not called.is_error
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "through registered MCP"


def test_actual_http_authentication_and_unknown_field_refuse_before_write(http):
    task = task_add("auth baseline")
    transport, headers = http
    unauthorized = transport.post("/api/action/task", json={"id": task["id"], "title": "denied"})
    assert unauthorized.status_code == 401
    malformed = transport.post(
        "/api/action/task", json={"id": task["id"], "unknown_argument": "ignored?"}, headers=headers
    )
    assert malformed.status_code == 400
    assert current_task(task["id"])["title"] == "auth baseline"


def test_actual_registered_task_gateway_failure_rolls_back_mutation_and_receipt(monkeypatch):
    task = task_add("atomic wrapper")
    original = events.record_event_once_on_connection

    def fail_gateway(conn, channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic required gateway append failure")
        return original(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_gateway)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(gateway.LedgerWriteError):
            server.REGISTERED_TOOLS["task_update"](
                id=task["id"], title="must roll back", idempotency_key="atomic-wrapper-key"
            )
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "atomic wrapper"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "atomic-wrapper-key", "task_update") is None


def test_actual_registered_task_commit_needs_no_second_mandatory_gateway_append(monkeypatch):
    task = task_add("committed acknowledgement")
    original = events.record_event

    def fail_post_body_append(channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic unavailable post-body evidence connection")
        return original(channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event", fail_post_body_append)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        result = server.REGISTERED_TOOLS["task_update"](
            id=task["id"], title="durable outcome", idempotency_key="commit-ack-key"
        )
    finally:
        config.agent_identity_var.reset(identity)
    assert result["title"] == current_task(task["id"])["title"] == "durable outcome"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "commit-ack-key", "task_update") == result
    outcomes = gateway_payloads()
    assert len(outcomes) == 1 and outcomes[0]["decision"] == "allow"


def test_actual_http_task_gateway_failure_rolls_back_mutation_and_receipt(http, monkeypatch):
    task = task_add("HTTP atomic wrapper")
    original = events.record_event_once_on_connection

    def fail_gateway(conn, channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic HTTP transactional gateway failure")
        return original(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_gateway)
    transport, headers = http
    response = transport.post(
        "/api/action/task",
        json={"id": task["id"], "title": "must roll back", "idempotency_key": "http-atomic-key"},
        headers=headers,
    )
    assert response.status_code == 400
    assert current_task(task["id"])["title"] == "HTTP atomic wrapper"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "http-atomic-key", "task_update") is None


def gateway_payloads():
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY rowid",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_caller_owned_gateway_append_matches_normal_canonical_outcome():
    storage.init_db()
    arguments = {"id": 1, "title": "canonical outcome", "idempotency_key": "fixture"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update") as call:
            gateway.record_call_result(
                "task_update", call, None, {"id": 1}, arguments=arguments, mutating=True
            )
        with gateway.invocation("task_update") as call, storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identifier = gateway.record_call_result_on_connection(
                conn,
                "task_update",
                None,
                {"id": 1},
                arguments=arguments,
                dedupe_key="fixture:owned-outcome",
            )
            assert isinstance(identifier, str)
            assert conn.in_transaction
            # Preparing evidence cannot acknowledge a commit that has not happened.
            assert not call.decision_recorded
    finally:
        config.agent_identity_var.reset(identity)
    ordinary, owned = gateway_payloads()
    assert ordinary == owned
    assert owned["decision"] == "allow"
    assert owned["args_sha256"] == gateway.arguments_sha256(arguments)
    assert events.verify_event_chain()["status"] == "valid"


def test_owned_gateway_append_rolls_back_with_real_task_mutation():
    task = task_add("atomic outcome rollback")
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update") as call, storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET title='uncommitted' WHERE id=?", (task["id"],))
            gateway.record_call_result_on_connection(
                conn,
                "task_update",
                None,
                arguments={"id": task["id"], "title": "uncommitted"},
                dedupe_key="fixture:rolled-back-outcome",
            )
            conn.rollback()
            assert not call.decision_recorded
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "atomic outcome rollback"
    assert gateway_payloads() == []


def test_owned_gateway_failure_keeps_caller_transaction_uncommitted(monkeypatch):
    task = task_add("atomic outcome failure")
    identity = config.agent_identity_var.set("server_agent2")
    original = events.record_event_once_on_connection

    def fail_gateway(conn, channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic transactional gateway append failure")
        return original(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_gateway)
    try:
        with gateway.invocation("task_update") as call, storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET title='uncommitted' WHERE id=?", (task["id"],))
            with pytest.raises(gateway.LedgerWriteError):
                gateway.record_call_result_on_connection(
                    conn,
                    "task_update",
                    None,
                    arguments={"id": task["id"], "title": "uncommitted"},
                    dedupe_key="fixture:failed-outcome",
                )
            assert conn.in_transaction and not call.decision_recorded
            conn.rollback()
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "atomic outcome failure"
    assert gateway_payloads() == []


def test_owned_gateway_rejects_missing_transaction_without_implicit_commit():
    storage.init_db()
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update"), storage.connect() as conn:
            with pytest.raises(gateway.LedgerWriteError):
                gateway.record_call_result_on_connection(
                    conn, "task_update", None, arguments={"id": 1}, dedupe_key="fixture:no-txn"
                )
            assert not conn.in_transaction
    finally:
        config.agent_identity_var.reset(identity)
    assert gateway_payloads() == []


def test_owned_gateway_rejects_wrong_invocation_tool():
    storage.init_db()
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update"), storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with pytest.raises(gateway.LedgerWriteError):
                gateway.record_call_result_on_connection(
                    conn, "memory_save", None, arguments={"id": 1}, dedupe_key="fixture:wrong-tool"
                )
            conn.rollback()
    finally:
        config.agent_identity_var.reset(identity)
    assert gateway_payloads() == []


def test_owned_gateway_dedupe_refuses_a_different_canonical_outcome():
    storage.init_db()
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update"), storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            first = gateway.record_call_result_on_connection(
                conn, "task_update", None, arguments={"id": 1}, dedupe_key="fixture:canonical-key"
            )
            assert (
                gateway.record_call_result_on_connection(
                    conn,
                    "task_update",
                    None,
                    arguments={"id": 1},
                    dedupe_key="fixture:canonical-key",
                )
                == first
            )
            with pytest.raises(gateway.LedgerWriteError):
                gateway.record_call_result_on_connection(
                    conn,
                    "task_update",
                    None,
                    arguments={"id": 2},
                    dedupe_key="fixture:canonical-key",
                )
            conn.rollback()
    finally:
        config.agent_identity_var.reset(identity)
    assert gateway_payloads() == []


@pytest.mark.parametrize(
    "boundary", ["channel", "originator", "episode_id", "causal_parent_id", "org_id"]
)
def test_owned_gateway_dedupe_cannot_alias_another_evidence_boundary(boundary, monkeypatch):
    from unified_agent import edition

    storage.init_db()
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update") as call:
            gateway.record_call_result(
                "task_update", call, None, arguments={"id": 1}, mutating=True
            )
        payload = gateway_payloads()[0]
        with gateway.invocation("task_update"), storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attributes = {
                "channel": gateway.CHANNEL_GATEWAY_DECISION,
                "originator": "server_agent2",
                "episode_id": None,
                "causal_parent_id": None,
            }
            # Prepare a different immutable row through the real append port.
            # Do not rewrite the append-only ledger or disable its triggers.
            with monkeypatch.context() as context:
                if boundary == "org_id":
                    context.setattr(edition, "tenant_org_id", lambda: "other")
                else:
                    attributes[boundary] = "other"
                events.record_event_once_on_connection(
                    conn, payload=payload, dedupe_key="fixture:boundary", **attributes
                )
            with pytest.raises(gateway.LedgerWriteError):
                gateway.record_call_result_on_connection(
                    conn, "task_update", None, arguments={"id": 1}, dedupe_key="fixture:boundary"
                )
            conn.rollback()
    finally:
        config.agent_identity_var.reset(identity)
    assert gateway_payloads() == [payload]


def test_owned_gateway_requires_an_actual_current_invocation():
    storage.init_db()
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(gateway.LedgerWriteError):
            gateway.record_call_result_on_connection(
                conn, "task_update", None, arguments={"id": 1}, dedupe_key="fixture:no-invocation"
            )
        conn.rollback()
    assert gateway_payloads() == []


@pytest.mark.asyncio
async def test_actual_mcp_task_gateway_failure_rolls_back_mutation_and_receipt(monkeypatch):
    task = task_add("MCP atomic wrapper")
    original = events.record_event_once_on_connection

    def fail_gateway(conn, channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic MCP transactional gateway failure")
        return original(conn, channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_gateway)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        async with Client(server.mcp) as client:
            result = await client.session.call_tool(
                "task_update",
                {"id": task["id"], "title": "must roll back", "idempotency_key": "mcp-atomic-key"},
            )
        assert result.is_error
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "MCP atomic wrapper"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "mcp-atomic-key", "task_update") is None


def test_actual_sqlite_commit_failure_does_not_acknowledge_task_outcome(monkeypatch):
    import sqlite3

    task = task_add("failed actual SQLite commit")
    with storage.connect() as conn:
        conn.execute("CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE fixture_commit_fault (parent_id INTEGER REFERENCES fixture_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
    original_connect = storage.connect

    def enforcing_connection():
        conn = original_connect()
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    original_append = gateway.record_call_result_on_connection
    invocations = []

    def append_then_defer_violation(conn, *args, **kwargs):
        result = original_append(conn, *args, **kwargs)
        invocations.append(gateway._CURRENT.get())
        conn.execute("INSERT INTO fixture_commit_fault (parent_id) VALUES (999)")
        return result

    monkeypatch.setattr(storage, "connect", enforcing_connection)
    monkeypatch.setattr(gateway, "record_call_result_on_connection", append_then_defer_violation)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            server.REGISTERED_TOOLS["task_update"](
                id=task["id"], title="must roll back", idempotency_key="failed-commit-key"
            )
    finally:
        config.agent_identity_var.reset(identity)
    assert len(invocations) == 1
    # The wrapper may acknowledge its subsequent ERROR evidence, but no
    # committed ALLOW exists and both the real mutation and receipt rolled back.
    assert current_task(task["id"])["title"] == "failed actual SQLite commit"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "failed-commit-key", "task_update") is None
        assert conn.execute("SELECT COUNT(*) FROM fixture_commit_fault").fetchone()[0] == 0
    assert all(outcome["decision"] != "allow" for outcome in gateway_payloads())


@pytest.fixture
def verified_workos_transport(http, monkeypatch):
    from unified_agent import workos_hub_auth
    from unified_agent.pilot_auth import WorkOSClaims

    class Provider:
        organization_id = "org-a"
        subject = "fixture-subject"

        def authenticate_sealed(self, sealed_session):
            assert sealed_session == "fixture-session"
            return WorkOSClaims(
                self.subject,
                self.organization_id,
                session_id="fixture-idp-session",
                expires_at=4102444800,
            )

        def active(self, claims):
            return True

    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org-a")
    provider = Provider()
    leg = workos_hub_auth.WorkOSHubAuth(
        provider, organization_id="org-a", identity_map={"fixture-subject": "server_agent2"}
    )
    monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: leg)
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: True)
    return http[0], {"Authorization": "WorkOS fixture-session"}, provider, leg


@pytest.mark.parametrize("foreign", [False, True])
def test_actual_workos_http_reconciles_verified_request_and_storage_tenant(
    verified_workos_transport, monkeypatch, foreign
):
    transport, headers, provider, leg = verified_workos_transport
    task = task_add("verified tenant HTTP")
    if foreign:
        provider.organization_id = "org-b"
        leg.organization_id = "org-b"
    response = transport.post(
        "/api/action/task",
        json={"id": task["id"], "title": "verified outcome", "idempotency_key": "workos-http-key"},
        headers=headers,
    )
    assert response.status_code == (400 if foreign else 200)
    if foreign:
        assert "organization does not match" in response.json()["error"]
    assert current_task(task["id"])["title"] == (
        "verified tenant HTTP" if foreign else "verified outcome"
    )
    with storage.connect() as conn:
        allows = conn.execute(
            "SELECT org_id FROM event_log WHERE channel=? AND json_extract(payload, '$.decision')='allow'",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchall()
    assert [row[0] for row in allows] == ([] if foreign else ["org-a"])


@pytest.mark.parametrize("foreign_resource", ["org-b", None])
def test_actual_workos_http_refuses_foreign_or_null_task_before_receipt_replay(
    verified_workos_transport, monkeypatch, foreign_resource
):
    transport, headers, _, _ = verified_workos_transport
    if foreign_resource:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", foreign_resource)
    else:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    task = task_add("foreign resource HTTP")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org-a")
    response = transport.post(
        "/api/action/task",
        json={"id": task["id"], "title": "forbidden", "idempotency_key": "foreign-http-key"},
        headers=headers,
    )
    assert response.status_code == 400
    assert "no task with id" in response.json()["error"]
    assert current_task(task["id"])["title"] == "foreign resource HTTP"


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_actual_authenticated_workos_mcp_http_tenant_boundary(
    verified_workos_transport, foreign
):
    import httpx

    _, headers, provider, leg = verified_workos_transport
    task = task_add("verified tenant MCP")
    if foreign:
        provider.organization_id = "org-b"
        leg.organization_id = "org-b"
    app = server.mcp.streamable_http_app()
    headers = {**headers, "Accept": "application/json, text/event-stream"}
    async with (
        server.mcp.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers=headers,
        ) as client,
    ):
        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "invocation1973", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        client.headers["mcp-session-id"] = session_id
        notified = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert notified.status_code == 202
        called = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "task_update",
                    "arguments": {
                        "id": task["id"],
                        "title": "verified MCP outcome",
                        "idempotency_key": "workos-mcp-key",
                    },
                },
            },
        )
        assert called.status_code == 200
        if called.headers.get("content-type", "").startswith("text/event-stream"):
            body = json.loads(
                next(line[6:] for line in called.text.splitlines() if line.startswith("data: "))
            )
        else:
            body = called.json()
        assert body["result"].get("isError", False) is foreign
        if foreign:
            assert "organization does not match" in body["result"]["content"][0]["text"]
    assert current_task(task["id"])["title"] == (
        "verified tenant MCP" if foreign else "verified MCP outcome"
    )


def test_actual_registered_committed_replay_after_later_edit_needs_no_new_evidence(monkeypatch):
    from unified_agent.tools.tasks import task_update

    task = task_add("lost response")
    arguments = {
        "id": task["id"],
        "title": "original outcome",
        "idempotency_key": "lost-response-key",
        "expected_updated_at": current_task(task["id"])["updated_at"],
    }
    identity = config.agent_identity_var.set("server_agent2")
    try:
        original = server.REGISTERED_TOOLS["task_update"](**arguments)
        task_update(task["id"], title="later edit")
        initial_outcomes = gateway_payloads()
        append = events.record_event

        def unavailable_gateway(channel, *args, **kwargs):
            if channel == gateway.CHANNEL_GATEWAY_DECISION:
                raise RuntimeError("synthetic unavailable evidence on committed replay")
            return append(channel, *args, **kwargs)

        monkeypatch.setattr(events, "record_event", unavailable_gateway)
        replay = server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert replay == original
    assert current_task(task["id"])["title"] == "later edit"
    assert gateway_payloads() == initial_outcomes


def approve_registered_task(arguments, monkeypatch):
    from unified_agent.tools import approvals

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_update")
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    operator = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(operator)
    return pending.value.approval_id


@pytest.mark.parametrize("failure", ["gateway", "revision"])
def test_registered_task_failed_transaction_restores_generic_pass(monkeypatch, failure):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("generic approval transaction")
    arguments = {"id": task["id"], "title": "uncommitted", "idempotency_key": "grant-rollback"}
    if failure == "revision":
        arguments["expected_updated_at"] = "stale-authority"
    approval_id = approve_registered_task(arguments, monkeypatch)
    original = events.record_event_once_on_connection

    def fail_gateway(conn, channel, *args, **kwargs):
        if channel == gateway.CHANNEL_GATEWAY_DECISION:
            raise RuntimeError("synthetic transaction evidence failure")
        return original(conn, channel, *args, **kwargs)

    if failure == "gateway":
        monkeypatch.setattr(events, "record_event_once_on_connection", fail_gateway)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(ToolError):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "generic approval transaction"
    assert approvals.get_approval(approval_id)["status"] == "approved"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "grant-rollback", "task_update") is None


def test_registered_task_revalidates_generic_grant_under_owner_transaction(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("grant changed before mutation")
    arguments = {"id": task["id"], "title": "unauthorized", "idempotency_key": "changed-grant"}
    approval_id = approve_registered_task(arguments, monkeypatch)
    original = storage.connect
    revoked = False

    def revoke_between_gate_and_owner():
        nonlocal revoked
        conn = original()
        receipts = approvals._invocation_passes.get() or {}
        if not revoked and any(
            isinstance(item, dict) and item.get("approval_id") == approval_id
            for item in receipts.values()
        ):
            revoked = True
            conn.execute("UPDATE approvals SET status='revoked' WHERE id=?", (approval_id,))
            conn.commit()
        return conn

    monkeypatch.setattr(storage, "connect", revoke_between_gate_and_owner)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(ToolError, match="fresh approval required"):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert revoked
    assert current_task(task["id"])["title"] == "grant changed before mutation"
    assert approvals.get_approval(approval_id)["status"] == "revoked"


def test_registered_task_commit_keeps_generic_pass_spent(monkeypatch):
    from unified_agent.tools import approvals

    task = task_add("grant commit boundary")
    arguments = {"id": task["id"], "title": "committed", "idempotency_key": "spent-grant"}
    approval_id = approve_registered_task(arguments, monkeypatch)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        result = server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert result["title"] == current_task(task["id"])["title"] == "committed"
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_registered_task_bound_replay_needs_no_fresh_generic_approval(monkeypatch):
    from unified_agent.tools import approvals
    from unified_agent.tools.tasks import task_update

    task = task_add("bound lost response")
    arguments = {"id": task["id"], "title": "original", "idempotency_key": "bound-replay"}
    approval_id = approve_registered_task(arguments, monkeypatch)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        first = server.REGISTERED_TOOLS["task_update"](**arguments)
        task_update(task["id"], title="later independent edit")
        before = gateway_payloads()
        replay = server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert replay == first
    assert current_task(task["id"])["title"] == "later independent edit"
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    assert approvals.approval_pending()["pending"] == []
    assert gateway_payloads() == before


@pytest.mark.parametrize("fault", ["payload", "identity", "resource"])
def test_registered_task_bound_receipt_refuses_rebinding(monkeypatch, fault):
    from mcp.server.mcpserver.exceptions import ToolError

    task = task_add("receipt binding")
    other = task_add("other resource")
    arguments = {"id": task["id"], "title": "original", "idempotency_key": "bound-conflict"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    changed = dict(arguments)
    if fault == "payload":
        changed["title"] = "changed payload"
    elif fault == "resource":
        changed["id"] = other["id"]
    if fault == "identity":
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_update")
    identity = config.agent_identity_var.set(
        "Desktop_Agent2" if fault == "identity" else "server_agent2"
    )
    try:
        with pytest.raises(
            ToolError,
            match="requires approval" if fault == "identity" else "receipt|bound|conflict",
        ):
            server.REGISTERED_TOOLS["task_update"](**changed)
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "original"
    assert current_task(other["id"])["title"] == "other resource"
    if fault == "identity":
        from unified_agent.tools import approvals

        [fresh] = approvals.approval_pending()["pending"]
        assert fresh["requested_by"] == "Desktop_Agent2"
        assert fresh["tool"] == "task_update"


def test_registered_task_bound_replay_still_obeys_current_deny(monkeypatch, tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

    task = task_add("current policy")
    arguments = {"id": task["id"], "title": "original", "idempotency_key": "policy-replay"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        server.REGISTERED_TOOLS["task_update"](**arguments)
        policy = tmp_path / "current-deny.yaml"
        policy.write_text(
            "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
            "  - name: deny_task\n    type: tool\n    tool: task_update\n    action: deny\n"
        )
        monkeypatch.setenv("UA_POLICY_FILE", str(policy))
        with pytest.raises(ToolError, match="denied"):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)


def fresh_task_http(arguments, mode, actor="server_agent2"):
    import os
    import subprocess
    import sys
    from pathlib import Path

    # Child gets only explicit temporary fixture locations, never host secrets.
    home = config.data_home()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "UA_HOME": str(home),
        "UA_WORKSPACE": str(home / "workspace"),
        "UA_VAULT_PATH": str(home / "vault"),
        "UA_DEV_ROOT": str(home / "dev"),
        "UA_SETTINGS_FILE": "",
        "UA_STATE_SYNC": "0",
        "UA_EVENT_SOCKET": str(home / "missing.sock"),
        "UA_POLICY_FILE": str(home / "no-policy"),
        "UA_APPROVAL_TOOLS": "task_update",
        "UA_APPROVAL_BASELINE": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    program = r"""
import json, os, secrets, sys
from unified_agent import config
config.load_gemini_env = lambda: None
from unified_agent import gateway, server, webui
from starlette.applications import Starlette
from starlette.testclient import TestClient
arguments, mode, actor = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
token = secrets.token_urlsafe(24)
os.environ['UA_AGENT_TOKENS'] = actor + ':' + token
os.environ['UA_ENFORCE_AUTH'] = '1'
os.environ['UA_HTTP_ALLOWED_HOSTS'] = '127.0.0.1:*'
if mode in {'crash','mcp-crash'}:
    def crash_before_commit(*args, **kwargs):
        os._exit(0)
    gateway.commit_registered_call_on_connection = crash_before_commit
if mode == 'mcp-crash':
    import asyncio, httpx
    async def call_mcp():
        app = server.mcp.streamable_http_app()
        headers = {'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}
        async with server.mcp.session_manager.run(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765',headers=headers
        ) as client:
            initialized = await client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':'2024-11-05','capabilities':{},
                          'clientInfo':{'name':'fixture-restart','version':'1'}}})
            assert initialized.status_code == 200
            client.headers['mcp-session-id'] = initialized.headers['mcp-session-id']
            notified = await client.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'})
            assert notified.status_code == 202
            await client.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'task_update','arguments':arguments}})
    asyncio.run(call_mcp())
    raise AssertionError('MCP request never reached the crash boundary')
app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
with TestClient(app, base_url='http://127.0.0.1') as client:
    body = {key:value for key,value in arguments.items() if key != 'actor'}
    response = client.post('/api/action/task', json=body, headers={'Authorization':'Bearer '+token})
assert mode != 'crash', 'request never reached the crash boundary'
print(json.dumps({'status':response.status_code,'body':response.json()}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(arguments), mode, actor],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    if mode in {"crash", "mcp-crash"}:
        assert result.stdout == ""
        return None
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "fault", ["none", "expired", "revoked", "grant", "aba", "payload", "identity"]
)
def test_actual_http_fresh_process_recovers_only_exact_pending_consumption(monkeypatch, fault):
    from unified_agent.tools import approvals
    from unified_agent.tools.tasks import task_update

    task = task_add("fresh process approval")
    arguments = {
        "id": task["id"],
        "title": "recovered",
        "idempotency_key": "restart-key",
        "actor": "server_agent2",
    }
    approval_id = approve_registered_task(arguments, monkeypatch)
    fresh_task_http(arguments, "crash")
    assert current_task(task["id"])["title"] == "fresh process approval"
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "restart-key", "task_update") is None
        nonce = conn.execute(
            "SELECT ulid FROM event_log WHERE channel='agent.approval.consumed' ORDER BY rowid DESC"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
            == 0
        )
        statements = {
            "expired": "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            "grant": "UPDATE approvals SET resolved_by='changed-fixture-operator' WHERE id=?",
        }
        if fault in statements:
            conn.execute(statements[fault], (approval_id,))
    if fault == "revoked":
        operator = config.agent_identity_var.set("Desktop_Person")
        try:
            server.REGISTERED_TOOLS["approval_resolve"](id=approval_id, decision="revoke")
        finally:
            config.agent_identity_var.reset(operator)
    if fault == "aba":
        approvals.restore_consumed_approval(approval_id, consumption_nonce=nonce)
        identity = config.agent_identity_var.set("server_agent2")
        try:
            with approvals.approval_invocation("task_update", arguments):
                approvals.check_approval_gate("task_update", arguments, force=True)
                approvals.mark_governed_execution()
        finally:
            config.agent_identity_var.reset(identity)
    changed = dict(arguments)
    if fault == "payload":
        changed["title"] = "different payload"
    outcome = fresh_task_http(
        changed, "recover", "Desktop_Agent2" if fault == "identity" else "server_agent2"
    )
    if fault == "none":
        assert outcome["status"] == 200, outcome
        assert outcome["body"]["title"] == current_task(task["id"])["title"] == "recovered"
        with storage.connect() as conn:
            receipt = json.loads(
                conn.execute(
                    "SELECT payload FROM event_log WHERE channel='agent.task.invocation_receipt'"
                ).fetchone()[0]
            )
        assert receipt["approvals"] == [{"approval_id": approval_id, "nonce": nonce}]
        task_update(task["id"], title="later edit after lost response")
        replay = fresh_task_http(arguments, "replay")
        assert replay == outcome
        assert current_task(task["id"])["title"] == "later edit after lost response"
        approvals.restore_consumed_approval(approval_id, consumption_nonce=nonce)
        assert approvals.get_approval(approval_id)["status"] == "consumed"
    else:
        assert outcome["status"] in {400, 202}, outcome
        assert current_task(task["id"])["title"] == "fresh process approval"
        expected = {
            "expired": "expired",
            "grant": "grant changed",
            "aba": "superseded",
            "payload": "different request",
        }
        if fault in expected:
            assert expected[fault] in outcome["body"]["error"]
    with storage.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
        ).fetchone()[0]
    assert count == (2 if fault == "aba" else 1)


def test_actual_http_restart_reuses_both_generic_and_native_clear_consumptions(monkeypatch):
    from unified_agent.tools import approvals

    task = task_add("quarantine restart", untrusted=True)
    arguments = {
        "id": task["id"],
        "title": "cleared after restart",
        "untrusted": False,
        "idempotency_key": "clear-restart",
        "actor": "server_agent2",
    }
    generic_id = approve_registered_task(arguments, monkeypatch)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(approvals.ApprovalPendingError) as native:
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert approvals.get_approval(generic_id)["status"] == "approved"
    operator = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(native.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(operator)
    fresh_task_http(arguments, "crash")
    assert current_task(task["id"])["untrusted"] == 1
    assert approvals.get_approval(generic_id)["status"] == "consumed"
    assert approvals.get_approval(native.value.approval_id)["status"] == "consumed"
    with storage.connect() as conn:
        before = conn.execute(
            "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
        ).fetchone()[0]
    recovered = fresh_task_http(arguments, "recover")
    assert recovered["status"] == 200, recovered
    assert current_task(task["id"])["untrusted"] == 0
    assert fresh_task_http(arguments, "replay") == recovered
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
            ).fetchone()[0]
            == before
        )
        receipt = json.loads(
            conn.execute(
                "SELECT payload FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
        )
    assert {item["approval_id"] for item in receipt["approvals"]} == {
        generic_id,
        native.value.approval_id,
    }


@pytest.mark.asyncio
async def test_actual_mcp_http_pending_crash_recovers_through_http_without_reconsuming(monkeypatch):
    from unified_agent.tools import approvals

    task = task_add("cross adapter restart")
    arguments = {
        "id": task["id"],
        "title": "same semantic operation",
        "idempotency_key": "cross-restart",
    }
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_update")
    identity = config.agent_identity_var.set("server_agent2")
    try:
        async with Client(server.mcp) as client:
            pending = await client.session.call_tool("task_update", arguments)
        assert pending.is_error
    finally:
        config.agent_identity_var.reset(identity)
    [requested] = approvals.approval_pending()["pending"]
    operator = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(requested["id"], "approve")
    finally:
        config.agent_identity_var.reset(operator)
    fresh_task_http(arguments, "mcp-crash")
    assert approvals.get_approval(requested["id"])["status"] == "consumed"
    assert current_task(task["id"])["title"] == "cross adapter restart"
    recovered = fresh_task_http(arguments, "recover")
    assert recovered["status"] == 200, recovered
    assert recovered["body"]["title"] == "same semantic operation"
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
            ).fetchone()[0]
            == 1
        )
    assert approvals.approval_pending()["pending"] == []


@pytest.mark.parametrize("keyed", [False, True])
def test_registered_task_interrupt_after_commit_keeps_all_authority_spent(monkeypatch, keyed):
    from unified_agent.tools import approvals

    task = task_add("postcommit interruption")
    arguments = {"id": task["id"], "title": "committed despite interruption"}
    if keyed:
        arguments["idempotency_key"] = "interrupt-key"
    approval_id = approve_registered_task(arguments, monkeypatch)
    original = gateway.commit_registered_call_on_connection

    def commit_then_interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("fixture interruption after commit before memory marker")

    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", commit_then_interrupt)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(KeyboardInterrupt):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "committed despite interruption"
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_supported_operator_revoke_blocks_pending_fresh_recovery(monkeypatch):
    from unified_agent.tools import approvals

    task = task_add("operator withdraws pending recovery")
    arguments = {
        "id": task["id"],
        "title": "must not recover",
        "idempotency_key": "operator-revoke",
        "actor": "server_agent2",
    }
    approval_id = approve_registered_task(arguments, monkeypatch)
    fresh_task_http(arguments, "crash")
    operator = config.agent_identity_var.set("Desktop_Person")
    try:
        withdrawn = server.REGISTERED_TOOLS["approval_resolve"](
            id=approval_id, decision="revoke", reason="withdraw before pending execution"
        )
    finally:
        config.agent_identity_var.reset(operator)
    assert withdrawn["status"] == "revoked"
    recovered = fresh_task_http(arguments, "recover")
    assert recovered["status"] in {400, 202}
    assert current_task(task["id"])["title"] == "operator withdraws pending recovery"
    assert approvals.get_approval(approval_id)["status"] == "revoked"
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("keyed", [False, True])
def test_supported_operator_cannot_revoke_completed_task_authority(monkeypatch, keyed):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("spent authority")
    arguments = {"id": task["id"], "title": "completed"}
    if keyed:
        arguments["idempotency_key"] = "completed-revoke"
    approval_id = approve_registered_task(arguments, monkeypatch)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    operator = config.agent_identity_var.set("Desktop_Person")
    try:
        with pytest.raises(ToolError, match="consumed|committed|completed"):
            server.REGISTERED_TOOLS["approval_resolve"](id=approval_id, decision="revoke")
    finally:
        config.agent_identity_var.reset(operator)
    assert approvals.get_approval(approval_id)["status"] == "consumed"


@pytest.mark.parametrize("winner_commits_first", [False, True])
def test_registered_initial_race_reuses_consumption_without_extra_approval(
    monkeypatch, winner_commits_first
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from unified_agent.tools import approvals

    task = task_add("initial approved race")
    arguments = {"id": task["id"], "title": "one outcome", "idempotency_key": "initial-race"}
    approval_id = approve_registered_task(arguments, monkeypatch)
    barrier = threading.Barrier(2)
    initial_lookup = approvals._recover_bound_consumption
    completed = threading.Event()
    lookup_lock = threading.Lock()
    lookups = 0
    commit = gateway.commit_registered_call_on_connection

    def observe_commit(*args, **kwargs):
        result = commit(*args, **kwargs)
        completed.set()
        return result

    def simultaneous_empty_lookup(*args, **kwargs):
        nonlocal lookups
        result = initial_lookup(*args, **kwargs)
        assert result is None
        with lookup_lock:
            index = lookups
            lookups += 1
        barrier.wait(timeout=10)
        if winner_commits_first and index == 1:
            assert completed.wait(timeout=10)
        return result

    monkeypatch.setattr(approvals, "_recover_bound_consumption", simultaneous_empty_lookup)
    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", observe_commit)

    def invoke():
        identity = config.agent_identity_var.set("server_agent2")
        try:
            return server.REGISTERED_TOOLS["task_update"](**arguments)
        finally:
            config.agent_identity_var.reset(identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: invoke(), range(2)))
    assert outcomes[0] == outcomes[1]
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    assert approvals.approval_pending()["pending"] == []
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
            ).fetchone()[0]
            == 1
        )


def test_supported_revoke_waits_for_task_commit_and_preserves_spent_authority(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("revocation races completion")
    arguments = {"id": task["id"], "title": "committed winner", "idempotency_key": "revoke-race"}
    approval_id = approve_registered_task(arguments, monkeypatch)
    owner_ready = threading.Event()
    revoker_waiting = threading.Event()
    release_owner = threading.Event()
    commit = gateway.commit_registered_call_on_connection
    connect = storage.connect

    def hold_owner_commit(*args, **kwargs):
        owner_ready.set()
        assert release_owner.wait(timeout=10)
        return commit(*args, **kwargs)

    def observe_operator_lock():
        conn = connect()
        if config.verified_agent_identity() == "Desktop_Person":
            conn.set_trace_callback(
                lambda sql: revoker_waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
        return conn

    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", hold_owner_commit)
    monkeypatch.setattr(storage, "connect", observe_operator_lock)

    def execute():
        token = config.agent_identity_var.set("server_agent2")
        try:
            return server.REGISTERED_TOOLS["task_update"](**arguments)
        finally:
            config.agent_identity_var.reset(token)

    def revoke():
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            with pytest.raises(ToolError, match="consumed|committed|completed"):
                server.REGISTERED_TOOLS["approval_resolve"](id=approval_id, decision="revoke")
        finally:
            config.agent_identity_var.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(execute)
        assert owner_ready.wait(timeout=10)
        operator = pool.submit(revoke)
        assert revoker_waiting.wait(timeout=10)
        release_owner.set()
        assert owner.result(timeout=10)["title"] == "committed winner"
        operator.result(timeout=10)
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_agent_cannot_revoke_pending_recovery_authority(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("operator-only pending withdrawal")
    arguments = {
        "id": task["id"],
        "title": "pending",
        "idempotency_key": "agent-revoke",
        "actor": "server_agent2",
    }
    approval_id = approve_registered_task(arguments, monkeypatch)
    fresh_task_http(arguments, "crash")
    token = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(ToolError, match="verified human operator"):
            server.REGISTERED_TOOLS["approval_resolve"](id=approval_id, decision="revoke")
    finally:
        config.agent_identity_var.reset(token)
    assert approvals.get_approval(approval_id)["status"] == "consumed"


@pytest.mark.parametrize("gateway_unavailable", [False, True])
def test_competing_committed_clear_replay_has_no_second_evidence(monkeypatch, gateway_unavailable):
    from concurrent.futures import ThreadPoolExecutor

    from unified_agent import events
    from unified_agent.tools import approvals, tasks

    task = task_add("before competing clear")
    args = {
        "id": task["id"],
        "title": "one committed result",
        "untrusted": False,
        "idempotency_key": "review-clear-race",
    }
    approval_id = approve_registered_task(args, monkeypatch)
    original_preflight = tasks.registered_task_replay
    original_append = events.record_event
    triggered = False
    outcomes = []

    def invoke():
        identity = config.agent_identity_var.set("server_agent2")
        try:
            return server.REGISTERED_TOOLS["task_update"](**args)
        finally:
            config.agent_identity_var.reset(identity)

    def old_preflight_with_intervening_commit(arguments):
        nonlocal triggered
        result = original_preflight(arguments)
        if not triggered:
            assert result is None
            triggered = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                outcomes.append(pool.submit(invoke).result(timeout=10))
        return result

    def unavailable_after_winner(channel, *args, **kwargs):
        if outcomes and channel == gateway.CHANNEL_GATEWAY_DECISION and gateway_unavailable:
            raise RuntimeError("synthetic append failure after competing commit")
        return original_append(channel, *args, **kwargs)

    monkeypatch.setattr(tasks, "registered_task_replay", old_preflight_with_intervening_commit)
    monkeypatch.setattr(events, "record_event", unavailable_after_winner)
    assert invoke() == outcomes[0]
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    assert approvals.approval_pending()["pending"] == []
    with storage.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM event_log WHERE channel=? AND json_extract(payload, '$.decision')='allow'",
            (gateway.CHANNEL_GATEWAY_DECISION,),
        ).fetchone()[0]
    assert count == 1, (
        "committed replay must acknowledge the original evidence instead of appending again"
    )


def test_registered_task_callers_have_independent_key_spaces():
    first_task = task_add("first caller")
    second_task = task_add("second caller")
    outcomes = []
    for actor, task in [("server_agent2", first_task), ("Desktop_Agent2", second_task)]:
        identity = config.agent_identity_var.set(actor)
        try:
            outcomes.append(
                server.REGISTERED_TOOLS["task_update"](
                    id=task["id"], title=actor + " outcome", idempotency_key="shared-client-key"
                )
            )
        finally:
            config.agent_identity_var.reset(identity)
    assert outcomes[0]["id"] != outcomes[1]["id"]
    for actor, task, outcome in zip(
        ["server_agent2", "Desktop_Agent2"], [first_task, second_task], outcomes, strict=True
    ):
        identity = config.agent_identity_var.set(actor)
        try:
            assert (
                server.REGISTERED_TOOLS["task_update"](
                    id=task["id"], title=actor + " outcome", idempotency_key="shared-client-key"
                )
                == outcome
            )
        finally:
            config.agent_identity_var.reset(identity)


def test_registered_task_independent_scope_cannot_overwrite_original_raw_alias():
    first = task_add("original raw alias")
    other = task_add("independent caller")
    identity = config.agent_identity_var.set("server_agent2")
    try:
        original = server.REGISTERED_TOOLS["task_update"](
            id=first["id"], title="original", idempotency_key="raw-alias"
        )
    finally:
        config.agent_identity_var.reset(identity)
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        second = server.REGISTERED_TOOLS["task_update"](
            id=other["id"], title="second", idempotency_key="raw-alias"
        )
    finally:
        config.agent_identity_var.reset(identity)
    assert second["id"] == other["id"]
    with storage.connect() as conn:
        assert storage.get_idempotency_record(conn, "raw-alias", "task_update") == original


@pytest.mark.parametrize("gated", [False, True])
def test_registered_task_legacy_receipt_replay_preserves_history_without_backfill(
    monkeypatch, gated
):
    from unified_agent.tools import approvals
    from unified_agent.tools.tasks import task_update

    task = task_add("legacy native outcome")
    arguments = {"id": task["id"], "title": "historical", "idempotency_key": "legacy-replay"}
    historical = task_update(**arguments)
    task_update(task["id"], title="later independent change")
    if gated:
        approval_id = approve_registered_task(arguments, monkeypatch)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        assert server.REGISTERED_TOOLS["task_update"](**arguments) == historical
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "later independent change"
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
            == 0
        )
    if gated:
        assert approvals.get_approval(approval_id)["status"] == "approved"


def test_actual_http_client_keys_are_independent_for_verified_actors(http, monkeypatch):
    from unified_agent.tools.tasks import task_update

    transport, first_headers = http
    second_token = secrets.token_urlsafe(24)
    first_token = first_headers["Authorization"][7:]
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"server_agent2:{first_token},Desktop_Agent2:{second_token}"
    )
    second_headers = {"Authorization": "Bearer " + second_token}
    tasks = [task_add("first HTTP actor"), task_add("second HTTP actor")]
    requests = [
        {"id": task["id"], "title": f"outcome {index}", "idempotency_key": "HTTP-shared-key"}
        for index, task in enumerate(tasks)
    ]
    headers = [first_headers, second_headers]
    outcomes = []
    for arguments, credential in zip(requests, headers, strict=True):
        response = transport.post("/api/action/task", json=arguments, headers=credential)
        assert response.status_code == 200, response.json()
        outcomes.append(response.json())
    assert outcomes[0]["id"] != outcomes[1]["id"]
    for task in tasks:
        task_update(task["id"], title="later independent edit")
    for arguments, credential, outcome in zip(requests, headers, outcomes, strict=True):
        replay = transport.post("/api/action/task", json=arguments, headers=credential)
        assert replay.status_code == 200, replay.json()
        assert replay.json() == outcome
        assert current_task(arguments["id"])["title"] == "later independent edit"


def test_actual_workos_http_client_keys_are_independent_for_verified_subjects(
    verified_workos_transport,
):
    from unified_agent.tools.tasks import task_update

    transport, headers, provider, leg = verified_workos_transport
    leg.identity_map["second-fixture-subject"] = "server_agent2"
    tasks = [task_add("first WorkOS subject"), task_add("second WorkOS subject")]
    subjects = ["fixture-subject", "second-fixture-subject"]
    requests = [
        {
            "id": task["id"],
            "title": f"subject outcome {index}",
            "idempotency_key": "WorkOS-shared-key",
        }
        for index, task in enumerate(tasks)
    ]
    outcomes = []
    for subject, arguments in zip(subjects, requests, strict=True):
        provider.subject = subject
        response = transport.post("/api/action/task", json=arguments, headers=headers)
        assert response.status_code == 200, response.json()
        outcomes.append(response.json())
    assert outcomes[0]["id"] != outcomes[1]["id"]
    for task in tasks:
        task_update(task["id"], title="later independent edit")
    for subject, arguments, outcome in zip(subjects, requests, outcomes, strict=True):
        provider.subject = subject
        replay = transport.post("/api/action/task", json=arguments, headers=headers)
        assert replay.status_code == 200, replay.json()
        assert replay.json() == outcome
        assert current_task(arguments["id"])["title"] == "later independent edit"
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel='agent.task.invocation_receipt'"
        ).fetchall()
    assert {json.loads(row[0])["binding"]["subject"] for row in rows} == {
        "workos:fixture-subject",
        "workos:second-fixture-subject",
    }


def test_bound_scoped_receipt_without_producer_proof_fails_closed(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals, tasks

    task = task_add("no invented binding")
    arguments = {"id": task["id"], "title": "cannot replay", "idempotency_key": "missing-proof"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with gateway.invocation("task_update") as call:
            call.registered_arguments = arguments
            with tasks.registered_update_binding(), storage.connect() as conn:
                binding = approvals.registered_task_binding()
                storage.record_idempotency_required(
                    conn,
                    "task-invocation:" + binding["scope_key"],
                    approvals._SCOPED_TASK_RECEIPT_TOOL,
                    current_task(task["id"]),
                )
        with pytest.raises(ToolError, match="no matching committed request binding"):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "no invented binding"


def test_predecessor_bound_raw_receipt_replays_without_inferred_backfill():
    from unified_agent.tools import approvals, tasks
    from unified_agent.tools.tasks import task_update

    task = task_add("pre-scoped receipt")
    arguments = {"id": task["id"], "title": "original", "idempotency_key": "pre-scoped"}
    identity = config.agent_identity_var.set("server_agent2")
    try:
        original = server.REGISTERED_TOOLS["task_update"](**arguments)
        # The predecessor stored the same producer proof and raw result but
        # had no principal-scoped result row. Retain that actual proof verbatim.
        with gateway.invocation("task_update") as call:
            call.registered_arguments = arguments
            with tasks.registered_update_binding(), storage.connect() as conn:
                key = approvals._task_receipt_storage_key(approvals.registered_task_binding())
                conn.execute("DELETE FROM idempotency_keys WHERE key=?", (key,))
        task_update(task["id"], title="later edit")
        assert server.REGISTERED_TOOLS["task_update"](**arguments) == original
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "later edit"
    with storage.connect() as conn:
        assert conn.execute("SELECT 1 FROM idempotency_keys WHERE key=?", (key,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.invocation_receipt'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["MCP", "HTTP"])
async def test_actual_transports_use_shared_invocation_transaction_owner(
    http, monkeypatch, adapter
):
    from unified_agent.invocation_service import InvocationService

    task = task_add("shared service owner")
    arguments = {"id": task["id"], "title": "composed", "idempotency_key": "shared-owner"}
    begin = getattr(InvocationService, "begin_mutation", None)
    complete = getattr(InvocationService, "complete_mutation", None)
    opened = []
    completed = []

    def observe_begin(connect):
        assert begin is not None
        conn = begin(connect)
        assert conn.in_transaction
        opened.append(id(conn))
        return conn

    def observe_complete(conn, prepare=None):
        assert complete is not None
        assert conn.in_transaction
        complete(conn, prepare)
        assert not conn.in_transaction
        completed.append(id(conn))

    monkeypatch.setattr(
        InvocationService, "begin_mutation", staticmethod(observe_begin), raising=False
    )
    monkeypatch.setattr(
        InvocationService, "complete_mutation", staticmethod(observe_complete), raising=False
    )
    if adapter == "HTTP":
        transport, headers = http
        result = transport.post("/api/action/task", json=arguments, headers=headers)
        assert result.status_code == 200, result.json()
    else:
        identity = config.agent_identity_var.set("server_agent2")
        try:
            async with Client(server.mcp) as client:
                result = await client.session.call_tool("task_update", arguments)
                assert not result.is_error
        finally:
            config.agent_identity_var.reset(identity)
    assert opened and opened == completed
    assert current_task(task["id"])["title"] == "composed"
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invocation_receipts'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("illegal", ["commit", "rollback", "script"])
def test_registered_receipt_port_cannot_control_service_transaction(monkeypatch, illegal):
    from unified_agent.invocation_service import TransactionOwnershipError
    from unified_agent.tools import approvals

    task = task_add("receipt port cannot commit")
    arguments = {"id": task["id"], "title": "must roll back", "idempotency_key": "illegal-port"}
    approval_id = approve_registered_task(arguments, monkeypatch)

    def attempt_control(conn, result):
        if illegal == "script":
            conn.executescript("SELECT 1")
        else:
            getattr(conn, illegal)()

    monkeypatch.setattr(approvals, "record_task_receipt_on_connection", attempt_control)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(TransactionOwnershipError):
            server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "receipt port cannot commit"
    assert approvals.get_approval(approval_id)["status"] == "approved"
    with storage.connect() as conn:
        # Only the task's own keys: the approval decision's receipt message to the
        # requester (task #2435) records its own message_send receipt.
        assert (
            conn.execute(
                "SELECT count(*) FROM idempotency_keys WHERE tool LIKE 'task_update%'"
            ).fetchone()[0]
            == 0
        )
    assert all(event["decision"] != "allow" for event in gateway_payloads())


@pytest.mark.parametrize("tenant", [None, "org-alias-proof"])
@pytest.mark.parametrize("spelling", ["predecessor", "binary_text"])
def test_scoped_namespace_crafted_native_alias_cannot_reserve_internal_receipt(
    monkeypatch, http, tenant, spelling
):
    import hashlib

    from unified_agent import edition
    from unified_agent.tools import tasks

    if tenant:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", tenant)
    else:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    transport, first_headers = http
    second_token = secrets.token_urlsafe(24)
    first_token = first_headers["Authorization"].removeprefix("Bearer ")
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"server_agent2:{first_token},Desktop_Agent2:{second_token}"
    )
    second_headers = {"Authorization": "Bearer " + second_token}
    key = "target-principal-client-key"
    scope = [tenant, "agent:Desktop_Agent2", "Desktop_Agent2", "task_update", 1, key]
    digest = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
    crafted = (
        "task-invocation:" if spelling == "predecessor" else "task-invocation:\0v2:"
    ) + digest
    first_task, second_task = task_add("alias source"), task_add("target principal")
    first_args = {"id": first_task["id"], "title": "source result", "idempotency_key": crafted}
    second_args = {"id": second_task["id"], "title": "target result", "idempotency_key": key}
    first = transport.post("/api/action/task", json=first_args, headers=first_headers)
    assert first.status_code == 200, first.json()
    second = transport.post("/api/action/task", json=second_args, headers=second_headers)
    assert second.status_code == 200, second.json()
    assert second.json()["id"] == second_task["id"]
    with storage.connect() as conn:
        preserved = storage.get_idempotency_record(conn, crafted, "task_update")
        scoped_types = conn.execute(
            "SELECT typeof(key) FROM idempotency_keys WHERE tool='task_update.invocation'"
        ).fetchall()
    assert preserved == first.json()
    assert [row[0] for row in scoped_types] == ["blob", "blob"]
    assert edition.tenant_org_id() == tenant
    tasks.task_update(first_task["id"], title="later source edit")
    tasks.task_update(second_task["id"], title="later target edit")
    assert (
        transport.post("/api/action/task", json=first_args, headers=first_headers).json()
        == first.json()
    )
    assert (
        transport.post("/api/action/task", json=second_args, headers=second_headers).json()
        == second.json()
    )
    assert current_task(first_task["id"])["title"] == "later source edit"
    assert current_task(second_task["id"])["title"] == "later target edit"


@pytest.mark.parametrize("tenant", [None, "org-predecessor-proof"])
def test_scoped_namespace_predecessor_text_receipt_replays_without_migration(monkeypatch, tenant):
    from unified_agent.tools import approvals, tasks

    if tenant:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", tenant)
    else:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    task = task_add("preserve predecessor text")
    arguments = {
        "id": task["id"],
        "title": "original outcome",
        "idempotency_key": "text-predecessor",
    }
    identity = config.agent_identity_var.set("server_agent2")
    try:
        original = server.REGISTERED_TOOLS["task_update"](**arguments)
        with gateway.invocation("task_update") as call:
            call.registered_arguments = arguments
            with tasks.registered_update_binding(), storage.connect() as conn:
                binding = approvals.registered_task_binding()
                current_key = approvals._task_receipt_storage_key(binding)
                old_key = storage.idempotency_storage_key("task-invocation:" + binding["scope_key"])
                conn.execute(
                    "UPDATE idempotency_keys SET key=? WHERE key=?", (old_key, current_key)
                )
                before = conn.execute(
                    "SELECT key,tool,result_json,created_at FROM idempotency_keys ORDER BY key"
                ).fetchall()
        tasks.task_update(task["id"], title="later edit retained")
        assert server.REGISTERED_TOOLS["task_update"](**arguments) == original
        with storage.connect() as conn:
            after = conn.execute(
                "SELECT key,tool,result_json,created_at FROM idempotency_keys ORDER BY key"
            ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
    finally:
        config.agent_identity_var.reset(identity)
    assert current_task(task["id"])["title"] == "later edit retained"


def test_adopted_consumption_is_not_restored_by_a_failing_retry(monkeypatch):
    """Only the attempt that consumed a pass may restore it (#1980)."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("adopter must not restore")
    arguments = {
        "id": task["id"],
        "title": "adopted",
        "idempotency_key": "adopt-key",
        "actor": "server_agent2",
    }
    approval_id = approve_registered_task(arguments, monkeypatch)
    fresh_task_http(arguments, "crash")
    assert approvals.get_approval(approval_id)["status"] == "consumed"

    def unavailable(*args, **kwargs):
        raise ToolError("fixture gateway unavailable")

    commit = gateway.commit_registered_call_on_connection
    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", unavailable)
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with pytest.raises(ToolError, match="fixture gateway unavailable"):
            server.REGISTERED_TOOLS["task_update"](**arguments)
        # The adopted pass stays with its original consumption.
        assert approvals.get_approval(approval_id)["status"] == "consumed"
        monkeypatch.setattr(gateway, "commit_registered_call_on_connection", commit)
        outcome = server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert outcome["title"] == current_task(task["id"])["title"] == "adopted"
    with storage.connect() as conn:
        consumed = conn.execute(
            "SELECT count(*) FROM event_log WHERE channel='agent.approval.consumed'"
        ).fetchone()[0]
    assert consumed == 1


def test_a_pass_never_moves_to_another_caller_and_its_requester_is_not_wedged(monkeypatch):
    """#2492: the pass stays with its requester, and that requester's retry still
    finishes the work instead of wedging on its own superseded attempt (#1980).

    The original cross-caller shape is gone by design: another caller making the
    identical call parks a request of its own and cannot spend this pass.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals

    task = task_add("superseded key")
    arguments = {"id": task["id"], "title": "same payload", "idempotency_key": "shared-key"}
    approval_id = approve_registered_task(arguments, monkeypatch)

    def call_as(label):
        identity = config.agent_identity_var.set(label)
        try:
            return server.REGISTERED_TOOLS["task_update"](**arguments)
        finally:
            config.agent_identity_var.reset(identity)

    def unavailable(*args, **kwargs):
        raise ToolError("fixture gateway unavailable")

    # The requester consumes the pass, fails, and its own scope restores it.
    commit = gateway.commit_registered_call_on_connection
    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", unavailable)
    with pytest.raises(ToolError, match="fixture gateway unavailable"):
        call_as("server_agent2")
    monkeypatch.setattr(gateway, "commit_registered_call_on_connection", commit)
    assert approvals.get_approval(approval_id)["status"] == "approved"

    # Another caller's identical call parks its own request and leaves the pass.
    with pytest.raises(approvals.ApprovalPendingError) as other:
        call_as("Desktop_Agent2")
    assert other.value.approval_id != approval_id
    assert approvals.get_approval(approval_id)["status"] == "approved"

    # The requester's own retry finishes the work rather than wedging.
    assert call_as("server_agent2")["title"] == "same payload"
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_replay_admission_without_a_receipt_never_writes():
    """A call admitted only as a committed replay cannot fall into a write (#1980)."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import approvals
    from unified_agent.tools.tasks import task_update

    task = task_add("replay marker")
    token = approvals._task_replay_binding.set({"scope_key": "fixture"})
    try:
        with pytest.raises(ToolError, match="no matching receipt"):
            task_update(task["id"], title="must not be written", idempotency_key="no-receipt")
    finally:
        approvals._task_replay_binding.reset(token)
    assert current_task(task["id"])["title"] == "replay marker"


def test_registered_task_replay_survives_event_retention_prune(monkeypatch):
    """A committed receipt still replays after pruning old events (#1973 review)."""
    import datetime

    from unified_agent.tools import approvals

    task = task_add("replay after prune")
    arguments = {"id": task["id"], "title": "committed long ago", "idempotency_key": "old-key"}
    approve_registered_task(arguments, monkeypatch)
    stamp = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=90)).isoformat(
        timespec="seconds"
    )
    identity = config.agent_identity_var.set("server_agent2")
    try:
        with monkeypatch.context() as aged:
            aged.setattr(storage, "now", lambda: stamp)
            first = server.REGISTERED_TOOLS["task_update"](**arguments)
        events.record_event("agent.test.updated", payload={"after": True})
        assert events.prune_events(older_than_days=30) > 0
        assert events.verify_event_chain()["status"] == "valid"
        replay = server.REGISTERED_TOOLS["task_update"](**arguments)
    finally:
        config.agent_identity_var.reset(identity)
    assert replay == first
    assert approvals.approval_pending()["pending"] == []
