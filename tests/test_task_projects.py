import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools.tasks import PROJECTS, task_add, task_claim, task_list, task_update


def test_task_add_defaults_to_unified_agent():
    t = task_add("Untagged task")
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["project"] == "unified-agent"


def test_task_add_explicit_project():
    t = task_add("Browser task", project="browser")
    row = task_list(status="all", project="browser")["tasks"]
    assert any(r["id"] == t["id"] for r in row)


def test_task_add_rejects_unknown_project():
    with pytest.raises(ToolError, match="project must be one of"):
        task_add("Bad project task", project="not-a-real-project")


def test_task_add_rejects_markdown_heading_as_title():
    """Task #378: a markdown heading is never a real title — belt-and-suspenders
    guard alongside the delegate() fix, for any other call site that might
    pass template/spec text positionally into title."""
    with pytest.raises(ToolError, match="markdown heading"):
        task_add("### Objective")


def test_task_add_rejects_multiline_title():
    with pytest.raises(ToolError, match="markdown heading"):
        task_add("Objective\nSome body text on a second line")


def test_task_add_subtask_inherits_parent_project():
    parent = task_add("Sun Market parent", project="sample-store")
    child = task_add("Sun Market child", parent_id=parent["id"])
    rows = task_list(status="all", fields="full")["tasks"]
    child_row = next(r for r in rows if r["id"] == child["id"])
    assert child_row["project"] == "sample-store"


def test_task_add_subtask_explicit_project_overrides_parent():
    parent = task_add("TAP parent", project="client-portal")
    child = task_add("TAP child in unified-agent", parent_id=parent["id"], project="unified-agent")
    rows = task_list(status="all", fields="full")["tasks"]
    child_row = next(r for r in rows if r["id"] == child["id"])
    assert child_row["project"] == "unified-agent"


def test_task_list_project_filter_is_cross_project_by_default():
    task_add("Unified task", project="unified-agent")
    task_add("Exo task", project="browser")
    all_tasks = task_list(status="all")["tasks"]
    projects_seen = {t["project"] for t in all_tasks}
    assert {"unified-agent", "browser"}.issubset(projects_seen)


def test_task_list_project_filter_scopes_results():
    task_add("Unified only", project="unified-agent")
    task_add("Exo only", project="browser")
    exo_tasks = task_list(status="all", project="browser")["tasks"]
    assert all(t["project"] == "browser" for t in exo_tasks)


def test_task_list_rejects_unknown_project_filter():
    with pytest.raises(ToolError, match="project must be one of"):
        task_list(project="not-a-real-project")


def test_task_list_compact_mode_includes_project():
    t = task_add("Compact project task", project="browser")
    rows = task_list(status="all", fields="compact", project="browser")["tasks"]
    row = next(r for r in rows if r["id"] == t["id"])
    assert row["project"] == "browser"


def test_task_update_reassigns_project():
    t = task_add("Reassign me", project="unified-agent")
    updated = task_update(t["id"], project="sample-store")
    assert updated["project"] == "sample-store"


def test_task_update_rejects_unknown_project():
    t = task_add("Reject me", project="unified-agent")
    with pytest.raises(ToolError, match="project must be one of"):
        task_update(t["id"], project="not-a-real-project")


def test_task_update_rejects_whitespace_only_title_without_mutation():
    t = task_add("Keep this title")
    with pytest.raises(ToolError, match="title must not be empty"):
        task_update(t["id"], title=" \t \n")

    row = next(r for r in task_list(status="all", fields="full")["tasks"] if r["id"] == t["id"])
    assert row["title"] == "Keep this title"

    updated = task_update(t["id"], title="  Renamed  ")
    assert updated["title"] == "  Renamed  "


def test_task_update_expected_project_guard_blocks_cross_project_mutation():
    t = task_add("Guarded task", project="browser")
    with pytest.raises(ToolError, match="belongs to project 'browser'"):
        task_update(t["id"], notes="sneaky edit", expected_project="unified-agent")
    # Notes must be untouched — the guard rejects before any write happens.
    row = task_list(status="all", fields="full")["tasks"]
    row = next(r for r in row if r["id"] == t["id"])
    assert row["notes"] != "sneaky edit"


def test_task_update_expected_project_guard_allows_matching_project():
    t = task_add("Guarded ok task", project="browser")
    updated = task_update(t["id"], notes="fine edit", expected_project="browser")
    assert updated["notes"] == "fine edit"


def test_task_claim_expected_project_guard_blocks_cross_project_claim():
    t = task_add("Guarded claim task", project="client-portal")
    with pytest.raises(ToolError, match="belongs to project 'client-portal'"):
        task_claim(t["id"], agent="Desktop_Agent5", expected_project="sample-store")


def test_task_claim_expected_project_guard_allows_matching_project():
    t = task_add("Guarded claim ok task", project="client-portal")
    claimed = task_claim(t["id"], agent="Desktop_Agent5", expected_project="client-portal")
    assert claimed["status"] == "doing"


def test_all_stable_project_slugs_accepted():
    for slug in PROJECTS:
        t = task_add(f"Slug check {slug}", project=slug)
        row = task_list(status="all", fields="full")["tasks"]
        row = next(r for r in row if r["id"] == t["id"])
        assert row["project"] == slug


def test_migration_backfills_legacy_rows_as_unified_agent(tmp_path, monkeypatch):
    """A DB created before task #327 (no `project` column) backfills to
    'unified-agent' on the next init_db(), not NULL — same guarantee the
    ALTER TABLE ... DEFAULT clause gives new rows, extended to old ones.
    """
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
            "worktree_path TEXT, branch TEXT)"
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
        row = conn.execute("SELECT project FROM tasks WHERE title='pre-existing task'").fetchone()
    finally:
        conn.close()
    assert row[0] == "unified-agent"
