# CI batch verification

**Depot CI is the automatic provider since 2026-11-12 (task #2533).**
GitHub-hosted Actions refused jobs over account billing that day, so the
automatic triggers moved to `.depot/workflows/`, and the `.github/workflows/`
copies are dispatch-only. See [Depot CI](#depot-ci-the-automatic-provider) for
how to dispatch, debug and size runs there. To switch back, swap the triggers
between the two copies and set `AUTOMATIC_PROVIDER` in
`tests/test_ci_workflow_parity.py`.

CI minutes cost money on either provider. Pull requests run the cheap gates
only. The merge queue runs the affected tests for a batch, and a nightly
schedule runs the full suite. A dispatch still runs everything.
`.depot/workflows/ci.yml` holds the rules, and
`tests/test_ci_workflow_parity.py` pins both copies together.

## When CI runs

| Event | What runs |
|---|---|
| Push to a pull request | The cheap gates: lint, smoke, security audit, changelog, test budget, local pytest attestation verify, and the [inventory tier](#inventory-tier). Not the pytest shards. |
| Pull request that only changes markdown outside `src/`, `tests/` and `packages/` | No run. Its doc-reading tests run in the batch or the nightly. |
| `merge_group`, or a Mergify pull request whose head ref starts with `mergify/merge-queue/` | Gates, then the affected pytest files from `scripts/ci_changes.py affected`, plus the JS and desktop jobs that classifier enables. `ALL` means the diff could not be narrowed, so that run uses the full shards. A batch that changes, renames or deletes Python under `src/` or `scripts/`, or whose selection reports a caveat, is always `ALL`: the import graph misses lazy hops and unreadable dynamic imports, so a subset cannot certify it. |
| Schedule, 08:00 UTC | The full suite. |
| `depot ci dispatch --repo anonb3ll/unified-agent --workflow ci.yml --ref <branch>` | Everything, same as the nightly. |
| Push to `main` | Nothing. Push is not a trigger. |

**Test budget on a pull request (task #2430).** The `gates` job counts net new
test functions from the parent of GitHub's `refs/pull/<n>/merge` commit that is
not the PR head (the *current* base tip), never `pull_request.base.sha`. That
SHA is the base tip at the PR's last synchronize, so counting from it credits
the PR with every commit the base gained since. `merge-parent --pr-head`
refuses a checkout that is not that two-parent merge. Batch and SHA dispatches
skip the count. See [development.md](development.md#test-budget-task-2233).

Docker and the install gate still run after a merge that changes their inputs,
and by hand (`depot ci dispatch --repo anonb3ll/unified-agent --workflow docker.yml --ref <ref>`).

Mergify queues a pull request that carries the `queue` label, is not a draft,
and is not labelled `held`. The queue batches up to five pull requests, waits
at most five minutes to fill a batch, and merges only after `CI / gates` and
`CI / full-verification` succeed on the batch.

A dispatched batch run also applies the held-despite-green rule (task #1260). The
`gates` job finds every open PR labelled `held` whose head the batch contains,
and fails the run if one of them adds no regression test. The steward can run
the same check locally, before dispatching:

```bash
python3 scripts/check_batch_held_prs.py --repo <owner>/<repo> --sha "$(git rev-parse batch/<name>)"
python3 scripts/check_batch_held_prs.py --repo <owner>/<repo> --pr 123 --pr 456
```

It reads the token from `GH_TOKEN` or `gh auth token`, and never prints it.

## Inventory tier

A pull request also runs the registry and inventory tests named in
`tests/pr_inventory_tier.txt` (task #2440). Each one freezes a central
registry that a change anywhere can break without touching what the test
imports: a table with no `tenant_tables.TABLE_TENANCY` class (#1479), an import
reference missing from `tools/export.py` (#1480), a module missing from
`docs/architecture/layers.toml` (#1471), an unscoped route or an unregistered
tool. Those misses used to fail only the merge batch. The batch and nightly
shards still run the same files; the tier step runs only on `pull_request`.
Once `scripts/check_components.py` exists (task #2439), the same step runs it.

To add a test, put its file or pytest node id on its own line in that list.
Time the file alone first (`uv run pytest <file> -q`); the whole tier has a
budget of about 90 seconds on a 4-core desktop, so `gates` grows by at most
3 minutes. Prefer node ids when only part of a slow file is an inventory, as
the list does for `test_route_scopes.py` and `test_profiles.py`. Never add a
test marked `embeddings`. `tests/test_ci_workflow_parity.py` checks that every
entry exists and that every file in `affected_tests.ALWAYS_RUN` is listed.

## Verifying a merge batch

1. Make a batch branch from current `main` and merge in each member PR's head:
   `git switch -c batch/<name> origin/main`, then
   `git merge --no-ff <pr-head-sha>` for each PR. Push the branch.
2. Dispatch CI on it:
   `depot ci dispatch --repo anonb3ll/unified-agent --workflow ci.yml --ref batch/<name>`.
   It prints the run id. Re-dispatching the same branch cancels the older run.
3. Wait for the run to go green at the batch head: `depot ci status <run-id>`,
   or the `CI / full-verification` check on the batch head commit
   (`gh api repos/<owner>/<repo>/commits/<sha>/check-runs`).
   `full-verification` is the single result to read.
4. Merge the members in batch order, each pinned with `--match-head-commit`.

A green batch run covers a member PR when both of these hold:

- the member's head commit is an ancestor of the batch head
  (`git merge-base --is-ancestor <pr-head> <batch-head>`), so the code CI tested
  includes exactly that PR's commits; and
- after the last member merges, `main`'s tree is the batch tree
  (`git rev-parse origin/main^{tree}` equals `git rev-parse <batch-head>^{tree}`).
  A squash or merge commit that produces the same tree ran the same code.

If `main` moves while the batch runs, or a member's head changes, the batch
head no longer matches. Rebuild the batch and dispatch again. A single PR that
needs the full suite on its own head before a batch, for example a risky
change, is dispatched the same way with `--ref <branch>`. The
historical `ci:full` label is not a workflow trigger; the reviewer's
`awaiting-ci` note still names it.

Ready means CI is green on that exact head, and the Approval Ledger has
approved that head. A CodeRabbit check is
not one of `UA_REVIEW_CI_REQUIRED_CHECKS` (the four `CI / test (shard=N)`
jobs) and does not pass or fail that green (task #1353).

## Check receipts: reuse, targeted or full (report mode)

`scripts/gate_route.py` says how a merge should be verified (task #2412). It
changes no gate and dispatches nothing.

```bash
uv run python scripts/gate_route.py route --repo <owner>/<repo> --head batch/<name>
```

| Route | When |
|---|---|
| `reuse` | The newest terminal full run on this exact git tree and environment passed, within 7 days (`--max-age-days`). The output names the run. |
| `targeted` | No such run, and `ci_changes.affected_selection` can certify the diff with a subset. Python under `src/` or `scripts/` never narrows. |
| `full` | Everything else, including an unknown tree, environment or selection, and a newest full run that failed. |

A receipt only proves what its fingerprint covers. A run counts as full only
on `workflow_dispatch` or `schedule` with `full-verification` and all four
shards green, so a pull request's gates and a merge queue's subset never
qualify. A cancelled or skipped run is evidence of nothing. The CI environment
is the runner label (`UA_CI_RUNNER`, else `ubuntu-latest`); everything else CI
installs is in the tree. A run whose head commit is not in the clone cannot be
matched, so fetch the branch first.

`reuse` does not replace the rule above: after the last member lands,
`origin/main^{tree}` must still equal the tree the run tested.

`local-gate.sh` records one receipt per leg on a clean tree, in
`ua-check-receipts.jsonl` under the git common directory; `--env local` routes
from those.

## The review gate without per-PR CI

The review cron (`reviewer.py`) trusts exact-head CI shards when they exist,
which means a full dispatch on that PR's head. A pull request's own run is
the cheap gates, so it does not satisfy that check. Otherwise it runs the `direct`
tier of the static import-graph selector (`affected_tests.select`, task #2231;
`UA_REVIEW_LOCAL_PYTEST=targeted`, the default). That is more than the files
the diff names: a test that imports a changed module through helpers still
runs. It is less than a full transitive walk: a test that reaches the change
only through another src module does not. Source-tree scanners (`rglob("*.py")`
/ `os.walk`) and tests whose imports cannot be read statically do not count as
a mapping, so a src-only change that those would cover still waits for batch
CI. When nothing maps, the diff needs the full suite, or more than
`UA_REVIEW_TARGETED_MAX_FILES` (40) files map, it holds the task as
`awaiting-ci` instead of running the whole suite on the hub. A task with no PR
never reaches a batch, so it keeps the old whole-suite run. A `+yolo` merge
reuses the review's scope for the same head instead of re-running the whole
suite. Preview a selection with `uv run python scripts/affected_tests.py
--explain --depth direct`; the CLI default is `transitive` (what
`local-gate.sh --affected` runs), which is broader than review. See
[development.md](development.md#which-tests-a-change-can-affect).

**A batch run does not clear `awaiting-ci`.** The reviewer reads checks on the
PR's own head, and a batch run reports on the batch branch's head. So an
`awaiting-ci` PR stays held until one of these happens:

- the steward verifies it in a batch and merges it. The batch run is the
  merge evidence: a green `full-verification` at the batch head, the
  ancestor and tree checks above, and the held-PR check. The steward records
  them in its review record. After the merge, reconcile closes the task as
  `merged-upstream`. The `awaiting-ci` note is not a merge block; it says the
  hub ran no tests;
- someone dispatches the full workflow on the pull request's head. The next
  review scan sees green exact-head shards and validates it.

## Depot CI (the automatic provider)

Depot CI runs the GitHub Actions workflow syntax from `.depot/workflows/` in
Depot sandboxes and posts the same `CI / <job>` check runs on the commit
(GitHub app `depot-code-access`), so the review gate, Mergify and
`UA_REVIEW_CI_REQUIRED_CHECKS` read it unchanged. Install the CLI and log in
once per machine (`depot login`); the desktop and the Mac hub both have it.

| Need | Command |
|---|---|
| Run CI on a branch or batch | `depot ci dispatch --repo anonb3ll/unified-agent --workflow ci.yml --ref <branch>` |
| Watch a run | `depot ci status <run-id>` |
| Read a job's log | `depot ci logs <attempt-id>` (the id `status` prints) |
| Why did it fail | `depot ci diagnose --run <run-id>` |
| Retry only the failed jobs | `depot ci retry <run-id> --failed` (or `--job <job-id>`) |
| Shell into a running job | `depot ci ssh <run-id> --job ci.yml:<job>` |
| CPU and memory per job | `depot ci metrics --job <job-id>` |
| Run your uncommitted tree | `depot ci run` (patches local changes in after checkout; `--job` picks jobs, `--ssh-after-step` stops for a shell) |
| CI variables and secrets | `depot ci vars`, `depot ci secrets` |

**Sandbox sizes.** Plan minutes are counted at 2 vCPU, so an N-core sandbox
bills N/2 per minute. The jobs that scale with cores get them, and the rest
stay small (`test_depot_sizes_each_ci_sandbox_to_its_job` pins this):

| Job | Sandbox | Why |
|---|---|---|
| `test (shard=N)` | `depot-ubuntu-24.04-8` | pytest-xdist, one worker per core; at 2 vCPU a shard sat at 98% CPU |
| `web` | `depot-ubuntu-24.04-16` | Playwright runs half the cores as workers; the a11y gate took ~53 min on 2 cores |
| `desktop-core` | `depot-ubuntu-24.04-8` | parallel `cargo` builds |
| `sdk` | `depot-ubuntu-24.04-4` | three npm builds and a Python codegen check |
| `gates`, `js`, `full-verification` | `depot-ubuntu-latest` (2 vCPU) | short or I/O-bound |

**Cache.** Depot Cache backs `actions/cache`, `setup-uv` and `setup-node`
caching with no extra configuration. Images build with
`depot/build-push-action` on Depot's builders, with their layer cache.
`depot/tests-run-action` balances the four pytest shards by recorded test
timings.

## CI on GCP Agent20 Build (`scripts/cloud-ci.sh`)

> **The route when a provider will not run jobs.** Depot CI is the automatic
> provider (task #2533), but either provider can stop running jobs: GitHub
> Actions refuses them on an account billing failure, and Depot stops at its CI
> minute cap or under rate limiting. When either happens, dispatch through this
> script instead of waiting for that provider to recover; `UA_CI_BACKEND=spot`
> (the default) is the cheap path. This is a fallback for that day, not a move
> away from Depot.

Task #2115. GitHub-hosted jobs can be refused on account billing (2026-10-16,
and again on 2026-11-13) and Depot carries a CI minute cap, so CI can run on
Cloud Build in project `ua-worker`:

```bash
UA_CI_GCP_PROJECT=ua-worker scripts/cloud-ci.sh <pr-number | branch | sha>
```

The project must be explicit to prevent billing an unintended project. The
GitHub repository is inferred from the checkout; `UA_CI_REPO` overrides it.

- **What it runs:** the script submits the exact head and comparison base as
  shallow Git snapshots, with `deploy/cloudbuild/ci.yaml` from your checkout.
  The build checks the source SHA and classifies the changed paths itself.
  - The build runs the `gates` job's checks, the four balanced pytest shards
    (`ci_changes.py candidates --scope full`) and, when the canonical classifier
    requires them, JS and desktop-core (including browser accessibility and Rust).
  - Steps run in parallel on one `E2_HIGHCPU_8` builder. Each step runs under
    `docker run --privileged`, so bubblewrap can create its user namespace.
  - Root installs the packages; the checks run as uid 1000, as on a GitHub
    runner.
- **Reporting:** it posts commit statuses with the contexts the review gate
  reads through `statusCheckRollup`: `CI / gates`, `CI / test (shard=0..3)`,
  `CI / js` and `CI / full-verification`.
  - Each status links to the build's log.
  - Statuses are posted from your machine with your `gh` login; no GitHub
    credential enters the build.
  - `UA_CI_NO_STATUS=1` suppresses GitHub writes; it still submits a billed build.
  - The watcher captures its shell program before submission. Editing the
    checkout's runner later cannot change that running program.
  - A status read that fails or exceeds its 30-second limit is retried until
    the queue or run deadline. One failed read does not settle statuses.
    On that deadline, or on catchable `INT`/`TERM`/`HUP`, it reads the existing
    build once and attempts every required status. A terminal result retains
    the actual step outcomes; an unreadable or unfinished build reports
    `error`, with its build link. Each recovery command has a 30-second limit;
    status publication retries once. The watcher still exits nonzero.
    Recovery never submits or cancels a build. Inspect the linked build before
    deciding whether another run is needed. `SIGKILL`, host loss, a lost submit
    response without a build ID, or a persistent GitHub outage still require
    manual reconciliation; local exit is not proof that the build stopped.
  - **Re-attach (task #2205):** when the watcher left `error` on a build that
    was still running, it prints the exact command to settle them later:
    `scripts/cloud-ci.sh --reconcile <build-id> <pr-number | branch | sha>`.
    It submits nothing. It refuses unless the build's `_SOURCE_SHA` is the
    resolved head. It settles the base contexts from that build's own step
    results (a missing step reads as failure), plus each optional context
    (`CI / js`, `CI / desktop-core`) the submitting run's classifier enabled.
    A fresh run records that on the build as the tags `ua-ci-js-<0|1>` and
    `ua-ci-native-<0|1>` (added to a copy of `ci.yaml` at submit), because a
    skipped leg's step still runs and passes and so proves nothing. A skipped
    leg gets no status, as in the fresh run. For a build submitted before
    those tags, an optional context is settled only when a status names the
    build for it; a malformed tag refuses. An
    unfinished build is awaited with the same queue and run deadlines as a
    fresh run; they are upper bounds, counted from the re-attach. Statuses are
    last-write-wins per context, so it refuses, before waiting and again
    before posting, when any status in the history of a context it would
    settle is from a run it cannot prove older. Which run is newer is decided
    by build `createTime`, never by the order statuses were posted: an older
    build's watcher can finish late, and the newer build may then overwrite
    it. A status from a tool, one with an empty description, or one from a
    build whose creation time cannot be read counts as newer. Contexts are
    matched as GitHub matches them, without case, and the runner sets
    `LC_ALL=C` so the caller's locale cannot change that matching. A fresh
    run applies the same rule to every other run's status posted after its
    pre-submit baseline, so it can publish over such a status, but a later
    `--reconcile` on that SHA still refuses on it: reconcile reads the whole
    history. It also repairs statuses after `SIGKILL` or
    host loss, even when no status names the build. A fresh run that cannot
    read the statuses just before its first (pending) write posts nothing,
    exits nonzero and prints this command for its build (task #2211).
- A final aggregate step fails the build if any required check fails or its
  result is missing. `allowFailure` on diagnostic steps keeps the other shards
  running; it does not certify their results. Each job has private source and
  Git metadata; the synchronized Python environment is shared read-only.
  Each container uses `--init` so
  orphaned descendants are reaped during process-supervision tests.
- **Cost:** Cloud Build bills about $0.016 per build-minute on
  `E2_HIGHCPU_8`. October 2026 used about 4,200 minutes (about $68), so runs
  now default to the Spot VM backend below. Larger machine types are not
  available on this project's Cloud Build quota.

### Spot VM backend (default, task #2252)

`UA_CI_BACKEND` picks where a fresh run executes: `spot` (the default) or
`cloudbuild`. Both run the same `deploy/cloudbuild/ci.yaml` and post the same
statuses, so everything above still holds. A run id starting `spot-` is read
from the VM backend and any other id from Cloud Build, so supersession and
`--reconcile` work across runs from both.

- `scripts/spot_ci.py` uploads the directory Cloud Build would receive to
  `gs://<project>-spot-ci/runs/<id>/`, queues it, refreshes the VM's startup
  script and executor from your checkout, and starts the VM.
- VM `ua-ci-runner` is a Spot `e2-highcpu-8` (the Cloud Build machine: 8 vCPU,
  8 GB). `deploy/spot-ci/startup.sh` drains the queue in submission order,
  then powers the VM off. A stopped VM bills only its 30 GB disk. GCE also
  stops it 3 hours after it starts, whatever the script is doing, so the script
  starts a run only in the first 15 minutes after boot. Spot reserves 160 minutes
  for the executor and five minutes for cleanup. The executor begins to stop
  its steps two minutes before the hard limit so it can publish a result.
  The Cloud Build backend retains the timeout in `ci.yaml`. Outside the
  start window the VM powers off with the run still queued and
  the next status poll restarts the VM with a fresh window (task #2613). A queue
  therefore needs a live watcher (cloud-ci.sh or --reconcile) to advance.
- `deploy/spot-ci/executor.py` runs `ci.yaml` the way Cloud Build does. Each
  step runs as `docker run <name> <args>` with `/workspace` and the Docker
  socket mounted. It applies the same substitutions, `waitFor`,
  `allowFailure` and `timeout` rules. It writes `state.json`, shaped like
  `gcloud builds describe`, and `log.txt` (the status link).
- A run cut short by preemption is retried once when the VM next boots, then
  reported as `INTERNAL_ERROR`. While a run is unfinished, each status poll
  starts the VM if it has stopped. A Spot capacity stockout leaves the run
  `QUEUED` until the queue deadline.
- Setup, once per project: `UA_CI_GCP_PROJECT=ua-worker
  deploy/spot-ci/provision.sh`. The VM has no external IP, so the project
  needs a Cloud NAT in the VM's region first; the script checks for one and
  exits before creating anything if there is none (it never creates the NAT).
  It creates the bucket (objects deleted after 30 days), a service account
  limited to that bucket, and the stopped VM. It is idempotent.
- Trust: as on Cloud Build, steps run the commit under test in privileged
  containers. They can reach the VM's service account, which can write only
  the CI bucket. Run only this repository's own heads.
- Cost: about $0.07 per VM-hour while a run executes, plus about $3 a month
  for the disk.

This differs from `deploy/ci-runner` (below), which needs GitHub Actions to
accept jobs.

## Blacksmith runners are not available to this repository

Same-repository jobs in `.github/workflows/ci.yml` run on `vars.UA_CI_RUNNER`
when that variable is set, and on GitHub-hosted `ubuntu-latest` when it is
unset. A pull request from a fork stays on `ubuntu-latest`.

Blacksmith cannot serve this repo, and not for a fixable reason: its runners
arrive through GitHub **runner groups, which are organisation-scoped**, and
`anonb3ll/unified-agent` is owned by a **user** account. `gh api
repos/…/actions/runners` reports `total_count: 0`, and a job labelled
`blacksmith-4vcpu-ubuntu-2404` has never started — it stays `queued` with
`runner=none`, which reads as a pending check rather than a broken one
(task #2291).

So the two ways to get CI executing again were the operator's, and neither is
a workflow change (clearing the failed payment restores Actions when billing is
the cause; until then the Cloud Build route above runs the same jobs):

1. Clear the failed GitHub Actions payment so Actions can schedule jobs.
2. Point `UA_CI_RUNNER` at a self-hosted runner that actually exists — see
   the section below.

## Moving CI to a self-hosted runner

Set `UA_CI_RUNNER` to the self-hosted runner's label. That replaces the
Blacksmith fallback. `docker.yml` and `install-gate.yml` follow
`UA_CI_ISOLATED_RUNNER` instead and stay GitHub-hosted unless it names a
dedicated machine: they need root-equivalent Docker access or a cold machine.

```bash
gh variable set UA_CI_RUNNER --body ua-ci     # the label deploy/ci-runner registers
gh variable delete UA_CI_RUNNER               # back to blacksmith-4vcpu-ubuntu-2404
```

`deploy/ci-runner/README.md` sets up the runner on a GCP spot VM. Read its
trust model first: credentials stay root-only and each slot is an
unprivileged user, but the VM is not a trust boundary. Register at least as
many slots as the jobs that run in parallel (gates, then four shards, the JS
job and desktop core), or jobs queue behind each other.

## Finding the pull request that broke a batch

`scripts/bisect_batch.py` binary-searches an ordered batch. The runner is any
command that exits 0 when the prefix in `BISECT_BATCH_PRS` (comma-separated)
passes:

```bash
python3 scripts/bisect_batch.py 101 102 103 104 -- ./scripts/run_prefix.sh
```

## pytest-xdist and sampled matrices

Each Depot shard runs one pytest-xdist worker per sandbox core
(`--dist loadfile`, default `auto`, task #2533); the dispatch-only GitHub copy
runs two on its 2-vCPU runner (task #2230). The dispatch input
`pytest_workers` or the CI variable `UA_CI_PYTEST_WORKERS` (`depot ci vars`
on Depot, `gh variable` on GitHub) changes the count; `0` runs serially. Cloud Build runs
its four shards as containers on one 8-vCPU, 8 GB machine, so each runs
serially: two workers per shard there ran the machine out of memory. The shard
containers receive no build environment, so the count there is the default in
`deploy/cloudbuild/ci-step.sh`.

Parametrize matrices in the redaction, JWK, secret and audit test files
(`SAMPLED_FILES` in `tests/param_sampling.py`) with more than 8 cases run a
representative sample by default: every value of every dimension still occurs
at least once, plus a stable slice of about one case in 100 of the product
(task #2261). The six largest redaction and JWK matrix files drop from about
50,700 cases to about 3,200 that way, and the whole suite from about 71,800 to
about 23,400. `--param-sample=pairwise` keeps the earlier pairwise-covering
tier (every pair of values of every two dimensions, task #2230). The
classifier's
`exhaustive` rule (`scripts/ci_changes.py`) runs every case when a change
touches the redaction, JWK or audit scanners, those test files, the sampler,
`tests/conftest.py`, the CI decision and shard files, `pyproject.toml` or
`uv.lock`, or when the change set is
unknown. Locally, `--exhaustive` or `UA_TEST_EXHAUSTIVE=1` runs every case, and
`@pytest.mark.exhaustive` keeps one matrix unsampled. See
[development.md](development.md#sampled-parametrize-matrices).
