# Complete tool catalog

The server registers these 201 MCP tools. Each is declared in its component's
manifest under [`components/`](../src/unified_agent/components/), and
[`server.py`](../src/unified_agent/server.py) composes them (see
[Manifests](architecture/components.md#manifests)). The common server wrapper applies
the active profile, policy gate, and performance record to every registered call.

This page helps an operator find a tool family. Use [Tools reference](tools-reference.md)
for parameters, limits, return fields, and examples. Read the named source module
and its tests when a tool contract is unclear.

## Research, workspace, and quality

| Family | Tools | Source |
|---|---|---|
| Web and local documentation | `web_search`, `web_fetch`, `dash_list`, `dash_search`, `dash_view`, `gemini_ask` | [`web.py`](../src/unified_agent/tools/web.py), [`dash.py`](../src/unified_agent/tools/dash.py), [`gemini.py`](../src/unified_agent/tools/gemini.py) |
| Workspace inspection | `fs_read`, `fs_write`, `fs_list`, `fs_search`, `fs_index_symbols`, `fs_list_symbols`, `fs_search_symbols`, `fs_repo_map`, `read_image` | [`files.py`](../src/unified_agent/tools/files.py), [`indexer.py`](../src/unified_agent/tools/indexer.py), [`repo_map.py`](../src/unified_agent/tools/repo_map.py), [`media.py`](../src/unified_agent/tools/media.py) |
| Commands and code quality | `run_cmd`, `run_cmd_check`, `python_lint_and_format`, `detect_dev_servers` | [`shell.py`](../src/unified_agent/tools/shell.py), [`lint.py`](../src/unified_agent/tools/lint.py), [`devservers.py`](../src/unified_agent/tools/devservers.py) |
| Security review | `security_audit`, `injection_triage`, `audit_triage_tasks`, `skill_review`, `red_team_qa` | [`audit.py`](../src/unified_agent/tools/audit.py), [`skills.py`](../src/unified_agent/tools/skills.py), [`red_team.py`](../src/unified_agent/tools/red_team.py) |

## Source control and durable state

| Family | Tools | Source |
|---|---|---|
| Git and worktrees | `git_diff_staged`, `git_commit_staged`, `git_worktree_list`, `git_worktree_add`, `git_worktree_remove`, `git_merge_task`, `cleanup_stale_worktrees` | [`git.py`](../src/unified_agent/tools/git.py), [`worktree_policy.py`](../src/unified_agent/tools/worktree_policy.py) |
| Memory | `memory_save`, `memory_search`, `memory_list`, `memory_get`, `memory_restore`, `memory_feedback`, `memory_delete`, `memory_prune_candidates`, `memory_promote` (copy a high-confidence note to a `Wiki/Learnings` page; `dry_run` previews), `recall_maintenance` (report stale and low-confidence notes; never deletes) | [`memory.py`](../src/unified_agent/tools/memory.py), [`memory_prune.py`](../src/unified_agent/tools/memory_prune.py), [`recall_curation.py`](../src/unified_agent/recall_curation.py) |
| Tasks and dependencies | `task_add`, `task_update`, `task_list` (ranked mode via `query=`, see [`docs/task-search.md`](task-search.md)), `task_delete`, `task_claim`, `project_build`, `dependency_list`, `dependency_add`, `dependency_remove`, `handoff`, `handoff_list`, `coordination_manage` (team specs: `team_up`, `team_list`, `team_seat`, `team_spec_commit`, see [`docs/team-spec.md`](team-spec.md); queue closure: `queue_health`, `queue_stale`, `queue_sweep`, `queue_park`, `queue_fold`, `queue_unpark`, `queue_close_merged`, see [`docs/work-queue.md`](work-queue.md#closure-stale-parked-folded-merged); subtree holds: `task_hold`, `task_holds`, `task_hold_get`, see [`docs/work-queue.md`](work-queue.md#subtree-holds-pause-cancel-resume-restore); operator claim release: `task_claim_release`, see [`docs/agent-lifecycle.md`](agent-lifecycle.md); operator node retire: `node_retire`, `node_unretire`, see [`docs/fleet-console.md`](fleet-console.md#retire-a-node-task-2181); supervised long commands: `node_command_start`, `node_command_status`, `node_command_adopt`, see [`docs/harnesses.md`](harnesses.md#supervised-long-commands-task-2413)), `graph_canvas_read`, `graph_canvas_write`, `evidence_report` | [`tasks.py`](../src/unified_agent/tools/tasks.py), [`task_search.py`](../src/unified_agent/task_search.py), [`graph.py`](../src/unified_agent/tools/graph.py), [`team_spec.py`](../src/unified_agent/team_spec.py), [`queue_closure.py`](../src/unified_agent/queue_closure.py), [`task_holds.py`](../src/unified_agent/task_holds.py), [`graph_canvas.py`](../src/unified_agent/tools/graph_canvas.py), [`evidence.py`](../src/unified_agent/tools/evidence.py) |
| Goals and skills registry | `goal_manage`, `task_comment`, `skill_registry` | [`goals.py`](../src/unified_agent/goals.py), [`task_comments.py`](../src/unified_agent/task_comments.py), [`skills_registry.py`](../src/unified_agent/skills_registry.py), [skills-registry.md](skills-registry.md) |
| State checks and export | `state_sync_verify`, `export_tasks`, `export_memory`, `export_journal`, `export_migration_manifest`, `compare_migration_manifests`, `import_migration_manifest`, `rollback_migration_import`, `export_data` | [`state_verify.py`](../src/unified_agent/tools/state_verify.py), [`export.py`](../src/unified_agent/tools/export.py) |

## Plans, delegation, and execution

| Family | Tools | Source |
|---|---|---|
| Plans | `plan_create`, `plan_update`, `plan_get`, `plan_list`, `plan_checkpoint`, `plan_resume`, `plan_step_retry`, `plan_step_claim`, `plan_handoff`, `plan_delete`, `plan_manage` (`close` marks a superseded plan `abandoned` with a reason and keeps its history; `step_add` / `step_edit` / `step_remove` / `step_reorder` / `edit` change an active plan's structure and title (#2480); `preview` / `review` → [plan-preview-widget.md](plan-preview-widget.md)) | [`plan.py`](../src/unified_agent/tools/plan.py) |
| Delegation | `delegate`, `delegate_list`, `delegate_claim`, `delegate_release`, `delegation_ledger`, `delegation_manage`, `dispatch_report_self`, `dispatch_delivery_receipt`, `delegation_spec_check`, `delegation_spec_template`, `delegation_spec_build` (assemble and validate a Delegation Spec from structured arguments), `dispatch_cancel` (cancel the caller's live dispatch run), `dispatch_resume` (resume a cancelled or retryable run), `dispatch_retry_cancellation` (retry termination of an unconfirmed cancellation), `dispatch_confirm_cancellation` (confirm a cancellation after an operator verified the target is gone), `dispatch_delivery_receipts_list` (read-only delivery-receipt summaries of recent runs), `check_agent_capability`, `capability_catalog`, `message_send`, `message_list` (per-recipient mailbox over typed messages: cursor, unread, reply tracking; `to_session` and `session_id` address one session of an agent; read state changes through the `coordination_manage` actions `message_inbox` and `message_ack`) | [`delegate.py`](../src/unified_agent/tools/delegate.py), [`dispatch_report.py`](../src/unified_agent/tools/dispatch_report.py), [`delegation_spec.py`](../src/unified_agent/tools/delegation_spec.py), [`capability.py`](../src/unified_agent/tools/capability.py), [`messages.py`](../src/unified_agent/tools/messages.py) |
| Cycles and workflow | `autonomous_cycle_start`, `autonomous_cycle_get`, `autonomous_cycle_transition`, `autonomous_cycle_record_usage`, `autonomous_cycle_list`, `autonomous_cycle_kill`, `autonomous_cycle_manage`, `bug_worker_manage`, `pi_run_task`, `workflow_list`, `workflow_run` | [`cycles.py`](../src/unified_agent/tools/cycles.py), [`bug_worker.py`](../src/unified_agent/tools/bug_worker.py), [`pi_runner.py`](../src/unified_agent/tools/pi_runner.py), [`workflow.py`](../src/unified_agent/tools/workflow.py) |
| Approvals and review | `approval_pending`, `approval_resolve`, `scan_for_reviewable_tasks`, `review_and_notify`, `autonomous_cycle_green_path_eligibility` | [`approvals.py`](../src/unified_agent/tools/approvals.py), [`reviewer.py`](../src/unified_agent/reviewer.py) |
| Policy authorizations | `policy_authorization_record` (a `UA_POLICY_AUTHORIZERS` member cites a standing policy for one exact PR head, under its verified identity; pre-merge, the task must be live with a branch), `policy_authorization_list` (recorded authorizations for a PR, newest first, each row's current `valid`; `valid_for_head` answers the exact-head query; never returns signatures) | [`policy_authorization.py`](../src/unified_agent/tools/policy_authorization.py), [`github_app.py`](../src/unified_agent/github_app.py), [connector-activation.md](connector-activation.md#policy-authorized-merges-task-1407) |
| Config revisions | `config_revision_manage` (list, diff, show, capture, commit, rollback of policy, profile overlay, approval rules, and org charter) | [`config_revisions.py`](../src/unified_agent/tools/config_revisions.py), [config-revisions.md](config-revisions.md) |
| MCP gateway | `mcp_gateway` (lists, searches and calls tools on configured downstream MCP servers with hub-held credentials: gbrain, codebase-memory; a write needs human approval; off unless `UA_MCP_GATEWAY=1`; steward and chief-of-staff only) | [`mcp_gateway.py`](../src/unified_agent/tools/mcp_gateway.py), [reference](tools-reference.md#mcp-gateway-toolsmcp_gatewaypy) |
| Horizon 2 connectors | `horizon2_server_list`, `horizon2_server_register`, `horizon2_server_revoke`, `horizon2_connector_authorize`, `horizon2_package_list`, `horizon2_package_register` | [`horizon2.py`](../src/unified_agent/tools/horizon2.py) |
| Deployment bootstrap | `deployment_claim` | [`token_lifecycle.py`](../src/unified_agent/tools/token_lifecycle.py), [claim reference](tools-reference.md) |
| Credential pool | `credential_pool_register`, `credential_pool_list`, `credential_pool_revoke`, `model_quota_status` | [`credential_pool.py`](../src/unified_agent/credential_pool.py), [contract](credential-pool.md) |
| Routines | `routine_add` (register an inert routine; cron ticks need `UA_ROUTINES_ENABLED=1`), `routine_list` (`recent_runs=N` adds each routine's receipts and typed skip reasons), `routine_pause`, `routine_fire` (fire an API/webhook routine with the caller's stable event id) | [`routines.py`](../src/unified_agent/routines.py) |
| Crew sessions | `crew_view` (live crew status and recent messages for an active session), `crew_intervene` (send a typed intervention to that one session) | [`crew.py`](../src/unified_agent/crew.py) |
| Self-organization | `org_proposal` (propose, list, get, review, apply, reject organizational changes, and read or set the self-organization toggle; apply needs the human approval gate) | [`self_organization.py`](../src/unified_agent/self_organization.py) |
| Work products | `work_product_submit` (submit a document or file for named-reviewer approval and evidence-backed release) | [`work_product.py`](../src/unified_agent/tools/work_product.py) |
| LangGraph | `langgraph_run`, `langgraph_progress_note`, `langgraph_workflow_run`, `langgraph_checkpoints` | [`langgraph.py`](../src/unified_agent/tools/langgraph.py) |

## Vault, operations, and integrations

| Family | Tools | Source |
|---|---|---|
| Vault | `vault_read`, `vault_write`, `vault_move`, `vault_archive_plan`, `vault_list`, `vault_search`, `vault_links`, `vault_journal`, `vault_briefing_digest`, `vault_ingest_status`, `vault_wiki_custodian`, `vault_query`, `vault_to_memory_ingest` | [`vault.py`](../src/unified_agent/tools/vault.py), [`vault_graph.py`](../src/unified_agent/tools/vault_graph.py), [`vault_ingest.py`](../src/unified_agent/tools/vault_ingest.py) |
| Obsidian runtime | `obsidian_status`, `obsidian_open`, `obsidian_active_note`, `obsidian_search`, `obsidian_append` | [`obsidian.py`](../src/unified_agent/tools/obsidian.py) |
| Profiles and agent registry | `profile_activate`, `profile_reset`, `profile_status`, `agent_heartbeat`, `agent_list`, `agent_context` (peers with their live sessions), `session_list` (`live` flag, `live_only`), `session_start` (idempotent on agent and external session id), `session_end`, `propose_session_learnings`, `propose_skill_clusters`, `session_packet`, `lifecycle_record` | [`profiles.py`](../src/unified_agent/tools/profiles.py), [`registry.py`](../src/unified_agent/tools/registry.py), [`lifecycle.py`](../src/unified_agent/tools/lifecycle.py), [`learning.py`](../src/unified_agent/tools/learning.py), [`evolve.py`](../src/unified_agent/tools/evolve.py) |
| Observability | `agent_activity`, `metrics_snapshot`, `check_anomalies`, `check_anomalies_and_notify`, `perf_summary`, `observability_manage`, `mcp_health_telemetry` (read-only MCP session health, protocol negotiation and tool-schema fingerprint), `event_log_query`, `event_log_verify`, `system_doctor` | [`monitor.py`](../src/unified_agent/monitor.py), [`events.py`](../src/unified_agent/tools/events.py), [`doctor.py`](../src/unified_agent/tools/doctor.py) |
| Notifications and projection | `notify`, `batch_notify`, `notify_delivery_status` (read-only recent outbound notification outcomes), `flush_digest`, `format_render`, `browser_derive_task_reference` | [`notify.py`](../src/unified_agent/tools/notify.py), [`digest.py`](../src/unified_agent/tools/digest.py), [`format.py`](../src/unified_agent/tools/format.py), [`projection.py`](../src/unified_agent/tools/projection.py) |
| Zulip coordination | `zulip_send_message`, `zulip_fetch_messages` | [`zulip.py`](../src/unified_agent/tools/zulip.py) |
| Public-reply relay | `relay_manage` | [`public_relay.py`](../src/unified_agent/tools/public_relay.py) |
| Secrets and agent tokens | `managed_secret_set` (store a provider secret; only digest metadata reaches the approval queue), `agent_token_issue` (issue a managed agent token that authenticates from the next request; returns metadata, never the raw value), `agent_token_rotate` (replace an agent's managed token and record the rotation in the audit trail; `UA_AGENT_TOKENS` entries are not touched), `agent_token_list` (token metadata rows, hashes only), `oauth_device_manage` (list signed-in device sessions or revoke one; revoke is approval-gated) | [`profiles.py`](../src/unified_agent/tools/profiles.py), [`token_lifecycle.py`](../src/unified_agent/tools/token_lifecycle.py), [`oauth_devices.py`](../src/unified_agent/tools/oauth_devices.py) |
| Cloud operations | `gcloud_inventory` (read-only GCP project resource inventory), `gcloud_ops` (bounded GCP operations; start and stop are approval-gated), `cloud_worker_recover` (detect a stopped private worker and, with approval, start it; `dry_run` by default) | [`gcloud.py`](../src/unified_agent/tools/gcloud.py), [`cloud_recovery.py`](../src/unified_agent/tools/cloud_recovery.py) |

## Profile note

Stdio clients can discover every registered tool. An authenticated HTTP client
receives a profile-filtered tool list. Developer and steward use deny lists;
research uses an allow list (`RESEARCH_ALLOWED_TOOLS`). Steward can call
`task_update` but not `task_claim`; another agent's `claimed_by` is refused
inside the tool. The profile rules are in
[`profiles.py`](../src/unified_agent/profiles.py). Consolidated tools such as
`plan_manage` and `observability_manage` support HTTP profile clients. Legacy
tools remain available for compatible stdio clients. See
[Architecture](architecture.md#operational-profiles).

## Disposable acceptance

`synthetic_workflow` links a governed task note, exact approval consumption, a
local synthetic effect and durable evidence with explicitly unknown cost. It is
withheld from the default developer profile and disabled unless an operator
marks the data home disposable. See [the contract](tools-reference.md#synthetic_workflowtask_id-run_id-client_key-label-recovery_fencenone).
