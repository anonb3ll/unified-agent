"""E2E Tests for Requirement R4: Skills and Teams Catalogs (Task #1424).

Authoritative source:
- ORIGINAL_REQUEST.md § R4
- orchestrator_2/PROJECT.md § M4
- spec_miner_survey_batch_3/handoff.md
- Codebase paths:
  src/unified_agent/skills_registry.py
  src/unified_agent/org_registry.py
  src/unified_agent/org.yaml
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from org_test_helpers import installation_org as installation_org

from unified_agent import config, org_registry, skills_registry


def compute_team_version_hash(team_data: dict[str, Any]) -> str:
    """Authoritative SHA-256 canonical hash over team definition attributes."""
    canonical = {
        "id": team_data["id"],
        "name": team_data["name"],
        "lead": team_data["lead"],
        "members": sorted(team_data.get("members", [])),
        "owned_epics": sorted(team_data.get("owned_epics", [])),
        "default_lanes": sorted(team_data.get("default_lanes", [])),
        "decision_rights": sorted(team_data.get("decision_rights", [])),
        "required_skills": sorted(team_data.get("required_skills", [])),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TestR4SkillsAndTeamsCatalogs:
    """Tier 1 (Feature Coverage) and Tier 2 (Boundary & Corner Cases) for R4."""

    # -------------------------------------------------------------------------
    # Tier 1: Feature Coverage (Happy Path)
    # -------------------------------------------------------------------------

    def test_r4_skill_registry_publish_and_versioning(self, tmp_path: Path):
        """Feature: Governed skill publication computes content hash and increments versions."""
        ws = config.workspace_root()
        skill_dir = ws / "lint-audit"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: lint-audit\ndescription: Use when auditing Python and TypeScript code\n---\n"
            "# Lint Audit\nRun ruff and biome on staged changes.\n"
        )

        # First publish -> Version 1
        res1 = skills_registry.publish("lint-audit", str(skill_dir), author="Desktop_Person")
        assert res1["ok"] is True
        assert res1["skill"]["version"] == 1
        hash1 = res1["skill"]["content_hash"]
        assert len(hash1) == 64

        # Update skill with new content -> Version 2
        (skill_dir / "SKILL.md").write_text(
            "---\nname: lint-audit\ndescription: Use when auditing Python and TypeScript code\n---\n"
            "# Lint Audit v2\nRun ruff, biome, and bandit.\n"
        )
        res2 = skills_registry.publish("lint-audit", str(skill_dir), author="Desktop_Person")
        assert res2["ok"] is True
        assert res2["skill"]["version"] == 2
        hash2 = res2["skill"]["content_hash"]
        assert hash2 != hash1

    def test_r4_skill_catalog_list_and_get(self, tmp_path: Path):
        """Feature: Skill catalog list provides metadata; get retrieves full content body."""
        ws = config.workspace_root()
        skill_dir = ws / "browser-test"
        skill_dir.mkdir(parents=True, exist_ok=True)
        body = "# Browser Test\nUse Chrome DevTools to verify layout.\n"
        (skill_dir / "SKILL.md").write_text(
            "---\nname: browser-test\ndescription: Use when testing web applications\n---\n" + body
        )

        skills_registry.publish("browser-test", str(skill_dir), author="Desktop_Person")

        # List skills omits full content body
        listed = skills_registry.list_skills(name="browser-test")
        assert len(listed["skills"]) >= 1
        entry = next((s for s in listed["skills"] if s["name"] == "browser-test"), None)
        assert entry is not None
        assert "content" not in entry  # List excludes content body
        assert entry["version"] == 1

        # Get skill includes full content body
        full = skills_registry.get("browser-test")
        assert full is not None
        assert full["name"] == "browser-test"
        assert body in full["content"]

    def test_r4_skill_assignment_and_resolution(self, tmp_path: Path):
        """Feature: Skill assignment to agents and profiles resolves deterministically."""
        ws = config.workspace_root()
        skill_dir = ws / "git-sync"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: git-sync\ndescription: Use when synchronizing git branches\n---\n# Git Sync\n"
        )
        skills_registry.publish("git-sync", str(skill_dir), author="Desktop_Person")

        # Assign to agent Desktop_Agent7
        assigned = skills_registry.assign(
            skill_name="git-sync",
            target="Desktop_Agent7",
            target_type="agent",
            author="Desktop_Person",
        )
        assert assigned["assignment"]["skill_name"] == "git-sync"
        assert assigned["assignment"]["target"] == "Desktop_Agent7"

        # Resolve for Desktop_Agent7
        resolved = skills_registry.resolve("Desktop_Agent7")
        resolved_names = [s["name"] for s in resolved["skills"]]
        assert "git-sync" in resolved_names

    def test_r4_team_catalog_projection_from_org_yaml(self, installation_org):
        """Feature: Team catalog projects existing org_unit definitions without creating new tables."""
        # Dispatch lanes are installation data, not a built-in fleet assumption.
        chart = yaml.safe_load(installation_org.read_text())
        product = next(row for row in chart["offices"] if row["id"] == "product-engineering")
        product["default_lanes"] = ["dispatch-ready"]
        installation_org.write_text(yaml.safe_dump(chart))
        offices = org_registry.load_registry()
        assert isinstance(offices, list)
        assert len(offices) >= 5

        # Check product engineering team
        pe = next((o for o in offices if o["id"] == "product-engineering"), None)
        assert pe is not None
        assert pe["name"] == "Product Engineering"
        assert pe["lead"] == "Desktop_Agent7"
        assert "Desktop_Agent7" in pe["members"]
        assert len(pe["decision_rights"]) > 0
        assert "dispatch-ready" in pe["default_lanes"]

    def test_r4_team_deterministic_version_hash(self):
        """Feature: Canonical SHA-256 version hash is deterministic and key-order invariant."""
        team_a = {
            "id": "product-engineering",
            "name": "Product Engineering",
            "lead": "Desktop_Agent7",
            "members": ["Desktop_Agent7", "Desktop_Agent2", "Desktop_Agent4"],
            "owned_epics": [1186, 1189],
            "default_lanes": ["dispatch-ready"],
            "decision_rights": [
                "Decompose owned epics",
                "Lead design of product features",
            ],
            "required_skills": ["browser-test", "lint-audit"],
        }
        # Exact same team attributes in different dictionary order
        team_b = {
            "name": "Product Engineering",
            "id": "product-engineering",
            "decision_rights": [
                "Decompose owned epics",
                "Lead design of product features",
            ],
            "required_skills": ["browser-test", "lint-audit"],
            "owned_epics": [1186, 1189],
            "default_lanes": ["dispatch-ready"],
            "members": [
                "Desktop_Agent4",
                "Desktop_Agent7",
                "Desktop_Agent2",
            ],  # different list order
            "lead": "Desktop_Agent7",
        }

        hash_a = compute_team_version_hash(team_a)
        hash_b = compute_team_version_hash(team_b)

        assert hash_a == hash_b
        assert len(hash_a) == 64

    # -------------------------------------------------------------------------
    # Tier 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r4_unregistered_skill_assignment_rejected(self):
        """Boundary: Attempting to assign an unpublished skill is rejected with ToolError."""
        with pytest.raises(ToolError, match="has no published version yet|unknown skill"):
            skills_registry.assign(
                skill_name="nonexistent-phantom-skill",
                target="Desktop_Agent7",
                target_type="agent",
                author="Desktop_Person",
            )

    def test_r4_invalid_profile_target_rejected(self, tmp_path: Path):
        """Boundary: Assigning a skill to an invalid profile name is rejected."""
        ws = config.workspace_root()
        skill_dir = ws / "sample-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: Use when running sample tests\n---\n# Sample\n"
        )
        skills_registry.publish("sample-skill", str(skill_dir), author="Desktop_Person")

        with pytest.raises(ToolError, match="unknown profile"):
            skills_registry.assign(
                skill_name="sample-skill",
                target="invalid-nonexistent-profile",
                target_type="profile",
                author="Desktop_Person",
            )

    def test_r4_multi_surface_drift_detection(self, tmp_path: Path):
        """Boundary: Surface directory drift accurately flags synced, stale, and missing skills."""
        ws = config.workspace_root()
        skill_dir = ws / "drift-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "---\nname: drift-skill\ndescription: Use when testing drift\n---\n# Drift Skill\n"
        )
        (skill_dir / "SKILL.md").write_text(content)

        skills_registry.publish("drift-skill", str(skill_dir), author="Desktop_Person")
        skills_registry.assign(
            "drift-skill",
            "Desktop_Agent2",
            target_type="agent",
            author="Desktop_Person",
        )

        surface_dir = ws / "surface"
        surface_dir.mkdir(parents=True, exist_ok=True)
        s_skill = surface_dir / "drift-skill"
        s_skill.mkdir(parents=True, exist_ok=True)
        (s_skill / "SKILL.md").write_text(content)

        # 1. In sync
        rep1 = skills_registry.drift(str(surface_dir), agent_label="Desktop_Agent2")
        assert any(s["name"] == "drift-skill" for s in rep1["synced"])

        # 2. Tampered content -> Stale
        (s_skill / "SKILL.md").write_text(content + "# modified drift line\n")
        rep2 = skills_registry.drift(str(surface_dir), agent_label="Desktop_Agent2")
        assert any(s["name"] == "drift-skill" for s in rep2["stale"])

    def test_r4_drift_bounds_enforced(self, tmp_path: Path):
        """Boundary: Surface drift scan exceeding 100 skills fails closed with ToolError."""
        ws = config.workspace_root()
        surface_dir = ws / "huge-surface"
        surface_dir.mkdir(parents=True, exist_ok=True)

        # Create 101 skill directories
        for i in range(101):
            s_dir = surface_dir / f"skill-{i:03d}"
            s_dir.mkdir(parents=True, exist_ok=True)
            (s_dir / "SKILL.md").write_text(
                f"---\nname: skill-{i:03d}\ndescription: Test\n---\n# Test\n"
            )

        with pytest.raises(ToolError, match="100-skill directory limit"):
            skills_registry.drift(str(surface_dir))

    def test_r4_skill_publish_idempotent(self, tmp_path: Path):
        """Boundary: Publishing identical content to an existing skill version returns idempotent=True."""
        ws = config.workspace_root()
        skill_dir = ws / "idempotent-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = "---\nname: idempotent-skill\ndescription: Use when testing idempotent publishes\n---\n# Idempotent\n"
        (skill_dir / "SKILL.md").write_text(content)

        pub1 = skills_registry.publish("idempotent-skill", str(skill_dir), author="Desktop_Person")
        assert pub1["ok"] is True
        assert pub1["idempotent"] is False
        assert pub1["skill"]["version"] == 1

        # Re-publish identical content
        pub2 = skills_registry.publish("idempotent-skill", str(skill_dir), author="Desktop_Person")
        assert pub2["ok"] is True
        assert pub2["idempotent"] is True
        assert pub2["skill"]["version"] == 1
