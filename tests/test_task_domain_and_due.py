"""Portfolio-domain classification (task #373 Tier 1, 2.3) and the light
commitment/reminder due-date field (2.2) added to tasks."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools.tasks import DOMAINS, task_add, task_list, task_update


def test_task_add_defaults_to_hobby_for_every_current_project():
    for slug in ("unified-agent", "browser", "sample-store", "client-portal"):
        t = task_add(f"Hobby default for {slug}", project=slug)
        row = task_list(status="all", fields="full")["tasks"]
        row = next(r for r in row if r["id"] == t["id"])
        assert row["domain"] == "hobby"


def test_task_add_explicit_domain_overrides_project_default():
    t = task_add("Personal task in unified-agent project", domain="personal")
    row = task_list(status="all", domain="personal")["tasks"]
    assert any(r["id"] == t["id"] for r in row)


def test_task_add_rejects_unknown_domain():
    with pytest.raises(ToolError, match="domain must be one of"):
        task_add("Bad domain task", domain="not-a-real-domain")


def test_task_add_subtask_inherits_parent_domain():
    parent = task_add("Personal parent", domain="personal")
    child = task_add("Personal child", parent_id=parent["id"])
    rows = task_list(status="all", fields="full")["tasks"]
    child_row = next(r for r in rows if r["id"] == child["id"])
    assert child_row["domain"] == "personal"


def test_task_add_subtask_explicit_domain_overrides_parent():
    parent = task_add("Hobby parent")
    child = task_add("Professional child", parent_id=parent["id"], domain="professional")
    rows = task_list(status="all", fields="full")["tasks"]
    child_row = next(r for r in rows if r["id"] == child["id"])
    assert child_row["domain"] == "professional"


def test_task_list_domain_filter_scopes_results():
    task_add("Hobby only")
    task_add("Personal only", domain="personal")
    personal_tasks = task_list(status="all", domain="personal")["tasks"]
    assert all(t["domain"] == "personal" for t in personal_tasks)


def test_task_list_rejects_unknown_domain_filter():
    with pytest.raises(ToolError, match="domain must be one of"):
        task_list(domain="not-a-real-domain")


def test_task_update_reassigns_domain():
    t = task_add("Reclassify me")
    task_update(id=t["id"], domain="professional")
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["domain"] == "professional"


def test_all_domain_slugs_accepted():
    for slug in DOMAINS:
        t = task_add(f"Domain check {slug}", domain=slug)
        row = task_list(status="all", fields="full")["tasks"]
        row = next(r for r in row if r["id"] == t["id"])
        assert row["domain"] == slug


def test_migration_backfills_legacy_rows_as_hobby(tmp_path, monkeypatch):
    """A DB created before task #373 (no `domain` column) backfills to
    'hobby' on the next init_db(), not NULL — every pre-existing project was
    a Hobby Builder project (task #339)."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db_path = tmp_path / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT, "
            "status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'med', "
            "parent_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "completed_at TEXT, archived_at TEXT, source_host TEXT, claimed_by TEXT, "
            "claimed_at TEXT, lock_expires_at TEXT, untrusted INTEGER NOT NULL DEFAULT 0, "
            "worktree_path TEXT, branch TEXT, project TEXT NOT NULL DEFAULT 'unified-agent')"
        )
        conn.execute(
            "INSERT INTO tasks (title, created_at, updated_at) VALUES "
            "('pre-existing task', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT domain FROM tasks WHERE title='pre-existing task'").fetchone()
    finally:
        conn.close()
    assert row[0] == "hobby"


# --- due_at (light commitment/reminder field, 2.2) ---


def test_task_add_due_at_defaults_to_none():
    t = task_add("No due date")
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["due_at"] is None


def test_task_add_with_due_at():
    t = task_add("Send the redlines", notes="[owed-to: Priya]", due_at="2026-08-12")
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["due_at"] == "2026-08-12"


def test_task_list_due_before_finds_due_and_overdue_only():
    due_soon = task_add("Due 08-12", due_at="2026-08-12")
    due_later = task_add("Due 08-20", due_at="2026-08-20")
    no_due = task_add("No due date")

    results = task_list(status="all", due_before="2026-08-15")["tasks"]
    ids = {t["id"] for t in results}
    assert due_soon["id"] in ids
    assert due_later["id"] not in ids
    assert no_due["id"] not in ids


def test_task_update_sets_due_at():
    t = task_add("Set due date later")
    task_update(id=t["id"], due_at="2026-09-01")
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["due_at"] == "2026-09-01"


def test_task_update_clear_due_at():
    t = task_add("Fulfilled commitment", due_at="2026-08-12")
    task_update(id=t["id"], clear_due_at=True)
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["due_at"] is None


def test_task_update_rejects_due_at_and_clear_due_at_together():
    t = task_add("Ambiguous update")
    with pytest.raises(ToolError, match="pass either due_at or clear_due_at"):
        task_update(id=t["id"], due_at="2026-09-01", clear_due_at=True)


def test_migration_backfills_legacy_rows_due_at_as_null(tmp_path, monkeypatch):
    """A DB created before task #373 (no `due_at` column) backfills to NULL —
    unlike `domain`, there is no safe non-null default for an unset due date."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    db_path = tmp_path / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT, "
            "status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'med', "
            "parent_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "completed_at TEXT, archived_at TEXT, source_host TEXT, claimed_by TEXT, "
            "claimed_at TEXT, lock_expires_at TEXT, untrusted INTEGER NOT NULL DEFAULT 0, "
            "worktree_path TEXT, branch TEXT, project TEXT NOT NULL DEFAULT 'unified-agent')"
        )
        conn.execute(
            "INSERT INTO tasks (title, created_at, updated_at) VALUES "
            "('pre-existing task', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    storage.init_db()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT due_at FROM tasks WHERE title='pre-existing task'").fetchone()
    finally:
        conn.close()
    assert row[0] is None
