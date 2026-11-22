"""Worktree creation and disposal around the task lifecycle (task #194).

`task_update` used to create a worktree whenever a row *ended up* `doing` with
no worktree recorded — including calls that never changed status, and
bookkeeping claims for work already finished elsewhere. Every such call left a
directory and, more durably, a branch: `git worktree remove` does not delete
the branch it created, and `.worktrees/` is gitignored, so `git status` is
suppressed by design on exactly this path.

`task_claim` is deliberately not touched by any of this: the executor resolves
its sandbox and merge target from the `worktree_path` that claim persists
(`daemon.task_worktree`, `safety.get_active_worktree_path`,
`merge.git_merge_task`), so creation there is load-bearing.
"""

import subprocess

import pytest

from unified_agent import config, storage
from unified_agent.tools import tasks as tasks_tool


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo_workspace(isolated_env, workspace_is_product_repo):
    """The workspace root as a git repo with one commit on `main`."""
    _git("init", "-b", "main", cwd=isolated_env)
    _git("config", "user.name", "Test User", cwd=isolated_env)
    _git("config", "user.email", "test@example.com", cwd=isolated_env)
    (isolated_env / "initial.txt").write_text("hello")
    _git("add", "initial.txt", cwd=isolated_env)
    _git("commit", "-m", "feat: initial commit", cwd=isolated_env)
    storage._connection = None
    storage.init_db()
    yield isolated_env


def _branches(workspace):
    out = _git("branch", "--format=%(refname:short)", cwd=workspace).stdout
    return [b for b in out.split() if b]


def _task_branches(workspace):
    return [b for b in _branches(workspace) if b.startswith("task/")]


def test_notes_only_update_on_a_doing_task_creates_nothing(repo_workspace):
    """The sharpest case: a call that changes no status must have no side effect.

    A steward appending progress notes to an in-flight task would materialize a
    worktree and a branch, because the check asked what the row's status *is*
    rather than what this call *changed*.
    """
    t = tasks_tool.task_add(title="already in flight")
    tasks_tool.task_update(t["id"], status="doing", worktree=False)
    assert _task_branches(repo_workspace) == []

    tasks_tool.task_update(t["id"], notes="progress: still going")

    assert _task_branches(repo_workspace) == []
    assert not (repo_workspace / f".worktrees/task-{t['id']}").exists()


def test_repeated_doing_update_does_not_recreate_a_released_worktree(repo_workspace):
    """Re-asserting `doing` must not resurrect a worktree that was disposed of."""
    t = tasks_tool.task_add(title="re-asserted task")
    tasks_tool.task_update(t["id"], status="doing")
    assert len(_task_branches(repo_workspace)) == 1

    tasks_tool.task_update(t["id"], status="done", actor="test-agent")
    assert _task_branches(repo_workspace) == []

    tasks_tool.task_update(t["id"], notes="reopening the notes, not the work")

    assert _task_branches(repo_workspace) == []


def test_worktree_false_suppresses_creation_for_bookkeeping_claims(repo_workspace):
    """Recording a claim for work already done elsewhere must cost nothing."""
    t = tasks_tool.task_add(title="done in another checkout")

    res = tasks_tool.task_update(t["id"], status="doing", worktree=False)

    assert res["status"] == "doing"
    assert res["worktree_path"] is None
    assert res["branch"] is None
    assert _task_branches(repo_workspace) == []
    assert not (repo_workspace / ".worktrees").exists()


def test_genuine_transition_still_creates_a_worktree(repo_workspace):
    """The isolation the behavior exists for is preserved by default."""
    t = tasks_tool.task_add(title="real work starts here")

    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] == str(repo_workspace.resolve() / f".worktrees/task-{t['id']}")
    assert (repo_workspace / res["worktree_path"]).is_dir()
    assert len(_task_branches(repo_workspace)) == 1


def test_grok_claim_does_not_create_a_worktree(repo_workspace):
    """Grok is a governed hub citizen: attributed claims, no git worktree."""
    t = tasks_tool.task_add(title="grok conversation work")
    claimed = tasks_tool.task_claim(t["id"], agent="Desktop_Agent8")
    assert claimed["claimed_by"] == "Desktop_Agent8"
    assert claimed["worktree_path"] is None
    assert claimed["branch"] is None
    assert _task_branches(repo_workspace) == []
    assert not (repo_workspace / ".worktrees").exists()

    forced = tasks_tool.task_update(t["id"], worktree=True)
    assert forced["worktree_path"] is None
    assert _task_branches(repo_workspace) == []


def test_grok_updater_cannot_mint_worktree_on_foreign_claim(repo_workspace):
    """Grok stays worktree-free even when the row is claimed by someone else."""
    t = tasks_tool.task_add(title="claude already claimed this")
    parked = tasks_tool.task_update(t["id"], status="doing", worktree=False)
    assert parked["worktree_path"] is None
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by=? WHERE id=?",
            ("Desktop_Agent1", t["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    token = config.agent_identity_var.set("Desktop_Agent8")
    try:
        forced = tasks_tool.task_update(t["id"], worktree=True)
    finally:
        config.agent_identity_var.reset(token)

    assert forced["worktree_path"] is None
    assert _task_branches(repo_workspace) == []
    assert not (repo_workspace / ".worktrees").exists()


def test_done_releases_the_worktree_and_its_branch(repo_workspace):
    """Creation and disposal must be symmetric.

    The branch is the half that outlived everything: seven empty `task/*`
    branches survived their worktrees' removal on 2026-07-27 because
    `git_worktree_remove` leaves the branch behind.
    """
    t = tasks_tool.task_add(title="finishes cleanly")
    created = tasks_tool.task_update(t["id"], status="doing")
    wt = repo_workspace / created["worktree_path"]
    assert wt.is_dir()

    res = tasks_tool.task_update(t["id"], status="done", actor="test-agent")

    assert not wt.exists()
    assert _task_branches(repo_workspace) == []
    assert res["worktree"] == "removed"
    assert res["worktree_path"] is None
    assert res["branch"] is None


def test_done_retains_a_worktree_holding_uncommitted_work(repo_workspace):
    """Marking a row done is bookkeeping; it must never destroy code."""
    t = tasks_tool.task_add(title="has unsaved work")
    created = tasks_tool.task_update(t["id"], status="doing")
    wt = repo_workspace / created["worktree_path"]
    (wt / "scratch.txt").write_text("work nobody has committed yet")

    res = tasks_tool.task_update(t["id"], status="done", actor="test-agent")

    assert wt.is_dir()
    assert (wt / "scratch.txt").read_text() == "work nobody has committed yet"
    assert res["worktree"].startswith("retained:")
    # The row keeps pointing at the surviving directory — a dangling
    # worktree_path is worse than a retained one.
    assert res["worktree_path"] == created["worktree_path"]
    assert _task_branches(repo_workspace) == [created["branch"]]


def test_done_retains_a_branch_carrying_unmerged_commits(repo_workspace):
    """Committed-but-unmerged work is exactly what a squash-merge check is for."""
    t = tasks_tool.task_add(title="has unmerged commits")
    created = tasks_tool.task_update(t["id"], status="doing")
    wt = repo_workspace / created["worktree_path"]
    (wt / "feature.txt").write_text("content that exists nowhere else")
    _git("add", "feature.txt", cwd=wt)
    _git("commit", "-m", "feat: unmerged work", cwd=wt)

    res = tasks_tool.task_update(t["id"], status="done", actor="test-agent")

    assert wt.is_dir()
    assert res["worktree"].startswith("retained:")
    assert created["branch"] in _task_branches(repo_workspace)


def test_non_git_workspace_still_completes_a_task(isolated_env):
    """A workspace that is not a repo is supported; disposal must not raise."""
    t = tasks_tool.task_add(title="no repo here")
    tasks_tool.task_update(t["id"], status="doing")

    res = tasks_tool.task_update(t["id"], status="done", actor="test-agent")

    assert res["status"] == "done"
    assert res["worktree"] is None


def test_done_under_workspace_deny_does_not_fail_the_committed_transition(repo_workspace):
    """A DENY identity finishing a task with a recorded worktree must not error (task #1857).

    The status change is durable before the worktree release runs, and the release
    resolves a workspace-relative path through `config.workspace_root()`, which raises
    for an explicit UA_AGENT_WORKSPACES DENY identity. That OS-side effect may be
    reconciled later; it must not turn a committed `done` into a tool error.
    """
    t = tasks_tool.task_add(title="finished by a denied identity")
    tasks_tool.task_update(t["id"], status="doing", worktree=False)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET worktree_path=?, branch=? WHERE id=?",
            (f".worktrees/task-{t['id']}", f"task/{t['id']}-denied", t["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    binding = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        assert tasks_tool._release_task_worktree(f".worktrees/task-{t['id']}") == (
            "retained:workspace_denied"
        )
        result = tasks_tool.task_update(t["id"], status="done", actor="test-agent")
    finally:
        config.workspace_root_var.reset(binding)

    assert result["status"] == "done"
    # The worktree reference stays so the checkout can be reconciled later.
    assert result["worktree_path"] == f".worktrees/task-{t['id']}"
