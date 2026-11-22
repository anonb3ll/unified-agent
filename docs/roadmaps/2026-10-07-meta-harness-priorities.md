# Meta-harness development review — 2026-10-07

## Objective and evidence boundary

Unified Agent should let an operator choose and govern heterogeneous agents,
inspect their work and decisions, and reuse attributable knowledge through one
hub. Authority remains in the Python kernel/control plane; MCP, HTTP, CLI and
the Control Center project the same objects. A second memory or execution
authority would make the audit harder rather than complete the product.

This review combines independent planning/design subagents with the live hub
queue, claims and decision records. The inspected source baseline was
`e9c604cbd3aac79a63b8ff084af47b39d70d5143`. Source presence is not live harness
acceptance or a deployed feature claim. Milestones close on acceptance evidence,
not their scheduled date.

The earlier route is mission #1450 plus the recorded #1855 override: security
prerequisites alongside Sessions and collaboration. The published native
harness strengthening plan supplies the latest foundation sequence below;
this reconciliation does not change mission, claim or scheduler records.
The September 27 durable plan and October 2 `NEXT-WORK.md` contain older
status pointers. In particular,
queue/steer #1740, resume/adopt #1741, declared route scopes #1745,
evidence-backed memory #1459, memory RRF #1913 and run coverage #1836 have code
at this baseline. Their remaining acceptance work must be checked live before
filing or rebuilding them. Session credentials #1744 have a flag-off first
phase; that does not establish a live credential rollout.

Reconciliation cutoff: **2026-10-07 10:35 UTC**. The source is the published
Mac-vault note
`Wiki/Research/Unified-Agent-Native-Harness-Strengthening-Plan-2026-10-07.md`
(266 lines), especially its five priorities (lines 11–19), assessment boundary
(line 31), and detailed priorities (lines 60–108). Its repository baseline is
`e9c604c`, dated 08:17 UTC, and its hub/task observations are around 09:25 UTC.
Those measurements and statuses are dated research snapshots; this update
does not independently refresh them or present their ratios as current.
External designs are reference material for native Unified Agent work behind
the existing Mac hub. No framework, backend, hook pack or model dependency is
introduced. This document creates no new tasks, claims or scheduler edges,
and grants no live-provider benchmark authorization.

## Selected development stretch

1. **#1742: effective ACP session settings.** Apply an advertised model and
   permission mode before the first prompt, preserve settings when resuming,
   expose requested/effective values and unsupported capabilities, and prove
   supervised permission requests still enter the approval path. The live
   decision `59b322825bb2` records the operator's prior scope ruling: supervised by
   default, full access only through an explicit per-session choice. Existing
   dispatch approval remains required. Final-head review and shipping approval
   remain independent gates.
2. **#1955: auditable memory retrieval.** A semantic failure currently falls
   back to FTS without response-level status. Return bounded, versioned
   retrieval diagnostics so healthy hybrid misses differ from degraded
   lexical fallback. Distinguish model acquisition failures from runtime
   defects without exposing raw errors. Preserve ranking, scope filtering and
   the existing `results` contract. This is an observable foundation for
   unified retrieval, not a claim that external stores have been integrated.
3. **#1956: semantic tool-schema fingerprint.** Preserve the existing
   names/requiredness fingerprint and add a versioned advertised-input
   fingerprint covering types, enums, defaults and nested constraints. Keep
   callable, manifest and registry surfaces distinct. Reference-dependent
   normalization must be conservative: a description or example can become
   validation-relevant when referenced. This detects contract drift, not
   handler/policy changes or complete JSON Schema equivalence.

These lanes use separate claimed worktrees. Session leaf files are separate
from the active #1953 shell, App, navigation and palette work. The parallel
#1954 shipping catch-up owns its integration checkout and delivery authority;
this stretch does not alter that claim or shipper identity.

## Foundation order

The published plan places durable identity and trustworthy usage before budget
enforcement and broader autonomous supervision. Existing implementations and
done design slices do not establish end-to-end acceptance. Evaluation design
and baseline capture may proceed alongside the first two contracts within
existing scope; the order is proposed build sequencing, not scheduler enforcement.

| Order | Capability | Acceptance that makes it auditable |
| --- | --- | --- |
| 1 | #1919 / #1836 durable run and effect lifecycle; contracts with #1737 / #1738 | Share run, attempt, claim-generation and terminal-state contracts across executor, node and interactive paths. Persist effect identity and payload fingerprint before dispatch; reconcile receipts and crashes without duplicate effects. Changed payloads under one identity and stale writers fail; uncertain effects remain honestly indeterminate. Typed handoff and restore preserve original identity and evidence. |
| 2 | #1837 usage attribution with #1917 / #1929 | Bind usage to the original attempt, provider request, lease generation, source and measurement time. Reconcile delayed, duplicate and out-of-order snapshots/finals after cancellation or replacement. Separate API, broker, subscription, estimated and unknown cost; missing cost never becomes zero. |
| 3 | #1917 / #1929 nested budgets and midrun cancellation | Reserve parent/child and phase budgets before dispatch, including a review reserve; conserve reservations across retries and reconciliation. Stops block new work, propagate to descendants and reap or account for workers. Preserve late usage and report provider termination limits and bounded overshoot honestly. |
| 4 | Acceptance evaluation within #1836 / #1837 / #1919 and #1915 M0 | Join task, attempt, trace, artifact hash, exact HEAD, reviewer decision and policy version. Count each accepted artifact once, retaining failed/cancelled attempts and later defect outcomes. Reproduce paired results; show unknown cost and unmeasured dimensions. Required-control failures reject promotion regardless of apparent savings. Agree bounded scope before adding any evaluation task. |
| 5 | #1951 / #1749 / #1873 bounded supervision | Build on lifecycle, usage and budget contracts; #1918 completion covers only its design/read-only slice. Deduplicate event/head inputs and persist cursor, intent and outcomes durably. Replay/restart produces no extra effect or wake; declared waits, retry exhaustion and escalation remain bounded. Prove real-pane wakes and zero idle LLM calls before opt-in effectful rollout. |

## Product tracks alongside the foundations

Sessions, collaboration and memory/tool depth remain product tracks. They do
not replace the foundation sequence or broaden the three selected changes.

| Track | Existing scope | Acceptance that makes it auditable |
| --- | --- | --- |
| Sessions and collaboration | #1743 turn checkpoints/diffs; #1426 conversation/project handoff | Bind session, turn, workspace and exact snapshot refs; preserve HEAD/index/WIP and reject unsupported rollback before touching files. Reuse canonical sessions/messages/tasks, retain origin/correlation, prevent replay duplicates and enforce workspace/project scope. |
| Native memory depth | #1915, split into bounded children before claim | Evaluate temporal validity, version provenance, contradiction handling and graph retrieval through the native store with frozen M0 fixtures before expansion. Each child states scope and attribution semantics; restart and cross-organization exclusion are explicit acceptance cases. |
| Memory/tool gateway and adapter parity | #1336 and bounded cross-adapter work | Identify source/ref, fact/inference, supersession, ranking and availability in scoped retrieval; unavailable differs from empty and scope leakage is zero. Migrate one adapter/object at a time and compare allow/deny/approval-pending/attribution across MCP, HTTP and workers through one policy implementation. Live auth/credential rollout remains separately approved. |

Additive near-term follow-ups identified by the design review are work-graph
exposure of already-recorded run usage/provenance after the attribution
contract, session-packet retrieval status, and consumption of retrieval
diagnostics by the UI/SDK. The semantic
tool-schema fingerprint was promoted to the selected stretch as #1956.

## Completion and limits

Each implementation must have scoped offline tests, an independent review,
and a linked PR with evidence for its exact committed head. Targeted desktop
checks do not substitute for covering CI. Landing and Mac deployment use the
designated deploy steward and the existing exact-head approval gates.

Memory baselines are synthetic fixtures, not production quality estimates.
Live fleet rehearsal, cross-source recall, credential lifecycle, supervisor
activation, cost enforcement and M1–M3 verdicts remain separate evidence
items. Unknown or unreported behavior must remain visibly unknown.

## References

- Cockpit `docs/company/{CHARTER,OPERATING-SYSTEM,BUILD-GUIDE}.md` and
  `docs/queue/NEXT-WORK.md`; live hub mission #1450 and route #1855.
- Published Mac-vault research note
  `Wiki/Research/Unified-Agent-Native-Harness-Strengthening-Plan-2026-10-07.md`,
  prepared 2026-10-07; assessment around 09:25–09:26 UTC, reconciled here at
  10:35 UTC. Its measurements and task appendix remain dated observations.
- [Harness control matrix](../harness-control-matrix.md).
- [Operating profiles](../operating-profiles.md).
- [Memory evaluation boundary](../designs/memory-evaluation-1458.md).
- [Memory retrieval and evidence](../memory-tasks-vault.md).
- [Supervisor design](../designs/2026-10-06-first-mate-supervisor-loop.md).
- [Session-scoped credential design](../designs/session-scoped-mcp-credentials.md).
- [Platform rework review](../architecture/platform-rework-review-2026-10-03.md).
