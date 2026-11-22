"""Storage migration version guard (task #373 Tier 3, 4.6).

storage.py::_migrate is additive-only and had no way to detect a DB ahead
of the code it's running under — a real hazard because the CHECK-constraint
table-rebuild blocks (tasks/approvals/delegations/autonomous_cycles) copy a
hardcoded column list; run by older code against a newer DB, a rebuild
would silently drop a column it doesn't recognize rather than error.
"""

import sqlite3

import pytest

from unified_agent import storage


def test_fresh_db_has_no_guard_to_trip():
    """A brand-new DB (no meta table at all) must not be treated as a
    version mismatch — that's the normal first-run case."""
    storage.init_db()  # must not raise
    conn = storage.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        conn.close()
    assert int(row["value"]) == storage.SCHEMA_VERSION


def test_current_version_db_reinitializes_cleanly():
    """The normal steady-state case: init_db() called again against a DB
    already at the current version (every process restart) must not raise."""
    storage.init_db()
    storage.init_db()  # must not raise the second time either


def test_older_db_migrates_forward_without_raising():
    """A DB stamped with a version older than the code's is exactly what
    _migrate() exists to handle — the guard must not block this."""
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(storage.SCHEMA_VERSION - 1),),
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db()  # must not raise

    conn = storage.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        conn.close()
    assert int(row["value"]) == storage.SCHEMA_VERSION


def test_newer_db_refuses_to_start():
    """The actual guard: a DB stamped with a version AHEAD of this code's
    SCHEMA_VERSION must refuse to run, not silently proceed."""
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(storage.SCHEMA_VERSION + 1),),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(storage.SchemaVersionError, match="newer than this code understands"):
        storage.init_db()


def test_newer_db_refusal_happens_before_any_schema_mutation():
    """Guard fires before conn.executescript(_SCHEMA) — verified by asserting
    the connection passed to the guard sees the pre-existing meta row exactly
    as the (not-yet-touched-this-call) DB already had it, not a value any
    part of this init_db() call could have produced."""
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(storage.SCHEMA_VERSION + 5),),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(storage.SchemaVersionError):
        storage.init_db()

    # The stored version must be untouched by the refused call — proves
    # nothing wrote to the DB before the guard raised.
    conn = storage.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        conn.close()
    assert int(row["value"]) == storage.SCHEMA_VERSION + 5


def test_guard_ignores_unparseable_stored_version():
    """A corrupt/non-integer stored value is not this guard's job to
    diagnose -- it must not crash startup over it."""
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute("UPDATE meta SET value='not-a-number' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()

    storage.init_db()  # must not raise


def test_guard_handles_missing_meta_table_directly():
    """Unit-level check of _guard_schema_version itself against a connection
    to a database with no tables at all (not even via init_db once)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        storage._guard_schema_version(conn)  # must not raise
    finally:
        conn.close()
