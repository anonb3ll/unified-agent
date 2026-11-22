# Unified-Agent capability catalog and ledger

> Rendered view. The authoritative structured artifact is [`catalog.json`](./catalog.json); edit that file and re-run `python scripts/validate_capability_catalog.py --render`. This view is generated and must not be hand-edited.

- Freshness date (as_of): **2026-09-22**
- Source baseline SHA: `7952f3e04ab8e0efc1aa25c91ac2784a7f24f3df`
- origin/main SHA: `7952f3e04ab8e0efc1aa25c91ac2784a7f24f3df`
- Live hub checked at: `2026-09-22T22:24:19Z`
- Items: **55** across 10 families

## Classification counts

| Classification | Count |
|---|---|
| implementation_state: dormant/uncertain | 2 |
| implementation_state: partially-wired | 8 |
| implementation_state: shipped-and-used | 43 |
| implementation_state: shipped-but-unconsumed | 2 |
| connection_state: dormant | 2 |
| connection_state: live | 34 |
| connection_state: not-applicable | 4 |
| connection_state: unknown | 15 |
| wedge: evidence-wedge | 35 |
| wedge: expansion-slice | 20 |

## Verification limits

- 2026-09-22 freshness update is limited to prq-subscription-pool: an authenticated hub doctor recheck reported subscription_pool:up and an independent unauthenticated tailnet request returned 401. Other catalog items retain their dated evidence and the 2026-09-21 source baseline; they were not reverified by this update.
- Hub registration, hub-side profile filtering, and client-side allowlists are three distinct layers, measured separately on 2026-09-19: the Mac hub registers 177 MCP tools (capability_catalog.mcp_tools); an authenticated developer-profile session's tools/list serves 127 of them (hub-side deny-list plus hidden consolidated legacy aliases — e.g. notify is registered but not developer-visible); the agent-hermes chief-of-staff client additionally applies its own tools.include allowlist (~/.hermes/config.yaml), measured 2026-09-18 at 120 entries (175 hub-served / 120 client-loadable / 55 filtered) and widened under task #1120 to 159 with a 16-tool deliberate withhold.
- Correction (task #1124): the 2026-09-17 limit claiming the connected MCP exposure 'did not register' system_doctor, mcp_health_telemetry, evidence_report, observability_manage, or model_quota_status recorded the client's allowlist as a hub capability limit. All five are hub-registered; they were absent only from that client's tools.include list. A client- or profile-level filter is a surface limit, never hub non-registration.
- The 2026-09-19 live-verify pass (task #1122, agent-devin/developer via the Mac hub) re-probed the 12 shipped-and-used items then at connection_state=unknown: seven verified live with dated evidence, four remain unknown-with-reason (metered provider legs or operator-only delivery not exercisable read-only), and run-cross-harness-boundaries is verified on the local-harness leg only — its remote/multi-harness launch still needs an operator.
- Doctor-derived LIVE/DORMANT facts not re-measured on 2026-09-19 still cite the 2026-09-17 05:47 UTC snapshot from capture #1057 (task #1057).
- #993 (blocked), #1024 (open), and #1063 (done) are all present in the live queue as of 2026-09-19 — the 2026-09-17 'absent' finding pre-dated #1063 by hours and missed #993/#1024 entirely; their queue_coverage rows now carry measured live statuses.
- No provider-leg (ollama/ai_studio/openrouter), device (mobile/Apple Watch), cloud worker, or SDK v0.3 runtime call was freshly exercised; the 2026-09-19 pass held the no-metered-spend boundary, so those items carry connection_state unknown rather than live.
- Credential and quota data (credential_pool.model_quota_status) is last-known/unknown by design; this catalog records that honestly instead of inferring a live state from code presence.
- docs/README.md states 177 registered tools at baseline 0e522ab, matching the 177 entries capability_catalog.mcp_tools returned live on 2026-09-19.
- Web UI nav surface area (since the Control Center v2 cutover, task #2479: packages/web/src/app/routes.ts, eight destinations; the classic packages/web/src/shell/nav.ts is deleted) was confirmed to exist but its exact route count was not independently re-counted for this catalog; the item cites the file, not a specific number.

## Update instructions

1. Read task #1061, #1064, the four 2026-09-17 evidence captures (UA-Integration-Inventory, UA-Cross-System-Seam-Audit, UA-UI-Discoverability-Evidence-Audit, UA-Integration-Closure-Synthesis), current docs/README.md and docs/tool-catalog.md, and a live task_list before editing.
2. Recheck origin/main SHA (git rev-parse origin/main) against source_baseline_sha and update both freshness fields if they diverge.
3. Run whatever live read-only probes are available through the connected MCP exposure and record connection_state strictly from what those probes actually returned this session, never from code or config alone.
4. Measure the tool surface per layer: hub registration (capability_catalog.mcp_tools or docs/tool-catalog.md), the calling profile's tools/list, and any client-side allowlist (e.g. ~/.hermes/config.yaml tools.include). A tool missing from a client or profile list is a surface filter — record it as such, never as hub non-registration.
5. Edit only docs/capability-catalog/catalog.json.
6. Run `python scripts/validate_capability_catalog.py --render` from the repo root and iterate until it exits 0.
7. Update the item/family counts in docs/capability-catalog/README.md if totals changed (create that README first if this is the first pass and it does not yet exist).
8. Before filing a new queue_coverage entry, search the live task queue to confirm the capability is not already tracked; do not file duplicate tasks.

## Governance & evidence (`governance-evidence`)

_Pilot family (first-slice deep coverage)._

Identity, operational profiles, authorization/policy gates, human approval, evidence reports and exports, the audit hash chain, and review/merge receipts that make agent action accountable.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `gov-identity-profiles` | Operational identity and profile activation | shipped-and-used | live | `profile_status` | operator (profile_status call) and dispatch routing (profile-scoped tool allowlists) | evidence-wedge |
| `gov-authorization-policy` | Sandbox and production-action policy gates | shipped-and-used | live | `src/unified_agent/safety.py` | every fs_write/run_cmd/git_* call and gated production tools (e.g. gcloud_ops) | evidence-wedge |
| `gov-approval-queue` | Human approval queue and elicitation | shipped-and-used | live | `approval_pending` | human operator (approval_resolve) and the dispatch coordinator (blocks on pending approvals) | evidence-wedge |
| `gov-evidence-reports` | Evidence report generation and Evidence UI view | shipped-and-used | live | `evidence_report` | operator via the Web UI Evidence view | evidence-wedge |
| `gov-audit-ledger` | Append-only event log and audit hash chain | shipped-and-used | live | `event_log_query` | webui SSE event stream and event_log_verify callers (operators/CI) | evidence-wedge |
| `gov-review-merge-receipts` | Review pipeline, merge gate, and dispatch delivery receipts | shipped-and-used | live | `dispatch_delivery_receipt` | review pipeline timer and the claiming agent's merge step | evidence-wedge |
| `gov-security-audit-tooling` | Security audit, injection triage, and red-team QA tooling | shipped-and-used | live | `security_audit` | developer agents and CI | evidence-wedge |

### Detail

#### `gov-identity-profiles` — Operational identity and profile activation

- **Customer purpose:** Lets an operator see and switch which governed operating profile an agent is running under, so a fleet of agents stays auditable rather than anonymous.
- **Origin:** source — src/unified_agent/profiles.py, src/unified_agent/tools/profiles.py, src/unified_agent/tools/registry.py
- **Implementation state:** shipped-and-used
- **Consumer:** operator (profile_status call) and dispatch routing (profile-scoped tool allowlists)
- **Entry point:** profile_status (`mcp-tool`) — profile_activate/profile_reset/profile_status in tools/profiles.py; agent_heartbeat/agent_list/agent_context in tools/registry.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/profiles.py:8,25,33 — profile_activate, profile_reset, profile_status defined and registered.
  - `test` — tests/test_profiles.py — Covers profile activation/reset/status behavior.
  - `live` (2026-09-17) — profile_status — profile_status returned chief-of-staff/agent-hermes via the connected hub MCP.
- **Boundaries:**
  - security: profile switches only change the operational profile's allowed-tool set (operational_profiles.allowed_tools); they never bypass safety.py sandbox checks.
  - identity: agent_heartbeat/agent_list/agent_context stamp and read the calling agent's label; identity is asserted by the caller, not cryptographically verified.
  - approval: profile activation itself is not approval-gated; downstream tools it unlocks may be (see gov-approval-queue).
  - privacy: not-applicable
  - release: shipped on main; no edition gate found in profiles.py.
  - tenant_workspace: profile state is process-local to the calling agent session, not shared across workspaces.
- **Wedge:** evidence-wedge

#### `gov-authorization-policy` — Sandbox and production-action policy gates

- **Customer purpose:** Keeps agent file writes, shell commands, and production-affecting actions inside declared boundaries, so a mistake or a bad instruction can't reach outside the sandbox.
- **Origin:** source — src/unified_agent/safety.py, src/unified_agent/policy.py
- **Implementation state:** shipped-and-used
- **Consumer:** every fs_write/run_cmd/git_* call and gated production tools (e.g. gcloud_ops)
- **Entry point:** src/unified_agent/safety.py (`library`) — Enforced in-process ahead of fs_*/run_cmd/git_* tool bodies; not a callable MCP tool itself.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/safety.py, src/unified_agent/policy.py — Workspace/vault sandbox resolution and prod-action gating.
  - `test` — tests/test_policy.py, tests/test_prod_action_policy.py, tests/test_command_gate.py, tests/test_trust_boundary.py — Policy, command-gate, and trust-boundary coverage.
  - `live` (2026-09-17) — hub auth — Doctor snapshot #1057 recorded hub auth LIVE with 20 registered bearer tokens.
- **Boundaries:**
  - security: fs_write/run_cmd resolve only inside UA_WORKSPACE union an active task worktree (resolve_in_workspace); no flag widens this to a full product checkout.
  - identity: bearer-token-scoped per agent; the doctor snapshot counted 20 issued tokens.
  - approval: specific destructive/production tools additionally require the approval queue (see gov-approval-queue).
  - privacy: secret-bearing filenames (.env, id_rsa, *.pem, *credentials*, *secret*, *token*) are refused at name-component boundaries.
  - release: core to every edition; not feature-gated.
  - tenant_workspace: one sandbox root per agent (config.py workspace_root/agent_workspaces); cross-repo read is widened (task #497) but write/execute stay workspace-scoped.
- **Wedge:** evidence-wedge

#### `gov-approval-queue` — Human approval queue and elicitation

- **Customer purpose:** Forces specific high-risk tool calls to wait for an explicit human yes/no before they execute, so no agent can unilaterally take a destructive or production action.
- **Origin:** source — src/unified_agent/tools/approvals.py, src/unified_agent/elicitation.py
- **Implementation state:** shipped-and-used
- **Consumer:** human operator (approval_resolve) and the dispatch coordinator (blocks on pending approvals)
- **Entry point:** approval_pending (`mcp-tool`) — approval_pending/approval_resolve; gated tool set controlled by UA_APPROVAL_TOOLS.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/approvals.py, src/unified_agent/elicitation.py — Approval queue storage and elicitation prompt flow.
  - `test` — tests/test_approvals.py, tests/test_elicitation.py — Approval request/resolve and elicitation coverage.
  - `live` (2026-09-17) — approval gates — Doctor snapshot #1057 recorded approval gates LIVE for delegate and gcloud_ops.
- **Boundaries:**
  - security: gated tools cannot execute their side effect until a resolve call records an explicit decision.
  - identity: resolver identity is recorded on the approval record.
  - approval: this is the approval mechanism itself; #1058 records the decision to keep human approval as the only resolver.
  - privacy: not-applicable
  - release: core; UA_APPROVAL_TOOLS is an opt-in allowlist of which tools are gated.
  - tenant_workspace: approval records are process/hub-scoped, not per-workspace.
- **Wedge:** evidence-wedge

#### `gov-evidence-reports` — Evidence report generation and Evidence UI view

- **Customer purpose:** Produces an exportable, reviewable evidence packet an operator can hand to a stakeholder or auditor showing what an agent actually did.
- **Origin:** source — src/unified_agent/tools/evidence.py, docs/evidence-report.md
- **Implementation state:** shipped-and-used
- **Consumer:** operator via the Web UI Evidence view
- **Entry point:** evidence_report (`mcp-tool`) — tools/evidence.py; rendered in the Web UI Evidence view.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/evidence.py — evidence_report tool implementation.
  - `test` — tests/test_evidence_report.py — Evidence report generation coverage.
  - `live` (2026-09-17) — web UI — Doctor snapshot #1057 recorded the web UI LIVE (56 routes, static assets), of which Evidence is one view.
- **Boundaries:**
  - security: read-only report generation; no state mutation.
  - identity: reports are attributable to the querying agent/task scope requested.
  - approval: not-applicable — generation itself is not approval-gated.
  - privacy: report content is scoped to task/evidence data already stored; no external egress.
  - release: gov-evidence-reports is edition-gated; require_feature('evidence_reporting') is checked before generation, so it is not available on every edition.
  - tenant_workspace: scoped to the calling workspace's task/event data.
- **Wedge:** evidence-wedge

#### `gov-audit-ledger` — Append-only event log and audit hash chain

- **Customer purpose:** Gives a tamper-evident record of what happened and when, so a dispute about agent behavior can be settled by checking a verifiable chain instead of trusting a log file.
- **Origin:** source — src/unified_agent/events.py, src/unified_agent/tools/events.py
- **Implementation state:** shipped-and-used
- **Consumer:** webui SSE event stream and event_log_verify callers (operators/CI)
- **Entry point:** event_log_query (`mcp-tool`) — event_log_query/event_log_verify in tools/events.py, backed by events.py's hash chain.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/events.py:15,53 — event_log_query and event_log_verify definitions.
  - `test` — tests/test_audit_ledger_chain.py, tests/test_events.py — Hash chain and event log coverage.
  - `live` (2026-09-17) — event bus — Doctor snapshot #1057 recorded the event bus LIVE.
- **Boundaries:**
  - security: hash chain makes retroactive tampering detectable via event_log_verify.
  - identity: each event row is attributed to an actor/agent.
  - approval: not-applicable to read/verify path.
  - privacy: event payloads may include task content; no external egress.
  - release: core, not edition-gated.
  - tenant_workspace: event stream is hub-scoped, shared across connected agents.
- **Wedge:** evidence-wedge

#### `gov-review-merge-receipts` — Review pipeline, merge gate, and dispatch delivery receipts

- **Customer purpose:** Confirms a piece of agent work was actually reviewed, merged, and delivered — with a receipt — rather than just claimed done in a chat message.
- **Origin:** pr — #553
- **Implementation state:** shipped-and-used
- **Consumer:** review pipeline timer and the claiming agent's merge step
- **Entry point:** dispatch_delivery_receipt (`mcp-tool`) — reviewer.py scan_for_reviewable_tasks/review_and_notify; tools/dispatch_report.py dispatch_report_self/dispatch_cancel/dispatch_resume/dispatch_delivery_receipt (#553); delivery.py; merge.py; delegation_ledger_log.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/reviewer.py:760,1851 — scan_for_reviewable_tasks and review_and_notify.
  - `code` — src/unified_agent/tools/dispatch_report.py:36,106,123,141 — dispatch_report_self/dispatch_cancel/dispatch_resume/dispatch_delivery_receipt added in PR #553.
  - `test` — tests/test_delivery.py, tests/test_delivery_receipt.py, tests/test_reviewer_pipeline.py, tests/test_delegation_ledger_log.py — Delivery, receipt, and review-pipeline coverage.
  - `live` (2026-09-17) — review pipeline timer — Doctor snapshot #1057 recorded the review pipeline / dispatch tick timers LIVE.
- **Boundaries:**
  - security: merge.py only merges tasks that pass the reviewer's test-and-audit gate.
  - identity: receipts are attributed to the delivering run_id/agent.
  - approval: review pipeline surfaces but never merges; the claiming agent merges its own PR on exact-head green CI plus one independent review with no unresolved blocking finding, review threads resolved (the operator, 2026-09-23; see docs/automation.md); red-tier diffs stay with the operator.
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: operates over the shared task queue, not a single workspace.
- **Wedge:** evidence-wedge

#### `gov-security-audit-tooling` — Security audit, injection triage, and red-team QA tooling

- **Customer purpose:** Gives a developer or CI a repeatable way to scan agent-authored code for injection risk and unsafe patterns before it ships.
- **Origin:** source — src/unified_agent/tools/audit.py, src/unified_agent/tools/skills.py, src/unified_agent/tools/red_team.py, .ua-audit-ignore
- **Implementation state:** shipped-and-used
- **Consumer:** developer agents and CI
- **Entry point:** security_audit (`mcp-tool`) — security_audit/injection_triage/audit_triage_tasks in tools/audit.py; skill_review in tools/skills.py; red_team_qa in tools/red_team.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/audit.py:48,122,181 — security_audit, injection_triage, audit_triage_tasks definitions.
  - `code` — src/unified_agent/tools/skills.py:338, src/unified_agent/tools/red_team.py:18 — skill_review and red_team_qa definitions.
  - `test` — tests/test_audit.py, tests/test_audit_ast.py, tests/test_audit_surfaces.py, tests/test_skill_review.py, tests/test_red_team.py — Audit, skill-review, and red-team coverage.
  - `live` (2026-09-19) — security_audit + injection_triage via Mac hub — 2026-09-19 12:20Z as agent-devin/developer: security_audit(path=projects/COLLABORATION.md) resolved on the hub host and returned status=pass grade=B (1 info finding); injection_triage returned scored verdicts (score 0.15, band=review-later). Doc-scan and scoring path live (task #1122).
- **Boundaries:**
  - security: read-only static analysis over workspace or UA_AUDIT_ROOTS; does not modify code.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: findings may reference source content; no external egress.
  - release: core, run in pre-commit gate and CI per repo CLAUDE.md.
  - tenant_workspace: resolve_for_audit widens through UA_AUDIT_ROOTS, separate from the project-checkout read-roots widening.
- **Wedge:** evidence-wedge

## Dispatch & delegation (`dispatch-delegation`)

_Pilot family (first-slice deep coverage)._

Task lifecycle, claims, dependencies, plans, agent-to-agent delegation, the dispatch scheduler and executor boundary, autonomous cycles, and the kill switch that runs and bounds unattended agent work.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `dsp-task-lifecycle` | Task lifecycle, claims, and worktree/lane management | shipped-and-used | live | `task_claim` | every registered agent and the Web UI task/board views | evidence-wedge |
| `dsp-dependencies-lineage` | Task dependencies, coordination, and handoff lineage | shipped-and-used | live | `dependency_add` | dispatch scheduler (dependency-aware routing) and agents following a handoff chain | evidence-wedge |
| `dsp-plans` | Plan create/update/checkpoint/resume and step claiming | shipped-and-used | live | `plan_create` | review pipeline's plan-approval step and plan-following agents | evidence-wedge |
| `dsp-delegation` | Agent-to-agent delegation with an audit ledger | shipped-and-used | live | `delegate` | delegating and receiving agents; delegation_ledger read by operators | evidence-wedge |
| `dsp-scheduler-running` | Dispatch scheduler, coordinator, and worker launch | shipped-and-used | live | `Web UI Dispatch/Board` | Web UI Dispatch/Board and dispatch-eligible agents | evidence-wedge |
| `dsp-executor-boundary` | Executor backends and daemon boundary | partially-wired | unknown | `src/unified_agent/executor/daemon.py` | dispatch scheduler (routes execution through the executor when configured) | evidence-wedge |
| `dsp-autonomous-cycles` | Autonomous cycle lifecycle and kill switch | partially-wired | unknown | `autonomous_cycle_start` | operator (autonomous_cycle_kill) and dispatch supervise.py | evidence-wedge |
| `dsp-capability-checks` | Dispatch capability checks and this catalog's own build tool | shipped-and-used | live | `check_agent_capability` | dispatch routing (DISPATCHABLE_LABELS check before launch) | evidence-wedge |

### Detail

#### `dsp-task-lifecycle` — Task lifecycle, claims, and worktree/lane management

- **Customer purpose:** Lets an operator or agent create, claim, and track a unit of work with a durable status, so nothing is silently dropped or double-worked.
- **Origin:** source — src/unified_agent/tools/tasks.py
- **Implementation state:** shipped-and-used
- **Consumer:** every registered agent and the Web UI task/board views
- **Entry point:** task_claim (`mcp-tool`) — task_add/task_update/task_list/task_delete/task_claim in tools/tasks.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/tasks.py:47 — Worktree/claim lifecycle noted as a repo-vs-non-repo-workspace design point in CLAUDE.md.
  - `test` — tests/test_task_claim.py, tests/test_task_claim_authority.py, tests/test_task_lanes.py — Claim, authority, and lane coverage.
  - `live` (2026-09-17) — task_list — task_list succeeded through the connected hub MCP this session, listing #935, #944, #1032, #1043-1056, #1061, #1064, #1065, #1003, #885.
- **Boundaries:**
  - security: task_claim auto-stamps the calling agent as claimed_by; task_update may clear only the verified caller's claim or another agent's expired claim when release_claim=True.
  - identity: claimed_by is the calling agent's label.
  - approval: not-applicable to ordinary claim/update.
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: a non-repo workspace (e.g. the cockpit) is a supported configuration; task_claim then does not mint a worktree there by design.
- **Wedge:** evidence-wedge

#### `dsp-dependencies-lineage` — Task dependencies, coordination, and handoff lineage

- **Customer purpose:** Tracks which tasks block or depend on others and records handoffs between agents, so multi-step work has a visible chain of custody.
- **Origin:** source — src/unified_agent/tools/graph.py
- **Implementation state:** shipped-and-used
- **Consumer:** dispatch scheduler (dependency-aware routing) and agents following a handoff chain
- **Entry point:** dependency_add (`mcp-tool`) — dependency_add/dependency_list/dependency_remove and coordination_manage/handoff/handoff_list in tools/graph.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/graph.py — Dependency, coordination, and handoff tooling.
  - `test` — tests/test_dependency_bounds.py — Dependency bounds coverage.
  - `live` (2026-09-17) — task_list lineage fields — Live task_list output on 2026-09-17 included dependency/lineage-shaped fields (e.g. #1032 blocked on #743), but a fresh handoff round-trip was not separately exercised this session.
  - `live` (2026-09-19) — coordination_manage + delegation_manage via Mac hub — 2026-09-19 12:20Z: handoff_list returned real rows (id 62 task #1176 Hermes→Cursor pending; id 61 failed after 5 attempts), dependency_list returned 5 edges, and the delegation ledger rendered agent_stats/failure_pairs/outcomes_awaiting_review (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: handoff records the sending and receiving agent.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: shared task graph, not per-workspace.
- **Wedge:** evidence-wedge

#### `dsp-plans` — Plan create/update/checkpoint/resume and step claiming

- **Customer purpose:** Gives an agent a durable, resumable plan with checkpoints, so a long multi-step task can survive a restart or handoff without losing progress.
- **Origin:** source — src/unified_agent/tools/plan.py
- **Implementation state:** shipped-and-used
- **Consumer:** review pipeline's plan-approval step and plan-following agents
- **Entry point:** plan_create (`mcp-tool`) — plan_create/plan_update/plan_get/plan_list/plan_checkpoint/plan_resume/plan_step_claim/plan_step_retry/plan_handoff/plan_delete; plan_manage preview/review.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/plan.py:134,186,247,276,303,357,397,465,541,640 — Full plan lifecycle function set.
  - `test` — tests/test_plan.py, tests/test_plan_preview.py — Plan lifecycle and preview coverage.
  - `live` (2026-09-19) — plan_manage lifecycle via Mac hub — 2026-09-19 12:20-12:24Z: plan_manage create→checkpoint→resume→delete on plan 65f526f84353 — checkpoint seq=1 stored, resume returned plan+steps+next_step_idx+checkpoint context, delete confirmed, and a follow-up get correctly errors 'no plan with id' (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: plan_step_claim/plan_handoff record the claiming/handing-off agent.
  - approval: not-applicable to creation; plan review can gate merge (see gov-review-merge-receipts).
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: plans are scoped by the calling agent's task/workspace context.
- **Wedge:** evidence-wedge

#### `dsp-delegation` — Agent-to-agent delegation with an audit ledger

- **Customer purpose:** Lets one agent hand a task to another named agent with a recorded, auditable trail, so delegation isn't an untracked side chat.
- **Origin:** source — src/unified_agent/tools/delegate.py, src/unified_agent/tools/delegation_spec.py, src/unified_agent/delegation_ledger_log.py
- **Implementation state:** shipped-and-used
- **Consumer:** delegating and receiving agents; delegation_ledger read by operators
- **Entry point:** delegate (`mcp-tool`) — delegate/delegate_list/delegation_manage/delegate_claim/delegate_release/delegate_cancel/delegate_fail/delegate_complete/delegate_retry/delegate_get/delegation_ledger in tools/delegate.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/delegate.py:103,291,410,534,576,761 — delegate, delegate_list, delegation_ledger, delegate_claim, delegate_release, delegation_manage.
  - `test` — tests/test_delegate.py, tests/test_delegation_spec.py, tests/test_delegation_ledger_log.py — Delegation and ledger coverage.
  - `live` (2026-09-17) — approval gate — Doctor snapshot #1057 recorded the approval gate LIVE for the delegate tool specifically.
- **Boundaries:**
  - security: delegate is one of the tools gated by the approval queue per doctor snapshot #1057.
  - identity: each ledger row records delegator and delegate agent labels.
  - approval: gated (see above); resolved only by a human via approval_resolve.
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: delegation ledger is shared/hub-scoped.
- **Wedge:** evidence-wedge

#### `dsp-scheduler-running` — Dispatch scheduler, coordinator, and worker launch

- **Customer purpose:** Runs approved agent work unattended on a schedule with bounded concurrency and retries, so agents don't need a human to manually kick off every run.
- **Origin:** source — src/unified_agent/dispatch/scheduler.py, src/unified_agent/dispatch/coordinator.py, src/unified_agent/dispatch/worker.py, src/unified_agent/dispatch/routing.py, src/unified_agent/dispatch/launch.py, src/unified_agent/dispatch/supervise.py, src/unified_agent/dispatch/claude_sandbox.py, src/unified_agent/dispatch/child_env.py
- **Implementation state:** shipped-and-used
- **Consumer:** Web UI Dispatch/Board and dispatch-eligible agents
- **Entry point:** Web UI Dispatch/Board (`web-ui`) — Scheduler runs server-side; surfaced to operators via the Web UI dispatch views.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/dispatch/scheduler.py, coordinator.py, worker.py, routing.py, launch.py, supervise.py, claude_sandbox.py, child_env.py — Scheduler, coordinator, routing, launch, and sandboxed child-process pipeline.
  - `test` — tests/test_dispatch_scheduler.py, tests/test_dispatch_coordinator.py, tests/test_dispatch_routing.py, tests/test_dispatch_sandbox.py, tests/test_dispatch_e2e_smoke.py — Scheduler, coordinator, routing, sandbox, and e2e smoke coverage.
  - `live` (2026-09-17) — dispatch scheduler — Doctor snapshot #1057 recorded the dispatch scheduler LIVE (batch 10, max 3 attempts, max 2 live runs).
- **Boundaries:**
  - security: claude_sandbox.py/child_env.py bound what a dispatched child process can see/do; RoutedLaunchBoundary in launch.py enforces routing constraints.
  - identity: each dispatch run is attributed to a routed agent/profile.
  - approval: specific dispatch actions (e.g. delegate, gcloud_ops) route through the approval queue; the scheduler itself is not gated.
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: each dispatched run gets its own worktree/child env, not a shared write root.
- **Wedge:** evidence-wedge

#### `dsp-executor-boundary` — Executor backends and daemon boundary

- **Customer purpose:** Provides a separate-process execution boundary for dispatched work so a failing or malicious task run can't take down the hub process it was launched from.
- **Origin:** source — src/unified_agent/executor/backends.py, src/unified_agent/executor/daemon.py, src/unified_agent/executor/evidence.py
- **Implementation state:** partially-wired
- **Consumer:** dispatch scheduler (routes execution through the executor when configured)
- **Entry point:** src/unified_agent/executor/daemon.py (`not-applicable`) — Internal execution boundary, not a directly callable MCP tool.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/executor/backends.py, daemon.py, evidence.py — Executor backend, daemon process, and evidence-capture modules.
  - `test` — tests/test_executor.py, tests/test_executor_backends.py, tests/test_executor_evidence.py, tests/test_dispatch_e2e_smoke.py — Executor backend, daemon, evidence-capture, and dispatch smoke coverage (includes the auth-rejection regression from PR #82).
- **Boundaries:**
  - security: PR #82 (7449c18) fixed an auth rejection degrading into unguarded local execution; the daemon defaults to requiring the hub and only allows the in-process fallback with UA_EXECUTOR_ALLOW_DIRECT=1 (a test/dev convenience, never for production).
  - identity: executor bearer-token wiring was completed under task #173; live separate-process auth proof is still absent, so the doctor classifies the executor UNKNOWN.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core when enabled; daemon defaults to hub-required; direct fallback is an explicit opt-in for standalone/test use.
  - tenant_workspace: runs as a separate process boundary from the hub.
- **Incomplete (owner / decision / next action / non-action):** shared / #1057/#1058 - executor UNKNOWN is an evidence gap, not proof of failure. / run one separate-process executor auth probe and one disposable failed-dispatch/reclaim rehearsal (#1058 R1/R2), recording task id, run id, lease/retry count, event cursor, executor identity, evidence ref, and final task state. / do not set UA_EXECUTOR_ALLOW_DIRECT=1 in production; do not add a second live task state to mask the unknown.
- **Wedge:** evidence-wedge

#### `dsp-autonomous-cycles` — Autonomous cycle lifecycle and kill switch

- **Customer purpose:** Lets an operator start, watch, and forcibly stop a bounded run of unattended agent work, so autonomy always has a manual off switch.
- **Origin:** source — src/unified_agent/tools/cycles.py
- **Implementation state:** partially-wired
- **Consumer:** operator (autonomous_cycle_kill) and dispatch supervise.py
- **Entry point:** autonomous_cycle_start (`mcp-tool`) — autonomous_cycle_start/get/transition/kill/record_usage/list in tools/cycles.py.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/tools/cycles.py:84,181,198,415,496,587 — Cycle start, get, transition, kill, usage recording, and list.
  - `test` — tests/test_autonomous_cycles.py — Autonomous cycle coverage.
- **Boundaries:**
  - security: autonomous_cycle_kill is the hard stop; no separate rate limit observed beyond usage recording.
  - identity: cycle actor is recorded per transition.
  - approval: not-applicable to start/kill; downstream dispatched actions may be gated separately.
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: cycle state is hub-scoped.
- **Incomplete (owner / decision / next action / non-action):** shared / — / run one fresh autonomous_cycle_start -> kill round trip and record it as live evidence. / do not claim live/shipped-and-used from the code and tests existing on main alone.
- **Wedge:** evidence-wedge

#### `dsp-capability-checks` — Dispatch capability checks and this catalog's own build tool

- **Customer purpose:** Checks whether a named agent is actually allowed to run a given task before dispatching it, and (via capability_catalog) gives the fleet a self-describing map of what it can do.
- **Origin:** task — #1065
- **Implementation state:** shipped-and-used
- **Consumer:** dispatch routing (DISPATCHABLE_LABELS check before launch)
- **Entry point:** check_agent_capability (`mcp-tool`) — check_agent_capability/capability_catalog in tools/capability.py; delegation_spec_check/template/build in tools/delegation_spec.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/capability.py:111,232 — check_agent_capability and capability_catalog definitions.
  - `test` — tests/test_capability.py, tests/test_dispatch_contract_spec.py — Capability and dispatch-contract-spec coverage.
  - `live` (2026-09-19) — check_agent_capability via Mac hub — 2026-09-19 12:21Z: check_agent_capability on #476 (annotated [requires: Xcode, simctl, xcodebuild] → capable=true vs agent-devin) and #1122 (required_tools=[] → capable=true); annotation-parse and unrestricted paths both exercised (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: checked against the target agent_label's configured tool allowlist.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core; this catalog (docs/capability-catalog/catalog.json) is itself the artifact task #1065 asked capability_catalog-adjacent tooling to produce.
  - tenant_workspace: not-applicable — pure lookup against configured allowlists.
- **Wedge:** evidence-wedge

## Workroom / multi-harness (`workroom-multi-harness`)

Provider/connector envelopes, Horizon 2 registry and authorization, and the cross-harness execution boundaries that let work be routed to more than one agent harness safely.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `run-horizon2-connectors` | Horizon 2 connector registry and authorization | shipped-but-unconsumed | dormant | `horizon2_server_register` | no verified consumer | expansion-slice |
| `run-envelopes` | Execution envelope / A2A contract version | partially-wired | unknown | `src/unified_agent/horizon2.py` | capability_catalog MCP tool (src/unified_agent/tools/capability.py:243,298) calls mcp_matrix.get_client_matrix() as part of the MCP client-surface report it returns; this is a real, currently-registered, frequently-used tool (re-verified live in this repo on 2026-09-21). The catalog had not recorded this consumer. A real cross-harness envelope *exchange* (not just protocol-compatibility reporting) remains a separate, already-tracked gap under the sibling item run-cross-harness-boundaries (task #1122). | expansion-slice |
| `run-cross-harness-boundaries` | Cross-harness execution launch boundary | shipped-and-used | unknown | `src/unified_agent/dispatch/launch.py` | dispatch launch (local harness) | expansion-slice |

### Detail

#### `run-horizon2-connectors` — Horizon 2 connector registry and authorization

- **Customer purpose:** Registers and authorizes external connector servers so agent work can be routed to another harness under an explicit, revocable grant instead of an ad hoc integration.
- **Origin:** source — src/unified_agent/horizon2.py, src/unified_agent/tools/horizon2.py
- **Implementation state:** shipped-but-unconsumed
- **Consumer:** no verified consumer
- **Entry point:** horizon2_server_register (`mcp-tool`) — horizon2_server_list/register/revoke, connector_authorize, package_list/register in tools/horizon2.py.
- **Connection state:** dormant
- **Evidence:**
  - `code` — src/unified_agent/tools/horizon2.py:85,103,166,202,239,248 — Server list/register/revoke, connector_authorize, package_list/register.
  - `code` — src/unified_agent/horizon2.py — GovernedServer model and CONTRACT_VERSION.
  - `test` — tests/test_horizon2.py, tests/test_horizon2_tools.py — Connector registry and tools coverage.
- **Boundaries:**
  - security: registry operations and connector_authorize are one-shot approval-gated per the governed-connector decision (AGENTS.md task #592); no live exchange evidence exists yet.
  - identity: server registrations are keyed by server_id.
  - approval: connector_authorize is the approval seam; no live exchange evidence exists yet.
  - privacy: connector registration stores server metadata only.
  - release: core module, no edition gate found.
  - tenant_workspace: not-applicable — registry is hub-scoped.
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / pilot exactly one reviewed connector per the #1032/#743 decision chain. / do not roll out broad connector coverage before one pilot connector is proven end to end.
- **Wedge:** expansion-slice

#### `run-envelopes` — Execution envelope / A2A contract version

- **Customer purpose:** Defines a versioned contract for what an execution request and response look like across harness boundaries, so a receiving harness can validate what it's being handed.
- **Origin:** source — src/unified_agent/horizon2.py, src/unified_agent/mcp_live_verification.py, src/unified_agent/mcp_live_state.py, src/unified_agent/mcp_matrix.py, docs/mcp-live-verification.md, docs/mcp-2x-operator-guide.md
- **Implementation state:** partially-wired
- **Consumer:** capability_catalog MCP tool (src/unified_agent/tools/capability.py:243,298) calls mcp_matrix.get_client_matrix() as part of the MCP client-surface report it returns; this is a real, currently-registered, frequently-used tool (re-verified live in this repo on 2026-09-21). The catalog had not recorded this consumer. A real cross-harness envelope *exchange* (not just protocol-compatibility reporting) remains a separate, already-tracked gap under the sibling item run-cross-harness-boundaries (task #1122).
- **Entry point:** src/unified_agent/horizon2.py (`library`) — ua.execution-envelope/v1 contract constant; MCP live verification/state/matrix modules validate protocol compatibility.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/horizon2.py — CONTRACT_VERSION for the execution envelope.
  - `code` — src/unified_agent/mcp_live_verification.py, mcp_live_state.py, mcp_matrix.py — MCP protocol/version verification and matrix modules.
  - `test` — tests/test_mcp2_compatibility.py, tests/test_mcp2_smoke.py — MCP 2.x compatibility and smoke coverage.
  - `code` (2026-09-21) — src/unified_agent/tools/capability.py:243,298 (task #1121 phase 2) — Re-verified this session: capability.py:243 imports mcp_matrix; capability.py:298 calls mcp_matrix.get_client_matrix() and includes its result under 'mcp_client_matrix' in the capability_catalog tool's return payload. Catalog correction only — connection_state stays 'unknown' per the #1067 guard; no dated live round trip was performed beyond confirming the call path exists in source.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core protocol-compatibility layer.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / exercise one cross-harness envelope exchange and record real behavior evidence. / do not assume envelope compatibility from the contract constant existing.
- **Wedge:** expansion-slice

#### `run-cross-harness-boundaries` — Cross-harness execution launch boundary

- **Customer purpose:** Keeps a dispatched run's blast radius bounded to its own sandbox and child environment regardless of which harness launched it.
- **Origin:** source — src/unified_agent/dispatch/launch.py, src/unified_agent/dispatch/claude_sandbox.py, src/unified_agent/dispatch/child_env.py, src/unified_agent/executor/backends.py
- **Implementation state:** shipped-and-used
- **Consumer:** dispatch launch (local harness)
- **Entry point:** src/unified_agent/dispatch/launch.py (`library`) — RoutedLaunchBoundary and sandboxed child-process construction.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/dispatch/launch.py, claude_sandbox.py, child_env.py — Launch boundary and sandboxed child env construction.
  - `test` — tests/test_dispatch_sandbox.py — Sandbox launch coverage.
  - `live` (2026-09-19) — system_doctor + session_list via Mac hub — 2026-09-19 12:20Z: local-harness leg verified — system_doctor reports dispatch_scheduler LIVE ('REAL agent CLI spawn, max 2 live runs') with agent-server-agent2 and agent-server-agent3 executors online, and session_list shows real codex_run sessions with pid+worktree (delegations 53-58 through 09-18). The remote/multi-harness leg was not exercised: a real launch spawns a paid agent run and needs an operator (task #1122).
- **Boundaries:**
  - security: bounds child-process environment and filesystem reach for local dispatch.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: shipped-and-used for local single-harness dispatch; remote/multi-harness use is unproven.
  - tenant_workspace: per-dispatch worktree/child env, not shared.
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / operator-run remote/multi-harness launch through dispatch/launch.py to prove the remote leg. / do not claim the boundary is proven for remote harnesses on the strength of the local-harness tests alone.
- **Wedge:** expansion-slice

## LangGraph planning (`langgraph-planning`)

_Pilot family (first-slice deep coverage)._

Workflow execution and checkpoints, progress notes, graph/canvas inspection, and the plan-to-review lifecycle built on LangGraph.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `lgp-engine-checkpoints` | LangGraph run engine and SQLite checkpoints | shipped-and-used | live | `langgraph_run` | langgraph_workflow_run and Activity > LangGraph Web UI view | evidence-wedge |
| `lgp-governed-workflow-run` | Governed plan-build-verify-merge workflow run | shipped-and-used | live | `langgraph_workflow_run` | dispatch scheduler (langgraph_workflow_dispatch) and reviewer.py merge gate | evidence-wedge |
| `lgp-progress-observability` | LangGraph progress notes and observability | shipped-and-used | live | `langgraph_progress_note` | Web UI Activity > LangGraph view | evidence-wedge |
| `lgp-graph-canvas` | Graph canvas read/write and task-graph inspection | shipped-and-used | live | `graph_canvas_read` | Web UI Task Graph / Canvas views | evidence-wedge |
| `lgp-plan-review-lifecycle` | Plan-to-review lifecycle handoff | shipped-and-used | live | `plan_step_claim` | reviewer.py review pipeline | evidence-wedge |
| `lgp-langsmith-boundary` | LangSmith/export tracing boundary (disabled by decision) | dormant/uncertain | not-applicable | `src/unified_agent/langgraph_observability.py` | no verified consumer | expansion-slice |

### Detail

#### `lgp-engine-checkpoints` — LangGraph run engine and SQLite checkpoints

- **Customer purpose:** Runs a governed multi-step agent workflow with durable checkpoints, so a long workflow can resume after an interruption instead of restarting from scratch.
- **Origin:** source — src/unified_agent/langgraph_pilot.py, src/unified_agent/tools/langgraph.py
- **Implementation state:** shipped-and-used
- **Consumer:** langgraph_workflow_run and Activity > LangGraph Web UI view
- **Entry point:** langgraph_run (`mcp-tool`) — langgraph_run and langgraph_checkpoints in tools/langgraph.py, backed by langgraph_pilot.py's SqliteSaver.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/langgraph_pilot.py, src/unified_agent/tools/langgraph.py — LangGraph engine and checkpoint tool wiring.
  - `test` — tests/test_tools_langgraph.py, tests/test_langgraph_pilot.py — LangGraph run and pilot coverage.
  - `live` (2026-09-17) — LangGraph runtime — Doctor snapshot #1057 recorded LangGraph LIVE, v1.2.11, SQLite checkpoints.
- **Boundaries:**
  - security: runs within the same fs_write/run_cmd sandbox as any other dispatched work.
  - identity: checkpoints are keyed by run/thread id.
  - approval: not-applicable to the engine itself; the workflow it executes may include gated steps.
  - privacy: not-applicable
  - release: core, pilot family.
  - tenant_workspace: checkpoints stored in the hub's SQLite store, not per-workspace.
- **Wedge:** evidence-wedge

#### `lgp-governed-workflow-run` — Governed plan-build-verify-merge workflow run

- **Customer purpose:** Runs the standard plan-then-build-then-verify-then-merge sequence as one governed workflow instead of four separately trusted steps.
- **Origin:** source — src/unified_agent/langgraph_workflow.py, src/unified_agent/langgraph_workflow_dispatch.py
- **Implementation state:** shipped-and-used
- **Consumer:** dispatch scheduler (langgraph_workflow_dispatch) and reviewer.py merge gate
- **Entry point:** langgraph_workflow_run (`mcp-tool`) — langgraph_workflow_run tool; ordering enforced in langgraph_workflow.py/langgraph_workflow_dispatch.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/langgraph_workflow.py, langgraph_workflow_dispatch.py — Plan->build->verify->merge workflow and its dispatch integration.
  - `test` — tests/test_langgraph_workflow.py, tests/test_langgraph_dispatch.py — Workflow and dispatch coverage.
  - `live` (2026-09-19) — langgraph_checkpoints + event_log via Mac hub — 2026-09-19: checkpoint DB holds thread ua-lg:01M2WRQCQVH4RAH925TX3CBXRD stamped 12:02:15Z with 6 checkpoints (multi-node run); event_log shows agent.langgraph_effect.updated at 12:02:15Z — 'prepared: task #1122 task_update' via governed_tool:workflow-build — staged at the merge gate and held, as designed; system_doctor reports langgraph_engine LIVE v1.2.11 (task #1122).
- **Boundaries:**
  - security: same sandbox as dsp-scheduler-running for the steps it dispatches.
  - identity: not-applicable beyond the dispatching agent.
  - approval: merge step still routes through the human merge gate.
  - privacy: not-applicable
  - release: core, pilot family.
  - tenant_workspace: per-dispatch worktree for the build/verify steps.
- **Wedge:** evidence-wedge

#### `lgp-progress-observability` — LangGraph progress notes and observability

- **Customer purpose:** Surfaces in-flight progress on a running LangGraph workflow in the Web UI, so an operator can see what step an agent is on without reading raw logs.
- **Origin:** source — src/unified_agent/langgraph_note.py, src/unified_agent/langgraph_observability.py
- **Implementation state:** shipped-and-used
- **Consumer:** Web UI Activity > LangGraph view
- **Entry point:** langgraph_progress_note (`mcp-tool`) — tools/langgraph.py:252 langgraph_progress_note; langgraph_note.py, langgraph_observability.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/langgraph.py:252 — langgraph_progress_note definition.
  - `test` — tests/test_langgraph_note.py, tests/test_langgraph_note_dispatch.py, tests/test_langgraph_observability.py, tests/test_webui_langgraph.py — Progress-note, dispatch, observability, and Web UI coverage.
  - `live` (2026-09-17) — web UI — Doctor snapshot #1057 recorded the Web UI LIVE (56 routes), including the Activity > LangGraph view.
- **Boundaries:**
  - security: read/append-only progress annotations.
  - identity: notes are attributed to the reporting agent/run.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core, pilot family.
  - tenant_workspace: scoped to the run's task/thread id.
- **Wedge:** evidence-wedge

#### `lgp-graph-canvas` — Graph canvas read/write and task-graph inspection

- **Customer purpose:** Lets an operator visually inspect the task/dependency graph and canvas annotations, without changing task lifecycle state through this surface.
- **Origin:** source — src/unified_agent/tools/graph_canvas.py
- **Implementation state:** shipped-and-used
- **Consumer:** Web UI Task Graph / Canvas views
- **Entry point:** graph_canvas_read (`mcp-tool`) — graph_canvas_read/graph_canvas_write in tools/graph_canvas.py:200,234.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/graph_canvas.py:200,234 — graph_canvas_read/graph_canvas_write definitions.
  - `test` — tests/test_webui_graph_canvas.py, tests/test_webui_task_graph.py — Web UI graph-canvas and task-graph coverage.
  - `live` (2026-09-19) — graph_canvas_read via Mac hub — 2026-09-19 12:20Z: graph_canvas_read(board_id=default) returned exists=true (updated_by agent-hermes 09-08); scene parsed to node_count=2 edge_count=1 with task_suggestions and dependency_suggestions returned (task #1122).
- **Boundaries:**
  - security: read-only inspection surface for graph state; write path stores canvas annotations, not task lifecycle changes.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: canvas boards are keyed by board_id, hub-scoped.
- **Wedge:** evidence-wedge

#### `lgp-plan-review-lifecycle` — Plan-to-review lifecycle handoff

- **Customer purpose:** Connects an agent's step-by-step plan to the human/CI review pipeline, so a plan is proven out rather than just filed.
- **Origin:** source — src/unified_agent/tools/plan.py, src/unified_agent/reviewer.py
- **Implementation state:** shipped-and-used
- **Consumer:** reviewer.py review pipeline
- **Entry point:** plan_step_claim (`mcp-tool`) — plan_step_claim/plan_handoff feed into reviewer.py's plan-approval step and confirmed merge receipt.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/plan.py:465,541 — plan_step_claim, plan_handoff.
  - `test` — tests/test_plan.py, tests/test_reviewer_pipeline.py — Plan and review-pipeline coverage.
  - `live` (2026-09-17) — review pipeline timer — Doctor snapshot #1057 recorded review pipeline timers LIVE.
- **Boundaries:**
  - security: not-applicable beyond the review gate itself.
  - identity: claim/handoff record the acting agent.
  - approval: merge still requires the human gate (gov-review-merge-receipts).
  - privacy: not-applicable
  - release: core, pilot family.
  - tenant_workspace: scoped to the plan's task.
- **Wedge:** evidence-wedge

#### `lgp-langsmith-boundary` — LangSmith/export tracing boundary (disabled by decision)

- **Customer purpose:** Documents that workflow traces are not exported to a third-party tracing service, preserving the current privacy posture until a decision says otherwise.
- **Origin:** decision — #1057
- **Implementation state:** dormant/uncertain
- **Consumer:** no verified consumer
- **Entry point:** src/unified_agent/langgraph_observability.py (`not-applicable`) — Opt-in LangSmith sink exists (LangSmithTraceExporter) gated by env flag + explicit privacy/cost decision; langgraph_note.py invokes tracing_context(enabled=False, parent=False) by default. No un-gated export path in source.
- **Connection state:** not-applicable
- **Evidence:**
  - `code` — src/unified_agent/langgraph_observability.py:115,134, src/unified_agent/langgraph_note.py:16,216,229 — LangSmithTraceExporter defined as an opt-in sink gated by env + explicit decision; default tracing_context is enabled=False.
  - `test` — tests/test_langgraph_observability.py — LangGraph observability coverage includes the gated sink behavior.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: enabling this would need an explicit decision + approval given its privacy implications.
  - privacy: export stays off until an explicit decision; the sink requires an env flag plus decision, so no workflow trace leaves the hub today.
  - release: opt-in sink shipped but disabled; decision-gated per #1057.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / #1057 — do not enable LangSmith export without a fresh decision. / none — this is intentionally not being built until the operator decides otherwise. / do not enable LangSmith/tracing export as a side effect of unrelated LangGraph work.
- **Wedge:** expansion-slice

## Provider & quota (`provider-quota`)

_Pilot family (first-slice deep coverage)._

The LLM provider fallback chain, credential/subscription pools, quota status, and the usage/cost telemetry and fail-soft boundaries around them.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `prq-provider-chain` | LLM provider fallback chain (ollama -> ai_studio -> openrouter) | shipped-and-used | unknown | `gemini_ask` | session_capture, sleep pass, injection_triage, gemini_ask | expansion-slice |
| `prq-credential-pool` | Credential pool registry (metadata only) | shipped-and-used | unknown | `GET /api/credential_pool` | Control Center Settings > Credentials page (/ui/settings/credentials, packages/web/src/app/settings/CredentialsPage.tsx; the classic Credential Pool pane until the v2 cutover, task #2479), backed by GET /api/credential_pool -> credential_pool_payload() -> credential_pool_list() (task #1121). | expansion-slice |
| `prq-quota-status` | Model quota status (last-known/unknown) | shipped-and-used | unknown | `GET /api/quota` | Control Center Settings > Usage page (/ui/settings/usage, packages/web/src/app/settings/UsagePage.tsx; the classic Quota pane until the v2 cutover, task #2479), backed by GET /api/quota -> quota_payload() -> model_quota_status() (task #1136, landed 2026-09-19, already on main before this dispatch). | expansion-slice |
| `prq-subscription-pool` | Opt-in subscription pool fallback | shipped-and-used | live | `src/unified_agent/llm_client.py` | sleep_pass._generate_journal_digest, consolidate cluster adjudication, and session_capture.summarize opt-in call sites (global and per-call gates both required) | expansion-slice |
| `prq-usage-telemetry` | Usage/cost telemetry and Web UI Telemetry view | shipped-and-used | live | `Web UI Telemetry view` | Web UI Telemetry view | expansion-slice |
| `prq-failsoft-nospend` | Fail-soft provider boundaries and no-spend decisions | shipped-and-used | unknown | `src/unified_agent/llm_client.py` | every llm_client caller (session_capture, sleep pass, injection_triage, autonomous cycles) | expansion-slice |

### Detail

#### `prq-provider-chain` — LLM provider fallback chain (ollama -> ai_studio -> openrouter)

- **Customer purpose:** Keeps agent text-generation calls working across providers automatically, so one provider outage doesn't stop agent work.
- **Origin:** decision — 2026-07-27 Vertex retirement decision
- **Implementation state:** shipped-and-used
- **Consumer:** session_capture, sleep pass, injection_triage, gemini_ask
- **Entry point:** gemini_ask (`mcp-tool`) — llm_client.py chain (ollama -> ai_studio -> openrouter, plus opt-in subscription pool); tools/gemini.py gemini_ask.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/llm_client.py, src/unified_agent/tools/gemini.py — Provider fallback chain and gemini_ask tool.
  - `test` — tests/test_llm_client.py, tests/test_llm_client_subscription_pool.py — Provider-chain and subscription-pool coverage.
  - `live` (2026-09-19) — llm_client legs via Mac hub (read-only) — 2026-09-19: metered legs not probed under the no-spend boundary — gemini_ask calls AI Studio (metered), and the ollama /api/tags probe was correctly refused by the web_fetch SSRF guard ('localhost resolves to a non-public address ::1'). Stream evidence: session-capture memory notes through 2026-09-16 prove at least one leg served real summarize calls; a 2026-09-18 memory note records the hub lacks OPENROUTER_API_KEY, so the openrouter leg is unconfigured (task #1122).
- **Boundaries:**
  - security: ai_studio leg spends real AI Studio credit; uncapped by the operator's explicit choice.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: prompt content is sent to whichever external provider leg is used.
  - release: core; Vertex leg was retired 2026-07-27 and is not present.
  - tenant_workspace: not-applicable — provider calls are not workspace-scoped.
- **Incomplete (owner / decision / next action / non-action):** shared / — / needs-operator: exercise one live call through each provider leg and record which legs respond. / do not assume all three legs are currently reachable from code presence alone.
- **Wedge:** expansion-slice

#### `prq-credential-pool` — Credential pool registry (metadata only)

- **Customer purpose:** Tracks which provider credentials exist and their status without ever storing the secret value itself, so credential inventory can be reviewed safely.
- **Origin:** source — src/unified_agent/credential_pool.py
- **Implementation state:** shipped-and-used
- **Consumer:** Control Center Settings > Credentials page (/ui/settings/credentials, packages/web/src/app/settings/CredentialsPage.tsx; the classic Credential Pool pane until the v2 cutover, task #2479), backed by GET /api/credential_pool -> credential_pool_payload() -> credential_pool_list() (task #1121).
- **Entry point:** GET /api/credential_pool (`web-ui`) — credential_pool_payload() in webui/api.py calls credential_pool_list() and re-projects each row to a named metadata field set (id/provider/label/note/status/active/created_at/updated_at) before it reaches the page, so a secret value can never be forwarded even if the tool's schema changes. Rendered by CredentialsPage.tsx via fetchCredentialPool() (packages/web/src/app/settings/api.ts) and TanStack Query.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/credential_pool.py:43,77,95 — credential_pool_register/list/revoke definitions; rejects secret values in free text.
  - `test` — tests/test_credential_pool.py — Credential pool coverage.
  - `code` — src/unified_agent/webui/api.py credential_pool_payload/_credential_pool_row, src/unified_agent/webui/routes.py credential_pool route, packages/web/src/app/settings/{CredentialsPage.tsx,api.ts} fetchCredentialPool, the /settings/credentials route in packages/web/src/app/routes.ts (task #1121; moved from the classic pane at the v2 cutover, task #2479). — New GET /api/credential_pool route wired to a navigable Control Center page (Settings > Credentials since the v2 cutover; Fleet & Health > Credential Pool before it). Metadata-only by a second, independent guard on top of the tool's own secret-rejection.
  - `test` — tests/test_webui_credential_pool.py, packages/web/tests/app/settings/system.test.tsx — Route test asserts real credential_pool_list rows reach the HTTP surface with exactly the named metadata field set, a named-error unavailable state, GET-only + no /api/action/credential* route, and that credential_pool_payload never calls register/revoke. Frontend test asserts the pane renders registered and revoked rows and a named unavailable state, and that no secret-shaped text appears in the rendered output. Both pass (16 pytest / 4 vitest new cases). That vitest file (packages/web/tests/credential_pool.test.tsx) was deleted with the classic panes at the v2 cutover (task #2479); the Credentials page is covered by the 'settings: credentials pool' cases in packages/web/tests/app/settings/system.test.tsx.
  - `live` (2026-09-21) — attempted live round trip, this session (2026-09-21) — Attempted to verify the reachable-hub path live rather than infer it: (1) mcp__unified-agent__credential_pool_list from this agent-claude session returned 'Hub-mediated filesystem access is denied for agent agent-claude (explicit UA_AGENT_WORKSPACES DENY entry); use this identity's own native file tools instead' -- an identity-scoped ACL denial, not a tool failure; (2) an unauthenticated GET to http://<hub-ip>:8765/api/credential_pool (the Mac hub address, redacted in #2016) returned HTTP 401 'Unauthorized: invalid or missing bearer token', confirming hub auth is enforced on the new route (secrets hygiene: no bearer token was read, echoed, or used from this session). Neither result is a dated live success for connection_state, so connection_state stays 'unknown' per the #1067 guard -- a working local route+test path is not the same claim as a live authenticated round trip, and this item does not claim the latter.
- **Boundaries:**
  - security: explicitly rejects secret values passed as text; stores metadata only, never the credential itself. The Control Center route re-projects every row to a named metadata field set as a second, independent guard, so a future schema change on the tool side cannot widen what the page displays.
  - identity: entries are labeled by provider and a human-chosen label, not tied to a specific agent identity.
  - approval: not-applicable
  - privacy: no secret material stored or logged.
  - release: core.
  - tenant_workspace: not-applicable — pool is hub-scoped.
- **Wedge:** expansion-slice

#### `prq-quota-status` — Model quota status (last-known/unknown)

- **Customer purpose:** Tells an operator whether a provider's quota is known to be exhausted, without pretending to know something the system can't actually observe live.
- **Origin:** source — src/unified_agent/credential_pool.py
- **Implementation state:** shipped-and-used
- **Consumer:** Control Center Settings > Usage page (/ui/settings/usage, packages/web/src/app/settings/UsagePage.tsx; the classic Quota pane until the v2 cutover, task #2479), backed by GET /api/quota -> quota_payload() -> model_quota_status() (task #1136, landed 2026-09-19, already on main before this dispatch).
- **Entry point:** GET /api/quota (`web-ui`) — quota_payload() in webui/api.py calls model_quota_status() and passes through the per-lane availability projection. Rendered by UsagePage.tsx via fetchQuotaReport() (packages/web/src/app/settings/api.ts) and TanStack Query, at the /settings/usage route in packages/web/src/app/routes.ts.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/credential_pool.py:120 — model_quota_status returns last-known/unknown rather than a live-probed value.
  - `test` — tests/test_credential_pool.py — Covers credential_pool.py including quota status.
  - `code` — packages/web/src/app/settings/{UsagePage.tsx,api.ts} fetchQuotaReport, the /settings/usage route in packages/web/src/app/routes.ts (the classic packages/web/src/quota pane until the v2 cutover, task #2479), src/unified_agent/webui/api.py quota_payload, src/unified_agent/webui/routes.py quota route (task #1136, already on main at 2026-09-19 commit a6eaeff -- this catalog entry was stale; the pane exists and is reachable, it was simply never recorded here as the item's consumer). — GET /api/quota is wired to a navigable Control Center page (Settings > Usage since the v2 cutover; Fleet & Health > Quota before it), landed by task #1136 before this dispatch. This PR corrects the catalog to record that existing wiring rather than re-implementing it.
  - `test` — tests/test_webui_quota.py, packages/web/tests/app/settings/system.test.tsx — Route test asserts real model_quota_status lanes reach the HTTP surface including honest unknown/unavailable/available distinctions, a named-error unavailable state, and GET-only with no mutation route. Frontend test asserts per-lane rendering, unknown-vs-available-vs-exhausted distinction, and the named unavailable state. Both pass (re-run this session: pytest 16/16, vitest 4/4 in this file, 308/308 across the full packages/web suite). That vitest file (packages/web/tests/quota.test.tsx) was deleted with the classic panes at the v2 cutover (task #2479); the Usage page is covered by the 'settings: platform, cloud, usage' cases in packages/web/tests/app/settings/system.test.tsx.
  - `live` (2026-09-21) — attempted live round trip, this session (2026-09-21) — Attempted the same live verification as prq-credential-pool: mcp__unified-agent__model_quota_status from this agent-claude session returned the same identity-scoped UA_AGENT_WORKSPACES DENY error (not a tool failure), and an unauthenticated GET to http://<hub-ip>:8765/api/quota (the Mac hub address, redacted in #2016) returned HTTP 401 as expected (no token read or used). connection_state stays 'unknown' -- a dated live authenticated round trip was not obtained this session, so it is not claimed.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core; deliberately honest about staleness rather than inferring a live value.
  - tenant_workspace: not-applicable
- **Wedge:** expansion-slice

#### `prq-subscription-pool` — Opt-in subscription pool fallback

- **Customer purpose:** Lets an operator opt a call into a shared subscription-based provider pool instead of the default per-call credential, for cases where that's cheaper or has more headroom.
- **Origin:** pr — #548
- **Implementation state:** shipped-and-used
- **Consumer:** sleep_pass._generate_journal_digest, consolidate cluster adjudication, and session_capture.summarize opt-in call sites (global and per-call gates both required)
- **Entry point:** src/unified_agent/llm_client.py (`library`) — subscription_pool endpoint added in PR #548 (feat: add opt-in subscription pool fallback), global + per-call flags.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/llm_client.py — Opt-in subscription pool fallback leg.
  - `test` — tests/test_llm_client_subscription_pool.py — Subscription pool coverage.
  - `live` (2026-09-19) — llm_client subscription leg via Mac hub (read-only) — 2026-09-19: the opt-in leg is gated on UA_SUBSCRIPTION_POOL_ENABLED plus a pool URL env on the hub, and no read surface exposes either (not in system_doctor checks). Zero observed calls through the pool leg — nothing measurable without spend (task #1122).
  - `live` (2026-09-22) — task #1252/#1253 Mac verification and 2026-09-22 hub recheck — agent-devin recorded an authenticated 200 through _call_subscription_pool in task #1252. agent-claude recorded dedicated client-key rotation, unauthenticated and wrong-key 401, authenticated /v1/models and chat 200, and hub restart in task #1253. agent-codex rechecked on 2026-09-22: the live tailnet /v1/models endpoint returns 401 without a header, and authenticated system_doctor reports llm_provider_chain LIVE with subscription_pool:up. The client key was never read or printed in this recheck.
- **Boundaries:**
  - security: opt-in only; not used unless a caller explicitly requests it.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: prompt content passes to the pooled provider when used.
  - release: core, added #548.
  - tenant_workspace: not-applicable
- **Wedge:** expansion-slice

#### `prq-usage-telemetry` — Usage/cost telemetry and Web UI Telemetry view

- **Customer purpose:** Shows an operator token/cost usage and a request waterfall, so provider spend is visible instead of discovered later on a bill.
- **Origin:** source — src/unified_agent/lane_b_telemetry.py, src/unified_agent/telemetry/producers.py
- **Implementation state:** shipped-and-used
- **Consumer:** Web UI Telemetry view
- **Entry point:** Web UI Telemetry view (`web-ui`) — lane_b_telemetry.py, telemetry/producers.py feed token/cost/waterfall and exception views.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/lane_b_telemetry.py, src/unified_agent/telemetry/producers.py — Telemetry production for cost/token/waterfall views.
  - `test` — tests/test_telemetry_contract.py — Telemetry contract coverage.
  - `live` (2026-09-17) — MCP 2.x telemetry / web UI — Doctor snapshot #1057 recorded MCP 2.x runtime telemetry LIVE and the Web UI LIVE.
- **Boundaries:**
  - security: read-only aggregation of usage data already recorded.
  - identity: usage rows attributed to the calling agent/task where available.
  - approval: not-applicable
  - privacy: no prompt content in the aggregated view, only usage metadata.
  - release: core; per #1059, quota is not yet first-class in the UI.
  - tenant_workspace: hub-scoped, not per-workspace.
- **Wedge:** expansion-slice

#### `prq-failsoft-nospend` — Fail-soft provider boundaries and no-spend decisions

- **Customer purpose:** Guarantees a provider failure degrades gracefully (never raises uncaught) and documents which spend paths are deliberately closed off, so cost surprises don't come from the LLM client itself.
- **Origin:** decision — 2026-07-27 Vertex retirement decision
- **Implementation state:** shipped-and-used
- **Consumer:** every llm_client caller (session_capture, sleep pass, injection_triage, autonomous cycles)
- **Entry point:** src/unified_agent/llm_client.py (`library`) — Never-raises fallback contract; no Vertex leg by decision; AI Studio $25 pool; autonomous_cycle_record_usage records spend for budget tracking.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/llm_client.py, src/unified_agent/tools/cycles.py:496 — Fail-soft contract and autonomous_cycle_record_usage.
  - `test` — tests/test_llm_client.py, tests/test_autonomous_cycles.py — Fail-soft and usage-recording coverage.
  - `live` (2026-09-19) — autonomous_cycle_list + /api/session-capture via Mac hub — 2026-09-19: no fresh all-legs-failure event to observe — exercising one needs a real provider call (no-spend boundary). Stream evidence: recent autonomous cycles all show used_cost_cents=0/used_tokens=0 (no-spend holding) and the LLMConnectionError fail-loud contract is live at /api/session-capture (502 with route); capture notes keep flowing, so the chain keeps serving (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core; Vertex intentionally excluded per 2026-07-27 decision.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / needs-operator: exercise one live failure-and-fallback path and confirm no exception escapes. / do not reintroduce a Vertex leg without a fresh decision reversing 2026-07-27.
- **Wedge:** expansion-slice

## First Mate (`first-mate`)

_Pilot family (first-slice deep coverage)._

Session capture, delivery-mode selection, scout intake, supervision and wake signals, crew watch/intervention, and operator-attention surfaces for a human overseeing agent work.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `fm-session-capture` | Session capture and lifecycle end recording | shipped-and-used | live | `session_end` | session_end callers (agents at session close) | evidence-wedge |
| `fm-delivery-modes` | Delivery mode selection (no-mistakes / direct-pr / local-only) | shipped-and-used | live | `src/unified_agent/delivery.py` | review pipeline | evidence-wedge |
| `fm-scout` | Scout intake and standalone report | shipped-and-used | unknown | `src/unified_agent/scout.py` | The automated review pipeline, src/unified_agent/reviewer.py: is_scout() (imported at reviewer.py:95,821,1250) gates the scout-report review path, and _review_scout_task() (reviewer.py:1266) calls validate_scout_report() (reviewer.py:1281,1328) to classify a scout-tagged task's report without requiring a PR branch. This is a real, existing consumer the catalog had not recorded. | expansion-slice |
| `fm-supervision` | Zero-token supervision classifier and wake events | shipped-and-used | live | `agent.supervisor.wake` | COS briefing/cadence via drain_supervisor_wakes | evidence-wedge |
| `fm-crew-watch` | Crew live sessions, mailbox, and intervention log | partially-wired | live | `packages/web/src/app/sessions/SessionPage.tsx` | operator via the Web UI session page's live crew watch (/ui/sessions/:id, SessionPage.tsx; the classic CrewLivePanel until the v2 cutover, task #2479); dev agents via the crew_view MCP tool (developer/chief-of-staff/steward profiles) and the operator profile via crew_view MCP (read-only, task #1151) | evidence-wedge |
| `fm-operator-attention` | Operator attention summary and acknowledgment | shipped-and-used | live | `Web UI Home attention summary` | operator via Web UI Home attention summary | evidence-wedge |

### Detail

#### `fm-session-capture` — Session capture and lifecycle end recording

- **Customer purpose:** Captures what happened in an agent session when it ends, with deduplication, so session history is preserved without duplicate memory entries.
- **Origin:** task — #813
- **Implementation state:** shipped-and-used
- **Consumer:** session_end callers (agents at session close)
- **Entry point:** session_end (`mcp-tool`) — session_capture.py backs the session_end tool; deduplicates via memory_save idempotency.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/session_capture.py — Session capture and dedup logic (task #813).
  - `test` — tests/test_session_capture.py — Session capture coverage.
  - `live` (2026-09-19) — session_start + session_end via Mac hub — 2026-09-19 12:25Z: session_start(kind=verify, task #1122) → session_end round trip on probe session 01M2WSTZCJ0MXYJR556ZPBEW15 returned closed:true with learning_proposal evaluated (skipped:low_signal) and friction_nudge computed; deduped session-capture memory notes through 2026-09-16 corroborate the transcript→summarize→memory_save leg (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: capture is attributed to the ending agent session.
  - approval: not-applicable
  - privacy: session content becomes a memory record subject to memory_* access rules.
  - release: core.
  - tenant_workspace: scoped to the calling agent's session.
- **Wedge:** evidence-wedge

#### `fm-delivery-modes` — Delivery mode selection (no-mistakes / direct-pr / local-only)

- **Customer purpose:** Lets an operator control how finished agent work gets delivered — reviewed PR, direct PR, or kept local — instead of one fixed delivery path for every task.
- **Origin:** task — #1026
- **Implementation state:** shipped-and-used
- **Consumer:** review pipeline
- **Entry point:** src/unified_agent/delivery.py (`library`) — no-mistakes/direct-pr/local-only delivery modes plus a yolo mode; consumed by the review pipeline.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/delivery.py — Delivery mode implementation (task #1026); PR #542 fixed a yolo-mode defect.
  - `test` — tests/test_delivery.py, tests/test_delivery_receipt.py — Delivery and receipt coverage.
  - `live` (2026-09-17) — review pipeline timer — Doctor snapshot #1057 recorded review pipeline timers LIVE, the surface that consumes delivery mode.
- **Boundaries:**
  - security: no-mistakes mode is the conservative default; direct-pr and yolo modes bypass some review friction and should be chosen deliberately.
  - identity: delivery mode choice is recorded against the delivering run.
  - approval: direct-pr/yolo modes still respect the merge gate (gov-review-merge-receipts).
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: per-task worktree.
- **Wedge:** evidence-wedge

#### `fm-scout` — Scout intake and standalone report

- **Customer purpose:** Runs a lightweight scan/investigation task that reports findings as a standalone decision inventory, without opening a PR branch just to look around.
- **Origin:** task — #1027
- **Implementation state:** shipped-and-used
- **Consumer:** The automated review pipeline, src/unified_agent/reviewer.py: is_scout() (imported at reviewer.py:95,821,1250) gates the scout-report review path, and _review_scout_task() (reviewer.py:1266) calls validate_scout_report() (reviewer.py:1281,1328) to classify a scout-tagged task's report without requiring a PR branch. This is a real, existing consumer the catalog had not recorded.
- **Entry point:** src/unified_agent/scout.py (`not-applicable`) — Task-shape: scout (#1027); scanner/executor intake producing a standalone report, no PR branch.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/scout.py — Scout task-shape implementation.
  - `code` (2026-09-21) — src/unified_agent/reviewer.py:95,821,1250,1266,1281,1328 (task #1121 phase 2) — Re-verified this session: is_scout() is imported and called at three sites in reviewer.py (a task-shape check, a no-code-task guard, and the main review dispatch), and the dispatch calls _review_scout_task(), which calls validate_scout_report() against the scout markdown. Catalog correction only — connection_state stays 'unknown' per the #1067 guard; no dated live scout-task round trip was run this session.
- **Boundaries:**
  - security: read-only investigation by design (no PR branch produced).
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / find or create one real consumer of a scout report before treating this as shipped-and-used with a verified consumer. / do not add a PR-branch path to scout without a stated reason — its value is being the no-branch scan.
- **Wedge:** expansion-slice

#### `fm-supervision` — Zero-token supervision classifier and wake events

- **Customer purpose:** Watches agent activity for signals worth a human's attention without spending model tokens to do it, and wakes the right consumer when something needs eyes.
- **Origin:** task — #1028
- **Implementation state:** shipped-and-used
- **Consumer:** COS briefing/cadence via drain_supervisor_wakes
- **Entry point:** agent.supervisor.wake (`event-hook`) — supervision.py (#1028); UA_EVENT_SUPERVISION defaults on; durable wake event emitted at task_update.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/supervision.py — Zero-token classifier and wake emitter (task #1028).
  - `test` — tests/test_supervision.py, tests/test_dispatch_supervision.py — Supervision and dispatch-supervision coverage.
  - `live` (2026-09-17) — event bus — Doctor snapshot #1057 recorded the event bus LIVE, the transport supervision wakes use.
- **Boundaries:**
  - security: not-applicable — classifier is heuristic, not model-driven.
  - identity: wakes reference the task/agent that triggered them.
  - approval: not-applicable
  - privacy: not-applicable
  - release: on by default via UA_EVENT_SUPERVISION.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

#### `fm-crew-watch` — Crew live sessions, mailbox, and intervention log

- **Customer purpose:** Lets an operator see live agent sessions and intervene (send a message or record an intervention) mid-run through the Web UI, instead of only after the fact.
- **Origin:** task — #1025
- **Implementation state:** partially-wired
- **Consumer:** operator via the Web UI session page's live crew watch (/ui/sessions/:id, SessionPage.tsx; the classic CrewLivePanel until the v2 cutover, task #2479); dev agents via the crew_view MCP tool (developer/chief-of-staff/steward profiles) and the operator profile via crew_view MCP (read-only, task #1151)
- **Entry point:** packages/web/src/app/sessions/SessionPage.tsx (`web-ui`) — crew.py (#1025) live sessions and .ua/interventions.jsonl; message_send mailbox in tools/messages.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/crew.py:28 — INTERVENTION_RELPATH = .ua/interventions.jsonl append-only drop.
  - `code` — src/unified_agent/tools/messages.py:8 — message_send mailbox tool.
  - `code` — packages/web/src/app/sessions/SessionPage.tsx fetchCrewLive — Web UI session page: the live crew watch (/ui/sessions/:id).
  - `test` — tests/test_crew.py, tests/test_typed_messages.py — Crew and typed-message coverage.
  - `live` (2026-09-19) — crew_view via Mac hub MCP — 2026-09-19 ~15:10Z as agent-devin/developer: crew_view(session_id=01M2WSTZCJ0MXYJR556ZPBEW15) returned a real session payload — task #1122 verify session, status=completed, live=false, message_total=0, interventions_path=null (task #1151).
- **Boundaries:**
  - security: interventions are append-only, not editable in place.
  - identity: interventions and messages record the sending agent/operator.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core; UI present, fresh end-to-end intervention proof absent.
  - tenant_workspace: per-session worktree for the .ua/interventions.jsonl file.
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / exercise one live intervention through the session page's crew watch end to end and record it as live evidence. / do not claim connection_state live from the UI component and backend existing separately.
- **Wedge:** evidence-wedge

#### `fm-operator-attention` — Operator attention summary and acknowledgment

- **Customer purpose:** Rolls supervision wakes up into a single Home-view attention summary an operator can acknowledge, instead of hunting through individual events.
- **Origin:** task — #1059
- **Implementation state:** shipped-and-used
- **Consumer:** operator via Web UI Home attention summary
- **Entry point:** Web UI Home attention summary (`web-ui`) — supervision wakes + dispatch/supervise.py + projection/attention_ack.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/dispatch/supervise.py, src/unified_agent/projection/attention_ack.py — Attention aggregation and acknowledgment.
  - `test` — tests/test_dispatch_supervision.py — Dispatch-supervision coverage (attention-adjacent path).
  - `live` (2026-09-17) — web UI — Doctor snapshot #1057 recorded the Web UI LIVE; #1059 records the Home attention summary as a live UI surface.
- **Boundaries:**
  - security: not-applicable
  - identity: acknowledgment is attributed to the acknowledging operator.
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

## Telemetry & utilization (`telemetry-utilization`)

MCP health, the event log and audit chain, anomaly/review pipeline observability, and utilization/cost telemetry views.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `tel-mcp-health` | MCP health and 2.x protocol telemetry | shipped-and-used | live | `mcp_health_telemetry` | operator (mcp_health_telemetry) and the connecting client's own compatibility check | evidence-wedge |
| `tel-event-log` | Durable event log and SSE cursor | shipped-and-used | live | `event_log_query` | Web UI SSE stream | evidence-wedge |
| `tel-metrics-anomaly-review` | Metrics snapshot, anomaly detection, and review/anomaly/steward timers | shipped-and-used | live | `check_anomalies_and_notify` | reviewer.py pipeline and operator notification (check_anomalies_and_notify) | evidence-wedge |
| `tel-utilization-views` | Utilization/cost Web UI telemetry views | shipped-and-used | live | `Web UI Telemetry view` | operator via Web UI Telemetry view | expansion-slice |
| `tel-otel-internals` | OpenTelemetry span schema (consumed) and span emitter (retired, internal-only) | partially-wired | unknown | `src/unified_agent/telemetry/otel.py` | Split item. telemetry/schema.py (SpanOutcome, validate_telemetry_event) has a real consumer: telemetry/projection.py imports both and uses them to build the telemetry payload; webui/api.py:telemetry_payload (webui/api.py:2953-2960, importing telemetry.projection as telemetry_projection) serves that payload to the Control Center's Runs > Telemetry page (/ui/runs/telemetry). The catalog had not recorded this half's consumer. telemetry/otel.py (the actual OTel span emitter/exporter) has zero consumers anywhere in the repo outside its own test file (tests/test_otel.py) — RETIRED as internal-only-by-default; see the non_action note below. Correcting a phase-1 over-broad claim: telemetry/redaction.py is NOT unconsumed — redact_sensitive_content/sanitize_metadata are imported and used by at least 13 other modules repo-wide (e.g. lifecycle/packet.py, lifecycle/capture.py, messaging/send.py, horizon2.py, crew.py, evolve.py, session_friction.py, several langgraph_* modules, tools/learning.py, tools/gcloud.py) as a general-purpose redaction utility, independent of telemetry/otel.py. redaction.py is not part of the retirement; only otel.py's span-emission/exporter surface is retired. | expansion-slice |

### Detail

#### `tel-mcp-health` — MCP health and 2.x protocol telemetry

- **Customer purpose:** Reports whether the MCP transport is healthy and which protocol version connected clients actually negotiated, so a silent protocol mismatch doesn't hide as a working connection.
- **Origin:** source — src/unified_agent/tools/mcp_health.py, src/unified_agent/mcp_telemetry.py, src/unified_agent/mcp_runtime_telemetry.py
- **Implementation state:** shipped-and-used
- **Consumer:** operator (mcp_health_telemetry) and the connecting client's own compatibility check
- **Entry point:** mcp_health_telemetry (`mcp-tool`) — tools/mcp_health.py; mcp_telemetry.py; mcp_runtime_telemetry.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/mcp_health.py, src/unified_agent/mcp_telemetry.py, src/unified_agent/mcp_runtime_telemetry.py — MCP health/telemetry modules.
  - `test` — tests/test_mcp_runtime_telemetry.py, tests/test_mcp2_compatibility.py, tests/test_mcp2_smoke.py — Runtime telemetry and 2.x compatibility coverage.
  - `live` (2026-09-17) — MCP 2.x telemetry — Doctor snapshot #1057 recorded MCP 2.x telemetry LIVE and healthy, and recorded the negotiated client protocol as 2025-06-18 against a 2026-07-28 target.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

#### `tel-event-log` — Durable event log and SSE cursor

- **Customer purpose:** Streams live durable events to the Web UI with a resumable cursor, so a reconnecting client doesn't miss or double-count events.
- **Origin:** source — src/unified_agent/events.py, src/unified_agent/tools/events.py
- **Implementation state:** shipped-and-used
- **Consumer:** Web UI SSE stream
- **Entry point:** event_log_query (`mcp-tool`) — events.py + tools/events.py; audit hash chain; durable-event SSE cursor consumed by the Web UI.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/events.py, src/unified_agent/tools/events.py — Event log and query/verify tools.
  - `test` — tests/test_webui_sse.py, tests/test_events.py — SSE cursor and event log coverage.
  - `live` (2026-09-17) — event bus — Doctor snapshot #1057 recorded the event bus LIVE.
- **Boundaries:**
  - security: not-applicable beyond the audit chain already covered under gov-audit-ledger.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: hub-scoped stream.
- **Wedge:** evidence-wedge

#### `tel-metrics-anomaly-review` — Metrics snapshot, anomaly detection, and review/anomaly/steward timers

- **Customer purpose:** Watches agent activity metrics for anomalies and notifies automatically, so a stuck or misbehaving agent gets flagged without a human polling logs.
- **Origin:** source — src/unified_agent/monitor.py
- **Implementation state:** shipped-and-used
- **Consumer:** reviewer.py pipeline and operator notification (check_anomalies_and_notify)
- **Entry point:** check_anomalies_and_notify (`mcp-tool`) — agent_activity/metrics_snapshot/check_anomalies/perf_summary/observability_manage in monitor.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/monitor.py — Activity, metrics, anomaly, and observability functions; ANOMALY_SERVICES_DEFAULT covers unified-agent,gbrain.
  - `test` — tests/test_monitor.py — Monitor coverage.
  - `live` (2026-09-17) — anomaly + review pipeline timers — Doctor snapshot #1057 recorded anomaly and review-pipeline timers LIVE.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

#### `tel-utilization-views` — Utilization/cost Web UI telemetry views

- **Customer purpose:** Gives an operator a Web UI view of token/cost/usage over time, so utilization is visible in one place rather than derived by hand.
- **Origin:** task — #1059
- **Implementation state:** shipped-and-used
- **Consumer:** operator via Web UI Telemetry view
- **Entry point:** Web UI Telemetry view (`web-ui`) — lane_b_telemetry.py, telemetry/projection.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/lane_b_telemetry.py, src/unified_agent/telemetry/projection.py — Telemetry projection feeding the Web UI.
  - `test` — tests/test_telemetry_contract.py, tests/test_telemetry_projection.py — Telemetry contract and projection coverage.
  - `live` (2026-09-17) — web UI — Doctor snapshot #1057 recorded the Web UI LIVE; #1059 records quota is not yet first-class in this view.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: core.
  - tenant_workspace: not-applicable
- **Wedge:** expansion-slice

#### `tel-otel-internals` — OpenTelemetry span schema (consumed) and span emitter (retired, internal-only)

- **Customer purpose:** Captures structured internal spans with redaction applied, laying groundwork for external exportability without yet exporting anywhere.
- **Origin:** source — src/unified_agent/telemetry/otel.py, src/unified_agent/telemetry/schema.py, src/unified_agent/telemetry/redaction.py, src/unified_agent/telemetry/fixtures.py
- **Implementation state:** partially-wired
- **Consumer:** Split item. telemetry/schema.py (SpanOutcome, validate_telemetry_event) has a real consumer: telemetry/projection.py imports both and uses them to build the telemetry payload; webui/api.py:telemetry_payload (webui/api.py:2953-2960, importing telemetry.projection as telemetry_projection) serves that payload to the Control Center's Runs > Telemetry page (/ui/runs/telemetry). The catalog had not recorded this half's consumer. telemetry/otel.py (the actual OTel span emitter/exporter) has zero consumers anywhere in the repo outside its own test file (tests/test_otel.py) — RETIRED as internal-only-by-default; see the non_action note below. Correcting a phase-1 over-broad claim: telemetry/redaction.py is NOT unconsumed — redact_sensitive_content/sanitize_metadata are imported and used by at least 13 other modules repo-wide (e.g. lifecycle/packet.py, lifecycle/capture.py, messaging/send.py, horizon2.py, crew.py, evolve.py, session_friction.py, several langgraph_* modules, tools/learning.py, tools/gcloud.py) as a general-purpose redaction utility, independent of telemetry/otel.py. redaction.py is not part of the retirement; only otel.py's span-emission/exporter surface is retired.
- **Entry point:** src/unified_agent/telemetry/otel.py (`not-applicable`) — Internal span/schema/redaction/fixtures modules; no external exporter observed.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/telemetry/otel.py, schema.py, redaction.py, fixtures.py — OTel span schema, redaction, and fixture modules.
  - `code` (2026-09-21) — src/unified_agent/telemetry/projection.py:15,20-27,51,209,223,231,251; src/unified_agent/webui/api.py:37,2953,2960 (task #1121 phase 2) — Re-verified this session: projection.py imports SpanOutcome and validate_telemetry_event from schema.py and uses them throughout its waterfall-building logic; webui/api.py's telemetry_payload endpoint calls telemetry_projection.telemetry_payload(...), which is the data source of the Control Center's Runs > Telemetry page. Catalog correction only — connection_state stays 'unknown' per the #1067 guard.
  - `code` (2026-09-21) — grep across src/ and tests/ for telemetry.otel / telemetry/redaction.py importers (task #1121 phase 2) — Re-verified this session: no module outside telemetry/otel.py itself imports from telemetry.otel except tests/test_otel.py (OTEL_EXPORT_ENV, OTLPTraceExporter, spans_to_otlp_json) — confirms the span-emitter half is genuinely unconsumed in production code. Separately, telemetry/redaction.py's redact_sensitive_content and sanitize_metadata ARE imported by 13+ other modules repo-wide (lifecycle/packet.py, lifecycle/capture.py, messaging/send.py, horizon2.py, crew.py, evolve.py, session_friction.py, langgraph_note.py, langgraph_note_dispatch.py, langgraph_workflow.py, langgraph_workflow_dispatch.py, langgraph_dispatch.py, langgraph_observability.py, tools/learning.py, tools/gcloud.py) as a standalone redaction utility — this corrects phase 1's framing, which grouped otel.py and redaction.py together as both unconsumed. redaction.py is not retired; only otel.py's own span-emission/exporter surface is.
- **Boundaries:**
  - security: redaction.py exists specifically to prevent secret leakage into spans.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: redaction module is the privacy control for this surface.
  - release: internal only; no external exporter wired.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / schema.py half: none — it already has a real consumer (the Control Center's Runs > Telemetry page), recorded above. otel.py span-emitter half: none without a decision — 'decide and wire one external exporter target, or explicitly close this as internal-only' is still the right next step, and it is a decision, not a build. / RETIRE (task #1121 phase 2, 2026-09-21): telemetry/otel.py's standalone OTel span-emission/exporter surface is explicitly recorded as not-consumed-by-decision, not silently left as unrecorded debt. No code was deleted or disabled — otel.py, its OTLPTraceExporter, and its test coverage (tests/test_otel.py) remain exactly as they were; a future decision to wire an external exporter target can build on them directly. Do not wire an external OTel exporter without a stated destination and redaction review (unchanged from phase 1).
- **Wedge:** expansion-slice

## Integrations & surfaces (`integrations-surfaces`)

Slack, Zulip, iMessage/notify, OAuth and the public boundary, GCP/cloud, mobile/Apple Watch, the modern Web UI, and the SDK/conformance surface.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `int-slack` | Slack gateway, chat, and outbound relay | partially-wired | unknown | `src/unified_agent/slack_gateway.py` | message_send tool -> src/unified_agent/messaging/send.py:107-109 -> slack_outbound.schedule_mirror_hub_message(). Any message_send call schedules a Slack mirror when Slack is configured. This is a real, existing consumer path the catalog had not recorded. The open gap is proof of a live Slack connection (credentials configured + one round trip), which is an operator action, not a code gap. | expansion-slice |
| `int-zulip-relay` | Zulip relay (chat dormant, worker unknown) | shipped-and-used | live | `zulip_send_message` | Zulip stream subscribers | expansion-slice |
| `int-imessage-notify` | iMessage/notify downstream delivery | shipped-and-used | unknown | `notify` | reviewer.py review_and_notify and monitor.py check_anomalies_and_notify | expansion-slice |
| `int-oauth-boundary` | OAuth authorization server and public exposure boundary | shipped-and-used | live | `src/unified_agent/oauth.py` | agent-server-agent8 (authorized OAuth client) | evidence-wedge |
| `int-gcp-agent20` | GCP inventory, ops, and cloud worker recovery | partially-wired | unknown | `gcloud_ops` | autonomous_cycle_manage tool -> src/unified_agent/tools/cycles.py:654-784 -> cloud_recovery._cloud_worker_recover() (the cloud_worker_recover capability) and a gcloud_ops profile-allowlist gate on the 'recover_worker' cycle action. This is exactly the GCP worker self-healing consumer task #1043 asked for, and it already exists in code — the catalog had not recorded it. | expansion-slice |
| `int-mobile-watch` | Mobile / Apple Watch action and attention projection | shipped-but-unconsumed | unknown | `Web UI Devices routes` | no verified Apple Watch consumer; the authenticated iPhone relay is the current mobile route boundary | expansion-slice |
| `int-modern-webui` | Modern Web UI (SPA, routes, SSE, contracts, auth) | shipped-and-used | live | `src/unified_agent/webui/spa.py` | operator (primary daily-use surface) | evidence-wedge |
| `int-sdk-conformance` | TypeScript/protocol SDK and conformance suite | shipped-and-used | not-applicable | `packages/client` | external SDK consumers and the in-repo conformance suite | evidence-wedge |

### Detail

#### `int-slack` — Slack gateway, chat, and outbound relay

- **Customer purpose:** Lets agent activity and notifications reach a Slack workspace, and lets Slack messages reach an agent, when credentials are configured.
- **Origin:** source — src/unified_agent/slack_gateway.py, src/unified_agent/slack_chat.py, src/unified_agent/slack_outbound.py
- **Implementation state:** partially-wired
- **Consumer:** message_send tool -> src/unified_agent/messaging/send.py:107-109 -> slack_outbound.schedule_mirror_hub_message(). Any message_send call schedules a Slack mirror when Slack is configured. This is a real, existing consumer path the catalog had not recorded. The open gap is proof of a live Slack connection (credentials configured + one round trip), which is an operator action, not a code gap.
- **Entry point:** src/unified_agent/slack_gateway.py (`config`) — Slack gateway/chat/outbound code and config; no doctor credential proof of a live Slack connection.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/slack_gateway.py, slack_chat.py, slack_outbound.py — Slack integration code.
  - `test` — tests/test_slack_gateway.py, tests/test_slack_chat.py, tests/test_slack_outbound.py, tests/test_slack_tls_forwarder.py, tests/test_slack_web_route.py — Slack gateway, chat, outbound, TLS forwarder, and web-route coverage.
  - `code` (2026-09-21) — src/unified_agent/messaging/send.py:107-109 (task #1121 phase 2) — Re-verified this session: send.py imports slack_outbound and calls slack_outbound.schedule_mirror_hub_message(message_result=result, body=text) from within message_send's normal flow. Catalog correction only — connection_state stays 'unknown' per the #1067 guard; no Slack credential configuration or live message round trip was attempted or claimed this session.
- **Boundaries:**
  - security: TLS forwarder path exists per test_slack_tls_forwarder.py.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: message content crosses to Slack's servers when configured.
  - release: requires Slack credentials to be configured; not verified live.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / confirm Slack credentials are configured on the hub and record one live message round trip. / do not claim a live Slack connection from code/config presence alone.
- **Wedge:** expansion-slice

#### `int-zulip-relay` — Zulip relay (chat dormant, worker unknown)

- **Customer purpose:** Relays agent notifications into a Zulip stream so a team following Zulip sees agent activity without checking the hub directly.
- **Origin:** source — src/unified_agent/tools/zulip.py, src/unified_agent/zulip_worker.py, src/unified_agent/zulip_automation.py, src/unified_agent/zulip_chat.py
- **Implementation state:** shipped-and-used
- **Consumer:** Zulip stream subscribers
- **Entry point:** zulip_send_message (`mcp-tool`) — tools/zulip.py; zulip_worker.py; zulip_automation.py; zulip_chat.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/zulip.py, zulip_worker.py, zulip_automation.py, zulip_chat.py — Zulip relay, worker, automation, and chat modules.
  - `test` — tests/test_zulip.py, tests/test_zulip_worker.py, tests/test_zulip_automation.py, tests/test_zulip_chat.py — Zulip relay, worker, automation, and chat coverage.
  - `live` (2026-09-17) — Zulip relay — Doctor snapshot #1057 recorded the Zulip relay LIVE, live chat DORMANT, and the worker UNKNOWN (remote host cannot probe the service manager).
- **Boundaries:**
  - security: not-applicable
  - identity: messages are attributed to the sending agent/bot identity.
  - approval: not-applicable
  - privacy: message content crosses to the Zulip server.
  - release: relay is live; interactive live chat is dormant per doctor snapshot.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / probe the Zulip worker's service manager directly (not remotely) to resolve its unknown status. / do not re-enable live chat without first confirming why it went dormant.
- **Wedge:** expansion-slice

#### `int-imessage-notify` — iMessage/notify downstream delivery

- **Customer purpose:** Delivers a notification or digest to iMessage as a side-effect surface, not as the authority on whether an action happened.
- **Origin:** source — src/unified_agent/tools/notify.py, src/unified_agent/messaging/send.py
- **Implementation state:** shipped-and-used
- **Consumer:** reviewer.py review_and_notify and monitor.py check_anomalies_and_notify
- **Entry point:** notify (`mcp-tool`) — notify tool (tools/notify.py:34) calls messaging/send.py (bun send.ts); batch_notify/flush_digest live in tools/digest.py, called from reviewer.py/monitor.py/tools/delegate.py. Profile gate (#1178): notify sits in profiles.py DEVELOPER_DENIED_TOOLS, so the mcp-tool entry point assumes a chief-of-staff or stdio/unauthenticated caller; developer-profile sessions cannot reach it. Read-safe reachability preflight exists without notify access: the bridge_disconnect anomaly rule in check_anomalies (observability_manage action 'anomalies') verifies send.ts presence and bun on PATH.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/tools/notify.py:34, src/unified_agent/messaging/send.py — notify tool and iMessage send bridge.
  - `code` — src/unified_agent/reviewer.py:613,1869, src/unified_agent/monitor.py:1217 — Real callers of notify() from the review pipeline and anomaly monitor.
  - `test` — tests/test_digest.py, tests/test_typed_messages.py — Digest and typed-message coverage.
  - `live` (2026-09-19) — capability_catalog.mcp_tools + developer-profile tools/list — 2026-09-19: notify is hub-registered — capability_catalog.mcp_tools (177 tools) lists it — but the developer profile's tools/list does not serve it (hub-side profile deny-list, profiles.py DEVELOPER_DENIED_TOOLS). Hub capability present; developer-surface filtered. The end-to-end leg — an actual iMessage delivery to the operator's devices — still needs an operator send plus receipt confirmation; profile visibility of notify is tracked under #1178 (task #1122).
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: message content is delivered to the configured recipient's device.
  - release: core; notify is a downstream side effect, not the source of truth for whether an action occurred, per #1057.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / operator sends one real notify to a device and confirms receipt (delivery, not hub registration, is the unverified leg). / do not treat a successful notify call as proof the underlying action succeeded.
- **Wedge:** expansion-slice

#### `int-oauth-boundary` — OAuth authorization server and public exposure boundary

- **Customer purpose:** Issues OAuth tokens for authorized external clients while keeping the hub off the public internet by default, so remote access is deliberate rather than accidental.
- **Origin:** decision — #1003
- **Implementation state:** shipped-and-used
- **Consumer:** agent-server-agent8 (authorized OAuth client)
- **Entry point:** src/unified_agent/oauth.py (`config`) — oauth.py; webui/auth.py; UA_HTTP_PUBLIC_HOSTS gates public exposure.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/oauth.py, src/unified_agent/webui/auth.py — OAuth AS and Web UI auth modules.
  - `test` — tests/test_mcp_oauth.py, tests/test_webui_auth.py — MCP OAuth and Web UI auth coverage.
  - `live` (2026-09-17) — OAuth AS — Doctor snapshot #1057 recorded the OAuth AS LIVE for agent-server-agent8, client OAuth credentials OFF, and public exposure disabled by decision.
- **Boundaries:**
  - security: public exposure is off by default (UA_HTTP_PUBLIC_HOSTS); client OAuth credentials are currently OFF.
  - identity: OAuth tokens are scoped per authorized client.
  - approval: #1003 (owner agent-claude) is a preserved candidate to change this posture; do not dispatch it without the operator.
  - privacy: not-applicable
  - release: core; negotiated protocol currently 2025-06-18 vs a 2026-07-28 target per doctor snapshot.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

#### `int-gcp-agent20` — GCP inventory, ops, and cloud worker recovery

- **Customer purpose:** Lets an operator inspect and act on GCP resources and recover an unhealthy cloud worker through a gated tool instead of manual console access.
- **Origin:** task — #1043, #1044
- **Implementation state:** partially-wired
- **Consumer:** autonomous_cycle_manage tool -> src/unified_agent/tools/cycles.py:654-784 -> cloud_recovery._cloud_worker_recover() (the cloud_worker_recover capability) and a gcloud_ops profile-allowlist gate on the 'recover_worker' cycle action. This is exactly the GCP worker self-healing consumer task #1043 asked for, and it already exists in code — the catalog had not recorded it.
- **Entry point:** gcloud_ops (`mcp-tool`) — tools/gcloud.py:203,441 gcloud_inventory/gcloud_ops; tools/cloud_recovery.py:164 cloud_worker_recover.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/tools/gcloud.py:203,441 — gcloud_inventory and gcloud_ops definitions.
  - `code` — src/unified_agent/tools/cloud_recovery.py:164 — cloud_worker_recover definition (PR #555).
  - `test` — tests/test_gcloud_tools.py — GCP tool coverage.
  - `live` (2026-09-17) — approval gate — Doctor snapshot #1057 recorded the approval gate LIVE for gcloud_ops specifically.
  - `code` (2026-09-21) — src/unified_agent/tools/cycles.py:654,700-703,739,759,780,783-784 (task #1121 phase 2) — Re-verified this session: cycles.py:654 requires the cloud_worker_recover and gcloud_ops capabilities for the recovery path; cycles.py:700-703 imports and calls cloud_recovery._cloud_worker_recover(); cycles.py:783-784 requires gcloud_ops to be allowed in the active profile before the 'recover_worker' cycle action runs. This is the #1043 self-healing consumer already wired. Catalog correction only — connection_state stays 'unknown' per the #1067 guard; the approval-gate-live finding from doctor snapshot #1057 is a gate, not a demonstrated live recovery, and is not upgraded here.
- **Boundaries:**
  - security: gcloud_ops is approval-gated per doctor snapshot #1057.
  - identity: not-applicable
  - approval: gated; requires human approval_resolve.
  - privacy: not-applicable
  - release: TS worker component recorded DORMANT per doctor snapshot; no doctor proof the GCP worker itself is live.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / pursue #1043 (GCP worker self-healing) and #1044 (Control Center GCP health/budget) only. / do not build a parallel GCP health dashboard outside #1043/#1044 per #1060.
- **Wedge:** expansion-slice

#### `int-mobile-watch` — Mobile / Apple Watch action and attention projection

- **Customer purpose:** Lets an operator see attention summaries and take a small set of safe actions from a phone or watch, contract-first, without giving the device broad agent control.
- **Origin:** pr — #188
- **Implementation state:** shipped-but-unconsumed
- **Consumer:** no verified Apple Watch consumer; the authenticated iPhone relay is the current mobile route boundary
- **Entry point:** Web UI Devices routes (`web-ui`) — ConnectorManifest mobile-watch; GET /api/mobile/v1/projection and POST /api/mobile/v1/action; UA_MOBILE_WATCH_ENABLED=1 opts in. Hub auth remains required and action retains origin/CSRF checks. The authenticated iPhone relay is the current route boundary; no Watch client is verified.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/projection/mobile_v1.py, mobile_action_v1.py, attention_ack.py — Mobile projection, safe-action, and acknowledgment modules (PR #188 defined the Apple Watch projection and safety contract).
  - `test` — tests/test_mobile_projection.py, tests/test_mobile_action.py — Mobile projection and action coverage.
  - `code` — src/unified_agent/connectors.py, src/unified_agent/config.py, src/unified_agent/webui/routes.py — Default-off mobile-watch manifest and UA_MOBILE_WATCH_ENABLED route gate; the connector registry, doctor, and the Settings > Integrations page expose state without implying a device consumer.
  - `test` — tests/connectors/test_mobile_watch_conformance.py, tests/test_mobile_action.py — Manifest/doctor/pane conformance, exact opt-in behavior, disabled-route response, and authenticated action CSRF coverage.
- **Boundaries:**
  - security: The mobile action contract exposes a small safe-action set, not general agent control; both routes require hub auth, and actions retain origin/CSRF checks.
  - identity: hub-authenticated iPhone relay boundary; no verified Apple Watch client
  - approval: attention_ack records human acknowledgment.
  - privacy: not-applicable
  - release: Default-off connector and route gate are tested; no fresh iPhone/device round-trip proof.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / run an authenticated iPhone relay/device rehearsal per #1060 P1 and #785; keep Apple Watch consumer status unverified until evidence exists. / do not build a new mobile dashboard outside the #1060 lane or claim a Watch client/push transport exists.
- **Wedge:** expansion-slice

#### `int-modern-webui` — Modern Web UI (SPA, routes, SSE, contracts, auth)

- **Customer purpose:** Gives an operator a single modern web interface for tasks, dispatch, telemetry, sessions, and evidence instead of scattered CLI calls.
- **Origin:** task — #885
- **Implementation state:** shipped-and-used
- **Consumer:** operator (primary daily-use surface)
- **Entry point:** src/unified_agent/webui/spa.py (`web-ui`) — webui/api.py, routes.py, spa.py, sse.py, contracts.py, auth.py, project_registry.py; packages/web/src/app/routes.ts defines the eight destinations and every route (Control Center v2, task #2479; the classic app and its legacy_ui_index fallback are deleted).
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/webui/api.py, routes.py, spa.py, sse.py, contracts.py, auth.py, project_registry.py — Web UI backend modules.
  - `code` — packages/web/src/app/routes.ts — SPA destinations and route samples.
  - `test` — tests/test_webui_sse.py, tests/test_webui_auth.py, tests/test_webui_dispatch_stack.py — Web UI SSE, auth, and dispatch-stack coverage.
  - `live` (2026-09-17) — web UI — Doctor snapshot #1057 recorded the Web UI LIVE with 56 routes and static assets.
- **Boundaries:**
  - security: auth.py gates access; UA_HTTP_PUBLIC_HOSTS gates public exposure.
  - identity: session-based, tied to the connecting agent/operator identity.
  - approval: gated actions surfaced in the UI still route through approval_pending/resolve.
  - privacy: not-applicable
  - release: core; #885 tracks the modern UI cutover (legacy retirement is pending — #993 is a stale reference, not present in the live queue as of 2026-09-17).
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

#### `int-sdk-conformance` — TypeScript/protocol SDK and conformance suite

- **Customer purpose:** Gives an external TypeScript client a typed, versioned SDK against the unified-agent protocol, verified by a conformance suite rather than trust.
- **Origin:** task — #935, #944
- **Implementation state:** shipped-and-used
- **Consumer:** external SDK consumers and the in-repo conformance suite
- **Entry point:** packages/client (`library`) — packages/client, packages/protocol, packages/conformance.
- **Connection state:** not-applicable
- **Evidence:**
  - `code` — packages/client, packages/protocol, packages/conformance — SDK client, protocol types, and conformance harness.
  - `test` — tests/test_conformance_path.py, tests/test_conformance_shell.py — Conformance path and shell coverage.
- **Boundaries:**
  - security: not-applicable — library surface.
  - identity: not-applicable
  - approval: not-applicable
  - privacy: not-applicable
  - release: v0.2 mirror implemented and shipped; v0.3 expansion lane is open (#935) with #944 (SDK/accountability product lens) doing.
  - tenant_workspace: not-applicable — library, not a workspace-bound tool.
- **Wedge:** evidence-wedge

## Memory, vault & gbrain (`memory-vault-gbrain`)

Agent memory, the Obsidian vault, session packet/recall, and the deliberate boundary that keeps gbrain a separate, non-adapted store.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `mem-agent-memory` | Agent memory save/search/feedback with semantic search | shipped-and-used | live | `memory_search` | every connected Claude Code / Antigravity / Cursor / Codex session | evidence-wedge |
| `mem-vault-obsidian` | Obsidian vault read/write/search/journal/query | shipped-and-used | live | `vault_read` | operator's Obsidian vault and steward/chief-of-staff overlays | evidence-wedge |
| `mem-session-packet-recall` | Session packet capture and recall | shipped-and-used | unknown | `session_start(recall_prior_packet=True)` | session_start tool (src/unified_agent/tools/registry.py) with recall_prior_packet=True (task #1121 phase 2, new this PR): when task_id is given, it calls registry.most_recent_prior_session() to find the most recent other session already recorded against that task, then calls the same lifecycle/packet.py:build_session_packet() the session_packet MCP tool uses, scoped to that prior session, and returns it as recalled_packet plus recalled_from_session_id. This is the genuinely-open gap phase 1 identified: nothing previously called session_packet's backing logic to recall a *prior* session's context at session start (the pre-existing include_packet=True flag only ever built a packet for the brand-new session itself, and was only ever exercised by a test, never a real caller). | evidence-wedge |
| `mem-gbrain-boundary` | Gbrain continuity boundary (deliberately no in-repo adapter) | dormant/uncertain | not-applicable | `src/unified_agent/monitor.py` | no verified consumer | expansion-slice |

### Detail

#### `mem-agent-memory` — Agent memory save/search/feedback with semantic search

- **Customer purpose:** Lets any connected agent save and recall durable notes across sessions with hybrid keyword + semantic search, so context isn't lost between conversations.
- **Origin:** source — src/unified_agent/tools/memory.py, src/unified_agent/embeddings.py
- **Implementation state:** shipped-and-used
- **Consumer:** every connected Claude Code / Antigravity / Cursor / Codex session
- **Entry point:** memory_search (`mcp-tool`) — memory_save/search/list/get/feedback/delete/restore in tools/memory.py; tools/memory_prune.py for candidate pruning; embeddings.py backs semantic search.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/memory.py, src/unified_agent/tools/memory_prune.py, src/unified_agent/embeddings.py — Memory CRUD, pruning, and embeddings modules.
  - `test` — tests/test_memory_vec.py, tests/test_semantic_search.py, tests/test_memory_tasks.py — Vector memory, semantic search, and memory-task coverage.
  - `live` (2026-09-17) — semantic search — Doctor snapshot #1057 recorded semantic search LIVE with 3,581 embedded notes.
- **Boundaries:**
  - security: not-applicable beyond the general fs/vault sandbox.
  - identity: memory rows are tagged with an author label.
  - approval: not-applicable
  - privacy: memory content stays in the hub's own store; no external egress from memory_* tools.
  - release: core.
  - tenant_workspace: shared across all connected agents on the hub, not per-workspace.
- **Wedge:** evidence-wedge

#### `mem-vault-obsidian` — Obsidian vault read/write/search/journal/query

- **Customer purpose:** Lets an agent read and write to the operator's real Obsidian vault — journaling, PARA filing, graph queries — as a first-class interface instead of a one-way log dump.
- **Origin:** source — src/unified_agent/tools/vault.py, src/unified_agent/tools/vault_graph.py, src/unified_agent/tools/vault_ingest.py, src/unified_agent/tools/obsidian.py
- **Implementation state:** shipped-and-used
- **Consumer:** operator's Obsidian vault and steward/chief-of-staff overlays
- **Entry point:** vault_read (`mcp-tool`) — vault_read/write/list/search/journal/query/links/ingest/wiki_custodian/briefing_digest in tools/vault.py; vault_graph.py; vault_ingest.py; obsidian.py.
- **Connection state:** live
- **Evidence:**
  - `code` — src/unified_agent/tools/vault.py, vault_graph.py, vault_ingest.py, obsidian.py — Vault read/write/graph/ingest/obsidian modules.
  - `test` — tests/test_vault.py, tests/test_vault_graph.py, tests/test_vault_ingest.py, tests/test_wiki.py, tests/test_obsidian.py — Vault, wiki, and Obsidian coverage.
  - `live` (2026-09-17) — vault read/search — vault read/search calls through the connected hub MCP succeeded this session.
- **Boundaries:**
  - security: writes refuse notes marked locked: true unless force=True, and are restricted to UA_VAULT_WRITE_DIRS.
  - identity: journal entries are agent-attributed.
  - approval: a forced write to a locked note requires human/steward approval per repo instructions.
  - privacy: vault content stays local to the configured UA_VAULT_PATH; no external egress.
  - release: core.
  - tenant_workspace: vault is intentionally a separate store from the task/memory database, by design (#1060 P2).
- **Wedge:** evidence-wedge

#### `mem-session-packet-recall` — Session packet capture and recall

- **Customer purpose:** Bundles a session's key context into a retrievable packet, so a later session can recall what happened without re-reading a full transcript.
- **Origin:** source — src/unified_agent/lifecycle/capture.py, src/unified_agent/lifecycle/packet.py, src/unified_agent/tools/lifecycle.py
- **Implementation state:** shipped-and-used
- **Consumer:** session_start tool (src/unified_agent/tools/registry.py) with recall_prior_packet=True (task #1121 phase 2, new this PR): when task_id is given, it calls registry.most_recent_prior_session() to find the most recent other session already recorded against that task, then calls the same lifecycle/packet.py:build_session_packet() the session_packet MCP tool uses, scoped to that prior session, and returns it as recalled_packet plus recalled_from_session_id. This is the genuinely-open gap phase 1 identified: nothing previously called session_packet's backing logic to recall a *prior* session's context at session start (the pre-existing include_packet=True flag only ever built a packet for the brand-new session itself, and was only ever exercised by a test, never a real caller).
- **Entry point:** session_start(recall_prior_packet=True) (`mcp-tool`) — src/unified_agent/tools/registry.py:session_start -> registry.most_recent_prior_session() -> lifecycle/packet.py:build_session_packet() for the found prior session. Off by default (recall_prior_packet=False); a resuming agent or a future /context-restore-style flow opts in explicitly.
- **Connection state:** unknown
- **Evidence:**
  - `code` — src/unified_agent/lifecycle/capture.py, packet.py, src/unified_agent/tools/lifecycle.py — Session packet capture and lifecycle recording.
  - `test` — tests/test_session_packet_memory.py — Session packet memory coverage.
  - `code` (2026-09-21) — src/unified_agent/registry.py:most_recent_prior_session; src/unified_agent/tools/registry.py:session_start (task #1121 phase 2) — New wiring, this PR: registry.most_recent_prior_session(task_id, exclude_session_id=...) looks up the most recent other session row for the task (any status, best-effort, never raises). session_start's new recall_prior_packet=True path calls it, then builds and returns that prior session's packet via the same build_session_packet() the session_packet MCP tool already used, so the resuming agent gets the prior session's task/delegations/approvals/memory/prior_handoff/lifecycle context without a transcript read.
  - `test` (2026-09-21) — tests/test_lifecycle_handoff.py (task #1121 phase 2) — test_session_start_recalls_the_prior_session_packet: two sessions opened against one task by different agents; the second, opened with recall_prior_packet=True, returns recalled_from_session_id equal to the first session's id and a recalled_packet whose schema/session/task fields match the first (now-completed) session. test_session_start_recall_is_none_without_a_prior_session: a fresh task's first session gets recalled_packet=None, recalled_from_session_id=None rather than an error. test_session_start_recall_is_off_by_default: neither key appears in the result when recall_prior_packet is not passed, confirming no existing caller's return shape changed. All three ran this session: `uv run pytest tests/test_lifecycle_handoff.py -q` -> 13 passed.
  - `live` (2026-09-21) — connection_state not upgraded (task #1121 phase 2, #1067 guard) — This is a real code-level round trip exercised by the new tests above (session A's data really is recalled by session B via the actual storage layer), but it is a local pytest exercise, not a live authenticated hub round trip from a real resuming agent. Per the #1067 guard, connection_state stays 'unknown' rather than being promoted to 'live' on test-level demonstration alone.
- **Boundaries:**
  - security: not-applicable
  - identity: packet is attributed to the capturing session.
  - approval: not-applicable
  - privacy: packet content stored as a memory record, subject to memory_* access rules.
  - release: core.
  - tenant_workspace: scoped to the capturing agent's session.
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / Have a real resuming agent flow (e.g. the /context-restore skill, or a per-agent session-open convention) actually pass recall_prior_packet=True in live use, then record a dated live evidence row from that real round trip before considering connection_state=live. / do not upgrade connection_state to live without a fresh, dated, live capture-and-recall round trip from a real (non-test) caller.
- **Wedge:** evidence-wedge

#### `mem-gbrain-boundary` — Gbrain continuity boundary (deliberately no in-repo adapter)

- **Customer purpose:** Documents that gbrain is a separate knowledge store with its own lane, so no one accidentally builds a second correlation database inside unified-agent.
- **Origin:** decision — #1060
- **Implementation state:** dormant/uncertain
- **Consumer:** no verified consumer
- **Entry point:** src/unified_agent/monitor.py (`not-applicable`) — monitor.py references the gbrain service by name only for anomaly monitoring (ANOMALY_SERVICES_DEFAULT); no UA-to-gbrain adapter or data-sync module exists in this repo — verified by grep across src/unified_agent for 'gbrain', which returned only this one file.
- **Connection state:** not-applicable
- **Evidence:**
  - `code` — src/unified_agent/monitor.py:68,253,440,657 — Only in-repo references to gbrain: an anomaly-monitoring service name, not an adapter.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: keeping the stores unconnected is itself the boundary — no unified-agent data flows into gbrain automatically.
  - release: not-applicable — gbrain runs and is managed as its own service outside this repo.
  - tenant_workspace: not-applicable
- **Incomplete (owner / decision / next action / non-action):** unassigned / #1060 — keep gbrain separate; correlation report only if a real operator failure appears. / none — gbrain keeps its own lane by design. / do not add an in-repo gbrain store, adapter, or correlation database.
- **Wedge:** expansion-slice

## Browser boundary (`browser-boundary`)

Browser as an explicitly separate project: the receiving-half projection contract, its current adapter status, and the refusal to imply a customer capability that does not exist yet.

| ID | Capability | State | Connection | Entry point | Consumer | Wedge |
|---|---|---|---|---|---|---|
| `exc-projection-receiving-half` | Browser projection receiver (manifest-only, no verified consumer) | partially-wired | dormant | `browser_derive_task_reference` | no verified consumer | expansion-slice |
| `exc-boundary-isolation` | Browser project isolation (no implicit customer capability) | shipped-and-used | not-applicable | `docs/browser-projection-contract-v1.md` | unified-agent product boundary itself (enforced by contract and docs, not a callable tool) | evidence-wedge |

### Detail

#### `exc-projection-receiving-half` — Browser projection receiver (manifest-only, no verified consumer)

- **Customer purpose:** Provides a default-off, fail-closed receiver seam for Browser focus-anchor projections. It validates and derives a task reference without persistence and rejects Horizon 2 registration/authorization; no verified Browser client is connected.
- **Origin:** task — #1045, #1046, #1047, #1048, #1312
- **Implementation state:** partially-wired
- **Consumer:** no verified consumer
- **Entry point:** browser_derive_task_reference (`mcp-tool`) — Profile-restricted wrapper revalidates a focus_anchor, verifies an existing non-terminal task, and derives a task_reference without persistence. Its manifest is default-off and rejects Horizon 2 registration and authorization; no authenticated Browser client is verified to invoke it.
- **Connection state:** dormant
- **Evidence:**
  - `code` — src/unified_agent/projection/browser_v1.py:7 — Pure validator module; it parses untrusted records and performs no storage or transport.
  - `code` — src/unified_agent/tools/projection.py:22 — browser_derive_task_reference(record, task_id, confidence) definition.
  - `code` — docs/browser-projection-contract-v1.md — Contract documentation.
  - `test` — tests/test_browser_projection.py, tests/test_browser_projection_tool.py — Projection contract and tool coverage.
  - `test` — tests/connectors/test_browser_projection_conformance.py — Default-off manifest resolves, appears in the Connectors package projection, reports DORMANT without active registration, and remains profile-restricted.
- **Boundaries:**
  - security: fails closed on any input that doesn't validate against the contract.
  - identity: MCP profile filtering limits the existing receiver tool to steward and chief-of-staff; no Browser service identity is wired to call it.
  - approval: not-applicable
  - privacy: private topology, credentials, and prompts are excluded from any customer-facing projection by contract.
  - release: default-off manifest and derive wrapper reject Horizon 2 registration and authorization; doctor remains DORMANT without a real transport or verified live producer-to-receiver call path.
  - tenant_workspace: not-applicable — Browser is a separate project boundary.
- **Incomplete (owner / decision / next action / non-action):** unassigned / — / name and authorize a specific authenticated Browser client to invoke the receiver, then verify that call before enabling or registering a live connection. / do not add projection persistence, autonomous task writes, a new transport, or a customer-facing Browser capability without a separately approved consumer and security boundary.
- **Wedge:** expansion-slice

#### `exc-boundary-isolation` — Browser project isolation (no implicit customer capability)

- **Customer purpose:** Guarantees Browser stays a separate project so its private topology, credentials, and prompts never leak into a customer-facing unified-agent surface by accident.
- **Origin:** decision — #1064
- **Implementation state:** shipped-and-used
- **Consumer:** unified-agent product boundary itself (enforced by contract and docs, not a callable tool)
- **Entry point:** docs/browser-projection-contract-v1.md (`not-applicable`) — Enforced by the fail-closed contract (exc-projection-receiving-half) plus explicit docs, not a runtime toggle.
- **Connection state:** not-applicable
- **Evidence:**
  - `code` — src/unified_agent/projection/browser_v1.py, docs/browser-projection-contract-v1.md — Contract and docs establishing the isolation boundary.
- **Boundaries:**
  - security: not-applicable
  - identity: not-applicable
  - approval: not-applicable
  - privacy: the entire point of this item — private topology/credentials/prompts stay out of customer-facing projections.
  - release: not-applicable — this is a boundary decision, not a shippable feature.
  - tenant_workspace: not-applicable
- **Wedge:** evidence-wedge

## Queue coverage (no duplicate work filed)

| Task ref | Title | Status | Coverage note | Decision |
|---|---|---|---|---|
| #935 | SDK v0.3 expansion lane | open | covered by int-sdk-conformance | no duplicate filed |
| #944 | SDK/accountability expansion — tracking; typed object consumer delivered | open | covered by int-sdk-conformance and gov-evidence-reports | no duplicate filed |
| #1024 | Phase 3 - Pre-YC product: policy engine, workflow connector, YC app | open | live queue row as of 2026-09-19 (created 2026-09-15 — pre-dates the check that recorded it absent); parent of the #1032 connector pilot | no duplicate filed |
| #1032 | Pilot workflow connector for a selected customer workflow | blocked | covered by run-horizon2-connectors | no duplicate filed; connector pilot stays #1032 |
| #1043 | GCP worker self-healing via approval-gated autonomous cycle | blocked | covered by int-gcp-agent20 | no duplicate filed |
| #1044 | Control Center GCP health, instance, and budget panel | open | covered by int-gcp-agent20 and tel-utilization-views | no duplicate filed |
| #1045 | Browser SDK: validate and prototype governed context interface | open | covered by exc-projection-receiving-half | no duplicate filed |
| #1046 | Browser SDK: define core contract, types, and governance invariants | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1047 | Browser SDK: implement minimal core and existing-store adapter seam | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1048 | Browser SDK: prove one Workroom/Sample_App_B integration use case | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1050 | Browser expansion: research, plan, and ship next verified vertical slice | open | covered by exc-projection-receiving-half | no duplicate filed |
| #1051 | Research Browser expansion candidates and recommend one vertical slice | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1052 | Plan the recommended Browser vertical slice against existing seams | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1053 | Implement the approved Browser vertical slice with runtime evidence | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1054 | Independently verify Browser expansion artifact and runtime behavior | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1055 | Audit Browser expansion capacity, subscriptions, and usage meters | done | covered by exc-projection-receiving-half | no duplicate filed |
| #1056 | Unified Agent integration closure: audit implementations, emulations, and user surfaces | done | source evidence for this catalog | cited, not duplicated |
| #1063 | Fix stale SessionsPane empty activity-feed accessibility test | done | live queue row as of 2026-09-19; filed 2026-09-17 07:38Z — after that day's 05:47Z freshness check — and completed the same day (9c1aac4) | no duplicate filed |
| #1061 | Enterprise agent evidence and control product specification — capability inventory, anti-orphaning, and first consumer slice | done | origin document for this catalog's evidence-wedge framing | cited, not duplicated |
| #1064 | Enterprise capability discoverability follow-up — catalog, consumer surfaces, and verification loop | open | this catalog is a direct deliverable toward #1064 | no duplicate filed |
| #1065 | Build the evidence-backed Unified-Agent capability catalog and ledger | done | this catalog is the deliverable for #1065 | no duplicate filed |
| #1003 | OAuth without Grok: integrate preserved candidate d45a (client_credentials + revocation cascade + public-host identity pinning) | done | covered by int-oauth-boundary; owner agent-claude | do not dispatch |
| #885 | Initiative: Modernize Unified Agent Web UI (Wayfinder Map) | open | covered by int-modern-webui | keep open, no duplicate filed |
| #993 | Wayfinder C8: retire /ui/legacy — single-mount cutover PR (gated on C1–C7) | blocked | live queue row as of 2026-09-19 (created 2026-09-13 — pre-dates the 2026-09-17 check that recorded it absent); legacy_ui_index fallback still present in webui/routes.py | no duplicate filed |

## Traceable paths (capability -> consumer -> behavior evidence -> owner/closure)

### `gov-approval-queue`

- **Consumer:** dispatch coordinator and human operator (approval_pending/approval_resolve)
- **Behavior evidence:** tests/test_approvals.py plus doctor snapshot #1057 recording approval gates LIVE for delegate and gcloud_ops (2026-09-17)
- **Owner / closure decision:** human-only resolver by design; #1058 records the decision to keep human approval as the only resolver

### `dsp-task-lifecycle`

- **Consumer:** every agent plus the Web UI Work surfaces
- **Behavior evidence:** tests/test_task_claim.py, tests/test_task_claim_authority.py, and live hub task_list on 2026-09-17 showing claims/lineage across #935-#1065
- **Owner / closure decision:** shipped-and-used; owner is the shared hub core

### `lgp-engine-checkpoints`

- **Consumer:** langgraph_workflow_run (plan -> build -> verify -> merge) and the Web UI Activity > LangGraph view
- **Behavior evidence:** tests/test_tools_langgraph.py and tests/test_langgraph_pilot.py, plus doctor snapshot #1057 recording LangGraph LIVE at v1.2.11 with SQLite checkpoints
- **Owner / closure decision:** shipped-and-used

### `prq-quota-status`

- **Consumer:** no verified consumer (model_quota_status returns last-known/unknown)
- **Behavior evidence:** src/unified_agent/credential_pool.py:120, tests/test_credential_pool.py, and #1057's note that provider quota data is incomplete and unknown is the honest answer
- **Owner / closure decision:** open; owner is the #1060 P1 lane pending a controlled rehearsal

### `fm-supervision`

- **Consumer:** COS briefing/cadence consumers via drain_supervisor_wakes
- **Behavior evidence:** tests/test_supervision.py, tests/test_dispatch_supervision.py, and doctor snapshot #1057 recording the event bus LIVE
- **Owner / closure decision:** shipped-and-used

### `exc-projection-receiving-half`

- **Consumer:** no verified Browser client; profile-restricted MCP wrapper and default-off manifest only
- **Behavior evidence:** tests/connectors/test_browser_projection_conformance.py verifies manifest-only package visibility, DORMANT doctor status, and profile restrictions; src/unified_agent/tools/projection.py:22 validates and derives without persistence
- **Owner / closure decision:** open; next action is to name and authorize an authenticated Browser client before enabling a live connection
