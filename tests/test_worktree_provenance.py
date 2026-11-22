import subprocess
from unittest.mock import MagicMock

import pytest

from unified_agent import config, merge
from unified_agent.tools import worktree_policy


@pytest.mark.parametrize("product_present", [False, True])
def test_absolute_product_checkout_miss_never_adopts_cockpit_evidence(
    tmp_path, monkeypatch, product_present
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    if product_present:
        product.mkdir()
    row = {
        "project": "unified-agent",
        "notes": "unmarked product task",
        "worktree_path": str(product / ".worktrees/task-42"),
        "branch": "task/42-product",
    }
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "dev_root", lambda: tmp_path)
    monkeypatch.setattr(
        config, "project_repo_path", lambda _project: product if product_present else None
    )
    monkeypatch.setattr(config, "project_repo_roots", lambda: [product] if product_present else [])
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = row
    monkeypatch.setattr(merge.storage, "connect", lambda: connection)
    monkeypatch.setattr(
        merge,
        "_worktrees_of_project_repo",
        lambda _repo: [{"path": str(product), "branch": "main"}],
    )
    monkeypatch.setattr(
        merge,
        "git_worktree_list",
        lambda **_kwargs: {
            "worktrees": [
                {"path": str(workspace), "branch": "master"},
                {"path": str(workspace / ".worktrees/task-42"), "branch": "task/42-cockpit"},
            ]
        },
    )
    assert merge._task_repo_routing(row)[1] is True
    assert merge.find_task_worktree(42)[0] is None


def test_only_valid_legacy_relative_paths_keep_cockpit_fallback(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    product.mkdir()
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "dev_root", lambda: tmp_path)
    monkeypatch.setattr(config, "project_repo_path", lambda _project: product)
    row = {"project": "unified-agent", "notes": "", "worktree_path": ".worktrees/task-42"}
    assert merge._task_repo_routing(row)[1] is False
    for path in [
        None,
        "../.worktrees/task-42",
        "other/task-42",
        ".worktrees/.git/task-42",
        ".worktrees/not-a-task",
    ]:
        assert merge._task_repo_routing({**row, "worktree_path": path})[1] is True


def test_cleanup_discovers_product_worktrees_when_workspace_has_none(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    worktree = product / ".worktrees/task-42"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("fixture")
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [product, product])
    monkeypatch.setattr(worktree_policy, "_load_task_status_map", lambda: {42: {"status": "done"}})
    result = worktree_policy.cleanup_stale_worktrees(dry_run=True)
    assert len(result["stale_worktrees"]) == 1
    candidate = result["stale_worktrees"][0]
    assert candidate["path"] == str(worktree) and candidate["repo_path"] == str(product)
    assert candidate["task_id"] == 42 and candidate["reason"] == "task_done"


@pytest.mark.parametrize("state", ["clean", "dirty", "unmerged"])
def test_product_cleanup_uses_owning_repo_and_preserves_uncommitted_work(
    tmp_path, monkeypatch, state
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    product.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(product), *args], check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    (product / "marker.txt").write_text("committed baseline")
    git("add", "marker.txt")
    git("commit", "-m", "test fixture")
    worktree = product / ".worktrees/task-42"
    git("worktree", "add", "-b", "task/42-fixture", str(worktree))
    if state != "clean":
        (worktree / "marker.txt").write_text("recoverable work remains here")
    if state == "unmerged":
        git("-C", str(worktree), "add", "marker.txt")
        git("-C", str(worktree), "commit", "-m", "unmerged work")
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [product])
    monkeypatch.setattr(worktree_policy, "_load_task_status_map", lambda: {42: {"status": "done"}})
    result = worktree_policy.cleanup_stale_worktrees(dry_run=False)
    assert product.is_dir() and (product / "marker.txt").read_text() == "committed baseline"
    if state != "clean":
        assert result["cleaned_count"] == 0
        expected = "uncommitted_changes" if state == "dirty" else "unmerged_content"
        assert result["retained"][0]["retained_because"] == expected
        assert (worktree / "marker.txt").read_text() == "recoverable work remains here"
    else:
        assert result["cleaned_count"] == 1 and result["failed"] == []
        assert not worktree.exists()
        assert str(worktree) not in git("worktree", "list").stdout


def test_read_only_survey_includes_configured_product_roots(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    worktree = product / ".worktrees/task-42"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("fixture")
    monkeypatch.setattr(config, "process_workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "audit_roots", list)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [product, product])
    monkeypatch.setattr(worktree_policy, "_load_task_status_map", lambda: {42: {"status": "done"}})
    monkeypatch.setattr(worktree_policy, "_worktree_is_disposable", lambda _p: (False, "unmerged"))
    rows = worktree_policy.survey_worktrees()
    assert len(rows) == 1 and rows[0]["root"] == str(product)
    assert rows[0]["in_workspace"] is False and rows[0]["disposable"] is False
    assert worktree.exists()


def test_idle_product_checkout_with_active_claim_is_retained(tmp_path, monkeypatch):
    import os
    from datetime import UTC, datetime, timedelta

    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    worktree = product / ".worktrees/task-42"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("fixture")
    os.utime(worktree, (1, 1))
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [product])
    monkeypatch.setattr(
        worktree_policy,
        "_load_task_status_map",
        lambda: {
            42: {
                "status": "doing",
                "claimed_by": "Desktop_Agent1",
                "lock_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        },
    )
    monkeypatch.setattr(
        worktree_policy,
        "_worktree_is_disposable",
        lambda _p: pytest.fail("live checkout inspected for deletion"),
    )
    result = worktree_policy.cleanup_stale_worktrees(dry_run=False)
    assert result["stale_worktrees"] == [] and result["cleaned_count"] == 0
    assert worktree.exists()
