# Project -> source-repository binding

Task #1425 (Parity 20). Source: [`project_bindings.py`](../src/unified_agent/project_bindings.py).

## The gap this closes

[`webui/project_registry.py`](../src/unified_agent/webui/project_registry.py)
resolves a project stream (`unified-agent`, `browser`, `sample-store`,
`client-portal`) to a DEVICE-LOCAL `~/dev` checkout path, existence-checked only
(`os.path.isdir`), and deliberately never lists directory contents (task
#356). It never recorded which git repository a project stream *is meant to
be* -- only whether some directory happened to exist on this host. This
module adds that binding, persisted in the hub DB.

**This is display-only resolution.** Binding a repo grants no new filesystem
reach. Both existing security properties are preserved exactly:

- `project_registry.registry()` still only ever calls `os.path.isdir` on the
  existing fixed per-project path; it still never lists a directory.
- The one new filesystem touch this module adds is reading a checked-out
  repo's own `<path>/.git/config` file directly -- see "Reading a checkout's
  origin, safely" below -- to compare it against the bound repo. It opens no
  other file and lists no directory inside the checkout.

## Binding shape

```
project (one of config._KNOWN_PROJECTS) -> {
  repo:            "owner/name" on github.com, normalized and validated
  default_branch:  optional
  bound_by:        agent/human label
  bound_at:        ISO timestamp
}
```

Stored in the `project_repo_bindings` table (one row per project, primary
key `project`), created by `project_bindings.ensure_schema`, hooked into
`storage.init_db` alongside `goals.ensure_schema` / `task_comments.ensure_schema`.
`SCHEMA_VERSION` in `storage.py` was **not** bumped -- this follows the
module-owned idempotent `ensure_schema(conn)` pattern those two already use,
so the table is created lazily and safely regardless of `SCHEMA_VERSION`.

### Repo validation (`project_bindings.normalize_repo`)

Accepts `owner/name`, `github.com/owner/name`,
`https://github.com/owner/name(.git)?`, `ssh://git@github.com/owner/name(.git)?`,
and `git@github.com:owner/name.git`. Normalizes to lowercase `owner/name`.
Rejects:

- empty or whitespace input
- any URL carrying credentials or userinfo (`user:pass@host`, or a
  non-`git` username before the host -- `git@` itself is the conventional
  SSH user, not a credential, so `ssh://git@github.com/owner/name` is
  accepted)
- any host other than `github.com`/`www.github.com` (`ALLOWED_HOSTS`) -- no
  existing config names another allowed host, so this is the one allow-listed
  set for now
- anything that doesn't resolve to exactly two **non-empty** path segments
  (`owner`, `name`), each matching a conservative GitHub-identifier pattern --
  `owner//name`, `owner/`, and `/name` are all rejected outright, never
  silently collapsed into a valid-looking `owner/name`

**Casing:** GitHub owner/repo names are case-insensitive (`Owner/Name` and
`owner/name` are the same repository), so `normalize_repo` always lowercases
its result. This is the one representation stored in `project_repo_bindings`
and compared against everywhere in this module -- a bound repo and a
checkout's normalized `origin` are compared case-insensitively as a
consequence, without either side needing to know that explicitly. Display
casing from the original input is not preserved.

### Config-seeded default (`config.PROJECT_REPO_SEED`)

```python
PROJECT_REPO_SEED = {"unified-agent": {"repo": "anonb3ll/unified-agent"}}
```

**A DB binding always wins over this seed.** `project_bindings.effective_binding(project)`
reads the DB row first; only when there is none does it fall back to the
config seed, and only `unified-agent` has one -- guessing a repo for the
other three streams would be worse than an honest "not bound" resolution.
`effective_binding()`'s result carries `"source": "db"` or
`"source": "config_default"` so a caller can tell which one it got.

## Resolution result (`project_bindings.resolve`)

```python
{
    "project": "unified-agent",
    "repo": "anonb3ll/unified-agent",  # or None if unbound
    "default_branch": None,
    "repo_source": "config_default",  # "db" | "config_default" | None
    "local_path": "/home/you/dev/unified_agent",  # or None
    "local_state": "checked_out",  # "checked_out" | "not_checked_out_here" | "unknown"
    "remote_matches": True,  # bool | None
}
```

- `local_state` is `"unknown"` only for a project stream this hub doesn't
  recognize at all (not one of the four known streams).
- `remote_matches` is `None` whenever it can't be determined: not checked out
  here, no bound repo, or the checkout's `origin` remote can't be read (no
  `origin` section, `.git` is a worktree/submodule pointer file, unreadable
  file, etc.). It is `True`/`False` only when both a bound repo and a
  readable `origin` exist to compare.

### Reading a checkout's origin, safely

`project_bindings._read_git_origin(path)` reads `<path>/.git/config`
**directly**, with its own minimal, non-recursive parser
(`_parse_git_config_origin_url`) -- it never shells out to `git remote
get-url origin` or any other git subcommand. That matters because:

- `git remote get-url` applies `url.<base>.insteadOf` rewrites, so a rule
  planted anywhere in an untrusted checkout's own config could make it
  report a different repo than the literal `url =` value under
  `[remote "origin"]`.
- Git config resolution follows `include.path` / `includeIf` directives and
  merges in global (`~/.gitconfig`) and system config, so a plain `git
  config`/`git remote` invocation reads more than "this one file."

The direct reader avoids all of that by construction: it parses exactly the
bytes of `<path>/.git/config`, looks only for `[remote "origin"]` or the
legacy dotted `[remote.origin]` section and its `url =` entries, and treats
`include.path`, `includeIf.*.path`, and `url.*.insteadOf` lines as inert
key/value pairs -- they are never interpreted, so they can never change
what's reported. Repeated identical origin URLs are accepted; conflicting
origin URLs are treated as unreadable. `tests/test_project_bindings.py` has
regression tests for both: an `insteadOf` rule planted in the checkout's own
config must not change the resolved origin, and an `include.path` pointing at
a file that *does* define `[remote "origin"]` must not be followed (resolves as
if no origin were configured at all).

A worktree or submodule checkout's `.git` is a **file** containing a
`gitdir: <path>` pointer to the real repo data elsewhere on disk. This reader
deliberately does not follow it -- an untrusted checkout could point that
pointer anywhere -- and treats it the same as "no origin" (`.git` must be a
directory). The `.git` directory and `config` file are opened with symlink
following disabled; `config` must be a regular file and is capped at 1 MiB.
Symlinked paths, non-regular config files, and larger files are treated as
unreadable.

`webui/project_registry.py`'s `registry()` calls `resolve()` for every entry
and adds `repo`, `repo_source`, `repo_default_branch`, `repo_local_state`,
`repo_remote_matches` alongside the existing `repo_path` / `checked_out`
fields -- this is what the Control Center project view reads (via
`webui/api.py`'s `projects_payload()`).

## Consumption

### Task-driven worktree creation

`tools/tasks.py::_ensure_task_worktree` cuts a task's worktree from a named
project's real `~/dev` checkout (task #371/#994, `[code-task]`-marked or a
non-default project) via `_worktree_source_repo` /
`_git_worktree_add_from_known_project_repo`. Right before that happens,
`project_bindings.check_worktree_binding(project, project_repo_path)` runs:

- **No bound repo**, or **the checkout's origin doesn't match the bound
  repo** (or can't be read): by default this **warns** -- logged and
  recorded as a `task.worktree.repo_binding_warning` event
  (`event_log_query`) -- and worktree creation proceeds exactly as before.
- Set `UA_PROJECT_REPO_STRICT=1` (`config.project_repo_strict_worktree_check()`)
  to make this a **hard refusal** instead: `check_worktree_binding` raises
  `ToolError`, which `_ensure_task_worktree`'s existing `except Exception`
  catches -- the same "no worktree created, logged, task still proceeds"
  path any other worktree-creation failure already takes.

Non-strict is the default because, as of task #1425, only `unified-agent`
has a repo binding (via the config seed) -- a hard refusal by default would
break worktree creation today for `browser`, `sample-store`, and
`client-portal`, none of which are bound yet. This is exactly the "warning
field + opt-in strict flag" the task called for.

The general-purpose, always-workspace-scoped `git_worktree_add` MCP tool
(`tools/git.py`) is intentionally untouched: it has no `project` concept to
check a binding against, and it is the tool every non-project-sourced
worktree (including the cockpit's own default workspace) already depends on
-- widening its checks would risk exactly the "default workspace" breakage
the task warned against.

### Evidence reports

`evidence_report(project=...)` (`tools/evidence.py` /
`audit/evidence_report.py`) now includes a `project_repo_binding` field --
the same `resolve()` result -- whenever the `project` stream filter is set,
so a reader of that report's `pr_url`/`commit_sha` records can tell which
repo they're expected to belong to. The key is **omitted entirely** (not
present, not `null`) when `project` is unset, matching the `NotRequired`
TypedDict field and keeping the unfiltered report's shape exactly as it was
before this task. Purely additive; the report's own `schema_version` is
unchanged.

## Write surface: `coordination_manage`

Bind/unbind/get/list are exposed as `coordination_manage` actions rather
than a new top-level MCP tool, because `server.py`'s single tool-registration
tuple is a standing merge hotspot across concurrent branches, and
`coordination_manage` (`tools/graph.py`) is the existing precedent for adding
a cross-module action family without touching it (it already carries
dependency-graph, handoff, and agent-lifecycle actions).

- `coordination_manage("project_repo_bind", {"project": "unified-agent", "repo": "owner/name", "default_branch": "main"})`
- `coordination_manage("project_repo_unbind", {"project": "unified-agent"})`
- `coordination_manage("project_repo_get", {"project": "unified-agent"})`
- `coordination_manage("project_repo_list", {})`

A **verified caller identity always wins** over a caller-claimed
`bound_by`/`unbound_by` param -- an authenticated agent cannot attribute its
own write to a different label. Only when there is no verified identity does
the explicit param get used, and a call with neither is refused.

`bind`/`unbind` write the `project_repo_bindings` row and append its
`project.repo.bound`/`project.repo.unbound` audit event on the **same SQLite
transaction** (`events.record_event_once_on_connection`, the pattern
`config_revisions.py`'s `_record`/`_append_live` use for its own durable
writes) -- either both land or neither does. A separate commit-then-record
sequence would let a later `record_event` failure report an error while the
binding row had already committed unaudited.

### Profile visibility

`coordination_manage` itself is already denied to the `steward` profile
(`profiles.STEWARD_DENIED_TOOLS`), so none of these actions reach steward
sessions -- steward can still see repo bindings read-only via the Control
Center's `/api/projects` view, a separate, unauthenticated-for-this-purpose
code path.

For the allow-list `research` profile, only the two read actions were added
to `RESEARCH_ALLOWED_ACTIONS`: `project_repo_get`, `project_repo_list`.
`project_repo_bind`/`project_repo_unbind` stay out -- research already gets
some coordination writes (`dependency_add`/`dependency_remove`), but binding
a project's source repo is treated as closer to a fleet-config change than a
dependency edge, so it stays developer/steward territory. The `public`
profile has no consolidated managers on its allow list at all
(`PUBLIC_ALLOWED_ACTIONS` is empty), so none of this reaches it either.

## Tests

`tests/test_project_bindings.py` covers: repo-format normalization and its
rejections (credentials/userinfo, non-GitHub hosts, malformed/repeated-slash
paths), case-insensitive owner/repo matching, DB bind/unbind/get/list and
DB-wins-over-seed precedence, the atomic write+event transaction, `resolve()`'s
three `local_state` values and `remote_matches` tri-state (including a real
git-repo fixture), the direct-config-read security regressions (`insteadOf`
must not change the result, `include.path` must not be followed, a worktree
`.git` file must not be followed), and the worktree refusal case
(`check_worktree_binding` raising under `UA_PROJECT_REPO_STRICT=1` for an
unbound project, vs. warning by default). `tests/test_project_registry.py`
and evidence-report tests cover the two consumption sites' additive fields.
