# Operating profiles

Tasks #1732 (operating profiles), #1735 (per-run comms policy) and #1756
(wiring the other fields to their consumers).

An operating profile says how work runs: who may talk to the operator, how
work is delivered, how much autonomy agents get, how much planning a piece of
work needs, which model runs it, the run's budget, and standing instructions.
The operator names profiles once, binds them to offices, and overrides them
for a sprint or for a single run.

Code: `src/unified_agent/operating_policy.py` (core layer: parsing,
inheritance, tables) and `src/unified_agent/operating_profiles.py` (services
layer: resolution, run snapshots, comms enforcement, operator writes). Tests:
`tests/test_operating_profiles.py`.

## Layers

Lowest to highest precedence:

| Layer | Set where | Set by |
|---|---|---|
| Org default | `operating_profiles.default` in `policy.yaml` | Policy revision (`config_revision_manage`) |
| Office (unit) | `operating_profiles.units.<office-id>` in `policy.yaml` | Policy revision |
| Sprint | `goal_manage("operating_sprint_add" / "operating_sprint_update")` | Operator; an agent's write waits for approval |
| Run | `goal_manage("operating_run_set")` on a task | Operator; an agent's write waits for approval |

A layer applies a profile, overrides, or both. Mapping fields (`comms`,
`delivery`, `budget`, `relay`) merge key by key; every other field is replaced. Lists
(`comms.allow`, `relay.autosend`) are replaced, never appended.

The unit is the sprint's `org_unit` when it has one; otherwise the first
office in `org.yaml` order that contains the run's agent and has a binding.

`goal_manage("operating_resolve", {"task_id": N})` returns the effective
settings, the layer that set each value (`sources`), each layer's
contribution (`layers`), a `fingerprint`, and per-field `enforcement`.

## Frozen per run

When a task is claimed, the resolved settings are written into the run's
claim event (`selfpickup.claimed` payload key `operating_profile`), next to
the trust-preset snapshot. The claim transaction also writes the same snapshot
into `run_operating_snapshots`, keyed by run id (an empty record for an
unprofiled claim supersedes older snapshots and stays frozen if profiles are
enabled later). Startup backfills pre-upgrade
claim snapshots once; comms checks use `tasks.current_run_id` and keyed
lookups instead of scanning the event log. A run keeps the posture it started with; a later
policy or sprint edit applies to the next run. `operating_run_set` therefore
sets how the **next** run of a task operates.

## Profile fields

```yaml
operating_profiles:
  default: standard
  profiles:
    standard:
      description: Anyone may reach the operator.
      comms: {mode: direct}
      delivery: {mode: no-mistakes, yolo: false}
      autonomy: action-review          # observe | exception | checkpoint | action-review
      planning: P2                     # P0 (none) .. P4 (full design review)
      model: claude-opus-5-5
      budget: {max_cost_usd: 5, max_time_s: 3600, max_tokens: 2000000}
      instructions: Text appended to every brief for runs in this profile.
      relay: {autosend: [slack]}       # channels a public relay may send to unprompted
    crew:
      extends: standard
      comms: {mode: funnel, liaison: "office:chief-of-staff"}
  units:
    platform-engineering: crew
```

Unknown keys, unknown profile names, unknown offices, `extends` loops and
incomplete comms policies fail closed. A policy revision that would introduce
one is refused before it is written.

| Field | Status | Consumer |
|---|---|---|
| `comms` | Enforced | Comms policy (below) |
| `delivery` | Enforced | `delivery.resolve_delivery` |
| `autonomy` | Enforced | `autonomy_modes`: the mission default |
| `model` | Enforced | Dispatch launch and the executor daemon |
| `budget` | Enforced | `autonomous_cycle_start` |
| `planning`, `instructions` | Enforced | Dispatch brief and session packet |
| `relay` | Recorded | None yet: `tools/public_relay.resolve_relay_autosend` exists (#1734) but always returns False, so nothing reads the field |

`operating_resolve` reports each field as `enforced` or `recorded`, so a
setting never claims to do more than it does.

## How consumers read the run

`operating_profiles.run_snapshot(conn, task_id)` returns the open run's
frozen snapshot, or None. Pass `finished=True` for the newest run whether or
not it finished: review and merge act on work a run already handed off. A
policy, sprint or run-settings edit after the claim never changes the
running run; it applies to the next one. No snapshot means each consumer
behaves exactly as it did before profiles existed.

### Delivery

`resolve_delivery(project, notes=..., snapshot=...)`, highest first:

1. A sprint- or run-layer `delivery` value
2. The task-note marker `[delivery: mode=... yolo=...]`
3. The project's entry in `project_delivery.yaml`
4. An org- or unit-layer `delivery` value
5. The file's `default` block, then the built-in `no-mistakes`, yolo off

`mode` and `yolo` are layered separately, using `sources` to tell the layers
apart. A value from a profile reports `source: profile` and the `layer` that
set it. The executor finish stamps a generated `[delivery-run: mode=... yolo=...]`
record from the open run's snapshot. It is a record only: resolution never reads it
back, so a finished run's policy does not govern the next run, and only an
operator's `[delivery: ...]` note counts. A note that gives only `mode` takes
`yolo` from the project's entry, then the org or unit layer, else off. The
reviewer reads the newest run's snapshot.

### Autonomy

A mission with no explicit mode takes the run's `autonomy` as its default
instead of `action-review`: the run of the task being merged when the action
names one, else the actor's live runs on the mission's tasks, strictest
first. The org ceiling still tightens it. An explicit mission mode is never
replaced. See `docs/autonomy-modes.md`.

### Model

Dispatch launch and the executor daemon pass the profile `model` to
`config.resolve_run_model` as the override when the task spec has no
`[model: ...]` marker. The profile may change the model, never the provider:
the model must belong to the same provider as the model the run would use
without it (or, when that is unknown, the backend's own provider: `claude`
Anthropic, `codex` OpenAI, `agy` Google). A model from another provider, or
one whose provider cannot be told, is refused with a warning and the run
keeps its normal model. A dispatch launch usually precedes the agent's
claim, so with no open run the launch resolves the settings the claim is
about to freeze.

### Budget

`autonomous_cycle_start(task_id=N)` fills caps the caller did not pass from
the run's `budget`: `max_time_s` to `time_s`, `max_tokens` to `tokens`,
`max_cost_usd` to `cost_cents`. Explicit caps win. The result lists the
filled caps under `operating_budget`.

### Planning and instructions

Dispatch prompts (CLI spawn, node launch, executor daemon) end with an
"Operating profile" block: the planning level with its meaning and the
instructions text with the layer that set it. `session_packet` carries the
same as `operating_profile`.

| Level | Meaning |
|---|---|
| `P0` | No planning step: act directly on the task. |
| `P1` | Light: state the approach in a sentence or two before acting. |
| `P2` | Standard: write a short plan (steps, files, tests) before changing code. |
| `P3` | Thorough: plan with risks and alternatives, checked with the requester before large changes. |
| `P4` | Full design review: write a design and get it reviewed before implementing. |

### Relay autosend

`relay: {autosend: [channel, ...]}` names the channels a public relay may
send to without asking. `operating_profiles.relay_autosend(snapshot,
channel)` answers it, False by default. Nothing reads it yet: the public
relay (`tools/public_relay.py`, #1734) is on this branch, but its
`resolve_relay_autosend` hook still returns False unconditionally and is not
wired to the run's snapshot, so every relay send waits for human approval and
the field is reported as `recorded`.

## Comms policy

| Mode | Rule |
|---|---|
| `direct` | Anyone may message anyone, including the operator. |
| `funnel` | Only the liaison may address the operator. Agents still message each other and the liaison. |
| `custom` | Only messages matching an `allow` rule pass. |

`liaison` is an identity label or `office:<id>` (that office's lead).
`allow` rules take `from` and `to` parties: a label, `*`, `operator`,
`liaison`, or `office:<id>` (any member).

The operator is never restricted. Enforcement covers every agent-initiated
route: `message_send` (and the crew intervene path that calls it), `notify`,
`batch_notify`, and a `delegate` completion callback of `notify`. The
callback is checked when the delegation is made and again when it fires,
against the delegator's policy at that moment.

Which policy judges a message:

- Every message must pass every live run the sender holds. With no live
  run, the sender's current org and office profiles decide.
- A `task_id` of a task the sender ran adds the sender's own newest run of
  that task as another restriction, even after the run finished and even
  if another agent has claimed the task since. It can only narrow
  permission: an old direct run never bypasses a current funnel.
- A `task_id` the sender never ran grants nothing; the message is judged as
  if it had no task.
- Frozen run snapshots keep applying even if the `operating_profiles` block
  is later removed. With no block and no frozen run, nothing is refused.
- A caller with no verified identity (a stdio session without an agent
  token) cannot notify the operator while operating profiles are
  configured. Only named hub-internal deliveries pass without a sender:
  the digest flush, anomaly alerts, the review pipeline and queue hygiene
  (`operating_profiles.INTERNAL_ROUTES`). Digest entries are gated when
  queued; a flush sends them under system authority, so a crew caller may
  flush an already-approved liaison queue.

A refusal names the layer that set the policy and who to send to instead,
and is recorded on the evidence channel `agent.comms.denied`.

`delegate` defaults to `callback="notify"`. A crew agent under a funnel
must choose `callback="none"` when delegating without operator contact;
the default notify callback is refused. Check existing callers before rollout.

## Degraded stored settings

Policy revisions refuse removal of profiles still referenced by an open
sprint, a closed sprint with linked tasks, or stored run settings. Clear or repoint those settings first. If a
policy file is changed outside that path, live resolution skips removed
profile references and invalid stored overrides, keeps the remaining layers,
and returns `warnings`. Removed office bindings are ignored with a warning. If the registry is unreadable,
all office bindings are skipped for that load. Office references inside default
or frozen communications policies resolve to no liaison or members when the
registry is unavailable, and emit a degraded warning. Funnel messages between
workers continue; operator contact and custom office rules fail closed.
An incomplete resolved comms rule remains active and fails closed. Claims
still record their resolved snapshot; communications emit a daily-deduplicated
`agent.operating.degraded` event per task and warning identity. Policy replacement
and sprint/run writes revalidate under the same database writer lock, preventing
concurrent writes from introducing orphaned references. Operator writes remain
strict and refuse settings that cannot resolve.

## Sprints

A sprint groups tasks under a name, an optional goal and office, a profile
and overrides, with a status (`planned`, `active`, `closed`) and optional
`starts_at` / `ends_at`. Dates are ISO-8601 dates or datetimes (a value
without a timezone is UTC), and the start may not be after the end. A task
belongs to at most one sprint; deleting a task removes its sprint link and
its run settings.

Every sprint, link and run write is resolved before it is stored, together
with the layers below it. A write that would not resolve (for example a
`funnel` override when no layer names a liaison) is refused and nothing is
written, so a stored setting cannot block a later claim.

## Who can change what

Profiles and office bindings are policy: they change through the reviewed
policy revision path. Sprint and run settings are operational: a verified
human writes them directly; an agent's write is queued as an
`operating_settings` approval and applies when a human approves and the
agent retries the identical call. A verified agent cannot name another actor.

## MCP actions

All on `goal_manage`:

| Action | Params |
|---|---|
| `operating_profiles` | — |
| `operating_resolve` | `task_id?`, `sprint_id?`, `agent?` |
| `operating_sprint_add` | `name`, `goal_id?`, `org_unit?`, `profile?`, `overrides?`, `starts_at?`, `ends_at?` |
| `operating_sprint_list` | `status?` |
| `operating_sprint_get` | `sprint_id` |
| `operating_sprint_update` | `sprint_id`, any of `name`, `status`, `profile`, `overrides`, `org_unit`, `starts_at`, `ends_at` |
| `operating_sprint_link` / `operating_sprint_unlink` | `sprint_id`, `task_id` |
| `operating_run_set` | `task_id`, `profile?`, `overrides?` |
| `operating_run_clear` | `task_id` |

The four read actions (`operating_profiles`, `operating_resolve`,
`operating_sprint_list`, `operating_sprint_get`) are admitted under the
`inspection` trust preset and do not enter the mixed-action write approval
gate when `goal_manage` is in `UA_APPROVAL_TOOLS`. Sprint and run mutations
still require write authority.

## Control Center

The **Operating** tab of Agents → Teams (`/ui/agents/teams?tab=operating`, task
#1757) is the operator's view
of all four layers.

| Section | Shows | Changes |
|---|---|---|
| Profiles | Each profile's effective settings (`extends` applied), with an `enforced` or `recorded` badge per field | Read-only |
| Departments | Each office, its bound profile (or the org default) and its comms mode | Read-only |
| Change profiles or department bindings | A JSON draft of the live `operating_profiles` block | Commit through `config_revision_manage(action="commit", kind="policy")`; history and rollback are in the policy revisions on Agents → Organization (`/ui/agents/org`) |
| Sprints | Sprints with status, dates, profile, office, overrides and linked tasks | Create (with optional start and end dates), set status, profile and overrides, link and unlink tasks |
| Run settings | The resolved settings for a task, each value with a "set by `<layer>`" badge | Set a profile or overrides for the next run, or clear them |

A write the hub refuses (a date that is not ISO-8601, a start after the
end, or settings that would not resolve, such as a funnel with no liaison)
answers 400 and the tab shows the hub's reason next to the form.

Profiles and office bindings are policy, so the tab does not write them:
there is no HTTP commit route, and the draft goes through the reviewed
policy revision path. Sprint and run writes are operational.

HTTP routes:

| Route | Does |
|---|---|
| `GET /api/operating` | `operating_profiles` view plus `offices` (each with its bound `profile`) and `policy_edit` (the commit path) |
| `GET /api/operating/resolve?task_id=&sprint_id=` | `operating_resolve`, plus `run_settings` (the task's stored run layer, or null) so an edit starts from what is stored; an unknown task or sprint is 404 |
| `GET /api/operating/sprints?status=` | Sprints with their `task_ids` |
| `POST /api/action/operating-sprint` | `{op: add\|update\|link\|unlink, ...}`, the matching `operating_sprint_*` params |
| `POST /api/action/operating-run` | `{op: set\|clear, task_id, profile?, overrides?}` |

The writes take the actor from the authenticated identity (an `actor` or
`author` field is refused), need the `x-ua-csrf` header with a session
cookie, and run the same gates as the MCP `goal_manage`
action: `agent_access`, trust presets, policy rules and rate limits, and the
`UA_APPROVAL_TOOLS` gate. A verified human skips only the profile check
(the operator's web profile has no `goal_manage`) and writes directly.
An approval pass consumed by a write that is then refused or parked is
restored, so a retry does not need it granted again. A non-human caller
needs `goal_manage` both in its request profile and in its identity's
default (which carries any committed downgrade). Its write answers
`202 {pending, approval_id}` and applies when a human approves and the
agent sends the identical request again.
