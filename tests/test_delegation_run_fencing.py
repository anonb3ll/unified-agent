"""A delegation generation cannot publish an obsolete task run's outcome."""

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools import delegate as delegation_tools
from unified_agent.tools import tasks


@pytest.fixture
def attempt(isolated_env, monkeypatch):
    monkeypatch.setattr(config, "agent_skips_task_worktree", lambda _agent: True)
    result = delegation_tools.delegate(
        "Implement a bounded regression", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    claimed = delegation_tools.delegate_claim(result["delegation_id"], "Desktop_agent3")[
        "delegation"
    ]
    task = tasks.task_claim(result["task_id"], "Desktop_agent3")
    return {
        "id": result["delegation_id"],
        "task_id": result["task_id"],
        "agent": "Desktop_agent3",
        "expected_claim_generation": claimed["claim_generation"],
        "expected_run_id": task["current_run_id"],
    }


@pytest.mark.parametrize(
    "action,key",
    [("release", "released"), ("retry", "retried"), ("fail", "failed"), ("complete", "completed")],
)
@pytest.mark.parametrize("change", ["successor", "cancel", "status", "owner", "missing"])
def test_same_generation_cannot_publish_after_task_authority_changes(attempt, action, key, change):
    conn = storage.connect()
    before = dict(conn.execute("SELECT * FROM delegations WHERE id=?", (attempt["id"],)).fetchone())
    if change == "successor":
        conn.execute(
            "UPDATE tasks SET current_run_id='sp-successor' WHERE id=?", (attempt["task_id"],)
        )
    elif change == "cancel":
        conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (attempt["task_id"],))
    elif change == "status":
        conn.execute(
            "UPDATE tasks SET status='blocked', notes='new notes' WHERE id=?", (attempt["task_id"],)
        )
    elif change == "owner":
        conn.execute(
            "UPDATE tasks SET current_run_agent='Desktop_Agent2', claimed_by='Desktop_Agent2' WHERE id=?",
            (attempt["task_id"],),
        )
    else:
        conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (attempt["task_id"],))
    conn.commit()
    conn.close()
    result = delegation_tools.delegation_manage(action, attempt)
    assert result[key] is False
    assert result["reason"] == "run_not_current"
    conn = storage.connect()
    assert (
        dict(conn.execute("SELECT * FROM delegations WHERE id=?", (attempt["id"],)).fetchone())
        == before
    )
    conn.close()


@pytest.mark.parametrize(
    "action,key",
    [("release", "released"), ("retry", "retried"), ("fail", "failed"), ("complete", "completed")],
)
def test_current_owned_run_can_publish(attempt, action, key):
    assert delegation_tools.delegation_manage(action, attempt)[key] is True


def test_forged_task_binding_or_actor_does_not_grant_authority(attempt):
    assert not delegation_tools.delegation_manage("complete", {**attempt, "task_id": 999})[
        "completed"
    ]
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="identity|caller|owner"):
            delegation_tools.delegation_manage("complete", attempt)
    finally:
        config.agent_identity_var.reset(token)


def test_real_review_finish_can_complete_once_but_not_after_a_successor(attempt):
    tasks.task_update(
        attempt["task_id"],
        status="waiting_review",
        append_note="[waiting-for: review] Delivered",
        worktree=False,
        actor=attempt["agent"],
        expected_run_id=attempt["expected_run_id"],
    )
    conn = storage.connect()
    assert (
        conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (attempt["task_id"],)
        ).fetchone()[0]
        is None
    )
    conn.close()
    assert delegation_tools.delegation_manage("complete", attempt)["completed"]
    assert not delegation_tools.delegation_manage("complete", attempt)["completed"]


def test_review_finish_cannot_complete_when_same_agent_has_started_a_successor(attempt):
    tasks.task_update(
        attempt["task_id"],
        status="waiting_review",
        append_note="[waiting-for: review] Delivered",
        worktree=False,
        actor=attempt["agent"],
        expected_run_id=attempt["expected_run_id"],
    )
    successor = tasks.task_claim(attempt["task_id"], attempt["agent"])
    assert successor["current_run_id"] != attempt["expected_run_id"]
    assert not delegation_tools.delegation_manage("complete", attempt)["completed"]


@pytest.mark.parametrize("invalid", ["", " ", True, 5, "x" * 257])
def test_run_token_is_validated(attempt, invalid):
    with pytest.raises(ToolError, match="expected_run_id"):
        delegation_tools.delegation_manage("complete", {**attempt, "expected_run_id": invalid})


@pytest.mark.parametrize("clear_backoff", [False, True])
def test_recovery_backoff_option_retains_attempt_history(attempt, clear_backoff):
    before = delegation_tools.delegate_get(attempt["id"])["delegation"]
    assert before["last_attempt_at"] is not None
    assert delegation_tools.delegation_manage("retry", {**attempt, "clear_backoff": clear_backoff})[
        "retried"
    ]
    after = delegation_tools.delegate_get(attempt["id"])["delegation"]
    assert after["attempts"] == before["attempts"]
    assert after["claim_generation"] == before["claim_generation"]
    assert after["last_attempt_at"] == (None if clear_backoff else before["last_attempt_at"])


def test_stale_recovery_cannot_clear_successor_backoff(attempt):
    before = delegation_tools.delegate_get(attempt["id"])["delegation"]
    conn = storage.connect()
    conn.execute("UPDATE tasks SET current_run_id='sp-successor' WHERE id=?", (attempt["task_id"],))
    conn.commit()
    conn.close()
    assert not delegation_tools.delegation_manage("retry", {**attempt, "clear_backoff": True})[
        "retried"
    ]
    assert delegation_tools.delegate_get(attempt["id"])["delegation"] == before


@pytest.mark.parametrize("status", ["done", "blocked", "open", "cancelled"])
def test_handed_off_run_completes_even_after_reviewer_moves_task(attempt, status):
    tasks.task_update(
        attempt["task_id"],
        status="waiting_review",
        worktree=False,
        append_note="[waiting-for: review] Delivered",
        actor=attempt["agent"],
        expected_run_id=attempt["expected_run_id"],
    )
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, attempt["task_id"]))
        conn.commit()
    assert delegation_tools.delegation_manage("complete", attempt)["completed"]


def test_run_fenced_cancel_requires_generation(attempt):
    with pytest.raises(ToolError, match="expected_claim_generation"):
        delegation_tools.delegate_cancel(
            attempt["id"], attempt["agent"], expected_run_id=attempt["expected_run_id"]
        )


def test_cancel_from_stale_generation_leaves_successor_in_flight(attempt):
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET current_run_id='sp-successor' WHERE id=?", (attempt["task_id"],)
        )
        conn.execute(
            "UPDATE delegations SET claim_generation=claim_generation+1 WHERE id=?",
            (attempt["id"],),
        )
        conn.commit()
    assert not delegation_tools.delegate_cancel(
        attempt["id"],
        attempt["agent"],
        expected_run_id=attempt["expected_run_id"],
        expected_claim_generation=attempt["expected_claim_generation"],
    )["cancelled"]
    assert delegation_tools.delegate_get(attempt["id"])["delegation"]["status"] == "in_flight"


@pytest.mark.parametrize("task_id", [True, 0, -1, "1"])
def test_task_scoped_inventory_validates_task_id(attempt, task_id):
    with pytest.raises(ToolError, match="task_id"):
        delegation_tools.delegate_list(status="all", task_id=task_id)


def test_task_scoped_inventory_reports_completeness(attempt):
    extra = delegation_tools.delegate(
        "Second matching delegation", "Desktop_Agent1", "Desktop_agent3", callback="none"
    )
    with storage.connect() as conn:
        conn.execute(
            "UPDATE delegations SET task_id=? WHERE id=?",
            (attempt["task_id"], extra["delegation_id"]),
        )
        conn.commit()
    first = delegation_tools.delegate_list(status="all", task_id=attempt["task_id"], limit=1)
    assert len(first["delegations"]) == 1
    assert first["truncated"] is True
    complete = delegation_tools.delegate_list(status="all", task_id=attempt["task_id"], limit=2)
    assert len(complete["delegations"]) == 2
    assert complete["truncated"] is False
    assert all(row["task_id"] == attempt["task_id"] for row in complete["delegations"])


@pytest.mark.parametrize("successor", ["generation", "delegation"])
def test_cancel_task_publication_cannot_retire_successor_attempt(attempt, monkeypatch, successor):
    from unified_agent import run_records

    original_flush = run_records.flush
    observed = {}

    def publish_then_reopen(tx):
        original_flush(tx)
        if observed:
            return
        with storage.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (attempt["task_id"],)).fetchone()
        if task["status"] != "cancelled":
            return
        observed["started"] = True
        token = config.agent_identity_var.set(attempt["agent"])
        try:
            tasks.task_update(attempt["task_id"], status="doing", worktree=False)
            tasks.task_claim(attempt["task_id"], attempt["agent"])
            with storage.connect() as conn:
                if successor == "generation":
                    successor_id = attempt["id"]
                    conn.execute(
                        "UPDATE delegations SET status='pending' WHERE id=?", (successor_id,)
                    )
                else:
                    row = dict(
                        conn.execute(
                            "SELECT * FROM delegations WHERE id=?", (attempt["id"],)
                        ).fetchone()
                    )
                    row.pop("id")
                    row["status"] = "pending"
                    row["claim_generation"] = 0
                    columns = ",".join(row)
                    placeholders = ",".join("?" for _ in row)
                    successor_id = conn.execute(
                        f"INSERT INTO delegations ({columns}) VALUES ({placeholders})",
                        tuple(row.values()),
                    ).lastrowid
                conn.commit()
            claimed = delegation_tools.delegate_claim(successor_id, attempt["agent"])
            assert claimed["claimed"]
            observed["successor"] = claimed["delegation"]
            with storage.connect() as conn:
                observed["task"] = dict(
                    conn.execute("SELECT * FROM tasks WHERE id=?", (attempt["task_id"],)).fetchone()
                )
        finally:
            config.agent_identity_var.reset(token)

    monkeypatch.setattr(run_records, "flush", publish_then_reopen)
    token = config.agent_identity_var.set(attempt["agent"])
    try:
        tasks.task_update(
            attempt["task_id"],
            status="cancelled",
            release_claim=True,
            worktree=False,
            expected_run_id=attempt["expected_run_id"],
            expected_delegation_id=attempt["id"],
            expected_claim_generation=attempt["expected_claim_generation"],
        )
    finally:
        config.agent_identity_var.reset(token)
    assert observed["task"]["current_run_id"] != attempt["expected_run_id"]
    assert observed["task"]["status"] == "doing"
    assert (
        delegation_tools.delegate_get(observed["successor"]["id"])["delegation"]
        == observed["successor"]
    )
    with storage.connect() as conn:
        assert (
            dict(conn.execute("SELECT * FROM tasks WHERE id=?", (attempt["task_id"],)).fetchone())
            == observed["task"]
        )


def test_cancel_task_and_delegations_roll_back_together(attempt, monkeypatch):
    with storage.connect() as conn:
        before = dict(
            conn.execute("SELECT * FROM tasks WHERE id=?", (attempt["task_id"],)).fetchone()
        )
        conn.execute(
            "CREATE TRIGGER reject_delegation_cancel BEFORE UPDATE OF status ON delegations WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT, 'synthetic cancellation refusal'); END"
        )
        conn.commit()
    token = config.agent_identity_var.set(attempt["agent"])
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic cancellation refusal"):
            tasks.task_update(
                attempt["task_id"],
                status="cancelled",
                release_claim=True,
                worktree=False,
                expected_run_id=attempt["expected_run_id"],
                expected_delegation_id=attempt["id"],
                expected_claim_generation=attempt["expected_claim_generation"],
            )
    finally:
        config.agent_identity_var.reset(token)
    with storage.connect() as conn:
        assert (
            dict(conn.execute("SELECT * FROM tasks WHERE id=?", (attempt["task_id"],)).fetchone())
            == before
        )
    assert delegation_tools.delegate_get(attempt["id"])["delegation"]["status"] == "in_flight"
