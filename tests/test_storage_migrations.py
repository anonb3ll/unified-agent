"""Migrations that rebuild a table must restore everything that table had.

DROP TABLE takes its indexes with it. _SCHEMA runs BEFORE _migrate, so a
`CREATE INDEX IF NOT EXISTS` there does not put them back in the same init_db
call — and production restarts once, so a missed index is silently absent until
the next restart. Caught on the delegations rebuild (task #169), which
originally recreated one of three, losing idx_delegations_status: the index the
executor's own poll query needs.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from unified_agent import storage

REBUILT_TABLES = {
    "delegations": {
        "idx_delegations_task",
        "idx_delegations_to_agent",
        "idx_delegations_status",
    },
    "approvals": {"idx_approvals_status", "idx_approvals_tool"},
    "tasks": {"idx_tasks_status_priority", "idx_tasks_parent", "idx_tasks_lane"},
}


def _indexes(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


@pytest.mark.parametrize("table,expected", sorted(REBUILT_TABLES.items()))
def test_one_init_db_leaves_every_index_in_place(table, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()  # ONE call, as a service restart does
    db = tmp_path / "memory.db"

    assert expected <= _indexes(db, table), (
        f"{table}: a rebuild dropped indexes without recreating them in the same run"
    )


@pytest.mark.parametrize("table,expected", sorted(REBUILT_TABLES.items()))
def test_migration_is_idempotent(table, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    storage.init_db()
    storage.init_db()
    assert expected <= _indexes(tmp_path / "memory.db", table)


# --- event_log (task #157) ------------------------------------------------------
# Additive rather than rebuilt, but the same failure mode applies: production
# restarts ONCE, so a table or index that only appears on the second init_db is
# absent in production for as long as the service keeps running.

EVENT_LOG_INDEXES = {
    "idx_event_log_channel",
    "idx_event_log_created",
    "idx_event_log_channel_created",
    "idx_event_log_episode",
    "idx_event_log_dedupe",
    "idx_event_log_node_item_turn",
}


def _expected_event_log_indexes():
    conn = sqlite3.connect(":memory:")
    try:
        supported = storage.node_turn_index_supported(conn)
    finally:
        conn.close()
    return EVENT_LOG_INDEXES if supported else EVENT_LOG_INDEXES - {"idx_event_log_node_item_turn"}


def _pre_157_db(tmp_path):
    """A DB as it existed before task #157: everything else, no event_log."""
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE event_log")
        conn.commit()
    finally:
        conn.close()
    return db


def test_one_init_db_adds_event_log_and_its_indexes_to_an_older_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db = _pre_157_db(tmp_path)
    assert _indexes(db, "event_log") == set()

    storage.init_db()  # ONE call, as a service restart does

    assert _expected_event_log_indexes() <= _indexes(db, "event_log")

    # Present is not the same as usable: prove the upgraded table actually
    # accepts a write on the columns the bus and #156 depend on.
    from unified_agent import events

    ulid = events.record_event(
        channel="agent.health.alert",
        payload={"upgraded": True},
        episode_id="ep_1",
        causal_parent_id="01JZZZ",
    )
    row = events.query_events(channels=["agent.health.alert"])[0]
    assert row["ulid"] == ulid
    assert row["episode_id"] == "ep_1"
    assert row["causal_parent_id"] == "01JZZZ"


def test_event_log_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    _pre_157_db(tmp_path)
    storage.init_db()
    storage.init_db()
    storage.init_db()
    assert _expected_event_log_indexes() <= _indexes(tmp_path / "memory.db", "event_log")


def test_migration_never_sets_source_host_from_execution_events(tmp_path, monkeypatch):
    """tasks.source_host is origin-machine provenance, not the execution host.

    Execution events can name a different machine than task_add. Backfilling
    source_host from those events corrupts filters/exports (PR #401 P1).
    Legacy NULL stays unknown; a recorded creation host is never overwritten.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()

    from unified_agent import events

    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, ?, 'done', 'med', ?, ?)",
            (701, "legacy unknown origin", now, now),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, source_host) "
            "VALUES (?, ?, 'done', 'med', ?, ?, ?)",
            (702, "created on laptop", now, now, "laptop"),
        )
        conn.commit()
    finally:
        conn.close()
    events.record_event("agent.dispatch.transition", payload={"task_id": 701, "host": "mac-hub"})
    events.record_event("agent.dispatch.transition", payload={"task_id": 702, "host": "desktop"})

    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, source_host FROM tasks WHERE id IN (701, 702) ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["id"], row["source_host"]) for row in rows] == [
        (701, None),
        (702, "laptop"),
    ]


# --- Zulip worker state (task #517) --------------------------------------------


def test_one_init_db_adds_zulip_state_to_an_older_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE zulip_worker_health")
        conn.execute("DROP TABLE zulip_cursors")
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    assert {"idx_zulip_cursors_updated"} <= _indexes(db, "zulip_cursors")
    assert {"idx_zulip_worker_health_status"} <= _indexes(db, "zulip_worker_health")
    assert storage.save_zulip_cursor("Desktop_Agent2", "coord", 101)["last_message_id"] == 101
    assert storage.get_zulip_worker_health("Desktop_Agent2", "coord") is None
    schema = (
        sqlite3.connect(db)
        .execute("SELECT sql FROM sqlite_master WHERE name='zulip_cursors'")
        .fetchone()[0]
    )
    assert "api_key" not in schema.lower()
    assert "credential" not in schema.lower()


def test_one_init_db_adds_event_dedupe_column_to_an_older_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX idx_event_log_dedupe")
        conn.execute("ALTER TABLE event_log DROP COLUMN dedupe_key")
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    assert "idx_event_log_dedupe" in _indexes(db, "event_log")
    from unified_agent import events

    first = events.record_event(
        "zulip.inbound", payload={"message_id": 901}, dedupe_key="zulip:901", notify=False
    )
    second = events.record_event(
        "zulip.inbound", payload={"message_id": 901}, dedupe_key="zulip:901", notify=False
    )
    assert second == first


# --- agent registry (web control center M1) -------------------------------------
# Same invariant, same reason: these tables arrive on a live memory.db that is shared
# across machines, and the upgrade path is a single service restart.

REGISTRY_TABLES = ("agents", "agent_sessions", "control_flags", "ui_sessions")


def _pre_registry_db(tmp_path):
    """A DB as it existed before the registry landed."""
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        for table in REGISTRY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    finally:
        conn.close()
    return db


def test_one_init_db_adds_the_registry_and_its_indexes_to_an_older_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db = _pre_registry_db(tmp_path)
    assert _indexes(db, "agents") == set()

    storage.init_db()  # ONE call, as a service restart does

    expected = {name for name, _target in storage._AGENT_REGISTRY_INDEXES}
    present = set()
    for table in REGISTRY_TABLES:
        present |= _indexes(db, table)
    assert expected <= present

    # Present is not the same as usable: prove the new tables accept the writes the
    # registry actually performs.
    from unified_agent import registry

    registry.reset_throttle()
    assert registry.touch_agent("Desktop_Agent1", via="hub_request", force=True) is True
    sid = registry.start_session("Desktop_Agent1", "pi_run")
    assert sid and registry.end_session(sid) is True


def test_registry_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    _pre_registry_db(tmp_path)
    storage.init_db()
    storage.init_db()
    storage.init_db()
    expected = {name for name, _target in storage._AGENT_REGISTRY_INDEXES}
    present = set()
    for table in REGISTRY_TABLES:
        present |= _indexes(tmp_path / "memory.db", table)
    assert expected <= present


def test_registry_tables_are_added_without_rebuilding_anything(tmp_path, monkeypatch):
    """The registry migration must be purely additive.

    A rebuild is what dropped indexes in #169. Existing rows surviving the upgrade is
    the observable proof that no table was recreated underneath them.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    _pre_registry_db(tmp_path)

    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
            " VALUES ('survivor','open','med','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    conn = sqlite3.connect(db)
    try:
        titles = [r[0] for r in conn.execute("SELECT title FROM tasks").fetchall()]
    finally:
        conn.close()
    assert "survivor" in titles


# --- tasks.status widened to include waiting_review/cancelled (task-lifecycle-
# contract.md §1.4/§6 Q5-Q6) -------------------------------------------------


def _pre_widen_tasks_db(tmp_path):
    """A DB with the old 4-value tasks.status CHECK, plus a parent/child pair.

    tasks.parent_id is a self-referential FK (foreign_keys=ON at connect()), so
    the rebuild must insert parents before children within the same INSERT — this
    fixture's child row exists specifically to exercise that ordering.
    """
    monkeypatch_db = tmp_path / "memory.db"
    conn = sqlite3.connect(monkeypatch_db)
    try:
        conn.execute(
            """
            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              notes TEXT,
              status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','doing','blocked','done')),
              priority TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
              parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              archived_at TEXT,
              source_host TEXT,
              claimed_by TEXT,
              claimed_at TEXT,
              lock_expires_at TEXT,
              untrusted INTEGER NOT NULL DEFAULT 0,
              worktree_path TEXT,
              branch TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at)"
            " VALUES (1, 'parent task', 'done', 'med', '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, parent_id, created_at, updated_at)"
            " VALUES (2, 'child task', 'open', 'med', 1, '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    return monkeypatch_db


def test_tasks_status_widen_preserves_rows_and_parent_link(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db = _pre_widen_tasks_db(tmp_path)

    # Old CHECK genuinely rejects the new values pre-migration.
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
                " VALUES ('x', 'cancelled', 'med', '2026-01-01T00:00:00+00:00',"
                "'2026-01-01T00:00:00+00:00')"
            )
    finally:
        conn.close()

    storage.init_db()  # ONE call, as a service restart does

    conn = sqlite3.connect(db)
    try:
        rows = {
            r[0]: r[1:]
            for r in conn.execute("SELECT id, title, status, parent_id FROM tasks").fetchall()
        }
        # Rows and the parent/child link both survived the rebuild.
        assert rows[1][0] == "parent task"
        assert rows[2] == ("child task", "open", 1)

        # New values are now writable — the CHECK was actually widened, not
        # just left permissive by accident.
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
            " VALUES ('y', 'cancelled', 'med', '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
            " VALUES ('z', 'waiting_review', 'med', '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()


def test_tasks_status_widen_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    _pre_widen_tasks_db(tmp_path)
    storage.init_db()
    storage.init_db()
    storage.init_db()
    assert REBUILT_TABLES["tasks"] <= _indexes(tmp_path / "memory.db", "tasks")


def _task_cols(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    finally:
        conn.close()


def test_evidence_ref_column_added_and_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db = tmp_path / "memory.db"

    storage.init_db()  # ONE call, as a service restart does
    assert "evidence_ref" in _task_cols(db)

    # Bare ADD COLUMN, not a rebuild — re-running init_db() must not error on
    # an already-present column (the `if "evidence_ref" not in task_cols`
    # guard in _migrate is what's under test here).
    storage.init_db()
    storage.init_db()
    assert "evidence_ref" in _task_cols(db)

    # Nullable with no default: existing/new rows are unaffected unless a
    # task opts in via the [code-task] marker.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
            " VALUES ('x', 'open', 'med', '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT evidence_ref FROM tasks WHERE title='x'").fetchone()
        assert row[0] is None
    finally:
        conn.close()


# --- dispatch_runs (task #548) ------------------------------------------------

DISPATCH_RUNS_INDEXES = {
    "idx_dispatch_runs_task",
    "idx_dispatch_runs_state",
    "idx_dispatch_runs_delegation",
    "idx_dispatch_runs_live_task",
}


def test_one_init_db_creates_dispatch_runs_and_indexes(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    assert DISPATCH_RUNS_INDEXES <= _indexes(db, "dispatch_runs")

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
            " VALUES ('d', 'open', 'med', '2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES ('run-1', 1, 'Desktop_Agent1', 'claimed', 'dispatch-coordinator',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db()
    assert DISPATCH_RUNS_INDEXES <= _indexes(db, "dispatch_runs")


def test_legacy_live_slot_conflict_fails_closed(tmp_path, monkeypatch):
    """A conflicting legacy row must not restore the weaker live-slot index."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX idx_dispatch_runs_live_task")
        conn.execute(
            "CREATE UNIQUE INDEX idx_dispatch_runs_live_task "
            "ON dispatch_runs(task_id) WHERE state IN ('claimed','launched','running')"
        )
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at) "
            "VALUES ('legacy conflict', 'open', 'med', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES ('legacy-cancel', 1, 'Desktop_Agent1', 'cancelled', 'Desktop_Agent1', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO dispatch_runs "
            "(run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES ('legacy-live', 1, 'Desktop_Agent1', 'running', 'Desktop_Agent1', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
    finally:
        conn.close()

    storage._connection = None
    with pytest.raises(sqlite3.IntegrityError, match="fail-closed"):
        storage.init_db()

    conn = sqlite3.connect(db)
    try:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_dispatch_runs_live_task'"
        ).fetchone()[0]
        assert "termination_outcome" not in index_sql
    finally:
        conn.close()


def test_lane_migration_preserves_delegations(tmp_path, monkeypatch):
    """DROP TABLE tasks with FK on must not cascade-delete delegation rows."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        tid = conn.execute(
            "INSERT INTO tasks (title, notes, status, priority, created_at, updated_at) "
            "VALUES ('parent', NULL, 'open', 'med', '2026-01-01', '2026-01-01')"
        ).lastrowid
        conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, callback, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'notify', 'pending', ?, ?)",
            ("Desktop_Agent1", "Desktop_Agent5", tid, "spec", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.execute("ALTER TABLE tasks DROP COLUMN lane")
    except Exception:
        pass
    finally:
        conn.close()
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        row = conn.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["lane"] == "default"
    finally:
        conn.close()


# --- elicitation challenges (task #832) ---------------------------------------


def test_one_init_db_adds_elicitation_challenges_to_an_older_db(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS must land on the first restart, not the second."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE elicitation_challenges")
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(elicitation_challenges)")}
        assert cols == {
            "approval_id",
            "nonce_hash",
            "issued_at",
            "consumed_at",
            "session_key",
        }
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, created_at, updated_at) "
            "VALUES ('run_cmd', '{}', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO elicitation_challenges "
            "(approval_id, nonce_hash, issued_at, consumed_at, session_key) "
            "VALUES (1, 'abc', '2026-01-01', NULL, NULL)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT nonce_hash FROM elicitation_challenges WHERE approval_id=1"
        ).fetchone()
        assert row["nonce_hash"] == "abc"
    finally:
        conn.close()


# --- org_id tenant boundary (task #1232) ---------------------------------------
# The reversal is the load-bearing part of this migration: it must actually run
# and restore the prior schema, not just be asserted in prose. See
# storage.downgrade_org_id_columns for why this is a standalone function rather
# than a slot in a down-migration framework -- none exists here.

ORG_ID_TABLES = ("tasks", "memory", "event_log", "approvals", "delegations")


def _table_columns(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def test_org_id_migration_is_reversible(tmp_path, monkeypatch):
    """Up: org_id + its index appear on every one of the five tables. Down:
    storage.downgrade_org_id_columns actually removes them again, restoring
    the exact prior schema -- same columns, same indexes -- not merely a
    claim that it would."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"

    pre_migration_columns = {
        table: _table_columns(db, table) - {"org_id"} for table in ORG_ID_TABLES
    }
    pre_migration_indexes = {
        table: _indexes(db, table) - {f"idx_{table}_org_id"} for table in ORG_ID_TABLES
    }

    for table in ORG_ID_TABLES:
        assert "org_id" in _table_columns(db, table)
        assert f"idx_{table}_org_id" in _indexes(db, table)

    # Exercise a row with org_id actually populated, to prove the down-path
    # is a real schema reversal and not merely a no-op on empty tables.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET org_id='org_pilot_2' WHERE id = "
            "(SELECT id FROM tasks ORDER BY id LIMIT 1)"
        )
        conn.commit()
    finally:
        conn.close()
    # (tasks may be empty in a fresh DB; that's fine, the UPDATE is a no-op then)

    conn = storage.connect()
    try:
        storage.downgrade_org_id_columns(conn)
        conn.commit()
    finally:
        conn.close()

    for table in ORG_ID_TABLES:
        assert _table_columns(db, table) == pre_migration_columns[table], (
            f"{table}: downgrade left the schema different from before the migration"
        )
        assert _indexes(db, table) == pre_migration_indexes[table], (
            f"{table}: downgrade left indexes different from before the migration"
        )
        assert f"idx_{table}_org_id" not in _indexes(db, table)

    # The rest of the schema (unrelated tables/indexes) must be untouched.
    assert REBUILT_TABLES["tasks"] <= _indexes(db, "tasks")
    assert REBUILT_TABLES["approvals"] <= _indexes(db, "approvals")
    assert REBUILT_TABLES["delegations"] <= _indexes(db, "delegations")


def test_org_id_downgrade_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"

    conn = storage.connect()
    try:
        storage.downgrade_org_id_columns(conn)
        storage.downgrade_org_id_columns(conn)
        conn.commit()
    finally:
        conn.close()

    for table in ORG_ID_TABLES:
        assert "org_id" not in _table_columns(db, table)


def test_org_id_migration_reapplies_cleanly_after_a_downgrade(tmp_path, monkeypatch):
    """A downgraded DB is a legitimate "pre-existing DB" for _migrate() to
    upgrade again -- prove the up-path is not one-shot."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"

    conn = storage.connect()
    try:
        storage.downgrade_org_id_columns(conn)
        conn.commit()
    finally:
        conn.close()
    for table in ORG_ID_TABLES:
        assert "org_id" not in _table_columns(db, table)

    storage.init_db()  # re-upgrade, ONE call

    for table in ORG_ID_TABLES:
        assert "org_id" in _table_columns(db, table)
        assert f"idx_{table}_org_id" in _indexes(db, table)


# --- executor_attempts task_type CHECK widened for 'scout' (task #1543) -----------
# The daemon records scout runs with task_type='scout', but the CHECK only
# allowed the older vocabulary, so start_attempt's INSERT failed and the
# failure was swallowed: scout runs left no trust-ladder evidence. SQLite cannot
# ALTER a CHECK, so existing DBs need a rebuild that preserves EVERY column, row,
# id, foreign key and index.

_NARROW_TYPES = "'code','docs','research','review','operations','unknown'"
_OUTCOMES = (
    "'success','failure','blocked','quarantined','cooldown','killed','waiting_review','unknown'"
)

# Shape of a DB created by the fresh DDL just before #1543 (model mid-table).
_LEGACY_FRESH = f"""
CREATE TABLE executor_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  delegation_id INTEGER REFERENCES delegations(id) ON DELETE SET NULL,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  model TEXT,
  task_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (task_type IN ({_NARROW_TYPES})),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  outcome TEXT NOT NULL DEFAULT 'unknown'
    CHECK (outcome IN ({_OUTCOMES})),
  detail TEXT,
  failure_class TEXT
)
"""

# Shape of the very first table (task #458): ON DELETE CASCADE, no model, no
# failure_class. It must travel the whole chain: CASCADE rebuild -> ADD COLUMNs
# -> scout widening, and must NOT come out with the narrow CHECK.
_LEGACY_CASCADE = f"""
CREATE TABLE executor_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  delegation_id INTEGER REFERENCES delegations(id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  backend TEXT NOT NULL,
  task_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (task_type IN ({_NARROW_TYPES})),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  outcome TEXT NOT NULL DEFAULT 'unknown'
    CHECK (outcome IN ({_OUTCOMES})),
  detail TEXT
)
"""

_ATTEMPT_INDEXES = {
    "idx_executor_attempts_task",
    "idx_executor_attempts_delegation",
    "idx_executor_attempts_outcome",
    "idx_executor_attempts_failure_class",
}


def _attempts_sql(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='executor_attempts'"
        ).fetchone()[0]
    finally:
        conn.close()


def _attempt_columns(db):
    conn = sqlite3.connect(db)
    try:
        return [
            (r[1], r[2], r[3], r[4], r[5])
            for r in conn.execute("PRAGMA table_info(executor_attempts)").fetchall()
        ]
    finally:
        conn.close()


def _legacy_attempts_db(tmp_path, monkeypatch, legacy_ddl):
    """A DB whose executor_attempts predates #1543, holding three rows (ids 1-3
    -> the last one deleted so AUTOINCREMENT's high-water mark is 3, not 2)."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE executor_attempts")
        conn.execute(legacy_ddl)
        conn.execute("CREATE INDEX idx_executor_attempts_task ON executor_attempts(task_id)")
        conn.execute(
            "CREATE INDEX idx_executor_attempts_delegation ON executor_attempts(delegation_id)"
        )
        conn.execute("CREATE INDEX idx_executor_attempts_outcome ON executor_attempts(outcome)")
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (901, 'legacy attempt task', 'open', 'med', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO executor_attempts "
            "(id, task_id, agent, backend, task_type, started_at, finished_at, status, outcome, detail) "
            "VALUES (1, 901, 'Desktop_Agent2', 'codex', 'code', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:01:00+00:00', 'success', 'success', 'first')"
        )
        conn.execute(
            "INSERT INTO executor_attempts "
            "(id, task_id, agent, backend, task_type, started_at, outcome) "
            "VALUES (2, NULL, 'Desktop_agent3', 'agy', 'docs', '2026-01-02T00:00:00+00:00', 'failure')"
        )
        conn.execute(
            "INSERT INTO executor_attempts (id, agent, backend, started_at) "
            "VALUES (3, 'x', 'y', '2026-01-03')"
        )
        conn.execute("DELETE FROM executor_attempts WHERE id=3")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.parametrize(
    "legacy_ddl,next_id",
    # The pre-existing CASCADE rebuild (#467) copies rows without carrying the
    # AUTOINCREMENT high-water mark, so the deleted id 3 is already gone from
    # the sequence before the #1543 rebuild sees the table; only the
    # narrow-check shape can prove the #1543 rebuild itself keeps it.
    [(_LEGACY_FRESH, 4), (_LEGACY_CASCADE, 3)],
    ids=["narrow-check", "cascade-era"],
)
def test_scout_task_type_migration_preserves_everything(tmp_path, monkeypatch, legacy_ddl, next_id):
    db = _legacy_attempts_db(tmp_path, monkeypatch, legacy_ddl)
    assert "'scout'" not in _attempts_sql(db)

    # Reference: the columns a fresh DB has today, whatever later migrations add.
    ref_home = tmp_path / "ref"
    ref_home.mkdir()
    monkeypatch.setenv("UA_HOME", str(ref_home))
    storage._connection = None
    storage.init_db()
    fresh_cols = {c[0]: c for c in _attempt_columns(ref_home / "memory.db")}
    assert "'scout'" in _attempts_sql(ref_home / "memory.db")

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()

    assert "'scout'" in _attempts_sql(db)
    migrated_cols = {c[0]: c for c in _attempt_columns(db)}
    assert migrated_cols == fresh_cols  # every column, type, notnull, default, pk
    assert _ATTEMPT_INDEXES <= _indexes(db, "executor_attempts")

    conn = storage.connect()
    try:
        rows = conn.execute("SELECT * FROM executor_attempts ORDER BY id").fetchall()
        assert [r["id"] for r in rows] == [1, 2]
        assert (rows[0]["task_id"], rows[0]["agent"], rows[0]["task_type"]) == (
            901,
            "Desktop_Agent2",
            "code",
        )
        assert rows[0]["detail"] == "first" and rows[0]["outcome"] == "success"
        assert rows[0]["finished_at"] == "2026-01-01T00:01:00+00:00"
        assert (rows[1]["task_type"], rows[1]["outcome"]) == ("docs", "failure")

        # 'scout' is now insertable; the id continues past the pre-migration
        # high-water mark (never reusing the deleted id 3, narrow-check shape).
        cur = conn.execute(
            "INSERT INTO executor_attempts (task_id, agent, backend, task_type, started_at) "
            "VALUES (901, 'Desktop_Agent2', 'codex', 'scout', '2026-02-01')"
        )
        assert cur.lastrowid == next_id
        # Other values are still rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO executor_attempts (agent, backend, task_type, started_at) "
                "VALUES ('a', 'b', 'bogus', '2026-02-01')"
            )
        conn.commit()

        # The foreign key is still ON DELETE SET NULL, not CASCADE.
        conn.execute("DELETE FROM tasks WHERE id=901")
        conn.commit()
        rows = conn.execute("SELECT id, task_id FROM executor_attempts ORDER BY id").fetchall()
        assert [(r["id"], r["task_id"]) for r in rows] == [(1, None), (2, None), (next_id, None)]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "legacy_ddl", [_LEGACY_FRESH, _LEGACY_CASCADE], ids=["narrow-check", "cascade-era"]
)
def test_scout_task_type_migration_is_idempotent(tmp_path, monkeypatch, legacy_ddl):
    db = _legacy_attempts_db(tmp_path, monkeypatch, legacy_ddl)
    storage._connection = None
    storage.init_db()
    first_sql = _attempts_sql(db)
    conn = sqlite3.connect(db)
    try:
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='executor_attempts'"
        ).fetchone()[0]
    finally:
        conn.close()

    storage._connection = None
    storage.init_db()
    storage.init_db()

    assert _attempts_sql(db) == first_sql
    assert _ATTEMPT_INDEXES <= _indexes(db, "executor_attempts")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT id FROM executor_attempts ORDER BY id").fetchall() == [
            (1,),
            (2,),
        ]
        assert (
            conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='executor_attempts'"
            ).fetchone()[0]
            == seq
        )
        # No leftover scratch table from the rebuild.
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'executor_attempts__%'"
        ).fetchall()
    finally:
        conn.close()


def test_fresh_db_accepts_scout_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO executor_attempts (agent, backend, task_type, started_at) "
            "VALUES ('Desktop_Agent2', 'codex', 'scout', '2026-02-01')"
        )
        conn.commit()
    finally:
        conn.close()


# --- idempotency_keys: one declaration (re-land of PR #858) -------------------

IDEMPOTENCY_COLUMNS = ["key", "tool", "result_json", "created_at"]


def test_idempotency_schema_is_declared_once():
    """init_db and _migrate share one constant; no second or third CREATE."""
    from pathlib import Path

    text = Path(storage.__file__).read_text(encoding="utf-8")
    assert text.count("CREATE TABLE IF NOT EXISTS idempotency_keys") == 1
    assert text.count("CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created") == 1


def test_one_init_db_adds_idempotency_keys_to_a_db_without_it(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.execute("DROP TABLE idempotency_keys")
    conn.commit()
    conn.close()
    assert _indexes(tmp_path / "memory.db", "idempotency_keys") == set()

    storage.init_db()

    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(idempotency_keys)")]
    finally:
        conn.close()
    assert cols == IDEMPOTENCY_COLUMNS
    assert "idx_idempotency_keys_created" in _indexes(tmp_path / "memory.db", "idempotency_keys")


def test_idempotency_migration_keeps_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?,?,?,?)",
            ("preexisting", "tool", '{"ok": 1}', storage.now()),
        )
        conn.commit()
        storage._migrate(conn)
        conn.commit()
        row = conn.execute(
            "SELECT tool, result_json FROM idempotency_keys WHERE key='preexisting'"
        ).fetchone()
    finally:
        conn.close()
    storage.init_db()
    assert tuple(row) == ("tool", '{"ok": 1}')


def test_migrate_applies_idempotency_schema_without_committing(tmp_path, monkeypatch):
    """_migrate runs inside init_db's transaction; it must not COMMIT it early."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE idempotency_keys")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO meta(key, value) VALUES ('probe_858', 'x')")
        storage._apply_idempotency_schema(conn)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT 1 FROM meta WHERE key='probe_858'").fetchone() is None
    finally:
        conn.close()


# --- #1433 key constraints and partition indexes (task #2326) ------------------


def _pre_2326_store(tmp_path, monkeypatch):
    """A store whose eight rebuilt tables have the pre-#2326 shape, seeded with
    rows a real run keeps and rows it must delete. Returns the db path."""
    from unified_agent import constraint_migration

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    monkeypatch.setattr(storage, "run_post_init_hooks", lambda: None)
    storage.init_db()
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        for spec in constraint_migration.REBUILDS:
            table = spec.table
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            old = sql.replace(" REFERENCES tasks(id) ON DELETE CASCADE", "", 1)
            old = old.replace("TEXT PRIMARY KEY NOT NULL", "TEXT PRIMARY KEY", 1)
            assert old != sql, table
            own = [
                r[0]
                for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN "
                    "('index','trigger') AND sql IS NOT NULL",
                    (table,),
                )
            ]
            conn.execute(f"DROP TABLE {table}")
            conn.execute(old)
            for statement in own:
                conn.execute(statement)
        conn.execute("DELETE FROM meta WHERE key=?", (constraint_migration.MARKER_KEY,))
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (1, 'live', 'open', 'med', 't', 't')"
        )
        conn.execute(
            "INSERT INTO memory (id, content, created_at, updated_at) "
            "VALUES ('note-1', 'body', 't', 't')"
        )
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, body, thread_revision, created_at) "
            "VALUES (1, 1, 'ada', 'kept', 0, 't'), (7, 99999, 'ada', 'orphan', 0, 't')"
        )
        conn.execute(
            "INSERT INTO memory_embeddings (note_id, embedding) "
            "VALUES ('note-1', X'11'), (NULL, X'22')"
        )
        conn.execute("INSERT INTO memory_recall_votes (note_id) VALUES ('note-1'), (NULL)")
        conn.execute(
            "INSERT INTO node_dispatch_cwd (item_id, cwd_realpath, created_at) "
            "VALUES ('item-1', '/a', 't'), (NULL, '/b', 't')"
        )
        conn.execute(
            "INSERT INTO node_dispatch_wait (item_id, reason, expires_at, created_at) "
            "VALUES ('item-1', 'offline', 't', 't')"
        )
        conn.commit()
    finally:
        conn.close()
    storage.forget_initialized(db)
    return db


_DOOMED = {
    "task_comments": 1,
    "memory_embeddings": 1,
    "memory_recall_votes": 1,
    "node_dispatch_cwd": 1,
    "node_acceptance": 0,
    "ui_session_workos": 0,
    "node_dispatch_resume": 0,
    "node_dispatch_wait": 0,
}


def _dump(db):
    """Schema and every row of every ordinary table (iterdump needs sqlite-vec)."""
    conn = sqlite3.connect(db)
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        rows = {
            name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for _type, name, _tbl, sql in schema
            if _type == "table" and sql and not sql.startswith("CREATE VIRTUAL TABLE")
        }
        return schema, rows
    finally:
        conn.close()


def _shape(db, table):
    conn = sqlite3.connect(db)
    try:
        return (
            [tuple(r) for r in conn.execute(f"PRAGMA table_info({table})")],
            [tuple(r) for r in conn.execute(f"PRAGMA foreign_key_list({table})")],
            sorted(
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE tbl_name=? AND type IN "
                    "('index','trigger')",
                    (table,),
                )
            ),
        )
    finally:
        conn.close()


def _events(db, channel):
    conn = sqlite3.connect(db)
    try:
        return [
            json.loads(r[0])
            for r in conn.execute("SELECT payload FROM event_log WHERE channel=?", (channel,))
        ]
    finally:
        conn.close()


def test_constraint_rebuild_dry_run_backup_and_one_shot(tmp_path, monkeypatch, capsys):
    from unified_agent import constraint_migration, db_cli

    db = _pre_2326_store(tmp_path, monkeypatch)
    before = _dump(db)

    # Dry run: exact per-table counts, and the store is byte-for-byte unchanged.
    assert db_cli.run_db_cli(["migrate-constraints", "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["would_run"] and report["marker"] is None
    assert report["would_delete"] == _DOOMED
    assert report["would_delete_total"] == 4
    assert _dump(db) == before
    assert not (tmp_path / "backups").exists()

    # Real run, as a hub start performs it.
    storage.init_db()
    marker = json.loads(
        sqlite3.connect(db)
        .execute("SELECT value FROM meta WHERE key=?", (constraint_migration.MARKER_KEY,))
        .fetchone()[0]
    )
    assert marker["deleted"] == _DOOMED
    assert sorted(marker["rebuilt"]) == sorted(_DOOMED)
    backup = Path(marker["backup"])
    assert backup.parent == tmp_path / "backups"
    assert backup.stat().st_mode & 0o777 == 0o600

    # The backup verifies and holds the pre-change rows, including the doomed ones.
    snap = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snap.execute("SELECT id FROM task_comments ORDER BY id").fetchall() == [(1,), (7,)]
        assert snap.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE note_id IS NULL"
        ).fetchone() == (1,)
        assert dict(snap.execute("SELECT key, value FROM meta"))["schema_version"] == str(
            storage.SCHEMA_VERSION
        )
    finally:
        snap.close()

    # Exactly the doomed rows are gone; the new constraints hold.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT id, body FROM task_comments").fetchall() == [(1, "kept")]
        assert conn.execute("SELECT note_id FROM memory_embeddings").fetchall() == [("note-1",)]
        assert conn.execute("SELECT note_id FROM memory_recall_votes").fetchall() == [("note-1",)]
        assert conn.execute("SELECT item_id FROM node_dispatch_cwd").fetchall() == [("item-1",)]
        assert conn.execute("SELECT COUNT(*) FROM node_dispatch_wait").fetchone() == (1,)
        assert [
            (r[3], r[2], r[4], r[6]) for r in conn.execute("PRAGMA foreign_key_list(task_comments)")
        ] == [("task_id", "tasks", "id", "CASCADE")]
        # AUTOINCREMENT keeps its high-water mark: deleted id 7 is never reused.
        assert (
            conn.execute("SELECT seq FROM sqlite_sequence WHERE name='task_comments'").fetchone()[0]
            >= 7
        )
        # The trigger on tasks that names task_comments was parked and replayed.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='task_comments_on_task_delete'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO node_dispatch_cwd VALUES (NULL, '/c', 't')")
    finally:
        conn.close()
    assert "idx_task_comments_task" in _indexes(db, "task_comments")
    assert "idx_node_dispatch_resume_session" in _indexes(db, "node_dispatch_resume")

    (event,) = _events(db, constraint_migration.EVIDENCE_CHANNEL)
    assert event["deleted"] == _DOOMED and event["backup"] == str(backup)

    # The marker makes it one shot: no second backup, event or change.
    def rebuilt_tables():
        schema, rows = _dump(db)
        tables = set(_DOOMED)
        return [s for s in schema if s[2] in tables], {t: rows[t] for t in tables}

    after = rebuilt_tables()
    storage.forget_initialized(db)
    storage.init_db()
    conn = storage.connect()
    try:
        assert constraint_migration.run(conn) is None
    finally:
        conn.close()
    assert rebuilt_tables() == after
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert len(_events(db, constraint_migration.EVIDENCE_CHANNEL)) == 1
    assert db_cli.run_db_cli(["migrate-constraints"]) == 0
    assert "already applied" in capsys.readouterr().out


def test_constraint_rebuild_failure_rolls_back_everything(tmp_path, monkeypatch):
    from unified_agent import constraint_migration

    db = _pre_2326_store(tmp_path, monkeypatch)
    before = _dump(db)
    real = constraint_migration._rebuild_table
    calls = []

    def fail_on_third(conn, spec):
        calls.append(spec.table)
        if len(calls) == 3:
            raise sqlite3.OperationalError("injected")
        return real(conn, spec)

    monkeypatch.setattr(constraint_migration, "_rebuild_table", fail_on_third)
    conn = storage.connect()
    try:
        with pytest.raises(constraint_migration.ConstraintMigrationError, match="rolled back"):
            constraint_migration.run(conn)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    assert len(calls) == 3
    assert _dump(db) == before
    assert _events(db, constraint_migration.EVIDENCE_CHANNEL) == []

    # A later start retries from the untouched store and succeeds.
    monkeypatch.setattr(constraint_migration, "_rebuild_table", real)
    conn = storage.connect()
    try:
        assert constraint_migration.run(conn)["deleted"] == _DOOMED
    finally:
        conn.close()


def test_fresh_store_has_the_migrated_shape(tmp_path, monkeypatch):
    from unified_agent import constraint_migration

    migrated = _pre_2326_store(tmp_path / "old", monkeypatch)
    storage.init_db()

    monkeypatch.setenv("UA_HOME", str(tmp_path / "new"))
    storage.init_db()
    fresh = tmp_path / "new" / "memory.db"
    for spec in constraint_migration.REBUILDS:
        assert _shape(fresh, spec.table) == _shape(migrated, spec.table), spec.table

    # A fresh store needs no rebuild: marker only, no backup, no event.
    conn = sqlite3.connect(fresh)
    try:
        marker = json.loads(
            conn.execute(
                "SELECT value FROM meta WHERE key=?", (constraint_migration.MARKER_KEY,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert marker["rebuilt"] == [] and marker["backup"] is None
    assert not (tmp_path / "new" / "backups").exists()
    assert _events(fresh, constraint_migration.EVIDENCE_CHANNEL) == []


def test_partition_indexes_exist_and_downgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"

    def leading(conn):
        return {
            table
            for table in storage.ORG_ID_PARTITION_INDEX_TABLES
            if [r[2] for r in conn.execute(f"PRAGMA index_info(idx_{table}_org_id)")][:1]
            == ["org_id"]
        }

    conn = sqlite3.connect(db)
    try:
        assert len(storage.ORG_ID_PARTITION_INDEX_TABLES) == 18
        assert leading(conn) == set(storage.ORG_ID_PARTITION_INDEX_TABLES)
        storage.downgrade_org_id_indexes(conn)
        storage.downgrade_org_id_indexes(conn)
        conn.commit()
        assert leading(conn) == set()
    finally:
        conn.close()
    storage.forget_initialized(db)
    storage.init_db()
    conn = sqlite3.connect(db)
    try:
        assert leading(conn) == set(storage.ORG_ID_PARTITION_INDEX_TABLES)
    finally:
        conn.close()


@pytest.mark.parametrize("quote", ['"{}"', "`{}`", "[{}]"])
def test_constraint_rebuild_accepts_quoted_table_names(tmp_path, monkeypatch, quote):
    """Review of #2326: SQLite stores a quoted name after ALTER TABLE ... RENAME;
    the stored CREATE must still be recognised or every start fails."""
    from unified_agent import constraint_migration

    db = _pre_2326_store(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_comments'"
        ).fetchone()[0]
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='task_comments'",
            (sql.replace("task_comments", quote.format("task_comments"), 1),),
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(db)
    try:
        result = constraint_migration.run(conn)
    finally:
        conn.close()
    assert result is not None


def test_revision_and_operator_end_migrations_are_idempotent(tmp_path, monkeypatch):
    """Task #2480: running the additive migrations again changes nothing, existing
    rows start at revision 0, and each trigger still advances exactly once."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plans (id, goal, status, created_at, updated_at) "
            "VALUES ('p1', 'g', 'active', 't', 't')"
        )
        conn.commit()
        for _ in range(2):
            storage._add_revision_counters(conn)
            storage._add_operator_end_marker(conn)
        conn.commit()
        for table in ("plans", "goals"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert cols.count("revision") == 1
        session_cols = [r["name"] for r in conn.execute("PRAGMA table_info(agent_sessions)")]
        assert session_cols.count("operator_ended_by") == 1
        triggers = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {"plans_revision_bump", "goals_revision_bump"} <= triggers
        assert conn.execute("SELECT revision FROM plans WHERE id='p1'").fetchone()[0] == 0
        conn.execute("UPDATE plans SET goal='g2' WHERE id='p1'")
        conn.commit()
        assert conn.execute("SELECT revision FROM plans WHERE id='p1'").fetchone()[0] == 1
    finally:
        conn.close()
    storage.init_db()
