"""autonomous_cycle_green_path_eligibility (task #373 Tier 2, 3.4) — CRITERIA
only, never an action. See docs/s5-bounded-autonomy-acceptance-gates.md
MUST 8-9: nothing here bypasses the mandatory human approval on
autonomous_cycle_transition(..., to_state="completed").

Mirrors test_reviewer_pipeline.py's monkeypatch-at-the-module-seam style
(unified_agent.merge.find_task_worktree / run_validation_gates,
cycle_eligibility._changed_files_vs_main) so nothing touches a real repository.
"""

import pytest

from unified_agent import cycle_eligibility, reviewer
from unified_agent.tools.tasks import task_add

SHA = "abc123def4567890abc123def4567890abc123de"


def _cycle(**overrides) -> dict:
    base = {
        "state": "pr",
        "pr_url": "https://github.com/example/repo/pull/1",
        "stop_reason": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def green_worktree(monkeypatch):
    """Same seam-mocking pattern as test_reviewer_pipeline.py's fixture of
    the same name: worktree found, gates green, no red files."""
    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": SHA}

    def fake_gates(task_id, wt_path, force=False):
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr(cycle_eligibility, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        cycle_eligibility,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )


def _task_id() -> int:
    return task_add("green path eligibility test task")["id"]


def test_eligible_when_state_evidence_no_stop_reason_no_red_tier_gates_pass(green_worktree):
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is True
    assert any("no red-tier files touched" in r for r in result["reasons"])
    assert any("validation gates passed" in r for r in result["reasons"])


def test_completed_state_is_also_eligible(green_worktree):
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle(state="completed"))
    assert result["eligible"] is True


def test_ineligible_when_state_is_not_pr_or_completed():
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle(state="executing"))
    assert result["eligible"] is False
    assert "executing" in result["reasons"][0]


def test_ineligible_when_pr_url_missing():
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle(pr_url=""))
    assert result["eligible"] is False
    assert "no pr_url recorded" in result["reasons"][-1]


def test_ineligible_when_cycle_carries_a_stop_reason():
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(
        tid, _cycle(stop_reason="autonomy budget exhausted: tokens")
    )
    assert result["eligible"] is False
    assert "stop_reason" in result["reasons"][-1]


def test_ineligible_when_diff_touches_a_red_tier_file(monkeypatch):
    tid = _task_id()
    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(
        cycle_eligibility,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/unified_agent/safety.py", "status": "M"}],
    )
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is False
    assert "red-tier" in result["reasons"][-1]
    assert "safety.py" in result["reasons"][-1]


def test_ineligible_when_validation_gates_fail(monkeypatch):
    tid = _task_id()
    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(cycle_eligibility, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        cycle_eligibility,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )

    def failing_gates(task_id, wt_path, force=False):
        raise RuntimeError("pytest failed: 2 tests")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", failing_gates)
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is False
    assert "validation gates failed" in result["reasons"][-1]


def test_ineligible_when_no_worktree_found(monkeypatch):
    tid = _task_id()
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is False
    assert "no git worktree found" in result["reasons"][-1]


def test_ineligible_when_head_moves_during_validation(monkeypatch):
    """task #598: the gates take minutes, and nothing else stopped a
    concurrent commit/rebase/fixup from moving HEAD mid-run. An eligible
    verdict must describe the commit it actually validated — if HEAD after
    the gates doesn't match HEAD before, the gate must hold rather than
    approve a bypass for a commit it never validated."""
    tid = _task_id()
    wt = {"path": "/tmp/wt-x", "branch": "fix/task-x", "head": SHA}
    moved_sha = "f" * 40

    def fake_gates(task_id, wt_path, force=False):
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    heads = iter([SHA, moved_sha])
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr(cycle_eligibility, "_head_sha", lambda wt_path: next(heads))
    monkeypatch.setattr(
        cycle_eligibility,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )

    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is False
    assert "HEAD moved during validation" in result["reasons"][-1]
    assert SHA[:12] in result["reasons"][-1]
    assert moved_sha[:12] in result["reasons"][-1]


def test_never_calls_the_real_merge_path(monkeypatch, green_worktree):
    """Criteria, not action — this function must never reach orchestrate_merge
    or perform any state transition, regardless of the verdict."""

    def boom(*args, **kwargs):
        raise AssertionError("real merge path (orchestrate_merge) was invoked")

    monkeypatch.setattr("unified_agent.merge.orchestrate_merge", boom)
    tid = _task_id()
    result = reviewer.autonomous_cycle_green_path_eligibility(tid, _cycle())
    assert result["eligible"] is True  # boom would have fired if it were ever called
