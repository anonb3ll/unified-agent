"""Tier 4: Real-World Application Scenarios for Batch 2 Requirements.

Authoritative source:
- ORIGINAL_REQUEST.md
- orchestrator_2/PROJECT.md § Milestones & Real-World Tracks
- explorer_survey_batch_1/handoff.md
- explorer_survey_batch_2/handoff.md
- spec_miner_survey_batch_3/handoff.md

Scenarios:
1. Scenario 1: Full Fleet Bootstrap & Doctor Verification
2. Scenario 2: End-to-End Skill & Team Installation with Multi-Surface Drift Checking
3. Scenario 3: Governed Chief-of-Staff Briefing Notification Delivery Pipeline
4. Scenario 4: Autonomous Approval Lifecycle & Migration Integrity
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unified_agent import (
    autonomy_modes,
    config,
    goals,
    host_registry,
    node_queue,
    project_bindings,
    registry,
    skills_registry,
    storage,
)
from unified_agent.tools import approvals, doctor

from .conftest import resolve_human_approval
from .test_r2_fleet_surfaces import EXPECTED_FLEET_SURFACE_MAP


class TestTier4RealWorldApplicationScenarios:
    """Tier 4: End-to-End Real-World Application Scenarios."""

    def test_tier4_scenario_full_fleet_bootstrap_and_doctor_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Scenario 1: Complete multi-node fleet bootstrap, surface mapping, and doctor audit verification."""
        env_map = ",".join(f"{k}:{v}" for k, v in EXPECTED_FLEET_SURFACE_MAP.items())
        monkeypatch.setenv("UA_SURFACE_MAP", env_map)
        monkeypatch.setenv("UA_SOURCE_HOST", "hubhost")

        # 1. Bootstrap Desktop Node with reported surfaces
        node_queue.register(
            "Desktop",
            identity="Desktop_Agent5",
            facts={
                "surfaces": {
                    "hermes_profiles": [
                        "default",
                        "agy-dev",
                        "claude-dev",
                        "codex-dev",
                        "cursor-dev",
                        "reviewer",
                    ],
                    "kanban_assignees": [
                        "claude-dev",
                        "codex-dev",
                        "agy-dev",
                        "cursor-dev",
                    ],
                }
            },
        )
        registry.touch_agent("Desktop_Agent5", via="hub_request", force=True)

        # 2. Bootstrap Mac Hub Node with reported surfaces
        node_queue.register(
            "hub-host.local",
            identity="server_agent1",
            facts={
                "surfaces": {
                    "hermes_profiles": [
                        "agy-dev",
                        "claude-dev",
                        "codex-dev",
                        "cursor-dev",
                        "reviewer",
                    ],
                    "kanban_assignees": ["claude-dev"],
                }
            },
        )
        registry.touch_agent("server_agent1", via="hub_request", force=True)

        # 3. Bind authoritative project repository
        project_bindings.bind(
            project="unified-agent",
            repo="https://github.com/anonb3ll/unified-agent.git",
            bound_by="Desktop_Person",
        )

        # 4. Execute Fleet Doctor Audit
        doc_report = doctor.system_doctor()
        assert "checks" in doc_report
        assert len(doc_report["checks"]) > 0

        # 5. Verify Fleet Payload Surface Mapping
        fleet = host_registry.fleet_payload()
        hosts_by_name = {h["host"]: h for h in fleet["hosts"]}

        assert "Desktop" in hosts_by_name
        desktop_surfaces = {
            (s["kind"], s["name"]): (s["agent"], s["mapping"])
            for s in hosts_by_name["Desktop"]["surfaces"]
        }
        assert desktop_surfaces[("hermes", "default")] == ("Desktop_Agent5", "explicit")
        assert desktop_surfaces[("hermes", "claude-dev")] == (
            "Desktop_Agent1",
            "explicit",
        )
        assert desktop_surfaces[("hermes", "reviewer")] == (None, "unmapped")
        assert desktop_surfaces[("kanban", "codex-dev")] == (
            "Desktop_Agent2",
            "explicit",
        )

        assert "hub-host.local" in hosts_by_name
        mac_surfaces = {
            (s["kind"], s["name"]): (s["agent"], s["mapping"])
            for s in hosts_by_name["hub-host.local"]["surfaces"]
        }
        assert mac_surfaces[("hermes", "claude-dev")] == ("server_agent1", "explicit")
        assert mac_surfaces[("hermes", "codex-dev")] == ("server_agent2", "explicit")
        assert mac_surfaces[("hermes", "reviewer")] == (None, "unmapped")
        assert mac_surfaces[("kanban", "claude-dev")] == ("server_agent1", "explicit")

    def test_tier4_scenario_end_to_end_skill_installation_and_drift_checking(self, tmp_path: Path):
        """Scenario 2: Multi-skill publication, team assignment, disk materialization, and drift detection."""
        ws = config.workspace_root()

        # 1. Publish Skill 1: steward
        steward_dir = ws / "steward"
        steward_dir.mkdir(parents=True, exist_ok=True)
        steward_content = (
            "---\nname: steward\ndescription: Use when acting as senior engineer or steward reviewer\n---\n"
            "# Steward Review\nConduct independent cross-cutting review.\n"
        )
        (steward_dir / "SKILL.md").write_text(steward_content)
        pub_steward = skills_registry.publish("steward", str(steward_dir), author="Desktop_Person")
        assert pub_steward["ok"] is True

        # 2. Publish Skill 2: a11y-debugging
        a11y_dir = ws / "a11y-debugging"
        a11y_dir.mkdir(parents=True, exist_ok=True)
        a11y_content = (
            "---\nname: a11y-debugging\ndescription: Use when debugging web accessibility issues\n---\n"
            "# A11y Debugging\nRun Chrome DevTools audits for ARIA and contrast.\n"
        )
        (a11y_dir / "SKILL.md").write_text(a11y_content)
        pub_a11y = skills_registry.publish("a11y-debugging", str(a11y_dir), author="Desktop_Person")
        assert pub_a11y["ok"] is True

        # 3. Assign skills to Desktop_Agent7
        skills_registry.assign(
            "steward", "Desktop_Agent7", target_type="agent", author="Desktop_Person"
        )
        skills_registry.assign(
            "a11y-debugging",
            "Desktop_Agent7",
            target_type="agent",
            author="Desktop_Person",
        )

        # 4. Materialize onto agent surface directory
        surface_dir = ws / "agent-skills-surface"
        surface_dir.mkdir(parents=True, exist_ok=True)

        # steward on disk matches registry
        s_steward = surface_dir / "steward"
        s_steward.mkdir(parents=True, exist_ok=True)
        (s_steward / "SKILL.md").write_text(steward_content)

        # a11y-debugging on disk is modified (tampered)
        s_a11y = surface_dir / "a11y-debugging"
        s_a11y.mkdir(parents=True, exist_ok=True)
        (s_a11y / "SKILL.md").write_text(a11y_content + "# Unauthorized local edit\n")

        # Add an unregistered skill directly on disk
        s_unregistered = surface_dir / "rogue-skill"
        s_unregistered.mkdir(parents=True, exist_ok=True)
        (s_unregistered / "SKILL.md").write_text(
            "---\nname: rogue-skill\ndescription: Unknown\n---\n# Rogue\n"
        )

        # 5. Run Unified Drift Detection
        drift_report = skills_registry.drift(str(surface_dir), agent_label="Desktop_Agent7")

        synced_names = [s["name"] for s in drift_report["synced"]]
        stale_names = [s["name"] for s in drift_report["stale"]]
        unregistered_names = [s["name"] for s in drift_report["unregistered"]]

        assert "steward" in synced_names
        assert "a11y-debugging" in stale_names
        assert "rogue-skill" in unregistered_names

    # The briefing notification pipeline is exercised against the real
    # deploy/mac-hub/run-cluster-briefing.sh in
    # tests/test_mac_briefing_entrypoint.py. An inline re-implementation here
    # proved nothing about production delivery (Devin review, PR #922).

    def test_tier4_scenario_autonomous_approval_lifecycle_and_migration_integrity(self):
        """Scenario 4: Complete approval lifecycle under mission autonomy mode with audit verification."""
        conn = storage.connect()
        try:
            goals.ensure_schema(conn)
            now = storage.now()
            conn.execute(
                "INSERT OR IGNORE INTO goals (id, title, status, created_at, updated_at) "
                "VALUES (1600, 'Autonomous Deployment Mission', 'open', ?, ?)",
                (now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Set mission mode to 'checkpoint'
        autonomy_modes.set_mode(
            1600, "checkpoint", actor="Desktop_Person", reason="Mission deployment"
        )

        # Agent triggers deployment command requiring approval
        payload = {"command": "deploy-mac-hub.sh --target 100.64.0.10", "force": True}
        with pytest.raises(approvals.ApprovalPendingError) as exc_info:
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
        app_id = exc_info.value.approval_id

        # Verification: Approval record created in pending status
        rec_pending = approvals.get_approval(app_id)
        assert rec_pending["status"] == "pending"

        # Authorized human signs approval
        resolve_human_approval(app_id, "approve", approver="Desktop_Person")
        rec_approved = approvals.get_approval(app_id)
        assert rec_approved["status"] == "approved"
        assert rec_approved["resolved_by"] == "Desktop_Person"

        # Execution proceeds under approval_invocation
        with approvals.approval_invocation():
            receipt = approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
            assert receipt["gated"] is True
            assert receipt["approval_id"] == app_id

        # Verification: Pass is consumed
        rec_consumed = approvals.get_approval(app_id)
        assert rec_consumed["status"] == "consumed"

        # Verification: Audit event logged in event_log
        conn = storage.connect()
        try:
            events_rows = conn.execute(
                f"SELECT channel, payload FROM event_log WHERE channel='{approvals._CONSUMPTION_CHANNEL}' ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
            matching = [
                json.loads(r["payload"])
                for r in events_rows
                if json.loads(r["payload"]).get("approval_id") == app_id
            ]
            assert len(matching) == 1
            assert matching[0]["tool"] == "shell.run_cmd"
        finally:
            conn.close()

        # Re-execution attempt without new approval raises ApprovalPendingError
        with pytest.raises(approvals.ApprovalPendingError) as exc_info2:
            approvals.check_approval_gate(
                "shell.run_cmd",
                payload,
                requested_by="Desktop_Agent7",
                force=True,
            )
        assert exc_info2.value.approval_id != app_id
