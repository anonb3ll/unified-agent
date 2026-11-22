# Open-Core Architecture: Free Local Hub vs Paid Evidence Layer

Task #782 implements the architectural and configuration boundary in code separating the **Free Local Hub** from the **Attribution & Evidence Layer**.

Source: `docs/designs/user-attributable-agent-accountability.md`

---

## 1. The Open-Core Split

| Layer | Buyer / User | Motion | Capabilities | Price |
|---|---|---|---|---|
| **Local Hub (Core)** | Software Developers | Bottom-up install, local extension | Context, memory, tasks, tool sandboxing, multi-client adapters (Cursor, Codex, Claude Code, Antigravity), local Web UI | **Free (OSS)** |
| **Attribution & Evidence Layer** | Accountable Owners (Risk, Platform, Compliance) | Top-down compliance sale | Cryptographic hash chain, tamper-evident audit ledger, chain re-anchoring, evidence reporting (`human -> device -> agent -> action -> proof`) | **Paid** |

### Why This Split Works
- **Developers pick their tools**: Developers adopt free, high-utility, local-first tools without needing enterprise procurement.
- **Enterprises buy liability protection**: AI agents prefer free tools, but risk officers require verifiable audit trails and proof of an accountable human. You cannot hold an API key liable.

---

## 2. Code Seam & Configuration

The seam is implemented in `src/unified_agent/edition.py` and exposed via `config.edition()`.

### Environment Flag
- `UA_EDITION`: `core` | `enterprise` (default: `enterprise`).
- **Zero Behavior Change**: By defaulting to `enterprise`, existing single-operator and developer environments maintain complete unrestricted access without breaking changes.

### Feature Matrix

```python
from unified_agent import edition

# Check current edition
edition.current_edition()  # "core" or "enterprise"

# Check feature availability
edition.is_feature_available("evidence_reporting")  # True in enterprise, False in core
edition.is_feature_available("local_memory")  # True in both
```

| Feature Identifier | Description | Core Tier | Enterprise Tier |
|---|---|:---:|:---:|
| `local_memory` | SQLite FTS5 & vector memory | ✅ | ✅ |
| `local_tasks` | Task tracking and queue management | ✅ | ✅ |
| `mcp_tools` | Standard MCP tool execution | ✅ | ✅ |
| `process_sandbox` | Bash command isolation & allowlists | ✅ | ✅ |
| `webui_local` | Local dashboard and control center | ✅ | ✅ |
| `agent_registry` | MCP agent last-seen / identity registry | ✅ | ✅ |
| `local_sqlite` | Local `memory.db` | ✅ | ✅ |
| `sqlite_backup` | Online SQLite backup / restore | ✅ | ✅ |
| `evidence_reporting` | 5-link attributable accountability report | ❌ | ✅ |
| `audit_hash_chain` | SHA-256 tamper-evident event log chain | ❌ | ✅ |
| `audit_reanchor` | Daily external chain head snapshots | ❌ | ✅ |
| `compliance_export` | Evidence export (JSON/Markdown) | ❌ | ✅ |
| `multi_machine_fleet` | Cross-host fleet control (reserved) | ❌ | ✅ |
| `gated_autonomy_policy` | Extra autonomy policy surface (reserved) | ❌ | ✅ |

## 3. Enforcement at entry points (task #790)

The flag is not documentation-only. Callers that need an enterprise feature
call `edition.require_feature(...)` and raise `FeatureNotAvailableError`.

| Surface | What is gated | Failure |
|---|---|---|
| MCP wrapper (`server.py`) | `event_log_verify` requires `audit_hash_chain` | `ToolError` / feature error |
| Evidence CLI / `GET /api/evidence` | `evidence_reporting` | CLI exit / HTTP 403 |
| Ledger CLI + daily reanchor | `audit_hash_chain`, `audit_reanchor` | CLI refuse |
| Event hash-chain writes | `audit_hash_chain` skipped in core | Events still record; chain fields stay off |

Core edition keeps the local hub: memory, tasks, MCP tools, sandbox, Control
Center shell, and SQLite backup. It does not emit a 5-link evidence report or
re-anchor an audit chain. Default `UA_EDITION=enterprise` keeps existing
single-operator installs unchanged.

Set `UA_EDITION=core` only when you intend to hide the paid evidence layer.
Restart the hub after changing it.
