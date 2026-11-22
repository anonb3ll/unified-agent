"""Self-pickup run fencing and cancellation (task #1525).

Follow-up to the vetoed PR #866, re-scoped onto the current event-log-based
recorder (#1436/#1526/#875): `task_update(..., request_cancel=True)` flags a
task's current run for cancellation (there is no standalone `request_cancel`
MCP tool here — see the task write-up for why), and
`task_update(..., expected_run_id=...)` is an optional fencing token a caller
can assert against `tasks.current_run_id`.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, run_records, storage
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


def _new_task(title: str = "t") -> int:
    return task_add(title, lane="dispatch-ready")["id"]


def _current_run(task_id: int) -> tuple[str | None, int]:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT current_run_id, cancel_requested FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["current_run_id"], row["cancel_requested"]


# --- cancellation --------------------------------------------------------


def test_request_cancel_sets_the_flag_and_is_visible_on_the_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        result = task_update(tid, request_cancel=True, worktree=False)
    assert result["cancel_requested"] == 1
    run_id, flag = _current_run(tid)
    assert flag == 1
    [run] = run_records.run_history(task_id=tid)["runs"]
    assert run["cancel_requested"] is True
    assert run["cancel_requested_by"] == "Desktop_Agent1"
    got = run_records.get_run(run_id)
    assert got["cancel_requested"] is True


def test_request_cancel_is_idempotent_and_writes_one_event():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, request_cancel=True, worktree=False)
        task_update(tid, request_cancel=True, worktree=False)
    conn = storage.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.kind')=?",
            (run_records.CHANNEL, run_records.KIND_CANCEL_REQUESTED),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_request_cancel_rolls_back_flag_when_event_write_fails(monkeypatch):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")

        def failed_append(*_args, **_kwargs):
            raise RuntimeError("event insert failed")

        monkeypatch.setattr(run_records, "_append", failed_append)
        with pytest.raises(RuntimeError, match="event insert failed"):
            task_update(tid, request_cancel=True, worktree=False)

    assert _current_run(tid) == (claimed["current_run_id"], 0)
    assert run_records.get_run(claimed["current_run_id"])["cancel_requested"] is False


def test_renewing_the_same_run_is_refused_after_cancellation():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, request_cancel=True, worktree=False)
        with pytest.raises(ToolError, match="cancellation request"):
            task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    assert _current_run(tid) == (claimed["current_run_id"], 1)


def test_task_claim_renewal_rejects_stale_run_id():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        with pytest.raises(ToolError, match="current run is not the expected"):
            task_claim(
                tid,
                "Desktop_Agent1",
                expected_lane="dispatch-ready",
                expected_run_id=claimed["current_run_id"] + "stale",
            )


def test_request_cancel_refuses_a_task_with_no_open_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        with pytest.raises(ToolError, match="no open run to cancel"):
            task_update(tid, request_cancel=True, worktree=False)


def test_request_cancel_does_not_reopen_a_completed_task():
    """A completed task with a stray open claim event must never be
    cancellable (PR #866 review: cancellation must not target a finished
    task)."""
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    # Direct-SQL completion, bypassing task_update: leaves the claim event
    # open (no finish) while the task itself is done.
    _exec("UPDATE tasks SET status='done', current_run_id=NULL WHERE id=?", (tid,))
    with _as("Desktop_Agent1"):
        with pytest.raises(ToolError, match="no open run to cancel"):
            task_update(tid, request_cancel=True, worktree=False, status="done")


def test_request_cancel_is_scoped_to_its_own_task():
    """Cancelling task A's run must never be reachable through task B, even
    when B is also in flight (PR #866 review: task-scoped cancellation)."""
    a, b = _new_task("a"), _new_task("b")
    with _as("Desktop_Agent1"):
        task_claim(a, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_claim(b, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(a, request_cancel=True, worktree=False)
    _, a_flag = _current_run(a)
    _, b_flag = _current_run(b)
    assert a_flag == 1
    assert b_flag == 0


def test_stranger_cannot_cancel_another_agents_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    with _as("Desktop_Agent2"):
        with pytest.raises(ToolError, match="may not cancel"):
            task_update(tid, request_cancel=True, worktree=False)
    assert _current_run(tid)[1] == 0


def test_operator_can_cancel_another_agents_run(monkeypatch):
    from unified_agent import agent_lifecycle

    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "server_agent5")
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    assert "server_agent5" in agent_lifecycle.operators()
    with _as("server_agent5"):
        result = task_update(tid, request_cancel=True, worktree=False)
    assert result["cancel_requested"] == 1


def test_reclaiming_after_release_clears_a_stale_cancel_flag():
    """A fresh run must never inherit a previous, already-closed run's
    cancellation flag on the same task."""
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        task_update(tid, request_cancel=True, worktree=False)
        task_update(tid, release_claim=True, worktree=False)
        assert _current_run(tid) == (None, 0)
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _, flag = _current_run(tid)
    assert flag == 0


# --- fencing ---------------------------------------------------------------


def test_expected_run_id_matching_the_current_run_succeeds():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        run_id = claimed["current_run_id"]
        result = task_update(tid, status="done", expected_run_id=run_id)
    assert result["status"] == "done"
    assert result["current_run_id"] is None
    assert result["cancel_requested"] == 0


def test_finished_run_idempotency_receipt_matches_committed_row():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        result = task_update(
            tid,
            status="done",
            expected_run_id=claimed["current_run_id"],
            idempotency_key=f"finish-{tid}",
        )
        replay = task_update(tid, status="done", idempotency_key=f"finish-{tid}")
    assert result == replay
    assert result["current_run_id"] is None


def test_stale_expected_run_id_is_refused():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        claimed = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        stale_run_id = claimed["current_run_id"]
        # Lease lapses; a second agent takes over, opening a new run.
        _exec(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", tid),
        )
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    with _as("Desktop_Agent1"):
        with pytest.raises(ToolError, match="current run is"):
            task_update(tid, status="done", expected_run_id=stale_run_id)
    # The task is unaffected by the refused, stale-fenced call.
    assert _current_run(tid)[0] != stale_run_id


def test_same_agent_claim_after_lease_lapse_opens_a_new_run():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        first = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        old_run = first["current_run_id"]
        _exec(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", tid),
        )
        with pytest.raises(ToolError, match="lease expired"):
            task_claim(
                tid,
                "Desktop_Agent1",
                expected_lane="dispatch-ready",
                expected_run_id=old_run,
            )
        second = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        assert second["current_run_id"] != old_run
        with pytest.raises(ToolError, match="current run is"):
            task_update(tid, status="done", expected_run_id=old_run)


def test_expected_run_id_is_optional_and_enforces_nothing_when_omitted():
    tid = _new_task()
    with _as("Desktop_Agent1"):
        task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        result = task_update(tid, status="done")
    assert result["status"] == "done"


@pytest.mark.parametrize("successor", ["Desktop_Agent1", "Desktop_Agent2"])
@pytest.mark.parametrize("failure_point", ["recorder", "new_claim_event"])
def test_takeover_rolls_back_when_bound_run_recording_fails(monkeypatch, successor, failure_point):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        first = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
    _exec(
        "UPDATE tasks SET lock_expires_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", tid),
    )
    before = _task_snapshot(tid)
    original_append = run_records._append

    def failed_recorder(*_args, **_kwargs):
        raise RuntimeError("recorder unavailable")

    def failed_new_claim(tx, payload, originator, key):
        if payload["kind"] == run_records.KIND_CLAIMED:
            raise RuntimeError("new claim event unavailable")
        return original_append(tx, payload, originator, key)

    if failure_point == "recorder":
        monkeypatch.setattr(run_records, "record_claim_in_tx", failed_recorder)
    else:
        monkeypatch.setattr(run_records, "_append", failed_new_claim)
    with _as(successor):
        with pytest.raises(ToolError, match="run recording failed"):
            task_claim(tid, successor, expected_lane="dispatch-ready")
    assert _task_snapshot(tid) == before
    [run] = run_records.run_history(task_id=tid)["runs"]
    assert run["run_id"] == first["current_run_id"]
    assert run["state"] == "open"


@pytest.mark.parametrize("transition", ["release", "open", "blocked", "done"])
def test_bound_run_transition_rolls_back_when_recording_fails(monkeypatch, transition):
    tid = _new_task()
    with _as("Desktop_Agent1"):
        first = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")
        before = _task_snapshot(tid)

        def failed_append(*_args, **_kwargs):
            raise RuntimeError("finish event unavailable")

        monkeypatch.setattr(run_records, "_append", failed_append)
        arguments = {"release_claim": True} if transition == "release" else {"status": transition}
        with pytest.raises(ToolError, match="run recording failed"):
            task_update(
                tid,
                expected_run_id=first["current_run_id"],
                worktree=False,
                idempotency_key=f"failed-transition-{tid}",
                **arguments,
            )
    assert _task_snapshot(tid) == before
    [run] = run_records.run_history(task_id=tid)["runs"]
    assert run["state"] == "open"
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    finally:
        conn.close()


def _task_snapshot(task_id):
    conn = storage.connect()
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    finally:
        conn.close()
