"""Completion state must agree with the completion timestamp and the review record.

#288 added a notes-level guard (a task explicitly waiting for review cannot be
marked done without a recorded review outcome). These cover the remaining
field-level contradictions: see docs/task-lifecycle-contract.md §1 item 7.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools import tasks as tasks_mod
from unified_agent.tools.tasks import (
    _completion_review_error,
    _evidence_required_error,
    task_add,
    task_update,
)


@pytest.fixture
def authed_agent():
    token = config.agent_identity_var.set("Desktop_Agent1")
    yield "Desktop_Agent1"
    config.agent_identity_var.reset(token)


def _row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def test_recompleting_a_task_preserves_the_original_completion_time(authed_agent, monkeypatch):
    """done -> done must not restamp completed_at.

    monitor.py derives cycle time from created_at -> completed_at; re-asserting
    'done' while appending notes silently reset it to "now".
    """
    res = task_add("finish once")
    monkeypatch.setattr(tasks_mod.storage, "now", lambda: "2026-08-01T00:00:00+00:00")
    task_update(res["id"], status="done")
    first = _row(res["id"])["completed_at"]
    assert first == "2026-08-01T00:00:00+00:00"

    monkeypatch.setattr(tasks_mod.storage, "now", lambda: "2026-08-09T00:00:00+00:00")
    task_update(res["id"], status="done", notes="follow-up detail")

    assert _row(res["id"])["completed_at"] == first


def test_reasserting_done_backfills_a_missing_completion_time(authed_agent):
    """A pre-guard row can be done with completed_at NULL; re-completion repairs it."""
    res = task_add("historically inconsistent")
    task_update(res["id"], status="done")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (res["id"],))
        conn.commit()
    finally:
        conn.close()

    task_update(res["id"], status="done")

    assert _row(res["id"])["completed_at"] is not None


def test_a_merged_task_satisfies_the_review_completion_guard():
    """git_merge_task records `[merged commit: ...]`, which is a review outcome.

    Without this the merge tool's own completion note fails #288's guard, so a
    genuinely merged task reads as an unreviewed completion forever.
    """
    notes = "[waiting-for: review]\n[merged commit: abc1234 @ 2026-08-07T00:00:00Z]"

    assert _completion_review_error(notes) is None


def test_cancelling_a_done_task_preserves_completed_at(authed_agent, monkeypatch):
    """done -> cancelled must not null completed_at (task-lifecycle-contract.md §7).

    Both are terminal; the transition-guard used to null completed_at on any
    status change other than 'done', which would have erased the timestamp here.
    """
    res = task_add("finished, then reclassified")
    monkeypatch.setattr(tasks_mod.storage, "now", lambda: "2026-08-01T00:00:00+00:00")
    task_update(res["id"], status="done")
    stamped = _row(res["id"])["completed_at"]
    assert stamped == "2026-08-01T00:00:00+00:00"

    task_update(res["id"], status="cancelled")

    assert _row(res["id"])["completed_at"] == stamped


def test_cancelling_an_open_task_leaves_completed_at_null(authed_agent):
    """A task cancelled without ever being done was never completed."""
    res = task_add("never started")
    task_update(res["id"], status="cancelled")
    assert _row(res["id"])["completed_at"] is None


# --- Phase 0: evidence-gated done for [code-task]-marked tasks ---


def test_evidence_required_error_is_none_for_unmarked_task():
    """A task without [code-task] is unaffected — this guard is opt-in."""
    assert _evidence_required_error("just a normal task", None) is None


def test_evidence_required_error_fires_for_marked_task_with_no_evidence():
    err = _evidence_required_error("[code-task] fix the bug", None)
    assert err is not None
    assert "code-task" in err
    assert "evidence_ref" in err


def test_evidence_required_error_satisfied_by_evidence_ref():
    assert _evidence_required_error("[code-task] fix the bug", "abc1234") is None
    # A blank/whitespace-only evidence_ref does not count.
    assert _evidence_required_error("[code-task] fix the bug", "   ") is not None


def test_evidence_required_error_satisfied_by_review_completion_marker():
    """A merged PR is itself evidence — no redundant evidence_ref required."""
    notes = "[code-task] fix the bug\n[merged commit: abc1234 @ 2026-08-10T00:00:00Z]"
    assert _evidence_required_error(notes, None) is None


def test_marking_a_code_task_done_without_evidence_is_rejected(authed_agent):
    res = task_add("ship the fix", notes="[code-task] needs a real commit")
    with pytest.raises(ToolError, match="code-task"):
        task_update(res["id"], status="done")
    assert _row(res["id"])["status"] != "done"


def test_marking_a_code_task_done_is_accepted_with_evidence_ref(authed_agent):
    res = task_add("ship the fix", notes="[code-task] needs a real commit")
    task_update(res["id"], status="done", evidence_ref="commit abc1234")
    row = _row(res["id"])
    assert row["status"] == "done"
    assert row["evidence_ref"] == "commit abc1234"


def test_marking_a_code_task_done_is_accepted_with_prior_evidence_ref(authed_agent):
    """evidence_ref supplied on an earlier update still satisfies a later done."""
    res = task_add("ship the fix", notes="[code-task] needs a real commit")
    task_update(res["id"], evidence_ref="PR #999 in flight")
    task_update(res["id"], status="done")
    assert _row(res["id"])["status"] == "done"
