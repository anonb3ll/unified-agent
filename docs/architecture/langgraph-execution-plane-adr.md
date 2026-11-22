---
title: ADR — LangGraph execution-plane contract
date: 2026-08-25
author: agent-codex
status: accepted-for-pilot
tags: [adr, architecture, langgraph, execution, unified-agent]
project: unified-agent
---

# ADR: LangGraph execution-plane contract

## Decision

Unified-Agent may use Python LangGraph for durable, branching, resumable graph
execution. Unified-Agent remains the only control plane. LangGraph does not own
identity, task or delegation state, claims, approvals, capabilities, budgets,
memory, vault records, audit policy, or cross-device coordination.

This contract is accepted for the Python pilot and is the prerequisite for the
governed dispatch and observability work. LangGraph.js is deferred until a
concrete TypeScript product needs it. LangSmith is not enabled by this decision.

## Scope

The first graph is a read-heavy research flow:

```text
intake -> parallel source collection -> evidence reduction
       -> Unified-Agent approval interrupt -> proposed synthesis
```

The research pilot may call only a static, reviewed allowlist of read-only
Unified-Agent tools. Task #757 adds the separately selected progress-note graph
defined below. It must not merge, deploy, restart services, write externally, change
shared configuration, or dynamically expose the full MCP tool inventory.

## Authority boundary

| Concern | Authority | LangGraph may retain |
|---|---|---|
| Verified identity and active profile | Authenticated Unified-Agent HTTP hub | Last observed label/profile, as non-authoritative trace context |
| Task, claim, delegation, and dependency state | Unified-Agent task and delegation records | IDs plus an authority precondition token |
| Capabilities, tool policy, and budgets | Unified-Agent policy/profile/capability gates | The requested capability set, never a grant |
| Human approval | Unified-Agent approval queue and exact tool/payload match | Approval request ID and observed status |
| Memory, vault, and artifacts | Existing Unified-Agent stores and Git/vault boundaries | References and content digests |
| Audit and durable operational events | Unified-Agent event and telemetry contracts | Correlation IDs and emission status |
| Node routing, interrupts, retries, and short-term state | LangGraph | Checkpointed execution state |

Checkpoint content is evidence about execution, not permission to execute. On
every start or resume, and again immediately before a governed tool call, the
adapter must query the authenticated hub and fail closed if current authority
does not match the run preconditions.

## Protocol contract v1

The protocol identifier is `ua.langgraph.run/v1`. All persisted values and
interrupt payloads must be JSON-serializable.

The bounded operator stream is defined by the
[`execution-stream-v1.schema.json`](execution-stream-v1.schema.json) JSON
Schema. It currently contains only `step_start`, `gate_waiting`,
`tool_executed`, and `done`. Events are ordered per `run_id` by the positive
`sequence` field; event IDs are unique within the stream. The stream carries
digests and references, not raw prompts, tool payloads, credentials, or large
artifacts.

### Graph definition identity

Every deployed graph definition has:

- `graph_name`: stable lowercase slug, such as `governed_research`;
- `graph_version`: immutable semantic version for operator-readable releases;
- `definition_digest`: SHA-256 of the executable graph definition and state schema;
- `state_schema_version`: positive integer changed for any checkpoint-shape change;
- `contract_version`: `ua.langgraph.run/v1`.

Changing node behavior, routing, interrupt order, tool schemas, or state shape
creates a new immutable definition digest. An old run resumes only against its
exact digest. A checkpoint migration must be an explicit, tested transformer;
otherwise recovery creates a new child run and preserves the old run unchanged.

### Run and task identity

Unified-Agent creates the run identity before invoking LangGraph:

| Field | Contract |
|---|---|
| `run_id` | Globally unique ULID; immutable for one logical run |
| `thread_id` | `ua-lg:<run_id>`; passed to the LangGraph checkpointer and never reused by another run |
| `task_id` | Canonical Unified-Agent task ID |
| `task_updated_at` | Canonical task revision precondition captured at dispatch |
| `claimed_by` | Expected verified task owner |
| `claim_expires_at` | Claim lease observed at dispatch; never treated as a durable grant |
| `delegation_id` / `cycle_id` | Optional canonical control-plane references |
| `parent_run_id` | Optional predecessor for an explicit retry, migration, or recovery run |
| `trace_id` | Unified-Agent telemetry trace ID shared by run, node, model, and tool spans |
| `input_digest` | SHA-256 of canonical JSON input; raw input is not required in trace data |

`task_updated_at`, ownership, status, project, lease, profile, capabilities, and
budget form an authority precondition. Their checkpoint copy is informational.
The adapter must refresh them from Unified-Agent. A task revision or owner
change blocks the existing run as `stale_authority`; it does not silently adopt
new authority.

### Node and operation identity

Each node has a stable `node_key` inside one graph definition. Each replayable
operation declares an `operation_key` that is stable across process restarts
and retries. Attempts are numbered from 1 for telemetry only.

The idempotency key is:

```text
ua-lg:<run_id>:<node_key>:<operation_key>
```

Attempt number must not appear in the idempotency key. A retry is the same
logical operation, not permission to perform a second effect.

## Approval and side-effect rules

1. A LangGraph `interrupt()` is a pause mechanism, not an approval decision.
2. The interrupt payload contains only `run_id`, `task_id`, `node_key`, the
   proposed tool name, a redacted payload summary/digest, and any canonical
   Unified-Agent `approval_id`.
3. The adapter submits the exact tool and payload through the existing
   Unified-Agent policy and approval chokepoints. It never resolves its own
   approval.
4. On resume, a client-supplied `approved: true` is ignored as authority. The
   adapter revalidates the task and retries the identical hub call. Only the
   hub may consume the live one-shot pass.
5. Pending approval pauses without consuming a retry. Denial or revocation is
   terminal for that proposed effect. An expired pass requires a fresh queue
   decision; it is never treated as an implicit approval.
6. Safety, profile, workspace, capability, and budget checks run even after
   approval. Approval may tighten policy; it cannot bypass another check.

The research pilot permits no mutating effect. Unified-Agent already owns an
effect ledger keyed by the idempotency key, with at
least `prepared`, `executing`, `succeeded`, `failed`, and `unknown` outcomes.
The ledger and its audit record live outside LangGraph checkpoints. A tool with
native `idempotency_key` support receives the same key. A tool without either
native idempotency or a safe reconciliation read is not eligible for automatic
retry.

## Checkpoint backend and data rules

The pilot uses LangGraph's SQLite checkpointer in a dedicated database under
the Unified-Agent state directory, conceptually:

```text
${UA_HOME}/langgraph/checkpoints.sqlite3
```

It must not share tables, a connection, migrations, or a file with
`memory.db`. The pilot is single-process, local-only, and uses restrictive
owner permissions. An in-memory saver is allowed only in unit tests that do not
claim restart recovery.

Production promotion requires a separate checkpoint-backend decision. SQLite
may continue only after a documented locking, backup, retention, and
multi-worker concurrency review; otherwise use the supported PostgreSQL
checkpointer. Failure to open the durable checkpointer must stop the governed
run. There is no silent in-memory fallback.

Checkpoint state may contain bounded graph inputs, reduced evidence, routing
state, counters, digests, and references. It must not contain bearer tokens,
API keys, environment values, raw approval passes, unrestricted tool payloads,
or long-term memory copied from Unified-Agent. Sensitive source bodies should
be stored in their authoritative system and referenced by artifact ID plus
digest.

Checkpoint retention is not delegated to LangGraph defaults. The pilot keeps
only its bounded test/evaluation data. Before production dispatch is enabled,
the operator must approve retention and deletion rules for completed, failed,
and interrupted runs.

## Retry and idempotency policy

Graph definitions declare retry policy per node. The hard ceiling is three
retries after the first attempt. Every attempt has a timeout and emits a span.

| Failure class | Action |
|---|---|
| Transient transport error, rate limit, or retryable 5xx | Bounded exponential backoff with jitter |
| Deterministic validation, policy denial, malformed contract, or missing graph version | Fail without automatic retry |
| Approval pending | Interrupt; do not consume retry budget |
| Stale claim, task revision, identity, capability, or budget | Block as `stale_authority`; require a new authorized run |
| Unknown side-effect outcome | Quarantine and reconcile from the Unified-Agent effect ledger/provider before retry |

Model and tool calls record input/output digests and bounded error classes. Raw
prompts, arguments, results, and exception bodies are not idempotency evidence.

## Trace and redaction contract

LangGraph emits through Unified-Agent's existing `TelemetrySpan` and event-log
path. Run, node/chain, model, and tool spans share `trace_id`; child spans set
`parent_span_id`. Required metadata is bounded to:

- `run_id`, `graph_name`, `graph_version`, `definition_digest` prefix;
- `task_id`, optional delegation/cycle IDs, verified agent label;
- `node_key`, `operation_key`, attempt, checkpoint sequence;
- outcome, duration, retry class, approval ID/status, and input/output digests;
- model/provider and aggregate token/cost fields when available.

All metadata and error strings pass through
`telemetry.redaction.sanitize_metadata` and
`redact_sensitive_content` before persistence or export. Traces contain no raw
prompts, model responses, source documents, tool arguments/results, credentials,
approval payloads, or environment values by default. An exporter is a sink and
never becomes authoritative state. External export, including LangSmith,
requires a separate privacy, retention, access, and cost decision.

## Lifecycle and recovery

The detailed graph status maps into the existing coarse `Execution` status:

| Graph condition | Execution status | Recovery |
|---|---|---|
| Created or running | `pending` / `running` | Continue with the same `run_id` |
| Approval interrupt, retry wait, or stale authority | `blocked` | Revalidate; resume only the same immutable definition |
| Successful terminal node | `completed` | Record output/artifact refs in Unified-Agent |
| Exhausted retry, invalid state, denial, or cancellation | `failed` | Preserve evidence; create a child run only by explicit dispatch |

Recovery sequence after process loss:

1. Load the latest checkpoint by the exact `thread_id`.
2. Verify contract version, graph version, definition digest, and state schema.
3. Re-authenticate to the canonical hub and revalidate task authority, profile,
   capabilities, budget, and any approval.
4. Reconcile every operation that could have crossed a side-effect boundary.
5. Resume from the checkpoint, or fail closed with a redacted durable reason.

If a checkpoint is missing or corrupt, a definition is unavailable, authority
is stale, or an effect outcome cannot be established, the adapter must not
guess. It records the failure in Unified-Agent and leaves the run failed,
blocked, or quarantined for an explicit recovery decision.

## Acceptance contract for follow-on tasks

The Python pilot must prove these cases before governed dispatch begins:

1. Restart after a completed node resumes without repeating that operation.
2. Duplicate delivery returns one logical result and one effect/audit outcome.
3. Approval pending, denied, revoked, and expired states cannot execute an
   effect through a forged resume value.
4. A changed task revision, owner, lease, profile, capability, or budget fails
   closed before a tool call.
5. Retryable failures stay within declared attempt and timeout limits;
   deterministic failures do not churn.
6. Missing/corrupt checkpoints and missing graph versions produce durable,
   redacted failures with no in-memory fallback.
7. Trace output correlates run/task/node/tool activity and contains no raw
   sensitive payloads or known secret patterns.
8. The pilot uses Python and a separate SQLite checkpoint database; it makes no
   production service or shared-configuration change.

Follow-on libraries (not MCP surfaces) now exist:

- Governed dispatch adapter: [`langgraph_dispatch.py`](../../src/unified_agent/langgraph_dispatch.py)
  (`UnifiedAgentDispatchAdapter`). Read-only governed calls and task-outcome
  recording only. The separate progress-note adapter below grants one bounded
  mutation; it does not extend the research proposal allowlist.
- Observability seam: [`langgraph_observability.py`](../../src/unified_agent/langgraph_observability.py).
  Span projection and evaluation sink. LangSmith stays off unless both an
  explicit `ExporterDecision` and `UA_LANGGRAPH_LANGSMITH_EXPORT=1` are set.

See [isolated Python pilot](langgraph-execution-pilot.md).

## Consequences

- Unified-Agent gains resumable cyclic/branching execution without a competing
  task, approval, memory, or audit system.
- The adapter pays a revalidation call at every start/resume and governed tool
  boundary. This latency is accepted to prevent checkpointed authority from
  becoming a stale capability.
- Exact graph pinning makes recovery deterministic but requires old definitions
  or explicit checkpoint migrations to remain available while runs are live.
- Mutating graph nodes beyond the approved progress-note and workflow-merge
  scope remain unavailable until their tool-specific reconciliation behavior
  is reviewed.

## Decision: first governed progress-note write (task #757)

**Date:** 2026-09-04 (CDT) / 2026-09-05 (UTC). **Decided by:** the operator.
**Status:** implementation authorized; merge and deployment require their own gates.
The operator approved the reviewed capability proposal with “proceed. Implementation
is authorized”. This closes the capability-selection gate for this scope only.

The explicit `langgraph_progress_note(task_id, output_digest, run_id=None)` entry
point runs a separate terminal graph. Its only mutation is wrapped `task_update`
with a generated receipt line containing the run ULID and output SHA-256 digest.
The adapter pins the task ID to the run's own claimed task and supplies
`expected_project`, `expected_updated_at`, `worktree=False` and the native
idempotency key. No arbitrary note text or other mutation parameters are accepted.
The task remains `doing`; this graph never calls the research wrapper's
`auto_record` completion path. Normal task snapshot synchronization still applies.

Every first write requires an exact-envelope, one-shot
`langgraph_governed_call` approval even when the global opt-in list is empty.
Profile, policy, ownership, lease, task revision, capability and budget checks
run at the governed boundary. The budget fingerprint is freshly derived from
hub autonomy limits; this graph has one local metadata write and no model or
provider spending. A resume uses the original immutable run and revalidates it;
passing an approval-looking resume value never grants authority. The research
graph's executable definition is unchanged.

The outer effect key and inner `task_update` key are distinct and bound to one
immutable envelope. `task_update` currently commits the task before saving its
native receipt: its idempotency support alone does not close that crash window.
A timeout does not prove that the worker stopped. On an uncertain invocation,
retain `executing`/`unknown`, do not restore approval, and never retry the write
automatically. Reconciliation authenticates and reads the bound native receipt.
A validated receipt proves a historical write and allows the outer ledger to
record success. Missing, corrupt or mismatched receipts remain quarantined;
matching task prose is insufficient evidence. A recovered historical result
never grants current task authority or starts another effect.

The write is terminal because its own task revision changes. Further work needs
a new authorized run. This increment adds no general-purpose mutating proposal
allowlist, status transition, claim mutation, arbitrary task editing, external
write, auto-merge or deployment authority.

Acceptance evidence lives in `tests/test_langgraph_note_dispatch.py`,
`tests/test_langgraph_note.py` and `tests/test_tools_langgraph.py`: exact approval,
rejection of stale/forged authority, durable same-run resume, one append on
duplicate delivery, crash-window quarantine/reconciliation, bounded receipts,
and unchanged task status/claim/worktree. Existing pilot tests cover the retained
research graph contract.

## Decision: second mutating hop + plan→build→verify→merge chain (task #777)

**Date:** 2026-09-06. **Decided by:** the operator (priority dispatch on #777).
**Status:** implementation authorized for a bounded merge-authorization receipt.

Task #757 remains the first mutating node (progress note). Task #777 adds a
separate `governed_workflow` graph:

```text
plan -> build (gated task_update receipt) -> verify -> merge (gated task_update receipt)
```

Every hop is attributed (`run_id`, `claimed_by`, digests). Build and merge each
require an exact-envelope one-shot `langgraph_governed_call` even when the global
opt-in list is empty. Merge is the second mutating node and the named gate: the
chain cannot complete without that human decision.

The merge hop does **not** call `git_merge_task` and does not dispatch agents
("no dispatch without go"). Developer HTTP profiles cannot call `git_merge_task`.
The hop appends a generated merge-authorization receipt to the run's own claimed
task through wrapped `task_update`, with the same native-receipt reconciliation
as #757. Actual git merge stays the existing review/`git_merge_task` path.

After a confirmed build write, the graph adopts that hop's own task revision and
uses it as `expected_updated_at` for merge. A foreign note or owner/lease/profile
change between hops is `stale_authority`. Origin authority (the dispatch-time
run) stays immutable in the checkpoint. Resume never grants approval.

The MCP entry point is `langgraph_workflow_run(task_id, query, run_id=None)`.
The task remains `doing`. Duplicate delivery of either hop returns the bounded
receipt without another append.

Each hop is also a Unified-Agent plan step. `GovernedWorkflowRunner` calls
`plan_step_claim` before the hop and `plan_handoff` after it
([`langgraph_workflow.py`](../../src/unified_agent/langgraph_workflow.py)).
LangGraph checkpoints are not a second plan store. If merge's effect is
durable and the process dies before `plan_handoff`, `_recover_historical`
reclaims the merge step and hands it off so the plan cannot stay `doing`.

Acceptance evidence lives in `tests/test_langgraph_workflow.py`,
`tests/test_langgraph_workflow_dispatch.py` and `tests/test_tools_langgraph.py`.

## References

- [Unified-Agent architecture](../architecture.md)
- [Recording decisions](../decisions.md)
- [TypeScript platform strategy](typescript-platform-strategy-adr.md)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph graph API and retry policies](https://docs.langchain.com/oss/python/langgraph/use-graph-api)
