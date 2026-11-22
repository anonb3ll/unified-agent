---
title: Meta-harness capability map
date: 2026-11-10
author: agent-claude
status: proposal (task #2447, epic #2437); no task filed from it yet
tags: [roadmap, architecture, meta-harness, unified-agent]
project: unified-agent
---

# Meta-harness capability map (task #2447)

Epic #2437 splits this repo into a kernel and 15 components
(`docs/architecture/kernel-split-spec.md` on branch
`task/2438-kernel-split-spec`). This map says what each component must become
for the owner's goal of 2026-11-10: "a fully functioning meta harness platform
that allows for organization, coordination, memory and session synchronicity,
handoffs, graph execution and planning", to which he can "add local and cloud
agents". It compares eight capabilities with the reference projects and keeps
two columns apart: what exists in code, and what works on the hub.

## What "working" means

One operator with local and cloud agents has a working meta harness when, on a
normal day, these six things are true and can be seen without reading code.
He adds an agent, local or cloud, with one command; within five minutes it
shows a heartbeat and an office in the org view, and he can remove it with one
command that revokes its credential. Work he files reaches an owner without his
routing: a free agent claims it, and a message or a closed blocker wakes the
agent it concerns. A session that starts, local or cloud, any harness, opens
with the task, the last handoff and the memory that matter to it, and the
receiver of a handoff can check the claimed state (branch, head, test result)
before acting on it. A goal becomes a plan he approves and then tasks with
dependencies, and the tasks run in dependency order without his routing. When
an agent dies or runs out of quota, its task is handed to another, and nothing
is lost.

## How state on the hub was measured

All hub readings were taken on 2026-11-10 between 12:58 and 13:20 UTC from
`hub-host.local` at hub commit `7010e09a`.

- **doctor**: `system_doctor()`, summary 49 LIVE, 28 DORMANT, 0 degraded, 0
  misconfigured, 1 UNKNOWN (`active_executor_service`: auth probe skipped).
- **perf24 / perf30**: `observability_manage(action="performance")` with the
  default 24 h window, and with `params={"since_hours": 720}`. Calls are
  sampled by the `instrumented` wrapper (`src/unified_agent/monitor.py:1631`).
  Errors are always recorded, but OK samples can be excluded per tool
  (`storage.should_record_perf_sample`, `src/unified_agent/storage.py:1155`).
  `task_list` shows 13 calls, all errors, so its OK calls are almost certainly
  excluded on the hub. **A tool missing from perf30 is therefore "no recorded
  call", not "never called".** The `session_*` tools are absent from perf30,
  yet doctor's `session_owed` reports 36 sessions in 7 days.
- **events**: `event_log_query(channel_prefix=...)`, giving
  `source_event_count` and `last_event_at` per channel.

"Live" means doctor says LIVE or perf30/events show use in the last 7 days.
"Dormant" means doctor says DORMANT. "Built, unused" means code is present
with no recorded call or event in 30 days.

## Reference projects

Note `1f006c0b3087` (north star, 2026-10-01) names Paperclip, t3code,
Hindsight, OpenRig and FirstMate. **It does not name crewAI, Clowder or
supermemory.** I took them to be the following. **The owner should confirm
these choices.**

| Name | Repository taken | Why |
|---|---|---|
| crewAI | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | Listed in vault `Wiki/Research/Multi-Agent-Harness-Recommendations-2026-10-07.md`; the only crewAI. |
| Clowder | [zts212653/clowder-ai](https://github.com/zts212653/clowder-ai) | Vault `Wiki/Coordination-Build-Plan-2386.md` records the owner's 2026-11-09 instruction to "emulate the coordination capabilities" of clowder-ai (epic #2386). Others with the name (RedHatInsights/clowder, a Kubernetes operator; clowder-framework/clowder, a research data manager; 5hubham5ingh/clowder, a terminal panel) are not agent platforms. |
| supermemory | [supermemoryai/supermemory](https://github.com/supermemoryai/supermemory) | Compared in `docs/designs/memory-evaluation-1458.md` and `docs/memory-tasks-vault.md:400`. |
| Paperclip | [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | From the note. |

Pages read for this map (all fetched 2026-11-10): the Paperclip
[README](https://github.com/paperclipai/paperclip/blob/master/README.md); the
crewAI concept pages
[memory](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/memory.mdx),
[planning](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/planning.mdx),
[processes](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/processes.mdx),
[flows](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/flows.mdx),
[checkpointing](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/checkpointing.mdx)
and [collaboration](https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/collaboration.mdx);
the clowder-ai [README](https://github.com/zts212653/clowder-ai/blob/main/README.md)
and feature specs [F002](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F002-agent-to-agent.md),
[F003](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F003-hindsight-lite.md),
[F048](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F048-restart-recovery.md),
[F050](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F050-a2a-external-agent-onboarding.md),
[F064](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F064-a2a-exit-check.md)
and [F065](https://github.com/zts212653/clowder-ai/blob/main/docs/features/F065-session-continuity.md);
and the supermemory [README](https://github.com/supermemoryai/supermemory/blob/main/README.md).
Hindsight, OpenRig, FirstMate and t3code were not re-read for this pass. Their
mechanisms appear only where an earlier repo doc already adopted them, and
that doc is cited.

Short names used in the tables: **PC** = Paperclip README, **CR-x** = crewAI
concept page x, **CL** = clowder-ai README, **CL-Fnnn** = clowder-ai feature
spec, **SM** = supermemory README. Components are the 16 names fixed by the
kernel split spec.

## 1. Organization (org, roles, offices, reporting lines, budgets)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| Org chart: roles, titles, reporting lines, job descriptions (PC "Org Chart"; "Org Chart & Agents") | The only reference with a reporting line as data; crewAI and Clowder only have per-agent roles | Offices with `lead`, `members` and `decision_rights` in `src/unified_agent/org.yaml` (starter) and the installation chart at `UA_ORG_REGISTRY_PATH`, read by `src/unified_agent/org_registry.py` (231 lines) | **Live (read side)**: doctor `org_registry` LIVE, "9 offices match the configured and active agent roster". No `agent.org*` events ever, so no change has been recorded through a tool | No office-to-office or agent-to-lead edge; the chart is a file, not an object with history | kernel | T1: add `reports_to` to offices, serve it through `object_model.py`, check: `tests/test_object_model.py` serves an org tree and doctor rejects a cycle |
| Scoped budgets with warning threshold and hard stop, by company, agent, project and goal (PC "Budget & Cost Control") | Only reference that stops work at a limit; crewAI tracks tokens only | `src/unified_agent/budgets.py` (721 lines, rollups, warning and hard stop), `src/unified_agent/budget_policy.py` (146); read through `observability_manage` `budget_status` / `cost_rollup` | **Built, unused, as far as can be seen**: `agent.budget*` has 0 events. perf30 records `observability_manage` 26 times but does not split by action. I could not establish whether any budget is configured | Spend comes from `run_usage` that agents self-report on `task_update`; no budget has been seen to stop anything | telemetry | T2: configure one per-agent budget and prove the stop, check: a real-SQLite test where a claim past the hard stop is refused, and `budget_status` on the hub lists ≥1 budget |
| Many organizations in one deployment (PC "Multi-Organization") | Clean tenant boundary | `src/unified_agent/org_partition.py`, `tenant_tables.py` (1,416 lines together) | **Dormant**: doctor `tenant_consistency` DORMANT, "tenant boundary off on the hub" | The owner runs one org | auth | None; see section 5 |
| Self-organization: agents propose org changes, a human applies (PC roadmap item; owner ruling #1431 in vault `Wiki/Paperclip-Parity.md`) | Ruled by the owner, 2026-09-26 | `src/unified_agent/self_organization.py` (819 lines), tool `org_proposal` | **Built, unused**: not in perf30; 0 `agent.org*` events | None in code; nobody proposes | kernel | None until T1 lands |

## 2. Adding an agent, local and cloud (enrolment, identity, credentials, first heartbeat, removal)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| "If it can receive a heartbeat, it's hired": agent API keys plus short-lived run JWTs (PC "Bring Your Own Agent"; "Identity & Access") | Adapter-neutral enrolment: one credential, one heartbeat, any runtime | Per-agent bearers in `UA_AGENT_TOKENS`; issue and rotate in `src/unified_agent/tools/token_lifecycle.py` (672 lines); per-session credentials in `src/unified_agent/session_credentials.py` (1,012) | **Auth live, enrolment path unused**: doctor `hub_auth` LIVE (26 tokens, enforced); `agent_token_lifecycle` DORMANT, "no agent_tokens metadata recorded yet (tokens issued outside agent_token_issue/rotate)" | All 26 live tokens were issued by hand, so issue date, owner and expiry are unknown for every one | auth | T3: `unified-agent agent add <label> --office <id>` issues a token, adds the roster label and office seat, and waits for the first heartbeat. Check: an e2e test (add, heartbeat, remove, refused call), and on the hub `agent_token_lifecycle` LIVE |
| External agent onboarding contract with two lanes: L1 CLI adapter, L2 A2A protocol; proved by four real agents (CL-F050) | A written contract an outside agent must meet; Paperclip only lists adapters | ua-node connects out, registers, heartbeats and long-polls: `src/unified_agent/node_queue.py` (3,570), `node_agent.py` (2,101), `node_registry.py` (703); ACP client in `acp_client.py` / `acp_backend.py` | **Live**: doctor `fleet_nodes` LIVE (Desktop, Mac hub, ua-worker; Mac laptop unreported); `agent.node` 448 events, last 2026-11-10 10:53 | No conformance test a new harness must pass; support is in `docs/harness-control-matrix.md`, which is prose | node | T4: an enrolment conformance suite (register, heartbeat, claim, message, handoff, release), run once per harness. Check: a matrix generated from the suite replaces the prose matrix |
| Sandbox providers run agents beyond the local host (PC "Workspaces & Runtime") | Cloud execution is a first-class place, not a separate product | `src/unified_agent/hosted_execution.py`, `cloud_mirror.py`; cloud sessions get a throwaway stdio hub (`CLAUDE.md`, "Cloud sessions") | **Dormant**: doctor `execution_locality` DORMANT ("local (default)"), `connector:cloud-mirror` DORMANT (no Horizon 2 registration) | **No cloud agent is a hub member.** A Claude Code web session cannot reach the hub (tailnet only) and works against a fixture DB | cloud | T5: run ua-node in one cloud VM, enrolled over an authenticated public endpoint. Check: doctor `fleet_nodes` shows a cloud node live, and it claims, works and closes one task end to end |
| Pause, resume, terminate (PC "Governance & Approvals") | The same verbs as ours; no reference adds credential revocation on retire | `src/unified_agent/agent_lifecycle.py` (1,039); `token_lifecycle.revoke_for_agent` (`tools/token_lifecycle.py:84`) | **Built, rarely used**: `agent.lifecycle` 5 events, last 2026-10-01; doctor `retired_agent_credentials` LIVE ("no retired agents") | Removal does not delete a hand-issued token (see the first row) | auth | Covered by T3 |

## 3. Coordination (queue, claims, messages, mentions, wake)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| Atomic task checkout with execution locks (PC "Why Paperclip is special") | The common primitive; Paperclip states it most precisely | `task_claim` / `task_update` in `src/unified_agent/tools/tasks.py` (3,955 lines); `claim_release.py`, `lease_reaper.py`, `queue_hygiene.py` (779) | **Live**: perf30 `task_claim` 260 (2 errors), `task_update` 560 (4 errors), `task_add` 211; doctor `stale_task_locks` LIVE | The queue grows faster than it closes: doctor `queue_closure` "open 223, parked 50, stale 83 (>= 30d)" | kernel | T6: a weekly closure pass proposes closing stale tasks to the owner. Check: doctor `queue_closure` stale count under 20 for 4 weeks |
| DB-backed wakeup queue with coalescing; agents wake for assigned work, follow-ups and schedules (PC "Heartbeats"; "Heartbeat Execution") | Wake is the default, not opt-in | `src/unified_agent/messaging/wake.py` (868), opt-in with `UA_WAKE=1`; supervisor wakes on `agent.supervisor.wake` | **Dormant**: doctor `agent_wake` DORMANT, "no open session requested wakes". `agent.supervisor.wake` has 1,639 events (last 2026-11-10), but hub-delivery reports messages "not read within 24h; no wake reached a live session" | Messages wait until the receiver happens to start a session | dispatch | T7: wake is on by default for node-launched sessions. Check: doctor `agent_wake` LIVE, and over 7 days there are 0 hub-delivery "not read within 24h" notices for a label that had a live node |
| @mention routing plus an exit check before send, so the chain does not stall on the operator (CL "A2A Communication"; CL-F002; CL-F064) | Measured fix for the "operator becomes the router" failure that the owner names in the goal | Typed acts in `src/unified_agent/messaging/send.py` (1,309); a stored next owner in `task_ownership.py` (#2388). No mention parser under `messaging/` | **Messaging live**: perf30 `message_send` 260 (12 errors), `message_list` 59; `agent.message*` 1,815 events | A reply that names another agent does not reach it, and nothing asks "who moves next?" before a turn ends | kernel | T8: an exit check on `session_end` and handoff: refuse, in report mode first, when a `doing` task has no `next_owner`. Check: a 7-day report of turns that ended with no next owner |
| Delegation tools given to agents: delegate work, ask a question (CR-collaboration) | Simplest model of peer delegation | `delegation_manage` in `src/unified_agent/tools/delegate.py` (1,368) with receipts in `tools/delegate_receipts.py` | **Built, unused since October**: perf30 `delegation_manage` 1 call (error), `dispatch_delivery_receipt` 16 calls, **16 errors**; `agent.delegation*` last event 2026-10-11 (1,065 in all) | Agents delegate with `task_add` plus `message_send` instead | dispatch | R4 in section 4 |

## 4. Memory (write, recall, scoping, provenance, consolidation, forgetting)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| One `remember` / `recall` API ranked by semantic, recency and importance with tunable weights (CR-memory "Quick Start") | Smallest API that covers the cases | `memory_save` / `memory_search` (`src/unified_agent/tools/memory.py`, 3,375 lines; hybrid FTS and vectors); `memory_ranking.py` (326) | **Live**: perf30 `memory_search` 154, `memory_save` 10 (2 errors); doctor `semantic_search` LIVE (5,556 notes embedded); `agent.memory*` 8,367 events, last 2026-11-10. LongMemEval strict recall@5 = 0.883 on a 120-question sample (`docs/evals/longmemeval.md`) | Agents read 15 times for every write; little new knowledge enters | retrieval | T9: automatic capture (next row). Check: ≥5 saves a day across the fleet with no fall in recall@5 |
| Automatic fact extraction plus a per-user profile with static and dynamic halves, injected at session start (SM "How it works", "User profiles") | Memory written without the agent deciding to; a profile works without a query | `propose_session_learnings`; capture in `src/unified_agent/session_capture.py`, `lifecycle/capture.py` | **Dormant**: doctor `lifecycle_capture` DORMANT, "optional capture off (default)". Adoption of supermemory was deferred in `docs/designs/memory-evaluation-1458.md` | No operator or agent profile; capture off | knowledge-jobs | T9: a profile projection (static: `pref` notes; dynamic: open claims and recent decisions) in `session_packet`, plus capture on. Check: the packet carries a profile of ≤600 tokens, and LongMemEval preference questions do not regress |
| Hierarchical scopes and scoped views: an agent can be limited to `/agent/x` (CR-memory "Hierarchical Scopes", "MemoryScope") | The only reference with private memory per agent | Scope is the tag `project:<name>` (convention in `AGENTS.md`, "Memory"); tenant scope is off (`org_partition.py`) | **Convention only**: no measurement of tag compliance was available | An agent cannot keep private notes, and a search cannot be confined to a subtree | kernel | T10: a `scope` path on notes, with `memory_search(scope=...)` confined to it. Check: a test that a search with scope `/agent/a` never returns a note from `/agent/b` |
| Contradictions resolved and expired facts forgotten automatically (SM "How memory works under the hood") | Forgetting without a human | `expires_at`, `superseded_by`, `valid_to` on notes (`tools/memory.py:64-66, 207`); `contradicts` links lower ranking (`memory_get` docstring); proposal-only merge in `consolidate.py` (1,643); nightly `sleep_pass.py` (1,150) | **Live timer**: doctor `sleep_pass_timer` LIVE. `memory_prune_candidates` not in perf30 | Consolidation proposes and a human applies; I could not establish how many proposals were applied | knowledge-jobs | T11: report consolidation throughput. Check: the sleep-pass report shows proposals made and applied per week |
| Evidence store, lessons and decision log as shared memory (CL "Shared Memory") | Clowder ties memory to evidence | Notes carry author, source host, version history and a trust label (`memory_get`); events are hash-chained (`prev_hash` / `entry_hash` on `event_log` rows) | **Live** | None found | kernel | None |

## 5. Session synchronicity (what a session knows at start, resume, cross-harness continuity)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| Bootstrap injects the task snapshot, and a rolling per-thread summary is written at each context seal (CL-F065 phases A and B) | The only reference that handles context exhaustion mid-task | `build_session_packet` (`src/unified_agent/lifecycle/packet.py:41`, 728 lines): operating profile, why-chain, task, delegations, approvals, experience, memory | **Live, count unknown**: `session_*` tools have no perf30 rows (see method); doctor `session_owed` LIVE, "7d: 36 session(s) reported, 9 left work owed (18 item(s))" | No rolling summary is written at compaction, so a fifth session cannot see the first. Lane E of epic #2386 (#2397, #2398) targets this | kernel | T12: finish #2397 (compaction writes a summary into the packet). Check: a test where the packet after two compactions carries both summaries; on the hub, `session_owed` items fall below 5 a week |
| Adapters resume saved sessions across runs (PC "Persistent work context") | Plain resume, adapter-owned | `src/unified_agent/node_resume.py`, `native_sessions.py` (adopt a session started outside the hub), `turn_checkpoints.py` (643, per-turn git refs) | **Live path, use unmeasured**: `agent.node` events are live; I could not separate resume events from the others | None found in code | node | None |
| Work passes to another agent when one runs out of quota (CL "Same Bug, Two Endings") | Continuity across harnesses, which is the owner's "session synchronicity" | Quota observation in `quota_observe.py`, `plan_usage_limit.py`; ownership in `task_ownership.py` | **Not built**: no failover code was found | When a Claude session hits its limit, its task waits for a human | dispatch | T13: when a usage-limit signal arrives for a session holding a `doing` task, write a handoff and hand the task to the next eligible agent. Check: a real-SQLite test where the claim moves with its handoff payload |

## 6. Handoffs (what is carried, how the receiver verifies it)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| Structured handoff in the A2A channel (CL "A2A Communication"; CL-F065 `view=handoff`) | Typed, in the channel agents already use | Typed fields (`done`, `next_action`, `why`, `open_questions`, ...) and receiver grounding in report mode (`src/unified_agent/messaging/grounding.py`, 632; #2390) | **Report mode, just shipped**: #2392 merged on 2026-11-10; I could not count typed handoffs because there is no handoff-specific channel (`agent.handoff*` has 0 events) | The receiver is told, not checked | kernel | T14: emit `agent.handoff.grounded` for each typed handoff, then enforce. Check: 7 days of events, with ≥90% of handoffs carrying `next_action` before enforcement |
| Conversations, plans, blockers, files and run history attached to the task (PC "Task Threads") | One place to look | `task_comment` (`src/unified_agent/task_comments.py`); chat-to-task handoff (`messaging/handoff.py`, #1426); `plan_handoff` (`tools/plan.py`) | **Live**: perf30 `task_comment` 95 (11 errors), `handoff_list` 11 | Comments have an 11.6% error rate; I did not establish the causes | kernel | T15: classify the 11 `task_comment` errors and fix the top cause. Check: the error rate is under 2% in perf30 |
| Receiver verifies the claimed state | No reference does this; Clowder's grounding is shadow-only by its own code (vault `Wiki/Coordination-Build-Plan-2386.md`, "Open risks") | `review_head_status.py`, `pr_watch.py` (doctor `pr_watch_job` LIVE) | **Live for PRs only** | A handoff that claims "PR #n at head X, tests green" is not compared with GitHub | github | T16: `handoff_verify` compares claimed PR, head and check state with `pr_watch`. Check: a test where a stale head is flagged |

## 7. Graph execution (task graph, dependency-driven dispatch, recovery)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| First-class blocker dependencies; the wake queue resumes the assignee (PC "Work & Task System", "Heartbeat Execution") | Dependencies drive wake, not only display | `dependency_add` / `dependency_list` (`src/unified_agent/tools/graph.py`, 521); ready filter `frontier=True` (`tools/tasks.py:2990`); read-only projection `work_graph.py` (765) | **Live read, no push**: perf30 `dependency_list` 21 | I found no code that wakes the owner of a dependent task when its prerequisite closes. Outside the graph readers, the only readers of `task_dependencies` are `goals.py` and `planning_loop.py` | dispatch | T17: on a prerequisite moving to `done`, wake the `next_owner` or lane agent of each newly ready dependent. Check: a real-SQLite test that closing A wakes B's owner exactly once |
| Event-driven flows (`@start`, `@listen`, `@router`) with typed state (CR-flows) | Deterministic control over agent steps | Two engines: LangGraph (`langgraph_*.py` plus `tools/langgraph.py`, 4,514 lines) and YAML workflows (`src/unified_agent/workflows/`, `tools/workflow.py`, 567) | **Built, unused**: doctor `langgraph_engine` LIVE (library importable); `agent.langgraph*` has **1 event ever** (2026-09-19); perf30 `langgraph_checkpoints` 1, `workflow_list` 7, `workflow_run` 0 | Two engines, neither used for real work | dispatch | R1 in section 4 |
| Checkpoint per finished task; resume skips completed tasks; fork into a new lineage (CR-checkpointing) | Recovery that is part of the runtime | Lease expiry and requeue (`lease_reaper.py`, `queue_hygiene.py`); per-turn git checkpoints (`turn_checkpoints.py`) | **Live**: doctor `stale_task_locks` LIVE | No resume that skips finished subtasks of a parent | dispatch | Covered by T17 (the ready set is the resume point) |
| On restart, sweep in-flight runs to failed and notify the thread (CL-F048 phases A and A+) | Small, and states its failure mode | `node_queue.py` reconciler ("the reconciler already handed it back", around line 2121) | **Live**: `agent.node` events | None found | node | None |

## 8. Planning (goal to plan to tasks, revision, joint plans)

| Reference mechanism | Why it is the best of the set | unified_agent today | State on the hub | Gap | Owning component | Proposed task (pass/fail check) |
|---|---|---|---|---|---|---|
| Tasks carry goal ancestry, so agents see the why (PC "Goal Alignment"; "Goal-aware execution") | Cheap and visible in every task | `src/unified_agent/goals.py` (523) why-chain; injected by `lifecycle/packet.py` `_why_chain` | **Built, unused**: `goal_manage` not in perf30; `agent.goal*` 0 events. I could not count stored goals | The why-chain is empty unless someone links goals | kernel | T18: link every open epic to a goal (one-time backfill, owner-approved). Check: ≥90% of `doing` tasks have a non-empty why-chain in their packet |
| Crew planning: a planner writes a per-task plan before the run (CR-planning) | Plan before act, with no extra objects | `plan_manage` storage (`src/unified_agent/tools/plan.py`, 1,097); weekly read-only plans (`planning_loop.py`, 1,811) | **Plans unused, loop live**: perf30 `plan_manage` 1, `plan_list` 7; `agent.plan*` 3 events, last 2026-09-19; doctor `planning_loop_timer` LIVE | Plans exist as a store nobody writes | kernel | R5 in section 4 |
| Proposal cards (what, why, which agent): approve one and its conversation starts (CL "Conversation Proposals") | Decomposition the operator approves in one action | Decomposition happens in the steward skill's prose; `task_add` with `parent_id`; `self_organization.py` proposals | **Task filing live** (perf30 `task_add` 211) | No approve-to-dispatch object: a plan becomes tasks by hand | dispatch | T19: a plan proposal object whose approval files the tasks and their dependencies. Check: an e2e test (propose, approve, then tasks exist with edges, then T17 wakes the first) |
| Joint plans: two agents revise one plan with peer review | Ours; no reference has it | `src/unified_agent/joint_planning.py` (650; `docs/joint-planning.md`) | **Built, unused**: `plan.joint*` 0 events | — | kernel | Folded into R5 |

## Adopt as is, adapt, or decline

- PC atomic checkout: **adopted** (`task_claim`).
- PC reporting lines: **adapt**, as `reports_to` on offices (T1).
- PC scoped budgets with hard stop: **adapt**; the code exists, so prove it (T2).
- PC multi-organization: **adapt later**; dormant, and one org is enough today.
- PC heartbeat wake queue: **adapt**, with wake as the default (T7).
- PC "if it can heartbeat, it's hired": **adapt**, as one-command enrolment (T3).
- PC sandbox providers: **adapt** as a cloud ua-node (T5).
- PC goal ancestry: **adopted** in code; needs data (T18).
- PC task threads: **adopted** (`task_comment`).
- PC CEO Chat: **declined** (owner veto, 2026-09-26, vault `Wiki/Paperclip-Parity.md`).
- PC MAXIMIZER MODE, anonymous telemetry, same-origin plugin UI: **declined** (memory `656916c8f55d`).
- PC desktop app (#1428) and Linear/Jira/Asana on-ramps (#1429): **parked**
  in lane `tracking`, not declined (memory `ccf8791f84f7`).
- PC self-organization: **built** as ruled (#1431).
- CR unified `remember` / `recall`: **adopted** (`memory_save` / `memory_search`).
- CR hierarchical memory scopes: **adapt** (T10).
- CR crew planning: **adapt** as plan proposals (T19), not an LLM planner in the hub.
- CR flows: **decline as a second engine**; one engine only (R1).
- CR checkpointing: **adapt** through the ready set (T17).
- CR peer delegation tools: **decline**; task plus typed handoff instead (R4).
- CR hierarchical process with a manager LLM: **decline**. Routing stays with
  the operator and the chief-of-staff profile, and policy stays in Python
  (`ccf8791f84f7`, "no second policy implementation").
- CL @mention routing and exit check: **adapt**, as an exit check on next owner (T8).
- CL external agent onboarding contract: **adapt**, as a conformance suite (T4).
- CL session continuity: **adapt** (T12, which is #2397).
- CL restart recovery: **adopted** (node reconciler).
- CL quota failover: **adapt** (T13).
- CL proposal cards: **adapt** (T19).
- CL shared MCP process, anchor-first reads, memory contradiction at write,
  harness eval plane, F079 regex voting, F254 freshness gates, F118 CPU stall
  kill, F250 plan board, F334 multi-machine hosting, cross-model review rounds:
  **declined** (vault `Wiki/Coordination-Build-Plan-2386.md`, "Considered and
  left out").
- SM automatic extraction and profiles: **adapt** natively (T9). Adopting
  supermemory itself stays **deferred** (`docs/designs/memory-evaluation-1458.md`).
- SM automatic forgetting: **adapt** through the existing expiry and sleep pass (T11).
- SM connectors (Drive, Gmail, Notion): **decline for now**; the Obsidian vault
  is the prose channel (`AGENTS.md`, "Topology").

## Where a rewrite beats a refactor

Churn is commits that touched each file in the last 60 days
(`git log --since='60 days ago' --format= --name-only -- src | sort | uniq -c | sort -rn`, run at `origin/main` on 2026-11-10).

**R1. Two graph engines, both unused, become one dependency-driven runner.**
Evidence: `langgraph_*.py` and `tools/langgraph.py` hold 4,514 lines; there is
1 `agent.langgraph*` event ever and 1 recorded call in 30 days. The YAML
workflow engine (`tools/workflow.py`, 567 lines, 6 definitions) has 0 recorded
`workflow_run` calls. Neither file appears in the top 25 by churn. Replacement:
the ready set plus T17, with steps as tasks, so recovery is task recovery.
YAML workflows stay as task templates. Proof of retained function: port
`tests/test_langgraph*.py` cases that assert governed effects (approval before
merge) to the task runner; doctor `tool_classification` reports 0 drift after
`langgraph_*` tools are withdrawn through the profile deny list first.

**R2. The Control Center backend becomes routes derived from object-model readers.**
Evidence: `src/unified_agent/webui/routes.py` (6,577 lines) and `webui/api.py`
(6,413) are the two most-churned files in `src/` (144 and 87 commits). Doctor
`web_ui` reports 172 routes. HTTP use is not in the tool perf table, so **I
could not establish route use**. `api.py:177` parses `unblock-condition:` out of
task notes, which goes against the no-inferred-edges rule in `docs/work-graph.md`.
Replacement: integration shape (c) from `ccf8791f84f7`, where each pane reads
`platform_readers.py` or `/api/platform/*` and hand-written routes are deleted.
Proof: a route inventory test listing each route's reader; the SPA's API calls
(`packages/web`) resolve to the same JSON shape in a recorded-fixture test.

**R3. The 5,057-line doctor becomes probes declared by component manifests.**
Evidence: `src/unified_agent/tools/doctor.py` has 135 commits in 60 days
(second in `src/`) and is called 1,515 times in 30 days, 452 in the last 24 h,
mostly by polling. Every new capability edits it, which is the
central-registry miss that kernel split K5a names. Replacement: each component
manifest (K4) registers its probes; doctor composes them. Proof: a snapshot
test that the 78 capability names and status rules are unchanged on a fixture
host, and `test_doctor*.py` passes unmodified.

**R4. Delegation becomes a task plus a typed handoff.**
Evidence: `tools/delegate.py` (1,368 lines), `tools/delegate_receipts.py`,
`delegation_ledger_log.py`: 1 `delegation_manage` call in 30 days (error), and
`dispatch_delivery_receipt` 16 calls with 16 errors. The last
`agent.delegation*` event was 2026-10-11. Replacement: `task_add` with
`next_owner` (`task_ownership.py`) plus a `grounding.py` handoff. The `delegate`
approval gate (`UA_APPROVAL_BASELINE`) moves to "assign to another agent".
Proof: each `tests/test_delegat*.py` behaviour maps to a test on the new path,
and the evidence ledger still records who assigned what to whom.

**R5. Plans, joint plans and the planning loop become one plan object over the task tree.**
Evidence: `tools/plan.py` (1,097), `joint_planning.py` (650) and
`planning_loop.py` (1,811) total 3,558 lines. They have 1 `plan_manage` call in
30 days, 3 plan events ever and 0 joint-plan events, while `task_add` (211) and
`task_update` (560) do the real planning. Replacement: a plan is a parent task
with a goal link and child tasks with dependencies. The proposal (T19) is its
revision record, and the weekly loop reads it. Proof:
`tests/test_object_model.py` keeps serving a `plan` kind derived from the tree;
`tests/test_joint_planning.py` peer-review rules hold on proposals.

## What to stop carrying

These are candidates for the owner. Nothing here is decided. "Lost" is what
goes away if the code is deleted rather than kept dormant.

| Surface | Evidence | Serves none of the eight because | Lost if removed |
|---|---|---|---|
| Dispatch lane A (`src/unified_agent/dispatch/`, 8,103 lines) | doctor `dispatch_scheduler` and `dispatch_tick_timer` DORMANT, "retired by #1435" | Execution moved to ua-node | The approved-dispatch state machine (`docs/architecture/approved-dispatch-state-machine.md`); check what the executor still imports first |
| Zulip (`zulip_*.py`, `tools/zulip.py`, 1,720 lines) | doctor `zulip_relay`, `zulip_chat`, `zulip_worker_service` DORMANT, "Zulip retired" | Hub messaging replaced it | Chat bridge to Zulip; 5 bot credential files on the hub become garbage to remove |
| Discord and Telegram surfaces (`ts_worker/`, 1,079 lines) | doctor `ts_worker` and `connector:discord` DORMANT, no tokens | No operator use recorded | Mobile chat to the fleet (the ua-worker node itself is live and is a separate question) |
| Multi-org partitioning (`org_partition.py`, `tenant_tables.py`, 1,416) | doctor `tenant_consistency` DORMANT | One operator, one org | Future multi-tenant hosting; the tenancy classification test |
| Cluster briefing timer | doctor `cluster_briefing_timer` DORMANT, "retired by #1888" | Replaced | Nothing; only the probe remains |
| Pilot apps (`pilot_app.py`, `pilot_auth.py`, `workos_hub_auth.py`, 792) | No doctor row; no tool calls | Not one of the eight | The WorkOS pilot (`docs/workos-pilot.md`) |
| Symbol embeddings | doctor `symbol_embeddings` DORMANT | Developer tooling, not coordination | Semantic symbol search |

## Proposed tasks, ordered

Proposed only. No hub task was filed. Order: things that unblock the owner's
daily loop first (enrol, wake, next owner), then the graph, then memory and
planning, then rewrites, which wait for kernel split K1 and K4.

1. **T3** (auth). One-command agent add and remove. Check: e2e test passes, and doctor `agent_token_lifecycle` is LIVE.
2. **T7** (dispatch). Wake on by default for node-launched sessions. Check: doctor `agent_wake` LIVE, and 0 unread-24h notices in 7 days.
3. **T8** (kernel). Exit check for next owner, report mode. Check: a 7-day report exists.
4. **T17** (dispatch). Closing a blocker wakes its dependents. Check: a real-SQLite test where the wake fires exactly once.
5. **T5** (cloud). One cloud ua-node enrolled. Check: it is in doctor `fleet_nodes` and completes one task end to end.
6. **T13** (dispatch). Quota failover with handoff. Check: a test where the claim moves with its payload.
7. **T12** (kernel). Compaction summary in the packet (#2397). Check: a test with two compactions.
8. **T14** (kernel). `agent.handoff.grounded` events. Check: 7 days of data, then decide on enforcement.
9. **T16** (github). `handoff_verify` against `pr_watch`. Check: a stale-head test.
10. **T19** (dispatch). Plan proposal that files tasks and edges on approval. Check: e2e test.
11. **T18** (kernel). Goal backfill. Check: ≥90% of `doing` tasks have a why-chain.
12. **T1** (kernel). `reports_to` on offices. Check: an object-model test and a doctor cycle check.
13. **T2** (telemetry). Prove one budget hard stop. Check: a test, plus ≥1 budget on the hub.
14. **T9** (knowledge-jobs). Profile in the packet, capture on. Check: ≤600 tokens and no LongMemEval regression.
15. **T10** (kernel). Memory scope paths. Check: a scope-isolation test.
16. **T4** (node). Enrolment conformance suite. Check: a generated harness matrix.
17. **T6** (kernel). Weekly stale-task closure proposals. Check: stale count under 20.
18. **T15** (kernel). Fix the top `task_comment` error cause. Check: error rate under 2%.
19. **R3, then R2** (mcp-adapter, control-center). After K4. Check: doctor snapshot parity, then the route inventory test.
20. **R1, R4, R5** (dispatch, kernel). After T17 and T19. Check: the parity tests named in section 4.

## What I could not verify

- Call counts for any tool whose OK samples are excluded from perf sampling
  (at least `task_list`; probably the `session_*` tools). The hub's
  `UA_PERF_SAMPLE_EXCLUDE_TOOLS` value was not read, to keep within secrets
  hygiene for `service.env`.
- Control Center route use: HTTP routes are not in the tool perf table.
- Whether any budget, goal or plan row exists on the hub, as opposed to having
  no recorded call or event.
- How many consolidation proposals were applied.
- Hindsight, OpenRig, FirstMate and t3code were not re-read for this map.
