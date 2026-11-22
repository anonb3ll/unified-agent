import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unified_agent import config, merge, storage
from unified_agent.merge import orchestrate_merge

pytestmark = pytest.mark.smoke

# A fully-formed passing audit report (the shape run_audit returns), so the
# in-process audit seam and _format_text render without KeyErrors.
PASSING_AUDIT = {
    "root": "/tmp/worktree",
    "files_scanned": 10,
    "status": "pass",
    "summary": {"high": 0, "medium": 0, "low": 0, "info": 0, "excluded": 0},
    "findings": [],
    "truncated": False,
}


def _record_legacy_workspace_path(task_id):
    """The mocked merge fixtures represent old workspace-sourced checkouts."""
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET worktree_path=? WHERE id=?", (f".worktrees/task-{task_id}", task_id)
        )
        conn.commit()


def test_orchestrate_merge_success(tmp_path):
    conn = storage.connect()
    # Insert completed task (status='done') with created_at and updated_at
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (24, 'Test Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(24)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-24"),
        "branch": "agy/24",
        "head": "abcdef123",
    }

    # Ensure vault directory exists under test vault path
    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch(
            "unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}
        ) as mock_wt_list,
        # main_repo is a mock path here, not the real workspace root or a
        # known project repo, so the repo-identity guard (_known_repo_or_raise)
        # is bypassed the same way git_worktree_list itself is mocked away —
        # its own boundary behavior has dedicated coverage below.
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo") as mock_wt_remove,
        patch("unified_agent.merge.sync_state_to_vault") as mock_sync_vault,
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="All tests passed", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git_in_repo,
        patch(
            "unified_agent.tools.workflow.workflow_run",
            return_value={"workflow": "post-merge-verify", "status": "done", "steps": []},
        ) as mock_wf_run,
    ):
        # Task #496: the clean-tree status check is now resolved through
        # resolve_task_worktree and run via _run_git_in_repo, so it is this
        # mock's first call. The rest run against main_repo, in order:
        # checkout main, pull, merge, log, worktree list, merge-base, update-ref.
        mock_git_in_repo.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git status (worktree)
            MagicMock(returncode=0, stdout="Switched to branch main", stderr=""),  # checkout main
            MagicMock(returncode=0, stdout="Already up to date", stderr=""),  # pull
            MagicMock(returncode=0, stdout="Merge successful", stderr=""),  # merge
            MagicMock(returncode=0, stdout="mockhash123", stderr=""),  # log hash
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree list
            MagicMock(returncode=0, stdout="", stderr=""),  # merge-base --is-ancestor
            MagicMock(returncode=0, stdout="Deleted branch agy/24", stderr=""),  # delete branch
        ]

        res = orchestrate_merge(task_id=24)

        assert res["success"] is True
        assert res["commit_hash"] == "mockhash123"
        assert res["branch"] == "agy/24"
        assert res["post_merge_verification"] == {
            "workflow": "post-merge-verify",
            "status": "done",
            "steps": [],
        }
        mock_wf_run.assert_called_once_with(name="post-merge-verify")

        # Verify repo_path on git_worktree_list: located before the gates,
        # re-read after them and again before cleanup to prove the head held.
        assert mock_wt_list.call_count == 3
        mock_wt_list.assert_called_with(repo_path=str(config.workspace_root()))

        # Verify sync_state_to_vault was called
        mock_sync_vault.assert_called_once()

        # Verify cleanup was called
        mock_wt_remove.assert_called_once_with(
            str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-24"),
            str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-24"),
            force=False,
        )

        # Verify task is updated in DB
        conn = storage.connect()
        task = conn.execute("SELECT * FROM tasks WHERE id=24").fetchone()
        conn.close()

        assert task["status"] == "done"
        assert "[merged commit: mockhash123" in task["notes"]


def test_git_merge_task_dry_run(tmp_path):
    """The MCP tool passes dry_run through: validations run, nothing merges or deletes."""
    from unified_agent.tools.git import git_merge_task

    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (24, 'Test Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(24)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-24"),
        "branch": "agy/24",
        "head": "abcdef123",
    }

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._worktree_remove_in_repo") as mock_wt_remove,
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="All tests passed", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        patch("unified_agent.tools.workflow.workflow_run") as mock_wf,
    ):
        # Only the clean-state git status runs; dry_run returns before checkout/merge.
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout=""),  # git status
        ]

        res = git_merge_task(task_id=24, dry_run=True)

        assert res == {"success": True, "dry_run": True}
        assert "post_merge_verification" not in res
        mock_wf.assert_not_called()
        mock_wt_remove.assert_not_called()

        # Task notes untouched — no merge was recorded.
        conn = storage.connect()
        task = conn.execute("SELECT * FROM tasks WHERE id=24").fetchone()
        conn.close()
        assert task["notes"] == "Initial notes"


def test_orchestrate_merge_validation_failure(tmp_path):
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (24, 'Test Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(24)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-24"),
        "branch": "agy/24",
        "head": "abcdef123",
    }

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=1, stdout="Failing test case", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
    ):
        # Only the clean-state git status runs before validation fails.
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout=""),  # git status
        ]

        with pytest.raises(RuntimeError) as exc_info:
            orchestrate_merge(task_id=24)

        assert "Validation checks failed" in str(exc_info.value)
        assert "Merge-Failed-Task-24.md" in str(exc_info.value)

        # Verify failure report is written into Obsidian inbox
        report_file = vault_dir / "00 - Inbox" / "Merge-Failed-Task-24.md"
        assert report_file.exists()

        content = report_file.read_text(encoding="utf-8")
        assert "Merge Failure Report: Task 24" in content
        assert "Failing test case" in content


def test_find_task_worktree_regex_slugs():
    """Verify that find_task_worktree handles various branch and path patterns with slugs."""
    from unified_agent.merge import find_task_worktree

    # We mock git_worktree_list to return a list of worktrees.
    # Pattern: rf"(?:^|[-_/])(?:task[-_]?)?{task_id}(?:-agy)?(?:[-_][a-zA-Z0-9-]+)?/?$"
    worktrees = [
        {"path": "/path/to/main", "branch": "main"},
        {"path": "/path/to/task-24-slug-name", "branch": "agy/task-24-slug-name"},
        {"path": "/path/to/24-agy", "branch": "task_24-agy"},
        {"path": "/path/to/task24-something-else", "branch": "agy/24-something-else"},
        {"path": "/path/to/release-24.2", "branch": "release-24.2"},  # should NOT match
    ]

    with patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": worktrees}):
        # Match task-24-slug-name
        # agy/task-24-slug-name matches branch
        wt, main = find_task_worktree(24)
        assert wt is not None
        assert wt["path"] == "/path/to/task-24-slug-name"

    # Match 24-agy / task_24-agy
    worktrees_2 = [
        {"path": "/path/to/main", "branch": "main"},
        {"path": "/path/to/24-agy", "branch": "task_24-agy"},
    ]
    with patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": worktrees_2}):
        wt, main = find_task_worktree(24)
        assert wt is not None
        assert wt["path"] == "/path/to/24-agy"

    # Match path /path/to/task24-something-else, branch agy/24-something-else
    worktrees_3 = [
        {"path": "/path/to/main", "branch": "main"},
        {"path": "/path/to/task24-something-else", "branch": "agy/24-something-else"},
    ]
    with patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": worktrees_3}):
        wt, main = find_task_worktree(24)
        assert wt is not None
        assert wt["path"] == "/path/to/task24-something-else"

    # No match for release-24.2
    worktrees_4 = [
        {"path": "/path/to/main", "branch": "main"},
        {"path": "/path/to/release-24.2", "branch": "release-24.2"},
    ]
    with patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": worktrees_4}):
        wt, main = find_task_worktree(24)
        assert wt is None


def test_find_task_worktree_falls_back_to_project_repo(monkeypatch):
    """Task #465: a worktree registered against a project repo is still found.

    A project-scoped task's worktree directory lives under the workspace root,
    but its git metadata belongs to the project's own ~/dev checkout, so
    `git worktree list` in the workspace does not list it. Before this fix the
    reviewer reported "no git worktree found" for every such task.
    """
    from unified_agent.merge import find_task_worktree

    project_wt = {"path": "/ws/.worktrees/task-77", "branch": "task/77-slug"}
    project_main = {"path": "/dev/proj", "branch": "main"}

    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws", "branch": "master"}]},
        ),
        patch("unified_agent.config.project_repo_roots", return_value=[Path("/dev/proj")]),
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[project_main, project_wt],
        ) as mock_list,
    ):
        wt, main = find_task_worktree(77)

    assert wt == project_wt
    assert main == "/dev/proj"
    mock_list.assert_called_once_with(Path("/dev/proj"))


def test_find_task_worktree_skips_project_repos_on_workspace_hit():
    """The project-repo lookup is deferred: a workspace hit costs no subprocess."""
    from unified_agent.merge import find_task_worktree

    workspace_wt = {"path": "/ws/.worktrees/task-78", "branch": "task/78-slug"}

    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws", "branch": "master"}, workspace_wt]},
        ),
        patch("unified_agent.merge._worktrees_of_project_repo") as mock_list,
    ):
        wt, _main = find_task_worktree(78)

    assert wt == workspace_wt
    mock_list.assert_not_called()


def test_find_task_worktree_tolerates_a_non_git_workspace():
    """UA_WORKSPACE is allowed to be a non-git cockpit directory (CLAUDE.md
    topology). Before this fix, git_worktree_list's ToolError on such a
    workspace propagated straight out of find_task_worktree, so the
    project-repo fallback below was never reached and every project-scoped
    task read as "no git worktree found" (task #467 / PR #202 review)."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.merge import find_task_worktree

    project_wt = {"path": "/ws/.worktrees/task-77", "branch": "task/77-slug"}
    project_main = {"path": "/dev/proj", "branch": "main"}

    with (
        patch("unified_agent.merge.git_worktree_list", side_effect=ToolError("not a git repo")),
        patch("unified_agent.config.project_repo_roots", return_value=[Path("/dev/proj")]),
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[project_main, project_wt],
        ),
    ):
        wt, main = find_task_worktree(77)

    assert wt == project_wt
    assert main == "/dev/proj"


def _insert_routed_task(task_id: int, notes: str, project: str = "unified-agent") -> None:
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, project, created_at, updated_at) "
        "VALUES (?, 'Routed', 'doing', 'high', ?, ?, '2026-10-02T00:00:00Z', "
        "'2026-10-02T00:00:00Z')",
        (task_id, notes, project),
    )
    conn.commit()
    conn.close()


def _product_checkout(tmp_path) -> Path:
    """A checked-out default-project repo under the isolated UA_DEV_ROOT."""
    repo = tmp_path / "dev" / "unified_agent"
    repo.mkdir(parents=True)
    return repo.resolve()


def test_find_task_worktree_product_task_never_uses_same_named_workspace_worktree(tmp_path):
    """Task #1844: a [code-task] row of the default project is product code.

    The workspace (cockpit) repo held a claim-minted `.worktrees/task-<id>`
    with no product code in it. Searching the workspace first returned that
    checkout, and the review gates ran pytest there for a false failure. A
    product-sourced task is looked up only in its own repo; a miss there is a
    miss, so the reviewer's no-worktree fallbacks apply instead.
    """
    from unified_agent.merge import find_task_worktree

    product = _product_checkout(tmp_path)
    _insert_routed_task(1636, "[code-task] product fix")
    cockpit_wt = {"path": "/ws/.worktrees/task-1636", "branch": "task/1636-slug"}

    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws", "branch": "master"}, cockpit_wt]},
        ) as ws_list,
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[{"path": str(product), "branch": "main"}],
        ) as product_list,
    ):
        wt, main = find_task_worktree(1636)

    assert wt is None
    assert main == str(product)
    product_list.assert_called_once_with(product)
    ws_list.assert_not_called()


def test_find_task_worktree_product_task_with_no_checkout_never_uses_workspace(tmp_path):
    """PR #1000 review: a product-sourced task whose checkout is absent on this
    host is still not cockpit code; a same-numbered workspace worktree must not
    be validated in its place."""
    from unified_agent.merge import find_task_worktree

    _insert_routed_task(1639, "[code-task] product fix")
    cockpit_wt = {"path": "/ws/.worktrees/task-1639", "branch": "task/1639-slug"}

    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws", "branch": "master"}, cockpit_wt]},
        ) as ws_list,
        patch("unified_agent.merge._worktrees_of_project_repo") as product_list,
    ):
        wt, _main = find_task_worktree(1639)

    assert wt is None
    product_list.assert_not_called()
    ws_list.assert_not_called()


def test_find_task_worktree_absent_product_checkout_trusts_legacy_relative_workspace_path(tmp_path):
    """PR #1000 review: with the product checkout absent, claim time cut the
    worktree from the workspace. That recorded checkout is the task's; a
    different same-numbered workspace worktree still is not."""
    from unified_agent.merge import find_task_worktree

    _insert_routed_task(1641, "[code-task] product fix")
    recorded = tmp_path / "ws" / ".worktrees" / "task-1641"
    conn = storage.connect()
    conn.execute(
        "UPDATE tasks SET worktree_path=?, branch=? WHERE id=?",
        (".worktrees/task-1641", "task/1641-x", 1641),
    )
    conn.commit()
    conn.close()
    claimed = {"path": str(recorded), "branch": "task/1641-x"}
    other = {"path": "/ws/.worktrees/task-1641-old", "branch": "task/1641-old"}
    ws_main = {"path": "/ws", "branch": "master"}

    with (
        patch("unified_agent.merge.resolve_task_worktree", return_value=recorded),
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [ws_main, other, claimed]},
        ),
    ):
        assert find_task_worktree(1641) == (claimed, "/ws")

    with (
        patch("unified_agent.merge.resolve_task_worktree", return_value=recorded),
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [ws_main, other]},
        ),
    ):
        assert find_task_worktree(1641) == (None, None)


def test_find_task_worktree_product_task_found_in_its_own_repo(tmp_path):
    """The routed repo's worktree wins even when the workspace has one too."""
    from unified_agent.merge import find_task_worktree

    product = _product_checkout(tmp_path)
    _insert_routed_task(1637, "[code-task] product fix")
    product_wt = {"path": str(product / ".worktrees" / "task-1637"), "branch": "task/1637-x"}

    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws/.worktrees/task-1637", "branch": "t"}]},
        ),
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[{"path": str(product), "branch": "main"}, product_wt],
        ),
    ):
        wt, main = find_task_worktree(1637)

    assert wt == product_wt
    assert main == str(product)


def test_find_task_worktree_unmarked_default_task_prefers_product_then_workspace(tmp_path):
    """An unmarked default-project row is workspace-sourced at claim time.

    Its product work, when present, is still in the default project's repo,
    so that repo is searched first; a genuine cockpit task (nothing for its
    id in the product repo) keeps resolving to the workspace worktree.
    """
    from unified_agent.merge import find_task_worktree

    product = _product_checkout(tmp_path)
    _insert_routed_task(1638, "plain notes")
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET worktree_path=? WHERE id=1638", (".worktrees/task-1638",))
        conn.commit()
    cockpit_wt = {"path": "/ws/.worktrees/task-1638", "branch": "task/1638-slug"}
    product_wt = {"path": str(product / ".worktrees" / "task-1638"), "branch": "codex/1638-x"}
    ws = {"worktrees": [{"path": "/ws", "branch": "master"}, cockpit_wt]}

    with (
        patch("unified_agent.merge.git_worktree_list", return_value=ws),
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[{"path": str(product), "branch": "main"}, product_wt],
        ),
    ):
        assert find_task_worktree(1638) == (product_wt, str(product))

    with (
        patch("unified_agent.merge.git_worktree_list", return_value=ws),
        patch(
            "unified_agent.merge._worktrees_of_project_repo",
            return_value=[{"path": str(product), "branch": "main"}],
        ),
    ):
        assert find_task_worktree(1638) == (cockpit_wt, "/ws")


@pytest.mark.parametrize(
    "text, task_id",
    [
        ("/r/.worktrees/task-1844", 1844),
        ("task/1844-review-pipeline-repo", 1844),
        ("claude/1457-autonomy-modes", 1457),
        ("/r/.worktrees/claude-1457-autonomy-modes", 1457),
        ("/r/.worktrees/cursor-1357", 1357),
        ("steward/1689", 1689),
        ("cursor/1440-1442-appliance-packaging", 1442),
        ("task-1755-design-system-build", 1755),
        ("task_24-agy", 24),
        ("/path/to/task24-something-else", 24),
        ("agy/24", 24),
    ],
)
def test_names_task_id_accepts_task_naming_forms(text, task_id):
    from unified_agent.merge import names_task_id

    assert names_task_id(text, task_id)


@pytest.mark.parametrize(
    "text, task_id",
    [
        ("pr-994", 994),
        ("/r/.worktrees/pr-994", 994),
        ("pr/994", 994),
        ("pr_994", 994),
        ("pull/994-head", 994),
        ("/r/.worktrees/pr968", 968),
        ("/r/.worktrees/gate-960-3c09db7a", 960),
        ("/r/.worktrees/reland-858", 858),
        ("/r/.worktrees/deploy-40fb7195", 40),
        ("/r/.worktrees/codex-review-1559-pr906", 906),
        ("release-24.2", 24),
        ("task/1844-review-pipeline-repo", 184),
    ],
)
def test_names_task_id_rejects_pr_numbers_and_other_ids(text, task_id):
    """Task #1844: a number after a PR-style word is a PR number, not a task id."""
    from unified_agent.merge import names_task_id

    assert not names_task_id(text, task_id)


def test_names_task_id_strict_form_refuses_a_slug():
    from unified_agent.merge import names_task_id

    assert names_task_id("/r/.worktrees/wt-77", 77, slug=False)
    assert not names_task_id("task/77-fix", 77, slug=False)


def test_find_task_worktree_skips_pr_numbered_worktree():
    """Task #1844: `.worktrees/pr-<N>` (a PR review checkout) is never task N's
    worktree, even when no other worktree matches."""
    from unified_agent.merge import find_task_worktree

    pr_wt = {"path": "/ws/.worktrees/pr-994", "branch": "pr-994"}
    with (
        patch(
            "unified_agent.merge.git_worktree_list",
            return_value={"worktrees": [{"path": "/ws", "branch": "master"}, pr_wt]},
        ),
        patch("unified_agent.config.project_repo_roots", return_value=[]),
    ):
        wt, _main = find_task_worktree(994)

    assert wt is None


def test_run_pytest_gate_env_excludes_host_approval_policy(tmp_path, monkeypatch):
    """Task #1844: the gate's pytest run must not inherit the hub's approval
    policy, neither from its process env nor from its settings.toml."""
    from unified_agent.merge import _run_pytest

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "delegate:fs_write")
    monkeypatch.setenv("UA_APPROVAL_BASELINE", "1")
    monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "48")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    _run_pytest(str(tmp_path))

    env = captured["env"]
    assert not any(key.startswith("UA_APPROVAL") for key in env)
    assert env["UA_SETTINGS_FILE"] == ""
    assert set(env) == {"PATH", "HOME", "LANG", "UA_SETTINGS_FILE"}


def test_known_repo_or_raise_accepts_workspace_and_project_repos_only(monkeypatch):
    """The narrow unsandboxed git runner refuses anything but the workspace
    root or a config.project_repo_roots() entry — main_repo always comes from
    find_task_worktree, never directly from caller input, but the guard
    enforces that invariant at the point of use."""
    from unified_agent.merge import _known_repo_or_raise

    project_repo = Path("/dev/known-proj")
    monkeypatch.setattr(config, "project_repo_roots", lambda: [project_repo])

    assert _known_repo_or_raise(str(config.workspace_root())) == str(config.workspace_root())
    assert _known_repo_or_raise(str(project_repo)) == str(project_repo)
    with pytest.raises(Exception, match="unrecognized repo path"):
        _known_repo_or_raise("/tmp/some-arbitrary-path")


def test_validation_reports_no_tests_collected_distinctly(tmp_path):
    """Task #466: pytest exit 5 reads as "No tests were collected", not a failure.

    The gate must still FAIL — an empty suite validates nothing — but the
    reason a human reads has to say which of the two happened.
    """
    from unified_agent.merge import PYTEST_NO_TESTS_COLLECTED, run_validation_gates

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(
                returncode=PYTEST_NO_TESTS_COLLECTED,
                stdout="collected 0 items\n\nno tests ran in 0.01s",
                stderr="",
            ),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo", return_value=MagicMock(returncode=0, stdout="")
        ),
        patch("unified_agent.merge.resolve_task_worktree", side_effect=lambda p: Path(p)),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            run_validation_gates(task_id=79, wt_path=str(tmp_path))

    message = str(exc_info.value)
    assert "No tests were collected" in message
    assert "Unit tests failed" not in message


def test_validation_still_reports_real_test_failures(tmp_path):
    """Exit 1 keeps the original wording — the fix must not blur the other way."""
    from unified_agent.merge import run_validation_gates

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=1, stdout="1 failed, 3 passed", stderr=""),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo", return_value=MagicMock(returncode=0, stdout="")
        ),
        patch("unified_agent.merge.resolve_task_worktree", side_effect=lambda p: Path(p)),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            run_validation_gates(task_id=80, wt_path=str(tmp_path))

    message = str(exc_info.value)
    assert "Unit tests failed" in message
    assert "No tests were collected" not in message


def test_run_pytest_uses_configured_timeout(tmp_path, monkeypatch):
    """Task #1254: UA_REVIEW_PYTEST_TIMEOUT_S threads through to subprocess.run,
    replacing the old hardcoded 600s ceiling."""
    from unified_agent.merge import _run_pytest

    monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "42")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    _run_pytest(str(tmp_path))

    assert captured["timeout"] == 42


def test_run_pytest_default_timeout_is_1800_not_600(tmp_path, monkeypatch):
    """Task #1254: the old ceiling (600s) was below observed full-suite runtime
    (447-488s with no margin as the suite kept growing); the new default (1800s)
    replaces it when UA_REVIEW_PYTEST_TIMEOUT_S is unset."""
    from unified_agent.merge import _run_pytest

    monkeypatch.delenv("UA_REVIEW_PYTEST_TIMEOUT_S", raising=False)
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    _run_pytest(str(tmp_path))

    assert captured["timeout"] == 1800


def test_run_pytest_timeout_raises_dedicated_exception(tmp_path, monkeypatch):
    """Task #1254: a real timeout must raise PytestTimeoutError — a distinct,
    catchable type — not a bare RuntimeError indistinguishable from a genuine
    test failure, which is what let the review pipeline post false FAILED
    verdicts (7 instances 09-20..09-22, two later merged green)."""
    from unified_agent.merge import PytestTimeoutError, _run_pytest

    monkeypatch.setenv("UA_REVIEW_PYTEST_TIMEOUT_S", "5")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    with pytest.raises(PytestTimeoutError) as exc_info:
        _run_pytest(str(tmp_path))

    # Still a RuntimeError so any caller that only catches that keeps working.
    assert isinstance(exc_info.value, RuntimeError)
    assert "5 seconds" in str(exc_info.value)


def test_validation_gates_propagate_pytest_timeout_distinctly(tmp_path):
    """Task #1254: run_validation_gates must let PytestTimeoutError pass through
    as its own type (not folded into the generic 'Unit tests failed' message),
    since reviewer.py branches on the type to avoid a false FAILED verdict."""
    from unified_agent.merge import PytestTimeoutError, run_validation_gates

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            side_effect=PytestTimeoutError("pytest timed out after 1800 seconds"),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo", return_value=MagicMock(returncode=0, stdout="")
        ),
        patch("unified_agent.merge.resolve_task_worktree", side_effect=lambda p: Path(p)),
    ):
        with pytest.raises(PytestTimeoutError) as exc_info:
            run_validation_gates(task_id=81, wt_path=str(tmp_path))

    assert "timed out" in str(exc_info.value)


def test_bun_projects_use_their_native_test_runner(tmp_path, monkeypatch):
    from unified_agent.merge import _run_tests

    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "bun test tests/"}}', encoding="utf-8"
    )
    monkeypatch.setattr("unified_agent.merge.shutil.which", lambda name: "/usr/bin/bun")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    with patch("unified_agent.merge._run_bun_test") as run_bun:
        _run_tests(str(tmp_path))
    run_bun.assert_called_once_with(str(tmp_path), ["test", "tests/"])


def test_bun_runner_does_not_inherit_process_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr("unified_agent.merge.shutil.which", lambda name: "/usr/bin/bun")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    result = merge._run_bun_test(str(tmp_path), ["test"])

    assert result.returncode == 0
    assert "OPENAI_API_KEY" not in captured["env"]


def test_run_bun_test_default_timeout_is_600(tmp_path, monkeypatch):
    """Task #1295 follow-up to #1254: the bun-test ceiling stays 600s by
    default (unlike pytest's, which #1254 raised to 1800) — nothing in this
    task suggested 600s was wrong for bun, only that it couldn't be changed."""
    monkeypatch.delenv("UA_REVIEW_BUN_TEST_TIMEOUT_S", raising=False)
    monkeypatch.setattr("unified_agent.merge.shutil.which", lambda name: "/usr/bin/bun")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    merge._run_bun_test(str(tmp_path), ["test"])

    assert captured["timeout"] == 600


def test_run_bun_test_uses_configured_timeout(tmp_path, monkeypatch):
    """Task #1295: UA_REVIEW_BUN_TEST_TIMEOUT_S threads through to subprocess.run,
    replacing the old hardcoded 600s with no way to raise it."""
    monkeypatch.setenv("UA_REVIEW_BUN_TEST_TIMEOUT_S", "42")
    monkeypatch.setattr("unified_agent.merge.shutil.which", lambda name: "/usr/bin/bun")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    merge._run_bun_test(str(tmp_path), ["test"])

    assert captured["timeout"] == 42


def test_run_bun_test_timeout_raises_dedicated_exception(tmp_path, monkeypatch):
    """Task #1295: a real bun-test timeout must raise BunTestTimeoutError — a
    distinct, catchable type sharing merge.TestTimeoutError with
    PytestTimeoutError — not a bare RuntimeError indistinguishable from a
    genuine bun-test failure (the same false-FAILED-verdict bug #1254 fixed
    for pytest)."""
    from unified_agent.merge import BunTestTimeoutError, TestTimeoutError

    monkeypatch.setenv("UA_REVIEW_BUN_TEST_TIMEOUT_S", "5")
    monkeypatch.setattr("unified_agent.merge.shutil.which", lambda name: "/usr/bin/bun")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)

    with pytest.raises(BunTestTimeoutError) as exc_info:
        merge._run_bun_test(str(tmp_path), ["test"])

    # Still a TestTimeoutError/RuntimeError so callers that only catch one of
    # those (reviewer.py's _test_timeout_cause, or a generic handler) keep working.
    assert isinstance(exc_info.value, TestTimeoutError)
    assert isinstance(exc_info.value, RuntimeError)
    assert "5 seconds" in str(exc_info.value)


def test_validation_gates_propagate_bun_timeout_distinctly(tmp_path):
    """Task #1295: run_validation_gates must let BunTestTimeoutError pass
    through as its own type, the same as PytestTimeoutError — reviewer.py
    branches on it to avoid a false FAILED verdict for a bun-based project."""
    from unified_agent.merge import BunTestTimeoutError, run_validation_gates

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            side_effect=BunTestTimeoutError("bun test timed out after 600 seconds"),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo", return_value=MagicMock(returncode=0, stdout="")
        ),
        patch("unified_agent.merge.resolve_task_worktree", side_effect=lambda p: Path(p)),
    ):
        with pytest.raises(BunTestTimeoutError) as exc_info:
            run_validation_gates(task_id=81, wt_path=str(tmp_path))

    assert "timed out" in str(exc_info.value)


class TestProjectWorktreeValidation:
    """Task #496: the gates must run inside a project-sourced worktree.

    Since #371 a `[code-task]` worktree is deliberately registered against its
    project's own ~/dev checkout and can live under `<project>/.worktrees/`.
    Every gate runner resolved its cwd through `resolve_in_workspace`, which
    refuses that path by construction, so a complete and green task was stamped
    `[review: rejected]` — a claim that the gates found a problem, when in fact
    they never ran.
    """

    @pytest.fixture
    def project_worktree(self, tmp_path, isolated_env, monkeypatch):
        repo = tmp_path / "dev" / "unified_agent"
        wt = repo / ".worktrees" / "task-494"
        wt.mkdir(parents=True)
        monkeypatch.setattr(config, "project_repo_roots", lambda: [repo])
        return wt

    def test_gates_resolve_a_project_sourced_worktree(self, project_worktree):
        """The regression: this raised ToolError before the fix."""
        from unified_agent.safety import resolve_task_worktree

        assert resolve_task_worktree(str(project_worktree)) == project_worktree.resolve()

    def test_clean_tree_gate_runs_in_a_project_worktree(self, project_worktree):
        """run_validation_gates reaches real git output instead of a path error."""
        from unified_agent.merge import run_validation_gates

        with (
            patch(
                "unified_agent.merge._run_git_in_repo",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as git,
            patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
            patch(
                "unified_agent.merge._run_tests",
                return_value=subprocess.CompletedProcess([], 0, "ok", ""),
            ),
        ):
            run_validation_gates(494, str(project_worktree))

        assert git.call_args[0][1] == str(project_worktree.resolve())

    def test_a_project_repo_root_itself_is_refused(self, tmp_path, project_worktree):
        """Only disposable per-task worktrees qualify, never the live checkout."""
        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent.safety import resolve_task_worktree

        repo = tmp_path / "dev" / "unified_agent"
        for bad in (repo, repo / ".worktrees", repo / "src"):
            with pytest.raises(ToolError, match="not a task worktree"):
                resolve_task_worktree(str(bad))

    def test_an_unrelated_path_is_refused(self, tmp_path, project_worktree):
        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent.safety import resolve_task_worktree

        with pytest.raises(ToolError, match="not a task worktree"):
            resolve_task_worktree(str(tmp_path / "elsewhere" / "task-1"))

    def test_the_agent_write_sandbox_is_unchanged(self, project_worktree):
        """#496's hard boundary, asserted rather than assumed.

        Widening the *gates* must not widen what an agent can touch. fs_write
        and run_cmd still refuse the very path the gates now accept.
        """
        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent.safety import resolve_in_workspace
        from unified_agent.tools.files import fs_write
        from unified_agent.tools.shell import run_cmd

        target = str(project_worktree / "pwned.txt")
        with pytest.raises(ToolError, match="escapes the workspace"):
            resolve_in_workspace(target)
        with pytest.raises(ToolError, match="escapes the workspace"):
            fs_write(target, "nope\n")
        with pytest.raises(ToolError, match="escapes the workspace"):
            run_cmd("git status", cwd=str(project_worktree))
        assert not (project_worktree / "pwned.txt").exists()


def test_orchestrate_merge_invokes_post_merge_verify(tmp_path):
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (101, 'Verify Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(101)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-101"),
        "branch": "agy/101",
        "head": "abcdef101",
    }
    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    wf_result = {
        "workflow": "post-merge-verify",
        "status": "done",
        "steps": [
            {"name": "run_tests", "tool": "run_cmd", "status": "done"},
            {"name": "audit", "tool": "security_audit", "status": "done"},
            {"name": "doctor", "tool": "system_doctor", "status": "done"},
        ],
        "outputs": {},
    }

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        patch("unified_agent.tools.workflow.workflow_run", return_value=wf_result) as mock_wf_run,
    ):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="hash101"),
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree list
            MagicMock(returncode=0, stdout="", stderr=""),  # merge-base --is-ancestor
            MagicMock(returncode=0, stdout=""),
        ]

        res = orchestrate_merge(task_id=101)
        assert res["success"] is True
        assert res["commit_hash"] == "hash101"
        assert res["post_merge_verification"] == wf_result
        mock_wf_run.assert_called_once_with(name="post-merge-verify")


def test_post_merge_verify_inherits_authenticated_caller(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:

        def run_workflow(**kwargs):
            assert kwargs == {"name": "post-merge-verify"}
            assert config.verified_agent_identity() == "Desktop_Agent1"
            return {"workflow": "post-merge-verify", "status": "done", "steps": []}

        with patch("unified_agent.tools.workflow.workflow_run", side_effect=run_workflow) as run:
            result = merge._run_post_merge_verify(task_id=101)

        assert result["status"] == "done"
        run.assert_called_once_with(name="post-merge-verify")
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("word", ["0", "false", "no", "off", "OFF", " Off "])
def test_post_merge_verify_off_words_disable_hook(monkeypatch, word):
    monkeypatch.setenv("UA_POST_MERGE_VERIFY", word)
    with patch("unified_agent.tools.workflow.workflow_run") as run:
        assert merge._run_post_merge_verify(task_id=101) is None
    run.assert_not_called()


@pytest.mark.parametrize("word", ["1", "true", "yes", ""])
def test_post_merge_verify_other_values_keep_hook_on(monkeypatch, word):
    monkeypatch.setenv("UA_POST_MERGE_VERIFY", word)
    result = {"workflow": "post-merge-verify", "status": "done", "steps": []}
    with patch("unified_agent.tools.workflow.workflow_run", return_value=result) as run:
        assert merge._run_post_merge_verify(task_id=101) == result
    run.assert_called_once_with(name="post-merge-verify")


@pytest.mark.parametrize("word", ["0", "false", "no", "off"])
def test_review_trust_ci_shares_off_words(monkeypatch, word):
    monkeypatch.setenv("UA_REVIEW_TRUST_CI", word)
    assert config.review_trust_ci() is False


def test_orchestrate_merge_dry_run_does_not_invoke_workflow(tmp_path):
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (102, 'Dry Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(102)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-102"),
        "branch": "agy/102",
        "head": "abcdef102",
    }

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "unified_agent.merge._run_git_in_repo", return_value=MagicMock(returncode=0, stdout="")
        ),
        patch("unified_agent.tools.workflow.workflow_run") as mock_wf_run,
    ):
        res = orchestrate_merge(task_id=102, dry_run=True)
        assert res == {"success": True, "dry_run": True}
        assert "post_merge_verification" not in res
        mock_wf_run.assert_not_called()


def test_orchestrate_merge_workflow_exception_handled_gracefully(tmp_path):
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) VALUES (103, 'Fail Task', 'done', 'high', 'Initial notes', '2026-07-12T00:00:00Z', '2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(103)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-103"),
        "branch": "agy/103",
        "head": "abcdef103",
    }
    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        patch(
            "unified_agent.tools.workflow.workflow_run", side_effect=RuntimeError("Workflow crash")
        ),
    ):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="hash103"),
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree list
            MagicMock(returncode=0, stdout="", stderr=""),  # merge-base --is-ancestor
            MagicMock(returncode=0, stdout=""),
        ]

        res = orchestrate_merge(task_id=103)
        assert res["success"] is True
        assert res["commit_hash"] == "hash103"
        assert res["post_merge_verification"]["status"] == "failed"
        assert "Workflow crash" in res["post_merge_verification"]["error"]

        conn = storage.connect()
        task = conn.execute("SELECT * FROM tasks WHERE id=103").fetchone()
        conn.close()
        assert task["status"] == "done"
        assert "[warning: post-merge verification failed @" in task["notes"]
        assert "Workflow crash" in task["notes"]


def test_orchestrate_merge_refuses_when_pull_of_a_tracked_main_fails(tmp_path):
    """A failed pull leaves main stale or conflicted; merging onto it is refused.
    Only a main with no upstream (local-only) may skip the pull."""
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) "
        "VALUES (104, 'Pull Task', 'done', 'high', '', '2026-07-12T00:00:00Z', "
        "'2026-07-12T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(104)
    wt = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-104"),
        "branch": "agy/104",
        "head": "abcdef104",
    }
    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [wt]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch("unified_agent.merge._run_tests", return_value=MagicMock(returncode=0, stdout="")),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        pytest.raises(RuntimeError, match="pulling main"),
    ):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git status
            MagicMock(returncode=0, stdout="", stderr=""),  # checkout main
            MagicMock(returncode=1, stdout="", stderr="CONFLICT"),  # pull
            MagicMock(returncode=0, stdout="origin/main", stderr=""),  # upstream exists
        ]
        orchestrate_merge(task_id=104)
    assert [c.args[0][0] for c in mock_git.call_args_list] == [
        "status",
        "checkout",
        "pull",
        "rev-parse",
    ]


def test_delegation_closeout_on_merge(tmp_path):
    """Step 9: delegation ledger closeout annotates [review: merged] and completes delegations."""
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) "
        "VALUES (200, 'Delegated Task', 'done', 'med', 'task notes', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
    )
    # Insert a delegation row pointing at this task
    conn.execute(
        "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, status, created_at, updated_at) "
        "VALUES ('Desktop_Agent1', 'Desktop_Agent2', 200, 'test delegation spec', 'pending', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(200)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-200"),
        "branch": "agy/200",
        "head": "abc200",
    }

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="ok", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        patch(
            "unified_agent.tools.workflow.workflow_run",
            return_value={"workflow": "post-merge-verify", "status": "done", "steps": []},
        ),
    ):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git status
            MagicMock(returncode=0, stdout="", stderr=""),  # checkout main
            MagicMock(returncode=0, stdout="", stderr=""),  # pull
            MagicMock(returncode=0, stdout="", stderr=""),  # merge
            MagicMock(returncode=0, stdout="hash200", stderr=""),  # log hash
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree list
            MagicMock(returncode=0, stdout="", stderr=""),  # merge-base --is-ancestor
            MagicMock(returncode=0, stdout="", stderr=""),  # update-ref -d
        ]

        res = orchestrate_merge(task_id=200)

        assert res["success"] is True
        assert res["delegation_closeout"]["delegations_closed"] == 1

        conn = storage.connect()
        task = conn.execute("SELECT * FROM tasks WHERE id=200").fetchone()
        deleg = conn.execute("SELECT * FROM delegations WHERE task_id=200").fetchone()
        conn.close()

        assert "[review: merged @" in task["notes"]
        assert deleg["status"] == "completed"
        assert deleg["completed_at"] is not None


def test_delegation_closeout_no_delegations(tmp_path):
    """Step 9: no-op when task has no delegations — returns 0 closed."""
    conn = storage.connect()
    conn.execute(
        "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) "
        "VALUES (201, 'No Deleg Task', 'done', 'med', 'plain notes', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    _record_legacy_workspace_path(201)

    mock_worktree = {
        "path": str(tmp_path / "workspace" / ".worktrees" / "mock-worktree-201"),
        "branch": "agy/201",
        "head": "abc201",
    }

    vault_dir = Path(config.vault_root())
    vault_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("unified_agent.merge.git_worktree_list", return_value={"worktrees": [mock_worktree]}),
        patch("unified_agent.merge._known_repo_or_raise", side_effect=lambda repo: repo),
        patch("unified_agent.merge._worktree_remove_in_repo"),
        patch("unified_agent.merge.sync_state_to_vault"),
        patch("unified_agent.merge._run_audit", return_value=PASSING_AUDIT),
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="ok", stderr=""),
        ),
        patch("unified_agent.merge._run_git_in_repo") as mock_git,
        patch(
            "unified_agent.tools.workflow.workflow_run",
            return_value={"workflow": "post-merge-verify", "status": "done", "steps": []},
        ),
    ):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="hash201", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree list
            MagicMock(returncode=0, stdout="", stderr=""),  # merge-base --is-ancestor
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        res = orchestrate_merge(task_id=201)

        assert res["success"] is True
        assert res["delegation_closeout"]["delegations_closed"] == 0

        conn = storage.connect()
        task = conn.execute("SELECT * FROM tasks WHERE id=201").fetchone()
        conn.close()

        assert "[review: merged" not in task["notes"]


def test_run_pytest_targets_only_safe_files_inside_the_worktree(tmp_path, monkeypatch):
    """#1787: the review fallback runs named test files, never options or outside paths."""
    from unified_agent.merge import _run_pytest

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("unified_agent.merge.subprocess.run", fake_run)
    _run_pytest(
        str(tmp_path),
        ["tests/test_a.py", "-p", "--rootdir=/", "../escape.py", "/etc/passwd", "tests/missing.py"],
    )
    assert captured["argv"][-2:] == ["-q", "tests/test_a.py"]


def test_run_pytest_refuses_to_widen_an_empty_target_list(tmp_path, monkeypatch):
    from unified_agent.merge import _run_pytest

    monkeypatch.setattr("unified_agent.merge.resolve_task_worktree", lambda path: tmp_path)
    monkeypatch.setattr(
        "unified_agent.merge.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pytest must not run")),
    )
    with pytest.raises(RuntimeError, match="no valid test files"):
        _run_pytest(str(tmp_path), ["-x"])


def test_git_merge_task_reaches_merge_through_its_registered_orchestrator(monkeypatch):
    """#2450: tools.git no longer imports merge; merge registers its orchestrator."""
    from unified_agent import merge
    from unified_agent.tools import git

    assert git.merge_orchestrator() is merge._orchestrate
    calls = []
    monkeypatch.setattr(merge, "orchestrate_merge", lambda **kw: calls.append(kw) or {"ok": 1})
    assert git.git_merge_task(task_id=7, dry_run=True) == {"ok": 1}
    assert calls == [{"task_id": 7, "force": False, "dry_run": True, "allow_in_flight": False}]


def test_git_merge_task_refuses_when_no_orchestrator_is_loaded(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import git

    monkeypatch.setattr(git, "_merge_orchestrator", None)
    with pytest.raises(ToolError, match="not loaded in this process; nothing was merged"):
        git.git_merge_task(task_id=7, dry_run=True)
