"""The per-process template store each test's home starts from (task #2238)."""

import re
import sqlite3
import stat

from store_template import fingerprint, seed_store

from unified_agent import config, constraint_migration, storage


def _mask_clock(row):
    """Hide the clock stamps a fresh init writes into ``meta``, and nothing else.

    A seeded store keeps the template's time, so these two values differ
    whenever two inits straddle a second. The rest of each row still compares.
    """
    if "'message_index_backfill'" in row:
        return "backfill-time"
    if repr(constraint_migration.MARKER_KEY) in row:
        return re.sub(r'"at": "[^"]*"', '"at": "rebuild-time"', row)
    return row


def _state(db):
    """Every schema object's definition and every row of every plain table."""
    with sqlite3.connect(db) as conn:
        schema = sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        rows = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        ):
            rows[name] = sorted(map(repr, conn.execute(f'SELECT * FROM "{name}"')))
            if name == "meta":
                rows[name] = [_mask_clock(r) for r in rows[name]]
    return schema, rows


def test_each_test_gets_its_own_initialized_store(isolated_env):
    db = config.db_path()
    assert db.is_file()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    with sqlite3.connect(db) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version == (str(storage.SCHEMA_VERSION),)


def test_a_seeded_store_matches_a_fresh_one(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_TEST_FRESH_STORE", raising=False)
    templates = tmp_path / "templates"
    templates.mkdir()
    stores = []
    for name in ("first", "second", "fresh"):
        monkeypatch.setenv("UA_HOME", str(tmp_path / name))
        if name == "fresh":
            monkeypatch.setenv("UA_TEST_FRESH_STORE", "1")
        seed_store(templates, tmp_path, config.db_path(), storage.init_db)
        stores.append(config.db_path())
    assert len(list(templates.glob("*.db"))) == 1
    first, second, fresh = stores
    assert _state(second) == _state(fresh) == _state(first)
    assert stat.S_IMODE(second.stat().st_mode) == 0o600


def test_only_the_clock_stamps_are_masked():
    """Stores one second apart compare equal; any other marker field still differs."""
    key = constraint_migration.MARKER_KEY
    row = "(%r, %r)"
    earlier = row % (key, '{"at": "2026-11-05T16:24:39+00:00", "rebuilt": []}')
    later = row % (key, '{"at": "2026-11-05T16:24:40+00:00", "rebuilt": []}')
    other = row % (key, '{"at": "2026-11-05T16:24:40+00:00", "rebuilt": ["tasks"]}')
    assert _mask_clock(earlier) == _mask_clock(later)
    assert _mask_clock(later) != _mask_clock(other)
    assert _mask_clock("('schema_version', '9')") == "('schema_version', '9')"


def test_writes_to_a_seeded_store_never_reach_the_template(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_TEST_FRESH_STORE", raising=False)
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("first", "second"):
        monkeypatch.setenv("UA_HOME", str(tmp_path / name))
        seed_store(templates, tmp_path, config.db_path(), storage.init_db)
    (template,) = templates.glob("*.db")
    before = template.read_bytes()
    with sqlite3.connect(config.db_path()) as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('probe', 'x')")
    assert template.read_bytes() == before


def test_the_template_key_ignores_per_test_paths_but_not_settings(tmp_path):
    base = {"UA_REDACTION_STRICT": "1", "PATH": "/bin"}
    a = fingerprint(tmp_path, {**base, "UA_HOME": str(tmp_path / "h")})
    b = fingerprint(tmp_path, {**base, "UA_HOME": str(tmp_path / "other")})
    assert a == b
    assert fingerprint(tmp_path, {**base, "UA_EMBED_PROVIDER": "none"}) != a
    assert fingerprint(tmp_path, {**base, "PATH": "/usr/bin"}) == fingerprint(tmp_path, base)
