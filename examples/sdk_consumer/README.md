# sdk_consumer — external governed consumer proof

The thinnest **real** consumer of the Unified Agent typed SDK that is **not**
part of the agent fleet: a standalone Node script holding only a bearer token,
talking to the hub over MCP streamable-HTTP through `@unified-agent/client`.

It performs exactly **one approval-gated, attributed action** —
`horizon2_package_register`, which the hub gates behind a human approval
unconditionally (`check_approval_gate(..., force=True)`, independent of
`UA_APPROVAL_TOOLS`) — and exports the evidence bundle for that action to a
JSON file.

The governed path is the real one, not a simulation:

```
consumer: horizon2_package_register(...)          # proposal
hub:      parked as `pending` approval row, call rejected
human:    approval_resolve(<id>, 'approve')       # any authenticated operator
consumer: retries the identical call              # one-shot pass consumed
hub:      governed_servers row + horizon2.connector.registered on the
          tamper-evident event hash chain
consumer: evidence-<server_id>.json               # exported proof bundle
```

Everything in the evidence file is a hub API read (`profile_status`,
`approval_pending`, `horizon2_server_list`, `event_log_query`,
`event_log_verify`, `evidence_report`) — the script fabricates nothing.

## Prerequisites

- Node.js ≥ 20 and npm
- A checkout of this repo
- Network reachability to the hub (e.g. Tailscale)
- A **non-the operator** agent bearer token (see "Identity" below)

## Install

```bash
# 1. Build the SDK packages the consumer links against
cd packages/protocol && npm ci && npm run build
cd ../client && npm ci && npm run build

# 2. Install the consumer (file: dependency on the built client)
cd ../../examples/sdk_consumer && npm install

# 3. Hermetic test — full gated flow against a mock wire, no hub needed
npm test
```

## Identity (operator step — cannot be done by the consumer)

The proof calls for a **dedicated low-privilege, non-the operator** identity. The
honest wiring for one:

```bash
# On the hub host ONLY (operator):
#   UA_AGENT_TOKENS  += "external_consumer_demo:<generated-token>"
#   UA_AGENT_LABELS  += ":external_consumer_demo"   # roster is closed
#   restart the hub service
```

That is a **hub-side secrets edit** — deliberately recorded here as an
operator step rather than performed by the consumer or committed anywhere.
Until it exists, any already-issued non-the operator agent token works for the
demo; the verified identity the hub reports is what lands in the evidence
(`profile_status` is asserted non-the operator at runtime).

## Run

```bash
export UA_HUB_URL=http://100.64.0.10:8765
export UA_AGENT_BEARER_TOKEN=<token>          # or: UA_AGENT_BEARER_TOKEN_FILE=~/.devin/unified-agent-token

cd examples/sdk_consumer
node consumer.mjs --wait-seconds 900 --poll-seconds 10 2>&1 | tee run-transcript.log
```

What happens, live:

1. `ping` + `profile_status` — handshake and the hub-verified identity.
2. A small tracking `task_add` anchors the run in the platform's task store
   (bookkeeping only — not part of the governed action; the `claimed-by:`
   note is the documented attribution marker so `evidence_report(agent=…)`
   finds the record). Skip with `--no-task`.
3. `horizon2_package_register` is proposed; the hub parks approval request
   `N` as `pending` and rejects the call.
4. The consumer polls `approval_pending` until a human resolves `N`
   (`--wait-seconds`, default 900s). Re-running with the same `--server-id`
   resumes cleanly — a parked row is reused, never duplicated.
5. On approval the identical call is retried, consumes the one-shot pass,
   and the connector registration executes.
6. `evidence-<server_id>.json` is written; the tracking task is closed.

The operator approves from any authenticated surface, e.g. a fleet MCP
session: `approval_resolve(id=<N>, decision="approve", resolved_by="Operator")`.
A `deny` is also a governed outcome — the consumer records it and exits 3.

### Flags

| flag | default | meaning |
|---|---|---|
| `--package` | `granola` | connector package (`slack`, `drive`, `obsidian`, `readwise`, `granola`) |
| `--server-id` | `sdk-consumer-<pkg>-<epoch>` | `governed_servers` key (`^[a-z][a-z0-9_.-]{0,63}$`) |
| `--review-ref` | `task:1137-sdk-consumer-proof` | required review reference |
| `--out` | `evidence-<server_id>.json` | evidence output path |
| `--wait-seconds` | `900` | approval wait budget |
| `--poll-seconds` | `10` | approval poll interval |
| `--scope` | package default | comma-separated subset of the package's scopes |
| `--no-task` | off | skip the tracking task |

### Exit codes

`0` approved + executed · `3` denied · `4` approval timeout (re-runnable) ·
`5` transport/auth/contract failure · `6` unexpected outcome · `2` usage

## What remains internal-only

Honest boundary of the external proof:

- **Token issuance** is hub-side (`UA_AGENT_TOKENS` + closed `UA_AGENT_LABELS`
  roster) and operator-only. The consumer can never mint or widen its own
  credential; a revoked or unknown token just fails auth.
- **Approval resolution** is a human/fleet action (`approval_resolve`, the
  `/ui` queue). The consumer can only propose and wait — it cannot
  self-approve; `UA_ENFORCE_AUTH=1` pins `resolved_by` to the verifier.
- **Attribution is bearer-bound.** `reviewed_by`, `requested_by`, and the
  event originator all come from the verified token identity, never from
  caller-supplied labels.
- **`evidence_report` is task-centric.** The register approval carries no
  `task_id`, so it does not appear in the task record's `action.approvals`;
  its durable linkage is the `pending → approved → consumed` row lifecycle
  plus the `agent.approval.*` / `horizon2.connector.registered` hash-chained
  events — all of which the bundle exports.
- The SDK used is the in-repo `@unified-agent/client` (`callTool` escape
  hatch — no new SDK surface was added for this consumer).
