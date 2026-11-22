# Development

## Repo layout

```
src/unified_agent/
  server.py        MCPServer wiring: registers every tool, picks stdio/HTTP transport
  components/      One manifest per component: the tools it registers (server.py
                   composes them). UA_COMPONENTS selects which load (unset = all;
                   kernel = kernel + mcp-adapter). See architecture/components.md.
  config.py        UA_* env vars, hard limits, path helpers
  safety.py        Sandbox + allowlist chokepoints (see security.md)
  storage.py       SQLite schema (memory/tasks/workspace_symbols), migrations, connect()
  tenant_tables.py Tenancy of every table (partitioned/derived/accelerator/global); see below
  embeddings.py     fastembed vector generation, cosine similarity (lazy-loaded model)
                    memory_vec: sqlite-vec accelerated KNN mirror of memory_embeddings,
                    optional/best-effort — see storage.upsert_memory_vec
  sync.py          Dumps memory/tasks state to vault Logs/ on every mutation
  merge.py         unified-agent-merge CLI + orchestrate_merge() (shared with git_merge_task)
  wiki.py          unified-agent-wiki CLI + run_custodian() (shared with vault_wiki_custodian)
  tui.py           unified-agent-tui: Textual dashboard (local or remote-hub mode)
  tools/
    web.py         web_search, web_fetch
    dash.py        dash_list, dash_search, dash_view
    files.py       fs_read, fs_write, fs_list, fs_search
    indexer.py     fs_index_symbols, fs_list_symbols, fs_search_symbols
    shell.py       run_cmd, run_cmd_check, container sandbox execution
    audit.py       security_audit (thin wrapper over audit/)
    skills.py      skill_review
    git.py         git_diff_staged, git_commit_staged, git_worktree_*, git_merge_task
    lint.py        python_lint_and_format
    memory.py      memory_save/search/list/get/delete + index-note maintenance
    tasks.py       task_add/update/list/delete/claim
    vault.py       vault_read/write/list/search/links/journal/wiki_custodian
    obsidian.py    obsidian_status/open/active_note/search/append
    dispatch_report.py  dispatch_report_self (hub-mediated run completion)
    langgraph.py   langgraph_run, langgraph_progress_note, langgraph_workflow_run, langgraph_checkpoints
    server.py      horizon2_server_list/register/revoke, horizon2_connector_authorize, horizon2_package_list/register
  dispatch/        approved-dispatch coordinator, scheduler, spawn, sandbox, report
  langgraph_pilot.py / langgraph_dispatch.py / langgraph_observability.py
  langgraph_note.py / langgraph_workflow.py
                   execution-plane library; MCP wrappers in tools/langgraph.py
  horizon2.py      provider envelope + governed-server registry (not an executor)
  queue_hygiene.py stale-claim / aging-demote / auto-archive (review scan)
  pr_watch.py      unified-agent-pr-watch: gh PR events → memory + journal
  lane_b_telemetry.py  read-only Lane B gate report (module CLI, not installed)
  pytest_attestation.py  sign/verify local pytest as an in-toto DSSE envelope
                    (module CLI, not installed; see [Local pytest attestation](#local-pytest-attestation))
  shipping_preflight_cli.py  exact-head Ledger and review-head preflight
                    (module CLI, not installed)
  review_head_status.py  the unified-agent/review-head check, per head
  audit/
    engine.py      run_audit(): file walk, rule dispatch, suppression/exclusion, dedup
    rules.py       CONTENT_RULES / REPO_RULES, Finding dataclass, SEVERITIES
    remediate.py   Auto-fix logic for 4 of the rules
    cli.py         unified-agent-audit entry point
tests/              pytest suite (one file roughly per module)
scripts/install-hooks.sh   Installs the pre-commit audit gate
scripts/local-gate.sh   Local verification gate command-for-command
                    mirroring CI's `test` job — ruff check, ruff format
                    check, pytest, make smoke, security audit, both named
                    gates — plus a local-only leg (staged
                    conflict-marker guard); against
                    the exact SHA (or working tree, if dirty) in your
                    worktree; single PASS/FAIL summary (task #1214). Run
                    before pushing.
scripts/smoke.py    Live smoke suite (make smoke) — unmocked checks against
                    the real installed venv, see its module docstring
scripts/gate_replay.py   Read-only soak-week gate replay report: classifies
                    approval-gate history (`approvals`/`agent_perf_samples`)
                    into an auto/notify/hard-block model as evidence for the
                    phase-3 conditional fs_write-gating decision. Never writes
                    raw command/payload text — see its module docstring.
scripts/gate_e_rollback_rehearsal.py   Gate E rollback-proof rehearsal
                    (task #285): exercises export_migration_manifest/
                    import_migration_manifest/rollback_migration_import
                    end-to-end against fixture data under a throwaway
                    UA_HOME, never touching real host state. Emits a JSON
                    evidence report; `uv run python
                    scripts/gate_e_rollback_rehearsal.py [--out PATH]`.
scripts/db_restore_rehearsal.py        Complete SQLite backup/restore rehearsal
                    under a throwaway UA_HOME; verifies schema, complete table
                    set, and portable row counts. Emits a JSON evidence report.
scripts/node-release-roll.sh   Rolls this host's ua-node through
                    ~/.unified_agent/release/current: gate, atomic swap,
                    restart, automatic rollback; --check-only, --dry-run,
                    --retire-stale (docs/deploy/node-release-roll.md).
deploy/              systemd unit + steward cron script for hub mode
packages/            protocol (Zod/Pydantic), conformance golden fixtures, typed hub client
.github/workflows/ci.yml   Cheap gates on pull requests; affected tests on
                    the merge queue; full suite on the nightly schedule and
                    `workflow_dispatch` ([ci-batch-verify.md](ci-batch-verify.md))
```

## Build, test, lint

```bash
uv sync --all-extras                 # create .venv, install runtime deps + every optional extra + dev group
uv run pytest                        # full test suite (large matrices sampled, see below)
uv run pytest tests/test_safety.py -k dotdot   # single test by keyword
uv run ruff check .          # lint
make smoke                           # live smoke suite: unmocked checks against the real venv
```

`make smoke` (`scripts/smoke.py`) is a deliberate complement to `pytest`, not a
replacement: it exercises the real fastembed model, the real sqlite-vec
extension, real tree-sitter parsing, and the real audit engine — no mocks —
in an isolated temp `UA_HOME`/`UA_WORKSPACE`/`UA_VAULT_PATH` so it never
touches real state. This is what catches the class of bug a fully-mocked
unit suite can miss (tasks #104/#110: tree-sitter arrow-function indexing
shipped broken and stayed broken in production because its tests were fully
mocked). PDF fetch and export remain optional runtime dependencies through the
`pdf` extra, but the development group includes them because the full pytest
suite covers both behaviors unconditionally. The local CI-parity gate uses
`uv sync --all-extras`; a plain `uv sync` remains sufficient for the basic
commands above. Run it before a hub deploy (cockpit
`docs/runbooks/DEPLOY-STEWARD.md`); the old `deploy/pull-and-restart.sh` that ran it
is retired to `deploy/systemd/retired/`.

`pyproject.toml`: `[tool.pytest.ini_options]` sets `asyncio_mode = "auto"` and
`testpaths = ["tests"]`. Product integration scenarios live in `tests/e2e/`; run
them on their own with `uv run pytest -q tests/e2e`. External CI and
GitHub merge gates are verified by those systems, not by simulated Python gates.
`[tool.pyright]` holds the
Python include/exclude rules. `[tool.ruff]` sets `line-length = 100`; `[tool.ruff.lint]`
selects `E4 E7 E9 F I UP B` (pycodestyle subset, pyflakes, import sort, pyupgrade,
bugbear).

### Sampled parametrize matrices

The redaction, JWK, secret and audit test files (`SAMPLED_FILES` in
`tests/param_sampling.py`) hold about 50,000 of the suite's 72,000 parametrized
cases. A plain `pytest` run, locally and in CI, collects a **representative**
subset of each large matrix: every value of every dimension (each shape,
consumer, layer, layout and spelling) occurs in at least one kept case, plus a
stable slice of the product, about one case in 100, chosen by a CRC-32 of the
node id. The selection is deterministic and the same on every pytest-xdist
worker. The cases left out are reported as `deselected` in the summary line.

| Tier | How to select it | Cases collected (whole suite, 2026-10) |
|---|---|---|
| `representative` | default | about 23,400 |
| `pairwise` (every pair of values of every two dimensions) | `--param-sample=pairwise` or `UA_TEST_PARAM_SAMPLE=pairwise` | about 27,700 |
| `exhaustive` (every case) | `--exhaustive`, `UA_TEST_EXHAUSTIVE=1`, `--param-sample=exhaustive` or `UA_TEST_PARAM_SAMPLE=exhaustive` | about 71,800 |

```bash
uv run pytest tests/test_redaction_oct_blocks.py                 # representative
UA_TEST_EXHAUSTIVE=1 uv run pytest tests/test_redaction_oct_blocks.py   # every case
```

`UA_TEST_EXHAUSTIVE=1` wins over any narrower tier. CI sets it whenever a change
can alter a sampled matrix (`scripts/ci_changes.py exhaustive`: the redaction,
JWK and audit scanners, the matrix files, the sampler, `tests/conftest.py`, the
CI decision, `pyproject.toml`, `uv.lock` or an unknown change set). Run the
exhaustive tier on demand, or on a nightly schedule, to cover the full product
when nothing forced it. `@pytest.mark.exhaustive` keeps one matrix unsampled in
every tier. Never run the exhaustive tier of the whole suite on the 4-core
desktop; scope it to the files you changed.

### SQLite turn-count acceleration

The `idx_event_log_node_item_turn` expression index accelerates ACP turn summaries
when SQLite 3.42 or later has JSON5 support. Older engines still initialize and
append original event payloads; initialization removes this optional index after
an engine downgrade. Existing NaN/Infinity payloads and their ledger hashes remain
unchanged. Older SQLite retains the existing best-effort summary limitation for
non-finite payloads. `system_doctor` reports the actual engine version and the
`node_turn_index` capability as LIVE, DORMANT, or DEGRADED. Restarting on a supported
engine rebuilds the index without rewriting history.

See [SQLite's JSON compatibility documentation](https://www.sqlite.org/json1.html#compatibility).

## Pre-commit hook and CI

`./scripts/install-hooks.sh` installs a `pre-commit` hook running
`scripts/run_audit.py . --fail-on high`, the audit of the committing checkout's
own `src` (a worktree is audited with its own code, task #2204). It is required
after every fresh clone, since `.git/hooks` isn't version-controlled.

**Pre-push (Antigravity).** `.agents/hooks.json` runs
`scripts/hook_pre_push.py` on `git push`. The gate runs `ruff check` and
`scripts/run_audit.py . --fail-on high` in the checkout being pushed: a
linked `.worktrees/task-<id>` has its own `.git`, so lint and audit read that
tree, not the primary checkout that holds the hook. An unexpected
error while those checks run blocks the push. A push whose cwd is outside
this repo is left alone. The hook still audits the working tree, not the git
objects on the wire; CI's `gates` job is the backstop for the commit under
test. See [security.md](security.md#security_audit-rule-catalog).

**Commit identity (#2079).** `scripts/check_commit_identity.py` fails when a
commit in a range (default `origin/main..HEAD`, merges included) has an
author or committer that is not a fleet label at an allowed address, or an
exact accepted `Name <address>` pair, or that looks like git's
`user@host.lan` fallback. Allowed addresses come only from `--allow-email`
or `UA_COMMIT_ALLOWED_EMAILS` (exact match; `git config user.email` is never
a default). The script prints the SHA and the amend command; it never
prints the rejected name or address. Run it isolated wherever the output
is published:

```bash
python3 -I -S scripts/check_commit_identity.py --allow-email ADDRESS
```

See [COLLABORATION.md](COLLABORATION.md) for accepted pairs, operator-name
setup on GitHub web merges, and how to allow a legacy identity already on
`main`.

GitHub Actions (`.github/workflows/ci.yml`) runs cheap gates on pull requests,
affected tests on the merge queue, and the full suite nightly
(task #1787; [ci-batch-verify.md](ci-batch-verify.md)):

- a pull request runs the gates job only. Markdown-only PRs (outside `src/`,
  `tests/` and `packages/`) never start a run;
- `merge_group` and a Mergify merge-queue pull request run
  `scripts/ci_changes.py affected`. The nightly schedule (08:00 UTC) and
  `gh workflow run ci.yml --ref batch/<name>` run the full suite;
- same-repository jobs run on `vars.UA_CI_RUNNER`, or
  `blacksmith-4vcpu-ubuntu-2404` when that variable is unset. Fork pull
  requests stay on `ubuntu-latest`. A self-hosted runner
  ([deploy/ci-runner](../deploy/ci-runner/README.md)) takes over by setting
  the variable. Docker and the install gate follow `UA_CI_ISOLATED_RUNNER`
  and stay GitHub-hosted by default.

`.depot/workflows/ci.yml` keeps the same jobs on Depot runners as a
`workflow_dispatch`-only copy; switching providers back is a trigger swap
described in that file's header and pinned by
`tests/test_ci_workflow_parity.py`. Check names are the same on both providers
(`CI / test (shard=0)` and so on), so the review gate accepts either.

| Job | What it runs |
|---|---|
| `gates` | the change classifier, the held-PR regression rule (PRs), the test budget (PRs), then for Python changes `uv sync --all-extras`, `ruff check .`, `ruff format --check .`, the layer check, local pytest attestation verify (and a sample rerun when the classifier asked for the full suite), `make smoke`, `unified-agent-audit . --fail-on high`, both named pytest gates, and the connector conformance harness when connector sources moved |
| `test` | waits for `gates`; on a full run, `pytest -q` across four shards split by test count (`scripts/ci_changes.py candidates --shard/--total`). A merge-queue run uses `scripts/ci_changes.py affected` instead, in one shard unless the selector prints `ALL`. `--durations=25` reports the slowest tests. one pytest-xdist worker per core on Depot's 8-vCPU shard sandboxes (two on the dispatch-only GitHub copy; `pytest_workers` dispatch input or `UA_CI_PYTEST_WORKERS`; `0` is serial); large parametrize matrices run the representative sample unless the change can alter them ([sampled matrices](#sampled-parametrize-matrices)) ([ci-batch-verify.md](ci-batch-verify.md)) |
| `sdk` | protocol (`npm test`, build, `codegen:check`, generated Pydantic tests/import), then client (`npm test`, build) |
| `web` | build SDK prerequisites, then frontend typecheck, Vitest, build and the WCAG 2.2 AA axe-core gate; runs independently of `sdk` |
| `js` | join the enabled SDK/frontend jobs, rejecting skipped or missing required results |
| `desktop-core` | the Tauri shell's engine-independent crate: `cargo fmt`, `clippy`, `test` |
| `full-verification` | requires success from every enabled job; drafts cannot pass this check |

The classifier, held-PR and conformance checks used to be separate jobs; each
job bills at least a minute and pays its own checkout and setup, so they are
steps in `gates` now. SDK and frontend checks run on separate runners after
`gates`; their stable aggregate remains `js`, so existing review check names
and `full-verification` retain their meaning. Each frontend job builds its SDK
prerequisites locally; it never relies on another runner's workspace or an
unverified artifact.

`scripts/ci_changes.py js-components` selects these two jobs separately from
the persisted six-key `classify` contract used by Cloud Build submission and
reconciliation. Frontend-only changes (plus optional Markdown documentation)
run `web` without SDK tests/codegen. SDK changes select both jobs because the
frontend consumes the SDK. Shared dependencies, mixed code changes, unknown
package paths and unreadable diffs select both. Python-only and docs-only
changes retain their existing JS skip behavior. Drafts and already-tested
trees cannot accidentally certify skipped component jobs.

Cloud Build applies the same component selection serially inside its existing
`js` container. Its topology, `CI / js` status, final gate and serial pytest
worker default are unchanged. Registry dependencies and a workspace superproject
are subsequent migrations, not part of this CI split.

**Which jobs run (task #1498).** The `changes` step skips work that cannot
change the answer. The rules live in `scripts/ci_changes.py` and are pinned by
`tests/test_ci_changes.py`. The table is the classifier's selection; a pull
request still only starts `gates`. `test` and the JS jobs wait until `full`
or `affected` is true (merge queue, nightly, or dispatch):

| Change | Python | JS stack |
|---|---|---|
| Markdown only (outside `src/`, `tests/`) | `gates` + the test files that name docs, one shard | skipped |
| Python sources (`src/`, `tests/`, `scripts/`, `Makefile`), with or without markdown | full, four shards | skipped |
| `packages/**` only | skipped | runs |
| `pyproject.toml` / `uv.lock` (protocol codegen uses uv tooling), workflows, anything else | full | runs |
| A dispatch (merge batch), or a path list that cannot be read | full | runs |

A PR's changed files come from the GitHub API, so every checkout is shallow.

**Drafts.** A labelled draft runs the cheap gates only; pytest and the JS
stack wait until the PR is ready. A draft's `full-verification` check
deliberately fails with an explanation that integration verification has not
run. This is not a merge-ready result. The optional CI green-rate provider
samples ready PR heads only, and missing draft metadata makes that metric
unavailable rather than inventing a sample.

The final check rejects a skipped, failed, cancelled or missing enabled job.
It also checks the matrix result, so every enabled pytest shard must succeed.

**Other workflows.** `docker.yml` and `install-gate.yml` never run on a PR.
They run after a merge that moves their inputs (Dockerfile and Docker assets,
the installer, its gate policy, `pyproject.toml`, `uv.lock`, package manifests
and lockfiles) and by hand. Dispatch both on a release tag before publishing
(`gh workflow run docker.yml --ref v<version>`).
Releases are cut by hand (there is no release script or workflow). Before
tagging, run `python scripts/changelog_assemble.py` to fold the pending
`changelog.d/` fragments into `CHANGELOG.md` `## [Unreleased]`, then rename that
section to the version in the release commit. CI runs the same script with
`--check` in the `gates` job. `dependency-audit.yml` is
manual. Every job in every workflow has `timeout-minutes`.

**Budget.** The first full GitHub-hosted run (PR #969, Python and JS) took
gates 1m31, shards 3m16/5m18/5m41/7m16 and js 7m43: about 35 billed minutes
with each job rounded up. A Python-only run skips `js`, so expect about 27.
Measure with `gh api repos/<owner>/<repo>/actions/runs/<id>/timing`; do not
reduce full coverage to meet a quota.

**Held PRs.** The held-PR step (task #1260; see
[automation.md's Held-despite-green section](automation.md#held-despite-green--same-day-regression-test))
runs `scripts/check_held_pr_regression_test.py` on every labelled PR run and
passes cleanly unless the PR carries the GitHub `held` label. On a batch
dispatch, `scripts/check_batch_held_prs.py` applies the same rule to every open
`held` PR the batch contains.

**Test budget (task #2233).** `scripts/test_budget.py` keeps the suite from
growing back. `tests/test_test_budget.py` fails any run that adds a test file
named after an issue number (`tests/**/test_*_<digits>.py`) or a test file over
1500 lines; files that broke either rule earlier are grandfathered in the
script at a fixed ceiling, and those lists may only shrink. On a pull request,

the `gates` step counts against the **current** base tip, not
`pull_request.base.sha` (task #2430). GitHub checks out a two-parent merge of
that tip and the PR head. `scripts/test_budget.py merge-parent --pr-head
<head>` returns the parent that is not the head. It refuses a revision that is
not a two-parent merge, and refuses a `--pr-head` that is not one of those
parents — otherwise the checkout is not this PR's merge and the count would
measure a different change. Counting from `pull_request.base.sha` credited the
PR with every commit the base gained since the last synchronize (#1481: 3
files / +31 tests failed as 20 files / +73). A stacked PR still carries its
stack, so land the under-budget PRs first.

It then runs `test_budget.py count --base <that parent> --head HEAD`. It
reads only the paths under `tests/` that differ between base and head (added,
modified, deleted or renamed), both versions of each, and counts every
`def`/`async def test*` in them at any nesting, helpers and `conftest.py`
included. Net is head minus base over those paths, so renames, moves and
`def test_` text inside strings count nothing. More than 60 net new functions
fail unless one of the PR's commit messages ends in a trailer block with
`Test-Budget: <reason>`. A missing commit, an unparsable file or unreadable
messages fail the step, as does a checkout that is not that pull request's
two-parent merge (or a `--pr-head` that is not one of its parents): the step
refuses rather than counting a different change. Cloud Build
(`scripts/cloud-ci.sh <pr-number>`) still counts from the merge base with the
PR's own base branch; batch and SHA runs skip it. Check a branch locally with

```bash
python3 -I scripts/test_budget.py count --base "$(git merge-base origin/main HEAD)"
```

That local merge-base matches the `gates` count only after you rebase or merge
current `origin/main`. To reproduce the CI parent on a merge commit:

```bash
python3 -I scripts/test_budget.py merge-parent --pr-head <pr-head-sha>
python3 -I scripts/test_budget.py count --base <that-parent> --head HEAD
```


This is a budget on syntactic test definitions, not on collected tests: it does
not bound collected-test growth (deleting uncollected `test*` helpers offsets
added tests, for example). The count does not interpret pytest collection. When
a changed path is a symlink in either version, or either version of a changed
`.py` file spells `__test__`, `pytest_pycollect_makeitem` or
`pytest_collect_file` anywhere in an identifier or string literal, the count is
unknown. That fails the same way and takes the same trailer once the real count
has been checked by hand. Removing a construct needs the trailer too: deleting
only a `__test__ = False` line leaves the syntactic count unchanged while
pytest starts collecting. Unchanged files are never read, so a construct that
once landed with a trailer does not affect later PRs.

The guard is aimed at accidental growth by honest PRs; constructs it cannot
count fail closed. Names built at runtime (`"__" + "test__"`) and other
deliberate evasion are out of scope; review and CI remain responsible for those.

### Local pytest attestation

Task #1355. Before opening a pull request, an agent can sign the pytest
outcome and test list as an in-toto Statement in a DSSE envelope:

```bash
export UA_TEST_ATTESTATION_KEY="$(cat /path/to/ed25519-private.pem)"
uv run python -m unified_agent.pytest_attestation sign -- tests/test_area.py -q
```

`sign` reads the key only from that variable: an Ed25519 PEM, or 32 raw
bytes in standard base64. A bare `export` with no value leaves it empty,
and `sign` then exits without writing a file.

The command writes `attestation/local-tests.intoto.json`. That directory
is gitignored, and the CI workflow does not upload or download the
envelope. The `gates` job runs `python -m unified_agent.pytest_attestation ci`
against that path in a fresh checkout. When the file is absent, the step
prints `no test attestation at attestation/local-tests.intoto.json;
verification skipped` and exits 0. The `UA_TEST_ATTESTATION_REQUIRED`
repository variable makes the same absence fail. A signature is checked
only when the envelope is already in the job's workspace. When it is,
CI verifies it with `UA_TEST_ATTESTATION_PUBLIC_KEY` and, when the
classifier asked for the full suite, re-runs a deterministic sample of
the attested passed tests (10 percent, at least one, at most twenty;
seed is the PR head SHA). A changed outcome or test list fails
verification. Skipped tests are recorded as `warnedTests` and do not fail
the result. Node ids must stay inside the workspace: absolute paths,
`..`, and shell-looking prefixes are refused.

The key stays in the environment. PEM values may use literal `\n` in place
of newlines. If both the private and public key env vars are set at sign
time, they must be a matching pair. `UA_TEST_ATTESTATION_REQUIRED=1` makes
a missing file fail; without it, CI prints `verification skipped` and
exits 0. `scripts/local-gate.sh` does not sign or verify; run the module
CLI separately.

```bash
uv run python -m unified_agent.pytest_attestation verify attestation/local-tests.intoto.json
uv run python -m unified_agent.pytest_attestation ci --seed "$HEAD_SHA"   # verify only
uv run python -m unified_agent.pytest_attestation ci --seed "$HEAD_SHA" --run  # verify and sample
```

Local Python parity for the `test` job:

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
make smoke
uv run unified-agent-audit . --fail-on high
```

See [cli-reference.md](cli-reference.md#unified-agent-audit) for audit exit-code
semantics. Protocol and client package commands are in those package READMEs.

### One-command local gate

`scripts/local-gate.sh` mirrors the `test` job (pytest) and the `gates` job
(everything else) above command-for-command —
`uv sync --all-extras`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`,
`make smoke`, `uv run unified-agent-audit . --fail-on high`, and both named
gates (`tests/test_attribution_coverage.py`,
`tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture`)
— plus one **local-only** leg kept separate from the CI-mirror legs so a
skip/fail on it never reads as a CI-parity gap: the staged
conflict-marker guard (`scripts/check_staged_conflict_markers.py`, task
#1016). It runs against the exact SHA/branch/dirty-state of your
worktree, printing one PASS/FAIL summary table. Non-zero exit if any leg
fails.

If the worktree is dirty, the gate does **not** attribute its result to the
HEAD commit — a dirty run necessarily also exercises uncommitted changes
that `git push` would not send, so certifying HEAD's SHA would be a false
attribution. Instead the summary and final verdict report against
`<sha>+dirty` (the working tree) and say so explicitly. Commit or stash
first if you need a result you can attribute to a specific SHA.

Pull requests run cheap gates; the merge queue and the nightly run the suite.
This script is a **pre-push gate**: run it before `git push` to catch what CI
would catch without spending CI minutes on it, and it doubles as a substitute
if CI is ever unavailable. It never changes what CI does.

```bash
./scripts/local-gate.sh                 # full gate: full pytest suite (~31 min on a 4-core desktop)
./scripts/local-gate.sh --fast          # skip the full pytest suite; every other leg still runs
./scripts/local-gate.sh --targeted "tests/test_foo.py -k 'failed login'" # shell-style quoting is preserved
./scripts/local-gate.sh --affected      # pytest on the import-graph selection for this branch
```

`--fast`/`--targeted`/`--affected` trade full test coverage for speed — useful
for a quick sanity check, but only the default (full) mode is equivalent to
what CI verifies. `--affected` uses `scripts/affected_tests.py` at depth
`transitive` (see below). When that script prints `ALL`, the pytest leg is
skipped with the reason — it does not run the full suite; CI does. The
security-audit leg intentionally treats `status: warn` (only medium/low/info
findings, `high=0`) as a PASS — this repo currently carries 2 pre-existing
medium findings (`INJECTION-BASE64` in two `docs/designs/*.md` files); only a
high-severity finding fails the gate.

### Which tests a change can affect

Task #2231. `scripts/affected_tests.py` prints the test files a change can
affect, from a static import graph of `src/unified_agent`, `tests` and
`scripts` (`src/unified_agent/affected_tests.py`). It parses each file with
`ast` and never imports the code it reads. Every selection is a targeted
subset, not a certification: batch CI still runs the full suite.

```bash
uv run python scripts/affected_tests.py --explain
uv run python scripts/affected_tests.py --files src/unified_agent/reviewer.py
uv run python scripts/affected_tests.py --depth direct --explain
git diff --name-status origin/main...HEAD | uv run python scripts/affected_tests.py --from-stdin
```

A file is selected when it is a changed test, when it is `tests/**/test_<stem>.py`
for a changed `src/` or `scripts/` module, when it imports a changed module
(or names it in a patch target, a process spawn, a pytest plugin, or a path
string), or when it is one of the cross-cutting inventory tests
(`affected_tests.ALWAYS_RUN`: route inventory, route scopes, the layer check,
the default-profile cap, the docs tool-count gate, the component-map check,
and the test-budget gate). `--explain` writes the
counts and any unread dynamic imports to stderr; stdout is one path per line,
empty (nothing to run), or the sentinel `ALL` (run the full suite; exit 0).

| `--depth` | What it follows | Who uses it |
|---|---|---|
| `transitive` (CLI default) | Module-level import chains; lazy imports one step out of the changed file | `scripts/affected_tests.py`, `local-gate.sh --affected` |
| `direct` | Tests that import the changed file through helpers, scripts and directory conftests, not through other src modules | The review pipeline (`reviewer._targeted_test_plan`) |
| `full` | Every lazy edge too | On demand; close to the whole suite for most src changes |

Constructs the graph cannot read fail toward selection on the test side (the
file is unresolved and runs on every Python change) and are reported, not
followed, on the src side. A test that only walks the source tree as text is
selected for any src change, but the review pipeline does not treat that as a
mapping: a src-only change that those scanners would cover still waits for
batch CI. Preview the review's set with `--depth direct`. Changing
`pyproject.toml`, `uv.lock` or `tests/conftest.py` prints `ALL`. So does any
other file nothing names, except markdown outside `src/`, `tests/` and
`packages/`. A documentation-only change prints the docs test scope from
`scripts/ci_changes.py` along with the inventory tests, including a page no
test names.

## Control Center SPA

The browser UI lives in `packages/web`. `npm run build` writes `dist/`; the
hub serves it at `/ui`. For live hub data while iterating, point the Vite
dev server at the hub and keep it on loopback (task #1579):

```bash
UA_DEV_HUB_URL=http://<hub-host>:8765 npm --prefix packages/web run dev
```

Without `UA_DEV_HUB_URL` nothing is proxied. `--host` is refused while the
proxy is on, because it would expose the hub session cookie. Details:
[web-ui.md](web-ui.md#developing-against-a-live-hub),
[packages/web/README.md](../packages/web/README.md).

## Adding a new table

A new table must declare its tenancy in `src/unified_agent/tenant_tables.py`:
`partitioned` (it carries an indexed `org_id`), `derived` (it inherits a
partitioned parent's tenant through a NOT NULL reference), `accelerator` (a
virtual FTS or vector index mirroring a parent by rowid) or `global` (no tenant
data at all). `tests/test_tenant_table_inventory.py` fails on a
`CREATE TABLE` that has no entry. When unsure, choose `partitioned`. The rule,
the kinds and the pending ratchets are in
[architecture/tenancy.md](architecture/tenancy.md) (task #1433).

## Adding a new tool

1. Implement it in the appropriate `tools/<group>.py` module (or a new module for a new
   group).
2. Route every filesystem/vault/audit path through the matching `safety.py` resolver
   (`resolve_in_workspace`, `resolve_in_vault`, `resolve_for_audit`) — never construct
   paths by hand. Route shell execution through `safety.vet_command`.
3. Raise `mcp.server.mcpserver.exceptions.ToolError` with an actionable message on any
   invalid input or safety rejection (not a bare `Exception` or `ValueError`) — this is
   what the host surfaces to the calling agent as a tool error rather than a crash.
4. Write the docstring as the tool's contract: parameter meaning, limits, and the shape
   of the return dict — hosts and this documentation both derive from it.
5. Register the function in the tuple in `server.py` (`for fn in (...)`).
6. Add tests in `tests/` (there's roughly one test file per module already).
7. If you added a module, place it in a layer in
   [`docs/architecture/layers.toml`](architecture/layers.toml); the layer check
   fails on an unplaced module or an import that points up the
   [layer map](architecture/layers.md).
8. **Restart the MCP server in the host** — tools are registered once at process start,
   so a running Claude Code / Antigravity session keeps serving the old tool list until
   it reconnects. In hub mode, restart the systemd service
   (`systemctl --user restart unified-agent`) and reconnect every connected host.

## Audit rule ownership boundaries

To avoid two rules double-reporting the same defect, each finding *family* has exactly
one owning check (see [security.md](security.md#security_audit-rule-catalog) for the
full table):

- **Python `eval`/`exec`/shell execution**: `check_py_ast` (AST-based) is authoritative
  for parseable `.py` files. It falls back to a regex scanner
  (`_py_llm_regex_scan`) only when `ast.parse` raises `SyntaxError`.
- **JS/TS `eval`/`new Function`**: `check_llm_code`, regex-based, gated on an LLM SDK
  import string appearing in the file. It deliberately skips Python (that's
  `check_py_ast`'s job) to avoid duplicate findings.
- **All hardcoded-secret patterns**: `check_secrets` — every regex in `SECRET_PATTERNS`
  runs there, regardless of assignment shape or language; `check_py_ast` explicitly does
  not re-implement a secrets check at the AST level for this reason (see the comment at
  the end of `check_py_ast` in `audit/rules.py`).

The engine only deduplicates **byte-identical** findings (same rule, path, line, and
message) — don't add a new rule that re-detects an existing rule's pattern under a
different message, since that will show up as a second finding rather than being merged.

## Worktree hygiene

`git_worktree_add`/`git_worktree_remove` (and the `git_merge_task` orchestration, which
calls both) create/remove checkouts under the workspace. After a task branch merges,
remove its worktree and delete the branch so stale full checkouts don't accumulate as
noise for `fs_search`/`security_audit`/`fs_index_symbols` (which all walk the workspace
tree):

```bash
git worktree remove .claude/worktrees/<name>
git branch -d <name>
```

`git_merge_task`/`unified-agent-merge` do this automatically on a successful merge
(`git_worktree_remove` + `git branch -d`, falling back to `-D` under `--force`); this is
only a manual concern when a task's worktree is abandoned without going through the
merge flow.
