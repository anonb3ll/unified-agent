# Skills registry (task #1353, Parity 6)

## The gap this closes

We already lint an individual skill (`skill_review`, `tools/skills.py`), but
skills themselves were hand-synced per surface: the cockpit's
`~/agent-workspace/.claude/skills` is tracked in git and pushed to both remotes,
while `~/.claude/skills`, `~/.agents/skills`, and similar per-machine
directories are symlinks or manual copies. There was no org-wide record of
*which version of which skill is where*, and no way for dispatch to hand an
agent a skill it doesn't already have on disk.

This is the registry kernel: immutable versions, assignment to an agent or a
profile, a pure resolver dispatch injects from, and a read-only drift report
for checking a surface directory against the registry. It does not build a
catalog UI, an installer, or a Skill Studio-style editor — that is task
#1424, described below.

## The object

Module: [`src/unified_agent/skills_registry.py`](../src/unified_agent/skills_registry.py).
Same shape as [`goals.py`](../src/unified_agent/goals.py): a small,
self-contained platform object with its own `ensure_schema(conn)`, called
from `storage.py` next to `goals.ensure_schema` / `task_comments.ensure_schema`.
No `SCHEMA_VERSION` bump — two other tables land the same way this task does.

Two tables:

- **`skill_versions`** — one row per published version. `(name, version)` is
  unique and versions are monotonically increasing per name, starting at 1.
  Each row carries a `content_hash` (SKILL.md SHA-256 for single-file packages,
  canonical package-manifest SHA-256 for multi-file packages), the `source` path it was published from, the `content` itself (so a
  resolver never has to re-read disk), `published_by`, and `published_at`.
  Rows are never updated or deleted (enforced by SQLite triggers) — a new version
  is always a new row. Nullable package-manifest and captured-payload columns extend
  this same version object; legacy rows remain unattested.
- **`skill_assignments`** — one row per `(skill_name, target_type, target)`.
  `target_type` is `agent` (an agent label like `agent-claude`) or
  `profile` (a profile name like `developer`). `pinned_version` is nullable;
  null means "whatever is latest when resolved." Re-assigning the same
  `(skill_name, target_type, target)` updates the row in place — assignment
  is a pointer, not a history.

## Lifecycle

1. **Publish** (`publish(name, path, author)`): runs `skill_review(path)`
   first. Any `high`-severity finding blocks the publish — the function
   returns `{ok: false, blocked: true, review}` with the findings rather than
   raising, so a caller can show them and retry after fixing the skill.
   `medium`/`low`/`info` findings do not block. On a pass, the function checks
   that `SKILL.md` did not change during review, then hashes the reviewed
   content and either:
   - finds an existing version for that `(name, content_hash)` pair and
     returns it unchanged (`idempotent: true`) — publishing the same content
     twice never mints a new version; or
   - inserts the next version number for that name.
   A `SKILL.md` that is not valid UTF-8, larger than 64 KiB, or contains
   detected credential/private-key material is rejected before it enters the
   registry. `get` also checks the stored body against the same limit before
   returning it.
2. **Assign** (`assign(skill_name, target, target_type, version?, author)`):
   points a skill at an agent label or a profile. Requires at least one
   published version to exist for that name (nothing to assign otherwise).
   **Unassign** (`unassign(...)`) removes the pointer; it is idempotent
   (`removed: false` if there was nothing to remove).
3. **Resolve** (`resolve(agent_label, profile?)`): the reader every other
   piece of this feature is built on — see below.
4. **Drift** (`drift(surface_dir, agent_label?, profile?)`): the reader a
   surface's sync state is checked against — see below.

## The resolver — the stable reader API for #1424

`resolve(agent_label, profile=None) -> {agent_label, profile, skills}` is a
**pure function**: given a label (and, if resolvable, its profile default via
`profiles.default_for_identity`), it returns the skills assigned to it —
agent-level assignments win over profile-level ones for the same skill name.
Each entry is `{name, version, hash, source, content}`, with `content`
capped at `CONTENT_CAP` (4000 chars) and the list capped at
`MAX_RESOLVED_SKILLS` (20) entries. Selection is deterministic: direct agent
assignments come first in name order, then profile assignments in name order;
a direct assignment replaces the profile assignment for the same skill name.
When the cap is reached, later profile assignments are omitted.

It is explicitly **best-effort and bounded**: any internal failure (empty
registry, a profile lookup that raises, a stale DB before this migration
runs) degrades to `{skills: []}` rather than propagating. This matters
because it sits in the dispatch hot path (below) — a broken registry must
never take down a run.

Task #1424 ("installable catalog UI over this registry") is expected to
build on `list_skills`, `get`, and `resolve` directly rather than querying
`skill_versions` / `skill_assignments` itself. Keeping those three functions
stable and side-effect-free is the point of calling this a "reader API":
adding a catalog UI should not require touching this module.

## Dispatch injection

`append_assigned_skills(prompt, agent_label) -> str` mirrors
`goals.append_why_chain` exactly: it calls `resolve`, and if there's nothing
assigned, returns the prompt unchanged. Otherwise it appends a block listing
each assigned skill (name, version, short hash) followed by its content, up
to `INJECTION_TOTAL_CAP` (8000 chars) total — truncating rather than growing
the prompt without bound. The cap includes the block introduction, headers,
separators, and leading blank lines. The block is explicitly labeled read-only
reference material, not additional authority or instructions (same posture
as the why-chain block).

**The one hook point**, both in
[`src/unified_agent/dispatch/launch.py`](../src/unified_agent/dispatch/launch.py):

- `CliProcessSpawner.spawn` (local/headless launch): right after
  `append_why_chain`, guarded by `if agent_label:`.
- `NodeLaunch` (routed-to-a-device launch): right after `append_why_chain`,
  unconditionally — `agent_label` is already required upstream in that path.

Both call sites already resolve `agent_label` for the why-chain decoration,
so this is a same-shape addition, not a new seam. No other dispatch path
(the scheduler, the coordinator, the supervisor) currently builds its own
prompt independently of `launch.py`; if one is added later, it should call
`append_assigned_skills` the same way.

## Drift report

`drift(surface_dir, agent_label=None, profile=None)` is **read-only** — it
never writes to `surface_dir`. `surface_dir` is resolved through
`safety.resolve_for_audit`, the same helper `skill_review`'s `path` argument
uses: a relative path resolves against the workspace root, an absolute path
must fall under the workspace root, an `UA_AUDIT_ROOTS` entry, or the
Obsidian vault root. This is the existing fs sandbox boundary — the same one
`skill_review`, `security_audit`, and `audit_triage_tasks` already use — not
a new one invented for this tool.

It scans `surface_dir` for `<name>/SKILL.md` immediate subdirectories,
hashes each on-disk `SKILL.md` the same way `publish` does, and compares
against the latest published registry version per name when `agent_label` is
omitted (`comparison: latest_published`). This is a surface-wide canonical
version check. With `agent_label`, it compares to the version resolved for
that agent's assignment set, including a pinned version
(`comparison: resolved_assignments`). A drift scan checks at most 100 immediate
directories that contain `SKILL.md` and 64 KiB per `SKILL.md`; unrelated
directories do not count toward the skill limit. It stops after 10,000 total
directory entries to bound work. Exceeding a bound returns a tool error before
reporting partial results. It reports:

- `missing` — registered (and, if scoped to an agent, assigned) but absent
  from the surface.
- `stale` — present on both sides with a different content hash.
- `unregistered` — present on disk with no registry entry for that name at
  all.
- `unassigned` — in agent-scoped mode, present on disk and in the registry,
  but not assigned to that agent/profile. It is excluded from synced and
  stale comparisons; `unregistered` means there is no registry entry.
- `synced` — present on both sides with a matching hash.

## MCP surface

One consolidated tool, `skill_registry(action, params)` — the same pattern
as `goal_manage` / `plan_manage`. Actions: `publish`, `list`, `get`,
`assign`, `unassign`, `resolve`, `drift`. `list`, `get`, `resolve`, and
`drift` are classified read-only (mirroring `goal_manage`'s
`{list, tree, get, ancestry}` read-only set in `server.py`'s
`_read_only_call`); `publish`, `assign`, and `unassign` are writes.

**Profile exposure**: developer and steward are deny lists (a newly
registered tool is reachable by default); research, operator, and public are
allow lists (a newly registered tool is unreachable by default, with no
change needed). `skill_registry` is in `tool_classification.OMITTED_TOOLS`
(hub-served, not yet on the operator client allowlist — same bucket as
`goal_manage`) and in `profiles.DEFAULT_PROFILE_PRUNED_TOOLS` (excluded from
the *default* developer profile specifically, to stay under the 100-tool
developer ceiling — the same trade `mcp_health_telemetry` and
`model_quota_status` made when `goal_manage`/`task_comment` landed). It
remains fully reachable under steward and chief-of-staff, and under an
explicit developer-profile-adjacent call is denied and logged, not silently
dropped, per the standard `policy_guarded` behavior. It is not reachable from
`public`, `research`, or `operator` (none of their allow-lists name it).

## Git-pinned sources (task #1750)

Shared skills can be imported from a Git repository with exact provenance,
instead of being hand-published from a local copy (`skill_sources.py`).

1. `source_add` (`url`, `ref?` default `HEAD`, `author?`) registers a repository
   and pins the ref to the 40-character commit SHA it resolves to now.
   `skill_sources` rows hold `url`, `ref`, `pinned_sha`, `refreshed_sha`,
   `last_refreshed`, `added_by`, `added_at`.
2. `source_discover` (`source_id`, `sha?`) lists every `SKILL.md` package at
   the pinned SHA (or the last refreshed one), with `importable` and the
   `problems` that refuse a package.
3. `source_import` (`source_id`, `path`, `sha`, `name?`, `author?`) creates an
   immutable version whose `source` is `git:<url>@<sha>:<path>`, with the
   usual `content_hash`. It runs the same `skill_review` lint and
   supporting-file audit as `publish` and returns `blocked` on a high finding.
4. `source_refresh` (`source_id`, `author?`) re-resolves the ref, records the
   new SHA as `refreshed_sha`, and returns a diff summary (files, and packages
   added, removed and changed). It never imports and never moves the pin; an
   explicit `source_import` with `sha=<resolved_sha>` adopts the new commit and
   advances `pinned_sha`.

`sha` may only be the pinned or the last refreshed SHA of that source, and
`source_import` requires it: an import names the exact commit its caller
discovered, so a pin another caller moved in between is refused, not silently
imported. A package whose member or directory paths collide after Unicode
(NFKC) normalization and case folding, or whose files plus directories exceed
the installer's 512-entry limit, is refused, matching the publish and
install capture rule. Discovery is linear in the tree size (bounded at 50,000
entries).

**Safety.** The repository is fetched shallowly into a private bare cache under
`$UA_HOME/skill_sources/` (created with an empty template, so no hooks
directory) and read only with `ls-tree`, `cat-file` and `diff-tree`. Nothing is
checked out, so no hook, filter, installer or script in the repository runs;
every git call also forces `core.hooksPath=/dev/null` and
`core.fsmonitor=false`, which overrides a user-level hook or fsmonitor setting.
The source repository itself is never written. A package is the directory
holding a `SKILL.md`; it is refused when it contains a nested `SKILL.md`
package, a symlink, a submodule, a hidden, secret-named or otherwise unsafe
member path, or exceeds the package file, byte or count limits. Import paths
that are absolute or contain `.`/`..` segments are refused.

**URLs and credentials.** `https://`, `ssh://git@host/path` and
`git@host:path` only. `file://` is refused unless
`UA_SKILL_SOURCES_ALLOW_FILE=1` (tests and local mirrors). URLs carrying a
username or password are refused: private repositories use the git and `gh`
credentials already on the host, and nothing is prompted for.

## Access and approvals

The default developer profile prunes `skill_registry` to stay under its
100-tool limit. Public, research, and operator profiles cannot call this
tool; steward and chief-of-staff can. A publisher explicitly shares the
selected `SKILL.md` with those registry readers by publishing it. Publish
sources must be under the workspace or an operator-configured
`UA_SKILL_REGISTRY_ROOTS` directory. An external registry source must also be
in `UA_AUDIT_ROOTS` so `skill_review` can lint it. Broad audit roots and the
vault remain audit-only unless separately allowlisted for publishing, so
sensitive files there cannot be published by accident. `list` returns
metadata only. `get` returns the stored `SKILL.md`
body because catalog consumers need the published instructions. Publish
responses return metadata without echoing the body. Writes go through
`approvals.check_approval_gate` when `UA_APPROVAL_TOOLS` includes
`skill_registry`; the approval wrapper is inactive when that setting is
absent.

Each mutation appends its audit event in the same transaction as the registry
write, then publishes the committed event to the local event bus for live
subscribers. Fan-out is best-effort; reconnecting subscribers replay from the
durable event log.

## What's out of scope here, deliberately

- **No catalog/installer UI.** Task #1424 owns "Skills Manager / Skill
  Studio / Skills Store" on top of this kernel.
- **No URL-sourced publish.** `publish`'s `path` is a local file or skill
  directory resolved through the same audit-root sandbox as `skill_review`.
  Remote skills come in through Git-pinned sources instead (below).
- **Full supporting-file attestation** now extends this kernel in task #1547.
  See [Reviewed skill packages](skill-packages.md) for bounds, migration, review,
  immutable installation and full-package drift. A support-only edit creates a
  new version; legacy rows keep their original hash and provenance.

The installable Control Center adapter, guarded installation API, team revision
projection, and current package limits are documented in [Skills & Teams catalog](skills-teams-catalog.md).
