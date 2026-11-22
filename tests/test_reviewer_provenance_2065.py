"""Origin/PR fallbacks retain the recorded task checkout provenance."""

import pytest

from unified_agent import merge, reviewer


@pytest.mark.parametrize("kind", ["origin", "merged"])
def test_review_fallback_passes_the_full_task_to_repo_routing(monkeypatch, kind):
    task = {
        "id": 42,
        "project": "unified-agent",
        "notes": "branch task/42-product\n[pr: #59]",
        "worktree_path": "/other-host/dev/unified_agent/.worktrees/task-42",
        "branch": "task/42-product",
    }
    captured = []

    def route(row):
        captured.append(dict(row))
        return None, True

    monkeypatch.setattr(merge, "_task_repo_routing", route)
    monkeypatch.setattr(
        reviewer, "_branch_exists_on_origin", lambda *_: pytest.fail("cockpit probe")
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda *_: pytest.fail("cockpit PR evidence"))
    if kind == "origin":
        result = reviewer._origin_branch_fallback(
            42, task["notes"], project=task["project"], task=task
        )
    else:
        result = reviewer._merged_pr_fallback(
            42, "product", task["notes"], None, project=task["project"], task=task
        )
    assert result is None
    assert captured == [task]
