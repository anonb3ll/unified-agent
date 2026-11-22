"""Cross-feature checks for task events, workspace boundaries, and live slots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, safety, storage
from unified_agent.tools import tasks as tasks_tool


class TestTier3CrossFeatureCombinations:
    """Tier 3: Pairwise cross-feature interactions across R1, R2, R3, and R4."""

    def test_tier3_r3_task_claim_creates_r4_audit_ledger_entry(self):
        """Pairwise R3 + R4: Task lifecycle mutations create verifiable hash-chained audit events."""
        res = tasks_tool.task_add(title="Audited Task #1388", priority="med")
        task_id = res["id"]

        tasks_tool.task_update(id=task_id, status="doing", worktree=False)
        tasks_tool.task_update(id=task_id, status="done", actor="Desktop_Agent7", worktree=False)

        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT ulid, payload FROM event_log WHERE channel=? ORDER BY rowid",
                (events.CHANNEL_TASK_UPDATED,),
            ).fetchall()
        finally:
            conn.close()
        lifecycle = [
            (row["ulid"], json.loads(row["payload"]))
            for row in rows
            if json.loads(row["payload"]).get("task_id") == task_id
        ]
        assert len(lifecycle) == 2
        assert lifecycle[0][1]["status"] == "doing"
        assert lifecycle[1][1]["status"] == "done"

        # Verify audit chain integrity
        report = events.verify_event_chain()
        assert report["status"] == "valid"
        assert report["verified"] is True
        assert report["total_verified"] >= 2

    def test_tier3_r4_workspace_boundary_enforced_in_r3_task_worktree(self, tmp_path: Path):
        """Pairwise R4 + R3: Task worktree isolation enforces strict workspace boundaries."""
        ws = config.workspace_root()
        worktree_dir = ws / ".worktrees" / "task-1327"
        worktree_dir.mkdir(parents=True, exist_ok=True)

        # File creation inside task worktree is allowed
        inside_file = worktree_dir / "node_queue.py"
        inside_file.write_text("# inside worktree\n")
        resolved = safety.resolve_in_workspace(str(inside_file))
        assert resolved == inside_file.resolve()

        # Traversing above the workspace root is rejected
        with pytest.raises(ToolError, match="escapes the workspace root"):
            safety.resolve_in_workspace(str(worktree_dir / "../../../etc/shadow"))

    def test_tier3_r1_ceiling_exclusion_with_r3_multi_agent_concurrency(self):
        """Pairwise R1 + R3: Multi-agent dispatch concurrent slot management with exclude_run_id."""
        conn = storage.connect()
        try:
            now = storage.now()
            # Agent 1 has an active run
            cur1 = conn.execute(
                "INSERT INTO tasks (title, status, created_at, updated_at) VALUES ('T1', 'doing', ?, ?)",
                (now, now),
            )
            t1_id = cur1.lastrowid
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES ('run-agent-1', ?, 'Desktop_Agent7', 'running', 'agent1', ?, ?)",
                (t1_id, now, now),
            )
            conn.commit()

            # Agent 2 claims and checks ceiling with exclude_run_id
            cur2 = conn.execute(
                "INSERT INTO tasks (title, status, created_at, updated_at) VALUES ('T2', 'doing', ?, ?)",
                (now, now),
            )
            t2_id = cur2.lastrowid
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES ('run-agent-2', ?, 'Desktop_Agent2', 'claimed', 'agent2', ?, ?)",
                (t2_id, now, now),
            )
            conn.commit()

            # When max live runs is 2:
            predicate = storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE
            # Without exclusion: count is 2 (both counted)
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM dispatch_runs WHERE ({predicate})"
            ).fetchone()["n"]
            assert total == 2

            # Excluding run-agent-2: count is 1 (only agent 1 counted, agent 2 does not reject itself)
            excluding_self = conn.execute(
                f"SELECT COUNT(*) AS n FROM dispatch_runs WHERE ({predicate}) AND run_id != 'run-agent-2'"
            ).fetchone()["n"]
            assert excluding_self == 1
        finally:
            conn.close()
