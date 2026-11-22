"""Durable routine creation and scheduler behavior."""

from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, edition, profiles, storage
from unified_agent.routines import (
    SKIP_REASONS,
    cron_next,
    routine_add,
    routine_fire,
    routine_list,
    routine_pause,
    routine_tick,
)
from unified_agent.tools.tasks import task_claim, task_update


def test_routine_tools_require_chief_of_staff_profile():
    from unified_agent.server import REGISTERED_TOOLS

    names = set(REGISTERED_TOOLS)
    routine_tools = {"routine_add", "routine_list", "routine_pause", "routine_fire"}
    assert routine_tools.isdisjoint(profiles._allowed_tools_for_profile(profiles.DEVELOPER, names))
    assert routine_tools <= profiles._allowed_tools_for_profile(profiles.CHIEF_OF_STAFF, names)


def test_cron_next_uses_utc_minute_boundary():
    assert cron_next("*/15 * * * *", datetime(2026, 9, 25, 7, 1, tzinfo=UTC)) == datetime(
        2026, 9, 25, 7, 15, tzinfo=UTC
    )


def test_sparse_cron_reaches_next_leap_day():
    assert cron_next("0 0 29 2 *", datetime(2029, 1, 1, tzinfo=UTC)) == datetime(
        2032, 2, 29, tzinfo=UTC
    )
    monday_leap_day = cron_next("0 0 29 2 1", datetime(2029, 1, 1, tzinfo=UTC))
    assert monday_leap_day.month == 2 and monday_leap_day.day == 29
    assert monday_leap_day.weekday() == 0


def test_tick_creates_one_tracked_task_and_is_idempotent():
    routine_add(
        "Review anomalies",
        schedule="*/5 * * * *",
        lane="dispatch-ready",
        start_at="2026-09-25T07:00:00+00:00",
    )
    at = datetime(2026, 9, 25, 7, 10, tzinfo=UTC)
    assert routine_tick(now=at, force=True)["created"] == 1
    assert routine_tick(now=at, force=True)["created"] == 0
    with storage.connect() as conn:
        tasks = conn.execute("SELECT id, title, lane FROM tasks").fetchall()
        runs = conn.execute("SELECT task_id, scheduled_for FROM routine_runs").fetchall()
    assert len(tasks) == len(runs) == 1
    assert tasks[0]["lane"] == "dispatch-ready"
    assert runs[0]["task_id"] == tasks[0]["id"]
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.updated'"
            ).fetchone()[0]
            == 1
        )


def test_api_fire_is_idempotent_and_pause_blocks_firing(monkeypatch):
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    routine = routine_add("Manual checklist", trigger="api")
    first = routine_fire(routine["id"], idempotency_key="event-1")
    assert routine_fire(routine["id"], idempotency_key="event-1") == first
    routine_pause(routine["id"], paused=True)
    assert routine_fire(routine["id"], idempotency_key="event-1") == first
    with pytest.raises(ToolError, match="paused"):
        routine_fire(routine["id"], idempotency_key="event-2")
    assert routine_list()["routines"][0]["paused"] == 1


def test_skip_concurrency_does_not_create_duplicate_open_work():
    routine_add(
        "One at a time",
        schedule="* * * * *",
        concurrency="skip",
        start_at="2026-09-25T07:00:00+00:00",
    )
    assert routine_tick(now=datetime(2026, 9, 25, 7, 1, tzinfo=UTC), force=True)["created"] == 1
    result = routine_tick(now=datetime(2026, 9, 25, 7, 2, tzinfo=UTC), force=True)
    assert result["created"] == 0
    assert result["skipped"] == 1
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_queue_releases_next_only_after_previous_task_completes():
    routine_add(
        "Serial review",
        schedule="* * * * *",
        concurrency="queue",
        catch_up="all",
        start_at="2026-09-25T07:00:00+00:00",
    )
    result = routine_tick(now=datetime(2026, 9, 25, 7, 2, tzinfo=UTC), force=True)
    assert (result["created"], result["queued"]) == (1, 1)
    with storage.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM tasks ORDER BY id")] == [
            "open",
            "blocked",
        ]
        queued_id = conn.execute("SELECT id FROM tasks WHERE status='blocked'").fetchone()[0]
    with pytest.raises(ToolError, match="queued routine"):
        task_claim(queued_id, agent="Desktop_Agent2")
    with pytest.raises(ToolError, match="queued routine"):
        task_update(queued_id, status="doing", worktree=False)
    with pytest.raises(ToolError, match="queued routine"):
        task_update(queued_id, status="done", worktree=False)
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=1")
    assert routine_tick(now=datetime(2026, 9, 25, 7, 2, tzinfo=UTC), force=True)["released"] == 1
    with storage.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM tasks ORDER BY id")] == [
            "done",
            "open",
        ]
        assert (
            conn.execute(
                "SELECT count(*) FROM event_log WHERE channel='agent.task.updated'"
            ).fetchone()[0]
            == 3
        )


def test_catch_up_last_emits_latest_only():
    routine_add(
        "Latest only", schedule="* * * * *", catch_up="last", start_at="2026-09-25T07:00:00+00:00"
    )
    assert routine_tick(now=datetime(2026, 9, 25, 7, 10, tzinfo=UTC), force=True)["created"] == 1
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT scheduled_for FROM routine_runs")
            .fetchone()[0]
            .startswith("2026-09-25T07:10")
        )


def test_last_after_long_outage_uses_actual_latest_fire():
    routine_add(
        "Latest after outage",
        schedule="* * * * *",
        catch_up="last",
        start_at="2026-09-25T07:00:00+00:00",
    )
    result = routine_tick(now=datetime(2026, 9, 25, 10, 20, tzinfo=UTC), force=True)
    assert result["created"] == 1
    assert not result.get("backlog")
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT scheduled_for FROM routine_runs")
            .fetchone()[0]
            .startswith("2026-09-25T10:20")
        )


def test_resume_does_not_replay_paused_minutes():
    routine = routine_add(
        "No paused replay",
        schedule="* * * * *",
        catch_up="all",
        start_at="2026-09-25T07:00:00+00:00",
    )
    routine_pause(routine["id"], paused=True)
    routine_pause(routine["id"], paused=False)
    assert routine_tick(now=datetime.now(UTC), force=True)["created"] == 0


def test_manual_fire_requires_routine_opt_in(monkeypatch):
    routine = routine_add("Manual opt in", trigger="api")
    monkeypatch.delenv("UA_ROUTINES_ENABLED", raising=False)
    with pytest.raises(ToolError, match="disabled"):
        routine_fire(routine["id"], idempotency_key="event-1")


def test_invalid_routine_input_uses_actionable_tool_error():
    with pytest.raises(ToolError, match="invalid cron field"):
        routine_add("Bad schedule", schedule="oops * * * *")
    with pytest.raises(ToolError, match="invalid start_at"):
        routine_add("Bad timestamp", schedule="* * * * *", start_at="not-a-time")


def test_untrusted_routine_creates_untrusted_task(monkeypatch):
    monkeypatch.setattr(config, "is_agent_untrusted", lambda _agent: True)
    routine_add("Untrusted routine", schedule="* * * * *", start_at="2026-09-25T07:00:00+00:00")
    routine_tick(now=datetime(2026, 9, 25, 7, 1, tzinfo=UTC), force=True)
    with storage.connect() as conn:
        assert conn.execute("SELECT untrusted FROM tasks").fetchone()[0] == 1


LEDGER_START = "2026-09-25T06:00:00+00:00"


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=UTC)


def _runs(routine_id: int) -> list:
    with storage.connect() as conn:
        return conn.execute(
            "SELECT * FROM routine_runs WHERE routine_id=? ORDER BY id", (routine_id,)
        ).fetchall()


def _task_total() -> int:
    with storage.connect() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def _hourly_none(title: str = "Hourly") -> int:
    return routine_add(title, schedule="0 * * * *", catch_up="none", start_at=LEDGER_START)["id"]


def test_overlap_skip_names_the_active_task():
    routine_id = routine_add("One at a time", schedule="* * * * *", start_at=LEDGER_START)["id"]
    routine_tick(now=_at(7, 1), force=True)

    result = routine_tick(now=_at(7, 2), force=True)

    first, second = _runs(routine_id)
    assert (first["status"], first["reason"]) == ("created", None)
    assert first["subject_key"] == f"routine:{routine_id}"
    assert (second["status"], second["reason"]) == ("skipped", "overlap")
    assert second["subject_key"] == f"task:{first['task_id']}"
    assert result["skip_reasons"] == {"overlap": 1}


def test_queued_fire_subject_is_the_routine():
    routine_id = routine_add(
        "Serial", schedule="* * * * *", concurrency="queue", catch_up="all", start_at=LEDGER_START
    )["id"]
    routine_tick(now=_at(6, 2), force=True)

    assert [(r["status"], r["reason"], r["subject_key"]) for r in _runs(routine_id)] == [
        ("created", None, f"routine:{routine_id}"),
        ("queued", None, f"routine:{routine_id}"),
    ]


def test_api_fire_overlap_keeps_return_shape_and_records_the_reason(monkeypatch):
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    routine_id = routine_add("Manual", trigger="api")["id"]
    first = routine_fire(routine_id, idempotency_key="event-1")

    assert routine_fire(routine_id, idempotency_key="event-2") == {
        "status": "skipped",
        "task_id": None,
    }
    run = _runs(routine_id)[1]
    assert (run["reason"], run["subject_key"]) == ("overlap", f"task:{first['task_id']}")


def test_passed_over_slot_writes_one_receipt_and_no_task():
    routine_id = _hourly_none()

    result = routine_tick(now=_at(8, 30), force=True)

    assert (result["created"], result["skipped"]) == (0, 1)
    assert result["skip_reasons"] == {"catch_up_none": 1}
    assert _task_total() == 0
    [run] = _runs(routine_id)
    assert run["fire_key"] == "cron:" + _at(8, 0).isoformat()
    assert run["scheduled_for"] == _at(8, 0).isoformat()
    assert (run["status"], run["reason"]) == ("skipped", "catch_up_none")
    assert run["subject_key"] == f"routine:{routine_id}"
    assert run["task_id"] is None

    repeat = routine_tick(now=_at(8, 30), force=True)
    assert (repeat["skipped"], repeat["skip_reasons"]) == (0, {})
    assert len(_runs(routine_id)) == 1


def test_racing_tick_does_not_duplicate_a_passed_over_slot():
    routine_id = _hourly_none()
    routine_tick(now=_at(8, 30), force=True)
    # A second tick that read the old cursor reaches the same slot.
    with storage.connect() as conn:
        conn.execute("UPDATE routines SET cursor_at=? WHERE id=?", (LEDGER_START, routine_id))

    result = routine_tick(now=_at(8, 30), force=True)

    assert (result["skipped"], result["skip_reasons"]) == (0, {})
    assert len(_runs(routine_id)) == 1
    assert _task_total() == 0


def test_catch_up_none_on_the_current_minute_fires_without_a_skip():
    routine_id = _hourly_none()

    result = routine_tick(now=_at(8, 0), force=True)

    assert (result["created"], result["skipped"], result["skip_reasons"]) == (1, 0, {})
    [run] = _runs(routine_id)
    assert (run["status"], run["reason"]) == ("created", None)


def test_tick_counts_each_skip_reason():
    routine_add("One at a time", schedule="* * * * *", start_at=LEDGER_START)
    routine_tick(now=_at(6, 1), force=True)
    gated = routine_add(
        "Sweep",
        schedule="* * * * *",
        start_at=LEDGER_START,
        activity_gate="require_activity",
    )["id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE routines SET created_at='2999-01-01T00:00:00+00:00' WHERE id=?", (gated,)
        )
    _hourly_none()

    result = routine_tick(now=_at(8, 30), force=True)

    assert result["skip_reasons"] == {"overlap": 1, "no_signal": 1, "catch_up_none": 1}
    assert (result["skipped"], result["suppressed"]) == (2, 1)
    assert set(result["skip_reasons"]) <= set(SKIP_REASONS)


def test_disabled_tick_keeps_its_shape(monkeypatch):
    monkeypatch.delenv("UA_ROUTINES_ENABLED", raising=False)
    assert routine_tick(now=_at(7, 0)) == {
        "status": "disabled",
        "created": 0,
        "queued": 0,
        "skipped": 0,
    }


def _seed_runs(routine_id: int, count: int) -> None:
    with storage.connect() as conn:
        conn.executemany(
            "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at,"
            "reason,subject_key) VALUES (?,?,'x','skipped','x','overlap','task:1')",
            [(routine_id, f"cron:{n}") for n in range(count)],
        )


def test_recent_runs_is_absent_by_default():
    routine_id = _hourly_none()
    _seed_runs(routine_id, 1)

    [plain] = routine_list()["routines"]
    assert "recent_runs" not in plain
    assert "recent_runs" not in routine_list(recent_runs=0)["routines"][0]
    assert "recent_runs" not in routine_list(recent_runs=-1)["routines"][0]


def test_recent_runs_is_newest_first_and_clamped():
    routine_id = _hourly_none()
    _seed_runs(routine_id, 105)

    [limited] = routine_list(recent_runs=2)["routines"]
    assert [r["fire_key"] for r in limited["recent_runs"]] == ["cron:104", "cron:103"]
    assert set(limited["recent_runs"][0]) == {
        "id",
        "fire_key",
        "scheduled_for",
        "status",
        "reason",
        "subject_key",
        "task_id",
        "created_at",
        "window_start",
        "gate_matched",
    }
    assert len(routine_list(recent_runs=1000)["routines"][0]["recent_runs"]) == 100


def test_recent_runs_stays_inside_the_tenant(monkeypatch):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    theirs = _hourly_none("Theirs")
    _seed_runs(theirs, 1)
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    mine = _hourly_none("Mine")
    _seed_runs(mine, 2)

    routines = routine_list(recent_runs=10)["routines"]

    assert [r["id"] for r in routines] == [mine]
    assert len(routines[0]["recent_runs"]) == 2


def test_operator_run_now_fires_a_cron_routine_once_per_key(monkeypatch):
    """Task #2480: Run now fires a scheduled routine only inside the operator's
    run-now context; outside it a cron routine is still the scheduler's."""
    from unified_agent import routines

    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    routine = routine_add("Nightly sweep", schedule="0 3 * * *", concurrency="skip")
    cursor = routine_list()["routines"][0]["cursor_at"]
    with pytest.raises(ToolError, match="cron routines are fired by the scheduler"):
        routine_fire(routine["id"], idempotency_key="run-1")
    with routines.operator_run_now():
        first = routine_fire(routine["id"], idempotency_key="run-1")
        assert first["status"] == "created" and first["task_id"]
        assert routine_fire(routine["id"], idempotency_key="run-1") == first
        # A run is still active: concurrency=skip writes a skipped receipt, no task.
        assert routine_fire(routine["id"], idempotency_key="run-2") == {
            "status": "skipped",
            "task_id": None,
        }
    with pytest.raises(ToolError, match="cron routines"):
        routine_fire(routine["id"], idempotency_key="run-3")
    assert routine_list()["routines"][0]["cursor_at"] == cursor  # the schedule is untouched
