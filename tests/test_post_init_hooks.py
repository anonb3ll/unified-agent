"""storage.init_db() post-init hooks (task #1720).

init_db() is schema only; row migrations and backfills owned by modules above
core run as registered post-init hooks. These tests pin the registry contract,
the default hook order, and that the schema does not depend on the hooks.
"""

from __future__ import annotations

import sqlite3

import pytest

from unified_agent import config, post_init, storage, task_search
from unified_agent.tools.tasks import task_add

EXPECTED_ORDER = [
    ("autonomy_modes.legacy_grants", False),
    ("task_search.task_fts", False),
    ("run_records.open_run_cache", False),
    ("tools.memory.graph_backfill", False),
    ("memory_semantic.embeddings", True),
    ("memory_chunks.backfill", True),
    ("task_search.embeddings", True),
    ("tools.indexer.symbol_embeddings", True),
    ("sync.startup_snapshot", True),
    ("completion_return.startup_drain", True),
    ("approval_slack.startup_drain", True),
]


@pytest.fixture
def empty_registry(monkeypatch):
    registry: dict = {}
    monkeypatch.setattr(storage, "_post_init_hooks", registry)
    return registry


def _master(path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def test_default_hooks_register_in_the_order_init_db_used_to_run_them(empty_registry):
    post_init.register_default_hooks()
    post_init.register_default_hooks()  # idempotent: keyed by name
    hooks = storage.post_init_hooks()
    assert [(h.name, h.background) for h in hooks] == EXPECTED_ORDER


def test_post_init_init_db_registers_the_hooks_before_initializing(empty_registry, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        storage, "init_db", lambda: seen.append([h.name for h in storage.post_init_hooks()])
    )
    post_init.init_db()
    assert seen == [[name for name, _ in EXPECTED_ORDER]]


def test_schema_is_identical_with_and_without_hooks(tmp_path, monkeypatch, empty_registry):
    """The DDL stays in core: a process that registered no hooks still gets
    every table, column, index and trigger the hub has."""
    monkeypatch.setenv("UA_HOME", str(tmp_path / "bare"))
    storage.init_db()
    bare = _master(config.db_path())

    post_init.register_default_hooks()
    monkeypatch.setenv("UA_HOME", str(tmp_path / "hooked"))
    storage.init_db()
    storage.join_init_background_threads()
    hooked = _master(config.db_path())

    assert bare == hooked
    names = {row[1] for row in bare}
    for expected in (
        "autonomy_standing_grants",
        "uq_autonomy_pending_actions_open",
        "autonomy_pending_context_immutable",
        "task_fts",
        "task_fts_ai",
        "task_embeddings",
        "skill_versions",
        "skill_versions_immutable_update",
        "run_usage_heartbeats",
    ):
        assert expected in names


def test_foreground_hooks_run_in_order_on_one_committed_connection(empty_registry):
    calls: list[str] = []

    def second(conn):
        calls.append("second")
        assert conn.execute("SELECT value FROM meta WHERE key='probe_1720'").fetchone()

    def first(conn):
        calls.append("first")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe_1720', 'x')")

    storage.register_post_init_hook("b", second, order=20)
    storage.register_post_init_hook("a", first, order=10)
    storage.init_db()
    assert calls == ["first", "second"]
    conn = storage.connect()
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='probe_1720'").fetchone()
    finally:
        conn.close()


def test_a_failing_foreground_hook_rolls_back_and_propagates(empty_registry):
    background: list[str] = []

    def failing(conn):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe_1720_fail', 'x')")
        raise RuntimeError("hook failed")

    storage.register_post_init_hook("fails", failing, order=10)
    storage.register_post_init_hook(
        "never", lambda: background.append("ran"), order=100, background=True
    )
    with pytest.raises(RuntimeError, match="hook failed"):
        storage.init_db()
    conn = storage.connect()
    try:
        assert conn.execute("SELECT 1 FROM meta WHERE key='probe_1720_fail'").fetchone() is None
        # The schema committed before the hooks ran.
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        conn.close()
    assert background == []


def test_background_hooks_run_on_tracked_threads(empty_registry):
    ran: list[str] = []
    storage.register_post_init_hook("bg", lambda: ran.append("bg"), order=1, background=True)
    storage.init_db()
    storage.join_init_background_threads()
    assert ran == ["bg"]
    assert [t.name for t in storage._init_background_threads] == ["ua-post-init-bg"]


def test_unregistered_hook_no_longer_runs(empty_registry):
    ran: list[str] = []
    storage.register_post_init_hook("gone", lambda conn: ran.append("x"), order=1)
    storage.unregister_post_init_hook("gone")
    storage.init_db()
    assert ran == []


def test_default_hooks_resolve_their_target_when_they_run(empty_registry, monkeypatch):
    """A patch applied after registration still takes effect, as it did when
    init_db looked task_search.backfill_all_task_embeddings up itself."""
    post_init.register_default_hooks()
    called: list[str] = []
    monkeypatch.setattr(task_search, "backfill_all_task_embeddings", lambda: called.append("x"))
    storage.init_db()
    storage.join_init_background_threads()
    assert called == ["x"]


def test_task_fts_backfill_waits_for_its_hook(empty_registry):
    """Without the hook, init_db leaves a task missing from task_fts alone;
    the registered hook indexes it."""
    task_id = task_add("legacy task predating task_fts")["id"]
    conn = storage.connect()
    try:
        row = conn.execute("SELECT id, title, notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.execute(
            "INSERT INTO task_fts(task_fts, rowid, title, notes) VALUES ('delete', ?, ?, ?)",
            (row["id"], row["title"], row["notes"]),
        )
        conn.commit()
    finally:
        conn.close()

    def indexed() -> bool:
        c = storage.connect()
        try:
            return (
                c.execute("SELECT 1 FROM task_fts_docsize WHERE id=?", (row["id"],)).fetchone()
                is not None
            )
        finally:
            c.close()

    storage.init_db()
    assert not indexed()
    post_init.register_default_hooks()
    storage.init_db()
    assert indexed()


def test_repeated_init_db_does_not_stack_background_threads(empty_registry, tmp_path, monkeypatch):
    """A background hook still running is reused, not started again.

    system_doctor calls init_db once per connector, and a polled caller used to
    start a new backfill thread per hook per call. Each held a connection while
    it waited on the serialized embedding model, so threads and memory.db
    handles stacked up until the hub ran out of file descriptors.

    Calls run_post_init_hooks (the tail of init_db) directly: the conftest
    wraps init_db to join its background threads, which would serialize the
    calls this test needs to overlap.
    """
    import threading

    release = threading.Event()
    started = threading.Semaphore(0)
    starts: list[str] = []

    def slow_backfill():
        starts.append(str(config.db_path()))
        started.release()
        release.wait(timeout=30)

    storage.register_post_init_hook("slow", slow_backfill, order=1, background=True)
    try:
        for _ in range(5):
            storage.run_post_init_hooks()
        assert started.acquire(timeout=10)
        assert not started.acquire(timeout=0.2)
        alive = [t for t in threading.enumerate() if t.name == "ua-post-init-slow"]
        assert len(starts) == 1
        assert len(alive) == 1
        assert storage._init_background_threads == alive

        # A different database is different work: it still gets its own run.
        monkeypatch.setenv("UA_HOME", str(tmp_path / "other"))
        storage.run_post_init_hooks()
        assert started.acquire(timeout=10)
        assert len(starts) == 2
    finally:
        release.set()
        storage.join_init_background_threads()

    # Once the earlier run finished, the next call starts a fresh one.
    storage.run_post_init_hooks()
    storage.join_init_background_threads()
    assert len(starts) == 3


def test_join_waits_for_hooks_of_an_earlier_database(tmp_path, monkeypatch):
    """PR #1001 review: a hook still running for database A must be joined even
    after init_db ran for database B, before the caller changes environment."""
    import threading

    from unified_agent import storage

    release = threading.Event()
    done = threading.Event()

    def slow():
        release.wait(5)
        done.set()

    t = threading.Thread(target=slow, name="ua-post-init-test-a", daemon=True)
    with storage._background_hook_lock:
        storage._background_hook_threads[("test-a", str(tmp_path / "a.db"))] = t
    t.start()
    monkeypatch.setattr(storage, "_init_background_threads", [])
    threading.Timer(0.2, release.set).start()
    storage.join_init_background_threads(timeout=5)
    assert done.is_set()


def test_finished_hooks_are_pruned_from_the_registry(tmp_path):
    import threading

    from unified_agent import storage

    t = threading.Thread(target=lambda: None, name="ua-post-init-test-b", daemon=True)
    t.start()
    t.join()
    key = ("test-b", str(tmp_path / "b.db"))
    with storage._background_hook_lock:
        storage._background_hook_threads[key] = t
    storage.run_post_init_hooks()
    assert key not in storage._background_hook_threads
