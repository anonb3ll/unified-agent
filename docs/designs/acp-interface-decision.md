# ACP interface decision

**Status:** decided 2026-09-06

> **Direction update 2026-09-25 (the operator):** T3 Code is a separate project with a
> separate team, with no access from here. Unified-Agent may **emulate aspects**
> of T3 Code to build its own harness and UI, but must not depend on T3's code,
> its integration boundary, or its roadmap. Read this document as the *protocol*
> decision it is — ACP is the external provider wire — and read the
> "integrate through T3's `ProviderAdapterShape` → `ProviderService`" seam below
> as an **emulation reference**, not a dependency. See the 2026-09-25 addendum.

## Decision

Target ACP as the external provider protocol, but integrate it through T3
Code's native `ProviderAdapterShape` → `ProviderService` boundary. Do not build
a second provider-specific control interface in Unified-Agent and do not make
the evidence surface depend directly on raw ACP JSON-RPC.

The resulting boundary is:

```text
ACP agent process
        ↓ initialize / session / permission / update
T3 ACP runtime adapter
        ↓ ProviderAdapterShape
T3 ProviderService + ProviderRuntimeEvent
        ↓ MCP / Control Center integration
Unified-Agent governance and evidence
```

## Evidence

| Question | Finding | Source |
|---|---|---|
| What ACP standardizes | A bidirectional JSON-RPC client/agent lifecycle: `initialize`, session creation or resume, `session/prompt`, streamed `session/update`, and permission requests. | [ACP protocol overview](https://agentclientprotocol.com/protocol/v2/overview) |
| Where T3 already speaks ACP | `AcpSessionRuntime` spawns the agent process, negotiates initialization, handles session setup, sends prompts, receives updates, and exposes permission/file/terminal handlers. | `anonb3ll/tcode: apps/server/src/provider/acp/AcpSessionRuntime.ts` — **emulation reference, not a dependency** (2026-09-25 direction) |
| T3's stable integration seam | `ProviderAdapterShape` already owns session lifecycle, turns, approvals, user input, snapshots, and canonical runtime events; `ProviderService` routes by provider instance and publishes those events. | `anonb3ll/tcode: apps/server/src/provider/Services/ProviderAdapter.ts` and `apps/server/src/provider/Layers/ProviderService.ts` |
| What ACP does not provide | ACP carries agent/client interaction. It does not define Unified-Agent task authority, approval policy, actor attribution, or the audit ledger. | ACP protocol scope plus Unified-Agent [`architecture.md`](../architecture.md) |

## Why this target

1. **Reuse exists.** T3 already has an ACP transport/runtime and a canonical
   provider adapter contract. A direct raw-ACP integration would duplicate
   session routing, permission handling, and event normalization.
2. **Governance stays above transport.** Unified-Agent's value is identity,
   approval, task, and evidence authority. Those concerns must remain stable if
   the provider transport changes from ACP to a native driver or another
   protocol.
3. **The event surface stays typed.** Phase 3 should consume T3's
   `ProviderRuntimeEvent` stream or an equivalent normalized export, with the
   raw ACP method retained as provenance rather than becoming the public
   contract.

## Consequences

- A future T3 integration should implement or configure an ACP-backed provider
  adapter and project its normalized lifecycle into Unified-Agent.
- Evidence records should carry the T3 provider instance, agent/session/thread
  identifiers, normalized event type, and raw ACP method when available.
- ACP version negotiation remains runtime behavior: clients must use
  `initialize` and capabilities, not assume compatibility from an SDK package
  version. See the [official schema repository](https://github.com/agentclientprotocol/agent-client-protocol).
- No ACP dependency or ACP server is added to Unified-Agent in this spike.
  Hosted accounts and autonomous dispatch remain out of scope.

## Rejected alternatives

### T3-native-only target

Using only the T3-native adapter contract would fit T3 today but would make the
integration fork-specific. ACP is the portable wire boundary; the native
adapter contract is the correct local seam behind it.

### Raw ACP in the evidence surface

Exposing raw ACP JSON-RPC directly would couple governance storage and UI to
protocol details, duplicate T3's normalization, and make provider-instance
routing harder to audit.

### ACP inside Unified-Agent

Unified-Agent is currently the MCP governance hub. Adding an ACP server here
would expand the transport surface without a demonstrated client or a defined
mapping from ACP sessions to governed tasks. Defer until an external user
requires that direction of call.

## Addendum 2026-09-25 — ACP v2 status

ACP **v2** now exists as a published **draft consolidation release**
([overview](https://agentclientprotocol.com/protocol/v2/overview),
[migrating from v1](https://agentclientprotocol.com/protocol/v2/migration)).
This addendum records the delta; it does not reopen the decision above.

### What v2 changes

1. **`session/prompt` no longer ends the turn.** It acknowledges acceptance;
   foreground progress and completion arrive as `state_update` notifications
   (`running` / `idle` / `requires_action`), and the stop reason moved there.
2. **Updates are upserts.** Messages, tool calls and plans are patched by ID:
   omitted field = unchanged, `null` = cleared, value = replaced, chunks append.
3. **The Client filesystem, terminal-execution and session-modes APIs are
   removed.** Agent-owned terminal output is a separate display-only surface;
   an Agent that needs Client-side tools is given client-provided MCP servers
   through `mcpServers` instead.
4. **Capabilities were reorganised.** One `capabilities` plus a required `info`
   field on both sides (v1 used role-specific `clientCapabilities` /
   `agentCapabilities`), session-scoped groups nested under `session`, object
   support markers instead of booleans, and a required baseline of session
   methods (`session/resume`, `session/list`, `session/close`).
   `session/load` and `session/set_mode` are gone; `authenticate` / `logout`
   are `auth/login` / `auth/logout`.
5. **The schema is forward-compatible by default.** Unknown enum and tag-union
   values are preserved; `_`-prefixed values are implementation-reserved.

### Readiness on this cluster (verified 2026-09-25)

| Implementation | Version | Protocol surface |
|---|---|---|
| Python SDK `agent-client-protocol` | 0.12.1 (PyPI latest) | **v1** — `acp/meta.py` declares `PROTOCOL_VERSION = 1` |
| TypeScript SDK `@agentclientprotocol/sdk` | 1.5.0 (npm latest) | v1 by default; **v2 behind `experimental/v2`** |
| `hermes acp` / `acp_adapter/` | v0.21.5 | **v1** (`acp.PROTOCOL_VERSION`); live, `hermes acp --check` = OK |
| T3 Code `packages/effect-acp` | generated from schema release **v0.11.3** | **v1** |

T3 Code's own schema-pin policy is out of scope here — it belongs to the T3
codebase and is not tracked in this repository's queue.

### What this does not change

Governance still sits above transport, and the evidence surface still must not
depend on raw ACP JSON-RPC. ACP v2 is a change of message shapes inside the same
JSON-RPC framing, not a new transport.

### What the 2026-09-25 direction changes

The integration seam is **ours**. T3 Code is a separate project with a separate
team and is not a dependency of this repository: Unified-Agent builds and owns
its own ACP client and adapter boundary (the ua-node adapter in #1328), and uses
T3's provider/runtime design only as an **emulation reference** for building the
Unified-Agent harness and session UI. No T3 code, package, type, or roadmap
commitment is imported, and T3-side work is not tracked in this repository's
queue.

### What it does require

- **Version-aware integration.** Negotiate on `initialize` and select the v1 or
  v2 surface per connection; `protocolVersion: 2` does not imply the unstable
  schema. v2 support is additive — keep serving v1 peers, because dropping v1
  cuts off both existing Agents and existing Clients.
- **Do not encode the v1 prompt lifecycle** into the adapter or the normalized
  event contract. A turn ends on the idle `state_update` with its stop reason,
  not on the `session/prompt` response.
- **Gate v2 behind a flag** until it stabilises upstream.

### Where the protocol reference lives

The full protocol reference for this cluster — v1 → v2 method and
`session/update` change tables, transports, extensibility conventions, SDK and
registry inventory, RFDs, and the local implementation inventory — is the vault
wiki page **`Wiki/Agent-Client-Protocol.md`** (`[[Agent-Client-Protocol]]`).
The vault is the single source for that reference; do not duplicate it here.

The vault is a separate private knowledge base and is **not** part of this
repository, so there is no in-repo URL for it. How to reach it:

- read it with the unified-agent `vault_read` tool at path
  `Wiki/Agent-Client-Protocol.md`;
- on the desktop it is `~/vaults/main/Wiki/Agent-Client-Protocol.md`; the
  authoritative copy lives on the hub at `UA_VAULT_PATH` (`~/vaults/main`) and
  syncs to the other machines;
- or open it in Obsidian by note name `Agent-Client-Protocol`.

Upstream remains authoritative over both: <https://agentclientprotocol.com/llms.txt>
indexes every page.

Tracked follow-up: task #1402 (ACP v2 readiness sweep) — the adapter v2
gating under #1328 and a docs reference page.
