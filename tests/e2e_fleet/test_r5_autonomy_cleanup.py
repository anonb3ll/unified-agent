"""E2E Tests for Requirement R5: Post-Merge Autonomy Schema Cleanup (Task #1544).

Authoritative source:
- ORIGINAL_REQUEST.md § R5
- orchestrator_2/PROJECT.md § M5
- spec_miner_survey_batch_3/handoff.md
- Codebase paths:
  src/unified_agent/autonomy_modes.py
  src/unified_agent/storage.py
  src/unified_agent/toolsets.py
  src/unified_agent/tools/tasks.py
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import autonomy_modes, goals, storage
from unified_agent.tools import tasks as tasks_tool

from .conftest import identity_context


def _create_test_mission(mission_id: int = 1450) -> None:
    """Helper to insert an active parent goal/mission into storage."""
    conn = storage.connect()
    try:
        goals.ensure_schema(conn)
        now = storage.now()
        conn.execute(
            "INSERT OR IGNORE INTO goals (id, title, status, created_at, updated_at) "
            "VALUES (?, 'Mission Test Goal', 'open', ?, ?)",
            (mission_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()


class TestR5AutonomySchemaCleanup:
    """Tier 1 (Feature Coverage) and Tier 2 (Boundary & Corner Cases) for R5."""

    # -------------------------------------------------------------------------
    # Tier 1: Feature Coverage (Happy Path)
    # -------------------------------------------------------------------------

    def test_r5_schema_migration_in_storage_migrate(self):
        """Feature: autonomy_modes.ensure_schema initializes standing_grants and action_reviews tables."""
        conn = storage.connect()
        try:
            autonomy_modes.ensure_schema(conn)
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            assert "autonomy_standing_grants" in tables
            assert "autonomy_action_reviews" in tables
            assert "autonomy_mission_mode" in tables
        finally:
            conn.close()

    def test_r5_autonomy_mission_mode_lifecycle(self):
        """Feature: Setting and querying mission autonomy modes (task #1457)."""
        _create_test_mission(1450)

        # Set mission mode to 'checkpoint'
        res = autonomy_modes.set_mode(
            1450, "checkpoint", actor="Desktop_Person", reason="E2E testing"
        )
        assert res["mode"] == "checkpoint"
        assert res["mission_id"] == 1450

        # Query mission mode
        current = autonomy_modes.get_mode(1450)
        assert current["mode"] == "checkpoint"
        assert current["mission_id"] == 1450

    def test_r5_standing_grants_grant_and_revoke(self):
        """Feature: Granting and revoking standing permissions on a mission."""
        _create_test_mission(1450)
        autonomy_modes.set_mode(1450, "checkpoint", actor="Desktop_Person")

        # Grant standing permission for 'deploy'
        grant_res = autonomy_modes.grant_standing(1450, "deploy", actor="Desktop_Person")
        assert "deploy" in grant_res["standing_grants"]

        # Verify reflected in get_mode
        mode = autonomy_modes.get_mode(1450)
        assert "deploy" in mode["standing_grants"]

        # Revoke standing permission
        revoke_res = autonomy_modes.revoke_grant(1450, "deploy", actor="Desktop_Person")
        assert "deploy" not in revoke_res["standing_grants"]

        # Verify revoked
        mode_after = autonomy_modes.get_mode(1450)
        assert "deploy" not in mode_after["standing_grants"]

    def test_r5_legacy_standing_grants_backfill_idempotent(self):
        """Feature: Backfill of legacy JSON standing grants to relational table is clean and idempotent."""
        _create_test_mission(1460)
        conn = storage.connect()
        try:
            now = storage.now()
            conn.execute(
                "INSERT OR REPLACE INTO autonomy_mission_mode (mission_id, mode, standing_grants, updated_by, updated_at) "
                "VALUES (1460, 'checkpoint', ?, 'Desktop_Person', ?)",
                (json.dumps(["deploy"]), now),
            )
            conn.commit()

            # Execute schema ensure / backfill
            autonomy_modes.ensure_schema(conn)

            # Check rows migrated to autonomy_standing_grants
            rows = conn.execute(
                "SELECT action_class FROM autonomy_standing_grants WHERE mission_id=1460"
            ).fetchall()
            assert any(r["action_class"] == "deploy" for r in rows)

            # Legacy column must be cleared to '[]'
            legacy_col = conn.execute(
                "SELECT standing_grants FROM autonomy_mission_mode WHERE mission_id=1460"
            ).fetchone()["standing_grants"]
            assert legacy_col == "[]"

            # Running again is completely idempotent
            autonomy_modes.ensure_schema(conn)
            rows2 = conn.execute(
                "SELECT action_class FROM autonomy_standing_grants WHERE mission_id=1460"
            ).fetchall()
            assert len(rows2) == len(rows)
        finally:
            conn.close()

    def test_r5_task_update_request_cancel_with_live_run(self):
        """Feature: task_update(request_cancel=True) requests cancellation on a task with active dispatch run."""
        task = tasks_tool.task_add(title="Cancelable Work Item", priority="med")
        task_id = task["id"]
        now = storage.now()

        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET status='doing', claimed_by='Desktop_Agent7', current_run_id='sp-cancel-test', "
                "current_run_agent='Desktop_Agent7', updated_at=? WHERE id=?",
                (now, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Update with request_cancel=True as claimant Desktop_Agent7
        with identity_context("Desktop_Agent7"):
            updated = tasks_tool.task_update(id=task_id, request_cancel=True, worktree=False)
        assert updated["id"] == task_id
        assert updated.get("cancel_requested") == 1 or updated.get("cancel_requested") is True

    # -------------------------------------------------------------------------
    # Tier 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r5_request_cancel_without_live_run_rejected(self):
        """Boundary: Calling task_update(request_cancel=True) on a task with no active run fails closed."""
        task = tasks_tool.task_add(title="Idle Task No Runs")
        task_id = task["id"]

        with pytest.raises(ToolError, match="has no open run to cancel"):
            tasks_tool.task_update(id=task_id, request_cancel=True, worktree=False)

    def test_r5_invalid_mode_rejected(self):
        """Boundary: Setting an invalid autonomy mode outside ('observe', 'exception', 'checkpoint', 'action-review') is rejected."""
        _create_test_mission(1450)
        with pytest.raises(ToolError, match="mode must be one of"):
            autonomy_modes.set_mode(1450, "unrestricted_bypass", actor="Desktop_Person")

    def test_r5_unknown_mission_rejected(self):
        """Boundary: Setting an autonomy mode on a nonexistent mission ID is rejected with ToolError."""
        with pytest.raises(ToolError, match="unknown mission"):
            autonomy_modes.set_mode(999999, "checkpoint", actor="Desktop_Person")

    def test_r5_repeated_migrations_idempotent(self):
        """Boundary: Repeatedly invoking autonomy_modes.ensure_schema does not throw or corrupt DB."""
        conn = storage.connect()
        try:
            for _ in range(3):
                autonomy_modes.ensure_schema(conn)
            # Tables exist and are queryable
            count = conn.execute("SELECT COUNT(*) AS c FROM autonomy_mission_mode").fetchone()["c"]
            assert count >= 0
        finally:
            conn.close()

    def test_r5_passive_connect_read_isolation(self, monkeypatch):
        """Boundary: Read paths on autonomy modes execute SELECT statements without DDL."""
        _create_test_mission(1450)
        executed_statements: list[str] = []

        # get_mode opens its own connection, so trace every connection the read
        # opens rather than one this test holds (Devin review, PR #922).
        real_connect = storage.connect

        def traced_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(executed_statements.append)
            return conn

        monkeypatch.setattr(storage, "connect", traced_connect)
        autonomy_modes.get_mode(1450)
        monkeypatch.setattr(storage, "connect", real_connect)

        assert executed_statements, "the trace must observe the read's own statements"
        ddl_statements = [
            stmt
            for stmt in executed_statements
            if stmt.strip().upper().startswith(("CREATE", "ALTER", "DROP"))
        ]
        assert ddl_statements == []
