"""Task subtree holds: pause, cancel, resume and restore a subtree (task #1747).

A hold snapshots a root's subtree. While it is active, new claims, dispatch and node
dispatch on a member are refused; a live renewal of an in-flight run is not.
``cancel`` reuses ``task_update(request_cancel=True)`` and ``node_queue.cancel``;
``restore`` undoes only the hold's own requests, waits for a cancelled run to close,
and never revives a task that was terminal before the hold or that its claimant
cancelled on its own. Only the creator or an operator may release.
``after_active_runs_finish`` (pause only, needs a live run) releases itself once no
member has a live run. Reads and writes are fenced to the caller's tenant.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_dispatch_coordinator import _spec
from test_executor_commit_fencing import wire_canonical_rpc_peer

from unified_agent import config, lease_reaper, profiles, run_records, server, storage, task_holds
from unified_agent.dispatch import coordinator
from unified_agent.executor.daemon import ExecutorDaemon
from unified_agent.tools import delegate
from unified_agent.tools.graph import coordination_manage
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.webui.routes import web_routes

OPERATOR = "Desktop_Operator"
DEV = "Desktop_Agent2"
OTHER = "Desktop_Agent1"


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


def _task(title: str = "t", parent: int | None = None) -> int:
    return task_add(title, lane="dispatch-ready", parent_id=parent)["id"]


def _claim(agent: str, tid: int) -> dict:
    with _as(agent):
        return task_claim(tid, agent, expected_lane="dispatch-ready")


def _tree() -> tuple[int, int, int]:
    root = _task("epic")
    child = _task("child", root)
    grandchild = _task("grandchild", child)
    return root, child, grandchild


def _hold(root: int, mode: str = "pause", who: str = OPERATOR, **kw) -> dict:
    with _as(who):
        return task_holds.task_hold(root, mode=mode, reason=kw.pop("reason", "stop"), **kw)


def _row(tid: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()


def _exec(sql: str, params: tuple = ()) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _executor_ack(tid: int, run_id: str) -> dict:
    """The executor's acknowledgement of a cancel request: a fenced cancel of its run."""
    with _as(DEV):
        return task_update(
            tid,
            status="cancelled",
            release_claim=True,
            worktree=False,
            expected_run_id=run_id,
            append_note="[executor acknowledged run cancellation]",
        )


def _retryable_run(tid: int, delegation_id: int, run_id: str = "run-held") -> None:
    """A dispatch run ``dispatch_resume`` could relaunch."""
    stamp = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, actor, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'retryable', ?, ?, ?)",
        (run_id, tid, delegation_id, OTHER, OTHER, stamp, stamp),
    )


def _hold_events(action: str | None = None) -> list[dict]:
    import json

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ulid", (task_holds.CHANNEL,)
        ).fetchall()
    finally:
        conn.close()
    payloads = [json.loads(r["payload"]) for r in rows]
    return [p for p in payloads if action is None or p["action"] == action]


# --- refusal -------------------------------------------------------------------------


def test_pause_refuses_new_claims_on_every_member_naming_the_hold():
    root, child, grandchild = _tree()
    hold = _hold(root)["hold"]
    assert hold["status"] == "active" and hold["mode"] == "pause"
    assert hold["member_ids"] == sorted([root, child, grandchild])
    for tid in (root, child, grandchild):
        with pytest.raises(ToolError, match=rf"held by hold #{hold['id']} on task #{root}"):
            _claim(DEV, tid)
        assert _row(tid)["status"] == "open"
    [event] = _hold_events("pause")
    assert event["hold_id"] == hold["id"] and event["actor"] == OPERATOR


def test_self_pickup_entering_doing_is_refused_while_held():
    root, child, _ = _tree()
    _hold(root)
    with _as(DEV), pytest.raises(ToolError, match="held by hold"):
        task_update(child, status="doing", worktree=False)
    assert _row(child)["status"] == "open"


def test_live_renewal_is_allowed_so_an_in_flight_run_can_finish():
    root, child, _ = _tree()
    first = _claim(DEV, child)
    _hold(root)
    renewed = _claim(DEV, child)
    assert renewed["current_run_id"] == first["current_run_id"]
    with _as(DEV):
        done = task_update(child, status="blocked", append_note="finished under hold")
    assert done["status"] == "blocked"
    # Re-entering doing is a new claim, refused while held.
    with pytest.raises(ToolError, match="held by hold"):
        _claim(DEV, child)


def test_a_takeover_of_a_lapsed_lease_is_refused_while_held():
    root, child, _ = _tree()
    _claim(DEV, child)
    _exec("UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (child,))
    _hold(root)
    with pytest.raises(ToolError, match="held by hold"):
        _claim(OTHER, child)
    with pytest.raises(ToolError, match="held by hold"):
        _claim(DEV, child)


def test_a_task_added_after_the_hold_is_not_held():
    root, child, _ = _tree()
    _hold(root)
    late = _task("added later", child)
    assert _claim(DEV, late)["status"] == "doing"


def test_dispatch_readiness_refuses_a_held_member():
    root = _task("epic")
    child = delegate.delegate(
        task_spec=_spec("held dispatch"),
        from_agent=DEV,
        to_agent=OTHER,
        callback="none",
        parent_task_id=root,
    )
    assert coordinator.evaluate_ready(task_id=child["task_id"])["reason"] != "held"
    _retryable_run(child["task_id"], child["delegation_id"])
    assert coordinator.resume_run("run-held", actor=DEV)["reason"] != "held"
    _hold(root)
    verdict = coordinator.evaluate_ready(task_id=child["task_id"])
    assert verdict["ready"] is False and verdict["reason"] == "held"
    assert "held by hold" in verdict["detail"]
    resumed = coordinator.resume_run("run-held", actor=DEV)
    assert (resumed["ok"], resumed["reason"]) == (False, "held")
    assert "held by hold" in resumed["detail"]


def test_resume_releases_the_hold_and_claims_work_again():
    root, child, _ = _tree()
    hold = _hold(root)["hold"]
    released = _hold(root, mode="resume", reason="")
    assert released["hold"]["status"] == "released"
    assert released["hold"]["released_by"] == OPERATOR
    assert _claim(DEV, child)["status"] == "doing"
    assert [e["hold_id"] for e in _hold_events("resume")] == [hold["id"]]
    with pytest.raises(ToolError, match="no active hold"):
        _hold(root, mode="resume")


# --- idempotency ---------------------------------------------------------------------


def test_re_holding_a_held_root_returns_the_existing_hold():
    root, _, _ = _tree()
    first = _hold(root)
    again = _hold(root, reason="a second reason")
    assert again["changed"] is False
    assert again["hold"]["id"] == first["hold"]["id"]
    assert again["hold"]["reason"] == "stop"
    assert len(_hold_events("pause")) == 1
    assert len(task_holds.task_holds(root_task_id=root)["holds"]) == 1


def test_pause_requires_a_reason_and_a_known_mode_and_policy():
    root, _, _ = _tree()
    with _as(OPERATOR):
        with pytest.raises(ToolError, match="reason"):
            task_holds.task_hold(root, mode="pause", reason=" ")
        with pytest.raises(ToolError, match="mode"):
            task_holds.task_hold(root, mode="freeze", reason="x")
        with pytest.raises(ToolError, match="release_policy"):
            task_holds.task_hold(root, reason="x", release_policy="eventually")
        with pytest.raises(ToolError, match="no task with id"):
            task_holds.task_hold(999999, reason="x")


# --- release policy ------------------------------------------------------------------


def test_after_active_runs_finish_releases_once_no_member_has_a_live_run():
    root, child, grandchild = _tree()
    _claim(DEV, child)
    hold = _hold(root, release_policy="after_active_runs_finish")["hold"]
    assert hold["live_run_task_ids"] == [child]
    with pytest.raises(ToolError, match="held by hold"):
        _claim(OTHER, grandchild)
    assert task_holds.task_hold_get(hold["id"])["status"] == "active"
    with _as(DEV):
        task_update(child, status="blocked", append_note="run finished")
    # The claim meets the due hold, releases it and goes through.
    assert _claim(OTHER, grandchild)["status"] == "doing"
    after = task_holds.task_hold_get(hold["id"])
    assert after["status"] == "released"
    assert after["released_by"] == task_holds.AUTO_RELEASE_ACTOR
    [event] = _hold_events("auto_release")
    assert event["hold_id"] == hold["id"]


def test_after_active_runs_finish_is_also_evaluated_on_read():
    root, child, _ = _tree()
    _claim(DEV, child)
    hold = _hold(root, release_policy="after_active_runs_finish")["hold"]
    assert [h["id"] for h in task_holds.task_holds()["holds"]] == [hold["id"]]
    with _as(DEV):
        task_update(child, status="blocked", append_note="run finished")
    assert task_holds.task_holds()["holds"] == []
    assert task_holds.task_hold_get(hold["id"])["status"] == "released"
    assert len(_hold_events("auto_release")) == 1


def test_manual_hold_does_not_release_itself():
    root, _, _ = _tree()
    hold = _hold(root)["hold"]
    assert [h["id"] for h in task_holds.task_holds()["holds"]] == [hold["id"]]


# --- cancel and restore --------------------------------------------------------------


def test_cancel_requests_cancel_on_each_members_live_run():
    root, child, grandchild = _tree()
    run = _claim(DEV, child)["current_run_id"]
    result = _hold(root, mode="cancel", reason="wrong direction")
    assert result["cancel_requests"] == [
        {"task_id": child, "path": "run", "ref": run, "control": "requested", "detail": ""}
    ]
    assert _row(child)["cancel_requested"] == 1
    assert run_records.get_run(run)["cancel_requested"] is True
    assert run_records.get_run(run)["cancel_requested_by"] == OPERATOR
    member = {m["task_id"]: m for m in result["hold"]["members"]}
    assert member[child]["cancel_requested_by_hold"] is True
    assert member[child]["cancelled_run_id"] == run
    assert member[grandchild]["cancel_requested_by_hold"] is False
    assert result["hold"]["cancelling"] is False
    # The cancelled run can no longer renew its lease.
    with pytest.raises(ToolError, match="cancellation request"):
        _claim(DEV, child)
    assert [e["action"] for e in _hold_events()] == ["cancel", "cancel_requested"]


def test_cancel_keeps_the_existing_authority_rule_and_reports_refusals():
    root, child, _ = _tree()
    _claim(DEV, child)
    result = _hold(root, mode="cancel", who=OTHER, reason="not mine to stop")
    [outcome] = result["cancel_requests"]
    assert outcome["control"] == "refused" and "operator action" in outcome["detail"]
    assert _row(child)["cancel_requested"] == 0
    assert result["hold"]["status"] == "active"


def test_cancel_escalates_an_active_pause_hold():
    root, child, _ = _tree()
    _claim(DEV, child)
    paused = _hold(root)["hold"]
    escalated = _hold(root, mode="cancel")
    assert escalated["changed"] is True
    assert escalated["hold"]["id"] == paused["id"]
    assert escalated["hold"]["mode"] == "cancel"
    assert _row(child)["cancel_requested"] == 1


def test_restore_waits_for_open_runs_and_never_revives_tasks_terminal_before_the_hold():
    root = _task("epic")
    done_before, cancelled_before = _task("done", root), _task("dropped", root)
    running, acknowledged = _task("running", root), _task("acknowledged", root)
    with _as(DEV):
        task_update(done_before, status="done", worktree=False)
        task_update(cancelled_before, status="cancelled", worktree=False)
    run = _claim(DEV, running)["current_run_id"]
    ack_run = _claim(DEV, acknowledged)["current_run_id"]
    hold = _hold(root, mode="cancel")["hold"]
    flagged = {m["task_id"] for m in hold["members"] if m["cancel_requested_by_hold"]}
    assert flagged == {running, acknowledged}
    # One executor acknowledges the cancellation (the fenced cancel); the other run is
    # still open.
    _executor_ack(acknowledged, ack_run)
    # Even a member row claiming the hold flagged a terminal task is never revived.
    _exec(
        "UPDATE task_hold_members SET cancel_requested_by_hold=1 WHERE hold_id=? AND task_id IN "
        "(?, ?)",
        (hold["id"], done_before, cancelled_before),
    )
    first = _hold(root, mode="restore", reason="back on track")
    controls = {r["task_id"]: r["control"] for r in first["restored"]}
    assert controls == {running: "pending", acknowledged: "reopened"}
    assert first["released"] is False and first["pending"] == [running]
    assert first["hold"]["status"] == "active"
    # The open run is left alone: the row and the run ledger keep the request.
    assert _row(running)["cancel_requested"] == 1
    assert run_records.get_run(run)["cancel_requested"] is True
    reopened = _row(acknowledged)
    assert reopened["status"] == "open" and reopened["claimed_by"] is None
    assert f"[hold #{hold['id']} restored" in reopened["notes"]
    assert [e["action"] for e in _hold_events()][-1] == "restore_pending"
    # The run stops; a second restore finishes the job and releases.
    with _as(DEV):
        task_update(running, status="blocked", append_note="stopped on cancel request")
    second = _hold(root, mode="restore", reason="back on track")
    controls = {r["task_id"]: r["control"] for r in second["restored"]}
    assert controls == {running: "unchanged", acknowledged: "restored"}
    assert second["released"] is True and second["pending"] == []
    assert second["hold"]["status"] == "released"
    assert _row(running)["cancel_requested"] == 0
    assert _row(done_before)["status"] == "done"
    assert _row(cancelled_before)["status"] == "cancelled"
    [event] = _hold_events("restore")
    assert {r["task_id"] for r in event["restored"]} == {running, acknowledged}
    assert _claim(DEV, acknowledged)["status"] == "doing"


def test_restore_after_a_pause_only_releases():
    root, child, _ = _tree()
    _claim(DEV, child)
    _hold(root)
    result = _hold(root, mode="restore", reason="")
    assert "restored" not in result
    assert result["hold"]["status"] == "released"
    assert _row(child)["status"] == "doing"


# --- review regressions: cancel and restore -------------------------------------------


def test_restore_leaves_a_cancel_request_the_hold_did_not_make():
    root, child, _ = _tree()
    run = _claim(DEV, child)["current_run_id"]
    with _as(OPERATOR):
        task_update(child, request_cancel=True, worktree=False)  # before the hold
    result = _hold(root, mode="cancel")
    [outcome] = result["cancel_requests"]
    assert outcome["control"] == "already_requested"
    member = {m["task_id"]: m for m in result["hold"]["members"]}
    assert member[child]["cancel_requested_by_hold"] is False
    released = _hold(root, mode="restore")
    assert "restored" not in released and released["hold"]["status"] == "released"
    assert _row(child)["cancel_requested"] == 1
    assert run_records.get_run(run)["cancel_requested"] is True


def test_restore_clearing_a_cancel_request_advances_the_revision():
    """Task #2087: restore cleared the hold's cancel request without a revision
    bump, so a caller holding the revision read while the request stood passed
    expected_updated_at after the flag changed underneath it."""
    from unified_agent.tool_errors import ConflictError

    root, child, _ = _tree()
    run = _claim(DEV, child)["current_run_id"]
    hold = _hold(root, mode="cancel")["hold"]
    with _as(DEV):
        task_update(child, status="blocked", append_note="stopped on cancel request")
    # The run is closed; leave the flag standing as a row without a bound run
    # holds it (the finish normally clears it alongside the run cache).
    _exec("UPDATE tasks SET cancel_requested=1 WHERE id=?", (child,))
    conn = storage.connect()
    try:
        assert run_records.finish_event_on(conn, run) is not None
    finally:
        conn.close()
    held = _row(child)
    out = _hold(root, mode="restore")
    assert {r["task_id"]: r["control"] for r in out["restored"]} == {child: "cancel_cleared"}
    assert out["hold"]["id"] == hold["id"]
    after = _row(child)
    assert after["cancel_requested"] == 0
    assert after["updated_at"] > held["updated_at"]
    with _as(DEV), pytest.raises(ConflictError):
        task_update(child, append_note="stale writer", expected_updated_at=held["updated_at"])


def test_restore_while_the_cancelled_run_is_open_is_pending_and_keeps_the_hold():
    root, child, grandchild = _tree()
    run = _claim(DEV, child)["current_run_id"]
    _hold(root, mode="cancel")
    out = _hold(root, mode="restore")
    assert out["restored"] == [
        {
            "task_id": child,
            "run_id": run,
            "control": "pending",
            "detail": "run still stopping; restore again once it closes",
        }
    ]
    assert out["released"] is False and out["hold"]["status"] == "active"
    assert _row(child)["cancel_requested"] == 1
    assert run_records.get_run(run)["cancel_requested"] is True
    # The claimant still cannot renew: the request stands until the run closes.
    with pytest.raises(ToolError, match="cancellation request"):
        _claim(DEV, child)
    with pytest.raises(ToolError, match="held by hold"):
        _claim(OTHER, grandchild)


def test_restore_keeps_a_task_its_claimant_cancelled_on_its_own():
    root, child, _ = _tree()
    _claim(DEV, child)
    _hold(root, mode="cancel")
    with _as(DEV):  # the claimant decides independently, without the run fence
        task_update(child, status="cancelled", append_note="abandon: superseded", worktree=False)
    out = _hold(root, mode="restore")
    assert [r["control"] for r in out["restored"]] == ["unchanged"]
    assert out["hold"]["status"] == "released"
    assert _row(child)["status"] == "cancelled"


def test_a_third_partys_fenced_cancel_is_not_an_acknowledgement():
    # Only the run's own agent acknowledges; anyone else's fenced cancel is a
    # cancel of its own, and restore must not reopen it.
    root, child, _ = _tree()
    run = _claim(DEV, child)["current_run_id"]
    _hold(root, mode="cancel")
    with _as(OTHER):
        task_update(
            child,
            status="cancelled",
            expected_run_id=run,
            append_note="dropped: duplicate",
            worktree=False,
        )
    out = _hold(root, mode="restore")
    assert [r["control"] for r in out["restored"]] == ["unchanged"]
    assert _row(child)["status"] == "cancelled"


def test_restore_keeps_a_task_whose_status_changed_after_the_acknowledgement():
    root, child, _ = _tree()
    run = _claim(DEV, child)["current_run_id"]
    _hold(root, mode="cancel")
    _executor_ack(child, run)
    with _as(OPERATOR):  # cancelled again for another reason after the acknowledgement
        task_update(child, status="open", worktree=False)
        task_update(child, status="cancelled", append_note="dropped from sprint", worktree=False)
    out = _hold(root, mode="restore")
    assert [r["control"] for r in out["restored"]] == ["unchanged"]
    assert _row(child)["status"] == "cancelled"


def test_after_active_runs_finish_is_refused_with_cancel():
    root, child, _ = _tree()
    _claim(DEV, child)
    with pytest.raises(ToolError, match="cannot be combined with cancel"):
        _hold(root, mode="cancel", release_policy="after_active_runs_finish")
    assert task_holds.task_holds(status="all")["holds"] == []
    # Escalating an auto-releasing pause hold to cancel is refused the same way.
    paused = _hold(root, release_policy="after_active_runs_finish")["hold"]
    with pytest.raises(ToolError, match="cannot be combined with cancel"):
        _hold(root, mode="cancel")
    assert task_holds.task_hold_get(paused["id"])["mode"] == "pause"
    assert _row(child)["cancel_requested"] == 0


def test_after_active_runs_finish_needs_a_live_run_in_the_subtree():
    root, child, _ = _tree()
    with pytest.raises(ToolError, match="nothing is running"):
        _hold(root, release_policy="after_active_runs_finish")
    assert task_holds.task_holds(status="all")["holds"] == []
    assert _hold_events() == []
    assert _claim(DEV, child)["status"] == "doing"


def test_restore_refuses_while_the_cancel_is_still_recording():
    root, child, _ = _tree()
    _claim(DEV, child)
    hold = _hold(root, mode="cancel")["hold"]
    _exec("UPDATE task_holds SET cancelling_at=? WHERE id=?", (storage.now(), hold["id"]))
    assert task_holds.task_hold_get(hold["id"])["cancelling"] is True
    with pytest.raises(ToolError, match="still recording its cancel requests"):
        _hold(root, mode="restore")
    # Resume is refused the same way: releasing mid fan-out would strand
    # cancel requests on a released hold.
    with pytest.raises(ToolError, match="still recording its cancel requests"):
        _hold(root, mode="resume")
    # A stale mark (an interrupted cancel) does not wedge restore.
    _exec(
        "UPDATE task_holds SET cancelling_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (hold["id"],),
    )
    assert _hold(root, mode="restore")["hold"]["status"] == "active"  # run still open


def test_release_names_the_hold_it_means():
    root, _, _ = _tree()
    hold = _hold(root)["hold"]
    with pytest.raises(ToolError, match=f"hold #{hold['id'] + 1} is not the active hold"):
        _hold(root, mode="resume", hold_id=hold["id"] + 1)
    assert task_holds.task_hold_get(hold["id"])["status"] == "active"
    assert _hold(root, mode="resume", hold_id=hold["id"])["hold"]["status"] == "released"


# --- review regressions: who may release ---------------------------------------------


def test_only_the_creator_or_an_operator_may_release():
    root, child, _ = _tree()
    _claim(DEV, child)
    _hold(root, mode="cancel")  # the operator's hold
    with pytest.raises(task_holds.HoldAuthorityError, match="only its creator"):
        _hold(root, mode="restore", who=DEV)
    with pytest.raises(task_holds.HoldAuthorityError, match="only its creator"):
        _hold(root, mode="resume", who=DEV)
    assert _row(child)["cancel_requested"] == 1
    assert task_holds.task_holds(root_task_id=root)["holds"][0]["status"] == "active"
    # The operator may release its own, or anyone's, hold.
    assert _hold(root, mode="resume")["hold"]["released_by"] == OPERATOR
    root2, _, _ = _tree()
    _hold(root2, who=DEV)
    with pytest.raises(task_holds.HoldAuthorityError):
        _hold(root2, mode="resume", who=OTHER)
    assert _hold(root2, mode="resume", who=DEV)["hold"]["released_by"] == DEV
    root3, _, _ = _tree()
    _hold(root3, who=DEV)
    assert _hold(root3, mode="resume")["hold"]["released_by"] == OPERATOR


def test_restore_undoes_only_the_cancels_the_caller_may_undo():
    root, child, _ = _tree()
    run = _claim(OTHER, child)["current_run_id"]
    _hold(root, who=DEV)  # the developer's pause hold
    escalated = _hold(root, mode="cancel")  # the operator stops OTHER's run
    assert [o["control"] for o in escalated["cancel_requests"]] == ["requested"]
    with _as(OTHER):
        task_update(child, status="blocked", append_note="stopped")
    out = _hold(root, mode="restore", who=DEV)  # the creator, not the run's claimant
    assert [r["control"] for r in out["restored"]] == ["refused"]
    assert out["released"] is False and out["hold"]["status"] == "active"
    out = _hold(root, mode="restore")
    assert [(r["run_id"], r["control"]) for r in out["restored"]] == [(run, "unchanged")]
    assert out["hold"]["status"] == "released"


# --- review regressions: leases, publish, node dispatch ------------------------------


def test_a_claim_without_a_lease_expiry_does_not_renew_or_count_as_live_while_held():
    root, child, grandchild = _tree()
    _claim(DEV, child)
    _exec("UPDATE tasks SET lock_expires_at=NULL WHERE id=?", (child,))
    with pytest.raises(ToolError, match="nothing is running"):
        _hold(root, release_policy="after_active_runs_finish")
    hold = _hold(root)["hold"]
    assert hold["live_run_task_ids"] == []
    with pytest.raises(ToolError, match="held by hold"):
        _claim(DEV, child)


def test_an_auto_release_inside_a_claim_is_published_after_commit(monkeypatch):
    root, child, grandchild = _tree()
    _claim(DEV, child)
    hold = _hold(root, release_policy="after_active_runs_finish")["hold"]
    with _as(DEV):
        task_update(child, status="blocked", append_note="run finished")
    published: list[dict] = []
    monkeypatch.setattr(task_holds.events, "publish", lambda event: published.append(event))
    assert _claim(OTHER, grandchild)["status"] == "doing"
    payloads = [e.get("payload") or {} for e in published]
    assert any(
        p.get("action") == "auto_release" and p.get("hold_id") == hold["id"] for p in payloads
    ), published


def test_claim_block_hands_an_in_transaction_release_to_the_caller(monkeypatch):
    root, child, grandchild = _tree()
    _claim(DEV, child)
    hold = _hold(root, release_policy="after_active_runs_finish")["hold"]
    _exec("UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (child,))
    published: list[dict] = []
    monkeypatch.setattr(task_holds.events, "publish", lambda event: published.append(event))
    pending: list = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert task_holds.claim_block(conn, grandchild, pending=pending) is None
        assert published == []  # nothing goes out before the caller commits
        conn.commit()
    finally:
        conn.close()
    task_holds.publish(pending)
    assert len(published) == 1
    assert task_holds.task_hold_get(hold["id"])["status"] == "released"


NODE = "hold-node"
NODE_ROOT = "/srv/dev"


def _node() -> None:
    from unified_agent import node_queue

    node_queue.register(
        NODE,
        identity=DEV,
        facts={
            "os": {"system": "Linux"},
            "backends": ["codex"],
            "worktree_roots": [{"path": NODE_ROOT, "exists": True, "worktrees": 1}],
        },
    )


def _node_item(tid: int) -> dict:
    from unified_agent import node_queue

    return node_queue.enqueue(
        NODE, prompt="p", cwd=f"{NODE_ROOT}/repo", backend="codex", created_by=DEV, task_id=tid
    )


def test_node_dispatch_refuses_a_held_member_before_the_approval_gate(monkeypatch):
    from unified_agent import node_queue, safety

    root, child, _ = _tree()
    _node()
    _hold(root)

    def gate(*_a, **_k):
        raise AssertionError("the approval gate must not be reached for a held task")

    monkeypatch.setattr(safety, "check_prod_action_gate", gate)
    with pytest.raises(node_queue.NodeError, match="held by hold") as exc:
        node_queue.request_dispatch(
            NODE, actor=DEV, prompt="p", cwd=f"{NODE_ROOT}/repo", backend="codex", task_id=child
        )
    assert exc.value.status == 409
    with pytest.raises(node_queue.NodeError, match="held by hold"):
        _node_item(child)


def test_a_held_task_is_not_claimed_from_the_queue_and_waits_for_it():
    from unified_agent import node_queue

    root, child, _ = _tree()
    _node()
    item = _node_item(child)
    _hold(root)

    # Task #2493: the item was queued before the hold, and the claim path never
    # asked whether the task was held, so it could be claimed anyway. A held task
    # takes no dispatch, and the item stays queued rather than failing: the hold
    # clears and the work resumes from the same item.
    assert node_queue.claim_next(NODE, identity=DEV) is None
    queued = node_queue.get_item(item["item_id"])
    assert queued is not None and queued["status"] == "queued"

    _hold(root, mode="resume", reason="")
    claimed = node_queue.claim_next(NODE, identity=DEV)
    assert claimed is not None and claimed["item_id"] == item["item_id"]


def test_node_items_count_as_live_and_a_cancel_hold_stops_them():
    from unified_agent import node_queue

    root, child, grandchild = _tree()
    _node()
    item = _node_item(grandchild)
    hold = _hold(root, release_policy="after_active_runs_finish")["hold"]
    assert hold["live_run_task_ids"] == [grandchild]
    _hold(root, mode="resume")
    result = _hold(root, mode="cancel")
    assert result["cancel_requests"] == [
        {
            "task_id": grandchild,
            "path": "node",
            "ref": item["item_id"],
            "control": "requested",
            "detail": "will not start",
        }
    ]
    assert node_queue.get_item(item["item_id"])["cancel_requested"]


def test_a_cancel_hold_refuses_another_identitys_node_item():
    root, child, _ = _tree()
    _node()
    _node_item(child)
    result = _hold(root, mode="cancel", who=OTHER)
    [outcome] = result["cancel_requests"]
    assert outcome["path"] == "node" and outcome["control"] == "refused"


# --- tenant fence --------------------------------------------------------------------


def test_holds_are_fenced_to_the_callers_tenant(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_one")
    root, child, _ = _tree()
    hold = _hold(root)["hold"]
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_two")
    other_root, other_child, other_grand = _tree()
    assert task_holds.task_holds(status="all")["holds"] == []
    with pytest.raises(ToolError, match="no task hold"):
        task_holds.task_hold_get(hold["id"])
    with pytest.raises(ToolError, match="no task with id"):
        _hold(root)
    with pytest.raises(ToolError, match="no task with id"):
        _hold(root, mode="resume")
    conn = storage.connect()
    try:
        assert task_holds.claim_block(conn, child) is None
    finally:
        conn.close()
    other = _hold(other_root)["hold"]
    assert other["member_ids"] == sorted([other_root, other_child, other_grand])
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_one")
    assert [h["id"] for h in task_holds.task_holds()["holds"]] == [hold["id"]]
    conn = storage.connect()
    try:
        assert "held by hold" in task_holds.claim_block(conn, child)
        assert task_holds.claim_block(conn, other_child) is None
    finally:
        conn.close()


# --- MCP surface ---------------------------------------------------------------------


def test_coordination_manage_actions():
    root, child, _ = _tree()
    with _as(OPERATOR):
        created = coordination_manage(
            "task_hold",
            {"root_task_id": root, "mode": "pause", "reason": "sprint stop"},
        )
        hold_id = created["hold"]["id"]
        listed = coordination_manage("task_holds", {"root_task_id": root})
        got = coordination_manage("task_hold_get", {"hold_id": hold_id})
        coordination_manage("task_hold", {"root_task_id": root, "mode": "resume"})
    assert [h["id"] for h in listed["holds"]] == [hold_id]
    assert got["member_ids"] == created["hold"]["member_ids"]
    assert task_holds.task_hold_get(hold_id)["status"] == "released"
    with pytest.raises(ToolError, match="invalid params"):
        coordination_manage("task_hold", {"root": root})


def test_holding_needs_task_update_in_the_profile(monkeypatch):
    root, _, _ = _tree()
    real = profiles.tool_is_allowed
    monkeypatch.setattr(
        profiles, "tool_is_allowed", lambda name: name != "task_update" and real(name)
    )
    with _as(DEV), pytest.raises(ToolError, match="task_update"):
        task_holds.task_hold(root, reason="x")


def test_profiles_expose_hold_writes_only_with_task_update():
    def allowed(profile, action):
        return profiles.consolidated_action_is_allowed_for(profile, "coordination_manage", action)

    for profile in (profiles.DEVELOPER, profiles.CTO, profiles.CHIEF_OF_STAFF):
        assert allowed(profile, "task_hold")
    # Steward has task_update but no coordination_manage at all.
    assert not allowed(profiles.STEWARD, "task_hold")
    assert not allowed(profiles.RESEARCH, "task_hold")
    assert allowed(profiles.RESEARCH, "task_holds") and allowed(profiles.RESEARCH, "task_hold_get")
    assert not allowed(profiles.PUBLIC, "task_holds")
    assert {"task_hold", "task_holds", "task_hold_get"} <= profiles.CONSOLIDATED_LEGACY_TOOLS


# --- HTTP surface --------------------------------------------------------------------

TOKENS = {
    DEV: "fixture-developer-token",
    "Desktop_Agent8": "fixture-research-token",
    OPERATOR: "fixture-operator-token",
}


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


def test_http_routes_hold_list_and_release(http):
    root, child, _ = _tree()
    created = http.post(
        "/api/tasks/holds",
        headers=_bearer(DEV),
        json={"root_task_id": root, "mode": "pause", "reason": "sprint stop"},
    )
    assert created.status_code == 200, created.text
    hold = created.json()["hold"]
    assert hold["created_by"] == DEV and child in hold["member_ids"]
    listed = http.get("/api/tasks/holds", headers=_bearer("Desktop_Agent8"))
    assert listed.status_code == 200
    assert [h["id"] for h in listed.json()["holds"]] == [hold["id"]]
    released = http.post(
        f"/api/tasks/holds/{hold['id']}/release", headers=_bearer(OPERATOR), json={}
    )
    assert released.status_code == 200, released.text
    assert released.json()["hold"]["released_by"] == OPERATOR
    again = http.post(f"/api/tasks/holds/{hold['id']}/release", headers=_bearer(OPERATOR), json={})
    assert again.status_code == 409
    missing = http.post("/api/tasks/holds/999999/release", headers=_bearer(DEV), json={})
    assert missing.status_code == 404
    bad = http.post("/api/tasks/holds", headers=_bearer(DEV), json={"root_task_id": "x"})
    assert bad.status_code == 400
    assert [e["action"] for e in _hold_events()] == ["pause", "resume"]


def test_http_hold_writes_refuse_a_profile_without_task_update(http):
    root, _, _ = _tree()
    assert not profiles.tool_is_allowed_for(profiles.RESEARCH, "task_update")
    refused = http.post(
        "/api/tasks/holds",
        headers=_bearer("Desktop_Agent8"),
        json={"root_task_id": root, "reason": "x"},
    )
    assert refused.status_code == 403
    # In-process under enforced auth, the operator profile itself has no task_update.
    with pytest.raises(ToolError, match="task_update"):
        _hold(root)
    hold = _hold(root, who=DEV)["hold"]
    refused = http.post(
        f"/api/tasks/holds/{hold['id']}/release", headers=_bearer("Desktop_Agent8"), json={}
    )
    assert refused.status_code == 403
    assert task_holds.task_hold_get(hold["id"])["status"] == "active"
    # The bare handler refuses the same caller (defence in depth).
    from unified_agent.webui import routes

    bare = TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=routes._declared_routes())),
        base_url="http://127.0.0.1",
    )
    res = bare.post(
        "/api/tasks/holds", headers=_bearer("Desktop_Agent8"), json={"root_task_id": root}
    )
    assert res.status_code == 403


def test_http_release_is_limited_to_the_creator_or_the_operator(http, monkeypatch):
    root, _, _ = _tree()
    created = http.post(
        "/api/tasks/holds",
        headers=_bearer(OPERATOR),
        json={"root_task_id": root, "mode": "pause", "reason": "operator stop"},
    )
    assert created.status_code == 200, created.text
    hold = created.json()["hold"]
    refused = http.post(f"/api/tasks/holds/{hold['id']}/release", headers=_bearer(DEV), json={})
    assert refused.status_code == 403
    assert "only its creator" in refused.json()["error"]
    assert task_holds.task_hold_get(hold["id"])["status"] == "active"
    # The verified operator over HTTP needs no lifecycle-operator entry.
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Someone_Else")
    dev_root, _, _ = _tree()
    dev_hold = http.post(
        "/api/tasks/holds",
        headers=_bearer(DEV),
        json={"root_task_id": dev_root, "reason": "developer stop"},
    ).json()["hold"]
    released = http.post(
        f"/api/tasks/holds/{dev_hold['id']}/release", headers=_bearer(OPERATOR), json={}
    )
    assert released.status_code == 200, released.text
    assert released.json()["hold"]["released_by"] == OPERATOR
    # The hold route enforces the same rule for a resume sent to it.
    other = http.post(
        "/api/tasks/holds",
        headers=_bearer(DEV),
        json={"root_task_id": root, "mode": "resume"},
    )
    assert other.status_code == 403


def test_the_real_executors_acknowledgement_lets_restore_reopen(monkeypatch):
    # End to end: a cancel hold, the executor daemon retiring its stopped
    # delegation (a fenced cancel under its own identity), then restore.
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    root = task_add("epic", lane="dispatch-ready")["id"]
    created = delegate.delegate("Hold me", OTHER, DEV, callback="none", parent_task_id=root)
    with _as(DEV):
        delegation = delegate.delegate_claim(created["delegation_id"], DEV)["delegation"]
        run = task_claim(created["task_id"], DEV)["current_run_id"]
    daemon = ExecutorDaemon(
        agent_label=DEV, hub_url="http://hub.invalid/mcp", bearer_token="synthetic-only"
    )
    wire_canonical_rpc_peer(daemon.hub_client, monkeypatch)
    out = _hold(root, mode="cancel")
    assert any(o.get("control") == "requested" for o in out["cancel_requests"]), out
    assert daemon._retire_stopped_delegation(delegation, run_id=run, reason="cancelled")
    assert _row(created["task_id"])["status"] == "cancelled"
    restored = _hold(root, mode="restore")
    assert [r["control"] for r in restored["restored"]] == ["reopened"]
    assert _row(created["task_id"])["status"] == "open"


def test_an_operators_fenced_cancel_is_not_an_acknowledgement():
    root, child, _ = _tree()
    run = _claim(DEV, child)["current_run_id"]
    _hold(root, mode="cancel")
    with _as(OPERATOR):
        task_update(child, status="cancelled", expected_run_id=run, worktree=False)
    out = _hold(root, mode="restore")
    assert [r["control"] for r in out["restored"]] == ["unchanged"]
    assert _row(child)["status"] == "cancelled"


# --- interrupted work is held too (task #2482) -----------------------------------------

INTERRUPTED = "held as interrupted work"


def _reap() -> tuple[lease_reaper.ReapResult, ...]:
    conn = storage.connect()
    try:
        reaped = lease_reaper.reap_stale_claim_leases(conn)
        conn.commit()
        return reaped
    finally:
        conn.close()


def _owner_vanishes(tid: int) -> None:
    """The claimant's lease lapses and no trace of it is left: its run stays open."""
    owner = _row(tid)["claimed_by"]
    _exec("UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,))
    _exec("DELETE FROM agents WHERE label=?", (owner,))


def _interrupted_delegation() -> tuple[int, int]:
    """A delegate died mid-run, the reaper held its task, and the delegation is queued again."""
    created = delegate.delegate(
        task_spec=_spec("interrupted"), from_agent=DEV, to_agent=OTHER, callback="none"
    )
    tid, delegation_id = created["task_id"], created["delegation_id"]
    with _as(OTHER):
        assert delegate.delegate_claim(delegation_id, OTHER)["claimed"]
        task_claim(tid, OTHER)
    _owner_vanishes(tid)
    [reaped] = _reap()
    assert reaped.to_status == "blocked" and reaped.interrupted_run
    _exec("UPDATE delegations SET status='pending' WHERE id=?", (delegation_id,))
    return tid, delegation_id


def _unowned(tid: int) -> bool:
    row = _row(tid)
    return (row["status"], row["claimed_by"], row["lock_expires_at"]) == ("blocked", None, None)


def test_interrupted_work_takes_no_claim_that_rides_its_queued_delegation():
    tid, _ = _interrupted_delegation()
    conn = storage.connect()
    try:
        assert INTERRUPTED in task_holds.claim_block(conn, tid)
    finally:
        conn.close()
    # The delegate's own claim would execute the queued delegation: not a resume.
    with _as(OTHER), pytest.raises(ToolError, match=INTERRUPTED):
        task_claim(tid, OTHER)
    with _as(OTHER), pytest.raises(ToolError, match=INTERRUPTED):
        task_update(tid, status="doing", worktree=False)
    assert _unowned(tid)


def test_an_explicit_claim_or_a_reopen_ends_the_interrupted_hold():
    tid, delegation_id = _interrupted_delegation()
    with _as(DEV):
        resumed = task_claim(tid, DEV, expected_lane=OTHER)
    assert (resumed["status"], resumed["claimed_by"]) == ("doing", DEV)

    again, queued = _interrupted_delegation()
    with _as(DEV):
        task_update(again, status="open", worktree=False)
    with _as(OTHER):
        assert delegate.delegate_claim(queued, OTHER)["claimed"]
        assert task_claim(again, OTHER)["claimed_by"] == OTHER
    assert delegation_id != queued


def test_approved_dispatch_and_resume_refuse_interrupted_work():
    tid, delegation_id = _interrupted_delegation()
    _retryable_run(tid, delegation_id)
    verdict = coordinator.evaluate_ready(task_id=tid)
    assert (verdict["ready"], verdict["reason"]) == (False, "held")
    assert INTERRUPTED in verdict["detail"]
    launched = coordinator.claim_and_dispatch(task_id=tid, actor=DEV)
    assert (launched["launched"], launched["reason"], launched["run_id"]) == (False, "held", None)
    resumed = coordinator.resume_run("run-held", actor=DEV)
    assert (resumed["ok"], resumed["reason"]) == (False, "held")
    assert INTERRUPTED in resumed["detail"]
    conn = storage.connect()
    try:
        states = conn.execute(
            "SELECT d.status, r.state FROM delegations d JOIN dispatch_runs r "
            "ON r.delegation_id = d.id WHERE d.id=?",
            (delegation_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in states] == [("pending", "retryable")]
    assert _unowned(tid)


def test_node_dispatch_refuses_interrupted_work_even_with_an_item_queued(monkeypatch):
    from unified_agent import node_queue, safety

    _node()
    tid = _task("node work")
    _claim(OTHER, tid)
    _node_item(tid)  # queued before the owner vanished: it does not stop the hold
    _owner_vanishes(tid)
    [reaped] = _reap()
    assert reaped.to_status == "blocked"

    def gate(*_a, **_k):
        raise AssertionError("the approval gate must not be reached for interrupted work")

    monkeypatch.setattr(safety, "check_prod_action_gate", gate)
    with pytest.raises(node_queue.NodeError, match=INTERRUPTED) as exc:
        node_queue.request_dispatch(
            NODE, actor=DEV, prompt="p", cwd=f"{NODE_ROOT}/repo", backend="codex", task_id=tid
        )
    assert exc.value.status == 409
    with pytest.raises(node_queue.NodeError, match=INTERRUPTED):
        _node_item(tid)
    assert _unowned(tid)
