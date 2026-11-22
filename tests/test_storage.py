"""Tests for SQLite schema and migration indexes (Task #162)."""

import os
from pathlib import Path

import pytest

from unified_agent import config, storage

TOMBSTONE_DB_TEXT = "AUTHORITATIVE memory.db is on Mac hub only. Hub: http://100.64.0.10:8765/mcp\n"


@pytest.mark.parametrize(
    "payload",
    [
        TOMBSTONE_DB_TEXT.encode("utf-8"),
        b"\x00\x01not sqlite\x02\x03",
    ],
)
def test_connect_reports_non_sqlite_db_actionably(isolated_env, payload):
    db = Path(config.db_path())
    db.write_bytes(payload)

    with pytest.raises(storage.LiveStoreError, match="not a SQLite database|UA_HOME"):
        storage.connect()


# SQLite's unix VFS lock bytes (os_unix.c): readers hold a POSIX read lock on
# SHARED_SIZE bytes starting at SHARED_FIRST for as long as a WAL connection
# is open. Another process may treat the database as unused only when it can
# take a write lock over that range.
_SQLITE_SHARED_FIRST = 0x40000000 + 2
_SQLITE_SHARED_SIZE = 510

_PROBE_SHARED_RANGE = """
import fcntl, sys
with open(sys.argv[1], "r+b") as fh:
    try:
        fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB, int(sys.argv[3]), int(sys.argv[2]))
    except OSError:
        print("held")
    else:
        print("free")
"""


def _other_process_sees_shared_lock(db: Path) -> bool:
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE_SHARED_RANGE,
            str(db),
            str(_SQLITE_SHARED_FIRST),
            str(_SQLITE_SHARED_SIZE),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    assert out in {"held", "free"}, out
    return out == "held"


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory locks")
def test_connect_keeps_live_connections_sqlite_lock(isolated_env):
    """A second connect() must not drop the first connection's POSIX lock.

    Task #1637 (Install Gate, Depot, 2026-10-01): connect() used to read the
    16-byte header through a Python file object before opening SQLite.
    Closing that descriptor releases every POSIX lock this process holds on
    memory.db, including the SHARED lock of each live hub connection
    (SQLite's "How To Corrupt" section 2.2). A separate CLI process
    (`unified-agent-audit verify`) could then take EXCLUSIVE at close,
    conclude it was the last connection, checkpoint, and delete -wal/-shm
    under the running hub. The hub's next connect failed with "disk I/O
    error" at PRAGMA journal_mode=WAL and /api/evidence returned 500.
    """
    storage.init_db()
    db = Path(config.db_path())
    held = storage.connect()
    try:
        held.execute("SELECT count(*) FROM tasks").fetchall()
        assert _other_process_sees_shared_lock(db)

        storage.connect().close()

        assert _other_process_sees_shared_lock(db), (
            "connect() released the POSIX lock held by a live connection"
        )
    finally:
        held.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory locks")
def test_require_live_task_store_keeps_live_connections_sqlite_lock(isolated_env):
    storage.init_db()
    db = Path(config.db_path())
    held = storage.connect()
    try:
        held.execute("SELECT count(*) FROM tasks").fetchall()
        storage.require_live_task_store()
        assert _other_process_sees_shared_lock(db), (
            "require_live_task_store() released the POSIX lock held by a live connection"
        )
    finally:
        held.close()


def test_connect_can_create_missing_db(isolated_env):
    db = Path(config.db_path())
    db.unlink(missing_ok=True)

    conn = storage.connect()
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_skips_chmod_for_a_store_this_process_does_not_own(isolated_env, monkeypatch):
    storage.connect().close()

    def boom(*_args, **_kwargs):
        raise AssertionError("chmod must not run when the process does not own the path")

    monkeypatch.setattr(storage.os, "chmod", boom)
    monkeypatch.setattr(storage, "_process_owns", lambda _path: False)
    conn = storage.connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def test_connect_closes_when_an_owned_chmod_fails(isolated_env, monkeypatch):
    closed: list[int] = []
    real_connect = storage.sqlite3.connect

    class _TrackingConnection(storage.sqlite3.Connection):
        def close(self):
            closed.append(1)
            return super().close()

    def tracking(path, *args, **kwargs):
        kwargs["factory"] = _TrackingConnection
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", tracking)

    def boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(storage.os, "chmod", boom)
    with pytest.raises(storage.LiveStoreError, match="could not be restricted"):
        storage.connect()
    assert closed


def test_connect_refuses_world_writable_store_parent(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    monkeypatch.setenv("UA_HOME", str(shared))
    with pytest.raises(storage.LiveStoreError, match="group- or world-writable"):
        storage.connect()
    db = Path(config.db_path())
    assert shared.stat().st_mode & 0o7777 == 0o1777
    assert not db.exists()


def test_connect_refuses_group_shared_home(tmp_path, monkeypatch):
    shared = tmp_path / "ua-team"
    shared.mkdir()
    os.chmod(shared, 0o770)
    assert storage._shared_data_directory(shared) is True
    monkeypatch.setenv("UA_HOME", str(shared))
    with pytest.raises(storage.LiveStoreError, match="group- or world-writable") as raised:
        storage.connect()
    assert f"run: chmod 700 {shared}" in str(raised.value)
    db = Path(config.db_path())
    assert shared.stat().st_mode & 0o777 == 0o770
    assert not db.exists()


def test_connect_tightens_a_private_home(tmp_path, monkeypatch):
    home = tmp_path / "private"
    home.mkdir()
    os.chmod(home, 0o755)
    assert storage._shared_data_directory(home) is False
    monkeypatch.setenv("UA_HOME", str(home))
    conn = storage.connect()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS perm_probe (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    db = Path(config.db_path())
    assert home.stat().st_mode & 0o777 == 0o700
    assert db.stat().st_mode & 0o777 == 0o600


def test_connect_restricts_db_file_mode(isolated_env):
    conn = storage.connect()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS perm_probe (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    conn = storage.connect()
    conn.close()
    db = Path(config.db_path())
    assert db.stat().st_mode & 0o777 == 0o600
    assert db.parent.stat().st_mode & 0o777 == 0o700
    for suffix in ("-wal", "-shm"):
        sibling = Path(str(db) + suffix)
        if sibling.exists():
            assert sibling.stat().st_mode & 0o777 == 0o600


def test_sqlite_indexes_exist(isolated_env):
    """Verify all 5 missing performance indexes are present in the DB schema."""
    conn = storage.connect()
    try:
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }

        expected_indexes = {
            "idx_tasks_status_priority",
            "idx_tasks_parent",
            "idx_memory_updated",
            "idx_memory_superseded",
            "idx_agent_events_agent_kind_ts",
        }

        for idx in expected_indexes:
            assert idx in indexes, f"Missing expected SQLite index: {idx}"
    finally:
        conn.close()


def test_sqlite_migrate_creates_indexes(isolated_env):
    """Verify _migrate creates performance indexes on pre-existing database tables."""
    conn = storage.connect()
    try:
        storage._migrate(conn)
        conn.commit()

        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }

        expected_indexes = {
            "idx_tasks_status_priority",
            "idx_tasks_parent",
            "idx_memory_updated",
            "idx_memory_superseded",
            "idx_agent_events_agent_kind_ts",
        }

        for idx in expected_indexes:
            assert idx in indexes, f"Missing expected SQLite index after migration: {idx}"
    finally:
        conn.close()
