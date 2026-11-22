"""WAL mode + concurrency-safety proof for memory.db (task #964, child of #963).

storage.connect() (src/unified_agent/storage.py) already sets
``PRAGMA journal_mode=WAL`` and ``PRAGMA busy_timeout=5000`` on every
connection it opens (storage.py:2095-2096) — there is exactly one function in
the codebase that opens a read-write connection to the live memory.db, so
every real write/read path gets WAL for free. Nothing here duplicates
test_storage_soak.py's 25-daemon fleet soak (task #783); these tests instead
pin two things that soak test does not:

1. A regression pin on the exact PRAGMA values storage.connect() sets, so a
   future refactor that drops or weakens them fails a fast, targeted test
   instead of only showing up as flaky lock errors under load.
2. WAL's actual mechanism claim — a reader must see a consistent snapshot and
   must NOT block behind an in-flight writer transaction — proven with a real
   writer holding a real open transaction against a real temp file while a
   real reader thread concurrently queries it.
3. The literal failure mode task #964 references (the "GBrain PGLite
   single-writer lesson"): PGLite has no cross-process file locking, so
   multiple OS processes hitting one instance can corrupt state without an
   application-level writer queue. SQLite's file locking is designed for
   exactly this. test_concurrent_writer_processes_no_corruption proves it
   holds across real OS *processes* (multiprocessing), not just threads in
   one interpreter, using storage.connect() completely unmocked.

Conclusion documented here rather than in a task-notes-only claim: no
explicit application-level writer lock/queue exists anywhere in storage.py
(grep confirms the only serialization is SQLite's own file locking plus
``busy_timeout=5000``), and these tests demonstrate that is sufficient for
this workload — an app-level writer arbitration layer would be solving a
problem SQLite's VFS layer already solves for a single local file, and would
only be justified if the proof below showed lock-timeout errors or
corruption under load, which it does not.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time

from unified_agent import storage


def _mp_writer_worker(idx: int, iterations: int) -> dict:
    """Top-level (picklable) worker for the cross-process proof.

    Runs in its own spawned OS process with its own Python interpreter and
    its own sqlite3 connections; inherits UA_HOME from the parent's
    environment (multiprocessing's spawn context copies os.environ at spawn
    time), so config.db_path() resolves to the same real temp file as the
    parent without any shared Python state.
    """
    from unified_agent import storage as _storage

    errors = 0
    for i in range(iterations):
        try:
            conn = _storage.connect()
            try:
                conn.execute(
                    "INSERT INTO tasks (title, created_at, updated_at) VALUES (?, ?, ?)",
                    (f"mp-{idx}-{i}", _storage.now(), _storage.now()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            errors += 1
    return {"idx": idx, "errors": errors}


def test_connect_enables_wal_and_busy_timeout(isolated_env):
    """Regression pin for storage.py:2095-2096. If a future edit drops WAL or
    shrinks busy_timeout, this fails immediately instead of surfacing only as
    intermittent 'database is locked' errors under concurrent load."""
    conn = storage.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"
    assert timeout == 5000


def test_every_fresh_connection_gets_wal_independently(isolated_env):
    """storage.connect() caches nothing (no module-level connection object
    exists in storage.py despite several test modules resetting a
    ``storage._connection`` attribute that storage.py never defines — that
    reset is a no-op today). Every call opens a brand-new sqlite3.Connection,
    so WAL must be — and is — re-asserted on each one rather than relied on
    from a shared/cached connection."""
    for _ in range(5):
        conn = storage.connect()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()


def test_reader_does_not_block_behind_open_writer_transaction(isolated_env):
    """Core WAL guarantee under test: a writer holding an open transaction
    must not block a concurrent reader, and the reader must not observe the
    writer's uncommitted row (no torn/partial reads)."""
    ready = threading.Event()
    release = threading.Event()
    writer_committed = threading.Event()
    writer_errors: list[BaseException] = []

    def writer() -> None:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO tasks (title, created_at, updated_at) VALUES (?, ?, ?)",
                ("wal-writer", storage.now(), storage.now()),
            )
            ready.set()
            release.wait(timeout=5)
            conn.commit()
            writer_committed.set()
        except BaseException as exc:  # pragma: no cover - surfaced via assertion
            writer_errors.append(exc)
        finally:
            conn.close()

    reader_saw_uncommitted_row = False
    reader_elapsed = None
    reader_errors: list[BaseException] = []

    def reader() -> None:
        nonlocal reader_saw_uncommitted_row, reader_elapsed
        try:
            assert ready.wait(timeout=5), "writer never reached its open transaction"
            conn = storage.connect()
            try:
                start = time.monotonic()
                row = conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE title='wal-writer'"
                ).fetchone()
                reader_elapsed = time.monotonic() - start
                if row["c"] > 0:
                    reader_saw_uncommitted_row = True
            finally:
                conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced via assertion
            reader_errors.append(exc)

    w = threading.Thread(target=writer)
    w.start()
    assert ready.wait(timeout=5), "writer never signalled readiness"
    r = threading.Thread(target=reader)
    r.start()
    r.join(timeout=5)
    release.set()
    w.join(timeout=5)

    assert not writer_errors, f"writer thread raised: {writer_errors}"
    assert not reader_errors, f"reader thread raised: {reader_errors}"
    assert writer_committed.is_set()
    # WAL isolation: the reader started while the writer's transaction was
    # still open and must not have seen the uncommitted insert.
    assert reader_saw_uncommitted_row is False
    # WAL non-blocking guarantee: the reader must not have waited for the
    # writer's commit (which was gated on `release`, held for the duration of
    # r.join(timeout=5)) — it must return near-instantly regardless.
    assert reader_elapsed is not None and reader_elapsed < 1.0, (
        f"reader blocked for {reader_elapsed}s behind an open writer transaction"
    )

    conn = storage.connect()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='wal-writer'").fetchone()[
            "c"
        ]
    finally:
        conn.close()
    assert count == 1


def test_concurrent_writer_threads_no_corruption_real_file(isolated_env):
    """Many real threads hammering the same real sqlite file concurrently
    through storage.connect() — never mocked. Proves SQLite's own file
    locking plus busy_timeout=5000 is sufficient serialization on its own:
    zero errors, zero lost writes, full integrity, with no application-level
    writer lock/queue anywhere in the path."""
    num_workers = 40
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            conn = storage.connect()
            try:
                conn.execute(
                    "INSERT INTO tasks (title, created_at, updated_at) VALUES (?, ?, ?)",
                    (f"concurrent-{idx}", storage.now(), storage.now()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM tasks WHERE title=?", (f"concurrent-{idx}",)
                ).fetchone()
                assert row is not None
            finally:
                conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced via assertion
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"{len(errors)} errors under {num_workers}-way concurrency: {errors[:3]}"

    conn = storage.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE title LIKE 'concurrent-%'"
        ).fetchone()["c"]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert count == num_workers
    assert integrity == "ok"


def test_concurrent_writer_processes_no_corruption(isolated_env):
    """The literal GBrain PGLite failure mode task #964 cites: separate OS
    *processes* (not threads sharing one interpreter) writing to the same
    database file concurrently. PGLite has no cross-process file locking to
    prevent that from corrupting state; SQLite's file locking is built for
    exactly this. This spawns real Python processes, each with its own
    storage.connect() calls against the same real temp memory.db, and proves
    zero corruption / zero errors without any change to storage.py."""
    num_procs = 6
    iterations = 15
    db_home = os.environ.get("UA_HOME")
    assert db_home, "isolated_env fixture must set UA_HOME"

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=num_procs) as pool:
        results = pool.starmap(_mp_writer_worker, [(i, iterations) for i in range(num_procs)])

    total_errors = sum(r["errors"] for r in results)
    assert total_errors == 0, f"errors across processes: {results}"

    conn = storage.connect()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title LIKE 'mp-%'").fetchone()["c"]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert count == num_procs * iterations
    assert integrity == "ok"
