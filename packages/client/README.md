# `@unified-agent/client`

Typed hub client for Unified Agent (ADR: TypeScript Platform Strategy).

This is this repository's own client, used by the Control Center and in-repo
consumers. The SDK for applications outside this repo is
[`anonb3ll/ua-rust`](https://github.com/anonb3ll/ua-rust) → `sdk/typescript`.

**Invariant:** this package is a **typed remote**. Tool calls go to the Python
hub over MCP streamable-HTTP; HTTP/JSON writes go through authenticated
Control Center routes. Policy, approvals, sandbox, and identity stay on the
hub. There is no local fallback and no second policy implementation.

## Install / build

```bash
cd packages/protocol && npm ci && npm run build
cd ../client && npm install && npm test && npm run build
```

## Usage

```ts
import {
  ControlCenterClient,
  HubAuthError,
  HubTransportError,
  HubValidationError,
  UnifiedAgentClient,
} from "@unified-agent/client";
import { z } from "zod";

const hubOrigin = process.env.UA_HUB_URL;
if (!hubOrigin) throw new Error("UA_HUB_URL is required");
const hubMcpUrl = `${hubOrigin.replace(/\/$/, "")}/mcp`;
const token = process.env.UA_AGENT_BEARER_TOKEN!;

const client = new UnifiedAgentClient({
  url: hubMcpUrl,
  bearerToken: token,
});

try {
  await client.ping();
  // Discovery is read-only; the hub still decides profile access and policy.
  const profile = await client.profileStatus();
  const controlCenter = new ControlCenterClient({ baseUrl: hubOrigin, bearerToken: token });
  const [manifest, evidence, live] = await Promise.all([
    controlCenter.platformManifest(),
    controlCenter.capabilityCatalog(),
    client.capabilityCatalog(),
  ]);
  console.log(profile.identity, manifest.object_types.length, evidence.available);
  console.log(live.mcp_tools.includes("task_update") ? "known" : "unknown");
  for (const item of evidence.catalog?.items ?? []) {
    console.log(item.id, item.connection_state ?? "unknown");
  }
  const taskEnvelopeSchema = z.object({
    uri: z.string(),
    task: z.object({ id: z.number(), title: z.string() }).passthrough(),
  });
  const taskRef = (await controlCenter.queueHealth()).desktop_open[0]?.platform;
  if (taskRef) {
    const task = await controlCenter.platformObject(taskRef, taskEnvelopeSchema);
    console.log(task.task.title);
  }
} catch (error) {
  if (error instanceof HubAuthError) console.error("forbidden", error.status);
  else if (error instanceof HubValidationError) console.error("malformed discovery", error.message);
  else if (error instanceof HubTransportError) console.error("unavailable", error.message);
  else throw error;
} finally {
  await client.close();
}
```

## Surface (v0.4 write path)

| Method | Hub tool | Notes |
|---|---|---|
| `ping` | `tools/list` | Handshake + auth check |
| `taskList` / `taskAdd` / `taskUpdate` / `taskClaim` | `task_*` | Claim + updates are full mutations |
| `memorySearch` / `memorySave` / `memoryGet` | `memory_*` | Saves are mutations; forward `idempotency_key` |
| `approvalPending` / `approvalResolve` | `approval_*` | Pending returns the full five-key queue payload |
| `delegate` | `delegate` | Creates child task + ledger row |
| `vaultJournal` | `vault_journal` | Append-only vault write |
| `dispatchCancel` / `dispatchResume` | `dispatch_*` | Governed run control |
| `dispatchDeliveryReceipt` | `dispatch_delivery_receipt` | Read-only per-run accountability receipt (policy/approval outcomes, lease, transitions, executor-auth probe) |
| `profileStatus` | `profile_status` | Active profile + identity |
| `systemDoctor` | `system_doctor` | `{as_of, checks, host, hosts, summary}` — no phantom `status` |
| `mcpHealthTelemetry` | `mcp_health_telemetry` | Protocol negotiation health |
| `ControlCenterClient.queueHealth` | `GET /api/queue-health` | Read-only typed queue view |
| `ControlCenterClient.listRuns` | `GET /api/runs` | Read-only run projection. Query: `kind` (`dispatch`, `selfpickup`, `session`), `agent`, `task_id`, `limit`, `cursor`. Page: `{runs, next_cursor, count, truncated, limit}` |
| `ControlCenterClient.getRun` | `GET /api/runs/{run_id}` | One run row. An unknown id raises `HubTransportError` with status 404 |
| `ControlCenterClient.platformManifest` | `GET /api/platform/manifest` | Read-only object registry |
| `ControlCenterClient.platformObject` | `GET /api/platform/objects/{kind}/{id}` | Read one authenticated object; caller supplies the Zod envelope schema |
| `ControlCenterClient.capabilityCatalog` | `GET /api/capability-catalog` | Static, provenance-bearing evidence |
| `ControlCenterClient.createTask` / `updateTask` | `POST /api/tasks` / `/api/action/task` | Actor is derived from verified hub identity |
| `ControlCenterClient.requestApproval` | `POST /api/approvals/request` | Queues only; never executes or resolves the request |
| `ControlCenterClient.resolveApproval` | `POST /api/action/approval` | Hub permits verified human identities only |
| `ControlCenterClient.createEvidence` | `POST /api/evidence` | Canonical evidence writer; actor/originator are derived by the hub |
| `ControlCenterClient.submitWorkProduct` / `releaseWorkProduct` | `POST /api/work-products` / `/api/work-products/{id}/release` | Hub workspace file, named-human review, stored-input release retry; the hub enforces profile, policy, and separation of duties |
| `capabilityCatalog` | `capability_catalog` | Live MCP operator inventory |
| `callTool` | any | Untyped escape hatch |

Protocol Zod schemas from `@unified-agent/protocol` validate list/search/claim
responses when `validate` is true (default). Failures raise `HubValidationError`.

### `TaskUpdateRequest` / `TaskCreateRequest` field narrowing (task #1300)

The hub's `action_task` route (`/api/action/task`) accepts a wider field
whitelist than `TaskUpdateRequest` exposes. Two of those extra fields were
deliberately reconciled, one way each, when the typed contract was audited:

- **`title` and `parent_id` were added.** Both are ordinary, policy-gated
  mutation fields — the hub applies them with the same generic tool-call
  gating as `notes`/`priority`/`project`, so narrowing them out only forced
  callers around the typed client. `parent_id` follows the hub tool's own
  sentinel: `0` detaches the task from its parent, any other positive id
  reparents it, and omitting the field leaves the parent unchanged.
- **`untrusted` stays out of both `TaskCreateRequest` and
  `TaskUpdateRequest`, and always will.** It is a hub-computed
  provenance/audit signal (`config.is_agent_untrusted`, derived from the
  verified caller's agent label) that the executor's trust-ladder gate reads
  directly to decide whether to quarantine a task's work. Exposing it as a
  writable field would let a typed client declare `untrusted: false` for its
  own work and launder that signal. `task_add`'s hub-side merge is OR-only
  (a caller-supplied `true` can only add the flag), but `task_update`'s
  currently lets a caller-supplied value override the computed one in either
  direction — a hub-side gap this SDK narrowing exists specifically to not
  reopen from the typed surface.
The task, approval, and evidence HTTP methods also validate strict request
schemas before sending and validate their response envelopes. They always send
the bearer identity; callers cannot supply actor, originator, or resolver fields.
Task and evidence writes return a typed `PendingApprovalResult` with
`status: "pending"` and `approval_id` when policy parks the mutation; callers
must wait for a verified human decision before retrying the same payload.
Evidence channels must begin with `sdk.`.

Work-product submission accepts a path on the **hub workspace**, not an upload
from the caller's machine. A pending response includes the work-product and
approval ids. After a different, verified human approves, the original
producing identity calls `releaseWorkProduct(id)`; the hub reloads the saved
path, tags, pack, title, and model, so the retry cannot alter reviewed inputs.
The developer profile remains denied by the hub. A first-party CLI adapter
uses these same SDK methods without putting the bearer token in command-line
arguments:

```bash
export UA_AGENT_BEARER_TOKEN=... # obtain from the hub operator
printf '%s' '{"source_path":"/hub/workspace/drafts/memo.md","title":"Synthetic memo","template_pack":"legal","tags":{"matter":"M-1","client":"Example"}}' \
  | node packages/client/bin/work-product.mjs submit http://127.0.0.1:8765 UA_AGENT_BEARER_TOKEN
printf '%s' '{"work_product_id":27}' \
  | node packages/client/bin/work-product.mjs release http://127.0.0.1:8765 UA_AGENT_BEARER_TOKEN
```

The live HTTP-write conformance suite is disabled by default. Run it only
against a disposable hub by setting `UA_SDK_WRITE_CONFORMANCE_URL` and
`UA_SDK_WRITE_CONFORMANCE_TOKEN`; optionally set
`UA_SDK_WRITE_CONFORMANCE_HUMAN_TOKEN` to verify successful human-only approval
resolution. It creates a task, queues a denyable approval, and appends one
deduplicated evidence event. `requestApproval` only queues a request for a tool
the hub gates (an ungated tool is refused with 409), so the approval leg asks
for `crew_intervene`, which ua-rust gates by default; on a hub that gates other
tools, set `UA_SDK_WRITE_CONFORMANCE_APPROVAL_TOOL` and, if its arguments
differ, `UA_SDK_WRITE_CONFORMANCE_APPROVAL_PAYLOAD` (a JSON object).

## Reliability (v0.2)

- Expired MCP sessions (`HTTP 404`) re-handshake once, then retry the same call
  (same policy as Python `HubClient`).
- Transient transport failures (network / 502–504) retry only when the call is
  retryable: pass `CallToolOptions.retryable`, or include a non-empty
  `idempotency_key` in tool arguments. Mutations without an idempotency key are
  never blind-retried.
- `HubAuthError` and `HubRpcError` never retry.
- `AbortSignal` cancels the in-flight JSON-RPC request and sends
  `notifications/cancelled` exactly once.

## Non-goals (this package)

- Reimplementing `safety.py` / approval gates in TypeScript
- Embedding credentials or dual-writing to a second database
- A full generated wrapper for all ~150 tools (use `callTool` until codegen)
- Publishing to npm (still `private: true`; publication is a separate decision)

## Release notes — next (unreleased)

- Read-only runs projection: `listRuns` (`GET /api/runs`) and `getRun`
  (`GET /api/runs/{run_id}`). `Run` keeps kind-specific fields. An unknown
  run id is `HubTransportError` with status 404.
- Authenticated Control Center writes (#1282): typed task create/update,
  pending-only approval request, human-only approval resolution, and
  identity-attributed evidence append; strict Zod request schemas are generated
  into JSON Schema/Pydantic outputs.
- Typed `dispatchDeliveryReceipt` consumer (task #944): validates the hub's
  `DeliveryReceipt` output schema — policy/approval outcomes, lease snapshot,
  transition history, and the executor-auth probe — and raises
  `HubValidationError` on drift. Unknown `run_id` remains a hub tool error
  (`HubRpcError`), not a null receipt.

## Release notes — 0.2.1

- Typed-surface fidelity (#929): `Task` keeps queue fields; `approvalPending`
  returns all five hub keys; `systemDoctor` matches the live payload; tool-error
  `content[].text` becomes `HubRpcError.message`.
- Transport hardening: single-flight `ensureSession`, validated
  `protocolVersion`, multi-frame/multi-line SSE decode.

## Release notes — 0.2.0

- Session recovery + bounded idempotent transport retries.
- Typed wrappers: `taskClaim`, `profileStatus`, `systemDoctor`, `mcpHealthTelemetry`.
- Expanded `taskList` / `taskAdd` / `taskUpdate` / `memorySave` params (lane,
  domain, evidence, idempotency).
- `HubValidationError` for fail-closed response validation.
- Protocol `Task` optional fields: `domain`, `lane`, `due_at`, `evidence_ref`,
  `source_host`, `archived_at`.
