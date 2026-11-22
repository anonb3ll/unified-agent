# Approved CLI expansion

The operator approved this plan on 2026-10-07 and then specified gateway
support, including desktop agents using the Mac hub. The live
roadmap is #1935, under mission #1450; this document describes its intended
architecture, not live task status. Check the hub for ownership and delivery.

## Architecture

Build a curated CLI over one versioned platform command contract. Generate schema
validation, discovery and SDK bindings from platform declarations; hand-design
the common operator workflows. A stable command has a public name/version,
input/output schema, profile gate, side-effect/risk annotation, approval behavior
and backing writer. Python function names remain implementation details.

Reuse remodel #1434 for this contract. Its first slice preserves every existing
registration and permission, with parity tests and no tool-semantic changes.
Its existing #1433 sequencing remains intact. The initial CLI client consumes
authorized MCP schemas/readers while that remodel is incomplete and explicitly
reports the platform command version as unknown. It does not create another
platform authority or modify MCP registrations.

Human and automated callers use the same canonical hub over authenticated
HTTP(S)/MCP gateway connections. Identity follows credentials, including existing
device sign-ins and session credentials; flags never elevate authority. The Mac
hub owns shared tasks, memory, plans, approvals, runs and evidence. A satellite
client cannot substitute local SQLite or direct in-process execution when a
gateway connection fails.

Use readable summaries, completion, review previews and control-center links
for operators. Offer strict structured inputs, versioned JSON results, explicit
failure outcomes, noninteractive operation and resumable JSONL events for agents.
Keep stdout machine-clean and send diagnostics to stderr. Optional natural-language
assistance produces inspectable typed proposals through these same gates.

## Delivery slices

1. **#1936 — Client foundation.** A lightweight `ua` entry point, explicit gateway
   context, credential-verified identity, live authorized discovery, a client
   result/error contract and curated reads. Preserve every legacy script and
   bare `unified-agent` startup. Prove help/import isolation, strict input,
   pagination, terminal rendering, refused credentials/profiles, bounded
   transport and real desktop-to-Mac read behavior.
2. **#1937 — Daily work.** Task creation/claim/update, explicit mailbox
   read/ack/reply, run/approval inspection and evidence retrieval. Preserve
   authenticated actor attribution, project/session guards and existing
   revision/run preconditions. Advertise idempotency only when the server
   implements it. Complete one work-discovery → claim → coordination → run →
   approval → evidence journey. Message content is never implicit authority.
3. **#1938 — Controlled execution.** Delegation, dispatch, workflows and
   intervention through existing gates. Add server-enforced idempotency with
   lookup/recovery, revision/lease fencing, durable receipts, bounded waits and
   cursor-based JSONL progress. Distinguish completed, rejected, conflict,
   awaiting approval and outcome unknown. A dropped response after commit must
   reconcile without issuing a duplicate effect. Stopping a client wait does
   not cancel the run. Coordinate required declarations with #1434.
4. **#1939 — Operator polish.** Completion, meaningful help/examples, task
   owners/blockers/next actions, evidence links, named non-secret contexts and
   a TUI consuming the same client. Preserve specialty scripts as compatibility
   wrappers. Add optional typed natural-language proposals after the deterministic
   contract is established. SDK changes happen in the canonical SDK repository,
   then enter the mirror through its supported sync script.

The hub records dependencies #1937 → #1936, #1938 → #1937, #1939 → #1938. Each
slice delivers a reviewable PR in its own claimed task worktree, with focused
local checks, current-head review and CI, matching approval evidence where
required, and authorized serialized deploy-steward delivery. Local success does
not claim that new CLI code is installed on every device or deployed to the hub.

## Verification requirements

The critical checks span adapters: the same credential sees the same authorized
surface, reaches the same tool/resource gate and leaves the same attributed
evidence regardless of presentation. Discovery is not permission to execute a
tool. Session-scoped credentials retain their grant and expiry; an agent does
not inherit human authority or a different session's profile overlay.

Exercise invalid and extra parameters, string booleans, flag-shaped prompts,
concurrent claims, stale revisions/leases, refused credentials, redirection,
revocation, paginated results, schema drift, dropped committed responses and
event-stream reconnection as each relevant slice is implemented. Gateway tests
must demonstrate actual remote reads plus fail-closed behavior, rather than
only a reachable URL or a configured token file. Approval actions requiring
verified human presence use the existing browser handoff, with `--yes` affecting
only a client confirmation prompt.

See [the initial CLI reference](../cli-expansion.md) for implemented syntax and
the exact limits of the first client contract.
