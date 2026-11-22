"""Surface layer: ua-node reports Hermes profiles and kanban assignees (task #1391).

Names only leave the node; the hub maps a surface to an agent label only by an
explicit UA_SURFACE_MAP entry or an exact label match, else 'unmapped'.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from unified_agent import config, host_registry, node_agent, node_queue, registry


def _hermes(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    for name in ("default", "codex-dev", "reviewer", "bad name!"):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("private config")
    (home / "profiles" / "not-a-dir.txt").write_text("x")
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, assignee TEXT, status TEXT)")
    conn.executemany(
        "INSERT INTO tasks VALUES (?, 'secret title', 'secret body', ?, 'open')",
        [
            ("1", "codex-dev"),
            ("2", "desktop_agent2"),
            ("3", None),
            ("4", "codex-dev"),
            ("5", "x; drop"),
        ],
    )
    conn.commit()
    conn.close()
    return home


def test_collect_surfaces_reports_names_only(tmp_path):
    surfaces = node_agent.collect_surfaces(_hermes(tmp_path))
    assert surfaces == {
        "hermes_profiles": ["codex-dev", "default", "reviewer"],
        "kanban_assignees": ["codex-dev", "desktop_agent2"],
    }
    assert "secret" not in repr(surfaces)


def test_missing_hermes_is_empty(tmp_path):
    assert node_agent.collect_surfaces(tmp_path / "none") == {
        "hermes_profiles": [],
        "kanban_assignees": [],
    }


def test_map_surfaces_never_guesses():
    rows = host_registry.map_surfaces(
        "Desktop",
        {
            "hermes_profiles": ["default", "codex-dev", "Desktop_Agent2"],
            "kanban_assignees": ["desktop_agent2"],
        },
        mapping={"Desktop/hermes/default": "Desktop_Agent5"},
        labels={"Desktop_Agent2", "Desktop_Agent5"},
    )
    by = {(r["kind"], r["name"]): (r["agent"], r["mapping"]) for r in rows}
    assert by[("hermes", "default")] == ("Desktop_Agent5", "explicit")
    assert by[("hermes", "Desktop_Agent2")] == ("Desktop_Agent2", "exact")
    assert by[("hermes", "codex-dev")] == (None, "unmapped")
    assert by[("kanban", "desktop_agent2")] == (None, "unmapped")


def test_fleet_payload_carries_mapped_surfaces(monkeypatch):
    monkeypatch.setenv("UA_SOURCE_HOST", "hubhost")
    monkeypatch.setenv("UA_SURFACE_MAP", "Desktop/kanban/codex-dev:Desktop_Agent2")
    node_queue.register(
        "Desktop",
        identity="Desktop_agent3",
        facts={"surfaces": {"hermes_profiles": ["reviewer"], "kanban_assignees": ["codex-dev"]}},
    )
    registry.touch_agent("Desktop_agent3", via="hub_request", force=True)
    host = next(h for h in host_registry.fleet_payload()["hosts"] if h["host"] == "Desktop")
    assert {(s["kind"], s["name"], s["agent"], s["mapping"]) for s in host["surfaces"]} == {
        ("hermes", "reviewer", None, "unmapped"),
        ("kanban", "codex-dev", "Desktop_Agent2", "explicit"),
    }


def test_exact_surface_maps_to_configured_agent_never_seen(monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_agent3:Desktop_NeverSeen")
    monkeypatch.delenv("UA_SURFACE_MAP", raising=False)
    node_queue.register(
        "Desktop",
        identity="Desktop_agent3",
        facts={"surfaces": {"hermes_profiles": ["Desktop_NeverSeen"]}},
    )
    assert "Desktop_NeverSeen" not in host_registry._observed_agent_labels()
    host = next(h for h in host_registry.fleet_payload()["hosts"] if h["host"] == "Desktop")
    assert host["surfaces"] == [
        {
            "kind": "hermes",
            "name": "Desktop_NeverSeen",
            "agent": "Desktop_NeverSeen",
            "mapping": "exact",
        }
    ]


def test_surface_map_drops_conflicts():
    assert config.surface_map("a/hermes/x:L1,a/hermes/x:L2,b/kanban/y:L3") == {"b/kanban/y": "L3"}


def test_default_fleet_surface_map_is_empty(monkeypatch):
    monkeypatch.delenv("UA_SURFACE_MAP", raising=False)
    assert config.DEFAULT_FLEET_SURFACE_MAP == {}
    assert config.surface_map() == {}


def test_explicit_installation_surface_map(monkeypatch):
    mapping = {
        "workstation/hermes/default": "Coordinator_One",
        "workstation/hermes/builder": "Builder_One",
        "workstation/kanban/builder": "Builder_One",
        "hub-host.local/hermes/reviewer": "Reviewer_One",
        "hub-host.local/kanban/reviewer": "Reviewer_One",
    }
    monkeypatch.setenv("UA_SURFACE_MAP", ",".join(f"{k}:{v}" for k, v in mapping.items()))
    assert config.surface_map() == mapping
    rows = host_registry.map_surfaces(
        "hub-host.local",
        {"hermes_profiles": ["reviewer"], "kanban_assignees": ["reviewer"]},
        mapping=config.surface_map(),
        labels=set(mapping.values()),
    )
    assert all(row["agent"] == "Reviewer_One" and row["mapping"] == "explicit" for row in rows)


def test_surface_map_has_no_implicit_other_bindings(monkeypatch):
    monkeypatch.setenv("UA_SURFACE_MAP", "workstation/hermes/default:Coordinator_One")
    assert config.surface_map() == {"workstation/hermes/default": "Coordinator_One"}


def test_unverified_reviewer_remains_unmapped(monkeypatch):
    monkeypatch.delenv("UA_SURFACE_MAP", raising=False)
    smap = config.surface_map()
    desktop_mapped = host_registry.map_surfaces(
        "Desktop",
        {"hermes_profiles": ["reviewer", "unregistered_tester"]},
        mapping=smap,
        labels=config.agent_labels(),
    )
    for entry in desktop_mapped:
        assert entry["agent"] is None
        assert entry["mapping"] == "unmapped"

    mac_mapped = host_registry.map_surfaces(
        "hub-host.local",
        {"hermes_profiles": ["reviewer"]},
        mapping=smap,
        labels=config.agent_labels(),
    )
    assert mac_mapped[0]["agent"] is None
    assert mac_mapped[0]["mapping"] == "unmapped"


def test_surface_map_whitespace_and_empty_entries_ignored(monkeypatch):
    monkeypatch.setenv(
        "UA_SURFACE_MAP",
        "  \n  Desktop/hermes/default: Desktop_Agent5 , , \n Desktop/hermes/claude-dev:Desktop_Agent1\n",
    )
    res = config.surface_map()
    assert res["Desktop/hermes/default"] == "Desktop_Agent5"
    assert res["Desktop/hermes/claude-dev"] == "Desktop_Agent1"


def test_conflicting_override_drops_default_surface_binding(monkeypatch):
    """Conflicting explicit labels remove a surface binding (fail-closed)."""
    monkeypatch.setenv(
        "UA_SURFACE_MAP",
        "Desktop/hermes/default:Desktop_Agent1,Desktop/hermes/default:Desktop_Agent2",
    )
    smap = config.surface_map()
    assert "Desktop/hermes/default" not in smap

    # Devices mapping shows it as unmapped rather than falling back to default
    mapped = host_registry.map_surfaces(
        "Desktop",
        {"hermes_profiles": ["default"]},
        mapping=smap,
        labels=config.agent_labels(),
    )
    assert mapped[0]["agent"] is None
    assert mapped[0]["mapping"] == "unmapped"


def test_explicit_suppression_of_default_surface_binding(monkeypatch):
    """NONE and empty values explicitly leave a surface unmapped."""
    # Suppress with NONE
    monkeypatch.setenv("UA_SURFACE_MAP", "Desktop/hermes/default:NONE")
    smap = config.surface_map()
    assert "Desktop/hermes/default" not in smap

    mapped = host_registry.map_surfaces(
        "Desktop",
        {"hermes_profiles": ["default"]},
        mapping=smap,
        labels=config.agent_labels(),
    )
    assert mapped[0]["agent"] is None
    assert mapped[0]["mapping"] == "unmapped"

    # Suppress with empty value
    monkeypatch.setenv("UA_SURFACE_MAP", "Desktop/hermes/default:")
    smap2 = config.surface_map()
    assert "Desktop/hermes/default" not in smap2
