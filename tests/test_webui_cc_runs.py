"""Control Center v2 runs routes: routines and LangGraph runs (tasks #2466, #2480).

The real scoped app runs behind the hub auth middleware with the fixture
identities of ``test_route_scopes``: Laptop_Agent5 is chief-of-staff (the only
profile with the routine tools), Desktop_Agent2 is a developer, server_agent8 is
public, and Desktop_Operator is the verified human operator.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_route_scopes import OPERATOR, TOKENS, _bearer, _client
from test_route_scopes import _isolated as _isolated
from test_webui_cc_work import _approve

from unified_agent import profiles, server, storage
from unified_agent.tools import tasks
from unified_agent.webui import routes

COS = "Laptop_Agent5"
DEV = "Desktop_Agent2"

#: Every write route with a valid body (``{routine}`` is filled per test).
WRITES = {
    "/api/action/routine/add": {"title": "t", "trigger": "api"},
    "/api/action/routine/pause": {"routine_id": "{routine}"},
    "/api/action/routine/resume": {"routine_id": "{routine}"},
    "/api/action/routine/run": {"routine_id": "{routine}"},
    "/api/action/langgraph/run": {"task_id": 1, "query": "q"},
}


@pytest.fixture(autouse=True)
def _pin_cos(monkeypatch):
    """Browser stand-in. cloud_agent13 is the only real chief of staff and cannot open /api."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )


@pytest.fixture
def client() -> TestClient:
    return _client(routes.web_routes())


def _fill(body: dict, routine_id: int) -> dict:
    return {k: routine_id if v == "{routine}" else v for k, v in body.items()}


def _post(client, path, who, body, key="key-1"):
    headers = {**_bearer(who), "Idempotency-Key": key}
    return client.post(path, headers=headers, json=body)


def _add(client, **body) -> int:
    key = "add-" + "-".join(f"{k}={v}" for k, v in sorted(body.items()))
    res = _post(client, "/api/action/routine/add", COS, {"title": "Sweep", **body}, key=key)
    assert res.status_code == 200, res.text
    return res.json()["id"]


# --- routines -------------------------------------------------------------------------


def test_routine_list_add_pause_resume(client):
    rid = _add(client, schedule="0 3 * * *")
    listed = client.get("/api/routines", headers=_bearer(COS))
    assert listed.status_code == 200, listed.text
    [routine] = listed.json()["routines"]
    assert routine["id"] == rid and routine["next_run_at"].endswith("03:00:00+00:00")
    assert routine["recent_runs"] == [] and listed.json()["routines_enabled"] is False
    paused = _post(client, "/api/action/routine/pause", COS, {"routine_id": rid})
    assert paused.json() == {"id": rid, "paused": 1}
    again = client.get("/api/routines?recent_runs=2", headers=_bearer(OPERATOR)).json()
    assert again["routines"][0]["next_run_at"] is None
    resumed = _post(client, "/api/action/routine/resume", OPERATOR, {"routine_id": rid})
    assert resumed.json() == {"id": rid, "paused": 0}
    assert _post(client, "/api/action/routine/pause", COS, {"routine_id": 999}).status_code == 404
    assert client.get("/api/routines?recent_runs=99", headers=_bearer(COS)).status_code == 400


def test_run_now_fires_a_scheduled_routine_for_the_operator_only(client, monkeypatch):
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, schedule="0 3 * * *", concurrency="skip")
    body = {"routine_id": rid}
    path = "/api/action/routine/run"
    agent = _post(client, path, COS, body)
    assert agent.status_code == 409 and "scheduler" in agent.json()["error"]
    first = _post(client, path, OPERATOR, body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "created" and first.json()["task_id"]
    assert _post(client, path, OPERATOR, body).json() == first.json()  # same key, same receipt
    active = _post(client, path, OPERATOR, body, key="key-2")
    assert active.status_code == 200 and active.json() == {"status": "skipped", "task_id": None}
    runs = client.get("/api/routines", headers=_bearer(OPERATOR)).json()["routines"][0]
    assert [r["status"] for r in runs["recent_runs"]] == ["skipped", "created"]
    assert client.post(path, headers=_bearer(OPERATOR), json=body).status_code == 400  # no key
    assert _post(client, path, OPERATOR, {"routine_id": 999}, key="key-x").status_code == 404
    _post(client, "/api/action/routine/pause", OPERATOR, body)
    assert _post(client, path, OPERATOR, body, key="key-3").status_code == 409
    monkeypatch.delenv("UA_ROUTINES_ENABLED")
    assert _post(client, path, OPERATOR, body, key="key-4").status_code == 503


def test_run_now_parks_on_the_approval_gate_and_replays_with_the_same_key(client, monkeypatch):
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "routine_fire")
    parked = _post(client, "/api/action/routine/run", COS, {"routine_id": rid})
    assert parked.status_code == 202, parked.text
    assert parked.json()["code"] == "approval_required"
    assert parked.json()["tool"] == "routine_fire"
    _approve(parked.json()["approval_id"])
    ran = _post(client, "/api/action/routine/run", COS, {"routine_id": rid})
    assert ran.status_code == 200 and ran.json()["status"] == "created"


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*WRITES, "/api/routines"])
def test_unauthenticated_developer_and_public_are_refused(client, path):
    method = "GET" if path == "/api/routines" else "POST"
    body = _fill(WRITES.get(path, {}), 1)
    assert client.request(method, path, json=body).status_code == 401
    for who in (
        ("server_agent8", DEV) if path != "/api/action/langgraph/run" else ("server_agent8",)
    ):
        denied = client.request(method, path, headers=_bearer(who), json=body)
        assert denied.status_code == 403, (who, denied.text)
        assert "unavailable to this profile" in denied.json()["error"]


@pytest.mark.parametrize("path", list(WRITES))
def test_cookie_session_write_needs_the_csrf_token(path):
    session = _client([r for r in routes.web_routes() if r.path == "/api/session"])
    login = session.post("/api/session", json={"token": TOKENS[COS]})
    assert login.status_code == 200
    browser = TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=routes.web_routes())),
        base_url="http://127.0.0.1",
        cookies=dict(session.cookies),
    )
    res = browser.post(path, headers={"Idempotency-Key": "k"}, json=_fill(WRITES[path], 1))
    assert res.status_code == 403
    assert "CSRF" in res.json()["error"]


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/action/routine/add", {"title": "t", "trigger": "manual"}),
        ("/api/action/routine/add", {"title": "t", "agent": COS}),
        ("/api/action/routine/pause", {"routine_id": "1"}),
        ("/api/action/routine/run", {"routine_id": 1, "agent": COS}),
        ("/api/action/routine/run", {}),
        ("/api/action/langgraph/run", {"task_id": 1}),
        ("/api/action/langgraph/run", {"task_id": 1, "query": "q", "engine": "research"}),
        ("/api/action/langgraph/run", {"task_id": 1, "query": "q", "agent": DEV}),
    ],
)
def test_bad_input_and_named_identities_are_400(client, path, body):
    res = _post(client, path, COS, body)
    assert res.status_code == 400, res.text


# --- LangGraph ------------------------------------------------------------------------


def _invocation_sessions() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT agent, status, ended_at, task_id FROM agent_sessions WHERE kind=?",
            ("control_center",),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_langgraph_workflow_runs_from_the_browser_and_continues_by_run_id(client, monkeypatch):
    """The real ``langgraph_workflow_run`` entry point, through its guarded wrapper.

    Each native approval answers 202 ``approval_required``; the run continues with the
    returned ``run_id``. Each request ran under its own registered, ended session.
    """
    pytest.importorskip("langgraph")
    from unified_agent.langgraph_dispatch import UnifiedAgentDispatchAdapter

    monkeypatch.setattr(UnifiedAgentDispatchAdapter, "record_task_outcome", lambda *a, **k: None)
    task = tasks.task_claim(tasks.task_add(title="Workflow from the browser")["id"], DEV)
    body = {"task_id": task["id"], "query": "plan then merge"}
    path = "/api/action/langgraph/run"
    first = _post(client, path, DEV, body)
    assert first.status_code == 202, first.text
    parked = first.json()
    assert parked["code"] == "approval_required" and parked["pending_hop"] == "build"
    assert parked["approval_id"] == parked["interrupted_by"]["approval_id"]
    # Task #2516: the canvas reads where the run is from its checkpoints.
    progress_path = f"/api/langgraph/runs/{parked['run_id']}"
    progress = client.get(progress_path, headers=_bearer(DEV)).json()
    assert progress["status"] == "interrupted" and progress["pending_hop"] == "build"
    assert progress["approval_id"] == parked["approval_id"] and progress["task_id"] == task["id"]
    assert [(h["hop"], h["status"], h["waiting_approval"]) for h in progress["hops"][:2]] == [
        ("plan", "done", False),
        ("build", "doing", True),
    ]
    assert progress["checkpoint"]["count"] >= 1 and progress["checkpoint"]["id"]
    _approve(parked["approval_id"])
    second = _post(client, path, DEV, {**body, "run_id": parked["run_id"]}, key="key-2")
    assert second.status_code == 202 and second.json()["pending_hop"] == "merge"
    _approve(second.json()["approval_id"])
    done = _post(client, path, DEV, {**body, "run_id": parked["run_id"]}, key="key-3")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert done.json()["output"]["claimed_by"] == DEV
    finished = client.get(progress_path, headers=_bearer(OPERATOR)).json()
    assert finished["status"] == "completed" and finished["pending_hop"] is None
    assert {h["status"] for h in finished["hops"]} == {"done"}
    missing = client.get("/api/langgraph/runs/" + "0" * 26, headers=_bearer(DEV))
    assert missing.status_code == 404
    assert client.get("/api/langgraph/runs/bad", headers=_bearer(DEV)).status_code == 400
    sessions = _invocation_sessions()
    assert len(sessions) == 3
    assert all(s["agent"] == DEV and s["ended_at"] and s["task_id"] == task["id"] for s in sessions)
    # Another identity cannot drive the run: the tool's own owner check refuses it.
    other = _post(client, path, COS, {**body, "run_id": parked["run_id"]}, key="key-4")
    assert other.status_code == 400
    assert all(s["ended_at"] for s in _invocation_sessions())


def test_langgraph_run_keeps_the_wrapper_approval_gate(client, monkeypatch):
    pytest.importorskip("langgraph")
    task = tasks.task_claim(tasks.task_add(title="gated run")["id"], DEV)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "langgraph_workflow_run")
    res = _post(client, "/api/action/langgraph/run", DEV, {"task_id": task["id"], "query": "q"})
    assert res.status_code == 202, res.text
    assert res.json()["code"] == "approval_required"
    assert res.json()["tool"] == "langgraph_workflow_run"
    assert all(s["ended_at"] for s in _invocation_sessions())


def test_invocation_session_closes_when_the_request_is_cancelled(monkeypatch):
    """Review #1516: a cancelled request must still end its control_center session,
    and a close that fails is recorded, not ignored."""
    import anyio

    from unified_agent import config, events
    from unified_agent.webui import cc_runs

    task = tasks.task_add(title="cancelled run")
    started = anyio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await anyio.sleep(60)

    monkeypatch.setattr(cc_runs, "call", hang)

    async def cancel_mid_call():
        token = config.agent_identity_var.set(DEV)
        try:
            with profiles.http_route_authority(DEV, ("langgraph_workflow_run",), ()):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        cc_runs.run_in_invocation_session,
                        DEV,
                        {"task_id": task["id"], "query": "q"},
                    )
                    await started.wait()
                    tg.cancel_scope.cancel()
        finally:
            config.agent_identity_var.reset(token)

    anyio.run(cancel_mid_call)
    [session] = _invocation_sessions()
    assert session["ended_at"] and session["status"] == "failed"

    monkeypatch.setattr(cc_runs.registry, "end_session", lambda *a, **k: False)
    assert (
        anyio.run(
            lambda: cc_runs._close_invocation_session("s-1", DEV, status="failed", summary="x")
        )
        is False
    )
    [event] = events.query_events(channels=["control_center.invocation_session_unclosed"])
    assert event["payload"]["session_id"] == "s-1"


def test_run_now_replays_its_first_answer_and_binds_the_key_to_the_body(client, monkeypatch):
    """Review #1516: a fire that first answered queued keeps answering queued after the
    queue releases it, and the same key with another routine is a 409."""
    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api", concurrency="queue")
    other = _add(client, trigger="api")
    path = "/api/action/routine/run"
    first = _post(client, path, COS, {"routine_id": rid}, key="k-1").json()
    queued = _post(client, path, COS, {"routine_id": rid}, key="k-2").json()
    assert first["status"] == "created" and queued["status"] == "queued"
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first["task_id"],))
        conn.commit()
    finally:
        conn.close()
    routines.routine_tick(force=True)  # releases the queued run: its row becomes created
    assert _post(client, path, COS, {"routine_id": rid}, key="k-2").json() == queued
    clash = _post(client, path, COS, {"routine_id": other}, key="k-2")
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    runs = client.get("/api/routines", headers=_bearer(COS)).json()["routines"]
    assert [r["recent_runs"] for r in runs if r["id"] == other] == [[]]


def test_a_slow_session_close_does_not_hold_the_response(monkeypatch):
    """#2503: the close waited for its worker thread past the deadline. The response
    now returns at the bound and the close still finishes, recorded once."""
    import time

    import anyio

    from unified_agent import events, registry
    from unified_agent.webui import cc_runs

    session = registry.start_session(DEV, "control_center")
    real_end = registry.end_session

    def slow_end(*args, **kwargs):
        time.sleep(0.3)
        return real_end(*args, **kwargs)

    monkeypatch.setattr(cc_runs, "SESSION_CLOSE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(cc_runs.registry, "end_session", slow_end)
    started = time.monotonic()
    outcome = anyio.run(
        lambda: cc_runs._close_invocation_session(session, DEV, status="completed", summary="x")
    )
    assert time.monotonic() - started < 0.2
    assert outcome is None  # still closing in the background
    deadline = time.monotonic() + 5
    while registry.get_session(session)["ended_at"] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert registry.get_session(session)["ended_at"]
    assert events.query_events(channels=["control_center.invocation_session_unclosed"]) == []


def test_run_now_receipts_are_tenant_scoped(client, monkeypatch):
    """Review round 2: a receipt from another tenant must not replay, and a receipt
    written before tenant scoping is refused rather than replayed or repeated."""
    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    rid = _add(client, trigger="api")
    path = "/api/action/routine/run"
    assert _post(client, path, COS, {"routine_id": rid}, key="k-t").status_code == 200
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    foreign = _post(client, path, COS, {"routine_id": rid}, key="k-t")
    assert foreign.status_code == 404, foreign.text  # org_b cannot see org_a's routine
    # A pre-scoping receipt under the raw key is ambiguous under a tenant.
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?)",
            (routines._run_now_row_key(COS, "k-legacy"), "cc_routine_run", "{}", storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    legacy = _post(client, path, COS, {"routine_id": rid}, key="k-legacy")
    assert legacy.status_code == 409 and legacy.json()["code"] == "ambiguous_receipt"


def test_two_identities_with_one_client_key_get_their_own_runs(client, monkeypatch):
    """Review round 2: the key passed to routine_fire is derived per identity."""
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api", concurrency="queue")
    path = "/api/action/routine/run"
    mine = _post(client, path, COS, {"routine_id": rid}, key="shared").json()
    theirs = _post(client, path, OPERATOR, {"routine_id": rid}, key="shared").json()
    assert mine["status"] == "created" and theirs["status"] == "queued"
    assert mine["task_id"] != theirs["task_id"]


def test_run_now_in_flight_duplicates_and_receipt_retention(client, monkeypatch):
    """Review round 2: a duplicate while the first request is in flight answers 409
    in_flight; only the owner settles the reservation; every later duplicate gets the
    stored first answer; receipts past the retention window are pruned."""
    from datetime import UTC, datetime

    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api")
    path = "/api/action/routine/run"
    digest = routines.run_now_digest({"routine_id": rid})
    owner, stored = routines.run_now_begin(COS, "k-f", digest)
    assert stored is None
    busy = _post(client, path, COS, {"routine_id": rid}, key="k-f")
    assert busy.status_code == 409 and busy.json()["code"] == "in_flight"
    # Another owner cannot drop or settle the reservation.
    assert routines.run_now_finish(COS, "k-f", digest, "not-the-owner", None) == (False, None)
    with pytest.raises(routines.RunNowInFlight):
        routines.run_now_begin(COS, "k-f", digest)
    canonical = {"status_code": 200, "body": {"status": "queued", "task_id": 7}}
    assert routines.run_now_finish(COS, "k-f", digest, owner, canonical) == (True, canonical)
    replay = _post(client, path, COS, {"routine_id": rid}, key="k-f")
    assert replay.status_code == 200 and replay.json() == canonical["body"]
    old = (datetime.now(UTC) - routines.RUN_NOW_RECEIPT_TTL * 2).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE idempotency_keys SET created_at=? WHERE tool='cc_routine_run'", (old,))
        conn.commit()
    finally:
        conn.close()
    _post(client, path, COS, {"routine_id": rid}, key="k-new")
    conn = storage.connect()
    try:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM idempotency_keys WHERE tool='cc_routine_run' "
            "AND created_at=?",
            (old,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert left == 0


def test_a_superseded_run_now_owner_never_returns_its_own_answer(client, monkeypatch):
    """Review round 3: A fires and stalls; after the takeover window B owns the
    reservation. A must not return its local 200: it answers 409 in_flight until B
    settles, and then everyone reads B's stored answer."""
    from datetime import UTC, datetime

    from unified_agent import routines
    from unified_agent.webui import cc_runs, routes

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api")
    digest = routines.run_now_digest({"routine_id": rid})
    taken: dict[str, str] = {}

    async def stalled_fire(*_args, **_kwargs):
        # While A is "stalled", its reservation ages past the window and B takes over.
        old = (datetime.now(UTC) - routines.RUN_NOW_PENDING_STALE * 2).isoformat()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE idempotency_keys SET created_at=? WHERE tool='cc_routine_run'", (old,)
            )
            conn.commit()
        finally:
            conn.close()
        taken["owner"], _ = routines.run_now_begin(COS, "k-s", digest)
        return routes._json({"status": "queued", "task_id": 41})

    monkeypatch.setattr(cc_runs, "call", stalled_fire)
    a = _post(client, "/api/action/routine/run", COS, {"routine_id": rid}, key="k-s")
    assert a.status_code == 409 and a.json()["code"] == "in_flight", a.text
    b_answer = {"status_code": 200, "body": {"status": "created", "task_id": 41}}
    assert routines.run_now_finish(COS, "k-s", digest, taken["owner"], b_answer) == (
        True,
        b_answer,
    )
    again = _post(client, "/api/action/routine/run", COS, {"routine_id": rid}, key="k-s")
    assert again.status_code == 200 and again.json() == b_answer["body"]


def test_a_run_now_taken_over_after_an_unsettled_fire_does_not_fire_twice(client, monkeypatch):
    """Review of #1532 (shared receipt helper): a takeover re-calls routine_fire with
    the same derived key, and the routine's own receipt answers; no second task."""
    from datetime import UTC, datetime

    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api")
    settle = storage.request_receipt_finish

    def crash(*_args, **_kwargs):
        raise RuntimeError("hub died before the receipt settled")

    monkeypatch.setattr(storage, "request_receipt_finish", crash)
    with pytest.raises(RuntimeError):
        _post(client, "/api/action/routine/run", COS, {"routine_id": rid}, key="k-t")
    monkeypatch.setattr(storage, "request_receipt_finish", settle)
    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - routines.RUN_NOW_PENDING_STALE * 2).isoformat()
        conn.execute("UPDATE idempotency_keys SET created_at=? WHERE tool='cc_routine_run'", (old,))
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    retry = _post(client, "/api/action/routine/run", COS, {"routine_id": rid}, key="k-t")
    assert retry.status_code == 200, retry.text
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    finally:
        conn.close()


def test_a_bearer_fire_with_a_look_alike_key_does_not_capture_a_run_now(client, monkeypatch):
    """Review round 3: Run now fires live in a server-owned receipt namespace that an
    ordinary routine_fire key (bearer webhook or MCP) cannot select."""
    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api", concurrency="queue")
    look_alike = routines.run_now_fire_key(COS, "k-ns")
    bearer = client.post(
        f"/api/routines/{rid}/fire",
        headers={**_bearer(COS), "Idempotency-Key": look_alike},
    )
    assert bearer.status_code == 200, bearer.text
    mine = _post(client, "/api/action/routine/run", COS, {"routine_id": rid}, key="k-ns")
    assert mine.status_code == 200, mine.text
    assert mine.json()["task_id"] != bearer.json()["task_id"]
    assert mine.json()["status"] == "queued"  # its own fire, behind the bearer's task


def test_an_expired_run_now_receipt_keeps_the_key_bound_to_its_first_fire(client, monkeypatch):
    """Review round 3: pruning expires only the frozen answer. The same client key for
    the same routine still names its first fire (the routine's own receipt answers),
    so a new run needs a fresh key. A reservation inside its takeover window is never
    pruned, because the retention window is longer than the takeover window."""
    from datetime import UTC, datetime, timedelta

    from unified_agent import routines

    assert routines.RUN_NOW_RECEIPT_TTL > routines.RUN_NOW_PENDING_STALE
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    rid = _add(client, trigger="api")
    path = "/api/action/routine/run"
    first = _post(client, path, COS, {"routine_id": rid}, key="k-old").json()
    pending_owner, _ = routines.run_now_begin(COS, "k-pending", "digest-p")
    assert pending_owner
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        expired = (now - routines.RUN_NOW_RECEIPT_TTL - timedelta(hours=1)).isoformat()
        in_window = (now - routines.RUN_NOW_PENDING_STALE + timedelta(minutes=1)).isoformat()
        conn.execute(
            "UPDATE idempotency_keys SET created_at=? WHERE tool='cc_routine_run' "
            "AND result_json LIKE '%response%'",
            (expired,),
        )
        conn.execute(
            "UPDATE idempotency_keys SET created_at=? WHERE tool='cc_routine_run' "
            "AND result_json NOT LIKE '%response%'",
            (in_window,),
        )
        conn.commit()
    finally:
        conn.close()
    _post(client, path, COS, {"routine_id": rid}, key="k-trigger-prune")
    with pytest.raises(routines.RunNowInFlight):  # the young reservation survived
        routines.run_now_begin(COS, "k-pending", "digest-p")
    again = _post(client, path, COS, {"routine_id": rid}, key="k-old")
    assert again.status_code == 200
    assert again.json()["task_id"] == first["task_id"]  # no second fire
    # A fresh key is a new fire: the first task is still open, so concurrency=skip
    # records a skipped receipt for it.
    fresh = _post(client, path, COS, {"routine_id": rid}, key="k-fresh")
    assert fresh.json() == {"status": "skipped", "task_id": None}


# --- Idempotency-Key on starts and creates (task #2516) -------------------------------


def _receipts(name: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT key, result_json FROM idempotency_keys WHERE tool=?", (f"cc_{name}",)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


#: The starts that spawn work: route, a valid body, another body and the receipt's name.
STARTS = {
    "workflow": (
        "/api/action/workflow",
        {"name": "wf", "inputs": {}},
        {"name": "wf", "inputs": {"a": 1}},
        "workflow_run",
    ),
    "langgraph": (
        "/api/action/langgraph/run",
        {"task_id": 1, "query": "q"},
        {"task_id": 1, "query": "other"},
        "langgraph_workflow_run",
    ),
    "cycle": (
        "/api/action/cycle",
        {"operation": "start", "task_id": 1},
        {"operation": "start", "task_id": 2},
        "autonomous_cycle_start",
    ),
}


def _fake_start(monkeypatch, kind: str, calls: list, *, fail: bool = False) -> None:
    """Replace the seam each start spawns its work through; record the receipt it saw."""
    from unified_agent.webui import api, cc_runs

    async def start(*_args, **_kwargs):
        calls.append(_receipts(STARTS[kind][3]))
        if fail:
            raise RuntimeError("the hub died mid-start")
        n = len(calls)
        if kind == "workflow":
            return {"workflow": "wf", "status": "done", "run": n}
        if kind == "cycle":
            return {"cycle": {"id": f"cycle-{n}"}, "created": True}
        return "s-1", routes._json({"status": "completed", "run_id": f"R{n}"})

    module, attr = {
        "workflow": (api, "run_workflow"),
        "cycle": (api, "cycle_start"),
        "langgraph": (cc_runs, "run_in_invocation_session"),
    }[kind]
    monkeypatch.setattr(module, attr, start)


@pytest.mark.parametrize("kind", list(STARTS))
def test_a_keyed_start_runs_once_per_caller_and_records_its_receipt_first(
    client, monkeypatch, kind
):
    path, body, other, _ = STARTS[kind]
    calls: list = []
    _fake_start(monkeypatch, kind, calls)
    first = _post(client, path, COS, body, key="start-1")
    assert 200 <= first.status_code < 300, first.text
    # The reservation was committed before the work started.
    assert len(calls[0]) == 1 and '"owner"' in calls[0][0]["result_json"]
    again = _post(client, path, COS, body, key="start-1")
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert len(calls) == 1
    clash = _post(client, path, COS, other, key="start-1")
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    assert len(calls) == 1
    theirs = _post(client, path, OPERATOR, body, key="start-1")  # keys are per caller
    assert 200 <= theirs.status_code < 300 and len(calls) == 2
    assert theirs.json() != first.json()
    unkeyed = client.post(path, headers=_bearer(COS), json=body)  # no key: as before
    assert 200 <= unkeyed.status_code < 300 and len(calls) == 3


@pytest.mark.parametrize("kind", list(STARTS))
def test_a_start_whose_outcome_is_unknown_is_never_run_again(client, monkeypatch, kind):
    path, body, _, _ = STARTS[kind]
    calls: list = []
    _fake_start(monkeypatch, kind, calls, fail=True)
    lost = TestClient(client.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert _post(lost, path, COS, body, key="lost").status_code >= 400
    _fake_start(monkeypatch, kind, calls)
    again = _post(client, path, COS, body, key="lost")
    assert again.status_code == 409 and again.json()["code"] == "outcome_unknown", again.text
    assert len(calls) == 1
    assert _post(client, path, COS, body, key="fresh").status_code < 300  # a new key runs


def test_a_keyed_graph_run_freezes_a_native_interrupt_but_not_a_wrapper_gate(client, monkeypatch):
    """A native approval started the run (it has a run_id), so it is the frozen answer;
    the wrapper gate parked before anything ran, so its repeat runs after approval."""
    from unified_agent.webui import cc_runs

    answers = [
        routes._json({"status": "approval_pending", "approval_id": 9, "tool": "t"}, status=202),
        routes._json(
            {"status": "interrupted", "run_id": "R1", "interrupted_by": {"approval_id": 3}}
        ),
    ]
    calls: list = []

    async def start(*_args, **_kwargs):
        calls.append(1)
        return "s-1", answers.pop(0) if answers else routes._json({"status": "completed"})

    monkeypatch.setattr(cc_runs, "run_in_invocation_session", start)
    path, body, _, _ = STARTS["langgraph"]
    gated = _post(client, path, COS, body, key="g")
    assert gated.status_code == 202 and "run_id" not in gated.json()
    native = _post(client, path, COS, body, key="g")
    assert native.status_code == 202 and native.json()["run_id"] == "R1"
    assert _post(client, path, COS, body, key="g").json() == native.json()
    assert len(calls) == 2


def test_routine_create_with_a_key_replays_and_binds_the_key_to_the_body(client):
    path = "/api/action/routine/add"
    first = _post(client, path, COS, {"title": "Nightly", "trigger": "api"}, key="add-k")
    assert first.status_code == 200, first.text
    again = _post(client, path, COS, {"title": "Nightly", "trigger": "api"}, key="add-k")
    assert again.json() == first.json()
    clash = _post(client, path, COS, {"title": "Weekly", "trigger": "api"}, key="add-k")
    assert clash.status_code == 409 and clash.json()["code"] == "conflict"
    listed = client.get("/api/routines", headers=_bearer(COS)).json()["routines"]
    assert [r["title"] for r in listed] == ["Nightly"]
    assert len(_receipts("routine_add")) == 1


def test_graph_run_progress_is_readable_by_its_caller_and_the_operator_only(client, monkeypatch):
    """Review #1538: tenant visibility is not ownership. Another agent and an
    unrelated cloud identity get 404 for a developer's run, exactly as for no run."""
    from unified_agent import langgraph_workflow

    task = tasks.task_claim(tasks.task_add(title="owned run")["id"], DEV)
    run_id = "01J" + "0" * 23
    progress = {"run_id": run_id, "task_id": task["id"], "claimed_by": DEV, "status": "running"}
    monkeypatch.setattr(
        langgraph_workflow, "run_progress", lambda rid: progress if rid == run_id else None
    )
    path = f"/api/langgraph/runs/{run_id}"
    assert client.get(path, headers=_bearer(DEV)).json()["claimed_by"] == DEV
    assert client.get(path, headers=_bearer(OPERATOR)).status_code == 200
    for stranger in (COS, "server_agent8"):
        refused = client.get(path, headers=_bearer(stranger))
        assert refused.status_code == 404, (stranger, refused.text)
        assert refused.json() == {"error": "no governed graph run with this id"}


def _research_run(monkeypatch, title: str) -> tuple[dict, list]:
    """A research run paused at its native approval, started over MCP as DEV."""
    from unified_agent import config, langgraph_dispatch
    from unified_agent.tools.langgraph import langgraph_run as research_run

    calls: list[str] = []

    def web_search(query: str, max_results: int = 5) -> dict:
        calls.append(query)
        return {"items": [{"title": "Result", "url": "https://example.test/r"}]}

    real = langgraph_dispatch._default_tool_registry
    monkeypatch.setattr(
        langgraph_dispatch, "_default_tool_registry", lambda: {**real(), "web_search": web_search}
    )
    task = tasks.task_claim(tasks.task_add(title=title)["id"], DEV)
    identity = config.agent_identity_var.set(DEV)
    session = config.mcp_session_id_var.set("cc-runs-research")
    try:
        first = research_run(task["id"], query="durable graph checkpoints")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
    assert first["status"] == "interrupted", first
    return first, calls


def test_langgraph_research_run_resumes_from_the_browser_by_run_id(client, monkeypatch):
    """Task #2522: the research engine continues a paused run by run_id, owner only."""
    pytest.importorskip("langgraph")
    first, calls = _research_run(monkeypatch, "Research from the browser")
    path = "/api/action/langgraph/run"
    body = {"engine": "research", "run_id": first["run_id"]}
    parked = _post(client, path, DEV, body)  # not decided yet: still waiting
    assert parked.status_code == 202, parked.text
    assert parked.json()["code"] == "approval_required" and parked.json()["tool"] == "langgraph_run"
    other = _post(client, path, COS, body)
    assert other.status_code == 403 and other.json()["outcome"] == "run_denied"
    _approve(int(parked.json()["approval_id"]))
    done = _post(client, path, DEV, body)
    assert done.status_code == 200, done.text
    assert done.json()["outcome"] == "run_completed" and done.json()["output"]
    assert done.json()["trace_id"] == first["trace_id"] and len(calls) == 1
    repeat = _post(client, path, DEV, body)
    assert repeat.status_code == 200 and repeat.json()["duplicate"] is True and len(calls) == 1
    unknown = {"engine": "research", "run_id": "01JQ9Z6Y5K7M2N4P6R8T0V2X4Z"}
    missing = _post(client, path, DEV, unknown)
    assert missing.status_code == 409 and missing.json()["code"] == "checkpoint_missing"
    assert all(s["ended_at"] for s in _invocation_sessions())


def test_the_operator_continues_another_identitys_paused_research_run(client, monkeypatch):
    pytest.importorskip("langgraph")
    first, calls = _research_run(monkeypatch, "Operator continues research")
    _approve(int(first["interrupted_by"]["approval_id"]))
    body = {"engine": "research", "run_id": first["run_id"]}
    done = _post(client, "/api/action/langgraph/run", OPERATOR, body)
    assert done.status_code == 200, done.text
    assert done.json()["outcome"] == "run_completed" and len(calls) == 1
    assert done.json()["budget_revision"] == first["budget_revision"]


def test_an_operator_resume_runs_under_the_owners_authority(client, monkeypatch):
    """Item 1 of the #1540 review: the operator triggers the owner's run, nothing more."""
    pytest.importorskip("langgraph")

    first, calls = _research_run(monkeypatch, "Owner downgraded mid-pause")
    _approve(int(first["interrupted_by"]["approval_id"]))
    real = profiles.default_for_identity
    monkeypatch.setattr(
        profiles, "default_for_identity", lambda who: "public" if who == DEV else real(who)
    )
    body = {"engine": "research", "run_id": first["run_id"]}
    owner = _post(client, "/api/action/langgraph/run", DEV, body)
    assert owner.status_code == 403, owner.text
    operator = _post(client, "/api/action/langgraph/run", OPERATOR, body)
    assert operator.status_code == 403, operator.text
    assert operator.json()["outcome"] == "run_denied" and calls == []
