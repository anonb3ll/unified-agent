"""Workspace boundary, secret-read, and event integrity integration checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, safety, storage, sync

# ==============================================================================
# TIER 1: FEATURE COVERAGE (R4)
# ==============================================================================


class TestR4FeatureCoverage:
    """Tier 1: Primary behavioral coverage for Requirement R4."""

    def test_r4_workspace_boundary_path_traversal_blocked(self):
        """Verify safety.resolve_in_workspace rejects directory traversal escaping root.

        Authoritative Source: ORIGINAL_REQUEST.md R4, safety.py resolve_in_workspace.
        """
        with pytest.raises(ToolError, match="escapes the workspace root"):
            safety.resolve_in_workspace("../../etc/passwd")

    def test_r4_workspace_hook_directory_write_blocked(self):
        """Verify safety.resolve_in_workspace blocks paths reaching into VCS hook directories.

        Authoritative Source: safety.py guard_hook_dir (.git, .husky, .hg, .svn).
        """
        with pytest.raises(ToolError, match="targets a hook-execution directory"):
            safety.resolve_in_workspace(".git/hooks/pre-commit")

        with pytest.raises(ToolError, match="targets a hook-execution directory"):
            safety.resolve_in_workspace(".husky/pre-push")

    def test_r4_secret_bearing_file_read_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify safety.resolve_for_project_read blocks reading secret-bearing files.

        Authoritative Source: safety.py _PROJECT_READ_SECRET_RX.
        """
        # Create an external project repo to test project read reach
        ext_project = tmp_path / "dev" / "browser"
        ext_project.mkdir(parents=True, exist_ok=True)
        (ext_project / ".env").write_text("SECRET=123")
        (ext_project / "id_rsa").write_text("KEY")

        monkeypatch.setattr(
            safety, "project_read_roots", lambda: [config.workspace_root(), ext_project]
        )

        with pytest.raises(ToolError, match="secret-bearing"):
            safety.resolve_for_project_read(str(ext_project / ".env"))

        with pytest.raises(ToolError, match="secret-bearing"):
            safety.resolve_for_project_read(str(ext_project / "id_rsa"))

    def test_r4_audit_ledger_hash_chain_verification(self):
        """Verify cryptographic HMAC/hash chain is formed and verified.

        Authoritative Source: events.py verify_event_chain, Task #746.
        """
        events.record_event("agent.task.updated", payload={"task_id": 1, "status": "doing"})
        events.record_event(
            "agent.approval.requested", payload={"tool": "run_cmd", "cmd": "pytest"}
        )
        u3 = events.record_event("agent.task.updated", payload={"task_id": 1, "status": "done"})

        report = events.verify_event_chain()
        assert report["status"] == "valid"
        assert report["verified"] is True
        assert report["total_verified"] == 3
        assert report["head_ulid"] == u3


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (R4)
# ==============================================================================


class TestR4BoundaryAndCornerCases:
    """Tier 2: Boundary conditions and adversarial corner cases for Requirement R4."""

    def test_r4_audit_ledger_detects_in_place_row_tampering(self):
        """Adversarial Case: In-place edit of SQLite audit ledger breaks hash chain."""
        events.record_event("agent.task.updated", payload={"task_id": 10, "state": "doing"})
        tamper_ulid = events.record_event("agent.approval.requested", payload={"approval_id": 42})
        events.record_event("agent.task.updated", payload={"task_id": 10, "state": "done"})

        # Tamper directly with the payload in SQLite
        conn = storage.connect()
        try:
            with events.ledger_maintenance(conn):
                conn.execute(
                    "UPDATE event_log SET payload=? WHERE ulid=?",
                    (json.dumps({"approval_id": 42, "tampered": True}), tamper_ulid),
                )
            conn.commit()
        finally:
            conn.close()

        report = events.verify_event_chain()
        assert report["status"] == "tampered"
        assert report["verified"] is False
        assert report["tampered_at"]["ulid"] == tamper_ulid

    def test_r4_symlink_pointing_outside_workspace_blocked(self, tmp_path: Path):
        """Adversarial Case: Symlink in workspace pointing to outside directory is rejected."""
        ws = config.workspace_root()
        outside_dir = tmp_path / "outside_secret"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "confidential.txt").write_text("secret")

        # Create symlink inside workspace
        symlink_path = ws / "symlink_escape"
        symlink_path.symlink_to(outside_dir)

        with pytest.raises(ToolError, match="escapes the workspace root"):
            safety.resolve_in_workspace("symlink_escape/confidential.txt")

    def test_r4_audit_ledger_deletion_gap_detected(self):
        """Adversarial Case: Deleting a row from the audit ledger breaks prev_hash link."""
        events.record_event("evt1", payload={"step": 1})
        u2 = events.record_event("evt2", payload={"step": 2})
        u3 = events.record_event("evt3", payload={"step": 3})

        # Delete intermediate row
        conn = storage.connect()
        try:
            with events.ledger_maintenance(conn):
                conn.execute("DELETE FROM event_log WHERE ulid=?", (u2,))
            conn.commit()
        finally:
            conn.close()

        report = events.verify_event_chain()
        assert report["status"] == "tampered"
        assert report["verified"] is False
        assert report["tampered_at"]["ulid"] == u3

    def test_r4_offline_state_sync_vault_logs(self, monkeypatch: pytest.MonkeyPatch):
        """Boundary: State sync generates host-scoped snapshot files in vault Logs/."""
        monkeypatch.setenv("UA_STATE_SYNC", "1")
        monkeypatch.setenv("UA_STATE_SYNC_DEBOUNCE", "0")
        monkeypatch.setenv("UA_SOURCE_HOST", "test-host")

        # Create vault Logs dir
        vault_logs = config.vault_root() / "Logs"
        vault_logs.mkdir(parents=True, exist_ok=True)

        sync.sync_state_to_vault()

        # Check that state files were written
        assert (vault_logs / "state_memory.test-host.md").exists()
        assert (vault_logs / "state_tasks.test-host.md").exists()
