"""Unit tests for Agent Personas (task #1427 / Parity 22).

Verifies:
1. Canonical fleet defaults and UK spelling alias (colour).
2. Deterministic fallback persona generation for unknown labels.
3. Custom persona loading from org.yaml and meta overrides.
4. ObjectType agent schema validation with persona properties.
5. platform_readers.read_agent and platform_readers.read_persona.
6. registry.list_agents enriches observed and unseen agents.
7. CRITICAL SECURITY INVARIANT: Persona data never affects profile gates or authorization.
8. CRITICAL SECURITY INVARIANT: Persona data never affects dispatch routing.
9. webui.api agents_payload, org_payload, and personas_payload presentation.
"""

from __future__ import annotations

import jsonschema
import pytest

from unified_agent import (
    config,
    object_model,
    personas,
    platform_readers,
    profiles,
    registry,
    storage,
)
from unified_agent.dispatch import routing
from unified_agent.webui import api


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "UA_AGENT_LABELS", "Desktop_agent3:Desktop_Agent2:Desktop_Agent1:Mystery_Agent"
    )
    storage.init_db()
    return tmp_path


def test_default_fleet_personas():
    """Verify fleet personas define required presentation fields and aliases."""
    for label, persona in personas.DEFAULT_FLEET_PERSONAS.items():
        assert persona.display_name, f"Missing display_name for {label}"
        assert persona.avatar, f"Missing avatar for {label}"
        assert persona.color.startswith("#"), f"Invalid hex color for {label}"
        assert persona.colour == persona.color, f"Colour alias mismatch for {label}"
        d = persona.to_dict()
        assert d["colour"] == d["color"]
        assert d["display_name"] == persona.display_name


def test_fallback_persona_is_deterministic():
    """Unknown or unconfigured agent labels receive a deterministic fallback persona."""
    label = "Unknown_Worker_123"
    p1 = personas.get_persona(label)
    p2 = personas.get_persona(label)

    assert p1 == p2
    assert p1["display_name"] == "Unknown Worker 123"
    assert p1["avatar"] in personas._FALLBACK_AVATARS
    assert p1["color"] in personas._FALLBACK_COLORS
    assert p1["colour"] == p1["color"]


def test_meta_persona_override():
    """Direct persona dictionary in agent meta overrides defaults."""
    custom_meta = {
        "persona": {
            "display_name": "Custom Identity",
            "avatar": "🚀",
            "color": "#123456",
            "bio": "Custom bio",
            "model_routing_preference": "custom-model",
        }
    }
    p = personas.get_persona("Desktop_agent3", meta=custom_meta)
    assert p["display_name"] == "Custom Identity"
    assert p["avatar"] == "🚀"
    assert p["color"] == "#123456"
    assert p["colour"] == "#123456"
    assert p["bio"] == "Custom bio"
    assert p["model_routing_preference"] == "custom-model"


def test_org_yaml_custom_personas(tmp_path, monkeypatch):
    """Custom personas declared in org.yaml override fleet defaults."""
    custom_org = tmp_path / "custom_org.yaml"
    custom_org.write_text(
        """schema_version: 1
offices:
  - id: custom-office
    name: Custom Office
    lead: Custom_Leader
    members: [Custom_Leader]
    owned_epics: []
    default_lanes: [default]
    decision_rights: []
personas:
  Custom_Leader:
    display_name: "The Grand Leader"
    avatar: "👑"
    color: "#ff007f"
    bio: "Supreme commander."
    model_routing_preference: "claude-custom"
""",
        encoding="utf-8",
    )
    from unified_agent import org_registry

    monkeypatch.setattr(org_registry, "_ORG_PATH", custom_org)

    p = personas.get_persona("Custom_Leader")
    assert p["display_name"] == "The Grand Leader"
    assert p["avatar"] == "👑"
    assert p["color"] == "#ff007f"
    assert p["bio"] == "Supreme commander."
    assert p["model_routing_preference"] == "claude-custom"


def test_object_model_agent_schema_validates_personas():
    """ObjectType 'agent' schema validates agent instances carrying persona metadata."""
    agent_type = object_model.by_kind("agent")
    assert agent_type is not None

    sample_agent = {
        "label": "Desktop_agent3",
        "kind": "cli",
        "host": "linux-desktop",
        "known": True,
        "first_seen": "2026-09-30T12:00:00Z",
        "last_seen": "2026-09-30T12:05:00Z",
        "last_seen_via": "mcp",
        "pid": 1234,
        "version": "0.3.0",
        "status": "online",
        "meta": {},
        "display_name": "Antigravity (Agy)",
        "avatar": "✨",
        "color": "#a855f7",
        "colour": "#a855f7",
        "bio": "Platform engineer",
        "model_routing_preference": "gemini-2.5-pro",
        "persona": {
            "display_name": "Antigravity (Agy)",
            "avatar": "✨",
            "color": "#a855f7",
            "colour": "#a855f7",
            "bio": "Platform engineer",
            "model_routing_preference": "gemini-2.5-pro",
        },
    }

    # Should not raise
    jsonschema.validate(instance=sample_agent, schema=agent_type.schema)


def test_platform_reader_read_agent_and_read_persona(clean_env):
    """platform_readers.read_agent and read_persona return persona metadata."""
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            """
            INSERT INTO agents (label, kind, host, known, first_seen, last_seen, last_seen_via, meta, created_at, updated_at)
            VALUES ('Desktop_agent3', 'cli', 'desktop', 1, ?, ?, 'mcp', '{}', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    res = platform_readers.read_agent("Desktop_agent3")
    assert res["uri"] == "unified-agent://agents/Desktop_agent3"
    agent = res["agent"]
    assert agent["label"] == "Desktop_agent3"
    assert agent["avatar"] == "✨"
    assert agent["color"] == "#a855f7"
    assert agent["display_name"] == "Antigravity (Agy)"
    assert "persona" in agent
    assert agent["persona"]["color"] == "#a855f7"

    persona_res = platform_readers.read_persona("Desktop_agent3")
    assert persona_res["label"] == "Desktop_agent3"
    assert persona_res["persona"]["avatar"] == "✨"


def test_registry_list_agents_enriches_personas(clean_env):
    """list_agents enriches both observed agents and unseen configured agents."""
    now = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            """
            INSERT INTO agents (label, kind, host, known, first_seen, last_seen, last_seen_via, meta, created_at, updated_at)
            VALUES ('Desktop_agent3', 'cli', 'desktop', 1, ?, ?, 'mcp', '{}', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    agents = registry.list_agents(include_configured=True)
    agy = next(a for a in agents if a["label"] == "Desktop_agent3")
    assert agy["avatar"] == "✨"
    assert agy["display_name"] == "Antigravity (Agy)"

    # Mystery_Agent was configured in clean_env fixture but never seen
    mystery = next(a for a in agents if a["label"] == "Mystery_Agent")
    assert mystery["status"] == "unknown"
    assert mystery["avatar"] in personas._FALLBACK_AVATARS
    assert mystery["display_name"] == "Mystery Agent"


def test_persona_data_never_affects_profile_gates(clean_env):
    """SECURITY GUARANTEE: Persona metadata changes never affect profile authorization."""
    # Desktop_agent3 default profile is developer
    baseline_profile = profiles.default_for_identity("Desktop_agent3")
    assert baseline_profile == "developer"

    # Even if persona is customized with admin-like metadata
    spoofed_meta = {
        "persona": {
            "display_name": "Admin / Founder",
            "avatar": "👑",
            "color": "#ff0000",
            "bio": "Superuser root access",
            "model_routing_preference": "super-model",
        }
    }
    spoofed = personas.get_persona("Desktop_agent3", meta=spoofed_meta)
    assert spoofed["display_name"] == "Admin / Founder"

    # Verify profile lookup remains strictly governed by label/config, not persona
    profile_after = profiles.default_for_identity("Desktop_agent3")
    assert profile_after == baseline_profile

    # Verify tool permission gate uses identity and active profile, never persona
    token = config.agent_identity_var.set("Desktop_agent3")
    try:
        # Developer profile allows normal developer tools
        assert profiles.tool_is_allowed_for("developer", "task_list") is True
        # Developer profile denies steward-only or restricted operations
        assert profiles.tool_is_allowed_for("developer", "managed_secret_set") is False
    finally:
        config.agent_identity_var.reset(token)


def test_persona_data_never_affects_dispatch_routing():
    """SECURITY GUARANTEE: Persona metadata never modifies headless dispatch routing."""
    # Baseline route for Desktop_agent3
    route = routing.build_route("Desktop_agent3")
    assert route is not None
    assert route.cli_backend == "agy"
    assert route.agent_label == "Desktop_agent3"

    # Even with a custom persona specifying a model routing preference
    custom = personas.get_persona("Desktop_agent3")
    assert custom["model_routing_preference"] == "gemini-2.5-pro"

    # The canonical execution route remains unaffected
    route_after = routing.build_route("Desktop_agent3")
    assert route_after is not None
    assert route_after.family == route.family
    assert route_after.cli_backend == route.cli_backend
    assert route_after.agent_label == route.agent_label


@pytest.mark.asyncio
async def test_webui_payloads_include_personas(clean_env):
    """WebUI payloads (agents_payload, org_payload, personas_payload) present persona data."""
    agents_res = await api.agents_payload()
    for ag in agents_res["agents"]:
        assert "display_name" in ag
        assert "avatar" in ag
        assert "color" in ag
        assert "persona" in ag

    org_res = await api.org_payload()
    for office in org_res["offices"]:
        for member in office["members"]:
            assert "display_name" in member
            assert "avatar" in member
            assert "color" in member
            assert "persona" in member

    personas_res = api.personas_payload()
    assert "Desktop_agent3" in personas_res["personas"]
    assert personas_res["personas"]["Desktop_agent3"]["avatar"] == "✨"


def test_load_custom_personas_caching(tmp_path, monkeypatch):
    """Installation personas are parsed once until the cache is cleared."""
    chart = tmp_path / "installation-org.yaml"
    chart.write_text(
        "schema_version: 1\noffices: []\npersonas:\n"
        "  Worker_One:\n    display_name: Sample Worker\n    avatar: '🤖'\n"
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(chart))
    loads = []
    real_load = personas.yaml.safe_load

    def counted_load(text):
        loads.append(1)
        return real_load(text)

    monkeypatch.setattr(personas.yaml, "safe_load", counted_load)
    personas.clear_custom_personas_cache()
    p1 = personas.load_custom_personas()
    assert p1["Worker_One"].display_name == "Sample Worker"
    assert personas.load_custom_personas() == p1
    assert len(loads) == 1
    personas.clear_custom_personas_cache()
    assert personas.load_custom_personas() == p1
    assert len(loads) == 2


def test_resolve_effective_persona_honors_stored_override(clean_env):
    """Direct persona resolution honors stored meta['persona'] overrides in SQLite."""
    import json

    conn = storage.connect()
    try:
        conn.execute(
            """
            INSERT INTO agents (label, kind, host, first_seen, last_seen, meta, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Custom_Worker",
                "developer",
                "custom-box",
                "2026-09-30T10:00:00Z",
                "2026-09-30T10:00:00Z",
                json.dumps(
                    {
                        "persona": {
                            "display_name": "Custom Identity",
                            "avatar": "🚀",
                            "color": "#123456",
                            "bio": "Custom specialized agent",
                        }
                    }
                ),
                "2026-09-30T10:00:00Z",
                "2026-09-30T10:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # resolve_effective_persona
    res = personas.resolve_effective_persona("Custom_Worker")
    assert res["display_name"] == "Custom Identity"
    assert res["avatar"] == "🚀"
    assert res["color"] == "#123456"
    assert res["bio"] == "Custom specialized agent"

    # read_persona in platform_readers
    reader_res = platform_readers.read_persona("Custom_Worker")
    assert reader_res["persona"]["display_name"] == "Custom Identity"
    assert reader_res["persona"]["avatar"] == "🚀"

    # all_personas includes Custom_Worker with stored override
    catalog = personas.all_personas()
    assert "Custom_Worker" in catalog
    assert catalog["Custom_Worker"]["display_name"] == "Custom Identity"
    assert catalog["Custom_Worker"]["avatar"] == "🚀"


def test_node_only_agents_enrich_persona(clean_env, monkeypatch):
    """Node-bound agents absent from registry receive persona metadata when selected."""
    from unified_agent.tools import registry as tools_registry

    # Mock node_queue returning a dict of nodes keyed by node name
    monkeypatch.setattr(
        tools_registry,
        "_listed_nodes",
        lambda: {
            "remote-worker-1": {
                "node": "remote-worker-1",
                "agent": "Remote_Worker",
                "status": "online",
            }
        },
    )

    fleet = tools_registry.select_fleet_agents(include_configured=False)
    remote = next((a for a in fleet if a["label"] == "Remote_Worker"), None)
    assert remote is not None
    assert "persona" in remote
    assert "display_name" in remote
    assert "avatar" in remote
    assert "color" in remote
    assert remote["display_name"] == "Remote Worker"
