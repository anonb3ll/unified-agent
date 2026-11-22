# Agent Personas (Parity 22)

**Task:** #1427\
**Status:** Shipped (object model schema, adapter, reader paths, Control Center UI rendering, and security tests).

---

## 1. Overview

Agent Personas provide a persisted visual identity for each agent in the fleet, independent of prompts and runtime state. Each persona defines:
- **`display_name`**: Human-friendly label (e.g., `Antigravity (Agy)`, `Claude (Steward)`).
- **`avatar`**: Distinctive visual emoji or icon (e.g., `✨`, `🛡️`, `🪽`, `⚡`).
- **`color` / `colour`**: Hex accent color used in borders, badges, and status chips (e.g., `#a855f7`).
- **`bio`**: Brief description of the agent's primary office, capabilities, and responsibilities.
- **`model_routing_preference`**: Optional advisory preferred model identifier (e.g., `gemini-2.5-pro`, `claude-3-7-sonnet`).

Personas are rendered across the Control Center web UI:
1. **Org View (`/ui/org`)**: Each office member displays their avatar, display name, and color border alongside real-time heartbeat liveness.
2. **Task Detail (`/ui/tasks`)**: The `Claimed by` field renders an `AgentPersonaBadge` linking visual identity to task ownership.
3. **Task Thread (`/ui/tasks`)**: Comment authors display their persona badge in the task conversation thread.

---

## 2. Architectural Design & Boundaries

### Purely Presentational Boundary
A persona is **strictly display metadata**:
- **No privilege escalation**: A persona cannot grant tools, elevate profiles, or bypass authorization gates (`profiles.default_for_identity()`, `profiles.tool_is_allowed_for()`, and `deny()`).
- **No dispatch alteration**: Headless CLI execution (`routing.build_route()`, `routing.resolve_named_agent()`) routes deterministically based on agent label and system topology, never persona preferences.
- **Design Rule 3 Compliance**: Agents have no stored lifecycle or status column in persona definitions. Liveness (`online`, `idle`, `stale`, `offline`, `unknown`) is computed dynamically at read time from heartbeat timestamps (`registry.derive_status()`).

---

## 3. Configuration & Precedence

Personas are resolved via `personas.get_persona(label, meta)` in strict order:
1. **Agent `meta["persona"]`**: In-database override from registered agent metadata.
2. **`src/unified_agent/org.yaml` `personas:` section**: Checked-in org charter declarations.
3. **Built-in Fleet Defaults**: Canonical visual identities defined in `src/unified_agent/personas.py` for all 27 standard fleet labels.
4. **Deterministic Fallback**: Automatic hashing algorithm that derives a stable avatar and color palette for any unseen or dynamic agent label.

### Example `org.yaml` Declaration

```yaml
personas:
  agent-agy:
    display_name: Antigravity (Agy)
    avatar: "✨"
    color: "#a855f7"
    bio: "Platform engineer & autonomous coding assistant on the desktop."
    model_routing_preference: "gemini-2.5-pro"
  agent-claude:
    display_name: Claude (Steward)
    avatar: "🛡️"
    color: "#d97706"
    bio: "Senior engineer & steward. Audits, cross-cutting fixes, and security."
    model_routing_preference: "claude-3-7-sonnet"
```

---

## 4. Platform Object Model & Readers

The `agent` object declaration in `src/unified_agent/object_model.py` exposes persona properties:
```json
{
  "display_name": {"type": ["string", "null"]},
  "avatar": {"type": ["string", "null"]},
  "color": {"type": ["string", "null"]},
  "colour": {"type": ["string", "null"]},
  "bio": {"type": ["string", "null"]},
  "model_routing_preference": {"type": ["string", "null"]},
  "persona": {
    "type": "object",
    "properties": {
      "display_name": {"type": "string"},
      "avatar": {"type": ["string", "null"]},
      "color": {"type": ["string", "null"]},
      "colour": {"type": ["string", "null"]},
      "bio": {"type": ["string", "null"]},
      "model_routing_preference": {"type": ["string", "null"]}
    }
  }
}
```

- **MCP Resource**: `unified-agent://agents/{agent_label}` via `platform_readers.read_agent`.
- **Direct Persona Reader**: `platform_readers.read_persona(label)` returns `{"label": ..., "persona": ...}`.
- **REST Endpoints**:
  - `GET /api/agents`: Enriched fleet agent list with persona metadata.
  - `GET /api/personas`: Full dictionary of known personas (`api.personas_payload`).
  - `GET /api/personas/{label}`: Direct persona lookup for a single agent.
