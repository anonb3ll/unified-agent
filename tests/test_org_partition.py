"""The org_id sentinel and its one storage chokepoint (task #1433, slice 3)."""

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from unified_agent import (
    config,
    constraint_migration,
    db_cli,
    edition,
    org_partition,
    org_partition_migration,
    sleep_pass,
    storage,
    tenant_tables,
)
from unified_agent.org_partition import (
    EXEMPT_TABLES,
    MARKER_KEY,
    SCOPED_TABLES,
    SENTINEL_ORG_ID,
    TRIGGER_PREFIX,
)
from unified_agent.tools import memory, tasks

SRC = Path(__file__).resolve().parents[1] / "src" / "unified_agent"

# Frozen: EXEMPT_TABLES may only shrink.
EXEMPT_BASELINE = frozenset(
    {"connector_oauth_pending", "connector_oauth_connections", "llm_broker_spend"}
)

# Every raw sqlite3.connect under src/. A new site fails until it is listed
# here with its reason, or goes through org_partition.open_store.
UNSCOPED_CONNECT_SITES = {
    "backup.py:*": "page-level copy and integrity checks of a backup file",
    "cloud_mirror_receiver.py:_connect": "the mirror receiver's own store",
    "constraint_migration.py:write_backup": "the backup file itself",
    "db_cli.py:_migrate_constraints": "dry run of a rebuild; reads stored values",
    "db_cli.py:_org_partition_report": "must see stored org_id values",
    "horizon2.py:*": "the LangGraph checkpoint store",
    "langgraph_pilot.py:*": "the LangGraph checkpoint store",
    "langgraph_resume.py:_connect": "resume leases in the LangGraph checkpoint store",
    "node_agent.py:*": "a Hermes kanban.db, not this store",
    "org_partition.py:open_store": "the chokepoint",
    "pilot_auth.py:*": "the pilot datastore",
    "storage.py:require_live_task_store": "reads sqlite_master only",
    "storage.py:connect": "the chokepoint's caller",
    "storage_backup.py:*": "page-level copy of the store",
    "tools/dash.py:*": "a Dash docset index",
    "tools/langgraph.py:*": "the LangGraph checkpoint store",
    "tools/synthetic_workflow.py:*": "the synthetic provider's disposable store",
}


def _raw():
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _partition(monkeypatch, backup_dir=None):
    monkeypatch.setenv("UA_ORG_PARTITION", "1")
    conn = storage.connect(scoped=False)
    try:
        return org_partition_migration.apply(conn, backup_dir=backup_dir)
    finally:
        conn.close()


def _triggers(conn):
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        if r[0].startswith(TRIGGER_PREFIX)
    }


def _task(conn, title="t", org_id=None):
    return conn.execute(
        "INSERT INTO tasks (title, status, created_at, updated_at, org_id) "
        "VALUES (?, 'open', 'now', 'now', ?)",
        (title, org_id),
    ).lastrowid


def _note(conn, note_id, org_id=None):
    conn.execute(
        "INSERT INTO memory (id, title, content, tags, author, created_at, updated_at, org_id) "
        "VALUES (?, 'title', 'quokka content', '[]', 'a', 'now', 'now', ?)",
        (note_id, org_id),
    )


def _synthetic_row(conn, table, serial):
    """A row built from the table's declared types; constraints are off in the caller."""
    names, marks, values = [], [], []
    for _cid, name, kind, notnull, default, primary in conn.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall():
        if name == "org_id" or (not notnull and not primary) or default is not None:
            continue
        names.append(f'"{name}"')
        marks.append("?")
        numeric = "INT" in kind.upper() or "REAL" in kind.upper()
        blob = "BLOB" in kind.upper()
        values.append(serial if numeric else b"x" if blob else f"{name}-{serial}")
    return names, marks, values


# --- off by default ---------------------------------------------------------------


def test_a_store_is_untouched_until_an_operator_applies(isolated_env):
    conn = storage.connect()
    assert type(conn) is sqlite3.Connection
    task = _task(conn)
    conn.commit()
    conn.close()
    with _raw() as raw:
        assert raw.execute("SELECT 1 FROM meta WHERE key=?", (MARKER_KEY,)).fetchone() is None
        assert _triggers(raw) == set()
        assert raw.execute("SELECT org_id FROM tasks WHERE id=?", (task,)).fetchone()[0] is None
    assert type(org_partition.open_store(config.db_path())) is sqlite3.Connection
    assert type(storage.connect_read_only()) is sqlite3.Connection


def test_init_never_starts_the_backfill_even_with_the_flag(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_ORG_PARTITION", "1")
    conn = storage.connect()
    _task(conn)
    conn.commit()
    conn.close()
    storage.init_db()
    with _raw() as raw:
        assert raw.execute("SELECT 1 FROM meta WHERE key=?", (MARKER_KEY,)).fetchone() is None
        assert raw.execute("SELECT COUNT(*) FROM tasks WHERE org_id IS NULL").fetchone()[0] == 1


def test_the_marker_is_absent_from_a_fresh_store_so_no_clock_value_reaches_it(isolated_env):
    with _raw() as raw:
        keys = {r[0] for r in raw.execute("SELECT key FROM meta")}
    assert not any("org_partition" in key for key in keys)


# --- apply ------------------------------------------------------------------------


def test_apply_needs_the_flag(isolated_env, monkeypatch):
    monkeypatch.delenv("UA_ORG_PARTITION", raising=False)
    conn = storage.connect(scoped=False)
    with pytest.raises(org_partition_migration.OrgPartitionError, match="UA_ORG_PARTITION"):
        org_partition_migration.apply(conn)
    conn.close()


def test_apply_refuses_a_tenanted_process(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_ORG_PARTITION", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    conn = storage.connect(scoped=False)
    with pytest.raises(org_partition_migration.OrgPartitionError, match="real organization"):
        org_partition_migration.apply(conn)
    conn.close()


def test_apply_refuses_rows_that_already_name_an_org(isolated_env, monkeypatch):
    conn = storage.connect()
    _task(conn, org_id="org_a")
    conn.commit()
    conn.close()
    with pytest.raises(org_partition_migration.OrgPartitionError, match="already name"):
        _partition(monkeypatch)
    with _raw() as raw:
        assert raw.execute("SELECT 1 FROM meta WHERE key=?", (MARKER_KEY,)).fetchone() is None
        assert _triggers(raw) == set()


def test_dry_run_reports_and_writes_nothing(isolated_env):
    conn = storage.connect()
    _task(conn)
    conn.commit()
    conn.close()
    with _raw() as raw:
        before = raw.total_changes
        report = org_partition_migration.dry_run(raw)
        assert raw.total_changes == before
    assert report["would_run"] is True
    assert report["would_stamp"]["tasks"] == 1
    assert report["marker"] is None


def test_apply_stamps_backs_up_and_records_once(isolated_env, tmp_path, monkeypatch):
    conn = storage.connect()
    task = _task(conn, "kept")
    conn.commit()
    conn.close()
    backups = tmp_path / "partition-backups"
    summary = _partition(monkeypatch, backups)
    assert summary["stamped"]["tasks"] == 1
    with _raw() as raw:
        assert raw.execute("SELECT org_id FROM tasks WHERE id=?", (task,)).fetchone()[0] == (
            SENTINEL_ORG_ID
        )
        recorded = json.loads(
            raw.execute("SELECT value FROM meta WHERE key=?", (MARKER_KEY,)).fetchone()[0]
        )
        event = raw.execute(
            "SELECT payload, org_id FROM event_log WHERE channel=?",
            (org_partition_migration.APPLIED_CHANNEL,),
        ).fetchone()
    assert recorded["backup"] == summary["backup"]
    assert event["org_id"] == SENTINEL_ORG_ID
    assert json.loads(event["payload"])["backup"] == summary["backup"]
    backup = sqlite3.connect(summary["backup"])
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert backup.execute("SELECT title, org_id FROM tasks").fetchall() == [("kept", None)]
    backup.close()
    assert _partition(monkeypatch, backups) is None  # one shot


def test_a_failed_apply_changes_nothing(isolated_env, monkeypatch):
    conn = storage.connect()
    _task(conn)
    conn.commit()
    conn.close()

    def broken(_conn, _tables):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(org_partition_migration, "_install_triggers", broken)
    with pytest.raises(org_partition_migration.OrgPartitionError, match="rolled back"):
        _partition(monkeypatch)
    with _raw() as raw:
        assert raw.execute("SELECT COUNT(*) FROM tasks WHERE org_id IS NULL").fetchone()[0] == 1
        assert raw.execute("SELECT 1 FROM meta WHERE key=?", (MARKER_KEY,)).fetchone() is None
        fences = raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE '%org_fence%'"
        ).fetchone()[0]
        channel = org_partition_migration.APPLIED_CHANNEL
        events = raw.execute("SELECT COUNT(*) FROM event_log WHERE channel=?", (channel,))
        assert events.fetchone()[0] == 0
    assert fences == 6


# --- every read that took NULL to mean the one org --------------------------------


@pytest.fixture
def partitioned(isolated_env, monkeypatch):
    conn = storage.connect()
    task = _task(conn, "before")
    _note(conn, "note-before")
    conn.commit()
    conn.close()
    _partition(monkeypatch)
    conn = storage.connect()
    assert isinstance(conn, org_partition.ScopedConnection)
    yield conn, task
    conn.close()


@pytest.mark.parametrize(
    ("sql", "params", "expected"),
    [
        ("SELECT COUNT(*) FROM tasks WHERE org_id IS ?", (None,), 1),
        ("SELECT COUNT(*) FROM tasks WHERE org_id IS NULL", (), 1),
        ("SELECT COUNT(*) FROM tasks WHERE org_id IS NOT ?", (None,), 0),
        ("SELECT COUNT(*) FROM tasks WHERE org_id IS NOT NULL", (), 0),
        ("SELECT COUNT(*) FROM tasks WHERE COALESCE(org_id, '') = ''", (), 1),
        ("SELECT COUNT(*) FROM tasks a JOIN tasks b ON a.org_id IS b.org_id", (), 1),
        ("SELECT COUNT(*) FROM tasks WHERE org_id IS ?", (SENTINEL_ORG_ID,), 0),
        ("SELECT COUNT(*) FROM memory m WHERE m.org_id IS ?", (None,), 1),
        (
            "SELECT COUNT(*) FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid "
            "WHERE memory_fts MATCH 'quokka' AND m.org_id IS ?",
            (None,),
            1,
        ),
        (
            "WITH RECURSIVE n(v) AS (SELECT 1 UNION ALL SELECT v + 1 FROM n WHERE v < 2) "
            "SELECT COUNT(*) FROM n JOIN tasks ON tasks.org_id IS ?",
            (None,),
            2,
        ),
    ],
)
def test_reads_of_the_null_org_keep_their_answers(partitioned, sql, params, expected):
    conn, _task_id = partitioned
    assert conn.execute(sql, params).fetchone()[0] == expected


def test_a_query_naming_an_index_or_rowid_keeps_its_shape(partitioned):
    conn, task = partitioned
    conn.execute(
        "INSERT INTO message_index (message_id, recipient, sender, act, conversation_id, "
        "expects_reply, event_ulid, created_at) VALUES ('m', 'r', 's', 'inform', 'c', 0, 'u', 'n')"
    )
    hinted = conn.execute(
        "SELECT i.message_id FROM message_index i INDEXED BY idx_message_index_tenant_id "
        "WHERE i.org_id IS ?",
        (None,),
    ).fetchall()
    assert [r["message_id"] for r in hinted] == ["m"]
    conn.rollback()
    plain = _raw().execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    for sql in (
        "SELECT * FROM tasks WHERE id=? ORDER BY rowid",
        "SELECT t.* FROM tasks t WHERE t.id=? ORDER BY t.rowid",
        "SELECT * FROM tasks WHERE rowid=?",
    ):
        assert conn.execute(sql, (task,)).fetchone().keys() == plain.keys()


def test_the_sentinel_never_reaches_python(partitioned):
    conn, task = partitioned
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    assert row["org_id"] is None
    plain = _raw().execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    assert row.keys() == plain.keys()
    for sql in ("SELECT org_id FROM memory", "SELECT DISTINCT t.org_id x FROM tasks t"):
        assert SENTINEL_ORG_ID not in map(str, conn.execute(sql).fetchall()[0])


def test_the_managed_worker_check_still_sees_its_token(partitioned):
    conn, _ = partitioned
    conn.execute(
        "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at) "
        "VALUES ('worker_one', 'h', 'sha256', 'active', 'now')"
    )
    conn.commit()
    config._drop_managed_worker_conn()
    try:
        assert config._managed_worker_label("worker_one") is True
    finally:
        config._drop_managed_worker_conn()


def test_every_reader_outside_connect_is_scoped(partitioned):
    readers = [storage.connect_read_only(), sleep_pass._connect_ro()]
    try:
        for reader in readers:
            assert isinstance(reader, org_partition.ScopedConnection)
            sql = "SELECT COUNT(*) FROM memory WHERE org_id IS ?"
            assert reader.execute(sql, (None,)).fetchone()[0] == 1
    finally:
        for reader in readers:
            reader.close()


def test_a_tenant_cannot_open_a_partitioned_store(partitioned, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert edition.tenant_org_id() == "org_a"
    with pytest.raises(storage.LiveStoreError, match="partitioned"):
        storage.connect()


def test_a_tenant_cannot_read_a_partitioned_store_around_connect(partitioned, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    uri = f"file:{config.db_path()}?mode=ro"
    with pytest.raises(edition.TenantStoreMismatchError, match="partitioned"):
        org_partition.open_store(uri, uri=True)


def test_a_join_naming_rowid_keeps_each_star_to_its_own_columns(partitioned):
    conn, task = partitioned
    tasks = [row[1] for row in _raw().execute("PRAGMA table_info(tasks)")]
    notes = [row[1] for row in _raw().execute("PRAGMA table_info(memory)")]
    row = conn.execute(
        "SELECT t.*, m.* FROM tasks t JOIN memory m ON m.rowid = m.rowid WHERE t.id=?",
        (task,),
    ).fetchone()
    assert row.keys() == tasks + notes
    assert row["org_id"] is None
    plain = conn.execute(
        "SELECT t.*, COUNT(*) AS n FROM tasks t JOIN memory m ON m.rowid = m.rowid WHERE t.id=?",
        (task,),
    ).fetchone()
    assert plain.keys() == [*tasks, "n"]
    with pytest.raises(sqlite3.NotSupportedError, match="join"):
        conn.execute("SELECT * FROM tasks t JOIN memory m ON m.rowid = m.rowid")


def test_only_a_stars_own_from_clause_decides_a_rowid_query(partitioned):
    conn, task = partitioned
    plain = _raw().execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    for sql in (
        "SELECT * FROM tasks WHERE rowid=? AND id NOT IN (SELECT rowid + 1000 FROM memory)",
        "SELECT * FROM tasks WHERE rowid=? AND id NOT IN "
        "(SELECT t.id + 1000 FROM tasks t JOIN tasks u ON u.id = t.id)",
    ):
        assert conn.execute(sql, (task,)).fetchone().keys() == plain.keys()
    with pytest.raises(sqlite3.NotSupportedError, match="join"):
        conn.execute("SELECT * FROM tasks, meta WHERE tasks.rowid=?", (task,))
    with pytest.raises(sqlite3.NotSupportedError, match="join"):
        conn.execute("SELECT * FROM meta, tasks WHERE tasks.rowid=?", (task,))


# --- writes -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "COALESCE(title, org_id, 'none')",
        "json_array(id, org_id, title)",
        "COALESCE(org_id, 'none')",
    ],
)
def test_returning_an_expression_stays_valid_sql(partitioned, expression):
    conn, task = partitioned
    row = conn.execute(
        f"UPDATE tasks SET title='' WHERE id=? RETURNING {expression} AS shown", (task,)
    ).fetchone()
    assert org_partition.SENTINEL_ORG_ID not in str(row["shown"])
    conn.rollback()


@pytest.mark.parametrize(
    "returning",
    ["org_id", "org_id, title", "title, org_id", "tasks.org_id", "org_id AS org_id", "*"],
)
def test_returning_never_shows_the_sentinel(partitioned, returning):
    conn, task = partitioned
    row = conn.execute(
        f"UPDATE tasks SET title='renamed' WHERE id=? AND org_id IS ? RETURNING {returning}",
        (task, None),
    ).fetchone()
    assert dict(row)["org_id"] is None
    conn.rollback()


def test_returning_keeps_its_shape_and_the_store_keeps_the_sentinel(partitioned):
    conn, task = partitioned
    plain = _raw().execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    row = conn.execute(
        "INSERT INTO tasks (title, status, created_at, updated_at) "
        "VALUES ('new', 'open', 'n', 'n') RETURNING *"
    ).fetchone()
    assert row.keys() == plain.keys()
    assert row["org_id"] is None
    masked = conn.execute(
        "UPDATE tasks SET title='x' WHERE id=? RETURNING COALESCE(org_id, 'none') AS shown",
        (task,),
    ).fetchone()
    assert masked["shown"] == "none"
    conn.commit()
    stored = _raw().execute("SELECT org_id FROM tasks WHERE id=?", (row["id"],)).fetchone()
    assert stored[0] == org_partition.SENTINEL_ORG_ID


def test_fenced_writes_still_find_their_rows(partitioned):
    conn, task = partitioned
    cur = conn.execute("UPDATE tasks SET title='x' WHERE id=? AND org_id IS ?", (task, None))
    assert cur.rowcount == 1
    returned = conn.execute(
        "UPDATE tasks SET title='y' WHERE id=? AND org_id IS NULL RETURNING id", (task,)
    ).fetchall()
    assert [r["id"] for r in returned] == [task]
    assert conn.execute("DELETE FROM tasks WHERE id=? AND org_id IS ?", (task, None)).rowcount == 1
    assert conn.in_transaction  # the implicit BEGIN survived the rewrite
    conn.rollback()
    assert conn.execute("SELECT title FROM tasks WHERE id=?", (task,)).fetchone()[0] == "before"


def test_a_subquery_inside_a_write_stays_scoped(partitioned):
    conn, task = partitioned
    cur = conn.execute(
        "UPDATE tasks SET title='z' WHERE id IN (SELECT t.id FROM tasks t WHERE t.org_id IS ?)",
        (None,),
    )
    assert cur.rowcount == 1
    conn.rollback()
    assert task


def test_inserts_stamp_before_unique_keys_are_checked(partitioned):
    conn, task = partitioned
    insert = (
        "INSERT INTO task_holds (root_task_id, mode, status, reason, release_policy, "
        "created_by, created_at, org_id) "
        "VALUES (?, 'pause', 'active', 'r', 'manual', 'a', 'now', ?)"
    )
    conn.execute(insert, (task, None))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(insert, (task, None))
    conn.rollback()


def test_an_insert_that_omits_org_id_is_stamped(partitioned):
    conn, _ = partitioned
    conn.execute(
        "INSERT INTO memory_entities(note_id, kind, value) VALUES (?, '', '')", ("note-before",)
    )
    conn.commit()
    with _raw() as raw:
        assert raw.execute("SELECT org_id FROM memory_entities").fetchone()[0] == SENTINEL_ORG_ID


def test_upsert_fenced_on_excluded_org_still_updates(partitioned):
    conn, _ = partitioned
    upsert = (
        "INSERT INTO message_index (message_id, recipient, sender, act, conversation_id, "
        "expects_reply, event_ulid, created_at, org_id) "
        "VALUES ('m1', 'r', 's', 'inform', 'c', 0, 'u', 'now', ?) "
        "ON CONFLICT(message_id, COALESCE(org_id, '')) DO UPDATE SET read_at='answered' "
        "WHERE message_index.org_id IS excluded.org_id"
    )
    conn.execute(upsert, (None,))
    conn.execute(upsert, (None,))
    rows = conn.execute("SELECT COUNT(*), MAX(read_at) FROM message_index").fetchone()
    assert tuple(rows) == (1, "answered")
    conn.rollback()


def test_insert_select_keeps_the_stored_org_for_the_memory_fence(partitioned):
    conn, _ = partitioned
    conn.execute(
        "INSERT OR IGNORE INTO memory_entities (note_id, kind, value, org_id) "
        "SELECT id, ?, ?, org_id FROM memory WHERE id=? AND org_id IS ?",
        ("k", "v", "note-before", None),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 1


def test_executemany_and_cursors_go_through_the_chokepoint(partitioned):
    conn, _ = partitioned
    cur = conn.cursor()
    assert isinstance(cur, org_partition.ScopedCursor)
    cur.executemany(
        "INSERT INTO tasks (title, status, created_at, updated_at, org_id) "
        "VALUES (?, 'open', 'now', 'now', ?)",
        [("a", None), ("b", None)],
    )
    assert cur.rowcount == 2
    conn.commit()
    with _raw() as raw:
        stamped = raw.execute("SELECT COUNT(*) FROM tasks WHERE org_id IS NOT NULL").fetchone()[0]
    assert stamped == 3


def test_a_raw_writer_is_stamped_and_fenced_by_the_triggers(partitioned):
    conn, _ = partitioned
    raw = _raw()
    task = _task(raw, "raw")
    raw.commit()
    assert raw.execute("SELECT org_id FROM tasks WHERE id=?", (task,)).fetchone()[0] == (
        SENTINEL_ORG_ID
    )
    with pytest.raises(sqlite3.IntegrityError, match="another org"):
        _task(raw, "foreign", org_id="org_b")
    with pytest.raises(sqlite3.IntegrityError, match="another org"):
        raw.execute("UPDATE tasks SET org_id=NULL WHERE id=?", (task,))
    raw.rollback()
    raw.execute(
        "INSERT INTO event_log (ulid, channel, payload, created_at, entry_hash) "
        "VALUES ('u', 'c', '{}', 'now', 'h')"
    )
    raw.commit()
    assert raw.execute("SELECT org_id FROM event_log WHERE ulid='u'").fetchone()[0] == (
        SENTINEL_ORG_ID
    )
    assert raw.execute("SELECT COUNT(*) FROM event_log_maintenance").fetchone()[0] == 0
    raw.close()


# --- real flows, before and after -------------------------------------------------


def test_tools_answer_the_same_before_and_after_the_backfill(isolated_env, monkeypatch):
    added = tasks.task_add("a task for the comparison", project="unified-agent")
    note = memory.memory_save("the quokka comparison note", author="tester")

    def views():
        listed = tasks.task_list(status="open", project="unified-agent")["tasks"]
        fetched = memory.memory_get(note["id"])
        found = memory.memory_search("quokka comparison")
        kept = {k: v for k, v in fetched.items() if k not in {"last_accessed", "access_count"}}
        return listed, kept, [r["id"] for r in found["results"]]

    before = views()
    _partition(monkeypatch)
    assert views() == before
    assert added["id"] in [t["id"] for t in before[0]]
    with _raw() as raw:
        assert raw.execute("SELECT org_id FROM memory WHERE id=?", (note["id"],)).fetchone()[0] == (
            SENTINEL_ORG_ID
        )


def test_the_event_chain_verifies_across_apply_and_downgrade(isolated_env, monkeypatch):
    from unified_agent import events

    events.record_event("agent.test.partition", payload={"n": 1})
    assert events.verify_event_chain()["verified"] is True
    _partition(monkeypatch)
    events.record_event("agent.test.partition", payload={"n": 2})
    assert events.verify_event_chain()["verified"] is True
    conn = storage.connect(scoped=False)
    org_partition_migration.downgrade(conn)
    conn.close()
    assert events.verify_event_chain()["verified"] is True


# --- downgrade --------------------------------------------------------------------


def _org_columns(raw):
    state = {}
    for table in sorted(SCOPED_TABLES):
        if raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            state[table] = raw.execute(
                f'SELECT rowid, org_id FROM "{table}" ORDER BY rowid'
            ).fetchall()
    return state


def test_downgrade_returns_the_store_to_null_and_old_reads_work(isolated_env, monkeypatch):
    conn = storage.connect()
    task = _task(conn, "before")
    conn.commit()
    conn.close()
    with _raw() as raw:
        schema = sorted(tuple(r) for r in raw.execute("SELECT type, name, sql FROM sqlite_master"))
    _partition(monkeypatch)
    conn = storage.connect()
    written = _task(conn, "while partitioned")
    conn.commit()
    conn.close()

    monkeypatch.delenv("UA_ORG_PARTITION")  # a recovery never depends on the flag
    conn = storage.connect(scoped=False)
    summary = org_partition_migration.downgrade(conn)
    assert summary["cleared"]["tasks"] == 2
    assert org_partition_migration.downgrade(conn) is None
    conn.close()

    with _raw() as raw:
        assert _triggers(raw) == set()
        assert raw.execute("SELECT 1 FROM meta WHERE key=?", (MARKER_KEY,)).fetchone() is None
        after = sorted(tuple(r) for r in raw.execute("SELECT type, name, sql FROM sqlite_master"))
        for table, rows in _org_columns(raw).items():
            assert [r["org_id"] for r in rows] == [None] * len(rows), table
    assert after == schema
    conn = storage.connect()
    assert type(conn) is sqlite3.Connection
    rows = conn.execute("SELECT id FROM tasks WHERE org_id IS ?", (None,)).fetchall()
    assert {r["id"] for r in rows} == {task, written}
    conn.close()


def test_a_store_can_be_partitioned_again_after_a_downgrade(isolated_env, monkeypatch):
    _partition(monkeypatch)
    conn = storage.connect(scoped=False)
    org_partition_migration.downgrade(conn)
    conn.close()
    assert _partition(monkeypatch) is not None


def test_init_brings_a_table_that_joined_later_into_the_partition(partitioned):
    conn, _ = partitioned
    conn.close()
    with _raw() as raw:
        for kind in ("stamp", "update", "insert", "delete"):
            raw.execute(f"DROP TRIGGER {TRIGGER_PREFIX}{kind}_goals")
        raw.execute("PRAGMA ignore_check_constraints=ON")
        names, marks, values = _synthetic_row(raw, "goals", 1)
        raw.execute(f"INSERT INTO goals ({', '.join(names)}) VALUES ({', '.join(marks)})", values)
    storage.init_db()
    with _raw() as raw:
        assert f"{TRIGGER_PREFIX}stamp_goals" in _triggers(raw)
        assert raw.execute("SELECT org_id FROM goals").fetchone()[0] == SENTINEL_ORG_ID


def test_reconcile_does_nothing_without_the_marker(isolated_env):
    conn = storage.connect(scoped=False)
    assert org_partition_migration.reconcile(conn) == {}
    assert _triggers(conn) == set()
    conn.close()


# --- the CLI ----------------------------------------------------------------------


def test_cli_report_apply_and_downgrade(isolated_env, monkeypatch, capsys):
    monkeypatch.delenv("UA_ORG_PARTITION", raising=False)
    assert db_cli.main(["org-partition", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["marker"] is None
    assert db_cli.main(["org-partition", "--apply"]) == 1
    assert "UA_ORG_PARTITION" in capsys.readouterr().err
    monkeypatch.setenv("UA_ORG_PARTITION", "1")
    assert db_cli.main(["org-partition", "--apply", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    assert db_cli.main(["org-partition", "--apply"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert db_cli.main(["org-partition"]) == 0
    assert "partitioned" in capsys.readouterr().out
    monkeypatch.delenv("UA_ORG_PARTITION")
    assert db_cli.main(["org-partition", "--downgrade", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    assert db_cli.main(["org-partition", "--path", "x", "--apply"]) == 2


# --- the ratchet ------------------------------------------------------------------


def test_every_partitioned_table_is_scoped_or_has_a_stated_reason():
    partitioned = tenant_tables.partitioned_tables()
    hub = {t for t in partitioned if tenant_tables.TABLE_TENANCY[t].store == tenant_tables.HUB}
    pending = set(tenant_tables.PARTITION_PENDING)
    assert hub - pending - set(EXEMPT_TABLES) == set(SCOPED_TABLES)
    assert not SCOPED_TABLES & pending
    assert set(EXEMPT_TABLES) <= EXEMPT_BASELINE, "EXEMPT_TABLES may only shrink"
    assert SCOPED_TABLES <= partitioned


def test_a_table_with_a_nullable_org_id_cannot_stay_out(isolated_env):
    with _raw() as raw:
        names = [
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%'"
            )
        ]
        for table in names:
            cols = raw.execute(f'PRAGMA table_info("{table}")').fetchall()
            org = [c for c in cols if c[1] == "org_id"]
            if table in SCOPED_TABLES:
                assert org and not org[0][3], f"{table} is scoped without a nullable org_id"
            elif table in EXEMPT_TABLES:
                assert org and org[0][3], f"{table} is exempt because its org_id is NOT NULL"
            else:
                assert not org, f"{table} has an org_id outside the chokepoint"


def _connect_sites():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                current, function = node, "<module>"
                while current in parents:
                    current = parents[current]
                    if isinstance(current, ast.FunctionDef):
                        function = current.name
                        break
                yield path.relative_to(SRC).as_posix(), function


def test_no_new_raw_connection_can_step_around_the_chokepoint():
    sites = set(_connect_sites())
    unknown = {
        f"{file}:{function}"
        for file, function in sites
        if f"{file}:{function}" not in UNSCOPED_CONNECT_SITES
        and f"{file}:*" not in UNSCOPED_CONNECT_SITES
    }
    assert not unknown, f"open the hub store through org_partition.open_store: {sorted(unknown)}"
    keys = {f"{f}:{fn}" for f, fn in sites} | {f"{f}:*" for f, _ in sites}
    assert not set(UNSCOPED_CONNECT_SITES) - keys, "stale UNSCOPED_CONNECT_SITES entry"


def test_no_sql_names_a_scoped_table_through_main():
    tables = "|".join(sorted(SCOPED_TABLES))
    pattern = re.compile(rf"""\bmain\.["'`\[]?(?:{tables})\b""", re.IGNORECASE)
    hits = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "org_partition.py" and pattern.search(path.read_text())
    ]
    assert not hits, f"main.<table> reads past the chokepoint: {hits}"


def test_org_equality_is_only_used_on_tables_the_chokepoint_leaves_alone():
    pattern = re.compile(r"\borg_id\s*=\s*\?", re.IGNORECASE)
    allowed = {"connector_oauth.py", "llm_spend.py", "invocation_service.py"}
    hits = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name not in allowed
        and any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "SET" not in node.value.upper()
            and pattern.search(node.value)
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert not hits, f"org_id = ? cannot match NULL; use IS (or list the table): {sorted(hits)}"


@pytest.mark.parametrize("table", sorted(SCOPED_TABLES))
def test_each_scoped_table_stamps_hides_and_fences(isolated_env, monkeypatch, table):
    _partition(monkeypatch)
    raw = _raw()
    if not raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        raw.close()
        pytest.skip(f"{table} is created on demand")
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("PRAGMA ignore_check_constraints=ON")
    for row in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        if "org_fence" in row[0]:
            raw.execute(f'DROP TRIGGER "{row[0]}"')
    raw.execute(f"DROP TRIGGER {TRIGGER_PREFIX}insert_{table}")
    names, marks, values = _synthetic_row(raw, table, 900001)
    insert = (
        f'INSERT INTO "{table}" ({", ".join([*names, "org_id"])}) '
        f"VALUES ({', '.join([*marks, '?'])})"
    )
    foreign_id = raw.execute(insert, [*values, "org_b"]).lastrowid
    names, marks, values = _synthetic_row(raw, table, 900002)
    mine_id = raw.execute(insert, [*values, None]).lastrowid
    raw.commit()
    assert raw.execute(f'SELECT org_id FROM "{table}" WHERE rowid=?', (mine_id,)).fetchone()[0] == (
        SENTINEL_ORG_ID
    )

    conn = storage.connect()
    rows = conn.execute(f'SELECT rowid, org_id FROM "{table}"').fetchall()
    assert foreign_id not in [r["rowid"] for r in rows], "another org's row was readable"
    assert [r["org_id"] for r in rows if r["rowid"] == mine_id] == [None]
    assert (
        conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE rowid=?', (foreign_id,)).fetchone()[0]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError, match="another org"):
        conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (foreign_id,))
    with pytest.raises(sqlite3.IntegrityError, match="another org"):
        conn.execute(f'UPDATE "{table}" SET org_id=? WHERE rowid=?', ("org_c", mine_id))
    conn.rollback()
    conn.close()
    raw.close()


def test_rewrite_leaves_everything_else_alone():
    columns = {"tasks": ("id", "org_id")}.get
    for sql in (
        "PRAGMA table_info(tasks)",
        "BEGIN IMMEDIATE",
        "COMMIT",
        "CREATE INDEX i ON tasks(id)",
        "SELECT 'tasks' FROM meta",
        "SELECT value FROM meta WHERE key='tasks'",
        "SELECT * FROM agents",
    ):
        assert org_partition.rewrite(sql, columns) == sql


def test_writes_keep_their_leading_verb_for_pythons_dml_handling():
    columns = {"tasks": ("id", "org_id"), "memory": ("id", "org_id")}.get
    for sql in (
        "INSERT INTO tasks (id, org_id) VALUES (?, ?)",
        "UPDATE tasks SET id=1 WHERE org_id IS ?",
        "DELETE FROM tasks WHERE id IN (SELECT id FROM memory WHERE org_id IS ?)",
        "REPLACE INTO tasks (id) VALUES (1)",
    ):
        rewritten = org_partition.rewrite(sql, columns)
        assert rewritten != sql
        assert rewritten.split()[0] == sql.split()[0]


def test_a_statements_own_cte_is_not_shadowed():
    columns = {"tasks": ("id", "org_id")}.get
    sql = "WITH tasks AS (SELECT 1 AS id) SELECT id FROM tasks"
    assert org_partition.rewrite(sql, columns) == sql


def test_the_backfill_backup_is_not_a_constraint_backup():
    assert org_partition_migration.BACKUP_PREFIX != constraint_migration.BACKUP_PREFIX
