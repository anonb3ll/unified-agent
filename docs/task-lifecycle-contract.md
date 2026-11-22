# Task Lifecycle & Ownership Contract (Phase 0 / gate G0)

**Status: G0 APPROVED (the operator, 2026-08-07).**

This document is the written contract produced by Phase 0 of the two-part program
The operator approved:

- **(A)** migrate to a single desktop unified-agent hub reachable over Tailscale,
  with laptop agents as authenticated remote clients;
- **(B)** repair the task queue's ownership/lifecycle bugs as a first-class,
  non-deferred **prerequisite** to that migration.

Phase 0 is read-only investigation plus this contract. It contains **zero code
behavior changes, zero schema migrations, and zero task-queue row mutations**.
Nothing here has been implemented. Every recommendation below is explicitly
deferred to a later, separately-approved phase.

Most file:line citations were read from the source at commit `a5d5542` on
`main`, in the worktree `.worktrees/task-287-p0-lifecycle-contract`. Sections
with an inline **Refreshed** or **Implementation status** stamp were
re-verified against a later `main` revision; each stamp names the commit used.

---

## 0. Why this is a prerequisite, not a follow-up

The Tailscale migration turns "which agent is this?" from a local convenience
into a security boundary. The queue currently answers that question two
different ways that disagree with each other, and one of those answers already
feeds the filesystem sandbox.

Live, verified read-only against `~/.unified_agent/memory.db` during this
session (no rows written):

| Task | `status` | `claimed_by` column | notes say |
|---|---|---|---|
| #277 | `doing` | **NULL** | `[assigned-to: agent-claude]` … trailing `claimed-by: agent-claude` |
| #278 | `doing` | **NULL** | `[assigned-to: agent-codex]` … trailing `claimed-by: agent-claude` |
| #287 | `doing` | `agent-claude` | `claimed-by: agent-claude` |

#278 is the clean failure case: the *assignment* marker names `agent-codex`,
the *claim* marker names `agent-claude`, and the authoritative column is NULL —
three answers, none authoritative. #287 (this task) is the control: it was
claimed through `task_claim`, so the column is populated. The difference between
the rows is entirely which tool wrote them.

This is not two outliers. Across the whole live table:

- **177 rows** have `claimed_by IS NULL` while their notes contain a
  `claimed-by:` line.
- **8 rows** are `status='open'` while their title contains `[archived`.
- Status distribution: `done` 258, `open` 17, `doing` 3, `blocked` 1.

---

## 1. Canonical lifecycle

### 1.1 Current state

`STATUSES = ("open", "doing", "blocked", "done")` — `src/unified_agent/tools/tasks.py:13`.

Four values. They are enforced in three independent places, which is the first
thing that matters:

1. **Application validation**, `tools/tasks.py:238-239` (`task_update`),
   `tools/tasks.py:403-404` (`task_list`), `tools/export.py:104-105`
   (`export_tasks`, which imports `STATUSES` at `tools/export.py:21`).
2. **A SQLite `CHECK` constraint in the table definition** —
   `src/unified_agent/storage.py:97`:
   ```sql
   status TEXT NOT NULL DEFAULT 'open'
     CHECK (status IN ('open','doing','blocked','done')),
   ```
3. **An index built on the column**, `storage.py:111` and `storage.py:946`
   (`idx_tasks_status_priority`).

### 1.2 Adding a status value is NOT additive — it is a migration

This is the single most important finding in this section, and it contradicts
the natural assumption that new enum values are cheap.

SQLite cannot `ALTER TABLE … ALTER CONSTRAINT`. Changing the `CHECK` at
`storage.py:97` requires the full 12-step table-rebuild dance (create new table,
copy rows, drop old, rename, rebuild indexes and the `parent_id … ON DELETE
CASCADE` self-reference at `storage.py:99`). Any phase that widens `STATUSES`
without also rebuilding that table produces a runtime `IntegrityError` on the
first write of a new value — the application validation would pass and the
database would reject it.

Note also that `storage.py:97` is inside a `CREATE TABLE IF NOT EXISTS`, so
editing it changes behavior *only for fresh databases*. Existing deployments
keep the old constraint until an explicit migration runs. A change that looks
correct in tests (fresh DB) would silently not apply on the operator's live hub.

### 1.3 Consumers that would misbehave on an unrecognized value

I grepped every `status`-sensitive read across `src/`. These are the ones that
would silently do the wrong thing, not merely fail loudly:

**Silent-drop (worst category) — the web UI queue view.**
`src/unified_agent/webui/api.py:175-190` is an `if / elif / elif / elif` chain
with **no `else` and no fallback bucket**:

```
175:  if status == "blocked" or is_archived:      → blocked_archived
177:  elif is_laptop and status != "done":        → laptop_lane
179:  elif status == "doing":                     → doing
187:  elif status == "open":                      → desktop_open / waiting_review
```

A task in a new status (say `waiting_review` or `cancelled`) matches none of
these branches and is appended to **no list at all**. It vanishes from the
dashboard entirely while remaining live in the database. the operator's primary
visibility surface would stop showing exactly the tasks a richer lifecycle was
introduced to make visible.

**Hardcoded negative filters that would over-include.** These treat "not done"
as "active", so any new terminal-ish state (`cancelled`, `archived`) would be
counted as live work forever:

- `src/unified_agent/sync_cli.py` — `WHERE status != 'done'` still feeds the
  optional `--include-snapshots` dump. Default ua-sync no longer injects that
  block (task #564); cancelled/archived rows would still leak if snapshots are
  re-enabled without a status filter.
- `src/unified_agent/monitor.py:487` — `review_wait` rule: `AND status!='done'`.
- `src/unified_agent/tools/tasks.py:621` — `task_claim`'s conditional UPDATE
  guards on `status != 'done'`, so a `cancelled` or `archived` task would remain
  claimable.

**Hardcoded positive filters that would under-include.** These would stop seeing
tasks that moved into a new state:

- `src/unified_agent/monitor.py:437` — `stale_doing_task`: `WHERE status='doing'`.
- `src/unified_agent/monitor.py` — `agent_never_completes` (formerly
  `task_never_completes`): an agent who has claimed at least the threshold
  number of distinct tasks, with none of those tasks `done` or `cancelled`.
  `cancelled` counts as completed, so cancelling a task no longer leaves the
  rule firing.
- `src/unified_agent/reviewer.py:383`, `:416`, `:530` — the review pipeline
  scans `WHERE … status IN ('doing', 'blocked')`. A `waiting_review` status
  would make tasks invisible to the reviewer that the status exists to feed.
- `src/unified_agent/tools/doctor.py:62` and
  `src/unified_agent/executor/daemon.py:335` — the two independent stale-lock
  reapers both select `WHERE status='doing'` and reset to `status='open'`
  (`doctor.py:75`, `daemon.py:341`).
- `src/unified_agent/safety.py:38` — see §2.3; this one is a sandbox boundary.

**Transition-keyed logic that assumes the 4-value graph.** `tools/tasks.py:328`
(`entered_doing`), `:334` (`status == "done"` releases the worktree), `:365`
(fires delegation completion callbacks) and `:280-284` (`completed_at` set on
`done`, cleared on any other status). Note `:283-284`: *any* status change other
than `done` nulls `completed_at`. Moving a finished task to a new `archived`
status would erase its completion timestamp, which `monitor.py:181` reads.

**Event-log string matching.** `monitor.py:514` and `:541` detect claims by
`e.summary LIKE '%->doing%'` against free-text summaries written at
`tools/tasks.py:354` and `:643`. Renaming or adding claim-bearing states
silently breaks both the `task_reassignment_thrash` and `agent_never_completes`
rules without any error.

Also note there are already four *other* status enums in the same database with
different vocabularies — `plan.py:24-25` (`STEP_STATUSES`, `PLAN_STATUSES`),
`delegate.py:21` (`DELEGATION_STATUSES`), `registry.py:41`
(`_SESSION_OPEN_STATUSES`), plus the CHECK constraints at `storage.py:145`,
`:161`, `:335`, `:507`, `:526`. `DELEGATION_STATUSES` already contains
`cancelled`; `PLAN_STATUSES` already contains `abandoned`. Any new task
vocabulary should be chosen for consistency with these, not in isolation.

### 1.4 Proposed lifecycle

Rather than the full nine-state proposal, I recommend the **smallest widening
that removes a currently-untyped state**, because each added value costs one
table rebuild and an audit of all ~15 call sites above:

| State | Meaning | Replaces today's workaround |
|---|---|---|
| `open` | filed, unclaimed, actionable | — |
| `doing` | claimed, in flight, lock held | — |
| `waiting_review` | work complete, awaiting review/merge | `notes` marker `[waiting-for: review]` (`reviewer.py:383`, `monitor.py:486`, `daemon.py:731`) |
| `blocked` | cannot proceed | — |
| `done` | completed | — |
| `cancelled` | terminal, will not be done | today: `done` with an explanatory note, which corrupts throughput metrics |

I deliberately do **not** propose `planned`/`ready`/`claimed`/`archived` as
statuses:

- `claimed` is redundant — `claimed_by`/`claimed_at`/`lock_expires_at`
  (`storage.py:104-106`) already carry it as typed columns, and `task_claim`
  already sets `status='doing'` atomically with them (`tools/tasks.py:620`).
- `planned`/`ready` duplicate what `priority` and the dependency graph
  (`dependency_add`/`dependency_list`) express.
- `archived` should be a **separate orthogonal field**, not a status — see §4.
  Archival is not mutually exclusive with `done`, and modelling it as a status
  would force the two facts to overwrite each other.

**Concrete change list if this were approved (later phase, not now):**

1. Migration: rebuild `tasks` with the widened `CHECK` (`storage.py:97`),
   preserving `parent_id` FK, both indexes (`storage.py:111`, `:946`), and all
   258+ existing rows. Backfill nothing — every existing row keeps its status.
2. `tools/tasks.py:13` — widen `STATUSES`.
3. `webui/api.py:175-190` — **add a terminal `else` bucket first, before any
   status is added**, so an unknown value degrades to visible-but-uncategorized
   rather than invisible. This is safe to do independently and is the single
   highest-value defensive fix in the codebase.
4. `sync_cli.py:189` — replace `status != 'done'` with an explicit active-set
   list.
5. `monitor.py:487`, `:555`, `tools/tasks.py:621` — replace `!= 'done'` /
   `= 'done'` with explicit terminal-set membership.
6. `reviewer.py:383`, `:416`, `:530` — add `waiting_review` to the scan set and
   plan the migration off the `[waiting-for: review]` notes marker (the marker
   must keep working during the transition; `monitor.py:482-485` already
   documents that the `LIKE` is a coarse pre-filter).
7. `tools/tasks.py:283-284` — stop nulling `completed_at` on transitions between
   terminal states. **Partially done (Q3, #292):** `done`→`done` now writes
   `completed_at=COALESCE(completed_at, ?)`, so re-asserting completion
   preserves the original timestamp (and backfills a NULL) instead of
   restamping it. Adding a second terminal status still needs the `elif`
   widened past a bare `status is not None`.
8. `doctor.py:62-75`, `daemon.py:335-341` — confirm the reapers still only
   target `doing`.
9. `tools/export.py:104` — inherits automatically via the import at `:21`.

---

## 2. Ownership / claim authority rule

### 2.1 The rule

> **`task_claim` is the sole path that may set the `claimed_by` column.
> The `claimed_by` column is the sole authority on task ownership.
> A `claimed-by:` line in `notes` is descriptive text with no authorization
> meaning and must never be synthesized by the server.**

### 2.2 Why the current code violates it

`task_claim` (`src/unified_agent/tools/tasks.py:548`) is already correct and
should not be changed. It:

- verifies the caller's identity — `:562`, `config.verify_write_identity`;
- evaluates lock expiry — `:591-599`;
- rejects a live foreign claim with a message naming the canonical owner and
  expiry — `:602-606`;
- performs **one atomic conditional UPDATE** keyed on the column — `:618-623`:
  ```sql
  UPDATE tasks SET status='doing', claimed_by=?, claimed_at=?, lock_expires_at=?, notes=?, updated_at=?
  WHERE id=? AND status != 'done' AND (claimed_by IS NULL OR claimed_by=? OR lock_expires_at < ?)
  ```
- detects zero-rowcount as a lost race — `:624-625`;
- distinguishes renewal from re-claim to avoid flooding the anomaly rules —
  `:636-646`.

The gap is elsewhere. **`task_update` (`tools/tasks.py:204`) never writes
`claimed_by` at all.** Its SET-list is built at `:262-279` from exactly
`status`, `title`, `notes`, `priority`, `untrusted`, `parent_id` (plus
`updated_at` and `completed_at`). `claimed_by` is not in it.

What `task_update` *does* do is worse than nothing — `tools/tasks.py:243-250`:

```python
if notes is not None:
    claimed = _claimed_by(notes)
    verified = config.verify_write_identity(claimed)
    if verified is not None and claimed is None:
        if notes.strip():
            notes = notes.rstrip() + f"\n\nclaimed-by: {verified}"
```

Any notes edit by an authenticated caller, on **any** task, silently appends a
`claimed-by: <caller>` line if one isn't already present. This is a free-text
mutation with no authorization semantics whatsoever: it does not check whether
the caller holds the claim, does not check whether someone else does, and does
not touch the authoritative column. `task_add` has the identical behavior at
`:147-153`, and its INSERT at `:173-186` likewise omits `claimed_by` — so every
task is born with a NULL column and, often, a notes line asserting ownership.

That is exactly the #278 pattern, and it explains all 177 rows.

The reconciliation helper `_row()` at `tools/tasks.py:24-29` papers over it:

```python
if "claimed_by" in d and d["claimed_by"] is None:
    d["claimed_by"] = _claimed_by(d.get("notes"))
```

So `task_list`/`task_update` *return* a plausible `claimed_by` parsed from notes
via `_CLAIMED_BY_RE` (`:21`) even when the column is NULL. Every read path looks
healthy. The divergence is invisible until something queries the column
directly in SQL — which is precisely what the enforcement paths do.

### 2.3 The consequence that makes this a security prerequisite

`src/unified_agent/safety.py:23-47`, `get_active_worktree_path`, selects the
sandbox root for `fs_*`/`run_cmd` — `safety.py:38`:

```sql
SELECT worktree_path FROM tasks WHERE status='doing' AND claimed_by=? AND worktree_path IS NOT NULL
ORDER BY updated_at DESC LIMIT 1
```

It queries the **column**, not the notes, and there is no `_row()` fallback in
this path. For a task claimed via `task_update`, `claimed_by` is NULL, this
returns nothing, and `resolve_in_workspace` (`safety.py:52-77`) falls back to
the bare workspace root instead of redirecting into the task's worktree.

The isolation guarantee that COLLABORATION.md Rule 2 depends on therefore does
not engage for 177 of the queue's rows. Under a single local trusted user this
is untidy. Once laptop agents are authenticated remote clients over Tailscale,
the same NULL means a remote agent's writes are not confined to its task
worktree. **This is why (B) must land before (A), not after.**

### 2.4 Root cause is documentation, not just code

`~/Projects/agent/projects/COLLABORATION.md:35-45` (Rule 1 — "Claim before you
code") instructs agents to do exactly the broken thing:

> 2. Immediately `task_update(id, status="doing")` and prepend to notes:
>    `claimed-by: <agent> @ <UTC timestamp>`.

And `COLLABORATION.md:86-88` still states as a known limit:

> Task claiming is not atomic (`task_list` then `task_update` can race).

That was true before `task_claim` existed. It is now stale, and it is actively
steering every agent — including me, and including whoever claimed #277/#278 —
onto the path that produces NULL columns. **Fixing the code without fixing
Rule 1 will not stop the bug from recurring.**

### 2.5 Smallest safe fix

Ordered by risk, lowest first. Recommended to do (a) and (b) in the first
implementation phase and defer (c).

**Implementation status (2026-08-07, post-G0):** (a) and (b) are done. (a)
shipped in task #288 (merge commit `b3fd1a3`) — `task_update`/`task_add` no
longer synthesize `claimed_by` text; live-verified against the running hub.
(b) is done — COLLABORATION.md Rule 1 now mandates `task_claim(id,
agent=...)` and the stale "not atomic" known-limit line is gone, both at the
source of truth (`docs/COLLABORATION.md`) and the deployed copy
(`~/Projects/agent/projects/COLLABORATION.md`), which had drifted out of
sync and was re-synced as part of this update. (c) remains deferred, as
recommended — no claim-hold precondition has been added, and the NULL-column
backfill it depends on has not been proposed or run.

**(a) Delete the notes auto-synthesis.** Remove `tools/tasks.py:243-250` and
`:147-153`. The server stops inventing ownership assertions. Callers may still
write a `claimed-by:` line by hand — it just stops being something the server
does behind their back. Low risk; the only readers of the synthesized line are
`_claimed_by` fallbacks that would then correctly report "unclaimed".

**(b) Update COLLABORATION.md Rule 1** to mandate `task_claim(id, agent=...)`
and delete the stale "not atomic" known limit. Per COLLABORATION.md Rule 4
(`:80-82`) that file is the operator-owned, so this is proposed, not edited.

**(c) Require the caller to hold the claim before notes edits proceed.**
Attractive but *not* recommended for the first phase: with 177 rows currently
holding NULL columns, a hold-the-claim precondition would immediately reject
edits to most of the live queue. It should only follow a deliberate backfill,
and a backfill is itself a row mutation requiring the operator's approval (§5).

Explicitly **not** recommended: having `task_update` write `claimed_by` when it
sees a notes claim line. That would launder unverified free text into the
authoritative column and into `safety.py:38`.

---

## 3. Permission matrix

Grounded in `config.py`, `profiles.py`, and `safety.py` as read this session.

### 3.1 The mechanism (what actually enforces anything)

- **Identity** comes only from a bearer token. `config.verified_agent_identity()`
  (`config.py:477-479`) reads a ContextVar bound by the HTTP middleware.
  `config.agent_tokens()` (`config.py:548-553`) parses `UA_AGENT_TOKENS` into
  `{token: label}`. Under stdio there is no token, so identity is `None`.
- **Claimed identity is checked, not trusted** —
  `config.verify_write_identity()` (`config.py:487-508`). If the caller claims a
  label that differs from the verified one, behavior depends on
  `UA_ENFORCE_AUTH` (`config.py:497`): `1` raises `ToolError`; **anything else
  only prints a warning to stderr and proceeds** (`config.py:504-506`). Note the
  return at `config.py:492-494`: when there is **no** verified identity, the
  function returns the caller's *claimed* label unchanged. Under stdio, or with
  auth not enforced, an agent may assert any label it likes.
- **Workspace binding** — `config.agent_workspaces()` (`config.py:556-571`)
  maps a *verified* label to a root; `config.workspace_root()`
  (`config.py:60-72`) prefers that per-request override over the process-wide
  `UA_WORKSPACE`. The docstring at `config.py:565-567` is explicit that it never
  trusts a client-supplied path.
- **Path confinement** — `safety.resolve_in_workspace` (`safety.py:52-77`)
  `.resolve()`s and rejects anything escaping the root, symlinks included
  (`safety.py:75-78`); `resolve_in_vault` (`safety.py:89-116`) does the same for
  the vault. `_HOOK_DIRS` (`safety.py:20`) blocks `.git`, `.hg`, `.svn`,
  `.husky` — the dirs holding scripts that execute unattended.
- **Worktree redirection** — `safety.get_active_worktree_path`
  (`safety.py:23-47`), keyed on `claimed_by`. See §2.3 for why it currently
  under-fires.
- **Untrusted marking** — `config.untrusted_agents()` (`config.py:776-784`)
  parses `UA_UNTRUSTED_AGENTS`; `config.is_agent_untrusted()`
  (`config.py:787-791`) is a plain set membership test. Applied in
  `tools/tasks.py:156` and `:253`, and propagated parent→child at
  `tools/tasks.py:170` and `:301-311`.
- **Refreshed (2026-08-24, PR #282, `main` @ `9d8331b`):** operator/research
  profile defaults landed after the Phase 0 snapshot; the Profiles bullet and
  `Desktop_Person` matrix row below cite this revision, not `a5d5542`.
- **Profiles** — `profiles.py` defines `developer`, `steward`,
  `chief-of-staff`, `research`, and `operator`. `DEFAULT_PROFILES_BY_IDENTITY`
  (`profiles.py:42-46`) maps `agent-hermes` and `Laptop_Agent5` to
  `chief-of-staff` and `Desktop_Person` to `operator`; every other identity
  defaults to `developer` (`profiles.default_for_identity`). Profile governs
  vault write scope and, for authenticated HTTP sessions, server-side tool
  exposure. `vault_write_dirs()` (`profiles.py:393-406`): chief-of-staff gets
  the full PARA tree (`COS_WRITE_DIRS`), research gets Inbox and Logs only,
  operator has no vault write dirs (`OPERATOR_WRITE_DIRS` is empty), and
  developer and steward share `UA_VAULT_WRITE_DIRS` (default Inbox / Logs /
  Wiki; steward keeps `vault_write` per task #544). `profiles.current()`
  (`profiles.py:380-390`) never trusts a client-supplied label and re-binds to
  the verified identity + MCP session.

### 3.2 The matrix

**Important caveat, stated plainly:** tool exposure is now enforced for authenticated
HTTP sessions through the active profile deny lists. Stdio and legacy unauthenticated
mode retain the full surface for compatibility. The differentiation below still
includes protocol and convention from CLAUDE.md and COLLABORATION.md. The token roster
and any workspace bindings live in `~/.unified_agent/service.env` (referenced by
the systemd unit's `EnvironmentFile`), which I deliberately did **not** read —
it holds bearer tokens.

| Identity | Role (protocol) | Hub tools | Filesystem scope | Denied | Repo/worktree boundary |
|---|---|---|---|---|---|
| `agent-claude` | Developer (Claude Code) | Profile-filtered HTTP surface; full stdio compatibility surface | `UA_WORKSPACE` = `~/Projects/agent`; worktree redirect via `safety.py:38` when claim held | Outside workspace root (`safety.py:75`); `_HOOK_DIRS` (`safety.py:20`); profile-denied high-impact tools; vault outside Inbox/Logs/Wiki (`profiles.py`) | Code work in `~/dev/<repo>` and `.worktrees/<task>/` per COLLABORATION.md:50-66; never `main` of a shared checkout |
| `agent-codex` | Developer, standing (Lane A, `~/dev/unified_agent`) | Profile-filtered HTTP surface; full stdio compatibility surface | Same | Profile-denied high-impact tools; same filesystem limits | Same |
| `agent-agy` | Developer (Antigravity); Browser lane when assigned | Profile-filtered HTTP surface; full stdio compatibility surface | Same | Profile-denied high-impact tools; same filesystem limits | Same |
| `agent-hermes` | CTO / Head Engineer: technical direction, implementation and technical review (task #1788) | Profile-filtered HTTP surface; `developer` by default | Same as the developer rows | Profile-denied high-impact tools (approvals, merge, notify, routines, authority config); vault outside Inbox/Logs/Wiki (`profiles.py`) | Same as the developer rows; merges go through the deploy steward |
| `agent-laptop-agent1` | Developer, laptop lane | Profile-filtered HTTP surface; full stdio compatibility surface | Per-agent root via `UA_AGENT_WORKSPACES` if bound (`config.py:556`), else process `UA_WORKSPACE` | Profile-denied high-impact tools; same filesystem limits | Laptop-lane tasks marked in notes (`tools/tasks.py:124-126`) |
| `agent-laptop-agent3` | Developer, laptop lane | Profile-filtered HTTP surface; full stdio compatibility surface | Same | Profile-denied high-impact tools; same filesystem limits | Same |
| `Laptop_Agent5` | Chief of Staff, laptop | Full surface; `chief-of-staff` by default | Full PARA vault write set | Writing/running project code remains protocol-only | Same |
| `Desktop_Person` | Human, web UI operator | `operator` by default (`OPERATOR_ALLOWED_TOOLS`); bearer token, deliberately unregistered as an agent | `UA_AGENT_WORKSPACES` DENY — agent-facing `fs_*` / `run_cmd` raise; hub census uses `process_workspace_root()` | No vault writes (`OPERATOR_WRITE_DIRS` empty) | Approvals + read-only status; not an executing agent |

### 3.3 Label space is unconstrained

There is no enum of agent labels. `_CLAIMED_BY_RE` (`tools/tasks.py:21`) accepts
any `[A-Za-z0-9][\w.-]*`. The live `agent_events` table contains, alongside the
eight identities above: `Operator`, `Claude Code (Omnigent spike)`,
`agent-claude (steward)`, `pi_worker`, `unified-agent-cron`. Note that
`agent-claude (steward)` would be truncated by `_CLAIMED_BY_RE` to
`agent-claude` if it ever appeared in a claim line — a label with a space
silently becomes a different label. Any future per-identity permission logic
needs a canonical label list first.

---

## 4. Archival / visibility disposition

### 4.1 Current state

Archival is a **title string convention**. `webui/api.py:165-169`:

```python
is_archived = (
    "[archived" in title.lower()
    or bool(task.get("notes_archived", False))
    or "[archived" in notes_str.lower()
)
```

with `notes_archived` computed in SQL at `tools/tasks.py:127` as
`instr(lower(notes), '[archived') > 0`. Archived tasks are then folded into the
same `blocked_archived` bucket as genuinely blocked work
(`webui/api.py:175-176`) — two different facts rendered as one.

Live: **8 tasks** carry `[archived` in the title while `status='open'`. To every
SQL consumer that filters on status they are open, actionable work. In
particular `sync_cli.py:189` (`WHERE status != 'done'`) pulls them into the
generated *Active Tasks* block in `CLAUDE.md` — which is why archived tasks are
currently visible in every agent's system prompt, including this session's. One
of them (#40) has had `[ARCHIVED — do not surface]` written into its title in an
attempt to make the convention self-enforcing, which is itself evidence the
convention doesn't work.

### 4.2 Proposal (PROPOSAL ONLY)

Add one **additive, nullable** column:

```sql
ALTER TABLE tasks ADD COLUMN archived_at TEXT;
```

`ALTER TABLE … ADD COLUMN` is supported by SQLite in place — unlike the `CHECK`
change in §1.2, this genuinely is a cheap additive migration, and it is the
reason I prefer `archived_at` over a `visibility` enum: a timestamp is
additive, orthogonal to `status`, self-documenting about *when*, and needs no
constraint change. Archival then composes with any status (`done` + archived,
`blocked` + archived) instead of overwriting it.

Surfaces: a `archived: bool` filter on `task_list` defaulting to excluding
archived rows; a distinct `archived` bucket in `webui/api.py`, separated from
`blocked`; `sync_cli.py:189` excluding `archived_at IS NOT NULL`.

### 4.3 Explicit non-migration statement

**Existing rows MUST NOT be migrated, reclassified, or backfilled in this
phase.** The 8 title-convention rows keep their titles and their `status='open'`
until the operator approves a backfill as a separate, explicitly-authorized step.
Adding the column and backfilling it are two decisions, and only the first is
mechanical. The title convention must keep working during any transition —
`webui/api.py:165-169` should read `archived_at OR` the existing string checks,
not replace them.

**Implementation status (2026-08-07, post-G0):** the `archived_at` column
landed in task #288 (merge commit `b3fd1a3`). The non-migration statement has
held: live-checked all 8 title-convention rows (#40, #223, #225, #240, #241,
#242, #243, #244) and every one still has `archived_at IS NULL` — the column
exists but nothing has been backfilled. A backfill remains a separate,
not-yet-requested decision.

---

## 5. Approval authority

### 5.1 Developer-owned (no approval needed)

- Ordinary lifecycle transitions on a task the agent **holds a claim on**:
  `open → doing` (via `task_claim`), `doing ↔ blocked`, `doing → done`.
- Filing new tasks (`task_add`), editing one's own task notes, adding
  dependencies.
- Lock renewal on a held task (`tools/tasks.py:636`).
- Creating and removing one's own task worktree in `~/dev/<repo>/.worktrees/`.

### 5.2 Requires the operator's explicit approval

**Red-tier by file content.** Canonical list:
[`cycle_eligibility.RED_TIER_PATTERNS`](../src/unified_agent/cycle_eligibility.py)
(`reviewer` re-exports it). Agent-instruction files, config, deploy, hooks,
the security boundary (`SECURITY.md`, `security/`, `security.py`,
`security*.py`, `safety.py`, `server.py`), and the review pipeline's own
evidence and authority surfaces (`affected_tests.py`, `cycle_eligibility.py`,
`policy.py`, `audit/rules.py`, CI workflows). Matched by
`cycle_eligibility._is_red_tier`. Docs and changelog fragments are not
red-tier. Mirrored in [COLLABORATION.md](COLLABORATION.md#red-tier-files-require-human-approval).
Note that **this entire Phase-0 program touches red-tier paths by definition**
— §1 changes `storage.py` and §2.5 changes agent-behavior docs, and §3's
identity work touches `safety.py` and `config.py`. No part of the
implementation phases can be auto-merged.

**Red-tier by category, for this program specifically:**

| Action | Why gated |
|---|---|
| Any change to `UA_AGENT_TOKENS` / token issuance / rotation | Identity is the whole security boundary (`config.py:548`) |
| Any change to `UA_ENFORCE_AUTH` | `config.py:497` decides whether a mismatched claim raises or merely warns |
| Any change to `UA_UNTRUSTED_AGENTS`, `UA_AGENT_WORKSPACES` | Live in `~/.unified_agent/service.env` |
| Any change to `UA_AUDIT_ROOTS` | Lives in the `audit-roots.conf` systemd drop-in, **not** `service.env` (moved 2026-08-11 — it was set in both, so the winner was accidental). Replace-not-append, and there is no code default: `config.audit_roots()` returns `[]` when unset, so the effective set is workspace + this list (`safety.py:147-148` prepends the workspace) |
| `systemctl --user restart unified-agent` | COLLABORATION.md:186-188, Rule 6 — explicitly the operator/steward-gated |
| Client `.mcp.json` / MCP registration changes | Project scope silently shadows the hub |
| Disabling the laptop hub (program phase A) | Irreversible for remote agents mid-session |
| **The `tasks` table rebuild in §1.2** | Full-table rewrite of 279 rows; not transparently reversible |
| **Any backfill of `claimed_by` across the 177 NULL rows** | Retroactively assigns ownership from unverified free text into a column that `safety.py:38` treats as a sandbox boundary |
| **Any `archived_at` backfill** (§4.3) | Reclassification of live queue state |
| Editing `COLLABORATION.md` or `projects/README.md` | COLLABORATION.md:80-82 — the operator-owned; agents propose as a task |

**Structurally forbidden regardless of approval** — COLLABORATION.md:181-195,
Rule 6: an agent self-deploying a change to its own or another agent's
behavior, permissions, or security posture is to be treated as a security
incident. That covers an agent granting itself a token, widening its own
workspace binding, or removing itself from `UA_UNTRUSTED_AGENTS`.

---

## 6. Open questions for the operator

These are genuinely unresolved from code and docs. I have not guessed at them.

**Status as of 2026-08-08: all seven answered/resolved by the operator.** See each
item below; §7 records the resulting implementation.

**Q1 — What should happen to the 177 rows with NULL `claimed_by`?** RESOLVED:
option (iii) — hand-fix only rows currently `doing` after confirming each
owner; never backfill `done` rows from notes text. As of 2026-08-08 the queue
has zero `doing` rows, so there was nothing to hand-fix; the policy stands for
future occurrences.
Three options, each with a different failure mode. (i) Leave them; they never
get worktree redirection (`safety.py:38`) and the divergence persists.
(ii) Backfill from the notes line; this launders unverified free text into the
authoritative column — and #278 shows the notes are *wrong* at least sometimes
(notes say `agent-claude`, assignment says `agent-codex`). (iii) Backfill
only the 3 `doing` rows by hand after you confirm each owner, and leave the
`done` rows alone. I lean (iii) but this is your call, not mine.

**Q2 — Specifically, who actually owns #277 and #278?** RESOLVED (the operator,
2026-08-08, direct): agent-claude. Recorded on #278 itself; no column
backfill needed since `claimed_by` was already null on the completed task.
#278's notes contain both `[assigned-to: agent-codex]` and
`claimed-by: agent-claude`. I did not touch either row. Someone needs to say
which is authoritative before any backfill.

**Q3 — Is `Laptop_Agent5` getting the `developer` profile intentional?**
**Refreshed (2026-08-24, PR #282):** removed the stale contradictory tail that
still claimed `Laptop_Agent5` mapped only via `agent-hermes`. RESOLVED —
turned out to already be fixed: `profiles.py`'s
`DEFAULT_PROFILES_BY_IDENTITY` was widened to include `Laptop_Agent5` →
`CHIEF_OF_STAFF` in commit `df974f3` ("fix(identity): preserve Hermes role
across hosts", 2026-08-06), before this question was even written. No further
change needed.

**Q4 — Should `UA_ENFORCE_AUTH` be `1` before the Tailscale cutover?**
RESOLVED (the operator, 2026-08-08): yes, but at cutover time, not now — flip it
immediately before the Tailscale cutover (not during this session), then watch
logs for mismatched-identity warnings before trusting it. Deferred; not
actioned this session.
`config.py:497-506`: unless it is `1`, an identity mismatch only prints a
warning and proceeds with the verified identity. I could not determine its
current live value without reading `service.env`, which I chose not to do. Once
laptop agents are remote clients this seems like it must be `1`, but flipping it
may break callers that currently pass mismatched labels — which is exactly what
COLLABORATION.md Rule 1 encourages.

**Q5 — Is `waiting_review` worth a status, or should the notes marker stay?**
RESOLVED (the operator, 2026-08-08): yes, promote it. Implemented 2026-08-08 — see
§7.
The marker `[waiting-for: review]` is load-bearing in at least four places
(`reviewer.py:383`, `:530`, `monitor.py:486`, `daemon.py:731`) and
`monitor.py:479-485` already documents workarounds for its ambiguity with prose
mentions. Promoting it to a status is cleaner but costs the table rebuild and a
dual-read transition period.

**Q6 — Should `cancelled` exist, and what happens to the tasks that were
`done`-with-an-excuse?** RESOLVED (the operator, 2026-08-08): add `cancelled` going
forward; do not reclassify historical `done` rows. Implemented 2026-08-08 —
see §7.
existing mis-classified rows would stay `done` unless you approve a
reclassification (which §4.3's principle says we should not do unasked).

**Q7 — Does the ordering hold?** RESOLVED (the operator, 2026-08-08): agreed — and
moot, since Gate B (claim-authority fix, task #288) was already done and
deployed before this question was asked.
This contract asserts (B) gates (A) on the
strength of §2.3. If you disagree that the `safety.py:38` sandbox gap is
migration-blocking, the whole sequencing changes and this document should be
re-scoped.

---

## 7. Implementation record (2026-08-08)

Following the operator's Q1–Q7 answers above, the lifecycle widening proposed in
§1.4 was implemented: `waiting_review` and `cancelled` added to `STATUSES`,
full table-rebuild migration per §1.2, and every consumer in §1.3's audit
updated. `tests/test_tasks.py` has since been split into `tests/test_task_*.py`
(claim, completion, liveness, and the other task modules). `tests/test_storage.py`
still covers storage. `agent_never_completes` is covered in
`tests/test_monitor.py`. See those files for regression coverage, and the
commit introducing this section for the exact diff.

---

## Provenance

- Read-only. No task row was created, updated, deleted, or claimed by the
  investigation. The live DB was opened `mode=ro`.
- No service, token, or client configuration was read or modified.
  `~/.unified_agent/service.env` was deliberately not read.
- No source file was modified. This document is the only file added.
- Nothing pushed, nothing merged.
