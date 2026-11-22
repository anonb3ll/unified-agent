"""Schema, migration and status-derivation invariants for the agent registry.

The migration rules asserted here come from the #178 index-loss incident: a table
rebuild dropped indexes, and a single init_db() — what a service restart does — left
them missing. Every test in the first class exists to make that class of defect
impossible for these tables.
"""

import sqlite3

import pytest

from unified_agent import config, registry, storage


def _indexes(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }


def _tables(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


class TestSchema:
    def test_tables_created_by_one_init(self):
        storage.init_db()
        conn = storage.connect()
        try:
            tables = _tables(conn)
        finally:
            conn.close()
        for name in ("agents", "agent_sessions", "control_flags", "ui_sessions", "host_reports"):
            assert name in tables, f"{name} missing after init_db()"

    def test_every_index_present_after_a_single_init(self):
        """One init_db() must leave every index in place — that is what a restart is."""
        storage.init_db()
        conn = storage.connect()
        try:
            present = _indexes(conn)
        finally:
            conn.close()
        for name, _target in storage._AGENT_REGISTRY_INDEXES:
            assert name in present, f"index {name} missing after a single init_db()"

    def test_idempotent_across_repeated_inits(self):
        for _ in range(3):
            storage.init_db()
        conn = storage.connect()
        try:
            present = _indexes(conn)
            assert conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"] == 0
        finally:
            conn.close()
        for name, _target in storage._AGENT_REGISTRY_INDEXES:
            assert name in present

    def test_schema_version_is_stamped(self):
        storage.init_db()
        conn = storage.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        assert int(row["value"]) == storage.SCHEMA_VERSION >= 14

    def test_session_status_is_constrained(self):
        storage.init_db()
        conn = storage.connect()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO agent_sessions (id, agent, kind, status, started_at,"
                    " last_heartbeat_at) VALUES ('x','a','k','banana','t','t')"
                )
        finally:
            conn.close()

    def test_deleting_a_task_preserves_its_session(self):
        """ON DELETE SET NULL, not CASCADE.

        Deleting a task must not erase the record that an agent spent real time
        running for it — that history is the point of the table.
        """
        storage.init_db()
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO tasks (title, status, priority, created_at, updated_at)"
                " VALUES ('t','open','med','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            task_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            conn.execute(
                "INSERT INTO agent_sessions (id, agent, kind, status, started_at,"
                " last_heartbeat_at, task_id) VALUES ('s1','A','pi_run','running','t','t',?)",
                (task_id,),
            )
            conn.commit()
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()
            row = conn.execute("SELECT task_id FROM agent_sessions WHERE id='s1'").fetchone()
        finally:
            conn.close()
        assert row is not None, "session row was cascade-deleted with its task"
        assert row["task_id"] is None


class TestBackfill:
    def _seed_history(self):
        """Write history the way the live DB actually looks, unregistered labels included."""
        storage.init_db()
        conn = storage.connect()
        try:
            conn.executemany(
                "INSERT INTO agent_events (agent, ts, kind, ref_id, summary)"
                " VALUES (?, ?, 'task', NULL, NULL)",
                [
                    ("Desktop_Agent1", "2026-01-01T00:00:00+00:00"),
                    ("Desktop_Agent1", "2026-01-05T00:00:00+00:00"),
                    ("unified-agent-cron", "2026-01-03T00:00:00+00:00"),
                    (None, "2026-01-04T00:00:00+00:00"),
                ],
            )
            conn.execute("DELETE FROM agents")
            conn.commit()
        finally:
            conn.close()

    def test_backfill_seeds_from_history(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_agent3")
        self._seed_history()
        storage.init_db()

        conn = storage.connect()
        try:
            rows = {r["label"]: r for r in conn.execute("SELECT * FROM agents").fetchall()}
        finally:
            conn.close()

        assert "Desktop_Agent1" in rows
        assert rows["Desktop_Agent1"]["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert rows["Desktop_Agent1"]["last_seen"] == "2026-01-05T00:00:00+00:00"
        assert rows["Desktop_Agent1"]["known"] == 1
        # An unregistered label still gets a row: the registry mirrors what happened.
        assert rows["unified-agent-cron"]["known"] == 0
        # NULL attribution must not become an agent named "None".
        assert None not in rows and "None" not in rows

    def test_backfill_runs_once_and_never_rewinds_last_seen(self, monkeypatch):
        """A second init_db() must not drag a live agent back to its historical time."""
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1")
        self._seed_history()
        storage.init_db()

        registry.reset_throttle()
        registry.touch_agent("Desktop_Agent1", via="hub_request", force=True)

        conn = storage.connect()
        try:
            fresh = conn.execute(
                "SELECT last_seen FROM agents WHERE label='Desktop_Agent1'"
            ).fetchone()["last_seen"]
            count_before = conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"]
        finally:
            conn.close()

        storage.init_db()  # re-run the migration

        conn = storage.connect()
        try:
            after = conn.execute(
                "SELECT last_seen, first_seen FROM agents WHERE label='Desktop_Agent1'"
            ).fetchone()
            count_after = conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"]
        finally:
            conn.close()

        assert count_after == count_before, "backfill duplicated rows on re-run"
        assert after["last_seen"] == fresh, "backfill rewound a live agent's last_seen"
        assert after["first_seen"] == "2026-01-01T00:00:00+00:00"


class TestDeriveStatus:
    @pytest.mark.parametrize(
        ("age_s", "expected"),
        [
            (0, "online"),
            (119, "online"),
            (121, "idle"),
            (16 * 60, "stale"),
            (25 * 3600, "offline"),
        ],
    )
    def test_boundaries(self, age_s, expected):
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        seen = (now - timedelta(seconds=age_s)).isoformat(timespec="seconds")
        assert registry.derive_status(seen, now=now) == expected

    def test_never_seen_is_unknown_not_offline(self):
        """A configured agent we have never observed is a different fact from one
        that went quiet; collapsing them would hide config drift."""
        assert registry.derive_status(None) == "unknown"

    def test_unparseable_timestamp_is_unknown(self):
        assert registry.derive_status("not-a-date") == "unknown"

    def test_future_timestamp_reads_online_not_negative(self):
        """memory.db is shared across machines, so clock skew is expected."""
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        future = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
        assert registry.derive_status(future, now=now) == "online"

    def test_thresholds_are_configurable(self, monkeypatch):
        from datetime import UTC, datetime, timedelta

        monkeypatch.setenv("UA_AGENT_STATUS_THRESHOLDS", "10,20,30")
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        seen = (now - timedelta(seconds=15)).isoformat(timespec="seconds")
        assert registry.derive_status(seen, now=now) == "idle"

    def test_malformed_threshold_config_falls_back(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_STATUS_THRESHOLDS", "nonsense")
        assert config.agent_status_thresholds() == (120.0, 900.0, 86400.0)

    def test_out_of_order_thresholds_rejected(self, monkeypatch):
        """A descending triple would make status non-monotonic in age."""
        monkeypatch.setenv("UA_AGENT_STATUS_THRESHOLDS", "900,120,86400")
        assert config.agent_status_thresholds() == (120.0, 900.0, 86400.0)


def test_get_session_finds_history_older_than_list_limit():
    storage.init_db()
    conn = storage.connect()
    try:
        rows = [
            (
                "old-session",
                "Desktop_Agent1",
                "cli",
                "completed",
                "2026-09-28T00:00:00Z",
                "2026-09-28T00:00:00Z",
            ),
            *[
                (
                    f"new-{i}",
                    "Desktop_Agent1",
                    "cli",
                    "completed",
                    "2026-09-29T00:00:00Z",
                    "2026-09-29T00:00:00Z",
                )
                for i in range(501)
            ],
        ]
        conn.executemany(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, last_heartbeat_at) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    session = registry.get_session("old-session")
    assert session is not None
    assert session["id"] == "old-session"
    assert session["status"] == "completed"
    assert "task_title" in session and "heartbeat_age_s" in session
