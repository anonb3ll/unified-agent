# MCP 2.x Operator Guide and Runtime Reference

**Status:** Authoritative  
**Task:** UA #867 (Parent: #862 MCP 2.x Ecosystem Upgrade Wave 2)  
**Modules:**
- `src/unified_agent/server.py`
- `src/unified_agent/mcp_runtime_telemetry.py` (`mcp_telemetry.py` is a re-export shim)
- `src/unified_agent/tools/mcp_health.py`
- `src/unified_agent/elicitation.py`
- `src/unified_agent/mcp_progress.py`
- `src/unified_agent/mcp_live_state.py`
- `src/unified_agent/dispatch/coordinator.py`
- `src/unified_agent/tools/dispatch_report.py`
**Smoke Verification:** `scripts/mcp2_smoke.py` / `tests/test_mcp2_smoke.py`

---

## 1. Overview & Architectural Scope

Unified-Agent provides a unified Model Context Protocol (MCP) server that connects multiple developer agent runtimes (Claude Code, Google Antigravity / Agy, agent-codex, Cursor, and agent-hermes) to a shared tool and state layer.

Following the **MCP 2.x Wave 2 Hardening Roadmap** (UA #862), the runtime provides:
1. **Tool Risk Annotations & Structured Schemas**: Formal, machine-readable safety hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`) and typed output schemas.
2. **Interactive Elicitation with Nonce Challenge**: In-client human approvals for gated actions via `InputRequiredResult` with cryptographic replay protection.
3. **Progress Notifications & Live Resource Subscriptions**: Real-time progress milestone streaming and live resource observation over `unified-agent://` URIs with automatic SQLite change detection.
4. **Governed Operation Cancellation & Resumption**: Deterministic execution control (`dispatch_cancel`, `dispatch_resume`) with fail-closed lease checks and lineage validation.
5. **Runtime Health Telemetry**: In-process observability of the negotiated protocol, client capabilities, subscriptions, progress, and HTTP sessions via `mcp_health_telemetry` and `system_doctor`. Reconnect, stale-snapshot, and version-skew events are not recorded today; see §7.2.

---

## 2. Transports, Authentication & Network Topology

### 2.1 Transport Endpoints
Unified-Agent supports two primary runtime transports and one legacy fallback:

| Transport | Protocol Path | Default Endpoint | Intended Audience |
| :--- | :--- | :--- | :--- |
| **Streamable HTTP** *(Primary)* | `POST /mcp` | `http://127.0.0.1:8765/mcp` | Persistent hub serving local and LAN developer IDEs. |
| **stdio** *(Headless / Isolated)* | Stdio pipe | Subprocess stdin/stdout | Automated CI workers, satellite checkouts, and single-task runners. |
| **SSE** *(Legacy fallback)* | `GET /sse` | `http://127.0.0.1:8765/sse` | Backward-compatibility for older clients. |

### 2.2 Security & Access Controls
- **Bearer Token Auth (`UA_AGENT_TOKENS`)**: Client authentication format `UA_AGENT_TOKENS="agent-claude:token1,agent-agy:token2"`. Handled in HTTP headers as `Authorization: Bearer <token>`.
- **Session Identity (`mcp-session-id`)**: Each Streamable HTTP connection negotiates a distinct session ID, preserving connection metadata and capability flags across requests.
- **DNS Rebinding Protection (`UA_HTTP_ALLOWED_HOSTS`)**: Restricts acceptable HTTP `Host` header values (defaults to `127.0.0.1`, `localhost`, and the Tailscale IP of the hub).
- **CORS / Allowed Origins (`UA_HTTP_ALLOWED_ORIGINS`)**: Default configuration explicitly admits loopback origins and local browser extensions.

---

## 3. Tool Annotations & Structured Output Contracts

### 3.1 Tool Risk Annotations
Every registered tool exposes FastMCP `ToolAnnotations` metadata:
```python
class ToolAnnotations(BaseModel):
    readOnlyHint: bool  # Tool performs no disk/DB mutations
    destructiveHint: bool  # Tool performs irreversible/high-risk mutations
    idempotentHint: bool  # Tool is safe for automatic retry
```

#### Rule Enforcement & Classification:
- **`readOnlyHint=True`**: Tools that only query data (e.g. `task_list`, `fs_read`, `mcp_health_telemetry`, `system_doctor`).
- **`destructiveHint=True`**: Tools that alter external systems, remove data, or execute code (e.g. `run_cmd`, `fs_write`, `git_commit_staged`, `task_delete`, `dispatch_cancel`).
- **`idempotentHint=True`**: Tools safe to repeat. Unmetered read-only tools and idempotent writes (`fs_index_symbols`, `format_render`).
- **`idempotentHint=False`**: Metered billable API calls (`gemini_ask`), unkeyed mutations (`lifecycle_record`), and one-off workflows.

### 3.2 Structured Output Schemas
Tools returning complex structured data specify typed output models (e.g. `EvidenceReportOutput`, `GraphCanvasReadOutput`). FastMCP serializes these models into JSON Schema in the tool manifest. Validation errors raise explicit `ValidationError` diagnostics before responses return to the caller.

---

## 4. Interactive Elicitation & Nonce Challenge

When an action hits a configured approval gate (such as `UA_APPROVAL_TOOLS="run_cmd:task_delete"`), the hub parks a `pending` row in the approvals store.

### 4.1 Elicitation Resolution Flow
```
Client                          Hub                                Approvals DB
  |                              |                                       |
  | 1. tools/call (gated tool)   |                                       |
  |----------------------------->| 2. Check gate -> Pending              |
  |                              |-------------------------------------->|
  |                              | 3. Create CSPRNG Nonce                |
  | 4. InputRequiredResult       |                                       |
  |    (form + challenge token)  |                                       |
  |<-----------------------------|                                       |
  |                              |                                       |
  | [Human Approves In-Client]   |                                       |
  |                              |                                       |
  | 5. tools/call (with response |                                       |
  |    + challenge token)        | 6. Validate & consume nonce (atomic)  |
  |----------------------------->|-------------------------------------->|
  |                              | 7. approval_resolve(id, "approved")   |
  |                              | 8. Execute tool & return result       |
  | 9. Tool Result               |                                       |
  |<-----------------------------|                                       |
```

### 4.2 Security Constraints
- **Opt-In Flag (`UA_ELICITATION_APPROVALS=1`)**: Must be explicitly set in the hub environment. If unset, gated calls raise `ApprovalPendingError` requiring resolution via the `/ui` approval queue.
- **Session Profile Gate**: Only sessions with `approval_resolve` tool permission can resolve via elicitation.
- **Single-Use Cryptographic Nonce**: The hub generates a 32-byte CSPRNG token (`token_urlsafe`), hashes it (SHA-256), and stores it alongside the pending approval. When the client returns the challenge, it is consumed in a single atomic SQLite transaction. Replay attempts or cross-row token reuse fail closed.

---

## 5. Progress Notifications & Live Resource Subscriptions

### 5.1 Progress Milestone Reporting
Long-running tools report monotonic progress via `mcp_progress.report_progress(progress, total, message)`:
- Tools run in worker threads using `anyio.to_thread`.
- `progress_scope` binds the client context across thread boundaries.
- If the client supplies `_meta.progressToken`, progress events stream asynchronously to the client.
- If no client context exists, the call safely no-ops without error, while still capturing progress telemetry.

### 5.2 Resource Subscriptions & Live Poller
The hub exposes live state through three URI patterns:
- `unified-agent://board` — Compact task board summary.
- `unified-agent://tasks/{task_id}` — Individual task state and metadata.
- `unified-agent://runs/{run_id}` — Live dispatch execution status.

#### Subscription Mechanics:
1. **Immediate Publishing**: Same-process operations (`task_update`, `dispatch_cancel`) call `notify_task_change` or `notify_run_change` to publish `ResourceUpdated` events immediately.
2. **Background Poller Loop**: `mcp_live_state.poll_loop` polls SQLite revisions every `POLL_INTERVAL_S` (default 1.0s) to notify subscribers of changes made by out-of-process workers (active executors, cron jobs).
3. **Snapshot Reconciliation**: Reconnecting clients fetch a new snapshot upon connection, and use revision diffing to resume event streaming with zero duplicate or lost events.
4. **Failure Recovery**: A failed SQLite poll closes its connection and retries. After three consecutive failures the server logs a warning. Active-session clock changes notify on minute buckets; clients may read the resource at any time for the current age.
5. **Evidence Boundary**: The event cursor detects appends, pruning, and tail rewrites. An in-place rewrite of a non-tail event with unchanged row count and tail is outside live notification; use the evidence chain verifier to detect that tampering.
6. **Startup Baseline**: When the poller starts, it builds a baseline that later polls are diffed against. Resource reads wait for that baseline, so a read is not answered from state the baseline then absorbs without a notification. A read that waits `BASELINE_WAIT_S` (30 s) without a baseline fails with a `live state is not ready` resource error; retry it. One exception: when three consecutive baseline attempts fail, the poller stops holding reads. Reads made after that are best-effort, and a commit landing before the late baseline can go unannounced.

---

## 6. Governed Cancellation & Resume Semantics

For background dispatch runs and long-running subtasks, Unified-Agent provides explicit lifecycle control tools:

### 6.1 Tools
- `dispatch_cancel(run_id: str, reason: str)`: Request governed termination of an active dispatch run.
- `dispatch_resume(run_id: str)`: Resume a previously cancelled or interrupted run.

### 6.2 Fail-Closed Invariants
1. **Caller Identity Verification**: Only the agent label that owns the run (or human operator) can cancel or resume it. Identity mismatches raise `ToolError`.
2. **State Validation**:
   - Resuming a run in terminal finished state (`finished`, `dead_letter`) fails with `terminal_finished`.
   - Resuming a run whose underlying task has been cancelled fails with `task_terminal`.
   - Resuming an already-active run fails with `already_active`.
3. **Lease Collisions**: Active process leases prevent duplicate worker execution. Resumption acquires a fresh lease before relaunching.
4. **Lineage Safety**: If a parent task has been terminated or superseded, dependent child runs cannot be resumed.

---

## 7. Protocol Negotiation & Health Telemetry

### 7.1 Protocol Versions
- **Target / Latest Protocol**: `2026-07-28` (`mcp.types.LATEST_PROTOCOL_VERSION`), reported as `target_protocol_version`.
- **Health floor**: `2025-03-26` (`mcp.types.DEFAULT_NEGOTIATED_VERSION`). The MCP SDK still serves `2024-11-05` through the `initialize` handshake; the hub does not refuse it, it reports it as `degraded` (§7.2).

### 7.2 What the Telemetry Store Records (`mcp_runtime_telemetry`)
The store is process-local memory. It is empty after every hub restart, and it is
global to the process: the connection fields hold whichever client made the most
recent tool call, not one row per session.

Recorded in production today:

| Field in `mcp_health_telemetry()` | Recorded by | When |
| :--- | :--- | :--- |
| `negotiated_protocol_version`, `client` (name, version, sanitized capabilities) | `record_connection`, from the `_with_elicitation` tool boundary in `server.py` | On every MCP tool call that carries a request context. Capability keys containing `token`, `secret`, or `key` are dropped. |
| `transport_mode` | `record_connection` | Always `"streamable_http"`: the boundary does not pass a transport, so a stdio session reports the default too. |
| `subscriptions` | `record_subscription_counts`, from `mcp_live_state` | Each time the hub publishes a resource-change notification (`publish_uris`): after a same-process mutation, or when the live-state poller detects a SQLite change. Counts are `total`, `board`, `tasks`, `runs`, plus any other registry resource kind. |
| `progress` | `record_progress`, from `mcp_progress.report_progress` | On each progress milestone a tool reports, even when no MCP request is bound, so the count is not limited to MCP clients. |
| `session` (last id, last agent label, touch count) | `record_session_touch`, from the HTTP auth middleware in `server.py` | On each HTTP request that resolves a verified agent identity or carries an `mcp-session-id` header. stdio sessions never touch it. |

Present in the output but **not recorded in production**:

- `reconnect` (attempts, successes, failures, recent events), `stale_snapshots`, and
  `version_skew`. `record_reconnect`, `record_stale_snapshot`, and
  `record_version_skew` exist in `mcp_runtime_telemetry.py`, but nothing outside
  `tests/test_mcp_runtime_telemetry.py` calls them. On a live hub these counters
  stay `0` and the lists stay empty. A client that fails the handshake is refused
  by the MCP SDK before any hub code runs, so it never reaches the store.

`status` is computed by `evaluate_health()` in this order:

1. `incompatible` when any version-skew failure is recorded. Unreachable in production today, because nothing records one.
2. `degraded` when a reconnect failure is recorded. Unreachable in production today.
3. `degraded` when a stale snapshot is recorded. Unreachable in production today.
4. `degraded` when the last negotiated protocol sorts below `2025-03-26` (in practice, a `2024-11-05` client). **This is the only non-healthy state a live hub can report.** A version between the floor and the target, such as `2025-06-18`, is `healthy`.
5. `healthy` otherwise, including before any tool call has negotiated a protocol.

`actionable_evidence` carries the one-line reason for a non-healthy status. The
tool also returns `schema_fingerprint`, `manifest_schema_fingerprint`, and
`registry_schema_fingerprint` for detecting tool-schema drift on a long-lived
connection.

### 7.3 System Doctor Integration
`system_doctor` includes `mcp2_runtime_telemetry`, derived from the same `evaluate_health()`:
- **`LIVE`**: A protocol has been negotiated and the status is `healthy`.
- **`DORMANT`**: No tool call has recorded a negotiated protocol since the hub process started.
- **`MISCONFIGURED`**: The status is `degraded` or `incompatible`. On a live hub today this means only that the last client negotiated a protocol older than `2025-03-26`.

---

## 8. Operator Troubleshooting & Incident Runbooks

### Runbook 1: Doctor Reports a Legacy Protocol
- **Symptom**: `mcp_health_telemetry` status reports `degraded`; doctor reports `MISCONFIGURED` with `Legacy protocol negotiated`.
- **What the reading means**: The store keeps one global slot for protocol and client. Every MCP tool call overwrites `negotiated_protocol_version`, `client.name` and `client.version` in the tool boundary (`server.py`, `record_connection`) before the tool runs. That includes the call to `mcp_health_telemetry` or `system_doctor` itself. A reading therefore describes the most recent MCP caller, which is usually you. If you call from a modern client, the legacy warning disappears and the older client's name is gone. The hub keeps no history of past clients and logs no handshake line, so a legacy client cannot be identified after the fact.
- **Diagnostic**:
  1. To see the warning without overwriting it, read the doctor row through the Control Center: `GET /api/health` (the `/ui` health view). The web path calls `system_doctor` with no MCP session, so it does not record a connection. The `mcp2_runtime_telemetry` detail gives the legacy protocol version but not the client's name. The response is cached for up to 30 seconds; add `?force=1` to refresh.
  2. To find the client, call `mcp_health_telemetry` from each suspect client in turn. Each reply shows that client's own `negotiated_protocol_version` and `client.name` and `client.version`, because the call records the caller first. A concurrent call from another client can still interleave, so repeat a surprising result. You can also check each client's MCP SDK version on the client side.
  3. Running the function in a fresh Python process shows only an empty store, because the store lives in the hub process.
- **Resolution**:
  1. Upgrade the legacy client's MCP SDK so it negotiates `2025-03-26` or newer.
  2. The status clears as soon as any modern client makes a tool call, or on hub restart. A clear status does not prove the legacy client is gone; repeat step 2 of the diagnostic for that client.

### Runbook 2: Reconnects, Stale Snapshots, and Version Skew
The hub does not observe these events today (§7.2), so `mcp_health_telemetry`
cannot show them and a `healthy` status says nothing about them. Diagnose from the
client and transport instead:
1. **Unknown version in `initialize`.** The hub does not refuse it: the SDK answers
   with its latest handshake version, and the client decides whether it can speak
   it. Read the client's MCP log for the version it accepted.
2. **Era mismatch.** An `initialize` on a 2026-style (enveloped) connection gets
   `UNSUPPORTED_PROTOCOL_VERSION`, naming the served versions; an enveloped request
   on a legacy connection gets `INVALID_REQUEST`. The client must reconnect in one era.
3. For a client that stalls after a hub restart, reconnect the client so it renegotiates a session, then re-read `unified-agent://board`.
4. Check hub logs for transport errors: `~/.unified_agent/hub.log` and `hub.err` on the Mac hub (launchd, `com.unified-agent.hub`), or `journalctl --user -u unified-agent` on a systemd host.

### Runbook 3: Interactive Elicitation Not Prompting In-Client
- **Symptom**: Tool calls park in `/ui` approval queue instead of popping up an in-editor form.
- **Diagnostic**:
  1. Check if `UA_ELICITATION_APPROVALS=1` is set in the hub environment (`systemctl --user show unified-agent`).
  2. Verify the client declares `elicitation` in its capabilities handshake.
  3. Verify the caller agent profile has `approval_resolve` capability.

---

## 9. Automated Smoke Verification Suite

The repository includes an end-to-end unmocked smoke suite to verify all MCP 2.x capabilities in an isolated environment.

### Running the Smoke Suite
```bash
# Human-readable summary output
uv run python scripts/mcp2_smoke.py

# JSON output for automated ingestion
uv run python scripts/mcp2_smoke.py --json
```

### Verified Checks:
1. `check_tool_annotations_and_schemas` (reported as `tool_annotations_and_schemas`): Verifies tool risk annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) across all registered tools.
2. `check_progress_emission` (reported as `progress_emission`): Emits multi-stage progress milestones through `mcp_progress` and asserts telemetry updates.
3. `check_live_resource_subscriptions` (reported as `live_resource_subscriptions`): Verifies `unified-agent://board`, `tasks`, and `runs` resources and bus listener telemetry.
4. `check_governed_cancellation_and_resumption` (reported as `governed_cancellation_and_resumption`): Dispatches a run, verifies `dispatch_cancel`, and asserts successful `dispatch_resume` with state transitions.
5. `check_health_and_doctor_telemetry` (reported as `health_and_doctor_telemetry`): Exercises `mcp_health_telemetry` and verifies `system_doctor` returns a valid status.

Exit code is `0` when all checks pass, and `1` if any check fails.
