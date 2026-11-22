"""HTTP work-product writes retain the MCP policy and approval decisions."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.tools import approvals, work_product

TAGS = {"matter": "M-synthetic", "client": "Example Client"}


@pytest.fixture(autouse=True)
def _host_chief_of_staff(seat_chief_of_staff):
    seat_chief_of_staff("gcp_agent5")


@pytest.fixture
def setup_hub(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    monkeypatch.setenv("UA_WORK_PRODUCT_ENABLED", "1")
    monkeypatch.setenv("UA_WORK_PRODUCT_RELEASE_DIR", str(tmp_path / "released"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    tokens = {
        name: secrets.token_urlsafe(24)
        for name in ("gcp_agent5", "Desktop_Person", "Desktop_Agent2")
    }
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", ",".join(f"{name}:{token}" for name, token in tokens.items())
    )
    storage._connection = None
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client, workspace, tokens
    storage._connection = None


def _headers(tokens, identity):
    return {"Authorization": f"Bearer {tokens[identity]}"}


def _body(path: Path, title: str):
    return {
        "source_path": str(path),
        "title": title,
        "template_pack": "legal",
        "tags": TAGS,
        "model": "synthetic-model",
    }


def test_http_requires_identity_and_respects_mcp_profile(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    doc.write_text("# Synthetic memo\n")
    body = _body(doc, "Synthetic memo")

    assert client.post("/api/work-products", json=body).status_code == 401
    assert (
        client.post(
            "/api/work-products", headers=_headers(tokens, "Desktop_Agent2"), json=body
        ).status_code
        == 403
    )
    assert work_product.list_work_products()["work_products"] == []


def test_http_and_mcp_share_pending_and_release_decisions(setup_hub):
    client, workspace, tokens = setup_hub
    http_doc = workspace / "http.md"
    mcp_doc = workspace / "mcp.md"
    http_doc.write_text("# HTTP draft\n")
    mcp_doc.write_text("# MCP draft\n")
    http_body = _body(http_doc, "HTTP synthetic")
    mcp_body = _body(mcp_doc, "MCP synthetic")
    headers = _headers(tokens, "gcp_agent5")

    pending_http = client.post("/api/work-products", headers=headers, json=http_body)
    assert pending_http.status_code == 202
    http_result = pending_http.json()
    assert http_result["status"] == "pending"
    http_approval = approvals.get_approval(http_result["approval_id"])
    assert http_approval["tool"] == "work_product_release"
    assert http_approval["requested_by"] == "gcp_agent5"
    assert http_approval["payload"]["work_product_id"] == http_result["work_product_id"]

    actor = config.agent_identity_var.set("gcp_agent5")
    try:
        with pytest.raises(approvals.ApprovalPendingError) as pending_mcp:
            server.REGISTERED_TOOLS["work_product_submit"](**mcp_body)
        with pytest.raises(ToolError, match="separation of duties"):
            approvals.approval_resolve(http_result["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(actor)
    mcp_approval = approvals.get_approval(pending_mcp.value.approval_id)
    assert mcp_approval["status"] == http_approval["status"] == "pending"
    assert mcp_approval["requested_by"] == http_approval["requested_by"]

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(http_result["approval_id"], "approve")
        approvals.approval_resolve(pending_mcp.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(human)

    released_http = client.post(
        f"/api/work-products/{http_result['work_product_id']}/release", headers=headers
    )
    assert released_http.status_code == 200
    assert released_http.json()["status"] == "released"
    assert released_http.json()["id"] == http_result["work_product_id"]
    assert (
        client.post(
            f"/api/work-products/{http_result['work_product_id']}/release",
            headers=_headers(tokens, "Desktop_Agent2"),
        ).status_code
        == 403
    )

    actor = config.agent_identity_var.set("gcp_agent5")
    try:
        released_mcp = server.REGISTERED_TOOLS["work_product_submit"](**mcp_body)
    finally:
        config.agent_identity_var.reset(actor)
    assert released_mcp["status"] == released_http.json()["status"]
    assert released_mcp["resolved_by"] == released_http.json()["resolved_by"] == "Desktop_Person"
    assert work_product.work_product_evidence(http_result["work_product_id"])["consistent"] is True


def test_release_cannot_switch_inputs_or_owner(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    doc.write_text("# Synthetic memo\n")
    headers = _headers(tokens, "gcp_agent5")
    pending = client.post("/api/work-products", headers=headers, json=_body(doc, "Memo"))
    assert pending.status_code == 202
    wp_id = pending.json()["work_product_id"]

    assert (
        client.post(
            f"/api/work-products/{wp_id}/release",
            headers=headers,
            json={"tags": {"client": "changed"}},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/work-products/{wp_id}/release", headers=_headers(tokens, "Desktop_Person")
        ).status_code
        == 403
    )
    assert work_product.get_work_product(wp_id)["status"] == "submitted"


def test_release_retry_never_creates_a_replacement_after_denial(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    doc.write_text("# Synthetic memo\n")
    headers = _headers(tokens, "gcp_agent5")
    pending = client.post("/api/work-products", headers=headers, json=_body(doc, "Memo"))
    assert pending.status_code == 202
    wp_id = pending.json()["work_product_id"]

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending.json()["approval_id"], "deny", reason="Synthetic denial")
    finally:
        config.agent_identity_var.reset(human)

    retry = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert retry.status_code == 400
    assert "rejected" in retry.json()["error"]
    rows = work_product.list_work_products()["work_products"]
    assert len(rows) == 1
    assert rows[0]["id"] == wp_id
    assert rows[0]["status"] == "rejected"


def test_release_retry_never_creates_a_replacement_after_source_edit(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    doc.write_text("# Synthetic memo\n")
    headers = _headers(tokens, "gcp_agent5")
    pending = client.post("/api/work-products", headers=headers, json=_body(doc, "Memo"))
    assert pending.status_code == 202
    wp_id = pending.json()["work_product_id"]

    doc.write_text("# Changed after submission\n")
    retry = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert retry.status_code == 400
    assert "source changed" in retry.json()["error"]
    assert approvals.get_approval(pending.json()["approval_id"])["status"] == "revoked"
    rows = work_product.list_work_products()["work_products"]
    assert len(rows) == 1
    assert rows[0]["id"] == wp_id
    assert rows[0]["status"] == "tampered"


def test_released_retry_succeeds_after_source_draft_is_removed(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    doc.write_text("# Synthetic memo\n")
    headers = _headers(tokens, "gcp_agent5")
    pending = client.post("/api/work-products", headers=headers, json=_body(doc, "Memo"))
    assert pending.status_code == 202
    wp_id = pending.json()["work_product_id"]

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending.json()["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(human)
    released = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert released.status_code == 200
    doc.unlink()

    replay = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert replay.status_code == 200
    assert replay.json()["id"] == wp_id
    assert replay.json()["note"] == "already released"
    assert len(work_product.list_work_products()["work_products"]) == 1


def test_approved_retry_tamper_revokes_pass_and_cannot_release_after_restore(setup_hub):
    client, workspace, tokens = setup_hub
    doc = workspace / "memo.md"
    original = "# Synthetic memo\n"
    doc.write_text(original)
    headers = _headers(tokens, "gcp_agent5")
    pending = client.post("/api/work-products", headers=headers, json=_body(doc, "Memo"))
    assert pending.status_code == 202
    wp_id = pending.json()["work_product_id"]
    approval_id = pending.json()["approval_id"]

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(approval_id, "approve")
    finally:
        config.agent_identity_var.reset(human)
    doc.write_text("# Changed after approval\n")
    retry = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert retry.status_code == 400
    assert work_product.get_work_product(wp_id)["status"] == "tampered"
    assert approvals.get_approval(approval_id)["status"] == "revoked"

    doc.write_text(original)
    replay = client.post(f"/api/work-products/{wp_id}/release", headers=headers)
    assert replay.status_code == 400
    assert "tampered" in replay.json()["error"]
    assert len(work_product.list_work_products()["work_products"]) == 1


def test_listed_submit_costs_one_approval(setup_hub, monkeypatch):
    """Listing work_product_submit asks only for its forced release gate.

    A wrapper pass would be spent once the call started, before the release
    gate parked, so the retry would ask for the listed tool again.
    """
    _client, workspace, _tokens = setup_hub
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "work_product_submit")
    doc = workspace / "listed.md"
    doc.write_text("# Listed draft\n")
    body = _body(doc, "Listed synthetic")
    submit = server.REGISTERED_TOOLS["work_product_submit"]

    actor = config.agent_identity_var.set("gcp_agent5")
    try:
        with pytest.raises(approvals.ApprovalPendingError):
            submit(**body)
    finally:
        config.agent_identity_var.reset(actor)
    [pending] = approvals.approval_pending()["pending"]
    assert pending["tool"] == "work_product_release"

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending["id"], "approve")
    finally:
        config.agent_identity_var.reset(human)

    actor = config.agent_identity_var.set("gcp_agent5")
    try:
        released = submit(**body)
    finally:
        config.agent_identity_var.reset(actor)
    assert released["status"] == "released"
    assert approvals.approval_pending()["pending"] == []
