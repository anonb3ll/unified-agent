# MCP 2.x Client Capability Matrix and Compatibility Guide

**Status:** Authoritative  
**Task:** #863 (MCP 2.x Wave 2)  
**Parent:** #862 (MCP 2.x Wave 2: compatibility, runtime hardening, and verification)  
**Module:** `src/unified_agent/mcp_matrix.py`  
**Test Suite:** `tests/test_mcp2_compatibility.py`  

---

## 1. Overview & Architecture

Unified-Agent serves as the central MCP tool and resource hub for multiple AI developer agents and IDE environments operating across Desktop and Laptop workstations:

- **Desktop Agent5**: Autonomous queue stewardship, multi-agent coordination, and dispatch orchestration.
- **Claude Code / Claude Desktop**: Interactive terminal CLI and desktop pair programmer.
- **agent-codex**: Autonomous headless coding worker, PR preparation, and test automation.
- **Google Antigravity (Agy)**: Agentic IDE, split-terminal workflows, and Agy CLI orchestration.
- **Cursor (agent-cursor)**: In-editor coding assistant, inline diffs, and workspace tool consumer.

With the upgrade to the **MCP 2.x SDK**, Unified-Agent exposes five advanced protocol capabilities:
1. **Tool Annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`)
2. **Structured Outputs** (`output_model` typed Pydantic models on tools)
3. **Elicitation** (`elicitation` client capability, `InputRequiredResult`, one-time challenge nonces)
4. **Progress Notifications** (`report_progress`, `_meta.progressToken` absolute milestones)
5. **Resource Subscriptions** (live-state resources: `unified-agent://board`, `unified-agent://tasks/{id}`, `unified-agent://runs/{id}`)

---

## 2. Capability Matrix

| Client | Supported Transports | Tool Annotations | Structured Outputs | Elicitation | Progress | Resource Subscriptions |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Desktop Agent5** | `streamable_http`, `stdio` | **Supported** | **Supported** | **Unsupported** | **Unsupported** | **Unsupported** (Polling) |
| **Claude Code / Desktop** | `streamable_http`, `stdio` | **Supported** | **Supported** | **Conditional** | **Supported** | **Partial** |
| **agent-codex** | `stdio`, `streamable_http` | **Supported** | **Supported** | **Unsupported** | **Unsupported** | **Partial** (Read) |
| **Google Antigravity (Agy)** | `streamable_http`, `stdio` | **Supported** | **Supported** | **Conditional** | **Supported** | **Supported** |
| **Cursor** | `streamable_http`, `stdio` | **Supported** | **Supported** | **Unsupported** | **Partial** | **Partial** (Read) |

### Transport Summary
- **Streamable HTTP (`POST /mcp`)**: The canonical transport for the primary hub on macOS (`http://127.0.0.1:8765/mcp`) with bearer token authentication (`UA_AGENT_TOKENS`), session management (`mcp-session-id`), and DNS-rebinding protection (`UA_HTTP_ALLOWED_HOSTS`).
- **stdio**: Process-bound stdin/stdout transport used for isolated satellite checkouts and headless automated runners.
- **SSE (Legacy `GET /sse`)**: Supported for backward compatibility; newer sessions use Streamable HTTP.

---

## 3. Capability Deep Dive & Failure Semantics

### 3.1 Tool Annotations
- **Protocol Contract**: FastMCP `ToolAnnotations` model containing boolean hints:
  - `readOnlyHint`: Tool does not mutate database, filesystem, or external systems.
  - `destructiveHint`: Tool performs state-mutating or irreversible operations (`run_cmd`, `fs_write`, `git_commit_staged`, `task_delete`, etc.).
  - `idempotentHint`: Safe to auto-retry with identical arguments. Note: Metered API calls (`gemini_ask`) and unkeyed mutations (`lifecycle_record`) are explicitly marked `idempotentHint=False`.
- **Failure / Missing Semantics**: When annotations are evaluated, unannotated tools are treated conservatively as potentially mutating. Compatibility validation audits all registered tools to ensure zero missing or conflicting annotations.

### 3.2 Structured Outputs
- **Protocol Contract**: Tools declare typed Pydantic output schemas (e.g. `graph_canvas_read` -> `GraphCanvasReadOutput`, `evidence_report` -> `EvidenceReportOutput`). MCPServer emits output JSON schemas in tool manifests and validates returned data.
- **Failure / Missing Semantics**: Schema validation failures raise `pydantic.ValidationError` explicitly naming the missing or malformed fields instead of silently degrading to untyped or corrupted data.

### 3.3 Elicitation (Interactive Approvals)
- **Protocol Contract**: MCP 2.x `InputRequiredResult` flow (task #822 / #829 / #832).
  - Gated tools (`UA_APPROVAL_TOOLS`) park a row in SQLite `approvals` and evaluate `_elicitation_available()`.
  - If available, server returns `InputRequiredResult` containing approval form schema and a CSPRNG challenge token.
  - Client prompts human user; client sends `accept + approve=True` along with echoed challenge token to resolve row as `approved`.
- **Preconditions Required**:
  1. `UA_ELICITATION_APPROVALS=1` (hub opt-in).
  2. Client advertises `elicitation` capability in initialize handshake.
  3. Calling session profile permits `approval_resolve` (operator authority; `developer` / `steward` cannot self-approve).
- **Failure / Missing Semantics (No Silent Degradation)**:
  - Client lacks elicitation capability -> `_elicitation_available()` evaluates `False` -> raises `ApprovalPendingError` indicating request is parked in `/ui` queue.
  - Config disabled (`UA_ELICITATION_APPROVALS=0`) -> raises `ApprovalPendingError`.
  - Restricted profile (`developer`/`steward`) -> raises `ApprovalPendingError`.
  - Invalid / replayed / mismatched challenge nonce -> rejects resolution; row remains pending.
  - Declined / cancelled form (`action="decline"` or `"cancel"`) -> row remains pending; `ApprovalPendingError` propagates.

### 3.3.1 Elicitation modes: form vs URL (task #2256)

The approval adapter in `src/unified_agent/elicitation.py` reads the client's
declared `elicitation` capability per call (`session.client_capabilities`) and
picks one of three paths. All three are dormant unless the hub sets
`UA_ELICITATION_APPROVALS=1` (off by default).

**What the server negotiates.** The hub does not advertise anything for
elicitation; it reads what the client declared. Per the 2025-11-25 spec, a bare
`elicitation: {}` means form only, `{"form": {}}` is form, `{"url": {}}` is URL
only, and both keys mean both. A url-only client is never sent a form it cannot
render.

| Path | Needs from the client | Needs from the hub | Authority | Falls back to |
| :--- | :--- | :--- | :--- | :--- |
| **URL** (preferred when available) | `elicitation.url` declared; protocol `2026-07-28`+ (the request rides `InputRequiredResult`); client opens the URL for the human and retries | `UA_ELICITATION_APPROVALS=1`; `config.public_origin()` set (`UA_MCP_OAUTH_ISSUER`, else `UA_PUBLIC_URL`) with no userinfo, query or fragment; an MCP session id bound by the HTTP middleware (stdio and in-memory transports carry none, so they never get URL mode) | The human resolving the row in the Control Center (`approval_resolve`, named human, not the requester). URL accept is consent to open the page, nothing more. The session's profile is not consulted because the session gains no authority. | Form, if the client also declares form and the profile permits `approval_resolve`; else the queue |
| **Form** (existing, tasks #822/#829/#832) | form declared (bare `{}` counts); protocol with `InputRequiredResult` | `UA_ELICITATION_APPROVALS=1` | The calling session's own `approval_resolve` permission, plus the one-time challenge | The queue |
| **Queue / legacy park-retry** | nothing | nothing | A human in the Control Center or `approval_resolve` | — |

**URL-mode flow.**

1. The gated call parks a `pending` row as always. The hub mints the existing
   one-time challenge for the row, stored as a SHA-256 hash bound to the
   approval id, MCP session id, verified principal, tool name and a digest of
   the call's arguments (so a head SHA or any other payload field is bound).
2. The result is an `InputRequiredResult` carrying one `elicitation/create`
   request with `mode: "url"` and
   `url = <public origin>/ui/inbox?kind=approval&item=<id>`, the Control Center's
   Inbox link for that approval. The URL holds no nonce, token or credential:
   the human signs in to the Control Center as usual. The challenge rides only
   the opaque `request_state`.
3. The client shows the URL; the human opens it and approves or denies the row
   there. The client returns `accept` (consent) and retries.
4. The retried call verifies the bound challenge, then waits server-side up to
   `_URL_WAIT_SECONDS` (30 s) for the row to leave `pending`. Still pending: it
   returns a state-only `InputRequiredResult` (no new prompt), and the client's
   MRTR driver re-polls by itself. Resolved: it consumes the challenge once and
   runs the tool once; the gate spends the human's pass, or raises the denial
   with its reason.
5. The SDK client gives up after `input_required_max_rounds` (default 10, about
   five minutes at 30 s per leg). The row stays `pending`, so the legacy
   park/retry path resolves it exactly as before.

There is no model-managed retry loop. Protocol retries do happen, by design.

**Failure semantics.**

- Decline or cancel: the row stays `pending`, the challenge is not consumed,
  and `ApprovalPendingError` propagates.
- A mismatched session, principal, tool or argument digest (`binding`), a
  challenge older than 15 minutes (`expired`), a replay after consumption
  (`replay`), or a URL nonce presented as a form approve: the row is not
  touched, nothing runs, and `challenge_rejected` is logged with the reason
  code.
- Flag turned off mid-flow, or URL capability missing on the retry: the row
  stays queued, and an approved pass is left for the legacy retry.
- The URL and the challenge are never written to the event log, an
  `ApprovalPendingError` message, or any tool result text the model reads.

**SDK support (`mcp` 2.3.0 / `mcp-types` 2.3.0 in `.venv`).** Complete for
this path: `ElicitRequestURLParams` (`mode: "url"`, `url`, `elicitation_id`
optional and dropped at 2026-07-28), `UrlElicitationCapability` /
`FormElicitationCapability` on `ElicitationCapability`, state-only
`InputRequiredResult`, and the client MRTR driver with backoff. Not used here:
`ElicitCompleteNotification` and the 2025-11-25-only `-32042`
`UrlElicitationRequiredError`. The SDK's own `Client` declares **both** form
and URL whenever an `elicitation_callback` is set, so the in-process tests are
not evidence of any real harness.

**Per-harness support.** No harness has an observed handshake recorded in this
repo. The form-mode entries below repeat what `mcp_matrix.py` asserts; nothing
in the repo has verified URL mode for any harness.

| Harness | Form elicitation | URL elicitation |
| :--- | :--- | :--- |
| Claude Code / Claude Desktop | Conditional (asserted in `mcp_matrix.py`, not observed) | **UNVERIFIED** |
| Google Antigravity (IDE + Agy CLI) | Conditional (asserted, not observed) | **UNVERIFIED** |
| Codex | Unsupported (asserted) | **UNVERIFIED** |
| Cursor | Unsupported (asserted) | **UNVERIFIED** |
| Hermes | Unsupported (asserted) | **UNVERIFIED** |
| OpenCode, Copilot | Not in `mcp_matrix.py` | **UNVERIFIED** |

To verify a harness, enable the flag on a test hub, make one gated call from
it, and read `client.capabilities.elicitation` and
`negotiated_protocol_version` from `mcp_health_telemetry()` (see
[the operator guide](mcp-2x-operator-guide.md)). A harness counts as URL-capable
only when it declares `elicitation.url` on `2026-07-28`+ and a human can complete
the Control Center round-trip from it.

### 3.4 Progress Notifications
- **Protocol Contract**: MCP 2.x `report_progress` via `_progress_ctx` ContextVar. Synchronous tool functions in worker threads call `mcp_progress.report_progress(progress, total, message)` which delegates to `Context.report_progress` on the AnyIO event loop.
- **Failure / Missing Semantics**: When no MCP request is bound (direct CLI / tests) or client omits `progressToken`, `report_progress` safely no-ops without error, while the capability evaluator flags that the client will not receive milestone telemetry.

### 3.5 Resource Subscriptions (Live State)
- **Protocol Contract**: MCP resources exposed under `unified-agent://`:
  - `unified-agent://board`: Compact board snapshot matching `task_list(status="all", fields="compact")`.
  - `unified-agent://tasks/{task_id}`: Single task record.
  - `unified-agent://runs/{run_id}`: Dispatch run status.
- **Failure / Missing Semantics**:
  - Unknown task / run URI -> raises `ResourceNotFoundError` (`"unknown task: <id>"` / `"unknown dispatch run: <id>"`).
  - Non-numeric / negative task ID -> raises `ResourceNotFoundError`.
  - Unauthorized profile (e.g. profile lacking `task_list` permission) -> raises `ResourceError` (`"resource is not exposed by the active <profile> profile"`).

---

## 4. Programmatic Capability Evaluation

The `unified_agent.mcp_matrix` module provides programmatic verification for client/transport capability combinations:

```python
from unified_agent.mcp_matrix import (
    McpCapability,
    check_client_compatibility,
    get_client_matrix,
)

# Check Desktop Agent5 for elicitation capability
result = check_client_compatibility(
    client_name_or_id="hermes",
    transport="streamable_http",
    required_capabilities=[McpCapability.ELICITATION],
)

assert result["compatible"] is False
assert "elicitation" in result["missing_capabilities"]
# Explicit failure diagnostic explains the fallback path:
print(result["diagnostics"])
# -> ['Capability "elicitation" is unsupported by Desktop Agent5. Fallback: Approval-gated tools park in /ui queue and raise ApprovalPendingError. Failure indicator: ApprovalPendingError propagates with /ui queue resolution pointer']
```

---

## 5. Verification & Test Suite

Compatibility and failure paths are verified deterministically in `tests/test_mcp2_compatibility.py`:
- `test_matrix_data_integrity`: Verifies all 5 supported clients define valid capabilities and transports.
- `test_tool_annotations_consistency`: Verifies every registered tool has explicit annotations with no overlapping read-only and destructive flags.
- `test_structured_outputs_validation_and_failures`: Tests schema conformance and ensures missing required fields raise `ValidationError` with field identity.
- `test_elicitation_supported_and_unsupported_combinations`: Exercises all 6 permutation branches (supported, missing client capability, disabled config, restricted profile, invalid challenge, form decline).
- `test_progress_notifications_bound_and_unbound`: Exercises worker thread absolute progress reporting, multi-step progress, and safe unbound no-op.
- `test_resource_subscriptions_visibility_and_error_paths`: Tests reading board/task/run resources, template listings, unknown task/run `ResourceNotFoundError`, and profile denial `ResourceError`.
