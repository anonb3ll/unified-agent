"""Operator node actions over HTTP (task #2208).

``node_retire`` / ``node_unretire`` (task #2181) were MCP-only, and the human
operator's MCP profile is read-only, so the operator could not run them at all.
``POST /api/action/node-retire`` is their HTTP twin, used by the Devices pane
and the ``ua node`` CLI. Same harness as ``test_node_acceptance.py``: the real
route table behind the real auth middleware and the SQLite queue.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import node_queue, server, storage, webui
from unified_agent.webui import auth

OPERATOR = "Desktop_Operator"
OWNER = "Desktop_Agent2"
NODE = "old-host.local"
OPERATOR_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
OWNER_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
ROUTE = "/api/action/node-retire"


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{OPERATOR}:{OPERATOR_TOKEN},{OWNER}:{OWNER_TOKEN}")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    # Unset, the lifecycle operators are the operator label and its aliases.
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)
    monkeypatch.delenv("UA_OPERATOR_ALIASES", raising=False)
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    for name in (
        "UA_NODE_REQUIRE_ACCEPTANCE",
        "UA_REVOKED_AGENTS",
        "UA_FLEET_NODES",
        "UA_FLEET_OPTIONAL_NODES",
        "UA_APPROVAL_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    node_queue.register(NODE, identity=OWNER, facts={"backends": ["codex"]})
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client


def _bearer(token: str = OPERATOR_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post(hub, body, token: str | None = OPERATOR_TOKEN):
    return hub.post(ROUTE, json=body, headers=_bearer(token) if token else {})


def _retired_at(node: str = NODE):
    conn = storage.connect()
    try:
        row = conn.execute("SELECT retired_at FROM nodes WHERE node=?", (node,)).fetchone()
    finally:
        conn.close()
    return row["retired_at"]


def _queue_item(node: str = NODE) -> None:
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
            "status, created_by, created_at, updated_at) "
            "VALUES ('nd-0000000000002208', ?, ?, 'codex', 'p', '/w', 60, 'queued', ?, ?, ?)",
            (node, OWNER, OPERATOR, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


# ── authentication and CSRF ──────────────────────────────────────────────────


def test_no_identity_is_401(hub):
    resp = _post(hub, {"action": "retire", "node": NODE, "reason": "renamed"}, token=None)
    assert resp.status_code == 401
    assert _retired_at() is None


def test_a_cookie_session_needs_the_csrf_token(hub):
    session_id, csrf = auth.create_session(OPERATOR)
    hub.cookies.set(auth.COOKIE_NAME, session_id)
    body = {"action": "retire", "node": NODE, "reason": "renamed"}

    rejected = hub.post(ROUTE, json=body)
    assert rejected.status_code == 403
    assert rejected.json()["error"] == "invalid or missing CSRF token"
    assert _retired_at() is None

    accepted = hub.post(ROUTE, json=body, headers={"x-ua-csrf": csrf})
    assert accepted.status_code == 200, accepted.json()
    assert _retired_at() is not None


# ── authority ────────────────────────────────────────────────────────────────


def test_a_non_operator_is_refused_even_for_its_own_node(hub):
    resp = _post(hub, {"action": "retire", "node": NODE, "reason": "mine"}, token=OWNER_TOKEN)
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "permission_denied"
    assert "operator action" in body["error"]
    assert _retired_at() is None


def test_the_operator_retires_and_unretires_a_node(hub):
    retired = _post(hub, {"action": "retire", "node": NODE, "reason": "renamed device"})
    assert retired.status_code == 200, retired.json()
    assert retired.json()["retired"] is True and retired.json()["changed"] is True
    assert _retired_at() is not None

    # Retired rows leave ``nodes`` and are listed under ``retired_nodes``.
    listing = hub.get("/api/nodes", headers=_bearer()).json()
    assert NODE not in {n["node"] for n in listing["nodes"]}
    [row] = listing["retired_nodes"]
    assert row["node"] == NODE and row["agent"] == OWNER
    assert row["retired_by"] == OPERATOR and row["retired_reason"] == "renamed device"

    again = _post(hub, {"action": "retire", "node": NODE, "reason": "renamed device"})
    assert again.status_code == 200 and again.json()["changed"] is False

    back = _post(hub, {"action": "unretire", "node": NODE, "reason": "device is back"})
    assert back.status_code == 200, back.json()
    assert back.json()["retired"] is False and back.json()["changed"] is True
    assert _retired_at() is None
    assert hub.get("/api/nodes", headers=_bearer()).json()["retired_nodes"] == []


def test_retire_is_409_while_the_node_has_queued_work(hub):
    _queue_item()
    resp = _post(hub, {"action": "retire", "node": NODE, "reason": "renamed"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "conflict"
    assert "nd-0000000000002208" in body["error"]
    assert _retired_at() is None


# ── strict body ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body, message",
    [
        ({"action": "delete", "node": NODE, "reason": "x"}, "action must be"),
        ({"action": "retire", "reason": "x"}, "node is required"),
        ({"action": "retire", "node": "  ", "reason": "x"}, "node is required"),
        ({"action": "retire", "node": NODE}, "a reason is required"),
        ({"action": "retire", "node": NODE, "reason": "   "}, "a reason is required"),
        ({"action": "retire", "node": NODE, "reason": 5}, "a reason is required"),
        ({"action": "retire", "node": NODE, "reason": "x", "force": True}, "unknown field"),
    ],
)
def test_the_body_is_validated(hub, body, message):
    resp = _post(hub, body)
    assert resp.status_code == 400
    assert message in resp.json()["error"]
    assert _retired_at() is None


def test_an_unknown_node_is_400(hub):
    resp = _post(hub, {"action": "retire", "node": "never-registered", "reason": "x"})
    assert resp.status_code == 400
    assert "not registered" in resp.json()["error"]


def test_a_non_object_body_is_400(hub):
    resp = hub.post(ROUTE, content=b"[]", headers=_bearer())
    assert resp.status_code == 400


def test_a_parked_retire_is_202_pending_and_changes_nothing(hub, tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: park_node_actions\n    type: tool\n    tool: coordination_manage\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "coordination_manage")
    resp = _post(hub, {"action": "retire", "node": NODE, "reason": "renamed"})
    assert resp.status_code == 202, resp.json()
    body = resp.json()
    assert body["status"] == "pending" and body["code"] == "approval_required"
    assert type(body["approval_id"]) is int and body["approval_id"] > 0
    assert _retired_at() is None
