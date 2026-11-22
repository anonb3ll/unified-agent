"""Request-time store bootstrap runs at most once per process per database.

Horizon 2 and the credential pool used to call ``storage.init_db()`` on every
request: the whole schema script, migrations and the foreground post-init hooks,
inside a write transaction, each time. They now call ``storage.init_db_once()``,
which skips a store this process has already initialized (the hub's start-up
init counts) and still bootstraps a fresh one.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import credential_pool, storage
from unified_agent.tools import horizon2


@pytest.fixture
def init_calls(monkeypatch):
    calls: list[str] = []
    real = storage.init_db

    def _counting_init_db() -> None:
        calls.append("init")
        real()

    monkeypatch.setattr(storage, "init_db", _counting_init_db)
    return calls


def test_request_time_callers_reuse_the_initialized_store(init_calls):
    created = credential_pool.credential_pool_register("openai", "work")["credential"]
    credential_pool.credential_pool_register("google", "home")
    credential_pool.credential_pool_revoke(created["id"])
    assert horizon2._exists("srv-unknown") is False
    with pytest.raises(ToolError, match="unregistered"):
        horizon2._load("srv-unknown")

    assert init_calls == []


def test_a_fresh_data_home_still_bootstraps_once(init_calls, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "fresh_home"))

    first = credential_pool.credential_pool_register("openai", "work")["credential"]
    credential_pool.credential_pool_register("google", "home")

    assert init_calls == ["init"]
    listed = credential_pool.credential_pool_list()["credentials"]
    assert first["id"] in {row["id"] for row in listed}


def test_a_removed_store_is_bootstrapped_again(init_calls):
    credential_pool.credential_pool_register("openai", "work")
    storage.join_init_background_threads()
    db = storage.config.db_path()
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)

    credential_pool.credential_pool_register("google", "home")

    assert init_calls == ["init"]


def test_a_failed_post_init_hook_is_retried(isolated_env, monkeypatch):
    """PR #1002 review: a store whose foreground migration raised is not
    recorded as initialized, so the next init_db_once() runs init_db again."""
    from unified_agent import storage

    storage.forget_initialized()
    real = storage.run_post_init_hooks
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient migration failure")
        real()

    monkeypatch.setattr(storage, "run_post_init_hooks", flaky)
    with pytest.raises(RuntimeError):
        storage.init_db_once()
    storage.init_db_once()
    assert calls["n"] == 2
    storage.init_db_once()
    assert calls["n"] == 2


def test_restore_forgets_the_initialized_store(isolated_env, tmp_path):
    """PR #1002 review: a restore may bring back an older schema, so the next
    request-time init must migrate it again."""
    from unified_agent import config, storage, storage_backup

    storage.init_db_once()
    assert str(config.db_path()) in storage._initialized_db_paths
    backup = tmp_path / "backup.db"
    src = storage.connect()
    try:
        import sqlite3

        dest = sqlite3.connect(backup)
        src.backup(dest)
        dest.close()
    finally:
        src.close()
    storage_backup.restore_database(backup)
    assert str(config.db_path()) not in storage._initialized_db_paths


def test_restore_to_another_file_keeps_the_live_store_initialized(isolated_env, tmp_path):
    """PR #1002 review: only the restored file loses its initialized mark."""
    import sqlite3

    from unified_agent import config, storage, storage_backup

    storage.init_db_once()
    backup = tmp_path / "backup.db"
    src = storage.connect()
    try:
        dest = sqlite3.connect(backup)
        src.backup(dest)
        dest.close()
    finally:
        src.close()
    storage_backup.restore_database(backup, tmp_path / "recovery.db")
    assert str(config.db_path()) in storage._initialized_db_paths
