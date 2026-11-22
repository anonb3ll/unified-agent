"""Task #531: review-pipeline helpers must reach product-repo worktrees.

Regression: ``tools.git._run_git`` / ``git_worktree_list`` resolve through
``resolve_in_workspace``, so product checkouts under ~/dev silent-empty and
hide red-tier / reconcile evidence. The merge gates already use
``resolve_task_worktree`` + ``merge._run_git_in_repo``; reviewer helpers must
share that pipeline-only escape without widening the agent sandbox.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unified_agent import config, reviewer


@pytest.fixture
def project_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "dev" / "unified_agent"
    wt = repo / ".worktrees" / "task-531"
    wt.mkdir(parents=True)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [repo])
    return repo, wt


def test_head_sha_uses_pipeline_git_escape(project_worktree):
    _repo, wt = project_worktree
    with (
        patch(
            "unified_agent.safety.resolve_task_worktree",
            side_effect=lambda p: Path(p).resolve(),
        ) as resolve,
        patch(
            "unified_agent.merge._run_git_in_repo",
            return_value=MagicMock(returncode=0, stdout="abc123\n"),
        ) as git,
        patch("unified_agent.tools.git._run_git") as sandboxed,
    ):
        assert reviewer._head_sha(str(wt)) == "abc123"
    resolve.assert_called_once()
    assert git.call_args[0][1] == str(wt.resolve())
    sandboxed.assert_not_called()


def test_changed_files_vs_main_uses_pipeline_git_escape(project_worktree):
    _repo, wt = project_worktree
    with (
        patch(
            "unified_agent.safety.resolve_task_worktree",
            side_effect=lambda p: Path(p).resolve(),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo",
            return_value=MagicMock(returncode=0, stdout="M\tsrc/unified_agent/reviewer.py\n"),
        ) as git,
        patch("unified_agent.tools.git._run_git") as sandboxed,
    ):
        changed = reviewer._changed_files_vs_main(str(wt), "feat/x")
    assert changed == [{"file": "src/unified_agent/reviewer.py", "status": "M"}]
    assert git.call_args[0][1] == str(wt.resolve())
    sandboxed.assert_not_called()


def test_list_worktrees_uses_project_repo_escape(project_worktree):
    repo, _wt = project_worktree
    fake = [{"path": str(repo / ".worktrees" / "task-531"), "branch": "feat/x"}]
    with (
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=fake,
        ) as project_list,
        patch("unified_agent.tools.git.git_worktree_list") as sandboxed,
    ):
        assert reviewer._list_worktrees(str(repo)) == fake
    project_list.assert_called_once_with(repo.resolve())
    sandboxed.assert_not_called()


def test_list_worktrees_workspace_still_uses_sandboxed_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(config, "project_repo_roots", lambda: [])
    with patch(
        "unified_agent.tools.git.git_worktree_list",
        return_value={"worktrees": [{"path": str(tmp_path)}]},
    ) as sandboxed:
        assert reviewer._list_worktrees() == [{"path": str(tmp_path)}]
    sandboxed.assert_called_once()


def test_branch_helpers_use_known_repo_git(project_worktree):
    repo, _wt = project_worktree

    def _git(args, repo_path):
        if args[:1] == ["rev-parse"] and args[-1] == "feat/x":
            return MagicMock(returncode=0, stdout="branchsha\n")
        if args[:1] == ["rev-parse"] and args[-1] == "origin/main":
            return MagicMock(returncode=0, stdout="mainsha\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return MagicMock(returncode=0, stdout="")
        if args[:1] == ["fetch"]:
            return MagicMock(returncode=0, stdout="")
        if args[:1] == ["rev-parse"] and "--verify" in args:
            return MagicMock(returncode=0, stdout="")
        return MagicMock(returncode=0, stdout="")

    with (
        patch("unified_agent.merge._known_repo_or_raise", return_value=str(repo)) as known,
        patch("unified_agent.merge._run_git_in_repo", side_effect=_git) as git,
        patch("unified_agent.tools.git._run_git") as sandboxed,
    ):
        assert reviewer._branch_exists_locally("feat/x", str(repo)) is True
        assert reviewer._branch_merged_into_origin_main("feat/x", str(repo)) is True
        reviewer._fetch_origin(str(repo))
    assert known.call_count >= 3
    assert git.call_count >= 3
    sandboxed.assert_not_called()


def test_agent_sandbox_unchanged_for_product_worktree(project_worktree):
    """Widening reviewer gates must not widen agent-facing write/execute reach."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.safety import resolve_in_workspace
    from unified_agent.tools.files import fs_write
    from unified_agent.tools.shell import run_cmd

    _repo, wt = project_worktree
    target = str(wt / "pwned.txt")
    with pytest.raises(ToolError, match="escapes the workspace"):
        resolve_in_workspace(target)
    with pytest.raises(ToolError, match="escapes the workspace"):
        fs_write(target, "nope\n")
    with pytest.raises(ToolError, match="escapes the workspace"):
        run_cmd("git status", cwd=str(wt))
    assert not (wt / "pwned.txt").exists()
