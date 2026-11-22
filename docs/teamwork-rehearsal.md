# Teamwork acceptance rehearsal

Task #1456 exercises the existing joint-planning (#1455), task/run (#1436),
message, session and delegation contracts together. It starts separate worker
processes through the production `CodexBackend` and `CursorBackend` adapters.
The workers call `server.REGISTERED_TOOLS`, including the normal profile and
policy gates. Their CLI executables are deterministic fixtures. No model service,
authenticated fleet session, MCP transport or live A2A mesh is exercised.

From a developer checkout with dependencies installed:

```sh
PYTHONPATH="$PWD/src" uv run python scripts/teamwork_rehearsal.py \
  --out /tmp/teamwork-1456-evidence
```

The output directory must be new. Reusing it fails without replacing evidence.
Omitting `--out` creates and retains a fresh directory under the system temporary
directory. The final JSON names that directory. Exit zero means every acceptance
check passed; a failed check returns nonzero and writes a failing `report.json`.
The fixture identities are `agent-codex` and `agent-cursor`; these labels are
bound inside the synthetic store, without contacting or acting as their live
fleet sessions. Both are members of the checked-in platform-engineering office.

## What the run proves

1. Codex proposes one goal-linked plan; Cursor independently accepts its revision.
   A stale revision is refused instead of overwriting the accepted plan.
2. Separate adapter processes atomically claim different tasks and receive distinct
   run IDs. A competing claimant cannot steal the other task.
3. Cursor claims the delegation, then its child process is actually interrupted
   before producing output. A fresh process recalls the durable prior session,
   records abandonment, retries the delegation and receives a higher claim
   generation. The current task run survives renewal. Stale completion and a
   second claim are refused before business output is produced.
4. Cursor writes a structured artifact, records a plan-step handoff, sends one
   typed message and commits a fenced task/run closeout. It then hangs deliberately
   after commit, so the adapter times out before returning the response.
5. A cold process resends the same message ID and finishing idempotency key. The
   message is recognized as a duplicate; delegation completion remains terminal.
   There is one output marker and one finish receipt per run.
6. Codex replays the delivery and plan checkpoint, reads the actual artifact,
   verifies its SHA-256 and correlation ID **before** sending an acknowledgment.
   The acknowledgment points to the handoff message ID. Both tasks end at
   `waiting_review`; this rehearsal grants no human review approval.
7. The event hash chain verifies and the resulting evidence is retained.

This is evidence of durable delivery into the event log and explicit recipient
replay/acknowledgment. It does not establish push delivery, exactly-once arbitrary
external side effects, model-generated cooperation, provider availability, or
live fleet restart continuity. Two execution attempts occur around interruption;
only the fenced successor produces business output. Response-loss recovery
replays durable operations without re-running artifact production.

## Evidence and negative controls

`report.json` includes adapter/identity bindings, statuses (`success`, `killed`,
`timeout`), goal/plan/task/run/delegation IDs, both claim generations, conversation
and message IDs, artifact digest, run outcomes and event-chain verification.
`script_sha256` identifies the exact rehearsal source bytes.

The same bundle contains `store/memory.db`, `events.json`, `workspace/result.json`
and `runs/*.json`. The last files capture fixture progress at the injected crash
boundaries; they are diagnostic evidence, not a second task or run authority.
The SQLite event log remains the authority. Inspect with ordinary SQLite tools,
then remove the synthetic bundle when it is no longer needed.

These controls must return nonzero:

```sh
uv run python scripts/teamwork_rehearsal.py --fault corrupt-artifact \
  --out /tmp/teamwork-1456-corrupt
uv run python scripts/teamwork_rehearsal.py --fault deny-claim \
  --out /tmp/teamwork-1456-denied
```

The first proves the receiver refuses a mismatched artifact before acknowledging
it. The second installs a deny rule only in the synthetic policy file and proves
registered calls cannot bypass it. Neither changes a live policy.

The launcher constructs an allowlisted child environment. Ambient hub URLs,
bearers, provider keys, executor configuration and org overrides are omitted.
HOME and the XDG configuration/cache/data roots are explicitly set to fresh
fixture directories, so home-relative defaults cannot fall back to the real
operator home. All state/workspace/vault/cache/policy paths point into the new bundle; event-bus
publication and vault synchronization are disabled. Socket connection and DNS
resolution are refused inside the fixture processes. Delegation approval baseline
and spec enforcement are explicitly disabled **only in this synthetic store**.
Live approval, authentication and deployment isolation therefore remain outside
this proof. No fixture uses a real harness binary or changes fleet configuration.

Run the acceptance and its negative controls with:

```sh
PYTHONPATH="$PWD/src" uv run pytest tests/test_teamwork_rehearsal.py -q
```

A later live rehearsal needs named participants with their existing verified
sessions, usable harness/provider authentication, and explicit authorization for
synthetic hub tasks and messages addressed to those participants. #1443 owns mesh
configuration and identity changes. Keep its live reachability and provider
results separate from this report; `live_models_verified` and `live_a2a_verified`
remain false here.
