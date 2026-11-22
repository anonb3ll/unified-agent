"""Task claim, status transition, and hierarchy integration checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools import doctor
from unified_agent.tools import tasks as tasks_tool

# ==============================================================================
# TIER 1: FEATURE COVERAGE (R3)
# ==============================================================================


class TestR3FeatureCoverage:
    """Tier 1: Primary behavioral coverage for Requirement R3."""

    def test_r3_task_claim_before_code_transitions(self):
        """Verify claiming task sets status to doing, records claimant and lock lease.

        Authoritative Source: .agents/AGENTS.md Dual-Agent Collaboration Protocol Rule 1.
        """
        res = tasks_tool.task_add(title="Task #1387: Identity join", priority="high")
        task_id = res["id"]

        # Claim the task
        updated = tasks_tool.task_claim(id=task_id, agent="Desktop_Agent7")
        assert updated["status"] == "doing"
        assert updated["claimed_by"] == "Desktop_Agent7"
        assert updated["lock_expires_at"] is not None

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, notes, claimed_by, lock_expires_at FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            assert row["status"] == "doing"
            assert "claimed-by: Desktop_Agent7" in row["notes"]
            assert row["claimed_by"] == "Desktop_Agent7"
            assert row["lock_expires_at"] is not None
        finally:
            conn.close()

    def test_r3_task_completion_records_timestamp(self):
        """Verify transitioning task to done records completed_at.

        Authoritative Source: tools/tasks.py task_update specification.
        """
        res = tasks_tool.task_add(title="Task #1262: Dogfooding round", priority="med")
        task_id = res["id"]

        tasks_tool.task_update(id=task_id, status="doing", worktree=False)
        done_res = tasks_tool.task_update(
            id=task_id, status="done", actor="Desktop_Agent7", worktree=False
        )

        assert done_res["status"] == "done"
        assert done_res.get("completed_at") is not None

    def test_r3_stale_lock_reclamation_no_abandonment(self):
        """Verify reclaim_stale_task_locks resets abandoned tasks from doing to open.

        Authoritative Source: doctor.py reclaim_stale_task_locks, ORIGINAL_REQUEST.md.
        """
        conn = storage.connect()
        try:
            now = storage.now()
            # Insert a task that had its lock expire 10 minutes ago
            past_time = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                "INSERT INTO tasks (title, status, claimed_by, lock_expires_at, created_at, updated_at) "
                "VALUES ('Abandoned task', 'doing', 'Stale_Agent', ?, ?, ?)",
                (past_time, now, now),
            )
            task_id = cur.lastrowid
            conn.commit()

            # Run reclamation
            reclaim_report = doctor.reclaim_stale_task_locks()
            assert reclaim_report["reclaimed_count"] >= 1
            assert any(t["id"] == task_id for t in reclaim_report["reclaimed_tasks"])

            # Verify task status is back to 'open' and claim cleared
            row = conn.execute(
                "SELECT status, claimed_by, lock_expires_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            assert row["status"] == "open"
            assert row["claimed_by"] is None
            assert row["lock_expires_at"] is None
        finally:
            conn.close()


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (R3)
# ==============================================================================


class TestR3BoundaryAndCornerCases:
    """Tier 2: Boundary conditions and adversarial corner cases for Requirement R3."""

    def test_r3_circular_dependency_rejection(self):
        """Corner Case: Attempting to create parent cycles raises ToolError."""
        conn = storage.connect()
        try:
            res1 = tasks_tool.task_add(title="Task 1", priority="med")
            t1 = res1["id"]
            res2 = tasks_tool.task_add(title="Task 2", priority="med", parent_id=t1)
            t2 = res2["id"]

            # Attempting to reparent Task 1 under Task 2 creates a cycle
            with pytest.raises(ToolError, match="would create a cycle"):
                tasks_tool._assert_nesting(conn, parent_id=t2, moving_id=t1)
        finally:
            conn.close()

    def test_r3_task_max_depth_nesting_cap(self):
        """Boundary: Nesting depth cap TASK_MAX_DEPTH (8) refuses deeper trees."""
        current_parent = None
        for i in range(tasks_tool.TASK_MAX_DEPTH + 1):
            res = tasks_tool.task_add(title=f"Level {i}", priority="low", parent_id=current_parent)
            current_parent = res["id"]

        # Attempting one more level must fail
        with pytest.raises(ToolError, match="nesting depth cap is 8"):
            tasks_tool.task_add(title="Exceeds cap", priority="low", parent_id=current_parent)

    def test_r3_terminal_status_transition_preserves_completed_at(self):
        """Boundary: Transitioning from done to cancelled preserves original completed_at."""
        res = tasks_tool.task_add(title="Terminal preservation task", priority="low")
        task_id = res["id"]

        done_res = tasks_tool.task_update(
            id=task_id, status="done", actor="Desktop_Agent7", worktree=False
        )
        completed_at = done_res["completed_at"]
        assert completed_at is not None

        # Transition to cancelled
        canc_res = tasks_tool.task_update(
            id=task_id, status="cancelled", actor="Desktop_Agent7", worktree=False
        )
        assert canc_res["completed_at"] == completed_at
