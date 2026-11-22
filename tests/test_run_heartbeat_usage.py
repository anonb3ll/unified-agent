"""Heartbeat usage on a held self-pickup run (re-land of PR #866, task #1681).

A live lease renewal (``task_claim`` by the agent that already holds the task)
is the run's heartbeat. It may carry ``run_usage``: the run's cumulative usage
so far. The latest heartbeat is visible on the open run, and the finish or
abandon event carries it, so an agent that dies mid-run still leaves its cost
on the ledger. A heartbeat appends no event-log row.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import budgets, config, mcp_live_state, run_records, storage
from unified_agent.tools import cycles
from unified_agent.tools.tasks import task_add, task_claim, task_update

AGENT = "Desktop_Agent1"
OTHER = "Desktop_Agent2"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _one(sql: str, params: tuple = ()):
    conn = storage.connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _exec(sql: str, params: tuple = ()) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _event_count() -> int:
    return int(_one("SELECT COUNT(*) FROM event_log")[0])


def _claimed_task() -> tuple[int, str]:
    tid = task_add("heartbeat")["id"]
    with _as(AGENT):
        result = task_claim(tid, AGENT)
    return tid, result["current_run_id"]


def _heartbeat(tid: int, usage: dict, agent: str = AGENT) -> dict:
    with _as(agent):
        return task_claim(tid, agent, run_usage=usage)


def _account(tid: int, run_id: str, usage: dict, agent: str = AGENT):
    with _as(agent):
        return task_claim(tid, agent, expected_run_id=run_id, run_usage=usage, usage_only=True)


@pytest.mark.parametrize("finish", ["expired", "superseded", "cancelled", "waiting_review"])
def test_late_usage_preserves_run_lifecycle_and_successor(finish):
    tid, run_id = _claimed_task()
    if finish in ("expired", "superseded"):
        _exec("UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,))
        if finish == "superseded":
            with _as(OTHER):
                task_claim(tid, OTHER)
    else:
        with _as(AGENT):
            task_update(tid, status=finish)
    before_task = dict(_one("SELECT * FROM tasks WHERE id=?", (tid,)))
    before_run = run_records.get_run(run_id)
    finish_row = _one(
        "SELECT payload, entry_hash FROM event_log WHERE dedupe_key=?",
        (f"selfpickup:finished:{run_id}",),
    )
    before_finish = tuple(finish_row) if finish_row else None
    _account(tid, run_id, {"cost_usd": 3, "input_tokens": 700})
    assert dict(_one("SELECT * FROM tasks WHERE id=?", (tid,))) == before_task
    after = run_records.get_run(run_id)
    assert {k: v for k, v in after.items() if k not in ("usage", "usage_reported")} == {
        k: v for k, v in before_run.items() if k not in ("usage", "usage_reported")
    }
    assert after["usage"]["cost_usd"] == 3
    assert after["usage"]["input_tokens"] == 700
    finish_row = _one(
        "SELECT payload, entry_hash FROM event_log WHERE dedupe_key=?",
        (f"selfpickup:finished:{run_id}",),
    )
    assert (tuple(finish_row) if finish_row else None) == before_finish
    if finish != "expired":
        rollup = budgets.cost_rollup(agent=AGENT)
        assert sum(g["api_cost_usd"] for g in rollup["groups"]) == 3
        conn = storage.connect()
        try:
            policy = budgets.BudgetPolicy("late", (AGENT,), "month", 100)
            spend = budgets.agent_spend(conn, AGENT, policy)
            assert spend["api_cost_usd"] == 3
            assert spend["open_run_cost_usd"] == 0
            assert spend["committed_usd"] == 3
        finally:
            conn.close()


def test_accounting_replay_cannot_erase_spend_or_duplicate_events():
    tid, run_id = _claimed_task()
    _account(tid, run_id, {"cost_usd": 4, "input_tokens": 100})
    before = _event_count()
    _account(tid, run_id, {"cost_usd": 4, "input_tokens": 100})
    _account(tid, run_id, {"cost_usd": 1, "input_tokens": 10})
    assert _event_count() == before
    assert run_records.get_run(run_id)["usage"]["cost_usd"] == 4


def test_accounting_replay_cannot_exempt_api_spend():
    # Task #1837: billing is fenced like the numeric totals. A subscription
    # snapshot replayed after a mixed (api) cumulative total keeps the run
    # charged, before and after the finish.
    from unified_agent import budgets

    tid, run_id = _claimed_task()
    sub = {"billing": "subscription", "cost_usd": 1.0, "input_tokens": 5}
    mixed = {"billing": "api", "cost_usd": 3.0, "input_tokens": 10}
    _account(tid, run_id, sub)
    _account(tid, run_id, mixed)
    _account(tid, run_id, sub)
    usage = run_records.get_run(run_id)["usage"]
    assert usage["billing"] == "api"
    assert budgets._known_usd(usage) == pytest.approx(3.0)


def test_accounting_rejects_other_actor_task_and_unknown_run():
    tid, run_id = _claimed_task()
    other_task, _ = _claimed_task()
    for task_id, token, actor in (
        (tid, run_id, OTHER),
        (other_task, run_id, AGENT),
        (tid, "sp-missing", AGENT),
    ):
        with pytest.raises(ToolError, match="original run"):
            _account(task_id, token, {"cost_usd": 1}, actor)
    with _as(OTHER), pytest.raises(ToolError, match="original run"):
        task_claim(tid, AGENT, expected_run_id=run_id, run_usage={"cost_usd": 1}, usage_only=True)
    assert run_records.get_run(run_id)["usage_reported"] is False


def test_late_usage_requires_authenticated_actor():
    tid, run_id = _claimed_task()
    with pytest.raises(ToolError, match="authenticated actor"):
        task_claim(tid, AGENT, expected_run_id=run_id, run_usage={"cost_usd": 1}, usage_only=True)


def test_late_usage_refuses_identity_aliases_and_scope_mismatch(monkeypatch):
    tid, run_id = _claimed_task()
    monkeypatch.setattr(config, "identity_alias_allowlist", lambda: {OTHER: frozenset({AGENT})})
    with _as(OTHER), pytest.raises(ToolError, match="authenticated actor"):
        task_claim(tid, AGENT, expected_run_id=run_id, run_usage={"cost_usd": 1}, usage_only=True)
    with _as(AGENT), pytest.raises(ToolError, match="project guard"):
        task_claim(
            tid,
            AGENT,
            expected_run_id=run_id,
            run_usage={"cost_usd": 1},
            usage_only=True,
            expected_project="unrelated",
        )


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan"), True])
def test_accounting_rejects_invalid_cost_before_write(bad):
    tid, run_id = _claimed_task()
    before = _event_count()
    with pytest.raises(ToolError):
        _account(tid, run_id, {"cost_usd": bad})
    assert _event_count() == before


def test_terminal_cycle_accepts_only_bound_owner_usage_once():
    tid, run_id = _claimed_task()
    with _as(AGENT):
        cycle_id = cycles.autonomous_cycle_start(AGENT, task_id=tid)["cycle"]["id"]
        cycles.autonomous_cycle_kill(cycle_id, AGENT, "operator stop")
        args = {
            "cycle_id": cycle_id,
            "agent": AGENT,
            "usage_run_id": run_id,
            "usage_id": "attempt-1",
            "tokens": 50,
            "cost_cents": 300,
        }
        cycles.autonomous_cycle_record_usage(**args)
        assert cycles.autonomous_cycle_record_usage(**args)["replayed"] is True
        with pytest.raises(ToolError, match="different usage"):
            cycles.autonomous_cycle_record_usage(**{**args, "cost_cents": 100})
    cycle = cycles.autonomous_cycle_get(cycle_id)["cycle"]
    assert (
        cycle["state"] == "killed" and cycle["stop_reason"] == f"killed by {AGENT}: operator stop"
    )
    assert cycle["used_tokens"] == 50 and cycle["used_cost_cents"] == 300
    with _as(OTHER), pytest.raises(ToolError, match="original authenticated"):
        cycles.autonomous_cycle_record_usage(**{**args, "agent": OTHER, "usage_id": "attempt-2"})
    other_tid, other_run = _claimed_task()
    with _as(AGENT), pytest.raises(ToolError, match="bound task"):
        cycles.autonomous_cycle_record_usage(
            **{**args, "usage_run_id": other_run, "usage_id": "attempt-3"}
        )
    assert other_tid != tid


def test_heartbeat_usage_is_visible_on_the_open_run():
    tid, run_id = _claimed_task()

    _heartbeat(tid, {"provider": "anthropic", "input_tokens": 100})

    run = run_records.get_run(run_id)
    assert run["state"] == "open"
    assert run["usage_reported"] is True
    assert run["usage"]["provider"] == "anthropic"
    assert run["usage"]["input_tokens"] == 100
    assert run["usage"]["output_tokens"] is None
    [listed] = run_records.run_history(task_id=tid)["runs"]
    assert listed["usage"] == run["usage"]


def test_later_heartbeat_replaces_reported_fields_and_keeps_the_rest():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"provider": "anthropic", "input_tokens": 100})

    _heartbeat(tid, {"input_tokens": 250, "output_tokens": 40})

    usage = run_records.get_run(run_id)["usage"]
    assert usage["provider"] == "anthropic"
    assert usage["input_tokens"] == 250
    assert usage["output_tokens"] == 40


def test_heartbeat_appends_no_event_and_notifies_run_subscribers(monkeypatch):
    tid, run_id = _claimed_task()
    notified: list[list[str]] = []
    monkeypatch.setattr(mcp_live_state, "notify_uris", lambda uris: notified.append(list(uris)))
    before = _event_count()

    _heartbeat(tid, {"input_tokens": 5})

    assert _event_count() == before
    assert any(mcp_live_state.run_uri(run_id) in uris for uris in notified)


def test_finish_without_usage_carries_the_last_heartbeat():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"provider": "anthropic", "input_tokens": 300, "cost_usd": 0.25})

    with _as(AGENT):
        task_update(tid, status="done", evidence_ref="https://example/pr/1")

    run = run_records.get_run(run_id)
    assert run["state"] == "finished" and run["outcome"] == "completed"
    assert run["usage_reported"] is True
    assert run["usage"]["input_tokens"] == 300
    assert run["usage"]["cost_usd"] == 0.25


def test_finish_usage_overrides_heartbeat_fields_it_reports():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"provider": "anthropic", "input_tokens": 300, "output_tokens": 10})

    with _as(AGENT):
        task_update(
            tid,
            status="done",
            evidence_ref="https://example/pr/2",
            run_usage={"output_tokens": 90, "cost_usd": 0.5},
        )

    usage = run_records.get_run(run_id)["usage"]
    assert usage["provider"] == "anthropic"
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 90
    assert usage["cost_usd"] == 0.5


def test_abandoned_run_keeps_its_heartbeat_usage():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"input_tokens": 700})
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (past, tid))

    with _as(OTHER):
        task_claim(tid, OTHER)

    prior = run_records.get_run(run_id)
    assert prior["outcome"] == "abandoned"
    assert prior["usage_reported"] is True
    assert prior["usage"]["input_tokens"] == 700
    [current] = [r for r in run_records.run_history(task_id=tid)["runs"] if r["state"] == "open"]
    assert current["agent"] == OTHER
    assert current["usage_reported"] is False


def test_usage_on_a_first_claim_is_refused_without_a_write():
    tid = task_add("first claim")["id"]
    before = _event_count()

    with _as(AGENT), pytest.raises(ToolError, match="run_usage"):
        task_claim(tid, AGENT, run_usage={"input_tokens": 1})

    row = _one("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,))
    assert (row["status"], row["claimed_by"]) == ("open", None)
    assert _event_count() == before


def test_usage_after_a_lapsed_lease_is_refused():
    """The lapsed run's usage cannot be attributed to the run a reclaim opens."""
    tid, run_id = _claimed_task()
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (past, tid))

    with pytest.raises(ToolError, match="run_usage"):
        _heartbeat(tid, {"input_tokens": 1})

    assert _one("SELECT current_run_id FROM tasks WHERE id=?", (tid,))[0] == run_id
    assert run_records.get_run(run_id)["usage_reported"] is False


@pytest.mark.parametrize("bad", [{"input_tokens": -1}, {"nope": 1}, "tokens"])
def test_bad_heartbeat_usage_is_refused_before_the_lease_moves(bad):
    tid, _run_id = _claimed_task()
    before = _one("SELECT lock_expires_at, updated_at FROM tasks WHERE id=?", (tid,))

    with pytest.raises(ToolError, match="run_usage"):
        _heartbeat(tid, bad)

    after = _one("SELECT lock_expires_at, updated_at FROM tasks WHERE id=?", (tid,))
    assert tuple(after) == tuple(before)


def test_heartbeat_store_exists_on_a_fresh_database():
    storage.init_db()
    row = _one("SELECT name FROM sqlite_master WHERE type='table' AND name='run_usage_heartbeats'")
    assert row is not None


# --- Devin review on PR #940 ---------------------------------------------------


def _heartbeat_row(run_id: str):
    return _one("SELECT usage_json FROM run_usage_heartbeats WHERE run_id=?", (run_id,))


def test_heartbeat_changes_the_polled_run_revision():
    """A heartbeat written by another process appends no event, so the hub's
    poller must still see it through the run's revision."""
    tid, run_id = _claimed_task()
    uri = mcp_live_state.run_uri(run_id)
    before, cursor = mcp_live_state.snapshot_with_cursor()

    _heartbeat(tid, {"input_tokens": 100})
    after, cursor = mcp_live_state.snapshot_with_cursor(before, cursor)
    assert after[uri] != before[uri]

    # Stable while nothing changes, and a later heartbeat moves it again.
    steady, cursor = mcp_live_state.snapshot_with_cursor(after, cursor)
    assert steady[uri] == after[uri]
    _heartbeat(tid, {"input_tokens": 250})
    later, cursor = mcp_live_state.snapshot_with_cursor(steady, cursor)
    assert later[uri] != steady[uri]

    # The full-snapshot branch agrees with the incremental one.
    assert mcp_live_state.snapshot()[uri] == later[uri]


def test_runs_in_flight_self_pickup_entry_carries_heartbeat_usage():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"provider": "anthropic", "cost_usd": 0.25})

    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    [entry] = item["sources"]["self_pickup"]
    assert entry["run_id"] == run_id
    assert entry["usage_reported"] is True
    assert entry["usage"]["cost_usd"] == 0.25
    assert entry["usage"]["provider"] == "anthropic"


def test_runs_in_flight_entry_without_heartbeat_reports_no_usage():
    tid, _run_id = _claimed_task()

    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    [entry] = item["sources"]["self_pickup"]
    assert entry["usage_reported"] is False
    assert entry["usage"] == run_records.empty_usage()


def test_finish_drops_the_heartbeat_row():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"input_tokens": 300})
    assert _heartbeat_row(run_id) is not None

    with _as(AGENT):
        task_update(tid, status="done", evidence_ref="https://example/pr/3")

    assert _heartbeat_row(run_id) is None
    assert run_records.get_run(run_id)["usage"]["input_tokens"] == 300


def test_abandon_drops_the_heartbeat_row():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"input_tokens": 700})
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (past, tid))

    with _as(OTHER):
        task_claim(tid, OTHER)

    assert _heartbeat_row(run_id) is None
    assert run_records.get_run(run_id)["usage"]["input_tokens"] == 700


def test_task_delete_drops_heartbeat_rows():
    tid, run_id = _claimed_task()
    _heartbeat(tid, {"input_tokens": 1})

    from unified_agent.tools.tasks import task_delete

    assert task_delete(tid)["deleted"] is True
    assert _heartbeat_row(run_id) is None


def test_event_prune_drops_heartbeats_of_pruned_claims():
    from unified_agent import events

    stamp = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    original = storage.now
    storage.now = lambda: stamp
    try:
        old_tid, old_run = _claimed_task()
    finally:
        storage.now = original
    _heartbeat(old_tid, {"input_tokens": 1})
    new_tid, new_run = _claimed_task()
    _heartbeat(new_tid, {"input_tokens": 2})

    assert events.prune_events(older_than_days=30) >= 1

    assert _heartbeat_row(old_run) is None
    assert _heartbeat_row(new_run) is not None
