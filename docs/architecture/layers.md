# Layer map

`src/unified_agent` is one Python package of about 280 modules. This page
splits it into five layers, says which way imports may point, and records how
far the code is from that today. The map is enforced:
`scripts/check_layers.py` checks every import against
[`layers.toml`](layers.toml) in CI (`gates` job), in
`scripts/local-gate.sh`, and in `tests/test_layer_check.py`.

The point is to make boundaries visible and keep them from eroding, as the
first step toward splitting the package into separately installable pieces.
For what runs where, see the [launch guide](../launch-guide.md).

## The rule

Layers are ordered. A module may import from its own layer or any layer below
it. It may never import from a layer above it.

| # | Layer | What lives here | Modules |
|---|---|---|---|
| 1 | **adapters** | How the outside world reaches the hub: the MCP server (`server.py`, toolsets, live state, elicitation), HTTP/JSON (`platform_http`), the web control center (`webui/`), the Slack ingress, OAuth and sign-in endpoints, CLIs, the TUI and process entry points. | 53 |
| 2 | **runtime** | Things that own a loop or a process: the executor daemon, approved dispatch, the bug worker, chat workers, schedulers and pollers, the reviewer, the LangGraph and ACP runners, the ua-node queue. | 58 |
| 3 | **services** | Domain operations every adapter and worker calls on demand: `tools/*` (tasks, memory, plans, delegation, vault, workflows, ...), goals, run records, skills, budgets, audit and evidence reports, delivery receipts, cycle eligibility, merge, the node registry, planning. | 107 |
| 4 | **integrations** | Clients for outside systems: LLM providers and the LLM broker, Gemini, embeddings, GitHub and PR models, Slack config and outbound, connectors and connector OAuth, PDF extraction, tailnet, stored device credentials, the hub HTTP client. | 17 |
| 5 | **core** | What everything else stands on: `config`, `storage` and its table schemas, the event log, the safety and policy chokepoints, the gateway, identity and profiles, the approval gate, the governed tool surface, the change feed, the agent registry, telemetry and projection contracts, read-only dispatch-run readers, evidence signing and anchoring. | 52 |

Two things follow from the order. Core never knows who calls it, so it can be
tested, and later packaged, on its own. And every interface (MCP, HTTP, web,
CLI) reaches the same services, which is what lets one policy apply
everywhere ([architecture](../architecture.md#system-at-a-glance)).

### Placement is by role, not directory

A module goes in the layer that matches what it does, even when its file is
somewhere else. Where the two disagree, `layers.toml` marks it **misplaced**.
That list is input for a later move:

- `tools/approvals.py` is the approval gate that `safety`, `policy` and
  `gateway` depend on, so it is placed in **core**.
- `tools/gemini.py` is the Google AI Studio client behind the explicit
  `gemini_ask` tool (`llm_client` keeps an unwired `ai_studio` helper over it;
  no chain selects it), so it is placed in **integrations**.
- `tools/doctor.py`, `tools/langgraph.py`, `tools/bug_worker.py`,
  `tools/dispatch_report.py`, `tools/evolve.py` and `tools/mcp_health.py` are
  MCP surfaces over a runtime subsystem, or over the whole hub. They are
  placed in **adapters**.
- `dispatch/child_env.py` is the spawn-environment allowlist, a safety
  primitive, so it is placed in **core**.

Modules that are mostly a library with a thin `main()` (`merge`, `wiki`,
`device_login`, `planning_loop`) are placed by the library, not the CLI. The
`mcp_progress` and `mcp_runtime_telemetry` recorders and the `mcp_matrix`
client data import no adapter code, so they sit in core and services.

## Where it stands (2026-10-15)

When the map was drawn, 54 of 1,339 internal imports pointed up. **None of
1,839 do now** (#1723 removed the last two).
[`layers-baseline.txt`](layers-baseline.txt) is empty and only shrinks, so
any upward import that is not a declared seam fails the check.

### What was fixed, and how

| Pattern | Was | Fix | Now |
|---|---|---|---|
| Domain code reached into `server.py` for the tool registry and policy guard | 5 | `tool_surface` (core) holds the registry and the guard; `server.py` fills it. The one lazy import of `server` is a declared seam (below). | 0 |
| Domain code pushed MCP live-state notifications | 4 | `change_feed` (core): domain code publishes, `mcp_live_state` subscribes. | 0 |
| `storage` imported domain modules to create their tables | 12 | `table_schemas` (core) holds the DDL for routines, goals, task comments, project bindings and task-space cursors. A fresh database's schema is byte-identical. Then (#1720) the autonomy, task-search, skills-registry and run-heartbeat DDL moved there too, row migrations and backfills became post-init hooks (below), the similarity math moved to `vector_math` (core) and the memory embedding backfill to `memory_semantic`. | 0 |
| Services read runtime state | 11 | `node_registry` (node reads), `dispatch_states` (run-state vocabulary), workflows name the reviewer through `tool_surface`, `planning_loop` placed as a service. Then (#1724) the reviewer's cycle-eligibility checks moved to `cycle_eligibility` and the evidence report reads receipts from `delivery_receipts`. Then (#1723) `agent_lifecycle` stopped importing `dispatch.coordinator` and `node_queue`: they register its `dispatch` and `node` stop handlers when imported, and a pause or terminate in a process missing either is refused (fail closed) rather than skipping the cancel. | 0 |
| `tools.doctor` used as a helper library | 4 | Workflows name `system_doctor` through `tool_surface`. Then (#1722) the systemd and LaunchAgent probes moved to `service_probes` (services); the executor, delivery receipts and Horizon 2 tools import them from there, and the executor's in-process fallback names `system_doctor` through `tool_surface`. | 0 |
| Auth reached up | 4 | (#1721) The WorkOS hub leg (`WorkOSHubAuth`, `enabled`, `build_hub_auth`, `hub_auth`, its cache and the identity map) moved to `identity_core` (core), and `pilot_auth`, the WorkOS session verification it builds on, which imports nothing from the package, is placed in core. `config`'s start-up guard and `audit.producer_sign` read it there. `connector_oauth` and `tools.oauth_devices` ask `identity_core.oauth_provider()`, which `oauth` fills with `get_provider` when it is imported, as the hub server does at start-up. | 0 |
| One-off imports | 14 | Shared pieces moved down: vault-sync defaults and `task_list_project_key` into `config`, the journal parser into `tools.vault`, the audit text formatter into `audit.engine`, Slack config into `slack_config`, PR types into `pr_models`, budget policy parsing into `budget_policy`. `zulip_automation`, `tools.gemini` and the MCP telemetry recorders placed by role. Then (#1724) `horizon2` reads runs through `dispatch_reads` (core) and `planning_loop` posts through `hub_client` (integrations), which the executor's `HubClient` now subclasses. | 0 |

Old names are re-exported from their previous modules, so callers and tests
that use them keep working. `tools.doctor` also forwards a patch of a moved
probe name to `service_probes`, so `monkeypatch.setattr(doctor, "_service_env", ...)`
still reaches the moved code (#1722). `workos_hub_auth` keeps one copy of each
moved name, in `identity_core`: a read or a patch through the old module
resolves there, and `identity_core.oauth_provider()` calls `oauth.get_provider`
at call time, so a patch of either old name reaches every caller (#1721).

### Stop handlers (#1723)

`agent_lifecycle` (services) must cancel a stopped agent's dispatch runs and
node items, which live in the runtime layer. It names the handlers it needs in
`REQUIRED_STOP_HANDLERS` (`dispatch`, `node`); `dispatch.coordinator` and
`node_queue` call `register_stop_handler` at import, and `webui.routes`, which
`server.py` imports, imports both. Before writing anything, a pause or
terminate checks `missing_stop_handlers()` and raises if any is absent, so a
process that never loaded the dispatcher refuses the change instead of
recording a stop it never requested.

### Post-init hooks (#1720)

`storage.init_db()` is schema only: every table, column, index and trigger,
so a process that imports nothing but storage still gets the hub's whole
schema. Work that reads or rewrites rows, or warms a cache, belongs to a
module above core. `post_init.register_default_hooks()` registers it with
`storage.register_post_init_hook`, and every later `init_db()` in that process
runs it once the schema is committed: foreground hooks in order on one
connection (a failure rolls back and propagates, as a failed migration always
has), then background hooks on tracked daemon threads.

| Order | Hook | Kind |
|---|---|---|
| 10 | `autonomy_modes.legacy_grants`: JSON standing grants into their table | foreground |
| 20 | `task_search.task_fts`: index tasks that predate `task_fts` | foreground |
| 30 | `run_records.open_run_cache`: fill `tasks.current_run_*` | foreground |
| 110 | `memory_semantic.embeddings`: embed notes, mirror `memory_vec` | background |
| 120 | `task_search.embeddings`: embed tasks in batches | background |
| 130 | `tools.indexer.symbol_embeddings`: embed workspace symbols | background |
| 140 | `sync.startup_snapshot`: vault state snapshot | background |

The hub server, the CLIs and the workers register the hooks before they call
`init_db()`, and `tests/conftest.py` does too, so their start-up is unchanged.
Core modules that call `init_db()` themselves (the event bus `main`, the audit
anchor and reanchor) get whatever their process registered.

### Seams

A seam is a deliberate upward import with a stated reason. It is declared in
`layers.toml` with `[[seam]]` and kept apart from the baseline, because it is
design, not debt.

- **`tool_surface -> server`.** `server.py` is the composition root that
  registers every governed tool. `tool_surface` imports it lazily on first use,
  so the workflow engine, the LangGraph dispatcher, the chat worker and the
  skill catalog never name the MCP adapter, and a standalone process still
  gets the full, policy-wrapped surface.
- **`events -> run_records`.** `prune_events` deletes old `event_log` rows and,
  in the same transaction, drops the heartbeat-usage rows whose self-pickup
  claim it just pruned (#940). The import is lazy and runs only inside a prune
  that removed rows. Added when `main` was merged into this branch.
- **`team_spec -> node_queue`.** The default seat launcher enqueues a governed
  ACP item on the ua-node queue (the #1329 launch path). The import is lazy,
  inside `NodeSeatLauncher`, and tests inject a launcher instead.

### Can the package be split into separate installs yet?

Not yet. Everything that reaches the core layer is still in one import cycle
of 119 modules (215 before #1720), because core still imports upward.
Measured on the current graph (2026-10-02):

- Without the `tool_surface` seam and core's 2 upward imports, the largest
  cycle drops to 44.
- Without that seam and all 9, it drops to 33 (same-layer cycles, which are
  allowed).

So the order is: clear core's 2 (the auth items above), replace
the `tool_surface` seam with an installed entry point, then turn the layers
into a uv workspace with one distribution each. Systemd units and launchd
jobs run `python -m unified_agent.<module>` for several modules, so a split
must keep those module paths.

## When the check fails

Run `uv run python scripts/check_layers.py` (add `--report` for the tables
above). The check fails for one of these reasons:

- **`UNMAPPED`**: a new module is in no layer. Add it to `layers.toml`.
  Choose the layer by what the module does, not where the file is.
- **`NEW`**: an import points up. Do not add it to the baseline. Either
  import something from a lower layer, move the shared piece down, or invert
  the call: publish an event, or accept a callback.
- **`FIXED`**: an inversion is gone. Delete its line from
  `layers-baseline.txt`. The baseline only shrinks, so a fixed inversion
  cannot quietly come back.
- **`AMBIGUOUS`**: two globs in different layers claim a module. List it by
  exact name.
- **`UNUSED`**: a pattern in `layers.toml` matches no module, usually because
  the module was deleted, or a seam is no longer an upward import. Remove it.

`--write-baseline` regenerates the baseline from the current code. Use it only
when you reclassify modules in `layers.toml`, and review the diff. Never use
it to make a new inversion pass.

## What the check sees, and what it doesn't

- It sees every `import` and `from ... import` statement, including imports
  inside functions and under `if TYPE_CHECKING:`. A lazy import is still a
  dependency; it only hides the cycle from the interpreter.
- It checks direct imports only. That is enough because every module is
  placed in a layer: any upward chain of imports contains at least one upward
  edge.
- It does not see `importlib.import_module(...)` with a string argument
  (`object_model`, `connectors`, `tools.doctor`) or console-script entry
  points.
- It works at module level. A module whose functions belong in two layers is
  placed by its main role. Splitting such a module is part of fixing it, not
  part of drawing the map.
