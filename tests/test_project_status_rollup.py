"""PROOF-B1 (task #846): [project-spec] roots roll up descendant status.

No new tables. Derived status is computed from the existing parent_id tree.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config
from unified_agent.tools.tasks import task_add, task_list, task_update


@pytest.fixture
def authed_agent():
    token = config.agent_identity_var.set("Desktop_Agent4")
    yield "Desktop_Agent4"
    config.agent_identity_var.reset(token)


def _by_id(status: str = "all", **kwargs):
    return {t["id"]: t for t in task_list(status=status, **kwargs)["tasks"]}


def test_unmarked_task_has_no_rollup():
    root = task_add("ordinary epic")
    task_add("child", parent_id=root["id"])
    listed = _by_id()
    assert "rollup" not in listed[root["id"]]
    compact = _by_id(fields="compact")
    assert "rollup" not in compact[root["id"]]


def test_backticked_project_spec_is_not_a_root():
    root = task_add("docs only", notes="this discusses `[project-spec]`")
    listed = _by_id()
    assert "rollup" not in listed[root["id"]]
    compact = _by_id(fields="compact")
    assert "rollup" not in compact[root["id"]]


def test_project_spec_root_rollup_walks_nested_descendants(authed_agent):
    root = task_add("Ship the slice", notes="[project-spec]\nPROOF-B1")
    step = task_add("Step", parent_id=root["id"])
    open_leaf = task_add("Still open", parent_id=step["id"])
    doing = task_add("In flight", parent_id=root["id"])
    blocked = task_add("Stuck", parent_id=root["id"])
    waiting = task_add("In review", parent_id=root["id"])
    done = task_add("Finished", parent_id=root["id"])
    cancelled = task_add("Dropped", parent_id=root["id"])

    task_update(doing["id"], status="doing", worktree=False)
    task_update(blocked["id"], status="blocked")
    task_update(waiting["id"], status="waiting_review")
    task_update(done["id"], status="done")
    task_update(cancelled["id"], status="cancelled")

    listed = _by_id()
    rollup = listed[root["id"]]["rollup"]
    assert rollup["complete"] is False
    assert rollup["derived_status"] == "blocked"
    assert rollup["counts"]["open"] == 2  # step + open_leaf
    assert rollup["counts"]["doing"] == 1
    assert rollup["counts"]["waiting_review"] == 1
    assert rollup["counts"]["blocked"] == 1
    assert rollup["counts"]["done"] == 1
    assert rollup["counts"]["cancelled"] == 1
    active_ids = {row["id"] for row in rollup["active"]}
    assert active_ids == {step["id"], open_leaf["id"], doing["id"], blocked["id"], waiting["id"]}
    assert done["id"] not in active_ids
    assert cancelled["id"] not in active_ids
    assert "rollup" not in listed[step["id"]]

    compact = _by_id(fields="compact")
    assert compact[root["id"]]["rollup"]["derived_status"] == "blocked"
    assert compact[root["id"]]["lineage"] == []


def test_project_spec_is_complete_when_descendants_are_terminal(authed_agent):
    root = task_add("Wrap up", notes="[project-spec]")
    child = task_add("Last step", parent_id=root["id"])
    grandchild = task_add("Nested last step", parent_id=child["id"])
    extra = task_add("Abandoned", parent_id=root["id"])
    task_update(child["id"], status="done")
    task_update(grandchild["id"], status="done")
    task_update(extra["id"], status="cancelled")

    rollup = _by_id()[root["id"]]["rollup"]
    assert rollup["complete"] is True
    assert rollup["derived_status"] == "done"
    assert rollup["active"] == []
    assert rollup["counts"]["done"] == 2
    assert rollup["counts"]["cancelled"] == 1


def test_empty_project_spec_subtree_is_complete():
    root = task_add("Spec only", notes="[project-spec]\nno steps yet")
    rollup = _by_id()[root["id"]]["rollup"]
    assert rollup["complete"] is True
    assert rollup["derived_status"] == "done"
    assert rollup["active"] == []
    assert rollup["counts"] == {
        "open": 0,
        "doing": 0,
        "waiting_review": 0,
        "blocked": 0,
        "done": 0,
        "cancelled": 0,
    }


def test_cannot_complete_project_spec_while_active_descendants_exist(authed_agent):
    root = task_add("Project", notes="[project-spec]")
    child = task_add("Child still open", parent_id=root["id"])
    grandchild = task_add("Grandchild blocked", parent_id=child["id"])
    task_update(grandchild["id"], status="blocked")
    done_child = task_add("Already done", parent_id=root["id"])
    task_update(done_child["id"], status="done")

    with pytest.raises(ToolError, match=r"active descendants") as excinfo:
        task_update(root["id"], status="done")
    message = str(excinfo.value)
    assert f"#{child['id']} (open)" in message
    assert f"#{grandchild['id']} (blocked)" in message
    assert f"#{done_child['id']}" not in message
    listed = _by_id()
    assert listed[root["id"]]["status"] == "open"


def test_can_complete_project_spec_once_descendants_are_terminal(authed_agent):
    root = task_add("Project", notes="[project-spec]")
    child = task_add("Child", parent_id=root["id"])
    grandchild = task_add("Grandchild", parent_id=child["id"])
    task_update(child["id"], status="done")
    task_update(grandchild["id"], status="cancelled")

    updated = task_update(root["id"], status="done")
    assert updated["status"] == "done"
    assert _by_id(status="done")[root["id"]]["rollup"]["complete"] is True


def test_unmarked_parent_can_still_complete_with_open_children(authed_agent):
    parent = task_add("Not a project spec")
    task_add("Still open", parent_id=parent["id"])
    updated = task_update(parent["id"], status="done")
    assert updated["status"] == "done"


def test_project_view_does_not_treat_merge_notes_as_verified(authed_agent):
    root = task_add("Proof B2 spec", notes="[project-spec]")
    merged = task_add(
        "Merged step",
        parent_id=root["id"],
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/423",
        notes="[review: merged] https://github.com/anonb3ll/unified-agent/pull/423",
    )
    evidence_only = task_add(
        "Unmerged evidence",
        parent_id=root["id"],
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
    )
    task_add("Open step", parent_id=root["id"])
    task_update(merged["id"], status="done")
    task_update(evidence_only["id"], status="done")

    view = _by_id()[root["id"]]["project_view"]
    assert view["evidence_count"] == 2
    assert view["merge_verified_count"] == 0
    assert view["evidence_cli"] == f"unified-agent evidence report --project {root['id']}"
    assert "project_view" not in _by_id()[merged["id"]]
    compact = _by_id(fields="compact")[root["id"]]["project_view"]
    assert compact["evidence_count"] == 2
    assert compact["merge_verified_count"] == 0


def test_project_view_does_not_treat_stale_merge_note_as_verified(authed_agent):
    root = task_add("Stale merge spec", notes="[project-spec]")
    child = task_add(
        "New PR, old merge note",
        parent_id=root["id"],
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
        notes="[review: merged] https://github.com/anonb3ll/unified-agent/pull/423",
    )
    task_update(child["id"], status="done")
    view = _by_id()[root["id"]]["project_view"]
    assert view["evidence_count"] == 1
    assert view["merge_verified_count"] == 0


def test_ordinary_task_has_no_project_view():
    root = task_add("ordinary epic")
    assert "project_view" not in _by_id()[root["id"]]
    assert "project_view" not in _by_id(fields="compact")[root["id"]]
