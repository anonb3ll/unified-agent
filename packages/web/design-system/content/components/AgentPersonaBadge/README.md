AgentPersonaBadge names an agent with its avatar, display name and a 3px persona-colour stripe.

- **Consumer provides:** `label` (the fleet agent label, e.g. `agent-claude`); optionally a `persona` object from the hub or explicit `displayName`, `avatar`, `color` overrides; `size` `sm`/`md`; `showLabel={false}` for avatar-only in tight rows.
- Known fleet labels resolve to built-in personas (the `persona-*` tokens). An unknown label gets a stable colour hashed from the label and a 🤖 avatar.
- The full label shows in the `title` tooltip; the display name truncates at 180px.
- Use wherever a person or agent is the actor: table cells, card headers, timelines.
