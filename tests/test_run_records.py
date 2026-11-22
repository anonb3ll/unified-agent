"""Self-pickup run records (task #1436).

A run record is written when an agent claims a task and when it finishes, on
the durable event log. `task_claim`/`task_update` record it atomically, in
the same transaction as the task write itself (task #1525). A failing
recorder remains best-effort without a bound run token; lifecycle changes
with a bound token roll back so a retired executor cannot keep writing.
Missing coverage must be visible rather than silent.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, monitor, run_records, storage
from unified_agent.mcp_live_state import read_run
from unified_agent.tools.tasks import task_add, task_claim, task_update


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _exec(sql: str, params: tuple = ()) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _events(kind: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.kind')=? ORDER BY ulid",
            (run_records.CHANNEL, kind),
        ).fetchall()
    finally:
        conn.close()
    return [run_records._payload(r) for r in rows]


def _retained_run_events(task_id: int) -> tuple[dict, dict]:
    claim = next(
        item for item in reversed(_events(run_records.KIND_CLAIMED)) if item["task_id"] == task_id
    )
    finish = next(
        item
        for item in reversed(_events(run_records.KIND_FINISHED))
        if item["task_id"] == task_id and item["run_id"] == claim["run_id"]
    )
    return claim, finish


def _append_retained_run_event(payload: dict, *, dedupe_key: str | None = None) -> None:
    _, inserted = events.record_event_once(
        run_records.CHANNEL,
        payload=payload,
        originator=payload.get("agent"),
        notify=False,
        dedupe_key=dedupe_key,
    )
    assert inserted


def _assert_append_only_fixture_integrity() -> None:
    with pytest.raises(sqlite3.IntegrityError, match="event_log is append-only"):
        _exec("UPDATE event_log SET created_at=created_at")
    with pytest.raises(sqlite3.IntegrityError, match="event_log is append-only"):
        _exec("DELETE FROM event_log WHERE rowid=(SELECT MAX(rowid) FROM event_log)")
    assert events.verify_event_chain()["status"] == "valid"


def _history(task_id: int) -> list[dict]:
    return run_records.run_history(task_id=task_id)["runs"]


def _new_task(title: str = "t") -> int:
    return task_add(title, lane="dispatch-ready")["id"]


def _past(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_claim_then_finish_writes_one_run_with_outcome_and_duration():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        runs = _history(tid)
        assert len(runs) == 1 and runs[0]["state"] == "open"
        assert runs[0]["agent"] == "Desktop_Agent1"
        assert runs[0]["claim_source"] == "task_claim"
        task_update(tid, status="done", evidence_ref="https://example/pr/1")

    [run] = _history(tid)
    assert run["state"] == "finished"
    assert run["outcome"] == "completed"
    assert run["to_status"] == "done"
    assert run["finished_by"] == "Desktop_Agent1"
    assert run["wall_s"] is not None and run["wall_s"] >= 0
    assert run["run_id"].startswith("sp-")
    assert len(_events(run_records.KIND_CLAIMED)) == 1
    assert len(_events(run_records.KIND_FINISHED)) == 1


def test_cross_agent_release_finishes_the_pre_update_claimant_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))

    # A different agent may release an expired claim. The task row is cleared
    # before the best-effort recorder runs, so it must receive the old owner.
    with _as("Desktop_Agent2"):
        task_update(tid, status="open", release_claim=True)

    [run] = _history(tid)
    assert run["state"] == "finished"
    assert run["agent"] == "Desktop_Agent1"
    assert run["outcome"] == "released"
    assert run["finished_by"] == "Desktop_Agent2"


def test_cross_agent_completion_preserves_original_run_and_usage():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    with _as("Desktop_Agent2"):
        task_update(
            tid,
            status="done",
            evidence_ref="https://example/pr/2",
            run_usage={"input_tokens": 10},
        )

    [run] = _history(tid)
    assert run["state"] == "finished"
    assert run["agent"] == "Desktop_Agent1"
    assert run["outcome"] == "completed"
    assert run["finished_by"] == "Desktop_Agent2"
    assert run["usage"]["input_tokens"] == 10


def test_live_renewal_does_not_open_a_second_run():
    tid = _new_task()
    with _as("server_agent2"):
        task_claim(tid, "server_agent2", expected_lane="dispatch-ready")
        task_claim(tid, "server_agent2", expected_lane="dispatch-ready")
        task_claim(tid, "server_agent2", expected_lane="dispatch-ready")
    assert len(_history(tid)) == 1


def test_renewal_of_a_claim_made_before_the_recorder_opens_an_observed_run():
    tid = _new_task()
    now = storage.now()
    _exec(
        "UPDATE tasks SET status='doing', claimed_by=?, claimed_at=?, lock_expires_at=? WHERE id=?",
        ("server_agent2", now, "2999-01-01T00:00:00+00:00", tid),
    )
    assert _history(tid) == []
    with _as("server_agent2"):
        task_claim(tid, "server_agent2", expected_lane="dispatch-ready")
    [run] = _history(tid)
    assert run["observed_from"] == "renewal"
    assert run["state"] == "open"


def test_task_update_into_doing_without_task_claim_is_still_recorded():
    tid = _new_task()
    with _as("Desktop_Agent5"):
        task_update(tid, status="doing", worktree=False)
    [run] = _history(tid)
    assert run["claim_source"] == "task_update"
    assert run["agent"] == "Desktop_Agent5"
    with _as("Desktop_Agent5"):
        task_update(tid, status="waiting_review")
    [run] = _history(tid)
    assert run["outcome"] == "handed_off"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("blocked", "blocked"), ("open", "released"), ("cancelled", "cancelled")],
)
def test_leaving_doing_closes_the_run_with_the_status_outcome(status, outcome):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status=status)
    [run] = _history(tid)
    assert run["outcome"] == outcome


def test_release_claim_closes_the_run_as_released():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, release_claim=True)
    [run] = _history(tid)
    assert run["outcome"] == "released"


def test_takeover_after_lapsed_lease_closes_prior_run_as_abandoned():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    runs = _history(tid)
    assert [r["agent"] for r in runs] == ["Desktop_Agent2", "server_agent3"]
    assert runs[1]["outcome"] == "abandoned"
    assert runs[1]["finished_by"] == "Desktop_Agent2"
    assert runs[0]["state"] == "open"


def test_completion_with_no_recorded_claim_is_counted_not_hidden():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_update(tid, status="done", actor="Desktop_Agent1")
    [finish] = _events(run_records.KIND_FINISHED)
    assert finish["claim_recorded"] is False
    assert finish["run_id"] is None
    assert finish["finished_by"] == "Desktop_Agent1"
    cov = run_records.run_coverage(window_days=1)
    assert cov["completed"] == 1
    assert cov["recorded"] == 0
    assert cov["unrecorded_task_ids"] == [tid]
    assert cov["pct"] == 0.0


def test_coverage_counts_recorded_completions_and_is_none_when_empty():
    assert run_records.run_coverage(window_days=1)["pct"] is None
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="done")
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["pct"]) == (1, 1, 100.0)


def test_failing_recorder_never_fails_the_claim_and_leaves_a_trace(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("recorder down")

    # task_claim/task_update record atomically (task #1525) via the
    # `_in_tx` entrypoints, not the standalone `record_claim`/
    # `record_transition` (those remain for callers with no open
    # transaction of their own, e.g. the executor's direct-SQL paths).
    monkeypatch.setattr(run_records, "record_claim_in_tx", boom)
    monkeypatch.setattr(run_records, "record_transition_in_tx", boom)
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        assert claimed["status"] == "doing"
        assert claimed["claimed_by"] == "Desktop_Agent1"
        done = task_update(tid, status="done")
        assert done["status"] == "done"
    conn = storage.connect()
    try:
        traces = conn.execute(
            "SELECT ref_id, summary FROM agent_events WHERE kind='run_recorder'"
        ).fetchall()
    finally:
        conn.close()
    assert len(traces) == 2
    assert all(t["ref_id"] == str(tid) for t in traces)
    assert "recorder down" in traces[0]["summary"]
    assert run_records.run_coverage(window_days=1)["recorder_failures"] == 2


def test_event_log_failure_inside_recorder_does_not_fail_the_claim(monkeypatch):
    def broken_append(*_a, **_k):
        raise RuntimeError("event_log unavailable")

    monkeypatch.setattr(run_records.events, "record_event_once_on_connection", broken_append)
    tid = _new_task()
    with _as("Desktop_Agent1"):
        assert (
            task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")["status"] == "doing"
        )


def test_usage_is_typed_null_when_unreported_and_stored_when_reported():
    a, b = _new_task("a"), _new_task("b")
    with _as("Desktop_Agent1"):
        task_claim(a, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(a, status="done")
        task_claim(b, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(
            b,
            status="done",
            run_usage={"provider": "anthropic", "input_tokens": 1200, "cost_usd": 0.42},
        )
    [ra] = _history(a)
    assert ra["usage"] == run_records.empty_usage()
    assert ra["usage_reported"] is False
    [rb] = _history(b)
    assert rb["usage_reported"] is True
    assert rb["usage"] == {
        "provider": "anthropic",
        "model": None,
        "input_tokens": 1200,
        "output_tokens": None,
        "cost_usd": 0.42,
        "cost_complete": None,
        "billing": None,
        "phases": None,
        "contended": None,
        "capacity": None,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"tokens": 1},
        {"input_tokens": -1},
        {"input_tokens": "9"},
        {"cost_usd": True},
        "x",
        {"billing": "free"},
        {"contended": 1},
        {"cost_complete": 1},
        {"phases": {"build": -1}},
        {"phases": {"": 2}},
        {"phases": ["build"]},
    ],
)
def test_bad_usage_is_refused_before_any_write(bad):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        with pytest.raises(ToolError):
            task_update(tid, status="done", run_usage=bad)
    conn = storage.connect()
    try:
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    finally:
        conn.close()
    assert status == "doing"
    assert _history(tid)[0]["state"] == "open"


def test_in_flight_covers_every_path_and_flags_abandoned_and_unrecorded():
    running, stale, unrecorded, executor_task = (_new_task(n) for n in "abcd")
    with _as("Desktop_Agent1"):
        task_claim(running, "Desktop_Agent1", expected_lane="dispatch-ready")
    with _as("server_agent3"):
        task_claim(stale, "server_agent3", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), stale))
    # A 'doing' task no ledger covers (claimed before the recorder existed).
    _exec(
        "UPDATE tasks SET status='doing', claimed_by='Laptop_Agent2', claimed_at=?, "
        "lock_expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        (storage.now(), unrecorded),
    )
    # The executor daemon's own ledger, and a live node item with no task.
    now = storage.now()
    _exec(
        "UPDATE tasks SET status='doing', claimed_by='server_agent2', claimed_at=?, "
        "lock_expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        (now, executor_task),
    )
    _exec(
        "INSERT INTO executor_attempts (task_id, agent, backend, started_at) VALUES (?,?,?,?)",
        (executor_task, "server_agent2", "codex", now),
    )
    _exec(
        "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, status, "
        "created_by, created_at, claimed_at, updated_at) "
        "VALUES ('n1','mbp','server_agent3','agy','p','/tmp',60,'claimed','Operator',?,?,?)",
        (now, now, now),
    )

    view = run_records.runs_in_flight()
    by_task = {i["task_id"]: i for i in view["items"]}
    assert by_task[running]["state"] == "running"
    assert by_task[running]["sources"]["self_pickup"][0]["agent"] == "Desktop_Agent1"
    assert by_task[stale]["state"] == "abandoned"
    assert by_task[stale]["sources"]["self_pickup"][0]["abandon_reason"] == "lease_expired"
    assert by_task[unrecorded]["state"] == "unrecorded"
    assert by_task[executor_task]["state"] == "running"
    assert by_task[executor_task]["sources"]["executor"][0]["backend"] == "codex"
    assert by_task[None]["sources"]["node"][0]["id"] == "n1"
    assert view["counts"]["by_source"] == {
        "self_pickup": 2,
        "executor": 1,
        "dispatch": 0,
        "node": 1,
    }
    assert view["coverage"]["doing_tasks"] == 4
    assert view["coverage"]["unrecorded_task_ids"] == [unrecorded]
    assert view["counts"]["abandoned"] == 1


def test_never_claimed_task_is_not_in_flight_but_a_claimed_one_is():
    never, claimed = _new_task("never"), _new_task("claimed")
    with _as("Desktop_Agent1"):
        task_claim(claimed, "Desktop_Agent1", expected_lane="dispatch-ready")
    ids = {i["task_id"] for i in run_records.runs_in_flight()["items"]}
    assert claimed in ids and never not in ids


def test_task_leaving_doing_by_a_bypass_path_reads_as_abandoned():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET status='open' WHERE id=?", (tid,))
    [item] = [i for i in run_records.runs_in_flight()["items"] if i["task_id"] == tid]
    assert item["state"] == "abandoned"
    assert item["sources"]["self_pickup"][0]["abandon_reason"] == "task_left_doing"


def test_run_resource_resolves_self_pickup_ids():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    [run] = _history(tid)
    got = read_run(run["run_id"])
    assert got["run"]["run_id"] == run["run_id"]
    assert got["run"]["state"] == "open"
    assert got["run"]["path"] == "self_pickup"
    assert got["run"]["agent_label"] == "Desktop_Agent1"


def test_observability_manage_exposes_the_run_reads():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    flight = monitor.observability_manage("runs_in_flight")
    assert flight["counts"]["running"] == 1
    hist = monitor.observability_manage("run_history", {"task_id": tid})
    assert hist["count"] == 1
    cov = monitor.observability_manage("run_coverage", {"window_days": 7})
    assert cov["completed"] == 0


def test_run_history_reports_truncation():
    for n in range(3):
        tid = _new_task(str(n))
        with _as("Desktop_Agent1"):
            task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
            task_update(tid, status="done")
    page = run_records.run_history(limit=2)
    assert page["count"] == 2 and page["truncated"] is True
    assert run_records.run_history(agent="nobody")["runs"] == []


def test_review_then_done_is_one_run_and_counts_as_covered():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="waiting_review")
        task_update(tid, status="done")
    [run] = _history(tid)
    assert run["outcome"] == "handed_off"
    finishes = _events(run_records.KIND_FINISHED)
    assert len(finishes) == 1 and finishes[0]["claim_recorded"] is True
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"]) == (1, 1)


def test_queue_hygiene_release_closes_the_run_as_abandoned(monkeypatch):
    from unified_agent import queue_hygiene

    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(7200), tid))
    result = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert [r["task_id"] for r in result["released"]] == [tid]
    [run] = _history(tid)
    assert run["outcome"] == "abandoned"
    assert run["finished_by"] == "queue-hygiene"
    assert tid not in {i["task_id"] for i in run_records.runs_in_flight()["items"]}


def test_direct_sql_completion_closes_open_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    expected = run_records.capture_direct_completion(tid)
    assert run_records.complete_direct(expected, actor="merge-orchestrator")
    [run] = _history(tid)
    assert run["outcome"] == "completed"
    assert run["finished_by"] == "merge-orchestrator"


def test_open_run_on_a_finished_task_is_unclosed_not_in_flight():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET status='waiting_review' WHERE id=?", (tid,))
    view = run_records.runs_in_flight()
    assert tid not in {i["task_id"] for i in view["items"]}
    assert view["coverage"]["unclosed_runs"][0]["task_id"] == tid


# --- Devin review on PR #815 ---------------------------------------------------


def test_stale_claim_recorded_after_a_takeover_does_not_reverse_ownership():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    # server_agent3's claim recorder arrives late, after Desktop_Agent2's.
    late = run_records.record_claim(
        tid, agent="server_agent3", claimed_at=storage.now(), revision="late", prior_status="open"
    )
    assert late is None
    runs = _history(tid)
    assert runs[0]["agent"] == "Desktop_Agent2" and runs[0]["state"] == "open"
    assert all(r["agent"] != "Desktop_Agent2" or r["state"] == "open" for r in runs)
    conn = storage.connect()
    try:
        trace = conn.execute(
            "SELECT summary FROM agent_events WHERE kind='run_recorder'"
        ).fetchone()
    finally:
        conn.close()
    assert "superseded" in trace["summary"]


def test_stale_transition_is_not_applied_to_a_newer_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    out = run_records.record_transition(
        tid,
        old_status="doing",
        new_status="open",
        actor="Desktop_Agent1",
        claimed_by=None,
        revision="not-the-current-revision",
    )
    assert out is None
    assert _history(tid)[0]["state"] == "open"


def test_notes_update_after_transition_does_not_erase_the_run(monkeypatch):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        # Simulate a recorder delayed until a notes-only write commits (e.g.
        # a swallowed failure replayed later): task_update's in-transaction
        # write is `safe_in_tx` (task #1525), not the standalone `safe`.
        original_safe_in_tx = run_records.safe_in_tx
        monkeypatch.setattr(run_records, "safe_in_tx", lambda *args, **kwargs: None)
        row = task_update(tid, status="doing", worktree=False)
        monkeypatch.setattr(run_records, "safe_in_tx", original_safe_in_tx)
        revision = row["updated_at"]
        task_update(tid, append_note="additional context", worktree=False)
        result = run_records.record_transition(
            tid,
            old_status="open",
            new_status="doing",
            actor="Desktop_Agent1",
            claimed_by=None,
            revision=revision,
        )
    assert result["action"] == "opened"
    assert _history(tid)[0]["state"] == "open"


def test_late_review_completion_does_not_finish_a_stale_open_run_as_completed():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET status='waiting_review' WHERE id=?", (tid,))
    expected = run_records.capture_direct_completion(tid)
    assert run_records.complete_direct(expected, actor="review-cron")
    [run] = _history(tid)
    assert run["outcome"] == "abandoned"
    assert run["reason"] == "finish not recorded"


def test_notes_update_after_handoff_does_not_erase_the_finish():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        # Simulate a legacy direct-SQL handoff whose recorder arrives late.
        # Governed transitions now require the bound run to close atomically.
        revision = storage.now()
        _exec("UPDATE tasks SET status='waiting_review', updated_at=? WHERE id=?", (revision, tid))
        task_update(tid, append_note="review context", worktree=False)
        result = run_records.record_transition(
            tid,
            old_status="doing",
            new_status="waiting_review",
            actor="Desktop_Agent1",
            claimed_by="Desktop_Agent1",
            revision=revision,
        )
    assert result["outcome"] == "handed_off"
    assert _history(tid)[0]["outcome"] == "handed_off"


def test_old_completion_is_rejected_after_a_second_lifecycle():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        # A legacy completion bypassed the recorder; its delayed callback
        # must not overwrite the next governed claim/finish lifecycle.
        first_revision = storage.now()
        _exec(
            "UPDATE tasks SET status='done', updated_at=?, completed_at=? WHERE id=?",
            (first_revision, first_revision, tid),
        )
        task_update(tid, status="open", worktree=False)
        task_update(tid, status="doing", worktree=False)
        task_update(tid, status="done", worktree=False)
        before = len(_events(run_records.KIND_FINISHED))
        late = run_records.record_transition(
            tid,
            old_status="doing",
            new_status="done",
            actor="Desktop_Agent1",
            claimed_by="Desktop_Agent1",
            revision=first_revision,
        )
    assert late is None
    assert len(_events(run_records.KIND_FINISHED)) == before


def test_release_and_reclaim_within_one_second_are_two_runs():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="open")
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    runs = _history(tid)
    assert len(runs) == 2
    assert runs[1]["outcome"] == "released"
    assert runs[0]["state"] == "open"


def test_reentering_doing_closes_a_run_that_lost_its_finish():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    _exec(
        "UPDATE tasks SET status='open', claimed_by=NULL, lock_expires_at=NULL WHERE id=?", (tid,)
    )
    with _as("Desktop_Agent5"):
        task_update(tid, status="doing", worktree=False)
    runs = _history(tid)
    assert [r["agent"] for r in runs] == ["Desktop_Agent5", "server_agent3"]
    assert runs[1]["outcome"] == "abandoned"
    assert runs[0]["state"] == "open"


def test_reentering_doing_by_same_agent_starts_a_new_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET status='open', claimed_by=NULL WHERE id=?", (tid,))
    with _as("Desktop_Agent1"):
        task_update(tid, status="doing", worktree=False)
    runs = _history(tid)
    assert len(runs) == 2
    assert runs[0]["state"] == "open"
    assert runs[1]["outcome"] == "abandoned"


def test_direct_completion_of_a_never_claimed_task_is_an_unmatched_finish():
    tid = _new_task()
    expected = run_records.capture_direct_completion(tid)
    assert run_records.complete_direct(expected, actor="merge-orchestrator")
    [finish] = _events(run_records.KIND_FINISHED)
    assert finish["claim_recorded"] is False
    assert finish["finished_by"] == "merge-orchestrator"


def test_self_pickup_run_uri_revision_changes_when_the_run_finishes(monkeypatch):
    from unified_agent import mcp_live_state

    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    [run] = _history(tid)
    uri = mcp_live_state.run_uri(run["run_id"])
    before, cursor = mcp_live_state.snapshot_with_cursor()
    assert uri in before
    with _as("Desktop_Agent1"):
        task_update(tid, status="done")
    queries: list[str] = []
    original_connect = storage.connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(storage, "connect", traced_connect)
    after, _ = mcp_live_state.snapshot_with_cursor(before, cursor)
    assert after[uri] != before[uri]
    assert not any("LEFT JOIN event_log f" in sql for sql in queries)
    assert not any("json_extract(payload, '$.run_id')=" in sql for sql in queries)


def test_self_pickup_snapshot_reuses_prior_revisions_on_unrelated_event(monkeypatch):
    from unified_agent import events, mcp_live_state

    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    before, cursor = mcp_live_state.snapshot_with_cursor()
    events.record_event("test.unrelated", payload={"n": 1})
    queries: list[str] = []
    original_connect = storage.connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(storage, "connect", traced_connect)
    after, _ = mcp_live_state.snapshot_with_cursor(before, cursor)
    assert {uri: rev for uri, rev in before.items() if "/runs/sp-" in uri} == {
        uri: rev for uri, rev in after.items() if "/runs/sp-" in uri
    }
    assert not any("LEFT JOIN event_log f" in sql for sql in queries)


# --- Devin review on PR #815, round 2 -----------------------------------------


def test_stale_lock_cleanup_never_abandons_the_next_claimants_run():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    # A cleanup for server_agent3's expired claim lands after Desktop_Agent2 claimed.
    assert (
        run_records.record_abandoned(
            tid, agent="server_agent3", closed_by="queue-hygiene", reason="x"
        )
        is None
    )
    assert _history(tid)[0]["agent"] == "Desktop_Agent2"
    assert _history(tid)[0]["state"] == "open"


def test_stale_lock_cleanup_skips_a_same_agent_reclaim():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    assert (
        run_records.record_abandoned(
            tid, agent="server_agent3", closed_by="system-doctor", reason="x"
        )
        is None
    )
    assert _history(tid)[0]["state"] == "open"


def test_released_then_completed_without_a_claim_is_not_covered():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="open")
        task_update(tid, status="done", actor="Desktop_Agent1")
    finishes = _events(run_records.KIND_FINISHED)
    assert [f["claim_recorded"] for f in finishes] == [True, False]
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"]) == (1, 0)


def test_run_history_since_compares_instants_across_offsets():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    claimed = datetime.fromisoformat(_history(tid)[0]["claimed_at"])
    # One minute before the claim, written in a +05:00 offset: text comparison
    # would sort it after the UTC claim time and drop the run.
    earlier = (claimed - timedelta(minutes=1)).astimezone(timezone(timedelta(hours=5)))
    assert len(run_records.run_history(since=earlier.isoformat())["runs"]) == 1
    later = (claimed + timedelta(minutes=1)).isoformat()
    assert run_records.run_history(since=later)["runs"] == []
    with pytest.raises(ToolError):
        run_records.run_history(since="yesterday")


def test_run_history_fractional_since_does_not_admit_earlier_claims():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    claimed = datetime.fromisoformat(_history(tid)[0]["claimed_at"])
    just_after = (claimed + timedelta(milliseconds=500)).isoformat()
    assert run_records.run_history(since=just_after)["runs"] == []
    assert len(run_records.run_history(since=claimed.isoformat())["runs"]) == 1


def test_run_history_since_at_the_last_representable_second_is_empty():
    page = run_records.run_history(since="9999-12-31T23:59:59.500000+00:00")
    assert page == {"runs": [], "count": 0, "truncated": False, "limit": 100}


# --- current_run_id / current_run_agent cache (task #1526) ------------------
#
# This cache is what usage_ledger.resolve_run_id reads to attribute a provider
# call to a run in O(1), instead of scanning event_log. It must track the same
# open/close transitions run_history already asserts above.


def _cached(task_id: int) -> tuple[str | None, str | None]:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT current_run_id, current_run_agent FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["current_run_id"], row["current_run_agent"]


def test_current_run_cache_set_on_claim_and_cleared_on_finish():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        run_id, agent = _cached(tid)
        assert run_id == _history(tid)[0]["run_id"]
        assert agent == "Desktop_Agent1"
        task_update(tid, status="done", evidence_ref="https://example/pr/1")
    assert _cached(tid) == (None, None)


def test_current_run_cache_updated_on_takeover():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
        first_run_id, _ = _cached(tid)
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    run_id, agent = _cached(tid)
    assert agent == "Desktop_Agent2"
    assert run_id != first_run_id
    assert run_id == _history(tid)[0]["run_id"]


def test_current_run_cache_set_when_doing_entered_without_task_claim():
    tid = _new_task()
    with _as("Desktop_Agent5"):
        task_update(tid, status="doing", worktree=False)
    run_id, agent = _cached(tid)
    assert agent == "Desktop_Agent5"
    assert run_id == _history(tid)[0]["run_id"]


def test_current_run_cache_cleared_by_stale_lock_cleanup():
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    run_id, _ = _cached(tid)
    assert run_id is not None
    _exec("UPDATE tasks SET claimed_by=NULL WHERE id=?", (tid,))
    run_records.record_abandoned(tid, agent="server_agent3", closed_by="queue-hygiene", reason="x")
    assert _cached(tid) == (None, None)


def test_current_run_cache_not_clobbered_by_a_superseded_finish():
    """A finish for an already-superseded run must not clear a newer cache entry."""
    tid = _new_task()
    with _as("server_agent3"):
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    run_id, _ = _cached(tid)
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(60), tid))
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    # The abandon-finish for server_agent3's lapsed run was already written by the
    # takeover above; a redundant record_abandoned for the same stale claim
    # must not touch Desktop_Agent2's now-current cache entry.
    run_records.record_abandoned(tid, agent="server_agent3", closed_by="queue-hygiene", reason="x")
    new_run_id, agent = _cached(tid)
    assert agent == "Desktop_Agent2"
    assert new_run_id != run_id


# --- pre-upgrade backfill (task #1526 follow-up, Devin review of PR #875) ---
#
# A run claimed before the cache columns existed has NULL current_run_id even
# though its claim/finish events are already in event_log. These cover the
# three ways that gets fixed: renewal, a same-agent re-claim through
# the write-transaction renewal path, and the one-time migration backfill.


def test_renewal_of_a_pre_upgrade_claim_backfills_the_cache():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    run_id = _history(tid)[0]["run_id"]
    _exec("UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL WHERE id=?", (tid,))
    assert _cached(tid) == (None, None)
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")  # a renewal
    assert _cached(tid) == (run_id, "Desktop_Agent1")


def test_renewal_backfill_does_not_resurrect_a_run_finished_mid_renewal(monkeypatch):
    """A finish committed between the renewal's read and its backfill must
    leave the cleared cache alone (Devin review of PR #875)."""
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    run_id = _history(tid)[0]["run_id"]
    _exec("UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL WHERE id=?", (tid,))
    real_open_run_on = run_records._open_run_on
    reads = []

    def read_then_finish(conn, task_id):
        reads.append(task_id)
        held = real_open_run_on(conn, task_id)
        if len(reads) == 1:
            # Commit a real finish after the fast-path read, before the backfill.
            with _as("Desktop_Agent1"):
                task_update(tid, status="done", evidence_ref="https://example/pr/1")
        return held

    monkeypatch.setattr(run_records, "_open_run_on", read_then_finish)
    run_records.record_claim(tid, agent="Desktop_Agent1", claimed_at=storage.now(), renewal=True)

    assert _history(tid)[0]["run_id"] == run_id
    assert _history(tid)[0]["state"] == "finished"
    assert _cached(tid) == (None, None)


def test_renewal_already_correct_cache_is_idempotent():
    """A renewal when the cache is already correct must leave it unchanged
    (the guarded UPDATE's WHERE clause matches zero rows in that case)."""
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    before = _cached(tid)
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")  # a renewal
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")  # another
    assert _cached(tid) == before


def test_same_agent_renewal_in_transaction_backfills_the_cache():
    """The write-transaction renewal path also backfills pre-upgrade caches."""
    tid = _new_task()
    _exec("UPDATE tasks SET claimed_by='server_agent3', status='doing' WHERE id=?", (tid,))
    run_id = run_records.record_claim(tid, agent="server_agent3", claimed_at=storage.now())
    _exec(
        "UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL WHERE id=?",
        (tid,),
    )
    assert _cached(tid) == (None, None)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        tx = run_records.attach(conn)
        again = run_records.record_claim_in_tx(
            tx, tid, agent="server_agent3", claimed_at=storage.now(), renewal=True
        )
        conn.commit()
    finally:
        conn.close()
    run_records.flush(tx)
    assert again == run_id
    assert _cached(tid) == (run_id, "server_agent3")


def test_migration_backfills_doing_tasks_with_a_stale_null_cache():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    run_id = _history(tid)[0]["run_id"]
    _exec("UPDATE tasks SET current_run_id=NULL, current_run_agent=NULL WHERE id=?", (tid,))
    assert _cached(tid) == (None, None)
    storage.init_db()  # re-runs _migrate(); simulates a post-upgrade restart
    assert _cached(tid) == (run_id, "Desktop_Agent1")


def test_migration_backfill_is_a_no_op_once_the_cache_is_populated():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    before = _cached(tid)
    storage.init_db()
    assert _cached(tid) == before


def test_migration_backfill_ignores_a_doing_task_with_no_recorded_claim():
    """A 'doing' task with no self-pickup claim (predates run_records
    entirely) must not be given a fabricated run id."""
    tid = _new_task()
    _exec("UPDATE tasks SET status='doing', claimed_by='someone' WHERE id=?", (tid,))
    storage.init_db()
    assert _cached(tid) == (None, None)


@pytest.mark.parametrize("direct", [False, True])
def test_coverage_diagnostics_expose_unmatched_completion_without_inventing_a_claim(direct):
    tid = _new_task()
    if direct:
        assert run_records.complete_direct(
            run_records.capture_direct_completion(tid), actor="merge-orchestrator"
        )
    else:
        with _as("Desktop_Agent1"):
            task_update(tid, status="done", actor="Desktop_Agent1")
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["unrecorded"], cov["pct"]) == (1, 0, 1, 0.0)
    assert _history(tid) == []
    diagnostics = cov["diagnostics"]
    assert diagnostics["reason_counts"] == {"no_recorded_claim_event": 1}
    assert diagnostics["total"] == diagnostics["returned_count"] == 1
    assert diagnostics["truncated"] is False
    [item] = diagnostics["missing_tasks"]
    assert item["task_id"] == tid
    assert item["latest_claim"] is None
    assert item["matched_finish"] is None
    assert item["latest_unmatched_finish"]["run_id"] is None
    assert item["latest_unmatched_finish"]["claim_recorded"] is False
    assert item["latest_unmatched_finish"]["finished_by"] == (
        "merge-orchestrator" if direct else "Desktop_Agent1"
    )
    assert item["latest_unmatched_finish"]["event_ulid"]


@pytest.mark.parametrize("outcome", ["released", "blocked", "abandoned", "cancelled"])
def test_coverage_diagnostics_preserve_prior_noncovering_run_and_separate_unmatched_finish(
    outcome,
    monkeypatch,
):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    if outcome == "abandoned":
        from unified_agent import queue_hygiene

        monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)
        _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_past(7200), tid))
        queue_hygiene.run_hygiene_pass(send_notifications=False)
    else:
        with _as("Desktop_Agent1"):
            status = {"released": "open", "blocked": "blocked", "cancelled": "cancelled"}[outcome]
            task_update(tid, status=status)
    with _as("Desktop_Agent1"):
        task_update(tid, status="done", actor="Desktop_Agent1")
    [run] = _history(tid)
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["pct"]) == (1, 0, 0.0)
    assert cov["diagnostics"]["reason_counts"] == {f"latest_run_{outcome}": 1}
    [item] = cov["diagnostics"]["missing_tasks"]
    assert item["latest_claim"]["run_id"] == run["run_id"]
    assert item["latest_claim"]["claim_source"] == "task_claim"
    assert item["matched_finish"]["run_id"] == run["run_id"]
    assert item["matched_finish"]["outcome"] == outcome
    assert item["latest_unmatched_finish"]["run_id"] is None
    assert item["latest_unmatched_finish"]["event_ulid"] != item["matched_finish"]["event_ulid"]
    # Only an actual successor claim can change the latest recorded outcome.
    with _as("Desktop_Agent1"):
        task_update(tid, status="open")
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="done")
    covered = run_records.run_coverage(window_days=1)
    assert (covered["completed"], covered["recorded"], covered["pct"]) == (1, 1, 100.0)
    assert covered["diagnostics"]["reason_counts"] == {}


def test_coverage_diagnostics_preserve_older_handoff_and_recompletion_contract(monkeypatch):
    # Claims/handoffs may precede the cohort window; only completion selects it.
    old_stamp = _past(3 * 86400)
    with monkeypatch.context() as clock:
        clock.setattr(storage, "now", lambda: old_stamp)
        tid = _new_task()
        with _as("Desktop_Agent1"):
            task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
            task_update(tid, status="waiting_review")
            task_update(tid, status="done")
    with _as("Desktop_Agent1"):
        task_update(tid, status="open")
        task_update(tid, status="done", actor="Desktop_Agent1")
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["pct"]) == (1, 1, 100.0)
    assert cov["diagnostics"]["reason_counts"] == {}
    assert len(_history(tid)) == 1
    assert _history(tid)[0]["outcome"] == "handed_off"


def test_coverage_diagnostics_window_empty_and_full_counts_beyond_detail_cap(monkeypatch):
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(run_records, "datetime", FixedDatetime)
    empty = run_records.run_coverage(window_days=1)
    assert empty["pct"] is None
    assert empty["diagnostics"] == {
        "reason_counts": {},
        "missing_tasks": [],
        "returned_count": 0,
        "total": 0,
        "limit": 200,
        "truncated": False,
    }
    cutoff = (now - timedelta(days=1)).isoformat(timespec="seconds")
    ids = []
    # Retained administrative rows without events must stay honestly unknown.
    conn = storage.connect()
    try:
        for i in range(205):
            cur = conn.execute(
                "INSERT INTO tasks(title,status,priority,created_at,updated_at,completed_at) "
                "VALUES (?, 'done', 'med', ?, ?, ?)",
                (f"retained fixture {i}", cutoff, cutoff, cutoff),
            )
            ids.append(cur.lastrowid)
        for status, completed_at in (
            ("done", (now - timedelta(days=1, seconds=1)).isoformat(timespec="seconds")),
            ("done", None),
            ("open", cutoff),
            ("cancelled", cutoff),
        ):
            conn.execute(
                "INSERT INTO tasks(title,status,priority,created_at,updated_at,completed_at) "
                "VALUES ('excluded fixture', ?, 'med', ?, ?, ?)",
                (status, cutoff, cutoff, completed_at),
            )
        conn.commit()
    finally:
        conn.close()
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["unrecorded"], cov["pct"]) == (205, 0, 205, 0.0)
    assert cov["unrecorded_task_ids"] == ids[:200]
    diagnostics = cov["diagnostics"]
    assert diagnostics["reason_counts"] == {"no_recorded_claim_event": 205}
    assert diagnostics["total"] == 205
    assert diagnostics["returned_count"] == diagnostics["limit"] == 200
    assert diagnostics["truncated"] is True
    assert [item["task_id"] for item in diagnostics["missing_tasks"]] == ids[:200]
    assert all(item["latest_unmatched_finish"] is None for item in diagnostics["missing_tasks"])


@pytest.mark.parametrize("evidence", ["invalid-claim", "unknown-outcome", "open-run"])
def test_coverage_diagnostics_distinguish_retained_invalid_unknown_and_open_evidence(evidence):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        if evidence != "open-run":
            task_update(tid, status="blocked")
            task_update(tid, status="done", actor="Desktop_Agent1")
        else:
            _exec("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (storage.now(), tid))
    if evidence == "invalid-claim":
        claim, _ = _retained_run_events(tid)
        _append_retained_run_event({**claim, "run_id": None})
    elif evidence == "unknown-outcome":
        claim, finish = _retained_run_events(tid)
        legacy_id = f"retained-unknown-{tid}"
        _append_retained_run_event({**claim, "run_id": legacy_id})
        _append_retained_run_event(
            {**finish, "run_id": legacy_id, "outcome": "legacy_unknown"},
            dedupe_key=run_records._finish_key(legacy_id),
        )
    _assert_append_only_fixture_integrity()
    reason = {
        "invalid-claim": "invalid_recorded_claim",
        "unknown-outcome": "unknown_recorded_outcome",
        "open-run": "latest_run_open",
    }[evidence]
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["pct"]) == (1, 0, 0.0)
    assert cov["diagnostics"]["reason_counts"] == {reason: 1}
    [item] = cov["diagnostics"]["missing_tasks"]
    assert item["latest_claim"]["event_ulid"]
    if evidence == "open-run":
        assert item["matched_finish"] is None


@pytest.mark.parametrize(
    ("claim_json", "lookup_id", "finish_outcome", "legacy_outcome", "recorded", "reason"),
    [
        ("true", 1, "completed", "completed", 1, None),
        # Task #1887: a non-string run ID whose finish matched is labelled by
        # that finish's outcome, not as an invalid claim.
        ("true", 1, "blocked", "blocked", 0, "latest_run_blocked"),
        ("7", 7, "released", "released", 0, "latest_run_released"),
        ("7", 7, "legacy_unknown", "legacy_unknown", 0, "unknown_recorded_outcome"),
        ("false", 0, "completed", None, 0, "invalid_recorded_claim"),
        ("7", 7, "completed", "completed", 1, None),
        ("1.5", 1.5, "completed", "completed", 1, None),
    ],
)
def test_coverage_diagnostics_preserve_sql_retained_run_id_lookup(
    claim_json, lookup_id, finish_outcome, legacy_outcome, recorded, reason
):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="done")
    # Append retained legacy evidence through the real hashed writer, preserving JSON types.
    claim, finish = _retained_run_events(tid)
    _append_retained_run_event({**claim, "run_id": json.loads(claim_json)})
    _append_retained_run_event(
        {**finish, "run_id": lookup_id, "outcome": finish_outcome},
        dedupe_key=run_records._finish_key(lookup_id),
    )
    _assert_append_only_fixture_integrity()
    conn = storage.connect()
    try:
        assert run_records._latest_run_outcome(conn, tid) == legacy_outcome
        provenance = run_records._coverage_run_provenance(conn, tid)
        assert provenance["outcome"] == legacy_outcome
        if claim_json == "true":
            assert provenance["latest_claim"]["run_id"] is True
    finally:
        conn.close()
    cov = run_records.run_coverage(window_days=1)
    assert (cov["completed"], cov["recorded"], cov["unrecorded"], cov["pct"]) == (
        1,
        recorded,
        1 - recorded,
        100.0 * recorded,
    )
    assert cov["diagnostics"]["reason_counts"] == ({} if recorded else {reason: 1})


def test_coverage_diagnostics_reads_preserve_rows_history_usage_and_idempotent_completion():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_update(tid, status="done", actor="Desktop_Agent1", idempotency_key="private-done")
        task_update(tid, status="done", actor="Desktop_Agent1", idempotency_key="private-done")

    def stored():
        conn = storage.connect()
        try:
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("tasks", "event_log", "agent_events", "run_usage_heartbeats")
            }
        finally:
            conn.close()

    before = stored()
    cov = run_records.run_coverage(window_days=1)
    assert cov["diagnostics"]["reason_counts"] == {"no_recorded_claim_event": 1}
    assert run_records.run_coverage(window_days=1)["diagnostics"] == cov["diagnostics"]
    assert stored() == before
    assert _history(tid) == []
    assert cov["completed"] == 1


def test_coverage_diagnostics_use_one_snapshot_when_a_successor_claim_arrives(monkeypatch):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, status="done")
    original_connect = storage.connect
    successor = []
    errors = []

    def connected():
        conn = original_connect()

        def during_read(sql):
            if "FROM event_log" not in sql or successor:
                return
            successor.append(True)
            try:
                with _as("Desktop_Agent1"):
                    task_update(tid, status="open")
                    task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
                    task_update(tid, status="blocked")
                    task_update(tid, status="done", actor="Desktop_Agent1")
            except Exception as exc:
                errors.append(exc)

        conn.set_trace_callback(during_read)
        return conn

    monkeypatch.setattr(storage, "connect", connected)
    cov = run_records.run_coverage(window_days=1)
    assert successor and not errors
    assert (cov["completed"], cov["recorded"]) == (1, 1)
    assert cov["diagnostics"]["reason_counts"] == {}
    latest = run_records.run_coverage(window_days=1)
    assert latest["recorded"] == 0
    assert latest["diagnostics"]["reason_counts"] == {"latest_run_blocked": 1}


def _unmatched_finish_ulids(task_id: int) -> list[str]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
            "AND json_extract(payload, '$.task_id')=? "
            "AND json_extract(payload, '$.run_id') IS NULL ORDER BY ulid",
            (run_records.CHANNEL, run_records.KIND_FINISHED, task_id),
        ).fetchall()
    finally:
        conn.close()
    return [row["ulid"] for row in rows]


def _record_unmatched_finish_reads(monkeypatch) -> list[str]:
    """Record every unmatched-finish lookup that run_coverage issues."""
    original_connect = storage.connect
    reads: list[str] = []

    def connected():
        conn = original_connect()

        def trace(sql):
            if "'$.run_id') IS NULL" in sql:
                reads.append(sql)

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(storage, "connect", connected)
    return reads


def _missing_with_unmatched_finishes(n: int) -> list[int]:
    ids = []
    for _ in range(n):
        tid = _new_task()
        with _as("Desktop_Agent1"):
            task_update(tid, status="done", actor="Desktop_Agent1")
            task_update(tid, status="open")
            task_update(tid, status="done", actor="Desktop_Agent1")
        ids.append(tid)
    return ids


def test_coverage_diagnostics_batch_the_unmatched_finish_lookup(monkeypatch):
    """Task #1887: one unmatched-finish query for the detail page, not one per task."""
    ids = _missing_with_unmatched_finishes(3)
    covered = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(covered, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(covered, status="done")
    reads = _record_unmatched_finish_reads(monkeypatch)
    cov = run_records.run_coverage(window_days=1)
    assert len(reads) == 1, reads
    assert (cov["completed"], cov["recorded"], cov["unrecorded"]) == (4, 1, 3)
    details = {item["task_id"]: item for item in cov["diagnostics"]["missing_tasks"]}
    assert sorted(details) == ids
    for tid in ids:
        ulids = _unmatched_finish_ulids(tid)
        assert len(ulids) == 2
        # This task's latest unmatched finish, not an older one or another task's.
        assert details[tid]["latest_unmatched_finish"]["event_ulid"] == ulids[-1]


class _RowCountingConn:
    """Delegating connection that counts the rows each unmatched-finish read fetches."""

    def __init__(self, conn, fetched: list[int]):
        self._conn = conn
        self._fetched = fetched

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        if "'$.run_id') IS NULL" not in sql:
            return cursor
        rows = cursor.fetchall()
        self._fetched.append(len(rows))

        class _Fetched:
            def fetchall(self):
                return rows

        return _Fetched()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_coverage_unmatched_finish_read_is_bounded_by_task_not_history(monkeypatch):
    """Task #1887 (Devin, PR #1132): the batched lookup returns one row per task.

    A task completed without a claim many times keeps every null-run-ID finish.
    The read must select only the newest one in SQL, so its cost does not grow
    with the discarded history, and must still pick the same (newest) finish.
    """
    noisy = _new_task()
    with _as("Desktop_Agent1"):
        task_update(noisy, status="done", actor="Desktop_Agent1")
        for _ in range(11):
            task_update(noisy, status="open")
            task_update(noisy, status="done", actor="Desktop_Agent1")
    (quiet,) = _missing_with_unmatched_finishes(1)
    noisy_ulids = _unmatched_finish_ulids(noisy)
    quiet_ulids = _unmatched_finish_ulids(quiet)
    assert (len(noisy_ulids), len(quiet_ulids)) == (12, 2)

    fetched: list[int] = []
    conn = storage.connect()
    try:
        latest = run_records._latest_unmatched_finishes(
            _RowCountingConn(conn, fetched), [noisy, quiet]
        )
    finally:
        conn.close()
    assert fetched == [2], fetched
    assert {tid: row["ulid"] for tid, row in latest.items()} == {
        noisy: noisy_ulids[-1],
        quiet: quiet_ulids[-1],
    }

    original_connect = storage.connect
    coverage_fetched: list[int] = []
    monkeypatch.setattr(
        storage, "connect", lambda: _RowCountingConn(original_connect(), coverage_fetched)
    )
    cov = run_records.run_coverage(window_days=1)
    assert coverage_fetched == [2], coverage_fetched
    assert (cov["completed"], cov["recorded"], cov["unrecorded"]) == (2, 0, 2)
    details = {item["task_id"]: item for item in cov["diagnostics"]["missing_tasks"]}
    assert details[noisy]["latest_unmatched_finish"]["event_ulid"] == noisy_ulids[-1]
    assert details[quiet]["latest_unmatched_finish"]["event_ulid"] == quiet_ulids[-1]


def test_coverage_details_false_and_limit_keep_the_coverage_numbers(monkeypatch):
    """Task #1887: details=False and limit cap the detail list only."""
    _missing_with_unmatched_finishes(3)
    full = run_records.run_coverage(window_days=1)
    reads = _record_unmatched_finish_reads(monkeypatch)
    summary = run_records.run_coverage(window_days=1, details=False)
    assert reads == []
    keys = ("completed", "recorded", "unrecorded", "pct", "unrecorded_task_ids")
    assert {k: summary[k] for k in keys} == {k: full[k] for k in keys}
    diagnostics = summary["diagnostics"]
    assert diagnostics["reason_counts"] == full["diagnostics"]["reason_counts"]
    assert diagnostics["missing_tasks"] == []
    assert (diagnostics["returned_count"], diagnostics["total"]) == (0, 3)
    assert diagnostics["limit"] == 0 and diagnostics["truncated"] is True

    one = run_records.run_coverage(window_days=1, limit=1)
    assert {k: one[k] for k in keys} == {k: full[k] for k in keys}
    assert one["diagnostics"]["missing_tasks"] == full["diagnostics"]["missing_tasks"][:1]
    assert (one["diagnostics"]["returned_count"], one["diagnostics"]["limit"]) == (1, 1)
    assert one["diagnostics"]["truncated"] is True

    via_tool = monitor.observability_manage("run_coverage", {"window_days": 1, "details": False})
    assert via_tool["diagnostics"]["missing_tasks"] == []
    assert via_tool["recorded"] == full["recorded"]


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": -1}, {"limit": 201}, {"limit": True}, {"limit": "5"}, {"details": "no"}],
)
def test_coverage_rejects_an_invalid_details_option(kwargs):
    with pytest.raises(ToolError):
        run_records.run_coverage(window_days=1, **kwargs)
