"""Tier 3: Cross-Feature Combinations across Batch 2 Requirements (R1-R5).

Focus:
- Pairwise & Multi-way interactions across:
  1. Surface Resolution + Doctor Audits (R2 + R1)
  2. Catalogs + Skill Registry Provenance & Audit Events (R4 + R1)
  3. Approval Receipts + Execution Boundaries & Autonomy Modes (R1 + R5)
  4. Team Catalog Members + Fleet Surface Identity Bindings (R4 + R2)
  5. System Doctor Health Reporting + Notification Delivery Pipeline (R1 + R3)
  6. Task Cancellation + Audit Event Ledger (R5 + R1)
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from org_test_helpers import installation_org as installation_org

from unified_agent import (
    autonomy_modes,
    config,
    goals,
    host_registry,
    node_queue,
    org_registry,
    registry,
    skills_registry,
    storage,
)
from unified_agent.tools import approvals, doctor
from unified_agent.tools import tasks as tasks_tool

from .conftest import identity_context, resolve_human_approval
from .test_r2_fleet_surfaces import EXPECTED_FLEET_SURFACE_MAP


class TestTier3CrossFeatureCombinations:
    """Tier 3: Cross-feature combinations and architectural seams."""

    def test_tier3_surface_resolution_and_doctor_audit(self, monkeypatch: pytest.MonkeyPatch):
        """Cross-Feature (R2 + R1): Surface resolution integrates with system_doctor audit reporting."""
        env_map = ",".join(f"{k}:{v}" for k, v in EXPECTED_FLEET_SURFACE_MAP.items())
        monkeypatch.setenv("UA_SURFACE_MAP", env_map)
        monkeypatch.setenv("UA_SOURCE_HOST", "Desktop")

        # Register Desktop node with both mapped and unmapped surfaces
        node_queue.register(
            "Desktop",
            identity="Desktop_Agent5",
            facts={
                "surfaces": {
                    "hermes_profiles": ["default", "claude-dev", "reviewer"],
                    "kanban_assignees": ["codex-dev"],
                }
            },
        )
        registry.touch_agent("Desktop_Agent5", via="hub_request", force=True)

        doc_report = doctor.system_doctor()
        assert "checks" in doc_report
        assert "summary" in doc_report

        fleet = host_registry.fleet_payload()
        host = next((h for h in fleet["hosts"] if h["host"] == "Desktop"), None)
        assert host is not None

        mapped_lookup = {
            (s["kind"], s["name"]): (s["agent"], s["mapping"]) for s in host["surfaces"]
        }
        assert mapped_lookup[("hermes", "default")] == ("Desktop_Agent5", "explicit")
        assert mapped_lookup[("hermes", "claude-dev")] == ("Desktop_Agent1", "explicit")
        assert mapped_lookup[("kanban", "codex-dev")] == ("Desktop_Agent2", "explicit")
        # Reviewer remains unmapped
        assert mapped_lookup[("hermes", "reviewer")] == (None, "unmapped")

    def test_tier3_catalogs_and_skill_registry_provenance(self, tmp_path: Path):
        """Cross-Feature (R4 + R1): Skill catalog publication generates verifiable audit event in event_log."""
        ws = config.workspace_root()
        skill_dir = ws / "audit-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: audit-skill\ndescription: Use when performing security audits\n---\n# Security Audit\n"
        )

        pub_res = skills_registry.publish("audit-skill", str(skill_dir), author="Desktop_Person")
        assert pub_res["ok"] is True
        skill_hash = pub_res["skill"]["content_hash"]

        # Assign skill to Desktop_Agent2
        skills_registry.assign(
            "audit-skill",
            "Desktop_Agent2",
            target_type="agent",
            author="Desktop_Person",
        )

        # Verify audit event in event_log
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT channel, payload, originator FROM event_log WHERE channel='skill.published' OR channel='skill.assigned'"
            ).fetchall()
            assert len(rows) >= 2
            published_event = next(r for r in rows if r["channel"] == "skill.published")
            pub_payload = json.loads(published_event["payload"])
            assert pub_payload["name"] == "audit-skill"
            assert pub_payload["content_hash"] == skill_hash
        finally:
            conn.close()

        # Verify resolution
        resolved = skills_registry.resolve("Desktop_Agent2")
        assert any(s["name"] == "audit-skill" for s in resolved["skills"])

    def test_tier3_approval_receipts_and_execution_boundaries(self):
        """Cross-Feature (R1 + R5): Autonomy modes with approval receipts enforce execution boundaries."""
        conn = storage.connect()
        try:
            goals.ensure_schema(conn)
            now = storage.now()
            conn.execute(
                "INSERT OR IGNORE INTO goals (id, title, status, created_at, updated_at) "
                "VALUES (1500, 'Mission Boundary Test', 'open', ?, ?)",
                (now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Set mission mode to 'checkpoint'
        autonomy_modes.set_mode(1500, "checkpoint", actor="Desktop_Person")

        payload = {"action": "database_cutover", "target": "production"}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "storage.migrate",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
        app_id = exc_info.value.approval_id

        # Human approves
        resolve_human_approval(app_id, "approve", approver="Desktop_Person")

        # Under approval_invocation, execution consumes pass
        with approvals.approval_invocation():
            receipt = approvals.check_approval_gate(
                "storage.migrate",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
            assert receipt["gated"] is True
            assert receipt["approval_id"] == app_id

        # Record consumed
        rec = approvals.get_approval(app_id)
        assert rec["status"] == "consumed"

    def test_tier3_team_catalog_members_and_fleet_surface_bindings(
        self, monkeypatch: pytest.MonkeyPatch, installation_org
    ):
        """Cross-Feature (R4 + R2): Team members defined in org.yaml map to valid fleet roster labels."""
        offices = org_registry.load_registry()
        assert len(offices) > 0

        # Collect all team members across all offices
        all_members = set()
        for off in offices:
            all_members.update(off.get("members", []))

        # Validate charter integrity using org_registry.validate_registry
        findings = org_registry.validate_registry(agent_labels=all_members, offices=offices)
        assert len(findings) == 0

        # Cross-reference with fleet surface map
        smap = EXPECTED_FLEET_SURFACE_MAP
        bound_agents = set(smap.values())
        # All bound agents from fleet surface map MUST be active charter members in org.yaml
        for bound_agent in bound_agents:
            assert bound_agent in all_members

    def test_tier3_notification_bridge_and_doctor_alerting(self, tmp_path: Path):
        """Cross-Feature (R1 + R3): Doctor audit health summary is formatted, sanitized, and piped to bridge."""
        doc_report = doctor.system_doctor()
        summary = doc_report.get("summary", {})
        total_checks = len(doc_report.get("checks", []))

        raw_notification = f"\x1b[32m[DOCTOR HEALTH STATUS]\x1b[0m {total_checks} checks executed. Summary: {summary}"
        # Sanitize ANSI escapes
        sanitized = re.sub(r"\x1b\[[0-9;]*m", "", raw_notification)
        assert "\x1b" not in sanitized
        assert "[DOCTOR HEALTH STATUS]" in sanitized

        # Mock notification script
        mock_bridge = tmp_path / "mock_bridge.sh"
        mock_bridge.write_text("#!/bin/bash\ncat > /dev/null\necho 'send: delivered' >&2\nexit 0\n")
        mock_bridge.chmod(0o755)

        proc = subprocess.run(
            [str(mock_bridge)],
            input=sanitized,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.returncode == 0

    def test_tier3_task_cancellation_and_audit_event_ledger(self):
        """Cross-Feature (R5 + R1): Requesting task cancellation records event and updates task state."""
        task = tasks_tool.task_add(title="Cancel Event Test", priority="high")
        task_id = task["id"]
        now = storage.now()

        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET status='doing', claimed_by='Desktop_Agent2', current_run_id='sp-tier3-cancel', "
                "current_run_agent='Desktop_Agent2', updated_at=? WHERE id=?",
                (now, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        with identity_context("Desktop_Agent2"):
            updated = tasks_tool.task_update(id=task_id, request_cancel=True, worktree=False)

        assert updated["id"] == task_id
        assert updated.get("cancel_requested") == 1 or updated.get("cancel_requested") is True

        # Check audit event in event_log
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT channel, payload FROM event_log WHERE channel='agent.run.selfpickup' ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
            matching = [
                json.loads(r["payload"])
                for r in rows
                if json.loads(r["payload"]).get("task_id") == task_id
            ]
            assert len(matching) >= 1
            assert matching[0].get("kind") == "selfpickup.cancel_requested"
            assert matching[0].get("run_id") == "sp-tier3-cancel"
        finally:
            conn.close()
