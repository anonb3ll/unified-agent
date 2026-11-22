# Connector activation

Horizon 2 currently exposes six static package descriptions: Slack, Drive,
Obsidian, Readwise, Granola, and `github-app`. The package list is a catalog
of allowed default scopes. It does not prove that a provider is configured,
connected, or used, and it does not mint credentials or start a provider
session.

The package catalog is separate from the existing dedicated consumers. Slack
has dedicated inbound and outbound tools, Obsidian has its own runtime tools,
and `github-app` has a dedicated poll-first module (`github_app.py`, task
#1201) gated off by `UA_GITHUB_APP_ENABLED`. Drive and Readwise have no
verified live consumer in this checkout. Their presence in the catalog must
therefore be treated as dormant packaging, not as an enabled integration.
Granola likewise has no verified live consumer.

## `github-app`: App vs. operator-user-token mode (task #1261)

A real GitHub App requires a human to click through GitHub's app-creation
flow before the connector can dogfood anything, so `github_app.py` also
accepts an operator user token as a stand-in. `UA_GITHUB_TOKEN_MODE` (`app`,
the default, or `user`) selects between them:

- **`app`** — the original #1201 shape. The Checks API needs a GitHub App
  installation token. The worker mints one from `GITHUB_APP_ID` and
  `GITHUB_APP_PRIVATE_KEY`, scoped to `UA_GITHUB_CONNECTOR_REPO`, once per
  poll; a provider reused across calls keeps its token until shortly before
  it expires. A caller may still inject its own
  (`build_transport(app_installation_token=...)`).
  Manifest capabilities: `poll`, `check_runs`, `evidence_comments`.
- **`user`** — an operator token read from `UA_GITHUB_USER_TOKEN` (name
  configurable via `UA_GITHUB_USER_TOKEN_VAR`), falling back to `gh auth
  token` on the hub host. The Checks API is unavailable to a plain user
  token, so the check-run lifecycle is mapped onto commit statuses instead
  (`POST /repos/{o}/{r}/statuses/{sha}`, context `Approval Ledger`,
  `pending` while awaiting approval, `success`/`failure` mirroring the
  approval outcome, `error` for a check that didn't reach a normal
  conclusion — see `github_app._commit_status_state`). The check-run name
  (`Approval Ledger: awaiting human approval`) is not a status context
  in this mode; GitHub shows only the fixed `Approval Ledger` context.
  Evidence-comment posting is unchanged. Manifest capabilities:
  `poll`, `commit_statuses`, `evidence_comments`.

Both modes see the same `env var → dict` shape via
`github_app.github_app_manifest()`; `required_secrets` always names an env
var, never a value. **Security note:** give the operator token the least
privilege that works — `repo:status` plus issue/PR comment write — and
nothing broader. The token value is never logged, put into an exception, or
written into a manifest or evidence comment; only the configured env var's
NAME is ever surfaced, matching the `required_secrets` convention in
[connectors.md](connectors.md). On successful resolution, the hub logs
`github_user_token_source=env:<variable name>` or
`github_user_token_source=gh_auth_token` without logging token bytes.
A `gh` fallback may use a broader credential than the intended named env
var, so inspect that source signal when auditing the connector. User mode
must use `GitHubUserTokenTransport` from `build_transport`; a duck-typed
substitute bypasses its status lock and supersession checks. Full env var
reference:
[configuration.md](configuration.md).

### Registration, status, and rollback (task #1264)

`github-app` is a registered `connectors.ConnectorManifest`
(`GITHUB_APP_MANIFEST` in `connectors.py`), not just the mode-aware dict
above. That means:

- `system_doctor()` reports a `connector:github-app` check: `DORMANT` (no
  active Horizon 2 registration, or a named secret missing), `LIVE`
  (registered, secrets present), `MISCONFIGURED` (entry points broken, or
  secrets missing on an active registration), or `UNKNOWN` (doctor can't read
  the hub service's environment). See
  [connectors.md](connectors.md#system_doctor-and-the-integrations-page).
- The Integrations page (`/ui/settings/integrations`, `GET /api/connectors`) lists a `github-app` row with
  both its pre-existing Horizon 2 `scopes` (`read`, `checks`, `comments`) and
  the manifest's `capabilities`/`required_secrets`/`secrets` fields.
- `uv run pytest tests/connectors/test_github_app_conformance.py -q` runs the
  connector against the same five-clause harness every registered connector
  passes (see [connectors.md](connectors.md#the-five-conformance-clauses)).

**Setup, both modes:**

1. App mode (default): set `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` on
   the hub service environment. The key is an unencrypted RSA PEM; an env
   file holds it on one line with literal `\n` between its lines. The App
   must be installed on `UA_GITHUB_CONNECTOR_REPO`.
2. User mode: set `UA_GITHUB_TOKEN_MODE=user` and either export
   `UA_GITHUB_USER_TOKEN` (or point `UA_GITHUB_USER_TOKEN_VAR` at a
   differently-named env var already holding one), or leave both unset and
   let `resolve_user_token` fall back to `gh auth token` on the hub host.
3. Either way, the connector still does nothing until
   `UA_GITHUB_APP_ENABLED=1` is also set — see rollback below.

**Single-repo dogfood consumer (#1262):** the existing Mac `pr-watch`
LaunchAgent runs `github_app_worker.run_once()` after its memory/journal poll.
The worker reads only open PRs targeting `main` in
`anonb3ll/unified-agent`; it records one pending Approval Ledger status per PR
head, then resumes that exact event after the named human resolves its approval.
Set `UA_GITHUB_CONNECTOR_AGENT_TOKEN` to the bearer registered in
`UA_AGENT_TOKENS` for the worker's service identity, `approval_ledger_worker`
(task #1867). It verifies the bearer-to-identity mapping before any GitHub
call. Approval rows, pending-status records and signed evidence carry that
label. Until task #1867 the worker borrowed the `agent-codex` bearer, so its
rows showed `requested_by=agent-codex`; that bearer is still accepted, with a
warning on every poll, until the hub's `service.env` is migrated:

1. Mint a token and add `approval_ledger_worker:<token>` to `UA_AGENT_TOKENS`
   (and to `UA_AGENT_LABELS` if the hub runs a closed roster).
2. Point `UA_GITHUB_CONNECTOR_AGENT_TOKEN` at the same token.
3. If the `require_approval` rule for `github_app.process_pr_event` in
   `UA_POLICY_FILE` names `roles`, add `approval_ledger_worker` to them.
4. Restart the hub (the poll-now route runs in the hub process) and run
   `unified-agent-review ledger poll` once to check.

**Review-head check credentials (task #2320).** The poll publishes `unified-agent/review-head` through the same transport as the
ledger ([rule](automation.md#the-unified-agentreview-head-check-2320)), so it
needs no credential of its own, and it has this path's limits:

- **User mode** (`UA_GITHUB_TOKEN_MODE=user`) is the path that works today. The
  check is a commit status under its own context, never the `Approval Ledger`
  one. It needs `UA_GITHUB_USER_TOKEN` (or the hub's `gh` CLI login) with
  `repo:status`, or a fine-grained token with **Commit statuses: write**.
- **App mode** posts a real check run and needs the App's **Checks: write**
  permission. Set `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`; the worker
  mints the installation token itself. The token covers the check and the
  evidence comments only: the poll still lists and reads pull requests through
  the `gh` CLI, so the host also needs a `gh` login.

Branch protection accepts either form as a required check named
`unified-agent/review-head`. Until it is required, the check is advisory.

**Cadence (task #1867).** The `pr-watch` job decides each launch what is due,
from `$UA_HOME/pr-watch-schedule.json`. The legacy journal and the ledger poll
run every 1800 s. While a ledger row the worker can still act on exists, the
ledger poll is also due every `UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S` (default
120 s). Such a row is `pending`, or `approved` and inside its pass TTL, for a
PR head the last poll saw open and current
(`$UA_HOME/approval-ledger-open-heads.json`, trusted for at most 3600 s). A row
for a closed PR or an older head, an expired pass, or a stale snapshot (polls
failing) does not count, so the job falls back to 1800 s. A pending policy
correction (an unfinished publication with a
`connector.github.policy_withdrawn_after_post` marker: a success public under a
withdrawn authorization) also keeps the active cadence until it lands. These are local
reads; no GitHub call. A launch with nothing due exits without calling GitHub
or touching the run record; a failed poll is not stamped, so the next launch
retries it. Each live launch holds `$UA_HOME/pr-watch.lock` (non-blocking), so
an overlapping launch skips instead of running the same tick twice. The LaunchAgent's
`StartInterval` is 120 (since 2026-11-17, task #1867) so the faster cadence can
run; set it back to 1800 and every launch is due, the behaviour before #1867. A brand-new PR with no row yet
is still found by the next idle poll, or at once with poll-now below.

**Orphan rows (task #2103).** The poll finds a closed PR through the pending
status it posted for each head. A row whose pending-status POST failed has no
such marker, so before task #2103 it stayed in the approval queue after its PR
closed. Each poll now also looks at the live rows that no marker names: those
still `pending`, and `approved` rows past their pass TTL. A live pass inside
its TTL is kept until it expires. For each such row whose PR is not in this
poll's open list, the poll reads the PR from GitHub. It reads at most 10 PRs
per poll and reuses any read it already made. PRs it has never checked go
first, then the least recently checked, recorded in
`$UA_HOME/approval-ledger-orphan-checks.json`. A row the poll can never retire
(a PR whose read keeps failing, or one open at the row's head) therefore moves
to the back, and cannot hold a newer orphan out of the reads. The file is
disposable: one larger than 1 MiB or unparseable is ignored, and an entry that
is not a positive PR number with a finite time no later than a day from now
counts as never checked, so a corrupt file never fails the poll. The row is set
to `expired` when GitHub says the PR merged or closed, or that it is open at a
different head. The same transaction records a
`connector.github.orphan_retired` event naming the approval, PR, row head, PR
state and head, the previous status, and the reason. A row for the head that
merged first goes through the policy-authorized close-out used for a merged
head no poll saw. If a verified, non-red-tier policy authorization covers that
head and no human denied any row of the PR, the poll first commits the
decision in one transaction. That transaction reads every row of the PR and
refuses on any denial. It expires the row (with outcome `policy_authorized`
and the policy reference on its orphan event) and every other `pending` row of
the PR (listed as `fenced_rows`), and writes a `connector.github.policy_publication`
intent, unique per head and authorization. A denial that commits first wins.
A later one is refused, because no row of the PR is still pending and a merged
PR gets no new rows. The poll then posts the policy-authorized success status
and records `connector.github.policy_authorized` citing the row. If the post
or the outcome write fails, a later poll finishes the publication. It reads
GitHub first: commit statuses or check runs for the exact head, paged to the
end (a bound App check is read by id). It posts only when the success is
proven absent. App runs are matched only by the `external_id` the poll gives
each policy run; a run with the same title but another or no `external_id`
could be another App's and proves nothing. The App check-runs-for-ref
endpoint only covers a head's newest 1,000 check suites, so with more suites (counted before and after the
listing) absence is not proven. Created policy runs carry an `external_id`
naming the decision, and a run with that id in any other state is ambiguous.
A search that cannot finish within 10 pages, a suite window that is too large,
or an ambiguous run all leave the publication open rather than posting. If the success is already on GitHub
and its POST was verified (below), only its outcome is recorded. A success on
GitHub without that record is uncertain: its response was lost, its post-POST
check raised, or its withdrawal marker failed to write. The poll re-verifies
it now. If the authorization still holds, the poll records it as verified and
then records the outcome. If not, it is corrected as described below, on the
same App run, whose id the lookup recovers. Otherwise the authorization is re-verified
immediately before every POST, the first attempt included (it is also
re-verified just before the decision commits, so a withdrawal during the
changed-files read commits nothing). From that last check through the POST the
poll holds the hub's credential fence (`$UA_HOME/credential-authority.lock`),
with a 10 s request timeout. Every hub bearer change takes the same fence:
managed token issue and rotation, any write or delete of an `agent-token:`
managed secret (including an approved generic `managed_secret_set`), and
`unified-agent init` rewriting the token file or rolling it back. The fence is
re-entrant within one thread and is always taken before narrower locks. A
remote POST cannot be atomic with a local check, so the authorization is
verified once more straight after the POST. When it still holds, the poll
records `connector.github.policy_postverified` before the outcome. A change
outside hub code (a hand-edited token file, or a restart with new environment)
can still land in that one-request window. When it does, the poll records
`connector.github.policy_withdrawn_after_post`, then publishes a corrective
`cancelled` conclusion ("Policy authorization withdrawn after publication") on
the same status or check run, and closes the publication. A correction that
fails is retried by the next poll, which never posts the success again. When it no longer verifies (for example, its authorizer was
removed from `UA_POLICY_AUTHORIZERS`), or a human denied the PR, the
publication is cancelled on `connector.github.policy_withdrawn` and nothing is
posted. The head then closes out as it would have without a policy. Up to 5 unfinished publications
are retried per poll, in rounds in commit order. Up to 4 of those slots go to
the correction queue: a success already public under a withdrawn
authorization, and any publication whose authorization no longer verifies
(its success may be public with the record of it lost). The rest go to plain
retries, so an open plain retry gets at least one slot every poll; with none
open, corrections use all 5. Which queue is attempted first alternates every
poll, and the switch is saved before the first attempt, so a retry that
keeps killing the worker in one queue cannot keep the other from its turn. A round
covers the publications open when it started, so one committed later waits
for the next round and new arrivals can never overtake older work. Each
queue's position (the last publication attempted, and the newest one in its
round) is saved immediately before each attempt. A retry that fails, or even
kills the process, therefore costs only its own turn in the round. The two
positions live in `$UA_HOME/approval-ledger-policy-retries.json` (at most 4 KiB,
whatever the backlog). They are event-log ULIDs, not timestamps, so a
wall-clock step cannot reorder them. An unreadable file starts a fresh round
at the oldest publication. A correction that arrives mid-round waits out the
current round and then its place in the next, at most ceil(A/4) + ceil(B/4)
polls that complete their attempts, for A publications left in the round and
B ahead of it in the next. When attempts kill the worker, each queue still
leads every other poll and attempts at least one retry then, so the bound
becomes A + B leading turns, up to twice that in polls. These are bounds in
polls, not in time. With 40 queued corrections the first bound is about 20
polls for the newest. A pending correction (a marked public withdrawn
success) keeps the poll at the 120 s active cadence, so that is about 40
minutes. Placing retries in queues verifies the event hash chain once per
poll, not once per publication. A verification that fails or raises is
shared the same way, and puts every retry in the correction queue. The shared
result is used only by the thread and task that sort the queue, and only
while they do; every POST still re-verifies its authorization afresh. The same intent, denial re-check and reconcile-before-post
now apply to any merged head no poll saw, and to a merged head whose own row
has a pending status. That row is expired in the committing transaction before
its status is completed. A PR that is open at the
row's head keeps the row, even when it is missing from the open list (for
example, after a retarget off the ledger base). So does a GitHub read that
fails or returns nothing; the poll reports it as `orphan_unverified`, not as
an error, so it never fails the poll or pins the retry cadence. A human
decision made during the poll wins, because the update only applies to the
status the poll read. Apart from that policy status, no GitHub status is
posted, because the row never had one. A PR that reopens at the same head gets
a fresh row.

**Poll now (task #1867).** `unified-agent-review ledger poll` from any machine
with `UA_HTTP_HUB_URL` and an operator bearer asks the hub to run one ledger
poll immediately (`POST /api/approvals/ledger-poll`, the
`coordination_manage(action="approval_ledger_poll")` action). It needs no SSH.
Only `UA_LIFECYCLE_OPERATORS` and the verified operator may call it, with the
same profile ceiling and policy gate as the re-request below. It is the
scheduled poll, under the worker's identity and poll lock: it requests rows and
consumes only passes a human already approved; it never resolves an approval.
Each call is recorded on `agent.github_app.poll_requested` with the operator
and a per-status summary. The service's `UA_POLICY_FILE` must contain a
reviewed `require_approval` tool rule for `github_app.process_pr_event`.
Without that rule the connector cannot produce a consumed approval or signed
comment. Manual catch-up uses
`python -m unified_agent.github_app_worker` from the serving checkout with
the same service env. The worker is inert while `UA_GITHUB_APP_ENABLED` is off.

**Prompt status (task #2234).** The ledger is poll-only: the hub has no
public GitHub webhook ingress (its Funnel slice serves `/mcp` and OAuth only),
so two waits are shortened around the poll instead. First, when a human
approves or denies a `github_app.process_pr_event` row on the HTTP hub,
`approval_resolve` hands that row to a background worker
(`github_app_fastpath`) after the decision commits. It runs the poll's own
per-PR step for that PR at once, under the worker's service identity and poll
lock: it reads the PR fresh, retires rows for older heads, and advances the
live head (success with the signed evidence comment, or the denial's failure).
A head that is no longer the PR's head is retired as `stale`, never approved.
Every path that publishes an approved head (this worker, the open-PR and
closed-PR steps of the scheduled poll, and its reconcile step) settles it the
same way, in `github_app._settle_approved`. It re-reads the PR and decides from
the approved head and base and the live head, base and state alone: success and
evidence when the PR is at the approved head and base, open, closed or merged;
`stale`, not final, when it is open at the approved head on another base;
`stale` and superseded, final, when the head moved or it closed on another
base; and a retry when it cannot be read. It decides again right before and
right after the success POST. The decided status is posted on every settle
until a final record exists (the `connector.github.retargeted` marker is a
record only). A success is always posted, and evidence follows only that
POST's own return. A `stale` post is skipped only when both our own record of
the last status GitHub accepted from us (`connector.github.status_posted`) and
the current GitHub status already show it, so a held head adds no status per
poll and a lagging read cannot finalize anything. A success stays visible only once its evidence is written: any
failure from the success POST on replaces it with `action_required` before the
error propagates, and if that fails too the next settle posts the decided
status again. The scheduled poll reconciles consumed approvals with no final
record by PR number, the 10 least recently attempted per poll, ordered by a
monotonic sequence number each attempt takes before it runs (saved in
`$UA_HOME/approval-ledger-settle-checks.json`, validated on read, pruned to
unsettled approvals; no clock is involved), so each is revisited within
ceil(N/10) polls. Beyond
that, a closed or retargeted PR, a failure, or a full queue (32 rows) is left to the
next poll, which repeats nothing already published. Second, on a launch past
the active interval with no row in flight, the job sends one conditional
request for the first page of open PRs on the ledger base, newest update
first (`If-None-Match`, stored with that page in
`$UA_HOME/approval-ledger-probe.json`). GitHub's `304 Not Modified` costs no
rate limit. The full poll runs when the page shows a head the last poll did
not see: a push, a new PR, or a closed one. A full page of 100, or one whose
`Link` names a next page, cannot vouch for the rest and always runs the full
poll, as does a probe that fails. This is what makes a two-minute launch
interval cheap, which is why the shipped `StartInterval` is 120. At 1800
every launch would be an idle-interval poll and the probe would never run.

**GitHub rate limits (#2237).** A `429`, or a `403` carrying `Retry-After`,
`x-ratelimit-remaining: 0` or GitHub's "rate limit" message, starts a wait for
that credential, keyed by transport kind (`app` installation or `user` token)
and API origin. The wait is the longer of `Retry-After` and, with the quota
exhausted, `x-ratelimit-reset`; 60 s when neither is usable; at most one hour.
Malformed values are ignored. The process holds the deadline on its monotonic
clock and records it in `$UA_HOME/approval-ledger-github-backoff.json` under a
file lock, where a deadline only ever extends. A deadline read back is bounded
to one hour of elapsed time, even after the wall clock steps back or the file
holds a far-future value. A file that cannot be written leaves the process's
own deadline in force; one that cannot be read backs off 60 s from its last
write instead of counting as clear. While a wait runs, every send through the
transport raises `GitHubBackoff` unsent, the poll returns
`{"status": "backoff", "retry_after_s": N}` without reading GitHub, and the
fast path defers to the poll. A backoff that starts mid-pass stops the pass;
only the request that reached GitHub uses up a retry or reconcile turn, so
unfinished work, stuck corrections included, resumes in order once the
window ends rather than every 120 s. A `403` with none of those signals is a
permission error and is raised as before.

**Re-requesting a missed row (#1854).** When a PR head has no approval row
(a missed poll, or a push the next 30-minute poll has not reached yet), an
operator can open it from the hub without host access:
`coordination_manage(action="approval_ledger_rerequest", params={"number": N,
"head_sha": "<current head>"})`, or `POST /api/approvals/ledger-rerequest`
with `{"number", "head_sha"}` from the Control Center. Only
`UA_LIFECYCLE_OPERATORS` and the verified operator on a recovery channel may
call it. The Control Center route runs the same `coordination_manage` action as
the MCP call, so its profile ceiling, policy rules and approval gate apply to
both (the verified operator passes the profile ceiling only). The hub reads the PR fresh, refuses a closed PR or a `head_sha`
that is no longer the head, and then does what a scheduled poll does for that
head, under the worker's service bearer and poll lock. It retires
older heads' rows, including rows whose pending status was never posted, and
queues one `github_app.process_pr_event` approval with its pending status
through the connector policy: a policy that denies the connector, or has no
`require_approval` rule for it, queues nothing and says so. It never resolves
the approval: the operator still approves the queued row. A head with a live
row is reported as `already_requested`; a missing pending-status binding is
repaired without resolving or consuming the row, and the row is found by the
head, so a title or label edit since it was queued does not hide it. The
normal worker also repairs a missing binding before consuming an approved
pass. A head whose newest row expired or was revoked, or is an approved pass
past its TTL (the re-request marks it expired), gets a fresh one, unless
an older pending, approved or denied row for the same head is still live;
that is refused, by the re-request and by the poll, and never bound or spent,
until the operator resolves or retires it in the queue. A head that closes or
merges in that state settles as closed or merged without approval. Each call
is recorded on `agent.github_app.rerequested` with the operator and the
outcome, including a call that fails after it started changing approval rows,
a refused closed, off-base or stale head (`not_open`, `wrong_base`,
`stale_head`), a failed PR read from GitHub (`read_failed`) and an unresolvable GitHub
credential (`token_unavailable`).
The hub process needs the same connector env as the worker
(`UA_GITHUB_CONNECTOR_AGENT_TOKEN` and the GitHub credential).

### Policy-authorized merges (task #1407)

The operator ruled (2026-10-02) that a merge covered by a recorded standing
authorization is its own evidence class rather than a failure. The ledger
now ends a merged head in one of three ways:

| Outcome | Commit status | Description |
|---|---|---|
| Human approval recorded | `success` | `Human approval recorded`, plus the signed evidence comment |
| Policy-authorized | `success` | `Policy-authorized: <policy_ref> by <label>`; no human evidence comment |
| Merged without authorization | `error` (unchanged) | `Merged without human approval` |

**The record.** `github_app.record_policy_authorization(repo, number,
head_sha, policy_ref, task_id)` appends a row to the hash-chained event log on
`connector.github.policy_authorization`. It runs under the caller's verified
identity (nothing in the arguments can name another authorizer), binds the
full 40-character head SHA, cites the policy by reference
(`deploy-steward-2026-10-01`, `operator-chat:<ref>`; never conversation text),
captures the hub task's current claimant and its branch, and is HMAC-signed with the
authorizer's own `UA_AGENT_TOKENS` bearer the same way producer evidence is. It
is not an approvals row, so it never appears in the human approval queue or
counts as a human decision. Task #1798 exposes it as the MCP tools
`policy_authorization_record` (the write above) and
`policy_authorization_list` (the recorded authorizations for a pull
request, newest first, each row carrying `valid` under the same checks the
ledger applies at merge time minus the red-tier file check; `valid_for_head`
answers for one exact head; signatures are never returned). Both
`UA_POLICY_AUTHORIZERS` and `UA_POLICY_AUTHORIZATION_ALLOW_SELF` are
deployment environment the operator sets on the hub — no agent, tool
argument or task can widen who may authorize.

**Who may record one.** Only labels in `UA_POLICY_AUTHORIZERS` (default empty,
so nobody). Human labels are refused there. Separation of duties: the task's
claimant must differ from the authorizer unless
`UA_POLICY_AUTHORIZATION_ALLOW_SELF=1`; a task with no claimant is refused,
because the author cannot be told apart from the authorizer. The claimant is
captured at recording time, so closing the task later (which stamps the
closer into `claimed_by`) does not change the answer. The task must have a
branch recorded (its worktree branch), and the ledger honours the record only
for a pull request whose head branch is that branch, so citing an unrelated
task's claimant does not establish separation for this pull request. The same
head and policy recorded for a second task is a second record.

**When to record it.** Recording is a pre-merge step: the steward records the
authorization for the exact head it is about to merge, while the task is
still live (`open`, `doing`, `waiting_review` or `blocked`), and closes the
task after the merge and deploy. A `done` or `cancelled` task is refused with
an explicit error, as is a live task with no branch. Completing a task
releases its worktree and clears its branch, and the closer is stamped into
`claimed_by`, so a closed row no longer says which pull request or author the
merge belongs to. The branch is never accepted from the caller. An
authorization recorded before the task closes stays valid after it closes,
because the claimant and branch are signed into the record.

**When the ledger honours it.** At close-out, for a *merged* head whose
approval row would otherwise end unapproved (pending, expired, revoked, or an
approved pass that expired), the worker looks for a valid authorization bound
to exactly that head. It re-verifies the signature against the current
bearer, the authorizer's current membership in `UA_POLICY_AUTHORIZERS`, the
self-authorization rule, the hash chain, the task's branch against the PR's
head branch and the absence of a human denial on any head of the PR, then reads the PR's changed files
(`gh api --paginate repos/<repo>/pulls/<n>/files`, or the brokered REST read)
and refuses on any path matching
`cycle_eligibility.RED_TIER_PATTERNS` (`reviewer` re-exports the same list). An
unreadable, empty or possibly truncated (3000-file) list fails closed. If all
of that holds, the approval row is retired with the policy as its reason, the
status turns `success` with the policy description, and the outcome is
recorded on `connector.github.policy_authorized` (with `evidence_class:
"policy-authorized"`, `human_approval: false` and the `task_id`). Otherwise the
merge settles exactly as before.

What a policy authorization never does: cover any other head of the PR,
override a human denial on any head of the PR, replace a consumed human approval (both are
recorded when both exist, and the human evidence wins), cover a PR closed
without merging, or satisfy red-tier scope, which still needs the human row.
A head merged before any poll recorded it is handled too: the merged head
receives its own terminal policy-authorized status, and only then are older
heads retired as superseded, so a failed status post or outcome write leaves
the older pending rows in place and the next poll retries.

**Rollback:** set `UA_GITHUB_APP_ENABLED=0` (or leave it unset — that is the
default) on the hub service environment and restart it. This is the real,
connector-specific gate `github_app.enabled()` checks before
`process_pr_event` does anything (`{"status": "disabled", ...}` otherwise);
it is independent of `GITHUB_APP_MANIFEST.default_enabled=False`, of any
Horizon 2 registration, and of whether the named secrets are present — none
of those alone turn the connector on. Unsetting `UA_GITHUB_TOKEN_MODE` (or
setting it back to `app`) reverts to the App-mode default without touching
`UA_GITHUB_APP_ENABLED`.

Every Horizon 2 package is unregistered by default. A package becomes an
active governed connector only after a human reviews its provenance and scopes,
approves the exact registration, and an authenticated caller records it in the durable connector
inventory. Authorization remains a separate one-shot approval bound to the
server, scope, task, and trace. Revocation preserves the registration with a revoked timestamp and excludes it
from the default active list.

The read-only inventory reports durable registrations from the already
initialized service database. It does not run startup initialization, create workspace directories, migrate
tables, or start synchronization/background embedding work. It still uses the
standard database connection, including its normal SQLite connection settings.
Missing or unavailable storage fails closed. The inventory is not evidence of
live provider activity; verifying that requires a separate approved end-to-end
consumer check.

## `evidence-export`: scheduled signed evidence export (task #1266)

Off by default. `unified_agent.audit.evidence_export_sink.run_once()` (and
`unified-agent evidence export`) return `{"status": "disabled", ...}` and
touch no storage until `UA_EVIDENCE_EXPORT_ENABLED=1` is set. See
[connectors.md](connectors.md#evidence-export-a-scheduled-outbound-connector-task-1266)
for the connector-contract writeup (manifest, idempotency, retention).

**Enable it:**

1. `UA_EVIDENCE_EXPORT_ENABLED=1`.
2. `UA_EVIDENCE_EXPORT_DESTINATION` — one of `local`, `gcs`, `s3`.
3. The destination's bucket/path selector:
   - `local`: `UA_EVIDENCE_EXPORT_LOCAL_PATH` (a directory; created if absent).
   - `gcs`: `UA_EVIDENCE_EXPORT_GCS_BUCKET` (+ optional `UA_EVIDENCE_EXPORT_GCS_PREFIX`).
     Requires the `google-cloud-storage` package (not installed by default —
     `pip install google-cloud-storage`) and standard GCS credential
     resolution (`GOOGLE_APPLICATION_CREDENTIALS`, workload identity, ...).
   - `s3`: `UA_EVIDENCE_EXPORT_S3_BUCKET` (+ optional `UA_EVIDENCE_EXPORT_S3_PREFIX`,
     `UA_EVIDENCE_EXPORT_S3_REGION`). Requires the `boto3` package (not
     installed by default — `pip install boto3`) and standard AWS credential
     resolution (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, a shared
     credentials file, an instance/task role, ...).
4. Evidence export signing must already be configured — `UA_EVIDENCE_SIGNING_KEY`
   or `UA_EVIDENCE_SIGNING_SECRET_NAME` (same variables `evidence_signing.py`
   and `unified-agent evidence report`/`verify` already use). `run_once()`
   raises `SigningUnavailable` rather than writing an unsigned export.
5. Optional tuning: `UA_EVIDENCE_EXPORT_ORG_ID` (default `default`, the object
   key's org/hub segment), `UA_EVIDENCE_EXPORT_WINDOW_DAYS` (default `1`, the
   number of inclusive UTC calendar dates ending on the last completed UTC
   date (default `1` exports yesterday; `0` is an empty report window; must be
   zero or greater),
   `UA_EVIDENCE_EXPORT_RETENTION_DAYS` (default `90`; `<= 0` disables
   retention deletion entirely). Both settings must be integers when set;
   invalid values fail the run instead of silently selecting defaults. The
   object date follows export time, independently of the requested report
   window, so retention measures artifact age.

The daily timer is configured for 02:00 local time and exports only completed
UTC dates, so the default one-day window includes the full prior UTC date,
including records created after the previous timer run. `Persistent=true`
causes systemd to run once after a missed firing; it does not replay every
missed day. There is no durable report watermark. After a multi-day outage or
a write that arrives after its date was exported, set
`UA_EVIDENCE_EXPORT_WINDOW_DAYS` to cover all affected completed dates on a
successful run. For example, with the normal destination/signing environment
loaded, run `UA_EVIDENCE_EXPORT_WINDOW_DAYS=4 unified-agent evidence export`
to cover the four completed dates ending yesterday. The overlapping export is
content-addressed and does not overwrite the prior signed pair; the recovery
regression in `tests/test_evidence_export_sink.py` verifies both markers and
signatures. Restore the regular setting afterward.

**Rollback:** set `UA_EVIDENCE_EXPORT_ENABLED=0` (or unset it — the default)
and restart whatever process runs the timer/CLI. Independent of
`EVIDENCE_EXPORT_MANIFEST.default_enabled=False` and of whether the
destination's bucket/path is configured — neither alone turns the connector
on.

**Scheduling** is shipped inert: `deploy/systemd/ua-evidence-export.{service,timer}`
(uses `EnvironmentFile=` directly) and
`deploy/mac-hub/com.unified-agent.evidence-export.plist.example` +
`deploy/mac-hub/run-evidence-export.sh` (launchd has no
`EnvironmentFile=`, so the plist execs this wrapper, which sources
`~/.unified_agent/service.env` before exec'ing the real binary — same
pattern as `run-pr-watch.sh`). Copy, edit, and install manually per each
file's header comment; nothing installs or enables them automatically. The
one real run against a customer's dev bucket is a manual operator step, not
part of this connector shipping.
