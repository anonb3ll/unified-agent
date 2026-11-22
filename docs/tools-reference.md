# Tools reference

200 MCP tools, registered in `src/unified_agent/server.py` and implemented in
`src/unified_agent/tools/*.py`. This page is derived from the tool source (signatures,
docstrings, and the safety/config modules they call), not from `AGENTS.md` — its tool
catalog table predates `fs_index_symbols`, `fs_list_symbols`, `fs_search_symbols`,
`git_merge_task`, and `vault_wiki_custodian`, none of which it lists. The
executable test suite checks that every registered tool has a reference heading.

All examples show the MCP tool-call parameters as JSON. Every tool raises
`mcp.server.mcpserver.exceptions.ToolError` (surfaced to the calling host as a tool error,
not an exception) on invalid input or a safety-check rejection; error text is quoted
verbatim below where it clarifies behavior.

### Error contract: typed codes (#2054)

Typed refusals also carry a machine code. The result keeps `isError: true` and the
unchanged text block (`Error executing tool <name>: <message>`), and adds
`structuredContent`:

```json
{"isError": true,
 "content": [{"type": "text", "text": "Error executing tool task_claim: task 5 is already claimed/locked by 'agent-server-agent1' until ..."}],
 "structuredContent": {"code": "conflict"}}

{"isError": true,
 "content": [{"type": "text", "text": "Error executing tool task_add: 'task_add' requires approval (global allowlist): request 742 is queued. ..."}],
 "structuredContent": {"code": "approval_required", "approval_id": 742}}
```

| `code` | Raised by | Extra keys | What to do |
|---|---|---|---|
| `conflict` | `task_claim` and `task_update` stale revision (`expected_updated_at`), run fence (`expected_run_id`), expired lease, claim held by another agent (a `task_claim` with no `expected_run_id` returns `safe_wait` instead), lost claim race; every `ClaimReleaseConflict`; `project_build` on a spec claimed by another agent | none | Read the object again before retrying |
| `approval_required` | Every `ApprovalPendingError`: the `UA_APPROVAL_TOOLS` gate, quarantine clearing, elicitation declines | `approval_id` (omitted when unknown) | Retry the identical call after a human approves |
| `permission_denied` | The steward policy forbidding updates to another agent's claimed task | none | Ask the claimant or use an authorized identity; reading a newer revision does not authorize this action |

`structuredContent` holds only the code and those keys, never arguments or
payloads. Every other error, including argument validation and crashes, has no
`structuredContent`, exactly as before. A client should read the code first and
treat its absence as "untyped". The base class is
`unified_agent.tool_errors.TypedToolError`; the MCP `tools/call` handler that
adds the field is `tool_errors.install`.

The Control Center `/api` write routes return the same code over HTTP (#2056).
A typed refusal answers `{"error": "<message>", "code": "<code>"}`; a `conflict`
is HTTP 409 on every route, and the other codes keep the route's own status. A
parked approval is always HTTP 202 with the code, on every route:
`{"approval_id": 742, "status": "pending", "code": "approval_required"}`. Routes
with a richer pending body (`tool`, `payload`, an `autonomy_review` pause from a
team boot) keep it and add the code. A 202 for accepted work (a dispatch whose
agent has not started reports `status: "pending"`) is not an approval and has no
code. An untyped error keeps its status and has no `code` key.

Env vars referenced below are documented in full in [configuration.md](configuration.md).
The sandbox chokepoints (`resolve_in_workspace`, `resolve_in_vault`, `resolve_for_audit`,
`vet_command`, `check_fetch_host`) are covered in depth in [security.md](security.md).

---

## MCP resources (live board state)

### `credential_pool_register(provider: str, label: str, note: str = "") -> dict`

Stores provider, label, and non-secret operator metadata. See the
[Credential pool registry](credential-pool.md) contract.

### `credential_pool_list(include_revoked: bool = False) -> dict`

Lists credential pool metadata rows; revoked rows require `include_revoked=true`.

### `credential_pool_revoke(id: str) -> dict`

Marks one credential pool row revoked without deleting its metadata.

### `model_quota_status() -> dict`

Returns a read-only fleet quota / lane-availability projection. Always includes
the subscription lanes `codex`, `claude`, `agy`, and `cursor`, plus any
credential-pool registry rows.

Each entry carries `availability` (`available` | `unavailable` | `unknown`),
`source`, `observed_at`, and `last_checked`. Availability is derived only from
local observations:

- Codex rollout `rate_limits` JSONL (`~/.codex/sessions`, or
  `UA_CODEX_SESSIONS_ROOT`) — preferred meter; reports `used_percent`,
  `window_minutes`, `plan_type`, `source_path`. `resets_in_seconds` is always
  `null` (UNKNOWN).
- Classified dispatch/executor failure outcomes
  (`provider_quota_exhausted` / `iteration_budget_timeout` / `code_error`).

A lane with no observations is `unknown` with `source: "no source"` — never
treated as available, and never measured by contacting a provider.

Read-only MCP resources, registered in `src/unified_agent/server.py` and
derived from `object_model.served_types()` (see
[platform-object-model.md](platform-object-model.md)). They reuse the existing
bearer/OAuth session and operational profile: each URI is gated by that type's
declared `profile_tool` (for example board/task → `task_list`, run →
`dispatch_report_self`, approval → `approval_pending`). Missing or deleted rows
return not-found. Clients subscribe with MCP 2.x
`Client.listen(resource_subscriptions=[...])`.

The table below is pinned to `served_types()` by
`tests/test_object_model.py::TestToolsReferenceResourcesPin` — adding a served
kind without updating this list fails CI.

| URI | Gate | Contents |
|---|---|---|
| `unified-agent://board` | `task_list` | Read-only compact task board (same visibility as task_list). |
| `unified-agent://tasks/{task_id}` | `task_list` | Read-only task row. Missing or deleted ids return not-found. |
| `unified-agent://runs/{run_id}` | `dispatch_report_self` | Read-only run status: dispatch runs and self-pickup runs (sp-<ULID>, task #1436); same profile as dispatch_report_self. |
| `unified-agent://approvals/{approval_id}` | `approval_pending` | A one-shot human approval gate and its resolution. |
| `unified-agent://evidence/{event_ulid}` | `event_log_query` | One hash-chained entry of the tamper-evident audit ledger. |
| `unified-agent://plans/{plan_id}` | `plan_get` | Read-only plan row (same profile as plan_get). |
| `unified-agent://delegations/{delegation_id}` | `delegate_list` | Read-only agent-to-agent handoff record (same profile as delegate_list). |
| `unified-agent://agents/{agent_label}` | `agent_list` | A registered agent label and its derived liveness (same profile as agent_list). |
| `unified-agent://sessions/{session_id}` | `session_list` | One agent session and its outcome (same profile as session_list). |
| `unified-agent://memory/{memory_id}` | `memory_get` | One shared-memory note (same profile as memory_get). |
| `unified-agent://agent-tokens/{token_id}` | `agent_token_list` | Hash-only lifecycle record for one issued agent bearer token — never the token value itself. |
| `unified-agent://work-products/{work_product_id}` | `work_product_submit` | A submitted document/file awaiting or having completed named-reviewer approval and release, with its evidence chain. |
| `unified-agent://org-units/{org_unit_id}` | `task_list` | One checked-in office charter, including lead, members, owned epics, lanes, profile defaults, and decision rights. |
| `unified-agent://goals/{goal_id}` | `task_list` | One goal plus its why-ancestry. `parent_goal_id` is not a task dependency and not `tasks.parent_id`. |
| `unified-agent://comments/{comment_id}` | `task_list` | One append-only task comment. Task notes stay the specification. |
| `unified-agent://org-proposals/{proposal_id}` | `task_list` | One proposed organizational change (roles, lanes, routing, delegation, routines) pending review or resolution. |

A same-process `task_update` publishes `notifications/resources/updated` on
the board and task URIs. A hub lifespan poller covers SQLite changes from
another process (executor, scheduler). Resources never mutate state.

### `org_proposal(action, params)`

Self-organization proposals (task #1431). Proposing is always on by default;
applying is strictly gated behind human/policy approval. Toggleable off via
`control_flags` (key `self_organization`). Agents can only propose changes for
offices they belong to (scope-as-membership, task #704). Actions: `propose`,
`list`, `get`, `review`, `apply`, `reject`, `toggle`.

- `propose` (`title`, `proposal_type`, `scope`, `rationale`, `proposed_changes`, `author?`):
  creates an org proposal and parks an approval gate row.
- `list` (`status?`, `scope?`, `author?`, `limit?`): lists proposals.
- `get` (`proposal_id`): reads one proposal.
- `review` (`proposal_id`, `reviewed_by?`, `notes?`): marks a proposal reviewed.
- `apply` (`proposal_id`, `applied_by?`, `reason?`): consumes the human-approved gate and applies.
- `reject` (`proposal_id`, `rejected_by?`, `reason?`): rejects a proposal and cancels pending approval.
- `toggle` (`enabled?`, `reason?`, `set_by?`): reads or updates `self_organization` in `control_flags`.

### `goal_manage(action, params)` and `task_comment(action, params)`

`goal_manage` actions: `add`, `list`, `tree`, `get`, `link`, `unlink`, `edit`,
`close`, `reopen`, `ancestry`, `decompose`. `edit` takes `goal_id`, `title`,
`statement`; `close` takes `goal_id`, `status` (`achieved` default or
`abandoned`) and `reason`; `reopen` takes `goal_id`; `unlink` takes `task_id`
and the `goal_id` it serves (task #2480). Each takes an optional
`expected_revision` (the goal's `revision`, advanced by every update): a stale value, closing a closed goal or reopening an
open one raises a conflict. Closing a goal leaves its task links. A goal's `parent_goal_id` is why-ancestry (root first). Linking
a task sets `goal_task` (one goal per task). That is not `tasks.parent_id`
and not `dependency_add`. `decompose` files one child task per uncovered
epic DoD clause and links those tasks to the goal.

Goals are tenant-scoped (task #1999). A goal is stamped with the writer's
`org_id` when it is created. `add`, `list`, `tree`, `get`, `link`,
`ancestry` and `decompose` match goals and tasks on `org_id IS` the caller's
tenant, so another tenant's goal or task behaves exactly like a missing one.
An untenanted hub sees only untenanted goals, as before.

`goal_manage` also carries `operating_*` actions for operating profiles,
sprints and per-run settings (tasks #1732, #1735): `operating_profiles`,
`operating_resolve`, `operating_sprint_add`, `operating_sprint_list`,
`operating_sprint_get`, `operating_sprint_update`, `operating_sprint_link`,
`operating_sprint_unlink`, `operating_run_set`, `operating_run_clear`. See
[operating-profiles.md](operating-profiles.md).

Sprints are tenant-scoped too (task #2014): a sprint is stamped with the
writer's `org_id`, and `operating_sprint_list`, `operating_sprint_get`,
`operating_sprint_update` and `operating_sprint_link`/`unlink` see only the
caller's sprints and tasks. Plans are stamped the same way, and the
`work_graph` goal subgraph shows only the caller's plans as `plan_goal` nodes.
Every plan read is fenced on it (task #2042): `plan_manage` actions and the
legacy `plan_*` tools, the plan platform reader, `task_comment` plan refs and
joint planning treat another tenant's plan exactly like an unknown plan.
`operating_resolve`, `operating_run_set` and `operating_run_clear` treat
another tenant's task or sprint as unknown (task #2043). A task resolves only
a sprint of its own tenant, also on untenanted claim and launch paths, so a
link to another tenant's sprint refuses like a missing sprint.
Operating writes check their task, sprint and optional goal targets before
the public `goal_manage` policy and approval gates as well as the inner
operating-settings gate. The write transaction rechecks the targets. A tenant
authority refusal cannot silently discard the operating posture during a
claim or launch (#2065).

`task_comment` actions: `append`, `list`, `get`. There is no update or
delete. Task notes remain the specification (`append_note`). A comment may
name `mentions` and `refs` (`plan_id`, `decision_id`, `handoff_id`). A
repeated `idempotency_key` for the same author and task returns the original
comment. HTTP: `GET/POST /api/goals`, `GET /api/goals/{goal_id}`,
`GET /api/tasks/{task_id}/why-chain`, `GET/POST /api/tasks/{task_id}/comments`.

A `mention` notifies (task #2392). Each mentioned label other than the author
gets one `inform` message from the author carrying the task id, the comment id
and the thread revision — a pointer, never the comment body, because thread
text is data. A repeated `idempotency_key` does not notify twice, a failed
notice never fails the comment (it is already committed), and the append
refuses more mentions than `UA_MESSAGE_MAX_RECIPIENTS`. The append's result
lists `notified_mentions` when at least one notice was written.

### `skill_registry(action, params)`

Org-wide skill registry (task #1353, Parity 6): see
[skills-registry.md](skills-registry.md) for the full lifecycle. Actions:
`publish`, `list`, `get`, `assign`, `unassign`, `resolve`, `drift`, and the
`source_*` actions below.

- `publish` (`name`, `path`, `author?`): runs `skill_review` on `path` first
  and refuses (`{ok: false, blocked: true, review}`) on any `high`-severity
  finding. Re-publishing identical content returns the existing version
  (`idempotent: true`) instead of minting a new one.
- `list` (`name?`) / `get` (`name`, `version?`, defaults to latest): read the
  version history.
- `assign` (`skill_name`, `target`, `target_type: agent|profile`, `version?`,
  `author?`) / `unassign` (`skill_name`, `target`, `target_type?`, `author?`): point a
  skill at an agent label or a profile name, optionally pinned to a version.
- `resolve` (`agent_label`, `profile?`): pure reader — the skills assigned to
  that label (directly, or via its profile), bounded and best-effort. This is
  the stable API task #1424's catalog UI is expected to read from.
- `drift` (`surface_dir`, `agent_label?`, `profile?`): read-only comparison of
  an on-disk skills directory (`<name>/SKILL.md` per subdirectory) against the
  registry. `surface_dir` resolves the same way `skill_review`'s `path` does
  (workspace root or an `UA_AUDIT_ROOTS` entry) — never an arbitrary path, and
  never written to.
- `source_add` (`url`, `ref?`, `author?`), `source_list`, `source_discover`
  (`source_id`, `sha?`), `source_refresh` (`source_id`, `author?`) and
  `source_import` (`source_id`, `path`, `sha`, `name?`, `author?`): Git-pinned
  skill sources (task #1750). A source pins its ref to a 40-character SHA;
  discover lists `SKILL.md` packages at that SHA without checking anything out
  or running anything; import stores a version with
  `source=git:<url>@<sha>:<path>` after the `skill_review` gate; refresh reports
  the new SHA and a diff summary but never imports. URLs: `https://`,
  `ssh://git@`, `git@host:path` (`file://` only with
  `UA_SKILL_SOURCES_ALLOW_FILE=1`). See
  [skills-registry.md](skills-registry.md#git-pinned-sources-task-1750).

Registered under `OMITTED_TOOLS` (task_classification) and
`DEFAULT_PROFILE_PRUNED_TOOLS` (profiles), same treatment as `goal_manage`'s
siblings above it: reachable under steward and chief-of-staff, kept out of the
default developer profile to stay under its 100-tool ceiling, and never
reachable from the `public`/`research`/`operator` allow-list profiles.

---

## Web (`tools/web.py`)

### `web_search(query: str, max_results: int = 5) -> dict`

Searches the web via a pluggable provider registry (`UA_SEARCH_PROVIDER`, default
`ddg` — keyless DuckDuckGo through the `ddgs` package; no other provider is currently
registered). `max_results` is clamped to `[1, 10]`.

- **Errors:** empty/whitespace `query`; unknown provider; provider call failure (wraps
  the underlying exception).
- **Returns:** `{results: [{title, url, snippet}], provider}`.

```json
{"query": "fastmcp streamable http transport", "max_results": 5}
```

### `web_fetch(url: str, max_chars: int = 40000, raw: bool = False, pdf_base64: bool = False) -> dict`

Fetches a URL and returns either Trafilatura-extracted Markdown-ish readable text
(default) or the raw decoded body (`raw=True`). Only `http`/`https` schemes are
accepted. The host is vetted by `safety.check_fetch_host()` before any request, and every
request (including redirects, up to 5) is routed through `SSRFShieldTransport`, which
re-vets the resolved IP on each hop and pins the connection to it (defeats DNS
rebinding) while preserving the original `Host`/SNI. Local/private/loopback/link-local
addresses are rejected unless `UA_ALLOW_LOCAL_FETCH=1`. Every result is marked
untrusted (`untrusted`, `untrusted_note`).

- **Limits:** 20 s timeout, 2 MB download cap (streamed; stops once exceeded), output
  capped to `max_chars` clamped to `[100, 200000]` (default 40000).
- **PDFs:** with `pdf_base64=False` (default), text is extracted in a short-lived,
  confined subprocess (`pdf_worker.py`: macOS `sandbox-exec`, Linux `bwrap`; no network,
  resource limits, 10 s, 50 pages, 200,000 characters; fails closed when the sandbox
  is unavailable) and returned as `{url, final_url, title, content, truncated}`.
  `pdf_base64=True`, or a worker that is unavailable, times out, finds no text,
  rejects an oversize PDF, or cannot read it, falls through to the base64 media
  result, whose `note` says why (with `pdf_base64=True` the note is generic).
- **Errors:** non-http(s) scheme; unresolvable/disallowed host; any `httpx.HTTPError`
  (non-2xx status, connection failure, timeout).
- **Returns:** `{url, final_url, title, content, truncated}` for text. An image, or a
  PDF that was not text-extracted, returns `{url, final_url, kind, mime, base64, note}`.
  If HTML extraction yields no content (non-HTML body, or extraction failure),
  `content` falls back to the raw decoded text even when `raw=False`.

```json
{"url": "https://example.com/docs/page", "max_chars": 20000}
```

---

## Dash/Zeal documentation (`tools/dash.py`)

Offline docset search; no network access. Docsets are discovered by scanning
`UA_DASH_DOCSETS` plus built-in default paths (`~/Library/Application Support/Dash/DocSets`,
`~/Library/Application Support/Zeal/Zeal/docsets`, `~/.local/share/Zeal/Zeal/docsets`) for
directories ending in `.docset`. On a machine with neither Dash nor Zeal installed and no
`UA_DASH_DOCSETS` override, all three tools return empty results rather than erroring.

### `dash_list() -> dict`

Lists all discovered docsets. **Returns:** `{docsets: [{name, id, path, platform}]}`.

### `dash_search(query: str, docset: str | None = None) -> dict`

Fuzzy `LIKE`-matches symbol names against each docset's `docSet.dsidx` SQLite index
(read-only connection, `mode=ro`). `docset` filters to one docset by id, exact name, or
id prefix. Results are ranked (exact match, then prefix match, then substring) and capped
at 50.

- **Errors:** empty `query`; `docset` filter matches no discovered docset.
- **Returns:** `{results: [{docset_name, docset_id, name, type, path}]}` (≤50 rows,
  200-row-per-docset internal query cap before ranking).

```json
{"query": "asyncio.gather", "docset": "python"}
```

### `dash_view(docset_id: str, path: str) -> dict`

Renders one documentation page to Markdown (Trafilatura, with a raw-HTML fallback if
extraction fails or yields nothing). `path` is the relative path returned by
`dash_search`; a resolved path escaping the docset's `Contents/Resources/Documents` root
is rejected.

- **Limits:** output capped at 60000 chars (`truncated` flag reports overflow).
- **Errors:** empty `docset_id`/`path`; docset not found; path escapes the docset
  documents root; page not found; file read failure.
- **Returns:** `{docset_name, path, content, truncated}`.

---

## Files (`tools/files.py`)

Read and write use different resolvers. `fs_read` / `fs_list` / `fs_search` go
through `safety.resolve_for_project_read()` (workspace **or** a known project
checkout). Mutating tools (`fs_write`, and the other write-side file tools)
go through `safety.resolve_in_workspace()`: relative paths resolve against the
active task worktree when one exists, otherwise `UA_WORKSPACE`. Containment is
that cockpit root **union** the active worktree — a project-sourced
`[code-task]` worktree under `<project>/.worktrees/` is in-bounds for writes
while it is the active worktree, but a path outside both roots is rejected
(task #995). A relative `..` sequence is never reclassified as a project read.
Hook-execution directories (`.git`, `.hg`, `.svn`, `.husky`) are refused for
every tool in this section — see [security.md](security.md#workspace-sandbox).

### `fs_read(path: str, offset: int = 0, limit: int = 2000) -> dict`

Reads a text file, paginated by line. `limit` is clamped to `[1, 5000]` (default 2000,
but callers can request up to 5000 in one call). Files are decoded with
`errors="replace"`.

- **Limits:** 1 MB (`READ_MAX_BYTES`) file-size cap — files larger than that raise a
  `ToolError` unconditionally (no partial read; the tool suggests `fs_search` instead).
- **Errors:** file does not exist; oversized file; path is a directory (suggests
  `fs_list`).
- **Returns:** `{path, content, total_lines, truncated}`.

```json
{"path": "src/unified_agent/config.py", "offset": 0, "limit": 200}
```

### `fs_write(path: str, content: str, append: bool = False) -> dict`

Writes or appends a text file, creating parent directories as needed.

- **Limits:** 2 MB (`WRITE_MAX_BYTES`) content cap per call.
- **Errors:** content exceeds the cap; path escapes the workspace; path targets a
  hook-execution directory (`.git`, `.hg`, `.svn`, `.husky` — see
  [security.md](security.md#workspace-sandbox)).
- **Returns:** `{path, bytes_written}`.

### `fs_list(path: str = ".", glob: str | None = None, max_entries: int = 500) -> dict`

Lists a directory, optionally filtered by a glob (e.g. `"*.py"`). `max_entries` is
clamped to `[1, 2000]` (default 500). `glob` must be relative to `path` and must
not contain `..`; an absolute glob is refused (task #1423). Each candidate is
passed through the same hook-dir, secret-filename and symlink-escape guards as
`fs_read`. Names that would fail a direct read (`.git`, `.husky`, `service.env`,
a symlink out of the read roots) are omitted, not listed. Dropped names do not
count toward `max_entries` or `truncated`.

- **Errors:** path is not a directory; `glob` is absolute or contains `..`.
- **Returns:** `{entries: [{name, type, size}], truncated}` (`size` is `null` for
  directories).

### `fs_search(pattern: str, path: str = ".", glob: str | None = None, max_results: int = 100) -> dict`

Regex (Python `re`) search across text files under a directory, walked recursively.
Binary files (null byte in the first 8 KB) and files over 1 MB (`SEARCH_FILE_MAX_BYTES`)
are silently skipped. `max_results` is clamped to `[1, 1000]` (default 100); each
returned line is truncated to 500 chars.

- **Errors:** invalid regex; path is not a directory.
- **Returns:** `{matches: [{path, line_no, line}], truncated}` (`path` is workspace-relative).

```json
{"pattern": "def\\s+fs_\\w+", "glob": "*.py"}
```

---

## Symbol indexer (`tools/indexer.py`)

An AST-based Python symbol index backed by the shared SQLite DB (`workspace_symbols` /
`workspace_symbol_embeddings` tables), keyed by workspace root path so multiple
workspaces don't collide.

### `fs_index_symbols(path: str = ".") -> dict`

Walks the given directory (skipping `.git`, `node_modules`, `.venv`, `venv`,
`__pycache__`, `.tox`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `dist`, `build`,
`.next`, `target`), parses every `.py` file with `ast.parse`, and replaces the DB's
symbol rows for this workspace (`DELETE` then bulk `INSERT`). Files that fail to parse
(`SyntaxError`) are silently skipped (no symbols extracted, no error raised). After
saving, spawns a **background daemon thread** to backfill symbol embeddings
(best-effort; failures are swallowed).

- **Errors:** target path does not exist.
- **Returns:** `{workspace, files_indexed, symbols_indexed, classes, functions, methods}`.

```json
{"path": "src/unified_agent"}
```

### `fs_search_symbols(query: str, semantic: bool = False) -> dict`

Searches indexed symbols by substring (`LIKE %query%` on `symbol_name`, default) or, if
`semantic=True`, by cosine similarity over `fastembed` embeddings (falls back silently to
the substring search if embedding generation fails, e.g. offline first run before the
model is cached). Both modes cap at 100 rows and are scoped to the current
`UA_WORKSPACE`.

- **Returns:** `{results: [{file_path, name, type, parent, signature, docstring,
  line_no, similarity?}]}` (`similarity` present only in semantic mode).

### `fs_list_symbols(file_path: str) -> dict`

Returns the symbol structure of one file. If the file has not been indexed yet (no DB
rows for it), parses it on the fly rather than requiring a prior `fs_index_symbols` call.

- **Errors:** file does not exist; file resolves outside the workspace.
- **Returns:** `{symbols: [{name, type, parent, signature, docstring, line_no}]}`.

---

## Shell (`tools/shell.py`)

`run_cmd` never invokes a shell (`subprocess.run(argv, shell=False)`); the command
string is `shlex.split()` into an argv list, so shell metacharacters (`| ; & > < $() `` `)
reach the target program as literal argument characters rather than being interpreted.
The first token must be in the shell allowlist (`config.shell_allowlist()`).
Setting `UA_SHELL_ALLOWLIST` replaces that default; include each default program
name you still want.

### `run_cmd_check(command: str, cwd: str = ".") -> dict`

Dry-run vetting: parses and classifies a command without executing it. Useful to catch
mistakes before spending a `run_cmd` call.

- **Findings** (each `{severity, code, message}`):
  - `block` — `run_cmd` would reject the command (`rejected`: not on the allowlist /
    unparseable; `cwd`: escapes the workspace; `destructive`: GateGuard-style data-loss
    argv — `rm -rf`, force/path `git checkout`/`reset --hard`, destructive `find -exec`
    / `-delete`; `secret-path`: `cat`/`grep`/other content readers aimed at a
    credential, key or database filename that `fs_read` also refuses. Approvals
    cannot override these.)
  - `warn` — probably not what was intended: `shell-syntax` (literal `|`/`;`/`>` etc.
    reaching the program as args), `no-expansion` (`$VAR`/`` $(...) ``/backticks never
    expand), `path-escape` (an argument resolves outside the workspace — `run_cmd` only
    confines `cwd`, not arguments), `allowlist-bypass` (non-destructive `find -exec`,
    `find -fprint*`, awk program text/`-f`, sed `e`/`w`/`W`, `sort --compress-program`,
    writes inside `.git`, `.hg` or `.husky` (not only `hooks/`; `uniq IN OUT` treats
    `OUT` as that write), `git -c` / `clone --config` keys outside the safe allowlist,
    global `--git-dir`/`--work-tree`, a git subcommand that is not a built-in (a
    possible `alias.<name>=!cmd`), `git config` writes, git command-running flags such
    as `grep -O`, `rebase -x`, `bisect run`, `submodule foreach`, `--upload-pack`,
    `--config-env`, `--exec-path`; full list in docs/security.md), `arbitrary-code`
    (`python3`/`python` scripts, `-m`, `-c`, or the REPL; harmless
    `--version`/`-V`/`-h`/`--help` only; also `uv` and `pytest`), `not-found`
    (program isn't on `PATH`; `run_cmd` would return exit 127). `path-escape`,
    `allowlist-bypass`, and `arbitrary-code` force the approval queue even when
    `UA_APPROVAL_TOOLS` is unset.
  - `info` — heads-up only: `network` (`git fetch/pull/push/clone/ls-remote`; `git push`
    additionally forces approval).
- **Returns:** `{ok, verdict, argv, program, program_found, mutates, findings}`.
  `verdict` is `block`/`warn`/`ok`. `mutates` is a heuristic `no`/`maybe`/`yes` estimate
  of filesystem mutation risk (read-only programs → `no`; `sort -o` and `uniq IN OUT`
  → `yes`; `find`/`sed`/`ruff`/`git` classified by flags/subcommand; everything else,
  including `python3`/`uv`/`pytest`/`awk` and any other program named in
  `UA_SHELL_ALLOWLIST` → `maybe`, since they can do anything).

```json
{"command": "grep -rn TODO src | wc -l"}
```
Response would flag `shell-syntax` on `|` (it becomes a literal argument to `grep`, not a
pipe) and note `wc` would never run.

### `run_cmd(command: str, cwd: str = ".", timeout_s: int = 30) -> dict`

Executes the vetted command with `cwd` confined to the workspace and a minimal
environment (`PATH`, `HOME`, `LANG` only — not a full environment passthrough).
A `git` argv also gets `GIT_CONFIG_COUNT=1` / `safe.bareRepository=explicit` so a
bare repository the agent laid out with `fs_write` is never discovered (task
#1423). `timeout_s` is clamped to `[1, 120]` (`CMD_TIMEOUT_CAP_S`). If
`UA_SANDBOX_ENABLED=1`, runs inside a Docker/Podman container instead (see
[security.md](security.md) for the container invocation details); otherwise runs
directly on the host.

- **Limits:** 120 s timeout cap; 50 KB (`CMD_OUTPUT_CAP`) combined stdout+stderr, split
  in half and truncated if exceeded.
- **Errors:** raises `ToolError` for allowlist rejection, destructive-command deny,
  workspace-escaping `cwd`, or a content reader aimed at a secret-bearing filename
  (`secret-path`). A missing program or a timeout is reported in the return value,
  not raised.
- **Returns:** `{exit_code, stdout, stderr, truncated, timed_out, warnings}`.
  `exit_code` is `127` for a missing program (or missing/failed container engine when
  sandboxed) and `-1` for a timeout. `warnings` carries the same `warn`-severity
  advisories `run_cmd_check` would report, computed automatically on every call.
- **Approval:** the allowlist checks `argv[0]` plus a destructive-argv deny; only
  `cwd` is confined, so arguments are not. A clean command is gated only when
  `UA_APPROVAL_TOOLS` lists `run_cmd`. The `path-escape`, `allowlist-bypass` and
  `arbitrary-code` advisories, and `git push`, always force the approval gate
  (`_APPROVAL_REQUIRED_CODES`, `_GIT_REMOTE_MUTATING` in `tools/shell.py`).

```json
{"command": "pytest tests/test_safety.py -k dotdot", "timeout_s": 60}
```

---

## Security & skill quality (`tools/audit.py`, `tools/skills.py`)

Both are read-only. Full rule catalog and remediation behavior in
[security.md](security.md).

### `security_audit(path: str = ".", min_severity: str = "info", remediate: bool = False, excludes: list[str] | None = None, vault: bool = False) -> dict`

Runs the rule-based audit engine (`audit/engine.py`) over `path`, which may be a file or
a directory. Without `vault`, `path` is workspace-relative, or absolute under a
`UA_AUDIT_ROOTS` entry. With `vault=True`, `path` resolves inside the Obsidian vault
instead (the same read boundary as `vault_read` — anywhere except dot-directories),
and repo-hygiene rules (`.gitignore`/lockfile/agent-settings/`.mcp.json` checks) are
skipped since they assume a code-project tree, not vault content — this is the pre-ingest
scan path for `00 - Inbox` captures (see [security.md](security.md#untrusted-content-boundaries)).
`min_severity` (`high|medium|low|info`) filters returned findings; the `status` field is
computed before filtering. `remediate=True` applies the safe auto-fixes in
`audit/remediate.py` (creating/updating `.gitignore`, scaffolding `CLAUDE.md`, stripping
hidden-Unicode characters) — see [security.md](security.md) for exactly which rules are
auto-fixable; not available with `vault=True` (vault audits are always read-only).

- **Limits:** scans ≤5000 regular files (`MAX_FILES`), caps at 500 findings
  (`MAX_FINDINGS`); skips binaries, files >1 MB, and FIFOs/sockets/devices
  (task #2212). Non-regular names stay visible to name-based repo rules and
  do not count toward the file cap. See [security.md](security.md#security_audit-rule-catalog).
- **Errors:** invalid `min_severity`; path outside auditable roots (or outside the vault,
  with `vault=True`); path does not exist; `remediate=True` combined with `vault=True` or
  with a single-file `path`.
- **Returns:** `{root, files_scanned, status, summary, findings, findings_total, truncated, truncated_by}`. `findings_total` is the count before the cap and `truncated_by` names which cap fired (`files`, `findings`), so a truncated report cannot be mistaken for a complete one. `status`
  is `fail` (any high), `warn` (any medium, no high), else `pass`. `summary` includes an
  `excluded` count of files skipped via `.ua-audit-ignore`/`excludes`.

```json
{"path": ".", "min_severity": "medium"}
```

### `skill_review(path: str = ".", min_severity: str = "info") -> dict`

Lints one `SKILL.md`, one skill directory, or a directory of skill directories against
the auto-invocation contract (frontmatter validity, description trigger vocabulary,
sibling description overlap, body size/payload, filler phrases, broken support-file
links). A single skill is still compared against its unreviewed sibling directories,
since their descriptions compete with it at invocation time.

- **Limits:** reviews ≤100 skills per call (`MAX_SKILLS`); `path` may be workspace-relative
  or absolute under `UA_AUDIT_ROOTS`.
- **Errors:** invalid `min_severity`; `path` is a file that isn't `SKILL.md`; path not
  found; no `SKILL.md` at `path` or in its immediate subdirectories.
- **Returns:** `{root, skills_reviewed, status, summary, findings, truncated}` — same
  `fail`/`warn`/`pass` semantics as `security_audit`.

```json
{"path": ".claude/skills/my-skill"}
```

---

## Git (`tools/git.py`)

All git tools first confirm (via `git rev-parse --show-toplevel`) that the repository
root itself lies inside `UA_WORKSPACE` — `resolve_in_workspace` now also admits the
active task worktree (task #995), but this second check does **not**. A
project-sourced `[code-task]` worktree under `<project>/.worktrees/` is therefore
writable via `fs_write` / `run_cmd` while it is active, and still refused by
`git_*`. Git walks upward to find `.git`, so the toplevel check prevents
operating on a repo whose root is outside the cockpit sandbox.

### `git_diff_staged(repo_path: str = ".") -> dict`

- **Returns:** `{staged_files: [{file, status}], diff_text, is_clean}` (`status` is one
  of `M`/`A`/`D`/`R`/`C` from `git status --porcelain`'s index column).

### `git_commit_staged(message: str, repo_path: str = ".") -> dict`

Commits staged changes; the message header must match Conventional Commits:
`^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\(scope\))?!?:\s.{1,100}$`.
A body is allowed after a mandatory blank line following the header.

- **Errors:** header doesn't match the Conventional Commits regex; a non-blank second
  line (missing header/body separator); `git commit` failure (e.g. nothing staged).
- **Returns:** `{commit_hash, summary, stdout}`.

```json
{"message": "fix(safety): reject symlink escapes in resolve_in_workspace"}
```

### `git_worktree_list(repo_path: str = ".") -> dict`

**Returns:** `{worktrees: [{path, head, branch, bare}]}` (parsed from
`git worktree list --porcelain`).

### `git_worktree_add(path: str, branch: str, repo_path: str = ".") -> dict`

Creates a worktree at `path` (workspace-sandboxed) on `branch`; creates the branch off
`HEAD` if it doesn't already exist locally. `branch` must match
`^[A-Za-z0-9][A-Za-z0-9._/\-]*$` (can't start with `-`, which git would parse as an
option).

- **Returns:** `{path, branch, stdout}`.

### `git_worktree_remove(path: str, force: bool = False, repo_path: str = ".") -> dict`

`force=True` removes a worktree with uncommitted/staged changes.

- **Returns:** `{path, deleted, stdout}`.

### `git_merge_task(task_id: int, force: bool = False, dry_run: bool = False) -> dict`

Orchestrates end-to-end merge of a task's worktree branch (delegates to
`merge.orchestrate_merge`, shared with the `unified-agent-merge` CLI — see
[cli-reference.md](cli-reference.md)): looks up the task in the shared `tasks` table,
locates its worktree (`merge.find_task_worktree` searches the workspace repo, then
each `config.project_repo_roots()` checkout), verifies a clean worktree
(`git status --porcelain` inside that worktree), runs the in-process security audit
and `pytest` inside the worktree, and on success checks out `main`, pulls, merges the
branch (`feat(merge): merge task {id}: {title}` commit message), then cleans up.

Cleanup deletes the source branch only through `update-ref -d` with the merged
commit as the expected SHA (task #2373). If the source tip, worktree path or
branch moved after admission, cleanup keeps both and the result carries
`cleanup_skipped` (for example `"source changed after admission"`). A refused
delete reports `branch_retained` (`"expected-SHA deletion refused"`). `force=True`
does not waive that fence: it still will not delete a branch that is no longer
the merged commit. Mission-linked merges apply the same expected-SHA delete; see
[autonomy-modes.md](autonomy-modes.md).

`dry_run=True` runs every validation step (worktree lookup, cleanliness check, audit,
pytest) without merging or deleting anything, and returns `{success: true, dry_run: true}`
— the same behavior as the CLI's `--dry-run`.

Pytest exit code 5 (no tests collected) fails the gate as `No tests were collected`,
not as `Unit tests failed` (exit code 1). Both require `force=True` to proceed.

- **Errors:** task not found; task not `done` and `force=False`; no matching worktree;
  uncommitted changes and `force=False`; audit/test failure and `force=False` (writes a
  failure report to `00 - Inbox/Merge-Failed-Task-{id}.md` in the vault, when the vault
  is configured); any git subcommand failure.
- **Returns (success):** `{success, commit_hash, task_id, branch}` plus
  `cleanup_skipped` and/or `branch_retained` when the source was kept.

```json
{"task_id": 42}
```

---

## Lint/format (`tools/lint.py`)

### `python_lint_and_format(path: str) -> dict`

Runs `ruff check --fix` then `ruff format` (via `python -m ruff`, each with its own
30 s timeout) against a workspace-sandboxed file or directory. Mutates files in place.

- **Errors:** target path does not exist; either ruff invocation times out or fails to
  execute (not a nonzero exit — that's reported in the return value).
- **Returns:** `{lint_stdout, lint_stderr, format_stdout, format_stderr, success}`
  (`success` is `True` only if both commands exit 0).

```json
{"path": "src/unified_agent/tools/files.py"}
```

---

## Memory (`tools/memory.py`)

Persistent notes in the shared SQLite DB (`memory` table + FTS5 `memory_fts` +
`memory_embeddings`), visible across every host/session pointed at the same `UA_HOME`.
Conventions (tags, authorship, index notes) are covered in depth in
[memory-tasks-vault.md](memory-tasks-vault.md).

Semantic scoring (`memory_search`'s ranking and `memory_save`'s dedup check) tries an
accelerated `sqlite-vec` KNN index (`memory_vec`, mirrors `memory_embeddings` by
`rowid`) first, falling back to the original per-row `cosine_similarity()` scan when
the extension isn't available on this host/connection — identical results either way,
see `tools/memory._semantic_similarity_rows`. The KNN is bounded (task #2214): `k` grows
from 64 only until the cut provably holds every qualifying note, and stays within
sqlite-vec's limit of 4096 (`memory_knn.bounded_knn`). No tool parameter changed; see
[memory-tasks-vault.md](memory-tasks-vault.md#search).

### `memory_save(content, title=None, tags=None, id=None, author=None, append=False, overwrite=False, ..., epistemic=None, evidence=None, valid_from=None, supersedes=None, contradicts=None, links=None, expected_revision=None) -> dict`

Creates a note (omit `id`) or updates one (`id` of an existing note). Cross-author
overwrite guard: replacing the full content of a note owned by a different `author` (or
any owned note when `author` is omitted) is refused unless `overwrite=True`. `append=True`
instead appends an attributed block (`\n\n---\n*[author · timestamp]*\n\n{content}`)
without touching existing content, and requires an existing `id`. On update, omitting
`title`/`tags` preserves the existing values (pass `tags=[]` explicitly to clear tags).
Every save/delete triggers `sync_state_to_vault()` and refreshes the affected
per-tag and master index notes (best-effort embedding generation happens inline too).

- **Errors:** empty `content`; both `append` and `overwrite` passed; `append=True`
  without an `id`; `id` doesn't exist; clobber-guard rejection (message intentionally
  does not restate the `overwrite=True` escape hatch — see the code comment in
  `memory_save` about a 2026-07-12 guard drill); `broadcast` tag (any case, whole
  tag) or requested salience above 0.8 when the verified identity is not human
  (`Desktop_Person`). The claimed `author` is not authority for that. Agents
  may still save ordinary notes at salience 0.8 or below without that tag.
- **Evidence-backed fields (task #1459):** `epistemic` is `fact` or `inferred`;
  an inferred note needs `evidence` refs of the form `<kind>:<id>` (kinds:
  approval, commit, delegation, event, memory, pr, run, session, task, url,
  vault; at most 20, merged on update). `valid_from` is an ISO date/datetime.
  `supersedes` (create only) marks an existing live note as replaced by the new
  one; another author's note needs `overwrite=True`, a broadcast note needs the
  human identity, and a note in another tenant reads as missing.
  `contradicts` links disputed notes without hiding them; on update a list
  replaces the stored links and `contradicts=[]` clears them.
  `evidence_replace=True` (update) replaces the stored evidence instead of
  merging. A peer may add evidence to your note but not change its
  `epistemic`, `valid_from` or `contradicts` or replace its evidence without
  `overwrite=True`. Labelling a legacy note sets `valid_from` to its creation
  time unless given. Labelled notes return these fields from `memory_get`,
  `memory_list` and `memory_search`; `memory_get` also lists `supersedes`.
  See [memory-tasks-vault.md](memory-tasks-vault.md#evidence-backed-notes-and-experience-task-1459).
- **Links (task #2096):** `links` takes typed links as `<rel>:<note id>`, rel
  `extends` or `derives` (at most 20). Each target must be a live note in your
  tenant, else `links: no memory note with id ...`; any other rel is refused.
  On update a list replaces the note's extends/derives links and `links=[]`
  clears them; changing a peer's links needs `overwrite=True`. `supersedes`,
  `contradicts` and `memory:<id>` evidence refs are recorded as links of the
  same name (`evidence` for the refs) without this argument. Every save also
  stores the task, branch, project, service and agent identifiers the note
  names. See [memory-tasks-vault.md](memory-tasks-vault.md#memory-graph-task-2096).
- **Compare-and-set (task #2515):** `expected_revision` takes the `revision`
  `memory_get` returned and needs `id`. If the note changed since that read
  (another agent's append or replace, a link change, a delete), nothing is
  written and the call fails with code `conflict`, carrying
  `expected_revision` and `current_revision`; the check runs before the
  deleted-note refusal. Optional: without it updates behave as before, and
  `append` semantics are unchanged. An idempotent replay of a save that
  already committed returns its receipt without the check.
- **Returns:** `{id, created, revision}`, plus `supersedes` when a note was
  superseded. `revision` (task #2515) is the saved note's new revision.

```json
{"content": "Prefer uv over pip for this repo.", "title": "Package manager", "tags": ["pref", "project:unified-agent"], "author": "agent-claude"}
```

### `memory_search(query: str, tags: list[str] | None = None, limit: int = 10, source_host: str | None = None, include_expired: bool = False, as_of: str | None = None, author: str | None = None) -> dict`

Hybrid search: FTS5 `bm25` ranking plus cosine similarity over `fastembed` vectors
(degrades to FTS-only if embeddings are unavailable, e.g. offline before the model is
cached). A candidate must match FTS keywords OR have semantic similarity ≥ 0.65 (the configurable default for any
query length) OR match a task/branch identifier parsed from the query. `tags`, if
given, requires the note to carry *all* listed tags.
Auto-maintained index notes (`meta:index`) are excluded unless explicitly requested via
a `meta:index*` tag filter. Expired notes are excluded unless `include_expired=True`.
`limit` clamped to `[1, 50]`. The recency leg dates a note by `valid_from` when
set, else `updated_at`. `author` (task #2515) keeps notes by that exact author
label. Every filter (`tags`, `author`, `source_host`, expiry, deletion,
supersede) is part of eligibility, so it applies before each leg's cap and the
`limit` cap, never to an already-capped page.

`as_of` (task #2095) is an ISO datetime with a UTC offset. With it, search
returns the notes valid at that instant: `valid_from <= as_of < valid_to` (each
bound only when set) and `created_at <= as_of`. A note superseded after `as_of`
is returned; one superseded before `valid_to` was recorded is not. Expiry and
recency are measured at `as_of`, and the response carries `as_of`. Without it,
behaviour is unchanged. See
[memory-tasks-vault.md](memory-tasks-vault.md#evidence-backed-notes-and-experience-task-1459).

- **Errors:** query has no non-whitespace tokens; `as_of` malformed or
  without a UTC offset.
- **Returns:** `{results: [{id, title, snippet, tags, updated_at, score, similarity, combined_score, recency, explanation}]}`. Ranking is reciprocal rank fusion of lexical, semantic, entity, recency and graph legs times a prior in [0.8, 1.2] (task #1913, graph leg task #2096); `combined_score = rrf * prior`. `explanation` lists `why` (ordered ranking reasons), `ranks` (per-leg rank or `null`), `rrf`, `prior`, `prior_inputs` (signed `salience`, `confidence` and `vote` inputs), `confidence_posterior` (task #2097: `{alpha, beta, posterior, open_contradictions, effective}`; `effective` is the confidence the prior ranks by), `lexical_mode` (`and`/`or`), per-signal values (lexical, semantic, recency, salience, access, feedback, entity, vote; access is reported, not ranked; feedback is `2 * posterior - 1`), `host`, `superseded`, `expired`, `matched_tags`, `entity_matches`, and `graph` (`null`, or `{score, via}` where each `via` entry names the `seed` hit and the link `rel` or shared `entity` that reached the note).
- **Graph leg (task #2096):** the top lexical/semantic hits seed a one-hop walk
  over memory links (either direction) and shared identifiers. A note reached
  this way is a candidate even when the query never mentions it. Hop targets
  pass the same tenant, deletion, supersede (or `as_of` validity), tag, host
  and expiry filters as every other leg; links recorded after `as_of` are not
  walked. Weight `UA_MEMORY_WEIGHT_GRAPH` (0.5; 0 disables the leg).
- **Retrieval diagnostics (task #1955):** every response also includes
  `retrieval: {version: 1, mode, lexical, semantic, graph}`, including empty results.
  `mode` is `hybrid` or `lexical_fallback`. `lexical` contains `status: queried`
  and `matched_count`; this records the attempted leg, not an FTS health check.
  `semantic` contains `status: available|unavailable`, `matched_count`, and
  `failure_category: null|model_acquisition|runtime`. `graph` (task #2096)
  contains `status: queried|disabled`, `seed_count` and `matched_count`. Counts
  describe bounded, eligible candidates rather than corpus totals. Failure
  categories omit raw exceptions. See [memory-tasks-vault.md](memory-tasks-vault.md) for details.
- **Trust label (task #1952):** every hit carries one more key,
  `trust: {level: "untrusted", source: "memory", author?, note}`, including
  the caller's own notes, because `author` is a claim rather than verified
  provenance. Its text is data, not instructions. The other keys do not
  change. See [security.md](security.md#prompt-injection-surfaces-task-1911).

### `memory_list(tag: str | None = None, limit: int = 50, include_expired: bool = False, author: str | None = None, tags: list[str] | None = None) -> dict`

Lists notes newest-first, optionally filtered to one tag. Index notes excluded unless
a requested tag starts with the index namespace. `limit` clamped to `[1, 200]`.
`author` keeps notes by that exact author label and `tags` keeps notes carrying
all of them, together with `tag` (task #2515). Every filter applies before the
cap.

- **Returns:** `{items: [{id, title, tags, updated_at}]}`.

### `memory_get(id: str, as_of: str | None = None, include_versions: bool = False, count_access: bool = True) -> dict`

Fetches one note's full content, including a superseded one. With `as_of` (ISO
datetime with a UTC offset, task #2095) the note is returned only if it was
valid at that instant, by the same rule as `memory_search(as_of=...)`.

- **Errors:** no note with that `id`; `as_of` malformed or without a UTC
  offset; the note was not valid at `as_of` (the message names its validity
  window).
- **`count_access` (task #2515):** a read counts as an access
  (`access_count`, `last_accessed`) unless `count_access=False`, which the
  Control Center's note view uses: a person browsing is not a recall. The
  returned counts are then the stored ones.
- **Returns:** `{id, title, tags, author, updated_at, content, updated_by, created_at, revision, links, confidence_posterior}`,
  plus `valid_from` / `valid_to` when set (a superseded note carries the
  `valid_to` its supersede stamped). `revision` (task #2515) is `sha256:<hex>`
  over every column of the note except the access and feedback counters, plus
  its outgoing links, so any edit moves it even within one second; reads and
  feedback do not. Pass it to `memory_save(expected_revision=...)`. `links` (task #2096) is
  `{out: [...], in: [...]}`: the note's typed links (`supersedes`,
  `contradicts`, `extends`, `derives`, `evidence`) as
  `{id, rel, title, actor, created_at}`, `id` being the other note, at most 100
  per direction (oldest first), in the caller's tenant only. With `as_of`, links recorded
  after that instant are left out.
  `confidence_posterior` (task #2097) is `{alpha, beta, posterior,
  open_contradictions, effective}`: the Beta posterior from the feedback
  counts, and the effective confidence after open `contradicts` links against
  the note (counted at `as_of` when given). It is separate from the
  author-stated `confidence`.
  With `include_versions=True` (task #2097) the result adds `versions`, the
  note's earlier content and tags as `{version, content, tags, updated_at,
  actor, recorded_at}` newest first (at most 20), and `versions_total`.
  `updated_at`/`actor` say when and by whom a state was written, `recorded_at`
  when it was replaced. A trigger records a version on every content or tag
  change, whichever path made it; a hard delete erases them. With `as_of`,
  only versions recorded by then (`recorded_at <= as_of`, the states already
  replaced at that instant) are returned. Open contradictions under `as_of`
  cannot see a `contradicts` link cleared after it (links keep no deletion
  history).
  Every note also carries
  `trust: {level: "untrusted", source: "memory", author?, note}` (task #1952),
  including the caller's own; the other keys do not change.

### `memory_delete(id: str) -> dict`

Soft-deletes a note and refreshes its tag indexes. The row stays: content is
replaced with the overwrite tombstone and tagged `meta:deleted`. `memory_list`
and `memory_search` hide it. `memory_get` still returns it and sets `deleted`.

The note owner (author) must equal the verified identity, or that identity must
be human. No verified identity is a refusal. A second delete, or a missing id,
returns `{deleted: false}` and does not remove the row.

- **Errors:** no verified identity; caller is not the owner and not a human.
- **Returns:** `{deleted}` (`False`, not an error, if `id` doesn't exist or is already deleted).

---

## Migration manifests (`tools/export.py`)

### `export_migration_manifest(write_to_vault=False) -> dict`

Creates a deterministic, read-only snapshot of tasks, memory, claims,
relationships, source hosts, agent events, and the durable event log. The
manifest includes stable source IDs and a fingerprint. It never imports or
rewrites database rows. Tasks, memory, and durable event-log rows stay within
the caller’s tenant; NULL selects only the private-hub scope. Agent events
require a matching owner in that scope, including for NULL/private callers.
Foreign and unattributed events are withheld; private mode does not grant
installation-wide access. Unattributable references,
including integers outside SQLite’s signed 64-bit range, are omitted and
counted in `agent_events_withheld`.

### `compare_migration_manifests(left, right) -> dict`

Diffs two manifest objects without database access. Reports left/right-only
records, conflicting edits, active-claim conflicts, duplicate candidates, and
missing task parents. Any conflict sets `requires_human_disposition=true`.

### `import_migration_manifest(manifest: dict, apply: bool = False) -> dict`

Reconciles an `export_migration_manifest()` snapshot into the local database.
Idempotent and non-destructive: a manifest row absent locally is inserted; a
manifest row already present locally with identical values is a no-op; a
manifest row present locally with *different* values is held for human
review and the local row is never overwritten (mirrors the queue's
no-silent-reassign rule). Task rows are inserted parent-before-child so the
`parent_id` foreign key never fires; a row whose parent cannot be resolved is
held as `missing_parent` rather than inserted with a dangling reference.

`apply=False` (default) is a dry-run preview — nothing is written.
`apply=True` performs the safe inserts in one transaction and returns a
`rollback_token` (the inserted ids and a tenant-bound, one-use import receipt) for
`rollback_migration_import`. Returns
`{tasks: {inserted, unchanged, held}, memory: {inserted, unchanged, held}, applied, rollback_token?}`.

### `rollback_migration_import(rollback_token: dict) -> dict`

Undoes eligible rows from one prior `import_migration_manifest(apply=True)` call.
Keep the complete returned token: `migration_import_id` binds note/task rows to
their current import generation; graph collections use `memory_graph_import_id`
with the same receipt. Any native update or recreation, even with identical
values, revokes the affected row's rollback authority. Notes are retained when
deletion would cascade into graph data outside this receipt. Task parents are
retained when a descendant is unauthorized or has later operational references.
Retained imported notes keep their startup backfill protection.
Graph-only imports on pre-existing notes also retain the current startup marker
when any entity or incident link survives the explicit graph deletions. This
conservative check includes other imports, native rows and rows whose ownership
or tenant attribution cannot be proved; rollback does not promise to release the
last marker. With no surviving graph, a graph-only import can release its marker
ownership. Startup consumes a retained marker when it visits that note.

If any expected graph ownership INSERT, UPDATE or DELETE trigger is missing,
repair first discards all existing import proofs for that graph table in the
same transaction. A later import receives fresh proofs; old receipts cannot
delete rows whose protection had a gap. A rolled-back repair or dry-run import
does not commit these bookkeeping changes.

Operational references (including workflows, comments, dispatch/usage history,
feedback and delegation results) also retain their referenced rows. Known
structured paths are checked before graph backfill: note `superseded_by`,
`contradicts[]`, `evidence[]` memory/task references and comment
`refs.decision_id`. Jointly eligible imported notes can roll back together;
references from a retained note preserve their targets. This does not interpret
arbitrary prose or application-defined JSON as references. Original imported-note
membership is recorded durably, so trimming token IDs cannot release a retained
note's startup guard.

The receipt is tenant-bound and consumed atomically with rollback; replay returns
zero. Foreign-tenant attempts do not consume it. Older tokens without durable
ownership proof remain accepted but leave unprovable rows untouched. Counts
report only eligible rows deleted, so later edits or dependencies can produce a
partial rollback. Returns `{rolled_back: {tasks, memory, memory_entities?, memory_links?}}`.

## Agent context (`tools/registry.py`)

### `agent_context() -> dict`

Returns the verified caller's identity, active profile, current hub workspace,
peer labels/status/host, and stable operating-rule names. Tokens and peer
filesystem paths are never returned.

Each peer carries `live_sessions` (task #1860, at most 10, newest first): its
open sessions seen within `session_live_minutes` (`UA_SESSION_LIVE_MINUTES`,
default 30), each `{session_id, agent, kind, host, branch, task_id, task_title,
task_source, started_at, last_seen_at, heartbeat_age_s}`. `task_source` is
`session` when the session was registered against a task, `claim` when it named
none and its agent holds exactly one `doing` task, otherwise the task is null.
Pass a `session_id` as `message_send(to_session=...)` to reach that session
alone.

## Tasks (`tools/tasks.py`)

Durable shared task queue (SQLite `tasks` table). Conventions in
[memory-tasks-vault.md](memory-tasks-vault.md).

### Project-scoped task streams (task #327)

Every task carries a `project` column — one of the stable slugs
`unified-agent` | `browser` | `sample-store` | `client-portal`. It is additive,
not a breaking change: tasks created before this existed backfilled to
`unified-agent` (the original single queue), and every call below that omits
`project` keeps behaving exactly as it did before this column existed.

- `task_add(..., project=None)` — defaults to the parent's project if
  `parent_id` is set, else `unified-agent`. A subtask does not silently leak
  into the legacy queue just because its own `project` was left unset.
- `task_list(..., project=None)` — omitted returns tasks across **every**
  project (the explicit cross-project view for Hermes/the operator); pass a slug
  to scope a project-bound session to its own queue.
- `task_update(..., project=None, expected_project=None)` — `project`
  *reassigns* the task to a different stream (a deliberate move). Separately,
  `expected_project` is a *guard*: when set, the update is rejected unless the
  task's current project matches, so a project-scoped session can't mutate
  another project's task by id even if it guesses one.
- `task_claim(..., expected_project=None)` — same guard as `task_update`, for
  claims.
- `delegate(..., project=None)` — passed straight through to the `task_add`
  it creates internally; same inheritance-from-parent rule.
- `export_tasks(..., project=None)` — omitted exports across every project.

Invalid slugs raise `ToolError` from every one of the above; there is no
silent fallback to `unified-agent` for a caller that got the slug wrong.

### Queue lanes (task #705)

Every task also carries a `lane` column — the **actor lane**, who acts on the
task (#883). Allowed values are `default` | `dispatch-ready` | `legacy-operator-only` |
`interactive-only` | `tracking` | `laptop` plus every registered agent label
(`config.task_lane_values()`). Omitted `task_add(lane=)` inherits the parent,
else `default`. `personal` / `professional` / `hobby` are portfolio buckets now
— pass `domain=` for those. The bucket never lands on `lane`.

`task_claim(..., expected_lane=...)` is a guard: with it set, the task must be
in the open pool (`default` / `dispatch-ready`) or that exact lane; with it
omitted, only the open pool or a lane matching the claiming `agent` is
claimable. `tracking` tasks are never claimable. Per-agent and per-lane WIP
caps apply
(`UA_AGENT_WIP_MAX`, per-label `UA_AGENT_WIP_OVERRIDES`, `UA_LANE_WIP`) — see
[memory-tasks-vault.md](memory-tasks-vault.md#queue-lanes-and-wip-caps-task-705-actor-model-883).

`interactive-only` is an operator-started lane distinct from the operator's own
`legacy-operator-only` steps. It is outside the shared pickup/dispatch pool: an agent
must explicitly pass `expected_lane="interactive-only"` to claim it. Relaning
an existing task does not change its status or claimant.

Use `[urgency: now]`, `[urgency: next]`, or `[urgency: later]` in task notes
to make urgency available as a structured task field. Full and compact rows
from `task_list`, and full rows from `task_update` and `task_claim`, include
`urgency` and `urgency_rank` (0, 1, 2 respectively). Unmarked work has
`urgency=null` and rank 1. Markers are case-insensitive; when several valid
markers occur, the most urgent wins. Views that need urgency ordering sort
by `urgency_rank`, then priority (`high`, `med`, `low`), then `created_at` and
`id`. `task_list` itself retains its priority/age order and keyset cursor.

An authenticated `task_claim` reply, and an authenticated `task_update` reply
without an `idempotency_key`, also carries `verified_caller`, the caller's own
verified label (task #2037). It can differ from `current_run_agent`: a `doing`
reassert keeps the existing run, which may belong to another agent. A keyed
`task_update` omits it, because its reply is the stored receipt and another
caller may legitimately replay that key. Unauthenticated calls omit it.

Task rows also carry `next_owner`, `waiting_on`, `terminal_predicate` and
`ownership_generation` (task #2388). `task_update` sets the first three; an
empty string clears one, and `next_owner` must be a known agent label. Only the
stored owner, the claimant, the lane's agent when both are empty, a lifecycle
operator or the human operator may change them. The generation counts owner
changes: a new claim, a handoff, closing the task, or releasing its claim.

A `task_claim` on a task someone else owns is not an error. It returns
`{"id", "claimed": false, "safe_wait": {"owner", "waiting_on", "until",
"generation", "claimed_by"}}` and writes nothing. Ownership is live when another
agent holds an active lease, or when no lease is active and a `next_owner` other
than the caller has a declared wait or pending monitor still running. A stored
owner with neither does not hold the task. Check for `safe_wait` before treating
a reply as a claim. A call passing `expected_run_id` holds a fencing token and
gets a `conflict` error instead. Terminal, lane, parked and tenant refusals
still raise.

A successor replaces live ownership only on a recorded failure (task #2388): the
owner's latest run ended `abandoned`, `cancelled` or `released`, and the owner
has set no wait or monitor since. The claim then succeeds and returns
`replaced: {predecessor, run_id, outcome, finished_at, generation}`, recorded on
`agent.task.ownership`. A bare second claim stays a `safe_wait`.

Migration (#1444): search active task notes for the exact legacy
`[interactive-only]` marker, then move matching rows from `legacy-operator-only` to
`interactive-only` with `task_update(lane=...)`. On 2026-09-29 the live
open, doing, waiting_review, and blocked searches found no such work rows;
only this implementation task mentioned the marker in its specification.
Do not move unmarked `legacy-operator-only` rows.

### `task_add(title: str, notes: str | None = None, priority: str = "med", parent_id: int | None = None, untrusted: bool = False, project: str | None = None, domain: str | None = None, due_at: str | None = None, evidence_ref: str | None = None, idempotency_key: str | None = None, lane: str | None = None) -> dict`

- **Errors:** empty or whitespace-only `title`; title that looks like a markdown heading; invalid
  `priority` (`high`/`med`/`low`); invalid `project`/`domain`/`lane`;
  `parent_id` references a nonexistent task, would exceed `TASK_MAX_DEPTH` (8),
  or (on reparent) would create a cycle.
- **Returns:** `{id}`.

### `project_build(parent_task_id: int) -> dict`

Register a `[project-spec]` hub task as a project root (task #830). Pointer-to-task:
`parent_task_id` *is* the spec and the project root (`project_id` equals that id).
Creates no child steps — file those with `task_add(parent_id=...)`. Not
human-approval-gated.

- **Errors (fail-closed, no partial state):** caller is not a registered agent
  identity; no such task; notes lack a non-backticked `[project-spec]` marker;
  task is claimed by someone else.
- **Returns:** `{project_id, parent_task_id, identity, event_ulid}`. The event is
  appended to `event_log` on channel `agent.project.built` with the caller as
  `originator`, extending the audit hash chain.

### `task_update(id: int, status=None, title=None, notes=None, append_note=None, priority=None, parent_id=None, untrusted=None, archived=None, worktree=None, project=None, expected_project=None, expected_updated_at=None, domain=None, due_at=None, clear_due_at=False, evidence_ref=None, idempotency_key=None, lane=None, actor=None, release_claim=False, run_usage=None, request_cancel=False, expected_run_id=None, monitor_at=None, monitor_note=None, clear_monitor=False, declared_wait=None, next_owner=None, waiting_on=None, terminal_predicate=None) -> dict`

`append_note` atomically appends a non-empty note; a repeated leading marker is deduplicated.

Statuses are `open` | `doing` | `waiting_review` | `blocked` | `done` |
`cancelled`. Setting `status="done"` stamps `completed_at`; setting any other
status clears it. A task whose notes still carry a live `[waiting-for: review]`
must first record a merged/approved outcome or `[review: no-merge]`. A
`[project-spec]` root is refused while any descendant is still active; the
error lists those descendants.
Setting `archived=True` records an archival timestamp; `archived=False` reopens
visibility without changing lifecycle status. Only the fields passed are updated.
`lane` reassigns the queue lane; moving a `doing` task into a capped lane
enforces the same WIP rules as `task_claim`.
`untrusted=True` quarantines the task and its subtree, and anyone may set it.
Clearing it (`untrusted=False` on a quarantined task) needs a verified human
identity; any other caller gets an `ApprovalPendingError` naming a
`task_untrusted_clear` approval request, and the identical retry after a human
approves it consumes the one-shot pass. Each clear is recorded on the
`agent.task.untrusted_cleared` ledger channel (task #1911, see
[security.md](security.md#untrusted-content-boundaries)).

**WIP caps on entering `doing` (task #1886).** Moving a task into `doing` from
another status passes the same per-agent, lane and fleet caps as a new
`task_claim`, for the surviving claimant or else the verified caller, with the
same exemptions: soft overflow returns `wip_warnings`, hard overflow raises
`ToolError`, and `high`-priority or `[urgency: now]` work is never refused by
the fleet cap. Only a call on a row already in `doing` is not checked. A call
with no verified identity gets the lane and fleet caps but no per-agent cap,
and its `actor` is not charged (the run opens with no agent). A surviving
claim whose lease lapsed is renewed when the claimant moves the task, and
dropped when anyone else does. A move that leaves the row archived is
refused (`archived=False` on the same call unparks it first). A keyed retry
returns the same `wip_warnings` as the first call. The
update still takes no claim: `task_claim` is the lock. Details:
[memory-tasks-vault.md](memory-tasks-vault.md#queue-lanes-and-wip-caps-task-705-actor-model-883).

`release_claim=True` clears `claimed_by`, `claimed_at`, and `lock_expires_at`
without changing status. The verified claimant may release its own claim; a
different caller may release only an expired claim. An operator releases
another agent's live claim with `coordination_manage(task_claim_release)` (task
#1700), not with this flag.

**Run record (task #1436).** Entering `doing` opens a self-pickup run when none
is open; leaving `doing`, or `release_claim=True`, closes it with an outcome
(`completed`, `handed_off`, `blocked`, `released`, `cancelled`). `run_usage`
optionally reports the finishing run's cost as any subset of `{provider, model,
input_tokens, output_tokens, cost_usd, cost_complete, billing, phases, contended, capacity}`; unknown keys or wrong types are refused
before any write. `capacity` (task #1841) is `{limit_reached, remaining_pct,
resets_at, source, detail}`; a run that reports `limit_reached: true` and then
releases, blocks or lapses finishes with `reason_code: "capacity_ceiling"`
([self-pickup-runs.md](self-pickup-runs.md#capacity-ceilings-task-1841)). The record is best-effort and never fails the update. See
[self-pickup-runs.md](self-pickup-runs.md).

Under the steward profile (task #591), an update is refused when the task's
`claimed_by` column is a different verified identity, except that an expired
claim may be released with `release_claim=True`. Steward may still update
unclaimed or self-claimed rows. Developer and chief-of-staff are unaffected.
`task_claim` remains denied for steward.

**Monitors and declared waits (task #1749).** `monitor_at` (ISO datetime, at
most 14 days ahead) sets a one-shot monitor; `monitor_note` (optional, at most
500 characters, needs `monitor_at` on the same call) travels with the wake.
When it is due the liveness sweep clears it, counts the attempt, and wakes the
claimant once with a hub message, or records a digest entry when the task is
unclaimed or the message is refused. `clear_monitor=True` cancels a pending
monitor; passing it with `monitor_at` is an error. `declared_wait={"reason":
"...", "until": "<ISO>"}` declares a wait outside the hub; `reason` is
required (at most 500 characters), and `until` must be in the future and at
most 14 days ahead. `declared_wait` also takes an optional `next_step` and
`deployment: {"target", "sha"}` (task #2414): `target` is `hub` or a registered
ua-node, `sha` a full commit. The liveness sweep clears that wait only when the
target runs exactly that commit (the hub's running process, or an online node's
reported `code_sha`), tells the claimant once and records it on
`agent.task.deployment_wait`. An expired one surfaces in `queue_health` under
`deployment_waits`. One
agent may set at most 3 holds (a monitor or declared wait) per task in a rolling
hour; the fourth is refused and names the other three, and clearing one does not
refund it (task #2388). `declared_wait={}` clears it. `monitor_at` and
`declared_wait.until` must carry a UTC offset (`Z`, `+00:00` or another offset,
converted to UTC); a naive time is refused as ambiguous, the same convention
as `task_list(created_since=...)`. Only the task's claimant, the agent its lane
names when the task is unclaimed, a lifecycle operator
(`UA_LIFECYCLE_OPERATORS`) or the human operator (`UA_OPERATOR_LABEL`) may set
or clear either. While a monitor is pending or a wait is live, the task has an
action path and the liveness sweep leaves it alone. Calls that pass any of
these return `liveness: {monitor, declared_wait, monitor_attempts,
monitor_fired_at, monitor_result}`; `declared_wait.first_declared_at` is the
start of an unbroken chain of waits, reported to the digest once it passes 28
days. See [work-queue.md](work-queue.md#liveness-every-task-has-a-next-move).

**Worktree registration and repair (UA task #736).** Entering `doing` with
`worktree=True` (or the implicit default on the transition) creates
`.worktrees/task-<id>` and records `worktree_path`/`branch` on the row. A
worktree created by a dev agent on its *own host* — e.g. desktop-local work
under `~/dev/<repo>/.worktrees/<task>` — never registers on the hub row, so
the columns stay NULL. For an already-`doing` task whose work actually lives
in a local worktree, `task_update(id=N, worktree=True)` repairs the row in
place (materializes/records the worktree without touching status). If the
work was instead done elsewhere and pushed, register the branch by pushing
it to origin and naming it in the notes (`branch: <name>`) or recording its
open PR in `evidence_ref` — the
validate-and-hold review then falls back to a `git ls-remote` origin check
against that branch before it would reject with "no git worktree found"
(review instances #657/#693/#714/#715/#728).

- **Errors:** invalid `status`/`priority`/`project`/`domain`/`lane`; empty or
  whitespace-only `title` / `append_note` when those fields are passed; an
  unparseable or naive `monitor_at` or one beyond 14 days, `monitor_note`
  without `monitor_at`, or a `declared_wait` with other keys, a naive or past
  `until` or one beyond 14 days; a monitor or declared wait change from a
  caller other than the claimant, the unclaimed task's lane agent, a lifecycle
  operator or the human operator; no task
  with that `id`; `expected_project` set and not matching the task's actual
  project; steward updating another agent's claimed task; WIP cap on a
  `doing` lane reassignment.
- **Returns:** the full updated task row (plus a `worktree` field when a
  worktree transition ran: `removed`/`already_gone`/`retained:<reason>`).

### `task_list(status: str = "open", parent_id: int | None = None, notes_contain: str | None = None, priority: str | None = None, limit: int = 100, source_host: str | None = None, archived: bool = False, fields: str = "full", project: str | None = None, domain: str | None = None, lane: str | None = None, office: str | None = None, role: str | None = None, due_before: str | None = None, completed_after: str | None = None, frontier: bool = False, stale_claims: bool = False, cursor: str | None = None, created_since: str | None = None, updated_since: str | None = None, local_day: str | None = None, tz: str | None = None, query: str | None = None, parked: bool = False) -> dict`

`status="all"` returns every lifecycle status. Explicitly archived tasks are
excluded by default; pass `archived=True` to list only them, or `parked=True`
(task #1848) for the parked view: unfinished tasks that were parked or folded,
each with a `parked` object `{kind, reason, by, at, root}`. The two are
exclusive. See [work-queue.md](work-queue.md#closure-stale-parked-folded-merged). Sorted by priority
(`high` → `med` → `low`), then creation order, then `id`. `limit` is clamped
to `[1, 500]` for full rows and `[1, 100000]` for compact.
`fields="compact"` drops `notes` bodies (kilobyte-scale delegation specs) in
favor of `notes_chars`, the typed `claimed_by` column, plus queue-marker
booleans — `project`, `lane`, `urgency`, and `urgency_rank` are included in both
modes. `waiting_for_review`
is the live-marker SQL (`[waiting-for: review]` not immediately followed by
` when done` / ` once shipped`) **and** `status NOT IN ('done', 'cancelled')`.
`lane` / `domain` filter to one bucket; omitted returns the cross-lane /
cross-domain view.
`parent_id` lists direct children only. Each row includes `lineage`: ancestor
ids from the root down to the immediate parent (`[]` for a top-level task).
A `[project-spec]` root also includes `rollup`: derived status from the
entire descendant subtree (`complete` is false while any descendant is
`open`/`doing`/`waiting_review`/`blocked`; `derived_status` is the worst
active status, else `done`). The same roots include `project_view`:
`evidence_count` (done descendants with an evidence pointer),
`merge_verified_count` (those whose reported PR is merge-verified), and
`evidence_cli` (`unified-agent evidence report --project <id>`).

Pagination (task #1430): `truncated` is true when more matching rows exist
beyond the page, including when `limit` was clamped. `next_cursor` is then an
opaque keyset position after the last row returned. Pass it back as `cursor`
with the same filters to read the next page. Rows added or removed between
pages do not duplicate or shift rows already read. A malformed cursor is
refused. A `truncated` result is never the whole queue.

Time rule (task #1430): tasks are stored in UTC and stay UTC. An operator-facing
day is local. `local_day="today"` or `"YYYY-MM-DD"` resolves in `tz`, else
`UA_OPERATOR_TZ`, else the host zone. It matches tasks created, completed or
last updated inside that local calendar day, and the result carries
`window: {local_day, tz, start_utc, end_utc}`. So 23:30 CDT work, which is
already the next UTC day, stays in the local day it happened. `created_since`
and `updated_since` take ISO timestamps with an offset; a naive time is
refused. All three compare instants rather than strings: `julianday` to the
millisecond, then the UTC text within one millisecond. Mixed second and
microsecond precision is therefore exact. Windows compose with `cursor`.

- **Returns:** `{tasks: [...], truncated, next_cursor, limit}`, plus `window` when `local_day` is set.

### `task_delete(id: int) -> dict`

Cascade-deletes the whole subtree (`ON DELETE CASCADE` on `parent_id`),
including nested descendants. `children_deleted` is the descendant count.

- **Returns:** `{deleted, children_deleted, worktrees}`.

---

## Execution graph and autonomous cycles

### `coordination_manage(...)`

Consolidated dependency and handoff entry point for authenticated HTTP
profiles. Use `action` values `dependency_list`, `dependency_add`,
`dependency_remove`, `handoff`, `handoff_list`, or `lineage`, with
action-specific keyword arguments inside `params`. Underlying identity,
ownership, and policy checks remain active; legacy coordination tools remain
available to stdio clients.

Lifecycle actions (task #1351): `agent_state(agent)`, `agent_states()`,
`agent_pause(agent, reason)`, `agent_resume(agent, reason="")` and
`agent_terminate(agent, reason)`. An operator (`UA_LIFECYCLE_OPERATORS`) may
change any agent; an agent only itself. A paused or terminated agent's claims
and renewals are refused; stops are requested per execution path with an
honest `control` result. Retirement (task #2305): `agent_retire(agent, reason,
owner=None)` and `agent_unretire(agent, reason="")`, operators only. Retire is
refused while the agent holds `doing` claims, revokes its `agent_tokens` rows and
session credentials in the same transaction, and `agent_resume` cannot undo it.
See [agent-lifecycle.md](agent-lifecycle.md).

Approval Ledger re-request (task #1854): `approval_ledger_rerequest(number,
head_sha, independent_review=None)` opens the `github_app.process_pr_event` approval row for an open
PR head that a poll missed. Operators only (`UA_LIFECYCLE_OPERATORS`, or the
verified operator via `POST /api/approvals/ledger-rerequest`, which keeps the
action's profile ceiling for everyone else). `head_sha` must
be the PR's current head. It queues the approval and never resolves it, and
it is recorded on `agent.github_app.rerequested`. See
[connector-activation.md](connector-activation.md).
The existing MCP action also accepts a bounded v1 `independent_review` proposal
for human attestation in the **same** release decision. It can replace only an
undecided pending proposal and preserves native resolution/history fences. The
human must inspect the complete immutable payload and execution/review evidence
before approving via UI, API or MCP. Model routing preferences and agent claims
alone are not attestations. See the field contract, bootstrap and scope limits in
[the shipping preflight reference](cli-reference.md#optional-exact-head-approval-preflight-hub-local-module).
The HTTP re-request body remains `number`/`head_sha` only.

Approval Ledger poll-now (task #1867): `approval_ledger_poll()` runs the
scheduled Approval Ledger poll once on the hub, for every open PR head, under
the worker's service identity. Same operators and HTTP twin pattern as the
re-request (`POST /api/approvals/ledger-poll`, also
`unified-agent-review ledger poll`). It requests rows and never resolves one,
and is recorded on `agent.github_app.poll_requested`. Each poll also expires
rows whose pending-status POST failed once GitHub confirms that the PR closed
or moved past the row's head. Each expiry is recorded on
`connector.github.orphan_retired` and returned as `orphan_retired`; a failed
read keeps the row and is returned as `orphan_unverified` (task #2103, see
[connector-activation.md](connector-activation.md)). Each poll also publishes
the `unified-agent/review-head` check at every open PR head, returned as
`review_head_posted` or `review_head_unchanged` (task #2320, see
[automation.md](automation.md#the-unified-agentreview-head-check-2320)).

Device acceptance (task #2112): `node_acceptance_start(node)` queues a
harmless job on the ua-node, using the built-in `acceptance` backend (no model
and no harness). The job has two items: one completes and one is cancelled by
the hub mid-run. `node_acceptance_evaluate(node)` re-evaluates the run and
stores the result. `node_acceptance_status(node=None)` returns one node's live
result, or every node's stored summary when `node` is omitted. Only the node's
own agent or a lifecycle operator can start or re-evaluate a run. The HTTP
twins are `POST /api/action/node-acceptance` and `GET /api/nodes/acceptance`.
`node_acceptance_attest(node, run_id, evidence_ref)` records independent
operator verification of the exact run. Only a configured lifecycle operator
different from the node's agent can attest. Diagnostics alone remain `pending`
with `awaiting_operator_attestation`; `accepted` requires that attestation.
Node-reported events, file digest and code SHA cannot prove honest device
execution. The result is `pending`, `accepted` or `failed`, with a result for each of the
`launch`, `progress`, `receipt`, `cancel_confirmed` and `revocation` checks.
With `UA_NODE_REQUIRE_ACCEPTANCE=1`, a node takes no other work until it is
accepted. See [fleet-console.md](fleet-console.md#device-acceptance-task-2112).

Node retire (task #2181): `node_retire(node, reason)` marks a registered
ua-node row retired, for a renamed or decommissioned device. The row is kept;
`retired_at`, `retired_by` and `retired_reason` are stamped and one
`agent.node` ledger event (`kind="retire"`) is written in the same
transaction. A retired node is left out of the doctor's fleet rows, the
node/fleet listing, acceptance summaries and routing; its owner's
re-registration and heartbeats are refused (403, `code: "node_retired"`) until
`node_unretire(node, reason)` clears the marker
(`kind="unretire"`). Refused (409) while the node has queued or claimed work.
Lifecycle operators (`UA_LIFECYCLE_OPERATORS`) only; repeating either action
returns `changed: false`. See [fleet-console.md](fleet-console.md#retire-a-node-task-2181).

Supervised commands (task #2413): `node_command_start(node, argv, cwd,
timeout_s=None, task_id=None, session_id=None)` runs an argv list (never a
shell string; at most 64 arguments, each under 4096 characters) on a ua-node
that allowlists `argv[0]` exactly as written (`UA_NODE_COMMANDS`). Off until
`command` is also in that node's `UA_NODE_BACKENDS`. The hub keeps the
`node_dispatch` approval gate, with the argv in the approved payload. The
command outlives the starting session. `node_command_status(item_id)` is
visible to the owner, the starter, or a human identity (an unverified caller
reads nothing, 404) and never returns the fence token. `node_command_adopt(item_id,
session_id=None)` rebinds the owner session of an unfinished command to another
session of the owner label, or a lifecycle operator may adopt it; it never
changes the claim attempt or the fence. A completion from a writer that no
longer holds the fence is refused (409, `stale_fence`). When it ends, the
owner label gets one message from `hub-node` with the exit code, duration and
log path. See [harnesses.md](harnesses.md#supervised-long-commands-task-2413).

Work proposals (task #2393): `work_proposal_file(title, items, proposer=None,
reporting_mode="final_only")` files 1-50 items, each `{title, notes?, scope?:
{repo?, files?}, priority?: high|med|low}`; any verified identity may file, an
agent only as itself. `work_proposal_decide(proposal_id, decisions,
approver=None)` takes `[{index, approve, edits?}]` and is refused (403,
`permission_denied`) for any identity that is not the human operator; the
operator decides from the Control Center with
`POST /api/action/work-proposal-decide` `{proposal_id, decisions}`, because the
operator profile has no consolidated managers over MCP. `edits` (approvals
only) may change title, notes, scope or priority and re-freeze the item's
`contract_digest` (SHA-256 of canonical title, notes and scope; `filed_digest`
keeps the original). Each approved item gets one task whose notes end with
`[proposal: wp_…#n]`, `[contract: <digest>]`, `[scope: …]` and
`[reporting: <mode>]`; `task_claim` re-checks that contract and fails closed on
any drift in title, frozen notes or scope. Item states: pending, approving,
approved, rejected, withdrawn; the proposal's status rolls up from its items.
`work_proposal_withdraw(proposal_id)` is proposer only, before any decision.
`work_proposal_recover(older_than_s=300)` (also run by queue hygiene) moves a
stale `approving` item to approved if its task exists, else back to pending.
`work_proposal_get(proposal_id)` and `work_proposal_list(status, proposer,
limit)` read. Events: `agent.work_proposal.*` (ids, digests, status only).
Rows are tenant-fenced.

Claim release (task #1700): `task_claim_release(task_id, expected_claimant,
reason, expected_run_id=None, expected_claimed_at=None)` releases one task's live claim held by another
agent, without terminating that agent. Only a lifecycle operator
(`UA_LIFECYCLE_OPERATORS`) or the verified operator in the Control Center
(`POST /api/tasks/{task_id}/claim-release`) may call it; anyone else is refused
and the claim is untouched. `expected_claimant` (and `expected_run_id`, when
given) are checked inside the write transaction, together with the status and
the claim generation read up front, so a claim that changed hands, a renewed
run, or a drop and re-claim by the same agent is refused rather than released
(a conflict, HTTP 409). The HTTP route requires the `expected_run_id` key.
For a claim with no run, send `expected_run_id: null` and its exact
`expected_claimed_at` generation; both the absence of a run and the generation
are rechecked in the transaction. Omitted or empty run IDs remain invalid,
and stale generations return 409 (#2049). The MCP action keeps the run fence
optional. A done or cancelled task is refused (409): it has nothing to
transfer, and its `claimed_by` records who did the work. A `doing` task
returns to `open` and its self-pickup run is closed in the same transaction,
so the old holder cannot renew it and queue hygiene has no lease to extend;
any other status is kept. The reason is required, appended to the task notes
and recorded with the actor, authority, previous claimant and run on the
`agent.task.claim_released` ledger channel. Release does not stop a running
executor: a live dispatch run is refused until it is cancelled
(`dispatch_cancel`), and a self-pickup holder should be asked to stop with
`task_update(request_cancel=True)` first; pause the agent if it must not pick
the task up again. When `task_update` is approval-gated (`UA_APPROVAL_TOOLS`),
the verified operator's release waits for an approver other than the
requester, so a single-human deployment cannot approve its own release
(separation of duties).

Queue closure actions (task #1848). See
[work-queue.md](work-queue.md#closure-stale-parked-folded-merged).
`queue_health(stale_days=None)`, `queue_stale(stale_days=None, limit=200)` and
`queue_sweep(stale_days=None)` are reads; the sweep is a dry run that returns a
PROPOSED park/fold/close list and writes nothing. `queue_park(task_id, reason,
approved_by=None, fold_children=True, actor=None, dry_run=False)`,
`queue_fold(task_id, into, reason, actor=None, dry_run=False)` and
`queue_unpark(task_id, reason, actor=None, dry_run=False)`
are explicit recorded writes that require a reason; `queue_close_merged(dry_run=True)`
closes only unambiguous merged-PR matches, with the merge SHA as `evidence_ref`.

Task hold actions (task #1747). See
[work-queue.md](work-queue.md#subtree-holds-pause-cancel-resume-restore).
`task_hold(root_task_id, mode="pause", reason="", release_policy="manual",
actor=None, hold_id=None)` holds a root task and its subtree as snapshotted now
(`mode` `pause` or `cancel`, reason required), or releases its active hold
(`resume`, or `restore` to undo the hold's own cancel requests first). Restore
reports a member whose cancelled run is still open as `pending` and keeps the
hold active until a later restore finds every flagged member done.
`release_policy` is `manual` or `after_active_runs_finish` (pause only, and
only while something in the subtree is running). Resume and restore need the
hold's creator or an operator. `hold_id`, when given, must name the root's
active hold. Re-holding a held root returns the existing hold with
`changed: false`. Returns `{action, changed, hold, cancel_requests?,
released?, restored?, pending?}`; `hold` is `{id, root_task_id, mode, status,
reason, release_policy, created_by, created_at, released_by, released_at,
cancelling, member_ids, members, live_run_task_ids}`.
`task_holds(root_task_id=None, status="active", limit=200)` and
`task_hold_get(hold_id)` read. Holding needs `task_update` in the active
profile.

Mailbox actions (task #1859): `message_inbox(...)` and `message_ack(...)`, which
change read state on the caller's own mail. See
`message_list`.

Session handoff actions (task #2397, see
[agent-lifecycle.md](agent-lifecycle.md#session-handoff-notes-2397)). The writers
act as the verified identity; `agent` is honoured only without auth, and the
session must be the caller's own.

- `session_handoff_propose(session_id, done, next_steps, open_questions=None, task_id=None, supersede=False, agent=None)`
  writes a draft note. `done` and `next_steps` need at least one non-empty item
  each; items are redacted and bounded (20 items of 500 characters per list).
  `task_id` defaults to the session's task. Refused while the session already
  has a draft, or a sealed or assigned note not yet delivered unless `supersede=True` (the
  older note is superseded when the new one seals). Per session: a 5-minute
  cooldown between proposals and at most 5 in any rolling hour.
- `session_handoff_update(note_id, done=None, next_steps=None, open_questions=None, agent=None)`
  replaces fields of your draft in place; not a new proposal, so not rate-limited.
- `session_handoff_seal(note_id, agent=None)` is the commit point: the note
  becomes immutable and is what recovery and the session packet read.
- `session_handoff_discard(note_id, agent=None)` drops a draft; a sealed note
  cannot be discarded.
- `session_handoff_recover(session_id)` returns `{note}`: the latest sealed,
  assigned or delivered note for the session in the caller's tenant, or `null`.
  Never a draft.
- `session_handoff_ack(note_id, agent=None)` is the receiver saying it has the
  note. Only the agent the note was assigned or delivered to may call it. It
  sets `acknowledged_at` once, and an `assigned` note becomes `delivered`.

Each writer returns the note: `{id, session_id, agent, task_id, done,
next_steps, open_questions, state, epoch, supersedes, created_at, updated_at,
sealed_at, assigned_to, assigned_at, delivered_to, delivered_at,
acknowledged_at, failover_reason}`. Failover of a pending note to another agent
is hub-side only and not an action. It keeps three facts apart: `assigned` (a
receiver was chosen and recorded; nothing says it has the note), `delivered`
(the transport accepted the note, or the receiver acknowledged it) and
`acknowledged_at`. An assigned note is still pending, so it can be failed over
again; a delivered one cannot.

Team spec actions (task #1736). See [team-spec.md](team-spec.md).

- `team_up(team=None, spec=None, plan=True, expected_plan_sha256=None)`.
  With `plan=True` (the default) returns a read-only dry-run plan for a
  committed spec (`team`, an office id) or an inline draft (`spec`):
  `{team, office, spec_sha256, operating_profile, autonomy, harness_bundle,
  launch_order, edges, seats: [{label, harness, backend, model, host, node,
  workspace: {repo, worktree, cwd}, guidance, skills, edges, checks, refusals,
  warnings, prompt_sha256}], refusals, warnings, ready, plan_sha256,
  plan_text}`. The plan has no timestamps, so equal inputs give an equal plan.
  With `plan=False` boots a committed spec only: refuses when any seat is
  refused, then needs a one-shot approval of the exact plan (approval tool
  `team_up`; `ApprovalPendingError` until a human approves), launches seats
  in edge order through the ua-node queue, re-reading each seat's readiness
  right before its enqueue, and returns `{booted, team_run_id, team_run_ref,
  approval_id, plan_sha256, seats: [{label, node, backend, outcome:
  launched|refused|failed|not_started, item_id, run_ref, record_error?}],
  approval_restored?}`. A changed plan against `expected_plan_sha256`
  returns `{booted: false, stale_plan: true}`; a mission whose autonomy mode
  pauses returns `{booted: false, autonomy_review: {mission_id, pending_id,
  reason}}`.
- `team_list()` returns committed specs: `{teams: [{team, description,
  operating_profile, spec_sha256, seats, edges, launch_order}], source}`.
- `team_seat(label=None)` returns the caller's own seat(s): `{agent, seats:
  [{team, harness, model, host, workspace, skills, operating_profile,
  teammates, edges: {outgoing, incoming}, latest_run}]}`. A verified caller
  cannot read another label's seat. `profile_status` also carries a compact
  `team_seats` list when the caller holds a seat.
- `team_spec_commit(team, spec)` writes one spec into the policy file's
  `teams` block as a `policy` config revision. It needs
  `config_revision_manage` authority (chief of staff), like every config
  commit, and the policy validator refuses unknown offices, unknown roster
  labels and malformed specs.

### `dependency_list(limit: int = 500, task_id: int | None = None) -> dict`

Lists explicit task prerequisite edges with stable IDs, ordered by edge ID.
When `task_id` is supplied, filters to that task's prerequisites before applying
`limit` (clamped to 1–2000). Omit it to read a bounded global page. Returns
`{dependencies: [{id, task_id, depends_on_task_id, created_by, created_at}], truncated: bool}`.
`truncated` is true when more matching edges exist beyond the returned page;
callers must require `truncated` to be false before treating it as complete.

### `dependency_add(task_id: int, depends_on_task_id: int, agent: str | None = None) -> dict`

Adds a foreign-key-backed edge after validating both tasks and rejecting self-links,
duplicates, and transitive cycles. The caller must be a known verified agent.

### `dependency_remove(dependency_id: int, agent: str | None = None) -> dict`

Removes an edge only for its creating agent. Returns `{removed, dependency_id}`.

### `handoff(task_spec: str, to_agent: str, agent: str | None = None, deadline: str | None = None, priority: str = "med", parent_task_id: int | None = None) -> dict`

Creates a delegation handoff bound to the verified sender. Returns lifecycle IDs only:
`{handoff_id, task_id, status}`; task specs and callback payloads are not returned.

### `handoff_list(limit: int = 200) -> dict`

Returns sanitized handoff lifecycle metadata without specs, memory IDs, or callback
payloads.

### `lineage(task_id: int) -> dict`

Per-task execution lineage via one null-tolerant JOIN (task #730). Read-only
projection over existing rows: delegations with `cancelled` filtered out,
left-joined `dispatch_runs` and `autonomous_cycles` on `task_id`, plus
`refs` (`pr_url` with an evidence-ref fallback, `worktree_path`, `branch`).
Missing or unknown task ids return `{"empty": true}` rather than an error.
Exposed to the web UI as `GET /api/execution-graph?task=N`.

## Graph planning canvas (tools/graph_canvas.py, task #807)

The Control Center's Canvas tab embeds Excalidraw as a planning canvas. Scenes
are stored verbatim per board id in `graph_canvas_scenes`; the same storage
backs the web UI's `GET/PUT /api/graph-canvas`. These tools never create tasks,
edges, or delegations — the parser output is a SUGGESTION payload for a human,
and any execution would flow through the existing approval queue.

### `graph_canvas_read(board_id: str = "default") -> dict`

Returns the stored scene plus a deterministic parse of its text labels:
`{board_id, exists, updated_by, updated_at, scene, parsed}` where `parsed` is
`{node_count, edge_count, unparsed_count, task_suggestions,
dependency_suggestions}`. Labeled shapes become `task_suggestions`; arrows
bound to two labeled shapes become `dependency_suggestions` (from → to). A
never-written board returns `exists=False` with an empty scene, not an error.

### `graph_canvas_write(board_id: str = "default", scene: dict | None = None, agent: str | None = None) -> dict`

Stores an Excalidraw scene JSON object verbatim (≤2 MB, ≤1000 elements),
attributed to the verified caller identity (optional `agent` must match it). Writing does not parse or act on
the sketch. Returns `{board_id, saved, updated_by, updated_at, size_bytes}`.

## Horizon 2 connectors

Horizon 2 (`horizon2.py`, task #664) is a **provider boundary**, not an
executor. `ExecutionEnvelope` / `A2AAdapter` / `ServerGovernance` do not
mutate tasks, resolve approvals, or spawn work. `adapter_for()` (task #665)
returns observation adapters for `codex`, `claude`, `agy`, and `langgraph`
that project existing hub state; they still do not spawn work.
`A2AAdapter.discover` / `exchange` (task #666) talk to a remote Agent Card
and JSON-RPC endpoint; they never create or claim a Unified-Agent task. The hub remains the
authority: a remote provider may report observations, but cannot establish
ownership or bypass the policy gate. Durable state lives in
`governed_servers`. Register, revoke, and authorize always consume a
one-shot approval (`force=True`) even when the tool is absent from
`UA_APPROVAL_TOOLS`. Authorize also refuses expired reviews
(`UA_H2_REVIEW_TTL_DAYS`, default 90) and, when `UA_H2_MCP_CONNECTIONS`
is set, connectors that are not in that live MCP inventory. This is not
a second registry.

### `horizon2_server_list(include_revoked: bool = False) -> dict`

List reviewed Horizon 2 connector registrations. By default only active
(non-revoked) rows are returned. Requires hub authentication.

Returns `{servers, count}`. Each server includes `server_id`, `connector`,
`provenance`, `scopes`, `reviewed_by`, `review_ref`, `revoked_at`,
`created_at`, and `active`.

### `horizon2_server_register(server_id: str, connector: str, provenance: str, scopes: list[str], review_ref: str) -> dict`

Register one reviewed connector after consuming an exact one-shot approval
bound to this payload. Always gated (`force=True`), even when the tool is
absent from `UA_APPROVAL_TOOLS`. Duplicate `server_id` is refused without
consuming a second pass. Unified-Agent remains the authority; this does not
grant execution.

Returns `{server, approval_id, event_id}`.

### `horizon2_server_revoke(server_id: str) -> dict`

Persistently revoke a reviewed connector after consuming an exact one-shot
approval. Already-revoked servers return `{already_revoked: true}` without
consuming another pass.

Returns `{server, approval_id?, event_id?, already_revoked?}`.

### `horizon2_connector_authorize(server_id: str, scope: str, task_id: int, trace_id: str, connection_ref: str | None = None) -> dict`

Authorize one connector use for an active registered scope. The call consumes
an approval bound to connector, scope, task, and trace. Unregistered servers,
unapproved scopes, expired reviews, and unbound live MCP connections fail
before the pass is consumed. `connection_ref`, when set, must match the
registered connector.

Returns `{server, approval_id}`.

### `horizon2_package_list() -> dict`

List packaged connector names and default scopes (Slack, Drive, Obsidian,
Readwise, Granola). This is packaging for `governed_servers`, not live
Slack/Drive/Obsidian dogfood (that remains task #592).

Returns `{packages, count}`. Each package includes `package` and `scopes`.

### `horizon2_package_register(package: str, provenance: str, review_ref: str, server_id: str | None = None, scopes: list[str] | None = None) -> dict`

Register one packaged connector by wrapping `horizon2_server_register`.
Always approval-gated on that canonical tool. Unknown packages and scopes
outside the package fail before a pass is consumed. Does not mint live
tokens or start a Slack/Drive session.

Returns `{server, approval_id, event_id}`.

### `autonomous_cycle_manage(...)`

Consolidated autonomous-cycle lifecycle entry point for authenticated HTTP
profiles. Use `action` values `start`, `get`, `get_for_task`, `transition`,
`kill`, `record_usage`, or `list`, with action-specific keyword arguments inside
`params`. Existing state-transition, approval, kill-switch, and budget checks
remain active; legacy cycle tools remain available to stdio clients.

`get_for_task` (`params.task_id`) is the same read-only lookup the executor
uses before dispatch (`active_cycle_for_task`). Desktop daemons whose local
`memory.db` is an intentional tombstone call it over the hub so the kill /
budget gate still sees `killed` and `blocked` cycles (task #992). It excludes
only `completed` / `failed` — not `killed`. A hub failure is not "no cycle";
the daemon fails closed. See Mac hub topology.

### `bug_worker_manage(action: str, params: dict | None = None) -> dict`

Dispatch bounded SYSTEM bug-to-patch worker operations. Actions: `status`
(kill-switch/config report), `discover` (frontier SYSTEM bugs with
`bug: true` + `worker-eligible: true`), `check_eligibility` (one task, no
claim), `run_once` (full claim→patch→verify→waiting_review cycle; requires
an in-process patcher when invoked via MCP). Disabled unless
`UA_BUG_WORKER_ENABLED=1`. Never auto-merges — handoff stops at
`waiting_review` with `[waiting-for: review]`. See `docs/bug-worker.md`.

### `autonomous_cycle_start(agent: str, task_id: int | None = None, plan_id: str | None = None, delegation_id: int | None = None, max_rework: int = 2, budgets: dict | None = None, capabilities: list[str] | None = None) -> dict`

Creates a durable cycle in `plan` state. `budgets` may set positive `time_s`, `tokens`,
`retries`, and `cost_cents`; omitted values use the `UA_AUTONOMY_MAX_*` defaults.
Capability requests are checked against the agent's allowlist.

### `autonomous_cycle_transition(cycle_id: str, to_state: str, idempotency_key: str, agent: str, expected_version: int | None = None, reason: str | None = None, pr_url: str | None = None, capability: str | None = None) -> dict`

Applies one compare-and-swap transition through the bounded cycle state machine.
Retries with the same idempotency key are safe. `pr`, `completed`, `blocked`, and
`failed` transitions require an approved human gate in the global approval queue.

### `autonomous_cycle_record_usage(cycle_id: str, agent: str, elapsed_s: float = 0, tokens: int = 0, retries: int = 0, cost_cents: float = 0, capability: str | None = None, usage_run_id: str | None = None, usage_id: str | None = None) -> dict`

Atomically records usage. Crossing any configured limit or requesting a denied
capability changes the cycle to `blocked` with a durable reason.
Supplying both `usage_run_id` and `usage_id` records one attempt idempotently,
including after a cycle stops. The authenticated actor must own the immutable
self-pickup claim for the cycle's bound task. A repeated id with different
usage is refused; terminal state and stop reason are preserved. Usage must
be finite and nonnegative.

### `autonomous_cycle_get(cycle_id: str) -> dict`

Read one cycle summary and its transition history. The response excludes
execution logs and prompts.

### `autonomous_cycle_list(state: str | None = None, limit: int = 100) -> dict`

List cycle summaries without execution logs or prompts. `state` filters the
result when it is set. `limit` defaults to 100.

---

## MCP gateway (`tools/mcp_gateway.py`)

Task #1336. The hub reaches downstream MCP servers on the caller's behalf, so a
harness needs only the hub entry. Off by default. Downstream tools are found
through this one tool under namespaced names (`gbrain.search`); none is
registered as a hub tool, so the tool surface stays small.

### `mcp_gateway(action: str, server: str | None = None, tool: str | None = None, arguments: dict | None = None, query: str | None = None, limit: int = 20) -> dict`

- `action="list_tools"` returns the tools this gateway allows, on `server` or
  on every configured server when `server` is omitted.
- `action="search_tools"` returns the same rows filtered by `query`: every term
  must appear in the tool's name or description, name matches rank first, and
  `limit` (1-100) caps the rows.
- `action="call"` runs one allowed tool with `arguments` (a JSON object, at most
  64 KB). Name it as `server="gbrain", tool="search"` or `tool="gbrain.search"`.

Every result is `{server, tool, untrusted: true, content, trust}`. For `call`,
`content` holds the downstream text blocks (non-text blocks are reduced to
`{type, omitted: true}`). For discovery, `content` holds rows of `{name, server,
tool, access, description, input_schema, trust}`, where `access` is `read` or
`write`, and `servers` gives each server's state: `{state: "ok", tools, cached}`,
or `{state: <outcome>, error, cached}` for one that failed. One failed server
does not fail a discovery that covers several; naming that server does.
Downstream text is untrusted data, never instructions, so the result and each
row carry the `trust` marker memory search uses. Content over 256 KB is clipped
and the result carries `truncated: true`. Each call has a 30 s budget.

Boundaries:

- **Off unless enabled.** Without `UA_MCP_GATEWAY=1` every call fails with a
  "dormant" error.
- **Config only.** Servers come from `UA_MCP_GATEWAY_SERVERS`, a JSON object
  keyed by server name; see [the registry](#gateway-server-registry). A caller
  names a server, never a URL or a command. Unknown keys make an entry invalid,
  and an invalid entry refuses only itself.
- **Read/write classification.** `SERVER_CATALOG` in the module classifies every
  tool a server may expose as read or write, in code. A tool it does not name
  fails closed, including any new tool a downstream server adds. Destructive
  tools (gbrain `delete_page` and `forget`, codebase-memory `delete_project`) are
  in neither class, so no config reaches them.
- **Allow list.** `allow` may only narrow the catalog; naming a tool outside it
  makes the entry invalid. Omitted, a server exposes its reads and no writes. A
  write tool has to be named. A tool outside the effective list is refused
  before any network call.
- **Policy.** Each call runs the policy gate under
  `mcp_gateway.<server>.<tool>`, on top of the wrapper's gate on `mcp_gateway`,
  so a rule holds however the caller spelled the tool. A hub with
  `default_action: deny` needs an allow rule matching `mcp_gateway*`.
- **Approval.** A write also needs a verified, unrevoked caller and a one-shot
  human approval on `mcp_gateway_write`, whatever `UA_APPROVAL_TOOLS` says. The
  first call parks and returns the approval id, the identical call runs once
  approved, and any change to the arguments needs a new approval. The pending
  row shows the arguments, as for every gated tool, because the approver has to
  read them.
- **Credentials.** `secret` names a managed secret bound to consumer
  `mcp_gateway` (`managed_secret_set(name, value, consumer="mcp_gateway")`).
  HTTP sends it as a bearer header; stdio passes it only through the
  environment variable `secret_env` names. A downstream server that reflects it
  gets nothing back to the caller: the token and its raw, base64,
  percent-encoded and hex forms are redacted from returned content and from
  cached discovery rows, and a log filter redacts them from the MCP SDK and HTTP
  client loggers while a call is in flight. It never appears in a result, event,
  log line or error. A caller with `UA_AGENT_WORKSPACES` DENY can still use the
  gateway (task #2357): the hub reads that secret for its own outbound call,
  never hands it to the caller, and every other consumer of the store keeps
  the DENY refusal. Before this, the doctor reported a missing secret for those
  identities.
- **HTTP transport.** The URL host puts a server in one scope: loopback
  (`localhost`, `127.0.0.0/8`, `::1`), tailnet (`*.ts.net`, `100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`) or public. Plain `http` is allowed only for loopback and
  tailnet. The URL has no userinfo, query or fragment, and no numeric host other
  than a canonical IP literal (`2130706433`, `0177.0.0.1` and `127.1` are
  refused). At call time the host is resolved once and every returned address
  must be inside its scope. A public host must resolve to public unicast only:
  no private, link-local, metadata, multicast, unspecified, NAT64, 6to4, Teredo
  or IPv4-compatible addresses, and IPv4-mapped addresses are judged as IPv4. The
  connection is pinned to that address; TLS SNI, certificate checks and the Host
  header keep the configured name. No proxy environment, no redirects, identity
  encoding only. Every response body, including errors, is bounded before the SDK
  parses it: a Content-Length precheck plus a byte count that aborts past 1 MB
  per response or 4 MB per call.
- **Stdio transport.** `command` is an absolute path whose file name is on the
  server's code-owned list (`codebase-memory-mcp`), run with
  no shell and one child per call. The child gets `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM`, `USER`, `LANG`, `LC_ALL`, `TMPDIR` and `XDG_RUNTIME_DIR`, plus
  the entry's `env`, never the hub's environment. `env` may not name a credential,
  `PATH`, `LD_*`, `DYLD_*`, `PYTHONPATH` or `NODE_OPTIONS`. stderr is discarded.
  stdout carries the same byte bounds as an HTTP body. After each call stdin is
  closed, then the process group gets SIGTERM and SIGKILL, half a second apart.
- **Bounded failure.** A downstream failure or timeout is a short fixed error.
  A hard deadline just past the call budget holds even for a stuck resolver.
  Four calls run per server; another is refused with outcome `busy`, not queued,
  and before any approval is spent.
- **Discovery cache.** Tool lists are cached per server for
  `UA_MCP_GATEWAY_DISCOVERY_TTL_S` (default 300, at most 3600, 0 turns it off). A
  failure is cached for at most 30 s, so a dead server costs one timeout per
  window. A config change invalidates the entry.
- **Evidence.** Every call past the dormant gate records exactly one event on
  `agent.mcp_gateway.call` with `action`, `server`, `tool`, `access`, `caller`,
  `args_sha256` (the hub's redacted digest, which cannot confirm a guessed
  secret), `outcome` and `latency_ms`, plus `approval_id` on a gated write,
  including validation refusals and internal failures. Raw arguments are never
  recorded there. Outcomes: `ok`, `ok_truncated`, `ok_partial`, `refused_action`,
  `refused_arguments`, `misconfigured`, `refused_server`, `refused_tool`,
  `refused_identity`, `policy_denied`, `approval_pending`, `approval_denied`,
  `busy`, `secret_unavailable`, `resolve_failed`, `address_refused`,
  `redirect_refused`, `response_too_large`, `encoding_refused`, `spawn_failed`,
  `timeout`, `transport_error`, `downstream_error`, `internal_error`.

<a id="gateway-server-registry"></a>
**The registry.** One entry per server, for example:

```json
{
  "gbrain": {"url": "https://brain.example.ts.net:8443/mcp", "secret": "mcp:gbrain"},
  "codebase-memory": {
    "transport": "stdio",
    "command": "/opt/bin/codebase-memory-mcp",
    "args": ["--tool-profile=analysis"]
  }
}
```

| Key | Transport | Meaning |
|---|---|---|
| `transport` | both | `http` (default) or `stdio` |
| `url` | http | The server's MCP URL |
| `secret` | both | Managed secret name; required for http, optional for stdio |
| `command`, `args`, `cwd` | stdio | Absolute executable, its arguments, its working directory. Never put a credential in `args`. |
| `env` | stdio | Extra child environment; no credentials |
| `secret_env` | stdio | The variable that receives `secret`; set both or neither |
| `allow` | both | Tool names; narrows the catalog and is the only way to enable a write |

codebase-memory speaks MCP over stdio only: its HTTP port serves its graph UI,
not MCP. `--tool-profile=analysis` has it advertise only the catalog's reads.

Exposure: steward and chief-of-staff profiles and the `connectors` toolset.
It is pruned from the developer default profile (100-tool ceiling) and
classified WITHHELD for operator clients. `system_doctor` reports one
`mcp_gateway` row: DORMANT when the flag is off, MISCONFIGURED when the value is
unusable or no server is healthy, DEGRADED when only some are, LIVE when all are.
Its `servers` field and `detail` give each server's own state. A server is
MISCONFIGURED for an invalid entry, a missing or wrongly bound secret, or a
command that is not executable, and DEGRADED when its last discovery failed. The
doctor makes no network call and starts no process.

`ua mcp-entry [--harness NAME]` prints the one hub entry for each harness. See
[the CLI reference](cli-reference.md).

---

## Vault (`tools/vault.py`)

Obsidian vault bridge (`UA_VAULT_PATH`). Reads work anywhere in the vault except
dot-directories (`.git`, `.obsidian`, `.trash`); writes are additionally confined to
`UA_VAULT_WRITE_DIRS` and to `.md`/`.txt` files. All tools raise `ToolError` immediately
if `UA_VAULT_PATH=""` (vault disabled) or the configured path doesn't exist. Layered
conventions (Inbox/Wiki/PARA) are in [memory-tasks-vault.md](memory-tasks-vault.md).

### `vault_read(path: str, offset: int = 0, limit: int = 2000, expand_embeds: bool = False) -> dict`

Same shape as `fs_read`: 1 MB size cap, `limit` clamped to `[1, 5000]`.
`expand_embeds=True` expands `![[note]]` transclusions inline, up to two levels deep.

- **Returns:** `{path, content, total_lines, truncated, revision, locked}`. `revision`
  (task #2111) is `sha256:<hex>` of the note's raw bytes on disk, from the same
  read as `content`. It never covers expanded embeds or the paging window, so
  every page of one note, and every agent reading it, sees the same revision
  until the note changes. Pass it to `vault_write(expected_revision=...)`.
  `locked` (task #2515) is the verdict `vault_write`'s guard reaches on the
  same bytes (`note_lock.note_bytes_are_locked`): true for frontmatter
  `locked: true`, and true when the frontmatter is too long to decide, since
  the guard fails closed.

### `vault_write(path: str, content: str = "", mode: str = "overwrite", append: bool = False, force: bool = False, template: str | None = None, expected_revision: str | None = None) -> dict`

`template` instantiates the note from a vault template (placeholders such as
`{{date}}`, `{{title}}`, `{{content}}`). A note with `locked: true` frontmatter
is refused unless `force=True`.

- **`mode`:** `"overwrite"` (default — replaces the whole file) or `"append"`.
  `append=True` is the legacy alias for `mode="append"`. Any other `mode` raises
  `ToolError` — there is **no partial-edit mode**; to change part of a note,
  `vault_read` it and write the whole thing back. (Before task #854 an unknown
  keyword like `mode="append"` was silently dropped and the write clobbered the
  file.)
- **`template`:** a vault path to instantiate the note from. Placeholders
  `{{date}}`, `{{time}}`, `{{datetime}}`, `{{yesterday}}`, `{{tomorrow}}`,
  `{{title}}` (the target's file stem) and `{{content}}` are replaced; without a
  `{{content}}` placeholder, `content` is appended.
- **`force`:** a note with `locked: true` frontmatter is refused, templated or not,
  unless `force=True`. Pass it only with human or steward approval.
  The lock check is the same one `vault_journal`, `obsidian_append` and the Wiki
  custodian use, and it reads real-world spellings: CRLF line endings, a UTF-8 BOM,
  `yes`/`on`/`True`, quoted keys and inline comments. Cloud-hosted callers cannot
  override a lock at all. The check and the write run under the per-note advisory
  lock that `vault_journal` and `obsidian_append` also take (#1890): an `fcntl.flock`
  on a sidecar file `$UA_HOME/locks/vault/<sha256 of the case-folded vault-relative
  path>.lock` (directory 0700, file 0600), not on the note, so a writer that replaces
  the note atomically (sync, git checkout, a temp-file rename) does not escape it,
  and a note that does not exist yet is locked too. Obsidian and other non-hub
  writers do not take it. A note the hub cannot read is refused with "cannot read
  note ... to check its lock" rather than written past an unverified lock. A
  FIFO, socket or device at the note path is that case (task #2212): the lock
  check opens without blocking and refuses once `fstat` shows it is not a
  regular file, instead of hanging on `open()`.
- **`expected_revision`** (task #2111): makes the write conditional on the note
  still being at the `revision` `vault_read` returned. It applies to overwrite,
  append and template writes. The comparison runs inside the same per-note lock
  as the `locked: true` guard, after it, so a locked note still reports as locked
  and two writers holding the same revision cannot both succeed: the second
  queues on the lock, sees the first one's bytes and is refused. A mismatch
  raises `ToolError` naming the current revision; nothing is written. Re-read,
  merge your change into the current content, and write again with the new
  revision. `"absent"` means the note must not exist yet (create-only); a
  `sha256:` revision for a note that has since been moved or deleted is refused
  too. Any other value is refused as malformed. Omitted, the write is
  unconditional, as before, unless the hub sets `UA_VAULT_REQUIRE_REVISION=1`:
  then an overwrite of an existing note without `expected_revision` is refused,
  while creates and appends stay allowed.
- **Limits:** 2 MB content cap; `.md`/`.txt` only; must resolve under an allowlisted
  write dir.
- **Returns:** `{path, bytes_written, mode, append, total_lines_after, revision}`.
  `revision` is the note's revision after this write, taken under the note lock.
  If the post-write readback fails, `total_lines_after` (and an append's
  `revision`) is `None` rather than an error, because the write is already durable.

> Server-wide since task #854: every tool's argument model is `extra="forbid"`.
> A keyword a tool does not declare now fails the call with a validation error
> instead of being ignored.

### `vault_move(source: str, destination: str, force: bool = False) -> dict`

Moves/renames a single note within the vault, creating destination parent
directories as needed. **Chief-of-staff profile only** — raises `ToolError`
for developer/steward callers regardless of destination. Source and
destination both resolve through `resolve_in_vault(for_write=True)`, so they
get the same traversal/hidden-path/write-dir/file-type checks as
`vault_write`; chief-of-staff's widened `01 - Projects`..`04 - Archive`
allowlist is what lets this reach PARA folders `vault_write` cannot. A locked
source (`locked: true` frontmatter) or an existing destination is refused
unless `force=True` (no silent overwrite of curated notes or collisions). The
move is a single `shutil.move`, atomic on the vault's own filesystem.

- **Errors:** non-chief-of-staff caller; missing source; destination is a
  directory; source == destination; locked source without `force`; existing
  destination without `force`.
- **Returns:** `{source, destination, bytes_moved}`.

### `vault_archive_plan(inbox_path: str | None = None, archive_root: str | None = None, as_of: str | None = None) -> dict`

Read-only planner for the processed-inbox filing workflow. A processed marker is
movable only when its content-hash ingest ledger is unchanged. Only
`status: final` or an explicit `implementation_status: shipped` (also accepted:
`implementation: shipped` or `shipped: true`) permits authoritative Wiki
promotion; other processed notes are classified `archive-only`. An explicit
`superseded_by`/`superseded-by` frontmatter marker is independently movable but
never promotes to Wiki.

When omitted, desktop mode uses `00 - Inbox` and
`04 - Archive/inbox-archive`; laptop mode uses `00 - Inbox/Laptop` and
`04 - Archive/inbox-archive/Laptop` so synced captures remain device-scoped.

The result includes `read_only: true`, counts, source/destination pairs,
classification, promotion decision, and ingest status. It never moves or edits
notes; execution remains a separate chief-of-staff-only `vault_move` operation.

### `vault_list(path: str = ".", glob: str | None = None, max_entries: int = 500) -> dict`

Hidden entries (dot-prefixed) are always omitted. `max_entries` clamped to `[1, 2000]`.

- **Returns:** `{entries: [{name, type, size}], truncated}`.

### `vault_search(pattern: str, path: str = ".", max_results: int = 100) -> dict`

Regex search restricted to `.md`/`.txt` files, skipping hidden directories and files
over 1 MB. `max_results` clamped to `[1, 1000]`.

- **Returns:** `{matches: [{path, line_no, line}], truncated}`.

### `vault_links(path: str) -> dict`

Maps `[[wikilink]]` graph neighborhood: outgoing links parsed from the note, and
backlinks (any other note whose `[[target]]` matches this note's stem or vault-relative
path minus extension).

- **Returns:** `{path, outgoing: [str], backlinks: [str]}`.

### `vault_journal(entry: str, agent: str, tags: list[str] | None = None) -> dict`

Appends a timestamped, agent-attributed line to today's `Logs/YYYY-MM-DD.md`, creating
the note (with frontmatter) on first write of the day. This is the designated
bookkeeping chokepoint — every agent is expected to log notable actions here under its
own label.

- **Errors:** empty `entry`; empty/whitespace `agent`; the day's note is locked
  (`locked: true` frontmatter) — a locked journal takes no appends. Override with
  `vault_write(path, content, mode="append", force=True)` only with human or steward
  approval.
- **Returns:** `{path, entry, created_note}`.

```json
{"entry": "Committed fix(safety): reject symlink escapes.", "agent": "agent-claude", "tags": ["commit"]}
```

### `vault_wiki_custodian(dry_run: bool = False) -> dict`

Delegates to `wiki.run_custodian()` (shared with the `unified-agent-wiki` CLI — see
[cli-reference.md](cli-reference.md)): audits `Wiki/` and rewrites
`Wiki/Health.md` wholesale. `Wiki/Index.md` (the curated map) is never touched.
The same pass reports broken `[[links]]`, orphans, duplicate-identity
candidates, missing aliases, empty/stub pages, stale pages
(`UA_WIKI_STALE_DAYS`, default 90), ambiguous bare names, and pages missing
from `Index`. `Wiki/Health.md` and `Logs/state_*` mirrors are skipped as link
sources so generated text does not re-report the same non-findings (UA #855 /
task #206).
A locked `Wiki/Health.md` (`locked: true` frontmatter) is not overwritten: the
rewrite is skipped with a notice, the audit still returns, and the result carries
`health_report_skipped: "locked"` with `index_rebuilt: false`. Unlock it, or have a
human/steward approve the override, to regenerate the report.

- **Returns:** `{wiki_notes_count, inbox_notes_count, broken_links, orphans,
  duplicate_candidates, missing_aliases, empty_pages, stale_pages,
  ambiguous_names, index_present, unindexed_pages, index_rebuilt}`
  (`index_rebuilt` is `False` when `dry_run=True`).
  `dry_run=True` is write-free: no `Wiki/` or inbox `mkdir`, and no
  `Health.md` rewrite. A hosted empty vault is refused before the scan
  (`hosted_capabilities.refuse_unpopulated_vault`).

## Obsidian runtime (`tools/obsidian.py`)

These tools use the optional Obsidian Local REST API community plugin, not the
vault file tools. Configure `UA_OBSIDIAN_API_URL` and `UA_OBSIDIAN_API_KEY` in
the MCP server environment. The plugin normally uses HTTPS on port 27124 with
a self-signed certificate; the wrapper supports the plugin's local HTTP mode too.
For locality, the URL must target `localhost` or a loopback IP; remote endpoints
are rejected before any request is sent.
Read tools return `not-configured` when either variable is missing. `obsidian_open`
and `obsidian_append` resolve paths through the same `UA_VAULT_WRITE_DIRS` boundary
as vault writes and are subject to the global approval gate when those tools are
listed in `UA_APPROVAL_TOOLS`.

### `obsidian_status() -> dict`

Returns the plugin's root status payload, including `authenticated` and version
details. Without both configuration variables it returns
`{configured: false, reason: "not-configured", missing: [...]}`.

### `obsidian_open(path: str) -> dict`

Opens one allowlisted vault note in the running Obsidian UI. The path may be
vault-relative or absolute, but is returned and sent to the plugin as a
vault-relative path.

- **Returns:** `{path, opened: true, response}`.

### `obsidian_active_note() -> dict`

Returns the running app's active note content and its `Content-Location` path.

- **Returns:** `{path, content}`.

### `obsidian_search(query: str) -> dict`

Runs Obsidian's live fuzzy full-text search index via `POST /search/simple/`.

- **Errors:** empty/whitespace `query`; configured plugin request failures.
- **Returns:** `{query, results}`.

### `obsidian_append(path: str, content: str, target_type: str = "heading", target: list[str] | str | None = None) -> dict`

Appends Markdown through the plugin's structured patch API. The default empty
heading target appends at the document root; pass a heading path, block ID, or
frontmatter key with the matching `target_type` when a narrower target is needed.

- **Locks:** a note marked `locked: true` is refused for every caller, and so is
  any `target_type="frontmatter"` patch whose target is the `locked` key (it would set
  or clear the lock itself). Nothing is sent to the plugin. To change a locked note, use
  `vault_write(force=True)` with human or steward approval. A note that does not exist
  yet is not treated as locked.
- **Concurrency (#1890):** the lock check, the PATCH and a re-check run under the
  per-note advisory lock (the sidecar lock file described under `vault_write`) that
  `vault_write` and `vault_journal` also take, so no hub writer can create, change or
  lock the note in between. The PATCH carries `ifMatch`: the plugin's content-hash
  version (first 6 hex characters of the sha256 of the note text as Obsidian's
  `vault.read` returns it, with one leading UTF-8 BOM dropped) of the bytes the hub
  checked. If the note changed before
  the plugin read it, the plugin answers 412 and the call fails with "note changed or
  locked since check"; nothing is written. If the lock is present after the write, the
  result carries `locked_after_write: true` and a `warning`, and the appended text may
  already be in the note (it is not undone).
- **Not fenced:** the plugin reads the note, patches that snapshot and writes it back
  with no version check at commit, and Obsidian does not take the hub's lock. An edit
  made in the Obsidian UI, by Obsidian Sync or by any other non-hub writer after the
  plugin's own read is overwritten by the write; a lock added that way is lost and
  cannot be detected. A note that does not exist yet is sent without `ifMatch`. The
  6-character version is a short hash, so a change that collides with it is not
  detected.
- **Unknown outcome:** a PATCH that was sent but got no answer (read timeout,
  dropped connection) may still be committed by the plugin. The call fails with
  "obsidian_append outcome unknown" and keeps the per-note lock for up to 5 seconds
  (`APPEND_SETTLE_S`) waiting for the note to change, so a hub writer cannot lock the
  note just before that late write lands. The error says whether the note changed. A
  plugin write later than the window can still overwrite a lock or edit made in
  between; re-read the note and its lock before retrying. The lock check and the
  `ifMatch` version are computed in bounded 1 MiB reads, so a large note is never
  held in memory whole.
- **Errors:** empty path/content; unsupported target type; path outside the
  configured vault write directories; locked note or `locked` frontmatter target;
  note changed since the check (plugin 412); a note the hub cannot read ("cannot
  read note"); outcome unknown after a timeout;
  configured plugin request failures.
- **Returns:** `{path, response}`, plus `locked_after_write` and `warning` only in the
  race case above.

## LangGraph execution

LangGraph is a core hub dependency (not an optional extra) and the following
functions are registered MCP tools. The runner still does not own identity,
claims, approvals, or task state — see
[LangGraph execution-plane ADR](architecture/langgraph-execution-plane-adr.md).
Checkpoints live in `${UA_HOME}/langgraph/checkpoints.sqlite3`, separate from
`memory.db`. `system_doctor` reports the engine as `LIVE` when the import
succeeds.

### `langgraph_run(task_id: int | None = None, query: str | None = None, auto_record: bool = True, run_id: str | None = None, resume: bool = False) -> dict`

Executes a governed LangGraph research run for a claimed task in `doing` status
and returns its run metadata, events, and output. `task_id` and `query` are
required to start a run.

**Resume (task #2522).** A run paused for its `langgraph_governed_call` approval
continues with `langgraph_run(run_id=<id>, resume=True)`. `task_id` and `query`
are optional; when given they must match the run. The run is rebuilt from its
latest checkpoint (`PilotRunner.load_run`) with its original claim, task
revision, profile, capabilities, budget revision and trace. Nothing is
refreshed: the budget revision is a digest of the hub's autonomy limits, so a
limit changed during the pause refuses the resume, and the transient-retry
count carries over. A checkpoint whose fields have the wrong types is
`checkpoint_corrupt`. Before anything is replayed or run, the run's task must be
visible in the caller's tenant and the caller must own the run (`claimed_by`)
or be the verified operator from the Control Center. The run then executes
under its owner's authority either way: the owner's profile must still be the
run's saved profile and inside the owner's current ceiling, and that profile,
never the operator's, decides whether the run's tools are exposed. The task,
claim, budget, policy gate and approval gate are checked again.

A lease on the run, kept in the checkpoint store (`langgraph_resume_leases`),
admits one resume at a time and is renewed by a heartbeat while the holder
executes. Every checkpoint write, including the one that records the
completion, checks the holder's lease token in its own transaction, so a holder
whose lease expired and was taken over is refused at its next write
(`run_conflict`, `lease_lost`) and records nothing. Repeating a finished resume
returns the stored outcome with `duplicate: true` and executes nothing.

- **Errors (ToolError):** invalid task/query, unclaimed or non-`doing` task on
  start, `run_id` without `resume=True`, `resume=True` without a well-formed
  `run_id`, a `task_id` or `query` that does not match the resumed run, or a
  checkpoint store that cannot be opened when starting.
- **Returns:** `run_id`, `thread_id`, `task_id`, `trace_id`, `budget_revision`,
  `status` (`completed`, `interrupted`, `denied`, `failed`, `conflict`),
  `outcome`, `code`, `reason`, `resumed`, `duplicate`, the events, and
  `output`. `outcome` is typed:

  | `outcome` | `code` | When |
  |---|---|---|
  | `run_completed` | — | the graph finished with a real output (never without one) |
  | `approval_interrupted` | — | waiting on the native approval; resume after deciding it |
  | `run_denied` | `task_not_accessible`, `not_run_owner`, `unauthenticated`, `stale_authority`, `approval_denied` | the task is outside the caller's tenant, the caller may not resume the run, authority failed at resume time, or the approval was denied |
  | `run_failed` | `checkpoint_missing`, `checkpoint_corrupt`, `checkpoint_incompatible` | the checkpoint is missing, cannot be decoded, or is from another graph revision |
  | `run_failed` | `gateway_failed`, `run_error`, `no_output`, `no_outcome` | the governed call exhausted its retries, the run raised, or it ended without an output |
  | `run_conflict` | `resume_in_flight`, `lease_lost` | another resume holds the run's lease, or this resume's lease expired and was taken over mid-run |

### `langgraph_progress_note(task_id: int, output_digest: str, run_id: str | None = None) -> dict`

Appends one generated progress receipt containing a run ID and lowercase SHA-256
output digest to the run's own claimed task. The task remains `doing`; arbitrary
note text and other task fields are not accepted. The digest is a caller-supplied
reference, not a claim that the graph verified the referenced artifact.

The first call returns `interrupted` and a canonical approval ID. After a human
decides that exact `langgraph_governed_call` request, call again with the same
task ID, digest and returned `run_id`. Original authority is retained, never
refreshed from resume input. Denial is terminal. Successful duplicate delivery
returns the bounded receipt without another append. Unknown outcomes reconcile
from the native receipt or stop for explicit recovery; writes are never retried
automatically. This tool requires an authenticated HTTP session and a profile
that exposes `task_update`.

- **Errors:** malformed inputs, missing checkpoint on resume, mismatched task/digest, stale authority, policy failure or quarantined uncertain outcome.
- **Returns:** graph/run metadata, `status`, bounded `output` receipt and `interrupted_by` approval payload when pending.

### `langgraph_workflow_run(task_id: int, query: str, run_id: str | None = None) -> dict`

Runs the governed plan→build→verify→merge chain for a claimed task in `doing`
status. Plan and verify are attributed local hops. Build and merge each append
one generated receipt (`claimed_by`, run ULID, digest) through wrapped
`task_update`. Merge is the second mutating node and always waits on
`langgraph_governed_call`. This tool does not call `git_merge_task` and does
not dispatch agents.

The first call returns `interrupted` at the build gate. After that approval,
repeat `task_id` and `query` with the returned `run_id` to reach the merge
gate. After the merge approval, a third call completes. Duplicate delivery
returns the bounded receipts without another append. The task remains `doing`.
Long runs report a handful of absolute MCP progress milestones (start, pending
hop, completed/denied) via `Context.report_progress` — not per-node spam.

Abandoned runs (task #1497): when a run's origin authority is gone (the origin
task is missing, no longer `doing`, owned by someone else, or past its lease),
its bound plan is closed `abandoned` through `plan_close` with reason
`origin authority expired: <cause> (run <ULID>)`, and the run ends `denied`,
never `completed`. This happens on a refused resume and on every queue hygiene
pass (`langgraph_workflow.close_expired_workflow_plans`, reported as
`workflow_plans`). A claimant still holding a live lease is never touched, and a
run whose checkpoint cannot be loaded is reported `unverifiable`, not closed.

- **Errors:** malformed inputs, missing checkpoint on resume, mismatched task/query, stale authority, policy failure or quarantined uncertain outcome.
- **Returns:** graph/run metadata, `status`, `pending_hop`, bounded `output` on completion, and `interrupted_by` when a gate is waiting.

### `langgraph_checkpoints(run_id: str | None = None, limit: int = 20) -> dict`

Inspects durable LangGraph checkpoint metadata from the local SQLite checkpoint database, optionally filtered to one run.

- **Returns:** checkpoint database path, existence flag, count, and records.

### `mcp_health_telemetry() -> dict`

Return runtime MCP 2.x health, protocol negotiation, capability set, and reconnect metrics (task #866).
Read-only diagnostic distinguishing healthy, degraded, and incompatible states. Sanitized (no secrets/tokens), bounded, and directly actionable.

Also returns ``schema_fingerprint`` (task #1169): a 16-hex digest of the live
tool-schema surface (per-tool name + required/optional arg keys). Clients capture
it at session start and re-check before writes; a mismatch means re-list or
reconnect. See [automation.md](automation.md#post-deploy-re-list-tools-schema-fingerprint-task-1169).

Task #1956 adds `semantic_schema_fingerprint_version` (`2`) and full SHA-256
`semantic_schema_fingerprint`, `manifest_semantic_schema_fingerprint` and
`registry_semantic_schema_fingerprint` hashes over advertised input schemas for
the callable, listed and complete registry surfaces. Types, enums, defaults and
nested constraints contribute; known presentation annotations and unordered
keyword-list order are normalized where reference targets/scopes are understood.
Opaque references conservatively retain the raw schema. Existing fingerprints
remain unchanged. See
[tool-schema-fingerprints.md](tool-schema-fingerprints.md) for canonicalization,
client reproduction and reference/extension limits.

## System doctor (`tools/doctor.py`)

### `system_doctor(refresh: bool = False) -> dict`

The report is shared (task #1850, stale-while-revalidate since #1897). A report
younger than `UA_DOCTOR_CACHE_S` (default 120 s) is served as is. One younger
than `UA_DOCTOR_MAX_STALE_S` (default 600 s) is served at once with
`stale: true` and `refreshing: true` while exactly one background run replaces
it. Only a missing report, one past the max-stale bound, or `refresh=True`
waits for a run: a cold start, a caller after more than the bound without any
caller (or with every background refresh failing that long), and an operator's
refresh. A run that fails then raises rather than serve an older report.
Unforced callers that arrive while a run is in progress share it; a forced
caller never shares a run that began before it asked, and forced callers
arriving meanwhile share one forced run queued behind it. A forced run clears
the LLM probe cache as it starts. A background run sees the triggering caller's
context, as a foreground run does. A caller waits at most 300 s on another
caller's run (`TimeoutError` after that), and a background run whose thread
cannot start is reported in `refresh_error` instead of left pending.
A reused report says so: `cached` is `true`, `cached_at` is when its run
finished and `cache_age_s` how long ago; a fresh one has `cached: false`.
`run_duration_s` is how long that run took. If the last background run failed,
`refresh_error` (`at`, `error`) sits beside the last good report until a run
succeeds. `refresh=True` runs every probe now, the worktree survey included:
it surveys once anew, and both the `stale_worktrees` row and the persisted host
census use that survey. Otherwise the worktree survey behind
`stale_worktrees` is shared separately for `UA_WORKTREE_SURVEY_CACHE_S`
(default 300 s). The `llm_provider_chain` row's metadata probes are reused separately for
`UA_LLM_PROBE_CACHE_S` seconds (default 60), failures included, and a reused
probe's detail ends `(cached Ns ago)`.

Read-only LIVE/DORMANT/DEGRADED/MISCONFIGURED/UNKNOWN report over every config-gated capability: hub
auth (`UA_AGENT_TOKENS`/`UA_ENFORCE_AUTH`), the global approval queue
(`UA_APPROVAL_TOOLS`), which web-search provider actually resolves
(`UA_SEARCH_PROVIDER`/`BRAVE_API_KEY`/`TAVILY_API_KEY`), per-agent hub workspace
bindings (`UA_AGENT_WORKSPACES`), whether `service.env` pins the hub sandbox root
(`workspace_pin`, below), the automation timers this repo installs (anomaly
check, sleep pass, review pipeline, steward audit —
`deploy/systemd/*`; weekly consolidate-proposal retired task #884), the hub service itself, installed-unit-vs-repo-template drift
(`unit_template_drift`: directives present in a `deploy/` unit template but missing
from the installed `~/.config/systemd/user` copy — `Environment=` entries compare by
variable name only, so local values like a real `UA_HTTP_ALLOWED_HOSTS` never
flag), the optional `pdf` extra, and required `tree-sitter-language-pack`.

Service probes share `monitor._service_probe`. Linux uses
`systemctl --user show`. Darwin uses `launchctl print gui/$UID/<label>` and maps
systemd-shaped names through `MAC_LAUNCHD_LABELS` in
[`monitor.py`](../src/unified_agent/monitor.py): hub → `com.unified-agent.hub`,
`unified-agent-executor.service` → `com.unified-agent.executor-agy`, Zulip worker
→ `com.unified-agent.zulip-worker` (retired; the plist is archived under
`deploy/mac-hub/retired/` and setup no longer installs or bootstraps it),
and the automation timers in
`deploy/mac-hub/com.unified-agent.*.plist` (review pipeline, anomaly check,
sleep pass, steward audit, cluster briefing, dispatch
tick). Unmapped units (gbrain, `executor-codex`, Linux-only timers such as
cluster-health-check) report `not-found` on Darwin rather than a probe failure.
`active_executor_service` therefore reflects the Agy LaunchAgent on a Mac hub,
not Codex. The Control Center worktree census uses
`config.process_workspace_root()`, so a `UA_AGENT_WORKSPACES` DENY login
(`Desktop_Person`) can still list hub worktrees.

The tool exists because capabilities have repeatedly shipped as code and then sat
inert for days — activation is a config/env step, not a code change (task #108 found
5 of 7 features in one batch dormant in production). Each finding carries the exact
env var or unit that gates it, so a `DORMANT` result is directly actionable.
It also reports `database_backup`: the latest integrity-checked complete SQLite
snapshot in `UA_BACKUP_DIR` (default `~/.unified_agent/backups/`) and its age.
No snapshot is `DORMANT`; a corrupt or more-than-26-hour-old snapshot is
`MISCONFIGURED`.

And `semantic_search`, which answers whether semantic memory search actually
works on this host — the one capability that can fail completely while every
tool call still returns success, because `memory_save` stores a note without an
embedding and `memory_search` falls back to FTS. It combines embedding failures
observed in the current process with the count of notes holding no embedding
row (plain SQL, so it needs no model and survives a restart):

| Status | Meaning |
|---|---|
| `DORMANT` | No memory notes to embed yet |
| `MISCONFIGURED` | Embedding is failing now, notes are missing embeddings, or the `memory_vec` search index has fallen behind — reported with the underlying error |
| `UNKNOWN` | Everything on record is embedded, but nothing has embedded successfully in this process, so readiness is unverified |
| `LIVE` | Everything embedded and the model has demonstrably worked since start |

Two distinctions carry this check. **"Failing now" means a failure with no
success after it**, not merely a non-zero failure count: a host that embedded
its notes and then lost the model must not read `LIVE` just because coverage is
complete — embedding a *search query* fails without leaving an un-embedded note
behind — while a host that failed once and recovered must not stay
`MISCONFIGURED` until it restarts. **`UNKNOWN` exists because a complete corpus
only proves the model worked once, on some earlier process.** A hub restarted
after the model went away has a fully-embedded corpus and no recorded failures,
and would otherwise report a false all-clear. It resolves on the first real save
or search rather than by probing, since probing would make a read-only status
call download a model on a cold host.

A third signal covers the search index itself. A complete `memory_embeddings`
table does not prove `memory_search` works: with sqlite-vec loaded,
`_semantic_similarity_rows` reads the `memory_vec` mirror and returns nothing
at all when it is empty — the per-row UDF fallback fires only on
`OperationalError`, which a present-but-empty table does not raise. Because
`upsert_memory_vec` swallows its failures by design, the mirror can fall behind
silently while the source of truth stays complete, so coverage counts both and
reports a mirror gap as `MISCONFIGURED`.

`tenant_consistency` (task #1996) compares the hub unit's tenant
(`UA_EDITION`, `WORKOS_ORGANIZATION_ID`) with the per-process beacons in
`$UA_HOME/tenant-beacons/` from the last 24 hours, and is `DEGRADED` when any
process that opened the store stamped a different tenant. Statuses and the fix
are in [Deployment modes](deployment-modes.md#6-one-tenant-environment-per-data-directory).

Three rows keep a broken job, a removed default or a moved checkout from
reading as healthy. None of them runs `launchctl` or `git`; they read files.

`pr_watch_job` (task #1783) reads `$UA_HOME/pr-watch-status.json`, which
pr-watch writes after every scheduled run (dry runs write nothing):

| Status | Meaning |
|---|---|
| `DORMANT` | No run recorded, the pr-watch LaunchAgent is not installed (no `~/Library/LaunchAgents/com.unified-agent.pr-watch.plist`), and the Approval Ledger connector is off |
| `UNKNOWN` | No run recorded but the job is installed (its LaunchAgent plist exists; the journal runs even with the connector off), no run recorded but the connector is on, or the record is unreadable |
| `DEGRADED` | The last run exited 1 (the detail carries its first error line and the last success), the last success is older than the 30-minute interval plus 15 minutes of grace, or `run-pr-watch.sh` recorded a launch failure in `$UA_HOME/pr-watch-launch-failure.json` that is not older than the last recorded run, or with no run recorded (the launcher may stamp whole seconds, so a tie within a second counts as the failure). Also when the journal pass or the connector poll failed the last time it ran, or while a repository the journal skipped has not been read again (the detail gives the count and the first two) |
| `LIVE` | The last run succeeded within that window, both passes succeeded the last time they ran, and no skip is unresolved |

The record keeps a `journal` and a `connector` section beside the whole-run
fields (task #2486), because a tick may run either pass alone. Each section
holds that pass's last run, exit, error and success, and a pass that did not
run keeps its section. `journal.unresolved` maps each skipped repository (or
`repo discovery`) to its reason until a later journal pass reads it;
`skipped_total` is the count and `last_skipped` the first five lines. A record
written before the sections existed still reads.

`github_approval_backlog` uses the same record, judged on the `connector`
section alone when there is one. Zero stale passes is `LIVE`
only while the poller is healthy; a failing or stalled poller makes it
`DEGRADED`, and no recorded run makes it `UNKNOWN`, because a poller that never
runs also never leaves a pass unconsumed. It counts the rows `approval_pending`
lists for `github_app.process_pr_event` (this tenant, `status='approved'`): a
live pass granted within one poll interval is fresh and named in the detail
("N fresh unconsumed GitHub pass(es) (none stale)") without changing the
status, while an older live pass or one past its TTL (`expired_passes`) is
stale and makes the row `DEGRADED` (task #1804).

`required_settings` (task #1783) checks each feature in
`src/unified_agent/required_settings.py` against the hub service environment
layered over the caller's (which has `settings.toml` applied). It is
`MISCONFIGURED` when an enabled feature's setting is unset or invalid,
`DORMANT` when no listed feature is enabled, and `LIVE` otherwise. Current
entries:

| Feature | Enabled when | Requires |
|---|---|---|
| Approval Ledger connector | `UA_GITHUB_APP_ENABLED=1` | `UA_GITHUB_CONNECTOR_REPO` (`github.connector_repo`), an owner/repository |
| pr-watch PR journal | the pr-watch LaunchAgent plist is installed or a pr-watch run is recorded, and pr-watch is not in broker-only mode | `UA_VAULT_PATH` (`paths.vault`), an existing directory |

Add an entry whenever a code default is removed that an installation may have
relied on.

`workspace_pin` (#1806, #1825) reads only the `UA_WORKSPACE` key of
`$UA_HOME/service.env` and `paths.workspace` from `settings.toml`. After
`unified-agent init` the uncommented `service.env` line is authoritative and
`settings.toml` cannot move the sandbox root:

| Status | Meaning |
|---|---|
| `LIVE` | `service.env` pins `UA_WORKSPACE`, and `settings.toml` names the same path or no path |
| `DEGRADED` | `settings.toml` names a different path: it is ignored, but it misleads anyone reading it |
| `MISCONFIGURED` | `service.env` exists but has no uncommented, non-empty `UA_WORKSPACE` (run `unified-agent init` to pin the effective value), or its pin references an unset variable or a command substitution, which `init` refuses by the same rule (`service_env.resolve_workspace_pin`) |
| `DORMANT` | No `service.env` (a dev checkout, or before `init`) |

`hub_checkout_drift` (task #1771) compares `$UA_HOME/hub-start.json`, which
the HTTP hub writes at startup (checkout path, HEAD SHA, pid, start time), with
the checkout's current HEAD. `DEGRADED` means the checkout moved without a
deploy or restart, so lazily imported modules would load newer code than the
rest of the process. `DORMANT` means no start record exists on this host;
`UNKNOWN` covers a recorded hub that is no longer running, a hub not started
from a git checkout, and an unreadable HEAD.

`wip_policy` (task #2759) reports the WIP numbers the running hub will actually
enforce: the effective per-agent cap, `UA_AGENT_WIP_OVERRIDES`, lane caps from
`UA_LANE_WIP`, and both fleet caps. It is `DEGRADED` when the fleet ceiling does
not sit above the per-agent cap, because the fleet warning then fires at the
same claim as the per-agent refusal and carries no information — the drift that
let production run 20 while the docs said 2.

`package_checkout` (task #1944) is the other "this process is not running the
code you think it is" row. It reads files only: this process's
`unified_agent` import path, and the main checkout's shared `.venv` editable
pointers (`*unified_agent*.pth` path lines and setuptools
`__editable__*unified_agent*finder.py` `MAPPING` values, resolved so a
symlink cannot hide the target). Per-task worktrees are
`<repo>/.worktrees/<name>` and `<repo>/.claude/worktrees/<name>`.

| Status | Meaning |
|---|---|
| `LIVE` | The import path is the main checkout, or a worktree that is running *its own* venv. A non-editable install has no pointer and is not judged |
| `DEGRADED` | This process imports `unified_agent` from a per-task worktree while its interpreter lives outside that worktree, or a pointer in the main checkout's `.venv` resolves outside that checkout's `src` (a task worktree, an old clone, a renamed tree). The detail names the pointer and where it should point |
| `UNKNOWN` | The package-path probe failed |

A worktree that ran `uv sync` in itself is fine. Never `uv pip install -e` a
worktree into the main checkout's `.venv`: services such as ua-node import
through that venv, so a pointer left at `.worktrees/task-*` keeps them on that
stale tree after main moves on. To repair: `uv sync` in the main checkout and
restart the services that import it. See [COLLABORATION.md](COLLABORATION.md)
and [troubleshooting](troubleshooting.md#doctor-reports-package_checkout-degraded).

`agent_token_collisions` (task #1657) names the labels whose bearer the hub
refuses as a collision. `config.agent_tokens` merges `UA_AGENT_TOKENS` with
the managed `agent_tokens` store and fails closed when one token maps to two
labels: that token authenticates no one. A client resolves its bearer from
its own environment, so a cross-source collision looks configured on the
client and is refused by the hub. Labels and sources only, never a token
value.

| Status | Meaning |
|---|---|
| `LIVE` | No two agent labels share a bearer token |
| `MISCONFIGURED` | Named labels share one token, within `UA_AGENT_TOKENS`, within the managed store, or across both. Remove or rotate the duplicate registration |
| `UNKNOWN` | The hub's auth configuration cannot be read (unit not installed or unreadable), or reading the tokens raised |

`agent_wake` (tasks #1873, #2050) reports idle-session wakes. A session
requests wakes only at launch: the `hub-inbox` hook of a session started with
`UA_WAKE=1` registers with `wake: true`, and no tool can set the flag. A request
wakes nothing until the human operator grants that session
(`POST /api/sessions/{id}/wake-grant`, operator only). The row counts requesting
open sessions, how many are granted, how many were woken at least once and how
many the circuit breaker paused, and names up to five sessions awaiting a grant.
Ids and counts only, never a session's mail.

| Status | Meaning |
|---|---|
| `LIVE` | A granted session has been woken at least once, and none is paused |
| `DEGRADED` | Requesting sessions are granted, but none has been woken yet. TUIOS panes can be woken; inspected Herdr 0.9.3/0.9.4 cannot atomically attest empty input, so Herdr panes skip with `input_state_unavailable` |
| `DEGRADED` | A granted session has 3 unanswered admitted attempts, including uncertain submissions, so further wake admission pauses until a real inbox read after the latest attempt. A genuinely new hub session starts fresh; routine registration does not reset the breaker |
| `DORMANT` | No open session is both requesting and granted; the detail lists any awaiting a grant |
| `UNKNOWN` | The wake state could not be read (for example, on a host with no local store) |

`fleet_nodes` (tasks #1393, #1835) starts from the hosts the operator
declares rather than from whichever nodes happen to be online, so a host whose
ua-node stopped cannot drop out of view. `UA_FLEET_NODES` (settings
`fleet.nodes`) lists the node names that must be live; `UA_FLEET_OPTIONAL_NODES`
(`fleet.optional_nodes`) lists names that may be absent by decision, such as a
machine that is often switched off. A name in both lists is required. The row
carries a `hosts` list with each declared node's `required` flag, `state`
(`live`, `stale` or `unregistered`; `stale` covers a missing, unreadable or
future-dated heartbeat) and last heartbeat. Liveness uses the same
window as the other fleet rows, except that a heartbeat stamped more than 60
seconds in the future is not live. A stale optional node's detail includes how
long ago it last reported.

| Status | Meaning |
|---|---|
| `LIVE` | Every required node has a heartbeat inside the window. Optional nodes are named either way: live, or "unreported, optional by operator decision" |
| `DEGRADED` | A required node is not live; the detail names it with its last heartbeat age, or "never registered" |
| `MISCONFIGURED` | A required name is not a valid node name, so no node could ever register under it. Liveness of the other hosts is still listed. An invalid optional name is named in the detail and, like any optional node, does not set the status |
| `UNKNOWN` | Reading the node registry raised an unexpected error. A store read failure returns no nodes, so it shows as every required node "never registered" (`DEGRADED`) |
| `DORMANT` | No required node is declared; the detail lists the nodes live now |

An optional node never changes the status, in either direction. To make one
required, install and register its ua-node, confirm it reads `live
(optional)`, then move its name from `UA_FLEET_OPTIONAL_NODES` to
`UA_FLEET_NODES`. Live nodes in neither list appear as "also live,
undeclared".

`fleet_node_versions` (task #1946) compares the commit each live ua-node runs
with the hub's. A node reads the HEAD of the checkout it was imported from once
per process, without running `git` (a worktree's `.git` file is followed), and
reports it as `code_sha` at registration and on heartbeats. The hub's commit is
the SHA in its start record while that process runs, else its checkout HEAD.
Liveness uses the same window as `fleet_vault_sync`; offline nodes are skipped.

| Status | Meaning |
|---|---|
| `LIVE` | Every live node reports the hub's commit |
| `DEGRADED` | At least one live node runs a different commit; the detail names each with its short SHA |
| `UNKNOWN` | A live node reports no SHA (an older ua-node, or not a git checkout), or the hub's own SHA cannot be read |
| `DORMANT` | No live ua-node |

`fleet_node_acceptance` (task #2112) reports which registered ua-nodes passed
[device acceptance](fleet-console.md#device-acceptance-task-2112) for the
agent they are bound to. A node silent past the registry's offline cutoff
(a day by default) is named under "not counted" and does not affect the status,
so a retired or renamed device's leftover row cannot hold it down.

| Status | Meaning |
|---|---|
| `LIVE` | Every registered node is accepted |
| `DEGRADED` | A node's latest run failed. With `UA_NODE_REQUIRE_ACCEPTANCE=1`, also any node that is not accepted, because that node takes no real work |
| `DORMANT` | No registered node, or (with the gate off) nodes whose run is pending or was never started |
| `UNKNOWN` | The node registry or the acceptance table could not be read |

The check never reports `MISCONFIGURED`: a node may lag a deploy briefly.

`node_turn_index` (tasks #2073, #2057) reports the optional
`idx_event_log_node_item_turn` expression index that speeds up ACP turn
summaries. The index needs SQLite 3.42 or later with JSON5 support, because the
event ledger keeps `NaN`/`Infinity` spellings that older engines cannot parse.
The detail names the SQLite version linked to the hub's connection
(`storage.sqlite_runtime_version`), not the Python package version. On an older
engine `storage.init_db()` drops the index instead of building it, and event
writes and ledger hashes are not affected. See
[development.md](development.md#sqlite-turn-count-acceleration).

| Status | Meaning |
|---|---|
| `LIVE` | SQLite 3.42 or later with JSON5, and the index is present |
| `DEGRADED` | A supported engine is missing the index (restart the hub to rebuild it), or an unsupported engine (older than 3.42, or lacking JSON5) still has it, for example after a downgrade (restart the hub to drop it) |
| `DORMANT` | The engine is older than 3.42 or lacks JSON5, and the index is correctly absent. Turn summaries scan node events without it, and stay best-effort for turns whose events carry `NaN`/`Infinity`; upgrade SQLite to enable the index |
| `UNKNOWN` | The SQLite probe raised |

- **Errors:** none — an individual probe failure (systemctl or launchctl missing,
  no systemd `--user` session, an extra not installed) surfaces as
  `DORMANT`/`MISCONFIGURED` on that one check rather than raising.
- **Returns:** `{checks: [{capability, status, detail, gate}], summary: {live,
  dormant, misconfigured}}`.

```json
{}
```

## Complete registered-tool index

The entries below close the reference coverage gap for tools whose detailed
sections are maintained in their implementation modules. Their headings are
kept under the executable coverage test so new registrations cannot be omitted.

### `agent_activity(...)`

Queries the `agent_events` activity index, newest first. Optional filters `agent` (exact label), `since` (ISO-8601 lower bound), and `kind` (`task`, `memory`, `journal`) are AND-combined. `limit` defaults to 200 and is capped at 1000. Returns `{events, truncated}`.

### `agent_heartbeat(...)`

Records that an agent is alive now. A hub request or any `record_agent_event` call already produces a heartbeat; this exists for bursty sessions and SessionStart/Stop hooks. Over HTTP the verified bearer identity wins over a caller-supplied `agent`. Optional `host` records that machine's last-seen, and optional `doctor` stores a `system_doctor` snapshot for it.

### `agent_list(...)`

Lists fleet agents with liveness derived at read time (`online`/`idle`/`stale`/`offline`, or `unknown` for a label never seen; never stored). A row needs a roster label (`UA_AGENT_LABELS` when set, otherwise a `FLEET_AGENT_LABELS` catalog identity) or a registered ua-node; attribution strings stay out of `agents` and `counts`. `include_configured=False` hides roster labels that have never been seen.

### `approval_pending(tool: str | None = None, limit: int = 100, newest_expired: bool = False) -> dict`

Lists queued approval requests oldest-first. Optional `tool` filter. `limit`
is clamped to `[1, 500]`. The queue is populated only when `UA_APPROVAL_TOOLS`
gates tools.

Unconsumed approved passes are split (task #596):

- `approved_unconsumed` — live one-shot passes (`expired: False`, with
  `expires_at` / `expires_in_hours`). These can still authorize a retry.
- `expired_passes` — past `UA_APPROVAL_PASS_TTL_HOURS`, so inert. Still
  listed for audit, with bulky payload fields elided (delegation `task_spec`
  was dominating the queue). Listing does **not** retire a pass to
  `expired`; `check_approval_gate` does that lazily when a matching call
  arrives, so a human can still revoke until then.

Classification runs **before** `limit` (task #1449 / #803). The approved
bucket is read whole, split into live vs expired, and only then is each
list sliced to `limit`. A SQL `LIMIT` would fill the window with the oldest
(often expired) rows and hide live passes. Pending rows still use SQL
`LIMIT`. Scan cost is a timestamp parse per approved row; bulky payloads
are elided only after the cap, on expired rows that survive.

`newest_expired` (default `False`) selects which expired passes survive the
`limit` slice. By default the oldest `limit` are kept; with `True` the newest
`limit` are kept instead (still listed oldest-first) and the result gains
`expired_passes_total`, the uncapped count of expired passes. The Control
Center uses it to show the most recent inert passes alongside the full count
(task #1786). It does not affect `pending` or `approved_unconsumed`.

Returns `{pending, approved_unconsumed, expired_passes, gated_tools,
pass_ttl_hours}`, plus `expired_passes_total` when `newest_expired=True`.
Control Center `GET /api/approvals` mirrors the same split.

Each row includes `run_id` (task #1653): the verified caller's current run
when that run is unambiguous, otherwise null. A `doing` task claimed by
that identity contributes its `current_run_id` (the open self-pickup run).
When the caller has no such run, a single live dispatch run for the same
identity in the same tenant is used. Two or more candidates, a cached value
that is not a bounded run id, an unverified caller, or another tenant's run
stores null. A caller-supplied `requested_by` does not select the run.
`approval_resolve` does not read `run_id`. It is display linkage for Inbox
and Runs, not an authorization input. `GET /api/approvals` and
`GET /api/approvals/{id}` return the same field. The request event
(`agent.approval.requested`) carries it next to the bounded `tool`,
`requested_by` and `summary` fields.

### `approval_resolve(...)`

Approves, denies, or revokes one approval row. Approve and deny act on a `pending` row: approve stores a one-shot pass for the next identical gated call, deny records `reason` and makes that exact retry fail. Revoke withdraws an `approved` row that was never consumed; a later identical request re-queues. Approve requires a named human approver who is a different person from the requester (`Desktop_Person` and `Operator` count as one person), for every tool; with auth enforced the approver is the verified identity, not the `resolved_by` argument. Deny and revoke stay open to the requester. Two exceptions: `task_delete` and `langgraph_governed_call` mint and consume an operator self-approval pass when the verified caller is the human operator (task #2500; see [human-in-the-loop.md](human-in-the-loop.md#the-approval-gate)). A one-shot pass is spendable only by the requester that queued it or the human who resolved it (task #2492); another caller making the identical call cannot spend it.

A verified resolver leaves a signed producer stamp (task #1672). For a high-tier tool (`approvals.HIGH_TIER_EVIDENCE_TOOLS`, the separation-of-duties set: `work_product_release`, `acp_permission`, `harness_config_apply`, `config_revision_rollback`, `config_revision_commit`, `org_proposal_apply`), the stamp is written in the same transaction as the status change, so a resolver that loses a concurrent race leaves no stamp. Approve fails closed when the stamp raises or returns nothing: the row stays `pending` and the error names the gap. Every other resolution commits. A missing stamp then appends `audit.evidence.gap` (with `tier` and `outcome`), and the gap appears on `/api/attention` as an `evidence_gap` item for seven days. The digest lists it under `waiting_on_operator`; the Watch projection shows it as a generic `anomaly`.

### `audit_triage_tasks(...)`

Runs `security_audit` and files one triage task per finding group (by rule, falling back to file) at or above `min_severity` (default `high`). A clean audit files nothing. Within a group, findings are split by class: the first segment of the finding's path under the audited root (`sessions`, `auth.json`, `src`), or `(repo)` when it has no path. Each (group, class) is deduped per audited root by an `[audit-triage: <basename>@<sha8>:<group>:<class>]` marker (`sha8` = first 8 hex digits of the SHA-256 of the resolved root) against tasks that are still open, or closed with `[accepted-risk]` in their notes; a group whose task was `done`, `cancelled` or archived without that tag and then reappears is filed again. Tasks filed before task #2157 carry a classless `<basename>@<sha8>:<group>` marker and cover the classes of the finding lines written under their `Auto-filed from ...` header, at most as many as the title's `N finding(s)` count, so a line appended later does not widen them (a task without that header, written by hand, covers every class). An open legacy `[audit-triage: <group>]` task still dedupes, but only for the root its notes name, with the same class rule. `dry_run=True` reports without writing.

### `autonomous_cycle_green_path_eligibility(...)`

Returns the green-path criteria verdict for a cycle as `{eligible, reasons}`: state `pr` or `completed`, a `pr_url`, no `stop_reason`, no red-tier file, and passing validation gates at a pinned head. The function itself does not merge; `autonomous_cycle_transition` uses an eligible verdict to skip the approval gate for a `merge` transition only.

### `autonomous_cycle_kill(...)`

Forces a cycle to terminal `killed` from any non-terminal state. It is never approval-gated. It does not signal a running subprocess: the daemon refuses further dispatch for that cycle, and an in-flight child ends on its own capped timeout.

### `batch_notify(...)`

Queues notification strings into a per-category digest buffer. `flush_now=True`, or reaching `max_batch_size` (default 5) pending messages in the category, combines them into one iMessage. Delivery depends on the local bridge.

### `check_agent_capability(...)`

Compares a candidate agent's tool allowlist with `[requires: ...]` annotations in a task's notes. A task with no such annotation is reported capable. It reports a gap and grants nothing.

### `capability_catalog(task_id: int | None = None, agent_label: str | None = None) -> dict`

Read-only inventory of agents, CLIs, product skills, MCP tools, versions, health,
and sandbox/approval boundaries (task #510). Pass both `task_id` and
`agent_label` to report missing tools and missing config gates before dispatch.
Never installs packages, never writes, and never opens an approval pass.

- **Returns:** `{read_only, install_is_explicit, approval_gated, versions, agents, mcp_tools, mcp_client_matrix, clis, skills, workflows, health, boundaries, prerequisites}`.
- **Reference:** See [MCP 2.x Client Capability Matrix](mcp-client-capability-matrix.md) for full matrix specifications.

### `check_anomalies(...)`

Runs the deterministic anomaly rules (`monitor.py`) over tasks, `agent_events`, the approval queue, the database size, and host probes: for example stale doing-tasks, silent agents, review wait, reassignment thrash, approval age, bridge and service health, low memory, and unverified `[code-task]` completions. No model call. Returns `{anomalies}`.

### `check_anomalies_and_notify(...)`

Runs `check_anomalies` and sends newly triggered anomalies through `notify`. `idle_lane` anomalies are never sent. A `(rule, subject)` pair already attempted within `cooldown_hours` (default 2; a positive `UA_ANOMALY_NOTIFY_COOLDOWN_HOURS` overrides the argument) is not re-sent, even if that send failed; each re-page of a still-active pair doubles its window (2 h, 4 h, 8 h, ... capped at 24 h, tracked in `anomaly_notifications.notify_count`); a cleared anomaly that recurs notifies immediately. An exception from `notify` is caught into `notify_error`, never raised; an undelivered send without an exception returns `notified: false` and `notify_error: None`.

### `cleanup_stale_worktrees(...)`

Scans the workspace's `.worktrees/`. A checkout is stale when its task is `done` or `blocked`, when its task id no longer exists, or when it has been idle longer than `max_idle_days` (default 7) whatever the task status. Idleness is the newest mtime of the checkout root and of its gitdir's `index`, `HEAD`, `logs/HEAD`, `FETCH_HEAD`, `ORIG_HEAD` and `MERGE_HEAD`, so staging, committing, checking out, fetching and merging count as activity (a root that vanished mid-scan reads as active); these are read before any git command runs, and the survey's `git status` runs with `--no-optional-locks` so it never refreshes the index itself. A deploy checkout, any name starting `release-` or `rollback-`, is never stale. The prefix match deliberately over-matches (`release-notes` is kept too): deploy names have no fixed grammar, and keeping an ordinary worktree is the safe side. A checkout a running process is seen to use is never idle: its cwd or executable is inside it, a command-line path is (absolute, relative to the process's cwd, or a bare argv[0] resolved against the process's `PATH`), or its `PYTHONPATH` or `VIRTUAL_ENV` points into it. Linux reads `/proc`; elsewhere `lsof -d cwd,txt` gives each process's cwd and mapped program files and `ps -axwwE` adds command-line paths and the environment. The environment is visible only for the caller's own processes (normally where the hub, node and executor run), and code imported any other way is not detected, so the deploy-checkout name guard is the backstop. On Linux a process owned by another user, or one of the caller's holding extra credentials (setgid, capabilities), cannot be read and is skipped apart from its command line; a refused read on a process with the caller's own credentials makes liveness unknown, and the reason names the first three such processes as `pid <n> (<comm>)`. A checkout whose task is done, blocked or gone is still listed as stale while in use, with `in_use: true`, and is retained as `in_use`, never removed. Liveness is read again just before each forced removal, and a checkout that came into use meanwhile is retained as `in_use`. If running processes cannot be listed at all, nothing is removed: the result carries `liveness_unknown: true` and a `note` naming the cause, candidates are retained as `liveness_unknown`, and the `system_doctor` `stale_worktrees` row reads DEGRADED. A pre-removal re-read that cannot list processes retains that candidate as `liveness_unknown` and sets the same `liveness_unknown: true` and `note`, naming the first failed scan's cause (task #2196). Command-line detection looks past wrappers (`env X=1`, `env -S`, `sudo`, `doas`, `nice`, `nohup`, `setsid`, `timeout`, `xargs`) after `find -exec`, and inside `sh|bash|zsh|dash|ksh|mksh|fish -c` strings, past shell keywords and in backticks (task #2192). Those strings are parsed with shlex, or split whole on operators and whitespace when too long or unparseable, and bare words in them are also looked up on PATH. This detection is best effort and bounded: the PATH lookup considers up to 2,048 distinct bare words, wrapper unwrapping stops after 8 hops and nested command strings after 2 levels, and no shell expansion is performed, so a variable-held program name (`t=tool; "$t"`) or other unsupported syntax can hide a process's use of a checkout. Keep the deploy-checkout name guard and inspect a `dry_run` before a cleanup that forces removal. A bare program name is resolved against the hub's own PATH when the process's PATH cannot be read. On Linux a same-user process with other groups, another group id or extra capabilities is still skipped rather than making liveness unknown: systemd's `(sd-pam)` helper is one on every user session. A partial `lsof` result is not treated as unknown on macOS: `lsof` does not say which processes it skipped, and treating every skipped cwd as unknown would leave cleanup removing nothing. `dry_run` defaults to false. When `dry_run=False`, a stale checkout that still holds uncommitted or unmerged work is listed under `retained`, not deleted; a dry run leaves `retained` empty.

PATH lookups (a process's own `PATH`, and every `PATH=` a wrapper or `-c` string assigns) search at most 64 directories (task #2209). Directories past that are still kept as live paths, so a late `PATH=<worktree>/bin` continues to mark the checkout in use. A relative `PATH`, `PYTHONPATH` or `VIRTUAL_ENV` entry is resolved against that process's cwd and skipped when the cwd is unknown. No path a process supplies is collapsed lexically: `<link>/../run.py` is looked up as the kernel resolves it, and its raw, normalized and resolved forms all count as live. Resolution walks one component at a time, following at most 40 links, and a component longer than 1,024 UTF-8 bytes is treated as unopenable. A path that cannot be resolved within those limits (a symlink loop), or whose lookup fails for a reason that does not prove it unused (ENAMETOOLONG from a long cwd, or a directory the hub may not search on the path of a process with the hub's own credentials), makes liveness unknown and names the pid, so cleanup removes nothing rather than guess.

Source for the liveness claims above (pinned to `fefd6847`; follow the function names if lines drift): [`_live_process_paths`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1185) picks the scanner; [`_scan_proc`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L938) and [`_scan_proc_entry`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1001) read Linux `/proc`; [`_lsof_process_paths`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1156) and [`_ps_process_paths`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1055) are the macOS paths; [`_unwrap`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L245), [`_shell_commands`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L339), [`_command_string_paths`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L772) and [`_argv_paths`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L817) do the wrapper and `-c` string detection; [`_path_dirs`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L393) bounds the `PATH` lookups; [`_in_use`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1231) decides `in_use`/`liveness_unknown`; [`cleanup_stale_worktrees`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/tools/worktree_policy.py#L1493) re-reads liveness before each removal.

### `delegate(...)`

Creates a child task addressed to `to_agent` (`[delegated-to: X]`), an attributed memory note, and a `delegations` row. With `UA_AGENT_LABELS` set, unknown labels are refused; unset is open-roster mode, which still refuses `Desktop_`/`Laptop_`/`server_` names missing from the fleet catalog. The delegation spec is checked first, then the approval gate: `delegate` is in the default approval baseline unless `UA_APPROVAL_BASELINE=0` or `UA_LEGACY_OPEN_POSTURE=1`, and the gate is skipped only for the verified human web-login identity. Callback is `notify`, `memory`, or `none`. Delegation grants the receiver no extra tools.

`idempotency_key` (task #1938) is an optional retry token of 1-128 letters, digits, `.`, `_`, `:` or `-`, scoped to the delegator. The hub holds the key while it creates the delegation; a concurrent request with the same key is a `conflict`. The `delegations` row and the key's receipt commit together, so a retry after a dropped response returns the committed result and never inserts a second row, and needs no second approval. The same key with a different request is a `conflict`. A request parked by the approval gate records its `approval_id`; the identical retry after approval consumes the one-shot pass. A refusal before commit releases the key. A holder that died mid-request is taken over after 15 minutes; the child task and memory note use keys derived from the receipt, so a takeover reuses them. Receipts use the shared tenant-scoped idempotency store: after a hub gains tenant scoping, a receipt written before it is refused with a request for a new key, never missed and repeated.

### `delegation_manage(...)`

Consolidated delegation entry point for authenticated HTTP profiles. Use
`action` values `create`, `list`, `claim`, `release`, `cancel`, `get`, `lookup`,
`ledger`, `spec_check`, or `spec_template`, with action-specific keyword
arguments inside `params`. Underlying authorization and policy gates remain
active; legacy delegation tools remain available to stdio clients during
migration.

`lookup` (task #1938) takes `idempotency_key` and reads the caller's own receipt
for a keyed `create` with no effect. It returns `state`: `committed` with the
original `result`, `approval_required` with `approval_id`, `in_progress` with
`since` and `stale`, or `not_found` (never received, or refused before commit;
the identical retry with the same key is then safe). Another identity's key
reads as `not_found`. A profile that cannot call `create` cannot call `lookup`.

The lifecycle actions (`claim`, `release`, `cancel`, `fail`, `complete`,
`retry`) take an `agent` that must be the verified caller (or an allowlisted
alias), the same rule as `task_claim`; naming another agent raises `ToolError`.
`fail` and `complete` act only for the delegator or assignee, and `retry` only
for the assignee.

### `delegate_claim(...)`

Atomically moves one `pending` delegation to `in_flight` with a single conditional update, so two pollers cannot both win. Returns `{claimed, delegation}`; `claimed` is false when the row was already taken.

### `delegate_list(...)`

Lists delegations by status (`pending`, `in_flight`, `completed`, `cancelled`, `failed`, or `all`; default `pending`), optionally filtered by `to_agent` or `task_id`. Pending rows past `deadline` carry `overdue: true`; nothing auto-expires.

### `delegate_release(...)`

Returns an `in_flight` delegation to `pending` for crash recovery. It is not a retry-on-failure path: releasing on every failure would re-run a failing delegation at poll cadence.

### `delegation_ledger(...)`

Aggregates per-agent delegation counts and review-outcome annotations (`merged`, `rework`, `denied`, `duplicate`). `agent` is optional; `limit` defaults to 100 (max 500). Returns `{agent, total_delegations_analyzed, agent_stats, failure_pairs, outcomes_awaiting_review, summary_markdown}`.

### `dispatch_report_self(state: str, *, run_id: str, evidence: str | None = None, failure_reason: str | None = None) -> dict`

Hub-mediated completion report for an authenticated dispatched agent (task
#623). Same `record_run_outcome` contract as
`unified-agent-dispatch-scheduler report`.

- `state` must be `finished`, `failed`, or `blocked`.
- `run_id` is required. Hub process env is not the child's; pass the value
  from the dispatch prompt or `$UA_HOME/dispatch-runs/<run_id>.ua-dispatch.json`.
- `finished` requires non-empty `evidence` (commit SHA, PR URL, or
  child-authored log path).
- The verified bearer identity must match the run's `agent_label`.
  Anonymous callers are refused.

Use this from a fail-closed Claude sandbox. The CLI report path needs
`~/.unified_agent/memory.db`, which that sandbox denies. See
[Approved-dispatch enablement](architecture/approved-dispatch-enablement.md#completion-reporting).

### `dispatch_delivery_receipt(*, run_id: str) -> DeliveryReceipt`

Returns the read-only accountability receipt for one dispatch run. The receipt
correlates task, run, delegation, executor, lease, retry, event, policy,
approval, evidence, and final task-state fields. It includes the existing
separate-process executor-auth proof when requested from a standalone process;
hub-local calls report an explicit unknown/skipped proof rather than treating a
same-process self-call as authoritative. Unknown run IDs are rejected.

### `dispatch_delivery_receipts_list(*, task_id: int | None = None, state: str | None = None, limit: int = 20) -> dict`

Read-only operator surface for Runs → Receipts (`/ui/runs/receipts`, task #1182): lists
recent dispatch runs as delivery-receipt summaries, a projection of the
existing `dispatch_runs` ledger — never a second one. Each summary row
(run_id, task_id, agent_label, state, evidence presence, termination outcome,
timestamps) is enough to see delivery outcome at a glance; call
`dispatch_delivery_receipt(run_id=...)` for the full accountability receipt on
any one run. Never mutates a run and never weakens an evidence gate.

### `dispatch_cancel(*, run_id: str, reason: str = "client cancelled") -> dict`

Cancel the caller's live dispatch run through the governed hub path (task #865).
The verified bearer identity must match the run's `agent_label`.

### `dispatch_resume(*, run_id: str) -> dict`

Resume a cancelled or retryable dispatch run through the governed hub path (task #865).
The verified bearer identity must match the run's `agent_label`. Fails closed if the run or underlying task is terminal, or if an active lease exists.

### `dispatch_retry_cancellation(*, run_id: str) -> dict`

Retry termination for an operator-visible unconfirmed cancellation (task #1221).
The verified operator identity is recorded as the actor. Fails closed if the run
is not in a state where a cancellation retry is meaningful.

### `dispatch_confirm_cancellation(*, run_id: str, note: str = "operator_confirmed") -> dict`

Confirm a cancellation after an operator has verified the target is gone
(task #1221). Records the verified operator identity and the confirmation note,
distinguishing a cancellation that was merely *recorded* from one that is
*confirmed*. Fails closed when the run is not awaiting operator confirmation.

### `evidence_report(task_id: int | None = None, project_id: int | None = None, since: str | None = None, until: str | None = None, agent: str | None = None, project: str | None = None, pr: str | None = None, limit: int | None = None, item: str | None = None) -> EvidenceReport`

`item` narrows task proof to a node dispatch item (`nd-<16 hex>` or its `node:<node>:<item>` evidence reference). Invalid filters fail; an unknown item or an item without a task returns no records. An item-filtered record names the dispatch's own agent and node (unverified device); with `item`, `agent` matches that dispatch agent, `since`/`until` test the dispatch's own timestamps, and `pr` tests the item's own reference. Its proof takes only the item's own evidence: the `node:<node>:<item>` reference, the item's events and the dispatch's timestamps, never the task's PR, commit or notes (a node receipt names none). It carries an `item_scope` block listing the fields that stay task-wide (human provenance and approvals); a `project_id` chain is still built from the whole subtree's proof. Each ACP turn contributes one summary with tool counts to the task chain (`tool_calls_failed` counts distinct calls whose latest status is failed): `outcome: completed` for a finished turn, or `outcome: aborted` with its `abort_reason` (cancel, steer, agent_cancel, timeout, error).

Read-only user-attributable evidence report. Returns typed MCP structured
content and retains the JSON text block for older clients. `project_id`
must name a task carrying a non-backticked `[project-spec]` marker. It adds a
typed `project_chain` derived from the full multi-level subtree independently
of display filters and pagination: completed-child evidence, authoritatively
merged PRs, and the latest merge. Missing/non-project roots are rejected; a
subtree with no verified merged PR returns `status="partial"`. The display
record limit remains 50 by default and is capped at 500.

Each record carries `denials` (schema 1.8, task #2309): the gateway denials
bound by episode id to the record's runs, each with tool, rule, reason code and
the redacted `args_sha256`, never arguments. `status` is `recorded`, `partial`
or `not-recorded`. `recorded` (the only status where an empty list means zero
denials) needs every run bound and no unbound deny by a participating agent in
the record's window (`unbound_in_window`); a record with no bound run says
`not-recorded`.
See [evidence-report.md](evidence-report.md#gateway-denials-bound-to-runs-task-2309).

### `delegation_spec_build(...)`

Builds a Delegation Spec markdown document from structured fields and, with `auto_validate=True` (default), validates it against the spec standard.

### `delegation_spec_check(...)`

Lints a delegation spec against the Delegation Spec Standard. Read-only and advisory; `delegate` is what enforces it. The definition of done must name at least one observable behaviour, not only build, lint, or test status.

### `delegation_spec_template(...)`

Returns the canonical delegation-spec template and its required section names.

### `detect_dev_servers(...)`

Reads the top-level `package.json`, `pyproject.toml`, docker-compose, and Procfile of a workspace or `UA_AUDIT_ROOTS` path and proposes `.claude/launch.json` entries. Read-only: it does not start a server or write the file. Ports are set only from explicit evidence, and a port already listening is flagged `conflict` (best-effort, Linux only).

### `event_log_query(channels: list[str] | None = None, since: str | None = None, limit: int = 100, after_seq: int | None = None, item_id: str | None = None, channel_prefix: str | None = None) -> dict`

Read-only cursor over the append-only swarm event log (`tools/events.py`,
oldest first). Publishing is in-process only (`events.record_event`); there is
no write tool.

`channels` filters by name; a trailing `*` is a prefix, a bare `*` matches
everything. `channel_prefix` is that prefix as one string (`agent.approval`
matches `agent.approval` and `agent.approval.requested`); a client that sends
it is accepted, and passing both unions them. `/api/events` takes
`channel_prefix` as a query parameter and forwards it. The event stream
applies it as the same prefix pattern. `since` is an exclusive ULID cursor — pass
back the previous `cursor` to resume without gaps. `limit` defaults to 100. `item_id` keeps only
events whose payload's top-level `item_id` matches, such as one node dispatch
item's `agent.node` events (task #1992); `/api/events` takes it as a query
parameter. An `item_id` read past its first page (`after_seq` > 0 or `since`)
omits `source_event_count` and `last_event_at`, which count the whole channel.

Returns `{events, count, cursor, source_event_count, last_event_at}`.
`source_event_count` / `last_event_at` (`events.event_log_liveness`) ignore
`since`: an empty page with a nonzero source count is an idle window; `0` /
`None` means no matching writer has ever been observed.

An allowed call writes no `agent.gateway.decision` row to the log it reads
(task #1811), so paging with `after_seq` and no channel filter reaches an
empty page at the tail, as `/api/ledger/rows` does. Refusals, parked approvals
and failed calls are still recorded.

### `event_log_verify() -> dict`

Verify hash-chain integrity of the append-only event log (`tools/events.py`).

Validates SHA-256 hash continuity from genesis across all entries in ROWID order,
allowing unhashed rows only as a contiguous legacy prefix before chain start. Returns
`{status: "valid"|"tampered"|"empty", verified: bool, total_verified: int,
unhashed_skipped, unhashed_after_chain_start, head_hash, head_ulid}` or pinpointed
tamper details under `tampered_at` on failure. An unhashed row after the first hashed
row reports `tampered` with `tampered_at.reason` `unhashed_row_after_chain_start` and
lists up to 100 offenders (`rowid`, `ulid`, `channel`) in `unhashed_rows` (task #1908).
See [security.md](security.md#evidence-ledger-event_log).

### `browser_derive_task_reference(...)`

Re-validates an untrusted Browser `focus_anchor` projection against contract v1 and derives a `task_reference` for `task_id`, which must name a real non-terminal task. Nothing is persisted.

### `export_journal(...)`

Exports vault journal lines from `Logs/YYYY-MM-DD.md` and every `Logs/<machine>/` namespace as JSONL, each row tagged with `machine`. Optional `agent` and `since` filters. Inline cap 2 MB; vault-write cap 32 MB.

### `export_memory(...)`

Exports memory notes as JSONL or JSON. Index notes are excluded; superseded notes are included and flagged. Optional `tag` and `since` filters. Inline cap 2 MB; vault-write cap 32 MB.

### `export_tasks(...)`

Exports tasks as CSV (RFC-4180), JSON, or Markdown. `status` is one status or `all`; `project` restricts to one project stream. `write_to_vault` writes under `00 - Inbox/exports/`; otherwise the body is returned inline. Inline cap 2 MB; vault-write cap 32 MB.

All formats and vault writes select only the caller’s tenant. NULL selects
only private-hub tasks; it does not export other organizations. Tenant scope
applies together with `status` and `project` filters.

### `export_data(...)`

Consolidated export and migration entry point for authenticated HTTP developer
profiles. Use `action` values `tasks`, `memory`, `journal`, `manifest`,
`compare`, `import`, or `rollback`, with action-specific keyword arguments in
`params`. Import and rollback still enforce their underlying authorization and
policy gates; legacy export tools remain available to stdio clients during the
compatibility migration.

### `flush_digest(...)`

Sends queued digest messages as one iMessage. Omitting `category` flushes every pending category. Returns `{flushed_count, delivered, error}`.

### `format_render(...)`

Renders `table`, `section`, `code`, or `chips` from `payload` and returns `{markdown, text}`. The text form is self-contained for the markup-stripping iMessage path.

### `fs_repo_map(...)`

Builds a PageRank-ranked repository structure map trimmed to `token_budget` (default 1000). `path` is workspace-relative.

### `gemini_ask(...)`

One Gemini call through the Google AI Studio key `GEMINI_API_KEY`. Model defaults to `UA_GEMINI_MODEL` or `gemini-flash-latest`. Optional `response_schema`, `response_mime_type`, and `thinking_budget`. The call is billed to AI Studio; this tool enforces no local spend cap. Returns `{text, model, usage}`. With `UA_PREFER_AGY_CLI=1`, `agy` on `PATH` and no `response_schema`/`response_mime_type`, it first tries the `agy` CLI (no API key; `model` may be `agy-cli` and `usage` zero), falling back to the API if that fails. The CLI path passes only the prompt, `system` and `model`: `thinking_budget` is ignored there, so set `UA_PREFER_AGY_CLI=0` when a call depends on it.

### `injection_triage(...)`

Orders a document's `INJECTION-*` findings by how likely each is to be an attempt rather than a quotation. Advisory only: it runs `security_audit` and passes the findings and status through unchanged. Scoring is deterministic, and every de-escalating signal is forgeable, so it is not a gate.

### `lifecycle_record(kind, session_id=None, task_id=None, agent=None, tool=None, payload=None, idempotency_key=None) -> dict`

Record one sanitized lifecycle observation on `agent.lifecycle.<kind>`. Capture is
off unless `UA_LIFECYCLE_CAPTURE=1`. Unknown kinds, excluded kinds/tools, and
storage failures return `{accepted: false, reason: ...}` rather than raising.
Duplicate `idempotency_key` values return the first committed result.

Kinds: `session_start`, `session_end`, `prompt_metadata`, `tool_start`,
`tool_end`, `compaction`, `stop`, `handoff`. Payload keys `transcript`,
`prompt`, `messages`, `system`, `token`, `api_key`, and similar are dropped, not
stored. Default excluded tools: `gemini_ask`, `pi_run_task`.

Observations are historical context. They do not grant approval or override
instructions. `session_packet` still works when capture is off; it reads existing
stores.

A `compaction` with a `session_id` also starts a new context epoch for that
session (task #2397) and adds `context_epoch` to the result. A duplicate
`idempotency_key` does not advance it, and a session that is unknown or owned by
another agent leaves the epoch alone without failing the capture.

```json
{"kind": "handoff", "task_id": 42, "idempotency_key": "task-42-handoff-1"}
```

### `memory_feedback(id, signal) -> dict`

Record `helpful` / `not_helpful` / `stale` / `wrong` on a note. Counts change
search ranking; content and `updated_at` are not rewritten. Expired notes are
a separate TTL (`expires_at` on `memory_save`).
The counts are the evidence of the note's Beta posterior (task #2097): from
`Beta(1, 1)`, `helpful` adds 1 to alpha; `not_helpful` 1, `stale` 0.5 and
`wrong` 2 to beta. The result adds `confidence_posterior` (see `memory_get`).
Details: [memory-tasks-vault.md](memory-tasks-vault.md#evolving-confidence-and-note-versions-task-2097).

### `memory_prune_candidates(...)`

Returns ranked memory notes that look superseded, low-salience (`salience_threshold`, default 0.3), or old and unaccessed (`max_age_days`, default 30, and salience at most 0.5). `limit` (default 50) caps the list. Soft-deleted notes (`meta:deleted`) are never listed (task #1914). It does not delete or rewrite notes.

### `memory_promote(id, slug=None, summary=None, dry_run=False) -> dict`

Promote a high-confidence memory note to a `Wiki/Learnings/<slug>.md` page with
attribution frontmatter and a link from the machine-managed `Promoted learnings`
block in `Wiki/Index.md` (TeamAI emulation F2, task #915). Refuses a note whose
`confidence` is below `UA_PROMOTE_MIN_CONFIDENCE` (default 0.7). `dry_run=True`
returns the page markdown and planned index line without writing. Writes to
`Wiki/` only — never a PARA folder or a locked note.

### `recall_maintenance(dry_run=True, stale_days=120.0, confidence_floor=0.35, limit=100) -> dict`

Report stale (old, never-recalled, low-salience) and low-confidence memory notes
for review (task #915). Deletes nothing: `dry_run=False` is refused — pruning is
a deliberate per-id `memory_delete` after a human reads the report, and
soft-deleted notes (`meta:deleted`) are left out (task #1914). Recall votes
(`memory_recall_votes`, incremented by every `memory_search` hit and by a
`helpful` `memory_feedback`) feed both this pass and a vote term in
`memory_search` ranking (`UA_MEMORY_WEIGHT_VOTE`, default 0.35).

### `memory_restore(...)`

Clears `superseded_by` on one demoted note so search and tag indexes see it again. Content, title, tags, author, and timestamps are unchanged. Returns `{id, restored, was_superseded_by}`.

A soft-deleted note (`meta:deleted`, task #2515) is restored in place: same
id, its content and tags from the newest recorded version that is not a
tombstone, its links and version history kept (the tombstone becomes one more
version), entities rebuilt and the tag indexes refreshed. The owner or a
verified human may do it, as with `memory_delete`; no verified identity is a
refusal. A note a verified human deleted needs a verified human to restore
it: `memory_delete` adds `meta:deleted-by-human` beside `meta:deleted` then,
and an older tombstone is judged by the identity that wrote it. A deletion with no recorded prior version (made before version
history, task #2097) is refused with a message saying so. Returns `{id,
restored: true, was_superseded_by: null, was_deleted: true, restored_version,
revision}`, plus `superseded_by` when the note is also superseded (call
`memory_restore` again to re-promote it).

### `message_list(conversation_id=None, to_agent=None, from_agent=None, act=None, limit=50, since=None, unread_only=False, count_only=False, waiting_on_reply=False, message_id=None, session_id=None, after_seq=None) -> dict`

Read the per-recipient mailbox index over the `agent.message.*` events (task
#1859). Read-only: it never changes read state. Mail is looked up by recipient
in a derived `message_index` table, so an agent's older messages are no longer
dropped by fleet-wide traffic. The event log stays the source of truth and
supplies each body. Filters are exact matches. Rows come oldest first.

- **Cursor (task #1870).** Rows are ordered and paged by `seq`, the event's
  `event_log` ROWID (its commit order), which every row carries. `after_seq` is
  an exclusive cursor: pass the previous `next_seq` and the oldest `limit` rows
  after it come back, so a poller pages forward without gaps. A message that
  committed late, with an older ULID than one you already paged past, still has a
  higher seq and is returned; a ULID cursor could skip it. Without a cursor the
  newest `limit` rows (1 to 200) come back. `next_seq` is the largest `seq`
  returned (the cursor you sent when nothing came back, `0` for an empty first
  call), so it never moves backwards. The ULID cursor is retired: `since` is still
  a parameter but raises `since (ULID) cursors are retired; page with after_seq
  and next_seq`, and `next_cursor` is no longer returned. A ULID cursor is
  ambiguous whenever ULID order and commit order differ, so a page ordered by one
  can skip mail or ack it unseen; it was retired before any consumer used it. A
  negative or non-integer `after_seq` is an error. Rows whose event retention
  pruned before the index carried a seq have `seq` null: cursor paging
  (`after_seq`) skips them, only the no-cursor newest-window read returns them,
  and a seq `through` does not match them unless `include_pruned` is set (see
  `message_ack`). `seq` is the event log's ROWID, so it holds only while the log is
  never rewritten by a `VACUUM` (nothing in the hub runs one; the `event_log_query`
  `after_seq` cursor shares the assumption).
- **Access.** A caller with a verified identity may list only mail it sent or
  received: `to_agent` naming another agent is refused unless `from_agent` is the
  caller, and a foreign `from_agent` returns only that agent's mail to the caller.
  The operator's own login (`UA_OPERATOR_LABEL`) and the `chief-of-staff` profile
  may list all mail. A caller with no verified identity (stdio without auth, or an
  in-process caller) is not restricted, as before; it names the mailbox it acts
  for with `to_agent`.
- **`unread_only`** with neither `to_agent` nor `from_agent` is the caller's own
  inbox. **`count_only`** returns `{count, unread}` and no rows.
  **`waiting_on_reply`** lists mail the caller sent with `expects_reply` that the
  recipient has not answered (a restricted caller sees only its own sent mail;
  naming another `from_agent` is refused). `unread` (the helper `messaging.index.unread_count`, one indexed query)
  is included whenever the caller's mailbox is known.
- **Sessions (task #1860).** Mail is either label mail (`to_session` null,
  addressed to the agent, visible to every one of its sessions) or session mail
  (`to_session` set, visible only to that session). `session_id` reads your
  mailbox as one of your sessions: label mail plus mail addressed to it, never
  another session's mail. Without `session_id` you see label mail only. The
  session must belong to the mailbox owner (the verified caller, or `to_agent`
  for an unauthenticated one); an unknown session and someone else's session
  give the same error. A call with a `session_id` also refreshes that session's
  `last_seen_at` (a liveness heartbeat, not mailbox state, so the tool stays
  read-only). Mail you sent is not affected by this scoping, so you still see
  what you addressed to someone's session.
- **New sessions and older label mail.** Label mail has no start date: a session
  registered later still sees label mail it has no receipt for and that the label
  has not read, even if sibling sessions already read it. That is intended
  (mail for the agent waits for whichever session picks it up); acking it
  without a `session_id`, or once from each session, clears it.
- **Per-session read state.** Session mail has one reader, so its `read_at` and
  `acked_at` live on the index row. Label mail read or acked *through a session*
  is recorded as a receipt in `message_reads` keyed `(message_id, session_id)`
  and leaves the row alone: it stays unread, and unacked, for the agent's other
  sessions and for the label itself. Label mail read or acked with no
  `session_id` sets the row's own state, which means "the agent read it" and
  counts as read for every session. `read_at`, `acked_at`, `status`, `unread`
  and `unread_only` are all computed for the viewer's own session.
- **Returns:** `{messages: [{seq, ulid, channel, created_at, message_id, act, conversation_id, reply_to, from_agent, to_agent, to_session, from_session, hop, body, task_id, delegation_id, event_id, expects_reply, read_at, acked_at, answered, answered_by, status, body_pruned, delivery, handoff?}], total, next_seq, unread?, session_id?}`.
  `status` is `delivered`, `read` (the recipient read or acked it) or `answered`
  (the recipient sent a message whose `reply_to` is this id). It is kept for
  existing readers: `delivered` there only means stored. `body` is null and
  `body_pruned` true when event-log retention pruned the event.
  `delivery` (task #2391) separates the facts: `{state, admitted_at, appended,
  appended_at, read_at, terminal, terminal_at}`. `admitted_at` is when the hub
  stored it. `appended` is `yes` when a wake rang a live session of the
  recipient after it arrived (from the wake receipts), `unknown` when the node
  could not confirm that wake, else `no`; an unknown append is verified by a
  later read and never re-sent. `terminal` is `answered`, `acked`,
  `dead_letter` (see `UA_MESSAGE_DEAD_LETTER`), `stranded` (session mail left
  unread when its session ended, see `UA_MESSAGE_STRANDED`; recover with
  `message_list(..., session_id=<ended-session>)` — that read does not revive
  the session; [troubleshooting](troubleshooting.md#session-mail-vanished-when-the-session-ended))
  or null. `state` is
  the furthest of `admitted`, `appended`, `dead_letter`, `stranded`, `read`,
  `acked`, `answered`.
- **Errors:** `act` not in `request|inform|query|done|handoff`; reading another
  agent's mailbox.

```json
{"to_agent": "agent-hermes", "unread_only": true, "after_seq": 1204}
```

Read state changes through two `coordination_manage` actions, so this tool keeps
its read-only annotation:

- `message_inbox(to_agent=None, conversation_id=None, act=None, limit=50, since=None, unread_only=True, session_id=None, after_seq=None)`
  lists the caller's inbox like `message_list` (same `after_seq`/`next_seq`
  cursor, which only moves forward: with no cursor the newest `limit` messages
  come back, so while `unread` is above zero, call again with no cursor to read
  older unread mail) and sets `read_at` on the returned messages addressed to the caller
  (`marked_read` counts them).
- `message_ack(message_id=None, through=None, to_agent=None, session_id=None, include_pruned=False)`
  acks one message, or with `through` everything up to and including a cursor:
  a `seq` (an integer, such as `next_seq`) or the `message_id` of a row in the
  caller's mailbox, which resolves to that row's seq. Only mail with a seq at or
  below the cursor's is acked, so a message that committed after you paged is
  never acked unseen, even when its ULID sorts earlier. ULIDs are not accepted: a
  string that is not a `message_id` in the mailbox is refused with an error
  pointing at `after_seq`/`next_seq`. A digit string is a `message_id`, not a seq:
  send the seq as an integer. Rows whose event was pruned before the index carried
  a seq have no position, so a seq `through` skips them; `include_pruned=True`
  also acks them (they are older than anything the log still holds), and
  `message_id=` acks one such row directly. `through=<message_id>` of a pruned row
  is refused and asks for an integer seq. The result adds `through_seq`. An ack
  also marks the mail read; repeating one is a no-op.

- `handoff_ground(message_id, session_id=None, to_agent=None)` (task #2390)
  grounds a handoff you sent or received against live state. Each claim (the
  `reviewed_head_sha`, then each `evidence` item) costs one resolver call, at most
  15 per handoff; the rest are `insufficient`. The resolver sets the tier: T0 for
  the operator's own message or a git commit, T1 for PR or CI state read from
  GitHub, T2 for anything agent-written (another agent's message, a task row, a
  vault or memory note). A cited message resolves only when the handoff's sender
  or the caller was party to it, and only its sender and time are reported. A
  PR or CI run whose head is not the `reviewed_head_sha` is a `mismatch`. The
  result is `{mode: "report", typed, verdict, reasons, high_risk, high_risk_met,
  authorization, authorization_checked, authorization_reference, claims,
  resolver_calls, budget, message_id, event_ulid}`:
  `verdict` is `mismatch` if any claim is, `verified` only when every claim is
  and, for a high-risk `next_action` (merge, deploy, release, delete, rotate,
  ...), at least one is T0 or T1, else `insufficient`. An untyped handoff is
  `insufficient`. It never blocks anything; the verdict, `authorization` and
  counts, never the body, are recorded on `agent.message.grounding`.

  A resolver checks the reference, not what the sender says the reference shows
  (task #2485). Each claim is `{kind, ref, repo, expect, claim, tier, checked,
  reference, verdict, detail, observed?, authorization?}`: `claim` is the
  sender's own words, `checked` names the predicate the resolver evaluated
  (`the commit exists`, `the PR's state and head`, `the CI run's status,
  conclusion and head`, `the task's status`, `the note exists`, `the message
  exists between the handoff's parties, and who sent it`; null when no resolver
  ran), and `reference` is that predicate's result. `verdict` equals `reference`
  except for the case below.

  `authorization` is about a handoff that says an approval or authorization was
  given. The wording is matched as an assertion ("operator approved the deploy",
  "signed off", "approval was granted", "with owner approval") in `done`, `why`
  and each evidence item's `claim` and `expect`; `next_action` and
  `open_questions` are not read, and product nouns such as "approval receipts"
  do not count.

  | `authorization` | Meaning |
  |---|---|
  | `not_claimed` | The handoff asserts no approval. Every other field means what it did before. |
  | `not_checked` | It asserts one. A commit, PR, CI run, task, note or message that exists is not that approval, and no cited message is read, the operator's own included. |

  `authorization_checked` is always `false`: grounding never evaluates an
  approval. `authorization_reference` is `"operator_message"` when the handoff
  asserts an approval and cites a `message` the resolver found to be the
  operator's own (T0), else `null`. It says where a person can read the
  approval. It changes no verdict, because that message may be about something
  else or say "do not deploy".

  While it is `not_checked`: an evidence item worded as the approval carries
  `authorization: "not_checked"` and is `insufficient` even when its `reference`
  is `verified`; the overall `verdict` is never `verified`; `high_risk_met` is
  false, whatever else is verified; and `reasons` says to cite the operator's own
  message, or to read it when it is cited. This changes the report only:
  grounding grants, refuses and gates nothing.

With `session_id`, both act as that session (see per-session read state above):
reading or acking label mail changes only that session's view, and mail addressed
to the session can be read and acked by it alone.

Only the recipient can read-mark or ack; a verified caller acts on its own
mailbox, an unauthenticated one on `to_agent`. Mail that predates the index
is backfilled when the table is created: mail older than three days starts read,
newer mail unread, and an agent can clear the rest with one
`message_ack(through=<next_seq>)`.

### Task workflow record (`coordination_manage`, task #2398)

One record per task: a `stage` (`plan`, `implement`, `verify`, `review`,
`merge`, `deploy`, `prove`, `done`), a resume capsule (`goal`, `done` as a list
of at most 20 short items, `focus`), the `head_sha` the capsule describes, the
`expected_checks` names, and the checks recorded on the task. `session_packet`
returns it as `workflow`, so a compacted or resumed session starts from the
stage. Report mode: nothing here gates a claim, merge or deploy.

- `task_workflow_get(task_id, head_sha=None)` grades every check at `head_sha`
  (default: the capsule's head) and returns `{stage, capsule, head_sha,
  revision, expected_checks, judged_at_head, checks, summary, trust}`.
  `summary` lists names per grade plus `failing` and `contradicted`.
- `task_workflow_set(task_id, stage=None, goal=None, done=None, focus=None,
  head_sha=None, expected_checks=None, expected_revision=None)` changes only
  the fields passed and bumps `revision`; with `expected_revision` it refuses a
  write over a revision you did not read.
- `task_check_record(task_id, name, outcome, head_sha, ref=None, detail=None,
  verify=True)` records `pass` or `fail` for one check at one head. It is
  `attested` (an agent's word). With `ref={"kind": "ci_run", "ref": <run id>,
  "repo": "owner/name"}` the hub resolves the run with `gh` (the resolver handoff
  grounding uses) and raises it to `verified` only when the run finished on that
  head with the matching conclusion. A run that disagrees leaves it `attested`
  and lists it under `contradicted`. Re-recording a check at the same head
  resets it to `attested`.
  A run's overall conclusion says nothing about one job: a run is green with a
  job skipped. Add `"job": "<exact job name>"` to the ref to grade the check on
  that job. It must exist in the run, have finished and have the matching
  conclusion; a missing or skipped job is a contradiction. Without `job`, a
  verified grade covers the whole run only: the check carries
  `verified_scope: "run"` (otherwise `"job"`) and is listed under
  `summary.verified_run_only`.
- `task_check_verify(task_id, name, head_sha)` resolves a stored check again.

A check with no row at the judged head is `unknown`; when a row exists only at
an older head it carries `stale_head`. Writers are the task's claimant, its
`next_owner`, a lifecycle operator or the operator; any caller who can see the
task may read it. Every write appends one event on `agent.task.workflow`. The
capsule and attested checks are agent-written (trust T2); only `verified` was
confirmed by the hub.

### Design panels (`coordination_manage`, task #2399)

An independent-first panel puts one design question to 2-7 seats so the first
answer cannot frame the rest. Design questions only, never code review: a PR is
read once by the steward, so a question asking seats to review or approve a PR,
diff, commit or patch, or naming a commit SHA, is refused.

- `panel_open(question, seats, context=None, task_id=None, deadline_hours=24)`
  sends every seat the same `request` (the question, the optional neutral
  context and how to answer). The opener does not sit on its panel, and a
  question that states the opener's own answer ("I think", "we should") is
  refused. The result lists the message id sent to each seat in `notified`.
- `panel_answer(panel_id, answer, confidence=None)`: a seat's one answer, final.
  The last answer reveals every answer together.
- `panel_get(panel_id)`: before reveal each seat shows only `answered`; the
  caller also sees its own answer. After reveal every answer is shown and a
  missing one reads `no answer`.
- `panel_reveal(panel_id)`: the opener or an operator reveals an open panel
  after its deadline; queue hygiene does the same (`design_panels`).
- `panel_decide(panel_id, decision, rationale, considered=None)`: the opener or
  an operator records the decision and its reasons after reveal, naming the
  seats whose answers it weighed (default: every seat that answered). There is
  no tally. With a `task_id` the decision is appended to the task's notes.
- `panel_list(status=None, seat=None, limit=50)`; `panel_cancel(panel_id,
  reason)` by the opener or an operator, while the panel is still open; a
  revealed panel cannot be cancelled, so its answers stay on record.

Each transition appends one `agent.panel.*` event. Seat answers are
agent-written data (trust T2).

### Friction rollup (`coordination_manage`, task #2399)

Friction is something that keeps going wrong for agents: a tool that times out,
a denied path, a flaky test. Reports carry a `key` (1-80 chars of
`[a-z0-9_.:/-]`, e.g. `tool:run_cmd:timeout`), a `severity` (`low`, `med`,
`high`), a redacted one-line `summary` and a `channel`: `report` (filed through
the action), `operator` (a verified human only) or `session_end` (ingested by
queue hygiene from session-end friction nudges, one report per session and
signal). Ingest reads only signals it has not turned into reports yet, oldest
session first, and writes at most 200 reports per pass, counted per report: a
session cut at the cap finishes on the next pass, and a burst drains over the
next passes. Its result carries `backlog`, the sessions with unread signals left.

- `friction_report(key, severity, summary, channel="report", reporter=None,
  task_id=None, source_ref=None)`: an agent reports as itself.
- `friction_rollup(window_days=14, limit=50, include_closed=False)` groups the
  window's reports by key and ranks them by
  `SEV[max] * log2(1 + count) * (1 + 0.5 * (channels - 1)) * (1 + 0.25 *
  (reporters - 1))`, with low=1, med=3, high=9, so a problem seen through
  several channels and by several agents outranks one agent's repeated note.
  Each item carries its verdict.
- `friction_claim_fixed(key, evidence_ref, note=None, recheck_days=3)` moves a
  key to `fix_claimed`. Saying it is fixed does not close it: it closes only
  when `friction_recheck(key=None)` (or queue hygiene) finds no report for that
  key after the claim once `recheck_days` have passed. A report after the claim
  reopens it. A due claim is not closed while session-end nudges in the ingest
  window are still unread, because one of them may be the recurrence: it stays
  `pending` and the result carries `unread_sessions`.
- `friction_accept(key, note)` closes a key as `accepted`, verified human only.
  A later report on an accepted key is counted but does not reopen it; a report
  on a key closed by re-check does.

Each verdict transition appends one event on `agent.friction.verdict`.

### `message_send(act, to_agent=None, body=None, from_agent=None, conversation_id=None, reply_to=None, message_id=None, hop=0, task_id=None, delegation_id=None, event_id=None, expects_reply=False, to_office=None, to_role=None, handoff=None, to_session=None, from_session=None) -> dict`

Record one typed inter-agent speech act (`request`, `inform`, `query`, `done`,
`handoff`) on the existing event log. The event, its `message_index` row and the
idempotency receipt commit in one transaction. Duplicate `message_id` values
return the first committed result without writing a second row. A `handoff` act is a
speech act only — it does not call `delegate` and does not change approvals.

Address by label (`to_agent`: every session of that agent sees it) or by one
session (task #1860): `to_session` is a hub `session_id`. The session's agent
label becomes the recipient, so `to_agent` may be omitted (if given it must
match), and only that session sees the message. A retry of an already committed `message_id` returns its saved result
before the session is checked, so it still succeeds after the session ended. A session that has ended
(`completed` or `failed`) is refused. A session the reaper marked `abandoned`, or one
still open but past the live window, would never read session mail, so the
message goes to the label instead (task #2547): `to_session` is null and the
result carries `session_fallback: {requested, reason: abandoned|not_live,
delivered_as: "label"}`. One of `to_agent` or `to_session` is
required (a reply may omit both, see below), and so is `body` (it is optional in
the signature only so `to_agent` can be left out; the first three positional
parameters stay `act, to_agent, body`, and the session parameters trail). The
target is stored on the event payload and the index row, so a
rebuilt index keeps it.

Address an office with `to_office` or `to_role` (task #2392). A role is the
office id or display name from the org registry, the same alias `task_list`
uses; the body is not scanned for addresses. When both are set they must name
the same office, and neither is combined with `to_agent` or `to_session`. The
send writes one ordinary label message per member except the sender, in charter
order, and refuses the whole fan-out when that list is longer than
`UA_MESSAGE_MAX_RECIPIENTS` (default 8). Each recipient is checked against the
hop and pair-streak guards on their own chain; if one check fails, nothing is
written. The result is `{accepted, duplicate, message_id, from_agent, office,
`to_office, to_role, conversation_id, count, recipients, availability,
warnings}`, and each entry of
`recipients` is an ordinary single-recipient result. Retrying `message_id`
returns that batch.

Every send also reports the recipient's availability (task #2392), as
`availability: {agent, availability, reason}`: `available` when a live
heartbeat, an open session or an online node can take it now; `degraded` when
the hub knows the label but nothing of it is live, in which case
`delivery_warning` says the message is stored and will wait; `unknown` when
there is no liveness record or the resolver itself failed. Only `unavailable` —
a label that is not an addressable identity — refuses a send. An office fan-out
reports each member in `availability` and collects the quiet ones in
`warnings`; quietness never drops a recipient, because label mail is durable.

`from_session` records the sender's own session on the message (payload and
index, returned as `from_session` on rows). It must be a session of the sender
(an unknown session and someone else's give the same error), and the call counts
as that session's heartbeat. It is what lets a reply find its way back: a
`reply_to` reply to a message that has a `from_session`, sent by that message's
recipient to its sender (`to_agent` omitted or that label), defaults its
`to_session` to that session, so two sessions can hold a conversation by passing
their own `session_id` as `from_session` and replying with `reply_to`. An
explicit `to_session` overrides the default, a reply addressed to a third agent
is not defaulted, and if the default session has ended the reply falls back to
label mail rather than failing. There is no way to force label addressing on a
reply whose parent has a `from_session` other than naming another session.

**Typed handoff fields (task #2390).** On `act="handoff"`, `handoff` is an
object with `done` and `open_questions` (strings or lists of strings),
`next_action`, `why`, `reviewed_head_sha` (7-40 hex), `repo` (`owner/name`, the
default for evidence) and `evidence`: up to 15 items
`{kind, ref, repo?, expect?, claim?}`. `kind` is `message` (a `message_id`),
`commit` (a SHA), `pr` (a number; `expect` open, merged, closed or a head SHA),
`ci_run` (a run id; `expect` a conclusion, default success), `task` (an id;
`expect` a status) or `doc` (a vault path or `memory:<id>`). The fields are
stored on the event beside the body and come back as `handoff` on
`message_list` rows. An unknown key, a wrong type, or `handoff` on another act
is refused and writes nothing. Report mode for everything else: a handoff with
no `handoff` object is still sent, with `handoff_typed: false` and a
`handoff_warnings` entry, and a missing `done`, `next_action` or `evidence` is
warned, not refused. A typed send returns `handoff_typed: true` and
`handoff_claims`, the number of claims a receiver can check.

Sender and recipient must be distinct labels from `UA_AGENT_LABELS`. Hop must
be in `0 .. UA_MESSAGE_MAX_HOPS-1` (default 16). Body is UTF-8 capped by
`UA_MESSAGE_MAX_BYTES` (default 4096) and run through telemetry redaction.

Loop guards (task #2387) apply between agents; a message to or from the human
operator is exempt. A reply's hop is one past its parent's, or the caller's `hop`
if that is higher, so a chain cannot run past the limit by leaving `hop` at 0.
The caller's `hop` is never trusted downward. `UA_MESSAGE_PAIR_STREAK_MAX`
(default 4, `0` disables) caps a run of short messages (under 200 characters)
between one pair in one reply chain. The last message allowed returns
`loop_warning`; the next is refused and nothing is written. A longer message, a
third party, a pruned parent or one side sending twice in a row ends the run. A
message without `reply_to` starts a new chain at the caller's `hop`, which
defaults to 0. Hop counting also restarts at a parent whose event has been
pruned.
An office or role send runs those same two guards once per recipient (task
#2392). One recipient's chain does not advance another's.

Threading (task #1859): `reply_to` must be the `message_id` of a message the
sender sent or received, otherwise the send is refused and nothing is written. A
reply with no `conversation_id` joins its parent's conversation. A `reply_to` in
the `ts:` namespace (the Discord/Telegram bridge) is an external reference and is
stored unchecked. `expects_reply=true` marks the message for the sender's
`message_list(waiting_on_reply=True)` view. The per-run comms policy (#1735)
applies to replies like any other send.

- **Returns:** `{accepted, duplicate, message_id, event_ulid, act, conversation_id, from_agent, from_session, to_agent, to_session, target_live?, hop, task_id, delegation_id, reply_to, expects_reply, loop_warning?}`.
- **Errors:** unknown act; unknown or identical labels; unknown or ended `to_session`, or a `to_agent` that is not the session's agent; hop overflow (default `UA_MESSAGE_MAX_HOPS` is 16); oversized body; `reply_to` not a message the sender was party to; a short-reply pair streak at `UA_MESSAGE_PAIR_STREAK_MAX` (the next short reply is refused and nothing is written); unknown office or role; `to_office` and `to_role` naming different offices; an office target combined with `to_agent` or `to_session`; a fan-out over `UA_MESSAGE_MAX_RECIPIENTS`.

```json
{"act": "inform", "from_agent": "agent-claude", "to_agent": "agent-hermes", "body": "merge of task 42 is on the review branch"}
```

### `metrics_snapshot(...)`

Returns a deterministic cluster snapshot: per-agent event counts for the last 7 days, tasks by status, done-task cycle time, and `memory.db` size.

### `observability_manage(...)`

Consolidated observability entry point for authenticated HTTP profiles. Use
`action` values `activity`, `metrics`, `anomalies`, `performance`,
`anomalies_notify`, `heartbeat`, `agents`, `context`, `runs_in_flight`,
`run_history`, `run_coverage`, `cost_rollup`, `budget_status`, `tool_allowance`, `scorecard`,
`eval_suites` or `eval_record`, with action-specific keyword arguments inside
`params`. The three run actions (task #1436) read the self-pickup run record:
`runs_in_flight()` is the single "what is running now" answer across self-pickup,
`executor_attempts`, `dispatch_runs` and `node_dispatch`, with
`capacity_warnings` for open runs whose heartbeat reports a reached usage limit
or `remaining_pct` at or below 15 (task #1841); `run_history(task_id,
agent, since, limit)` lists runs with outcome, duration and usage;
`run_coverage(window_days=7, details=true, limit=200)` reports completed tasks with and without a run.
Call it through `observability_manage(action="run_coverage", params={"window_days": 7})`.
The cohort contains current `done` tasks whose `completed_at` is at or after
`since`; the latest retained claim's `completed` or `handed_off` finish counts
as recorded, even if that run predates the window. `window_days` must be positive.
`details=false` omits the per-task `missing_tasks` list and `limit` (an integer
from 0 to 200) caps it (task #1887); neither changes the coverage numbers or
`reason_counts`.

The response retains `window_days`, `since`, `completed`, `recorded`,
`unrecorded`, `pct` (rounded to one decimal, `null` for an empty cohort),
`unrecorded_task_ids` (first 200 IDs in task-ID order), and `recorder_failures`
(the count of `run_recorder` trace events since the cutoff). The additive
`diagnostics` object has these fields:

| Field | Meaning |
|---|---|
| `reason_counts` | Counts for every missing task in the full cohort, grouped by the codes below; absent codes have no entries. Counts sum to `unrecorded`. |
| `missing_tasks` | Up to `limit` missing-task details, in ascending task-ID order. |
| `returned_count` | Number of entries in `missing_tasks`. |
| `total` | Total missing tasks, equal to `unrecorded`, including details omitted by the cap. |
| `limit` | Detail cap applied: the `limit` argument (default and maximum 200), or 0 with `details=false`. |
| `truncated` | Boolean indicating that `total` exceeds `returned_count`. |

Each detail contains `task_id`, `completed_at`, `reason`, `latest_claim`,
`matched_finish`, and `latest_unmatched_finish`. `latest_claim` is `null` if
no retained claim event exists; otherwise it contains `event_ulid`, `event_at`,
`run_id`, `agent`, `claimed_at`, `claim_source`, and `observed_from`.
Both finish fields are `null` when no corresponding event exists, or contain
`event_ulid`, `event_at`, `run_id`, `outcome`, `reason`, `finished_at`,
`finished_by`, and `claim_recorded`. Payload fields retain their recorded values
and may be `null` or have legacy types. `matched_finish` belongs to the latest
claim's run under the existing outcome lookup. `latest_unmatched_finish` is the
latest retained finish for that task with a null run ID; it is separate evidence
and is not assigned to an earlier claim or restricted to the completion window.

| Missing-task reason | Meaning |
|---|---|
| `no_recorded_claim_event` | No retained claim event was found for the task. |
| `invalid_recorded_claim` | The missing task's latest claim has an absent, empty, or non-string recorded run ID and no matched finish. A legacy non-string run ID whose finish matched takes that finish's reason below. |
| `latest_run_released` | The latest claim's matched finish has outcome `released`. |
| `latest_run_blocked` | Its matched finish has outcome `blocked`. |
| `latest_run_abandoned` | Its matched finish has outcome `abandoned`. |
| `latest_run_cancelled` | Its matched finish has outcome `cancelled`. |
| `latest_run_open` | The latest valid claim has no matching finish, or a retained finish explicitly reports `open`. |
| `unknown_recorded_outcome` | The latest valid claim has a matched finish with an unrecognized or missing outcome. |

Totals, reason counts, details, and trace counts use one read snapshot. These
fields explain retained evidence under the existing coverage calculation;
missing evidence does not prove no historical claim or execution occurred.
They do not backfill runs, change coverage semantics, select a coverage target,
or turn zero recorder trace events into proof of universal recorder success.

The two cost actions (task #1348) roll the run record up with broker spend:
`cost_rollup(group_by, since, until, agent)` groups by agent, task, project,
provider, model or billing, and `budget_status(agent)` reports each policy
budget's committed and remaining spend. See [budgets.md](budgets.md).
`tool_allowance(agent)` (task #2144) is the read-only view of the policy
gate's per-caller rate limits: per tool, the calls charged in the live
window, the configured limit, remaining, and the reset time while stopped,
plus today's allowed-call count — read from the same counter enforcement
uses. `stopped` requires the current rule generation *and* the gate's
bind/roles filter to still apply to the subject's current scope; glob rules
keep a `pattern` row for uncharged matching tools, and `capacity` reports
the bucket table's fill against `MAX_BUCKETS`. It answers for the calling
identity; the verified operator may name another identity or `"*"`. See
[budgets.md](budgets.md#per-tool-call-allowance-task-2144).
`scorecard(agent, window_days=14)`, `eval_suites()` and `eval_record(suite,
agent, results, model, harness_error)` (task #1354) are per-agent performance:
see [scorecards.md](scorecards.md).
See [self-pickup-runs.md](self-pickup-runs.md). The `anomalies_notify`
action is present for the compatibility surface but remains independently
profile-gated because it can send iMessage alerts. Legacy observability and
session tools remain available to stdio clients during migration.

### `notify(...)`

Sends one iMessage through `bun send.ts` in the imessage-bridge project. A message over 8000 characters is truncated with `\n…(truncated)` and sent, so the returned `chars` can exceed 8000. `timeout_s` defaults to 60 and is clamped to `[10, 120]`. Returns `{delivered, chars, error}`. A send that reaches `send.ts` is recorded, without the message text, in the delivery log that `notify_delivery_status` reads: success, a non-zero exit, or a timeout. Calls that fail before that raise an error and leave no delivery record: an empty message, a `send.ts` that is missing or resolves inside an agent-bound workspace, and `bun` missing from `PATH`.

`send.ts` is resolved from `UA_BRIDGE_DIR` when set, otherwise
`<process workspace>/projects/imessage-bridge` (`process_workspace_root()`,
not the per-agent `workspace_root()`). Set `UA_BRIDGE_DIR` when the bridge
does not live there. `notify` refuses to execute a `send.ts` that resolves
inside an agent-bound workspace different from the process workspace.
`fs_write` refuses the resolved bridge tree.

Profile gate: under authenticated HTTP exposure `notify` is denied to the
developer and steward profiles (`profiles.py` `DEVELOPER_DENIED_TOOLS`) — it
reaches outside the host, so only chief-of-staff sessions (and
stdio/unauthenticated compatibility callers) can invoke it. A developer
profile needing bridge reachability without sending should read the
`bridge_disconnect` finding via `observability_manage`/`check_anomalies`,
which preflights send.ts presence and `bun` on PATH.

### `notify_delivery_status(...)`

Read-only operator surface for Settings → Integrations → Notifications
(`/ui/settings/integrations/notifications`, task #1183): recent outbound `notify()` / `batch_notify()` delivery outcomes
(success/fail, last-send timestamp) from the durable delivery log those
tools write for every send that reaches `send.ts` — never the notification message text.
A `notify` call refused before the bridge runs (empty message, missing
`send.ts` or `bun`, agent-bound bridge) raises and is not in this log.
Never sends a notification itself; the send path stays exactly `notify` /
`batch_notify`, MCP/approval-gated.

### `zulip_send_message(...)`

Sends one bounded message to an allowlisted private coordination channel using
the mapped agent's owner-only `zuliprc`. Setup and governed relay design:
`docs/zulip-coordination.md`. This is an explicit outbound primitive;
it does not poll Zulip, interpret messages as commands, or automatically mirror
Unified-Agent events.

### `zulip_fetch_messages(...)`

Fetches up to 100 messages from one allowlisted channel, optionally narrowed to
one topic. Use `anchor="newest"` for an initial bounded read, then pass the
returned `next_anchor` as `anchor` to request newer messages. Messages sent by
the polling bot are excluded. The tool is read-only: it does not mark messages
read, execute message contents, call a model, or send replies. The supervised
worker additionally consumes one filtered message event queue per bot identity
and uses the REST cursor path for recovery when a queue expires.

### `relay_manage(action: str, params: dict | None = None) -> dict`

Public-reply relay (task #1734, `tools/public_relay.py`). An agent drafts a reply
for an external channel. The relay gates the draft, records the verdict on the
evidence ledger, and queues the gated text for a human decision before anything
is sent. The tool is pruned from the default developer profile; it is reachable
under steward and chief-of-staff.

| Action | What it does |
|---|---|
| `check` | Runs the gates only. Records nothing and queues nothing. |
| `draft` | Runs the gates and records `relay.public.verdict` (categories and masked excerpts, never the raw text). Queues the gated text as approval tool `relay.public.send`. A blocked draft is recorded but not queued. |
| `send` | Takes the identical draft after a human approved it. Re-runs the gates, consumes the one-shot pass, sends through the channel adapter and records the outcome (see below). |
| `status` | Returns one relay approval request's state (`params.approval_id`). |
| `resolve_attempt` | Operator only (a human identity). Records `delivered` or `not_delivered` for a delivery attempt that has no outcome (`params.attempt_id`, `outcome`, `reason`). It never posts. |

`params`:
- `channel`: `dry_run`, `github` or `zulip`.
- `target`: `owner/repo#123` for GitHub, `stream/topic` for Zulip, free text for `dry_run`.
- `text`: the draft.
- `context`: adapter extras, such as `agent_label` for Zulip.
- `requested_by`: ignored when the caller is authenticated.
- `public`: can only widen a private channel. The three built-in channels are always public, so hub-id redaction cannot be switched off on them.

**Gates.** The verdict is the strongest finding.

`block`:
- Secrets and private keys. The patterns come from the audit engine, the telemetry list and the shared `CREDENTIAL_SHAPE_PATTERNS` in `telemetry/redaction.py`: GitHub classic and fine-grained tokens, AWS key ids and secret keys, credentials in URLs, JWTs, Stripe keys, Slack app tokens and webhooks, Google OAuth tokens, HTTP Basic credentials, any `BEGIN … PRIVATE KEY` header, and PuTTY keys.
- Hidden Unicode: any format character (category Cf) or variation selector. Secret patterns run on the text with these characters removed, so an invisible character inside a token does not hide it.
- Text over the channel's length limit, checked before and after redaction.
- Terms in `UA_RELAY_BLOCKED_TERMS`. No excerpt ever shows text that overlaps a blocked term.

`redact`:
- Private, loopback and tailnet IP addresses.
- Internal hostnames: `*.ts.net`, `*.local`, `*.internal`, `*.lan`, `*.home.arpa`, and machine names from `UA_RELAY_INTERNAL_HOSTNAMES`, `UA_AGENT_HOSTS`, `UA_HTTP_ALLOWED_HOSTS`, the vault-sync host and this hostname.
- Home paths: `/home/<user>` becomes `~`.
- Hub task refs (`#NNN`), run ids (`sp-<ULID>`), event ULIDs and 12-hex memory ids.
- Email addresses and phone numbers.

Anything else is `pass`.

A secret is exempt as a placeholder only when its whole value is one of these forms: `${UPPER_CASE}`, `$UPPER_CASE`, `<lower-case>` or a case-sensitive `REDACTED`. A marker word inside a credential still blocks.

**Targets.** The target runs through the same gates. Hub-id redaction is skipped, since a GitHub `owner/repo#123` is the destination itself. A secret in the target blocks the draft. Where the target is published (a GitHub thread, a Zulip topic), a redact-class finding also blocks, because the target cannot be redacted in place. Evidence and receipts record only the gated target. Zulip limits are the sender's: content up to 4000 characters, topic up to 200.

**Approval binding.** The approval payload binds the channel, the target, the gated text, every adapter `context` field, the sender identity and the tenant org. Changing any of them needs a new human decision. Context keys an adapter does not read are refused. An unauthenticated caller can name any sender in `requested_by`, exactly as it can elsewhere on the hub. Only an authenticated identity makes the sender binding meaningful.

**Delivery.**
- The verdict is recorded before channel availability is checked, so a blocked draft leaves evidence even on an unavailable channel.
- Before calling the adapter, the relay appends `relay.public.sending`. It is keyed on the approval id, so one approval never starts two deliveries. Inside the same write transaction, the relay checks that no earlier attempt for the same channel, target and text hash is still open.
- An attempt closes only with `relay.public.sent` or `relay.public.send_failed`.
- `send_failed` is recorded only when nothing can have reached the channel: a failure before the request, a `PermissionError`, or an HTTP 4xx other than 408 or 429.
- Timeouts, connection errors, 5xx responses, 408 and 429, unparseable replies, and a Zulip send that did not confirm are recorded as `relay.public.send_uncertain`. That leaves the attempt open.
- A GitHub comment carries an opaque `<!-- relay-attempt:… -->` marker. Before anything is posted again, the relay looks for the open attempt's marker on the thread. If it is found, the relay records the delivery and refuses the repost. If it is not found, the repost is refused until an operator records the outcome.
- If `relay.public.sent` cannot be recorded after the channel accepted the message, the call returns `recording_gap: true` and appends `audit.evidence.gap`. The attempt stays open.

**Runbook: a stuck delivery.** An attempt with no outcome blocks that text on that target. To clear it:
1. Find the attempt id in the refusal message, or in the newest `relay.public.sending` event for the target.
2. Check the channel itself.
3. A human records what happened: `relay_manage('resolve_attempt', {'attempt_id': ..., 'outcome': 'delivered' | 'not_delivered', 'reason': ...})`.
4. After `not_delivered`, the agent drafts again and a human approves again. The relay never re-posts on its own.

**Approval and adapters.** The approval queue is used whatever `UA_APPROVAL_TOOLS` says. The one exception is `resolve_relay_autosend(channel, context)`, the hook that operating profiles (task #1732) will wire; today it always returns false. The GitHub adapter uses the configured `UA_GITHUB_TOKEN_MODE` transport, with no new credential. In app mode without an installation-token provider it reports itself unavailable. The Zulip adapter refuses while `zulip_retired()` holds.

### `perf_summary(...)`

Aggregates `agent_perf_samples` over `since_hours` (default 24): per-tool call counts, errors, error rate, and latency, plus RSS memory sample stats.

### `pi_run_task(...)`

Headless Pi RPC runner. Long runs report absolute MCP progress milestones
(start, spawned, first tool, completed/timeout) via `Context.report_progress`.
Executor-backed Pi dispatch uses this same function, so an MCP caller that
invokes it receives those events; live run status for out-of-process
executor work is the `unified-agent://runs/{run_id}` resource.

Pi is a coding agent with its own shell, so it does not inherit the hub's
environment. The child env is an allowlist built in `_build_env`: `PATH`
(with `~/.local/bin` prepended when missing), `LANG` (default `en_US.UTF-8`)
and `UA_AGENT_LABEL` always; `HOME`, `TERM`, `TMPDIR`, `USER` and `LOGNAME`
when set; and `OPENAI_API_KEY` and `PI_API_KEY` when already in the hub's
environment. Nothing else is copied, so `UA_AGENT_TOKENS` and integration
secrets stay behind. `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` are stripped
again after the allowlist, so a later edit cannot re-open a metered key. Key
files such as `~/.hermes/.env` or `gemini.env` are not read. `UA_AGENT_LABEL`
is the label checked against the verified identity, not the raw argument.

Pi is launched from `UA_PI_EXECUTABLE` when set (it must be an absolute path
to an executable), otherwise from a local `pi` on `PATH` or in
`~/.local/bin`, otherwise from `UA_PI_PACKAGE` through `bunx`/`npx -y`, and
only when that is a full semver spec (`package@x.y.z`). The unpinned `@earendil-works/pi-coding-agent`
fallback is refused. `session_dir` must resolve inside the workspace.
`timeout_s` is capped at max(120s, `UA_EXECUTOR_TIMEOUT_S`) and covers the
stdout read. Pi runs in its own process group, and a timeout or `kill_check`
cancel kills the whole group. The steward profile is refused.

### `plan_checkpoint(...)`

Stores a sequenced context slice (memory ids, file paths, and an `outputs` map) so a later `plan_resume` can reload it. Current step status is captured from the live plan.

### `plan_create(...)`

Persists an ordered plan. Each step is a string or `{description, owner?, depends_on?}` with 0-based `depends_on` indexes. Nothing is executed.

### `plan_manage(...)`

Consolidated plan lifecycle entry point for authenticated HTTP profiles. Use
`action` values `create`, `update`, `get`, `list`, `checkpoint`, `resume`,
`retry`, `claim`, `handoff`, `close`, `delete`, `step_add`, `step_edit`,
`step_remove`, `step_reorder`, `edit`, `preview`, `review`,
`joint_bind`, `joint_propose`, `joint_review`, `joint_decide`, `joint_get`,
or `joint_recover`, with the action's keyword arguments inside `params`.
Legacy `plan_*` tools remain available to stdio clients during the
compatibility migration. The joint actions are the shared-revision contract
in [joint-planning.md](joint-planning.md) (task #1455). They store revisions
in plan checkpoints, link a goal id and comment ids, and take team roles from
org membership. A stale `expected_revision` raises a conflict and writes
nothing; `joint_recover` reads the winning plan back.

`close` marks a superseded plan `abandoned` with a recorded reason and
keeps its history (task #1496). `params` are `{plan_id, reason, outputs?}`.
It writes a final checkpoint (`closed_as: abandoned`, the reason, optional
outputs, and the step list as it stood) then updates `plans.status`. Steps
are left unchanged. A plan already `done` or `abandoned` is refused.
`reason` must be a non-empty string. `plan_list(status="abandoned")` is how
closed plans stay visible; `plan_delete` is the destructive alternative.

`step_add` (`plan_id`, `description`, `position?`, `owner?`, `depends_on?`),
`step_edit` (`plan_id`, `step_idx`, `description`), `step_remove` (`plan_id`,
`step_idx`) and `step_reorder` (`plan_id`, `order`: a permutation of the
current indices) change an active, non-joint plan's steps (task #2480). Step
row ids stay with their step; positions and every `depends_on` are remapped in
one transaction. A claimed (`doing`) or `done` step is never edited, removed or
renumbered; a removal another step depends on, and a reorder that moves a step
ahead of its dependency, are refused. `edit` (`plan_id`, `title?`,
`description?`) stores the title as the first line of `goal` and the
description after a blank line; plan reads report both as `title` and
`description`. Each takes `expected_revision` (the plan's `revision`, which
every plan write advances); a
stale value raises a conflict. Exposed exactly where `plan_update` is.

`preview` writes an escaped HTML widget to
`.unified-agent/plan-preview/<plan_id>.html` and returns the
`::preview{file=…}` directive. `review` applies a `plan-review v1` Hermes
intent onto the existing `[waiting-for: plan-approval]` /
`[plan: approved @ <ts>]` gate. See [Plan review preview widget](plan-preview-widget.md).

### `plan_delete(...)`

Deletes a plan and cascades its steps and checkpoints. Returns `{deleted}`.

### `plan_get(...)`

Returns plan metadata, all steps, all checkpoints, and `next_step_idx`. `needs_human` is true once a step has exhausted `UA_PLAN_MAX_RETRIES`.

### `plan_handoff(...)`

Marks a step done, stores its `outputs` map, and computes the next actionable step. `notify_owner=True` sends a best-effort iMessage about the next step's owner; a delivery failure does not fail the handoff.

### `plan_list(...)`

Lists plans by status (`active`, `done`, `blocked`, `abandoned`, or `all`), newest first, optionally filtered by `scope`. Escalated plans carry `needs_human`.

### `plan_resume(...)`

Returns the plan, live steps, the next actionable step, and a checkpoint (the latest, or `from_checkpoint` by sequence). A plan with no checkpoint returns `checkpoint: null`.

### `plan_step_claim(...)`

Atomically claims one `open` or `failed` step with a single conditional update; re-claiming your own failed step is allowed. A lost race leaves the row unchanged and the error names the current owner.

### `plan_step_retry(...)`

Increments a step's retry count, marks it `failed`, and appends `reason`. At `UA_PLAN_MAX_RETRIES` (default 3) the step and plan become `blocked` with `needs_human`. It records the escalation and sends nothing.

### `plan_update(...)`

Sets one step's status (`open`, `doing`, `done`, `failed`, or `blocked`), note, or owner. `done` stamps `done_at`. When every step is done the plan completes unless it was escalated. Dependency order is not enforced here.

### `managed_secret_set(name: str, value: str, consumer: str = "zulip", org_id: str | None = None) -> dict`

Write a provider secret to the opt-in managed-secret sink. Requires an
authenticated identity and `UA_MANAGED_SECRETS=1`; returns only the name,
consumer, digest, and stored flag (plus `org_id` when tenant-scoped). The value
has no public read, export, or serialization route. Supported consumers are
`zulip`, `evidence-signing`, `agent_token`, `llm`, `connector` and
`mcp_gateway`; `evidence-signing` is used by detached evidence-export
signatures, and `mcp_gateway` holds downstream MCP server bearers for
`mcp_gateway`.

Tenancy (task #1343): secrets are keyed by `(org_id, name)`. Omitting `org_id`
on a single-tenant hub keeps the original layout (`managed-secrets/<name>`). A
tenant secret lives at `managed-secrets/orgs/<org_id>/<name>` (dirs `0700`,
file `0600`). Under an enforced tenant boundary (`UA_EDITION=enterprise` plus
`WORKOS_ORGANIZATION_ID`), `org_id` defaults to the caller's org and naming
another org is refused. Consumers (Zulip, evidence signing) resolve under
`edition.tenant_org_id()`. A tenant read never falls back to the root or to
another tenant, and an org-agnostic read of a name that any tenant holds fails
closed. The approval payload carries `org_id` only when scoped, so
single-tenant approval payloads are unchanged.

### `deployment_claim(agent_label: str, value: str, *, client_nonce: str | None = None, ttl_days: int | None = None, note: str | None = None) -> dict`

Provision the first agent bearer credential for a `local_trusted` deployment.
The target must be a known fleet identity and the current tenant must have zero
active token lifecycle rows. An already claimed deployment is refused and should
use authenticated `agent_token_issue` instead. The tool also refuses to overwrite
an existing `agent-token:<agent_label>` managed secret.

Supply the credential once as a non-empty `value`; it is stored through the
opt-in managed-secret sink (`UA_MANAGED_SECRETS=1`, consumer `agent_token`).
The response contains its SHA-256 digest and lifecycle metadata, never the raw
credential. The normal profile, policy, and agent-access gates apply; this admin
provisioning tool is pruned from the default developer profile.

A caller without a verified identity must supply `client_nonce` with at least
8 characters after trimming. The forced `managed_secret_set` approval binds the
claimant label/nonce, credential digest, and tenant scope. `ttl_days`, when set,
must be a positive integer. Issuance and `security.deployment.claimed` evidence
are recorded with the lifecycle row.

- **Returns:** `{token_id, agent_label, token_hash, issued_at, expires_at, rotated_from, secret_name, claimed, deployment_mode, next_mode, activation_step}`. `claimed` is `true`, `deployment_mode` is `local_trusted`, and `next_mode` is `authenticated_private`.
- **Activation:** the claim records the credential and returns instructions; it does not change the deployment's authentication configuration. The operator must set `UA_ENFORCE_AUTH=1`, update any explicit `UA_DEPLOYMENT_MODE` to `authenticated_private`, and authenticate with the claimed bearer token.

### `agent_token_issue(agent_label: str, value: str, ttl_days: int | None = None, note: str | None = None) -> dict`

Record a new agent bearer token's lifecycle metadata (task #1198). Stores the
raw value once via `managed_secret_set` (consumer `"agent_token"`) and
persists only the resulting SHA-256 digest, `issued_at`, and optional
`expires_at`/`note` in the `agent_tokens` table. Emits a hash-chained
`security.agent_token.issued` evidence-ledger event. The issued token is a
live hub credential: `config.agent_tokens()`, which the HTTP bearer check
calls on every request, merges active, unexpired managed tokens, so the value
authenticates as `agent_label` from the next request on (task #1826).
`UA_AGENT_TOKENS` is not edited; its entries keep authenticating alongside
managed ones.

- **Returns:** `{token_id, agent_label, token_hash, issued_at, expires_at, rotated_from, secret_name}` — never the raw value.

### `agent_token_rotate(agent_label: str, new_value: str, ttl_days: int | None = None, note: str | None = None) -> dict`

Record a rotation for `agent_label`: issues a new `agent_tokens` row via the
same path as `agent_token_issue`, links it to the previous active row via
`rotated_from`, marks that previous row `status='rotated'`, and appends a
hash-chained `security.agent_token.rotated` event to the evidence ledger.
Approval-gated the same way `managed_secret_set` is. For managed tokens the
rotation is live: the new value replaces the old one in the managed secret
store, so from the next request the new value authenticates and the previous
managed value does not. It never edits `UA_AGENT_TOKENS` or restarts the hub:
a previous value listed there keeps authenticating until the operator removes
it.

- **Returns:** `{agent_label, previous_token_id, new_token_id, new_token_hash, issued_at, expires_at, rotated_at}`.

### `agent_token_list(agent_label: str | None = None, status: str | None = None) -> dict`

Read-only listing of `agent_tokens` metadata rows, optionally filtered by
label and/or lifecycle status (`active`/`rotated`/`revoked`/`expired`).
Returns hashes only, never plaintext values.

- **Returns:** `{tokens: [{id, agent_label, token_hash, hash_algo, status, issued_at, expires_at, rotated_from, rotated_at, revoked_at, issued_by, note}, ...]}`.

### `oauth_device_manage(action: str = "list", device: str | None = None, session_id: str | None = None) -> dict`

Operator surface over the hub's durable OAuth store (task #1321). `list` is
read-only: live refresh-token sessions grouped by the tailnet device they were
bound to at sign-in, each with `session_id` (16-hex digest prefix), `agent`,
`client_id`, `auth_method`. `revoke` deletes the sessions for exactly one
`device` (node name or id) or one `session_id` and drops their live access
tokens. Revoke is a prod action (`revoke` scope + one-shot approval); the first
call parks an approval and revokes nothing. Never returns a token value.
Withheld from operator clients and pruned from the default profile. Agents →
Hosts (`/ui/agents/hosts`) calls the same code through `POST /api/action/oauth-revoke`.

- **Returns (list):** `{devices: [{device_node, device_node_id, bound, sessions: [...]}], session_count, oauth_enabled}`.
- **Returns (revoke):** `{status, device, session_id, revoked, sessions, actor}`.

### `policy_authorization_record(repo: str, pr: int, head_sha: str, policy_ref: str, task_id: int) -> dict`

Record that a standing policy covers merging one exact pull-request head
(task #1798; the MCP surface over `github_app.record_policy_authorization`,
which landed with the ledger in task #1407). The caller's verified identity
is the authorizer — no argument can name someone else. Only identities
listed in `UA_POLICY_AUTHORIZERS` may record (default empty, so nobody;
human labels are refused), and the task's claimant must differ from the
authorizer unless `UA_POLICY_AUTHORIZATION_ALLOW_SELF=1`. This is a
pre-merge step: `task_id` must still be open (`open`, `doing`,
`waiting_review` or `blocked`) and still carry its worktree branch — a
`done` or `cancelled` task is refused, and the branch is never taken from
the caller.

`repo` is `owner/name`, `pr` the pull request number, `head_sha` the full
40-character head commit the policy covers (exact-head binding: any other
head of the same PR is not covered), `policy_ref` a short reference such as
`deploy-steward-2026-10-01` or `operator-chat:<ref>` — never conversation
text. Validation, signing and the append share one write transaction, so
task completion cannot slip between the check and the record.

- **Returns:** `{status: "recorded", authorization_id, kind, evidence_class, human_approval, repo, number, head_sha, policy_ref, task_id, task_claimant, task_branch, authorized_by, red_tier}` — never the signature.
- **Errors:** `ToolError` naming the refusal — caller not in `UA_POLICY_AUTHORIZERS`, self-authorization, a missing/closed/branchless task, an unverifiable identity, or a malformed binding.
- **Docs:** [connector-activation.md](connector-activation.md#policy-authorized-merges-task-1407).

### `policy_authorization_list(repo: str, pr: int, head_sha: str | None = None) -> dict`

List the policy authorizations recorded for one pull request, newest first
(task #1798, over `github_app.list_policy_authorizations`). `head_sha`
(optionally) narrows the list to one exact 40-character head. Every row
carries `valid`: the same checks the ledger applies at merge time — a live
hash chain, a signature that still verifies against the authorizer's
current bearer, the authorizer's current membership in
`UA_POLICY_AUTHORIZERS`, and the self-authorization rule — minus the
red-tier check, which reads the pull request's changed files and stays
with the merge-time caller. Removing a label from `UA_POLICY_AUTHORIZERS`
or turning `UA_POLICY_AUTHORIZATION_ALLOW_SELF` back off flips the rows it
covered to `valid: false` on the next read.

- **Returns:** `{repo, number, head_sha, authorizations, count, valid_for_head}`. Each row is the public payload plus `authorization_id`, `originator`, `created_at` and `valid` — never the signature. `valid_for_head` is the newest valid row when `head_sha` was given, else `null`.
- **Errors:** `ToolError` on a malformed `repo`, `pr` or `head_sha`.

### `profile_activate(profile: str) -> dict`

Activate `developer`, `steward`, `chief-of-staff`, `operator`, or `research` for
this authenticated HTTP MCP session. A new session starts at the verified
identity's default (`chief-of-staff` for Hermes, `operator` for
`Desktop_Person`, `developer` for engineering labels).

Requires `UA_ENFORCE_AUTH=1` and a valid bearer token. Unavailable over stdio.
A research session cannot activate another profile; call `profile_reset` instead.

- **Returns:** `{profile, identity, session_scoped: true}`.
- **Errors:** unknown profile name; not an authenticated HTTP session; research
  attempting to escalate.

```json
{"profile": "research"}
```

### `profile_reset() -> dict`

Return this authenticated HTTP session to the verified identity's default
profile. Clears any explicit activation for the current MCP session id.

- **Returns:** `{profile, identity, session_scoped: true}`.

### `profile_status() -> dict`

Report the profile active for this request. Does not expose session secrets.

- **Returns:** `{profile, identity, session_scoped}`. `session_scoped` is true
  when an MCP session id is present.

### `config_revision_manage(action: str = "list", kind: str = "policy", revision: int | None = None, from_revision: int | None = None, to_revision: int | None = None, document: dict | None = None, limit: int = 50) -> dict`

List, diff, show, capture, commit, or roll back authority config (task #1352).
See [config-revisions.md](config-revisions.md). Revisions are event_log rows.
`rollback` is approval-gated and does not delete history.

### `propose_session_learnings(session_id: str, dry_run: bool = False) -> dict`

File a reviewable learning proposal for a **completed** session (tasks #511 / #837).
Writes vault-inbox markdown (`00 - Inbox/Session-Learning-<id>.md`) only when
deterministic candidates exist (error→fix sequences and always/never/gotcha
rules) with `confidence` and `provenance`. Dedupes against promoted memories
and earlier Inbox proposals. Never calls `memory_save`, never edits instruction
files, never auto-approves. Idempotent per session fingerprint; changed
evidence refreshes the same path. Empty/low-signal sessions return
`skipped: low_signal` and write nothing. `dry_run=True` returns markdown
without writing. `session_end(status="completed")` calls this best-effort.

A separate friction nudge (task #914) may also appear on that `session_end`
result as `friction_nudge`. It never files a proposal or writes memory; it
only suggests running this tool. Default on (`UA_FRICTION_NUDGE=1`). See
[memory-tasks-vault.md](memory-tasks-vault.md#session-learning-proposals-validate-and-hold).

- **Returns:** `{session_id, written, dry_run, applied: false, path, ...}`.
- **Errors:** missing/unknown session; session not completed; vault disabled.

### `propose_skill_clusters(dry_run: bool = False) -> dict`

Cluster high-trust promoted memories into ranked skill-outline candidates
(task #839 / ECC-6). Selects non-expired, non-superseded `fact`/`decision`
notes at the ECC instinct confidence floor (`UA_MEMORY_INJECT_CONFIDENCE_THRESHOLD`,
default 0.7), isolates by project/author/source_host, and clusters with
lexical Jaccard plus embedding cosine when embeddings exist. One-note
clusters are dropped. Writes `00 - Inbox/Skill-Cluster-Proposals.md` with
explainable evidence ids/scores. Never calls `memory_save`, never writes
`SKILL.md` or host skill/rule directories, never auto-approves. Idempotent
per ranked-set fingerprint; changed evidence refreshes the same path.
`dry_run=True` returns markdown without writing. Nightly
`unified-agent-sleep-pass` calls the same pass before any LLM work.

- **Returns:** `{written, dry_run, applied: false, path, proposal_count, ...}`.
- **Errors:** vault disabled on a non-dry write.

### `read_image(...)`

Returns base64, mime, dimensions, byte size, and a deterministic metadata description for a workspace image. No OCR. Cap 2 MB; unknown formats raise.

### `red_team_qa(...)`

Runs `security_audit`, then `python_lint_and_format` (which can rewrite Python files), then `uv run pytest -q` with a 120 s timeout. Returns `PASS`, `WARN`, or `FAIL` and annotates the task when `task_id` is set. It is not a sandbox.

### `review_and_notify(...)`

Calls `review_task` and sends one iMessage when the result carries a new notification; an unchanged head sends nothing. An in-flight task is validated and held unless the project's delivery policy has `yolo` on, in which case a green, non-red-tier local worktree is merged with `git_merge_task`. A task already `done` follows the local-merge path (`_merge_done_task`): it merges when audit and pytest pass and the diff is not red-tier. `dry_run=True` never merges. Returns the review result plus `notified`.

### `scan_for_reviewable_tasks(...)`

Returns tasks annotated `[waiting-for: review]` or `[waiting-for: plan-approval]`, each with a `kind` of `review`, `scout`, or `plan-approval`. Explicit `[no-code]` tasks are omitted unless they are scouts.

### `session_end(...)`

`session.operator_end` (task #2480) is not a tool: it is the `session` object's
platform writer (`tools/registry.py:session_operator_end`), reached only from
`POST /api/action/session/operator-end` through the governed write adapter. It
lets the verified human operator end another identity's session with a
reason, ledgers a `session.operator_ended` event, and leaves `session_end`'s
ownership rule unchanged.

Close a registry session. `status="completed"` best-effort files a learning
proposal (`propose_session_learnings`) and may attach `friction_nudge`
(task #914) — a suggestion only, never a memory write. See
[memory-tasks-vault.md](memory-tasks-vault.md#session-learning-proposals-validate-and-hold).

The result's `owed` reports what the session leaves behind (task #2389):
`unanswered` (`expects_reply` mail that arrived during the session, was read by
it or addressed to it, and has no reply), `next_moves` (tasks stored against its
agent that the agent does not hold) and `claims` (held `doing` tasks with no
action path once the lease stops), each capped at 20, plus `counts`, `count`
and `other_open_sessions`. Report only: it never refuses the close. Each ended
session, including one the reaper abandons, writes one `agent.session.owed`
event from the hygiene sweep, and the `session_owed` doctor row measures them.
A Stop hook asks `POST /api/inbox/session/owed` at each turn end; each distinct
owed set is recorded once on `agent.session.owed.turn`.

### `session_list(...)`

Lists agent sessions, newest first, joined to the task each was working. `status` accepts a literal status or `open`. Stale sessions are reaped to `abandoned` before the read. Each row carries `last_seen_at` (the same timestamp as `last_heartbeat_at`) and `live` (open and seen within `UA_SESSION_LIVE_MINUTES`, default 30). `live_only=True` returns only live sessions (the filter runs before `limit`, so stale newer sessions cannot hide a live one); the result also reports a `live` count. `agent_context` offers the same set, compacted, per peer.

Sessions are tenant-scoped (task #1995). Each row stores the `org_id` of the
caller that opened it (`edition.tenant_org_id()`; NULL on the untenanted hub),
and rows from before the column take their task's `org_id`. `session_list`,
`session_end`, `session_start`'s idempotent lookup and prior-session recall,
`session_packet`, the `sessions/{session_id}` resource, mailbox session checks,
live-session lists and per-agent open-session counts all select only the
caller's tenant. Another tenant's session answers exactly like a missing id
(`session_end` raises `no such session` and changes nothing). On the
untenanted hub every row is NULL, so nothing changes.

### `session_packet(task_id=None, session_id=None, agent=None) -> dict`

Build a bounded resume packet from existing control-plane state: task, worktree,
delegations, live approvals tied to the task (its `task_id` or
`parent_task_id`; a packet with no task lists none), recent events, related
memory, prior handoff, and lifecycle observations. It is fenced to the
caller's tenant: another tenant's task, or a session anchored to one (or, for
a tenanted caller, a session with no task), reads as not found. It never reads
a transcript file. The packet is labeled
historical context (`authority: false`); instructions and approval state stay
authoritative elsewhere.

Pass `task_id` or `session_id` (or both). When both are supplied they must
agree: a session belonging to a different task is rejected. The raw response
includes caller-specific `requested_by` and a fresh `generated_at` (and the
session section when a session row is loaded), so two callers do **not** get
byte-identical bodies. Use the in-process `comparable_packet()` helper (not
exposed as an MCP tool) to drop those per-caller fields before comparing.
Byte cap is 16 384; oversized sections are truncated in place.

- **Returns:** `{schema: "ua.session_packet.v1", labels, generated_at, requested_by, session_id, session, task, worktree, delegations, approvals, recent_events, memory, experience, prior_handoff, lifecycle, why_chain, operating_profile, handoff_note, context_epoch}`.
  `handoff_note` is the session's latest sealed handoff note and
  `context_epoch` is `{session_id, epoch, reason, started_at}` (task #2397);
  both are `null` without a session, and a failed read leaves them `null`
  rather than failing the packet. `labels.source_tiers` maps each section to a
  trust tier: `T0` operator (approvals, operating profile), `T1` hub state,
  `T2` agent-written prose (memory, experience, handoffs).
  `memory` hits carry `epistemic`, `author` and, when stored, `evidence` and
  `valid_from`. `experience` lists up to three ended sessions on the task
  (fact-labelled, agent-reported summary, `session:`/`task:` evidence) within
  half the memory injection token cap (task #1459).
- **Errors:** neither `task_id` nor `session_id` provided; or mismatched task/session anchors.

### `crew_view(session_id: str, limit: int = 50) -> dict`

Inspect a live crew session: status plus its most recent messages, bounded by
`limit`. Read-only, and registered in the read-only tool roster so a
coordinating agent can watch a session without a human relaying it.

### `crew_intervene(session_id: str, message: str, message_id: str | None = None, from_agent: str | None = None) -> dict`

Send a typed intervention into an active crew session. The message is addressed
to that one session (`to_session`, task #1860), not to the agent label, so a
second session of the same agent does not receive it; a session that has ended is
refused. The `.ua/interventions.jsonl` line written to the session's worktree is
an audit copy that `crew_view` echoes as `worktree_interventions`: nothing reads
it for delivery. Reuses the guards the
Control Center's crew action already enforces — message cap, duplicate
suppression, and no self-send — and takes caller identity from the verified
session context, never from the `from_agent` argument. Registered as a
destructive tool with maximum gating.

Per-profile exposure for both crew tools (task #1151; pinned by
`tests/test_profiles.py::test_crew_tools_profile_exposure_matrix`):

| Profile | `crew_view` | `crew_intervene` |
|---|---|---|
| `developer` | yes | yes |
| `chief-of-staff` | yes | yes |
| `steward` | yes | no |
| `operator` | yes | no |
| `research` | no | no |
| `public` | no | no |

`crew_intervene` stays with developer and chief-of-staff (steward's denial is
applied in `server.py`). The operator's intervention path is the CSRF-gated
`POST /api/action/crew-intervene`, which records the authenticated web
identity — see [web-ui.md](web-ui.md#live-crew-watch-and-intervene-task-1025).

```json
{"task_id": 42}
```

### `session_start(...)`

Opens a session row and returns its id. A session left without a heartbeat is reaped as `abandoned` after `UA_SESSION_ABANDON_MINUTES` (default 30).

Idempotent on `(agent, external_session_id)` (task #1860): registering the same external id again returns the same `session_id` with `created: false`, refreshes `last_seen_at`, reopens a row that was reaped or ended, and fills in any `task_id`, `branch`, `worktree_path`, `transcript_path` or `host` it now knows, so calling it again doubles as a heartbeat. Without an external id every call opens a new row. `host` says where the agent runs; a new session without one takes the host the label last heartbeated from, and a refresh keeps the session's existing host unless one is passed. Registration is identity-bound: a verified caller may register only under its own label, and naming another `agent` is refused. A `message_list`, `message_inbox` or `message_ack` call that passes the `session_id` also counts as a heartbeat. Returns `{session_id, agent, kind, created, host, last_seen_at, ...}`. `include_packet=True` attaches a bounded resume packet; `recall_prior_packet=True` attaches the packet of the latest other session on the same `task_id`.

### `state_sync_verify(...)`

Compares live memory and task ID sets (memory index notes excluded) with the vault `Logs/state_*` mirrors; counts are reported only. A missing mirror file is drift. On drift it runs a sync and reports `resynced`.

### `task_claim(id: int, agent: str, timeout_seconds: int = 3600, expected_project: str | None = None, expected_lane: str | None = None, expected_run_id: str | None = None, run_usage: dict | None = None, usage_only: bool = False) -> dict`

Atomic claim/lock: a single conditional `UPDATE`. Succeeds when the task is
not `done`/`cancelled` and is unclaimed, already held by `agent`, or its lock
has expired. Sets status to `doing`, writes `claimed_by` / `claimed_at` /
`lock_expires_at`, and appends a `claimed-by:` notes line. `timeout_seconds`
is the lease length and must be between 1 and 604800 (7 days); anything else
raises `ToolError`.

`expected_project` and `expected_lane` are guards. Lane rules and WIP caps
(`UA_AGENT_WIP_MAX`, per-label `UA_AGENT_WIP_OVERRIDES`, `UA_LANE_WIP`) are in
[memory-tasks-vault.md](memory-tasks-vault.md#queue-lanes-and-wip-caps-task-705-actor-model-883).
A lock renewal by the same agent does not re-count against WIP.
Soft lane overflow returns `wip_warnings`; hard overflow raises `ToolError`.
The fleet-wide cap (task #1838) counts live `doing` work across all agents
(claimed rows with a live lease, plus unclaimed rows inside their implicit
lease, task #1886): at
`UA_FLEET_WIP_MAX` a new claim returns `wip_warnings`, at
`UA_FLEET_WIP_HARD_MAX` it raises `ToolError` unless the task is
`high`-priority or `[urgency: now]`. `operator-only` and `interactive-only`
claims are exempt.
Policy budgets (task #1348, [budgets.md](budgets.md)) apply the same way: an
exhausted `block` budget refuses a new claim, a budget past its warning
threshold returns `budget_warnings`, and a renewal is never refused. A
renewal carrying `run_usage` is judged with that usage counted (task #1917):
it may return `budget_warnings`, or `budget_stop` once a `block` budget is
exceeded.
A paused or terminated agent (task #1351, [agent-lifecycle.md](agent-lifecycle.md))
cannot claim or renew.
A new claim opens a self-pickup run record (task #1436); a live renewal writes
nothing; a takeover after a lapsed lease closes the prior run `abandoned`. The
record is best-effort and never fails the claim
([self-pickup-runs.md](self-pickup-runs.md)).

`run_usage` is a heartbeat (task #1681). On a live renewal by the agent that
already holds the task, it records the run's cumulative usage so far, with
the same fields as `task_update`'s `run_usage`. Each heartbeat replaces the
fields it reports. The open run shows the latest one, and the run's finish
carries it. `run_usage` is refused on a new claim or after the lease lapsed,
because the usage would not belong to the run being renewed. A heartbeat
appends no event-log row.

`usage_only=True` requires `expected_run_id`, `run_usage`, and the exact
authenticated actor recorded on that run's immutable claim. It records
cumulative accounting even after expiry, cancellation, review, or takeover,
without writing the task or renewing a lease. Numeric totals never decrease.
`run_usage.cost_complete` is an optional boolean. `false` marks incomplete
pricing and stays sticky across overlays: known USD remains counted along
with an unknown-cost reserve even when a later retry supplies a price.
A deduplicated `selfpickup.usage_reported` ledger event preserves the report;
run readers and budgets overlay late usage on the unchanged finish. The response
is `{id, run_id, usage_only, usage}` plus optional budget warnings/stop.
Live renewals re-check recorded spend even without a new usage snapshot, so
a pending retry cannot bypass a stop accepted during replay.

- **Errors:** empty `agent`; missing task; project/lane guard mismatch;
  terminal status; active lock owned by someone else; lost race; WIP cap;
  invalid `run_usage`, or `run_usage` on anything but a live renewal.
- **Returns:** the updated task row, plus optional `wip_warnings`.

### `vault_briefing_digest(...)`

Merges the `Logs/{date}.md` journal, the historical `Logs/Laptop/{date}.md` mirror,
and the explicit `Logs/pr-watch/{date}.md` stream into one deduped digest. PR-watch
entries use `machine="pr-watch"`; `pr_watch_count` reports their source count. The
stream must pass the vault read guards and fit within 64 KiB. By default, the digest
also reports the ingest state of every `.md`/`.txt` capture directly in `00 - Inbox`
(not subfolders or attachments). Read-only: it never marks an ingest.

### `vault_ingest_status(...)`

Hashes an inbox note and classifies it `new`, `unchanged`, `changed`, or `empty` against the ingest ledger. `mark=True` records the hash after a human ingest and requires `agent`. Skip `unchanged`; reject `empty`.

### `vault_query(...)`

Walks `[[wikilinks]]` from lexical seeds with Personalized PageRank. No embedding index and no network. `max_results` is capped at 50.

### `vault_to_memory_ingest(...)`

Copies notes from vault subdirectories (default `Wiki` and `00 - Inbox`) into memory rows so FTS and semantic search can see them. This writes memory. Only the `.md` files directly inside each subdirectory are read: nested folders are skipped, not descended into, and each subdirectory listing stops at `vault_list`'s 500-entry cap. Pass a nested folder in `subdirs` to ingest it.

### `workflow_list(...)`

Lists the YAML workflows shipped in `src/unified_agent/workflows/`. Definitions are code, reviewed in a pull request.

### `workflow_run(...)`

Role definitions optionally accept `role_bindings` when starting, and `instance_id` plus `closure` when continuing. They return a durable task `frontier` and an append-only closure trail. Each role claims its task normally; tool calls retain the governed workflow path. See [role-workflows.md](role-workflows.md) for closure reasons, atomic handoffs and restart recovery.

Runs one of those YAML workflows. Each step calls a curated registry tool through the same governed path as a direct MCP call, so the shell allowlist, the container sandbox setting, vault write directories, the approval queue, and the access, profile, and policy wrappers still apply. A step held by the approval queue stops the run (`pending-approval`); a denied step stops it (`denied`). If decision recording fails after a mutating step runs, the workflow stops as `indeterminate` without retrying; verify its effects and repair the ledger before resuming. `workflow_run`, the approval tools, destructive deletes, and repo-mutating git tools are not callable as steps. `author` must match the authenticated caller.

### `gcloud_inventory(...)`

Read-only inventory of GCP project resources (project metadata, compute instances, storage buckets, billing budgets). Zero tokens, keys, or credentials in output.

Returns a `sections` map alongside the data, giving each requested section an
explicit outcome of `ok` or `unavailable`. Read that map rather than the
truthiness of `instances` / `buckets` / `budgets`: an empty list is a
legitimate result for a project that genuinely has none, and is *not*
distinguishable on its own from a section the caller lacked permission to
read.

`status` is derived from section coverage, not from the error list:

| `status` | meaning |
| --- | --- |
| `ok` | every requested section was read successfully |
| `degraded` | at least one section was read, at least one was not |
| `error` | no requested section could be read |

Any section that returns neither data nor an error is recorded as
`unavailable` with a named entry in `errors`. This matters for health checks
and self-healing: an under-privileged credential must fail closed rather than
report a running instance as an empty project.

### `cloud_worker_recover(...)`

Approval-gated self-healing for the private GCP worker. One recovery class:
starting `ua-worker` when health was actually read and reports it stopped.
It can never stop, delete, resize, or create an instance.

`dry_run` defaults to `True` — the default call reports what it would do and
changes nothing.

Fails closed in three ways:

- an unreadable `instances` section is `unknown`, never "stopped" (see
  `gcloud_inventory`'s `sections` map);
- if `gcloud_ops` is not in the hub's `UA_APPROVAL_TOOLS` allowlist it refuses
  with `ungoverned`, so disabling the approval queue disables recovery rather
  than making it unattended;
- a denial is terminal for the cooldown window and is never retried into a
  fresh approval request.

Attempts are written to the `agent.cloud.recovery` audit channel before the
mutation, with a `COOLDOWN_MINUTES` (30) window that blocks a repeat attempt.

### `gcloud_ops(...)`

Execute bounded GCP operations (instance describe, instance start/stop, bucket describe, bucket object list). Only the mutating actions — `instance_start`, `instance_stop` — require approval; the read-only inspections (`instance_describe`, `bucket_describe`, `bucket_list_objects`) run without one.

### `work_product_submit(source_path: str, title: str, template_pack: str, tags: dict | None = None, model: str | None = None) -> dict`

Submit a document/file (report, filing, remediation, patient note, audit
workpaper...) for named-reviewer approval and evidence-backed release (task
#1278, H2 — the non-code pilot wedge). Off by default
(`UA_WORK_PRODUCT_ENABLED=1` to enable) and pruned from the default developer
profile — reachable under `steward`/`chief-of-staff`, same treatment as
`agent_token_*`.

Vertical specifics never live in this tool: `template_pack` selects a YAML
pack under `src/unified_agent/templates/work_product/` (two synthetic packs ship: `legal`,
`accessibility-remediation`) that declares which `tags` are required, the
named reviewer role, and the release subfolder. A missing required tag is
rejected before anything is hashed or stored.

Always parks a `require_approval` request in the existing approval queue on
first call (`ApprovalPendingError`, the same shape `managed_secret_set` and
`agent_token_issue` already use) — the submitter can never approve its own
submission: `approval_resolve` refuses an approve by the requesting identity
for `work_product_release` (`approvals.SEPARATION_OF_DUTIES_TOOLS`) under
every profile, and is denied to the developer profile outright. A human resolves the pending row with `approval_resolve`;
calling `work_product_submit` again with the identical file/title/pack finds
the same submission (content-hash keyed, not a new row) and either performs
the release (approved: copies the exact bytes to
`UA_WORK_PRODUCT_RELEASE_DIR/<pack release_subfolder>/`, re-hashes on copy,
and appends `work_product.approved`/`work_product.released` evidence events
linking human → device → agent → model → document hash → approval id) or
records the rejection (denied: `work_product.rejected`, no copy).

- **Returns:** the stored `work_products` row (`id`, `title`, `content_sha256`, `source_path`, `size_bytes`, `mime_type`, `producing_agent`, `producing_model`, `template_pack`, `tags`, `status`, `previous_version_id`, `approval_id`, `released_path`, `released_sha256`, `reject_reason`, `submitted_by`, `resolved_by`, `resolved_at`, `released_at`, `created_at`, `updated_at`).
- **CLI:** `unified-agent work-product submit <file> --pack <id> [--title T] [--tag k=v] [--model M]`, `unified-agent work-product verify <id>` (re-hashes source/released copy, flags `tampered`), `unified-agent work-product list [--status S] [--pack P]`, `unified-agent work-product evidence <id>`, `unified-agent work-product diff <id>`, `unified-agent work-product packs`.
- **Platform object:** `unified-agent://work-products/{work_product_id}`, `kind="work_product"` in `object_model.OBJECT_TYPES` (`served_types()`), readable over the generic `platform get work_product <id>` HTTP/CLI surface.
- **Docs:** [work-product-release.md](work-product-release.md).

## Routines (task #1350, scheduler core)

### `routine_add(title: str, *, trigger: str = "cron", schedule: str | None = None, notes: str | None = None, lane: str = "default", project: str = "unified-agent", domain: str = "hobby", concurrency: str = "skip", catch_up: str = "last", start_at: str | None = None, activity_gate: str = "always", activity_gate_scope: str = "org") -> dict`

Store a routine definition. Cron schedules use five UTC fields. Cron ticks are
disabled until the hub operator sets `UA_ROUTINES_ENABLED=1` and invokes the
one-shot `unified-agent-routines tick` command. See [routines.md](routines.md).
`activity_gate="require_activity"` records a `suppressed` receipt instead of a
task when nothing happened in `activity_gate_scope` (`org` or `project`) since
the routine's previous fire (task #1752); `always` keeps every fire.
Routine management tools are available under the chief-of-staff profile.

### `routine_list(*, include_paused: bool = True, recent_runs: int = 0) -> dict`

List definitions in the caller's tenant. `recent_runs` above 0 (clamped to 100)
adds `recent_runs` to each definition: its newest receipts, newest first, with
`id`, `fire_key`, `scheduled_for`, `status`, `reason`, `subject_key`, `task_id`,
`created_at`, `window_start` and `gate_matched`. `reason` is `overlap`,
`no_signal`, `self_echo` or `catch_up_none` on a row that made no task (task
#2396); see [the run ledger](routines.md#run-ledger).

### `routine_pause(routine_id: int, *, paused: bool = True) -> dict`

Pause or resume a routine definition in the caller's tenant.

### `routine_fire(routine_id: int, *, idempotency_key: str) -> dict`

Fire an API/webhook definition through authenticated MCP. The key is a stable
source event ID; a replay returns the same task. HTTP webhook ingress and
assignee wake-up remain later acceptance gates. A cron routine is refused
unless the call runs inside `routines.operator_run_now()`, which only the
Control Center's "Run now" route enters, for the verified human operator
(task #2480); there is no tool argument for it.

### ACP session settings on the HTTP/UI dispatch surface

Task #1742 adds no MCP tool or SDK mirror change. `POST /api/action/node-session`
with `action: "start"`, and `POST /api/node/dispatch`, accept optional `model`
(advertised native ID) and `permission_mode` (`supervised`, `auto-accept-edits`,
`full-access`). ACP requests default to supervised and must target a ua-node
advertising `session_settings`; native CLI requests reject permission modes.
The existing one-shot `node_dispatch` approval includes the resolved settings.
Responses retain `201` queued, `202` approval pending, and preflight `409` for
older nodes. Protocol refusal occurs on execution, before the first prompt,
and is evidenced as `settings_refused` with an error receipt.

`GET /api/nodes` now includes each node's `session_settings`, keyed by backend
or `acp:hermes:<profile>`, containing bounded `available_modes`/`available_models`
arrays, native `current_mode_id`/`current_model_id`, mapped `permission_mode`,
setter paths, `observed_at`, and provenance `source`. Observations include
`mode_control` (`negotiated`, `native_default_unclassified`, or `unclassified`),
`baseline_mode_id`, and `model_resolution` (`exact`, `catalog_alias`,
`native_default`, or `profile_default_unavailable`). Unclassified native defaults
never report an effective supervised permission class. Catalogs select the latest
eligible event separately per node/backend/profile, independent of fleet traffic.
This is the last observed advertisement, not a live capability grant. `controls` adds `modes` and
`model_switch` capability rows. `session_open` records requested and effective
settings separately; the durable session model comes only from the effective
native ID. [Provider mappings and limitations](harness-control-matrix.md#session-models-and-permission-modes-task-1742)
include the supervised-only native-default compatibility path, profile model
preferences, upgrade behavior, and resume/model-origin preservation.

### synthetic_workflow(task_id, run_id, client_key, label, recovery_fence=None)

`client_key` is 1-200 characters from `A-Za-z0-9_.:-`.

Disposable, synthetic-only governed acceptance surface, exposed to steward and
chief-of-staff sessions; hidden from the default developer/CTO profile. Requires
`UA_SYNTHETIC_WORKFLOW=1` and a regular file `UA_HOME/DISPOSABLE_SYNTHETIC_WORKFLOW`
containing exactly `disposable-synthetic-workflow\n`. Use an isolated temporary
`UA_HOME`, workspace, vault and dev root in a fresh isolated process; never mark
a production data home or toggle this mode in a running fleet hub. Canonicalize
the absolute home path first (macOS `/tmp`/`/var` aliases are refused). Require
`UA_STATE_SYNC=0`, `UA_EVENT_BUS=0`, `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`; approval push/webhook URLs must be unset. Synthetic
initialization skips inherited post-init drains/workers, private credential
loading and implicit workspace creation.
Linked database/provider files, sidecars and symlinked ancestor directories are
refused before storage connections, including wrapper activity writes. Scoped
session credentials are refused. WorkOS requests must match the storage
tenant; legacy requests require verified transport identity, never a supplied
actor. A task must belong to that tenant and the caller's active, unexpired run.

The first call queues a forced one-shot approval for the immutable task/run,
client key and bounded label. Denial blocks execution. On approval, the existing
registered `task_update` appends one idempotent generated note. Its own policy
and approval gates still apply. The effect engine commits intent and dispatch
markers, then delivers only to a fixed local SQLite synthetic provider. The task
mutation and effect intent are distinct durable boundaries: a failed intent may
leave the task note committed and require a fresh approval; it cannot resend an
uncertain effect. No destination, provider, credential or path can be supplied.

Matching repeats return the original effect result. `recovery_fence` performs
lookup-only reconciliation under that exact fence; it never sends. A dispatch
marker with no confirmed provider outcome stays indeterminate. A later known
result can upgrade no-effect to completed. Sending revalidates original principal, grant, nonce, expiry/revocation, active
task/run ownership, lease, cancellation, current tool visibility and pinned policy source/profile/live claims at each owner
boundary. Lookup-only factual recovery retains original principal, tenant, grant,
nonce, revocation and current-policy checks; natural expiry, cancellation, or a
later task run cannot erase an already-landed fact. It never sends or mutates the
newer task run.
A verified human can revoke unresolved consumed authority. Completed factual
reads retain current policy/tenant/principal checks without a fresh approval or
an obsolete task revision/lease requirement; they execute nothing.

The response joins task/run, task receipt key, approval ID, operation/fence,
effect result and durable event IDs. Mandatory cost is always
`{availability: "unknown", cost_usd: null}`; a separate task cost projection is
best-effort and reports unknown on failure. Existing run-budget hard stops
refuse new dispatch, warnings permit it, and factual reconciliation never sends.
This does not prove a real runner stops or reconcile provider billing.

The exact selected-writer semantic/producer proof and generated note are required;
a legacy native alias cannot prove this new workflow's mutation. Canonical gateway
ALLOW evidence joins the intent transaction and its durable proof is acknowledged
by the wrapper, including replay, without another mandatory post-commit append.
Evidence and unknown cost commit atomically with each effect state transition
in the existing event ledger, readable through `event_log_query` and task-scoped
`evidence_report`; no new memory authority or telemetry exporter is introduced.
The local synthetic provider has a separate durable commit to exercise the
crash window. This is not live fleet acceptance, credential rollout or a host
cutover. Focused reproduction: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run
pytest tests/test_synthetic_workflow.py -q`.
