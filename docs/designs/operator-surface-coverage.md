# Operator surface coverage — capability → pane / CLI

> Superseded for navigation and panes by [Control Center v2](control-center-v2-spec.md) (cutover, task #2479).

**Task:** #1125 (agent-cursor, 2026-09-19).
**Source of truth for gates:** [`wayfinder-pane-parity-audit.md`](./wayfinder-pane-parity-audit.md).
**Catalog:** `docs/capability-catalog/catalog.json` (55 items; **do not edit from this lane** — #1124/#1151 own it).

This map answers: for every agent-callable capability with **operator value**,
where does a human operator go — Control Center pane (`?view=`), CLI, or
neither (gap → filed task)?

“Operator value” means the operator (or a steward) would reasonably want to *see
or act on* the capability without spawning an agent session. Pure library
internals, deliberate non-wiring (`not-applicable`), and agent-only bookkeeping
are listed as **agent/library** rather than gaps.

---

## Summary

| Catalog `entry_point.kind` | Count | Operator surface today |
|---|---:|---|
| `mcp-tool` | 31 | Mostly MCP; many also have panes/CLI (table below) |
| `library` | 7 | Code / policy — Doctor or docs, not a pane |
| `web-ui` | 7 | Declared in catalog (under-declared vs real NAV) |
| `not-applicable` | 7 | Intentional non-surface |
| `config` | 2 | Env / gateway config |
| `event-hook` | 1 | Background wake — no operator pane |

Modern NAV exposes **28** modes (`packages/web/src/shell/nav.ts`). Only **7**
catalog rows declare `web-ui` — the catalog is behind the shipped IA; gaps below
are about *missing operator affordances*, not about rewriting `catalog.json`
in this PR.

---

## Capability → surface

| ID | Capability | Pane (`?view=`) | CLI / other | Notes |
|---|---|---|---|---|
| `gov-identity-profiles` | Identity / profiles | Doctor, Platform | MCP `profile_status` | Operator sees health via Doctor |
| `gov-authorization-policy` | Sandbox / policy gates | Doctor | `safety.py` / docs | Library |
| `gov-approval-queue` | Approvals | **Approvals** | MCP `approval_*` | CSRF mutation |
| `gov-evidence-reports` | Evidence reports | **Evidence** | MCP `evidence_report` | |
| `gov-audit-ledger` | Event log / hash chain | Evidence, Sessions feed | `unified-agent-audit-ledger`, MCP `event_log_*` | |
| `gov-review-merge-receipts` | Review / merge receipts | **Delivery Receipts** (`?view=dispatch_receipts`) | `GET /api/dispatch_receipts`, MCP `dispatch_delivery_receipts_list` / `dispatch_delivery_receipt` | List + drill-down — closed #1182 |
| `gov-security-audit-tooling` | Security audit | — | `unified-agent-audit`, MCP `security_audit` | CLI exists; no pane (acceptable) |
| `dsp-task-lifecycle` | Tasks / claims | **Tasks**, **Board** | MCP `task_*` | |
| `dsp-dependencies-lineage` | Dependencies / handoffs | **Tasks** (add dep), **Dispatch** (handoff), **Flow** / **Task Graph** (read) | MCP `dependency_*`, `handoff` | Wired in #1125 C4 |
| `dsp-plans` | Plans | **Plans** | MCP `plan_*` | |
| `dsp-delegation` | Delegation ledger | **Executor** | MCP `delegate*` | |
| `dsp-scheduler-running` | Dispatch scheduler | **Dispatch**, **Board** | `unified-agent-dispatch-scheduler` | Catalog `web-ui` |
| `dsp-executor-boundary` | Executor daemon | — | `unified-agent-executor` | N/A surface |
| `dsp-autonomous-cycles` | Cycle lifecycle | **Cycles** | MCP `autonomous_cycle_*` | |
| `dsp-capability-checks` | Capability catalog | **Capabilities** | MCP `check_agent_capability` / `capability_catalog` | |
| `run-horizon2-connectors` | Connectors | **Connectors** | MCP `horizon2_*` | |
| `run-envelopes` | Execution envelopes | — | library | Agent/library |
| `run-cross-harness-boundaries` | Cross-harness launch | Dispatch (indirect) | library | Agent/library |
| `lgp-engine-checkpoints` | LangGraph engine | **LangGraph** | MCP `langgraph_*` | |
| `lgp-governed-workflow-run` | Governed workflow | **Dispatch** (Run workflow) | MCP `langgraph_workflow_run` / `workflow_run` | |
| `lgp-progress-observability` | Progress notes | LangGraph / Sessions | MCP `langgraph_progress_note` | |
| `lgp-graph-canvas` | Planning canvas | **Canvas** | MCP `graph_canvas_*` | |
| `lgp-plan-review-lifecycle` | Plan review | **Plans** | MCP `plan_step_claim` | |
| `lgp-langsmith-boundary` | LangSmith | — | N/A by decision | |
| `prq-provider-chain` | LLM providers | Doctor | MCP `gemini_ask` | |
| `prq-credential-pool` | Credential pool | **Credential Pool** | MCP `credential_pool_*` | Read-only pane, now including revoked rows (task #1121/#1184); register/revoke stay MCP-only, withheld from operator clients by `tool_classification.WITHHELD_TOOLS`, plus any policy-config approval rule an operator adds. |
| `prq-quota-status` | Model quota | **Quota** | MCP `model_quota_status` | |
| `prq-subscription-pool` | Subscription pool | — | library | Agent/library |
| `prq-usage-telemetry` | Usage telemetry | **Telemetry** | — | Catalog `web-ui` |
| `prq-failsoft-nospend` | Fail-soft / no-spend | Doctor (indirect) | library | |
| `fm-session-capture` | Session capture | **Sessions** | `unified-agent-session-end`, MCP `session_*` | |
| `fm-delivery-modes` | Delivery modes | — | library | Agent/library |
| `fm-scout` | Scout intake | — | N/A | |
| `fm-supervision` | Supervisor wake | — | event-hook | |
| `fm-crew-watch` | Crew watch / intervene | **Sessions** | MCP `crew_*` | Catalog `web-ui` |
| `fm-operator-attention` | Attention | **Home** / **Attention** | MCP attention API | Catalog `web-ui` |
| `tel-mcp-health` | MCP health | Doctor | MCP `mcp_health_telemetry` | |
| `tel-event-log` | Durable events / SSE | Sessions activity | MCP `event_log_*` | |
| `tel-metrics-anomaly-review` | Anomalies | Attention / Doctor | MCP `check_anomalies*` | |
| `tel-utilization-views` | Utilization views | **Telemetry** | — | Catalog `web-ui` |
| `tel-otel-internals` | OTel internals | — | N/A | |
| `int-slack` | Slack gateway | **Slack** (`?view=slack_gateway`) | `GET /api/slack_gateway`, MCP `event_log_query` | Status only (LIVE/DORMANT/MISCONFIGURED) — closed #1185 |
| `int-zulip-relay` | Zulip | **Zulip** | MCP `zulip_*`, workers | |
| `int-imessage-notify` | iMessage notify | **Notify Delivery** (`?view=notify_delivery`) | `GET /api/notify_delivery`, MCP `notify_delivery_status` | Read-only delivery status — closed #1183; send stays `notify` / `batch_notify` |
| `int-oauth-boundary` | OAuth boundary | — | config | Agent/library |
| `int-gcp-agent20` | GCP ops | **Cloud** | MCP `gcloud_*` | |
| `int-mobile-watch` | Mobile / Watch | **Devices** | — | Catalog `web-ui` |
| `int-modern-webui` | Modern SPA | `/ui` (all panes) | — | Catalog `web-ui` |
| `int-sdk-conformance` | TS SDK | — | `packages/client` | Developer, not operator |
| `mem-agent-memory` | Agent memory | **Memory** | MCP `memory_*` | |
| `mem-vault-obsidian` | Vault | **Vault** | MCP `vault_*`, `unified-agent-wiki` | |
| `mem-session-packet-recall` | Session packets | Sessions (related) | MCP `session_packet` | |
| `mem-gbrain-boundary` | Gbrain | — | N/A (external) | |
| `exc-projection-receiving-half` | Browser projection | — | N/A | |
| `exc-boundary-isolation` | Browser isolation | — | N/A | |

Also on NAV but not yet named as catalog `web-ui` rows (catalog debt for
#1124/#1151, not new panes): Proposals, Plans, Vault, Connectors, Quota,
Platform, Doctor, Cycles, Executor, Memory, Cloud, Zulip, etc.

---

## Filed gaps (no invented panes)

| Gap | Why operator-valued | Filed as |
|---|---|---|
| Review / merge receipt browser | Operators need to see delivery receipts without MCP spelunking | **#1182** |
| ~~Credential-pool operator view~~ | ~~Pool register/revoke is approval-sensitive; no pane~~ | **#1184 — closed** |
| Slack operator surface | Slack is a live ingress; status/errors are config-only today | **#1185** |
| Notify / iMessage delivery status | Outbound paging has no operator confirmation pane | **#1183** |

These are **filed tasks**, not implemented panes in #1125, except where noted closed.

### #1184 — Credential-pool operator view (closed)

The read path mostly already existed by the time this gap was picked up:
task #1121 built `GET /api/credential_pool`
(`src/unified_agent/webui/routes.py`, `webui/api.py::credential_pool_payload`)
and the **Credential Pool** pane (`packages/web/src/credential_pool/`), wired
into `nav.ts` and `App.tsx`, before this design doc's #1125 audit pass caught
up to it — but it fell short of #1184's stated objective
("operator-visible credential-pool status (**registered/revoked**/metadata
only)"): `credential_pool_payload()` called `credential_pool_list()` with no
arguments, so it took that tool's own default of `include_revoked=False` and
silently hid every revoked credential from the pane — arguably the single
most operationally interesting state. #1184's fix (this PR) is a one-line
change — `credential_pool_list(include_revoked=True)` — plus a red-then-green
regression test
(`test_revoked_credential_appears_and_is_marked_revoked` in
`tests/test_webui_credential_pool.py`) asserting a revoked row now reaches
the payload with `status: "revoked"` through the same metadata-only field
allowlist as any other row. See also `tests/test_credential_pool.py` and
`test_never_forwards_a_secret_shaped_field` for the read-only and
no-secret-leak coverage, both still passing.

Mutations (`credential_pool_register` / `credential_pool_revoke`) remain
MCP-only: no new write path was added. Precisely what keeps them off this
operator surface is narrower than "approval-gated" implies, though — both
tools are listed in `tool_classification.WITHHELD_TOOLS`, which keeps them
out of the baseline tool set handed to operator-facing MCP clients, and any
further per-call approval requirement comes from the optional Policy Engine
DSL (`policy.yaml`, evaluated by `safety.check_policy_gate` →
`policy.evaluate_policy`) — which only enforces anything for a tool that a
configured rule actually matches. No rule scoped to `credential_pool_*`
ships by default in this repo, so today the gate is "not exposed to operator
clients at all" (via `WITHHELD_TOOLS`), not "requires human approval" as a
built-in guarantee; an operator can add a `policy.yaml` rule to require
approval explicitly if they want that stronger guarantee.

---

## C6 build evidence (this worktree)

Built on 2026-09-19 in this worktree (`cd packages/web && npm run build`):

| Artifact | Value |
|---|---|
| SPA entry (from `dist/index.html`) | `assets/index-3Zs6PRSv.js` |
| Content SHA-256 | `4489035306bf9e9a7bc86de7ddf93647095151a10069933441c64b80b6d2757d` |

Live Mac-hub re-verification is Hermes's deploy step — command:

```bash
# After deploy-mac-hub.sh web-bundle step on the Mac hub:
curl -sS http://<hub-ip>:8765/ui/ | tee /tmp/ua-ui.html \
  | rg -o 'assets/index-[A-Za-z0-9_-]+\.js'
ASSET=$(rg -o 'assets/index-[A-Za-z0-9_-]+\.js' /tmp/ua-ui.html | head -1)
curl -sS "http://<hub-ip>:8765/ui/${ASSET}" | sha256sum
# Expect basename index-3Zs6PRSv.js and sha256
# sha256: 4489035306bf9e9a7bc86de7ddf93647095151a10069933441c64b80b6d2757d
# (or the newer hash if main advanced before deploy).
```
