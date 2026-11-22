"""Agent lifecycle controls: pause / resume / terminate (task #1351).

State lives on the event ledger; a stopped agent cannot claim or renew; stops
are requested on every execution path with an honest per-path result.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import agent_lifecycle, config, node_queue, run_records, server, storage
from unified_agent.dispatch import coordinator
from unified_agent.tools.graph import coordination_manage
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.webui.routes import web_routes

OP = "Desktop_Person"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _wip(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)


def _new_task() -> int:
    return task_add("t", lane="dispatch-ready")["id"]


def _claim(agent: str, tid: int | None = None) -> dict:
    tid = tid if tid is not None else _new_task()
    with _as(agent):
        return task_claim(tid, agent, expected_lane="dispatch-ready")


def _events(kind: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=?",
            (agent_lifecycle.CHANNEL, kind),
        ).fetchall()
    finally:
        conn.close()
    return [agent_lifecycle._payload(r) for r in rows]


def test_default_state_is_active():
    assert agent_lifecycle.agent_state("Desktop_Agent2")["state"] == "active"
    assert _claim("Desktop_Agent2")["status"] == "doing"


def _lifecycle_org(monkeypatch, org):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    if org is None:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    else:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)


def _org_lifecycle_event(monkeypatch, org, label, state, owner=None):
    from unified_agent import events

    _lifecycle_org(monkeypatch, org)
    events.record_event(
        agent_lifecycle.CHANNEL,
        {
            "kind": agent_lifecycle.KIND_CHANGED,
            "agent": label,
            "state": state,
            "actor": OP,
            "reason": str(org),
            "owner": owner,
        },
        originator=OP,
        notify=False,
    )


@pytest.mark.parametrize("org", [None, "org-A"])
@pytest.mark.parametrize("state,foreign_state", [("retired", "active"), ("active", "retired")])
def test_lifecycle_state_ignores_newer_foreign_case_variant(monkeypatch, org, state, foreign_state):
    label = "Desktop_Agent2"
    _org_lifecycle_event(monkeypatch, org, label.lower(), state)
    _org_lifecycle_event(monkeypatch, "org-B", label.upper(), foreign_state)
    _lifecycle_org(monkeypatch, org)

    assert agent_lifecycle.agent_state(label)["state"] == state
    conn = storage.connect()
    try:
        assert bool(agent_lifecycle.claim_block(conn, label)) == (state != "active")
        assert bool(agent_lifecycle.retired_state(label, conn=conn)) == (state == "retired")
    finally:
        conn.close()


@pytest.mark.parametrize("org", [None, "org-A"])
def test_lifecycle_roster_discloses_only_local_changes_and_owners(monkeypatch, org):
    label = "Desktop_Agent2"
    _org_lifecycle_event(monkeypatch, org, label, "active", "local-owner")
    _org_lifecycle_event(monkeypatch, "org-B", label, "active", "foreign-owner")
    _org_lifecycle_event(monkeypatch, "org-B", "Foreign_Only", "retired", "foreign-owner")
    _lifecycle_org(monkeypatch, org)

    assert agent_lifecycle.agent_state(label)["owner_of_record"] == "local-owner"
    roster = agent_lifecycle.agent_states()
    assert [row["agent"] for row in roster["agents"]] == [label]
    assert "Foreign_Only" not in {row["agent"] for row in roster["registered"]}


@pytest.mark.parametrize("initial_state", ["active", "retired"])
def test_precontext_retirement_cache_is_bound_to_process_org(monkeypatch, initial_state):
    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{RETIREE}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    other_state = "retired" if initial_state == "active" else "active"
    _org_lifecycle_event(monkeypatch, "org-A", RETIREE, initial_state)
    _org_lifecycle_event(monkeypatch, "org-B", RETIREE, other_state)
    agent_lifecycle.invalidate_identity_cache()
    with TestClient(_mcp_app()) as client:
        for org, state in (("org-A", initial_state), ("org-B", other_state)):
            _lifecycle_org(monkeypatch, org)
            response = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == (403 if state == "retired" else 200)
            if state == "retired":
                assert response.json()["code"] == "agent_retired"
    agent_lifecycle.invalidate_identity_cache(RETIREE)
    for org in ("org-A", "org-B"):
        _lifecycle_org(monkeypatch, org)
        assert agent_lifecycle.cached_identity_state(RETIREE) == (False, None)


def test_lifecycle_refusal_rate_limit_is_org_scoped(monkeypatch):
    for org in (None, "org-A", "org-B"):
        _lifecycle_org(monkeypatch, org)
        for _ in range(2):
            agent_lifecycle.record_refusal("Desktop_Agent2", site="org-scope-test")
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT org_id FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.site')=? ORDER BY org_id",
            (agent_lifecycle.CHANNEL, "org-scope-test"),
        ).fetchall()
        assert [row["org_id"] for row in rows] == [None, "org-A", "org-B"]
    finally:
        conn.close()


@pytest.mark.parametrize("org", [None, "org-A"])
@pytest.mark.parametrize("action", ["retire", "terminate"])
def test_lifecycle_claim_effects_are_org_scoped(monkeypatch, org, action):
    claims = {}
    for scope in (None, "org-A", "org-B"):
        _lifecycle_org(monkeypatch, scope)
        claims[scope] = _claim(RETIREE)["id"]
    _lifecycle_org(monkeypatch, org)
    conn = storage.connect()
    try:
        assert agent_lifecycle._doing_on(conn, RETIREE.upper()) == [claims[org]]
    finally:
        conn.close()
    assert agent_lifecycle._doing_tasks(RETIREE) == [claims[org]]
    with _as(OP):
        if action == "retire":
            with pytest.raises(ToolError, match="holds 1 doing claim"):
                agent_lifecycle.agent_retire(RETIREE, "decommission")
            # Even a stale caller-supplied target list cannot release another org's claim.
            agent_lifecycle._release_claims(RETIREE, list(claims.values()), "release")
        result = agent_lifecycle.set_agent_state(RETIREE, action, "decommission")
    assert result["state"] == ("retired" if action == "retire" else "terminated")
    conn = storage.connect()
    try:
        for scope, tid in claims.items():
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            assert row["status"] == ("open" if scope == org else "doing")
    finally:
        conn.close()


def test_pause_blocks_claims_and_renewals_and_resume_restores():
    held = _new_task()
    _claim("Desktop_Agent2", held)
    with _as(OP):
        res = agent_lifecycle.agent_pause("Desktop_Agent2", "runaway spend")
    assert res["state"] == "paused" and res["changed"] is True
    assert res["actor"] == OP and res["reason"] == "runaway spend"
    # The held task is a self-pickup claim: no process the hub controls.
    assert any(
        s["path"] == "self-pickup" and s["control"] == "unsupported" and s["ref"] == f"task:{held}"
        for s in res["stops"]
    )
    with pytest.raises(ToolError, match="is paused"):
        _claim("Desktop_Agent2")
    with pytest.raises(ToolError, match="is paused"):
        _claim("Desktop_Agent2", held)  # renewal refused too
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="is paused"):
        task_update(_new_task(), status="doing", worktree=False)
    assert _claim("server_agent3")["status"] == "doing"  # other agents unaffected

    with _as(OP):
        assert agent_lifecycle.agent_resume("Desktop_Agent2")["state"] == "active"
    assert _claim("Desktop_Agent2")["status"] == "doing"
    [paused, resumed] = _events(agent_lifecycle.KIND_CHANGED)
    assert (paused["previous"], paused["state"]) == ("active", "paused")
    assert (resumed["previous"], resumed["state"]) == ("paused", "active")


def test_unverified_actor_pause_does_not_gate_an_unattributed_run():
    with _as(OP):
        agent_lifecycle.agent_pause("Desktop_Agent2", "operator hold")
    result = task_update(_new_task(), status="doing", actor="Desktop_Agent2", worktree=False)
    assert result["current_run_agent"] is None
    # A verified caller still receives its own pause gate, regardless of actor.
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="is paused"):
        task_update(_new_task(), status="doing", actor="server_agent3", worktree=False)


def test_unverified_lifecycle_admission_still_uses_a_surviving_claimant():
    with _as(OP):
        agent_lifecycle.agent_pause("Desktop_Agent2", "operator hold")
    tid = _new_task()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', claimed_by='Desktop_Agent2', "
            "claimed_at=?, lock_expires_at=? WHERE id=?",
            (storage.now(), "2999-01-01T00:00:00+00:00", tid),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ToolError, match="is paused"):
        task_update(tid, status="doing", actor="server_agent3", worktree=False)


def test_terminate_releases_doing_claims_and_closes_runs():
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    with _as(OP):
        res = agent_lifecycle.agent_terminate("Desktop_Agent2", "decommissioned")
    assert res["state"] == "terminated"
    assert {"path": "claim", "ref": f"task:{tid}", "control": "released"}.items() <= next(
        s for s in res["stops"] if s["path"] == "claim"
    ).items()
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["claimed_by"]) == ("open", None)
    [run] = run_records.run_history(task_id=tid)["runs"]
    assert run["outcome"] == "abandoned" and "terminated" in run["reason"]
    with _as(OP), pytest.raises(ToolError, match="terminated"):
        agent_lifecycle.agent_pause("Desktop_Agent2", "again")
    assert _claim("server_agent3", tid)["claimed_by"] == "server_agent3"


def test_authority_rules():
    # Another agent may not pause a peer.
    with _as("Desktop_Agent1"), pytest.raises(ToolError, match="operator action"):
        agent_lifecycle.agent_pause("Desktop_Agent2", "no")
    # An agent may pause itself and resume its own pause.
    with _as("Desktop_Agent2"):
        agent_lifecycle.agent_pause("Desktop_Agent2", "stepping away")
        assert agent_lifecycle.agent_resume("Desktop_Agent2")["state"] == "active"
    # ...but not resume an operator's pause.
    with _as(OP):
        agent_lifecycle.agent_pause("Desktop_Agent2", "operator hold")
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="only an operator"):
        agent_lifecycle.agent_resume("Desktop_Agent2")
    # Pause and terminate need a reason; unknown actions are refused.
    with _as(OP), pytest.raises(ToolError, match="requires a reason"):
        agent_lifecycle.agent_terminate("server_agent3", " ")
    with _as(OP), pytest.raises(ToolError, match="action must be one of"):
        agent_lifecycle.set_agent_state("server_agent3", "kill", "x")
    # No identity at all is refused.
    with pytest.raises(ToolError, match="authenticated identity"):
        agent_lifecycle.agent_pause("server_agent3", "x")


def test_operators_are_configurable(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent5")
    with _as("Desktop_Agent5"):
        assert agent_lifecycle.agent_pause("Desktop_Agent2", "cos hold")["state"] == "paused"
    with _as(OP), pytest.raises(ToolError, match="operator action"):
        agent_lifecycle.agent_resume("Desktop_Agent2")


def test_repeat_is_a_no_op():
    with _as(OP):
        agent_lifecycle.agent_pause("Desktop_Agent2", "a")
        again = agent_lifecycle.agent_pause("Desktop_Agent2", "b")
    assert again["changed"] is False and again["reason"] == "a"
    assert len(_events(agent_lifecycle.KIND_CHANGED)) == 1


@pytest.mark.parametrize("org", [None, "org-A"])
@pytest.mark.parametrize("path", ["dispatch", "node", "executor"])
def test_lifecycle_stop_selectors_use_parent_org(monkeypatch, org, path):
    expected = {}
    for scope in (None, "org-A", "org-B"):
        _lifecycle_org(monkeypatch, scope)
        tid = _new_task()
        suffix = scope or "private"
        conn = storage.connect()
        try:
            now = storage.now()
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
                "created_at, updated_at) VALUES (?, ?, ?, 'running', 'test', ?, ?)",
                (f"run-{suffix}", tid, RETIREE, now, now),
            )
            conn.execute(
                "INSERT INTO node_dispatch (id, task_id, node, agent, backend, prompt, cwd, "
                "timeout_s, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, 'test-node', ?, 'codex', 'test', '.', 60, 'queued', 'test', ?, ?)",
                (f"node-{suffix}", tid, RETIREE, now, now),
            )
            cur = conn.execute(
                "INSERT INTO executor_attempts (task_id, agent, backend, started_at) "
                "VALUES (?, ?, 'codex', ?)",
                (tid, RETIREE, now),
            )
            expected[scope] = {
                "dispatch": {f"run-{suffix}"},
                "node": {f"node-{suffix}"},
                "executor": {f"attempt:{cur.lastrowid}"},
            }
            delegation = conn.execute(
                "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, "
                "created_at, updated_at, org_id) VALUES ('test', ?, ?, '{}', ?, ?, ?)",
                (RETIREE, tid, now, now, scope),
            ).lastrowid
            attempt = conn.execute(
                "INSERT INTO executor_attempts (delegation_id, agent, backend, started_at) "
                "VALUES (?, ?, 'codex', ?)",
                (delegation, RETIREE, now),
            ).lastrowid
            expected[scope]["executor"].add(f"attempt:{attempt}")
            conn.commit()
        finally:
            conn.close()
    # Org-less node items can still be owned through their dispatch run.
    conn = storage.connect()
    try:
        for scope in (None, "org-A", "org-B"):
            suffix = scope or "private"
            conn.execute(
                "INSERT INTO node_dispatch (id, run_id, node, agent, backend, prompt, cwd, "
                "timeout_s, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, 'test-node', ?, 'codex', 'test', '.', 60, 'claimed', 'test', ?, ?)",
                (f"linked-{suffix}", f"run-{suffix}", RETIREE, storage.now(), storage.now()),
            )
            expected[scope]["node"].add(f"linked-{suffix}")
        conn.execute(
            "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
            "status, created_by, created_at, updated_at) "
            "VALUES ('unbound', 'test-node', ?, 'codex', 'test', '.', 60, 'queued', 'test', ?, ?)",
            (RETIREE, storage.now(), storage.now()),
        )
        attempt = conn.execute(
            "INSERT INTO executor_attempts (agent, backend, started_at) VALUES (?, 'codex', ?)",
            (RETIREE, storage.now()),
        ).lastrowid
        expected[None]["node"].add("unbound")
        expected[None]["executor"].add(f"attempt:{attempt}")
        conn.commit()
    finally:
        conn.close()
    _lifecycle_org(monkeypatch, org)
    calls = []
    monkeypatch.setattr(
        coordinator,
        "cancel_run",
        lambda run_id, **kwargs: calls.append(run_id) or {"termination_outcome": "confirmed"},
    )
    monkeypatch.setattr(node_queue, "cancel", lambda item_id, **kwargs: calls.append(item_id))
    if path == "dispatch":
        rows = coordinator.lifecycle_stop_runs(RETIREE, OP, "hold")
    elif path == "node":
        rows = node_queue.lifecycle_stop_items(RETIREE, OP, "hold")
    else:
        rows = agent_lifecycle._executor_attempts(RETIREE)
    assert {row["ref"] for row in rows} == expected[org][path]
    if path != "executor":
        assert set(calls) == expected[org][path]


def test_node_items_and_executor_attempts_get_stop_requests():
    tid = _new_task()
    conn = storage.connect()
    try:
        now = storage.now()
        for item_id, status in (("n-queued", "queued"), ("n-claimed", "claimed")):
            conn.execute(
                "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
                "status, created_by, created_at, updated_at) VALUES (?, 'mac', 'Desktop_Agent2', "
                "'codex', 'p', '.', 60, ?, 'x', ?, ?)",
                (item_id, status, now, now),
            )
        conn.execute(
            "INSERT INTO executor_attempts (task_id, agent, backend, started_at) "
            "VALUES (?, 'Desktop_Agent2', 'codex', ?)",
            (tid, now),
        )
        conn.commit()
    finally:
        conn.close()
    with _as(OP):
        stops = agent_lifecycle.agent_pause("Desktop_Agent2", "hold")["stops"]
    by_ref = {s["ref"]: s for s in stops}
    assert by_ref["n-queued"]["control"] == "requested"
    assert "will not start" in by_ref["n-queued"]["detail"]
    assert "heartbeat" in by_ref["n-claimed"]["detail"]
    assert any(s["path"] == "executor" and s["control"] == "cooperative" for s in stops)
    conn = storage.connect()
    try:
        flags = {
            r["id"]: r["cancel_requested"]
            for r in conn.execute("SELECT id, cancel_requested FROM node_dispatch").fetchall()
        }
    finally:
        conn.close()
    assert flags == {"n-queued": 1, "n-claimed": 1}
    [recorded] = _events("lifecycle.stops_requested")
    assert len(recorded["stops"]) == len(stops)


def test_executor_kill_check_honours_a_pause():
    from unified_agent.executor.daemon import ExecutorDaemon

    daemon = ExecutorDaemon.__new__(ExecutorDaemon)
    daemon.agent_label = "server_agent2"
    clock = {"t": 0.0}
    daemon.clock = lambda: clock["t"]
    daemon.hub_client = None
    check = daemon._lifecycle_stop_check(min_interval_s=10)
    assert check() is False
    with _as(OP):
        agent_lifecycle.agent_pause("server_agent2", "hold")
    assert check() is False  # throttled: not re-read yet
    clock["t"] = 11
    assert check() is True
    assert check() is True
    [confirmed] = _events(agent_lifecycle.KIND_STOP_CONFIRMED)
    assert confirmed["agent"] == "server_agent2"


def test_coordination_manage_exposes_lifecycle_actions():
    # The MCP path is for an agent acting on itself (and configured operators);
    # the human operator profile uses the Control Center route instead.
    with _as("Desktop_Agent2"):
        res = coordination_manage("agent_pause", {"agent": "Desktop_Agent2", "reason": "hold"})
        assert res["state"] == "paused"
        state = coordination_manage("agent_state", {"agent": "Desktop_Agent2"})
        assert state["state"] == "paused"
        listing = coordination_manage("agent_states", {})
        assert [a["agent"] for a in listing["agents"]] == ["Desktop_Agent2"]
        assert coordination_manage("agent_resume", {"agent": "Desktop_Agent2"})["state"] == "active"


def test_lifecycle_routes(monkeypatch):
    token = f"tok-life-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{OP}:{token},Desktop_Agent1:{token}x")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        res = client.post(
            "/api/agent-lifecycle",
            json={"agent": "Desktop_Agent2", "action": "pause", "reason": "ui hold"},
            headers=auth,
        )
        assert res.status_code == 200, res.text
        assert res.json()["state"] == "paused" and res.json()["actor"] == OP
        listing = client.get("/api/agent-lifecycle", headers=auth)
        assert listing.status_code == 200, listing.text
        assert listing.json()["agents"][0]["state"] == "paused"
        bad = client.post(
            "/api/agent-lifecycle",
            json={"agent": "x", "action": "pause", "extra": 1},
            headers=auth,
        )
        assert bad.status_code == 400
        peer = client.post(
            "/api/agent-lifecycle",
            json={"agent": "Desktop_Agent2", "action": "resume"},
            headers={"Authorization": f"Bearer {token}x"},
        )
        assert peer.status_code == 400 and "operator" in peer.json()["error"]
        assert client.get("/api/agent-lifecycle").status_code in {401, 403}


# --- stop handlers fail closed (task #1723) -----------------------------------


def _changed_events() -> list[dict]:
    return _events(agent_lifecycle.KIND_CHANGED)


def test_hub_registers_both_stop_handlers_from_their_owners():
    # This test process imports server and webui.routes, as the hub does.
    assert agent_lifecycle.missing_stop_handlers() == ()
    assert agent_lifecycle._STOP_HANDLERS == {
        "dispatch": coordinator.lifecycle_stop_runs,
        "node": node_queue.lifecycle_stop_items,
    }


@pytest.mark.parametrize("action", ["terminate", "pause"])
@pytest.mark.parametrize("registered", [{}, {"dispatch"}, {"node"}])
def test_stop_without_registered_handlers_refuses_and_writes_nothing(
    monkeypatch, action, registered
):
    handlers = {n: f for n, f in agent_lifecycle._STOP_HANDLERS.items() if n in registered}
    monkeypatch.setattr(agent_lifecycle, "_STOP_HANDLERS", handlers)
    missing = [n for n in agent_lifecycle.REQUIRED_STOP_HANDLERS if n not in registered]
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    with _as(OP), pytest.raises(ToolError, match="stop handler") as err:
        agent_lifecycle.set_agent_state("Desktop_Agent2", action, "decommissioned")
    assert ", ".join(missing) in str(err.value)
    assert "Nothing was changed" in str(err.value)
    # Nothing recorded, nothing released: the agent is still active and holds its claim.
    assert agent_lifecycle.agent_state("Desktop_Agent2")["state"] == "active"
    assert _changed_events() == []
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["claimed_by"]) == ("doing", "Desktop_Agent2")


def test_stop_without_handlers_refuses_over_mcp(monkeypatch):
    monkeypatch.setattr(agent_lifecycle, "_STOP_HANDLERS", {})
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="stop handler"):
        coordination_manage("agent_terminate", {"agent": "Desktop_Agent2", "reason": "done"})
    assert _changed_events() == []


def test_resume_needs_no_stop_handlers(monkeypatch):
    with _as(OP):
        agent_lifecycle.agent_pause("Desktop_Agent2", "hold")
    monkeypatch.setattr(agent_lifecycle, "_STOP_HANDLERS", {})
    with _as(OP):
        assert agent_lifecycle.agent_resume("Desktop_Agent2")["state"] == "active"


def test_register_stop_handler_refuses_an_unlisted_name():
    with pytest.raises(ValueError, match="REQUIRED_STOP_HANDLERS"):
        agent_lifecycle.register_stop_handler("executor", lambda a, b, c: [])
    with pytest.raises(TypeError):
        agent_lifecycle.register_stop_handler("dispatch", None)  # type: ignore[arg-type]
    assert agent_lifecycle._STOP_HANDLERS["dispatch"] is coordinator.lifecycle_stop_runs


def test_terminate_cancels_live_dispatch_runs(monkeypatch):
    runs = [
        ("run-live", "Desktop_Agent2", "running"),
        ("run-claimed", "Desktop_Agent2", "claimed"),
        ("run-done", "Desktop_Agent2", "finished"),
        ("run-peer", "server_agent3", "running"),
    ]
    tids = [_new_task() for _ in runs]
    conn = storage.connect()
    try:
        now = storage.now()
        for (run_id, agent, state), tid in zip(runs, tids, strict=True):
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'x', ?, ?)",
                (run_id, tid, agent, state, now, now),
            )
        conn.commit()
    finally:
        conn.close()
    calls = []

    def fake_cancel(run_id, *, actor, reason="cancelled"):
        calls.append((run_id, actor, reason))
        if run_id == "run-claimed":
            raise RuntimeError("boom")
        return {"termination_outcome": "confirmed"}

    monkeypatch.setattr(coordinator, "cancel_run", fake_cancel)
    with _as(OP):
        stops = agent_lifecycle.agent_terminate("Desktop_Agent2", "decommissioned")["stops"]
    assert sorted(calls) == [
        ("run-claimed", OP, "decommissioned"),
        ("run-live", OP, "decommissioned"),
    ]
    by_ref = {s["ref"]: s for s in stops if s["path"] == "dispatch"}
    assert by_ref["run-live"] == {
        "path": "dispatch",
        "ref": "run-live",
        "control": "confirmed",
        "detail": "confirmed",
    }
    assert (
        by_ref["run-claimed"]["control"] == "failed" and "boom" in by_ref["run-claimed"]["detail"]
    )
    assert set(by_ref) == {"run-live", "run-claimed"}


def test_a_fresh_process_registers_handlers_only_once_the_hub_is_loaded(tmp_path):
    code = (
        "from unified_agent import agent_lifecycle as a\n"
        "print(','.join(a.missing_stop_handlers()))\n"
        "import unified_agent.server\n"
        "print(','.join(a.missing_stop_handlers()) or 'none')\n"
    )
    env = {**os.environ, "UA_HOME": str(tmp_path / "proc_home")}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["dispatch,node", "none"]


# --- retirement (task #2305) ---------------------------------------------------

RETIREE = "Local_Retiree"


def _fake_token() -> str:
    return f"tok-retire-{secrets.token_urlsafe(12)}"


def _token_row(label: str, status: str = "active") -> int:
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, "
            "issued_by) VALUES (?, ?, 'sha256', ?, ?, 'test')",
            (label, secrets.token_hex(32), status, storage.now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _token_statuses(label: str) -> list[str]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT status FROM agent_tokens WHERE agent_label=? ORDER BY id", (label,)
        ).fetchall()
    finally:
        conn.close()
    return [r["status"] for r in rows]


def _session_row(label: str) -> str:
    from unified_agent import session_credentials

    conn = storage.connect()
    try:
        session_credentials.ensure_schema(conn)
        cid = secrets.token_hex(8)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_credentials)")}
        now = storage.now()
        values = {
            "credential_id": cid,
            "token_sha256": secrets.token_hex(32),
            "agent_label": label,
            "device_agent": label,
            "node": "retire-node-01",
            "item_id": f"nd-{secrets.token_hex(8)}",
            "attempt": 1,
            "tools_json": "[]",
            "issued_at": now,
            "expires_at": "2999-01-01T00:00:00+00:00",
            "idle_expires_at": "2999-01-01T00:00:00+00:00",
        }
        values = {k: v for k, v in values.items() if k in cols}
        conn.execute(
            f"INSERT INTO session_credentials ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        conn.commit()
        return cid
    finally:
        conn.close()


def _session_reason(cid: str) -> str | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT revoke_reason FROM session_credentials WHERE credential_id=?", (cid,)
        ).fetchone()
    finally:
        conn.close()
    return row["revoke_reason"]


@pytest.mark.parametrize("org", [None, "org-A"])
def test_retirement_revokes_only_local_credentials_atomically(monkeypatch, org):
    token_ids = {}
    credential_ids = {}
    for scope in (None, "org-A", "org-B"):
        token_ids[scope] = _token_row(RETIREE.upper())
        credential_ids[scope] = [_session_row(RETIREE.upper()) for _ in range(2)]
        conn = storage.connect()
        try:
            conn.execute("UPDATE agent_tokens SET org_id=? WHERE id=?", (scope, token_ids[scope]))
            for index, cid in enumerate(credential_ids[scope]):
                conn.execute(
                    "UPDATE session_credentials SET org_id=?, agent_label=? WHERE credential_id=?",
                    (scope, RETIREE.upper() if index == 0 else "Local_Bystander", cid),
                )
            conn.commit()
        finally:
            conn.close()
    _org_lifecycle_event(monkeypatch, "org-B", RETIREE, "retired")
    _lifecycle_org(monkeypatch, org)
    real_hook = agent_lifecycle._before_append

    def fail_append(*_args):
        raise RuntimeError("append failed")

    monkeypatch.setattr(agent_lifecycle, "_before_append", fail_append)
    with _as(OP), pytest.raises(RuntimeError, match="append failed"):
        agent_lifecycle.agent_retire(RETIREE, "decommission")
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "active"
    assert all(_session_reason(cid) is None for ids in credential_ids.values() for cid in ids)
    assert _token_statuses(RETIREE.upper()) == ["active"] * 3

    monkeypatch.setattr(agent_lifecycle, "_before_append", real_hook)
    with _as(OP):
        result = agent_lifecycle.agent_retire(RETIREE, "decommission")
    assert result["changed"] is True
    assert result["revoked"]["agent_tokens"] == [token_ids[org]]
    assert set(result["revoked"]["session_credentials"]) == set(credential_ids[org])
    for scope, ids in credential_ids.items():
        assert [_session_reason(cid) for cid in ids] == [
            "agent_retired" if scope == org else None
        ] * 2


def test_retire_refuses_while_a_doing_claim_is_held_then_revokes_after_release():
    tid = _new_task()
    _claim(RETIREE, tid)
    token_id = _token_row(RETIREE)
    other = _token_row("Local_Bystander")
    cid = _session_row(RETIREE)
    with _as(OP), pytest.raises(ToolError, match=rf"holds 1 doing claim.*#{tid}"):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    # Refused before anything was written: still active, nothing revoked.
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "active"
    assert _changed_events() == []
    assert _token_statuses(RETIREE) == ["active"]
    with _as(RETIREE):
        task_update(tid, status="open", worktree=False)

    with _as(OP):
        res = agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    assert res["state"] == "retired" and res["changed"] is True
    assert res["revoked"] == {
        "agent_tokens": [token_id],
        "session_credentials": [cid],
        "device_signins": [],
    }
    assert _token_statuses(RETIREE) == ["revoked"]
    assert _token_statuses("Local_Bystander") == ["active"] and other
    assert _session_reason(cid) == "agent_retired"
    [changed] = _changed_events()
    assert (changed["previous"], changed["state"], changed["action"]) == (
        "active",
        "retired",
        "retire",
    )
    conn = storage.connect()
    try:
        channels = {
            r["channel"]
            for r in conn.execute(
                "SELECT channel FROM event_log WHERE channel IN (?, ?)",
                ("security.agent_token.revoked", "hub.session_credential.revoked"),
            )
        }
    finally:
        conn.close()
    assert channels == {"security.agent_token.revoked", "hub.session_credential.revoked"}
    # A retired agent cannot claim new work.
    with pytest.raises(ToolError, match="is retired"):
        _claim(RETIREE)


def test_double_retire_is_a_no_op_and_resume_cannot_undo_it():
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
        again = agent_lifecycle.agent_retire(RETIREE, "again")
        assert again["changed"] is False and again["reason"] == "decommissioned"
        with pytest.raises(ToolError, match="retired.*unretire"):
            agent_lifecycle.agent_resume(RETIREE)
        with pytest.raises(ToolError, match="retired"):
            agent_lifecycle.agent_pause(RETIREE, "hold")
        with pytest.raises(ToolError, match="retired"):
            agent_lifecycle.agent_terminate(RETIREE, "stop")
    assert len(_changed_events()) == 1
    with _as(OP):
        back = agent_lifecycle.agent_unretire(RETIREE, "rehired")
    assert back["state"] == "active" and back["changed"] is True
    assert _claim(RETIREE)["status"] == "doing"


def test_retire_and_unretire_are_operator_only_and_need_a_reason():
    with _as(RETIREE), pytest.raises(ToolError, match="operator action"):
        agent_lifecycle.agent_retire(RETIREE, "self")
    with _as("Local_Peer"), pytest.raises(ToolError, match="operator action"):
        agent_lifecycle.agent_retire(RETIREE, "peer")
    with _as(OP), pytest.raises(ToolError, match="requires a reason"):
        agent_lifecycle.agent_retire(RETIREE, " ")
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    with _as(RETIREE), pytest.raises(ToolError, match="operator action"):
        agent_lifecycle.agent_unretire(RETIREE, "back")
    with _as(OP):
        agent_lifecycle.agent_pause("Local_Peer", "hold")
        with pytest.raises(ToolError, match="is not retired"):
            agent_lifecycle.agent_unretire("Local_Peer")
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "retired"


def test_owner_of_record_defaults_to_the_office_and_records_an_explicit_owner(
    tmp_path, monkeypatch
):
    import yaml

    chart = tmp_path / "org.yaml"
    chart.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "offices": [
                    {"id": "test-office", "lead": RETIREE, "members": [RETIREE, "Local_Peer"]},
                    {"id": "other-office", "lead": "Local_Lead", "members": ["Local_Lead"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(chart))
    state = agent_lifecycle.agent_state(RETIREE)
    assert (state["owner_of_record"], state["owner_source"]) == ("test-office", "org_registry")
    assert agent_lifecycle.agent_state("Local_Nobody")["owner_of_record"] is None
    with _as(OP):
        with pytest.raises(ToolError, match="owner"):
            agent_lifecycle.agent_retire(RETIREE, "reorg", owner="not a real owner!")
        res = agent_lifecycle.agent_retire(RETIREE, "reorg", owner="other-office")
    assert (res["owner_of_record"], res["owner_source"]) == ("other-office", "explicit")
    [changed] = _changed_events()
    assert changed["owner"] == "other-office"
    listing = agent_lifecycle.agent_states()
    registered = {a["agent"]: a for a in listing["registered"]}
    assert set(registered) >= {RETIREE, "Local_Peer", "Local_Lead"}
    assert registered[RETIREE]["state"] == "retired"
    assert registered["Local_Peer"]["owner_of_record"] == "test-office"
    assert [a["agent"] for a in listing["agents"]] == [RETIREE]


def test_retired_history_stays_readable():
    tid = _new_task()
    _claim(RETIREE, tid)
    with _as(RETIREE):
        task_update(tid, status="done", worktree=False)
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done"
    assert run_records.run_history(task_id=tid)["runs"][0]["agent"] == RETIREE


def test_refused_label_covers_env_revocation_and_retirement(monkeypatch):
    assert agent_lifecycle.refused_label(RETIREE) is None
    monkeypatch.setenv("UA_REVOKED_AGENTS", RETIREE)
    assert "UA_REVOKED_AGENTS" in agent_lifecycle.refused_label(RETIREE)
    monkeypatch.delenv("UA_REVOKED_AGENTS")
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    assert "is retired" in agent_lifecycle.refused_label(RETIREE)
    with pytest.raises(agent_lifecycle.AgentRetiredError, match="is retired") as err:
        agent_lifecycle.refuse_if_retired(RETIREE, site="test.site")
    assert isinstance(err.value, ToolError) and err.value.agent == RETIREE
    agent_lifecycle.refuse_if_retired("Local_Peer", site="test.site")  # not retired: no raise
    [refused] = _events(agent_lifecycle.KIND_REFUSED)
    assert (refused["agent"], refused["site"]) == (RETIREE, "test.site")


def test_retired_identity_is_refused_at_team_up():
    from unified_agent import team_spec

    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    with pytest.raises(agent_lifecycle.AgentRetiredError, match="is retired"):
        team_spec.boot({"team": "t"}, actor=RETIREE)
    assert _events(agent_lifecycle.KIND_REFUSED)[0]["site"] == "team_spec.team_up"


def test_coordination_manage_and_route_expose_retire(monkeypatch):
    # MCP is for configured lifecycle operators; the human operator uses the route.
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Local_Ops")
    with _as("Local_Ops"):
        res = coordination_manage("agent_retire", {"agent": RETIREE, "reason": "decommissioned"})
        assert res["state"] == "retired"
        back = coordination_manage("agent_unretire", {"agent": RETIREE, "reason": "rehired"})
        assert back["state"] == "active"
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS")
    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{OP}:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        res = client.post(
            "/api/agent-lifecycle",
            json={"agent": RETIREE, "action": "retire", "reason": "ui", "owner": OP},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["state"] == "retired" and res.json()["owner_of_record"] == OP


def _doctor_row() -> dict:
    from unified_agent.tools import doctor

    return doctor._retired_agent_credentials_check()


def test_doctor_retired_agent_credentials(monkeypatch):
    monkeypatch.setattr(
        "unified_agent.tools.doctor._hub_setting", lambda name: (None, True), raising=True
    )
    assert _doctor_row()["status"] == "LIVE"
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    row = _doctor_row()
    assert row["capability"] == "retired_agent_credentials" and row["status"] == "LIVE"
    # A credential that slipped past the revocation (written after retire) is flagged.
    _token_row(RETIREE)
    _session_row(RETIREE)
    row = _doctor_row()
    assert row["status"] == "MISCONFIGURED"
    assert "1 active agent_tokens row" in row["detail"] and RETIREE in row["detail"]
    assert "1 live session credential" in row["detail"]
    # A device sign-in minted after the retirement is flagged too (task #2315).
    _signin_rows(RETIREE)
    assert "2 live device sign-in token(s)" in _doctor_row()["detail"]
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM oauth_access_tokens")
        conn.execute("DELETE FROM oauth_refresh_tokens")
        conn.commit()
    finally:
        conn.close()
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_tokens SET status='revoked' WHERE agent_label=?", (RETIREE,))
        conn.execute("UPDATE session_credentials SET revoked_at=? ", (storage.now(),))
        conn.commit()
    finally:
        conn.close()
    assert _doctor_row()["status"] == "LIVE"
    # An env bearer the DB revoke cannot reach.
    raw = f"{RETIREE}:{_fake_token()}"
    monkeypatch.setattr(
        "unified_agent.tools.doctor._hub_setting",
        lambda name: (raw if name == "UA_AGENT_TOKENS" else None, True),
    )
    row = _doctor_row()
    assert row["status"] == "MISCONFIGURED"
    assert "remove from UA_AGENT_TOKENS" in row["detail"]
    assert "tok-retire" not in row["detail"]
    # A shared bearer is dropped by agent_tokens, but the retired label is still configured.
    shared = _fake_token()
    raw = f"{RETIREE}:{shared},Desktop_Agent2:{shared}"
    monkeypatch.setattr(
        "unified_agent.tools.doctor._hub_setting",
        lambda name: (raw if name == "UA_AGENT_TOKENS" else None, True),
    )
    row = _doctor_row()
    assert row["status"] == "MISCONFIGURED"
    assert RETIREE in row["detail"] and "UA_AGENT_TOKENS" in row["detail"]


def test_token_issue_refuses_a_retired_label():
    from unified_agent.tools import token_lifecycle

    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
        with pytest.raises(ToolError, match="retired"):
            token_lifecycle.agent_token_issue(RETIREE, "fresh-token-value-not-a-secret")
    assert _token_statuses(RETIREE) == []


@pytest.mark.parametrize("first", ["self_pause", "operator_retire"])
def test_a_transition_racing_a_retire_cannot_overwrite_it(monkeypatch, first):
    """Review of #2305: read, authorize and append share one write transaction.

    The first transition is held between its read and its append while the
    other starts. The other must wait for the lock and then be judged against
    the committed state, so the agent always ends retired.
    """
    import threading
    import time

    moves = {
        "self_pause": (RETIREE, lambda: agent_lifecycle.agent_pause(RETIREE, "stepping away")),
        "operator_retire": (OP, lambda: agent_lifecycle.agent_retire(RETIREE, "decommissioned")),
    }
    second = "operator_retire" if first == "self_pause" else "self_pause"
    outcome: dict = {}
    real_hook = agent_lifecycle._before_append

    def run_second():
        label, move = moves[second]
        with _as(label):
            try:
                outcome["second"] = move()
            except ToolError as exc:
                outcome["second_error"] = str(exc)

    other = threading.Thread(target=run_second)

    def hook(conn, label, verb):
        if not other.is_alive() and "started" not in outcome:
            outcome["started"] = True
            other.start()
            time.sleep(0.5)
            # Still waiting on the lock: it read nothing before this append.
            outcome["blocked"] = other.is_alive()
        real_hook(conn, label, verb)

    monkeypatch.setattr(agent_lifecycle, "_before_append", hook)
    label, move = moves[first]
    with _as(label):
        move()
    other.join(10)
    assert outcome["blocked"] is True
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "retired"
    if first == "operator_retire":
        assert "is retired" in outcome["second_error"]
    with _as(RETIREE), pytest.raises(ToolError):
        agent_lifecycle.agent_resume(RETIREE)
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "retired"


def test_lifecycle_refusal_covers_pause_and_terminate_while_retired_checks_stay_narrow(
    monkeypatch,
):
    """Task #2315: one refusal for every stopped state; the retired-only helpers are unchanged."""
    assert agent_lifecycle.lifecycle_refusal(RETIREE, site="test.site") is None
    with _as(OP):
        agent_lifecycle.agent_pause(RETIREE, "stepping away")
    refusal = agent_lifecycle.lifecycle_refusal(RETIREE, site="test.site")
    assert (refusal["state"], refusal["code"]) == ("paused", "agent_paused")
    assert "is paused" in refusal["message"] and "stepping away" in refusal["message"]
    # The narrow helpers still mean "retired" only; env revocation stays refused_label's.
    assert agent_lifecycle.retired_refusal(RETIREE, site="test.site") is None
    assert agent_lifecycle.refused_label(RETIREE) is None
    monkeypatch.setenv("UA_REVOKED_AGENTS", RETIREE)
    assert "UA_REVOKED_AGENTS" in agent_lifecycle.refused_label(RETIREE)
    monkeypatch.delenv("UA_REVOKED_AGENTS")
    with _as(OP):
        agent_lifecycle.agent_terminate(RETIREE, "done")
    terminated = agent_lifecycle.lifecycle_refusal(RETIREE, site="test.site")
    assert terminated["code"] == "agent_terminated"
    with _as(OP):
        agent_lifecycle.agent_resume(RETIREE)
    assert agent_lifecycle.lifecycle_refusal(RETIREE, site="test.site") is None
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    retired = agent_lifecycle.lifecycle_refusal(RETIREE, site="test.site")
    assert retired["code"] == "agent_retired"
    assert retired["message"] == agent_lifecycle.retired_refusal(RETIREE, site="test.site")
    refused = [(e["site"], e["state"]) for e in _events(agent_lifecycle.KIND_REFUSED)]
    # Rate-limited per (label, site, state): one row per stopped state.
    assert sorted(refused) == [
        ("test.site", "paused"),
        ("test.site", "retired"),
        ("test.site", "terminated"),
    ]


# --- per-request refusal and device sign-ins (task #2315) ----------------------


def _mcp_app():
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def mcp(request):
        return JSONResponse(
            {
                "identity": config.verified_agent_identity(),
                "episode": config.current_episode_id(),
            }
        )

    return server.HubAuthASGIMiddleware(
        Starlette(routes=[Route("/mcp", mcp, methods=["POST"]), *web_routes()])
    )


def test_a_retired_bearer_is_refused_per_request_with_a_typed_403(monkeypatch):
    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{RETIREE}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(_mcp_app()) as client:
        assert client.post("/mcp", headers=auth).json()["identity"] == RETIREE
        with _as(OP):
            agent_lifecycle.agent_retire(RETIREE, "decommissioned")
        # The retire cleared the cached state at once: no TTL wait.
        for path in ("/mcp", "/api/agent-lifecycle"):
            res = client.post(path, headers=auth, json={})
            assert res.status_code == 403, (path, res.text)
            assert res.json()["code"] == agent_lifecycle.AgentRetiredError.reason
            assert "is retired" in res.json()["error"]
        refused = _events(agent_lifecycle.KIND_REFUSED)
        # Rate-limited: one event for repeated refusals at the same site.
        assert [(e["agent"], e["site"], e["state"]) for e in refused] == [
            (RETIREE, "hub.auth.bearer", "retired")
        ]
        with _as(OP):
            agent_lifecycle.agent_unretire(RETIREE, "rehired")
        assert client.post("/mcp", headers=auth).status_code == 200


def test_allowed_bearer_calls_write_nothing_and_read_the_lifecycle_once_per_ttl(monkeypatch):
    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{RETIREE}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setattr(server.registry, "touch_agent", lambda *a, **k: None)
    monkeypatch.setattr(server.mcp_runtime_telemetry, "record_session_touch", lambda *a, **k: None)
    _claim(RETIREE)  # one live claim: the episode lookup runs too
    agent_lifecycle.invalidate_identity_cache()
    statements: list[str] = []
    real_connect = storage.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(storage, "connect", traced)
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(_mcp_app()) as client:
        for _ in range(3):
            res = client.post("/mcp", headers=auth)
            assert res.status_code == 200 and res.json()["episode"].startswith("run:sp-")
    writes = [
        s
        for s in statements
        if s.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    assert writes == []
    lifecycle_reads = [s for s in statements if "agent.lifecycle" in s or "$.kind" in s]
    assert len(lifecycle_reads) == 1, lifecycle_reads


def _signin_rows(label: str) -> list[str]:
    from unified_agent import oauth_store

    rows: list[str] = []
    for subject, suffix in ((label, "a"), ("Local_Bystander", "b")):
        refresh = f"refresh-{suffix}-{secrets.token_hex(8)}"
        oauth_store.save_refresh(
            refresh,
            client_id="unified-agent-cli",
            subject=subject,
            scopes=["mcp"],
            resource=None,
            expires_at=None,
            claims={"device_node": {"node": f"dev-{suffix}", "node_id": f"n{suffix}"}},
        )
        oauth_store.save_access(
            f"access-{suffix}-{secrets.token_hex(8)}",
            refresh_token=refresh,
            client_id="unified-agent-cli",
            subject=subject,
            scopes=["mcp"],
            resource=None,
            expires_at=None,
            claims={},
            issued_at=1,
        )
        rows.append(oauth_store.digest(refresh)[: oauth_store.SESSION_ID_LEN])
    return rows


def _signin_subjects() -> list[tuple[str, str]]:
    conn = storage.connect()
    try:
        return sorted(
            (table, r["subject"])
            for table in ("oauth_refresh_tokens", "oauth_access_tokens")
            for r in conn.execute(f"SELECT subject FROM {table}").fetchall()
        )
    finally:
        conn.close()


def test_retire_revokes_device_signins_in_its_transaction_and_unretire_keeps_them_revoked(
    monkeypatch,
):
    retiree_session, _bystander = _signin_rows(RETIREE)
    before = _signin_subjects()
    assert ("oauth_refresh_tokens", RETIREE) in before

    real_hook = agent_lifecycle._before_append

    def boom(conn, label, verb):
        raise RuntimeError("ledger append failed")

    monkeypatch.setattr(agent_lifecycle, "_before_append", boom)
    with _as(OP), pytest.raises(RuntimeError, match="ledger append failed"):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    # Rolled back as one: still active, every sign-in still there.
    assert agent_lifecycle.agent_state(RETIREE)["state"] == "active"
    assert _signin_subjects() == before
    monkeypatch.setattr(agent_lifecycle, "_before_append", real_hook)

    with _as(OP):
        res = agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    assert res["revoked"]["device_signins"] == [retiree_session]
    assert _signin_subjects() == [
        ("oauth_access_tokens", "Local_Bystander"),
        ("oauth_refresh_tokens", "Local_Bystander"),
    ]
    conn = storage.connect()
    try:
        (row,) = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?",
            (agent_lifecycle.OAUTH_REVOKED_CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    payload = agent_lifecycle._payload(row)
    assert payload["agents"] == [RETIREE] and payload["reason"] == "agent_retired"
    with _as(OP):
        agent_lifecycle.agent_unretire(RETIREE, "rehired")
    assert ("oauth_refresh_tokens", RETIREE) not in _signin_subjects()


def test_a_retired_owners_node_is_refused_at_owned_node():
    from unified_agent import node_registry

    node_queue.register("retiree-node", identity=RETIREE, facts={"backends": ["codex"]})
    node_queue.heartbeat("retiree-node", identity=RETIREE)
    with _as(OP):
        agent_lifecycle.agent_retire(RETIREE, "decommissioned")
    with pytest.raises(node_registry.NodeError, match="is retired") as beat:
        node_queue.heartbeat("retiree-node", identity=RETIREE)
    assert (beat.value.status, beat.value.code) == (403, "agent_retired")
    # A foreign identity learns nothing about the retirement.
    with pytest.raises(node_registry.NodeError, match="different agent"):
        node_queue.heartbeat("retiree-node", identity="Local_Peer")
    agent_lifecycle.join_refusal_writers()
    assert "node_registry.owned_node" in {e["site"] for e in _events(agent_lifecycle.KIND_REFUSED)}


# --- review of #2315: cookies, case variants, fail closed ------------------------


def test_a_retired_bearer_mints_no_cookie_and_its_old_cookie_is_refused(monkeypatch):
    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{RETIREE}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with TestClient(_mcp_app()) as client:
        login = client.post("/api/session", json={"token": token})
        assert login.status_code == 200, login.text
        assert client.get("/api/whoami").status_code == 200  # the agent-derived cookie
        with _as(OP):
            agent_lifecycle.agent_retire(RETIREE, "decommissioned")
        # The cookie minted before the retirement is held to the agent's lifecycle.
        res = client.get("/api/whoami")
        assert res.status_code == 403 and res.json()["code"] == "agent_retired"
        client.cookies.clear()
        refused = client.post("/api/session", json={"token": token})
        assert refused.status_code == 403 and refused.json()["code"] == "agent_retired"
        assert "set-cookie" not in refused.headers
    sites = {e["site"] for e in _events(agent_lifecycle.KIND_REFUSED)}
    assert {"hub.auth.cookie", "webui.session_create"} <= sites


def test_a_case_variant_bearer_is_the_same_identity_for_retirement(monkeypatch):
    from unified_agent import oauth_store

    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"desktop_agent2:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    oauth_store.save_refresh(
        f"refresh-variant-{secrets.token_hex(8)}",
        client_id="unified-agent-cli",
        subject="DESKTOP_Agent2",
        scopes=["mcp"],
        resource=None,
        expires_at=None,
        claims={},
    )
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(_mcp_app()) as client:
        # Auth resolves the variant to the catalog label.
        assert client.post("/mcp", headers=auth).json()["identity"] == "Desktop_Agent2"
        with _as(OP):
            res = agent_lifecycle.agent_retire("desktop_Agent2", "decommissioned")
        assert res["agent"] == "Desktop_Agent2" and len(res["revoked"]["device_signins"]) == 1
        refused = client.post("/mcp", headers=auth)
        assert refused.status_code == 403 and refused.json()["code"] == "agent_retired"
    assert agent_lifecycle.refused_label("desktop_agent2") is not None
    with pytest.raises(ToolError, match="is retired"):
        _claim("desktop_agent2")


def test_the_request_path_fails_closed_when_the_lifecycle_is_unreadable(monkeypatch):
    import sqlite3

    token = _fake_token()
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{RETIREE}:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    auth = {"Authorization": f"Bearer {token}"}
    real_state = agent_lifecycle.retired_state

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    def expire_cache():
        with agent_lifecycle._identity_lock:
            for key, (_at, state) in list(agent_lifecycle._identity_cache.items()):
                agent_lifecycle._identity_cache[key] = (-1e9, state)

    with TestClient(_mcp_app()) as client:
        agent_lifecycle.invalidate_identity_cache()
        monkeypatch.setattr(agent_lifecycle, "retired_state", broken)
        res = client.post("/mcp", headers=auth)  # nothing cached: refuse, temporarily
        assert res.status_code == 503 and res.json()["code"] == "lifecycle_unavailable"
        assert res.headers["retry-after"] == "5"

        monkeypatch.setattr(agent_lifecycle, "retired_state", real_state)
        assert client.post("/mcp", headers=auth).status_code == 200  # caches "active"
        monkeypatch.setattr(agent_lifecycle, "retired_state", broken)
        assert client.post("/mcp", headers=auth).status_code == 200  # within the TTL: no read
        expire_cache()
        res = client.post("/mcp", headers=auth)  # a stale "active" is not trusted
        assert res.status_code == 503 and res.json()["code"] == "lifecycle_unavailable"

        monkeypatch.setattr(agent_lifecycle, "retired_state", real_state)
        with _as(OP):
            agent_lifecycle.agent_retire(RETIREE, "decommissioned")
        assert client.post("/mcp", headers=auth).status_code == 403  # caches the retirement
        monkeypatch.setattr(agent_lifecycle, "retired_state", broken)
        expire_cache()
        res = client.post("/mcp", headers=auth)  # a known retirement survives the outage
        assert res.status_code == 403 and res.json()["code"] == "agent_retired"
