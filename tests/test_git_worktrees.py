import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.git import git_worktree_add, git_worktree_list, git_worktree_remove

pytestmark = pytest.mark.smoke


@pytest.fixture
def mock_git_repo_with_commit(isolated_env, monkeypatch):
    """Create a git repository inside the isolated workspace with an initial commit."""
    repo_dir = isolated_env / "test_worktree_repo"
    repo_dir.mkdir()

    # Initialize repository
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True
    )

    # Commit dummy file (required since git worktree needs at least one commit)
    dummy = repo_dir / "initial.txt"
    dummy.write_text("hello")
    subprocess.run(["git", "add", "initial.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: initial commit"], cwd=repo_dir, capture_output=True
    )

    # Mock resolve_in_workspace in git.py
    import unified_agent.tools.git as git_tool

    monkeypatch.setattr(
        git_tool,
        "resolve_in_workspace",
        lambda path, use_worktree=True: repo_dir if path == "." else repo_dir / path,
    )

    return repo_dir


@pytest.fixture
def workspace_repo(isolated_env, workspace_is_product_repo):
    """A real git repo AT the workspace root, with `resolve_in_workspace` unpatched.

    `mock_git_repo_with_commit` stubs out path resolution, which is exactly the
    machinery under test for worktree placement — these tests need the real one.
    """
    repo_dir = isolated_env
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True
    )
    (repo_dir / "initial.txt").write_text("hello")
    subprocess.run(["git", "add", "initial.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: initial commit"], cwd=repo_dir, capture_output=True
    )
    return repo_dir


def test_worktree_creation_ignores_the_callers_active_worktree(workspace_repo, monkeypatch):
    """A second claim must not nest its worktree inside the first one's.

    Regression (task #187): `resolve_in_workspace` defaults to resolving
    relative paths against the caller's *active task worktree*, so an agent
    already holding task A that claims task B had B's `.worktrees/task-B`
    resolved against A — producing `.worktrees/task-A/.worktrees/task-B`,
    observed live as `.worktrees/task-180/.worktrees/task-159`. The worktree
    layout is a property of the workspace, not of who happens to be calling.
    """
    from unified_agent import config
    from unified_agent.tools.tasks import task_add, task_claim

    token = config.agent_identity_var.set("TestAgent")
    try:
        first = task_claim(task_add("First task")["id"], agent="TestAgent")
        second = task_claim(task_add("Second task")["id"], agent="TestAgent")
    finally:
        config.agent_identity_var.reset(token)

    first_wt = workspace_repo / first["worktree_path"]
    assert first_wt.is_dir()

    assert second["worktree_path"] == str(
        workspace_repo.resolve() / f".worktrees/task-{second['id']}"
    )
    assert (workspace_repo / second["worktree_path"]).is_dir()
    assert not (first_wt / ".worktrees").exists(), (
        "second worktree nested inside the first agent's active worktree"
    )


def test_task_delete_releases_a_clean_worktree(workspace_repo):
    """Deleting a task must not leave its worktree behind as an orphan.

    Source fix for task #187: `.worktrees/task-183` and `task-184` outlived
    the probe tasks that made them. Once the row is gone nothing can identify
    the directory, so it accumulates as `orphaned_task` forever.
    """
    from unified_agent.tools.tasks import task_add, task_claim, task_delete

    task_id = task_claim(task_add("Disposable probe")["id"], agent="TestAgent")["id"]
    wt = workspace_repo / f".worktrees/task-{task_id}"
    assert wt.is_dir()

    res = task_delete(task_id)

    assert res["deleted"] is True
    assert res["worktrees"] == {str(wt.resolve()): "removed"}
    assert not wt.exists()


def test_task_delete_keeps_a_worktree_holding_work(workspace_repo):
    """Row deletion is bookkeeping; it must never be able to destroy code."""
    from unified_agent.tools.tasks import task_add, task_claim, task_delete

    task_id = task_claim(task_add("Probe with work")["id"], agent="TestAgent")["id"]
    wt = workspace_repo / f".worktrees/task-{task_id}"
    precious = wt / "uncommitted.py"
    precious.write_text("# work in progress\n")

    res = task_delete(task_id)

    assert res["deleted"] is True
    assert res["worktrees"] == {str(wt.resolve()): "retained:uncommitted_changes"}
    assert precious.read_text() == "# work in progress\n"


def test_task_delete_also_deletes_the_released_branch(workspace_repo):
    """Regression (task #315): task_delete's worktree-cleanup query never
    selected `branch`, so the released worktree's branch was left behind —
    the exact leak class task #194 fixed, reintroduced via the delete path
    instead of task_update(status="done")'s equivalent (already-correct) path.
    """
    from unified_agent.tools.tasks import task_add, task_claim, task_delete

    task_id = task_claim(task_add("Disposable probe")["id"], agent="TestAgent")["id"]
    branch = f"task/{task_id}-disposable-probe"
    branches_before = subprocess.run(
        ["git", "branch", "--list", branch], cwd=workspace_repo, capture_output=True, text=True
    ).stdout
    assert branch in branches_before

    res = task_delete(task_id)

    assert res["worktrees"] == {
        str(workspace_repo.resolve() / f".worktrees/task-{task_id}"): "removed"
    }
    branches_after = subprocess.run(
        ["git", "branch", "--list", branch], cwd=workspace_repo, capture_output=True, text=True
    ).stdout
    assert branch not in branches_after, "task_delete left the released worktree's branch behind"


def test_git_worktree_flow(mock_git_repo_with_commit):
    # 1. Check initial list (contains only the main worktree)
    res_list = git_worktree_list()
    assert len(res_list["worktrees"]) == 1
    assert res_list["worktrees"][0]["branch"] == "main"

    # 2. Add a concurrent worktree
    # We create a worktree folder path relative to the repo
    res_add = git_worktree_add("dev-worktree", "feature-branch")
    assert res_add["branch"] == "feature-branch"
    assert "dev-worktree" in res_add["path"]

    # 3. Check updated list (should contain 2 worktrees)
    res_list2 = git_worktree_list()
    assert len(res_list2["worktrees"]) == 2
    branches = {w.get("branch") for w in res_list2["worktrees"]}
    assert "main" in branches
    assert "feature-branch" in branches

    # 4. Remove worktree
    res_remove = git_worktree_remove("dev-worktree")
    assert res_remove["deleted"] is True

    # Check list again (should be back to 1)
    res_list3 = git_worktree_list()
    assert len(res_list3["worktrees"]) == 1


@pytest.fixture
def git_call_recorder(monkeypatch):
    """Records every `git` subprocess invocation's (cwd, argv) for the test's duration.

    Task #314: a real full-suite run once mutated the actual host checkout
    (a branch was deleted, HEAD force-switched) — caught after the fact by
    conftest.py's session-scoped `host_repo_unchanged` guard, which can only
    report *that* something escaped, not *which* call did it. Investigation
    (instrumenting every `subprocess.run` git call across two full clean
    1721-test runs) found zero calls with a cwd outside an isolated tmp
    workspace anywhere in this codebase's current git-invoking paths — every
    one resolves through `safety.resolve_in_workspace()` or
    `config.workspace_root()`, both of which honor UA_WORKSPACE, and the
    known incident is most consistent with an external, concurrent process
    in this repo's documented shared checkout (memory
    `antigravity-claude-shared-checkout-collision-2026-07-22`), not a code
    defect. This fixture turns that one-off instrumentation into a permanent
    regression net: it does not target one hypothesized bug, it proves the
    *class* of bug (a git call whose cwd silently falls through to the real
    process cwd) cannot reappear undetected in the flows that matter most —
    the ones that create/remove worktrees and delete branches.
    """
    calls = []
    real_run = subprocess.run

    def _recording_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] in ("git", "/usr/bin/git"):
            calls.append({"cwd": kwargs.get("cwd"), "argv": list(argv)})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recording_run)
    return calls


def _effective_git_dir(call):
    """The directory git actually operates in, or None if it is the process cwd.

    git applies each ``-C <path>`` in order, relative to the previous one,
    starting from the subprocess cwd. An absolute ``-C`` path therefore pins the
    directory even when ``cwd`` is unset. A relative one with no ``cwd`` still
    falls through to the real process cwd, which is the escape this guards.
    """
    argv = call["argv"]
    base = Path(call["cwd"]) if call["cwd"] is not None else None
    for i, arg in enumerate(argv[1:-1], start=1):
        if arg == "-C":
            target = Path(argv[i + 1])
            if target.is_absolute():
                base = target
            elif base is not None:
                base = base / target
    return base


def _assert_every_call_stayed_inside(calls, allowed_root):
    allowed_root = allowed_root.resolve()
    assert calls, "expected at least one git subprocess call to have been made"
    escaped = []
    for c in calls:
        where = _effective_git_dir(c)
        if where is None or not where.resolve().is_relative_to(allowed_root):
            escaped.append(c)
    assert escaped == [], f"git call(s) escaped the isolated workspace: {escaped}"


class TestGitCallsStayInWorkspace:
    """Task #314 regression coverage: every git call made by worktree/branch
    lifecycle tools stays confined to the test's own isolated workspace."""

    def test_worktree_add_and_remove_stay_confined(
        self, mock_git_repo_with_commit, git_call_recorder
    ):
        git_worktree_add("dev-worktree", "feature-branch")
        git_worktree_remove("dev-worktree")
        _assert_every_call_stayed_inside(git_call_recorder, mock_git_repo_with_commit)

    def test_task_claim_then_done_stays_confined(self, workspace_repo, git_call_recorder):
        """Exercises the full worktree-create -> worktree-release -> branch-delete
        chain (_ensure_task_worktree, then task_update(status="done") ->
        _release_task_worktree -> _delete_task_branch with a real branch name)
        — the exact call path task #314's investigation flagged as the one
        place (`_delete_task_branch`) that reads `config.workspace_root()`
        directly rather than going through `resolve_in_workspace()` like every
        other git-invoking tool in this codebase. NOTE: `task_delete()` does
        NOT exercise this path — its worktree-cleanup query never selects
        `branch`, so `_delete_task_branch` always gets `None` there (a
        separate, filed-not-fixed finding, see task #314 notes). `task_update`
        to `done` is the real path; it's also what actually deletes the
        branch in this codebase, and what this session used repeatedly."""
        from unified_agent.tools.tasks import task_add, task_claim, task_update

        task_id = task_claim(task_add("Disposable probe")["id"], agent="TestAgent")["id"]
        task_update(task_id, status="done")
        _assert_every_call_stayed_inside(git_call_recorder, workspace_repo)

    def test_cleanup_stale_worktrees_stays_confined(self, workspace_repo, git_call_recorder):
        from unified_agent import storage
        from unified_agent.tools.tasks import task_add, task_claim
        from unified_agent.tools.worktree_policy import cleanup_stale_worktrees

        task_id = task_claim(task_add("Stale probe")["id"], agent="TestAgent")["id"]

        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        cleanup_stale_worktrees(max_idle_days=0, dry_run=False)
        _assert_every_call_stayed_inside(git_call_recorder, workspace_repo)


def test_worktree_add_rejects_option_like_branch(mock_git_repo_with_commit):
    # A branch name starting with "-" would be parsed as a git option
    for bad in ("--force", "-b", "-", "feature branch"):
        with pytest.raises(ToolError, match="invalid branch name"):
            git_worktree_add("wt-bad", bad)


def test_auto_worktree_on_task_doing_and_claim(workspace_repo):
    from unified_agent.tools.tasks import task_add, task_claim

    # Create a task
    t = task_add("Add auto worktree support")
    task_id = t["id"]

    # Claim task
    claimed = task_claim(task_id, agent="TestAgent")
    assert claimed["status"] == "doing"
    assert claimed["worktree_path"] == str(workspace_repo.resolve() / f".worktrees/task-{task_id}")
    assert claimed["branch"] == f"task/{task_id}-add-auto-worktree-support"

    # Verify worktree was registered
    res_list = git_worktree_list()
    branches = {w.get("branch") for w in res_list["worktrees"]}
    assert f"task/{task_id}-add-auto-worktree-support" in branches


class TestEffectiveGitDir:
    """The confinement check must still catch a call that falls through."""

    def test_absolute_dash_c_pins_the_directory(self, tmp_path):
        call = {"cwd": None, "argv": ["git", "-C", str(tmp_path), "status"]}
        assert _effective_git_dir(call) == tmp_path

    def test_relative_dash_c_without_cwd_is_an_escape(self):
        call = {"cwd": None, "argv": ["git", "-C", "repo", "status"]}
        assert _effective_git_dir(call) is None

    def test_no_cwd_and_no_dash_c_is_an_escape(self):
        assert _effective_git_dir({"cwd": None, "argv": ["git", "status"]}) is None

    def test_relative_dash_c_resolves_against_cwd(self, tmp_path):
        call = {"cwd": str(tmp_path), "argv": ["git", "-C", "sub", "status"]}
        assert _effective_git_dir(call) == tmp_path / "sub"

    def test_escape_outside_root_is_reported(self, tmp_path):
        call = {"cwd": None, "argv": ["git", "-C", "/", "status"]}
        with pytest.raises(AssertionError, match="escaped"):
            _assert_every_call_stayed_inside([call], tmp_path)
