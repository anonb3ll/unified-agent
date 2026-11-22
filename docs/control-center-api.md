# Control Center API

The HTTP routes the Control Center v2 front end codes against. Each route is a
thin adapter over a registered hub tool: the handler calls the tool through
`webui.api.call_tool`, so the tool's profile ceiling, policy gate, approval gate,
rate limit and safety resolvers run exactly as they do over MCP. The spec is
`docs/designs/control-center-v2-spec.md` §6 and §8.

This file has one section per module. Task #2469 wrote the `knowledge` and
`work` sections; task #2480 added the plan structure, goal and operator
session-end routes to `work` and wrote `runs`; task #2470 wrote `agents`;
task #2516 added keyed writes, the task subtree, graph-run progress, the session
credential list and evidence paging (`settings`); task #2523 added the Agents
area reads to `agents`.

## Conventions

**Authentication.** Every route needs a hub identity: a session cookie from
`POST /api/session`, or `Authorization: Bearer <token>`. No identity: `401
{"error": "authentication required"}`.

**CSRF.** Every `POST` here is CSRF-guarded. A request that carries the session
cookie must send the token from `GET /api/whoami` (`csrf`) in the
`x-ua-csrf` header, or it is refused with `403 {"error": "invalid or missing
CSRF token"}` before the handler runs. Bearer callers send no token.

**Authorization.** Each `(method, path)` has one entry in
`webui/route_scopes.py`. Writes are `CAPABILITY` scopes naming the tool (or the
`plan_manage` action) they call, with `operator=True`: an agent identity must
have that tool in its profile, and the verified human operator passes the
profile ceiling. A caller that fails it gets `403 {"error": "<tool> is
unavailable to this profile"}` (or `"plan_manage <action> is unavailable to
this profile"`). Rules the tool enforces itself still apply to the operator:
vault write directories and the locked-note guard, memory overwrite, joint-plan
office membership, session ownership.

**Identity.** Identity arguments are never read from the body. They are filled
from the verified request identity: `memory_save.author`, `memory_promote.agent`,
`vault_journal.agent`, `task_claim.agent`, `plan_manage` `author` (create and
the joint actions) and `agent` (claim, handoff). A body may repeat the caller's
own identity in that field; naming any other value answers `400 {"error":
"<field> is taken from the authenticated identity"}`.

**Bodies.** A JSON object, at most 256,000 bytes. Unknown keys answer `400
{"error": "unknown field(s): ...; allowed: ..."}`. `null` counts as absent.
Strings, lists and objects have the limits in the tables below (objects: at
most 20,000 bytes serialized). Malformed JSON or a non-object body answers
`400`.

**Responses.**

| Status | Body | When |
|---|---|---|
| 200 | the tool's result, unchanged | the call ran |
| 202 | `{"status": "approval_pending", "approval_id": <int>, "code": "approval_required", "tool": "<tool>"}` | the approval gate parked the call |
| 400 | `{"error": "..."}`, plus `"code"` when the tool raised a typed error | bad input, or the tool refused |
| 401 | `{"error": "authentication required"}` | no identity |
| 403 | `{"error": "..."}` | CSRF, profile ceiling, or the agent-access switch |
| 404 | `{"error": "..."}` | the named object does not exist |
| 409 | `{"error": "...", "code": "conflict"}` | the tool reported a conflict. A joint plan write that lost the revision race also carries `expected_revision` and `current_revision`: reload the joint read and retry |

`tool` in a 202 is the registered tool whose gate parked the call (`plan_manage`
for every plan route).

**Replaying a parked write.** The approval gate keys a pass on the tool name and
the exact arguments the tool is called with. Every write in this file builds
those arguments only from the request body and, where the table below names
one, an identity argument filled from the signed-in identity. Absent optional
fields are left out (never defaulted by the handler). So repeating the
identical request after the approval is approved rebuilds the identical
approval payload and runs the write once; the pass is one-shot, and a further
repeat parks again. `tests/test_webui_cc_work.py::
test_identical_request_after_approval_runs_the_parked_write` and
`test_task_delete_keeps_its_approval_gate` prove this.

What an approved pass is bound to. The hub matches an approved row on tool,
payload, tenant and the **requester that queued it** (`tools/approvals.py`,
`_check_approval_gate`, task #2492):

* A route with an identity argument puts the signed-in identity into the
  payload. Only the same identity rebuilds it; anyone else's identical request
  builds a different payload and parks anew.
* A route without one builds the same payload for every caller, and each
  caller's request stands on its own. While caller A's approval is approved and
  unconsumed, another caller sending the identical body parks a request of its
  own — it never spends A's pass, and A's pass is left unconsumed for A.
* A pass is spendable by exactly two parties: **the requester that queued it**,
  and **the human who resolved it**. The second is the hub's human-in-the-loop
  shape — an agent asks, a person accepts the form or resolves the request, and
  that person's own session runs the call (elicitation, the GitHub App check,
  the Control Center writes). No other caller can spend it.
* A **denial** stays action-scoped: a human's refusal of that exact call keeps
  refusing identical calls from any caller, because a denial grants no
  authority. Only positive authority is bound to the requester.

A pass that named no requester stays unbound — that is the in-process path
where the caller's tool supplied no label. Gated tool calls over the hub always
name the verified identity, so their passes are always bound.

<!-- identity-args:start -->
| Write route | Identity argument in the payload |
|---|---|
| `POST /api/action/memory/save` | `author` |
| `POST /api/action/memory/delete` | none |
| `POST /api/action/memory/restore` | none |
| `POST /api/action/memory/promote` | `agent` |
| `POST /api/action/memory/feedback` | none |
| `POST /api/action/vault/write` | none |
| `POST /api/action/vault/journal` | `agent` |
| `POST /api/action/plan/create` | `author` |
| `POST /api/action/plan/update` | none |
| `POST /api/action/plan/checkpoint` | none |
| `POST /api/action/plan/resume` | none |
| `POST /api/action/plan/retry` | none |
| `POST /api/action/plan/claim` | `agent` |
| `POST /api/action/plan/handoff` | `agent` |
| `POST /api/action/plan/close` | none |
| `POST /api/action/plan/delete` | none |
| `POST /api/action/plan/joint/bind` | `author` |
| `POST /api/action/plan/joint/propose` | `author` |
| `POST /api/action/plan/joint/review` | `author` |
| `POST /api/action/plan/joint/decide` | `author` |
| `POST /api/action/plan/joint/recover` | `author` |
| `POST /api/action/task/claim` | `agent` |
| `POST /api/action/task/delete` | none |
| `POST /api/action/session/end` | none (the tool still refuses another identity's session after the pass is consumed) |
| `POST /api/action/plan/step-add` | none |
| `POST /api/action/plan/step-edit` | none |
| `POST /api/action/plan/step-remove` | none |
| `POST /api/action/plan/reorder` | none |
| `POST /api/action/plan/edit` | none |
| `POST /api/action/goal/edit` | `author` |
| `POST /api/action/goal/link` | `author` |
| `POST /api/action/goal/unlink` | `author` |
| `POST /api/action/goal/close` | `author` |
| `POST /api/action/goal/reopen` | `author` |
| `POST /api/action/session/operator-end` | `operator` |
| `POST /api/action/routine/add` | none |
| `POST /api/action/routine/pause` | none |
| `POST /api/action/routine/resume` | none |
| `POST /api/action/routine/run` | none; the `Idempotency-Key` header is in the payload |
| `POST /api/action/langgraph/run` | none; not resumable by replay (see the route) |
| `POST /api/action/agent/add` | `actor`; not resumable by replay (see the route) |
| `POST /api/action/agent/remove` | `actor` |
| `POST /api/action/connector/register` | none; caller-bound anyway: the tool puts the caller in the payload as `reviewed_by` |
| `POST /api/action/connector/register-package` | none; caller-bound anyway: the tool puts the caller in the payload as `reviewed_by` |
| `POST /api/action/connector/authorize` | none |
| `POST /api/action/connector/revoke` | none |
| `POST /api/action/org-proposal` | reviewed_by, applied_by, rejected_by (the one for the action) |
<!-- identity-args:end -->

**Which writes are resumable by identical replay** (spec §6). Every write in
the table above rebuilds the identical approval payload from the identical
request, with two exceptions the UI must mark `resumable: false`:

* `POST /api/action/langgraph/run` after a native graph approval: the UI
  continues it by sending the returned `run_id`. Repeating the first request
  starts a new run.
* `POST /api/action/routine/run` replays only with the same `Idempotency-Key`;
  the stored write must keep the key, as `useHubWrite` does.

### Keyed writes: the `Idempotency-Key` header (tasks #2515, #2516)

These routes accept an optional `Idempotency-Key` header and share one scheme
(`cc_knowledge.respond_once`, over `storage.request_receipt_*`, the receipt
mechanics Run now uses):

| Route | What a repeat with the same key does not do again |
|---|---|
| `POST /api/action/plan/create` | make a second plan |
| `POST /api/action/routine/add` | make a second routine |
| `POST /api/action/workflow` | run the workflow again (its earlier steps already ran) |
| `POST /api/action/langgraph/run` | start a second graph run |
| `POST /api/action/cycle` with `operation: "start"` | start a second cycle |
| `POST /api/action/session-credential-revoke` | lose what the first request revoked |
| `POST /api/action/vault/write`, `/vault/journal` | append twice (task #2515) |

Without the header each route behaves exactly as before.

* **Key.** 1-200 characters, no surrounding whitespace, no control characters;
  anything else is 400 and nothing runs.
* **Scope.** One receipt per tenant (`storage.idempotency_storage_key`),
  verified identity and key. Another identity's identical key is its own write;
  another tenant's is absent. The route's scope (capability, CSRF, identity)
  runs before the receipt is read.
* **Request binding.** The key is bound to a SHA-256 digest of the route's
  validated request (the arguments it passes on). The same key with a different
  request answers **409 `{"code": "conflict"}`** and runs nothing.
* **Recorded first.** The reservation is committed before the write starts, so
  a start that spawns work has its receipt on disk before anything is spawned.
* **Frozen answer.** The first 2xx answer that is not a parked approval is
  stored, and every repeat gets that status and body back. A graph run's native
  approval (202 with a `run_id`) started the run, so it is frozen too; continue
  it with `run_id` under a new key. A workflow that stopped at
  `pending-approval` is frozen: its earlier steps ran, so re-running it is a new
  key's decision.
* **Classified by phase, not status.** Every other answer is judged by whether
  the call crossed its execution boundary (`approvals.execution_probe`, which
  `mark_governed_execution` sets). Before it, the answer was a refusal with no
  effect (the route's own validation, the profile ceiling, agent access, policy,
  a parked approval): the receipt is dropped and the identical repeat runs again
  (after an approval, the same key continues the write). After it, any
  non-frozen answer, whatever its status (a tool error, a ledger or evidence
  write that failed after the tool ran, a halted graph), keeps the receipt as
  outcome unknown (below).
* **In flight.** A repeat while the first request is still running answers
  **409 `{"code": "in_flight"}`**; repeat shortly.
* **Outcome unknown.** When the first request raised, was cancelled, or is
  still unsettled after 10 minutes, the write may or may not have happened. The
  key is never taken over and never runs again: every repeat answers **409
  `{"code": "outcome_unknown"}`**, naming what to read to find out (the plan or
  routine list, the workflow's run history, the task's notes and plans, the
  cycle list, the session credential list). Use a new key to run it again.
* **Legacy receipt.** A receipt for the key written before tenant scoping, on a
  tenanted hub: 409 `{"code": "ambiguous_receipt"}`; use a new key.
* **Retention.** Settled receipts are kept 7 days and pruned on later keyed
  writes of the same route (at most 500 at a time); reservations and
  outcome-unknown receipts are never pruned.

Run now (`POST /api/action/routine/run`) keeps its own required key and
takeover rule, because `routine_fire` dedupes the fire itself. Launches
(`webui/cc_launch.py`) keep their attempt records, which also prove a launch
from evidence.

## knowledge (`webui/cc_knowledge.py`)

Task #2515 closed the Knowledge gaps: browse reads, note revisions and
compare-and-set, in-place restore, server-side memory filters, the vault lock
verdict and idempotent vault appends.

### `GET /api/memory/note`

Adapts `memory_get`. Scope: `CAPABILITY`, tools `memory_get`, operator.

| Query | Type | Notes |
|---|---|---|
| `key` | string, required, ≤ 200 | the note id |
| `as_of` | string, ≤ 64 | ISO datetime with offset; the note as valid at that instant |
| `include_versions` | `true`/`false` | adds `versions` and `versions_total` |

200: the full note: `id`, `title`, `content`, `tags`, `author`, `updated_at`,
`revision`, `salience`, `confidence`, `expires_at`, `superseded_by`,
`source_host`, `access_count`, `last_accessed`, feedback counts, `links: {out,
in}`, `confidence_posterior`, `deleted: true` for a soft-deleted note, and with
`include_versions` the prior versions. 404 for an unknown key. 400 for an
unknown or malformed query parameter.

**Opening a note does not count as a read** (task #2515). The route calls
`memory_get(count_access=False)`, so `access_count` and `last_accessed` are
the stored values and browsing never raises "Read N times" or the access
signal. Agents' own `memory_get` calls still count.

`revision` (task #2515) is `sha256:<hex>` over the note's whole edit state:
every column of the note except the access and feedback counters (so text,
tags, salience, confidence, expiry, evidence, epistemic and validity fields,
the supersede pointer and the deletion tags all count) plus its outgoing
links. Any edit moves it, even within the same second; reads and feedback do
not. Send it back as `expected_revision` on a save.

### `POST /api/action/memory/save`

Adapts `memory_save` (create, replace, append). Scope: tools `memory_save`,
operator, CSRF. `author` is the verified identity.

| Field | Type | Notes |
|---|---|---|
| `content` | string, required, ≤ 200,000 | |
| `title` | string ≤ 500 | |
| `tags` | string list, ≤ 50 items of ≤ 100 | |
| `key` | string ≤ 200 | an existing note id: replace (or append) it |
| `append` | bool | append to the note named by `key` |
| `overwrite` | bool | required to replace a note another author wrote |
| `check_only` | bool | report similar notes without writing |
| `salience`, `confidence` | number | |
| `expires_at` | string ≤ 64 | |
| `idempotency_key` | string ≤ 200 | |
| `links` | string list, ≤ 50 items of ≤ 300 | typed links, as `memory_save` takes them |
| `expected_revision` | string ≤ 80 | with `key`: the `revision` the edit was based on (task #2515) |
| `author` | must equal the caller if present | |

200: `{"id", "created", "revision", "similar": [...]}` (and the tool's other
fields); `revision` is the note's new revision.
400 when replacing another author's note without `overwrite: true`; the
replaced content is kept under a tombstone line in the note.

**Compare-and-set** (task #2515). With `expected_revision`, the save is
checked under the write lock: if the note changed since that read (another
agent's append or replace, or a delete), nothing is written and the answer is
`409 {"error", "code": "conflict", "expected_revision", "current_revision"}`.
The check runs before the deleted-note refusal, so a save from a copy read
before the note was deleted is also this 409, with the tombstone's revision.
Reload the note, merge, and save with the new revision. `expected_revision`
without `key`, or not of the form `sha256:` + 64 lowercase hex, is 400.
Without it a save behaves as before, and `append: true` keeps its semantics:
an append never needs a revision. A repeat of a committed save with the same
`idempotency_key` returns its receipt without the check.

### `POST /api/action/memory/delete`

Adapts `memory_delete` (destructive; its own verified-identity and approval
gates apply). Scope: tools `memory_delete`, operator, CSRF. Body: `key`
(required, ≤ 200). 200: `{"deleted": true}`.

### `POST /api/action/memory/restore`

Adapts `memory_restore`. Scope: tools `memory_restore`, operator, CSRF. Body:
`key` (required). 200: `{"id", "restored", "was_superseded_by"}`. 404 for an
unknown key.

**A deleted note is restored in place** (task #2515): same `key`, its content
and tags from before the delete, its links and version history kept (the
tombstone becomes one more version). 200: `{"id", "restored": true,
"was_superseded_by": null, "was_deleted": true, "restored_version",
"revision"}`, plus `superseded_by` when the note is also superseded (restore
again to re-promote it). The owner or the verified human operator may do it,
as with delete; anyone else answers 400. A note a verified human deleted
(its tombstone carries `meta:deleted-by-human`, or for an older tombstone the
deleting identity is human) can be restored only by a verified human: the
owner answers 400. A note deleted before version
history existed has nothing to restore from and answers 400 saying so; its
tombstone (`GET /api/memory/note`) still holds the text, to save as a new
note.

### `POST /api/action/memory/promote`

Adapts `memory_promote` (memory note to a Wiki page). Scope: tools
`memory_promote`, operator, CSRF. `agent` is the verified identity.

| Field | Type |
|---|---|
| `key` | string, required, ≤ 200 |
| `slug` | string ≤ 200 |
| `summary` | string ≤ 4,000 |
| `dry_run` | bool |
| `agent` | must equal the caller if present |

200: the tool's promotion result. 400 when the note is not eligible (for
example confidence below 0.70).

### `POST /api/action/memory/feedback`

Adapts `memory_feedback`. Scope: tools `memory_feedback`, operator, CSRF. Body:
`key` (required), `signal` (required, one of `helpful`, `not_helpful`,
`stale`, `wrong`). 200: the note with its updated feedback counts and
`signal`.

### `GET /api/memory` (filters, task #2515)

The existing browse and search read (`ANY_AUTHENTICATED` with
`read_tools=("memory_list", "memory_search")`). With `q` (or `query`) it runs
`memory_search`; without, `memory_list` newest first.

| Query | Type | Notes |
|---|---|---|
| `q` / `query` | string | search text; empty browses |
| `tag` | string, repeatable | every one required |
| `project` | string | adds the tag `project:<name>` |
| `author` | string | exact author label |
| `limit` | int 1..200 | search caps at 50 |
| `as_of` | string | search only |

Every filter goes to the tool and is applied before its cap, so a page is the
top of the filtered set, not a filtered slice of an unfiltered page. 200:
`{"mode": "search"|"browse", "query", "tag", "tags", "author", "limit",
"results", "items", "total"}`; `tags` is the full list required.

### `GET /api/vault/page` (lock verdict, task #2515)

The existing note read (`vault_read` plus `vault_links`) now carries
`locked`: the verdict `vault_write`'s guard reaches on the same bytes (true for
frontmatter `locked: true`, and true when the frontmatter is too long to
decide, since the guard fails closed). A write to a locked note answers 400
from `POST /api/action/vault/write`; show the note read-only instead of
guessing from its frontmatter.

### `GET /api/vault/list`

Adapts `vault_list`. Scope: tools `vault_list`, operator.

| Query | Type | Notes |
|---|---|---|
| `dir` | string ≤ 1,000 | vault-relative directory; default the vault root |
| `glob` | string ≤ 200 | |
| `max_entries` | int 0..500 | |

200: `{"entries": [{"name", "type": "file"|"dir", "size"}], "truncated"}`.
The vault resolver bounds the path; a path outside the vault answers 400.

### `GET /api/vault/links`

Adapts `vault_links`. Scope: the shared authenticated read (`ANY_AUTHENTICATED`
with `read_tools=("vault_links",)`), the same admission as `GET
/api/vault/page`. Query: `path` (required, ≤ 1,000). 200: `{"path", "outgoing":
[str], "backlinks": [str]}`. (`GET /api/vault/page` returns the same links with
the note's content.)

### `POST /api/action/vault/write`

Adapts `vault_write`. Scope: tools `vault_write`, operator, CSRF. The write
directories (`UA_VAULT_WRITE_DIRS` and any per-identity narrowing) and the
`locked: true` guard stay in the tool; a refusal answers 400 with the tool's
reason.

| Field | Type |
|---|---|
| `path` | string, required, ≤ 1,000 |
| `content` | string ≤ 200,000 |
| `mode` | `overwrite` or `append` |
| `template` | string ≤ 1,000 |
| `expected_revision` | string ≤ 128 (optimistic concurrency) |

The tool's `force` (write a note marked `locked: true`) is not accepted here:
sending it answers 400 as an unknown field. Unlocking a note is done in the
vault itself.

Takes an optional `Idempotency-Key` header; see *Idempotent vault writes*
below.

200: `{"path", "bytes_written", "mode", "append", "total_lines_after",
"revision"}`.

### `POST /api/action/vault/journal`

Adapts `vault_journal`. Scope: tools `vault_journal`, operator, CSRF. `agent` is
the verified identity. Body: `entry` (required: at most 240 characters once runs
of whitespace are collapsed to one space, the tool's cap from task #379, and at
most 4,000 characters as sent; a longer entry answers 400 before any approval
gate parks it), `date` (≤ 10,
`YYYY-MM-DD`), `tags` (≤ 20 strings of ≤ 100), `agent` (must equal the caller
if present). 200: `{"path", "entry", "created_note"}`. Takes an optional
`Idempotency-Key` header.

### Idempotent vault writes (task #2515)

`POST /api/action/vault/write` (any mode, append included) and `POST
/api/action/vault/journal` take an optional `Idempotency-Key` header (1-200
characters, no surrounding whitespace or control characters; otherwise 400).
Without it they behave as before. With it:

* The key is scoped to the hub's tenant (`storage.idempotency_storage_key`)
  and to the signed-in identity: two identities sending the same key make two
  writes. It is bound to the request that first used it, by a SHA-256 digest of
  the tool and its arguments.
* The first `200` answer is stored (`idempotency_keys`, tool
  `cc_vault_write` or `cc_vault_journal`) and every repeat of the same request
  gets it back, **without a second write**.
* Same key, different body: `409 {"code": "conflict"}`, nothing written.
* The reservation is committed before the write runs, and **an attempt is
  never run twice under one key**. Same key while the first request is still
  running: `409 {"code": "in_flight"}`; repeat shortly.
* **Outcome unknown.** An attempt that ended without an answer (the hub
  failed, or the request was cut off, after the write may have started), or
  one still unsettled after 10 minutes, answers `409 {"code":
  "outcome_unknown"}` on every repeat: it is never taken over and run again.
  Read the note (`GET /api/vault/page`) to see whether the write happened,
  and use a new key to write again. Unlike Run now, these receipts are not
  pruned until they settle.
* A receipt written before tenant scoping on a tenanted hub: `409 {"code":
  "ambiguous_receipt"}`; use a new key.
* **Any other answer is classified by phase, not by status code**
  (`approvals.execution_probe`, shared with task #2516). A refusal before the
  tool starts executing (profile ceiling, agent access, policy, a parked
  approval) drops the receipt, so the repeat runs again; after an approval the
  identical repeat with the same key performs the write once. Once the tool
  has started, any other answer (the tool's own refusal such as the
  locked-note guard, an error during the write, or a ledger write that failed
  after the append) keeps the receipt as `outcome_unknown`: the write may
  have happened, so the key never runs it again.
* **An I/O error after the write began** (task #2521: a full disk or a failed
  flush or close once the note was opened, keyed or not) answers `409 {"code":
  "outcome_unknown"}` on the first answer too, not a 400 refusal: the note may
  hold part of the entry, or an overwrite may have truncated it. Read the note
  before writing again. An I/O error before the note is opened (a directory
  that cannot be created, a note that cannot be opened) is a plain 400 refusal:
  nothing was written. Over MCP the same failures carry the typed code
  `outcome_unknown`.
* Settled receipts are kept 7 days. A new write needs a new key.

The keyed wrapper is `cc_knowledge.respond_once` (the one the Runs and Work
keyed routes share). Its receipt helpers are the ones `POST
/api/action/routine/run` uses (`storage.request_receipt_begin` /
`request_receipt_finish`), with `takeover=False` and
`request_receipt_abandon`. Run now keeps its takeover:
`routine_fire` checks its derived fire key and creates the task in one
transaction, so a takeover replays the first fire instead of firing again.

## work (`webui/cc_work.py`)

### Existing routes to reuse

| Need | Route |
|---|---|
| plan review (`intent`) | `POST /api/action/plan`, unchanged |
| task comments | `GET`, `POST /api/tasks/{task_id}/comments` |
| create a goal | `POST /api/goals` with `action: "add"` (its `action: "link"` stays; `POST /api/action/goal/link` is the CC2 route) |
| every `task_update` field, including `archived` | `POST /api/action/task` |

LangGraph runs and routines are in the `runs` section.

### `POST /api/action/task` — `archived`

The existing route now accepts `archived` (bool): `true` archives the task,
`false` unarchives it, through `task_update`. Its scope is unchanged (tools
`task_update`, operator, CSRF). A 202 from this route carries `status:
"pending"`, `tool: "task_update"` and `payload`, as before.

### Plan writes: `POST /api/action/plan/<action>` and `/api/action/plan/joint/<action>`

One route and one scope per `plan_manage` action, each declaring exactly that
action in `Scope.actions` (a scope requires every action it lists). All are
`CAPABILITY`, operator, CSRF, and call `plan_manage(action, params)`.

| Route | Action (scope) | Fields | Identity |
|---|---|---|---|
| `plan/create` | `plan_create` | `goal` (required, ≤ 4,000), `steps` (required list ≤ 100: strings or `{description, owner?, depends_on?}`), `scope` (≤ 100) | `author` |
| `plan/update` | `plan_update` | `plan_id` (required, ≤ 128), `step_idx` (required int), `status` (`open`, `doing`, `done`, `failed`, `blocked`), `note` (≤ 4,000), `owner` (≤ 200), `expected_step_id` (int, #2480) | |
| `plan/checkpoint` | `plan_checkpoint` | `plan_id`, `memory_ids` (≤ 100 × 200), `file_paths` (≤ 100 × 1,000), `outputs` (object), `note` | |
| `plan/resume` | `plan_resume` | `plan_id`, `from_checkpoint` (int) | |
| `plan/retry` | `plan_step_retry` | `plan_id`, `step_idx`, `reason` (required, ≤ 4,000), `expected_step_id` | |
| `plan/claim` | `plan_step_claim` | `plan_id`, `step_idx`, `expected_step_id` | `agent` |
| `plan/handoff` | `plan_handoff` | `plan_id`, `step_idx`, `outputs`, `note`, `notify_owner` (bool), `expected_step_id` | `agent` |
| `plan/close` | `plan_close` | `plan_id`, `reason` (required), `outputs` | |
| `plan/delete` | `plan_delete` | `plan_id` | |
| `plan/joint/bind` | `plan_joint_bind` | `plan_id`, `goal_id` (required int), `org_unit_id` (required, ≤ 200), `comment_id` (int) | `author` |
| `plan/joint/propose` | `plan_joint_propose` | `plan_id`, `expected_revision` (required int), `steps` (required list), `comment_id` (required int) | `author` |
| `plan/joint/review` | `plan_joint_review` | `plan_id`, `expected_revision`, `decision` (required, ≤ 32), `comment_id` | `author` |
| `plan/joint/decide` | `plan_joint_decide` | `plan_id`, `expected_revision`, `text` (required, ≤ 8,000), `comment_id` | `author` |
| `plan/joint/recover` | `plan_joint_recover` | `plan_id` | `author` |

Responses are `plan_manage`'s: `create` returns `{plan_id, steps}`; `update`
returns `{step, plan, next_step_idx}`; `checkpoint` returns `{plan_id, seq,
checkpoint}`; `close` returns `{plan, seq, checkpoint}`; the joint actions
return the joint view (see below). `delete` answers 404 when no plan matched.
Joint planning keeps its rules inside the tool, for the operator too: the
caller must be a member of the plan's office, a proposal needs an authored
comment on the plan's thread, and reviews follow the peer-review rule. A
refusal answers 400 with the tool's reason, e.g. `"scoped membership refused:
Desktop_Operator is not a member of org unit product-engineering"`, which the UI
can show as the reason an action is disabled.

### Plan structure (task #2480)

Five more `plan_manage` actions, one route and one scope each, exposed to
exactly the profiles that have `plan_update` (chief-of-staff, developer, CTO,
research; the operator through the route). All are `CAPABILITY`, operator,
CSRF.

| Route | Action (scope) | Fields |
|---|---|---|
| `plan/step-add` | `plan_step_add` | `plan_id`, `description` (required, ≤ 4,000), `position` (int; default the end), `owner` (≤ 200), `depends_on` (list of current indices, ≤ 100), `expected_revision` |
| `plan/step-edit` | `plan_step_edit` | `plan_id`, `step_idx`, `description` (required), `expected_revision`, `expected_step_id` |
| `plan/step-remove` | `plan_step_remove` | `plan_id`, `step_idx`, `expected_revision`, `expected_step_id` |
| `plan/reorder` | `plan_step_reorder` | `plan_id`, `order` (required: a permutation of the current indices; `order[i]` is the step that moves to position `i`), `expected_revision` |
| `plan/edit` | `plan_edit` | `plan_id`, `title` (one line, ≤ 200), `description` (≤ 4,000; `""` clears it), `expected_revision` |

**Where title and description live.** In the existing `goal` column, no
migration: the title is its first line and the description the text after it
(`plan/edit` writes `"<title>\n\n<description>"`). Every plan read
(`plan_get`, `GET /api/plans/{plan_id}`, the results here) reports them as
`title` and `description` beside `goal`. A plan created with a one-line goal
has that line as its title and an empty description.

**Step ids and dependencies.** A step keeps its row id (`id`) for life; its
position is `step_idx`, which every other plan action addresses. Add, remove
and reorder renumber positions and rewrite every `depends_on` through the same
old → new map, in one write transaction. `step-add` takes `depends_on` in the
*current* indices and shifts the steps at and after `position` down one place.
`step-remove` closes the gap.

**Refusals.** A claimed (`doing`) or `done` step is never edited, removed or
renumbered: `409 {"code": "conflict"}` (`"step 1 is doing; a claimed or done
step cannot be renumbered"`), so an insertion or reorder that would move one is
refused. The rest answer 400 with the reason.
`step-remove` is refused while another step depends on the step (`"cannot
remove step 1: step 2 depends on it"`) and for a plan's only step. `reorder` is
refused when it moves a step ahead of a step it depends on. Structure changes
are refused on a `done` or `abandoned` plan and on a joint plan (`"change its
steps through joint_propose"`). `plan/edit` works on any plan, joint or closed,
except a **workflow-bound plan** (goal exactly `governed workflow <run_id>`,
created by `POST /api/action/langgraph/run`). Every route in this section answers
400 for such a plan: the goal text is the run's binding, which run recovery and
the expired-run sweep match in full, and the runner reads its plan, build,
verify and merge stages by fixed step index.

**Step ids against reorders.** Every route that addresses a step by
`step_idx` (`plan/update`, `plan/retry`, `plan/claim`, `plan/handoff`,
`plan/step-edit`, `plan/step-remove`) also takes `expected_step_id`: the step's
immutable `id` from the plan read. It is checked inside the write transaction;
if the step at that index is no longer that id, or the index no longer exists
(a reorder, insert or removal moved it), the write answers `409 {"code":
"conflict"}` and changes nothing.
The UI should always send it. Omitted, the write addresses the index alone, as
before. Conversely, once a step is claimed a reorder that would move it is
refused with 409, so whichever of a claim and a reorder lands second loses.

**Conflicts.** Every plan carries `revision`, an integer that every write to
the plan advances, step writes included (`update`, `claim`, `handoff`,
`retry`, `checkpoint`, `close`, and these routes). Send `expected_revision`
(the `plan.revision` you last read): a plan changed since then answers `409
{"error", "code": "conflict"}` (the message names the current revision), even
when both writes land in the same second. Without it the write applies to the
plan as it stands. Every result is `{plan, steps, next_step_idx}` (plus `step`
for add and edit, `removed` for remove); its `plan.revision` is the next
`expected_revision`.

### Goals: `POST /api/action/goal/<action>` (task #2480)

One route per `goal_manage` action, scope `CAPABILITY` tools `goal_manage`,
operator, CSRF. `author` is the verified identity (in the approval payload).
`goal_manage` gates these actions as writes (policy and approval gates); its
read actions are unchanged.

| Route | Fields | 200 |
|---|---|---|
| `goal/edit` | `goal_id` (required int), `title` (one line ≤ 200), `statement` (≤ 4,000), `expected_revision` | `{goal, edge, meaning}` |
| `goal/link` | `task_id`, `goal_id` (required ints) | the link and the task's why-chain |
| `goal/unlink` | `task_id`, `goal_id` (the goal it serves) | `{task_id, goal_id, edge, unlinked: true}` |
| `goal/close` | `goal_id`, `status` (`achieved` default, or `abandoned`), `reason` (≤ 4,000), `expected_revision` | `{goal, ...}` |
| `goal/reopen` | `goal_id`, `reason`, `expected_revision` | `{goal, ...}` |

404 for an unknown goal or task. 409 `code: "conflict"` when
`expected_revision` (the `goal.revision` you last read; every goal update
advances it) is stale, when closing a closed goal, or when reopening
an open one. 400 when linking a task that serves another goal, or unlinking
from a goal the task does not serve. Closing a goal does not close or unlink
its tasks. Each write appends an event (`goal.updated`, `goal.linked`,
`goal.unlinked`, `goal.closed`, `goal.reopened`) attributed to the caller.

### Read projections the Work area uses (task #2480)

**Board cards** (`GET /api/board`, `api.board_payload`). Every task card, nested
subtasks included, now carries `domain` (`personal`, `professional`, `hobby`,
or `null` for a legacy row) beside `project` and `lane`, so the UI can filter
the board and task list by domain.

**Task dependencies: direction and ids.** One row of `task_dependencies` says
"task `task_id` depends on task `depends_on_task_id`" (the prerequisite). The
three graph reads draw it differently; read the direction from this table, not
from the link name:

| Read | Link | `source` | `target` | Id |
|---|---|---|---|---|
| `GET /api/overview` → `task_graph.links` | `kind: "depends_on"` | prerequisite `task:<depends_on_task_id>` | dependent `task:<task_id>` | `dependency_id` (added by #2480) |
| `GET /api/execution-graph` → `links` | `kind: "depends_on"` | dependent `task:<task_id>` | prerequisite `task:<depends_on_task_id>` | `dependency_id` |
| `GET /api/work-graph` → `edges` | `type: "task_dependency"` | dependent `task:<task_id>` | prerequisite `task:<depends_on_task_id>` | none; look the id up in the overview or execution graph |

So the overview's arrow runs from the prerequisite to the work it unblocks,
and the execution and work graphs point from a task to what it waits on.

**Adding and removing a dependency.** Use the existing `POST
/api/action/dependency` (`routes.action_dependency`). Scope: `CAPABILITY`,
actions `(coordination_manage, dependency_add)` and `(coordination_manage,
dependency_remove)` together, operator, CSRF; the verified identity is the
`agent`.

| Body | Effect | Answer |
|---|---|---|
| `{"operation": "add", "task_id", "depends_on_task_id"}` | `task_id` now waits on `depends_on_task_id`; a self-edge, a missing task or a cycle is refused | 201 with the new row (its `id` is the `dependency_id`) |
| `{"operation": "remove", "dependency_id"}` | removes that row | 200 |

400 for a refused or malformed request. This route predates the CC2
conventions: it does not refuse unknown keys and its body is not size-capped
while streaming. No new dependency route was added.

### `GET /api/plans/{plan_id}/joint`

Adapts `plan_manage` `joint_get`. Scope: `CAPABILITY`, actions
`plan_joint_get`, operator. No query parameters. `/api/plans/{plan_id}` and this
route match different paths (a path parameter never spans `/`), so neither
shadows the other.

200: `{"plan_id", "revision", "goal_id", "org_unit_id", "actor", "role",
"action", "comment_id", "comment_ids", "pending", "decisions", "review",
"steps", "next_step_idx", "team": [{"identity", "role": "lead"|"member"}],
"why_chain", "recovered", "caller"}`. `pending` is the open proposal or null.
`caller` is the caller's own `team` entry; the tool decides each write when it
is made, so the UI enables actions by membership and role and shows the tool's
refusal otherwise. 400 when the plan is not a joint plan or the caller is not a
member of its office; 404 for an unknown plan.

### `GET /api/tasks/{task_id}/subtree` (task #2516)

Every descendant of a task, at any depth and in any status, so a board's tree
never omits old done, cancelled or archived work the capped `task_list` page
left out. Scope: `CAPABILITY` tools `task_list` (the `why-chain` ceiling).
200:

```json
{
  "root": {"id": 1, "parent_id": null, "title": "…", "status": "doing", …},
  "descendants": [{"id": 2, "parent_id": 1, "depth": 1, …}, {"id": 3, "parent_id": 2, "depth": 2, …}],
  "count": 2,
  "complete": true
}
```

Rows carry `id`, `parent_id`, `title`, `status`, `priority`, `project`,
`domain`, `lane`, `claimed_by`, `evidence_ref`, `created_at`, `updated_at`,
`completed_at`, `archived_at` and (descendants) `depth`, depth-first by id.
Tenant-fenced at every level: another tenant's root is 404. The walk is
breadth first under one budget that counts every node visited at any depth, so
it never answers a partial tree: more than 5,000 descendants answers 422
`{"code": "subtree_too_large"}` (read a lower subtree), and a parent chain that
loops back into the tree answers 422 `{"code": "subtree_cycle"}`. 404 unknown
task, 400 a non-integer id.

### `POST /api/action/task/claim`

Adapts `task_claim` for the signed-in identity. Scope: tools `task_claim`,
operator, CSRF.

| Field | Type |
|---|---|
| `id` | int, required |
| `timeout_seconds` | int ≤ 604,800 |
| `expected_project` | string ≤ 200 |
| `expected_lane` | string ≤ 100 |
| `agent` | must equal the caller if present |

200 does not by itself mean the claim was taken. Read `claimed_by`:

- claimed: the task row, with `claimed_by` equal to the caller;
- held by someone else: `{"id", "claimed": false, "safe_wait": {"owner",
  "waiting_on", "until", "generation", "claimed_by"}}`. Nothing was written.
  Show who holds it and until when.

400 with the tool's reason when the claim is refused outright (terminal task,
wrong project or lane, a hard WIP cap, an exhausted budget).

### `POST /api/action/task/delete`

Adapts `task_delete` (destructive). Scope: tools `task_delete`, operator, CSRF.
Body: `id` (int, required). The hub's approval baseline gates `task_delete`, so
the first call normally answers 202; repeat the identical request after the
approval to run it. 200: `{"deleted": true, "children_deleted", "worktrees"}`.
404 when no task matched.

### `POST /api/action/session/end`

Adapts `session_end`. Scope: tools `session_end`, operator, CSRF. The caller
ends only its own session: the tool refuses another identity's session with
400 (`"session <id> is owned by '<agent>'; verified caller is '<you>'"`).

| Field | Type |
|---|---|
| `session_id` | string, required, ≤ 128 |
| `status` | `completed`, `failed` or `abandoned` |
| `exit_status` | string ≤ 200 |
| `summary` | string ≤ 8,000 |

200: `{"session_id", "status", "closed": true, "owed"?, ...}`. 404 for an
unknown session.

### `POST /api/action/session/operator-end` (task #2480)

The verified human operator ends another identity's session. Scope:
`CAPABILITY`, operator, no tools (an agent identity answers 403 `"this action
is reserved for the human operator"`); the handler checks the operator again.
It calls the `session.operator_end` platform writer through the governed write
adapter (`webui.api._registered_platform_write`): the `session` object's
profile check, the agent-access switch (with the operator's recovery
exemption, so it works while agent access is off), the policy gate and the
approval gate on operation `session.operator_end`. The writer checks the
verified identity is `UA_OPERATOR_LABEL` again; `session_end` and its
ownership rule are not used and the identity is never cleared or replaced.

| Field | Type |
|---|---|
| `session_id` | string, required, ≤ 128 |
| `reason` | string, required, ≤ 2,000 |
| `status` | `abandoned` (default), `completed` or `failed` |
| `operator` | must equal the caller if present |

200: `{"session_id", "owner", "status", "ended_by", "closed": true,
"event_ulid", "outstanding": {"claimed_task_ids", "open_delegation_ids",
"note"}}`. **This endpoint ends the session row only: it does not cancel a
running process or run, release the owner's task claims, or cancel
delegations.** `outstanding` lists what the owner still holds so the UI can
offer those actions separately. An operator-ended session stays ended: the row
carries a write-once `operator_ended_by` marker that later ends (the owner's
`session_end`, the reaper, any cleanup) do not clear, so a heartbeat does not
revive it (as it would a reaped `abandoned` session), and re-registering the same
external session id opens a new session. The session row gets `exit_status: "operator_end:<you>"` and
`"ended by operator <you>: <reason>"` in `summary`, and the ledger gets one
`session.operator_ended` event (originator: the operator; payload:
`session_id`, `owner`, `task_id`, `status`, `reason`, `operator`, `ended_at`).
The read, the conditional end and the ledger append are one write transaction:
if the append fails nothing is ended and the request can be repeated; of two
concurrent ends exactly one wins and the other answers 409. 404 for an unknown session (another tenant's
reads as unknown). 409 `code: "conflict"` for a session that already ended.
202 when `session.operator_end` is in `UA_APPROVAL_TOOLS` (or a policy rule
requires approval). The approval payload is the body plus `operator`, so the
identical repeat after approval runs it once. Separation of duties applies: the
operator requested it, so only a *different* human identity may approve it. A
hub whose only human is `UA_OPERATOR_LABEL` (and its aliases) cannot approve
it at all, so leave `session.operator_end` out of `UA_APPROVAL_TOOLS` there.

## runs (`webui/cc_runs.py`, task #2480)

### `GET /api/routines`

Adapts `routine_list`. Scope: `CAPABILITY` tools `routine_list`, operator.
Query: `include_paused` (`true` default), `recent_runs` (0..20, default 5).
200: `{"routines": [...], "routines_enabled": bool}`. Each routine is its
stored row (`id`, `title`, `trigger`, `schedule`, `paused`, `concurrency`,
`catch_up`, `cursor_at`, `activity_gate`, ...) plus `recent_runs` (newest
first: `status` `created` | `queued` | `skipped` | `suppressed`, `reason`,
`task_id`, `scheduled_for`, `fire_key`, ...) and `next_run_at`: the
scheduler's next slot for an unpaused cron routine (the first slot after
`cursor_at`; a past value means it fires on the next tick), else `null`.
`routines_enabled` is false while `UA_ROUTINES_ENABLED` is not `1`: nothing
fires then.

### `POST /api/action/routine/add`, `/pause`, `/resume`

Scopes: tools `routine_add`, `routine_pause`, `routine_pause`; operator, CSRF.
`add` takes `title` (required, one line ≤ 200), `trigger` (`cron` default,
`api`, `webhook`), `schedule` (five-field UTC cron; required for cron, refused
otherwise), `notes`, `lane`, `project`, `domain`, `concurrency` (`skip`,
`queue`), `catch_up` (`none`, `last`, `all`), `start_at`, `activity_gate`
(`always`, `require_activity`), `activity_gate_scope` (`org`, `project`); 200
`{"id"}`. `pause` and `resume` take `routine_id`; 200 `{"id", "paused": 1|0}`;
404 for an unknown routine. Pausing a paused routine is not an error.

`add` accepts an optional `Idempotency-Key` (see "Keyed writes", task #2516).

### `POST /api/action/routine/run` ("Run now")

Fires a routine once now through `routine_fire`. Scope: tools `routine_fire`,
operator, CSRF. Header `Idempotency-Key` (required, 1-200 characters, no
surrounding whitespace); body `{"routine_id"}`. The bearer-only webhook route
`POST /api/routines/{routine_id}/fire` is unchanged and still refuses cookies
and cron routines.

* **Scheduled routines.** The verified human operator may run a cron routine:
  the handler enters `routines.operator_run_now()` around the call. Any other
  caller keeps `routine_fire`'s rule, and a cron routine answers 409
  `"cron routines are fired by the scheduler"`. Run now does not move the
  schedule: the next slot still fires.
* **Idempotency.** The key is scoped to the hub's tenant and the signed-in
  identity, and bound to the request that first used it (a SHA-256 digest of
  `{"routine_id"}`). Repeating the identical request with the same key returns the
  **first answer, frozen** (stored in `idempotency_keys` when the fire answered
  200): a run that first answered `queued` keeps answering `queued` after the
  queue releases it. Every successful duplicate gets that stored answer.
  * Same key, different `routine_id`: `409 {"code": "conflict"}`, nothing fires.
  * Same key while the first request is still running: `409 {"code":
    "in_flight"}`; repeat shortly to read the stored answer. A reservation left
    by a request that died is taken over after 10 minutes. If the original
    request then finishes after all, it answers with the stored answer, or 409
    `in_flight` while the takeover has not settled; it never returns its own
    result, so every successful caller sees the same answer.
  * A receipt for this key written before tenant scoping, on a tenanted hub:
    `409 {"code": "ambiguous_receipt"}`; use a new key.
  * An answer that was not 200 (refused, parked for approval) is not stored, so
    the repeat runs the request again. Use a new key for a new run.
  * **Retention.** Only the frozen answer expires: settled receipts are kept 7
    days and pruned on later Run now calls (at most 500 per call). The key stays
    bound to its first fire for that routine for good, because the routine's own
    fire receipt is permanent: after the frozen answer is pruned, the same key
    for the same routine fires nothing and answers from that receipt, whose
    `status` may since have changed (a `queued` fire later reads `created`).
    **A new run always needs a fresh client key.** A reservation that is still
    in flight is never pruned: the retention window (7 days) is longer than the
    takeover window (10 minutes), so only a reservation long since abandoned
    can be.
  * Two identities sending the same header key get separate runs: the key
    passed to `routine_fire` is derived from tenant, identity and client key
    (`cc:` + 48 hex characters of a SHA-256), and its fire receipt is stored in a
    namespace only this route can select (`cc-run:<key>`, where an ordinary
    `routine_fire` call, including `POST /api/routines/{routine_id}/fire`, stores
    `event:<key>`). A bearer fire that sends the derived key as its own key
    therefore makes its own, separate fire and cannot capture a Run now.
* **A run already active.** 200 is a receipt, not always a new task:
  `{"status": "created", "task_id"}` made a task; with `concurrency: "skip"`
  and an unfinished earlier task the answer is **200 `{"status": "skipped",
  "task_id": null}`** (nothing was created; the receipt's reason is
  `overlap`); with `concurrency: "queue"` it is `{"status": "queued",
  "task_id"}` (a blocked task released when the earlier one finishes); an
  activity-gated routine with no activity answers `{"status": "suppressed",
  "task_id": null}`.
* 404 unknown routine; 409 paused, or cron for a non-operator; 503 while
  `UA_ROUTINES_ENABLED` is not `1`; 400 bad key or body.
* 202 when `routine_fire` is gated; the payload is `{routine_id,
  idempotency_key}` with the derived key, which the same caller's identical
  repeat (same header key) derives again, so the replay must send the same key.

### `POST /api/action/langgraph/run`

Runs `langgraph_workflow_run`, the governed plan → build → verify → merge
chain for a task the caller has claimed (`doing`), or, with `engine:
"research"`, continues a paused `langgraph_run` research run (task #2522).
Scope: tools `langgraph_workflow_run`, `task_update` (the workflow's build and
merge hops and the research run's outcome note write through it),
`langgraph_run` and `web_search` (the research run's governed read), operator,
CSRF.

| Field | Type |
|---|---|
| `task_id` | int; required for `workflow`, an optional check for `research` |
| `query` | string ≤ 4,000; required for `workflow`, an optional check for `research` |
| `run_id` | the 26-character ULID a previous answer returned, to continue that run; required for `research` |
| `engine` | `workflow` (default) or `research` |

**Invocation session.** The tool refuses a call without a hub session id, and
a browser request carries none. The handler registers a real `agent_sessions`
row for the verified caller (kind `control_center`, linked to the task), binds
it for the one call (`profiles.http_route_invocation_session`, which refuses a
session that is ended or another identity's) and ends it afterwards
(`completed`, or `failed` when the call failed or the request was cancelled).
The close runs on its own thread; the request waits for it at most 10 seconds
(shielded from cancellation) and then answers anyway while the thread still
closes the row. A close that fails is logged and recorded once as a
`control_center.invocation_session_unclosed` event (the reaper then marks the
row `abandoned` after `UA_SESSION_ABANDON_MINUTES`). The background close is
best-effort: if the hub process dies before it runs, the row is closed only by
the reaper (as `abandoned`), so nothing should rely on the session's close status
or `exit_status`; the run's outcome is the route's answer and the run's own
records. Each request has its own
session; the run's authority is the task claim and the identity, checked again
on every continuation.

**Answers.**

* 202 `{"status": "approval_pending", "code": "approval_required",
  "approval_id", "run_status": "interrupted", "run_id", "pending_hop":
  "build"|"merge", "interrupted_by", "tool": "langgraph_workflow_run",
  "session_id"}`: a native graph approval. Decide it in the Inbox, then
  **continue by sending the same `task_id` and `query` with this `run_id`**.
* 200 `{"status": "completed", "run_id", "output", ...}`, or **200
  `{"status": "denied", ...}`** when an approval was denied: the run ended
  without its writes.
* 202 `{"status": "approval_pending", "code": "approval_required",
  "approval_id", "tool"}` with no `run_id`: the wrapper's
  `UA_APPROVAL_TOOLS` gate parked the call before the run started; the
  identical request after approval starts it.
* 400 with the tool's reason: task not claimed by the caller or not `doing`,
  `run_id` from another task or query, stale authority.

**Not resumable by identical replay** after a native approval: the stored write
must be continued with `run_id`.

**`Idempotency-Key`** (optional, task #2516): the start runs at most once per
caller and key (see "Keyed writes"). A wrapper-gate 202 (no `run_id`) is
dropped; a native-approval 202 (with `run_id`) and every 200 are frozen.

### `GET /api/langgraph/runs/{run_id}` (task #2516)

The authoritative progress of one governed graph run, read from its LangGraph
checkpoints and its bound plan, so the canvas can show where a run is whatever
the last `POST` answered (a lost answer, another tab, a run continued
elsewhere). Read-only: no hop runs and no gate is touched. Only the run's own
caller (the identity that started it, `claimed_by`) and the verified human
operator may read it, and its task must be visible in the caller's tenant;
anyone else gets the same 404 as for a run that does not exist.

```json
{
  "run_id": "01J…", "thread_id": "ua-lg:01J…", "task_id": 41, "claimed_by": "agent-codex",
  "graph_name": "governed_workflow", "graph_version": "1.2.0",
  "status": "created | running | interrupted | completed | denied",
  "pending_hop": "build", "approval_id": "12", "reason": null,
  "plan_id": "…", "plan_status": "active",
  "hops": [
    {"hop": "plan", "step_idx": 0, "status": "done", "waiting_approval": false, "owner": "…", "updated_at": "…"},
    {"hop": "build", "step_idx": 1, "status": "doing", "waiting_approval": true, …},
    {"hop": "verify", …}, {"hop": "merge", …}
  ],
  "interrupted": true, "next": ["build_gate"],
  "checkpoint": {"id": "…", "created_at": "…", "count": 7}
}
```

A hop's `status` is its plan step's (`open`, `doing`, `done`, `failed`,
`blocked`); `waiting_approval` marks the hop an `interrupted` run waits on, and
`approval_id` is that approval. `pending_hop` is set while the run is
`interrupted`, `running` or `denied` (the hop that was refused). 404 when no
governed workflow run has this id, or the caller may not read it, 400 for a
malformed id, 503 when the LangGraph extra is not installed.

**Research resume (`engine: "research"`, task #2522).** Starting a research run
stays an MCP call (`langgraph_run`); the browser continues one by `run_id`
(`engine: "research"` without `run_id` answers 400). The route calls
`langgraph_run(run_id, resume=True)` under an invocation session as above. The
tool rebuilds the run from its validated checkpoint with its original claim,
task revision, profile, budget and trace. Before anything is replayed, the
run's task must be in the caller's tenant and the caller must own the run or be
the verified operator (a human auth channel). The operator only triggers the
owner's run: it executes under the owner's saved profile and current ceiling,
never the operator's. The task revision, claim lease, budget, policy gate and
approval gate are checked again. A heartbeat-renewed lease on the run admits
one resume at a time, and every checkpoint write is fenced on its token, so two
concurrent continuations never both execute; this
route takes no `Idempotency-Key` (it reuses none of the receipt schemes) and is
safe to repeat: a finished run answers its stored outcome with `duplicate:
true`. Answers carry the tool's typed `outcome` and `code` (see
`langgraph_run` in `tools-reference.md`):

* 202 `{"status": "approval_pending", "code": "approval_required",
  "approval_id", "run_status": "interrupted", "tool": "langgraph_run", ...}`:
  the approval is still undecided.
* 200 `outcome: "run_completed"` with `output`; 200 `run_denied` with code
  `approval_denied`, or `run_failed` with code `gateway_failed`, `run_error`,
  `no_output` or `no_outcome`: the run ended.
* 403 `run_denied` with code `task_not_accessible`, `not_run_owner`,
  `unauthenticated` or `stale_authority`: the caller's access or the owner's
  authority failed before anything ran.
* 409 `run_failed` with code `checkpoint_missing`, `checkpoint_corrupt` or
  `checkpoint_incompatible`, or `run_conflict` with code `resume_in_flight` or
  `lease_lost` (this resume's lease expired and another took over; it recorded
  nothing).


## Launches (`webui/cc_launch.py`, task #2501)

A launch starts real agent work. When the browser loses the answer to one it
cannot tell from capped lists whether the work started, so the hub is the
authority: a launch may carry an `Idempotency-Key` header (the client's attempt
id), a repeat with the same key never starts a second job, and the hub reports
what happened to an attempt.

### Which routes are launches

| Route | Kind | Idempotent with a key |
|---|---|---|
| `POST /api/action/dispatch` | `dispatch` | yes |
| `POST /api/action/dispatch-stack` | `stack` | yes |
| `POST /api/action/node-session` `action: "start"` | `device` | yes |
| `POST /api/action/node-session` `action: "restart"` | `restart` | yes |
| `POST /api/action/node-session` `action: "cancel"` | — | the header is ignored; cancel is idempotent already |
| `POST /api/action/routine/run` | — | its own required key ("Run now" above) |
| `POST /api/action/langgraph/run` | — | an optional key for a workflow start; a research resume takes no key and holds a per-run lease (above) |
| `POST /api/teams/snapshots/{snapshot_id}/restore` | — | not covered: one request re-dispatches several seats (`team_snapshot.py`, outside this task); a seat whose resume is already queued or running is refused by the queue |
| `POST /api/action/node-resume`, `/node-adopt` | — | not covered; the queue already refuses a second resume of a session while one is queued or running (409) |
| `POST /api/action/node-followup` | — | its own `idempotency_key` body field (unchanged) |
| `POST /api/action/workflow`, `/cycle` (`start`) | — | an optional key under "Keyed writes" (task #2516) |
| `POST /api/action/team-up`, `/operating-run`, `/handoff` | — | not covered; unchanged |

Without the header every route behaves exactly as before: agents and the CLI
call them too.

### The contract with a key

* **Key.** 1-128 characters of letters, digits, `.`, `_`, `:` or `-`, starting
  with a letter or digit; anything else is 400 and nothing runs. The UI's
  `launch-<uuid>` keys fit.
* **Scope of a key.** One attempt per tenant, verified identity and key.
  Another identity's identical key is a different attempt; another tenant's is
  absent.
* **Request binding.** The first request's validated body (the parsed dispatch
  request, the stack selection, the node and its fields, the restart's
  `item_id`) and its kind are digested. The same key with a different request
  answers **409 `{"code": "conflict"}`** and starts nothing.
* **Recorded before anything irreversible.** The first request records the
  attempt `in_progress` before it creates anything, and records each step's ids
  as it goes (delegation and task, the approval note, the run; the queue item).
* **Started → frozen.** Once a launch started, a repeat returns the **first
  answer, frozen** (same status and body), without running anything.
* **Concurrent repeats.** While a request holds the attempt (a 60-second lease
  that a heartbeat renews every 20 seconds while the request runs, so a slow
  start never loses it; the hub worker running the request treats it as held
  whatever the clock says), another request with the same key answers **409
  `{"code": "launch_in_progress"}`** at once; it does not wait. Look the attempt
  up or retry shortly. The operations underneath refuse a second job by
  themselves too: `delegate` takes an idempotency key derived from tenant,
  identity and client key; a node item stores the same derived key under a
  unique index (`node_dispatch.launch_key`); and `claim_and_dispatch` never
  launches a claimed task twice.
* **Parked.** When a step parks for approval the answer is the usual 202
  `{"approval_id", "code": "approval_required", ...}` and the attempt is
  `parked` with its `approval_id`. After deciding the approval, **repeat the
  same request with the same key**: each step skips what already happened, so
  the repeat continues the same delegation (the approval note keeps its first
  timestamp, so the parked payload matches) or queues the one node item. A
  repeat before the decision parks again on the same approval. The policy and
  approval gates run on every real start: a key never skips a gate.
* **Failed.** A refusal (4xx) records `failed` with the route's own error text;
  a dispatch or stack start whose run did not reach launch (a spawn failure:
  `launched: false` with a `run_id`) records `failed` with the start's reason;
  any other failure records `"the launch failed on the hub"`. A failed attempt
  is not frozen: a repeat with the same key continues it from its recorded
  steps, unless its launch is proven (next section).
* **Receipts never downgrade a launch.** Recording a step's ids is a receipt,
  not the launch. When writing one fails (a locked or failing database), the
  receipt stays pending and the attempt's next write (a later step, the lease
  heartbeat or the final record) records it first. A request whose run
  committed still answers with its launch, and if even the final write fails
  the attempt is settled from the evidence once its lease lapses (task #2509).
* **Authorization first.** The route's scope (capability, CSRF, identity) runs
  before the attempt is read, so a recorded answer is only returned to a caller
  who could make the request now.

### Reconciliation: one state table

`GET /api/launch-attempts/{key}` and a repeat `POST` read an attempt through one
function (`cc_launch.reconcile`), so they always agree.

**What proves a launch.** Evidence is positively bound to the attempt by its
operation key (derived from tenant, identity and client key): a node item queued
under that key, or the dispatch run the coordinator bound to that key
(`dispatch_launch_keys`, written in the transaction that mints the run), that
**reached launch**: state `launched`, `running`, `finished`, `failed`, `blocked`
or `denied`, or a `dispatch-launched:<run_id>` event (a launched run later
cancelled or expired). A `claimed` run (committed before the launch boundary is
called) and a `retryable` run (a failed spawn) are not launches, and no other run
of the task, keyed or not, is ever evidence.

**One job per launch, across hub workers.** `claim_and_dispatch` refuses a claim
under a key already bound to another run: `launch_key_bound` (with the bound
`run_id`) for another task, `already_claimed` for the same one. Only a same-task
run that never reached launch may be replaced by a retry under the same key, and
never while its spawn may still be running: the launcher marks the spawn started
in a transaction that checks it still holds the claim (a launcher that lost it
spawns nothing) and records its outcome when the spawn returns; until then the
key answers `launch_unsettled`, and lease reclaim leaves the run claimed. A
stack launch records the task it chose before it starts it: a request that lost
its hold first answers 409 `launch_in_progress` and launches nothing, and one
whose database write failed answers 503 `launch_unavailable` and launches
nothing. A stack answer's `next_task_id` is the task that request started.

| Attempt record | Evidence | `GET` answers | Repeat `POST` |
|---|---|---|---|
| none | — | `unknown` | records the attempt and runs |
| `in_progress`, live lease | any | `in_progress` | 409 `launch_in_progress` |
| `in_progress`, lapsed lease | launch proven | `started` (with its ids) | replays the answer |
| `in_progress`, lapsed lease | none, a claimed run, or a retryable run | `failed` | continues from the recorded steps |
| `parked` (approval) | any | `parked` | continues; the gates run again |
| `failed` | launch proven | `started` (with its ids) | replays the answer |
| `failed` | none, a claimed run, or a retryable run | `failed` | continues |
| `in_progress` (lapsed), `failed` or `unsettled` | the bound run's spawn started with no recorded outcome | `unsettled` (with its `run_id`) | 409 `launch_unsettled` |
| `unsettled` | the operator settled the spawn as failed | `failed` | continues |
| `started` | its task still exists | `started` | replays the answer |
| `started` | its task is gone | `started` | 409 `conflict` |

**Unsettled launches.** When the bound run's spawn started but its outcome was
never recorded (the launcher died, or stalls, inside the spawn), the job may
exist, so the attempt is `unsettled`: not terminal, never pruned, and a repeat
answers 409 `{"code": "launch_unsettled", "run_id"}` without running anything.
Its reason names no retry. It becomes `started` if the spawn's outcome is later
recorded as launched, or `failed` once the operator settles it:

`POST /api/dispatch-runs/{run_id}/settle-spawn` (operator only, CSRF, no MCP
twin) records the spawn `failed` in one guarded transaction, and only when the
hub proves both gone: the launcher (its recorded process token names no running
process, or it is this hub process and that spawn is not in flight in it) and
the job (the run is not node-routed and not launched, records no running
process, and a still-claimed run's lease has expired). Otherwise it answers 409
`{"settled": false, "reason", "code": "spawn_not_settled"}` with one of
`not_unsettled`, `launcher_may_be_spawning`, `node_reports_its_outcome`,
`job_may_be_running` or `lease_still_live`. After it settles, the attempt reads
`failed` and a repeat with the same key continues it.

The fence is not only for keyed launches (#2512). Every dispatch run, including
the scheduler's unkeyed ones, marks its spawn the same way under its own per-run
key (`run:<run_id>`), so the same route settles an unkeyed run. While any run of
a task has a spawn without a recorded outcome, lease reclaim keeps that run
`claimed` instead of handing it back as `retryable`, and every claim or resume of
the task answers `launch_unsettled` with that `run_id`.

A resume relaunches under the same fence (#2527). It arms its own mark,
`run:<run_id>:resume`, in the transaction that claims the run back, and spawns
through the same fence, re-check, outcome and in-flight steps as a claim. A
resumed spawn that stalls is held like a claimed one, and the same route settles
it. The claim's own mark is never rewritten, so a keyed launch's recorded
`spawned` outcome stays that key's launch evidence after any number of resumes.

A held run is never silent. Reclaim and the queue-hygiene pass each warn once per
run past its lease: a task note (`[dispatch: spawn-unsettled run=<run_id>] ...`)
naming the settle route, and one `agent.health.alert` event (`kind:
dispatch_spawn_unsettled`, with `run_id`, `task_id` and `settle`), deduplicated by
run so the supervisor wake loop and the Zulip bridge see it once. The scheduler
tick (`spawn_unsettled`) and the hygiene pass (`dispatch_spawn_unsettled`) list
every held run, and the `system_doctor` row `dispatch_unsettled_spawns` turns
DEGRADED, naming each run, once one is more than 15 minutes past its lease.

A lapsed attempt without proof fails with reason `"the launch was interrupted
before it started; repeat it with the same key to continue"`. A `failed` record
whose own launch is proven lost a receipt, not a launch, so it is settled to
`started`. Every transition to `started` stores a replayable answer with it: the
route's own answer, or, when the launch was proven from evidence, a canonical
one with `"reconciled": true` (a dispatch: `status: "running"`, `task_id`,
`delegation_id`, `run_id`, `session_id`; a stack: `next_task_id` and
`dispatch: {launched: true, run_id}`; a device item: the queue item). A request
whose start reports `already_claimed` for the attempt's own launched run answers
that canonical form too. A repeat of a started attempt whose task has since gone
is a typed 409 `conflict`: start a new launch with a new key. An attempt never
blocks for longer than its lease once its holder is gone.

### Retention

Records live in `idempotency_keys` (tool `cc_launch`), through the tenant-scoped
storage helpers; a record written before tenant scoping is refused (409
`conflict`), never replayed. Terminal records (`started`, `failed`) older than
30 days are pruned, and beyond 5,000 records the oldest terminal ones go first;
both run, bounded to 500 rows, whenever a new attempt is recorded; `in_progress`,
`parked` and `unsettled` records are never pruned. A pruned key is forgotten: each record
carries a random generation that is part of the operation key, so the same
client key later starts a new launch and never replays or adopts the pruned
attempt's delegation, run or queue item. Records hold the identity, the kind,
the request digest, the generation, the state, the ids, the first started
answer and timestamps: no prompt beyond what the route itself returned.

### `GET /api/launch-attempts/{key}`

Scope: actions `(delegation_manage, delegate)` (what every launch route needs),
operator; matrix `dev cos +operator`. Read-only, no CSRF. The verified caller's
own attempt in its tenant; another identity's key reads as unknown. Always 200
(400 for a malformed key, 409 for an unscoped legacy record):

```json
{
  "key": "launch-…",
  "state": "unknown | in_progress | parked | unsettled | started | failed",
  "kind": "dispatch | stack | device | restart",
  "approval_id": 12,
  "delegation_id": 7,
  "task_id": 41,
  "run_id": "…",
  "item_id": "nd-…",
  "session_id": "…",
  "reason": null,
  "created_at": "…",
  "updated_at": "…"
}
```

An unknown key answers only `{"key", "state": "unknown"}`. `session_id` is the
hub registry session row id (`/api/sessions/{id}`) of a node item, once the node
reports its session; it is `null` for a hub dispatch, whose handle is `run_id`.
A lookup reconciles the attempt first (the state table above).

### Reads the launch UI uses

* `GET /api/sessions/{id}` now carries the same `wake` (idle-wake request and
  operator grant) and `platform`/`links` the list rows carry.
* Device queue items in `GET /api/nodes` `recent_items` carry
  `agent_session_id`: the hub registry row id of the item's session (the row
  whose `meta.item_id` is the item, else the newest row for its external
  session id), next to `session_id`, the harness's own id. `null` until the
  node reports a session.
## agents (`webui/cc_agents.py`, task #2470)

Agent enrolment, Horizon-2 connectors and org-proposal decisions. **Every error
answer in this section carries a `code`** (the scope gate's own 401/403, which
answer before the handler, may not):

| Code | Status | Meaning |
|---|---|---|
| `invalid_request` | 400 | unknown or ill-typed field, missing field, or the service refused the input |
| `operator_required` | 403 | the route or action is the verified human operator's alone |
| `policy_denied`, `agent_access_denied` | 403 | a policy rule or the agent-access switch refused the write |
| `separation_of_duties` | 403 | the org proposal's author may not apply it |
| `not_found` | 404 | the agent, server or proposal does not exist in this organization |
| `already_added`, `already_registered`, `retired`, `claims_held`, `sealed_store_required`, `approval_denied`, `conflict` | 409 | the object's state refuses the write (below) |
| `rate_limited` | 429 | a policy rate limit |
| `unavailable` | 503 | the hub process cannot run the write (remove without its stop handlers) |
| `approval_required` | 202 | parked on an approval (the body is the usual 202 shape) |

### Enrolment

`agent_enrollment` (PR #1497) holds services, not registered tools, so these
three routes are the **verified human operator's alone**: scope `operator=True`
with no tools, and the handler checks the operator again (an agent identity of
any profile answers 403; the bare handler answers `operator_required`). The
service then requires a lifecycle operator (`UA_LIFECYCLE_OPERATORS`, default
the operator label). Every answer is sent `Cache-Control: no-store` and
`Pragma: no-cache`.

#### `GET /api/agent-roster`

No query parameters (any is 400). 200:

```json
{"agents": [{"agent", "office", "profile", "kind", "added", "state",
             "token_source", "token_status", "token_issued_at", "token_expires_at",
             "last_seen"}],
 "count": 0, "offices": [{"id", "name"}], "kinds": ["local", "cloud"],
 "default_profile": "developer", "sealed_at_rest": true, "org": null}
```

One row per agent the hub knows (seats, the org chart, the configured roster,
token holders). `token_source` is `managed`, `env` or `null`. **No bearer and no
digest is ever in this answer.** Tenant-scoped: on a partitioned hub, seats and
token rows recorded under another organization are not listed. `sealed_at_rest`
says whether the managed store encrypts bearers (`UA_MANAGED_SECRET_KEYS`); when
it is `false` the add form must offer the explicit opt-in below.

#### `POST /api/action/agent/add`

| Field | Type |
|---|---|
| `label` | string, required, ≤ 64; a letter, then letters, digits, `_` or `-` |
| `office` | string, required, ≤ 100; an office id or name from `offices` |
| `profile` | string, ≤ 64; default `developer`; may lower, never raise, the label's default |
| `kind` | `local` (default) or `cloud` |
| `expires_days` | integer 1–3,650; default no expiry |
| `note` | string, ≤ 500; recorded on the token row |
| `allow_plaintext_at_rest` | boolean; accept a file-permission-only bearer when the store is unsealed |
| `actor` | must equal the caller if present |

200:

```json
{"agent", "office", "profile", "kind", "token": "<bearer>", "token_shown_once": true,
 "token_id", "token_hash_prefix", "issued_at", "expires_at", "warnings": []}
```

**The bearer is in this one response and nowhere else.** The route reveals it
only into the response body, never logs it, never caches or stores it, and never
puts it in an approval payload; the hub stores only its digest and, in the
managed store, a sealed copy. The UI shows it once, offers copy, and must not
write it to a draft, a parked write or any storage. It authenticates from the
next request, with no restart. `warnings` lists what the operator still does by
hand (a bearer listed in the hub's token configuration that keeps working, an
unsealed store, a cloud kind that needs a header-capable client).

Refusals: 409 `already_added` (the label has a seat or an active managed token
in this organization; a repeated add never mints or shows a second bearer, so
the route needs no idempotency key), 409 `retired`. Both are checked before the
approval gate, so a repeat answers 409 and queues no approval even when
`agent_add` is gated; the service checks again when the add runs. Also 409
`sealed_store_required` (`UA_MANAGED_SECRET_KEYS` is unset and
`allow_plaintext_at_rest` was not `true`; nothing is issued), 400 for an
unknown office, a reserved or human label, or a profile above the default.

Gates: the agent-access switch (the operator keeps the recovery exemption), the
policy gate and the approval gate run on operation `agent_add` with the
validated body plus `actor` (never a secret). 202 when `agent_add` is in
`UA_APPROVAL_TOOLS` or a policy rule requires approval; nothing is issued.
Separation of duties applies as for `session.operator_end`: only a different
human may approve, so a single-human hub should leave `agent_add` and
`agent_remove` out of `UA_APPROVAL_TOOLS`. **Not resumable by replay**
(`resumable: false`): the answer carries the bearer, so after approval the
operator submits the form again and sees the bearer there.

#### `POST /api/action/agent/remove`

| Field | Type |
|---|---|
| `label` | string, required, ≤ 64 |
| `reason` | string, required, ≤ 2,000; recorded on the retirement and on each released claim |
| `force_release` | boolean; release the agent's `doing` claims instead of refusing |
| `actor` | must equal the caller if present |

Retires the label and revokes its managed tokens, session credentials and
device sign-ins in one transaction, deletes the sealed bearer and records the
seat's removal. 200: `{"agent", "state", "changed", "released": [{"task_id",
"event_ulid"}], "revoked_tokens", "revoked_session_credentials",
"secret_deleted", "seat_removed", "warnings"}`. A bearer listed in the hub's
token configuration is refused from then on (403 `agent_retired`); `warnings`
says to remove it from that list.

Refusals: 404 `not_found` for a label the hub does not know, and on a
partitioned hub for a label with no seat, token row or configured token in this
organization; 409 `claims_held` while the agent holds `doing` claims and
`force_release` is not `true`; 503 `unavailable` when the hub process has not
registered the dispatch and node stop handlers. Gates as for add, on operation
`agent_remove`; resumable by identical replay.

### Connectors

Adapters over the registered Horizon-2 tools, through `api.call_tool`. Scope:
the tool's ceiling (developer, steward, chief-of-staff) plus the verified
operator. **Each tool forces its own one-shot approval**: the first call parks
(202, `tool` names the gate) and the identical request after a different human
approves it runs once. The route adds no identity argument, but what the tool
puts in its approval payload decides who a pass is bound to:

* `register` and `register-package` are **bound to the caller**: the tool puts
  the registration's `reviewed_by` (the verified caller) in the payload, so a
  different caller's identical body builds a different payload and parks anew.
* `authorize` and `revoke` are **not**: their payload holds no identity, so the
  same body from another caller matches the same approval row (see "Replaying a
  parked write").

| Route | Tool | Fields |
|---|---|---|
| `POST /api/action/connector/register` | `horizon2_server_register` | `server_id` (≤ 128), `connector` (≤ 100), `provenance` (≤ 500), `scopes` (list of ≤ 50 strings), `review_ref` (≤ 500), all required |
| `POST /api/action/connector/register-package` | `horizon2_package_register` (parks under `horizon2_server_register`) | `package`, `provenance`, `review_ref` required; `server_id`, `scopes` optional |
| `POST /api/action/connector/authorize` | `horizon2_connector_authorize` | `server_id`, `scope` (≤ 200), `task_id` (integer), `trace_id` (≤ 128) required; `connection_ref` optional |
| `POST /api/action/connector/revoke` | `horizon2_server_revoke` | `server_id` required |

200 is the tool's result (`{"server": {...}, "approval_id", "event_id"}`;
authorize has no `event_id`). 409 `already_registered` for a duplicate
`server_id` (no approval is consumed); revoke of an already revoked server
answers 200 `{"already_revoked": true}`; 404 `not_found` for an unregistered
server. The reviewer recorded on a registration is the verified caller.
`governed_servers` is not yet partitioned by organization
(`tenant_tables.PARTITION_PENDING`), so these routes see the hub's one set.

### `POST /api/action/org-proposal`

| Field | Type |
|---|---|
| `action` | `review`, `apply` or `reject`, required |
| `proposal_id` | string, required, ≤ 128 |
| `notes` | `review` only; string, ≤ 4,000 |
| `reason` | `apply` and `reject` only; string, ≤ 2,000 |
| `reviewed_by` / `applied_by` / `rejected_by` | the one for the action, must equal the caller if present; the others are refused |

Calls the registered `org_proposal` tool, so its wrapper approval gate and its
own checks run. `review` follows the tool's ceiling. **`apply` and `reject` are
decisions and are the verified human operator's** (403 `operator_required` for
any agent identity, whatever its profile). Apply also keeps the tool's rules:
the proposal's author may not apply it (403 `separation_of_duties`), and the
approval row the proposal parked when it was filed must be approved by a human.
Until it is, apply answers **202** with that row's `approval_id`, `code:
"approval_required"` and `tool: "org_proposal_apply"`; once it is approved the
identical apply runs. 200 is the tool's result (`{"ok", "proposal_id",
"status", "<actor field>", "<time field>"}`). 404 `not_found` for an unknown
proposal or another organization's; 409 `conflict` for a proposal already
applied, rejected or past `proposed` (review), and 409 `approval_denied` when
its approval row was denied.

### Work-proposal decisions (existing route)

`POST /api/action/work-proposal-decide` (task #2393) is reused, not twinned:
`{"proposal_id", "decisions": [{"index", "approve", "edits"?}]}`. The
`coordination_manage` `work_proposal_decide` action admits only a human
identity server-side (403 for any agent, whatever its profile;
`tests/test_work_proposals.py::test_the_operator_decides_through_the_control_center_route`).

### Agents area reads (task #2523)

Five gaps PR #1539 found in the Agents area. Errors on the two new routes carry a
`code` like the rest of this section; the two extended routes keep their own
shapes.

#### `GET /api/agent-seats`

Scope: `ANY_AUTHENTICATED` (every signed-in identity, agent or human, like
`GET /api/org`). No query parameters (any is 400 `invalid_request`). 200:

```json
{"agents": [{"agent", "office", "profile", "kind", "added", "state", "last_seen"}],
 "count": 0, "offices": [{"id", "name"}], "kinds": ["local", "cloud"], "org": null}
```

The rows of `GET /api/agent-roster` cut to `agent_enrollment.SEAT_FIELDS`, so a
viewer who is not the operator reads the hub's office seat and `local`/`cloud`
kind instead of guessing them from the label. **What is left out, and why:**
the four `token_*` fields describe a credential (whether a bearer exists, where
it comes from, its status and its issue and expiry dates), and
`default_profile` and `sealed_at_rest` only serve the add form; they stay on the
operator's roster. Every field kept is already readable without the operator
role: `GET /api/org` lists office members with their profile and last
sighting, and `GET /api/agent-lifecycle` the lifecycle `state`. `kind` is the
seat's kind, or `cloud` for a cloud-hosted identity and `local` otherwise.
Tenant-scoped like the roster: another organization's seats and token rows are
not listed.

#### `GET /api/agents/{label}/profile`

Scope: `CAPABILITY`, tool `skill_registry`, operator: the steward and chief of
staff profiles and the verified human operator, as `GET /api/skills-teams`
reads the same registry. Developer, research and public profiles answer 403. No
query parameters. 200:

```json
{"agent": "Local_Worker", "profile": "developer",
 "ceiling": {"profile": "developer", "tools": ["..."], "tool_count": 100},
 "skills": {"assigned": [{"name", "version", "hash", "source"}],
            "installed": null, "installed_known": false, "note": "..."}}
```

* `profile` is the label's default profile (`profiles.default_for_identity`:
  the code default, lowered by its seat or a committed overlay). That is its
  ceiling: an agent cannot activate a more privileged profile. `tools` lists
  every registered tool that profile exposes (`profiles.tool_is_allowed_for`);
  policy rules, approval gates and the agent-access switch still apply to each
  call.
* `skills.assigned` is the skill registry's resolution for the label
  (`skill_registry` action `resolve`: assignments to the label win over
  assignments to its profile), without the skill content. It is what dispatch
  injects.
* **Gap: installed skills.** The hub does not know which skills are installed
  on an agent's machine; no harness reports its skill directories. `installed`
  is `null` and `installed_known` is `false` until one does.

404 `not_found` for a label the hub's tenant does not know (no seat, chart
membership, configured label, token row or sighting).

#### `GET /api/agents/{label}`: never-connected agents and the first-connection wait

The existing detail route (scope unchanged: any signed-in identity) now
answers a label the hub's tenant knows but has never seen, instead of 404:

* `connection`: `connected` once the hub has recorded a sighting of the label
  (any authenticated request), else `never_connected`.
* `seat`: the label's `GET /api/agent-seats` row, or `null` (a sighted label
  outside the roster, such as the operator).
* For a never-connected label, `agent` has the registry row's shape with
  `status: "never_connected"` and null `last_seen`, `first_seen`, `host`,
  `pid` and `version`; `sessions` and `activity` are read as for any label.

A label the tenant does not know and the hub has never seen is still 404.

**Waiting for the first connection.** `?wait_for=connected` holds the answer
until the label is seen, and the add-agent page (`packages/web/src/app/agents/connectWait.ts`) loops on it
instead of polling: it asks again on `satisfied: false`, backs off with jitter on 429
and on failures, and aborts the read on unmount or an owner change.

| Parameter | Meaning |
|---|---|
| `wait_for` | `connected` (any other value is 400) |
| `timeout` | whole seconds, 0–25, default 25 (outside that range: 400) |
| `since` | ISO 8601 datetime: only a sighting at or after it counts. Pass the add answer's `issued_at`, so an earlier life of a re-added label does not count |

`timeout` or `since` without `wait_for` is 400. The hub re-reads the registry
every second (`routes.AGENT_WAIT_POLL_S`) and answers as soon as the label is
seen, or when the timeout passes, with the usual body plus

```json
"wait": {"for": "connected", "since": "...", "timeout_s": 25, "satisfied": true,
         "waited_s": 3.1}
```

`satisfied: false` means the timeout passed first; ask again. One process
holds at most 16 waits at once (`routes.AGENT_WAIT_MAX_WAITERS`); a further one
answers 429 `{"code": "rate_limited"}` at once. An unknown label answers 404 at
once, and a request that is already satisfied does not wait. Tenant scoping is
the plain read's: the seat lookup is tenant-scoped, while the sighting itself
comes from the `agents` registry table, which has no tenant column yet
(`tenant_tables.PARTITION_PENDING`), as for every existing reader of it. No event
on `GET /api/events` was used: a sighting writes no `event_log` row.

#### `GET /api/board?claimed_by=`

`claimed_by=<label>` keeps only task cards whose claim lock is that label
(exact spelling) and drops every delegation card. `agent=<label>` is unchanged:
it matches a claim or either end of a delegation, which is why the UI had to
filter `claimed_by` in the browser. A parent card stays when a nested subtask
matches, as for every board filter. `filters.claimed_by` echoes the value.
Scope unchanged (any signed-in identity).


### Existing routes the Agents area reuses (cutover parity, task #2479)

When the classic panes were deleted, the Agents area took over every action
they offered. These routes predate Control Center v2 and are reused as they
are; each answers **202** with `approval_id` when the write is parked, and the
pages treat that as waiting, never as done. None is replayed from storage.

| Route | Body | Page |
|---|---|---|
| `POST /api/action/operating-sprint` | `{op: add, name, profile?, org_unit?, overrides?, starts_at?, ends_at?}`, `{op: update, sprint_id, status?, profile?, overrides?}`, `{op: link\|unlink, sprint_id, task_id}` | `/agents/teams?tab=operating` |
| `POST /api/action/operating-run` | `{op: set, task_id, profile?, overrides?}` (replaces the run layer) or `{op: clear, task_id}` | `/agents/teams?tab=operating&run_task=<id>` |
| `GET /api/operating/sprints?status=`, `GET /api/operating/resolve?task_id=` | | same |
| `POST /api/action/fleet` | `{action: "restart_service", host, unit: "unified-agent-executor.service"}`; the hub's own host only | `/agents/hosts` |
| `POST /api/action/oauth-revoke` | `{device}` or `{session_id}`; approval-gated | `/agents/hosts` |
| `POST /api/oauth/passkeys/options`, `POST /api/oauth/passkeys`, `POST /api/oauth/passkeys/delete`, `GET /api/oauth/passkeys` | WebAuthn creation options, the signed registration with a label, `{credential_id}` | `/agents/hosts#approver-passkeys` |
| `POST /api/action/node-acceptance` | `{action: "attest", node, run_id, evidence_ref}` for a pending run whose checks all passed | `/agents/hosts` |
| `GET /api/config-revisions?kind=`, `POST /api/action/config-revision` | `{kind, revision}`: roll back; approval-gated | `/agents/org#policy-revisions` |
| `POST /api/action/skills-teams` | `{action: publish\|install\|team_install\|starter_preview\|starter_install, params}`; a 200 with `blocked`, `ok: false` or `error` is a refusal | `/agents/skills`, `/agents/teams?tab=registry` |

## settings (`webui/cc_settings.py`, task #2516)

### `GET /api/session-credentials`

The session-scoped MCP credentials (task #1744), for the verified human
operator's Settings page. Scope: operator only (`+operator`), no CSRF (a read),
`Cache-Control: no-store`. Query: `state` (`active` default: not revoked and
inside both the absolute and the idle window; `expired`; `revoked`; `all`),
`node`, `limit` (1..500, default 200). Unknown parameters are 400.

200 `{"credentials": [...], "count", "truncated"}`, newest first, tenant-scoped.
Each row is metadata only: `credential_id`, `agent_label`, `device_agent`,
`node`, `device_node`, `item_id`, `attempt`, `task_id`, `run_id`,
`acp_session_id`, `agent_session_id`, `tools`, `issued_at`, `idle_expires_at`,
`expires_at`, `last_used_at`, `revoked_at`, `revoke_reason` and `state`. The
credential value is never stored, and its digest is never returned.

### `POST /api/action/session-credential-revoke` (existing route)

Body: exactly one of `credential_id`, `item_id`, `node`. 200 `{"revoked":
[ids], "count", "credentials": [rows as above]}`: what this request revoked.
An optional `Idempotency-Key` (see "Keyed writes") replays that answer: a
repeat without the key revokes nothing new and answers `revoked: []`.

### `GET /api/evidence` paging (existing route)

The report is one page (`limit`, default 50, at most 500, newest task first).
Each page carries `next_cursor`: pass it back as `cursor` for the next page;
`null` on the last page. A malformed cursor is 400. `format=csv|pdf|markdown`
on this route still renders one page.

### `GET /api/evidence/export`

The complete export: every record matching the filters, with no 500-row cap.
Query: `format` (`csv` or `pdf`, required) and the `/api/evidence` filters
(`task_id`, `since`, `until`, `agent`, `project`, `pr`, `item`). Scope:
`CAPABILITY` tools `evidence_report`, operator; the evidence feature must be
enabled (403 otherwise).

* CSV streams one keyset page (200 records) at a time. Each page opens its own
  short-lived connection, and the first page's ledger verification is reused by
  every later page, so every row carries the same chain head, anchor and
  attestation columns.
* On a hub that signs its exports, the CSV is paged the same way into a private
  temporary file while an incremental signer hashes it; the file is then
  streamed with the signature headers (as on `/api/evidence`) and deleted. No
  full record list or body is held in memory.
* PDF is built whole first (the renderer needs every record), and signed when
  the hub signs exports.
* Every answer carries `x-evidence-complete: true` and a
  `content-disposition: attachment` filename.

## identity: `GET /api/whoami`

Existing route (`routes.whoami`), extended for spec §6. All earlier fields
are unchanged (`agent`, `via`, `csrf`, `hub_version`, `api_version`,
`enforce_auth`, `tokens_configured`, `control_enabled`,
`control_disabled_reason`). Added:

| Field | Value |
|---|---|
| `org` | the hub's tenant organization id (`edition.tenant_org_id()`, the value every write stamps on its rows), or `null` |
| `org_mode` | `"partitioned"` when `org` is set, else `"unpartitioned"` |

A hub serves one organization, so `org` is the same for every caller of that
hub. It is set only on an enterprise-edition hub with a configured
`WORKOS_ORGANIZATION_ID` (or `UA_HUB_WORKOS_ORGANIZATION_ID`).

## inbox (`webui/cc_inbox.py`, task #2514)

Routes and fields the Home and Inbox areas need. Approval reads are fields of the
existing `GET /api/approvals/{id}`; tasks finished in a window are a parameter of
the existing `GET /api/tasks`.

### `GET /api/approvals/{id}` — completeness, pass state, may I approve

Existing route, extended. Every earlier field is unchanged except `payload`,
which used to be the telemetry projection (`sanitize_metadata`: ten items per
list, ten keys per mapping, three levels, silently). It is now a complete
redacted preview (`telemetry.redaction.redacted_preview`): secret-shaped keys
and values are still `[REDACTED_SECRET]`, but nothing else is dropped up to
1,000 items per container, 12 levels, 20,000 characters per string, and one
1 MB budget that covers the preview and its omission metadata together. A value
left out is replaced by `[OMITTED]`; the text of a container below the depth
limit is never emitted, so nothing escapes key-based redaction.

| Field | Value |
|---|---|
| `payload_complete` | `true` when the preview leaves nothing out |
| `payload_omissions` | up to 50 `{path, kind, omitted}`: `kind` is `items` (list items or keys past 1,000), `depth` (a container past 12 levels), `chars` (a string past 20,000) or `bytes` (the rest once the budget ran out); `path` is `$` then `.key` / `[index]`, at most 256 characters with key segments cut at 64 |
| `payload_omissions_total` | number of omissions (the list above is capped) |
| `payload_redacted_values` | number of values replaced by `[REDACTED_SECRET]` |
| `pass` | `{state, expires_at, call_outcome}` (below) |
| `approver` | `{may_approve: "yes"\|"no"\|"unknown", reason}` for the caller (below) |

**An incomplete preview must be shown as incomplete.** When
`payload_complete` is `false` the UI says what was left out (from
`payload_omissions`) next to the Approve button; approving then means
approving arguments the person has not fully seen.

`pass.state` is `pending`, `live` (an approved pass inside its lifetime),
`expired` (approved, past `UA_APPROVAL_PASS_TTL_HOURS`; the gate retires it on
its next look), `consumed`, `denied` or `revoked`. `pass.expires_at` is set for
an approved pass. `pass.call_outcome` is set for a consumed pass:
`completed` or `failed` as recorded when the authorized call finished (event
channel `agent.approval.call_outcome`, keyed to the current consumption), or
`unknown` when no governed invocation recorded one (a consumption outside the
registered wrapper, or one made before this change).

`approver.may_approve` applies `approval_resolve`'s approve rule without
writing: `no` when the row is not pending, the caller is not a human identity,
or the caller is the requester under the hub's alias-aware `_same_person` (the
operator's login and their configured names are one person); `unknown` when
the request has no verified identity or the row names no requester; else
`yes`. It is a read: the approve call checks again.

### `GET /api/tasks?completed_after=`

Existing route, extended. `completed_after` is an ISO 8601 date or datetime (no
offset means UTC; 400 otherwise). The answer adds `completed` (done tasks with
`completed_at` at or after it, filtered in SQL by `task_list`, project-scoped
like the rest), `completed_after` (the bound, normalized to UTC seconds) and
`completed_truncated`. Without the parameter the answer is unchanged.

### `GET /api/work-proposals`, `GET /api/work-proposals/{proposal_id}`

Reads over `coordination_manage` `work_proposal_list` / `work_proposal_get`,
with the same action ceiling as the MCP read (the verified operator passes it)
and the tool's tenant fence.

* List query: `status` (`pending`, `approving`, `approved`, `rejected`,
  `withdrawn`), `proposer`, `limit` (1-200). Answer `{proposals: [...]}`,
  newest first, each with per-status item counts. Unknown parameters are 400.
* Detail: `{proposal: {..., items: [...]}}`; 404 for an unknown or
  other-tenant id.

Deciding stays on `POST /api/action/work-proposal-decide`.

### `POST /api/sessions/{id}/wake-decline`

The verified human operator refuses a session's wake request. Body
`{reason?}` (at most 500 characters). Answer `{session_id, requested, granted:
false, declined: true, declined_at, declined_by, decline_reason, changed}`;
404 for an unknown session, 403 for anyone but the operator.

A decline revokes any grant and outstanding offers, removes the request from
the operator inbox, and is bound to the session id: the session asking again
(or withdrawing and asking again) does not clear it. A later
`POST /api/sessions/{id}/wake-grant {grant: true}` clears it. The sessions read
reports `wake: {requested, granted, declined}`.

### `POST /api/action/review-hold`

The verified human operator releases or keeps a review hold. Body
`{task_id, decision: "release"|"keep", reason?}` (reason at most 2,000
characters). A hold is read from the task's notes the way the inbox reads it:
`red_tier_hold` (`[review: red-tier @ ...]`) or `held_pr`
(`[waiting-for: human-merge]` or `[review: held-...]`, no red-tier marker).

Answer 200: the decision row `{id, task_id, kind, hold_ref, decision, reason,
repo, pr_number, decided_by, decided_at, hold: {kind, marker, hold_ref},
changed}`. Repeating the current decision records nothing (`changed: false`).
`hold_ref` binds the decision to the exact marker it answers, so a new hold
marker is a new hold that needs a new decision.

* 409 `code: "red_tier_approval_required"`: a release of a red-tier hold.
  Releasing it would approve red-tier scope, so it is refused; approve the
  pull request's Approval Ledger request at its current head instead
  (`POST /api/action/approval`), or record `keep`.
* 409 `code: "no_hold"`: the task carries no hold, or is done or cancelled.
* 404: unknown task. 403: anyone but the operator. 400: bad body.

What reads the decision:

* the operator inbox (`needs_operator`) stops listing a hold whose current
  marker has a decision;
* the merge preflight (`python -m unified_agent.shipping_preflight_cli`) judges
  each hold on the pull request on its own: it refuses the merge while any
  hold's current decision is `keep` recorded at or after the approval it
  presents. A `release` of that same hold, or a fresh approval after the keep,
  clears it; a release of one hold never clears a keep on another.

Keeping a hold again after a Ledger approval superseded the earlier keep
records a fresh keep (`changed: true`), which the preflight then honours.

`release` is never an approval: the merge still needs the Ledger's head-bound
human approval.
