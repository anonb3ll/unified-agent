"""Direct tests for `_worktree_is_disposable` (task #423).

This function decides whether `cleanup_stale_worktrees` may run `git worktree
remove --force` on a directory, so a wrong answer either destroys unmerged work
or retains every worktree forever. It had no direct coverage: all four call
sites in `test_worktree_policy.py` monkeypatch it out. That is how a symmetric
`git diff base..HEAD` — which reports a difference whether the branch is ahead
of the base OR behind it — survived as the merged-ness test.

These build real repositories rather than mocking git, because every bug this
function has had was a disagreement about what a git command actually returns.
"""

import subprocess

import pytest

from unified_agent.tools.worktree_policy import _worktree_is_disposable


def _run(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _write(repo, name, text):
    (repo / name).write_text(text)


@pytest.fixture
def repo(tmp_path):
    """A repo on `main` with one commit, isolated from the caller's git config."""
    path = tmp_path / "repo"
    path.mkdir()
    _run(path, "init", "-b", "main")
    _run(path, "config", "user.name", "Test")
    _run(path, "config", "user.email", "test@example.invalid")
    _run(path, "config", "commit.gpgsign", "false")
    _write(path, "base.txt", "base\n")
    _run(path, "add", "-A")
    _run(path, "commit", "-m", "base")
    return path


def _branch(repo, name):
    _run(repo, "checkout", "-b", name)


def test_uncommitted_changes_are_never_disposable(repo):
    _write(repo, "base.txt", "dirty\n")
    assert _worktree_is_disposable(repo) == (False, "uncommitted_changes")


def test_untracked_file_counts_as_uncommitted(repo):
    _write(repo, "scratch.txt", "notes\n")
    assert _worktree_is_disposable(repo) == (False, "uncommitted_changes")


def test_head_identical_to_base_is_disposable(repo):
    disposable, reason = _worktree_is_disposable(repo)
    assert disposable, reason


def test_branch_with_unmerged_work_is_retained(repo):
    _branch(repo, "feature")
    _write(repo, "feature.txt", "new work\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "feature work")
    disposable, reason = _worktree_is_disposable(repo)
    assert not disposable
    assert reason == "unmerged_content"


def test_squash_merged_branch_is_disposable(repo):
    """The regression that motivated replacing the ancestry check.

    A squash merge rewrites the branch's commits, so the branch is not an
    ancestor of main — but its content is entirely present in main.
    """
    _branch(repo, "feature")
    _write(repo, "feature.txt", "new work\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "feature work")
    _run(repo, "checkout", "main")
    _run(repo, "merge", "--squash", "feature")
    _run(repo, "commit", "-m", "squashed feature")
    _run(repo, "checkout", "feature")

    assert not _is_ancestor(repo, "feature", "main"), "precondition: squash breaks ancestry"
    disposable, reason = _worktree_is_disposable(repo)
    assert disposable, reason


def test_merged_branch_stays_disposable_after_base_advances(repo):
    """The bug this task fixes.

    `git diff main..HEAD` is symmetric, so once main gains commits the branch
    does not have, a fully merged worktree reads as unmerged — permanently,
    since main always advances. Observed live on `codex/fix-webui-spec-findings`
    and `codex/reconcile-agy-mobile`, both merged, both reported unmerged.
    """
    _branch(repo, "feature")
    _write(repo, "feature.txt", "new work\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "feature work")
    _run(repo, "checkout", "main")
    _run(repo, "merge", "--squash", "feature")
    _run(repo, "commit", "-m", "squashed feature")

    # main moves on, touching files the branch never saw.
    _write(repo, "later.txt", "unrelated later work\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "later work on main")
    _run(repo, "checkout", "feature")

    disposable, reason = _worktree_is_disposable(repo)
    assert disposable, f"merged worktree retained after main advanced: {reason}"


def test_conflicting_branch_is_retained(repo):
    """Both sides edited the same lines — real divergence, keep the directory."""
    _branch(repo, "feature")
    _write(repo, "base.txt", "feature version\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "feature edit")
    _run(repo, "checkout", "main")
    _write(repo, "base.txt", "main version\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "main edit")
    _run(repo, "checkout", "feature")

    disposable, reason = _worktree_is_disposable(repo)
    assert not disposable
    assert reason == "conflicts_with_base"


def test_repo_without_main_or_master_is_retained(repo):
    _run(repo, "branch", "-m", "main", "trunk")
    assert _worktree_is_disposable(repo) == (False, "no_base_branch")


def test_non_repo_directory_is_retained(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    disposable, reason = _worktree_is_disposable(plain)
    assert not disposable
    assert reason == "git_status_unavailable"


def test_non_repo_directory_inside_a_repo_honors_the_git_ceiling(repo, monkeypatch):
    """A caller's GIT_CEILING_DIRECTORIES reaches git (task #2280).

    `_git` builds a minimal environment; without the ceiling, a plain directory
    inside a clean, merged repository read as that repository and so as
    disposable.
    """
    plain = repo / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo.resolve()))
    assert _worktree_is_disposable(plain) == (False, "git_status_unavailable")


def _is_ancestor(repo, maybe_ancestor, descendant):
    res = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


class TestSurveyWorktrees:
    """`survey_worktrees` is the visibility half of task #424.

    `cleanup_stale_worktrees` walks only `config.workspace_root()/.worktrees`,
    while COLLABORATION.md Rule 2 puts worktrees in `~/dev/<repo>/.worktrees`.
    The survey reads both; it must never remove anything outside the workspace.
    """

    def _worktree(self, base, name):
        wt = base / ".worktrees" / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run(base, "worktree", "add", "-b", f"branch-{name}", str(wt))
        return wt

    def test_finds_worktrees_under_an_extra_root(self, repo, monkeypatch, tmp_path):
        from unified_agent.tools import worktree_policy

        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))

        self._worktree(repo, "extra")
        found = worktree_policy.survey_worktrees(extra_roots=[repo])

        paths = [f["path"] for f in found]
        assert any(p.endswith(".worktrees/extra") for p in paths), paths
        assert all(f["in_workspace"] is False for f in found)

    def test_classifies_a_merged_worktree_as_disposable(self, repo, monkeypatch, tmp_path):
        from unified_agent.tools import worktree_policy

        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))

        self._worktree(repo, "merged")
        (found,) = worktree_policy.survey_worktrees(extra_roots=[repo])
        assert found["disposable"] is True
        assert found["reason"] == ""

    def test_survey_never_removes_anything(self, repo, monkeypatch, tmp_path):
        from unified_agent.tools import worktree_policy

        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))

        wt = self._worktree(repo, "keepme")
        worktree_policy.survey_worktrees(extra_roots=[repo])
        assert wt.is_dir(), "survey is read-only and must leave the directory in place"

    def test_missing_root_is_skipped_not_raised(self, monkeypatch, tmp_path):
        from unified_agent.tools import worktree_policy

        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))

        assert worktree_policy.survey_worktrees(extra_roots=[tmp_path / "nope"]) == []

    def test_survey_does_not_raise_when_request_workspace_is_denied(
        self, repo, monkeypatch, tmp_path
    ):
        """Desktop_Person's Control Center login is UA_AGENT_WORKSPACES DENY.

        The survey is hub census, not an fs_* tool, so DENY must not abort it.
        """
        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent import config
        from unified_agent.tools import worktree_policy

        workspace = tmp_path / "hub-workspace"
        workspace.mkdir(exist_ok=True)
        monkeypatch.setenv("UA_WORKSPACE", str(workspace))
        self._worktree(repo, "denied-visible")
        token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
        try:
            with pytest.raises(ToolError, match="denied"):
                config.workspace_root()
            found = worktree_policy.survey_worktrees(extra_roots=[repo])
        finally:
            config.workspace_root_var.reset(token)
        assert any(p.endswith(".worktrees/denied-visible") for p in [f["path"] for f in found])

    def test_survey_preserves_bound_request_workspace(self, repo, monkeypatch, tmp_path):
        from unified_agent import config
        from unified_agent.tools import worktree_policy

        process_root = tmp_path / "process"
        process_root.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(process_root))
        self._worktree(repo, "bound-visible")
        token = config.workspace_root_var.set(repo)
        try:
            found = worktree_policy.survey_worktrees(extra_roots=[])
        finally:
            config.workspace_root_var.reset(token)
        assert any(p.endswith(".worktrees/bound-visible") for p in [f["path"] for f in found])
