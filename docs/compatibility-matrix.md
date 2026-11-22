# Hub / client compatibility matrix

This is the authoritative answer to "will this SDK version work against that
hub?" Update it whenever either side's protocol contract moves — a stale
matrix here is worse than none, because the conformance suite below assumes
it's current when interpreting a failure.

**Correction (task #938, 2026-09-11):** the first version of this table
described the hub's `2026-07-28` "target" as something this SDK simply hadn't
gotten around to reaching yet. That was wrong, and the mistake is worth
naming so it doesn't recur: `2026-07-28` is a **different transport**, not a
newer version of the same one. See "The dual-era hub" below before editing
this table.

## How to read it

- **SDK sends** — `MCP_PROTOCOL_VERSION` in `packages/client/src/hub.ts`.
- **Hub's handshake ceiling** — the newest version the hub will ever return
  from a classic `initialize` response, regardless of what the client
  requests. Verified against the installed hub dependency's own source
  (`mcp_types.version.HANDSHAKE_PROTOCOL_VERSIONS` and
  `mcp.server.runner._negotiate_initialize`), not from operator docs or
  memory — re-verify the same way before editing this row, since it's a
  property of whatever `mcp`/`mcp_types` version the hub happens to have
  installed, not a unified_agent-specific constant.
- **Conformant** — the mock conformance suite (`npm run test:conformance`)
  passing proves the *typed contract* (schemas, tool names, response shapes,
  negotiation fail-closed behavior). It does **not** prove live compatibility
  — only `npm run test:conformance:live` against a real hub does that.

| SDK version | Sends protocol | Hub handshake ceiling | Status | Notes |
|---|---|---|---|---|
| 0.2.0 – 0.2.1 | `2025-06-18` | `2025-11-25` | ✅ Compatible, not current | Hub echoes back `2025-06-18` since it's a member of `HANDSHAKE_PROTOCOL_VERSIONS`. Works, but leaves the newest handshake-reachable revision (`2025-11-25`) unused for no reason. |
| Current `packages/client` source (manifest still `0.2.1`; task #938) | `2025-11-25` | `2025-11-25` | ✅ Compatible, current for this transport | Newest revision reachable via the classic `initialize` handshake this SDK implements. Fails closed (`HubNegotiationError`) if the hub ever echoes back something outside `KNOWN_HANDSHAKE_PROTOCOL_VERSIONS` — including `2026-07-28` itself, which a classic-handshake response can never honestly claim (see below). |

## The dual-era hub

The installed hub dependency (`mcp`/`mcp_types`) serves two protocol eras
over the same endpoint, and picks between them from the *shape* of the very
first request on a connection (`mcp.server.runner.serve_dual_era_loop`):

- **Legacy era** (`2024-11-05` through `2025-11-25`): opened by a classic
  `initialize` request. Session id in a header, `tools/call` per request,
  everything this SDK implements. `_negotiate_initialize` echoes back
  whatever protocol version the client requested if it's a member of
  `HANDSHAKE_PROTOCOL_VERSIONS`, or substitutes `2025-11-25` (the newest
  handshake revision) otherwise. It **never rejects** `initialize` over
  version choice.
- **Modern era** (`2026-07-28` only, so far): opened by a request carrying
  the `2026-07-28` per-request `_meta` envelope — no `initialize`, no session
  id, every request self-contained. A connection that opens this way rejects
  a later `initialize` outright with `UNSUPPORTED_PROTOCOL_VERSION`.

The two are mutually exclusive **per connection**, decided once, at the first
request. There is no version-string incantation that gets a classic
`initialize` call routed to the modern era — reaching `2026-07-28` requires
implementing the per-request-envelope transport, not editing
`MCP_PROTOCOL_VERSION`. The hub's own `mcp_health_telemetry`/`system_doctor`
`target_protocol_version` field reports `2026-07-28` because that's
literally `mcp_types.LATEST_PROTOCOL_VERSION` (the newest revision the
installed dependency knows about, of either era) — it is not a claim that a
classic-handshake client can reach it.

**Practical upshot:** don't read "hub target: 2026-07-28" from
`mcp_health_telemetry` as an SDK compatibility gap to close by requesting a
newer version string. The gap that field actually points at — real capability
this SDK doesn't use — is elicitation and resource-subscription plumbing
*within* the legacy era, some of which task #938 already exposes
(`HubTransport.serverCapabilities`, `notifications/progress` support,
capability declaration on `initialize`). Full `2026-07-28` support is a
separate transport implementation, not scoped to any task as of this writing.

## What "compatible" does *not* mean here

A ✅ above means the JSON-RPC handshake succeeds and the tool surface this SDK
wraps (see the conformance suite) round-trips correctly. It does not mean the
SDK exposes every capability the *legacy-era* hub offers (elicitation
round-trips, resource subscriptions, and `roots/list` serving are typed for
discovery as of task #938 but not fully implemented end-to-end — see that
task's PR for the precise boundary), and it does not mean anything about the
modern (`2026-07-28`) era, which this SDK cannot reach at all.

## Verifying a hub's handshake ceiling before editing this table

From an already-connected agent session (not this SDK — any MCP client),
call `mcp_health_telemetry` or `system_doctor` and read
`negotiated_protocol_version` — but remember that field reflects whatever
protocol version *that particular client* actually requested and got
echoed back, not a hub-wide ceiling. To find the actual ceiling, read the
installed hub dependency's source directly:

```bash
python3 -c "from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION; print(HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION)"
```

run from the hub's own virtualenv (`~/dev/unified_agent/.venv` on the
canonical Mac hub checkout, or wherever `UA_WORKSPACE` points). Do not infer
this from `LATEST_PROTOCOL_VERSION` / `target_protocol_version` alone — that
constant spans both eras and is not, by itself, evidence of what a classic
`initialize` handshake can reach. This is exactly the mistake the original
version of this table made.

## Adding a new row

When the SDK's `MCP_PROTOCOL_VERSION` changes, or the hub's handshake ceiling
changes:

1. Add a new row rather than editing history in place — old SDK versions in
   the wild still need their compatibility documented.
2. Run `npm run test:conformance` (mock leg) to confirm the typed contract
   — including negotiation fail-closed behavior — still holds.
3. Run `npm run test:conformance:live` against at least one reachable hub
   build in the new range before marking the row ✅.
4. If a hub capability the SDK doesn't yet use changes, note it in "Notes"
   even when the row stays ✅ — that's how the next negotiation-upgrade task
   gets scoped instead of rediscovered from scratch.
5. If you're about to write "hub target: `<version>`" in this table, first
   check the version's revision date against `KNOWN_PROTOCOL_VERSIONS` in
   `mcp_types.version` on the hub you're testing against — an unfamiliar
   *later* date than the SDK's own `MCP_PROTOCOL_VERSION` is exactly the
   shape of mistake this correction fixes, and it is worth five minutes to
   rule out before it ships as fact again.
