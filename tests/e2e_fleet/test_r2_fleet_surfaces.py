"""E2E Tests for Requirement R2: Fleet Surface Identity Bindings (Task #1538).

Authoritative source:
- ORIGINAL_REQUEST.md § R2
- orchestrator_2/PROJECT.md § M2
- explorer_survey_batch_2/handoff.md
"""

from __future__ import annotations

import pytest

from unified_agent import config, host_registry, node_queue, registry

EXPECTED_FLEET_SURFACE_MAP = {
    # Desktop (Linux desktop)
    "Desktop/hermes/default": "Desktop_Agent5",
    "Desktop/hermes/agy-dev": "Desktop_agent3",
    "Desktop/hermes/claude-dev": "Desktop_Agent1",
    "Desktop/hermes/codex-dev": "Desktop_Agent2",
    "Desktop/hermes/cursor-dev": "Desktop_Agent4",
    "Desktop/kanban/claude-dev": "Desktop_Agent1",
    "Desktop/kanban/codex-dev": "Desktop_Agent2",
    "Desktop/kanban/agy-dev": "Desktop_agent3",
    "Desktop/kanban/cursor-dev": "Desktop_Agent4",
    # Installation hub (generic fixture)
    "hub-host.local/hermes/agy-dev": "server_agent3",
    "hub-host.local/hermes/claude-dev": "server_agent1",
    "hub-host.local/hermes/codex-dev": "server_agent2",
    "hub-host.local/hermes/cursor-dev": "server_agent4",
    "hub-host.local/kanban/claude-dev": "server_agent1",
}


def _active_surface_map(
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> dict[str, str]:
    """Install the explicit fixture map; shipped defaults contain no fleet."""
    env_str = ",".join(f"{k}:{v}" for k, v in EXPECTED_FLEET_SURFACE_MAP.items())
    if monkeypatch:
        monkeypatch.setenv("UA_SURFACE_MAP", env_str)
        return config.surface_map()
    return config.surface_map(env_str)


class TestR2FleetSurfaceIdentityBindings:
    """Tier 1 (Feature Coverage) and Tier 2 (Boundary & Corner Cases) for R2."""

    # -------------------------------------------------------------------------
    # Tier 1: Feature Coverage (Happy Path)
    # -------------------------------------------------------------------------

    def test_r2_desktop_fleet_surface_bindings(self, monkeypatch: pytest.MonkeyPatch):
        """Feature: Desktop profiles and kanban lanes resolve to exact Desktop agent roster identities."""
        smap = _active_surface_map(monkeypatch)
        surfaces = {
            "hermes_profiles": [
                "default",
                "agy-dev",
                "claude-dev",
                "codex-dev",
                "cursor-dev",
            ],
            "kanban_assignees": ["claude-dev", "codex-dev", "agy-dev", "cursor-dev"],
        }
        mapped = host_registry.map_surfaces(
            "Desktop",
            surfaces,
            mapping=smap,
            labels=config.agent_labels(),
        )
        by_key = {(r["kind"], r["name"]): (r["agent"], r["mapping"]) for r in mapped}

        assert by_key[("hermes", "default")] == ("Desktop_Agent5", "explicit")
        assert by_key[("hermes", "agy-dev")] == ("Desktop_agent3", "explicit")
        assert by_key[("hermes", "claude-dev")] == ("Desktop_Agent1", "explicit")
        assert by_key[("hermes", "codex-dev")] == ("Desktop_Agent2", "explicit")
        assert by_key[("hermes", "cursor-dev")] == ("Desktop_Agent4", "explicit")
        assert by_key[("kanban", "claude-dev")] == ("Desktop_Agent1", "explicit")
        assert by_key[("kanban", "codex-dev")] == ("Desktop_Agent2", "explicit")
        assert by_key[("kanban", "agy-dev")] == ("Desktop_agent3", "explicit")
        assert by_key[("kanban", "cursor-dev")] == ("Desktop_Agent4", "explicit")

    def test_r2_mac_hub_surface_bindings(self, monkeypatch: pytest.MonkeyPatch):
        """Feature: Mac Hub profiles and kanban lanes resolve to exact server_* agent roster identities."""
        smap = _active_surface_map(monkeypatch)
        surfaces = {
            "hermes_profiles": ["agy-dev", "claude-dev", "codex-dev", "cursor-dev"],
            "kanban_assignees": ["claude-dev"],
        }
        mapped = host_registry.map_surfaces(
            "hub-host.local",
            surfaces,
            mapping=smap,
            labels=config.agent_labels(),
        )
        by_key = {(r["kind"], r["name"]): (r["agent"], r["mapping"]) for r in mapped}

        assert by_key[("hermes", "agy-dev")] == ("server_agent3", "explicit")
        assert by_key[("hermes", "claude-dev")] == ("server_agent1", "explicit")
        assert by_key[("hermes", "codex-dev")] == ("server_agent2", "explicit")
        assert by_key[("hermes", "cursor-dev")] == ("server_agent4", "explicit")
        assert by_key[("kanban", "claude-dev")] == ("server_agent1", "explicit")

    def test_r2_explicit_surface_map_override_precedence(self, monkeypatch: pytest.MonkeyPatch):
        """Feature: Explicit UA_SURFACE_MAP entry overrides fleet default mapping."""
        # Override Desktop/hermes/default to Desktop_Person
        custom_mapping = "Desktop/hermes/default:Desktop_Person"
        monkeypatch.setenv("UA_SURFACE_MAP", custom_mapping)
        smap = config.surface_map()

        assert smap.get("Desktop/hermes/default") == "Desktop_Person"

        mapped = host_registry.map_surfaces(
            "Desktop",
            {"hermes_profiles": ["default"]},
            mapping=smap,
            labels=config.agent_labels(),
        )
        assert mapped[0]["agent"] == "Desktop_Person"
        assert mapped[0]["mapping"] == "explicit"

    def test_r2_exact_roster_profile_matching(self):
        """Feature: Surface whose profile name exactly matches a known roster identity resolves with mapping='exact'."""
        mapped = host_registry.map_surfaces(
            "Desktop",
            {"hermes_profiles": ["Desktop_Agent2"]},
            mapping={},
            labels={"Desktop_Agent2", "Desktop_Agent5"},
        )
        assert len(mapped) == 1
        assert mapped[0]["agent"] == "Desktop_Agent2"
        assert mapped[0]["mapping"] == "exact"

    def test_r2_host_registry_fleet_payload_integration(self, monkeypatch: pytest.MonkeyPatch):
        """Feature: host_registry.fleet_payload incorporates mapped surfaces into host report."""
        monkeypatch.setenv("UA_SOURCE_HOST", "Desktop")
        monkeypatch.setenv(
            "UA_SURFACE_MAP",
            "Desktop/hermes/default:Desktop_Agent5,Desktop/kanban/claude-dev:Desktop_Agent1",
        )

        node_queue.register(
            "Desktop",
            identity="Desktop_Agent5",
            facts={
                "surfaces": {
                    "hermes_profiles": ["default", "reviewer"],
                    "kanban_assignees": ["claude-dev"],
                }
            },
        )
        registry.touch_agent("Desktop_Agent5", via="hub_request", force=True)

        payload = host_registry.fleet_payload()
        host = next((h for h in payload["hosts"] if h["host"] == "Desktop"), None)
        assert host is not None

        surfaces_by_key = {
            (s["kind"], s["name"]): (s["agent"], s["mapping"]) for s in host["surfaces"]
        }
        assert surfaces_by_key[("hermes", "default")] == ("Desktop_Agent5", "explicit")
        assert surfaces_by_key[("kanban", "claude-dev")] == (
            "Desktop_Agent1",
            "explicit",
        )
        assert surfaces_by_key[("hermes", "reviewer")] == (None, "unmapped")

    # -------------------------------------------------------------------------
    # Tier 2: Boundary & Corner Cases
    # -------------------------------------------------------------------------

    def test_r2_unverified_reviewer_surface_strictly_unmapped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Boundary: Unverified generic 'reviewer' profile remains strictly unmapped on all nodes."""
        smap = _active_surface_map(monkeypatch)
        desktop_mapped = host_registry.map_surfaces(
            "Desktop",
            {"hermes_profiles": ["reviewer"]},
            mapping=smap,
            labels=config.agent_labels(),
        )
        assert desktop_mapped[0]["agent"] is None
        assert desktop_mapped[0]["mapping"] == "unmapped"

        mac_mapped = host_registry.map_surfaces(
            "hub-host.local",
            {"hermes_profiles": ["reviewer"]},
            mapping=smap,
            labels=config.agent_labels(),
        )
        assert mac_mapped[0]["agent"] is None
        assert mac_mapped[0]["mapping"] == "unmapped"

    def test_r2_conflicted_surface_keys_dropped_fail_closed(self):
        """Boundary: A surface key mapped to conflicting identities in UA_SURFACE_MAP is dropped entirely."""
        raw = "Desktop/hermes/agy-dev:Desktop_agent3,Desktop/hermes/agy-dev:Desktop_Agent2,Desktop/hermes/claude-dev:Desktop_Agent1"
        parsed = config.surface_map(raw)

        # Conflicted key Desktop/hermes/agy-dev must be excluded
        assert "Desktop/hermes/agy-dev" not in parsed
        # Non-conflicted key Desktop/hermes/claude-dev is retained
        assert parsed["Desktop/hermes/claude-dev"] == "Desktop_Agent1"

    def test_r2_unknown_profile_strictly_unmapped(self, monkeypatch: pytest.MonkeyPatch):
        """Boundary: Unregistered arbitrary profiles (e.g. 'random-bot', 'tester') map to unmapped."""
        smap = _active_surface_map(monkeypatch)
        mapped = host_registry.map_surfaces(
            "Desktop",
            {"hermes_profiles": ["random-bot", "unregistered_profile"]},
            mapping=smap,
            labels=config.agent_labels(),
        )
        for entry in mapped:
            assert entry["agent"] is None
            assert entry["mapping"] == "unmapped"

    def test_r2_surface_map_raw_isolated_parsing(self):
        """Boundary: Passing explicit raw string to config.surface_map(raw) parses ONLY raw."""
        res = config.surface_map("a/hermes/x:L1,a/hermes/x:L2,b/kanban/y:L3")
        assert res == {"b/kanban/y": "L3"}

    def test_r2_whitespace_and_empty_entries_ignored(self):
        """Boundary: Trailing commas, empty lines, and excessive whitespace parse safely."""
        raw = "  \n  Desktop/hermes/default: Desktop_Agent5 , , \n Desktop/hermes/claude-dev:Desktop_Agent1\n"
        res = config.surface_map(raw)
        assert res == {
            "Desktop/hermes/default": "Desktop_Agent5",
            "Desktop/hermes/claude-dev": "Desktop_Agent1",
        }
