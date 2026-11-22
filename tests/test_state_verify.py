"""Tests for state_sync_verify tool."""

import pytest

from unified_agent import config, storage, sync
from unified_agent.tools.state_verify import state_sync_verify


@pytest.fixture(autouse=True)
def state_sync_on(monkeypatch):
    """Opt this module back in: conftest defaults UA_STATE_SYNC off (#201).

    Drift detection compares the DB against the vault mirrors, so it needs the
    writes those mirrors come from.
    """
    monkeypatch.setenv("UA_STATE_SYNC", "1")


def _setup_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "Logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "vault_root", lambda: vault_dir)
    return vault_dir


def test_state_sync_verify_clean(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch)

    # Perform initial sync
    sync.sync_state_now()

    res = state_sync_verify()
    assert res["in_sync"] is True
    assert res["resynced"] is False
    assert res["drift_details"]["memory_drift"] is False
    assert res["drift_details"]["task_drift"] is False


def test_state_sync_verify_detects_drift_and_resyncs(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch)

    # Perform initial sync
    sync.sync_state_now()

    # Insert a new task into DB without syncing to vault
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, notes, status, priority, created_at, updated_at) "
            "VALUES (999, 'Unsynced Task', 'notes', 'open', 'high', '2026-07-24T00:00:00', '2026-07-24T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    # Verify detects drift and resyncs
    res = state_sync_verify()
    assert res["in_sync"] is False
    assert res["resynced"] is True
    assert res["drift_details"]["task_drift"] is True

    # Subsequent call is in sync
    res_after = state_sync_verify()
    assert res_after["in_sync"] is True
    assert res_after["resynced"] is False
