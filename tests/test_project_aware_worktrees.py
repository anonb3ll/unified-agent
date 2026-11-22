"""Executor worktrees sourced from a task's declared project (task #371).

Before this, `_ensure_task_worktree` always sourced the new worktree from the
(deliberately non-git) cockpit workspace, regardless of a task's `project`
column — so a delegated task scoped to e.g. browser ran with zero access to
that project's actual code, "succeeding" in seconds with nothing to show.
"""

import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools import tasks as tasks_tool


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path, marker_filename, marker_content):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    (path / marker_filename).write_text(marker_content)
    _git("add", marker_filename, cwd=path)
    _git("commit", "-m", "feat: initial commit", cwd=path)


@pytest.fixture
def workspace_and_project_repo(isolated_env, monkeypatch):
    """A non-git cockpit workspace (today's real default) plus a real
    browser checkout under the isolated UA_DEV_ROOT."""
    dev_root = config.dev_root()
    _init_repo(dev_root / "browser", "exo_marker.txt", "browser source")
    storage._connection = None
    storage.init_db()
    return isolated_env, dev_root / "browser"


def test_project_scoped_task_worktree_is_sourced_from_the_project_repo(
    workspace_and_project_repo,
):
    workspace, project_repo = workspace_and_project_repo
    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")

    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is not None
    wt = Path(res["worktree_path"])
    assert wt.is_dir()
    # The proof: this file only exists in the browser repo, never in the
    # (non-git) cockpit workspace.
    assert (wt / "exo_marker.txt").read_text() == "browser source"

    assert not wt.is_relative_to(workspace)
    assert wt.is_relative_to(project_repo / ".worktrees")

    # And it really is a worktree of the project repo, not a copy — confirm
    # via `git -C <project_repo> worktree list`.
    listing = _git("worktree", "list", cwd=project_repo).stdout
    assert str(wt) in listing


def test_unchecked_out_project_falls_back_to_no_worktree(isolated_env, monkeypatch):
    """A declared project with no real ~/dev checkout must not silently
    succeed against the wrong repo; provisioning is skipped (task #2041)."""
    storage._connection = None
    storage.init_db()
    t = tasks_tool.task_add(title="tap work, not checked out", project="client-portal")

    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is None
    assert res["branch"] is None


def test_project_worktree_branch_name_is_still_validated(isolated_env, monkeypatch):
    """_git_worktree_add_from_known_project_repo must reject an invalid
    branch name exactly like the sandboxed git_worktree_add does, even though
    it bypasses tools.git entirely."""
    from unified_agent.tools.tasks import _git_worktree_add_from_known_project_repo

    dev_root = config.dev_root()
    _init_repo(dev_root / "browser", "exo_marker.txt", "browser source")

    with pytest.raises(Exception, match="invalid branch name"):
        _git_worktree_add_from_known_project_repo(
            dev_root / "browser" / ".worktrees/task-1", "; rm -rf /", dev_root / "browser"
        )


def _init_default_project_env(isolated_env):
    """A cockpit workspace and a ~/dev/unified_agent checkout, both real repos."""
    dev_root = config.dev_root()
    _init_repo(dev_root / "unified_agent", "ua_marker.txt", "unified-agent source")
    _init_repo(isolated_env, "workspace_marker.txt", "workspace source")
    storage._connection = None
    storage.init_db()


def test_code_task_marker_routes_default_project_to_the_repo(isolated_env, monkeypatch):
    """Task #467: a [code-task]-marked DEFAULT_PROJECT task gets real source.

    Before this, 84% of tasks carried DEFAULT_PROJECT and every one of them got
    a worktree cut from the cockpit — no product code, no test suite — which is
    why validation reported `collected 0 items` and audited cockpit files.
    """
    _init_default_project_env(isolated_env)

    t = tasks_tool.task_add(title="fix the parser", notes="[code-task] scope: src/parser.py")
    res = tasks_tool.task_update(t["id"], status="doing")

    wt = Path(res["worktree_path"])
    assert (wt / "ua_marker.txt").read_text() == "unified-agent source"
    assert not (wt / "workspace_marker.txt").exists()
    assert wt.is_relative_to(config.dev_root() / "unified_agent" / ".worktrees")


@pytest.mark.parametrize(
    "notes", ["", "[code-task] scope: src/parser.py", "Quotes the `[code-task]` marker."]
)
def test_claim_routes_default_project_to_its_repo_whatever_the_markers(isolated_env, notes):
    """Task #2041: the project picks the repo, never the notes' markers.

    A batch claim gave the three [code-task] rows a unified_agent worktree and
    the five unmarked ones a relative `.worktrees/task-<id>` cut from cockpit
    master, so product branches landed in the cockpit repo.
    """
    _init_default_project_env(isolated_env)
    ua_repo = config.dev_root() / "unified_agent"

    t = tasks_tool.task_add(title="product fix", notes=notes)
    res = tasks_tool.task_claim(t["id"], "Desktop_Agent2")

    wt = Path(res["worktree_path"])
    assert wt.is_absolute()
    assert wt.is_relative_to(ua_repo.resolve() / ".worktrees")
    assert (wt / "ua_marker.txt").read_text() == "unified-agent source"
    assert not (isolated_env / ".worktrees").exists()
    head = _git("rev-parse", "HEAD", cwd=ua_repo).stdout.strip()
    assert res["worktree_source_sha"] == head
    assert "provisioning" not in res


@pytest.mark.parametrize("via", ["claim", "update"])
@pytest.mark.parametrize("project", ["unified-agent", "client-portal"])
def test_no_product_checkout_skips_provisioning_instead_of_using_the_cockpit(
    isolated_env, via, project
):
    """Task #2041: with no product checkout here, a git cockpit workspace is
    never a fallback source, and no relative path is ever stored."""
    _init_repo(isolated_env, "workspace_marker.txt", "cockpit source")
    storage._connection = None
    storage.init_db()

    t = tasks_tool.task_add(title="nowhere to cut from", notes="[code-task]", project=project)
    if via == "claim":
        res = tasks_tool.task_claim(t["id"], "Desktop_Agent2")
        assert res["provisioning"] == {"status": "skipped", "reason": "no_product_repo"}
    else:
        res = tasks_tool.task_update(t["id"], status="doing")

    assert res["status"] == "doing"
    assert res["worktree_path"] is None and res["branch"] is None
    assert not (isolated_env / ".worktrees").exists()
    assert "task/" not in _git("branch", "--list", cwd=isolated_env).stdout


def test_project_with_dirname_different_from_its_key_still_routes(isolated_env, monkeypatch):
    """Task #475 regression: PROJECT_REPO_DIRNAMES must be consulted, not the
    project key used as the dirname directly.

    Every other project-routing test here uses "browser", where the project
    key and the ~/dev checkout dirname happen to be identical -- so none of
    them would have caught a dirname map that silently omits an entry.
    "sample-store" is exactly that case in production: the checkout is
    ~/dev/sample-store, not ~/dev/sample-store, and PROJECT_REPO_DIRNAMES
    lacked the mapping (task #472/#473 both observed the fallback: a
    sample-store task's worktree was silently sourced from the cockpit
    workspace instead of the real repo).
    """
    dev_root = config.dev_root()
    _init_repo(dev_root / "sample-store", "sun_marker.txt", "sun market source")
    storage._connection = None
    storage.init_db()

    t = tasks_tool.task_add(title="sun market work", notes="[code-task]", project="sample-store")
    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is not None
    wt = Path(res["worktree_path"])
    assert (wt / "sun_marker.txt").read_text() == "sun market source"


def test_named_project_routes_without_needing_the_marker(workspace_and_project_repo):
    """The marker is the DEFAULT_PROJECT tie-breaker only — a named project
    still routes on its own, unmarked, exactly as task #371 shipped it.

    Task #994 rework: a prior pass on this branch narrowed routing to
    marker-only for every project, which silently stopped the reconciler
    fetching a named project's repo for markerless rows
    (test_reconcile_uses_project_repo_not_cockpit_workspace regressed).
    Restored to the original policy.
    """
    _workspace, project_repo = workspace_and_project_repo

    t = tasks_tool.task_add(title="browser work, unmarked", project="browser")
    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is not None
    wt = Path(res["worktree_path"])
    assert (wt / "exo_marker.txt").read_text() == "browser source"
    assert wt.is_relative_to(project_repo / ".worktrees")


def test_completing_a_project_sourced_task_releases_its_worktree(workspace_and_project_repo):
    """task_update(status="done") must actually remove a project-sourced
    worktree, not just report a status.

    Before this fix, `_release_task_worktree` always called the sandboxed
    `git.git_worktree_remove`, which only ever targets the workspace repo —
    but a project-sourced worktree's git metadata is registered against the
    project repo, so removal was refused and the checkout and branch were
    silently abandoned (task #467 / PR #203 review).
    """
    _workspace, project_repo = workspace_and_project_repo

    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")
    claimed = tasks_tool.task_update(t["id"], status="doing")
    wt = Path(claimed["worktree_path"])
    assert wt.is_dir()

    result = tasks_tool.task_update(
        t["id"],
        status="done",
        actor="test-agent",
        evidence_ref="0f1e2d3c4b5a69788796a5b4c3d2e1f009182736",
    )

    assert not wt.exists()
    assert result["worktree_path"] is None
    listing = _git("worktree", "list", cwd=project_repo).stdout
    assert str(wt) not in listing
    branches = _git("branch", "--list", claimed["branch"], cwd=project_repo).stdout
    assert claimed["branch"] not in branches


def test_deleting_a_project_sourced_task_releases_its_worktree(workspace_and_project_repo):
    """task_delete must route removal through the owning project repo too."""
    _workspace, project_repo = workspace_and_project_repo

    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")
    claimed = tasks_tool.task_update(t["id"], status="doing")
    wt = Path(claimed["worktree_path"])
    assert wt.is_dir()

    result = tasks_tool.task_delete(t["id"])

    assert result["worktrees"].get(claimed["worktree_path"]) == "removed"
    assert not wt.exists()
    listing = _git("worktree", "list", cwd=project_repo).stdout
    assert str(wt) not in listing


def test_unbound_project_worktree_is_only_a_warning_by_default(workspace_and_project_repo):
    """Task #1425: browser has no repo binding in this test env. Without
    the opt-in strict flag, worktree creation must proceed exactly as before
    -- check_worktree_binding only warns."""
    _workspace, project_repo = workspace_and_project_repo
    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")

    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is not None
    assert Path(res["worktree_path"]).is_dir()


def test_unbound_project_worktree_is_refused_under_strict_flag(
    workspace_and_project_repo, monkeypatch
):
    """Task #1425: UA_PROJECT_REPO_STRICT=1 turns the same unbound-repo
    situation into a hard refusal. Since #912 a binding refusal surfaces as an
    error rather than a silent "no worktree" (see
    test_task_claim_provisioning_outcomes), and no worktree is created."""
    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    _workspace, project_repo = workspace_and_project_repo
    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")

    with pytest.raises(ToolError, match="has no bound source repository"):
        tasks_tool.task_update(t["id"], status="doing")

    listing = _git("worktree", "list", cwd=project_repo).stdout
    assert len(listing.splitlines()) == 1


def test_bound_and_matching_project_worktree_creates_cleanly_under_strict_flag(
    workspace_and_project_repo, monkeypatch
):
    """The strict flag only refuses a mismatch/absence -- a correctly bound
    project with a matching origin still gets its worktree."""
    from unified_agent import project_bindings

    monkeypatch.setenv("UA_PROJECT_REPO_STRICT", "1")
    _workspace, project_repo = workspace_and_project_repo
    _git("remote", "add", "origin", "https://github.com/someowner/browser.git", cwd=project_repo)
    project_bindings.bind("browser", "someowner/browser", bound_by="test-agent")

    t = tasks_tool.task_add(title="browser work", notes="[code-task]", project="browser")
    res = tasks_tool.task_update(t["id"], status="doing")

    assert res["worktree_path"] is not None
    assert Path(res["worktree_path"]).is_dir()
