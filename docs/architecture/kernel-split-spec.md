---
title: Spec: kernel split (components, manifests, component-scoped CI)
date: 2026-11-10
author: agent-claude
status: approved (the operator, 2026-11-10), including the red-tier files this spec names; K1, K4a, K4b, and K5a in build
tags: [spec, architecture, ci, unified-agent]
project: unified-agent
epic: 2437
---

# Kernel split

Epic #2437. This spec adds a second, vertical axis to the
[layer map](layers.md): every module belongs to one **component**, the
**kernel** imports no other component, and CI certifies a change by the
components it touches instead of by the whole suite.

The map and the checks move no files. From K6 on, a component may be
rewritten instead of untangled when that is the shorter road, on one
condition the operator set on 2026-11-10: functionality is retained, and the PR
proves it (see "Rewrite rule"). The
[TypeScript platform ADR](typescript-platform-strategy-adr.md) still holds
on one point: Python keeps the only policy implementation. There is no port
to another language.

## Goal

The product is a meta harness: one operator adds local and cloud agents to
an organization, and the platform gives them organization, coordination,
memory, session continuity, handoffs, graph execution and planning. The
split serves that goal by making each of those a component with an owner, a
test suite and a boundary, so one can be improved or replaced without
re-verifying the rest.

| Capability | Component that owns it |
|---|---|
| Organization: org, roles, offices, identity, policy, approvals | kernel |
| Adding an agent: local enrolment, cloud OAuth clients | auth, node, cloud |
| Coordination: queue, claims, messages, wake | kernel |
| Memory: write, lexical recall, scoping, provenance | kernel |
| Memory: semantic and graph recall, consolidation | retrieval, knowledge-jobs |
| Session continuity: packets, resume, native session discovery | kernel, node |
| Handoffs: typed handoff, grounding, receipts | kernel |
| Graph execution: work graph, dependency-driven dispatch, recovery | dispatch |
| Planning: goals, plans, joint plans, workflows | kernel, skills, knowledge-jobs |

Which mechanisms each component should take from the reference projects
(crewAI, Paperclip, Clowder, supermemory and the rest of the north-star
list) is decided in the capability map, task #2447,
`docs/roadmaps/2026-11-10-meta-harness-capability-map.md`.

## Context

One operator and about ten agents ship through this repo. The package grew
from 76 source files and 1,427 tests (2026-08-01) to 406 modules and 15,451
tests (2026-11-10). Every source change is verified by all of them, in a batch
run that passes about half the time. The fleet waits on that run, and most of
what it verifies has nothing to do with the change.

The layer map already proved the method this spec reuses: declare the
boundary in a TOML file, check every import, record today's violations in a
baseline that may only shrink. That took upward imports from 54 to 0. Layers
answer "which way may imports point". They do not answer "which tests can
this change break", because every layer contains a slice of every feature.

## Current state

Measured at `main` `7010e09a` on 2026-11-10. Commands are in the appendix.

| Fact | Value |
|---|---|
| Modules under `src/unified_agent` | 406, 266,611 lines |
| Test files, test functions | 682, 15,451 |
| Batch CI, last 100 dispatched runs | 54 passed, 26 failed, 20 cancelled |
| Passing batch run | median 25 min, p90 35 min |
| PR open to merge, last 200 merged | median 3.0 h, p75 9.1 h, p90 37.8 h |
| Batch selection for any `src/` or `scripts/` Python change | full suite (`scripts/ci_changes.py`, `UNCERTIFIABLE_PYTHON`) |
| Import closure of tasks, memory, vault, messaging, approvals | 346 of 406 modules (250,071 lines) |
| Same closure without the `tool_surface -> server` seam | 216 modules (162,489 lines) |
| Tools called on the hub in 24 h | 30 of about 199 registered |
| Doctor capabilities | 49 live, 28 dormant |

Why the full suite is forced: `affected_tests.py` reads a static import
graph, and its own docstring says a lazy hop or an unreadable dynamic import
is not followed. The biggest such hop is `tool_surface`, which imports
`server.py` lazily and through it every tool. Nothing declares which features
may call which, so no subset can be trusted.

Why batches fail: three of the four failures reported on 2026-11-09 and
2026-11-10 were a one-line miss in a central registry, found only by the full
run (see K5a). The fourth was a time-dependent test.

### Draft component map

A first cut by name prefix, to size the work. K1 replaced it with a reviewed
map (PR #1488), and [components.md](components.md) holds the numbers of
record: 75 of 2,178 internal imports cross a component without a declared
dependency, the kernel is 121 modules with 33 outbound edges, and `auth`,
`cloud` and `dev-tools` already have no undeclared importer. The table below
is the draft, kept to show how far placement by role moved it.

| Component | Modules | Lines | Test files owned | Tests owned |
|---|---|---|---|---|
| kernel | 134 | 79,785 | 135 | 2,022 |
| dispatch | 40 | 29,255 | 44 | 808 |
| control-center | 22 | 19,232 | 14 | 148 |
| github | 16 | 17,507 | 19 | 443 |
| dev-tools | 28 | 16,198 | 41 | 723 |
| audit | 25 | 15,164 | 20 | 542 |
| telemetry | 17 | 14,835 | 6 | 231 |
| node | 16 | 13,281 | 10 | 171 |
| mcp-adapter | 27 | 13,175 | 26 | 505 |
| knowledge-jobs | 17 | 12,847 | 13 | 287 |
| llm | 17 | 9,420 | 11 | 123 |
| auth | 10 | 6,593 | 4 | 48 |
| cloud | 13 | 6,320 | 8 | 165 |
| chat-bridges | 9 | 4,965 | 5 | 72 |
| skills | 7 | 4,736 | 5 | 115 |
| retrieval | 8 | 3,298 | 24 | 335 |
| (spans two or more) | | | 297 | 8,766 |

A test file is "owned" when its direct imports touch at most one non-kernel
component. On this draft the kernel imports other components through 107
edges (dispatch 30, telemetry 12, knowledge-jobs 11, retrieval 10, audit 10,
node 9, auth 6, github 5, chat-bridges 4, dev-tools 4, llm 2, skills 2,
cloud 1, mcp-adapter 1), and components import each other through 258 more.

## Proposed change

### Vocabulary

- **Component**: a named set of modules that ships a feature family. Every
  module is in exactly one.
- **Kernel**: the component everything else stands on. It imports no other
  component. It holds what is needed to serve tasks, messages, lexical
  memory, the vault, approvals, the evidence ledger, sessions, identity and
  policy. When in doubt a module is not kernel.
- **Composition root**: `mcp-adapter`. It may depend on every component and
  nothing may depend on it.
- **Certifiable**: a component whose every importer is declared. Only a
  certifiable component's changes can be verified by a subset.

The 16 component names are fixed: `kernel`, `mcp-adapter`, `control-center`,
`dispatch`, `node`, `github`, `chat-bridges`, `audit`, `telemetry`, `llm`,
`retrieval`, `cloud`, `auth`, `knowledge-jobs`, `skills`, `dev-tools`.
Adding or renaming one is a change to this spec.

### Phases

```
K0 spec ──> K1 map + checker ─┬─> K2 test owners ──> K3 scoped selection (shadow, then enforced)
                              │        └─> K2b ownable tests (needs K4b) ──> K3 reaches its target
                              ├─> K4 manifests compose the registry ──> K5b manifest-owned registries
                              └─> K6 edge burn-down (one component per PR) ──> K7 workspace members
K5a inventory tests on every PR (independent, starts now)
```

K1 goes first because every later phase reads its map. K5a is independent
and removes the commonest batch failure today. K3 needs K2's owners and at
least one certifiable component, which K6 produces. K4 is what makes
tool-name calls visible, so K3 is enforced only after K4. K7 is last because
moving files before the edges are gone would move the tangle, not cut it.

### K1: component map and checker (#2439)

New file `docs/architecture/components.toml`:

```toml
package = "unified_agent"
source = "../../src"
baseline = "components-baseline.txt"

[[component]]
name = "kernel"
description = "Identity, policy, storage, events, tasks, messages, lexical memory, vault, approvals, evidence."
depends_on = []
certifiable = false
modules = ["config", "storage", "events", "safety", "policy", "tools.tasks"]

[[component]]
name = "github"
description = "GitHub App, Approval Ledger statuses, PR watch, review pipeline, merge."
depends_on = ["kernel", "telemetry"]
certifiable = false
modules = ["github_app*", "pr_watch", "reviewer", "merge"]
```

Rules the checker enforces:

1. Every module is claimed by exactly one component. Exact names beat globs,
   as in `layers.toml`.
2. An import from module A to module B is allowed when they share a
   component, or when B's component is in the transitive `depends_on` of A's.
3. `kernel` has `depends_on = []`. `mcp-adapter` appears in no other
   component's `depends_on`.
4. The `depends_on` graph has no cycle. Where two components import each
   other today, declare the dominant direction and baseline the rest.
5. Every other import is a violation. Today's violations are written to
   `docs/architecture/components-baseline.txt`, one `importer -> imported`
   line each, sorted.

`scripts/check_components.py` reuses the import reader from
`scripts/check_layers.py` and fails with the same verdict names:
`UNMAPPED`, `AMBIGUOUS`, `NEW`, `FIXED` (delete the line), `UNUSED`, plus
`CYCLE` and `INVALID` (rule 3 broken, or a `depends_on` that names no
component). `--report` prints the per-component table above and the edge counts.
`--write-baseline` regenerates the baseline and is for reclassification only.

A module imported by many kernel modules is usually a kernel primitive in the
wrong folder, not an edge to invert. `telemetry.redaction` (11 kernel
importers on the draft) is the example: K1 places it in the kernel and says
so in `components.md`.

`certifiable` stays `false` for every component in K1.

### K2: test ownership map

`check_components.py --write-owners` writes `tests/component-owners.txt`, one
line per test file: `tests/test_x.py kernel`, `tests/test_y.py github`, or
`tests/test_z.py integration:dispatch+node`. Ownership is computed from the
file's imports and string references with the graph `affected_tests.py`
already builds. The check fails when the committed file is stale.

Measured by K2 (PR #1493, 683 test files, 15,522 tests):

| Owner | Files | Tests |
|---|---|---|
| `integration:*` (two or more components) | 270 | 6,557 |
| `unresolved` (a dependency the graph cannot read) | 101 | 3,950 |
| `kernel` | 98 | 1,353 |
| exactly one other component | 214 | 3,662 |

Only 312 files (45.7%) and 5,015 tests (32.3%) have a single owner. 179
files (5,149 tests) import `server` or scan the whole source tree, which
ties them to every component. So with K3 alone, a change confined to one
certifiable component still runs the unresolved tests and the whole-tree
tests: about 9,100 of 15,522. That is a cut of about 40%, not the 80% this
spec first aimed at.

### K2b: make tests ownable

Two changes move tests to a single owner, and K3's payoff depends on both:

- Resolve the unresolved. 17 of the 101 files were unresolved only through
  two helpers, `tests/growth_budget.py` and `scripts/ci_changes.py`; both
  loads are static now (#2458), which moved those files to real owners. The
  unresolved files that remain need their dynamic loads made static, file by
  file (#2572).
- Stop importing `server` to reach a tool. Tests that only need one
  component's tools behind the policy guard get them from a fixture that
  composes that component's manifest (K4a) with the kernel. This needs
  K4b, because the fixture must not load the other components.

### K3: component-scoped batch selection

For a batch diff, let C be the components that own the changed source
modules. The run is the full suite when any changed module is in `kernel` or
`mcp-adapter`, any component in C is not certifiable, or Python under
`scripts/` changed. Otherwise the run is the union of:

- tests owned by a component in C or by a component that depends on one;
- every `integration:` test that names one of those components;
- the inventory tier (K5a);
- whatever `affected_tests.select()` picks from the import graph.

A component may be marked `certifiable = true` when the baseline has no line
that imports it from an undeclared importer, `affected_tests` reports no
unresolved source module inside it, and its tools are registered through a
manifest (K4).

Shadow first: every full run writes `component-selection.json` with the
subset it would have run. A failing test outside that subset is a miss.
Enforcement turns on with the repository variable
`UA_CI_COMPONENT_SELECTION=1` after 30 consecutive full runs with zero
misses, and turns off by unsetting it. The nightly full suite does not change.

### K4: manifests compose the tool registry

New package `src/unified_agent/components/`, one module per component, each
exporting one `Component(name, tools, post_init, tables, env)` value.
`server.py` builds its registration tuple by concatenating manifests in a
fixed order instead of naming about 200 functions. This replaces the
`tool_surface -> server` seam with a registry the checker can read (it
closes #1725's first half), and it makes a tool-name call an edge: a literal
`tool_surface` lookup counts as an import of the module that defines the
tool, and a module that looks tools up by a computed name is listed as a
dynamic caller whose tests always run.

It ships in two PRs. K4a (#2449) moves registration into manifests with
every component loaded and no behaviour change. K4b adds the selection
below, and its first PR is the prerequisite the selector needs: `affected_tests`
now reads the component map's declared edges (`declared_component_modules`), so
a conditional or computed manifest load cannot shrink selection. That PR is a
no-op on today's numbering and proves the substitution before any import
changes (#2562).

`UA_COMPONENTS` selects what a hub loads (K4b shipped in #2603). Unset or blank means all. `kernel`
means the kernel and the adapter. The adapter loads with every selection,
because a process with no adapter registers no MCP tools, and it contributes its
own manifest only: `depends_on` is an import permission, not a load requirement,
so a named component pulls in nothing but itself. (`mcp-adapter` declares all
fifteen others; expanding that would load everything and make the variable do
nothing.) An unknown name stops start-up. A kernel-only selection is a real
posture, not a degraded one: the kernel's own tests run with no component
loaded, which is what catches a manifest edge lost silently. A component map
that cannot be read fails closed — the test selector widens to every module
rather than shrinking to none.

### K5a: inventory tests on every pull request (#2440)

| PR | Registry missed | Found by |
|---|---|---|
| #1471 | `layers.toml` placement for `messaging.availability` | gates, first run |
| #1479 | `tenant_tables.TABLE_TENANCY` entry for `approval_receipt_intents` | batch, `test_tenant_table_inventory` |
| #1480 | `_TASK_IMPORT_REFERENCES` entry for `work_proposal_items.task_id` (`tools/export.py:121`) | batch shard 0, `test_import_rollback_ownership` |

The PR-tier `gates` job gains one step that runs the registry and inventory
tests, named in one checked-in list. Budget: at most 3 minutes added to
`gates`. The same step runs `check_components.py` once K1 has merged.

### K5b: manifest-owned registries

Tables, tenancy class, import-rollback references, environment variables and
reference-doc sections move into each component's manifest. The central
files become assembled views with a `--check` mode, the way `changelog.d`
replaced hand edits to `CHANGELOG.md`. In the last 30 days
`docs/tools-reference.md` took 77 commits, `docs/configuration.md` 57,
`layers.toml` 44 and `docs/cli-reference.md` 39; each is a file that
unrelated PRs must all edit.

### K6: burn the kernel's outbound edges to zero

One component per PR, smallest first so components become certifiable early:
cloud, llm, skills, chat-bridges, dev-tools, github, auth, node, retrieval,
audit, knowledge-jobs, telemetry, dispatch. Use the inversions already in
the tree: a post-init hook, a stop handler, a `change_feed` subscription, or
moving a shared primitive down. Old names stay re-exported, as the layer
work did.

#### Rewrite rule

A K6 PR may replace a component's internals, or the kernel API a component
stands on, instead of inverting edges one by one. It is accepted when all of
these hold at the PR head:

1. The component's owned tests (K2) pass with no test deleted or weakened.
   A test that pinned an internal detail may be rewritten only if the PR
   lists it and says which behaviour test replaces it.
2. Tool names, argument schemas and `docs/tool-surface-baseline.json` are
   unchanged, or the PR lists each change and the capability map or the operator
   asked for it.
3. The object-model parity tests (`object_model.py`, MCP resources and
   `/api/platform/*`) pass.
4. Stored data survives: a database created at the previous `main` opens
   and reads back the same rows after the change, shown by a migration test.
5. The component's baseline lines go down, never up.

### K7: workspace members

A component with no undeclared importers becomes a uv workspace member.
Constraints: `python -m unified_agent.<module>` paths do not change, a
default `uv sync` installs every member, and `deploy/` changes only with
the operator's approval. The file layout is decided in a K7 design note after K4,
because it depends on whether `unified_agent/__init__.py` can become a
namespace package without breaking the systemd and launchd units.

## Acceptance criteria

1. K1: `uv run python scripts/check_components.py` exits 0 at the PR head;
   adding an import from a kernel module to a non-kernel module makes it
   exit 1 with `NEW`; deleting a baselined import makes it exit 1 with
   `FIXED`; a module in no component makes it exit 1 with `UNMAPPED`.
2. K1: all 16 components exist; every module is placed; `--report` prints
   module, line and edge counts per component.
3. K2: `tests/component-owners.txt` lists all test files and the check
   fails on a stale or missing line. K2b: at least 65% of tests have a
   single owner (32.3% at K2) and fewer than 20 files are unresolved (101
   at K2).
4. K3: 30 consecutive full runs with zero misses before enforcement; after
   it, a batch whose diff is confined to one certifiable component runs
   fewer than 9,500 tests at K2's ownership, and fewer than 3,000 once
   K2b's target is met.
5. K4a: the set of registered tool names and every tool's schema
   fingerprint are identical before and after. Registration order may
   change once, to component order then manifest order; the PR names
   whatever observes order and regenerates it. K4b: with
   `UA_COMPONENTS=kernel`, importing `unified_agent.server` in a fresh
   interpreter loads no module outside `kernel` and `mcp-adapter`.
6. K5a: a PR that adds a table without a `TABLE_TENANCY` entry fails `gates`
   on the pull request; `gates` grows by at most 3 minutes.
7. K6: kernel outbound edges in the baseline reach 0.
8. Epic: over 100 consecutive batch runs, at least 60 are certified by a
   subset, their median wall time is 10 minutes or less, and no batch fails
   on an inventory test.

## Testing plan

| Layer | What | Count |
|---|---|---|
| Unit | `check_components.py` verdicts on a fixture package: allowed, NEW, FIXED, UNMAPPED, AMBIGUOUS, UNUSED, CYCLE | +8 |
| Unit | The real tree passes; baseline is sorted and has no stale line | +2 |
| Unit | Owners file fresh; selection rule on fixture diffs (kernel change, certifiable change, mixed) | +6 |
| Integration | Tool surface parity; `UA_COMPONENTS=kernel` import closure in a subprocess | +3 |
| CI | `tests/test_ci_workflow_parity.py` pins the new gates step | +2 |

Each PR stays inside the 60-test budget.

## Rollback

| Phase | Undo |
|---|---|
| K1, K2 | Remove the gate step and the test; the map is data. |
| K3 | Unset `UA_CI_COMPONENT_SELECTION`. Batches run the full suite again. |
| K4 | Revert the PR; the tuple in `server.py` returns. Unset `UA_COMPONENTS`. |
| K5a | Remove the step from `ci.yml` and its `.depot` copy. |
| K6 | Revert the one PR; old names are still re-exported. |

## Effort

| Phase | Agent time |
|---|---|
| K1 | 4 h map review, 2 h checker and tests |
| K2 | 3 h |
| K3 | 4 h shadow, 2 h enforcement |
| K4 | 8 h |
| K5a | 2 h |
| K5b | 6 h |
| K6 | 13 PRs, 2 to 6 h each |
| K7 | design note 2 h, then per component |

## Red tier

K3 edits `scripts/ci_changes.py` and `affected_tests.py`. K4 edits
`server.py`. K5a edits `.github/workflows/ci.yml` and `.depot/workflows/`.
K7 may touch `deploy/`. The operator approved the direction and the red-tier
merges for the files named here on 2026-11-10 (recorded on task #2437).
`deploy/` in K7 and any red-tier file this spec does not name still need
their own approval.

## Out of scope

- Porting any of it to another language (decision note `0783e69645aa`).
- A rewrite that drops behaviour. Removing a feature is the operator's call, one
  feature at a time, not a side effect of a rewrite.
- Moving or renaming files before K6, other than by a K6 rewrite.
- Removing dormant features. A dormant component stays; it stops taxing
  unrelated changes.
- Changing the rules in `layers.toml`. Both checks run.
- The hub's deployment, storage engine or tenancy model.

## Related

- [Layer map](layers.md): the horizontal axis and the method.
- #1725: `tool_surface` entry point and uv workspace per layer. K4 and K7
  take it over along the component axis.
- #1231 (B4): directory repackaging under Epic B #1187. Still separate.
- [Platform rework review](platform-rework-review-2026-10-03.md): finding 1
  (governance composed inside the MCP adapter) is why `mcp-adapter` is the
  composition root here.
- [CI batch verification](../ci-batch-verify.md): the contract K3 extends.

## Appendix: how the numbers were measured

- Module, line and test counts: `git ls-tree -r origin/main`, then an `ast`
  walk of every file for imports and `def test_` lines.
- Closure: reachability over that graph from `tools.tasks`, `tools.memory`,
  `tools.vault`, `messaging`, `tools.approvals`, `storage`, `config`,
  `safety`, `policy`, `gateway`, `profiles`, `events`, `tool_surface`,
  `identity_core`, with and without `server`.
- Batch CI: `gh run list --workflow ci.yml --event workflow_dispatch --limit 100`.
- PR latency: `gh pr list --state merged --limit 200 --json createdAt,mergedAt`.
- Hub usage: `observability_manage(action="performance")` and
  `system_doctor()`, 24 hours ending 2026-11-10T12:20Z.
