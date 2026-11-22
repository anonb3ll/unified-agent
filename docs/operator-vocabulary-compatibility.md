# Operator vocabulary migration

New task writes and queue responses use `operator-only`, `operator_only`,
`awaiting_operator`, and `waiting_on_operator`. These names describe an actor
lane and decision queue; they do not grant operator authority or change any
approval, authenticated identity, explicit claim, or dispatch rule.

On database initialization, the retired owner lane is remapped in place to
`operator-only`. Only `tasks.lane` changes. IDs, timestamps, claims, leases,
run bindings, evidence and immutable events are preserved. The migration is
idempotent. Canonical task reads and filters also accept rows that have not yet
been migrated. The operator lane remains outside automatic pickup; an explicit
lane claim is still required under the existing claim policy.

For one release, the explicit legacy spellings in
`config.LEGACY_OPERATOR_*` remain accepted for lane requests and emitted as
queue/count/digest response aliases. They are compatibility debt and remain
visible in the privacy inventory. Legacy lane values are never written by new
requests. Clients must use the canonical bucket or its legacy alias once, not
concatenate them or sum both counts. Total active counts include each task once.

The canonical SDK parses either old or new queue responses into `operator_only`;
an explicitly supplied canonical array/count takes precedence, including empty
arrays and zero. An invalid canonical value fails validation instead of falling
back to the alias. The web overview uses the same normalization. Attention
responses emit `awaiting_operator`; the web accepts the retired kind from older
hubs. Other clients that dispatch actions by the retired attention kind must
upgrade. No historical evidence or note text is rewritten.

## Rolling back

A binary that predates this migration does not accept `operator-only` for
explicit claims. Before starting one:

1. Drain writers. Stop the hub service and every executor or node worker that
   writes to the store, and confirm their processes have exited. A status row
   alone does not prove a worker is idle.
2. Back up the store with the installation's normal procedure.
3. Remap only the canonical lane back to the retired value named by
   `config.LEGACY_OPERATOR_LANE`, with the hub still stopped:

   ```bash
   uv run python - <<'PYTHON'
   import sqlite3
   from unified_agent import config

   # db_path() resolves UA_HOME (default ~/.unified_agent), so no exported
   # variable is required.
   with sqlite3.connect(config.db_path()) as db:
       db.execute(
           "UPDATE tasks SET lane=? WHERE lane=?",
           (config.LEGACY_OPERATOR_LANE, "operator-only"),
       )
   PYTHON
   ```

   Claims, leases, run bindings and events are untouched. Never map the lane
   to `default`: that would expose human work to the shared dispatch pool.
4. Start the old binary. Do not start the new one first; it would run the
   forward migration again on initialization.

Rolling forward again needs no step: initialization re-applies the migration.
Removal of compatibility aliases requires a separate release and coordinated
client rollout.

## Deployment order

The web overview parses queue health through the SDK's
`QueueHealth` schema, which carries the `operator_only` alias handling. Ship
the `packages/protocol` and `packages/client` change with the hub and web
bundle, and deploy them together. Older hubs keep working with the new web build because the SDK
accepts both spellings.
