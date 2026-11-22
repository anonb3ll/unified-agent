# Meta-router and harness-routing decision

**Assessment date:** 2026-09-29
**Task:** #1506
**Status:** Recommended assessment; this record does not authorize an implementation or live routing change.

## Verdict

**ADAPT the architectural pattern; DEFER adopting HarnessRouter or changing production routing.** Keep unified-agent authoritative for tasks, assignments, identity, policy, approvals, and evidence. A router may consume that authority and dispatch a bounded execution request; it must not create a second task/run store or make policy decisions outside the hub. Treat UHP as a candidate execution contract and HarnessRouter as an optional adapter/benchmark substrate, not as the meta-router itself.

This supports the north-star pillar for **agent and harness orchestration with project mapping**. It does not provide team planning, unified memory, scoped authorization, or accountability on its own. The current HarnessRouter interface lets a client select a configured harness and model; the cited evidence does not show automatic per-task routing learned from our measured quality, cost, and latency. (Sources: [UHP Architecture, 2026-09-12](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/protocol/versions/2026-09-12/architecture.md), [HarnessRouter README, v0.25.17](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/README.md).)

This is an assessment recommendation, not authorization to deploy a router, grant it credentials, or bypass the existing policy and approval boundary. Existing work on the provider-key broker (#1326), MCP gateway (#1336), evals/scorecards (#1354), per-agent cost (#1348), lifecycle controls (#1351), and run records (#1436) remains the foundation. Any future router should call those seams and preserve their canonical IDs.

## #514 disposition

**Re-scope #514; keep it parked in tracking. Do not un-park or dispatch it from this assessment.** Its 2026-09-18 rationale (“no routing demand/cost-benefit evidence”) predates the north-star owner gap, so it should no longer be read as a permanent rejection. The north star establishes strategic relevance, but it does not establish a workload, measured benefit, or a safe routing policy. The 2026-09-18 telemetry review also recorded no demand and forbade dispatch without a go-ahead (task #784).

**Un-park trigger:** an active, named execution path needs a harness/model choice, and a frozen representative evaluation set plus per-run cost, latency, outcome-quality, and lifecycle telemetry exists for the native path and at least one candidate. Before implementation, its owner must define success thresholds and obtain the required go-ahead. Until then, leave #514 parked/tracking and do not change live routing.

## Fixed-signal decision rubric

Compare the existing ACP-backed node path with each candidate on the same frozen tasks, inputs, tool permissions, model settings, budgets, and retry policy. Stratify by task class, repeat runs, and report distributions and uncertainty; do not rank configurations on a single aggregate or vendor benchmark. Set numeric thresholds before the spike begins.

| Signal | Measure per accepted task | Decision rule |
|---|---|---|
| Quality | Fixed task rubric; correctness, grounding, and required artifact validity; blinded review where practical | Must meet the predeclared quality floor and show no material regression against the native path. A cheaper or faster incorrect result is a failure. |
| Cost | Provider cost per accepted result, including failed attempts, retries, and unusable outputs; retain token counts and billing mode | Count savings only after the quality floor is met; compare equivalent work and report tails/outliers. Reuse #1348/#1436 usage fields where present. |
| Latency | End-to-end p50/p95 and time-to-first-event; report queue/wait separately when observable | Must meet the task-class deadline. Report cost/latency tradeoffs rather than a universal winner. |
| Reliability and lifecycle | Success/error/timeout/cancel rates; follow-up/session continuity; restart recovery; artifact retrieval; stream/event fidelity and terminal-state reconciliation | Every terminal result must reconcile to the canonical run. Missing events, lost artifacts, unbounded hangs, or unrecoverable cancellation/restart failures block adoption. |
| Governance and security | Identity/scope propagation, approval-required action behavior, denied-action attempts, audit attribution/completeness, credential and workspace isolation | Hard gate: zero policy or approval bypasses, no authority duplication, and complete attribution for every attempted/finished run. Any escape blocks adoption. |
| Operations | Pin/upgrade/rollback effort, service availability, security boundary, support/debugging burden, and adapter maintenance | Adopt only if the total operating burden is acceptable and the deployment/security review passes; self-hosted licensing alone is not a security approval. |

Use a Pareto comparison across quality, cost, and latency after the governance/reliability hard gates. Prefer no routing over a weighted score that can hide an approval escape or quality regression. The benchmark currently published by HarnessRouter reports one synthetic Care Prep task across eight harness/model configurations, with five grounding runs per configuration; its roughly 475× cost range and 3.2× latency range are useful hypothesis-generators, not unified-agent savings or a general ranking ([benchmark and method](https://harnessrouter.ai/benchmarks), checked 2026-09-29).

## Bounded spike plan — gated, not started

The earlier research capture proposed a useful interoperability spike. Keep it conditional on explicit go-ahead, a separately claimed implementation task, and a security review. Prerequisites and limits:

1. Run only a pinned CE image digest on loopback in an isolated disposable environment. Review the actual image/configuration and default-credential exposure first; do not expose the console or gateway to a network. Use synthetic tasks and a dedicated limited provider key. No fleet credentials, customer data, or production routing.
2. Compare against the current ACP node path using the fixed rubric above. Pin the CE and harness versions, task set, model/provider route, budgets, and retries; record digests and configuration with the results.
3. Exercise first task, follow-up, model switch, artifact retrieval, cancellation, timeout, forced runner restart/recovery, event fidelity, and an action requiring unified-agent approval. Confirm a denied action cannot execute and confirm remote IDs reconcile to existing task/run records.
4. Repeat enough runs to report variability. Stop on any governance escape, un-attributed/lost terminal run, or material quality regression. If no current workload and baseline telemetry exist, do not run the spike; UHP remains a design reference.

No installation, credential use, deployment, benchmark reproduction, or live routing change was performed for this assessment.

## Evidence and limits

| Evidence | What it supports | Limit |
|---|---|---|
| [UHP Architecture 2026-09-12 at release v0.25.17](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/protocol/versions/2026-09-12/architecture.md) | Client → server → harness; configured harness is selected by the client; server execution internals are out of protocol scope. | Protocol scope describes an execution boundary; it is not proof of a secure or reliable deployment. |
| [README at release v0.25.17](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/README.md) and [SECURITY at release v0.25.17](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/SECURITY.md) | CE is Apache-2.0; documents local deployment, default console credentials, session workspaces/users, API features, and security reporting. | Upstream documentation is not an independent security audit. The release tag is the assessed snapshot; do not infer later fixes or current hosted-service terms. |
| [UHP conformance report at release v0.25.17](https://github.com/HarnessRouter/harnessrouter/blob/v0.25.17/protocol/conformance/README.md) | Upstream reports CE 0.17.3 passing 74/74 Full checks on 2026-09-15. | Project-reported, older than v0.25.17; not independently reproduced here and does not establish workload quality, policy enforcement, or production readiness. |
| [Issue #207](https://github.com/HarnessRouter/harnessrouter/issues/207), [issue #202](https://github.com/HarnessRouter/harnessrouter/issues/202) | Specific user reports of backend failures/timeouts and Codex/custom-Responses streaming/tool-shape problems. | Issues are mutable; checked 2026-09-29. Both describe particular CE 0.17.3-era setups, not fleet-wide failure rates or confirmed current-release defects. |
| [HarnessRouter benchmark](https://harnessrouter.ai/benchmarks) | Shows cost/latency can vary across harness/model combinations and describes its controlled synthetic task. | Vendor-published single task and limited configurations; figures do not predict our workloads or prove an automatic policy-aware router. |
| [Unified-Agent architecture](../architecture.md), [ACP interface decision](acp-interface-decision.md) | Hub policy/approval/identity boundaries and existing ACP execution seam. | Local design records describe our intended architecture; an actual spike must verify current end-to-end behavior before adoption. |

**Research cut:** primary sources checked 2026-09-29; pinned HarnessRouter release evidence uses `v0.25.17` (release commit `b4f81fe`). Dynamic issue and benchmark pages are date-stamped above. Existing vault capture: `00 - Inbox/2026-09-28 HarnessRouter research.md` (agent-codex, for review).
