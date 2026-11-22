"""Routine activity gate (task #1752): skip fires that would report nothing."""

import sqlite3
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import edition, storage, table_schemas
from unified_agent.routines import routine_add, routine_list, routine_tick
from unified_agent.tools.tasks import PROJECTS, task_add, task_update

# Times far from the wall clock, so a test decides exactly what falls inside a
# window. event_log is append-only, so its rows always carry the real time.
PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
LATER = "2999-02-01T00:00:00+00:00"
START = "2026-09-25T07:00:00+00:00"


def _minute(n: int) -> datetime:
    return datetime(2026, 9, 25, 7, n, tzinfo=UTC)


def _gated(title: str = "Daily sweep", **kwargs) -> int:
    kwargs.setdefault("activity_gate", "require_activity")
    return routine_add(title, schedule="* * * * *", start_at=START, **kwargs)["id"]


def _open_window(routine_id: int, at: str) -> None:
    """Set when the first window opens (the routine's creation time)."""
    with storage.connect() as conn:
        conn.execute("UPDATE routines SET created_at=? WHERE id=?", (at, routine_id))


def _touch(task_id: int, at: str) -> None:
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id=?", (at, at, task_id))


def _insert_task(title: str, at: str) -> int:
    """A task row with no event_log entry (that table is append-only)."""
    with storage.connect() as conn:
        return conn.execute(
            "INSERT INTO tasks (title,status,priority,created_at,updated_at) "
            "VALUES (?,'open','med',?,?)",
            (title, at, at),
        ).lastrowid


def _runs(routine_id: int) -> list[sqlite3.Row]:
    with storage.connect() as conn:
        return conn.execute(
            "SELECT * FROM routine_runs WHERE routine_id=? ORDER BY id", (routine_id,)
        ).fetchall()


def _task_count() -> int:
    with storage.connect() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def _org_subject() -> str:
    return f"org:{edition.tenant_org_id() or 'default'}"


def test_quiet_scope_records_suppressed_run_and_creates_no_task():
    routine_id = _gated()
    _open_window(routine_id, FUTURE)

    result = routine_tick(now=_minute(1), force=True)

    assert (result["created"], result["suppressed"]) == (0, 1)
    assert _task_count() == 0
    [run] = _runs(routine_id)
    assert run["status"] == "suppressed"
    assert run["reason"] == "no_signal"
    assert run["subject_key"] == _org_subject()
    assert run["window_start"] == FUTURE
    assert run["task_id"] is None
    assert run["gate_matched"] == 0


def test_activity_in_scope_fires_and_records_match_count():
    routine_id = _gated()
    _open_window(routine_id, FUTURE)
    outside = task_add("Outside work")["id"]
    _touch(outside, LATER)

    result = routine_tick(now=_minute(1), force=True)

    assert (result["created"], result["suppressed"]) == (1, 0)
    [run] = _runs(routine_id)
    assert run["status"] == "created"
    assert run["task_id"] is not None
    assert run["reason"] is None
    assert run["window_start"] == FUTURE
    assert run["gate_matched"] == 1


def test_routine_own_tasks_and_bookkeeping_events_are_not_activity():
    routine_id = _gated()
    _open_window(routine_id, FUTURE)
    _insert_task("Outside work", LATER)
    assert routine_tick(now=_minute(1), force=True)["created"] == 1
    own = _runs(routine_id)[0]["task_id"]
    # Outside work predates the fire; the fire's own task, its create event and
    # its completion (with the events that writes) fall inside the next window.
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id!=?", (PAST, PAST, own))
    task_update(own, status="done", worktree=False, actor="Desktop_Agent2")
    window = datetime.fromisoformat(_runs(routine_id)[0]["created_at"]).isoformat(
        timespec="seconds"
    )
    with storage.connect() as conn:
        own_events = conn.execute(
            "SELECT dedupe_key FROM event_log WHERE created_at>=?", (window,)
        ).fetchall()
    # The fire's bookkeeping event and the completion's events are all there.
    assert len(own_events) >= 2
    assert any((row[0] or "").startswith("routine:") for row in own_events)

    result = routine_tick(now=_minute(2), force=True)

    assert (result["created"], result["suppressed"]) == (0, 1)
    second = _runs(routine_id)[1]
    assert second["status"] == "suppressed"
    assert second["reason"] == "self_echo"
    assert second["subject_key"] == _org_subject()
    assert second["gate_matched"] == 0
    assert second["window_start"] == window
    assert _task_count() == 2


def test_two_gated_routines_do_not_keep_each_other_firing():
    first = _gated("Sweep A")
    second = _gated("Sweep B")
    for routine_id in (first, second):
        _open_window(routine_id, FUTURE)
    _insert_task("Outside work", LATER)
    assert routine_tick(now=_minute(1), force=True)["created"] == 2
    spawned = [_runs(r)[0]["task_id"] for r in (first, second)]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET created_at=?, updated_at=? WHERE id NOT IN (?,?)",
            (PAST, PAST, *spawned),
        )
    # Each routine's task is closed; neither counts for the other routine.
    for task_id in spawned:
        task_update(task_id, status="done", worktree=False, actor="Desktop_Agent2")

    result = routine_tick(now=_minute(2), force=True)

    assert (result["created"], result["suppressed"]) == (0, 2)
    assert [_runs(r)[1]["gate_matched"] for r in (first, second)] == [0, 0]


def test_self_echo_and_no_signal_follow_the_project_scope():
    other = next(p for p in sorted(PROJECTS) if p != "unified-agent")
    routine_add("Spawner", schedule="* * * * *", start_at=START, project="unified-agent")
    elsewhere = _gated("Elsewhere", project=other, activity_gate_scope="project")
    same = _gated("Same", project="unified-agent", activity_gate_scope="project")
    # Both windows include the spawner's task, which fires first in this tick.
    _open_window(elsewhere, PAST)
    _open_window(same, PAST)

    routine_tick(now=_minute(1), force=True)

    assert [(r["reason"], r["subject_key"]) for r in _runs(elsewhere)] == [
        ("no_signal", f"project:{other}")
    ]
    assert [(r["reason"], r["subject_key"]) for r in _runs(same)] == [
        ("self_echo", "project:unified-agent")
    ]


def test_passed_over_slot_does_not_move_the_activity_window():
    routine_id = routine_add(
        "Hourly",
        schedule="0 * * * *",
        catch_up="none",
        start_at=START,
        activity_gate="require_activity",
    )["id"]
    _open_window(routine_id, FUTURE)

    assert routine_tick(now=_minute(30).replace(hour=8), force=True)["skipped"] == 1
    assert routine_tick(now=_minute(0).replace(hour=9), force=True)["suppressed"] == 1

    missed, gated = _runs(routine_id)
    assert (missed["window_start"], missed["gate_matched"]) == (None, None)
    assert (gated["reason"], gated["window_start"]) == ("no_signal", FUTURE)


def test_ledger_migration_backfills_old_reasons_once(tmp_path):
    conn = sqlite3.connect(tmp_path / "gated.db")
    conn.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY);
        CREATE TABLE routines (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT,
          trigger TEXT NOT NULL, schedule TEXT, lane TEXT NOT NULL, project TEXT NOT NULL,
          domain TEXT NOT NULL, untrusted INTEGER NOT NULL DEFAULT 0,
          concurrency TEXT NOT NULL, catch_up TEXT NOT NULL,
          paused INTEGER NOT NULL DEFAULT 0, cursor_at TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, org_id TEXT,
          activity_gate TEXT NOT NULL DEFAULT 'always',
          activity_gate_scope TEXT NOT NULL DEFAULT 'org'
        );
        CREATE TABLE routine_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
          fire_key TEXT NOT NULL,
          scheduled_for TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('created','queued','skipped','suppressed')),
          task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          reason TEXT,
          window_start TEXT,
          gate_matched INTEGER,
          UNIQUE(routine_id, fire_key)
        );
        INSERT INTO routines (title,trigger,schedule,lane,project,domain,concurrency,
          catch_up,cursor_at,created_at,updated_at)
          VALUES ('r','cron','* * * * *','default','unified-agent','hobby','skip','last',
                  'x','x','x');
        INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at,reason)
          VALUES (1,'a','x','skipped','x',NULL),
                 (1,'b','x','suppressed','x','no_activity'),
                 (1,'c','x','created','x',NULL);
        """
    )

    table_schemas.routines(conn)

    rows = "SELECT fire_key, reason, subject_key FROM routine_runs ORDER BY id"
    assert conn.execute(rows).fetchall() == [
        ("a", "overlap", None),
        ("b", "no_signal", None),
        ("c", None, None),
    ]
    # Once only: a later pass does not rewrite rows written after the migration.
    conn.execute(
        "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at) "
        "VALUES (1,'d','x','skipped','x')"
    )
    table_schemas.routines(conn)
    assert conn.execute(rows).fetchall()[-1] == ("d", None, None)
    conn.close()


def test_a_view_on_routine_runs_does_not_block_the_rebuild(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE routines (id INTEGER PRIMARY KEY);"
        "CREATE TABLE routine_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, routine_id INTEGER,"
        " status TEXT NOT NULL CHECK (status IN ('created','queued','skipped')),"
        " task_id INTEGER, created_at TEXT);"
        "INSERT INTO routine_runs (routine_id, status, created_at) VALUES (1,'created','x');"
        "CREATE VIEW recent_runs AS SELECT id, status FROM routine_runs;"
    )
    table_schemas._widen_routine_run_status(conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='routine_runs'").fetchone()[0]
    assert "'suppressed'" in sql
    assert conn.execute("SELECT count(*) FROM recent_runs").fetchone()[0] == 1
    conn.close()


def test_project_scope_counts_only_the_routine_project():
    other = next(p for p in sorted(PROJECTS) if p != "unified-agent")
    routine_id = _gated(project="unified-agent", activity_gate_scope="project")
    _open_window(routine_id, FUTURE)
    elsewhere = task_add("Other project work", project=other)["id"]
    _touch(elsewhere, LATER)

    assert routine_tick(now=_minute(1), force=True)["suppressed"] == 1
    # The window now opens at that suppression, after the other project's task
    # event: an event mapped to another project's task still does not count.
    assert routine_tick(now=_minute(2), force=True)["suppressed"] == 1

    task_add("Same project work", project="unified-agent")
    result = routine_tick(now=_minute(3), force=True)

    assert (result["created"], result["suppressed"]) == (1, 0)
    assert [run["status"] for run in _runs(routine_id)] == ["suppressed", "suppressed", "created"]
    # The task row plus its create event, mapped to the project by ref_id.
    assert _runs(routine_id)[2]["gate_matched"] == 2


def test_org_scope_counts_other_projects():
    other = next(p for p in sorted(PROJECTS) if p != "unified-agent")
    routine_id = _gated(project="unified-agent")
    _open_window(routine_id, FUTURE)
    _touch(task_add("Other project work", project=other)["id"], LATER)

    assert routine_tick(now=_minute(1), force=True)["created"] == 1
    assert _runs(routine_id)[0]["gate_matched"] == 1


def test_gate_parameters_are_validated():
    with pytest.raises(ToolError, match="needs the routine to have a project"):
        _gated(project="", activity_gate_scope="project")
    with pytest.raises(ToolError, match="activity_gate must be"):
        _gated(activity_gate="sometimes")
    with pytest.raises(ToolError, match="activity_gate_scope must be"):
        _gated(activity_gate_scope="team")
    assert routine_list()["routines"] == []


def test_default_always_gate_fires_without_activity():
    routine_id = routine_add("Plain", schedule="* * * * *", start_at=START)["id"]
    _open_window(routine_id, FUTURE)

    result = routine_tick(now=_minute(1), force=True)

    assert (result["created"], result["suppressed"]) == (1, 0)
    [definition] = routine_list()["routines"]
    assert (definition["activity_gate"], definition["activity_gate_scope"]) == ("always", "org")
    [run] = _runs(routine_id)
    assert run["status"] == "created"
    assert (run["reason"], run["window_start"], run["gate_matched"]) == (None, None, None)


def test_suppression_advances_the_schedule():
    routine_id = _gated()
    _open_window(routine_id, FUTURE)

    assert routine_tick(now=_minute(1), force=True)["suppressed"] == 1
    with storage.connect() as conn:
        cursor = conn.execute("SELECT cursor_at FROM routines WHERE id=?", (routine_id,))
        assert cursor.fetchone()[0] == _minute(1).isoformat()
    assert routine_tick(now=_minute(1), force=True)["suppressed"] == 0
    assert len(_runs(routine_id)) == 1

    # The next minute is a new fire; its window opens at the suppression.
    assert routine_tick(now=_minute(2), force=True)["suppressed"] == 1
    first, second = _runs(routine_id)
    assert second["window_start"] == datetime.fromisoformat(first["created_at"]).isoformat(
        timespec="seconds"
    )


def test_gate_counts_only_the_routine_tenant(monkeypatch):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_b")
    _touch(task_add("Another tenant's work")["id"], LATER)
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "org_a")
    routine_id = _gated()
    _open_window(routine_id, FUTURE)

    assert routine_tick(now=_minute(1), force=True)["suppressed"] == 1

    _touch(task_add("Own tenant's work")["id"], LATER)
    assert routine_tick(now=_minute(2), force=True)["created"] == 1
    # The task row and its create event; the other tenant's task and event,
    # written moments earlier, are fenced out.
    assert [run["gate_matched"] for run in _runs(routine_id)] == [0, 2]
    assert _runs(routine_id)[0]["subject_key"] == "org:org_a"


def test_existing_tables_gain_gate_columns_and_suppressed_status(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY);
        CREATE TABLE routines (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT,
          trigger TEXT NOT NULL CHECK(trigger IN ('cron','api','webhook')), schedule TEXT,
          lane TEXT NOT NULL, project TEXT NOT NULL, domain TEXT NOT NULL,
          untrusted INTEGER NOT NULL DEFAULT 0,
          concurrency TEXT NOT NULL CHECK(concurrency IN ('skip','queue')),
          catch_up TEXT NOT NULL CHECK(catch_up IN ('none','last','all')),
          paused INTEGER NOT NULL DEFAULT 0, cursor_at TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, org_id TEXT,
          CHECK ((trigger='cron' AND schedule IS NOT NULL) OR
                 (trigger!='cron' AND schedule IS NULL))
        );
        CREATE TABLE routine_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
          fire_key TEXT NOT NULL,
          scheduled_for TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('created','queued','skipped')),
          task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          UNIQUE(routine_id, fire_key)
        );
        CREATE INDEX idx_routine_runs_task ON routine_runs(task_id);
        INSERT INTO routines (title,trigger,schedule,lane,project,domain,concurrency,
          catch_up,cursor_at,created_at,updated_at)
          VALUES ('r','cron','* * * * *','default','unified-agent','hobby','skip','last',
                  'x','x','x');
        INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at)
          VALUES (1,'a','x','skipped','x'), (1,'b','x','skipped','x');
        DELETE FROM routine_runs WHERE id=2;
        """
    )

    table_schemas.routines(conn)
    table_schemas.routines(conn)

    assert conn.execute("SELECT activity_gate, activity_gate_scope FROM routines").fetchall() == [
        ("always", "org")
    ]
    assert conn.execute("SELECT id, status, reason, subject_key FROM routine_runs").fetchall() == [
        (1, "skipped", "overlap", None)
    ]
    conn.execute(
        "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at,reason) "
        "VALUES (1,'c','x','suppressed','x','no_signal')"
    )
    # AUTOINCREMENT's high-water mark survived the rebuild: id 2 is not reused.
    assert conn.execute("SELECT max(id) FROM routine_runs").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO routine_runs (routine_id,fire_key,scheduled_for,status,created_at) "
            "VALUES (1,'d','x','bogus','x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE routines SET activity_gate='sometimes'")
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_routine_runs_task" in indexes
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
