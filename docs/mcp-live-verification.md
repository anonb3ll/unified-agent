# MCP 2.x Live Progress and Resource-Subscription Verification

**Date:** 2026-09-09  
**Task:** UA #864 (Parent: #862 MCP 2.x Ecosystem Upgrade Wave 2)  
**Target:** Live/unmocked verification of MCP 2.x progress notifications, resource subscriptions, reconnect semantics, and stale-client handling.  
**Status:** Verified via deterministic test suite (`tests/test_mcp_live_verification.py`), integration harness (`src/unified_agent/mcp_live_verification.py`), and smoke runner (`scripts/mcp_live_smoke.py`).

---

## 1. Executive Summary

This deliverable verifies the live server and client paths for MCP 2.x progress notifications and resource subscriptions within `unified_agent` across real execution environments (both in-process Client and streamable HTTP ASGI transport) without relying on test mocks alone.

All four core domains specified in task #864 are fully implemented and verified:
1. **Long-running progress emission:** Multi-stage monotonic milestone reporting across synchronous tools using `mcp_progress.report_progress` / `Context.report_progress` and client callbacks.
2. **Subscription lifecycle:** Initial snapshot capture, active event streaming (`subscriptions/listen`), direct and out-of-band poller updates, and clean unsubscribe teardown.
3. **Reconnect semantics:** Reconnecting clients reconcile offline mutations from snapshot state and resume live streams without dropped or duplicate updates.
4. **Stale-client behavior:** Optimistic concurrency guard rejection (`expected_updated_at`), 404 responses for deleted or invalid URIs, isolated subscriber fan-out, and profile-scoped access control.

---

## 2. Protocol Architecture & Live Flow

```
+--------------------------------------------------------------------------------+
|                                MCP 2.x Client                                  |
+--------------------------------------------------------------------------------+
     |                  |                           |                     |
     | 1. Snapshot      | 2. Listen stream          | 3. Tool call with   | 4. Reconnect
     |    read_resource |    subscriptions/listen   |    progressToken    |    Snapshot S1
     v                  v                           v                     v
+--------------------------------------------------------------------------------+
|                             Unified-Agent Hub                                  |
|                                                                                |
|  +------------------------+  +------------------------+  +------------------+  |
|  | mcp_live_state         |  | InMemorySubscriptionBus|  | mcp_progress     |  |
|  | - read_board()         |  | - publish(ResourceUpd) |  | - report_progress|  |
|  | - read_task(id)        |  | - subscribe(listener)  |  | - progress_scope |  |
|  | - read_run(id)         |  | - fan-out isolation    |  | - AnyIO bridge   |  |
|  | - poll_loop (SQLite)   |  +------------------------+  +------------------+  |
|  +------------------------+                                                    |
|               |                                         |                      |
|               v                                         v                      |
|  +--------------------------------------------------------------------------+  |
|  | SQLite storage: tasks (updated_at revisions), dispatch_runs              |  |
|  +--------------------------------------------------------------------------+  |
+--------------------------------------------------------------------------------+
```

---

## 3. Verified Capabilities & Test Matrix

The verification suite (`tests/test_mcp_live_verification.py`) exercises 19 deterministic tests covering the full capability matrix:

| Capability Group | Scenario / Invariant | Real-Path Verification | Status |
| :--- | :--- | :--- | :--- |
| **Progress Emission** | Multi-stage milestone reporting | `pi_run_task` tool reports 4 ordered milestones `[(0, 3, "starting Pi"), (1, 3, "Pi spawned"), (2, 3, "first tool"), (3, 3, "success")]` | **PASS** |
| **Progress Emission** | Fractional & burst updates | Sync worker emitting fractional progress `(0.2 ... 1.0)` delivered accurately | **PASS** |
| **Progress Emission** | Failure recovery | Tool failure midway reports progress up to failure point; subsequent calls on same session succeed | **PASS** |
| **Progress Emission** | Unbound safety | Direct Python call outside worker/MCP context executes as safe no-op | **PASS** |
| **Progress Emission** | HTTP Streamable transport | Tool call carrying `_meta.progressToken` over streamable HTTP executes cleanly | **PASS** |
| **Subscription Lifecycle** | Initial snapshot | `read_resource` retrieves complete `unified-agent://board`, `tasks/{id}`, and `runs/{id}` | **PASS** |
| **Subscription Lifecycle** | Direct tool mutation | Same-process `task_update` publishes `BOARD_URI` and `task_uri(id)` events immediately | **PASS** |
| **Subscription Lifecycle** | Selective subscription | Client subscribed strictly to `task_uri(1)` receives events only for task 1 (filters task 2) | **PASS** |
| **Subscription Lifecycle** | Background SQLite poller | Out-of-band task update in SQLite detected by `poll_loop` within interval and published | **PASS** |
| **Subscription Lifecycle** | Dispatch run updates | `notify_run_change(run_id)` delivers `run_uri(run_id)` event to subscriber | **PASS** |
| **Subscription Lifecycle** | Clean unsubscribe | Exiting `client.listen` context unsubscribes listener; subsequent mutations operate without dangling references | **PASS** |
| **Reconnect Semantics** | Offline delta reconciliation | Client disconnects -> 3 offline mutations (add, update, delete) -> reconnect snapshot S1 captures 100% of offline delta (0 lost updates) | **PASS** |
| **Reconnect Semantics** | Live stream resumption | Reconnected client receives subsequent online updates over new subscription stream | **PASS** |
| **Stale-Client Behavior** | Optimistic lock rejection | Stale `expected_updated_at` revision is rejected with authority mismatch error | **PASS** |
| **Stale-Client Behavior** | Deleted resource 404 | Reading deleted task returns `ResourceNotFoundError` ("unknown task") | **PASS** |
| **Stale-Client Behavior** | Malformed / invalid URI | Non-integer task IDs and invalid run IDs fail closed with `ResourceNotFoundError` | **PASS** |
| **Stale-Client Behavior** | Subscriber fault isolation | Raising/dropped listener does not crash the subscription bus or block healthy listeners | **PASS** |
| **Stale-Client Behavior** | Profile access control | `research` profile (`agent-grok`) hides run templates and denies unauthorized run resource access | **PASS** |
| **Integration Harness** | End-to-end report generation | `LiveProgressAndSubscriptionHarness.run_all()` completes 4/4 checks with structured evidence | **PASS** |

---

## 4. Reconnection and Reconciliation Invariant

When an MCP client disconnects and reconnects:

$$\Delta_{\text{offline}} = \text{State}(T_{\text{reconnect}}) \setminus \text{State}(T_{\text{disconnect}})$$

1. **Snapshot Baseline ($S_0$):** Read at $T_0$ prior to disconnect.
2. **Offline Window:** Mutations occurring during disconnect ($T_0 \to T_1$) update SQLite `tasks` and advance `updated_at`.
3. **Reconnection Snapshot ($S_1$):** Client reads `read_resource(BOARD_URI)` at $T_1$. $S_1$ reflects all newly created, updated, and deleted tasks.
4. **Subscription Resumption:** Client re-opens `client.listen(resource_subscriptions=[...])` at $T_1$. All subsequent mutations ($T > T_1$) are streamed as `ResourceUpdated` events.
5. **Zero Lost / Duplicate Updates:** No phantom updates or missed state transitions occur during or after reconnection.

---

## 5. Live Smoke Verification Evidence

The live smoke test (`scripts/mcp_live_smoke.py` and `scripts/smoke.py`) executes end-to-end against an isolated test environment:

```text
=== MCP 2.x Live Verification Report [2026-09-09T21:02:11Z] ===
Status: PASS (4/4 checks passed, 0 failed, 596.7ms)
Progress milestones recorded: 4
Live events recorded: 6
Checks:
  [PASS] progress_emission: Received 4 monotonically ordered milestones matching expected sequence
  [PASS] subscription_lifecycle: Delivered 2 direct events ({'unified-agent://board', 'unified-agent://tasks/1'}) and 2 poller events cleanly
  [PASS] reconnect_reconciliation: 0 lost updates in offline window; post-reconnect snapshot and live stream reconciled cleanly
  [PASS] stale_client_guards: Guards verified: optimistic_lock=True, deleted_404=True, invalid_404=True, profile_gate=True
```

Included in root smoke suite (`make smoke` / `python scripts/smoke.py`):
```text
[PASS] mcp_live_progress_and_subscriptions: verified progress (4 milestones) and subscriptions (6 events) in 617.8ms
smoke: 11 passed, 1 skipped, 0 failed (12 total)
```
