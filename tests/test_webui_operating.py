"""Control Center Operating pane HTTP surface (task #1757).

Reads: ``GET /api/operating``, ``/api/operating/resolve``, ``/api/operating/sprints``.
Writes: ``POST /api/action/operating-sprint`` and ``/api/action/operating-run``,
through goal_manage's gates. A human writes directly; an agent's write parks
as an ``operating_settings`` approval and answers 202.
"""

import json
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage
from unified_agent.tools import tasks
from unified_agent.webui import auth
from unified_agent.webui import routes as webui_routes
from unified_agent.webui.routes import web_routes

ORG = {
    "schema_version": 1,
    "offices": [
        {
            "id": "founder",
            "name": "Founder",
            "lead": "Desktop_Person",
            "members": ["Desktop_Person"],
        },
        {"id": "staff", "name": "Staff", "lead": "Desktop_Agent5", "members": ["Desktop_Agent5"]},
        {
            "id": "platform",
            "name": "Platform",
            "lead": "Desktop_Agent2",
            "members": ["Desktop_Agent2", "Desktop_agent3"],
        },
    ],
}

PROFILES = {
    "default": "standard",
    "profiles": {
        "standard": {
            "description": "Anyone may reach the operator.",
            "comms": {"mode": "direct"},
            "delivery": {"mode": "no-mistakes"},
            "planning": "P2",
        },
        "crew": {
            "extends": "standard",
            "comms": {"mode": "funnel", "liaison": "office:staff"},
            "autonomy": "checkpoint",
        },
    },
    "units": {"platform": "crew"},
}

HUMAN = f"tok-human-{secrets.token_urlsafe(8)}"
AGENT = f"tok-agent-{secrets.token_urlsafe(8)}"
RESEARCHER = f"tok-research-{secrets.token_urlsafe(8)}"


def _policy(block=PROFILES, rules=()) -> str:
    return json.dumps(
        {
            "version": 1,
            "enabled": True,
            "default_action": "allow",
            "rules": list(rules),
            "operating_profiles": block,
        }
    )


@pytest.fixture
def policy_path(isolated_env, tmp_path, monkeypatch):
    org = tmp_path / "org.yaml"
    org.write_text(json.dumps(ORG))
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(org))
    path = tmp_path / "policy.yaml"
    path.write_text(_policy())
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    storage.init_db()
    return path


@pytest.fixture
def client(policy_path, monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Desktop_Person:{HUMAN},Desktop_Agent2:{AGENT},Desktop_Agent8:{RESEARCHER}",
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as c:
        yield c


def _h(token: str = HUMAN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_profiles_view_lists_offices_with_bindings(client):
    res = client.get("/api/operating", headers=_h())
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is True
    assert body["default"] == "standard"
    assert body["units"] == {"platform": "crew"}
    assert body["profiles"]["crew"]["effective"]["planning"] == "P2"
    assert body["profiles"]["crew"]["effective"]["comms"]["mode"] == "funnel"
    # #1756 wires every field but relay to its consumer.
    assert body["enforced_fields"] == [
        "autonomy",
        "budget",
        "comms",
        "delivery",
        "instructions",
        "model",
        "planning",
    ]
    offices = {o["id"]: o for o in body["offices"]}
    assert offices["platform"]["profile"] == "crew"
    assert offices["founder"]["profile"] is None
    assert offices["platform"]["members"] == ["Desktop_Agent2", "Desktop_agent3"]
    assert body["policy_edit"]["tool"] == "config_revision_manage"
    assert body["policy_edit"]["kind"] == "policy"


def test_malformed_policy_is_a_named_error_not_a_500(client, policy_path):
    policy_path.write_text(_policy({"profiles": {"x": {"comms": {"mode": "nope"}}}}))
    res = client.get("/api/operating", headers=_h())
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == "operating_unavailable"
    assert body["profiles"] == {} and body["offices"] == []


def test_human_sprint_lifecycle_and_resolved_provenance(client):
    tid = tasks.task_add("sprint member")["id"]
    added = client.post(
        "/api/action/operating-sprint",
        headers=_h(),
        json={"op": "add", "name": "October", "profile": "crew", "org_unit": "platform"},
    )
    assert added.status_code == 200, added.text
    sprint = added.json()
    assert sprint["name"] == "October" and sprint["created_by"] == "Desktop_Person"
    sid = sprint["id"]

    updated = client.post(
        "/api/action/operating-sprint",
        headers=_h(),
        json={
            "op": "update",
            "sprint_id": sid,
            "status": "active",
            "overrides": {"planning": "P3"},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "active"

    linked = client.post(
        "/api/action/operating-sprint",
        headers=_h(),
        json={"op": "link", "sprint_id": sid, "task_id": tid},
    )
    assert linked.status_code == 200 and linked.json()["task_ids"] == [tid]

    listed = client.get("/api/operating/sprints?status=active", headers=_h())
    assert listed.status_code == 200
    assert [(s["id"], s["task_ids"]) for s in listed.json()["sprints"]] == [(sid, [tid])]
    assert listed.json()["statuses"] == ["planned", "active", "closed"]

    resolved = client.get(f"/api/operating/resolve?task_id={tid}", headers=_h())
    assert resolved.status_code == 200
    view = resolved.json()
    assert view["sprint_id"] == sid and view["unit"] == "platform"
    assert view["settings"]["planning"] == "P3"
    assert view["sources"]["planning"] == f"sprint:{sid}:overrides"
    assert view["sources"]["comms.mode"] == f"sprint:{sid}:crew"
    assert view["enforcement"]["comms"] == "enforced"
    assert view["enforcement"]["planning"] == "enforced"

    unlinked = client.post(
        "/api/action/operating-sprint",
        headers=_h(),
        json={"op": "unlink", "sprint_id": sid, "task_id": tid},
    )
    assert unlinked.status_code == 200 and unlinked.json()["task_ids"] == []


def test_human_run_set_and_clear(client):
    tid = tasks.task_add("one run")["id"]
    res = client.post(
        "/api/action/operating-run",
        headers=_h(),
        json={"op": "set", "task_id": tid, "profile": "crew", "overrides": {"model": "m-1"}},
    )
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["settings"]["model"] == "m-1"
    assert view["sources"]["model"] == f"run:task-{tid}:overrides"
    assert view["sources"]["comms.mode"] == f"run:task-{tid}:crew"

    cleared = client.post(
        "/api/action/operating-run", headers=_h(), json={"op": "clear", "task_id": tid}
    )
    assert cleared.status_code == 200
    assert "model" not in cleared.json()["settings"]
    assert cleared.json()["sources"]["comms.mode"] == "org:standard"


def test_agent_write_parks_for_approval(client):
    tid = tasks.task_add("agent asks")["id"]
    res = client.post(
        "/api/action/operating-run",
        headers=_h(AGENT),
        json={"op": "set", "task_id": tid, "overrides": {"comms": {"mode": "direct"}}},
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["pending"] is True and body["approval_id"]
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("path", "payload", "status", "needle"),
    [
        ("/api/action/operating-sprint", {"op": "delete", "sprint_id": 1}, 400, "op must be"),
        ("/api/action/operating-run", {"op": "set", "task_id": 1, "actor": "x"}, 400, "actor"),
        (
            "/api/action/operating-run",
            {"op": "clear", "task_id": 1, "profile": "x"},
            400,
            "unknown",
        ),
        (
            "/api/action/operating-sprint",
            {"op": "add", "name": "x", "profile": "nope"},
            400,
            "nope",
        ),
        ("/api/action/operating-run", {"op": "set", "task_id": 999999, "profile": "crew"}, 404, ""),
        ("/api/action/operating-sprint", {"op": "update", "sprint_id": 999, "name": "y"}, 404, ""),
    ],
)
def test_bad_writes_are_4xx(client, path, payload, status, needle):
    res = client.post(path, headers=_h(), json=payload)
    assert res.status_code == status, res.text
    assert needle in res.json()["error"]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"op": "add", "name": "x", "starts_at": "next week"}, "ISO-8601"),
        (
            {"op": "add", "name": "x", "starts_at": "2026-11-30", "ends_at": "2026-11-01"},
            "starts_at must not be after ends_at",
        ),
        ({"op": "add", "name": "x", "overrides": {"comms": {"mode": "funnel"}}}, "liaison"),
    ],
)
def test_sprint_writes_the_hub_refuses_are_400_with_the_reason(client, payload, needle):
    res = client.post("/api/action/operating-sprint", headers=_h(), json=payload)
    assert res.status_code == 400, res.text
    assert needle in res.json()["error"]
    listed = client.get("/api/operating/sprints", headers=_h()).json()["sprints"]
    assert listed == []


def test_sprint_dates_round_trip(client):
    res = client.post(
        "/api/action/operating-sprint",
        headers=_h(),
        json={"op": "add", "name": "Dated", "starts_at": "2026-11-01", "ends_at": "2026-11-30"},
    )
    assert res.status_code == 200, res.text
    assert (res.json()["starts_at"], res.json()["ends_at"]) == ("2026-11-01", "2026-11-30")


def test_bad_reads_are_4xx(client):
    assert client.get("/api/operating/resolve?task_id=abc", headers=_h()).status_code == 400
    assert client.get("/api/operating/resolve?task_id=999999", headers=_h()).status_code == 404
    assert client.get("/api/operating/resolve?sprint_id=999", headers=_h()).status_code == 404
    assert client.get("/api/operating/sprints?status=done", headers=_h()).status_code == 400
    org_only = client.get("/api/operating/resolve", headers=_h())
    assert org_only.status_code == 200
    assert org_only.json()["sources"]["comms.mode"] == "org:standard"


def test_auth_csrf_and_profile_gates(client, monkeypatch):
    assert client.get("/api/operating").status_code == 401
    assert (
        client.post("/api/action/operating-run", json={"op": "clear", "task_id": 1}).status_code
        == 401
    )
    client.cookies.set(auth.COOKIE_NAME, "stale-session")
    try:
        res = client.post(
            "/api/action/operating-sprint", headers=_h(), json={"op": "add", "name": "x"}
        )
        assert res.status_code == 403 and "CSRF" in res.json()["error"]
    finally:
        client.cookies.clear()

    real = webui_routes.profiles.tool_is_allowed_for
    monkeypatch.setattr(
        webui_routes.profiles,
        "tool_is_allowed_for",
        lambda profile, tool: False if tool == "goal_manage" else real(profile, tool),
    )
    assert client.get("/api/operating", headers=_h(AGENT)).status_code == 403
    res = client.post(
        "/api/action/operating-sprint", headers=_h(AGENT), json={"op": "add", "name": "x"}
    )
    assert res.status_code == 403
    # The human operator's web profile has no goal_manage; a verified human
    # is still admitted, because operating settings are the operator's to set.
    assert client.get("/api/operating", headers=_h()).status_code == 200


def _sprint_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM sprints").fetchone()[0]
    finally:
        conn.close()


def test_policy_deny_rule_blocks_a_human_operating_write(client, policy_path):
    """The human skips only profile visibility; policy rules still bind."""
    deny = {"name": "freeze-goal-manage", "tool": "goal_manage", "type": "tool", "action": "deny"}
    policy_path.write_text(_policy(rules=[deny]))
    res = client.post(
        "/api/action/operating-sprint", headers=_h(), json={"op": "add", "name": "Blocked"}
    )
    assert res.status_code == 403, res.text
    tid = tasks.task_add("frozen run")["id"]
    res = client.post(
        "/api/action/operating-run",
        headers=_h(),
        json={"op": "set", "task_id": tid, "profile": "crew"},
    )
    assert res.status_code == 403, res.text
    assert _sprint_count() == 0
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_settings").fetchone()[0] == 0
    finally:
        conn.close()


def test_non_human_without_goal_manage_is_refused(client):
    """Desktop_Agent8 defaults to the research profile, which has no goal_manage."""
    assert client.get("/api/operating", headers=_h(RESEARCHER)).status_code == 403
    assert client.get("/api/operating/sprints", headers=_h(RESEARCHER)).status_code == 403
    assert client.get("/api/operating/resolve", headers=_h(RESEARCHER)).status_code == 403
    res = client.post(
        "/api/action/operating-sprint", headers=_h(RESEARCHER), json={"op": "add", "name": "x"}
    )
    assert res.status_code == 403
    assert "goal_manage" in res.json()["error"]
    assert _sprint_count() == 0


def _approve(approval_id: int) -> None:
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET status='approved', resolved_at=?, updated_at=? WHERE id=?",
            (now, now, int(approval_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _approval_status(approval_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
    finally:
        conn.close()


def test_agent_retry_keeps_the_goal_manage_pass_until_the_write_lands(client, monkeypatch):
    """Two gates (goal_manage, operating_settings): a pass is spent only by the write."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    tid = tasks.task_add("two gates")["id"]
    body = {"op": "set", "task_id": tid, "overrides": {"model": "m-2"}}

    first = client.post("/api/action/operating-run", headers=_h(AGENT), json=body)
    assert first.status_code == 202, first.text
    gm = first.json()["approval_id"]
    _approve(gm)

    second = client.post("/api/action/operating-run", headers=_h(AGENT), json=body)
    assert second.status_code == 202, second.text
    ops = second.json()["approval_id"]
    assert ops != gm
    # Parked on operating_settings before any write: the goal_manage pass is restored.
    assert _approval_status(gm) == "approved"
    _approve(ops)

    third = client.post("/api/action/operating-run", headers=_h(AGENT), json=body)
    assert third.status_code == 200, third.text
    assert third.json()["settings"]["model"] == "m-2"
    assert _approval_status(gm) == "consumed" and _approval_status(ops) == "consumed"


def test_a_refused_write_does_not_spend_the_pass(client, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "goal_manage")
    tid = tasks.task_add("removed before approved replay")["id"]
    body = {"op": "set", "task_id": tid, "profile": "crew"}
    first = client.post("/api/action/operating-run", headers=_h(), json=body)
    assert first.status_code == 202, first.text
    gm = first.json()["approval_id"]
    _approve(gm)
    tasks.task_delete(tid)
    refused = client.post("/api/action/operating-run", headers=_h(), json=body)
    assert refused.status_code == 404, refused.text
    assert _approval_status(gm) == "approved"


def test_downgraded_agent_is_refused_without_a_session_profile(client, monkeypatch):
    """The identity's own default (with a committed downgrade) must allow goal_manage too."""
    real_default = webui_routes.profiles.default_for_identity
    monkeypatch.setattr(webui_routes.profiles, "current", lambda: "developer")
    monkeypatch.setattr(
        webui_routes.profiles,
        "default_for_identity",
        lambda label: "research" if label == "Desktop_Agent2" else real_default(label),
    )
    assert client.get("/api/operating", headers=_h(AGENT)).status_code == 403
    res = client.post(
        "/api/action/operating-sprint", headers=_h(AGENT), json={"op": "add", "name": "x"}
    )
    assert res.status_code == 403
    assert _sprint_count() == 0


def test_resolve_reports_the_stored_run_layer(client):
    tid = tasks.task_add("stored layer")["id"]
    assert (
        client.get(f"/api/operating/resolve?task_id={tid}", headers=_h()).json()["run_settings"]
        is None
    )
    client.post(
        "/api/action/operating-run",
        headers=_h(),
        json={"op": "set", "task_id": tid, "profile": "crew", "overrides": {"model": "m-3"}},
    )
    stored = client.get(f"/api/operating/resolve?task_id={tid}", headers=_h()).json()[
        "run_settings"
    ]
    assert stored["profile"] == "crew"
    assert stored["overrides"] == {"model": "m-3"}
    assert stored["set_by"] == "Desktop_Person"


def test_resolve_and_stored_layer_come_from_one_snapshot(client, monkeypatch):
    """resolve() reads on the same connection, inside the read transaction."""
    from unified_agent import operating_profiles

    seen = []
    real = operating_profiles.resolve

    def spy(**kwargs):
        conn = kwargs.get("conn")
        seen.append(conn is not None and conn.in_transaction)
        return real(**kwargs)

    monkeypatch.setattr(operating_profiles, "resolve", spy)
    tid = tasks.task_add("snapshot")["id"]
    res = client.get(f"/api/operating/resolve?task_id={tid}", headers=_h())
    assert res.status_code == 200 and "run_settings" in res.json()
    assert seen == [True]


def _write_run_profile(task_id: int, profile: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE run_settings SET profile=? WHERE task_id=?", (profile, task_id))
        conn.commit()
    finally:
        conn.close()


def test_concurrent_run_write_does_not_split_the_snapshot(client, monkeypatch):
    """A run write committing between resolve() and the stored-row read is not seen."""
    from unified_agent import operating_profiles

    tid = tasks.task_add("racing write")["id"]
    client.post(
        "/api/action/operating-run",
        headers=_h(),
        json={"op": "set", "task_id": tid, "profile": "crew"},
    )
    real = operating_profiles.resolve

    def racing(**kwargs):
        out = real(**kwargs)
        # Another operator's write commits on a second connection, after the
        # resolve read but before the endpoint reads the stored layer.
        _write_run_profile(tid, "standard")
        return out

    monkeypatch.setattr(operating_profiles, "resolve", racing)
    body = client.get(f"/api/operating/resolve?task_id={tid}", headers=_h()).json()
    # Both halves describe the pre-write state.
    assert body["run_settings"]["profile"] == "crew"
    monkeypatch.setattr(operating_profiles, "resolve", real)
    after = client.get(f"/api/operating/resolve?task_id={tid}", headers=_h()).json()
    assert after["run_settings"]["profile"] == "standard"
