# MCP SDK Transport Modernization Spike Findings

**Date:** 2026-08-19  
**Task:** #454 (follow-up to #452)  
**Target:** MCP Python SDK Transport Modernization (`mcp` v1.28.1+ Streamable HTTP / SSE)  
**Status:** Verified via isolated test harness (`tests/harness/test_mcp_transport_spike.py`)

---

## 1. Executive Summary

This spike evaluates newer Model Context Protocol (MCP) Python SDK transport mechanics—specifically Streamable HTTP and SSE transports, session lifecycle management, cancellation, progress reporting, and transport security—without altering the live unified-agent hub, its production dependencies, or runtime configuration.

All evaluations were executed using an isolated, deterministic, in-memory test harness built on Starlette and HTTPX ASGI transports.

---

## 2. Tested Architecture & Transport Landscape

| Transport Layer | Specification / Endpoint | Role & Behavior |
| :--- | :--- | :--- |
| **Streamable HTTP** | `POST /mcp`, `DELETE /mcp`, `GET /mcp` | Modern stateful HTTP transport introduced in MCP 2024-11-05; mints `mcp-session-id` on initialization and multiplexes tool calls, notifications, and cancellation over HTTP/SSE. |
| **SSE (Legacy/Standard)** | `GET /sse`, `POST /messages?session_id=...` | Standard Server-Sent Events transport where client opens persistent SSE stream and sends JSON-RPC payloads to separate message endpoint. |
| **stdio** | Stdio pipes (`sys.stdin` / `sys.stdout`) | Direct process-bound transport used for local sub-processes. |

---

## 3. Verified Capabilities & Harness Evidence

The test harness (`tests/harness/test_mcp_transport_spike.py`) validates 17 targeted scenarios across 6 test suites:

### 3.1 Streamable HTTP Handshake & Header Validation
- **Session Initialization:** `POST /mcp` with `initialize` method negotiates protocol version (`2024-11-05`), mints a cryptographically secure `mcp-session-id` header (>= 16 chars), and returns server capabilities.
- **Accept Header Enforcement:** Requests without `application/json` or `text/event-stream` in the `Accept` header are rejected with `406 Not Acceptable`.
- **Content-Type Validation:** Requests with non-JSON media types are rejected with `400` / `415`.
- **Malformed JSON Handling:** Malformed payloads return standard JSON-RPC parse errors (`-32700` / HTTP 400).

### 3.2 Session Lifecycle & Continuation
- **Stateful Continuation:** Subsequent `POST /mcp` requests carrying the `mcp-session-id` header successfully execute tools and maintain session state.
- **Session Expiration / Missing ID:** Requests missing session headers on non-init methods or carrying invalid/unknown session IDs return `404 Not Found` or `400 Bad Request`.
- **Explicit Termination:** Sending `DELETE /mcp` with `mcp-session-id` cleanly disposes session resources; subsequent calls return `404`.

### 3.3 Progress Reporting & In-Flight Cancellation
- **Progress Reporting:** Tool executions receiving `_meta.progressToken` emit progress notifications (`ctx.report_progress`) without blocking request completion.
- **In-Flight Cancellation:** Sending `notifications/cancelled` with `requestId` triggers immediate cancellation (`asyncio.CancelledError`) in the running tool coroutine and returns a cancellation response.

### 3.4 Transport Security & DNS Rebinding Protection
- **Host / Origin Validation:** When `TransportSecuritySettings(enable_dns_rebinding_protection=True)` is enabled, requests with untrusted `Host` headers are rejected with `421 Misdirected Request`.

### 3.5 Interoperability with `HubAuthASGIMiddleware`
- **Bearer Token Enforcement:** `HubAuthASGIMiddleware` operating in `UA_ENFORCE_AUTH=1` blocks unauthorized requests (HTTP 401) before reaching the FastMCP session manager.
- **Warn-Only Mode:** In `UA_ENFORCE_AUTH=0` (default during transition), unauthenticated requests are permitted with a warning log.
- **Context Propagation:** Authenticated agent identity (`UA_AGENT_TOKENS`) and mapped workspace root (`UA_AGENT_WORKSPACES`) correctly bind to `config.agent_identity_var` and `config.workspace_root_var` across tool executions.
- **Cookie Isolation:** Browser session cookies (`ua_session`) are strictly forbidden on `/mcp` endpoints; MCP access remains bearer-token-only.

---

## 4. Explicitly Not-Run & Unavailable Capabilities

To avoid speculative claims of compatibility:

1. **Multi-Node Distributed Event Store Resumption (`NOT_RUN`):**
   - Resuming SSE or HTTP event streams across multiple host nodes requires a centralized Redis/database-backed event store. In this single-node local harness, distributed event replay is marked `NOT_RUN`.
2. **Dynamic OAuth2 Client Registration (`NOT_RUN`):**
   - Full OAuth2 Authorization Server delegation requires external identity infrastructure. Evaluated against local bearer token auth only.
3. **Cross-Session ContextVar Mutation (`UNAVAILABLE`):**
   - FastMCP runs background tasks that do not inherit arbitrary caller ContextVar mutations post-initialization. Context must be established per-request at the ASGI middleware boundary.
4. **Bidirectional Stdio / Streamable HTTP Hybrid (`UNAVAILABLE`):**
   - Stdio and HTTP transports remain separate protocol drivers in the SDK; bridging requires an explicit transport proxy adapter.

---

## 5. Compatibility Risks & Migration Recommendations

Before bumping `pyproject.toml` or enabling Streamable HTTP on the live hub:

1. **ASGI Middleware Order:**
   - `HubAuthASGIMiddleware` MUST wrap the FastMCP ASGI application on the outer layer, so unauthorized requests are terminated before session state is allocated in FastMCP's session manager.
2. **Reverse Proxy / Host Header Whitelisting:**
   - If `TransportSecuritySettings` DNS rebinding protection is active, reverse proxies (e.g., Nginx, Caddy, Tailscale) must pass matching `Host` headers or be explicitly listed in `allowed_hosts`.
3. **Client Accept Headers:**
   - Any client calling `/mcp` MUST include `Accept: application/json, text/event-stream` to avoid 406 rejection.

---

## 6. Verification Results

- **Test Suite:** `tests/harness/test_mcp_transport_spike.py`
- **Results:** 17 passed in 1.86s
- **Linter & Formatter:** Ruff 100% clean
- **Security Check:** Zero network listeners opened, in-memory ASGI transport only
