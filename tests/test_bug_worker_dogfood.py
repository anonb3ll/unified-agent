"""Live dogfood regression for bug worker vs task #487 (merged SYSTEM bug)."""

from __future__ import annotations

from unified_agent.bug_worker.eligibility import is_system_bug_eligible

# Minimal excerpt of task #487 portfolio markers (merged 2026-08-20).
_TASK_487_NOTES = """[portfolio-markers v1]
stream: SYSTEM
project: unified-agent
lifecycle: remediation
owner: Desktop_Agent4
bug: true
severity: high
worker-eligible: true
[/portfolio-markers]

Shipped 2026-08-20: PR #217 squash-merged as e7e3833.
"""


def test_task_487_markers_reject_done_status():
    """Dogfood #487: markers present but merged/done tickets must stay ineligible."""
    done_task = {"id": 487, "status": "done", "notes": _TASK_487_NOTES, "untrusted": 0}
    ok, reason = is_system_bug_eligible(done_task)
    assert not ok
    assert "done" in reason

    open_task = {**done_task, "status": "open"}
    ok_open, _reason = is_system_bug_eligible(open_task)
    assert ok_open


def test_task_487_markers_reject_without_worker_eligible():
    notes = _TASK_487_NOTES.replace("worker-eligible: true", "worker-eligible: false")
    task = {"id": 487, "status": "open", "notes": notes, "untrusted": 0}
    ok, reason = is_system_bug_eligible(task)
    assert not ok
    assert "worker-eligible" in reason
