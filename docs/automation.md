# Development-pipeline automation

How a dev agent's finished work reaches `main` without the operator driving every step
(shared task 65; validate+hold contract from task 78). The review *logic* lives in
`src/unified_agent/reviewer.py` and is exposed as the MCP tools
`scan_for_reviewable_tasks` / `review_and_notify`; this document covers the
**deterministic driver layer** that runs that logic on a schedule — no LLM anywhere in
the loop.

## Policy: validate + hold

Decided 2026-07-16: **the automated pipeline never merges or pushes to remote
`main`.** It validates the gates (security audit + pytest + red-tier diff
classification), records the verdict on the task, and notifies — it is never the
merge actor. `tests/test_reviewer_pipeline.py` asserts the pipeline sources contain no
remote-write invocation (no `git`-push, no `gh`-PR-merge).

**Who merges.** For `unified_agent` delivery to the Mac hub, since 2026-10-01 the
**designated local deploy steward** merges and deploys through the recorded gate in the
cockpit's `docs/runbooks/DEPLOY-STEWARD.md`; the claiming author owns the implementation,
local checks, the PR and its review fixes. The rest of this section is the 2026-09-23
ruling, which still governs other repos. **2026-09-23 (the operator):** the 2026-07-16
corollary *"a human performs every merge"* is withdrawn; *"the pipeline never merges"*
stands. The **claiming agent merges its own PR** once the head is ready: CI is green on that head, and the Approval Ledger has approved that head, with every review thread resolved — e.g. `gh pr merge
--squash --match-head-commit <sha>`. The [origin-branch hold](#the-pipeline) also
writes `validated`, but with `gates not run (no local checkout)`; that note is a hold and
does not count as the independent review. A pipeline `validated` verdict confirms its
configured gates ran; the current pipeline does not review PR logic, so its verdict alone
does not satisfy the independent-review requirement. Sources: the cockpit's
`docs/company/CHARTER.md` (Decision rights: *"Implementation, merging on green CI + one
independent review, deploying"* → the claiming agent), `docs/company/OPERATING-SYSTEM.md`
step 5, `conductor/workflow.md` (Code Review), and the 2026-09-23 amendment to Rule 5 in
[COLLABORATION.md](COLLABORATION.md#rule-5--automated-review-flow). The ruling does
**not** cover:

- **the pipeline or any scheduled job merging** — `reviewer.py` / `review_and_notify`
  stay validate + hold;
- **one agent merging another agent's PR** — confirmed only for the claimant on its own
  PR; widening it is the operator's call;
- **red-tier diffs** ([Red tier](#the-pipeline) below) — still escalated to the operator;
- **Approval Ledger approval rows** (`github_app.process_pr_event`, see
  [connector-activation.md](connector-activation.md)) — resolved only by the named human.
  An agent resolving one would be recorded as human attestation (task #1217). A
  configured policy authorizer may instead record a *policy authorization* for an
  exact head (task #1407); the ledger then reports that merge as policy-authorized,
  a separate evidence class that never resolves the approval row, never overrides a
  denial and never covers red-tier files.

### Shipping preflight: human-attested independent review (#2158)

The opt-in shipping preflight (`python -m unified_agent.shipping_preflight_cli`,
result scope `exact-head-human-attested-review`) does not treat "green CI plus a
review comment" as enough. The **existing** human release decision on that
exact repo, PR and 40-character head must carry version-1
`independent_review` metadata:

| Required | Constraint |
|---|---|
| `statement` | Must be the exact `ATTESTATION_STATEMENT` from [`independent_review.py`](../src/unified_agent/independent_review.py) — not a paraphrase |
| `authors` | Every contributing execution/model (1–32), each with `label`, `subject`, `provider`, `model`, `execution_ref` |
| `reviewer` | One actual model; subject **and** canonical model must differ from every author |
| `verdict` | `approve` for this exact head |
| `review_ref` | A substantive review reference the human inspected |

These are **human-attested claims**, not collected telemetry. Self-review,
same-model review, unresolved aliases (`auto` / `latest` / `unknown`),
missing fields, or a statement that does not match the versioned text
**hold**. Mixed-model review and human-only review are unsupported in v1.
The Control Center release drawer shows the full statement and evidence
before **Approve release and attest review**.

Steward, held-PR and release gates stay separate. Preflight does not
merge, consume a pass, or block an out-of-band GitHub merge. It does hold
the steward's own merge unless the approved commit equals the live head and
every required CI check (the test shards, `CI / gates` and
`CI / full-verification`) is SUCCESS on that head. A failing, skipped, pending
or missing required check does not clear it. Queue the
bounded proposal on the existing MCP request path
(`coordination_manage` `approval_ledger_rerequest` with
`independent_review`); see [CLI reference](cli-reference.md) for the payload
shape.

### Shipping preflight: head-moved red-tier hold (#2259)

A Ledger approval bound to the live head still cannot tell "this tree was
read" from "this tree was approved". `review_reads` stores one row per
`(repo, number, head_sha, kind, reader)` for a steward read, a policy verdict, a
human's own read, or a delta read that names the head it moved from. The key
carries the reader (#2641): a second reader at a head is a second row, so an
author's later read never replaces an independent one, while one reader
re-recording a head still updates its own row. The preflight result includes
`review_head` with the verdict `review_head_gate` used, and `readers`, every
reader recorded at the head that verdict counts.

The rule (operator decision 2026-10-31): a commit that lands after a
recorded read **passes** when the delta touches nothing red-tier, and
**holds** when it does and no delta read naming those files was recorded.
An unread PR is not refused for being unread — the Ledger approval and its
independent review, both bound to this exact head, remain the authority.
Two writers record rows (#2294). An Approval Ledger approval is not one: it
answers whether a human authorized the head, not whether anyone read it, and
PR #1327 had an approval at its final head over an unread red-tier delta.

| Kind | Writer | When |
|---|---|---|
| `policy` | review pipeline (`reviewer.py`), as `review-cron` | A `validated` verdict, only when the PR's live head is the validated head; otherwise the result carries `review_read.skipped` (`head-moved` or `head-lookup-failed`) |
| `steward-read` / `human` / `delta-read` | `unified-agent-review record-read` | Explicitly, after the read, on the hub or from a hub client; `human` needs a human `--reader` and `--evidence-url` |

The pipeline writer is best-effort after its verdict has committed, so a
failed write is logged and surfaced but never loses the verdict.

A delta read counts only at its own head, and only when it passes there: a
full read (`steward-read`, `policy` or `human`) is recorded at its `from_sha`
for the same PR, `from_sha` is an ancestor, the compare is not truncated, and
its files cover every red-tier path. `record-read` checks all of that against
GitHub's compare before it stores the row; the gate checks it again and
ignores a row written around the CLI. A delta read is never the base for a
later push (`delta-clean` walks full reads only), so after a delta read, a
further push needs a delta read from the last full read again.

The verdict decides, not only the existence of a read (#2420). A read passes
its head only when its verdict opens with `pass`, `passed`, `ship` or
`validated`, optionally after `code`, `delta` or `full`, and asks no question:
`pass`, `pass-after-delta`, `SHIP: ...`, `Code PASS; CI pending`, `delta pass:
...`. Any other verdict (`fix-first: ...`, `hold`, `fail`, `pass?`, or a
sentence that names no verdict) is kept as written, because a rejection is
evidence, and it holds three things: the head it was recorded at, whichever
other kind passed that head (a pipeline `validated` cannot outvote a
steward's `fix-first`); any delta read or `delta-clean` counted from that
head; and every passing read recorded before it, on any route. Only a read
recorded after the rejection passes the PR again; re-recording the same kind
at the same head replaces the row. The preflight refuses, and
`review_head_check` answers `read-rejected` / `failure` with the verdict in
the summary.

A head that a rebase put off every recorded read still fails closed. Since
#2398 the `unified-agent/review-head` check also says why the head moved
(`review_provenance`, report mode: the conclusion does not change). It
compares the PR's own patch at the read head with its patch at the live head,
each from GitHub's three-dot compare against `main`, and reports
`head_change_cause`: `rebase-identical` (same normalized per-file patches),
`rebase-changed` (with `delta_files`, the files whose patch differs),
`new-commits`, or `unknown` (a truncated or patch-less compare, or GitHub
unavailable). `next_gate_owner` names the last reader when a re-read is due,
and the deploy steward otherwise. The answer is recorded once per pair of heads
on `agent.review.head_change`, so how often a rebase would have been bridged
can be measured before any gate trusts it.

```bash
# After a full-diff read of the live head:
unified-agent-review record-read --repo owner/name --pr 123 --head <sha> --reader <label>
# After reading only what changed since that read:
unified-agent-review record-read --repo owner/name --pr 123 --head <new-sha> \
  --kind delta-read --from <sha> --files deploy/x.sh --evidence-url <url> --reader <label>
```

On a hub client (`NO_LOCAL_MEMORY_DB`, or with `--hub`) the same command posts
to `POST /api/review/reads`, and needs `UA_HTTP_HUB_URL` or `--hub`. The hub
applies the same refusals and returns the row id. The reader is the verified
credential: `--reader` must match it, a body cannot name another, and only
humans and `UA_REVIEW_READERS` (empty by default) are admitted.

Three ways a recorded history can pass, in order of strength:

| `review_head.status` | Meaning |
|---|---|
| `read-at-head` | A steward, policy, or human read is recorded for exactly this head |
| `delta-read` | A delta read at this head names every red-tier path the delta added |
| `delta-clean` | The newest recorded full read is an ancestor of this head, and the delta since then is not red-tier |
| `no-read-recorded` | No rows for this PR; the gate does not invent a hold |

The preflight lets `no-read-recorded` through. The required check does not:
see [the `unified-agent/review-head` check](#the-unified-agentreview-head-check-2320).

A rebase that leaves every recorded read behind fails closed: no delta can
be proven against a base that is no longer an ancestor. GitHub's compare
endpoint is the authority (the hub has no checkout); a truncated file list
(300 entries) cannot prove a delta clean, so that answer is refused rather
than read as "nothing red-tier changed". An "ahead" compare with no `files`
array is the same refusal. Canonical red-tier paths come from
[`cycle_eligibility.RED_TIER_PATTERNS`](../src/unified_agent/cycle_eligibility.py).

The retained human gates are listed under [What stays human](#what-stays-human).

The one exception is the *local*-merge flow for tasks already marked `done`
(`git_merge_task` on non-PR worktree tasks, merging inside the workspace repo). That
flow is unchanged and still never touches the remote.

Since task #1026 a project may opt in to a second local exception: with `yolo` on
in its delivery policy (see `UA_PROJECT_DELIVERY_FILE` in
[Configuration](configuration.md), or a `[delivery: mode=... yolo=on]` note on the
task), green non-red-tier work is merged into the local workspace repo by the
review pipeline instead of being held. Because review runs *before* a task is
closed, that merge goes through `git_merge_task(allow_in_flight=True)`, which
relaxes the `status == 'done'` precondition and nothing else — the clean-worktree
check, the security audit and the test suite still gate it, and a red-tier diff
still requires a human. Absent config, every project resolves to `no-mistakes` /
`yolo=false`, i.e. validate + hold with the merge left to the claiming agent. Covered end to end (real git, real
merge) by `tests/test_reviewer_yolo_merge.py`.

### The `unified-agent/review-head` check (#2320)

The Approval Ledger poll publishes `unified-agent/review-head` at every open
PR head on the connector's base, and recomputes it on every poll. It runs in
the poll, not in CI, because `.github/workflows/ci.yml` filters out
markdown-only PRs and this check has to exist on those too.

A recorded read publishes its own head as well (task #2410,
`github_app_worker.publish_review_head`). The poll sees open pull requests
only, so a read recorded after the merge would otherwise leave a permanent
false `error` on the merged head: the head had left the set, and nothing would
recompute it.
`POST /api/review/reads` and `record-read` on the hub publish the head once the
row commits and report the outcome as `review_head` in the answer. `record-read`
in a hub shell reads `$UA_HOME/service.env` for that one call, under whatever
the shell already sets, so it publishes as the hub does (task #2418). That publish
is best-effort — a failure is reported there and never discards the read.
It answers as the poll does, the worst across every open pull request on that
head, and it stays serialized with the poll: behind a poll that holds the lock
past five seconds it reports `deferred` and publishes nothing. For 24 hours the
poll retries any closed pull request whose newest read has not reached GitHub
as `success`, which covers a deferred or failed publish. Any other real answer
given for that pull request after its newest read settles the head until the
next read (task #2417); an answer that came from an open sibling on the same
head does not.

`shipping_preflight_cli.review_head_check` is the strict entry point over
`review_head_gate`, so there is one rule and two readings of it:

| Gate answer | Check conclusion | Commit-status state (user-token mode) |
|---|---|---|
| `read-at-head`, `delta-read`, `delta-clean` | `success` | `success` |
| `no-read-recorded` | `action_required` | `error` |
| Refused: a rebase off every read, an uncovered red-tier delta, a delta too large to prove | `failure` | `failure` |

A head that moved shows `failure` until a covering read is recorded with
`unified-agent-review record-read`, then `success` — in the same request when
that read publishes the head, or at the next poll for an open PR. A read
recorded since the last poll makes the next launch of the job due, so it does
not wait out the idle interval.

Each launch of the job is a fresh process
(`run-pr-watch.sh`), and a launch with
nothing due exits early ([`plan_tick`](../src/unified_agent/pr_watch.py)). A
release that changes this check therefore takes effect at the first poll that
starts on it, up to one idle interval after the deploy. A poll already running
during the deploy finishes on the old code.

There is one check per head, updated in place. A poll whose answer matches what
GitHub last accepted makes no GitHub write; each accepted post is recorded on
`connector.github.review_head_posted`. In App mode a run is created only after
listing proves none on that head carries this head's `external_id`, so a POST
whose response was lost is adopted on the next poll. Two open PRs on one head
share one check, which shows the worse answer.

When GitHub cannot answer a compare, nothing is posted for that head and the
poll returns an `error` result: no check keeps a required check unmet, and the
next launch retries. A failed post is the same `error`, and neither stops the
ledger work, which runs first.

The check is advisory until branch protection requires it. It is not CI: the
review pipeline's required shards and the held-PR rule ignore it, so a PR nobody
has read still counts as CI green. `UA_REVIEW_HEAD_CHECK=0` stops new posts.
What it needs from the host is in
[connector-activation.md](connector-activation.md).

## The pipeline

```
dev agent                        automation (cron, no LLM)              claimant / the operator
─────────                        ─────────────────────────              ──────────────────
task_add / claim task
work in a git worktree
  (branch matches task id)
annotate task notes with
  [waiting-for: review]
        │
        └──────────────►  unified-agent-review scan  (every 15 min)
                            0. queue hygiene (task #729): stale claims,
                               aging open demote, auto-archive done
                            1. reconcile: already-merged PRs → done
                               (review-marked rows, plus markerless
                               doing/waiting_review when the PR is
                               MERGED — UA #851 / #868)             ──► ✅ once
                            for each candidate (status doing/blocked/
                            waiting_review; live review marker only):
                              2. already reviewed at this branch-head
                                 sha? → 'unchanged' (silent no-op)
                              3. validation gates in the worktree:
                                 security audit + pytest (skipped when
                                 exact-head CI shards are green; otherwise
                                 only the diff's test files, task #1787)
                              4. red-tier file classification
                              5. verdicts:
                                 green + not red  → 'validated', HOLD  ──► ✅ merge when the head is ready:
                                                                            Devin clean on that head,
                                                                            CI green, Ledger approved
                                                                            (unified_agent: the
                                                                            deploy steward)
                                 red-tier         → annotate, hold     ──► 🔴 iMessage the operator: approve?
                                 failing          → annotate, hold     ──► ❌ iMessage: fix needed
                                 pytest timeout   → annotate, retry    ──► ⏱️ iMessage: inconclusive
                                                                            (first timeout at a
                                                                             sha only — see below)
                                 no CI, no mapped → annotate, hold     ──► ⏳ awaiting batch CI
                                 tests (PR task)                            (once per sha)
                                   (each verdict noted + notified ONCE per sha,
                                    except 'timeout' — at most
                                    UA_REVIEW_TIMEOUT_MAX_RETRIES gate runs
                                    per sha in total, see below)
```

Task-note annotations are the state machine (written by `reviewer.py` unless noted):

| Annotation | Meaning |
|---|---|
| `[waiting-for: review]` | live request: the branch is ready; picked up by reconcile + scan. A backtick-quoted `` `[waiting-for: review]` `` is prose, not a live marker (`monitor.py` / related scanners use `(?<!`)…(?!`)`). |
| `[waiting-for: review] when done` | **not** a live request — boundary template on an unclaimed `open` spec (task #709). Scan and reconcile skip it. |
| `[waiting-for: review] once shipped` | **still a live request for scan/reconcile.** Compact `task_list(fields="compact")` treats it as not-waiting (same qualifier list as `when done`); the pipeline does not. Use `when done` on spec templates. |
| `[waiting-for: human-merge]` | legacy interim label written by the dev (the name predates the 2026-09-23 ruling): work is validated or PR'd and awaiting its merge — by the merge actor (the deploy steward for `unified_agent`, otherwise the claiming agent), with the operator's go for red-tier work. Watched by the reconcile step only |
| `[review: validated @ ts] gates green at <sha>; ready for human merge — <PR url or branch>` | gates green — held; the pipeline never merges. The note text still says "human merge" (`reviewer.py` wording predates the 2026-09-23 ruling); the merge actor (the deploy steward for `unified_agent`, otherwise the claiming agent) merges once exact-head CI is green and review threads are resolved |
| `[review: held-no-checkout @ ts] no local worktree on this host; branch <name> (<source>) found on origin (ls-remote) — …` | no checkout on the reviewing host, but the task's branch is on origin (task #2321); held for the merge decision with **no gates run**. Never counts as a validation at any SHA: the gates run once a worktree appears, and only their `gates green at <sha>;` note lets a later scan record the missed policy read. A pre-#2321 hold with the same text under `[review: validated` is read the same way |
| `[review: red-tier @ ts] at <sha> — …` | green but touches red-tier files — needs the operator |
| `[review: rejected @ ts] at <sha>: …` | validation (audit/tests) failed — back to the dev |
| `[review: timeout @ ts] at <sha>: … — will retry next scan` | pytest exceeded `UA_REVIEW_PYTEST_TIMEOUT_S` (default 1800s), or a bun-based task's `bun test` exceeded `UA_REVIEW_BUN_TEST_TIMEOUT_S` (default 600s, task #1295), before finishing — inconclusive, not a red test (task #1254). Both raise a `merge.TestTimeoutError` subclass, recovered from an arbitrarily deep `__cause__`/`__context__` wrap chain by `reviewer._test_timeout_cause` (task #1295 — previously unwrapped exactly one `__cause__` level). Not idempotent the way `rejected`/`red-tier` are: the next scan re-runs the gates at the same sha instead of reading this as resolved, but only up to `UA_REVIEW_TIMEOUT_MAX_RETRIES` times (default 2) — once that many timeout notes exist for the sha, the pipeline holds as `unchanged` (no gate run, no note, no notify) until a new commit lands. Without a resolvable head sha, `_merge_done_task` falls back to a task-level total timeout-note count for that cap instead of retrying forever (task #1295). Only the first timeout note at a sha sends an iMessage; retries append the note silently. Written by `_validate_and_hold` (the in-flight path, including a yolo task's second, git_merge_task-internal gate run) **and** by `_merge_done_task`'s two gate calls for status=`done` tasks — but the `done`-task wording says "retry with `unified-agent-review task <id>`" instead, since `scan_for_reviewable_tasks` excludes `done` and no cron scan will ever pick it back up automatically (Devin review, PR #691). |
| `[review: merged-upstream @ ts]` | reconcile found the branch/PR already merged into origin/main; task set to done |
| `[review: stale @ ts] stale — branch missing` | branch missing locally and no PR referenced — needs attention |
| `[review: approved @ ts]` | merged by the local-merge flow (status-done tasks; commit hash recorded) |
| `[review: rework — <reason>]` | **human**-written, never by `reviewer.py`: a reviewer declines to merge a validated (gates-green) PR and asks for changes instead. See [Held-despite-green](#held-despite-green--same-day-regression-test) — this marker, combined with a green exact-head CI, is the whole "held" signal. |
| `[waiting-for: plan-approval]` | reserved: dev wants plan sign-off before building |

A live review request is the marker `[waiting-for: review]` **not** followed
by ` when done` (`reviewer._REVIEW_MARKER_LIVE`). Open, unclaimed spec tasks
that only embed the template as a future instruction (`_is_open_boundary_review_spec`)
are omitted from `scan_for_reviewable_tasks` and reconcile so a boundary
checklist cannot enter the validate path (task #709).

**No full suite on the hub** (task #1787, import graph in task #2231). A
pull request's own GitHub Actions run is the cheap gates only (task #1356),
so most PR heads have no exact-head pytest shards. The merge queue runs
affected tests; the nightly schedule and `gh workflow run ci.yml --ref
<branch>` run the full suite. When the shards are missing, the pipeline
runs the `direct` tier of
[`affected_tests.select`](../src/unified_agent/affected_tests.py) for a task
with a PR: changed test files (renames under the new name), `tests/**/test_<stem>.py`
for each changed `src/` or `scripts/` module, every test that imports a changed
module (directly or through test helpers, scripts and directory conftests, but
not through other src modules), and the cross-cutting inventory tests
(`affected_tests.ALWAYS_RUN`) when something else already mapped. Tests selected
only because they scan the whole source tree as text, or only because their
imports cannot be read statically, do not count as a mapping. When nothing maps,
the diff needs the full suite (`pyproject.toml`, `tests/conftest.py`, a file
nothing names, a pytest plugin loaded into every test), or more than
`UA_REVIEW_TARGETED_MAX_FILES` files map, the task is held as `awaiting-ci` with
one `[review: awaiting-ci @ …] at <sha>` note and no gates run; it is re-checked
each scan, so a later exact-head full run (a dispatch on that pull
request's branch) validates it. A merge-queue run, including one the
selector could not narrow (`ALL`), reports on the batch head and does not
clear `awaiting-ci` for the PR head. The awaiting-ci note
still names the historical `ci:full` label; the workflow no longer keys off
that label. A task with no PR keeps the
whole-suite run. A `+yolo` merge reuses the review's scope for the same head
(`merge.reviewed_gate_scope`, internal, never a tool argument).
`UA_REVIEW_LOCAL_PYTEST=full` restores the whole suite everywhere. A batch CI
run does not clear `awaiting-ci`, because it reports on the batch head, not the
PR head; see [ci-batch-verify.md](ci-batch-verify.md) for the steward's path.
The selection is a targeted subset, not a certification — the note says so.

**Idempotence** (one note line per state × sha): before validating, the pipeline reads
the task notes; if a `validated`/`rejected`/`red-tier` line already exists for the
*current* branch-head sha, the verdict is `unchanged` — no gates re-run, no note
appended, no notification. A new commit on the branch (new sha) triggers a fresh
validation. This is what keeps a `*/15` cron from bloating task notes.
`timeout` is deliberately excluded from this idempotence check (task #1254): it
means the gates never finished, not that they reached a resolved state, so the
next scan re-runs them at the same sha rather than treating the timeout note as
final. That retry is bounded separately, not left open-ended: the pipeline
counts existing `[review: timeout @ …] at <sha>` notes and stops re-running the
gates once `UA_REVIEW_TIMEOUT_MAX_RETRIES` (default 2) is reached, holding the
task as `unchanged` until a new commit lands. Without the bound, a suite that
genuinely never finishes would re-run a (now up to 1800s-long) pytest on every
`*/15` cron tick indefinitely — reintroducing, in a milder form, the exact
note-bloat and notification-spam problem the sha idempotence rule exists to
prevent. Only the first timeout at a sha notifies; the retries within the cap
still append a note (so the count stays accurate) but stay silent.

**PR references**: if the task notes contain a `PR: <url>` line, the validated note and
notification point at the PR; otherwise they point at the branch.

**Origin-branch hold (UA #736).** Desktop-host worktrees never register on the
hub row (`worktree_path` / `branch` stay NULL). Before the scan rejects
`no git worktree found`, `_origin_branch_fallback` looks for the branch in
three places, in order: a notes line matching `branch: <name>` or
`branch <name>` (`reviewer._BRANCH_NOTE_RE`), the task row's `branch`
column, then the head of the PR named in `evidence_ref` (task #2321: a
native-checkout claim, `provisioning: native-required`, records neither of
the first two). The PR head counts only when `gh pr view` reports it `OPEN`,
not from a fork, and based on the task's own repo (its URL's `owner/name`
equals that checkout's origin); a gh error falls through. Each candidate
runs `git ls-remote --heads origin <branch>` against the task's routed repo,
or for a workspace-scoped task the workspace repo and then each
`config.project_repo_roots()` checkout. An exact `refs/heads/<branch>` match
writes `[review: held-no-checkout @ ts] no local worktree on this host;
branch <name> (<source>) found on origin (ls-remote) — held for human merge
decision; gates not run (no local checkout)` and returns verdict
`validated`. The PR's repository must be on github.com with the same
`owner/name` as the checkout's origin remote, compared after parsing both
URLs (https, ssh or scp-like; default port only; case and `.git` ignored).
The fallback runs even when an earlier scan already wrote the
`no git worktree found` rejection, so that false rejection is repaired once. Gates do
**not** run: there is no local checkout. A broken remote, missing branch
name, or prefix-only match (`fix/task-1` vs `fix/task-10`) falls through to
the existing rejection.

Branch names are checked before `git ls-remote`
([`reviewer.py`](../src/unified_agent/reviewer.py)). A notes line still has to
match `_BRANCH_NOTE_RE` (`branch: <name>` or `branch <name>`): the name starts
with an alphanumeric and then only `[A-Za-z0-9._/-]`, so `+` and `@` never
come from notes. A name from the task row or from a PR head goes through
`_is_safe_branch` instead: a leading `-` is refused (so it cannot be read as
a git option), then `git check-ref-format --branch` (which does allow `+` and
`@`). The command must echo the same name back; a timeout or a missing `git`
fails closed. A name that fails either check is skipped, not held.

The hold is idempotent: a later scan that already has that note line is
`unchanged`. Push the branch and name it in the notes before asking the Mac
hub to review desktop-local work.

**Red tier** (canonical in
[`cycle_eligibility.RED_TIER_PATTERNS`](../src/unified_agent/cycle_eligibility.py);
`reviewer` re-exports the same list). Agent-instruction files (`AGENTS.md`,
`CLAUDE.md`, `SOUL.md`, `SKILL.md`, `.cursorrules`), config (`.env`,
`config.yaml` / `config.yml`, `.mcp.json`, `mcp_config.json`), `deploy/`,
hooks, and the security boundary itself: `SECURITY.md`, a `security/`
package, `security.py`, any `security*.py` module or test, `safety.py`,
`server.py`. The review pipeline's own evidence and authority surfaces are
also red-tier (tasks #2274 / #2277): `.github/workflows/`, `.depot/`,
`scripts/ci_changes.py`, `affected_tests.py`, `cycle_eligibility.py`,
`policy.py`, and `audit/rules.py`. `profiles.py` is deliberately not: a
widened tool surface needs a diff-content check, not a path match.

The unanchored `security` substring is gone (task #2275). A changelog
fragment named `*.security.md` or prose under `docs/` no longer escalates;
the gate code that decides which tests run does. Red-tier changes are
**never** auto-merged, and the claiming agent does not merge them on green
CI either — the pipeline validates them, then waits for the operator. The
shipping preflight's [head-moved hold](#shipping-preflight-head-moved-red-tier-hold-2259)
uses this same list.

## The delegation ledger (UA #703)

After every `scan` (and single-task `task` run), the CLI appends one line per
agent per task whose state actually changed — `- <ts> | <agent> | #<task> |
<verdict> | ci:<state>` — to the month's vault page
`Logs/Delegation Ledger <YYYY-MM>.md`. The month comes from the wall clock at
write time, so the page rolls over automatically; a missing month is
bootstrapped with a minimal header. Existing pages are **append-only** and a
`locked: true` page is never forced. Attribution is the task's structured
`claimed_by` (fallback: the `claimed-by:` note marker, else `unknown`); the
CI state maps gate verdicts to `green` / `red` / `red-tier` (`n/a` when the
verdict has no CI dimension, e.g. plan-approval). Idempotent `unchanged`
polls produce no line. The append is best-effort: a vault failure is a
stderr warning that never changes a verdict or the exit code
(`delegation_ledger_log.py`; tested in `tests/test_delegation_ledger_log.py`).

## The reconcile step

Runs at the start of every `scan`, after hygiene and before validation.
Reconcile is **broader than the validation scan**. The scan still requires a
live review marker. Reconcile also closes markerless in-flight rows whose PR
has already landed (UA #851 / #868) so a Cursor/handoff completion that wrote
`evidence_ref` but never appended `[waiting-for: review]` does not sit in
`waiting_review` forever, then fail the next scan with `no git worktree found`.

Two eligibility tracks (`reviewer.reconcile_tasks`):

| Track | Who is selected | What reconcile may write |
|---|---|---|
| **Review-flow** | `open` / `doing` / `waiting_review` / `blocked` with a **live** `[waiting-for: review]` or `[waiting-for: human-merge]` (boundary-only `when done` templates skipped) | `merged-upstream`, `stale`, `unresolved-repo` |
| **Merge-only** | Markerless `doing` / `waiting_review` | `merged-upstream` **only**. Never `stale` or `unresolved-repo` — those are review-flow signals. |

`[no-code]` tasks are skipped on both tracks. Named-project code tasks are
**not** inferred as no-code from a missing `[code-task]` marker.

Reconcile never closes a **live** task (task #986): rows with a `pending` or
`in_flight` delegation, or an unexpired executor claim lease, are skipped
(`_active_reconcile_blocker`). The executor still owns that work; a
merged-looking PR in notes must not flip the task to `done` underneath it.

PR resolution, in order (`_pr_ref_from_notes` then `_pr_ref_from_evidence`,
then a unique title search):

1. Notes: **only pipeline-generated marker lines** that start with
   `[review:` or `[pr:` (task #986). A URL or `PR #N` on those lines is
   trusted. Author-supplied prose — including a bare `PR: <url>` line or a
   GitHub pull URL in a sentence — is ignored so an unrelated mention cannot
   complete the task.
2. The typed `evidence_ref` column (a URL, an embedded `…/pull/N`, or a
   trailing `#N` / `N`). Cursor's PR flow often writes the column and never
   copies it into notes.
3. When the row is merge-only and those are empty: `gh pr list --search
   "UA #<id>"` and keep the result only when **exactly one** title matches
   `UA #<id>` or `task-<id>` / `task_<id>` (`_gh_pr_for_task`). Zero or
   several matches leave the task alone.

Then, for a resolvable repo:

1. Resolve the branch: worktree whose branch/path matches the task id, else a
   `branch <name>` note.
2. `git fetch origin` (once per repo, for every resolved repo). Then, for
   review-flow **with no linked PR** only,
   `git merge-base --is-ancestor <branch> origin/main` — a true ancestor
   means the local branch landed (regular merge). An explicit PR in notes or
   `evidence_ref` is the delivery artifact (task #2537): local ancestry must
   not override an open, closed-without-merge, or unavailable PR, so native
   work on a newer remote head stays unfinished. Merge-only never uses
   ancestry.
3. When a PR is linked, or ancestry does not hold (squash/rebase), and `gh`
   is available, `gh pr view <ref> --json state` — only state `MERGED`
   counts as landed. Missing `gh` (e.g. the cron PATH) degrades gracefully:
   the task is left alone until a human or a gh-equipped run resolves it.
4. An absolute PR URL still resolves with no local checkout
   (`gh pr view <url>`). Merge-only rows with no PR and no checkout are
   skipped; review-flow rows get one idempotent `unresolved-repo` note.
5. Landed → status `done`, one `[review: merged-upstream @ ts]` note
   (PR URL + merge SHA when `gh` can supply them), one notification.
   Review-flow only: branch missing locally *and* no PR referenced → one
   idempotent `stale — branch missing` note. Anything else is left for the
   validation pass.

This closes the loop for PR-based work: once the PR is merged on GitHub (by the
claiming agent, or by the operator for red-tier work),
the next scan flips the task to `done` instead of leaving it `doing` /
`waiting_review`. Markerless rows still need a resolvable, actually-merged
PR — a title search that is not unique is not enough.

## Queue hygiene

`unified-agent-review scan` calls `queue_hygiene.run_hygiene_pass` **before**
reconcile (`review_cli.py`). Source: [`queue_hygiene.py`](../src/unified_agent/queue_hygiene.py).
`--dry-run` rolls the SQL transaction back; `--no-notify` skips iMessage.
Staleness, parking and folding are reads and explicit actions, never part of the
pass: see [work-queue.md](work-queue.md#closure-stale-parked-folded-merged).

| Kind | Condition | Effect | Notes marker |
|---|---|---|---|
| stale-claim | `doing`, `lock_expires_at` in the past, and the owner probes terminal (its run is recorded as over: failed self-pickup end, cancel request, failed dispatch run or executor attempt; or, with no live agent or node, every hub session of the owner recorded ended after the claim) or absent (no agent row, no node) | status `open`; claim columns and `next_owner` cleared. If the owner's self-pickup run is still open with no recorded end and no cancel request, the work is **interrupted**: status `blocked` (never re-run on its own), `waiting_on` set, an `[interrupted: run=…]` marker, and one `run.interrupted` event on `agent.run.stalled`. An active or unknown owner keeps the claim; an unknown one is listed under `leases_kept` | `[hygiene: stale-claim @ ts claim=<claimed_at>]` |
| aging-demote | `open`, priority not `low`, `created_at` older than 14 days | one priority step down | `[hygiene: aging-demote @ ts]` |
| auto-archive | `done`, `completed_at` older than 30 days | stamp `archived_at` | `[hygiene: auto-archive @ ts]` |
| close-on-merge (#1848) | `evidence_ref` or branch names exactly one PR in the pr-watch cursor, merged with a recorded SHA; no live claim, human gate or open child | status `done`, `evidence_ref` = merge SHA; applies only with `UA_CLOSE_ON_MERGE=1` (default: propose only) | `[queue-closure: closed-on-merge @ ts]` |
| stranded session mail (#2547) | `to_session` mail still unread, unacked and unanswered when that session has ended (`completed`, `failed`, `abandoned`) | one `agent.message.stranded` fact per message (no body); `notify` (default) also sends one `hub-mailbox` inform to the recipient label per ended session and one to each sender; nothing is re-sent. Runs before the dead-letter sweep. A sender who already got a dead-letter *notice* is skipped so one message never gets both notices (tasks #2551, #2555); a dead letter recorded in `report` mode does not silence the stranded notice. | none (mailbox, not task notes) |

Each action is once-per-task: the pass skips any row that already carries that
kind's marker. Stale-claim is the exception: it is once per claim (task #1916).
Its marker names the `claimed_at` it released, and the pass skips only that
claim, so a task released, claimed again and abandoned again is released again.
Legacy markers without `claim=` never block a later release.

The pass also closes tasks behind a lost dispatch completion (task #1916, audit
O3). `record_run_outcome` commits the run and delegation, then closes the task
in a second write. If the process dies between the two, the run reads
`finished` while the task stays open.
`dispatch.scheduler.reconcile_finished_runs` replays the close when the run's
`[dispatch: finished ... run=<id>]` note is missing, no newer live run owns the
task, and the task was not touched after the run finished. A task someone
reopened is never closed by an old run. The result is reported as
`dispatch_reconciled`. The scheduler tick runs the same reconciler, but that
timer is retired (#1435), so in production this pass is what heals.

Notifications batch into one best-effort message per pass. A
broken bridge never fails the scan. `task <id>` does **not** run hygiene.

The flow metrics on Runs → Insights (`/ui/runs/insights`, `GET /api/flow-metrics`) are the operator
view of the same aging windows; see [web-ui.md](web-ui.md#flow-metrics-task-729).

## CLI: `unified-agent-review`

Deterministic driver over `reviewer.py`, same shape as `unified-agent-audit`.

```
unified-agent-review scan       [--dry-run] [--json] [--no-notify]
unified-agent-review task <id>  [--dry-run] [--json] [--no-notify]
unified-agent-review label-held <id> [--dry-run] [--json]
```

- `scan` — run queue hygiene, then reconcile (review-flow **and** merge-only
  rows; see [The reconcile step](#the-reconcile-step)), then **validate**
  every task with a **live** `[waiting-for: review]` marker (status `open` /
  `doing` / `waiting_review` / `blocked`; boundary-only templates skipped).
  Markerless in-flight tasks are reconciled when their PR is MERGED; they
  are not validated. No candidates is a successful no-op. A hygiene or
  reconcile failure is a stderr warning, never a blocker.
- `task <id>` — run the pipeline on one task (no reconcile).
- `label-held <id>` — see [Held-despite-green](#held-despite-green--same-day-regression-test).
  The one subcommand that writes to GitHub (a PR label); not run by `scan`.
- `--dry-run` — full validation (audit + tests + red-tier), but write no notes,
  change no task state, and never merge. For `label-held`, reports the verdict
  without applying the label.
- `--no-notify` — skip the iMessage push (notification text still printed/logged).
- `--json` — machine-readable result list on stdout (reconcile results included).

Exit codes: `0` = nothing to do or every verdict clean (`merged`, `dry-run-ok`,
`validated`, `unchanged`, `merged-upstream`, `stale`) or inconclusive (`timeout` —
task #1254: a pytest-timeout verdict deliberately does not trip exit code 1, since
it is not a red test and would recreate the false-FAIL problem the verdict exists
to fix; it is still broken out in the summary line for visibility); `1` = at least
one `red-tier` or `failed` verdict (so cron logs show FAIL); `2` = configuration error
(including a missing, retired, or non-SQLite `UA_HOME/memory.db` — see
[Live task store](#live-task-store)).

Environment: honours the same `UA_*` variables as the server — most importantly
`UA_WORKSPACE` (the cockpit write sandbox), `UA_HOME` (authoritative
`memory.db` location), and `UA_BRIDGE_DIR` (iMessage bridge for
notifications; when unset, `notify` uses the process workspace's
`projects/imessage-bridge`, not a per-agent workspace — set it when the
bridge lives elsewhere). Task worktrees are **not** assumed to live only in that workspace.
`merge.find_task_worktree` searches the workspace repo first, then every existing
checkout in `config.project_repo_roots()` (`unified-agent`, `browser`,
`sample-store`, `client-portal`, existence-checked under `UA_DEV_ROOT`, default
`~/dev`). A project-sourced worktree is registered against the project repo, so
`git worktree list` in the cockpit never lists it. Notification delivery is
best-effort: a broken bridge never blocks a verdict, it is reported in the result
instead.

## Held-despite-green ⇒ same-day regression test

Task #1260, Epic C #1188. Company operating rule
(`docs/company/OPERATING-SYSTEM.md` step 5): "A PR held back despite green CI
gets a regression test the same day." Three pieces implement it.

**1. The detection.** `src/unified_agent/held_pr.py`'s `held_despite_green()`
is the one place the decision is made, and it is pure (no I/O, unit-tested in
`tests/test_held_pr.py`). A task/PR is held-despite-green when BOTH:

- a rework signal exists — a task note whose `[review: ...]` annotation
  *starts with* `rework` (`[review: rework]`, `[review: rework — reason]`;
  incidental words like "fix" elsewhere don't count) or an explicit
  `verdict="rework"`; and
- the exact-head CI (a caller-supplied set of check conclusions, *not* this
  pipeline's own internal gate run) was green — every conclusion in
  `success`/`neutral`/`skipped`. A check still queued or in progress is not
  green.

No CI data never counts as held — that would false-positive on every PR a
caller could not reach the Checks API for.

**2. The label.** The `held` GitHub label is what the CI check (next) reads.
`reviewer.py`'s unattended scan/task pipeline stays strictly read-only against
GitHub (see [Policy: validate + hold](#policy-validate--hold) and
`held_pr.py`'s module docstring) — applying a label is a GitHub *write*, and
the automated pipeline never records a rework verdict in the first place
(`[review: rework]` is exclusively human-authored). So labeling is a
**separate, explicitly opt-in operator step** today:

```
unified-agent-review label-held <task_id>
```

Run this right after writing the `[review: rework]` note on a PR that had
green CI. It resolves the task's PR reference (reusing the same
`_pr_ref_from_notes` / `_pr_ref_from_evidence` extraction the reconcile step
uses), reads the PR's current check conclusions (`gh pr view --json
statusCheckRollup` — the same `pr view` subcommand `reviewer.py` already uses
elsewhere, just a different `--json` field), and — only when
`held_despite_green()` says held — applies the `held` label via `gh pr edit
--add-label held`. Requires the `held` label to already exist on the GitHub
repo (`gh label create held`, a one-time setup step). `--dry-run` reports the
verdict without writing the label. A task whose PR reference is a bare number
and has no local checkout is refused rather than resolved against whatever
repo the command runs in.

**Label lifecycle.** The reviewer who writes `[review: rework]` owns the
label: they apply it with `label-held`, and they remove it (`gh pr edit <pr>
--remove-label held`) when they record the approving review. The CI gate
never adds or removes labels. A label left on after approval is harmless — the
PR already carries its test — but remove it so the PR list stays truthful.

**3. The CI gate.** A step in the `gates` job of `.github/workflows/ci.yml`
(mirrored in `.depot/workflows/ci.yml` as a dispatch-only copy — see
`tests/test_ci_workflow_parity.py`) runs `scripts/check_held_pr_regression_test.py`
on every `pull_request` run (cheap gates; [ci-batch-verify.md](ci-batch-verify.md)).
A held PR that needs this check is labelled:

- reads the `held` label straight from the `pull_request` webhook payload
  (`GITHUB_EVENT_PATH`) — no `gh` call needed for that part;
- when absent, **skips cleanly** (exit 0) — the PR was never held;
- when present, fails (exit 1, actionable message) unless the PR's changed
  files (base...head) add or modify at least one file under `tests/` (or
  `packages/*/src/**/*.test.*` / `__tests__/`), or the PR body/a commit
  message carries a `Regression-Test: <path>::<name>` trailer that names an
  existing test file (matching the test-path rule) containing `<name>`. Every
  trailer is checked; one valid trailer is enough. Deleted files never count
  as coverage (`git diff --diff-filter=ACMR`).
- **Same day.** The job looks up when the `held` label was applied (issue
  events API, best-effort) and passes it as `--held-at`. A qualifying test
  that landed more than 24h later still passes the check — failing it would
  block the fix forever — but prints a `LATE` warning into the job summary.
- Whether the test actually exercises the held defect is reviewer judgement;
  the gate only guarantees a test exists.

The script takes every input as an explicit CLI arg (`--held`,
`--changed-files[-json]`, `--pr-body[-file]`, `--commit-messages-json`), so
`tests/test_check_held_pr_regression_test.py` drives it end to end with no
`git`/`gh`/network — only the live workflow path (`--event-path` +
`--base-sha`/`--head-sha`) touches real git.

## Live task store

`unified-agent-review` calls `storage.require_live_task_store()` before
reconcile / plan-approval / scan. That preflight:

- refuses when `UA_HOME/NO_LOCAL_MEMORY_DB` is present (host retired its local
  brain — Mac hub is authoritative);
- refuses a missing `memory.db` instead of letting `sqlite3.connect` create an
  empty file that later fails with `no such table: tasks`;
- refuses a non-SQLite file at the `memory.db` path (the historical desktop
  retirement text stub) that otherwise fails with `file is not a database`;
- requires a real `tasks` table before any mutation scan proceeds.

Failures are exit code `2` with an actionable message — never soft-warned as
reconcile/plan-approval noise. The cron wrapper also short-circuits to
`OK skipped — local task store retired` when the marker is present, so a
leftover desktop timer cannot spam the vault inbox.

## Project-sourced worktrees

A code-bearing task creates its git worktree from that project's `~/dev`
checkout, not from the cockpit. Routing uses the task's typed `project`
column (set via `task_add(project=…)` / `task_update(project=…)`), not a
`project:` line in notes. Notes are inspected only for the `[code-task]`
marker, which is required solely when the project is the default
`unified-agent` — that marker is the tie-breaker that cuts a product-repo
worktree instead of a cockpit one. Named projects always route to their
checkout without needing `[code-task]`.

The worktree *directory* for those code-bearing tasks is
`<project>/.worktrees/task-<id>` on the product repo (task #994), not under
the cockpit. Git metadata is registered against that product repo, so
`git worktree list` in `~/agent-workspace` never lists it.

Consequences for review, merge, and agent tools (`merge.py`, `reviewer.py`,
`safety.py`):

- Lookup: database `worktree_path` / `branch` first, then branch/path regex
  matching `task<N>` across workspace worktrees, then the same match across
  each known project repo. A non-git cockpit is an empty list, not an error,
  so the project fallback still runs.
- Gates run **inside** the resolved worktree (`resolve_task_worktree`). That
  path may be the workspace or `~/dev/<project>/.worktrees/...`.
- While a project-sourced worktree is the **active** task worktree,
  `resolve_in_workspace` allows `fs_write` / `run_cmd` inside it (cockpit
  **union** that worktree — task #995). Agent-facing `git_*` still refuse:
  `_ensure_repo_in_workspace` requires the git toplevel inside
  `UA_WORKSPACE`. Review/merge internals and the executor daemon use their
  own git runners after `_known_repo_or_raise` / `resolve_task_worktree`.
- Pytest exit code **5** (no tests collected) is reported as `No tests were
  collected`, distinct from exit code 1 (`Unit tests failed`). Both fail the
  gate unless `force=True`.

If lookup still returns nothing, the usual causes are: the project slug is not
in `_KNOWN_PROJECTS`, the checkout directory does not exist on this host, or
the branch name does not contain the task id. See
[troubleshooting.md](troubleshooting.md#review-pipeline-cannot-find-a-worktree).

## Cron wrapper: `deploy/review-pipeline-cron.sh`

Same conventions as `deploy/steward-audit-cron.sh` — **unchanged by the validate+hold
work**; the reconcile step and the new verdicts ride inside the `scan` subcommand it
already calls:

- runs `unified-agent-review scan` with `UA_WORKSPACE=$HOME/dev/agent` and
  `UA_HOME` defaulting to `~/.unified_agent`
- if `UA_HOME/NO_LOCAL_MEMORY_DB` exists, logs one `OK skipped` line and exits
  `0` without invoking the CLI (desktop post-cutover safety)
- appends one line per run to `$UA_HOME/review.log`
  (`<ts> OK|EVENTS|FAIL <summary>`); quiet runs stay one line
- on any `red-tier` / `failed` verdict, appends the full findings to a dated note in
  the vault inbox (`00 - Inbox/Review Pipeline YYYY-MM-DD.md`) so it surfaces in
  Obsidian even if the iMessage push failed
- install: prefer `ua-review-pipeline.timer` on the hub host; crontab form is
  `*/15 * * * * $HOME/dev/unified_agent/deploy/review-pipeline-cron.sh`

The iMessage notification itself is sent by the CLI (via `reviewer.review_and_notify`
and the reconcile step), not the shell script — one message per **new** verdict;
`unchanged` no-ops send nothing, which is exactly the "single notification at the end"
the task-65 acceptance asks for.

## What stays human

The pipeline itself never merges, pushes, or opens PRs; those GitHub writes belong to
the claiming agent (branch push, PR create, and — since the operator's 2026-09-23 ruling —
merging its own PR on exact-head green CI plus one independent review with no unresolved blocking finding, review threads
resolved; the shipping preflight additionally requires that review to be
[human-attested on the exact head](#shipping-preflight-human-attested-independent-review-2158);
see
[Policy](#policy-validate--hold)). Reserved for the operator:

- **red-tier changes** — validated, then held for his approval, every time; the
  claiming agent does not merge a red-tier diff on green CI alone
- **Approval Ledger approval rows** — resolved by the named human in `/ui`, never by an
  agent (`approval_resolve` from an agent would read as human attestation, task #1217)
- **merging a PR the merging agent did not claim** — the 2026-09-23 ruling covers the
  claimant's own PR only; widening it is his call
- the charter's the operator-only decision rights that a change can touch: secret values,
  public exposure (Funnel, public ingress, multi-tenant enablement), new paid services,
  and destructive deletes (cockpit `docs/company/CHARTER.md`, Decision rights)

To observe the pipeline with no state writes at all, run the cron with
`UA_REVIEW_DRY_RUN=1` in the environment (the wrapper forwards it as `--dry-run`).

## Anomaly detection rules

`check_anomalies()` / `check_anomalies_and_notify()` (`src/unified_agent/monitor.py`,
task 64) run a fixed set of deterministic rules — no LLM, pure SQL/date-arithmetic or a
short subprocess probe — over the shared state every `*/30` cron tick
(`deploy/anomaly-check-cron.sh`). Each finding is `{rule, subject, detail}`; full
thresholds and env-var names live in the `check_anomalies()` docstring, which is also
the generated MCP tool description — treat it as the source of truth. Summary:

| Rule | Trigger |
|---|---|
| `stale_doing_task` | task stuck in `doing` past `UA_ANOMALY_STALE_DOING_DAYS` (5d) |
| `silent_agent` | agent with no `journal` event in `UA_ANOMALY_SILENT_AGENT_DAYS` (2d), counting only agents with at least `UA_ANOMALY_SILENT_AGENT_MIN_EVENTS` (2) journals — one journal is a tool run (`pi_worker`), not a cadence — **and** that either holds/held a structured claim in the window or whose hub `last_seen` is also stale. Labels in `UA_ANOMALY_SILENT_AGENT_IGNORE` (comma-separated, exact match, default empty) are skipped. `UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL` (`label=days`, default `weekly-export-cron=8`) gives a slower-cadence label its own threshold |
| `idle_lane` | companion to `silent_agent`: journal-stale but hub-alive (`agents.last_seen` fresh) with no claimed work. Emitted with `severity=info`; excluded from iMessage paging and from digest "stuck" attention |
| `task_reassignment_thrash` | task re-claimed `UA_ANOMALY_REASSIGN_MIN_CLAIMS` (3) times in `UA_ANOMALY_REASSIGN_WINDOW_DAYS` (7d). Terminal tasks are skipped; a live task can be acknowledged with a dated, rule-scoped marker (see [Acknowledging a live task's churn](#acknowledging-a-live-tasks-churn)) |
| `agent_never_completes` | agent claimed `UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS` (3) tasks, none done |
| `db_size_growth` | memory.db grew `UA_ANOMALY_DB_GROWTH_MB` (50MB) over `UA_ANOMALY_DB_GROWTH_WINDOW_DAYS` (7d) |
| `approval_queue_age` | pending approvals older than `UA_ANOMALY_APPROVAL_AGE_HOURS` (2h) |
| `bridge_disconnect` | iMessage bridge expected but send.ts/bun missing |
| `service_down` | a unit in `UA_ANOMALY_SERVICES` (default `unified-agent,gbrain`) has `ActiveState` != `active`. Linux: `systemctl --user show`. Darwin: `launchctl print` via `MAC_LAUNCHD_LABELS` (hub, Agy executor, Zulip worker). Unmapped units (gbrain, timers) are `not-found` on Darwin and skipped. Empty `UA_ANOMALY_SERVICES` disables the rule |
| `service_crash_loop` | one of those units' `NRestarts` exceeds `UA_ANOMALY_SERVICE_RESTARTS_MAX` (3) — catches a crash loop that's momentarily `active` |
| `host_low_memory` | `/proc/meminfo`'s `MemAvailable` below `UA_ANOMALY_LOW_MEM_PERCENT` (15%) of total; hosts without `/proc/meminfo` stay silent |

The two `service_*` rules exist because gbrain crash-looped ("activating"/failed,
restart counter 49) for hours without paging — `ActiveState` alone can catch a unit
mid-loop while it happens to be up, so `NRestarts` backstops it. Both rules share
one probe: `systemctl --user show <unit> --property=LoadState,ActiveState,NRestarts`
on Linux, or `launchctl print gui/$UID/<label>` on Darwin (~5s timeout). A missing
probe binary, a timeout, or any other unexpected error skips the unit silently,
never raised and never fabricated into a finding. A unit the host cannot load
(`LoadState` != `loaded` — not installed here, e.g. a CI runner or a Darwin
host without that LaunchAgent) is skipped the same way, so a machine without
the unit deployed never alarms.

`task_reassignment_thrash` only reports tasks that are still live. Once a task
is `done` or `cancelled`, its re-claim churn is history and the rule skips it,
so no acknowledgement marker is needed. Churn on an open, doing, blocked
or waiting_review task still fires within the
`UA_ANOMALY_REASSIGN_WINDOW_DAYS` window.

### Acknowledging a live task's churn

A task that is legitimately still open can carry an attributed acknowledgement in
its own notes, which stops the rule paging for churn that already has a known
cause:

```text
[anomaly: understood task_reassignment_thrash @ 2026-09-25T21:30:00Z by agent-hermes]
```

Append it with `task_update(id=…, append_note="…")`; the marker is the audit
record, so it needs a rule name, an ISO-8601 timestamp, and the acknowledging
agent. Its limits are deliberate:

- **Rule-scoped** — the ack must name the rule. A marker naming another rule, or
  the legacy bare `[anomaly: understood]`, suppresses nothing here.
- **Attributed** — `by <agent>` is required. Without it there is nobody answerable
  for the decision, so the churn keeps reporting.
- **Dated, and strictly later** — it covers only claims that predate it. A claim
  in the same second is ambiguous and keeps reporting, and any claim recorded
  afterwards re-pages on its own, so the marker cannot hide new churn; append a
  newer marker to acknowledge the new churn too.
- **Fail-visible** — a marker whose timestamp will not parse, or one dated in the
  future (beyond a 5-minute clock-skew tolerance), is ignored and the anomaly keeps
  reporting, rather than being silenced by a typo.
- **Quoted is not applied** — a backticked mention (`` `[anomaly: understood …]` ``)
  is a task *discussing* the marker, not carrying one. Same posture as
  `[waiting-for: review]` and `[code-task]`.
- **Not for terminal tasks** — the status filter runs first, so an ack cannot be
  applied to a finished task or change its state.

Thresholds and the window are never widened to silence a row; this marker is the
only suppression path for a live task.

## Bounded autonomous cycles

`autonomous_cycle_start` creates a durable cycle in `plan` state. The cycle ledger
connects optional plan, task, delegation, and session IDs without starting work itself.
`autonomous_cycle_transition` is the only state mutation and requires a known agent,
an idempotency key, and an optional expected version for compare-and-swap writes.

Allowed progression is:

```text
plan -> delegated -> executing -> validating -> reviewing -> pr -> completed
                                      \-> rework -> delegated
```

`blocked` and `failed` are explicit stop states. A stop requires a reason, entering
`pr` requires a PR URL, and rework automatically becomes `blocked` when the configured
budget is exhausted. Replaying an idempotency key returns the original transition;
stale versions are rejected. These tools persist lifecycle state only: execution,
validation, review, merge, and external GitHub mutation remain separate policy-gated
operations.

Each cycle also records time, token, retry, and estimated-cost usage. The configured
defaults come from `UA_AUTONOMY_MAX_TIME_S`, `UA_AUTONOMY_MAX_TOKENS`,
`UA_AUTONOMY_MAX_RETRIES`, and `UA_AUTONOMY_MAX_COST_CENTS`; callers may provide
smaller per-cycle limits. `autonomous_cycle_record_usage` atomically updates usage and
changes the cycle to `blocked` with the exhausted dimensions when a limit is crossed.
Capability requests are checked against the agent's configured tool allowlist, and a
denied capability also creates a visible blocked cycle rather than silently running it.

Transitions into `pr`, `completed`, `blocked`, or `failed` require a human approval
from the global approval queue. The request is recorded as
`autonomous_cycle:external_mutation`, `autonomous_cycle:merge`, or
`autonomous_cycle:escalation`; resolve it with `approval_resolve`, then retry the
identical transition to consume the one-shot approval. A pending or denied approval
leaves the cycle unchanged.

Two of those four can be **earned** rather than asked for, per transition, by a
pure-function eligibility check. Both apply the same criteria — no recorded
`stop_reason`, no red-tier file in the diff (the canonical `_is_red_tier` list,
never a copy), and `run_validation_gates` passing (clean tree, security audit,
pytest):

| transition | check | if eligible | if not |
|---|---|---|---|
| `reviewing -> pr` | `autonomous_cycle_pr_path_eligibility` | opens with no human; transition reason prefixed `[auto-pr]` | `autonomous_cycle:external_mutation` gate, unchanged |
| `pr -> completed` | `autonomous_cycle_green_path_eligibility` | merges with no human; reason prefixed `[green-path auto-merge]` | `autonomous_cycle:merge` gate, unchanged |

`blocked` and `failed` (escalation) have no bypass and are always human-gated.

The effect is that a clean cycle runs `plan -> delegated -> executing ->
validating -> reviewing -> pr` with no human in the loop, and the merge is the
gate.

Anything not provably green falls back to the approval queue. The checks fail
closed, and "closed" specifically includes the cases where the check cannot
reach a verdict at all: a missing worktree, a `git diff` that errors, no usable
`main`/`origin/main` base, an empty change list, or an exception inside the
check itself. `_red_tier_for_task_checked` returns an explicit
`unclassified_reason` for exactly this reason — an unreadable diff and a clean diff
both produce an empty hit list, and only a non-`None` reason tells them apart.
Treating the two alike would let a branch touching `safety.py` skip the gate
because the diff failed to read.

For `reviewing -> pr`, the supplied `pr_url` must also bind to the validated
worktree HEAD (SHA prefix match, or `gh pr view` headRefOid match for GitHub
URLs) before the `external_mutation` gate is cleared. Unbound or mismatched
evidence stays human-gated.

Both auto-bypass paths pin worktree HEAD around the validation gates (task
#598). The SHA is recorded before `run_validation_gates` and compared after.
If HEAD moved mid-run (a concurrent commit, rebase, or executor fixup), the
check returns ineligible: the gates no longer describe the commit that would
be approved. A missing worktree or unreadable HEAD is also ineligible. This
applies to `autonomous_cycle_green_path_eligibility` (`pr -> completed`) and
`autonomous_cycle_pr_path_eligibility` (`reviewing -> pr`).

The eligibility checks run the validation gates (security audit, pytest), which
take minutes, so they are evaluated *outside* the cycle's write transaction:
the transaction is committed first, the gates run, then it is reacquired and
the cycle version and transition legality are revalidated against a fresh row.
Holding the SQLite write lock across the gates would make unrelated task,
memory, and approval writes fail on the shared 5-second busy timeout.

Every bypass records *why* in its transition row, so "no human approved this"
is always auditable rather than invisible.

### Operator runbook

1. Inspect a cycle with `autonomous_cycle_get(cycle_id)` and confirm its state,
   version, usage, transition history, and linked task/delegation/session IDs.
2. Inspect `approval_pending()` for `autonomous_cycle:*` requests. Approve or deny
   the exact request with `approval_resolve(id, decision, resolved_by="Operator")`.
3. Retry the identical transition after approval. A stale `expected_version` or a
   changed idempotency key must be treated as a new request, not retried blindly.
4. For a blocked cycle, resolve the escalation gate and move `blocked -> plan` only
   after the underlying issue is understood. For failed cycles, preserve the terminal
   record and start a new cycle rather than reopening it.

Rollback is ledger-level recovery, not an automatic Git revert: stop the cycle in
`blocked`, preserve the approval and transition audit trail, and use the existing
review/merge tooling for repository rollback. The execution graph reads this durable
state even when event coverage is partial; missing validation, review, or PR artifacts
remain explicit `null` values rather than inferred success.

## Active Executor backends (native headless persona)

The Active Executor daemon separates the **lifecycle** from the **execution**. The
daemon always owns the lifecycle — claim the delegation, claim the task, create the
worktree, renew the lock, apply the failure/backoff policy, and annotate
`[waiting-for: review]`. A *backend* only turns a prompt into a run inside the
directory the daemon prepared.

**Remote worktree localization (tasks #1094 / #2288).** A desktop executor
claims through the Mac hub, so `task_claim` records a hub-side absolute
worktree (`/Users/you/dev/<repo>/.worktrees/task-<id>`). When that path does
not exist on this host,
[`localize_worktree`](../src/unified_agent/executor/daemon.py) keeps
everything from the `dev` segment onward and re-roots it at `~/dev`
(`Path.home() / "dev"`, not `UA_DEV_ROOT`). Then it walks up from the
candidate for a `.git` **strictly below** `~/dev` and runs `git worktree add`
there. Constraints:

- No `dev` segment, or a `..` after it, returns no path.
- The walk never uses an enclosing repository at `~/dev`, `~`, or `/` (a
  dotfile repo in the home directory used to receive the worktree).
- A checkout or `.git` whose resolved path is not strictly below the
  resolved `~/dev` is refused, so a `~/dev/<repo>` symlink that aliases `~`
  cannot smuggle the walk out. A `~/dev` that is itself a symlink to another
  volume still works.
- A mismatched `origin` is refused
  ([`_origin_matches_hub_repo`](../src/unified_agent/executor/daemon.py)).
  The remote's repo name must match the hub project, with `-` and `_`
  treated as the same character. A checkout with no `origin` remote is
  accepted.
- An attested (canonical) run checks HEAD and branch on a task worktree
  that already exists
  ([`_existing_worktree_matches_attestation`](../src/unified_agent/executor/daemon.py)),
  not on the project checkout. That worktree's HEAD must equal the hub's
  `worktree_source_sha`, and, when a branch is recorded, its current branch
  must equal it. A task worktree created on this host starts at the attested
  SHA; the project checkout can stay on its current branch.

When localization returns no path, a Git/canonical run is refused with
`ExecutorProvisioningFailed` (`canonical run worktree could not be localized
safely`). The task is blocked with
`[executor provisioning failed: …]` and the child never starts in the
workspace root or in a repository above `~/dev`. Clone the project under
`~/dev/<repo>` on that host, then retry. See
[troubleshooting.md](troubleshooting.md#remote-executor-refuses-a-run-worktree-could-not-be-localized).

**Kill / budget gate (task #992).** Before dispatch, the daemon reads the
task's governing cycle (`active_cycle_for_task`). Local `storage.connect()`
is tried first. If that raises — typical on a desktop whose `memory.db` is
an 82-byte tombstone after Mac cutover — it falls back to
`autonomous_cycle_manage(action="get_for_task")` on the hub. A killed or
blocked cycle terminal-fails the delegation (it is not released back to
`pending`, which would busy-loop). If the hub round-trip also fails, the
daemon **fails closed**: it does not treat "can't read the gate" as "no
cycle". Rehearsal: `scripts/kill_switch_rehearsal.py`.

**Cycle and budget usage.** After each run the daemon records against the bound
cycle the wall-clock time it measured, one retry for a non-success, and, when
the harness reported them, the run's tokens (input plus output) and cost in
cents (task #1917). Tokens come from Claude Code, Codex and Agy output; cost
only from Claude Code's `total_cost_usd`. The same usage goes to the original
run as authenticated
`task_claim(usage_only=True)` accounting carrying cumulative `run_usage`,
including after expiry, cancellation or takeover. Distinct attempts retain
known dollar cost and an unknown-cost reserve when any price is missing.
Run and cycle snapshots replay durably without duplicate charges, and a
blocking budget response suppresses another attempt. Budgets count it at once; see
[budgets.md](budgets.md#executor-runs).

**Host-side finish commit.** After a successful child run the daemon commits
from the host (`_commit_child_changes`) when the worktree is dirty, using
`git -C <worktree>` so linked worktrees whose `.git` is a gitdir file still
work (tasks #501 / #506). It refuses anything whose toplevel is not the
task worktree, or whose branch is not `task/…`. Child backends also get
writable `UV_CACHE_DIR` / `RUFF_CACHE_DIR` under `$TMPDIR` so a sandboxed
child can still run `uv` / `ruff` (task #505). Codex additionally passes
`--add-dir` pointing at the git common dir when that dir sits outside the
worktree cwd.

When `UA_EXECUTOR_BACKEND` is unset, the daemon runs that agent's **own**
headless CLI (Claude Code `-p`, Codex `exec`, Agy `-p`, Cursor `agent -p` via
`agent-with-hub`, Copilot `copilot -p` via `copilot-with-hub`, Hermes
`--oneshot`, Devin `-p` via `devin-with-hub`). `pi` remains an explicit alias
only — it is not the default, and it must not be used to wear another agent's
label.

| Backend | `UA_EXECUTOR_BACKEND` | Runs | Registry `kind` |
|---|---|---|---|
| Claude Code | `claude` (or unset + `agent-claude`) | `claude -p --output-format stream-json --verbose` | `claude_run` |
| OpenAI Codex CLI | `codex` (or unset + `agent-codex`) | `codex exec --json` | `codex_run` |
| Antigravity CLI | `agy` (or unset + `agent-agy`) | `agy -p --output-format stream-json` | `agy_run` |
| Antigravity interactive (opt-in) | `agy-interactive` | PTY-driven `agy` (pexpect; tmux fallback) | `agy_interactive_run` |
| Cursor Agent CLI | `cursor` (or unset + `agent-cursor`) | `agent-with-hub -p --output-format stream-json --trust` | `cursor_run` |
| GitHub Copilot CLI | `copilot` (or unset + `agent-copilot`) | `copilot-with-hub --output-format json --allow-all-tools -p` | `copilot_run` |
| Hermes Agent | `hermes` (or unset + `agent-hermes`) | `hermes --oneshot` | `hermes_run` |
| Devin CLI | `devin` (or unset + `agent-devin`) | `devin-with-hub --permission-mode accept-edits -p "<prompt>"` | `devin_run` |
| Pi Headless RPC (explicit alias) | `pi` | `pi_run_task` | `pi_run` |

**Agy CLI pitfalls (task #1158 / #1172).** Always launch with
`agy -p "<prompt>" --output-format stream-json` (`-p` before `--output-format`).
`agy --continue -p"…"` without stream-json exits 2 with a help dump; glued
`-p"…"` is the same trap. stream-json keeps the event pipe parseable but does
**not** defeat agy's print-mode background drain: when the agent backgrounds
long work (e.g. pytest) the CLI waits ~5s idle
(`root agent idle; waiting up to 5s for N background task(s)`), terminates
those tasks, and may still stamp SUCCESS. The executor detects that signature
as `agy_background_teardown` and refuses the attempt; a clean-worktree
"success" is recorded as `zero_output_attempt` and fails the delegation
immediately rather than burning the retry ceiling. Live agy binary not
required for the repro — tests use a fixture that emits the same stdout
pattern.

**Teardown auto-resume (task #1172).** When the teardown signature appears
*and* the task worktree is dirty (`git status --porcelain` non-empty — work
survived the kill), `AgyBackend` re-invokes
`agy --continue -p "…" --output-format stream-json` up to **2** times
(`resume_count` on the attempt). A clean worktree never resumes (that is a
completed delivery). If every resume still tears down with a dirty tree, the
attempt fails with `failure_class` `agy_teardown_resume_exhausted`. Dispatch
prompt foreground discipline is unchanged (defense in depth).

**Upgrade canary (task #1172, cross-ref #1143).** On **every** `agy` upgrade,
run `scripts/agy_teardown_canary.sh`. Before it starts agy it asks
`dispatch/harness_sandbox.py` for a verified sandbox posture (task #1965).
agy 1.3.0 has no documented way to disable `BypassSandbox`, so the canary
exits 2 with `harness_sandbox_unavailable:agy` and does not pass
`--dangerously-skip-permissions`. Do not add that flag by hand and do not
edit the installed agy config. When a future agy can prove bypass is off,
the same check places the sandbox argv it returns ahead of the flag and the
verdict below applies again. Until then a `KILLED`/`SURVIVED` line is not
expected. When the posture is verified, the script creates a throwaway git repo, asks
print-mode to background a sleep+write, waits up to 10 s for the marker
(`UA_AGY_CANARY_MARKER_WAIT_S`), then prints a one-line
`SURVIVED` / `KILLED` verdict (exit 0 / 1). An upstream fix that stops killing
background work will flip the canary to `SURVIVED` within minutes of the bump.
`KILLED` has two forms. `teardown=1` means agy printed one of the teardown
phrases. `teardown=0 silent=1` (task #1791) means agy printed nothing, but it
exited 0, the last stream-json `result` event reports `SUCCESS`, a
`run_command` step whose `CommandLine` is exactly the canary's launch command
finished `DONE`, and the marker never landed: the silent loss agy 1.2.7 and
1.2.14 show (next paragraph). A command that only mentions the marker file
does not count as the launch. `ERROR` (exit 2, "no_marker
no_teardown_signature", with the `result=` status and `canary_done=` flag it
saw) is now kept for a real setup or invocation failure: agy exited nonzero,
its terminal result is missing or not `SUCCESS`, or the canary command never
ran to `DONE`. Treat it as a broken canary run to fix and re-run, not as a
verdict on agy. The wait accepts whole seconds up to five digits, read as
decimal (`08` is 8 s).

**Silent-loss variant confirmed live, no fork/patch path exists (task #1143).**
Task #1166 established the fork-and-fix path is void: `google-antigravity/
antigravity-cli` is docs-only (no source, no build system), the installed
`agy` is an opaque ~224MB binary, and Antigravity/Google Terms prohibit
modifying or redistributing it. The remaining legitimate path is reproduce +
report upstream, which #1143 did: 5/5 canary runs against the locally
installed `agy 1.2.7` lost the backgrounded write (`SURVIVED` never fired),
but **none of the four teardown phrases `detect_agy_background_teardown`
matches appeared anywhere in stdout or stderr**. The canary then reported
`ERROR ... no_marker no_teardown_signature`, because 1.2.7 no longer emits any
of that log text for this failure mode; since task #1791 it reports
`KILLED ... teardown=0 silent=1` for the same trace (replayed in
`tests/test_agy_teardown_resume.py::TestAgyTeardownCanarySilentLoss`).
Both text-level checks still miss it:
`backends.detect_agy_background_teardown(stdout, stderr)` and
`backends.parse_agy_stream_json(stdout)["error"]` return `None` on the
captured trace. At capture time those were the only checks, so
`AgyBackend.run()` reported a clean `success` for an attempt that silently
dropped the backgrounded work. Task #1247 closed that gap in the executor:
`backends.detect_agy_unconfirmed_background_launch(stdout)` flags any `DONE`
`run_command` step whose `CommandLine` contains an unquoted shell `&`, whatever
agy prints or reports as its status. `AgyBackend._invoke_once` applies it after
the text check, so the same trace now returns `status: "error"` with an
`agy_background_teardown` reason (and the #1172 auto-resume applies when the
worktree is dirty); `tests/test_agy_false_success.py` pins that. Raw
trace, the 5-run summary, and a fixture-replay test with no live-`agy`
dependency: `tests/fixtures/agy_teardown_1143/` and
`tests/test_agy_teardown_repro.py`. Unsent upstream draft (the operator's
call to file):
`docs/upstream/2026-09-21-agy-print-mode-background-task-silent-drop.md`.

**Workaround (task #1143) — until an upstream fix lands:** print-mode `agy`
cannot be trusted to preserve any work handed off with `&`/background
intent, and can no longer even be relied on to *say* it dropped that work.
For a task whose prompt is expected to background long-running work (tests,
builds, long shell pipelines), prefer the interactive backend
(`UA_EXECUTOR_BACKEND=agy-interactive`, task #1177 below) over print mode —
its PTY-driven session does not exit at turn end the way print mode's
one-shot process does. For tasks that must stay on print mode, keep the
dispatch prompt's existing "prefer foreground commands; do not background"
discipline (defense in depth, not a fix on its own — see #1172's
`AGY_RESUME_PROMPT`) and treat *any* canary verdict other than `SURVIVED`
(`KILLED` in either form, or an `ERROR` you have not yet explained) as a
live-agy regression worth re-running before trusting
a print-mode `agy` result on backgrounded work. This does not change agy's
place on the executor path — `agy` stays a supported backend; this is a
per-task routing and prompting mitigation, not a lane removal.

**Interactive agy backend (task #1177, layer 3).** Opt-in with
`UA_EXECUTOR_BACKEND=agy-interactive`. Print-mode `agy` stays the default for
`agent-agy`; #1172 auto-resume and #1158 detection are unchanged on that
path. The interactive backend drives `agy` over a PTY (pexpect preferred;
tmux `send-keys` fallback), submits the task prompt with an explicit trailing
newline (turn submission requires Enter — validated in the #1146 experiment),
streams the transcript into the attempt progress record, and ends with
`/exit` when the `>` prompt returns. Approval posture is fail-closed: it
pre-seeds `~/.gemini/antigravity-cli/settings.json` (override with
`UA_AGY_SETTINGS_PATH`) with class-level `command(git|pytest|ruff|gh)` allows
from the #1146 allowlist, and the menu driver auto-approves only those
classes — unknown or destructive commands are refused. The status-line
"AI: Out of credits" string observed in interactive sessions is cosmetic (AI
Studio leg label); subscription turns still carry the session. Prefer this
backend when print-mode teardown is the binding constraint and the task's
command classes fit the allowlist; keep print mode as the fallback otherwise.

**Dispatch skill note.** When filing or routing an agy dispatch that must
survive long foreground work (pytest, multi-step git/gh delivery), prefer
`UA_EXECUTOR_BACKEND=agy-interactive` on the agy executor unit over relying
on print-mode + auto-resume alone. Do not widen the interactive allowlist
without a new reviewed experiment; do not set
`UA_AGY_SKIP_PERMISSIONS=1` as a substitute.

Native CLIs use subscription auth from that CLI's own credential store. Cursor,
Copilot, and Devin additionally load their own hub tokens through
`agent-with-hub`, `copilot-with-hub`, and `devin-with-hub` respectively. All
three ship in `scripts/`; install them on each host that dispatches (along with
the interactive `cursor-with-hub`, `code-with-hub`,
`github-copilot-desktop-with-hub` and `opencode-with-hub` launchers) with
`scripts/install-with-hub-wrappers.sh` (`--dry-run` to preview; default
destination `~/.local/bin`). None of these backends inject an API key. Do not point dispatch at a shared cloud/API
SDK as a stand-in for a missing persona.

**agent-devin registration.** Identity label: `agent-devin`; invocation:
`devin-with-hub --permission-mode accept-edits -p "<prompt>"`; MCP servers:
`unified-agent` and `gbrain`. `devin-with-hub` reads `~/.devin/unified-agent-token`
(override with `UA_DEVIN_TOKEN_FILE`) and exports it as `UA_AGENT_BEARER_TOKEN`
before exec'ing `devin`, isolating the child from whatever bearer the launching
parent process carries — defense-in-depth: `~/.config/devin/mcp_config.json`
today bakes agent-devin's token as a static header rather than reading
`UA_AGENT_BEARER_TOKEN` from env, so this wrapper protects against that config
ever being regenerated to interpolate the env var the way Cursor's and
Copilot's already do (see task #1168). Devin's `-p` plus `--permission-mode
dangerous` would auto-approve every tool. That mode is refused: Devin has no
sandbox switch, so dispatch and the executor do not spawn
(`harness_sandbox_unavailable:devin`). Leave `UA_DEVIN_PERMISSION_MODE` unset;
the default `accept-edits` is the worktree-edit mode.

Operator step: mint and rotate a dedicated gbrain identity/token for `agent-devin` through the gbrain operator flow, replacing the legacy `antigravity` identity; do not copy token values into this repository.

### Running a agent-codex executor

```
cp deploy/unified-agent-executor-codex.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unified-agent-executor-codex
journalctl --user -u unified-agent-executor-codex -f
```

The startup line states the pair explicitly, which is what you check first when two
executor units are installed:

```
executor starting: agent=agent-codex backend=codex shadow=False
```

Prerequisites: `codex` on `PATH` and logged in (`codex login`), and a `agent-codex`
bearer token the hub knows, supplied as `UA_EXECUTOR_TOKEN` via
`~/.config/unified-agent/codex-executor.env` (chmod 600). Without a valid token the
daemon refuses to start rather than executing outside the hub's auth and approval
boundaries.

Two executor units coexist safely because each polls `delegate_list` filtered by its
own `UA_EXECUTOR_AGENT` label. Stop one with
`systemctl --user disable --now unified-agent-executor-codex`; the agy executor is
unaffected.

On a Mac hub, the same daemons are LaunchAgents under `deploy/mac-hub/`
(`com.unified-agent.executor-agy`, `com.unified-agent.executor-codex`). Use
`launchctl print gui/$(id -u)/<label>` instead of `systemctl`. Doctor and the
Control Center executor-health check map `unified-agent-executor.service` to
the **Agy** label only; Codex liveness is a separate `launchctl` check. See
Mac hub topology.

**Executor memory ceilings (task #1251, C2).** Both `run-executor-*.sh`
entrypoints set `ulimit -v` — RLIMIT_AS, the only memory limit the macOS
kernel enforces (RLIMIT_RSS is ignored) — on the executor and every child it
spawns. The default is 16 GiB (`UA_EXECUTOR_MEM_CEILING_KB=16777216`), a
runaway brake rather than a working-set fit: the daemon itself sits near
200 MB and CLI children typically stay far under the cap; a Node/Bun child's
large sparse reservations are why the cap is generous. Override per executor
in its sourced env file (`~/.config/unified-agent/executor.env` /
`codex-executor.env`); `0` disables. Restart-on-breach is the posture, not a
page: a child that hits the ceiling dies on malloc failure and the
LaunchAgent's `KeepAlive=true` brings the daemon back.

### Why the Codex backend does not need `danger-full-access`

Codex 0.145.0 auto-cancels MCP tool calls under `read-only` and `workspace-write`,
succeeding only under `--sandbox danger-full-access`. The obvious reading is that a
Codex executor therefore needs full access to reach unified-agent — it does not.
The daemon performs every task, memory, and journal write itself over its own
authenticated hub client, so the child Codex process only ever has to edit files in
its worktree. `workspace-write` is sufficient and is the default.

Raising the posture consequently requires two independent settings —
`UA_CODEX_SANDBOX=danger-full-access` *and* `UA_CODEX_ALLOW_FULL_ACCESS=1` — because
removing the sandbox for every model-generated shell command is an operator's
security decision, not a default. Setting only the posture stops the daemon at
startup with an explanatory error.

### One-off runs vs. the service

The service is a polling loop over delegations. For a single ad-hoc task, run
`codex exec` directly instead of enabling the unit — that path is outside the
task/worktree/review lifecycle, so nothing annotates `[waiting-for: review]` and
nothing records a registry session.

## Post-deploy: re-list tools (schema fingerprint, task #1169)

Every hub deploy can change the MCP tool-schema surface (new args, renamed
kwargs, stricter required sets). Long-lived MCP clients — Claude Code,
Antigravity, Cursor, Codex, Hermes, the TypeScript `UnifiedAgentClient` —
cache the tool list from **connection time**. After the hub restarts under
them they keep calling the *old* shape: unknown kwargs are stripped client-side
before the request reaches the hub, so server-side `extra=forbid` never fires.
That was the root cause of the #1165 vault append-clobber class of bugs.

**Rule:** after every hub deploy (or any `systemctl --user restart unified-agent`
/ LaunchAgent reload), every connected client must **re-list tools or reconnect**
before further **write** calls (`vault_write`, `memory_save`, `task_update`,
`fs_write`, …). A read-only `mcp_health_telemetry` check is not enough by itself
— it only *detects* drift; the host still has to refresh its cached schemas.

### Exact check

Capture the fingerprint at session start, then re-check before writes:

```bash
# From a checkout that can import the installed hub package:
uv run python -c "
from unified_agent.tools.mcp_health import mcp_health_telemetry
from unified_agent.tool_schema_fingerprint import SchemaDriftGuard

guard = SchemaDriftGuard()
guard.capture(mcp_health_telemetry()['schema_fingerprint'])
print('session', guard.session_fingerprint)

# … after a suspected deploy / before a write …
fresh = mcp_health_telemetry()['schema_fingerprint']
result = guard.check_before_write(fresh)
print(result)
if result['relist_required']:
    raise SystemExit('schema drifted — reconnect MCP host or tools/list before writes')
"
```

Via MCP (any connected agent): call `mcp_health_telemetry()` and read
`schema_fingerprint`. Compare to the value captured at session start. On
mismatch, reconnect the host (or force `tools/list`) and capture the new
fingerprint before writing.

Python clients can use `unified_agent.tool_schema_fingerprint.SchemaDriftGuard`
the same way; on drift it logs a warning and returns
`action="relist_tools"` / `relist_required=True`.

See also [troubleshooting.md](troubleshooting.md#stale-stdio-snapshots-after-merges--tool-changes)
for the restart-and-reconnect sequence.

## Relation to the other automation

| Layer | Trigger | LLM? | What it does |
|---|---|---|---|
| pre-commit hook / CI | every commit / push | no | `unified-agent-audit --fail-on high`, pytest, ruff |
| `steward-audit-cron.sh` | daily 09:00 | no | `unified-agent-audit surfaces --fail-on high --file-tasks` — A–F grade, allowlisted agent surfaces, `[audit-triage:]` tasks |
| **`review-pipeline-cron.sh`** | **every 15 min** | **no** | **reconcile merged PRs + validate finished branches, hold for the claimant's merge** |
| `anomaly-check-cron.sh` | every 30 min | no | `check_anomalies_and_notify()` — deterministic rules above, 2h notify cooldown |
| `/steward*` skills | on demand | yes | triage, planning, inbox synthesis |
| Active Executor (`pi`/`codex`) | polls delegations | yes | claims delegated tasks, executes in a worktree, holds at `[waiting-for: review]` |

Future work tracked separately: plan primitives (task 61), delegation/workflows/approval
queue (task 62).

### Steward audit alert failure diagnostics

On a notifier failure, `~/.unified_agent/audit.log` records `run_id`,
`notify_exit`, `error_kind`, `started_at` and `finished_at` with UTC timestamps.
The scheduled launcher supplies a validated run ID; manual or invalid IDs use
the literal `manual`. A delivery failure still exits 2 and retains the failed
delivery status; successful alert receipts and deduplication keep their existing
behavior.

`error_kind` is one of `rate_limited`, `timeout`, `configuration_error`,
`authentication_error`, `transport_error` or `sender_failed`. Exit 124 means
`timeout` even with empty stderr. Other labels are best-effort hints from
recognized error prefixes on the first stderr line, including `send:` and
`notify.sh:` framing. Text elsewhere, including echoed message phrases, cannot
choose a category; unknown output stays `sender_failed`. Use `notify_exit` and
the correlated run record when investigating rather than treating a category
as proof of an upstream provider's state.

Only a 4096-byte stderr prefix is captured in memory; remaining bytes are drained
so the notifier can finish without a broken pipe. Sender stdout is discarded.
No raw stderr file, message body, recipient handle or credential is persisted.
These diagnostics describe future attempts and cannot recover errors discarded
by earlier wrapper versions. Inspect existing records first; a new notification
attempt is a separate operational action.

## Lane orphan reaper (task #1316)

`unified-agent-lane-reaper` classifies and SIGTERMs orphaned `codex-linux-sandbox`
and lane-scoped `pytest` children that survive dead Codex lanes (reparented to
PID 1 / systemd). Dry-run is the CLI default; the desktop user timer
`ua-lane-reaper.timer` runs `--kill` on `OnCalendar=*:0/15` (plus `OnBootSec=5min`); `Persistent=true` catches up missed calendar ticks after sleep/boot. Only sandboxes/pytest
workers whose cwd or argv sits under a known lane worktree root are targets;
processes with a live Cursor / Claude / Codex / Agy / OpenCode ancestor are
never targeted. Before SIGTERM the reaper re-checks `/proc/<pid>/stat`
starttime so a reused PID is skipped.

### Desktop install (units are copied, not linked)

From a checkout that contains `deploy/systemd/ua-lane-reaper.*`:

```
mkdir -p ~/.config/systemd/user
cp deploy/systemd/ua-lane-reaper.service deploy/systemd/ua-lane-reaper.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ua-lane-reaper.timer
systemctl --user status ua-lane-reaper.timer
```

Smoke without enabling the timer: `unified-agent-lane-reaper` (dry-run) then
`unified-agent-lane-reaper --kill` once.
