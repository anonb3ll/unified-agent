"""Control Center v2 Home and Inbox hub routes (tasks #2466, #2514).

The real scoped app runs behind the hub auth middleware with the fixture
identities of ``test_route_scopes``: Laptop_Agent5 is chief-of-staff, Desktop_Agent2
is a developer, server_agent8 is public, and Desktop_Operator is the verified human
operator (``UA_OPERATOR_LABEL``), whose default alias is ``Operator``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated
from test_webui_cc_work import _approve

from unified_agent import (
    config,
    github_app,
    needs_operator,
    profiles,
    registry,
    review_holds,
    storage,
    work_proposals,
)
from unified_agent.messaging import wake
from unified_agent.tools import approvals, tasks
from unified_agent.webui import routes

COS = "Laptop_Agent5"
DEV = "Desktop_Agent2"
PUBLIC = "server_agent8"


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _row(payload: str = "{}", *, requested_by: str | None = DEV, status: str = "pending") -> int:
    now = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at, "
            "resolved_at) VALUES ('delegate', ?, ?, ?, ?, ?, ?)",
            (payload, requested_by, status, now, now, now if status != "pending" else None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _detail(client, approval_id: int, who: str = OPERATOR) -> dict:
    res = client.get(f"/api/approvals/{approval_id}", headers=_bearer(who))
    assert res.status_code == 200, res.text
    return res.json()


# --- (h) approval preview completeness ---------------------------------------------


def test_approval_detail_shows_every_target_and_says_when_it_cannot(client):
    import json

    targets = [f"repo-{i}" for i in range(11)]
    meta = {f"key{i}": i for i in range(12)}
    full = _detail(
        client,
        _row(json.dumps({"targets": targets, "meta": meta, "api_token": "abc123"})),
    )
    assert full["payload"]["targets"] == targets  # the 11th target is not hidden
    assert full["payload"]["meta"] == meta
    assert full["payload"]["api_token"] == "[REDACTED_SECRET]"
    assert full["payload_complete"] is True and full["payload_omissions"] == []
    huge = _detail(client, _row(json.dumps({"targets": list(range(5_000))})))
    assert huge["payload_complete"] is False
    [omission] = huge["payload_omissions"]
    assert omission["path"] == "$.targets" and omission["kind"] == "items"
    assert omission["omitted"] == 5_000 - len(huge["payload"]["targets"])


# --- (r) whether the caller may approve ---------------------------------------------


def test_approval_detail_says_whether_the_caller_may_approve(client):
    by_agent = _row(requested_by=DEV)
    assert _detail(client, by_agent)["approver"]["may_approve"] == "yes"
    assert _detail(client, by_agent, DEV)["approver"]["may_approve"] == "no"  # not a human
    # The operator's alias is the operator: separation of duties, alias-aware.
    by_alias = _detail(client, _row(requested_by=config.operator_name()))["approver"]
    assert by_alias["may_approve"] == "no" and "different person" in by_alias["reason"]
    assert _detail(client, _row(requested_by=OPERATOR))["approver"]["may_approve"] == "no"
    decided = _row(status="approved")
    assert _detail(client, decided)["approver"]["may_approve"] == "no"
    unknown = _detail(client, _row(requested_by=None))["approver"]
    assert unknown["may_approve"] == "unknown" and unknown["reason"]


# --- (i) pass state and call outcome ------------------------------------------------


def _save(client, content: str):
    return client.post("/api/action/memory/save", headers=_bearer(DEV), json={"content": content})


def test_approval_detail_reports_pass_expiry_and_the_call_outcome(client, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "memory_save")
    parked = _save(client, "first")
    assert parked.status_code == 202, parked.text
    aid = parked.json()["approval_id"]
    assert _detail(client, aid)["pass"] == {
        "state": "pending",
        "expires_at": None,
        "call_outcome": None,
    }
    _approve(aid)
    live = _detail(client, aid)["pass"]
    assert live["state"] == "live" and live["expires_at"] and live["call_outcome"] is None
    assert _save(client, "first").status_code == 200
    spent = _detail(client, aid)
    assert spent["status"] == "consumed"
    assert spent["pass"]["state"] == "consumed" and spent["pass"]["call_outcome"] == "completed"
    # A call that fails after it started still spends the pass, and says so.
    missing = {"content": "x", "key": "no-such-note"}
    failing = client.post("/api/action/memory/save", headers=_bearer(DEV), json=missing)
    assert failing.status_code == 202, failing.text
    fid = failing.json()["approval_id"]
    _approve(fid)
    again = client.post("/api/action/memory/save", headers=_bearer(DEV), json=missing)
    assert again.status_code == 404
    assert _detail(client, fid)["pass"]["call_outcome"] == "failed"
    # An approved pass past its lifetime reads as expired, not as live.
    stale = _row(status="approved")
    old = (datetime.now(UTC) - timedelta(hours=config.approval_pass_ttl_hours() + 1)).isoformat()
    conn = storage.connect()
    conn.execute("UPDATE approvals SET resolved_at=? WHERE id=?", (old, stale))
    conn.commit()
    conn.close()
    assert _detail(client, stale)["pass"]["state"] == "expired"
    # A consumed pass with no recorded outcome (no governed invocation) is unknown.
    assert _detail(client, _row(status="consumed"))["pass"]["call_outcome"] == "unknown"


# --- (d) work proposals ---------------------------------------------------------------


def _file_proposal() -> str:
    token = config.agent_identity_var.set(DEV)
    try:
        filed = work_proposals.work_proposal_file(
            "Inbox batch", [{"title": "one", "notes": "n"}, {"title": "two", "notes": "n"}]
        )
    finally:
        config.agent_identity_var.reset(token)
    return filed["proposal"]["id"]


def test_work_proposal_reads_through_coordination_manage(client):
    pid = _file_proposal()
    for who in (DEV, COS, OPERATOR):
        listed = client.get("/api/work-proposals", headers=_bearer(who))
        assert listed.status_code == 200, (who, listed.text)
        assert [p["id"] for p in listed.json()["proposals"]] == [pid]
    one = client.get(f"/api/work-proposals/{pid}", headers=_bearer(OPERATOR))
    assert one.status_code == 200 and len(one.json()["proposal"]["items"]) == 2
    query = "/api/work-proposals?status=approved&limit=5"
    assert client.get(query, headers=_bearer(DEV)).json()["proposals"] == []
    assert client.get("/api/work-proposals?status=x", headers=_bearer(DEV)).status_code == 400
    assert client.get("/api/work-proposals?nope=1", headers=_bearer(DEV)).status_code == 400
    missing = client.get("/api/work-proposals/wp_000000000000", headers=_bearer(DEV))
    assert missing.status_code == 404
    assert client.get("/api/work-proposals", headers=_bearer(PUBLIC)).status_code == 403
    assert client.get("/api/work-proposals").status_code == 401


# --- (e) declining a wake request ---------------------------------------------------


def _wake_request(agent: str = "Desktop_Agent5") -> str:
    sid, _ = registry.register_session(
        agent, "claude_code", external_session_id="ext-1", host="box"
    )
    wake.set_opt_in(sid, True)
    return sid


def test_a_declined_wake_request_stays_declined_until_the_operator_grants(client):
    sid = _wake_request()
    path = f"/api/sessions/{sid}/wake-decline"
    assert client.post(path, headers=_bearer(COS), json={}).status_code == 403
    declined = client.post(path, headers=_bearer(OPERATOR), json={"reason": "not tonight"})
    assert declined.status_code == 200, declined.text
    body = declined.json()
    assert body["declined"] is True and body["declined_by"] == OPERATOR
    assert body["decline_reason"] == "not tonight" and body["declined_at"]
    rows = [r for r in needs_operator.payload()["rows"] if r["kind"] == "wake_grant"]
    assert rows == []
    # The session asking again (the hook shares the agent's bearer) changes nothing.
    wake.set_opt_in(sid, False)
    wake.set_opt_in(sid, True)
    assert wake.wake_states([sid])[sid] == {"requested": True, "granted": False, "declined": True}
    assert [r for r in needs_operator.payload()["rows"] if r["kind"] == "wake_grant"] == []
    assert client.post(path, headers=_bearer(OPERATOR), json={"x": 1}).status_code == 400
    unknown = client.post("/api/sessions/nope/wake-decline", headers=_bearer(OPERATOR), json={})
    assert unknown.status_code == 404
    # A later grant is the operator changing their mind: it clears the decline.
    grant = client.post(
        f"/api/sessions/{sid}/wake-grant", headers=_bearer(OPERATOR), json={"grant": True}
    )
    assert grant.status_code == 200
    assert wake.wake_states([sid])[sid] == {"requested": True, "granted": True, "declined": False}


def test_declining_a_granted_session_revokes_it_and_the_wake_path_skips_it(monkeypatch):
    sid = _wake_request()
    wake.set_grant(sid, True, actor=OPERATOR)
    assert [r["session_id"] for r in _candidates()] == [sid]
    result = wake.set_decline(sid, actor=OPERATOR, reason=None)
    assert result["declined"] is True and result["granted"] is False
    assert _candidates() == []
    # Even a grant written behind the route's back is not honoured while declined.
    conn = storage.connect()
    conn.execute("UPDATE session_wake SET granted_at=? WHERE session_id=?", (storage.now(), sid))
    conn.commit()
    conn.close()
    assert _candidates() == []


def _candidates():
    conn = storage.connect()
    try:
        return wake._candidates(conn, "box")
    finally:
        conn.close()


# --- (f) release or keep a review hold ----------------------------------------------

PR = "https://github.com/example/project/pull/123"


def _held(notes: str) -> int:
    return tasks.task_add("held", notes=notes, evidence_ref=PR)["id"]


def _decide(client, task_id: int, decision: str, who: str = OPERATOR, **extra):
    body = {"task_id": task_id, "decision": decision, **extra}
    return client.post("/api/action/review-hold", headers=_bearer(who), json=body)


def test_a_red_tier_hold_cannot_be_released_only_kept(client):
    task_id = _held("[review: red-tier @ 2026-11-11] files requiring human approval: deploy/x")
    refused = _decide(client, task_id, "release")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "red_tier_approval_required"
    assert "/api/action/approval" in refused.json()["error"]
    conn = storage.connect()
    assert conn.execute("SELECT COUNT(*) FROM review_hold_decisions").fetchone()[0] == 0
    conn.close()
    assert [r for r in needs_operator.payload()["rows"] if r["kind"] == "red_tier_hold"]
    kept = _decide(client, task_id, "keep", reason="needs a look")
    assert kept.status_code == 200, kept.text
    assert kept.json()["decision"] == "keep" and kept.json()["kind"] == "red_tier_hold"
    assert (kept.json()["repo"], kept.json()["pr_number"]) == ("example/project", 123)
    assert [r for r in needs_operator.payload()["rows"] if r["kind"] == "red_tier_hold"] == []


def test_a_held_pr_is_released_or_kept_by_the_operator_alone(client):
    task_id = _held("PR open\n[waiting-for: human-merge]")
    assert _decide(client, task_id, "release", who=COS).status_code == 403
    assert _decide(client, task_id, "keep", who=DEV).status_code == 403
    assert _decide(client, task_id, "hold").status_code == 400
    assert _decide(client, task_id, "keep", extra=1).status_code == 400
    released = _decide(client, task_id, "release")
    assert released.status_code == 200 and released.json()["changed"] is True
    assert [r for r in needs_operator.payload()["rows"] if r["kind"] == "held_pr"] == []
    again = _decide(client, task_id, "release")
    assert again.json()["changed"] is False and again.json()["id"] == released.json()["id"]
    # A new hold marker is a new hold: it is listed again and needs a new decision.
    tasks.task_update(id=task_id, notes="[review: held-no-checkout @ later] branch on origin")
    assert [r["subject"] for r in needs_operator.payload()["rows"] if r["kind"] == "held_pr"] == [
        str(task_id)
    ]
    assert _decide(client, 999_999, "keep").status_code == 404
    plain = tasks.task_add("not held")["id"]
    assert _decide(client, plain, "keep").json()["code"] == "no_hold"


# --- (g) completed in a window ------------------------------------------------------


def test_tasks_lists_completed_after_a_given_time(client):
    before = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    done = tasks.task_add("finished today")["id"]
    token = config.agent_identity_var.set(DEV)
    try:
        tasks.task_update(id=done, status="done")
    finally:
        config.agent_identity_var.reset(token)
    tasks.task_add("still open")
    res = client.get("/api/tasks", params={"completed_after": before}, headers=_bearer(DEV))
    assert res.status_code == 200, res.text
    body = res.json()
    assert [t["id"] for t in body["completed"]] == [done]
    normalized = datetime.fromisoformat(before).isoformat(timespec="seconds")
    assert body["completed_after"] == normalized and body["completed_truncated"] is False
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert (
        client.get("/api/tasks", params={"completed_after": later}, headers=_bearer(DEV)).json()[
            "completed"
        ]
        == []
    )
    assert "completed" not in client.get("/api/tasks", headers=_bearer(DEV)).json()
    bad = client.get("/api/tasks?completed_after=yesterday", headers=_bearer(DEV))
    assert bad.status_code == 400


def test_review_hold_reads_the_marker_it_answers():
    assert review_holds.current_hold("no hold here") is None
    red = review_holds.current_hold("[waiting-for: human-merge]\n[review: red-tier @ t] x")
    assert red["kind"] == "red_tier_hold" and red["marker"].startswith("[review: red-tier")
    held = review_holds.current_hold("a\n[review: held-no-checkout @ t] b")
    assert held["kind"] == "held_pr"
    assert review_holds.pull_request(None, f"see {PR}") == ("example/project", 123)
    assert review_holds.pull_request("no url", "none") is None


def test_approvals_may_approve_is_the_resolve_rule():
    row = {"status": "pending", "requested_by": DEV}
    assert approvals.may_approve(row, OPERATOR)[0] == "yes"
    assert approvals.may_approve(row, None)[0] == "unknown"


# --- review round 1 (Codex at 9e020091) ---------------------------------------------


def test_a_secret_below_the_depth_limit_is_never_emitted():
    import json

    from unified_agent.telemetry import redaction

    deep: dict = {"password": "hunter2-deep", "note": "fine"}
    for _ in range(redaction.PREVIEW_MAX_DEPTH + 2):
        deep = {"nested": deep}
    preview, meta = redaction.redacted_preview({"top": deep})
    assert "hunter2-deep" not in json.dumps(preview)
    assert meta["complete"] is False
    assert [o["kind"] for o in meta["omissions"]] == ["depth"]


def test_preview_and_its_omission_metadata_share_one_size_budget():
    import json

    from unified_agent.telemetry import redaction

    # Long keys down to the depth limit, then 60 subtrees past it: every omission
    # path repeats the long ancestor keys (about 22 KB each before the fix).
    leaf: dict = {f"c{i}": {"x": {"y": 1}} for i in range(60)}
    for level in range(redaction.PREVIEW_MAX_DEPTH - 1):
        leaf = {f"{level}" + "k" * 2_000: leaf}
    assert len(json.dumps(leaf)) < 30_000
    preview, meta = redaction.redacted_preview(leaf)
    total = len(json.dumps({"payload": preview, **meta}).encode())
    assert total <= redaction.PREVIEW_MAX_BYTES
    assert all(len(o["path"]) <= redaction.PREVIEW_MAX_PATH for o in meta["omissions"])
    assert meta["omissions_total"] == 60 and len(meta["omissions"]) == 50
    # A payload larger than the budget is cut during traversal and says so.
    big = {f"s{i}": "v" * 5_000 for i in range(300)}
    preview, meta = redaction.redacted_preview(big)
    assert len(json.dumps({"payload": preview, **meta}).encode()) <= redaction.PREVIEW_MAX_BYTES
    assert meta["complete"] is False and meta["omissions"][-1]["kind"] == "bytes"


def test_keep_again_after_an_approval_superseded_it_is_recorded_fresh(client):
    task_id = _held("[waiting-for: human-merge]")
    first = _decide(client, task_id, "keep").json()
    # An approval of this PR resolved after the keep supersedes it.
    conn = storage.connect()
    conn.execute(
        "UPDATE review_hold_decisions SET decided_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (first["id"],),
    )
    conn.execute(
        "INSERT INTO approvals (tool, payload, requested_by, status, created_at, updated_at, "
        "resolved_at) VALUES (?, ?, ?, 'approved', ?, ?, ?)",
        (
            github_app.TOOL_NAME,
            '{"repo": "example/project", "number": 123}',
            DEV,
            storage.now(),
            storage.now(),
            "2001-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    again = _decide(client, task_id, "keep").json()
    assert again["changed"] is True and again["id"] != first["id"]
    assert review_holds.LEDGER_TOOL == github_app.TOOL_NAME
