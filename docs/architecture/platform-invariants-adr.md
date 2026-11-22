---
title: ADR — Platform invariants, enforced now vs deferred
date: 2026-09-28
author: agent-claude
status: proposed
tags: [adr, architecture, invariants, unified-agent]
project: unified-agent
task: 1439
---

# ADR: Platform invariants, enforced now vs deferred

## Decision

The 18 platform invariants proposed in the 2026-09-25 Executive Architecture
Review (vault `00 - Inbox`, agent-codex) are split into two classes:

- **Enforced now.** The claim is true today, and a named test (or CI check)
  fails if it stops being true. A claim without a citation is not in this class.
- **Deferred.** Not built, or built only on a path that does not carry real
  work. Each deferred item carries a **quantified trigger**: a number and the
  measurement that feeds it. When the trigger fires, the item becomes funded
  work. Until then it is a known gap, not a promise.

Several invariants are partly true. Those are split into lettered clauses
(`3a`, `3b`) so that each clause gets exactly one classification. The review's
wording is kept. This ADR does not change it.

The list is **not a funded programme** (see the 2026-09-26 supersede banner on
the review). This ADR builds nothing. It adds no object, no table, no policy
implementation and no dependency. It also files no task for any deferred item.
A trigger that fires is filed as a task at that point, through the normal queue.

## Evidence base

- The review's invariant list. It is framing, not measurement.
- `docs/execution-substrate-survey.md` (#1432, PR #799). This is measured
  fact: 3 structural schedulers and 3 run ledgers keyed on `task_id`, all of
  them idle. About 330 tasks were completed through self-pickup between
  2026-09-15 and 2026-09-26, and none of them appears in any run ledger.
- The test suite at `origin/main` `6aa2dc7f`. Every test cited below was
  collected by `pytest --collect-only` on that revision (see *Verification*).

One survey fact changes how most rows read. **The approved-dispatch path
(`dispatch/`, "path A") carries a strong, well-tested contract, but it carries
no traffic.** It last ran on 2026-08-27, and #1435 retires it. Its tests prove
the design can be built. They do not prove that real work is protected. Where
the only proof comes from path A, the clause is marked *enforced (path A only)*,
and the self-pickup remainder is deferred.

## Summary

| # | Invariant | Class |
|---|---|---|
| 1 | One authoritative identity and policy decision per action | Enforced now |
| 2 | Least authority follows the work | Deferred |
| 3a | Approvals bind to tool, exact payload, identity, expiry, one use | Enforced now |
| 3b | Approvals bind to policy version, purpose, target resource | Deferred |
| 4a | Fail closed on unknown identity, approval or policy state | Enforced now |
| 4b | Fail closed on an expired lease | Deferred |
| 5 | Untrusted content cannot change control policy | Enforced now |
| 6a | Credentials and secrets are tenant-scoped | Enforced now |
| 6b | Storage queries and execution are tenant-partitioned | Deferred |
| 7a | Claims are atomic compare-and-set, one owner | Enforced now |
| 7b | Fencing generations; every path goes through the claim | Deferred |
| 8a | Transitions record actor and durable result, idempotently | Enforced now |
| 8b | Invalid task transitions are rejected | Deferred |
| 9a | Dispatch is recoverable (path A, executor restart) | Enforced now (path A only) |
| 9b | Recoverable execution for self-pickup | Deferred |
| 10a | Cancellation is a governed transition (path A, cycles) | Enforced now (path A only) |
| 10b | One governed cancel across every execution path | Deferred |
| 11a | Uncertain side effects are never replayed (governed graph) | Enforced now |
| 11b | Every retry is a distinguishable attempt on the live path | Deferred |
| 12a | Marked code tasks cannot close without evidence or review | Enforced now |
| 12b | Every task's completion satisfies its acceptance conditions | Deferred |
| 13a | The evidence ledger is append-only and tamper-evident | Enforced now |
| 13b | Evidence binds to assignment, run and runtime | Deferred |
| 14 | Consequential actions have an explainable decision trace | Deferred |
| 15a | Hub-brokered LLM spend has a fail-closed daily cap | Enforced now |
| 15b | Budgets follow assignments and aggregate to goals | Deferred |
| 16 | Health distinguishes live, safe and complete | Enforced now |
| 17 | Events and views reconcile from authoritative state | Enforced now |
| 18a | Connectors have explicit enablement and revocation | Enforced now |
| 18b | Credentials are excluded from all prompts and logs | Deferred |

Totals: 18 invariants in 30 clauses. 16 clauses are enforced now, and 2 of
those only on path A. 14 clauses are deferred. Invariants 2 and 14 are wholly
deferred.

## Authority and security

### 1. One authoritative identity and policy decision per action — ENFORCED NOW

The hub resolves a bearer token or a sealed WorkOS session to one identity for
each request, then binds that identity's workspace. A connected transport alone
is refused when enforcement is on. The deployed hub runs enforced, and
`system_doctor` reports it.

- `tests/test_hub_auth.py::TestHubAuth::test_enforce_mode_rejects_unauthorized`
- `tests/test_hub_auth.py::…::test_swapped_token_does_not_grant_the_other_identitys_workspace`
- `tests/test_hub_auth.py::…::test_unbound_laptop_identity_fails_closed_not_desktop_fallback`
- `tests/test_hub_auth.py::…::test_the_session_is_revalidated_on_every_request`
- `tests/test_approvals.py::…::test_verified_identity_is_recorded_over_caller_value`
- `tests/test_doctor.py::…::test_live_enforced` (the deployment check)

Scope note: with no tokens configured, the middleware passes requests through
(`test_no_tokens_configured_passes_through`). That is the local-development
mode. `system_doctor` reports it as not enforced, so the gap is visible rather
than silent. The tenant part of "identity, workspace/tenant" is covered by 6a
and 6b.

### 2. Least authority follows the work — DEFERRED

Authority attaches to an **identity profile**, not to an assignment. A
delegation (`tools/delegate.py`) carries a spec, a deadline and a callback, but
no scopes, tool list, data boundary, budget or expiry. So there is nothing for a
child delegation to narrow. The profile tests (`tests/test_profiles.py`) prove
per-identity limits. They say nothing about per-assignment limits.

**Trigger.** Build it when **agent-to-agent delegations exceed 10 in any 7-day
window**, or when **any delegation targets an identity whose profile is broader
than the delegator's**, whichever happens first. Measurement: `delegate_list`
/ the delegation ledger (`delegation_ledger_log.py`), counting rows where both
the delegator and the delegate are agent labels, joined to `profiles.py` to
compare profiles.

### 3. Approvals bind to what they approve

#### 3a. Tool, exact payload, verified identity, expiry, one use — ENFORCED NOW

An approval row binds to its tool and its exact payload. Any change to the
payload is a different request. A pass is consumed once under compare-and-swap.
It expires, and it can be revoked. The resolving identity is the verified one,
not the value the caller claims.

- `tests/test_approvals.py::…::test_approval_matches_exact_payload_only`
- `tests/test_approvals.py::…::test_approve_is_one_shot_pass`
- `tests/test_approvals.py::test_two_consumers_have_one_cas_winner`
- `tests/test_approvals.py::…::test_expired_pass_does_not_authorize_the_call`
- `tests/test_approvals.py::…::test_revoked_pass_does_not_authorize_the_call`
- `tests/test_horizon2_tools.py::test_connector_authorization_uses_canonical_bound_approval`

#### 3b. Policy version, purpose, explicit target resource — DEFERRED

The `approvals` table (`storage.py`) has no policy-version, purpose or
target-resource column. The target is implied by the payload. A policy change
therefore does not invalidate an outstanding pass.

**Trigger.** Build it when **2 or more distinct human identities resolve
approvals within 30 days**, or when **policy files change more than twice in 30
days while a pass is pending**. The policy files are `policy.py`, `profiles.py`
and `org.yaml`. Measurements: `SELECT COUNT(DISTINCT resolved_by) FROM
approvals` over the window, and `git log --since=30.days` on those files, joined
to `approval_pending`.

### 4. Fail closed on unknown authority or policy state

#### 4a. Unknown identity, approval or policy state — ENFORCED NOW

- `tests/test_hub_auth.py::…::test_a_mapping_to_an_unknown_label_is_refused_at_the_middleware_too`
- `tests/test_hub_auth.py::…::test_a_leg_that_fails_to_build_does_not_open_the_hub`
- `tests/test_approvals.py::…::test_undateable_pass_fails_closed`
- `tests/test_approvals.py::…::test_misconfigured_ttl_falls_back_rather_than_disabling_expiry`
- `tests/test_dispatch_coordinator.py::test_unknown_agent_refused`
- `tests/test_dispatch_coordinator.py::test_malformed_approval_marker_refused`
- `tests/test_managed_secrets_tenancy.py::test_unbound_read_fails_closed_when_a_tenant_holds_the_name`

#### 4b. Expired lease — DEFERRED

On the self-pickup path, an expired `lock_expires_at` makes a task
*reclaimable*. It does not stop the previous holder from writing. Without a
fencing generation (7b), a stale worker can still update the task. This clause
shares 7b's trigger and is built together with it.

### 5. Untrusted content cannot change control policy — ENFORCED NOW

Text inside task specs, tickets, skills and notes is data. An approval marker
written into a spec does not authorize anything. Untrusted tasks are
quarantined. Injection-shaped content becomes a finding, and it is not executed.

- `tests/test_dispatch_coordinator.py::test_marker_embedded_in_task_spec_does_not_authorize`
- `tests/test_executor.py::test_untrusted_task_quarantined_under_stage_2`
- `tests/test_bug_worker.py::test_prompt_injection_in_ticket_rejected`
- `tests/test_audit_surfaces.py::…::test_injection_in_scanned_skill_is_a_finding_not_executed`
- `tests/test_evolve.py::test_injection_and_secret_shaped_notes_are_dropped`
- `tests/test_browser_projection_tool.py::test_rejects_an_untrusted_record_that_fails_the_contract`

### 6. Tenant and workspace boundaries

#### 6a. Credentials and secrets are tenant-scoped — ENFORCED NOW

- `tests/test_managed_secrets_tenancy.py::test_tenant_cannot_read_another_tenants_or_root_secret`
- `tests/test_managed_secrets_tenancy.py::test_rotate_and_revoke_are_tenant_scoped`
- `tests/test_managed_secrets_tenancy.py::…::test_enforced_tenant_cannot_write_another_org`
- `tests/test_llm_broker.py::test_tenant_key_is_read_only_from_its_own_org`
- Workspace binding per identity: `tests/test_hub_auth.py::…::test_mapped_agent_gets_its_own_workspace`

#### 6b. Storage queries and execution are partitioned — DEFERRED

`org_id` is stamped on writes under the enterprise edition
(`tests/test_org_id_tenant_boundary.py`). But reads do not filter by it, and it
is null on the private hub. Stamping a column is the review's own counter-example:
"a tenant ID in an API parameter is not isolation". #1433 (org_id as the
partition key) is the in-flight work.

**Trigger.** Build it when **the hub holds 2 or more organizations with
`UA_EDITION=enterprise`**, which means the first external tenant on a shared
hub. Measurement: `org_registry.py` (the count of configured orgs) and the
`edition` doctor row. No second tenant may be onboarded before this clause is
enforced.

## Coordination correctness

### 7. At most one valid owner per assignment

#### 7a. Claims are atomic compare-and-set — ENFORCED NOW

`task_claim` sets the owner with one conditional `UPDATE`. The condition is that
there is no holder, the caller is the holder, or the lease has lapsed. Another
agent's active claim cannot be released through `task_update`.

- `tests/test_task_claim.py::test_task_claim_locked_by_other`
- `tests/test_task_claim_authority.py::test_task_update_cannot_release_an_active_claim_owned_by_other_agent`
- `tests/test_profiles.py::test_steward_cannot_update_a_task_claimed_by_another_agent`
- `tests/test_executor.py::test_atomic_claim_race_condition`
- `tests/test_dispatch_coordinator.py::test_duplicate_poll_does_not_double_launch_under_contention`

#### 7b. Fencing generations; every path goes through the claim — DEFERRED

There is no fencing generation anywhere in `src/`. A task can also reach
`doing` through generic `task_update` without writing `claimed_by`. That is by
design (`tests/test_task_claim_authority.py::test_task_update_does_not_synthesize_claim`),
and the survey observed it happening. So "one owner" holds only for agents that
use `task_claim`.

**Trigger.** Build it when **a finish is recorded by an identity other than the
current claimant at least once in any 30-day window**, or when **the fleet runs
more than 2 concurrent execution slots** (the current WIP limit in
`NEXT-WORK.md`). Measurement: the self-pickup run record from #1436 (claim and
finish identities for each run), plus the count of concurrent `doing` rows with
a live claim from `task_list(status="doing")`. Clause 4b shares this trigger.

### 8. Every state transition has an actor, precondition and durable result

#### 8a. Actor and durable result, atomically and idempotently — ENFORCED NOW

A task transition writes its event and receipt in the same transaction as the
row. If the event fails, the mutation rolls back. A repeated idempotency key
yields one transition.

- `tests/test_task_atomicity.py::test_transition_event_and_receipt_commit_together`
- `tests/test_task_atomicity.py::test_event_failure_rolls_back_task_and_receipt`
- `tests/test_task_atomicity.py::test_event_dedupe_collision_rolls_back_un_evidenced_mutation`
- `tests/test_task_atomicity.py::test_concurrent_same_key_has_one_transition_and_receipt`
- `tests/test_autonomous_cycles.py::test_cycle_transitions_are_bounded_and_idempotent`
- `tests/test_autonomous_cycles.py::test_cycle_rejects_stale_writer_and_identity_mismatch`

#### 8b. Invalid task transitions are rejected — DEFERRED

Path A has an explicit state machine
(`docs/architecture/approved-dispatch-state-machine.md`), and so do autonomous
cycles. The task table does not. It guards specific edges (completion
evidence, terminal `completed_at`, claims). It has no allowed-edge table, so an
edge such as `done → doing` is accepted.

**Trigger.** Build it when **5 or more task transitions in any 30-day window
leave a terminal status (`done`/`cancelled`) for an active one**. Measurement:
the durable `event_log` rows on channel `agent.task.updated` whose payload
carries `from_status` in (`done`, `cancelled`) and an active `status`. These
rows commit in the same transaction as the task row (8a), so none can go
missing. The best-effort `agent_events` activity index can, so it is not used.

### 9. Dispatch is recoverable across crashes

#### 9a. Path A and executor restart — ENFORCED NOW (path A only)

- `tests/test_dispatch_coordinator.py::test_reconcile_pending_cancellations_finishes_a_crash_left_intent`
- `tests/test_dispatch_coordinator.py::test_stale_lease_reclaim_allows_single_relaunch`
- `tests/test_dispatch_coordinator.py::test_approval_row_survives_launch_failure_for_retry`
- `tests/test_dispatch_e2e_smoke.py::test_e2e_duplicate_ticks_do_not_double_launch`
- `tests/test_executor.py::test_crash_recovery_reclaims_expired_locks`
- `tests/test_executor.py::…::test_restart_recovers_an_orphaned_in_flight_delegation`

#### 9b. Recoverable execution for self-pickup — DEFERRED

Self-pickup is the path that carries the work. It has no dispatch intent and no
run record, so a crashed agent leaves its task in `doing` until the claim lapses
(survey §3b, "abandonment is invisible"). #1436 records the run. Reconciling a
crashed run comes after that.

**Trigger.** Build it when **#1436's run records show 3 or more abandoned runs
(claimed and never finished) in any 7-day window**. Measurement: runs from
#1436 with no finish, together with `task_list(stale_claims=True)`.

### 10. Cancellation is a governed transition

#### 10a. Path A and autonomous cycles — ENFORCED NOW (path A only)

- `tests/test_dispatch_coordinator.py::test_cancel_run_is_atomic_idempotent_and_identity_checked`
- `tests/test_dispatch_coordinator.py::test_cancel_run_refused_or_mismatched_termination_is_not_confirmed_cessation`
- `tests/test_dispatch_coordinator.py::test_unconfirmed_cancellation_blocks_sibling_claim_and_resume`
- `tests/test_executor.py::…::test_killed_cycle_blocks_execution_without_running_the_backend`

#### 10b. One governed cancel across every path — DEFERRED

`dispatch_cancel` reaches path A only. The executor uses `killed`, node work
uses `cancel_requested`, and self-pickup has no cancel at all (survey §5). A
human cannot stop a self-pickup agent through the platform.

**Trigger.** Build it when **an operator needs to stop in-flight work
through the platform once and cannot**, or when **self-pickup carries 50 or more
runs a week**, whichever happens first. The second condition already holds in
spirit (about 330 tasks in 12 days), so it fires as soon as #1436's run records
can count it. Measurement: weekly run count from #1436, plus
`crew_intervene`/`dispatch_cancel` calls that return "not reachable".

### 11. Retry creates a distinguishable attempt

#### 11a. Uncertain side effects are never replayed — ENFORCED NOW

- `tests/test_langgraph_dispatch.py::test_governed_attempt_timeout_quarantines_uncertain_outcome`
- `tests/test_dispatch_coordinator.py::test_reclaim_never_promotes_a_pending_cancellation_to_retryable`
- `tests/test_plan.py::…::test_retry_increments_and_escalates_at_cap`

#### 11b. Every retry is a distinguishable attempt on the live path — DEFERRED

`executor_attempts` and `dispatch_runs` number their attempts. Self-pickup does
not: a second agent picking up a task leaves no attempt boundary.

**Trigger.** It follows #1436. Build it when **3 or more tasks in any 7-day window
are claimed by 2 or more identities before they finish**. Measurement: #1436's
run records per task, together with the thrash detector in
`tests/test_claim_renewal.py` (`…::test_two_agents_trading_a_task_is_thrash`).

### 12. Success requires acceptance evidence

#### 12a. Marked code tasks cannot close without evidence or review — ENFORCED NOW

The guarantee is scoped to markers. A task whose notes carry `[code-task]`
cannot reach `done` without an `evidence_ref` or a completion-review marker
(`[review: merged/...]`) already in its notes: a merged PR counts as evidence.
A task carrying a live
`[waiting-for: review]` marker cannot reach `done` without a merged or approved
outcome or `[review: no-merge]`. Code work filed without the markers is not
covered, and falls under 12b.

- `tests/test_task_completion_consistency.py::test_marking_a_code_task_done_without_evidence_is_rejected`
- `tests/test_task_claim_authority.py::test_task_update_cannot_complete_an_unreviewed_task`
- `tests/test_project_status_rollup.py::test_cannot_complete_project_spec_while_active_descendants_exist`
- `tests/test_autonomous_cycles.py::test_acceptance_cycle_reaches_completed_only_after_approved_pr_and_merge`

#### 12b. Every task's completion satisfies its acceptance conditions — DEFERRED

Tasks without the markers close with no evidence reference. That covers docs,
operations and decisions, and also code work filed without `[code-task]`. The
platform stores no machine-checkable acceptance conditions.

**Trigger.** Build it when **10% or more of tasks closed in a 30-day window are
reopened or recorded as regressions**. Measurement: the `event_log`
`agent.task.updated` rows (the same source as 8b). The cohort is every task with
a transition into `done` in the window; the numerator is the subset with a later
terminal→active transition. `task_list(status="done", completed_after=…)` is not
the cohort: reopening a task clears its `completed_at`, so reopened tasks drop
out of that list and the ratio would understate the trigger.

## Accountability and operations

### 13. Evidence binds to the work chain

#### 13a. Append-only, tamper-evident ledger — ENFORCED NOW

- `tests/test_audit_ledger_chain.py::test_sequential_hash_chain_valid`
- `tests/test_audit_ledger_chain.py::test_tamper_payload_detected`
- `tests/test_audit_ledger_chain.py::test_tamper_prev_hash_detected`
- `tests/test_attribution_wedge.py::test_tampered_signature_flips_provenance_false`
- `tests/test_evidence_anchor.py::test_git_one_byte_tamper_in_head_digest_fails`

#### 13b. Evidence binds to assignment, run and runtime — DEFERRED

Events carry task, actor and timestamps. With no run record (survey §3b), they
cannot carry the run, the runtime or the device for self-pickup work.

**Trigger.** Build it when #1436's run record lands and **covers 90% or more of
tasks completed in a 7-day window**. From that point every evidence event must
carry `run_id`. Measurement: completed tasks joined to #1436 run rows.

### 14. Consequential actions have an explainable decision trace — DEFERRED

Parts exist. Approvals record the requester, the verified resolver and a reason
(3a), and `evidence_report` exports the chain. But no single record answers
"which policy applied and why was this allowed". No policy version is stored
(3b), and an ungated tool call records no allow decision.

**Trigger.** Build it when **the first external evidence request arrives**
(Approval Ledger pilot customer or auditor), or when **an operator question of
the form "why was this allowed?" takes more than 15 minutes to answer from
`evidence_report`**. Measurement: pilot requests logged against Epic A/E, and
timed operator drills recorded in the M2 walkthrough (#1462).

### 15. Budgets and resource limits apply before and during work

#### 15a. Hub-brokered LLM spend has a fail-closed daily cap — ENFORCED NOW

- `tests/test_llm_spend.py::test_concurrent_reservations_cannot_exceed_daily_cap`
- `tests/test_llm_spend.py::test_reported_cost_reconciles_and_unknown_cost_keeps_reserve`
- `tests/test_dispatch_quota_gate.py::test_quota_exhausted_lane_refuses_dispatch`
- `tests/test_autonomous_cycles.py::test_budget_exhaustion_is_visible_as_blocked`

#### 15b. Budgets follow assignments and aggregate to goals — DEFERRED

Subscription CLI sessions (the path real work takes) are outside the broker. No
assignment carries a token, cost, time or tool-use ceiling. #1348 (cost ledger)
is the in-flight work, and it depends on #1436.

**Trigger.** Build it when **metered API spend through the hub broker exceeds
$25 on any day**, or when **any single run exceeds 4 hours of wall-clock
time**. Measurements: the `llm_spend` reservation ledger (reported cost per
day) for broker spend, and #1436 run duration for time. Subscription CLI
sessions currently have **no spend measurement at all**. The broker does not
see them, and `system_doctor` reports only provider reachability. Until #1348
records them, the 4-hour time trigger is the only one that covers the path real
work takes. #1348 landing does not fire this trigger; it provides the missing
measurement.

### 16. Health distinguishes live, safe and complete — ENFORCED NOW

`system_doctor` reports LIVE, DORMANT, DEGRADED, MISCONFIGURED or UNKNOWN for
each capability. It checks what a capability *does* rather than whether its
process is up. For example, the executor must actually authenticate, a backup
must pass its integrity check, and a claim stamp does not count as liveness.

- `tests/test_doctor.py::TestActiveExecutorCheck` (authentication, not process presence)
- `tests/test_doctor.py::TestDatabaseBackupCheck::test_live_after_fresh_integrity_checked_backup`
- `tests/test_claim_renewal.py::…::test_claim_stamp_alone_is_not_liveness`
- `tests/test_bootstrap.py::…::test_degraded_fails_normal_and_strict_doctor`

Scope note: "complete" and "customer acceptance" are clause 12. Health does not
claim them.

### 17. Events and views reconcile from authoritative state — ENFORCED NOW

The task row and its event commit together (8a). If publishing after commit
fails, the transition stays. Projections emit only allowlisted fields, and
receivers deduplicate by event id.

- `tests/test_task_atomicity.py::test_post_commit_publish_failure_does_not_undo_transition`
- `tests/test_cloud_mirror.py::test_projection_only_emits_allowed_keys`
- `tests/test_cloud_mirror_receiver.py::test_ingest_dedupes_by_event_id`
- `tests/test_cloud_mirror.py::test_file_sink_dedupes_retries_after_cursor_write_failure`

### 18. Privileged integrations have explicit enablement and revocation

#### 18a. Connector enablement, refresh and revocation — ENFORCED NOW

- `tests/test_horizon2_tools.py::test_revocation_persists_and_emits_audit_event`
- `tests/test_horizon2_tools.py::test_authorization_rejects_unregistered_server_without_consuming_pass`
- `tests/test_connector_oauth.py::test_refresh_rotates_grant_and_invalid_grant_marks_reconnect`
- `tests/test_connector_oauth.py::test_connect_routes_require_auth_and_callback_returns_no_token`
- `tests/test_credential_pool.py::test_secret_material_is_rejected_and_never_persisted`

#### 18b. Credentials are excluded from all prompts and logs — DEFERRED

Redaction exists on specific surfaces: task specs, handoffs and gcloud describe
(`tests/test_dispatch_routing.py::test_bound_task_spec_redacts_secrets`,
`tests/test_execution_graph.py::test_handoff_list_redacts_task_spec_and_callback_payload`).
There is no platform-wide guarantee. The fleet's main guard is a client-side
hook (`.claude/settings.json` in the cockpit), and the hub does not enforce it.

**Trigger.** Build it when **the first customer connector goes live**, or when
**a secret-shaped value is found in any hub log file or exported evidence
bundle at least once**. Measurements: the Horizon 2 registration count for
non-internal connectors, and `security_audit` run over the hub's log directory
and over an exported evidence bundle on disk (it scans files). Nothing
currently scans `event_log` or database rows for secret-shaped values, and
`evidence_report` exports without checking. Building that scanner is part of
18b, so until it exists a leak into the database is unmeasured and the
connector trigger is the operative one.

## Consequences

- Readers can tell a guarantee from a gap. A pitch, spec or review that cites an
  invariant must cite its clause here and must not upgrade a deferred clause.
- **Most deferred clauses depend on #1436.** Clauses 7b, 9b, 10b, 11b, 13b and
  15b are either measured by the self-pickup run record or blocked on it. This
  confirms the survey's finding that recording execution is the prerequisite.
- Path A's contract (7a, 9a, 10a, parts of 4a and 5) is the reference design
  for self-pickup. Retiring the path (#1435) must not delete the tests cited
  here until an equivalent test exists on the surviving path, or until the
  clause is reclassified in this ADR.
- **Maintenance.** When a deferred clause is built, move it to enforced with
  its test citation in the same PR. When a cited test is deleted or renamed,
  update the citation in the same PR, or reclassify the clause as deferred.

## Verification

Every `tests/…::test_…` citation above resolves under
`pytest --collect-only -q` at the revision this ADR merges on. Most citations
use `…` in place of a test class name, and they are matched by file and test
name. This ADR is documentation only and implements no invariant.
