# Costs and budgets (task #1348)

The hub rolls up what agent work cost and can stop an agent from claiming new
work once its budget is spent. It adds no table and no scheduler: cost is read
from records the platform already writes, budgets live in the reviewed policy
file, and enforcement sits in the claim path.

## Where cost comes from

| Source | Record | What it carries |
|---|---|---|
| Finished self-pickup runs | `event_log` channel `agent.run.selfpickup`, `selfpickup.finished` (see [self-pickup-runs.md](self-pickup-runs.md)) | The run's `run_usage`: provider, model, tokens, `cost_usd`, `billing`, `phases`, `contended`. It comes from the finishing `task_update`, the run's last heartbeat, or both (the finish wins per field) |
| Open self-pickup runs | `run_usage_heartbeats` (#1681) | The cumulative `run_usage` reported by a live renewal or authenticated usage-only report. Finished runs are excluded from open spend; late reports overlay their immutable finish, never count twice |
| Hub-brokered LLM calls | `llm_broker_spend` (J7 #1326, [llm-provider-broker.md](llm-provider-broker.md)) | Actual cost once reconciled; the reservation while a call is unfinished or its outcome uncertain |

A run reports usage only if something sends it. Before task #1917 nothing in
`executor/` or `dispatch/` did, so every executor-launched Claude Code or Codex
run counted only `run_reserve_usd`. The executor now parses what the harness
printed and sends it as the run's heartbeat (see [Executor runs](#executor-runs)).
An agent working by hand still reports only what it passes itself.

`usage_events` ([usage_ledger.py](../src/unified_agent/usage_ledger.py)) is not
read here. Its rows are provider calls the hub itself made: the broker's, which
`llm_broker_spend` already counts, and the hub's own `llm_client` and Gemini
calls, which no agent's run made. A harness talks to its provider directly, so
its spend never appears in `usage_events`; reading both would count broker calls
twice.

Every finished run falls in exactly one cost class:

- **priced**: reported a `cost_usd`, has no incomplete-pricing flag, and is not a subscription or local run.
- **unknown_cost**: reported no cost or explicitly set `cost_complete: false`.
  A mixed retry retains the sum of known `cost_usd` as a lower bound plus its
  unknown-cost reserve. An executed attempt with no usage-bearing output is
  also persisted as an unknown-price marker. Missing telemetry never turns
  into zero cost or a fully priced run when a later retry reports a price.
  `false` remains sticky through cache, heartbeat, finish and historical overlays.
- **subscription**: `billing: "subscription"`. Native subscription usage has no
  USD price; its tokens are reported separately and never folded into dollars.
  The lane's remaining quota comes from `quota_observe` and is `unknown` unless
  there is evidence.
- **local**: `billing: "local"` (a local model). Zero API cost by definition.

`phases` (`{phase: seconds}`) records where a run's time went; `contended`
marks a run whose subscription lane was queued or rate-limited rather than
merely slow. Both are rolled up per group.

## Budgets

Declare budgets in `policy.yaml` (`UA_POLICY_FILE`, default
`~/.config/agent_hub/policy.yaml`), beside the tool policy rules:

```yaml
budgets:
  - name: codex-monthly
    agents: [agent-codex, agent-server-agent2]   # omit (or ["*"]) for every agent; never empty
    period: month                           # month | day, UTC
    limit_usd: 200
    warn_pct: 80                            # default 80
    run_reserve_usd: 2.5                    # default 0
    action: block                           # block (default) | warn
```

For an agent in the current period:

```
committed = known API cost of its finished runs (including partial prices)
          + run_reserve_usd x unknown-cost finished runs
          + for each doing task it is running: max(run_reserve_usd, cost its open run reported)
          + cost reported by its other open runs (lease lapsed, not yet finished)
          + its broker spend (actual, or the reservation while unreconciled)

For an explicitly incompletely priced open run, hold known cost + reserve,
including after its lease lapses; do not replace the unknown part with its
known partial price. Finished incomplete runs hold known USD + reserve too.
```

An open run's reported cost counts from the heartbeat that reports it, not from
the finish. This matters for executor runs: the executor's finish leaves the
task in `doing` awaiting review, so the run stays open until review moves the
task on, and its cost would otherwise be invisible until then. Subscription and
local heartbeats carry no USD price, as with finishes. `budget_status` reports
the open-run part as `open_run_cost_usd`.

- A new claim (`task_claim`, or `task_update(status="doing")`) is **refused**
  under a `block` budget when `committed` has reached `limit_usd`, or when
  `committed + run_reserve_usd` would exceed it.
- "Doing tasks it is running" counts tasks it claimed and tasks it moved to
  `doing` through `task_update` (which hold its open run but no `claimed_by`).
  Claimed tasks reserve funds only while their lease is active (`expires_at > now`
  or no expiry); the reserve ends at the expiry instant. Actor-backed unclaimed
  runs continue to reserve funds until their recorded finish. A move into
  `doing` with no verified identity opens its run with no agent (task #1886),
  so it reserves nothing for the label passed as `actor`.
- When the claim would take `committed` past `warn_pct` of the limit, it succeeds and the result carries
  `budget_warnings`. A `warn` budget never refuses.
- A lock renewal by the agent already holding the task is never refused.
- A renewal that carries `run_usage` is judged with that usage counted
  (task #1917). Past `warn_pct` it returns `budget_warnings`. Once `committed`
  goes past `limit_usd` under a `block` budget it returns `budget_stop`, the
  reason the run should stop, and writes `budget.run_stop` once per budget,
  agent and period. The renewal itself still succeeds, so the runner keeps the
  lease it needs to stop cleanly. Reserves alone filling the budget exactly do
  not stop a run, because claims are admitted up to that point.
- The check runs inside the claim's `BEGIN IMMEDIATE` transaction, so
  simultaneous claims serialize against the same remaining budget: each
  claim's reserve is visible to the next.
- Each warning or refusal is written once per budget, agent and period to
  `event_log` channel `agent.budget` (`budget.warning`, `budget.blocked`,
  `budget.run_stop`), so
  an executor retrying every poll tick does not flood the ledger.
- A malformed `budgets` block **fails closed**: every claim is refused with the
  parse error until the file is fixed, because which agents a broken block
  governs cannot be known. Without a `budgets` block nothing is read.

Budgets stop new claims. They do not kill a run already in flight: a
`budget_stop` is advice to the runner, and the executor does not yet act on it
mid-run (below). Stopping a live run by hand is the runtime-aware control in
#1351.

## Executor runs

The executor daemon (`executor/daemon.py`) parses usage from its harness's
output (`executor/backends.py`) and, after the child exits, reports it as the
run's accounting snapshot: `task_claim(usage_only=True)` fenced to the original
run and its exact authenticated actor, carrying cumulative `run_usage`. This
path changes no task, lease, finish outcome, or successor. Distinct attempt
totals persist across daemon restarts; pending run and cycle usage survives
transient hub failure and retries with durable idempotency. A budget stop
terminates retry scheduling, and a later live renewal re-checks stored spend
before the executor starts another attempt.
It does so on every outcome, including a failed, timed-out or killed run, and
logs any `budget_warnings` or `budget_stop` the accounting response returns.

| Backend | Source | Tokens | `cost_usd` |
|---|---|---|---|
| Claude Code | `result` event of `claude -p --output-format stream-json`; a run cut short sums each assistant message's `usage` once per message id | yes | `total_cost_usd` when the CLI prints it; none for a run cut short |
| Codex | `turn.completed.usage` of `codex exec --json`, summed over turns | yes | none: the CLI prints no price, so the run stays unknown-cost and keeps its reserve |
| Agy | `result.usage` of `agy -p --output-format stream-json`, summed over auto-resumes | yes | none |
| Cursor, Copilot, Hermes, Devin, Pi | not parsed | no | no |

`input_tokens` counts every prompt token, cached or not (Claude Code reports
its cache reads and writes apart from `input_tokens`; they are added in). The
cache counts stay in the backend result: `run_usage` has no field for them.
The model is the one the harness names, else the run model the daemon resolved.
`billing` is what the CLI says about its own login (task #1837), asked under
the child's environment and working directory (project settings can select
another credential) by `harness_billing.probe` once per run (no cache, so a
changed login is seen on the next run): `claude auth status` (`authMethod`
`claude.ai` is `subscription`, but only when the run's own `init` event
reports `apiKeySource: "none"`, the OAuth login; `api_key` and `third_party`
are `api`) and
`codex login status`, read from stderr (`ChatGPT` is `subscription`,
`API key` is `api`). Because `codex login status` names the login even when
a session bills another provider (a `-c model_provider` override, a profile,
a system or project config layer), a ChatGPT answer counts as
`subscription` only when the measured session's own rollout records
`session_meta.model_provider = "openai"`: the executor finds it from the
run's `thread.started` id under the child's `CODEX_HOME`. A resumed Codex run
has no such evidence (Codex keeps the original `session_meta` on resume) and
gets no `billing`. Without that evidence, or when the child holds `CODEX_API_KEY`,
`OPENAI_API_KEY` or `OPENROUTER_API_KEY`, a Codex run has no `billing`. Any other answer,
or a probe that fails, also leaves `billing` unset. `total_cost_usd` is the
CLI's own API-price estimate and is printed under a subscription login too;
with `billing: "subscription"` the run counts as subscription and charges no
dollar budget, and its bound cycle gets its tokens but no `cost_cents`.
Without `billing`, the estimate is charged as API spend, which overcounts
rather than undercounts. Across retried attempts of one run, the total keeps a
bill only when every attempt reported the same one; otherwise it is `api`,
so a subscription attempt never exempts another attempt's API spend, and an
attempt without billing evidence leaves the total incompletely priced
(unknown cost, reserve kept). If the attempt totals cannot be read back, the
report is `api` with incomplete pricing. On the hub, a run once charged as
`api` stays `api`: a later or replayed report cannot exempt it.

The same run's tokens (input plus output) and cost (in cents) are added to its
bound autonomous cycle, if any, beside the wall-clock time and retry the daemon
already recorded, so a cycle's `max_tokens` and `max_cost_cents` now trip on
executor runs. Usage reaches the bound cycle before an ownership-loss return;
a stopped cycle accepts authenticated, bound-run accounting without changing
its terminal state or stop reason. Each attempt has a durable idempotency key.

The executor reports once, after the child exits; it sends no heartbeat while
the child runs, so a `budget_stop` cannot yet interrupt a run in flight. Its
lease (`lock_timeout_s`, default one hour) already outlasts the default run
timeout, and Claude Code prints its cost only at the end of a run.

## Interactive sessions (`ua-usage`)

Most runs are interactive: an agent claims a task in its own harness session
and closes it with `task_update`. Nothing measured those, so they finished
unknown-cost. `ua-usage` (task #1837) reads the session's model calls from the
harness's local store and prints the `run_usage` for one run's window:

```bash
ua-usage --since <the run's claimed_at>     # inside Claude Code: session detected
ua-usage --harness codex --session <id> --since <claimed_at> --until <finished_at>
ua-usage --window <start>,<end> --window <start>,<end>   # several disjoint windows, summed
```

Pass the printed object as `task_update(..., run_usage=<it>)` when finishing,
or later as `task_claim(id, usage_only=True, expected_run_id=<run>, run_usage=<it>)`.
`--report --task <id> --run <run> --agent <label>` does the second itself,
through the hub at `UA_HTTP_HUB_URL` with the bearer in `UA_AGENT_BEARER_TOKEN`
(exit 3 if the hub refuses).

**Automatic in Claude Code.** `scripts/hook_usage_report.py`, registered as a
`PostToolUse` hook on `task_claim|task_update` and a `SessionEnd` hook,
reports without the agent doing anything. A claim or a move to `doing` that
names the caller's live run is remembered per session (a run opened by `task_update(status="doing")` has no `claimed_at`, so its window starts when the hook first sees it)
(`~/.cache/ua-usage-hook/<session>.json`). A run's window ends, and is
reported from its `claimed_at` to that moment, when `task_update` moves the
task out of `doing`, when a reply for the task names another run or none
(`release_claim`, a takeover), or at session end. No model call is counted
for two runs: the session keeps a cutoff that only moves forward, each report
covers `[max(claimed_at, cutoff), end)` and then advances the cutoff, and a
newer claim or a replacement run ends the older window at its own
`claimed_at`. Each run keeps the list of windows it was open, and every
report sums all of them, because the hub keeps the largest value it has
seen per field: a run renewed after being superseded reports its whole
total, and no call is counted twice. `ua-usage` refuses
overlapping, empty or reversed windows. The cutoff survives session end,
so a resumed session cannot re-report time. Only the session's own runs
are remembered: it learns its agent label from the `verified_caller` the
hub adds to authenticated `task_claim` replies and unkeyed `task_update`
replies (without it, as on an older hub or a keyed update, only from a
`task_claim` reply, since a claim is always the caller's own), remembers nothing until it knows it, and ignores replies
naming another agent's run. Once a day the hook deletes state older than 30 days whose session
transcript is gone from both the store recorded with the state and the
current one; it holds the session's lock while doing so and skips a busy
session. State changes take a per-session lock with a bounded
wait and are saved by atomic replace; a failed `--report` logs only the
exception type and HTTP status, never its text. Each report is a detached
`ua-usage --report`, so the hook returns at once. The hub URL and bearer come
from the session's own `~/.claude.json` registration and reach the child
through its environment, never argv; the child's output goes to `report.log`
beside the state. It runs only inside Claude Code (`CLAUDECODE=1`), and any
failure is silent: a run it misses stays unknown-cost, as before.

| Harness | Store | Counted |
|---|---|---|
| Claude Code | `<store>/<project>/<session>.jsonl` and `<session>/subagents/*.jsonl` (`UA_NODE_CLAUDE_PROJECTS`, else `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`) | each assistant message once per message id, at its final line's `usage`, if its first line is in the window (a call in flight at a claim or finish counts once, to the run it started in); cache reads and writes count as input |
| Codex | `<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<session>.jsonl` | the last cumulative `total_token_usage` in the window minus the last one before it; a total without valid input and output counts is skipped |

- **One session only.** The session id binds the measurement, so a concurrent
  session in the same project is never summed in. Inside Claude Code the id is
  `CLAUDE_CODE_SESSION_ID`; elsewhere pass `--harness` and `--session`.
- **One model.** `run_usage` names one model; a session's subagent models are
  summed in and the run is named for the model with the most output.
- **No invented price.** The stores carry no cost, so none is derived.
- **Billing is stated, never inferred.** Neither store records which login
  or provider paid for a call, and asking the CLI how it is logged in now says
  nothing about calls already made: a login can change mid-session, and a
  resumed Codex session keeps its original provider record. `--billing api`,
  `subscription` or `local` states it; without the flag the run has no
  `billing` and stays `unknown_cost`, keeping its reserve.

It reads local files only and makes no hub or provider call. A window with no
model calls exits 1 and prints nothing to stdout.

## Reading it

- `observability_manage("cost_rollup", {"group_by": "agent", "since": ..., "until": ..., "agent": ...})`:
  runs grouped by `agent`, `task`, `project`, `provider`, `model` or
  `billing`, over finishes in `[since, until)` (default: this UTC month), plus
  broker spend per agent under `broker`, never merged into run cost. Broker
  rows carry only a UTC day, so broker spend covers every whole UTC day the
  window touches (`broker_days`); fractional bounds round up to whole seconds.
  A run's project is the one recorded on its claim, so moving a task later
  does not move its history.
- `observability_manage("budget_status", {"agent": ...})`: each budget with every
  governed agent's `committed_usd`, `remaining_usd`, `state`
  (`ok` | `warning` | `exhausted`) and subscription quota.
- Control Center: the *Spend and budgets* section of Settings → Usage
  (`/ui/settings/usage`), from
  `GET /api/costs` (read-only).

Nothing is backfilled. Runs that finished before usage was reported, Codex
runs (tokens but no price), and hand-run CLI sessions that report nothing show
as unknown cost or subscription counts.

Failed executor usage reports are stored in the executor's local
`UA_HOME/executor-pending-usage/` directory and retried against their original
run ID before closeout and on later polls. A restarted daemon resumes these
retries. The usage-only accounting path accepts snapshots after expiry, cancellation,
or takeover, bound to the original authenticated actor and immutable run.
It never changes task ownership, lease, finish outcome, or successor. Cycle
reports use the bound task/run and a per-attempt idempotency key. A failure
to persist attempt totals terminates further retry scheduling, records an
accounting recovery failure, and still finalizes local sessions and attempts.

## Per-tool call allowance (task #2144)

`policy.yaml` ``rate_limit`` rules are enforced against an in-process,
per-caller sliding-window counter in `gateway.py` — one bucket per
`(rule, rule generation, tool, caller)` holding the monotonic stamps of
charged calls. The allowance view reads that same table
(`gateway.rate_limit_snapshot`); it is not a second counter. Buckets are
per-process and reset on hub restart, so the view reflects the serving
process only — a satellite stdio server keeps its own table.

- `observability_manage("tool_allowance", {"agent": ...})`: each row is one
  `(rule, tool)` allowance with `used` (charged calls live in the window),
  `limit`, `remaining`, `window_seconds`, `stopped`, and — while stopped —
  `resets_at` / `resets_in_seconds` (when the earliest charge leaves the
  window and the next call will be admitted). `used_today` counts allowed
  calls since 00:00 UTC from the decision ledger
  (`agent.gateway.decision` + `decision.rollup`, plus calls still pending in
  the read coalescer); the window itself is at most one hour. For a
  `session:` or `unverified:` subject it is `null` with `used_today_reason`:
  the ledger records those calls under the shared empty caller, so they
  cannot be attributed (enforcement counts are unaffected). A tool with
  calls today but no matching rule appears with `limit: null`.
- Buckets are per concrete tool, so the `pattern` row of a *glob* rule
  (`tool: fs_*`) always stays at full remaining — every matching tool has
  its own `max_calls`, and one tool's full bucket (`fs_read` stopped) says
  nothing about another's (`fs_write` still open). Only a literal-tool rule
  drops its pattern row, and only once that exact `(subject, rule, tool)`
  bucket is live.
- The view never runs `gateway.reconcile_rate_rules`: the generation sweep
  belongs to `evaluate_policy` alone, so a read cannot change what the gate
  will decide. A bucket from a superseded rule generation stays visible with
  `current: false` and never reports `stopped` (that includes
  `explicit:`-namespaced buckets charged under an explicit `policy_config`,
  which are never the current generation).
- For a single subject, `stopped` is also gated on the subject's *current*
  scope: the view re-runs the gate's own `binding_matches`/`_roles_apply`
  against the subject's live claims and profile, and a charged row whose
  rule no longer applies (a task/project bind the caller no longer holds, a
  role the profile no longer satisfies) reports `applies: false` with
  `applies_reason` instead of `stopped`. The check is exact only for the
  caller's own identity, whose live session profile the request context
  knows. For another identity (`agent=<other>`, or each bucket's owner in the
  fleet view), binds are re-checked against that identity's live claims, but
  a role scope can only be confirmed when the identity itself is listed in
  `roles` — otherwise the row reports the raw counter with `applies: null`
  and `applies_reason` "session profile not visible from this view", never
  a guessed false and never `stopped`.
- A glob pattern row's `used_today` counts only calls on matching tools that
  do not have their own bucket row — calls already shown on a `(caller,
  tool)` row are not re-counted.
- Tenant-fenced: a caller reads the identity its calls are charged under
  (`resolve_scope`: verified identity, else `session:`-namespaced, else the
  local process). `agent=<other>` or `agent="*"` requires the verified
  operator (`UA_OPERATOR_LABEL`); an operator omitting `agent` gets the
  whole fleet (`agent: null`).
- `GET /api/tool-allowance`: the same payload over HTTP, same fence (the
  operator override additionally requires a human auth channel — bearer,
  OAuth, cookie or WorkOS). Any authenticated identity reaches it; the fence
  is in the handler (`route_scopes.py` notes this).
- Control Center: the *Tool call allowance* section of Settings → Usage
  (`/ui/settings/usage`) renders each row
  as a used/limit progress bar with remaining and the reset time, plus
  unlimited tools' daily counts.

A malformed policy file is reported as `policy_error`, and a failed
decision-ledger read as `ledger_error` — never a silent zero. The top-level
`capacity` reports the buckets the next gate call would charge against
`MAX_BUCKETS` (2048): windows not fully expired (`admit` prunes the rest), of
a rule generation the next sweep keeps (the loaded file's rate rules, or an
`explicit:` generation). A bucket of an edited or disabled rule is dropped by
that sweep before `admit` runs, so it is not counted, though its row stays
visible with `current: false`. Even so,
a *new* `(rule, tool, caller)` bucket can be refused with
`rate_limit_capacity` while every visible row still shows remaining.
