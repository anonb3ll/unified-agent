"""Fleet-wide WIP cap on direct task_claim (task #1838): soft, then hard.

Per-agent caps let N agents each hold UA_AGENT_WIP_MAX tasks, and the
dispatcher's live-run ceiling only counts dispatch_runs, so direct claims could
take the fleet well past its documented slot count without any signal.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, monitor, run_records, storage
from unified_agent.tools.tasks import task_add, task_claim, task_update

AGENTS = ("agent-a", "agent-b", "agent-c", "agent-d")


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


def _claim(agent: str, lane: str = "dispatch-ready", **add) -> dict:
    tid = task_add("t", lane=lane, **add)["id"]
    with _as(agent):
        return task_claim(tid, agent, expected_lane=lane)


def test_config_defaults_are_the_documented_policy(monkeypatch):
    # Task #2759: the fleet caps no longer borrow the dispatcher ceiling, and the
    # soft cap sits above the per-agent default (20) so the fleet warning means
    # something. The two gates still count different ledgers.
    monkeypatch.delenv("UA_AGENT_WIP_MAX", raising=False)
    monkeypatch.delenv("UA_FLEET_WIP_MAX")
    monkeypatch.delenv("UA_FLEET_WIP_HARD_MAX")
    monkeypatch.delenv("UA_DISPATCH_MAX_LIVE_RUNS", raising=False)
    assert config.fleet_wip_max() == 40
    assert config.fleet_wip_hard_max() == 80
    monkeypatch.setenv("UA_DISPATCH_MAX_LIVE_RUNS", "4")
    assert (config.fleet_wip_max(), config.fleet_wip_hard_max()) == (40, 80)
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "junk")
    assert config.fleet_wip_max() == 40
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "0")
    assert config.fleet_wip_max() == 1
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "3")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "1")
    assert config.fleet_wip_hard_max() == 3  # never below the soft cap


def test_claims_across_agents_warn_at_soft_cap_and_refuse_at_hard_cap():
    first = _claim("agent-a")
    second = _claim("agent-b")
    assert "wip_warnings" not in first and "wip_warnings" not in second
    third = _claim("agent-c")
    assert any("soft cap 2" in w for w in third["wip_warnings"])
    with pytest.raises(ToolError, match="WIP cap: fleet already has 3"):
        _claim("agent-d")


def test_urgent_work_is_warned_never_refused():
    for agent in AGENTS[:3]:
        _claim(agent)
    high = _claim("agent-d", priority="high")
    assert high["status"] == "doing"
    assert any("past the hard cap" in w for w in high["wip_warnings"])
    now = _claim("agent-d", notes="[urgency: now] production fix")
    assert now["status"] == "doing"
    assert any("past the hard cap" in w for w in now["wip_warnings"])


def test_exempt_lanes_keep_their_claim_semantics():
    for agent in AGENTS[:3]:
        _claim(agent)
    for lane in config.FLEET_WIP_EXEMPT_LANES:
        claimed = _claim("agent-d", lane=lane)
        assert claimed["status"] == "doing"
        assert "wip_warnings" not in claimed
        task_update(claimed["id"], status="open")


def test_live_renewal_is_never_refused_and_lapsed_leases_free_slots():
    held = _claim("agent-a")
    for agent in AGENTS[1:3]:
        _claim(agent)
    with _as("agent-a"):
        renewed = task_claim(held["id"], "agent-a", expected_lane="dispatch-ready")
    assert renewed["status"] == "doing" and "wip_warnings" not in renewed

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (held["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert _claim("agent-d")["status"] == "doing"


def test_runs_in_flight_reports_cap_count_and_claim_path():
    direct = _claim("agent-a")
    _claim("agent-b")
    _claim("agent-c")
    view = monitor.observability_manage("runs_in_flight")
    fleet = view["wip"]["fleet"]
    assert fleet == {
        "live_claims": 3,
        "unclaimed_doing": 0,
        "soft_cap": 2,
        "hard_cap": 3,
        "state": "at_hard_cap",
        "exempt_lanes": list(config.FLEET_WIP_EXEMPT_LANES),
    }
    assert view["wip"]["dispatch"] == {"live_slots": 0, "cap": config.dispatch_max_live_runs()}
    assert view["wip"]["running_by_path"] == {"direct_claim": 3}
    [item] = [i for i in view["items"] if i["task_id"] == direct["id"]]
    assert item["claim_path"] == "direct_claim"


def test_claim_path_names_the_dispatcher_and_task_update_paths():
    assert run_records._claim_path({"sources": {"dispatch": [{}], "self_pickup": []}}) == (
        "dispatcher"
    )
    assert run_records._claim_path({"sources": {"node": [{}]}}) == "dispatcher"
    assert run_records._claim_path({"sources": {"executor": [{}]}}) == "executor"
    updated = {"sources": {"self_pickup": [{"state": "running", "claim_source": "task_update"}]}}
    assert run_records._claim_path(updated) == "task_update"
    assert run_records._claim_path({"sources": {}}) == "unrecorded"


def _exec(sql: str, params: tuple) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _fill_to_hard_cap() -> None:
    for agent in AGENTS[:3]:
        _claim(agent)


@pytest.mark.parametrize("state", ["claimed", "running"])
def test_dispatched_child_claim_at_hard_cap_is_not_refused(state):
    _fill_to_hard_cap()
    tid = task_add("dispatched", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (f"run-{state}", tid, "agent-d", state, "coordinator", now, now),
    )
    with _as("agent-d"):
        claimed = task_claim(tid, "agent-d", expected_lane="dispatch-ready")
    assert claimed["status"] == "doing"
    assert "wip_warnings" not in claimed
    # The dispatched claim still counts: an unrelated direct claim is refused.
    with pytest.raises(ToolError, match="WIP cap: fleet already has 4"):
        _claim("agent-c")


def test_node_dispatched_child_claim_at_hard_cap_is_not_refused():
    _fill_to_hard_cap()
    tid = task_add("node dispatched", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, status, "
        "created_by, created_at, claimed_at, updated_at, task_id) "
        "VALUES ('n1','node-a','agent-d','codex','p','/tmp',60,'claimed','operator',?,?,?,?)",
        (now, now, now, tid),
    )
    with _as("agent-d"):
        claimed = task_claim(tid, "agent-d", expected_lane="dispatch-ready")
    assert claimed["status"] == "doing"
    with pytest.raises(ToolError, match="WIP cap"):
        _claim("agent-c")


def test_finished_dispatch_run_does_not_exempt_the_claim():
    _fill_to_hard_cap()
    tid = task_add("dispatched earlier", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
        "created_at, updated_at) VALUES ('run-done',?,?,?,?,?,?)",
        (tid, "agent-d", "finished", "coordinator", now, now),
    )
    with _as("agent-d"), pytest.raises(ToolError, match="WIP cap"):
        task_claim(tid, "agent-d", expected_lane="dispatch-ready")


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        ("cancelled", {"termination_outcome": None}),
        ("blocked", {"failure_reason": "runtime_ceiling_exceeded"}),
    ],
)
def test_slot_holding_dispatch_run_is_listed_with_its_state(state, extra):
    tid = task_add("reserved", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, failure_reason, "
        "termination_outcome, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "run-held",
            tid,
            "agent-d",
            state,
            "coordinator",
            extra.get("failure_reason"),
            extra.get("termination_outcome"),
            now,
            now,
        ),
    )
    view = run_records.runs_in_flight()
    assert view["wip"]["dispatch"]["live_slots"] == 1
    [item] = [i for i in view["items"] if i["task_id"] == tid]
    [row] = item["sources"]["dispatch"]
    assert row["run_id"] == "run-held" and row["state"] == state
    assert item["claim_path"] == "dispatcher"
    assert view["counts"]["by_source"]["dispatch"] == 1


def test_confirmed_cancelled_dispatch_run_holds_no_slot_and_is_not_listed():
    tid = task_add("released", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
        "termination_outcome, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("run-gone", tid, "agent-d", "cancelled", "coordinator", "confirmed", now, now),
    )
    view = run_records.runs_in_flight()
    assert view["wip"]["dispatch"]["live_slots"] == 0
    assert tid not in {i["task_id"] for i in view["items"]}


def test_stale_executor_attempt_does_not_label_a_newer_direct_claim():
    tid = task_add("reused", lane="dispatch-ready")["id"]
    # An old attempt whose finish write was lost, by the same agent.
    _exec(
        "INSERT INTO executor_attempts (task_id, agent, backend, started_at) VALUES (?,?,?,?)",
        (tid, "agent-a", "codex", "2000-01-01T00:00:00+00:00"),
    )
    with _as("agent-a"):
        task_claim(tid, "agent-a", expected_lane="dispatch-ready")
    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    assert item["sources"]["executor"]  # still visible as evidence
    assert item["claim_path"] == "direct_claim"


def test_executor_attempt_after_the_current_claim_labels_it_executor():
    tid = task_add("executor run", lane="dispatch-ready")["id"]
    with _as("agent-a"):
        task_claim(tid, "agent-a", expected_lane="dispatch-ready")
    _exec(
        "INSERT INTO executor_attempts (task_id, agent, backend, started_at) VALUES (?,?,?,?)",
        (tid, "agent-a", "codex", storage.now()),
    )
    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    assert item["claim_path"] == "executor"


def test_dispatch_run_for_another_agent_does_not_label_the_current_claim():
    tid = task_add("taken over", lane="dispatch-ready")["id"]
    now = storage.now()
    _exec(
        "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
        "created_at, updated_at) VALUES ('run-other',?,?,?,?,?,?)",
        (tid, "agent-d", "running", "coordinator", now, now),
    )
    with _as("agent-a"):
        task_claim(tid, "agent-a", expected_lane="dispatch-ready")
    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    assert item["claim_path"] == "direct_claim"
