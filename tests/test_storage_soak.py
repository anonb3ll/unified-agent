"""Fleet soak test: 25 concurrent daemons hitting SQLite memory.db (task #783).

Validates WAL-mode concurrency under simulated fleet scale:
- 25 concurrent worker threads executing interleaved tasks, events, and reads
- Online background backup running concurrently with active writes
- Asserts 0 locked errors and 100% data integrity
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from pathlib import Path

import pytest

from unified_agent import config, events, storage, storage_backup


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "backup_dir", lambda: backup_dir)
    storage.init_db()
    yield


_MAX_DIAGNOSTICS = 3


def _error_diagnostic(exc: Exception, daemon_idx: int, iteration: int, phase: str) -> dict:
    """Keep bounded, non-sensitive worker failure context for soak diagnosis."""
    diagnostic = {
        "daemon": daemon_idx,
        "iteration": iteration,
        "phase": phase,
        "exception": type(exc).__name__,
    }
    if isinstance(exc, sqlite3.Error):
        diagnostic["sqlite_errorcode"] = getattr(exc, "sqlite_errorcode", None)
        diagnostic["sqlite_errorname"] = getattr(exc, "sqlite_errorname", None)
    return diagnostic


def _diagnostic_summary(results: list[dict]) -> str:
    return repr([diagnostic for result in results for diagnostic in result["diagnostics"]])


def _daemon_worker(daemon_idx: int, iterations: int = 10) -> dict:
    """Simulate a single daemon executing tasks, notes, and events."""
    agent_label = f"daemon_{daemon_idx}"
    errors = 0
    ops = 0
    diagnostics = []

    for i in range(iterations):
        task_id = daemon_idx * 1000 + i
        phase = "connect"
        try:
            conn = storage.connect()
            try:
                # 1. Create task
                phase = "insert"
                conn.execute(
                    "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, claimed_by, project, domain, lane) "
                    "VALUES (?, ?, 'doing', 'med', ?, ?, ?, 'unified-agent', 'hobby', 'default')",
                    (task_id, f"Work item {task_id}", storage.now(), storage.now(), agent_label),
                )
                conn.commit()
                ops += 1

                # 2. Update task
                phase = "update"
                conn.execute(
                    "UPDATE tasks SET status='done', completed_at=?, notes='completed by daemon' WHERE id=?",
                    (storage.now(), task_id),
                )
                conn.commit()
                ops += 1

                # 3. Read query
                phase = "read"
                row = conn.execute(
                    "SELECT status, claimed_by FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                assert row is not None and row["status"] == "done"
                ops += 1
            finally:
                conn.close()

            # 4. Record event
            phase = "event"
            events.record_event(
                "agent.task.completed",
                payload={"task_id": task_id, "daemon": daemon_idx},
                originator=agent_label,
            )
            ops += 1

        except Exception as exc:
            errors += 1
            if len(diagnostics) < _MAX_DIAGNOSTICS:
                diagnostics.append(_error_diagnostic(exc, daemon_idx, i, phase))

        time.sleep(0.005)

    return {"ops": ops, "errors": errors, "diagnostics": diagnostics}


def test_fleet_scale_soak_25_daemons(tmp_path):
    """Simulate fleet scale of 25 active daemons writing concurrently."""
    num_daemons = 25
    iterations_per_daemon = 8

    start_time = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_daemons + 1) as executor:
        # Launch 25 worker daemons
        futures = [
            executor.submit(_daemon_worker, idx, iterations_per_daemon)
            for idx in range(num_daemons)
        ]

        # Launch concurrent online backup mid-soak
        time.sleep(0.02)
        backup_future = executor.submit(storage_backup.backup_database)

        results = [f.result(timeout=30) for f in futures]
        backup_res = backup_future.result(timeout=30)

    elapsed = time.monotonic() - start_time

    total_ops = sum(r["ops"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    diagnostics = _diagnostic_summary(results)

    # 1. Zero lock/write errors across all 25 daemons
    assert total_errors == 0, (
        f"Encountered {total_errors} errors across {num_daemons} daemons; diagnostics={diagnostics}"
    )
    assert total_ops == num_daemons * iterations_per_daemon * 4

    # 2. Online backup taken during active concurrent writes is valid
    assert backup_res["status"] == "success"
    assert backup_res["integrity"] == "ok"
    assert Path(backup_res["backup_path"]).exists()

    # 3. Live database passes complete integrity verification
    live_check = storage_backup.verify_database_integrity()
    assert live_check["valid"] is True
    assert live_check["status"] == "ok"

    # 4. Hash chain of event log is unbroken and verified
    chain_report = events.verify_event_chain()
    assert chain_report["status"] == "valid"
    assert chain_report["verified"] is True
    assert chain_report["total_verified"] == num_daemons * iterations_per_daemon

    # Metrics summary
    ops_per_sec = total_ops / elapsed
    print(
        f"\n[SOAK REPORT] 25 daemons: {total_ops} ops in {elapsed:.2f}s ({ops_per_sec:.1f} ops/s), 0 errors, backup verified"
    )


def test_worker_retains_bounded_diagnostic_and_cleans_up(monkeypatch):
    original = events.record_event

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(events, "record_event", original)
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(events, "record_event", fail_once)
    result = _daemon_worker(7, iterations=1)

    assert result["errors"] == 1
    assert result["diagnostics"] == [
        {
            "daemon": 7,
            "iteration": 0,
            "phase": "event",
            "exception": "OperationalError",
            "sqlite_errorcode": None,
            "sqlite_errorname": None,
        }
    ]
    # A subsequent connection remains usable after the injected event failure.
    with storage.connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    summary = _diagnostic_summary([result])
    assert "event" in summary
    assert "OperationalError" in summary
