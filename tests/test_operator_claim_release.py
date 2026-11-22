"""Operator release of another agent's live claim (task #1700).

An ordinary agent may release only its own claim or an expired one
(``task_update(release_claim=True)``). A lifecycle operator
(``UA_LIFECYCLE_OPERATORS``) or the verified human operator in the Control
Center may release one task's live claim held by another agent, fenced on the
expected claimant (and optionally the run), with a required reason recorded on
the ledger in the same transaction as the release.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import claim_release, config, queue_hygiene, run_records, server, storage
from unified_agent.tools.graph import coordination_manage
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.webui.routes import web_routes

OPERATOR = "Desktop_Operator"
HOLDER = "Desktop_Agent1"
PEER = "Desktop_Agent2"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)


def _claimed() -> tuple[int, str]:
    tid = task_add("held work", lane="dispatch-ready")["id"]
    with _as(HOLDER):
        claimed = task_claim(tid, HOLDER, expected_lane="dispatch-ready", timeout_seconds=3600)
    return tid, claimed["current_run_id"]


def _row(tid: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()


def _ledger() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator FROM event_log WHERE channel=? ORDER BY ulid",
            (claim_release.CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    return [{**json.loads(r["payload"]), "_originator": r["originator"]} for r in rows]


def _release(tid: int, who: str = OPERATOR, **kw) -> dict:
    kw.setdefault("expected_claimant", HOLDER)
    kw.setdefault("reason", "handoff session hit its usage limit")
    with _as(who):
        return claim_release.task_claim_release(tid, **kw)


def test_an_ordinary_agent_still_cannot_release_another_live_claim():
    tid, _ = _claimed()
    with _as(PEER), pytest.raises(ToolError, match="active lease"):
        task_update(tid, release_claim=True)
    with pytest.raises(ToolError, match="operator"):
        _release(tid, who=PEER)
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def test_operator_releases_a_live_claim_with_a_ledger_record():
    tid, run_id = _claimed()
    out = _release(tid, expected_run_id=run_id)

    row = _row(tid)
    assert row["claimed_by"] is None and row["lock_expires_at"] is None
    assert row["status"] == "open"
    assert row["current_run_id"] is None
    assert out["task"]["status"] == "open"
    assert out["released"]["previous_claimant"] == HOLDER
    assert out["released"]["previous_run_id"] == run_id
    assert "handoff session hit its usage limit" in row["notes"]
    [event] = _ledger()
    assert event["kind"] == claim_release.KIND_RELEASED
    assert event["task_id"] == tid
    assert event["previous_claimant"] == HOLDER
    assert event["previous_run_id"] == run_id
    assert event["reason"] == "handoff session hit its usage limit"
    assert event["actor"] == OPERATOR and event["_originator"] == OPERATOR
    assert event["authority"] == "lifecycle_operator"
    assert event["status_before"] == "doing" and event["status_after"] == "open"
    assert out["released"]["event_ulid"]
    # The released run is closed, so the old holder cannot renew it ...
    run = run_records.get_run(run_id)
    assert run is not None and run.get("finished_at")
    with _as(HOLDER), pytest.raises(ToolError):
        task_claim(tid, HOLDER, expected_lane="dispatch-ready", expected_run_id=run_id)
    # ... queue hygiene has nothing to renew, and another agent can take it.
    conn = storage.connect()
    try:
        assert all(r["id"] != tid for r in queue_hygiene.renew_live_claims(conn))
    finally:
        conn.close()
    with _as(PEER):
        assert task_claim(tid, PEER, expected_lane="dispatch-ready")["claimed_by"] == PEER


def test_release_is_fenced_on_claimant_and_run():
    tid, run_id = _claimed()
    with pytest.raises(ToolError, match="claimed by 'Desktop_Agent1'"):
        _release(tid, expected_claimant=PEER)
    with pytest.raises(ToolError, match="current run"):
        _release(tid, expected_run_id="sp-not-the-current-run")
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []
    # An unclaimed task has nothing to release.
    _release(tid, expected_run_id=run_id)
    with pytest.raises(ToolError, match="no claim"):
        _release(tid)
    assert len(_ledger()) == 1


def test_release_requires_a_reason_and_an_identity():
    tid, _ = _claimed()
    with pytest.raises(ToolError, match="reason"):
        _release(tid, reason="   ")
    with pytest.raises(ToolError, match="authenticated"):
        claim_release.task_claim_release(tid, expected_claimant=HOLDER, reason="x")
    assert _row(tid)["claimed_by"] == HOLDER


def test_the_grant_does_not_leak_to_a_plain_task_update():
    # The in-transaction exemption is scoped to the release call itself.
    tid, _ = _claimed()
    _release(tid)
    other, _ = _claimed()
    with _as(OPERATOR), pytest.raises(ToolError, match="active lease"):
        task_update(other, release_claim=True)
    assert _row(other)["claimed_by"] == HOLDER


def test_a_non_doing_claim_keeps_its_status():
    tid, _ = _claimed()
    with _as(HOLDER):
        task_update(tid, status="blocked", worktree=False)
    assert _row(tid)["claimed_by"] == HOLDER
    out = _release(tid)
    assert out["task"]["status"] == "blocked"
    assert _row(tid)["claimed_by"] is None


def test_coordination_manage_exposes_the_release():
    tid, _ = _claimed()
    with _as(OPERATOR):
        out = coordination_manage(
            "task_claim_release",
            {"task_id": tid, "expected_claimant": HOLDER, "reason": "operator transfer"},
        )
    assert out["released"]["previous_claimant"] == HOLDER
    tid2, _ = _claimed()
    with _as(PEER), pytest.raises(ToolError, match="operator"):
        coordination_manage(
            "task_claim_release",
            {"task_id": tid2, "expected_claimant": HOLDER, "reason": "not mine"},
        )


# --- Control Center route -------------------------------------------------------------

TOKENS = {PEER: "fixture-peer-token", OPERATOR: "fixture-operator-token"}


@pytest.fixture
def http(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    for name in ("UA_AGENT_ACCESS_ENABLED", "UA_APPROVAL_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=web_routes())),
        base_url="http://127.0.0.1",
    )


def _bearer(identity: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[identity]}


def test_http_release_by_the_verified_operator(http, monkeypatch):
    # The verified human operator needs no lifecycle-operator entry.
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Someone_Else")
    tid, run_id = _claimed()
    refused = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(PEER),
        json={"expected_claimant": HOLDER, "reason": "take over", "expected_run_id": run_id},
    )
    assert refused.status_code == 403, refused.text
    assert _row(tid)["claimed_by"] == HOLDER
    bad = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "reason": "x", "extra": 1, "expected_run_id": run_id},
    )
    assert bad.status_code == 400
    stale = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": PEER, "reason": "wrong holder", "expected_run_id": run_id},
    )
    assert stale.status_code == 409, stale.text
    ok = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["released"]["authority"] == "human_operator"
    assert _row(tid)["claimed_by"] is None
    [event] = _ledger()
    assert event["actor"] == OPERATOR and event["reason"] == "UI release"


def test_http_release_by_the_operator_with_default_lifecycle_operators(http, monkeypatch):
    # PR #1146 review P1-3: with UA_LIFECYCLE_OPERATORS unset the operator label
    # is a lifecycle operator too, and that branch routed the human through the
    # agent tool surface, which the operator profile does not expose.
    monkeypatch.delenv("UA_LIFECYCLE_OPERATORS", raising=False)
    tid, run_id = _claimed()
    ok = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["released"]["authority"] == "human_operator"
    assert _row(tid)["claimed_by"] is None
    [event] = _ledger()
    assert event["authority"] == "human_operator"


def test_http_release_with_a_stale_run_is_a_conflict(http):
    tid, _ = _claimed()
    stale = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": "sp-stale", "reason": "x"},
    )
    assert stale.status_code == 409, stale.text
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def test_http_release_rate_limit_is_throttling_not_forbidden(http, monkeypatch):
    from unified_agent import policy, safety

    def throttled(*_args, **_kwargs):
        raise policy.PolicyViolation("rate limited", reason_code="rate_limited")

    monkeypatch.setattr(safety, "check_policy_gate", throttled)
    tid, run_id = _claimed()
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "x"},
    )
    assert response.status_code == 429, response.text
    assert _row(tid)["claimed_by"] == HOLDER


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("control", ["policy", "approval"])
def test_http_release_obeys_task_update_admission(http, monkeypatch, tmp_path, control, human):
    tid, run_id = _claimed()
    if human:
        # The verified operator, with no lifecycle-operator entry: the human
        # recovery path. (Naming the operator in UA_AGENT_LABELS would make it
        # an agent label and silently run the lifecycle path instead.)
        monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Someone_Else")
    else:
        # A lifecycle operator is an agent, not the human recovery identity.
        monkeypatch.setenv("UA_OPERATOR_LABEL", "Human_Operator")
        monkeypatch.setenv("UA_AGENT_LABELS", f"{OPERATOR}:{HOLDER}:{PEER}")
    authorities: list[str] = []
    real_authority = claim_release._authority

    def spy(who: str) -> str:
        authorities.append(real_authority(who))
        return authorities[-1]

    monkeypatch.setattr(claim_release, "_authority", spy)
    if control == "policy":
        policy = tmp_path / "policy.yaml"
        policy.write_text(
            "rules:\n  - id: refuse-release\n    tool: task_update\n    action: deny\n"
        )
    else:
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_update")
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
    )
    assert response.status_code == (202 if control == "approval" else 403), response.text
    assert authorities == ["human_operator" if human else "lifecycle_operator"]
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []
    if control == "approval":
        # Approval payloads are stable despite the time between UI retries.
        again = http.post(
            f"/api/tasks/{tid}/claim-release",
            headers=_bearer(OPERATOR),
            json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
        )
        assert again.json()["approval_id"] == response.json()["approval_id"]
        from unified_agent.tools.approvals import approval_resolve, get_approval

        if human:
            # The human requested it, so the same human cannot approve it
            # (separation of duties); a second approver is needed.
            with _as(config.operator_label()), pytest.raises(ToolError, match="separation"):
                approval_resolve(response.json()["approval_id"], "approve")
            assert _row(tid)["claimed_by"] == HOLDER
            return
        with _as(config.operator_label()):
            approval_resolve(response.json()["approval_id"], "approve")
        completed = http.post(
            f"/api/tasks/{tid}/claim-release",
            headers=_bearer(OPERATOR),
            json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
        )
        assert completed.status_code == 200, completed.text
        assert _row(tid)["claimed_by"] is None
        assert len(_ledger()) == 1
        assert get_approval(response.json()["approval_id"])["status"] == "consumed"


def test_lifecycle_release_cannot_bypass_access_lockdown(monkeypatch):
    tid, _ = _claimed()
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    with pytest.raises(ToolError, match="read-only"):
        _release(tid)
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


@pytest.mark.parametrize("tool", ["task_claim_release", "coordination_manage"])
@pytest.mark.parametrize("human", [False, True])
def test_http_release_obeys_a_deny_rule_on_the_release_action(
    http, tmp_path, monkeypatch, tool, human
):
    # PR #1146 review round 4: the route called the implementation directly,
    # so only task_update policy applied; a deny rule on the consolidated
    # action (as the MCP twin checks it) was bypassed.
    if not human:
        monkeypatch.setenv("UA_OPERATOR_LABEL", "Human_Operator")
        monkeypatch.setenv("UA_AGENT_LABELS", f"{OPERATOR}:{HOLDER}:{PEER}")
    tid, run_id = _claimed()
    (tmp_path / "policy.yaml").write_text(
        f"rules:\n  - id: refuse-release\n    tool: {tool}\n    action: deny\n"
    )
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "UI release"},
    )
    assert response.status_code == 403, response.text
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


# --- Task #2003 / #2019 follow-ups ------------------------------------------------------


def _between_check_and_write(monkeypatch, change):
    """Run ``change`` once, after the release's up-front read, before its write."""
    from unified_agent.tools import tasks as tasks_mod

    real = tasks_mod._visible_task
    fired = []

    def hooked(conn, task_id, *args, **kwargs):
        row = real(conn, task_id, *args, **kwargs)
        if not fired:
            fired.append(True)
            change()
        return row

    monkeypatch.setattr(tasks_mod, "_visible_task", hooked)


def _rotate_run(tid: int) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET current_run_id='sp-rotated' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()


def test_a_run_that_rotates_before_the_write_is_a_conflict(monkeypatch):
    # #2003 (1) / #2019 (5): task_update's own run fence raised a plain ToolError.
    tid, run_id = _claimed()
    _between_check_and_write(monkeypatch, lambda: _rotate_run(tid))
    with pytest.raises(claim_release.ClaimReleaseConflict, match="current run"):
        _release(tid, expected_run_id=run_id)
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def test_http_run_rotation_before_the_write_is_409(http, monkeypatch):
    tid, run_id = _claimed()
    _between_check_and_write(monkeypatch, lambda: _rotate_run(tid))
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={"expected_claimant": HOLDER, "expected_run_id": run_id, "reason": "x"},
    )
    assert response.status_code == 409, response.text
    assert _row(tid)["claimed_by"] == HOLDER


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_terminal_task_keeps_its_claim_attribution(status):
    # #2003 (2): a terminal task has nothing to transfer.
    tid, _ = _claimed()
    with _as(HOLDER):
        task_update(tid, status=status, worktree=False, evidence_ref="test://evidence")
    assert _row(tid)["claimed_by"] == HOLDER
    with pytest.raises(claim_release.ClaimReleaseConflict, match=status):
        _release(tid)
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def test_an_unfenced_release_loses_to_a_drop_and_reclaim(monkeypatch):
    # #2003 (3): same claimant, same status, new claim generation.
    tid, run_id = _claimed()

    def reclaim():
        with _as(HOLDER):
            task_update(tid, release_claim=True, status="open", worktree=False)
            task_claim(tid, HOLDER, expected_lane="dispatch-ready", timeout_seconds=3600)

    _between_check_and_write(monkeypatch, reclaim)
    with pytest.raises(claim_release.ClaimReleaseConflict, match="re-claimed"):
        _release(tid)
    row = _row(tid)
    assert row["claimed_by"] == HOLDER and row["status"] == "doing"
    assert row["current_run_id"] != run_id
    assert _ledger() == []


def test_http_release_requires_expected_run_id(http):
    # #2019 (4): a replayed unfenced request could release a re-acquired claim.
    tid, _ = _claimed()
    for body in (
        {"expected_claimant": HOLDER, "reason": "x"},
        {"expected_claimant": HOLDER, "reason": "x", "expected_run_id": ""},
    ):
        response = http.post(
            f"/api/tasks/{tid}/claim-release", headers=_bearer(OPERATOR), json=body
        )
        assert response.status_code == 400, response.text
        assert "expected_run_id" in response.json()["error"]
    assert _row(tid)["claimed_by"] == HOLDER


def test_mcp_release_keeps_expected_run_id_optional():
    tid, _ = _claimed()
    assert _release(tid)["released"]["previous_claimant"] == HOLDER


def test_a_live_dispatch_run_must_be_cancelled_first():
    # #2019 (6): release does not stop an executor; refuse while one is live.
    tid, run_id = _claimed()
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("dr-live", tid, HOLDER, "running", "coordinator", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(claim_release.ClaimReleaseConflict, match="dispatch_cancel"):
        _release(tid, expected_run_id=run_id)
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def _legacy_claim_without_run():
    tid = task_add("legacy held work", lane="dispatch-ready")["id"]
    generation = "2026-10-14T01:02:03.123456+00:00"
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing', claimed_by=?, claimed_at=?, lock_expires_at=? "
            "WHERE id=?",
            (HOLDER, generation, "2099-01-01T00:00:00+00:00", tid),
        )
        conn.commit()
    finally:
        conn.close()
    return tid, generation


def test_http_releases_no_run_claim_with_explicit_claim_generation(http):
    tid, generation = _legacy_claim_without_run()
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={
            "expected_claimant": HOLDER,
            "reason": "recover legacy claim",
            "expected_run_id": None,
            "expected_claimed_at": generation,
        },
    )
    assert response.status_code == 200, response.text
    assert _row(tid)["status"] == "open" and _row(tid)["claimed_by"] is None
    assert _ledger()[-1]["previous_run_id"] is None


@pytest.mark.parametrize("generation", [None, "", 5])
def test_http_no_run_release_requires_a_claim_generation(http, generation):
    tid, _ = _legacy_claim_without_run()
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={
            "expected_claimant": HOLDER,
            "reason": "recover",
            "expected_run_id": None,
            "expected_claimed_at": generation,
        },
    )
    assert response.status_code == 400, response.text
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


@pytest.mark.parametrize("change", ["generation", "run"])
def test_http_no_run_release_refuses_replay_after_claim_changes(http, change):
    tid, generation = _legacy_claim_without_run()
    conn = storage.connect()
    try:
        if change == "generation":
            conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (generation + "-new", tid))
        else:
            conn.execute("UPDATE tasks SET current_run_id='sp-new-run' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={
            "expected_claimant": HOLDER,
            "reason": "recover",
            "expected_run_id": None,
            "expected_claimed_at": generation,
        },
    )
    assert response.status_code == 409, response.text
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []


def test_http_no_run_release_rechecks_absent_run_inside_transaction(http, monkeypatch):
    tid, generation = _legacy_claim_without_run()
    _between_check_and_write(monkeypatch, lambda: _rotate_run(tid))
    response = http.post(
        f"/api/tasks/{tid}/claim-release",
        headers=_bearer(OPERATOR),
        json={
            "expected_claimant": HOLDER,
            "reason": "recover",
            "expected_run_id": None,
            "expected_claimed_at": generation,
        },
    )
    assert response.status_code == 409, response.text
    assert _row(tid)["claimed_by"] == HOLDER
    assert _ledger() == []
