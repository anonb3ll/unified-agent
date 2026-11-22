"""Quota projections read existing metadata without contending with its writer."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, credential_pool, quota_observe, storage
from unified_agent.dispatch import coordinator


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))


@pytest.fixture
def observed_store():
    credential = credential_pool.credential_pool_register("codex", "fixture", "committed note")[
        "credential"
    ]
    observed_at = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO executor_attempts "
            "(agent, backend, started_at, finished_at, outcome, detail, failure_class) "
            "VALUES (?, ?, ?, ?, 'failure', ?, ?)",
            (
                "Desktop_Agent2",
                "codex",
                observed_at,
                observed_at,
                "committed quota exhaustion",
                quota_observe.FAILURE_PROVIDER_QUOTA_EXHAUSTED,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    storage.join_init_background_threads()
    return credential, observed_at


def _deny_init(monkeypatch):
    def forbidden_init():
        pytest.fail("an advisory metadata read attempted init_db")

    monkeypatch.setattr(storage, "init_db", forbidden_init)


def test_initialized_projection_never_initializes_or_migrates(observed_store, monkeypatch):
    credential, observed_at = observed_store
    _deny_init(monkeypatch)
    observation = quota_observe.latest_quota_exhaustion_for_lane("codex")
    assert observation["observed_at"] == observed_at
    assert observation["evidence"] == "committed quota exhaustion"
    assert credential_pool.credential_pool_list()["credentials"] == [credential]
    codex = next(
        row for row in quota_observe.build_quota_status()["quotas"] if row["label"] == "codex"
    )
    assert codex["availability"] == "unavailable"
    assert codex["source"] == "executor_attempts.failure_class"


@pytest.mark.parametrize("reader", ["observation", "registry", "projection"])
def test_advisory_reads_see_committed_metadata_while_writer_holds_lock(observed_store, reader):
    credential, observed_at = observed_store
    writer = storage.connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE executor_attempts SET detail='uncommitted observation'")
        writer.execute("UPDATE credential_pool SET note='uncommitted note'")
        changes = writer.total_changes
        before_bytes = Path(config.db_path()).read_bytes()
        start = time.monotonic()
        if reader == "observation":
            result = quota_observe.latest_quota_exhaustion_for_lane("codex")
            assert result["evidence"] == "committed quota exhaustion"
            assert result["observed_at"] == observed_at
        elif reader == "registry":
            assert credential_pool.credential_pool_list()["credentials"] == [credential]
        else:
            result = quota_observe.build_quota_status()
            codex = next(row for row in result["quotas"] if row["label"] == "codex")
            assert codex["availability"] == "unavailable"
            assert codex["evidence"] == "committed quota exhaustion"
            assert next(row for row in result["quotas"] if row["label"] == "fixture")
        assert time.monotonic() - start < 1.5
        assert writer.total_changes == changes
        assert Path(config.db_path()).read_bytes() == before_bytes
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize(
    "state", ["missing", "empty", "corrupt", "retired", "future", "columns", "directory"]
)
def test_unavailable_metadata_is_unknown_without_bootstrap(tmp_path, monkeypatch, state):
    home = tmp_path / "unavailable home?with#uri"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    path = config.db_path()
    if state in {"retired", "future", "columns"}:
        storage.init_db()
        if state == "retired":
            storage.no_local_memory_db_marker().write_text("fixture retired\n", encoding="utf-8")
        else:
            conn = storage.connect()
            try:
                if state == "future":
                    conn.execute(
                        "UPDATE meta SET value=? WHERE key='schema_version'",
                        (str(storage.SCHEMA_VERSION + 1),),
                    )
                else:
                    conn.execute(
                        "ALTER TABLE executor_attempts RENAME COLUMN detail TO legacy_detail"
                    )
                    conn.execute("ALTER TABLE credential_pool RENAME COLUMN note TO legacy_note")
                conn.commit()
            finally:
                conn.close()
    elif state == "empty":
        sqlite3.connect(path).close()
    elif state == "corrupt":
        path.write_bytes(b"fixture corrupt metadata")
    elif state == "directory":
        path.mkdir()
    before = path.read_bytes() if path.is_file() else None
    _deny_init(monkeypatch)
    rows = quota_observe.build_quota_status()["quotas"]
    assert rows and all(row["availability"] == "unknown" for row in rows)
    assert all(row.get("metadata_error") for row in rows)
    with pytest.raises(ToolError, match="credential metadata unavailable"):
        credential_pool.credential_pool_list()
    if state == "missing":
        assert not path.exists()
    elif before is not None:
        assert path.read_bytes() == before


def test_read_only_connection_preserves_uri_path_and_refuses_writes(tmp_path, monkeypatch):
    home = tmp_path / "metadata home?with#uri"
    monkeypatch.setenv("UA_HOME", str(home))
    storage.init_db()
    _deny_init(monkeypatch)
    conn = storage.connect_read_only()
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
            "value"
        ] == str(storage.SCHEMA_VERSION)
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            conn.execute("INSERT INTO meta(key, value) VALUES ('reader-write', 'forbidden')")
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_meter_provenance_survives_unavailable_metadata(tmp_path, monkeypatch):
    sessions = tmp_path / "synthetic-sessions"
    sessions.mkdir()
    (sessions / "meter.jsonl").write_text(
        json.dumps(
            {
                "timestamp": storage.now(),
                "payload": {
                    "rate_limits": {"primary": {"used_percent": 25, "window_minutes": 10080}}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))
    monkeypatch.setenv("UA_HOME", str(tmp_path / "missing-home"))
    _deny_init(monkeypatch)
    codex = next(
        row for row in quota_observe.build_quota_status()["quotas"] if row["label"] == "codex"
    )
    assert codex["availability"] == "unknown"
    assert codex["source"] == "codex_rollout_rate_limits"
    assert codex["used_percent"] == 25
    assert codex["metadata_error"]
    assert "used_percent=25" in codex["evidence"]
    assert not config.db_path().exists()


@pytest.mark.parametrize("metadata_state", ["missing", "corrupt"])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("reached", [False, True])
def test_meter_exhaustion_still_gates_without_metadata(
    tmp_path, monkeypatch, metadata_state, stale, reached
):
    checked = datetime(2026, 9, 29, 12, tzinfo=UTC)
    observed = checked - timedelta(days=2) if stale else checked
    sessions = tmp_path / "synthetic-sessions"
    sessions.mkdir()
    (sessions / "meter.jsonl").write_text(
        json.dumps(
            {
                "timestamp": observed.isoformat(),
                "payload": {
                    "rate_limits": {
                        "primary": {"used_percent": 25 if reached else 100},
                        "rate_limit_reached_type": "weekly" if reached else None,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "unavailable-home"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))
    monkeypatch.setenv("UA_QUOTA_EXHAUSTION_TTL_S", "86400")
    path = config.db_path()
    if metadata_state == "corrupt":
        path.write_bytes(b"fixture corrupt metadata")
    _deny_init(monkeypatch)
    status = quota_observe.build_quota_status(checked_at=checked.isoformat())
    codex = next(row for row in status["quotas"] if row["label"] == "codex")
    assert codex["source"] == "codex_rollout_rate_limits"
    assert codex["observed_at"] == observed.isoformat()
    assert codex["used_percent"] == (25 if reached else 100)
    assert codex["metadata_error"]
    if stale:
        assert codex["availability"] == "unknown"
        assert codex["failure_class"] is None
        assert codex["stale"] is True
        assert "stale" in codex["evidence"]
        assert coordinator._quota_gate("Desktop_Agent2", quota_status=status) is None
    else:
        assert codex["availability"] == "unavailable"
        assert codex["failure_class"] == quota_observe.FAILURE_PROVIDER_QUOTA_EXHAUSTED
        assert coordinator._quota_gate("Desktop_Agent2", quota_status=status)["lane"] == "codex"
    if metadata_state == "missing":
        assert not path.exists()


def _checkpointed_wal_store(tmp_path, monkeypatch):
    home = tmp_path / "checkpointed-metadata"
    monkeypatch.setenv("UA_HOME", str(home))
    storage.init_db()
    storage.join_init_background_threads()
    path = Path(config.db_path())
    conn = storage.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        conn.close()
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    assert all(not sidecar.exists() for sidecar in sidecars)
    _deny_init(monkeypatch)
    return home, path, sidecars


def test_application_read_only_wal_reader_can_create_sqlite_coordination_files(
    tmp_path, monkeypatch
):
    home, path, sidecars = _checkpointed_wal_store(tmp_path, monkeypatch)
    before_bytes = path.read_bytes()
    before_names = {entry.name for entry in home.iterdir()}
    conn = storage.connect_read_only()
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
            "value"
        ] == str(storage.SCHEMA_VERSION)
        assert conn.total_changes == 0
        assert all(sidecar.is_file() for sidecar in sidecars)
        assert {entry.name for entry in home.iterdir()} - before_names == {
            sidecar.name for sidecar in sidecars
        }
        assert path.read_bytes() == before_bytes
    finally:
        conn.close()
    assert path.read_bytes() == before_bytes


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses fixture directory/file permissions")
def test_nonwritable_wal_store_without_sidecars_reports_unavailable_without_mutation(
    tmp_path, monkeypatch
):
    home, path, sidecars = _checkpointed_wal_store(tmp_path, monkeypatch)
    before_bytes = path.read_bytes()
    before_names = {entry.name for entry in home.iterdir()}
    home.chmod(0o500)
    path.chmod(0o400)
    try:
        with pytest.raises(
            storage.LiveStoreError, match="metadata store unavailable"
        ) as unavailable:
            storage.connect_read_only()
        assert isinstance(unavailable.value.__cause__, sqlite3.OperationalError)
        rows = quota_observe.build_quota_status()["quotas"]
        assert rows and all(row["availability"] == "unknown" for row in rows)
        assert all(
            row["metadata_error"] == "quota observation metadata unavailable" for row in rows
        )
        with pytest.raises(ToolError, match="credential metadata unavailable"):
            credential_pool.credential_pool_list()
        assert all(not sidecar.exists() for sidecar in sidecars)
        assert {entry.name for entry in home.iterdir()} == before_names
        assert path.read_bytes() == before_bytes
        assert home.stat().st_mode & 0o777 == 0o500
        assert path.stat().st_mode & 0o777 == 0o400
    finally:
        home.chmod(0o700)
        path.chmod(0o600)


def test_read_only_connection_observes_live_writer_committed_uncheckpointed_wal(
    tmp_path, monkeypatch
):
    _home, path, sidecars = _checkpointed_wal_store(tmp_path, monkeypatch)
    before_bytes = path.read_bytes()
    writer = storage.connect()
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO meta(key, value) VALUES ('live-wal-fixture', 'committed')")
        writer.commit()
        assert sidecars[0].stat().st_size > 0
        assert path.read_bytes() == before_bytes
        reader = storage.connect_read_only()
        try:
            assert (
                reader.execute("SELECT value FROM meta WHERE key='live-wal-fixture'").fetchone()[
                    "value"
                ]
                == "committed"
            )
            assert reader.total_changes == 0
            assert path.read_bytes() == before_bytes
        finally:
            reader.close()
    finally:
        writer.close()
