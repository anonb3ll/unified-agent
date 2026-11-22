"""Persisted visual identities and display metadata for fleet agents (task #1427).

Personas provide a stable visual identity (display name, avatar, color accent,
bio, and optional model routing preference) rendered across the Control Center,
org chart, and task threads.

Design Rule 3 / Boundary Guarantee:
Personas are purely presentational display metadata. They grant no capability,
waive no authorization gate, and do NOT alter dispatch routing or profile
permissions. Agent liveness is computed at read time (registry.derive_status),
never stored as a persona status column.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentPersona:
    """Canonical presentation metadata for an agent."""

    display_name: str
    avatar: str
    color: str
    bio: str = ""
    model_routing_preference: str | None = None

    @property
    def colour(self) -> str:
        """UK spelling alias for color."""
        return self.color

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["colour"] = self.color
        return data


# Canonical default visual identities for known fleet agents.
# The human operator's persona is derived from settings (operator_personas()),
# never compiled in.
DEFAULT_FLEET_PERSONAS: dict[str, AgentPersona] = {
    "Desktop_Agent5": AgentPersona(
        display_name="Hermes (CTO)",
        avatar="🪽",
        color="#f59e0b",
        bio="CTO / Head Engineer. Platform architecture, technical direction, hands-on implementation and technical review of fleet output.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "server_agent5": AgentPersona(
        display_name="Server Agent5",
        avatar="🪽",
        color="#d97706",
        bio="Hub-hosted CTO surface: technical review and platform health on the hub host.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Laptop_Agent5": AgentPersona(
        display_name="Laptop Agent5",
        avatar="🪽",
        color="#b45309",
        bio="Laptop Chief of Staff worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "gcp_agent5": AgentPersona(
        display_name="GCP Agent5",
        avatar="☁️",
        color="#3b82f6",
        bio="Executive office lead on GCP. Handles research, calendar, and inbox.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "gcp_agent20": AgentPersona(
        display_name="GCP Agent20",
        avatar="☁️",
        color="#60a5fa",
        bio="GCP agent20 background worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "gcp_agent18": AgentPersona(
        display_name="Executive Assistant",
        avatar="📋",
        color="#93c5fd",
        bio="Executive office coordination and meeting preparation.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "gcp_agent19": AgentPersona(
        display_name="Librarian",
        avatar="📚",
        color="#2563eb",
        bio="Knowledge base and research librarian.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Desktop_Agent7": AgentPersona(
        display_name="Devin (Lead)",
        avatar="⚡",
        color="#10b981",
        bio="Product Engineering lead. Builds Approval Ledger and product capabilities.",
        model_routing_preference="cognition-devin",
    ),
    "Desktop_Agent2": AgentPersona(
        display_name="Codex (Lead)",
        avatar="🛠️",
        color="#06b6d4",
        bio="Platform Engineering lead. Core architecture, MCP, and daemon runtime.",
        model_routing_preference="gpt-4o",
    ),
    "server_agent2": AgentPersona(
        display_name="Server Agent2",
        avatar="🛠️",
        color="#0891b2",
        bio="Hub-hosted Platform Engineering worker.",
        model_routing_preference="gpt-4o",
    ),
    "Laptop_Agent2": AgentPersona(
        display_name="Laptop Agent2",
        avatar="🛠️",
        color="#0e7490",
        bio="Laptop Platform Engineering worker.",
        model_routing_preference="gpt-4o",
    ),
    "Desktop_Agent4": AgentPersona(
        display_name="Cursor",
        avatar="🎯",
        color="#8b5cf6",
        bio="Product & Platform Engineering IDE agent.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "server_agent4": AgentPersona(
        display_name="Server Agent4",
        avatar="🎯",
        color="#7c3aed",
        bio="Hub-hosted Cursor worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Laptop_Agent4": AgentPersona(
        display_name="Laptop Agent4",
        avatar="🎯",
        color="#6d28d9",
        bio="Laptop Agent4 worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Desktop_agent3": AgentPersona(
        display_name="Antigravity (Agy)",
        avatar="✨",
        color="#a855f7",
        bio="Platform engineer & autonomous coding assistant.",
        model_routing_preference="gemini-2.5-pro",
    ),
    "server_agent3": AgentPersona(
        display_name="Server Agent3",
        avatar="✨",
        color="#9333ea",
        bio="Hub-hosted executor lead.",
        model_routing_preference="gemini-2.5-pro",
    ),
    "Laptop_agent3": AgentPersona(
        display_name="Laptop Agent3",
        avatar="✨",
        color="#7e22ce",
        bio="Laptop Agent3 worker.",
        model_routing_preference="gemini-2.5-pro",
    ),
    "Desktop_Agent1": AgentPersona(
        display_name="Claude (Steward)",
        avatar="🛡️",
        color="#d97706",
        bio="Senior engineer & steward. Audits, cross-cutting fixes, and security.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "server_agent1": AgentPersona(
        display_name="Server Agent1",
        avatar="🛡️",
        color="#b45309",
        bio="Hub-hosted senior engineer worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Laptop_Agent1": AgentPersona(
        display_name="Laptop Agent1",
        avatar="🛡️",
        color="#92400e",
        bio="Laptop senior engineer worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "cloud_agent1": AgentPersona(
        display_name="Cloud Agent1",
        avatar="🛡️",
        color="#78350f",
        bio="Claude Code on the web senior engineer worker.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Desktop_agent12": AgentPersona(
        display_name="OpenCode",
        avatar="💻",
        color="#14b8a6",
        bio="Bench lead & overflow coding assistant.",
        model_routing_preference="deepseek-coder",
    ),
    "Desktop_Agent6": AgentPersona(
        display_name="Copilot",
        avatar="✈️",
        color="#0ea5e9",
        bio="GitHub Copilot bench agent.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Laptop_Agent6": AgentPersona(
        display_name="Laptop Agent6",
        avatar="✈️",
        color="#0284c7",
        bio="Laptop Agent6 bench agent.",
        model_routing_preference="claude-3-7-sonnet",
    ),
    "Desktop_agent10": AgentPersona(
        display_name="OMP",
        avatar="🔮",
        color="#ec4899",
        bio="Bench assistant.",
    ),
    "server_agent8": AgentPersona(
        display_name="Grok (Gateway)",
        avatar="🚪",
        color="#64748b",
        bio="Front-door OAuth public identity.",
    ),
}

_FALLBACK_AVATARS = ("🤖", "⚡", "🔮", "💡", "🛠️", "💻", "✨", "🎯")
_FALLBACK_COLORS = (
    "#3b82f6",  # blue
    "#10b981",  # emerald
    "#8b5cf6",  # violet
    "#f59e0b",  # amber
    "#ec4899",  # pink
    "#06b6d4",  # cyan
    "#14b8a6",  # teal
    "#6366f1",  # indigo
)


def _fallback_persona(label: str) -> AgentPersona:
    """Deterministic presentation fallback for unknown or ad-hoc agent labels."""
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    avatar_idx = digest[0] % len(_FALLBACK_AVATARS)
    color_idx = digest[1] % len(_FALLBACK_COLORS)
    # Format label e.g. "Desktop_Agent" -> "Desktop Agent"
    display = label.replace("_", " ") if "_" in label else label
    return AgentPersona(
        display_name=display,
        avatar=_FALLBACK_AVATARS[avatar_idx],
        color=_FALLBACK_COLORS[color_idx],
        bio=f"Agent identity for {label}.",
    )


_CUSTOM_PERSONAS_CACHE: dict[str, Any] = {"path": None, "mtime": None, "size": None, "personas": {}}


def clear_custom_personas_cache() -> None:
    """Clear cached custom personas loaded from org.yaml."""
    global _CUSTOM_PERSONAS_CACHE
    _CUSTOM_PERSONAS_CACHE = {"path": None, "mtime": None, "size": None, "personas": {}}


def load_custom_personas() -> dict[str, AgentPersona]:
    """Load custom persona declarations from org.yaml if present (cached by mtime)."""
    global _CUSTOM_PERSONAS_CACHE
    from . import config, org_registry

    try:
        path = org_registry.registry_path()
        if not path.is_file():
            return {}
        stat = path.stat()
        # Placeholders resolve against the operator, so it is part of the key.
        cache_key = f"{path}\0{config.operator_label()}\0{config.operator_name()}"
        if (
            _CUSTOM_PERSONAS_CACHE["path"] == cache_key
            and _CUSTOM_PERSONAS_CACHE["mtime"] == stat.st_mtime
            and _CUSTOM_PERSONAS_CACHE["size"] == stat.st_size
        ):
            return dict(_CUSTOM_PERSONAS_CACHE["personas"])

        raw = org_registry.resolve_placeholders(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            return {}
        entries = raw.get("personas")
        if not isinstance(entries, dict):
            return {}
        custom: dict[str, AgentPersona] = {}
        for label, val in entries.items():
            if not isinstance(val, dict):
                continue
            color = str(val.get("color") or val.get("colour") or "#64748b")
            custom[str(label)] = AgentPersona(
                display_name=str(val.get("display_name") or label),
                avatar=str(val.get("avatar") or "🤖"),
                color=color,
                bio=str(val.get("bio") or ""),
                model_routing_preference=val.get("model_routing_preference"),
            )
        _CUSTOM_PERSONAS_CACHE = {
            "path": cache_key,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "personas": custom,
        }
        return dict(custom)
    except Exception:
        return {}


def operator_personas() -> dict[str, AgentPersona]:
    """Persona for the configured human operator's login label and aliases."""
    from . import config

    name = config.operator_name()
    persona = AgentPersona(
        display_name=name,
        avatar="👤",
        color="#e11d48",
        bio="Human operator. Sets direction and approves gated actions.",
    )
    return {label: persona for label in (config.operator_label(), *config.operator_aliases())}


def default_personas() -> dict[str, AgentPersona]:
    """Checked-in fleet personas plus this deployment's operator."""
    return {**DEFAULT_FLEET_PERSONAS, **operator_personas()}


def get_persona(label: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the display persona for an agent label.

    Precedence:
    1. Direct override in agent meta JSON (e.g. meta["persona"])
    2. org.yaml `personas:` declarations
    3. Checked-in fleet default personas
    4. Deterministic fallback based on label hash
    """
    if meta and isinstance(meta.get("persona"), dict):
        p_dict = meta["persona"]
        color = str(p_dict.get("color") or p_dict.get("colour") or "#64748b")
        return {
            "display_name": str(p_dict.get("display_name") or label),
            "avatar": str(p_dict.get("avatar") or "🤖"),
            "color": color,
            "colour": color,
            "bio": str(p_dict.get("bio") or ""),
            "model_routing_preference": p_dict.get("model_routing_preference"),
        }

    custom = load_custom_personas()
    if label in custom:
        return custom[label].to_dict()

    defaults = default_personas()
    if label in defaults:
        return defaults[label].to_dict()

    return _fallback_persona(label).to_dict()


def resolve_effective_persona(label: str) -> dict[str, Any]:
    """Resolve persona for an agent label, honoring stored overrides in registry."""
    from . import registry

    try:
        agent = registry.get_agent(label)
        if agent and isinstance(agent.get("persona"), dict):
            return agent["persona"]
        meta = agent.get("meta") if agent and isinstance(agent.get("meta"), dict) else None
        return get_persona(label, meta)
    except Exception:
        return get_persona(label)


def enrich_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """Enrich an agent dict with display persona metadata in-place and return it."""
    label = agent.get("label", "")
    meta = agent.get("meta")
    persona = get_persona(label, meta if isinstance(meta, dict) else None)

    agent["display_name"] = persona["display_name"]
    agent["avatar"] = persona["avatar"]
    agent["color"] = persona["color"]
    agent["colour"] = persona["colour"]
    agent["bio"] = persona.get("bio", "")
    agent["model_routing_preference"] = persona.get("model_routing_preference")
    agent["persona"] = persona
    return agent


def all_personas(agents: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Return all known personas.

    Precedence:
    1. Fleet defaults
    2. org.yaml `personas:` declarations
    3. Registered / configured agents with stored `meta["persona"]` overrides or custom identities
    """
    result = {k: v.to_dict() for k, v in default_personas().items()}
    for k, v in load_custom_personas().items():
        result[k] = v.to_dict()

    if agents is None:
        try:
            from . import registry

            agents = registry.list_agents(include_configured=True)
        except Exception:
            agents = None

    if agents:
        for ag in agents:
            lbl = ag.get("label")
            if not lbl:
                continue
            if isinstance(ag.get("persona"), dict):
                result[lbl] = ag["persona"]
            elif lbl not in result:
                meta = ag.get("meta") if isinstance(ag.get("meta"), dict) else None
                result[lbl] = get_persona(lbl, meta)
    return result
