# Isolated Python LangGraph execution pilot

**Current wiring (2026-08):** LangGraph is a **core** dependency
(`pyproject.toml`, not an extra). `langgraph_run` and `langgraph_checkpoints`
are registered MCP tools on the live hub. The authority table in the
[execution-plane ADR](langgraph-execution-plane-adr.md) still holds: the
runner does not own identity, claims, approvals, or task state.

The rest of this page describes the original library boundary. Treat claims
that the pilot is "unregistered" or installed with `--extra langgraph` as
historical.

The task 586 pilot is implemented in
[`langgraph_pilot.py`](../../src/unified_agent/langgraph_pilot.py). It remains
an execution-plane library. Hub entry is the MCP wrappers in
[`tools/langgraph.py`](../../src/unified_agent/tools/langgraph.py).

Install with `uv sync`. There is no `langgraph` extra.

## Boundary

`PilotRunner` owns only graph routing, interrupts, streaming, and a dedicated
SQLite checkpoint. Callers inject two interfaces:

- `AgentPlanner` proposes one bounded `web_search` call;
- `GovernedGateway` revalidates canonical Unified-Agent authority and submits
  the exact read-only call through the existing policy and approval path.

The runner never resolves an approval. A value passed through
`Command(resume=...)` is discarded. The gateway must return a fresh
authoritative verdict before the graph leaves its approval loop. The library
itself still has no task writer, approval writer, service entrypoint, or
external trace exporter. Hub MCP registration lives in
[`tools/langgraph.py`](../../src/unified_agent/tools/langgraph.py) and records
terminal outcomes through the governed dispatch adapter.

The governed node passes a 20-second attempt timeout to the gateway, where the
HTTP transport can cancel it safely, and retries only the explicit
`TransientGatewayError` class, at most three times after the first attempt. All
attempts reuse one `ua-lg:<run>:<node>:<operation>` idempotency key and persist
a `transient_attempt_count` in checkpoint state so a resume cannot reset the
budget. Validation, authority, approval denial, and malformed-contract errors
never enter that retry policy. CI runs `uv sync` (LangGraph is a core
dependency). The live hub registers the MCP wrappers; the library still does
not spawn a daemon of its own.

The checkpoint defaults to
`${UA_HOME}/langgraph/checkpoints.sqlite3`, is separate from `memory.db`, and is
created with owner-only directory and file modes. Deserialization uses
LangGraph's strict safe-type allowlist with pickle fallback disabled. Failure to
open or read SQLite stops the run; there is no in-memory fallback.
The runner forces LangSmith tracing off around preflight and graph execution,
including when the host has tracing variables set for an unrelated workload.
An injected planner supplies a SHA-256 definition digest, which is incorporated
into the immutable graph digest used for recovery checks.

## Proven behavior

Run the focused acceptance suite:

```bash
uv run pytest -q tests/test_langgraph_pilot.py
```

The suite proves deterministic intake/synthesis, an injected agentic planning
node, the static read-only tool allowlist, approval interrupt/resume, forged
resume rejection, owner-only persistent checkpoints, restart recovery without
replaying completed nodes, stable operation keys, duplicate delivery safety,
authority revalidation at entry and immediately before the tool boundary, and
exact-definition recovery failure. A canonical denial is redacted, checkpointed
as a terminal state, and cannot be reopened by a forged or later resume value.
Exhausted transient retries checkpoint as terminal `failed` and raise
`GatewayExhaustedError` on redelivery. The pilot deliberately proves one
governed collection call; the ADR's parallel multi-source graph remains an
integration definition, not extra pilot authority.

The authenticated hub implementation of `GovernedGateway` is
[`langgraph_dispatch.py`](../../src/unified_agent/langgraph_dispatch.py)
(`UnifiedAgentDispatchAdapter`, task #587). It revalidates identity, profile,
task revision, claim lease, capabilities, and budget from Unified-Agent on
every start/resume and immediately before a governed read. Approvals still go
through `check_approval_gate`; the adapter never resolves them. Task outcomes
and the effect ledger stay in Unified-Agent. The adapter is a hub-side **library**. MCP tools `langgraph_run` /
`langgraph_checkpoints` (`tools/langgraph.py`) are the registered surface.
The adapter still never resolves an approval.

The research pilot does not authorize mutating tool proposals. Task #757 adds
a separately selected `langgraph_progress_note` graph with one approved,
generated task-note append and receipt-based reconciliation. Task #777 adds
`langgraph_workflow_run`: a plan→build→verify→merge chain whose second mutating
node is a gated merge-authorization receipt. It does not call `git_merge_task`.
Hops claim and hand off the matching `plan_*` steps; merge crash-window
recovery syncs that plan step if the process died after a durable merge
effect. See the
[bounded capability decisions](langgraph-execution-plane-adr.md#decision-first-governed-progress-note-write-task-757).
The research graph and its read-only allowlist remain unchanged.

**Resuming a research run (task #2522).** `PilotRunner.load_run(run_id)`
rebuilds a run from its latest checkpoint without executing it and returns a
`LoadedRun` (the run, the checkpoint revision, status, and whether it is
paused). It refuses a missing checkpoint (`CheckpointMissingError`), one from
another contract, graph version, state schema or definition digest
(`DefinitionMismatchError`), and one that cannot be decoded or whose authority
fields are malformed or of the wrong type (`CheckpointCorruptError`). It checks no authority: the
caller revalidates the loaded run through the gateway, which compares the
checkpointed claim, task revision, profile and budget revision with the control
plane and never adopts the current values. `langgraph_run(run_id=...,
resume=True)` takes a lease on the run before loading it, so a second
concurrent resume is refused (`run_conflict`) rather than queued. The lease
lives in the checkpoint store (`RESUME_LEASE_SCHEMA`), and a heartbeat renews
it while the holder runs. The runner's checkpointer (`_FencedSqliteSaver`)
checks the lease token inside each write's own `BEGIN IMMEDIATE` transaction: a
thread with a live lease accepts writes only from the runner fenced on that
token. A holder whose lease expired and was taken over is refused at its next
write and records no completion; the takeover continues the pending node from
the last committed checkpoint, and the governed call's effect ledger returns the
read already made instead of making it again. Every resume, by the owner or the
operator, goes through `ResearchResumeAdapter`, whose single check
(`revalidate_resume_authority`) holds the run to its owner's saved profile and
current ceiling; the operator only triggers it. Neither
`langgraph_dispatch.py` nor any hashed graph node changed, so existing research,
progress-note and workflow checkpoints keep their definition digests.

## Observability seam

Task 588 adds
[`langgraph_observability.py`](../../src/unified_agent/langgraph_observability.py).
It projects pilot stream events into existing `TelemetrySpan` records (run /
chain / tool), applies the shared redaction helpers, and records bounded
evaluation metrics through an injectable sink. Terminal run events
(`run_completed`, `run_denied`, `run_failed`) update the parent run span only;
child spans are emitted for node and approval events. External exporters remain
sinks only. LangSmith stays disabled unless both an explicit
`ExporterDecision` and `UA_LANGGRAPH_LANGSMITH_EXPORT=1` are present.
OTLP/JSON export (`OTLPTraceExporter`, task #668) is a separate opt-in
(`UA_H2_OTEL_EXPORT=1`) with an injectable transport; it does not add the
OpenTelemetry SDK. `export_and_replay` exports then re-scores via
`evaluate_trajectory`.
