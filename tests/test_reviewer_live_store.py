"""Live task-store preflight for the review pipeline (task #561).

Reproduces the 2026-08-22 desktop cron failures after Mac hub cutover:

- missing memory.db → sqlite3.connect creates an empty file →
  ``no such table: tasks``
- text stub at the memory.db path → ``file is not a database``
- ``NO_LOCAL_MEMORY_DB`` marker → host must not own the store

Also locks the explicit failure/success classes the reliability packet needs:
stale, merged-upstream, missing-worktree, red-tier, clean-hold (validated).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, reviewer, storage
from unified_agent.review_cli import main
from unified_agent.tools.tasks import task_add, task_update

SHA = "abc123def4567890abc123def4567890abc123de"
_DEFAULT_REVIEW_REPO = object()


def _write_stub_db(path: Path) -> None:
    path.write_text(
        "AUTHORITATIVE memory.db is on Mac hub only. Hub: http://100.64.0.10:8765/mcp\n",
        encoding="utf-8",
    )


def _get_notes(task_id: int) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()["notes"]
    finally:
        conn.close()


def test_require_live_store_rejects_missing_db(isolated_env):
    db = Path(config.db_path())
    db.unlink(missing_ok=True)
    with pytest.raises(storage.LiveStoreError, match="missing|no such table"):
        storage.require_live_task_store()


def test_require_live_store_rejects_text_stub(isolated_env):
    db = Path(config.db_path())
    _write_stub_db(db)
    with pytest.raises(storage.LiveStoreError, match="not a SQLite database"):
        storage.require_live_task_store()


def test_require_live_store_rejects_empty_sqlite_without_tasks(isolated_env):
    db = Path(config.db_path())
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    with pytest.raises(storage.LiveStoreError, match="no 'tasks' table"):
        storage.require_live_task_store()


def test_require_live_store_rejects_no_local_marker(isolated_env):
    marker = storage.no_local_memory_db_marker()
    marker.write_text("DO NOT CREATE — hub lives on Mac\n", encoding="utf-8")
    with pytest.raises(storage.LiveStoreError, match="retired"):
        storage.require_live_task_store()


def _remove_db_files() -> Path:
    db = Path(config.db_path())
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    return db


def test_connect_refuses_to_create_db_when_host_retired(isolated_env):
    """A retired host (marker present) must never grow a fresh memory.db.

    Regression: on 2026-09-27 an Linux desktop desktop without the marker got an
    empty 73-table memory.db the first time something imported storage. With
    the marker present, connect() must fail closed and leave no file behind.
    """
    db = _remove_db_files()
    storage.no_local_memory_db_marker().write_text("hub is on the Mac hub\n", encoding="utf-8")
    with pytest.raises(storage.LiveStoreError, match="retired"):
        storage.connect()
    assert not db.exists()


def test_init_db_refuses_when_host_retired(isolated_env):
    db = _remove_db_files()
    storage.no_local_memory_db_marker().write_text("hub is on the Mac hub\n", encoding="utf-8")
    with pytest.raises(storage.LiveStoreError, match="retired"):
        storage.init_db()
    assert not db.exists()


def test_require_live_store_accepts_initialized_db(isolated_env):
    path = storage.require_live_task_store()
    assert path == Path(config.db_path()).resolve()
    assert path.open("rb").read(16) == b"SQLite format 3\x00"


def test_cli_scan_exits_2_on_stub_db(isolated_env, capsys):
    _write_stub_db(Path(config.db_path()))
    code = main(["scan"])
    err = capsys.readouterr().err
    assert code == 2
    assert "not a SQLite database" in err
    assert "UA_HOME" in err


def test_cli_scan_exits_2_on_missing_db(isolated_env, capsys):
    Path(config.db_path()).unlink(missing_ok=True)
    code = main(["scan"])
    err = capsys.readouterr().err
    assert code == 2
    assert "missing" in err
    assert "refusing to auto-create" in err


def test_scan_for_reviewable_tasks_raises_toolerror_on_stub(isolated_env):
    _write_stub_db(Path(config.db_path()))
    with pytest.raises(ToolError, match="not a SQLite database"):
        reviewer.scan_for_reviewable_tasks()


def test_cron_wrapper_skips_when_no_local_marker(tmp_path):
    """deploy/review-pipeline-cron.sh must exit 0 without invoking the CLI."""
    ua_home = tmp_path / "ua_home"
    ua_home.mkdir(exist_ok=True)
    (ua_home / "NO_LOCAL_MEMORY_DB").write_text("DO NOT CREATE\n", encoding="utf-8")

    venv_bin = tmp_path / "dev" / "unified_agent" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "unified-agent-review"
    target.write_text("#!/bin/sh\necho SHOULD_NOT_RUN >&2\nexit 99\n", encoding="utf-8")
    target.chmod(0o755)

    script = Path(__file__).resolve().parents[1] / "deploy" / "review-pipeline-cron.sh"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "UA_HOME": str(ua_home),
    }
    res = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    log = (ua_home / "review.log").read_text(encoding="utf-8")
    assert "skipped — local task store retired" in log
    assert "SHOULD_NOT_RUN" not in res.stderr


def test_cron_wrapper_single_instance_lock(tmp_path):
    """Task #1254 (Devin review, PR #691): raising the pytest gate's default
    ceiling from 600s to 1800s means a genuinely slow run can now outlast the
    wrapper's own */15-minute cadence. Without a lock, an overlapping
    invocation would read the same pre-timeout note count as the run still
    in flight and bypass reviewer.py's per-sha retry cap. This launches two
    overlapping invocations of the real script (a slow stub CLI standing in
    for a long pytest run) and asserts exactly one of them actually runs the
    'CLI' while the other logs a lock-skip and never touches it.
    """
    ua_home = tmp_path / "ua_home"
    ua_home.mkdir(exist_ok=True)

    venv_bin = tmp_path / "dev" / "unified_agent" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "unified-agent-review"
    marker = tmp_path / "cli_invocations"
    # Slow enough that the second invocation is guaranteed to observe the
    # first one's lock (started ~0.3s later, well inside this 2s window).
    target.write_text(
        f"#!/bin/sh\necho run >> \"{marker}\"\nsleep 2\necho '[]'\nexit 0\n",
        encoding="utf-8",
    )
    target.chmod(0o755)

    script = Path(__file__).resolve().parents[1] / "deploy" / "review-pipeline-cron.sh"
    env = {**os.environ, "HOME": str(tmp_path), "UA_HOME": str(ua_home)}

    proc1 = subprocess.Popen(
        ["bash", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True
    )

    time.sleep(0.3)
    res2 = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, timeout=15
    )
    _out1, _err1 = proc1.communicate(timeout=15)
    assert proc1.returncode == 0
    assert res2.returncode == 0

    # The stub CLI ran exactly once — the second invocation never called it.
    assert marker.read_text(encoding="utf-8").count("run") == 1

    log = (ua_home / "review.log").read_text(encoding="utf-8")
    assert "another review-pipeline-cron run is still in progress" in log
    # And the lock is released after the holder finishes — no leftover dir.
    assert not (ua_home / "review-pipeline.lock").exists()


# ── outcome classes (reconcile + validate path) ─────────────────────────────


@pytest.fixture
def green_worktree(monkeypatch):
    wt = {"path": "/tmp/wt-live-store", "branch": "fix/task-x", "head": SHA}

    def fake_gates(task_id, wt_path, force=False, **kwargs):
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        reviewer,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    return wt


def _quiet_git(monkeypatch, worktrees=None, project_repo=_DEFAULT_REVIEW_REPO):
    # Mocked Git evidence still needs an existing declared product checkout.
    # An absent checkout is the distinct unresolved-repo outcome.
    if project_repo is _DEFAULT_REVIEW_REPO:
        project_repo = config.dev_root() / config.PROJECT_REPO_DIRNAMES["unified-agent"]
    if project_repo is not None:
        Path(project_repo).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path",
        lambda project: Path(project_repo) if project_repo else None,
    )
    monkeypatch.setattr(reviewer, "_list_worktrees", lambda repo_path=None: worktrees or [])
    monkeypatch.setattr(reviewer, "_fetch_origin", lambda repo_path: None)
    monkeypatch.setattr(reviewer, "_branch_exists_locally", lambda branch, repo_path: False)
    monkeypatch.setattr(
        reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: False
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: None)


def _review_task(status="doing", notes_extra="") -> int:
    notes = f"{reviewer.WAITING_FOR_REVIEW} {notes_extra}".strip()
    t = task_add("live-store-outcome", notes=notes)
    task_update(t["id"], status=status)
    return t["id"]


def test_outcome_clean_hold_validated(isolated_env, green_worktree):
    tid = _review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert "[review: validated @" in _get_notes(tid)


def test_outcome_red_tier(isolated_env, green_worktree, monkeypatch):
    monkeypatch.setattr(
        reviewer,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "CLAUDE.md", "status": "M"}],
    )
    tid = _review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "red-tier"
    assert res["red_tier_files"]


def test_outcome_missing_worktree_failed(isolated_env, monkeypatch):
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "failed"


def test_outcome_merged_upstream(isolated_env, monkeypatch):
    tid = _review_task()
    wts = [
        {"path": "/tmp/main", "branch": "main"},
        {"path": f"/tmp/wt-{tid}", "branch": f"fix/task-{tid}", "head": SHA},
    ]
    _quiet_git(monkeypatch, worktrees=wts)
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: True)
    out = reviewer.reconcile_tasks(dry_run=True)
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert out["results"][0]["task_id"] == tid


def test_outcome_stale_branch_missing(isolated_env, monkeypatch):
    tid = _review_task()
    _quiet_git(monkeypatch)
    out = reviewer.reconcile_tasks(dry_run=True)
    assert out["results"][0]["verdict"] == "stale"
    assert out["results"][0]["task_id"] == tid


def test_reconcile_uses_project_repo_not_cockpit_workspace(isolated_env, monkeypatch, tmp_path):
    """Canonical root for a named-project task is the project checkout, not UA_WORKSPACE."""
    product = tmp_path / "dev" / "browser"
    product.mkdir(parents=True)
    monkeypatch.setenv("UA_DEV_ROOT", str(tmp_path / "dev"))

    task_add(
        "project-root",
        notes=f"{reviewer.WAITING_FOR_REVIEW} branch feat/x",
        project="browser",
    )
    # task_add may leave status open; reconcile accepts open (#303).
    seen: list[str] = []

    def capture_fetch(repo):
        seen.append(str(repo))

    monkeypatch.setattr(reviewer, "_fetch_origin", capture_fetch)
    monkeypatch.setattr(reviewer, "_list_worktrees", lambda repo: [])
    monkeypatch.setattr(reviewer, "_branch_from_notes", lambda notes: "feat/x")
    monkeypatch.setattr(reviewer, "_branch_exists_locally", lambda branch, repo: False)
    monkeypatch.setattr(reviewer, "_pr_ref_from_notes", lambda notes, **_kw: None)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo: None)

    reviewer.reconcile_tasks(dry_run=True)
    assert seen, "reconcile should fetch the project repo"
    assert Path(seen[0]).resolve() == product.resolve()
    assert Path(seen[0]).resolve() != Path(config.workspace_root()).resolve()


@pytest.mark.parametrize("retired", [False, True])
def test_review_cli_refuses_missing_or_retired_store_before_initialization(
    isolated_env, monkeypatch, retired
):
    from unified_agent import review_cli

    db = _remove_db_files()
    if retired:
        storage.no_local_memory_db_marker().write_text("hub owns storage\n", encoding="utf-8")
    called = []
    monkeypatch.setattr(storage, "init_db", lambda: called.append("init"))
    monkeypatch.setattr(
        review_cli.reviewer, "review_task", lambda *args, **kwargs: called.append("review")
    )
    assert review_cli.main(["task", "17", "--no-notify"]) == 2
    assert called == []
    assert not db.exists()


@pytest.mark.parametrize("command", [["scan", "--no-notify"], ["task", "17", "--no-notify"]])
def test_review_cli_refuses_newer_schema_with_configuration_error(
    isolated_env, monkeypatch, capsys, command
):
    newer_version = storage.SCHEMA_VERSION + 1
    conn = storage.connect()
    try:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(newer_version),))
        conn.commit()
        schema_before = [tuple(row) for row in conn.execute("SELECT * FROM sqlite_master")]
    finally:
        conn.close()
    called = []
    monkeypatch.setattr(reviewer, "review_task", lambda *args, **kwargs: called.append("review"))
    monkeypatch.setattr(
        reviewer, "scan_for_reviewable_tasks", lambda *args, **kwargs: called.append("scan")
    )

    assert main(command) == 2
    error = capsys.readouterr().err
    assert f"schema_version={newer_version}" in error
    assert "newer than this code" in error
    assert "Traceback" not in error
    assert called == []
    conn = storage.connect()
    try:
        assert storage.get_schema_version(conn) == newer_version
        assert [tuple(row) for row in conn.execute("SELECT * FROM sqlite_master")] == schema_before
    finally:
        conn.close()


def test_a_merged_task_waiting_on_its_deployment_is_not_closed(isolated_env, monkeypatch):
    tid = _review_task()
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO task_liveness (task_id, wait_reason, wait_until, wait_deploy_target, "
            "wait_deploy_sha, updated_at) VALUES (?, 'rolling out', ?, 'hub', ?, ?)",
            (tid, until, "a" * 40, until),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert "deployment" in reviewer._delegation_reconcile_blocker(conn, row)
