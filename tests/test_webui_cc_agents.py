"""Control Center v2 agents routes: enrolment, connectors, org proposals (#2466, #2470).

The real scoped app runs behind the hub auth middleware with the fixture
identities of ``test_route_scopes``: Laptop_Agent5 is chief-of-staff,
Desktop_Agent2 a developer, server_agent8 public, and Desktop_Operator the verified
human operator (and the only lifecycle operator).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated
from test_webui_cc_work import _approve

from unified_agent import (
    agent_enrollment,
    agent_lifecycle,
    agent_roster,
    config,
    profiles,
    registry,
    storage,
)
from unified_agent.tools.tasks import task_add, task_claim
from unified_agent.webui import cc_agents, routes

COS = "Laptop_Agent5"
DEV = "Desktop_Agent2"
NEW = "Local_Worker"
ADD = "/api/action/agent/add"
REMOVE = "/api/action/agent/remove"
ROSTER = "/api/agent-roster"
SEATS = "/api/agent-seats"
ORG = "/api/action/org-proposal"
SERVER = {
    "server_id": "slack-prod",
    "connector": "slack",
    "provenance": "sha256:abc",
    "scopes": ["read"],
    "review_ref": "review:42",
}


@pytest.fixture(autouse=True)
def _hub(_isolated, monkeypatch, tmp_path):
    chart = tmp_path / "org.yaml"
    chart.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: founder-ceo\n    name: Operator\n    lead: $operator\n"
        "    members: [$operator]\n"
        "  - id: engineering\n    name: Engineering\n    lead: Desktop_Agent2\n"
        "    members: [Desktop_Agent2]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(chart))
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)
    monkeypatch.delenv("UA_EDITION", raising=False)
    agent_roster.invalidate()
    registry.reset_throttle()
    yield
    agent_roster.invalidate()
    agent_lifecycle.invalidate_identity_cache()


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _post(client, path, who, body):
    return client.post(path, headers=_bearer(who), json=body)


def _add(client, label=NEW, **extra):
    return _post(client, ADD, OPERATOR, {"label": label, "office": "engineering", **extra})


def _token_rows() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0]
    finally:
        conn.close()


def _files_holding(root: Path, secret: str) -> list[str]:
    needle = secret.encode()
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and needle in p.read_bytes()
    )


# --- enrolment ------------------------------------------------------------------------


def test_add_lists_authenticates_and_remove_revokes(client):
    added = _add(client, kind="local", expires_days=30, note="pilot")
    assert added.status_code == 200, added.text
    body = added.json()
    token = body["token"]
    assert body["agent"] == NEW and body["office"] == "engineering"
    assert body["token_shown_once"] is True and body["expires_at"]
    # The new bearer authenticates on the next request, with no restart.
    me = client.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["agent"] == NEW
    roster = client.get(ROSTER, headers=_bearer(OPERATOR))
    assert roster.status_code == 200 and roster.headers["cache-control"] == "no-store"
    [row] = [r for r in roster.json()["agents"] if r["agent"] == NEW]
    assert row["added"] and row["token_source"] == "managed" and row["token_status"] == "active"
    assert {"id": "engineering", "name": "Engineering"} in roster.json()["offices"]
    assert roster.json()["sealed_at_rest"] is True and token not in roster.text
    removed = _post(client, REMOVE, OPERATOR, {"label": NEW, "reason": "pilot over"})
    assert removed.status_code == 200, removed.text
    assert removed.json()["revoked_tokens"] == 1 and removed.json()["seat_removed"] is True
    assert removed.headers["cache-control"] == "no-store"
    refused = client.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code in (401, 403)


def test_the_bearer_appears_once_and_nowhere_else(client, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    added = _add(client)
    assert added.status_code == 200
    token = added.json()["token"]
    assert added.text.count(token) == 1
    assert added.headers["cache-control"] == "no-store" and added.headers["pragma"] == "no-cache"
    assert token not in caplog.text
    # Not in the database, the event ledger, the approval queue or any file
    # under the hub's data directory: the managed store keeps it sealed.
    assert _files_holding(tmp_path, token) == []
    # A repeat is refused and never mints or shows a second bearer.
    again = _add(client)
    assert again.status_code == 409 and again.json()["code"] == "already_added"
    assert "token" not in again.json() and _token_rows() == 1
    assert token not in client.get(ROSTER, headers=_bearer(OPERATOR)).text
    _post(client, REMOVE, OPERATOR, {"label": NEW, "reason": "done"})
    assert token not in caplog.text and _files_holding(tmp_path, token) == []


def test_add_refuses_an_unsealed_store_unless_the_opt_in_is_passed(client, monkeypatch):
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS")
    refused = _add(client)
    assert refused.status_code == 409 and refused.json()["code"] == "sealed_store_required"
    assert _token_rows() == 0 and agent_roster.seat(NEW) is None
    opted = _add(client, allow_plaintext_at_rest=True)
    assert opted.status_code == 200, opted.text
    assert any("stored unencrypted" in w for w in opted.json()["warnings"])


@pytest.mark.parametrize("path", [ADD, REMOVE, ROSTER])
def test_enrolment_is_the_operators_alone(client, path):
    method = "GET" if path == ROSTER else "POST"
    body = {"label": NEW, "office": "engineering", "reason": "r"}
    body = {k: v for k, v in body.items() if path != ADD or k != "reason"}
    if path == REMOVE:
        body.pop("office")
    assert client.request(method, path, json=body).status_code == 401
    for who in (DEV, COS, "server_agent8"):
        denied = client.request(method, path, headers=_bearer(who), json=body)
        assert denied.status_code == 403, (who, denied.text)
    assert _token_rows() == 0


def test_the_handler_refuses_a_non_operator_even_past_the_scope(client):
    """Defence in depth: the bare handler, with no scope gate in front of it."""
    bare = _client(cc_agents.ROUTES)
    for path, method in ((ADD, "POST"), (REMOVE, "POST"), (ROSTER, "GET")):
        res = bare.request(method, path, headers=_bearer(COS), json={})
        assert res.status_code == 403 and res.json()["code"] == "operator_required", path


@pytest.mark.parametrize(
    "body",
    [
        {"label": NEW, "office": "engineering", "token": "x"},  # unknown field
        {"label": NEW, "office": "engineering", "expires_days": "30"},  # ill-typed
        {"label": NEW, "office": "engineering", "kind": "remote"},  # not a kind
        {"label": NEW, "office": "engineering", "actor": DEV},  # another identity
        {"label": NEW},  # missing office
        {"label": NEW, "office": "nowhere"},  # unknown office (the service)
    ],
)
def test_add_refuses_a_bad_body_before_issuing(client, body):
    res = _post(client, ADD, OPERATOR, body)
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "invalid_request" and _token_rows() == 0


def test_remove_refuses_an_unknown_label_and_held_claims(client):
    unknown = _post(client, REMOVE, OPERATOR, {"label": "Desktop_Nobody", "reason": "r"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found"
    assert _post(client, REMOVE, OPERATOR, {"label": NEW}).json()["code"] == "invalid_request"
    _add(client)
    tid = task_add("held work")["id"]
    token = config.agent_identity_var.set(NEW)
    try:
        task_claim(tid, agent=NEW)
    finally:
        config.agent_identity_var.reset(token)
    held = _post(client, REMOVE, OPERATOR, {"label": NEW, "reason": "r"})
    assert held.status_code == 409 and held.json()["code"] == "claims_held"
    forced = _post(client, REMOVE, OPERATOR, {"label": NEW, "reason": "r", "force_release": True})
    assert forced.status_code == 200 and forced.json()["released"][0]["task_id"] == tid


def _approval_rows() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    finally:
        conn.close()


def test_a_repeated_add_answers_409_before_the_approval_gate(client, monkeypatch):
    """Review of f97738ef: with agent_add gated, a repeat must not queue an approval."""
    from unified_agent.tools import token_lifecycle

    assert _add(client).status_code == 200
    token = config.agent_identity_var.set(OPERATOR)
    try:  # a managed bearer issued outside the add route: no seat, an active token
        token_lifecycle.operator_issue("Local_Other", actor=OPERATOR)
    finally:
        config.agent_identity_var.reset(token)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "agent_add")
    for label in (NEW, "Local_Other"):
        again = _add(client, label=label)
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "already_added" and "token" not in again.json()
    assert _approval_rows() == 0 and _token_rows() == 2


@pytest.mark.parametrize("operation", ["agent_add", "agent_remove"])
def test_enrolment_parks_on_the_approval_gate(client, monkeypatch, operation):
    if operation == "agent_remove":
        assert _add(client).status_code == 200
    monkeypatch.setenv("UA_APPROVAL_TOOLS", operation)
    path, body = (
        (ADD, {"label": NEW, "office": "engineering"})
        if operation == "agent_add"
        else (REMOVE, {"label": NEW, "reason": "r"})
    )
    parked = _post(client, path, OPERATOR, body)
    assert parked.status_code == 202, parked.text
    assert parked.json()["code"] == "approval_required" and parked.json()["tool"] == operation
    assert "token" not in parked.json()
    # Nothing happened: no bearer issued, or the seat still in place.
    assert _token_rows() == (0 if operation == "agent_add" else 1)
    assert (agent_roster.seat(NEW) is None) is (operation == "agent_add")


def test_the_roster_and_remove_are_tenant_scoped(client, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    token = _add(client).json()["token"]
    mine = client.get(ROSTER, headers=_bearer(OPERATOR)).json()
    assert mine["org"] == "org_a" and NEW in {r["agent"] for r in mine["agents"]}
    seats = client.get(SEATS, headers=_bearer(DEV)).json()
    assert seats["org"] == "org_a" and NEW in {r["agent"] for r in seats["agents"]}
    assert client.get(f"/api/agents/{NEW}", headers=_bearer(DEV)).status_code == 200
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    theirs = client.get(ROSTER, headers=_bearer(OPERATOR)).json()
    assert theirs["org"] == "org_b" and NEW not in {r["agent"] for r in theirs["agents"]}
    # Task #2523: another organization's seat neither lists nor answers a detail.
    assert NEW not in {r["agent"] for r in client.get(SEATS, headers=_bearer(DEV)).json()["agents"]}
    assert client.get(f"/api/agents/{NEW}", headers=_bearer(DEV)).status_code == 404
    assert client.get(f"/api/agents/{NEW}/profile", headers=_bearer(OPERATOR)).status_code == 404
    foreign = _post(client, REMOVE, OPERATOR, {"label": NEW, "reason": "r"})
    assert foreign.status_code == 404 and foreign.json()["code"] == "not_found"
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert (
        client.get("/api/whoami", headers={"Authorization": f"Bearer {token}"}).json()["agent"]
        == NEW
    )


# --- Agents area hub gaps (task #2523) -------------------------------------------------


def test_any_signed_in_reader_gets_seats_without_credential_fields(client):
    """The roster answered only the operator, so other viewers guessed office and kind
    from the label. The seat read answers every identity and carries no token field."""
    assert _add(client, kind="cloud").status_code == 200
    for who in (OPERATOR, COS, DEV, "server_agent8"):
        res = client.get(SEATS, headers=_bearer(who))
        assert res.status_code == 200, (who, res.text)
        row = next(r for r in res.json()["agents"] if r["agent"] == NEW)
        assert set(row) == set(agent_enrollment.SEAT_FIELDS), row
        assert (row["office"], row["kind"], row["added"]) == ("engineering", "cloud", True)
        assert "token" not in res.text and "sealed_at_rest" not in res.json()
    assert client.get(SEATS).status_code == 401
    assert client.get(SEATS, params={"x": "1"}, headers=_bearer(DEV)).status_code == 400


def test_agent_profile_gives_the_ceiling_and_assigned_skills(client, tmp_path, monkeypatch):
    from test_skills_registry import make_skill

    from unified_agent import skills_registry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    assert _add(client).status_code == 200
    make_skill(workspace, "type-hints")
    assert skills_registry.publish("type-hints", "type-hints", author=OPERATOR)["ok"]
    skills_registry.assign("type-hints", NEW, author=OPERATOR)
    path = f"/api/agents/{NEW}/profile"
    for who in (OPERATOR, COS):
        res = client.get(path, headers=_bearer(who))
        assert res.status_code == 200, (who, res.text)
        body = res.json()
        assert body["profile"] == body["ceiling"]["profile"] == "developer"
        tools = body["ceiling"]["tools"]
        assert "task_claim" in tools and "skill_registry" not in tools
        assert body["ceiling"]["tool_count"] == len(tools)
        [skill] = body["skills"]["assigned"]
        assert skill["name"] == "type-hints" and skill["version"] == 1
        assert set(skill) == {"name", "version", "hash", "source"}  # never the content
        assert body["skills"]["installed"] is None and not body["skills"]["installed_known"]
    # skill_registry's ceiling: the developer and public profiles cannot read it.
    for who in (DEV, "server_agent8"):
        assert client.get(path, headers=_bearer(who)).status_code == 403
    missing = client.get("/api/agents/Nobody_Here/profile", headers=_bearer(OPERATOR))
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_a_seated_label_never_seen_has_a_detail_and_an_unknown_one_is_404(client):
    """GET /api/agents/{label} answered 404 until the first heartbeat."""
    assert _add(client).status_code == 200
    for who in (OPERATOR, DEV):
        res = client.get(f"/api/agents/{NEW}", headers=_bearer(who))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["connection"] == "never_connected"
        assert body["agent"]["status"] == "never_connected" and body["agent"]["last_seen"] is None
        assert body["seat"]["office"] == "engineering" and "token_status" not in body["seat"]
    assert client.get("/api/agents/Nobody_Here", headers=_bearer(DEV)).status_code == 404
    registry.touch_agent(NEW, via="test", force=True)
    seen = client.get(f"/api/agents/{NEW}", headers=_bearer(DEV)).json()
    assert seen["connection"] == "connected" and seen["agent"]["last_seen"]


def test_the_board_claimed_by_filter_is_a_shared_read(client):
    """Task #2523: the claimed_by board filter answers every signed-in identity alike."""
    task = task_add("claimed by the developer")
    task_add("claimed by nobody")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", (DEV, task["id"]))
        conn.commit()
    finally:
        conn.close()
    for who in (OPERATOR, DEV, "server_agent8"):
        res = client.get("/api/board", params={"claimed_by": DEV}, headers=_bearer(who))
        assert res.status_code == 200, (who, res.text)
        ids = [c.get("id") for col in res.json()["columns"] for c in col["cards"]]
        assert ids == [task["id"]], (who, ids)
    assert client.get("/api/board", params={"claimed_by": DEV}).status_code == 401


def test_wait_for_connected_returns_at_the_first_sighting(client, monkeypatch):
    """The add page polled every 5 s; the read now holds until the agent connects."""
    import threading

    monkeypatch.setattr(routes, "AGENT_WAIT_POLL_S", 0.05)
    added = _add(client).json()
    path = f"/api/agents/{NEW}"
    since = {"wait_for": "connected", "timeout": "1", "since": added["issued_at"]}
    timed_out = client.get(path, params=since, headers=_bearer(DEV)).json()
    assert timed_out["wait"]["satisfied"] is False and timed_out["connection"] == "never_connected"
    assert timed_out["wait"]["waited_s"] >= 0.9
    timer = threading.Timer(0.2, lambda: registry.touch_agent(NEW, via="test", force=True))
    timer.start()
    try:
        res = client.get(path, params={**since, "timeout": "5"}, headers=_bearer(OPERATOR))
    finally:
        timer.join()
    body = res.json()
    assert res.status_code == 200 and body["wait"]["satisfied"] is True, res.text
    assert body["connection"] == "connected" and body["wait"]["waited_s"] < 4
    # A sighting before ``since`` (an earlier life of the label) does not count.
    later = {**since, "since": "2999-01-01T00:00:00+00:00", "timeout": "0"}
    assert client.get(path, params=later, headers=_bearer(DEV)).json()["wait"]["satisfied"] is False
    for bad in (
        {"wait_for": "online"},
        {"wait_for": "connected", "timeout": "26"},
        {"timeout": "1"},
    ):
        assert client.get(path, params=bad, headers=_bearer(DEV)).status_code == 400
    assert (
        client.get("/api/agents/Nobody_Here", params=since, headers=_bearer(DEV)).status_code == 404
    )
    monkeypatch.setattr(routes, "AGENT_WAIT_MAX_WAITERS", 0)
    other = _add(client, label="Other_Worker").json()
    full = {**since, "since": other["issued_at"]}
    busy = client.get("/api/agents/Other_Worker", params=full, headers=_bearer(DEV))
    assert busy.status_code == 429 and busy.json()["code"] == "rate_limited"
    assert client.get(path, params={"wait_for": "connected"}).status_code == 401


# --- connectors -----------------------------------------------------------------------


def _approved(client, path, who, body):
    """Send, approve the parked call as the operator, send the identical body again."""
    parked = _post(client, path, who, body)
    assert parked.status_code == 202, parked.text
    assert parked.json()["code"] == "approval_required"
    _approve(parked.json()["approval_id"])
    return _post(client, path, who, body)


def test_connector_register_authorize_revoke(client):
    registered = _approved(client, "/api/action/connector/register", COS, SERVER)
    assert registered.status_code == 200, registered.text
    assert registered.json()["server"]["active"] is True
    again = _post(client, "/api/action/connector/register", COS, SERVER)
    assert again.status_code == 409 and again.json()["code"] == "already_registered"
    use = {"server_id": "slack-prod", "scope": "read", "task_id": 7, "trace_id": "trace-1"}
    authorized = _approved(client, "/api/action/connector/authorize", COS, use)
    assert (
        authorized.status_code == 200 and authorized.json()["server"]["server_id"] == "slack-prod"
    )
    # The operator passes the ceiling and parks too; only a different human may approve.
    parked = _post(
        client, "/api/action/connector/register", OPERATOR, {**SERVER, "server_id": "s2"}
    )
    assert parked.status_code == 202 and parked.json()["tool"] == "horizon2_server_register"
    revoked = _approved(client, "/api/action/connector/revoke", COS, {"server_id": "slack-prod"})
    assert revoked.status_code == 200 and revoked.json()["server"]["active"] is False
    repeat = _post(client, "/api/action/connector/revoke", OPERATOR, {"server_id": "slack-prod"})
    assert repeat.status_code == 200 and repeat.json()["already_revoked"] is True
    missing = _post(client, "/api/action/connector/revoke", OPERATOR, {"server_id": "nope"})
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_connector_package_register_parks_under_the_server_gate(client):
    body = {"package": "drive", "provenance": "sha256:def", "review_ref": "review:43"}
    registered = _approved(client, "/api/action/connector/register-package", COS, body)
    assert registered.status_code == 200, registered.text
    assert registered.json()["server"]["connector"] == "drive"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/connector/register", {**SERVER, "reviewed_by": OPERATOR}),
        ("/api/action/connector/register", {**SERVER, "scopes": "read"}),
        ("/api/action/connector/authorize", {"server_id": "s", "scope": "read", "task_id": "7"}),
        ("/api/action/connector/revoke", {}),
    ],
)
def test_connector_routes_refuse_a_bad_body(client, path, body):
    res = _post(client, path, COS, body)
    assert res.status_code == 400 and res.json()["code"] == "invalid_request", res.text


def test_connector_routes_hold_the_profile_ceiling(client):
    for path in ("/api/action/connector/register", "/api/action/connector/revoke"):
        assert _post(client, path, "server_agent8", SERVER).status_code == 403
        assert client.post(path, json=SERVER).status_code == 401


# --- org proposals --------------------------------------------------------------------


def _propose(author=DEV) -> dict:
    from unified_agent import self_organization

    token = config.agent_identity_var.set(author)
    try:
        return self_organization.org_proposal(
            "propose",
            {
                "title": "Split the queue",
                "proposal_type": "routing",
                "scope": "engineering",
                "rationale": "load",
                "proposed_changes": {"lanes": ["a", "b"]},
            },
        )
    finally:
        config.agent_identity_var.reset(token)


def test_org_proposal_review_apply_and_reject(client):
    proposal = _propose()
    pid = proposal["proposal_id"]
    reviewed = _post(client, ORG, DEV, {"action": "review", "proposal_id": pid, "notes": "ok"})
    assert reviewed.status_code == 200 and reviewed.json()["reviewed_by"] == DEV
    # A decision is the operator's, whatever the agent's profile allows.
    denied = _post(client, ORG, COS, {"action": "apply", "proposal_id": pid})
    assert denied.status_code == 403 and denied.json()["code"] == "operator_required"
    # Before a human approves the proposal's approval row, apply parks on that row.
    waiting = _post(client, ORG, OPERATOR, {"action": "apply", "proposal_id": pid})
    assert waiting.status_code == 202, waiting.text
    assert waiting.json()["approval_id"] == proposal["approval_id"]
    assert waiting.json()["code"] == "approval_required"
    _approve(proposal["approval_id"])
    applied = _post(client, ORG, OPERATOR, {"action": "apply", "proposal_id": pid})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    twice = _post(client, ORG, OPERATOR, {"action": "apply", "proposal_id": pid})
    assert twice.status_code == 409 and twice.json()["code"] == "conflict"
    other = _propose()["proposal_id"]
    assert _post(client, ORG, DEV, {"action": "reject", "proposal_id": other}).status_code == 403
    rejected = _post(client, ORG, OPERATOR, {"action": "reject", "proposal_id": other})
    assert rejected.status_code == 200 and rejected.json()["rejected_by"] == OPERATOR


@pytest.mark.parametrize(
    "body",
    [
        {"action": "approve", "proposal_id": "orgprop-x"},  # not an action
        {"action": "review", "proposal_id": "orgprop-x", "reason": "r"},  # apply's field
        {"action": "review", "proposal_id": "orgprop-x", "reviewed_by": COS},  # spoof
        {"action": "reject", "proposal_id": "orgprop-x", "reviewed_by": OPERATOR},  # wrong arg
        {"action": "review"},  # missing id
        {"action": [], "proposal_id": "orgprop-x"},  # unhashable action
        {"action": {}, "proposal_id": "orgprop-x"},  # unhashable action
    ],
)
def test_org_proposal_refuses_a_bad_body(client, body):
    res = _post(client, ORG, OPERATOR, body)
    assert res.status_code == 400 and res.json()["code"] == "invalid_request", res.text


def test_org_proposal_is_tenant_scoped(client, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    pid = _propose()["proposal_id"]
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    for action in ("review", "reject"):
        res = _post(client, ORG, OPERATOR, {"action": action, "proposal_id": pid})
        assert res.status_code == 404 and res.json()["code"] == "not_found", res.text


def test_connector_handlers_check_the_route_roles_themselves():
    # Task #2517: the handlers, not only the scope wrapper, refuse a caller outside
    # the declared roles (developer, steward, chief of staff, the operator).
    bare = _client(routes._declared_routes())
    bodies = {
        "/api/action/connector/register": SERVER,
        "/api/action/connector/register-package": {
            "package": "p",
            "provenance": "sha256:abc",
            "review_ref": "review:1",
        },
        "/api/action/connector/authorize": {
            "server_id": "slack-prod",
            "scope": "read",
            "task_id": 1,
            "trace_id": "t1",
        },
        "/api/action/connector/revoke": {"server_id": "slack-prod"},
    }
    for path, body in bodies.items():
        for outsider in ("Desktop_Agent8", "server_agent8"):
            refused = _post(bare, path, outsider, body)
            assert refused.status_code == 403, (path, outsider, refused.text)
            assert refused.json()["code"] == "forbidden"
        assert _post(bare, path, COS, body).status_code != 403, path
