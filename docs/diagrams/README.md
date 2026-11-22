# System diagrams

Self-contained HTML diagrams (inline SVG + CSS, no build step). Open any file in
a browser. Skin: Catppuccin Mocha, matching the operator's desktop / Obsidian theme.

Built with the `diagram-design` skill. To redraw one, edit its `.html` and
re-run the skill's geometry + self checks:

```
python3 <diagram-design>/scripts/verify-geometry.py <file>.html
python3 <diagram-design>/skills/diagram-design/scripts/self_check.py <file>.html
```

| File | Type | Shows | Source |
|---|---|---|---|
| [unified-agent-server-stack.html](unified-agent-server-stack.html) | Layer stack | The six layers a tool call passes through: client → transport → the policy/profile/identity gate → tool modules + safety → SQLite + event ledger → optional planes. | [`architecture.md`](../architecture.md) |
| [control-security-layers.html](control-security-layers.html) | Layer stack (control catalog) | Every check grouped by where it is enforced — on connect, per session, per tool call, on merge, on deploy/run, and the advisory audit that grants no authority. | [`architecture.md`](../architecture.md), [`security.md`](../security.md) |
| [vault-flow-para.html](vault-flow-para.html) | Flowchart | Where a note goes: capture → Inbox → Wiki / Projects / Archive → index in `Wiki/Filed/Inbox-Archive` → sweep the original to a dated archive folder. | vault `Wiki/Vault-Flow` |

Rendered `.svg` copies live in the vault at `Assets/diagrams/` and are embedded
in the matching Wiki pages (`unified-agent`, `Control-Center`, `Vault-Flow`).

The three cockpit-side diagrams (device/hub topology, cockpit ↔ dev-repo split,
task lifecycle) live in `~/agent-workspace/docs/diagrams/`.
