# Component map

The [layer map](layers.md) says which way imports may point. This page adds
the vertical axis: every module in `src/unified_agent` belongs to one
**component**, a feature family, and a component may import only the
components it declares. The map is [`components.toml`](components.toml); it
is checked by `scripts/check_components.py` in `scripts/local-gate.sh` and in
`tests/test_component_check.py` (the CI `gates` step lands with #2440).

It is phase K1 of the kernel split (epic #2437, spec
`docs/architecture/kernel-split-spec.md`, #2438). Later phases read this map:
test ownership (K2), component-scoped batch selection (K3) and manifests (K4).
K4a (#2449) and K4b (#2603) have shipped: each component has a manifest, and
`UA_COMPONENTS` selects which manifests a process loads. No module moves; the
map is data.

## The rule

1. Every module is in exactly one component. Exact names beat globs.
2. A module may import its own component and any component in the transitive
   `depends_on` of its own.
3. `kernel` depends on nothing. `mcp-adapter` is the composition root: it
   depends on every component and nothing depends on it.
4. The `depends_on` graph has no cycle. Where two components import each
   other, the map declares the direction with more edges and baselines the
   rest.
5. Every other import is a violation. The ones that existed when the map was
   drawn are in [`components-baseline.txt`](components-baseline.txt), which
   only shrinks.

The **kernel** is what the hub needs to serve tasks, messages, lexical
memory, the vault, approvals, the evidence ledger, sessions, identity and
policy. When in doubt a module is not kernel. Every component is
`certifiable = false` in K1.

## Loading (`UA_COMPONENTS`, task #2603)

`UA_COMPONENTS` is the load selection, not a tool filter (that is
`UA_TOOLSETS`). Unset or blank loads every component in the order above.
`kernel` expands only to kernel plus `mcp-adapter`. The adapter is always
added, because a process with no adapter registers no MCP tools. Named
components are never expanded by `depends_on`: that field says which
components a module may import, not which must be loaded. `mcp-adapter`
declares all fifteen others; expanding that would load everything and make
the variable do nothing.

An unknown name raises `ValueError` and stops start-up rather than serving a
silently smaller surface. A kernel-only process registers exactly the
kernel's and the adapter's tools, imports only those two manifests (plus
`components.base`), and still answers `/healthz` and `/readyz` from the
adapter so deploy probes do not 404. Toolset assignment still requires every
*registered* tool to be in the table; tools of components that were not
loaded are expected to be missing (`complete=False`). A component map that
cannot be read fails closed: the test selector widens to every module
instead of shrinking to none.

Operator contract: [configuration.md](../configuration.md). Pitfall:
[troubleshooting.md](../troubleshooting.md#a-tool-is-missing-from-toolslist).

## Where it stands

Measured on 2026-11-10, when the map first landed (K1, #2439). The numbers move
with every merge; `uv run python scripts/check_components.py --report` prints
them for the tree you are on.

| Component | Modules | Lines | Undeclared imports out | Undeclared importers in |
|---|---|---|---|---|
| kernel | 121 | 85,415 | 33 | 0 |
| auth | 9 | 5,215 | 1 | 0 |
| telemetry | 17 | 6,992 | 3 | 5 |
| retrieval | 9 | 3,804 | 0 | 6 |
| llm | 17 | 9,333 | 1 | 2 |
| node | 19 | 15,328 | 5 | 1 |
| dispatch | 32 | 23,080 | 8 | 8 |
| audit | 21 | 13,712 | 2 | 2 |
| cloud | 14 | 7,716 | 2 | 0 |
| github | 21 | 19,409 | 7 | 6 |
| chat-bridges | 14 | 8,568 | 3 | 6 |
| dev-tools | 19 | 6,690 | 1 | 0 |
| skills | 9 | 5,676 | 0 | 9 |
| knowledge-jobs | 12 | 10,971 | 2 | 8 |
| control-center | 29 | 21,669 | 7 | 1 |
| mcp-adapter | 43 | 23,059 | 0 | 21 |
| **total** | 406 | 266,637 | 75 | 75 |

75 of 2,178 internal imports are undeclared. The kernel's 33 outbound edges
go to chat-bridges 6, retrieval 6, telemetry 5, dispatch 4, knowledge-jobs 4,
audit 2, github 2, skills 2, mcp-adapter 1 and node 1; phase K6 burns them to
zero. 21 edges point into `mcp-adapter`: 13 are workers and entry points that call
`post_init.register_default_hooks()`, and 8 reach `server`, `toolsets`,
`review_cli` or `tools.doctor` from the web UI, TUI, Slack gateway, ua-node
and `tool_surface`. `auth`,
`cloud` and `dev-tools` already have no undeclared importer.

The declared order, base first: kernel, auth, telemetry, retrieval, llm,
node, dispatch, audit, cloud, github, chat-bridges, dev-tools, skills,
knowledge-jobs, control-center, mcp-adapter. It is the order with the fewest
violations among the feature components (21), and each `depends_on` lists
only the components it imports. Two pairs that import each other:
`dispatch` depends on `node` (10 edges against 4), and `skills` depends on
`dispatch` (5 against 3).

## Placement is by role, not name

A module goes where its role is, even when its name says otherwise. Each of
these carries the same note in `components.toml`.

**Kernel primitives in a feature folder.** Many kernel modules import them and
their own imports stay inside the kernel:

- `telemetry.redaction` and the stdlib-only detectors it imports
  (`telemetry.jwk`, `telemetry.oct_blocks`, `telemetry.yaml_jwk`): secret
  redaction, imported by 8 kernel modules (34 in all). Its imports are all
  kernel once the three detectors are, so nothing blocks it.
- `node_registry`: node and agent liveness reads; 6 kernel modules (messaging
  wake and availability, owner and task liveness, session credentials,
  `tools.registry`) import it.
- `audit.producer_sign`: evidence-ledger producer signatures that tasks,
  approvals, delegation and work products sign with; with it
  `dispatch.child_env`, the agent bearer-token resolver it imports (a core
  primitive in the layer map).
- `oauth_store` (storage creates its tables), `pilot_auth` (identity_core
  builds on it), `vector_math` (storage imports it), `dispatch_states` (the
  run-state vocabulary `run_records` reads), `budget_policy` (authority
  config that `config_revisions` versions), `review_reads` and `usage_ledger`
  (storage imports their tables).
- `run_records` and `session_credentials`: task run state and session
  identity; events, tasks, approvals and `agent_lifecycle` import them.
- `untrusted`: the provenance wrapper on the memory, message and vault read
  paths. Its import of `audit.rules` (the injection scanner) is baselined.
- `lifecycle.*`, `tools.lifecycle`, `tools.learning`: session start and end
  packets, capture and learnings.
- `tools.git`, `tools.worktree_policy`: task claims create and remove
  worktrees through them. `tools.git -> merge` is baselined.
- `tool_surface` stays kernel; its lazy import of `server` is one baselined
  edge, not a reason to move it.

**Out of the kernel.**

- `memory_knn` and `memory_graph` are retrieval legs (sqlite-vec KNN, the
  entity graph), so they are `retrieval`, beside embeddings.
- `tools.graph` (execution-graph handoffs over teams, holds and node
  acceptance) is `dispatch`; `tools.cycles` and `tools.policy_authorization`
  are `github`; `tools.mcp_gateway` (downstream MCP servers that Horizon 2
  registers) and `hosted_logging` are `cloud`; `tools.public_relay` and the
  A2A and gbrain bridges (`messaging.bridge_*`, `messaging.brain_format`) are
  `chat-bridges`; `tools.synthetic_workflow` and `mcp_matrix` are `skills`;
  `fleet_actions` is `node`; `delegation_ledger_log` is `github`.
- `platform_readers`, `work_graph`, `runs_projection`, `needs_operator`,
  `capability_verification`, `tools.graph_canvas`, `tools.projection` and
  `zulip_automation` are read models or views of the control center.
- `completion_return` (reply to the chat thread that asked) is
  `chat-bridges`; the four kernel imports of it are baselined.

**Elsewhere.**

- `acp_*` and `session_settings` are the ACP protocol ua-node drives, so
  `node`, not `dispatch`.
- `hub_client`, `device_login` and `device_key` are how a client signs in to
  and calls the hub: `auth`.
- `service_probes` (systemd and LaunchAgent health probes) is `telemetry`.
  `tools.dash` is Dash/Zeal offline docs, so `dev-tools`.
- `check_receipts`, `pytest_attestation` and `affected_tests` are shipping
  checks the review pipeline runs: `github`.
- `mcp-adapter` holds the composition: `server`, `health` (the always-on
  `/healthz` and `/readyz` routes, registered even without `control-center`),
  `post_init`, `toolsets`,
  MCP wiring, `tools.doctor` (imports 58 modules) and `tools.mcp_health`,
  every console-script CLI (`cli.*`, `*_cli`, `audit.cli`, `dispatch.cli`,
  `harness_usage`, `session_end`, ...) and the `bootstrap`, `demo` and
  `backup_runner` entry points. A component's own daemon or worker
  (`node_agent`, `executor.daemon`, `github_app_worker`,
  `dispatch.supervisor_spawn`) stays with it. `shipping_preflight_cli` stays
  in `github` because reviewer code imports it as a library.

## Manifests

Each component declares the MCP tools it registers in a manifest,
`src/unified_agent/components/<component>.py` (`-` becomes `_`), which exports
`COMPONENT = Component(name, tools)` (K4, #2449). `server.py` names no tool
function: it registers `components.tools()`, the concatenation of
`components.load()` in the map order above (`kernel` first, `mcp-adapter`
last), then each manifest's own order. That is also the order of `tools/list`.
The fingerprints, `docs/tool-surface-baseline.json` and the toolset table
sort by name, so they do not depend on it.

To add a tool:

1. Write the function in a module the map places in the component it serves.
2. Add one line to that component's manifest.
3. Do what any new tool needs: a toolset in `toolsets.py`, a classification in
   `tool_classification.py`, the baseline (`scripts/tool_surface_baseline.py
   --write`), and the docs counts and reference entries.

A manifest lists only functions defined in its own component's modules, and a
tool is in exactly one manifest; `tests/test_component_manifests.py` checks
both, plus that the registry is exactly the manifests. A tool whose module is
placed by role in another component (the `git_*` tools are in `tools.git`,
which is `kernel` because task claims create worktrees through it) is listed
by that component, not by the family it looks like. `node` registers no tools
yet; its manifest is empty.

| Component | Tools | Names |
|---|---:|---|
| `kernel` | 86 | `git_diff_staged`, `git_commit_staged`, `git_worktree_list`, `git_worktree_add`, `git_worktree_remove`, `git_merge_task`, `memory_save`, `memory_search`, `memory_list`, `memory_get`, `memory_restore`, `memory_feedback`, `memory_delete`, `memory_prune_candidates`, `vault_read`, `vault_write`, `vault_move`, `vault_archive_plan`, `vault_list`, `vault_search`, `vault_links`, `vault_journal`, `vault_briefing_digest`, `vault_ingest_status`, `vault_wiki_custodian`, `vault_query`, `task_add`, `task_update`, `task_list`, `task_delete`, `task_claim`, `task_comment`, `org_proposal`, `project_build`, `plan_create`, `plan_manage`, `plan_update`, `plan_get`, `plan_list`, `plan_checkpoint`, `plan_resume`, `plan_step_retry`, `plan_step_claim`, `plan_handoff`, `plan_delete`, `delegate`, `delegation_manage`, `delegate_list`, `delegate_claim`, `delegate_release`, `delegation_ledger`, `delegation_spec_check`, `delegation_spec_template`, `delegation_spec_build`, `approval_pending`, `approval_resolve`, `notify`, `notify_delivery_status`, `batch_notify`, `flush_digest`, `event_log_query`, `event_log_verify`, `agent_heartbeat`, `agent_list`, `agent_context`, `session_list`, `session_start`, `session_end`, `propose_session_learnings`, `lifecycle_record`, `session_packet`, `message_send`, `message_list`, `state_sync_verify`, `cleanup_stale_worktrees`, `vault_to_memory_ingest`, `profile_activate`, `profile_reset`, `profile_status`, `config_revision_manage`, `managed_secret_set`, `agent_token_issue`, `agent_token_rotate`, `agent_token_list`, `deployment_claim`, `work_product_submit` |
| `auth` | 1 | `oauth_device_manage` |
| `telemetry` | 6 | `agent_activity`, `observability_manage`, `metrics_snapshot`, `check_anomalies`, `perf_summary`, `check_anomalies_and_notify` |
| `retrieval` | 5 | `fs_index_symbols`, `fs_list_symbols`, `fs_search_symbols`, `memory_promote`, `recall_maintenance` |
| `llm` | 10 | `gemini_ask`, `credential_pool_register`, `credential_pool_list`, `credential_pool_revoke`, `model_quota_status`, `pi_run_task`, `langgraph_run`, `langgraph_progress_note`, `langgraph_workflow_run`, `langgraph_checkpoints` |
| `node` | 0 | none |
| `dispatch` | 19 | `routine_add`, `routine_list`, `routine_pause`, `routine_fire`, `dependency_list`, `coordination_manage`, `dependency_add`, `dependency_remove`, `handoff`, `handoff_list`, `dispatch_report_self`, `dispatch_cancel`, `dispatch_resume`, `dispatch_retry_cancellation`, `dispatch_confirm_cancellation`, `dispatch_delivery_receipt`, `dispatch_delivery_receipts_list`, `crew_view`, `crew_intervene` |
| `audit` | 4 | `security_audit`, `injection_triage`, `audit_triage_tasks`, `evidence_report` |
| `cloud` | 10 | `horizon2_server_list`, `horizon2_server_register`, `horizon2_server_revoke`, `horizon2_connector_authorize`, `horizon2_package_list`, `horizon2_package_register`, `gcloud_inventory`, `gcloud_ops`, `cloud_worker_recover`, `mcp_gateway` |
| `github` | 12 | `autonomous_cycle_start`, `autonomous_cycle_manage`, `autonomous_cycle_get`, `autonomous_cycle_transition`, `autonomous_cycle_record_usage`, `autonomous_cycle_list`, `autonomous_cycle_kill`, `policy_authorization_record`, `policy_authorization_list`, `scan_for_reviewable_tasks`, `review_and_notify`, `autonomous_cycle_green_path_eligibility` |
| `chat-bridges` | 3 | `zulip_send_message`, `zulip_fetch_messages`, `relay_manage` |
| `dev-tools` | 31 | `web_search`, `web_fetch`, `dash_list`, `dash_search`, `dash_view`, `fs_read`, `fs_write`, `fs_list`, `fs_search`, `run_cmd`, `run_cmd_check`, `detect_dev_servers`, `python_lint_and_format`, `fs_repo_map`, `export_tasks`, `export_data`, `export_memory`, `export_journal`, `export_migration_manifest`, `compare_migration_manifests`, `import_migration_manifest`, `rollback_migration_import`, `read_image`, `format_render`, `bug_worker_manage`, `red_team_qa`, `obsidian_status`, `obsidian_open`, `obsidian_active_note`, `obsidian_search`, `obsidian_append` |
| `skills` | 7 | `skill_review`, `synthetic_workflow`, `skill_registry`, `check_agent_capability`, `capability_catalog`, `workflow_run`, `workflow_list` |
| `knowledge-jobs` | 2 | `goal_manage`, `propose_skill_clusters` |
| `control-center` | 3 | `graph_canvas_read`, `graph_canvas_write`, `browser_derive_task_reference` |
| `mcp-adapter` | 2 | `system_doctor`, `mcp_health_telemetry` |
| **total** | **201** | |

## When the check fails

Run `uv run python scripts/check_components.py` (add `--report` for the
tables above).

- **`UNMAPPED`**: a new module is in no component. Add it to
  `components.toml`, by role. When in doubt, not kernel.
- **`NEW`**: an import crosses components without a declared dependency. Do
  not add it to the baseline. Import from a component you already depend on,
  move the shared piece down, or invert the call (a post-init hook, a stop
  handler, a `change_feed` subscription). Declare a new `depends_on` only when
  the dependency is real and makes no cycle; `kernel` never gets one.
- **`FIXED`**: an undeclared import is gone. Delete its line from
  `components-baseline.txt`.
- **`AMBIGUOUS`**: two globs in different components claim a module. List it
  by exact name.
- **`UNUSED`**: a pattern matches no module. Remove it.
- **`CYCLE`**: two components depend on each other, directly or through
  others. Keep one direction and baseline the other's edges.
- **`INVALID`**: `kernel` has a `depends_on`, something depends on
  `mcp-adapter`, or a `depends_on` names no component.

`--write-baseline` regenerates the baseline from the current code. Use it only
when you reclassify modules or change `depends_on`, and review the diff.

The check reads imports with the layer checker's reader, so it sees and
misses the same things (see [layers.md](layers.md#what-the-check-sees-and-what-it-doesnt)):
lazy imports count, `importlib.import_module` strings and tool-name lookups
through `tool_surface` do not. K4 makes those lookups visible.

**Component loading (K4b, #2603).** Each manifest is imported by name when the
process selects its component, so `components/__init__.py` no longer holds a
static import block. The map carries that edge instead: the checker adds every
module `components.toml` declares as an import of `components`, which is the
same edge `affected_tests` reads for selection. A map that cannot be parsed
fails closed — the checker and the selector both treat it as declaring every
module, so a broken map widens a run instead of quietly shrinking it.

## Test ownership

Phase K2 (#2448). Every test file under `tests/` has one owner in
[`tests/component-owners.txt`](../../tests/component-owners.txt), so batch CI
can later run the tests of the components a change touches (K3) instead of
all of them. `scripts/check_components.py` computes the owners and fails with
`UNOWNED` (a test file has no line) or `STALE_OWNERS` (a line no longer
matches); `--write-owners` rewrites the file.

**The rule.** The owner comes from the source modules a test file depends on
*directly*: its own imports and string references (a `monkeypatch` target, an
entry point, a `python -m` run, a path such as
`"src/unified_agent/x.py"`), plus those of the test helpers and scripts it
imports, its package `__init__` files and its directory conftests. The graph
is the one `src/unified_agent/affected_tests.py` builds for review-time test
selection. Edges from one src module to another are not followed: through the
src closure every test would reach nearly every component. Path strings count
only with a directory part, so a bare `"x.py"` names nothing. The root
`tests/conftest.py`, loaded by every test, is left out, as in affected_tests.
With N the components of those modules other than `kernel` and `mcp-adapter`:

| Case | Owner |
|---|---|
| The file, or a helper or script it reaches, is one affected_tests cannot fully read | `unresolved` (later phases always run it) |
| N empty, no mcp-adapter module | `kernel` |
| N empty, some mcp-adapter module | `mcp-adapter` |
| One component in N | that component |
| Two or more | `integration:<a>+<b>+...`, sorted |

A test that imports `server`, the composition root that registers every tool
family, reaches every component, and so does a test that scans the whole
source tree as text (affected_tests' tree scanners). Their N is all 14
components other than `kernel` and `mcp-adapter`: an honest blanket
`integration:` owner. `mcp-adapter` is never named inside an `integration:`
owner, because a change to it runs the full suite in K3 anyway.

Not seen: a test whose helper *script* reads every module (the layer and
component checks themselves) is owned by what it imports, not by every
component; K3 keeps such tests in its always-run inventory.

**Where it stands** (measured on 2026-11-10, when K2 landed).
`uv run python scripts/check_components.py --report`, test functions counted
as `def test_` and `async def test_` lines:

| Owner | Files | Tests |
|---|---|---|
| integration:* | 270 | 6,557 |
| unresolved | 101 | 3,950 |
| kernel | 98 | 1,353 |
| dispatch | 29 | 459 |
| github | 24 | 384 |
| audit | 17 | 361 |
| retrieval | 23 | 337 |
| telemetry | 10 | 308 |
| control-center | 22 | 300 |
| mcp-adapter | 24 | 300 |
| dev-tools | 19 | 267 |
| llm | 8 | 184 |
| cloud | 8 | 169 |
| node | 9 | 161 |
| chat-bridges | 8 | 151 |
| knowledge-jobs | 7 | 147 |
| skills | 4 | 105 |
| auth | 2 | 29 |
| **total** | 683 | 15,522 |

Single owner: 312 of 683 files (45.7%) and 5,015 of 15,522 tests (32.3%).
Unresolved: 101 files, 3,950 tests. 179 files (5,149 tests) import `server`
or scan the tree; 22 of them are unresolved anyway, and the other 157 files
(4,042 tests) make up most of the `integration:` row. Building the
affected_tests graph once takes 19 to 22 s on the 4-core desktop under load
(load average 14 to 18); the whole check, both graphs, runs in about 25 s.

The 20 largest `integration:` files by test count:

| File | Tests | Components |
|---|---|---|
| `tests/test_doctor.py` | 192 | every component |
| `tests/test_audit.py` | 158 | audit, skills |
| `tests/test_webui_routes.py` | 129 | every component |
| `tests/test_wake_idle_session.py` | 105 | every component |
| `tests/test_planning_loop.py` | 100 | auth, knowledge-jobs |
| `tests/test_node.py` | 97 | every component |
| `tests/test_session_credentials.py` | 95 | every component |
| `tests/test_operating_profiles.py` | 85 | control-center, dispatch, github, knowledge-jobs |
| `tests/test_safety.py` | 77 | audit, dev-tools |
| `tests/test_profiles.py` | 70 | every component |
| `tests/test_mcp_gateway.py` | 69 | every component |
| `tests/test_sleep_pass.py` | 68 | knowledge-jobs, llm |
| `tests/test_run_records.py` | 67 | knowledge-jobs, telemetry |
| `tests/test_acp_followups.py` | 66 | every component |
| `tests/test_skill_packages.py` | 65 | every component |
| `tests/test_task_liveness.py` | 62 | knowledge-jobs, node, telemetry |
| `tests/test_forwarded_trust.py` | 61 | every component |
| `tests/test_distro_cli.py` | 59 | audit, github |
| `tests/test_github_app_fastpath.py` | 57 | audit, github |
| `tests/test_autonomous_cycles.py` | 56 | cloud, github |

**What would move the most tests to one owner.** The composition root. 157
resolved files with 4,042 tests are `integration:` of every component only
because they import `server` (or scan the tree), often just to reach one tool
family's functions or the profile filter. Without that rule, 64 of them
(1,172 tests) would already have a single owner. Letting a test build the
registry of one component, which K4's manifests make possible, moves those
first, and every test rewritten to import its component instead of `server`
leaves the blanket row. The `unresolved` lever has since been pulled: 17 files
were unresolved only through a helper, `tests/growth_budget.py` (a computed
`import_module`) and `scripts/ci_changes.py` (an `exec_module` of a spec built
inside the function), and both loads are static now (task #2458), so those
files have real owners and neither helper drags them into every Python change.
The unresolved files that remain build a `python -c` or subprocess command at
run time, or touch `builtins`/`importlib` in a way affected_tests cannot
follow; #2572 works through them in batches.
