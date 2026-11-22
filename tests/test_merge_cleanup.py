"""Legacy merge cleanup is fenced to the merged commit, against real git (#2373)."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unified_agent import config, merge, storage

pytestmark = pytest.mark.smoke


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def merge_source(monkeypatch):
    """A done legacy task on a real worktree, with only the gates stubbed."""
    repo = Path(config.workspace_root())
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    worktree = repo / ".worktrees" / "task-31"
    branch = "task/31-source"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", branch)
    (worktree / "work.txt").write_text("reviewed\n")
    _git(worktree, "add", "work.txt")
    _git(worktree, "commit", "-q", "-m", "reviewed work")
    now = storage.now()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,status,priority,worktree_path,branch,created_at,updated_at) "
            "VALUES (31,'Source','done','med',?,?,?,?)",
            (".worktrees/task-31", branch, now, now),
        )
        conn.commit()
    monkeypatch.setattr(merge, "run_validation_gates", MagicMock())
    monkeypatch.setattr(merge, "sync_state_to_vault", MagicMock())
    monkeypatch.setattr(merge, "_run_post_merge_verify", lambda _id: None)
    monkeypatch.setattr(merge, "_close_delegation_ledger", MagicMock(return_value={}))
    return {
        "repo": repo,
        "worktree": worktree,
        "branch": branch,
        "ref": f"refs/heads/{branch}",
        "pinned": _git(worktree, "rev-parse", "HEAD"),
    }


def _ref_exists(source) -> bool:
    return (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", source["ref"]], cwd=source["repo"]
        ).returncode
        == 0
    )


@pytest.mark.parametrize("force", [True, False])
def test_cleanup_retains_a_source_that_advanced_after_the_head_check(
    merge_source, monkeypatch, force
):
    real_git = merge._run_git_in_repo
    advanced = {}

    def git(args, repo):
        result = real_git(args, repo)
        if args[0] == "merge":
            _git(merge_source["worktree"], "commit", "-q", "--allow-empty", "-m", "unreviewed")
            advanced["sha"] = _git(merge_source["worktree"], "rev-parse", "HEAD")
        return result

    monkeypatch.setattr(merge, "_run_git_in_repo", git)

    result = merge.orchestrate_merge(31, force=force)

    assert result["success"] is True
    assert result["cleanup_skipped"] == "source changed after admission"
    assert merge_source["worktree"].is_dir()
    assert _git(merge_source["repo"], "rev-parse", merge_source["ref"]) == advanced["sha"]
    assert _git(merge_source["repo"], "rev-parse", "main") == merge_source["pinned"]


@pytest.mark.parametrize("force", [True, False])
def test_cleanup_removes_a_source_still_at_the_merged_commit(merge_source, force):
    result = merge.orchestrate_merge(31, force=force)

    assert result["success"] is True
    assert "cleanup_skipped" not in result
    assert "branch_retained" not in result
    assert not merge_source["worktree"].exists()
    assert not _ref_exists(merge_source)
    assert _git(merge_source["repo"], "rev-parse", "main") == merge_source["pinned"]


def test_cleanup_ref_delete_refuses_a_tip_that_moved_after_the_check(merge_source, monkeypatch):
    real_remove = merge._worktree_remove_in_repo
    late = {}

    def remove_then_advance(path, repo, force=False):
        real_remove(path, repo, force=force)
        tree = _git(repo, "rev-parse", f"{merge_source['pinned']}^{{tree}}")
        late["sha"] = _git(repo, "commit-tree", tree, "-p", merge_source["pinned"], "-m", "late")
        _git(repo, "update-ref", merge_source["ref"], late["sha"])

    monkeypatch.setattr(merge, "_worktree_remove_in_repo", remove_then_advance)

    result = merge.orchestrate_merge(31, force=True)

    assert result["branch_retained"] == "expected-SHA deletion refused"
    assert _git(merge_source["repo"], "rev-parse", merge_source["ref"]) == late["sha"]


def test_cleanup_keeps_the_branch_of_a_worktree_it_could_not_remove(merge_source, monkeypatch):
    monkeypatch.setattr(
        merge, "_worktree_remove_in_repo", MagicMock(side_effect=merge.ToolError("dirty"))
    )

    result = merge.orchestrate_merge(31, force=True)

    assert result["cleanup_skipped"] == "source worktree retained: dirty"
    assert _ref_exists(merge_source)


def test_cleanup_keeps_a_branch_no_longer_contained_in_main(merge_source, monkeypatch):
    real_remove = merge._worktree_remove_in_repo

    def remove_then_rewind(path, repo, force=False):
        real_remove(path, repo, force=force)
        _git(merge_source["repo"], "reset", "-q", "--hard", f"{merge_source['pinned']}~1")

    monkeypatch.setattr(merge, "_worktree_remove_in_repo", remove_then_rewind)

    result = merge.orchestrate_merge(31, force=False)

    assert result["cleanup_skipped"] == "source commit is no longer contained in main"
    assert _git(merge_source["repo"], "rev-parse", merge_source["ref"]) == merge_source["pinned"]


@pytest.mark.parametrize("force", [True, False])
def test_cleanup_keeps_a_branch_checked_out_in_another_worktree(merge_source, monkeypatch, force):
    real_remove = merge._worktree_remove_in_repo
    other = merge_source["repo"] / ".worktrees" / "other-owner"

    def remove_then_reoccupy(path, repo, force=False):
        real_remove(path, repo, force=force)
        _git(merge_source["repo"], "worktree", "add", "-q", str(other), merge_source["branch"])

    monkeypatch.setattr(merge, "_worktree_remove_in_repo", remove_then_reoccupy)

    result = merge.orchestrate_merge(31, force=force)

    assert result["cleanup_skipped"] == "source branch is checked out in another worktree"
    assert _git(other, "symbolic-ref", "HEAD") == merge_source["ref"]
    assert _git(other, "rev-parse", "HEAD") == merge_source["pinned"]
