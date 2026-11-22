"""Reconciliation respects full task provenance when the product repo is absent."""

import pytest

from unified_agent import config, merge, reviewer, storage
from unified_agent.tools import tasks


def _fixture(monkeypatch, tmp_path, recorded_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    monkeypatch.setattr(config, "workspace_root", lambda: workspace)
    monkeypatch.setattr(config, "dev_root", lambda: tmp_path)
    monkeypatch.setattr(config, "project_repo_path", lambda _project: None)
    task_id = tasks.task_add("Product review", notes=reviewer.WAITING_FOR_REVIEW)["id"]
    tasks.task_update(task_id, status="waiting_review", actor="Desktop_Agent2", worktree=False)
    if recorded_path == "absolute":
        recorded_path = str(tmp_path / "unified_agent" / ".worktrees" / f"task-{task_id}")
    elif recorded_path == "legacy":
        recorded_path = f".worktrees/task-{task_id}"
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET worktree_path=?, claimed_by='Desktop_Agent1', "
            "claimed_at='2020-01-01T00:00:00+00:00', "
            "lock_expires_at='2020-01-01T01:00:00+00:00' WHERE id=?",
            (recorded_path, task_id),
        )
        conn.commit()
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    probes = []
    monkeypatch.setattr(reviewer, "_fetch_origin", lambda repo: probes.append(str(repo)))
    monkeypatch.setattr(
        reviewer,
        "_list_worktrees",
        lambda repo: [
            {
                "path": str(workspace / f".worktrees/task-{task_id}"),
                "branch": f"task/{task_id}-cockpit",
            }
        ],
    )
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda *a: True)
    monkeypatch.setattr(reviewer, "_gh_pr_merge_details", lambda *a: {})
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda *a: None)
    monkeypatch.setattr(reviewer, "_gh_pr_for_task", lambda *a: None)
    return before, workspace, probes


def _row(task_id):
    with storage.connect() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


@pytest.mark.parametrize("recorded_path", ["absolute", None, "other/task-42"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_exclusive_product_never_reconciles_same_numbered_cockpit_branch(
    monkeypatch, tmp_path, recorded_path, dry_run
):
    before, _workspace, probes = _fixture(monkeypatch, tmp_path, recorded_path)
    assert merge._task_repo_routing(before) == (None, True)
    results = []
    reviewer._reconcile_one_task(
        before, results=results, worktrees_by_repo={}, fetched=set(), dry_run=dry_run
    )
    assert [r["verdict"] for r in results] == ["unresolved-repo"]
    assert probes == [], "exclusive absence must not search cockpit branches or PRs"
    after = _row(before["id"])
    for field in ("status", "claimed_by", "claimed_at", "lock_expires_at", "current_run_id"):
        assert after[field] == before[field]
    assert "merged-upstream" not in after["notes"]
    if dry_run:
        assert after == before
    else:
        assert reviewer.REVIEW_UNRESOLVED_REPO in after["notes"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_product_keeps_valid_relative_legacy_cockpit_fallback(
    monkeypatch, tmp_path, dry_run
):
    before, workspace, probes = _fixture(monkeypatch, tmp_path, "legacy")
    assert merge._task_repo_routing(before) == (None, False)
    results = []
    reviewer._reconcile_one_task(
        before, results=results, worktrees_by_repo={}, fetched=set(), dry_run=dry_run
    )
    assert [r["verdict"] for r in results] == ["merged-upstream"]
    assert probes == [str(workspace)]
    assert _row(before["id"])["status"] == ("waiting_review" if dry_run else "done")
