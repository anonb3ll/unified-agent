"""task_update into doing passes the same WIP caps as task_claim (task #1886).

``task_update(status="doing")`` moved a task into doing without the per-agent,
lane or fleet WIP checks, and without a claimant the row was invisible to the
fleet count. These tests pin the gate on that path, its exemptions, and the
fleet count of unclaimed doing rows.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, run_records, storage
from unified_agent.tools import tasks
from unified_agent.tools.tasks import task_add, task_claim, task_update


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "2")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "3")
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)


def _claim(agent: str, lane: str = "dispatch-ready") -> dict:
    tid = task_add("claimed", lane=lane)["id"]
    with _as(agent):
        return task_claim(tid, agent, expected_lane=lane)


def _fill_to_hard_cap() -> None:
    for agent in ("agent-a", "agent-b", "agent-c"):
        _claim(agent)


def _move(agent: str | None, lane: str = "dispatch-ready", **kw) -> dict:
    add = {k: kw.pop(k) for k in ("priority", "notes") if k in kw}
    tid = task_add("moved", lane=lane, **add)["id"]
    if agent is None:
        return task_update(tid, status="doing", **kw)
    with _as(agent):
        return task_update(tid, status="doing", **kw)


def _exec(sql: str, params: tuple) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _counts() -> tuple[int, int]:
    conn = storage.connect()
    try:
        return run_records.live_wip_counts(conn, storage.now())
    finally:
        conn.close()


def test_update_into_doing_past_the_fleet_hard_cap_is_refused_like_a_claim():
    _fill_to_hard_cap()
    tid = task_add("moved", lane="dispatch-ready")["id"]
    with _as("agent-d"), pytest.raises(ToolError, match="WIP cap: fleet already has 3"):
        task_update(tid, status="doing")
    [row] = [t for t in tasks.task_list()["tasks"] if t["id"] == tid]
    assert row["status"] == "open"  # the refusal rolled the move back


def test_update_into_doing_warns_at_the_soft_cap():
    first = _move("agent-a")
    second = _move("agent-b")
    assert "wip_warnings" not in first and "wip_warnings" not in second
    third = _move("agent-c")
    assert third["status"] == "doing"
    assert any("soft cap 2" in w and "2 with no claimant" in w for w in third["wip_warnings"])


@pytest.mark.parametrize("add", [{"priority": "high"}, {"notes": "[urgency: now] prod fix"}])
def test_urgent_update_past_the_hard_cap_is_warned_never_refused(add):
    _fill_to_hard_cap()
    moved = _move("agent-d", **add)
    assert moved["status"] == "doing"
    assert any("past the hard cap" in w for w in moved["wip_warnings"])


def test_priority_raised_on_the_same_call_counts_as_urgent():
    _fill_to_hard_cap()
    tid = task_add("raised", lane="dispatch-ready")["id"]
    with _as("agent-d"):
        moved = task_update(tid, status="doing", priority="high")
    assert any("past the hard cap" in w for w in moved["wip_warnings"])


def test_already_doing_update_is_not_checked():
    held = _claim("agent-a")
    _claim("agent-b")
    _claim("agent-c")
    with _as("agent-a"):
        again = task_update(held["id"], status="doing", append_note="still on it")
    assert again["status"] == "doing"
    assert "wip_warnings" not in again


def test_exempt_lanes_are_not_capped_by_the_fleet():
    _fill_to_hard_cap()
    for lane in config.FLEET_WIP_EXEMPT_LANES:
        moved = _move("agent-d", lane=lane)
        assert moved["status"] == "doing"
        assert "wip_warnings" not in moved
        with _as("agent-d"):
            task_update(moved["id"], status="open")


def test_per_agent_cap_applies_when_the_callers_claimed_wip_is_at_cap(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    _claim("agent-a")
    _claim("agent-a")
    tid = task_add("third", lane="dispatch-ready")["id"]
    with _as("agent-a"), pytest.raises(ToolError, match="'agent-a' already holds 2"):
        task_update(tid, status="doing")


def test_per_agent_cap_counts_the_agents_own_unclaimed_doing_rows(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    _move("agent-a")
    _move("agent-a")
    with pytest.raises(ToolError, match="'agent-a' already holds 2"):
        _move("agent-a")
    # Another agent's cap is untouched by agent-a's rows.
    assert _move("agent-b")["status"] == "doing"
    # And task_claim sees the same per-agent count.
    tid = task_add("claim", lane="dispatch-ready")["id"]
    with _as("agent-a"), pytest.raises(ToolError, match="'agent-a' already holds 2"):
        task_claim(tid, "agent-a", expected_lane="dispatch-ready")


def test_hard_lane_cap_applies_on_update_into_doing(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _claim("agent-a")
    with pytest.raises(ToolError, match="lane 'dispatch-ready' already has 1"):
        _move("agent-b")


def test_unclaimed_doing_rows_count_toward_the_fleet_and_show_in_runs_in_flight():
    # Unverified moves below the fleet hard cap succeed, and they still count.
    for _ in range(3):
        assert _move(None)["status"] == "doing"
    assert _counts() == (0, 3)
    view = run_records.runs_in_flight()
    fleet = view["wip"]["fleet"]
    assert fleet["live_claims"] == 3
    assert fleet["unclaimed_doing"] == 3
    assert fleet["state"] == "at_hard_cap"
    # A direct claim now sees them.
    with pytest.raises(ToolError, match="3 with no claimant"):
        _claim("agent-a")


def test_unclaimed_doing_row_ages_out_after_its_implicit_lease():
    moved = _move(None)
    assert _counts() == (0, 1)
    stale = "2000-01-01T00:00:00+00:00"
    _exec("UPDATE tasks SET updated_at=? WHERE id=?", (stale, moved["id"]))
    assert _counts() == (0, 0)
    # Any write renews it, like a lease renewal.
    task_update(moved["id"], append_note="progress")
    assert _counts() == (0, 1)
    # Archived rows never count (queue_pressure's unowned_doing predicate).
    task_update(moved["id"], archived=True)
    assert _counts() == (0, 0)


def test_cutoff_is_one_default_lease_before_now():
    assert run_records.unclaimed_doing_cutoff("2026-10-06T12:00:00+00:00") == (
        "2026-10-06T11:00:00+00:00"
    )
    assert run_records.unclaimed_doing_cutoff("2026-10-06T12:00:00Z") == (
        "2026-10-06T11:00:00+00:00"
    )


def test_unverified_caller_gets_the_fleet_cap():
    # No in-process service caller moves a task into doing, so a call with no
    # verified identity is an unauthenticated client: the identity-independent
    # fleet cap binds it.
    _fill_to_hard_cap()
    tid = task_add("unverified", lane="dispatch-ready")["id"]
    with pytest.raises(ToolError, match="WIP cap: fleet already has 3"):
        task_update(tid, status="doing", actor="agent-d")


def test_unverified_actor_is_not_charged_to_that_label(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    for _ in range(3):
        moved = _move(None, actor="agent-a")
        assert moved["status"] == "doing"
        # The run is opened with no agent: an asserted actor is not a
        # verified identity, so it cannot fill agent-a's per-agent cap.
        assert moved["current_run_agent"] is None
    assert _counts() == (0, 3)
    assert _claim("agent-a")["status"] == "doing"


def _lapsed_blocked(agent: str) -> int:
    claimed = _claim(agent)
    with _as(agent):
        task_update(claimed["id"], status="blocked")
    _exec(
        "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (claimed["id"],),
    )
    return claimed["id"]


def test_blocked_task_with_a_lapsed_lease_reentering_doing_is_counted_and_capped():
    # The reviewer's repro: a lapsed lease left a claimed row neither count
    # saw, so every re-entry into doing passed every cap.
    ids = [_lapsed_blocked("agent-a") for _ in range(4)]
    assert _counts() == (0, 0)
    with _as("agent-a"):
        first = task_update(ids[0], status="doing")
        task_update(ids[1], status="doing")
        with pytest.raises(ToolError, match="'agent-a' already holds 2"):
            task_update(ids[2], status="doing")
    # The holder's lease was renewed, so its rows count as live claims.
    assert first["claimed_by"] == "agent-a" and first["lock_expires_at"] > storage.now()
    assert _counts() == (2, 0)


def test_lapsed_claim_taken_into_doing_by_another_agent_counts_for_that_agent(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    tid = _lapsed_blocked("agent-a")
    with _as("agent-b"):
        moved = task_update(tid, status="doing")
    # The dead claim is dropped; the row is unclaimed WIP under agent-b's run.
    assert moved["claimed_by"] is None
    assert moved["current_run_agent"] == "agent-b"
    assert _counts() == (0, 1)
    _move("agent-b")
    with pytest.raises(ToolError, match="'agent-b' already holds 2"):
        _move("agent-b")


def test_lapsed_claims_past_the_fleet_hard_cap_are_refused():
    ids = [_lapsed_blocked(agent) for agent in ("agent-a", "agent-b", "agent-c", "agent-d")]
    for agent, tid in zip(("agent-a", "agent-b", "agent-c"), ids, strict=False):
        with _as(agent):
            task_update(tid, status="doing")
    assert _counts() == (3, 0)
    with _as("agent-d"), pytest.raises(ToolError, match="WIP cap: fleet already has 3"):
        task_update(ids[3], status="doing")


def _archived(tid: int) -> None:
    task_update(tid, archived=True)


@pytest.mark.parametrize("case", ["new", "archived_unclaimed", "archived_claimed"])
def test_entering_doing_on_a_parked_row_is_refused_at_capacity(case):
    # Admission reads the resulting row: a row that ends archived is parked,
    # whatever the WIP count, and is refused before any cap is consulted.
    _fill_to_hard_cap()
    tid = task_add("parked", lane="dispatch-ready")["id"]
    kwargs = {}
    if case == "new":
        kwargs["archived"] = True
    elif case == "archived_unclaimed":
        _archived(tid)
    else:
        _exec(
            "UPDATE tasks SET claimed_by='agent-d', claimed_at=?, lock_expires_at=?, "
            "archived_at=? WHERE id=?",
            (storage.now(), "2999-01-01T00:00:00+00:00", storage.now(), tid),
        )
    with _as("agent-d"), pytest.raises(ToolError, match="is parked"):
        task_update(tid, status="doing", **kwargs)
    conn = storage.connect()
    try:
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "open"
    if case != "new":
        # task_claim refuses an already-parked row the same way.
        with _as("agent-d"), pytest.raises(ToolError, match="is parked"):
            task_claim(tid, "agent-d", expected_lane="dispatch-ready")


def test_unparking_on_the_same_call_is_admitted_as_an_unarchived_row():
    tid = task_add("unpark and start", lane="dispatch-ready")["id"]
    _archived(tid)
    _fill_to_hard_cap()
    # The resulting row is unarchived, so it faces the caps like any other.
    with _as("agent-d"), pytest.raises(ToolError, match="WIP cap: fleet already has 3"):
        task_update(tid, status="doing", archived=False)
    other = task_add("unpark below cap", lane="operator-only")["id"]
    _archived(other)
    with _as("agent-d"):
        moved = task_update(other, status="doing", archived=False)
    assert moved["status"] == "doing" and moved["archived_at"] is None


def _lane_occupant(state: str) -> int:
    occupant = _move(None)["id"]
    if state in ("expired", "renewed"):
        _exec(
            "UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (occupant,),
        )
    if state == "renewed":
        task_update(occupant, append_note="still going")
    if state == "archived":
        _archived(occupant)
    return occupant


@pytest.mark.parametrize("via", ["claim", "update"])
@pytest.mark.parametrize(
    ("state", "blocks"),
    [("fresh", True), ("expired", False), ("archived", False), ("renewed", True)],
)
def test_lane_count_applies_the_implicit_lease_to_unclaimed_occupants(
    monkeypatch, via, state, blocks
):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _lane_occupant(state)

    def enter() -> dict:
        return _claim("agent-b") if via == "claim" else _move("agent-b")

    if blocks:
        with pytest.raises(ToolError, match="lane 'dispatch-ready' already has 1"):
            enter()
    else:
        assert enter()["status"] == "doing"


def test_relaning_in_flight_work_into_a_full_lane_is_refused_even_when_fenced(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _claim("agent-a")
    tid = task_add("in flight")["id"]
    with _as("agent-b"):
        held = task_claim(tid, "agent-b")
        assert held["current_run_id"]
        with pytest.raises(ToolError, match="lane 'dispatch-ready' already has 1"):
            task_update(tid, lane="dispatch-ready", expected_run_id=held["current_run_id"])


def test_unverified_entry_with_a_relane_faces_the_destination_lane_cap(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _claim("agent-a")
    tid = task_add("relane and start")["id"]
    with pytest.raises(ToolError, match="lane 'dispatch-ready' already has 1"):
        task_update(tid, status="doing", lane="dispatch-ready", actor="agent-d")


def test_a_run_token_cannot_fence_an_entry_into_doing():
    # Leaving doing closes the run and clears the token, so a "fenced" entry
    # is refused by the fence itself; there is no admitted run to exempt.
    held = _claim("agent-a")
    with _as("agent-a"):
        task_update(held["id"], status="blocked")
        with pytest.raises(ToolError, match="current run is None"):
            task_update(held["id"], status="doing", expected_run_id=held["current_run_id"])


def test_idempotent_retry_returns_the_same_wip_warnings_with_one_transition():
    _claim("agent-a")
    _claim("agent-b")
    tid = task_add("keyed", lane="dispatch-ready")["id"]
    with _as("agent-c"):
        first = task_update(tid, status="doing", idempotency_key="move-3")
        replay = task_update(tid, status="doing", idempotency_key="move-3")
    assert first["wip_warnings"] and "soft cap 2" in first["wip_warnings"][0]
    assert replay["wip_warnings"] == first["wip_warnings"]
    conn = storage.connect()
    try:
        transitions = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE channel='agent.task.updated' "
            "AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.status')='doing'",
            (tid,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert transitions == 1


def test_dispatched_task_with_a_live_slot_is_not_refused_by_the_fleet_cap():
    _fill_to_hard_cap()
    tid = task_add("dispatched", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("run-live", tid, "agent-d", "running", "coordinator", now, now),
    )
    with _as("agent-d"):
        moved = task_update(tid, status="doing")
    assert moved["status"] == "doing"
    assert "wip_warnings" not in moved


# --- claimability parity with task_claim (task #1894) ---------------------------


def _status(tid: int) -> str:
    [row] = [t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid]
    return row["status"]


def test_update_into_doing_refuses_the_tracking_lane_like_a_claim():
    tid = task_add("roadmap umbrella", lane="tracking")["id"]
    with _as("agent-a"):
        with pytest.raises(ToolError, match="lane 'tracking'"):
            task_claim(tid, "agent-a", expected_lane="tracking")
        with pytest.raises(ToolError, match="lane 'tracking'"):
            task_update(tid, status="doing")
    with pytest.raises(ToolError, match="lane 'tracking'"):
        task_update(tid, status="doing", actor="agent-b")
    assert _status(tid) == "open"


def test_update_into_doing_reads_the_lane_the_call_leaves_behind():
    into_tracking = task_add("relaned into tracking", lane="dispatch-ready")["id"]
    out_of_tracking = task_add("relaned out of tracking", lane="tracking")["id"]
    with _as("agent-a"):
        with pytest.raises(ToolError, match="lane 'tracking'"):
            task_update(into_tracking, status="doing", lane="tracking")
        moved = task_update(out_of_tracking, status="doing", lane="dispatch-ready")
    assert _status(into_tracking) == "open"
    assert moved["status"] == "doing"


def test_update_into_doing_refuses_a_cancelled_ancestor_like_a_claim():
    root = task_add("root", lane="dispatch-ready")["id"]
    mid = task_add("mid", parent_id=root)["id"]
    leaf = task_add("leaf", parent_id=mid)["id"]
    task_update(root, status="cancelled")
    with _as("agent-a"):
        with pytest.raises(ToolError, match=f"cancelled ancestor {root}"):
            task_claim(leaf, "agent-a", expected_lane="dispatch-ready")
        with pytest.raises(ToolError, match=f"cancelled ancestor {root}"):
            task_update(leaf, status="doing")
    assert _status(leaf) == "open"


def test_relaning_in_flight_work_into_a_soft_capped_lane_returns_the_warning(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:soft")
    _claim("agent-a")
    tid = task_add("in flight")["id"]
    with _as("agent-b"):
        task_claim(tid, "agent-b")
        relaned = task_update(tid, lane="dispatch-ready")
    assert relaned["lane"] == "dispatch-ready"
    assert relaned["wip_warnings"] == ["lane 'dispatch-ready' already has 1 doing task(s) (max 1)"]


def test_relaning_in_flight_work_checks_the_claimants_per_agent_cap(monkeypatch):
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "10")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "10")
    first = _claim("agent-a", lane="default")
    _claim("agent-a", lane="default")
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "1")
    with _as("agent-a"), pytest.raises(ToolError, match="'agent-a' already holds 1"):
        task_update(first["id"], lane="dispatch-ready")


def test_update_into_doing_reads_the_parent_the_call_leaves_behind():
    cancelled = task_add("cancelled root", lane="dispatch-ready")["id"]
    task_update(cancelled, status="cancelled")
    reparented = task_add("moved under it", lane="dispatch-ready")["id"]
    detached = task_add("detached from it", parent_id=cancelled)["id"]
    with _as("agent-a"):
        with pytest.raises(ToolError, match=f"cancelled ancestor {cancelled}"):
            task_update(reparented, status="doing", parent_id=cancelled)
        moved = task_update(detached, status="doing", parent_id=0)
    assert _status(reparented) == "open"
    assert moved["status"] == "doing" and moved["parent_id"] is None


# --- a doing row never lands in an unclaimable lane (task #2002) ----------------


def _lane(tid: int) -> str:
    [row] = [t for t in tasks.task_list(status="all")["tasks"] if t["id"] == tid]
    return row["lane"]


def test_relaning_in_flight_work_into_the_tracking_lane_is_refused():
    tid = _claim("agent-a")["id"]
    with _as("agent-a"), pytest.raises(ToolError, match="lane 'tracking'"):
        task_update(tid, lane="tracking")
    with pytest.raises(ToolError, match="lane 'tracking'"):
        task_update(tid, lane="tracking", actor="agent-b")
    assert _status(tid) == "doing"
    assert _lane(tid) == "dispatch-ready"


def test_reasserting_doing_with_the_tracking_lane_is_refused():
    tid = _claim("agent-a")["id"]
    with _as("agent-a"), pytest.raises(ToolError, match="lane 'tracking'"):
        task_update(tid, status="doing", lane="tracking")
    assert _status(tid) == "doing"
    assert _lane(tid) == "dispatch-ready"


@pytest.mark.parametrize("lane", config.TASK_UNCLAIMABLE_LANES)
def test_every_unclaimable_lane_is_refused_for_in_flight_work(lane):
    tid = _claim("agent-a")["id"]
    with _as("agent-a"), pytest.raises(ToolError, match=f"lane {lane!r}"):
        task_update(tid, lane=lane)
    assert _lane(tid) == "dispatch-ready"


def test_relaning_into_tracking_while_leaving_doing_is_allowed():
    tid = _claim("agent-a")["id"]
    with _as("agent-a"):
        moved = task_update(tid, status="open", lane="tracking")
    assert moved["status"] == "open" and moved["lane"] == "tracking"


def test_a_legacy_doing_row_in_tracking_can_still_be_recovered():
    # A row that reached doing in tracking before this check (direct SQL or an
    # older hub) must stay recoverable: relaning it to a work lane, moving it
    # out of doing, or annotating it is not a new move into tracking.
    tid = _claim("agent-a")["id"]
    _exec("UPDATE tasks SET lane='tracking' WHERE id=?", (tid,))
    with _as("agent-a"):
        noted = task_update(tid, append_note="triage: move this out of tracking")
        assert noted["status"] == "doing"
        with pytest.raises(ToolError, match="lane 'tracking'"):
            task_update(tid, status="doing")
        relaned = task_update(tid, lane="dispatch-ready")
    assert relaned["status"] == "doing" and relaned["lane"] == "dispatch-ready"


def test_agent_wip_override_caps_a_move_into_doing(monkeypatch):
    # The move path reads the same per-agent override as task_claim.
    monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", "agent-a:4")
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "50")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "50")
    for _ in range(4):
        _move("agent-a")
    with pytest.raises(ToolError, match=r"'agent-a' already holds 4 doing task\(s\) \(max 4\)"):
        _move("agent-a")
    _move("agent-b")
    _move("agent-b")
    with pytest.raises(ToolError, match=r"'agent-b' already holds 2 doing task\(s\) \(max 2\)"):
        _move("agent-b")
