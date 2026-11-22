# Authenticated HTTP read inventory (#1880)

The route table is authoritative. This audit records all 62 authenticated GET entries and the finite guarded call graph reviewed for #1880. Only 38 entries reach registered tools (32 names: 31 read helpers plus the two exact plan manager read actions). Other entries retain direct projections or explicit per-object checks; this proposal grants them nothing.

Read declarations admit only their named helper at the registered wrapper, never MCP discovery. `vault_ingest_status` is mixed: its handler pins `mark=False`, and the wrapper rejects `mark=True` under a read grant. Existing path, policy, approval and agent-access gates remain. Credential-pool rows remain metadata-only; notification delivery remains outcome metadata. Tests use private stores or controlled read projections, with no provider/host probing.

| GET route | Guarded helper(s) |
|---|---|
| `/api/whoami` | None in the registered call path; retain existing projection/handler gates |
| `/api/overview` | `approval_pending`, `check_anomalies`, `delegate_list`, `dependency_list`, `event_log_query`, `handoff_list`, `message_list`, `metrics_snapshot`, `plan_list`, `system_doctor`, `task_list`, `workflow_list` |
| `/api/attention` | `approval_pending`, `autonomous_cycle_list`, `check_anomalies`, `system_doctor`, `task_list` |
| `/api/digest` | `approval_pending`, `autonomous_cycle_list`, `check_anomalies`, `system_doctor`, `task_list` |
| `/api/agents` | `task_list` |
| `/api/personas` | None in the registered call path; retain existing projection/handler gates |
| `/api/personas/{label}` | None in the registered call path; retain existing projection/handler gates |
| `/api/org` | `task_list` |
| `/api/dispatch-routes` | None in the registered call path; retain existing projection/handler gates |
| `/api/agents/{label}` | `agent_activity` |
| `/api/tasks` | `task_list` |
| `/api/board` | `delegate_list`, `dependency_list`, `task_list` |
| `/api/flow-metrics` | `task_list` |
| `/api/queue-health` | `task_list` |
| `/api/projects` | None in the registered call path; retain existing projection/handler gates |
| `/api/review-approval` | `approval_pending`, `task_list` |
| `/api/executor-delegation` | `delegate_list`, `system_doctor` |
| `/api/approvals` | `approval_pending` |
| `/api/approvals/{id}` | None in the registered call path; retain existing projection/handler gates |
| `/api/memory` | `memory_list`, `memory_search` |
| `/api/vault/search` | `vault_search` |
| `/api/vault/page` | `vault_links`, `vault_read` |
| `/api/vault/ingest-status` | `vault_ingest_status` |
| `/api/vault/briefing` | `vault_briefing_digest` |
| `/api/connectors` | `gcloud_inventory`, `horizon2_package_list`, `horizon2_server_list` |
| `/api/quota` | `model_quota_status` |
| `/api/credential_pool` | `credential_pool_list` |
| `/api/slack_gateway` | `event_log_query` |
| `/api/notify_delivery` | `notify_delivery_status` |
| `/api/dispatch_receipts` | `dispatch_delivery_receipts_list` |
| `/api/agent-graph` | `delegate_list`, `message_list`, `system_doctor`, `task_list` |
| `/api/zulip` | None in the registered call path; retain existing projection/handler gates |
| `/api/execution-graph` | `delegate_list`, `dependency_list`, `handoff_list`, `system_doctor`, `task_list` |
| `/api/dispatch-stack` | `dependency_list`, `task_list` |
| `/api/cycles` | `autonomous_cycle_list` |
| `/api/cycles/{cycle_id}` | `autonomous_cycle_get` |
| `/api/plans` | `plan_manage` |
| `/api/plans/{plan_id}` | `plan_manage` |
| `/api/proposals` | None in the registered call path; retain existing projection/handler gates |
| `/api/health` | `system_doctor` |
| `/api/cloud` | `gcloud_inventory` |
| `/api/devices` | `system_doctor` |
| `/api/nodes` | None in the registered call path; retain existing projection/handler gates |
| `/api/mobile/v1/projection` | `approval_pending`, `autonomous_cycle_list`, `check_anomalies`, `system_doctor`, `task_list` |
| `/api/langgraph/checkpoints` | `langgraph_checkpoints` |
| `/api/evidence` | None in the registered call path; retain existing projection/handler gates |
| `/api/platform/manifest` | None in the registered call path; retain existing projection/handler gates |
| `/api/platform/objects/{kind}` | None in the registered call path; retain existing projection/handler gates |
| `/api/platform/objects/{kind}/{object_id}` | None in the registered call path; retain existing projection/handler gates |
| `/api/platform/provenance-graph` | None in the registered call path; retain existing projection/handler gates |
| `/api/capability-catalog` | None in the registered call path; retain existing projection/handler gates |
| `/api/perf` | `perf_summary` |
| `/api/delivery_slo` | None in the registered call path; retain existing projection/handler gates |
| `/api/costs` | None in the registered call path; retain existing projection/handler gates |
| `/api/tool-allowance` | None in the registered call path; reads the gateway rate-limit table and the decision ledger, per-caller fence; another identity or `*` needs the verified operator on a human channel (task #2144) |
| `/api/scorecards` | None in the registered call path; retain existing projection/handler gates |
| `/api/events` | `event_log_query` |
| `/api/telemetry` | None in the registered call path; retain existing projection/handler gates |
| `/api/teams` | None in the registered call path; retain existing projection/handler gates |
| `/api/agent-lifecycle` | None in the registered call path; retain existing projection/handler gates |
| `/api/oauth/passkeys` | None in the registered call path; retain existing projection/handler gates |
| `/api/graph-canvas` | None in the registered call path; retain existing projection/handler gates |
| `/api/harness-config` | None in the registered call path; retain existing projection/handler gates |
| `/api/runs` | None in the registered call path; reads dispatch_runs, the agent.run.selfpickup channel and agent_sessions under the `dispatch_report_self` ceiling (task #1652) |
| `/api/runs/{run_id}` | None in the registered call path; same ceiling, 404 on unknown or other-tenant id |

`tests/test_webui_read_contract.py` exercises the finite HTTP contract with guarded projections and checks mutation, identity, discovery and lifetime limits. Existing quota, credential-pool, notification, vault, plans, connectors and other route suites validate real source projections and privacy constraints. The inventory follows local helper calls transitively; the only dynamic registered dispatch is `_safe(name)`, whose audited callers use literal tool names. Explicit platform-object profile checks are unchanged.

This is source evidence. The original live restart/reconnect cause remains unproven.
