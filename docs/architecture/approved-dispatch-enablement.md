# Approved-dispatch enablement and rollback (task #551 / live proof #567)

Status: operator procedure (dry-run / rollback / live soak).
Owner lane: agent-cursor.
Last updated: 2026-08-27.

**Live Mac (2026-08-24):**
`system_doctor.dispatch_scheduler` is **LIVE** (`UA_DISPATCH_SCHEDULER_ENABLED=1`).
That allows CLI ticks; the hub does **not** auto-poll. Scheduler ticks still
use `StubProcessSpawner`. Control Center `POST /api/action/dispatch` uses
`CliProcessSpawner` and starts the named agent's headless CLI (task #568 /
PR #285). First scheduler soak: `run-582-fe906b87` on child **#582**
(parent #567), stub path. Do not treat LIVE as unattended Kanban CLI launch.

## What this controls

| Piece | Authority | Default |
| --- | --- | --- |
| Task / delegation / approval / events | Mac hub `memory.db` only | always on (existing) |
| `dispatch_runs` index | Same DB; not a second mailbox | always on when coordinator runs |
| Scheduler poller | `UA_DISPATCH_SCHEDULER_ENABLED` | **off** (`0`) in code default; Mac hub is LIVE |
| Scheduler spawn | `UA_DISPATCH_REAL_SPAWN` | **off** (`0`) — `StubProcessSpawner`, no real CLI on ticks (task #599) |
| Scheduler node routing | `UA_DISPATCH_NODE_ROUTING` | **off** (`0`) — wraps the spawn boundary in `NodeRoutedLaunchBoundary`; labels bound to a registered ua-node launch on that device (task #1327, I1) |
| Concurrent live runs | `UA_DISPATCH_MAX_LIVE_RUNS` | `2`, hard cap `16` — real spawn and node-routed launches (task #599) |
| Control Center Dispatch | `CliProcessSpawner` | native headless CLI (task #568), unaffected by the flag |

### The two flags are independent, and both must be on

`UA_DISPATCH_SCHEDULER_ENABLED=1` makes ticks *run*. `UA_DISPATCH_REAL_SPAWN=1`
makes a tick *start a process*. With the scheduler live and real spawn off —
the Mac hub's state as of 2026-08-25 — a tick claims work, takes a lease,
resolves the route, and launches nothing: `stub: True`, `spawned: False`,
`pid: None`. The state machine is exercised end to end against a no-op.

That was the only available behaviour before task #599: `RoutedLaunchBoundary`
hardcoded `StubProcessSpawner`, and the sole construction of
`CliProcessSpawner` anywhere in the codebase was the Control Center click path
in `webui/api.py`.

Control Center dispatch deliberately ignores `UA_DISPATCH_REAL_SPAWN` and
always uses the real spawner. A human clicking a button is a different
authorization from an unattended poller; the flag governs only the latter.

Only the exact string `1` enables real spawn — `true`, `yes`, and `TRUE` do
not, matching `UA_DISPATCH_SCHEDULER_ENABLED`. The same parsing applies to
`UA_DISPATCH_NODE_ROUTING`.

### Node routing (task #1327, I1)

`UA_DISPATCH_NODE_ROUTING=1` wraps the tick's launch boundary in
`NodeRoutedLaunchBoundary`. For a delegated label with no registered ua-node,
everything defers to the inner boundary unchanged. For a label WITH a node,
the launch is fail-closed: it needs a node heartbeat fresher than 90 s that
advertises the route's backend, else the launch errors
(`node_offline:*`, `node_backend_unavailable:*`, `node_no_worktree_root`) and
the run goes through the ordinary retryable/launch_failed path — never a
silent hub-local spawn. A routed launch enqueues one `node_dispatch` item
carrying the run id; the node's claim moves the run to `running` and the
node's heartbeats keep its lease alive, and its receipt closes the run through
`record_run_outcome`. If the run's lease expires with the item still queued it
is cancelled and the run retries (`node_unclaimed`); if the item was claimed,
the run fails `lease_expired_outcome_unknown` — a lost heartbeat does not
prove the backend stopped, so the outcome must be reconciled, never silently
relaunched, and `resume_run` refuses that failure reason outright
(`outcome_unknown`) rather than risk overlapping a second execution.

Claim visibility and cancellation follow the run's own lifecycle: a queue item
is not claimable until its linked run is `launched` (the boundary commits the
item before the coordinator writes `launched`, and a run that dies while still
`claimed` is settled by reclaim → `node_unclaimed`), and `terminate_ours`
recognizes `node:` evidence — a queued item is cancelled and cessation is
confirmed immediately, while a claimed item gets `cancel_requested` relayed on
the node's next heartbeat and only the node's receipt confirms cessation (via
`finalize_cancellation`). Workspace policy is unchanged by routing: the
boundary runs `resolve_workspace_for_route` before enqueueing, so a denied
binding (e.g. an unbound device-scoped `Laptop_*` label) fails the launch
`workspace_denied` instead of shipping work to a device — give the label an
explicit `UA_AGENT_WORKSPACES` binding to route it.

### Concurrency ceiling

With real spawn on, a launch is refused (`max_live_runs_reached:<live>/<cap>`)
once `UA_DISPATCH_MAX_LIVE_RUNS` runs are already in state
`claimed`/`launched`/`running`. The count is read from `dispatch_runs`, not
from process memory, because ticks, Control Center, and the CLI are separate
processes and an in-process counter would undercount. The check runs *before*
the spawn, so a refusal never leaves an orphan child.

A non-positive or unparseable value falls back to the default rather than
disabling the ceiling — a misconfigured cap must not mean "no cap". The stub
is exempt: it starts nothing, and capping it would change existing tick
behaviour for no safety gain.

Note this is a host-wide ceiling. A separate partial unique index
(`idx_dispatch_runs_live_task`) already enforces at most one live run per task.

Disabling the scheduler does **not** delete evidence, tasks, delegations,
`dispatch_runs`, or `agent.dispatch.transition` events. It only stops new
claims/launches from the poller.

## Dry-run (no production authority)

1. Leave `UA_DISPATCH_SCHEDULER_ENABLED` unset or `0` (doctor reports
   `dispatch_scheduler` = **DORMANT**).
2. Run the focused smoke suite (fake worker only):

   ```bash
   uv run pytest -q tests/test_dispatch_e2e_smoke.py tests/test_dispatch_scheduler.py \
     tests/test_dispatch_coordinator.py tests/test_dispatch_routing.py
   ```

3. Read-only operator probes (no claims, no launches):

   ```bash
   unified-agent-dispatch-scheduler ready --json
   unified-agent-dispatch-scheduler runs --json
   unified-agent-dispatch-scheduler observe <run_id> --json
   ```

4. Inspect telemetry (same fields on events, notes, and CLI):

   ```bash
   unified-agent-dispatch-scheduler runs --json
   unified-agent-dispatch-scheduler observe <run_id> --json
   ```

   Or query `event_log_query(channels=["agent.dispatch.transition"])` and
   task notes carrying `[dispatch: …]`.

## Stateful probe (mutates hub DB — not a dry run)

`tick --force` is for **tests and explicit operator experiments only**. It
can claim approved delegations, consume one-shot approval passes, increment
`delegations.attempts`, and mint live `dispatch_runs` rows. Scheduler ticks
still use `StubProcessSpawner` and do not exec a CLI. A Control Center
Dispatch click is the real-CLI path. Do **not** run `tick --force` against
production-approved work during a dry-run checklist.

```bash
# Requires an isolated test DB or the operator-reviewed soak task.
unified-agent-dispatch-scheduler tick --force --json
```

`--force` does not persist scheduler enablement and must not be wired as a
cron substitute without the operator's review.

## Safe enablement (the operator-gated only)

Only after human review of #548–#551 and soak on the Mac hub:

1. Set in the hub environment file (not a second service authority):

   ```bash
   UA_DISPATCH_SCHEDULER_ENABLED=1
   # optional tuning
   UA_DISPATCH_SCHEDULER_MAX_ATTEMPTS=3
   UA_DISPATCH_SCHEDULER_BATCH_LIMIT=10
   UA_DISPATCH_SCHEDULER_LEASE_TTL_S=300
   ```

2. Reload the unit that owns that env (the operator performs restart — agents do
   not). Confirm `system_doctor` / `_dispatch_scheduler_check` reports **LIVE**.
3. Prefer an explicit timer/cron that calls
   `unified-agent-dispatch-scheduler tick` over embedding a second ready-set
   in Hermes/Kanban. Hermes may *poll* this queue; it must not own a parallel
   mailbox.

4. Before any tick, run `ready --json`. Abort if the ready set contains work
   you did not intend to claim — a tick claims every ready candidate up to
   `UA_DISPATCH_SCHEDULER_BATCH_LIMIT`. Prefer task-pinned
   `claim_and_dispatch` for soak/proof scripts (see `scripts/dispatch_live_soak_567.py`).

5. Live soak #567 uses a **two-phase** operator script — it never appends
   `[plan: approved @ …]` itself. Run `setup`, have a human steward approve the
   labeled child, then run `tick`.

## Closing the loop (tasks #601–#603)

Spawning was only one of three links. Until #601 landed, a real spawn reached
`launched` and stopped there: `record_run_outcome` had exactly two callers —
`worker.py` (the #551 smoke worker, which "never execs real agent CLIs") and
the `max_attempts_exceeded` dead-letter path. Neither fires for a healthy real
run.

| link | what provides it |
|---|---|
| a tick *can* exec a real CLI | `UA_DISPATCH_REAL_SPAWN` (task #599) |
| a finished run *reports back* | `dispatch report` CLI + `dispatch.report` (task #601) |
| a live run is *not* reclaimed mid-work | liveness-checked reclaim (task #602) |
| something *invokes* the tick | `ua-dispatch-tick.timer` (task #603) |

### Completion reporting

A dispatched session identifies its own run from the environment
`RoutedLaunchBoundary` already gives it (`UA_DISPATCH_RUN_ID`,
`UA_DISPATCH_AGENT`). There are two self-report paths; both land on
`record_run_outcome`:

| Path | When to use | Constraint |
|---|---|---|
| CLI `unified-agent-dispatch-scheduler report` | Child can write `$UA_HOME/memory.db` (Codex/Agy/Cursor/Hermes, or a non-sandboxed local CLI) | Reads `UA_DISPATCH_RUN_ID` from the child env. Exit 2 if that env is missing; exit 1 for `already_terminal` without mutating. |
| MCP `dispatch_report_self` | **Required for unattended Claude** (task #623) | Caller must present a hub bearer matching the run's `agent_label`. `run_id` is required (hub process env is not the child's). `finished` needs non-empty `evidence`. |

Claude dispatch cannot use the CLI report: the fail-closed sandbox denies
`~/.unified_agent`, so the child cannot open the hub database. The spawner
writes `$UA_HOME/dispatch-runs/<run_id>.ua-dispatch.json` and appends report
instructions to the prompt. Identity is also bound as a single-agent
`UA_AGENT_BEARER_TOKEN` — never the full `UA_AGENT_TOKENS` map. See
[Security model](../security.md#approved-dispatch-child-isolation).

```bash
# Works when the child can reach the hub DB (not Claude sandbox):
unified-agent-dispatch-scheduler report finished --evidence "$PR_URL"
unified-agent-dispatch-scheduler report failed --reason "tests failed"

# Claude (and any sandboxed child) must call the hub tool instead:
# dispatch_report_self(state="finished", run_id="<run_id>", evidence="<sha or PR>")
```

A supervisor still reports from the child's exit code when the agent never
self-reports. Duplicate reports are refused as `already_terminal` (CLI exit 1)
without mutating — the supervisor and the agent both reporting is the design
working, not a fault.

Child environment is an allowlist (`dispatch/child_env.py`). Do not assume a
dispatched CLI inherited hub secrets, `SSH_AUTH_SOCK`, or
`UA_CLAUDE_SKIP_PERMISSIONS`. Claude spawn writes a per-run settings file and
clears `--setting-sources` so `~/.claude/settings.json` cannot weaken the
sandbox.

### Why reclaim no longer kills live work

The claim lease is 300s; a real agent session routinely runs longer. Reclaim
previously checked pid liveness **only** for state `claimed` — the narrow
Popen-to-launched window — so every real spawn hit the timeout branch at lease
expiry, released its delegation back to `pending`, and let the next tick launch
the same task alongside the agent already doing it.

Reclaim now renews the lease for a `launched`/`running` run whose recorded
process is still alive **and still ours**, bounded by `UA_DISPATCH_RUN_MAX_S`
(default 3600s; `0` disables the ceiling). Past that ceiling the run is timed
out and the tick terminates the process — SIGTERM, then SIGKILL after a grace
period, never SIGKILL first.

"Still ours" matters because `os.kill(pid, 0)` answers "is *a* process alive".
The evidence token is now `pid:<n>:<start>`, where `<start>` fingerprints the
process start time via `ps -o lstart=` (portable to the macOS hub, which has no
`/proc`). Legacy `pid:<n>` rows still parse and degrade to liveness alone.

### Enabling the timer

`ua-dispatch-tick.timer` ships **installed-disabled**, like every other unit in
`deploy/systemd/`. It fires every 2 minutes and runs
`dispatch-tick-cron.sh`, which deliberately omits `--force` — so with the
scheduler flag off, a firing tick is a no-op.

```bash
systemctl --user daemon-reload
systemctl --user start ua-dispatch-tick.service   # one tick, verify output
systemctl --user enable --now ua-dispatch-tick.timer
```

On the authoritative Mac hub, use the LaunchAgent twin in
`deploy/mac-hub/` (`com.unified-agent.dispatch-tick.plist` +
`run-dispatch-tick.sh`). It fires every 120s, sources
`~/.unified_agent/service.env`, and is installed-disabled until an operator
loads it — same contract as the systemd timer.

The repo file is a template (`__HOME__` stands for the account's home), so
render it into `~/Library/LaunchAgents/` and bootstrap the installed copy, never
the repo file:

```bash
python3 ~/dev/unified_agent/deploy/mac-hub/render_launchagent.py \
  ~/dev/unified_agent/deploy/mac-hub/com.unified-agent.dispatch-tick.plist \
  ~/Library/LaunchAgents/com.unified-agent.dispatch-tick.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.unified-agent.dispatch-tick.plist
launchctl enable "gui/$(id -u)/com.unified-agent.dispatch-tick"
```

`system_doctor` reports `dispatch_tick_timer` so the poller's state is visible
rather than something you have to already know to check.

Order matters: do not enable the timer before completion reporting and
supervision are in place. A poller on top of a run that never reports finished
and loses its lease at 300s is an automatic duplicate-agent generator.

Control Center Dispatch already starts a native CLI (#568) and is unaffected by
`UA_DISPATCH_REAL_SPAWN`. #551's fake worker is test-only. Live soak #567 used
the scheduler stub path.

## Rollback (kill switch)

1. Set `UA_DISPATCH_SCHEDULER_ENABLED=0` (or remove the variable) and reload
   the hub unit env (the operator).
2. Confirm doctor reports **DORMANT**. Further `tick` without `--force` is a
   no-op (`status=disabled`).
3. Leave rows and evidence in place:
   - `dispatch_runs` history
   - `[dispatch: …]` task notes
   - `agent.dispatch.transition` events
   - child task `evidence_ref` values

   Do **not** wipe `memory.db` or invent a second dispatch queue to "clean up."

4. In-flight `claimed` / `launched` / `running` leases expire to `retryable`
   via `reclaim_expired_leases` on a later forced tick or coordinator call;
   they will not relaunch while the scheduler stays disabled (unless an
   operator runs `--force` or calls `claim_and_dispatch` directly).

## Observability fields

| Field | Where |
| --- | --- |
| `task_id` | events, `dispatch_runs`, notes, `observe` |
| `agent_label` | events, `dispatch_runs`, notes, `observe` |
| `run_id` | events, `dispatch_runs`, notes, `observe` |
| `state` | events, `dispatch_runs`, `observe` |
| `last_error` / `failure_reason` | events, `dispatch_runs`, notes, `observe` |
| `retry_count` | events (`retry_count`), `delegations.attempts`, notes (`attempts=`), `observe` |
| `evidence_location` / `evidence_ref` | events (on finish), `dispatch_runs`, notes, `observe` |

## Related

- Contract: [`approved-dispatch-state-machine.md`](./approved-dispatch-state-machine.md)
- Isolation: [Security model — child isolation](../security.md#approved-dispatch-child-isolation)
- CLI: [cli-reference.md](../cli-reference.md#unified-agent-dispatch-scheduler)
- Package: `src/unified_agent/dispatch/`
- Smoke: `tests/test_dispatch_e2e_smoke.py`
- Sandbox tests: `tests/test_dispatch_sandbox.py`
