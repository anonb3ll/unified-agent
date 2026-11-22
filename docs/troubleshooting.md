# Troubleshooting

## Venv portability

Python virtual environments hardcode absolute interpreter/library paths. If you move
`~/dev/unified_agent` (or copy it to a new machine at a different path), the existing
`.venv` will reference the old location and break. Delete it and rebuild:

```bash
rm -rf .venv
uv sync
```

Re-register the server in every host afterward — the registered command path
(`.venv/bin/unified-agent`) doesn't change, but the binary inside it does need
rebuilding.

## Desktop grows an empty `memory.db`

**Symptom:** a Desktop import, executor, or review cron creates
`~/.unified_agent/memory.db` (often empty / no `tasks` table), or
`storage.connect()` no longer raises `LiveStoreError` and the host starts
acting like a hub.

**Cause:** `sqlite3.connect` creates a missing file. On 2026-09-27 an Linux desktop
rebuild without `NO_LOCAL_MEMORY_DB` grew a fresh empty store on first
import. `storage.connect()` now fails closed when that marker is present
(task #804). `require_live_task_store()` already refused a missing or
non-SQLite file for review/CLI entry points (task #561).

**Fix:** do **not** keep the new file. Inspect it, then remove it. Recreate
the marker (`deploy/desktop-bootstrap.sh --apply` does this) and confirm
`unified-agent.service` is not installed. Shared state is the Mac hub. See
the operator desktop manual.

## Linux desktop CLI wrappers hang (`claude`, `gh`, `pi`, …)

**Symptom:** a CLI in `~/.local/bin` never returns. `ps` shows the same
binary stacked on itself.

**Cause:** `linux-desktop-mise-install` writes `exec mise x PKG -- BIN`. When
mise's exec PATH does not put the real binary ahead of `~/.local/bin`, `BIN`
resolves to the wrapper and loops (2026-09-27: `claude`, `copilot`, `gh`,
`opencode`, `pi`, `omp`).

**Fix:** `./deploy/desktop-bootstrap.sh --apply` rewrites only wrappers that
are byte-for-byte the generated form, keeping originals under
`~/.local/share/desktop-bootstrap/backups`. Customized wrappers are reported
as a gap for hand review, not rewritten.

## Desktop bootstrap reports `GAPS`

**Symptom:** `./deploy/desktop-bootstrap.sh` exits 1 with a table of `GAP`
rows.

**Cause:** the script is the desired-state checker for the Linux desktop client.
Typical remaining gaps need a human: hub unreachable (Tailscale), missing
device login (`unified-agent login --hub … --agent agent-agy`), missing
`~/.config/unified-agent/executor.env`, or a local listener on the hub port.

**Fix:** read the `GAP` column — each row names the step that closes it.
`--apply` only repairs non-secret drift. `UA_BOOTSTRAP_SKIP_SYSTEMD=1` is
for tests; do not set it on the live desktop.

## Absolute paths in host configs

Claude Code launches MCP servers **without a shell**, so `~` and `$HOME` are not
expanded in `claude mcp add` commands or in `~/.claude.json`. Always use the fully
expanded absolute path:

```bash
# wrong: ~ is passed through literally, the binary "~/dev/unified_agent/.venv/bin/unified-agent" won't be found
claude mcp add --scope user unified-agent -- ~/dev/unified_agent/.venv/bin/unified-agent

# right
claude mcp add --scope user unified-agent -- /home/you/dev/unified_agent/.venv/bin/unified-agent
```

The same applies to `env` values passed via `--env UA_WORKSPACE=...` and to
Antigravity's `~/.gemini/config/mcp_config.json` `command`/`env` fields.

## A tool is missing from `tools/list`

**Symptom:** `tools/list` (or the Control Center) does not show a tool the
docs name, or a kernel-only / hosted probe process has far fewer than the
full hub surface.

**Cause:** two different filters, often confused.

1. `UA_COMPONENTS` (task #2603) decides which components the process **loads**.
   Unset or blank loads all sixteen. `UA_COMPONENTS=kernel` loads kernel plus
   the MCP adapter only, so Control Center, dispatch, GitHub, and the rest
   are not registered. An unknown name raises `ValueError` at start-up; it
   never serves a silently smaller surface. `depends_on` is not expanded.
2. `UA_TOOLSETS` only filters which tools `tools/list` **shows**. It does not
   unload modules and does not block a call to a registered tool.

A kernel-only HTTP hub still answers `/healthz` and `/readyz` from the
adapter, so a passing health check does not prove the full tool surface is
loaded.

**Fix:** unset `UA_COMPONENTS` for a full hub. Use `UA_COMPONENTS=kernel` only
when you intend a kernel-only process (CI probes, split deploys). Check
`UA_TOOLSETS` separately if `tools/list` is a subset of a process that did
load the component. See [configuration.md](configuration.md) and
[component map](architecture/components.md#loading-ua_components-task-2603).

## JSON config corruption

If a host reports the MCP server failing to load or simply not showing up, check that
its config file is valid JSON:

- Claude Code: `~/.claude.json` (user scope) or the project's `.mcp.json`.
- Antigravity: `~/.gemini/config/mcp_config.json`.

A stray character breaks the whole file silently — on macOS, an accidental
`Option + s` keypress can append a trailing `ß` outside the closing braces, which is easy
to miss visually. Validate with:

```bash
python3 -m json.tool ~/.claude.json > /dev/null
```

Host logs (e.g. `~/Library/Logs/Antigravity/language_server.log` on macOS for
Antigravity) will show a JSON parse error if this is the cause.

## Stale stdio snapshots after merges / tool changes

Each host reads the tool list from the MCP server **once**, at connection time
(`server.py`'s tool tuple is only evaluated when the process starts). If you:

- merge a branch that changes `src/unified_agent/tools/*.py` or `server.py`, or
- pull an update to the repo,

the running server process (stdio, spawned per-session, or the hub's long-lived HTTP
process) is still serving the old code until it's restarted:

```bash
# stdio: just start a new session in the host (it spawns a fresh process)
# hub (HTTP) mode:
systemctl --user restart unified-agent
```

Then **reconnect every host** pointed at it — an already-open Claude Code / Antigravity
session keeps its stale tool snapshot from the initial handshake even after the server
process restarts underneath it. See `AGENTS.md` § Registration and
[installation.md](installation.md#hub-mode-shared--remote) for the full restart-and-reconnect
sequence.

## HTTP MCP client: `Session not found` / 404 after idle

**Symptom:** a 24/7 streamable-HTTP client (Hermes gateway, long-lived
MCP host) reports `MCPError: Session not found` or `Session termination
failed: 404` after 20–90 minutes of quiet.

**Cause:** the MCP SDK's `StreamableHTTPSessionManager` reaps a stateful
session after an idle period with no request in flight. Unset, that is
the SDK default of 1800s (30 minutes). A client that does not
re-initialize then hits a closed session.

**Fix:** set `UA_MCP_SESSION_IDLE_TIMEOUT_S` on the hub (task #1298) and
restart. A positive integer is seconds. `0` disables the idle timeout
(`config.mcp_session_idle_timeout_s()` returns `None`, which is the SDK's
real disable value — a literal `0` would raise at startup). Leave the
variable unset to keep the SDK default. Negative or non-numeric values
fail startup with `ValueError`. After a timeout the client must
re-initialize; raising the knob does not revive an already-reaped
session. See [configuration.md](configuration.md).

## First-run sandbox image pull vs. the 120 s cap

When `UA_SANDBOX_ENABLED=1` and the configured `UA_SANDBOX_IMAGE` hasn't been pulled
locally yet, `docker run`/`podman run` pulls it before executing the command. That pull
counts against `run_cmd`'s timeout (capped at 120 s regardless of the `timeout_s`
argument — see [configuration.md](configuration.md#hard-limits-not-configurable)), so a
large image on a slow connection can cause the very first sandboxed command to time out
even though the command itself would have finished instantly. Pre-pull the image once
outside `run_cmd`:

```bash
docker pull python:3.11-slim   # or your custom UA_SANDBOX_IMAGE
```

Also remember the default image (`python:3.11-slim`) has no `uv`, `pytest`, or `ruff` —
allowlisted commands relying on them need a custom image (`UA_SANDBOX_IMAGE`) built with
those tools installed.

## `run_cmd` refuses `cat .env` or parks `git clone --config`

**Symptom:** `run_cmd("cat service.env")` or `grep`/`head` of a `*.pem` / `id_rsa`
path raises that the path looks secret-bearing (`secret-path` in
`run_cmd_check`). `run_cmd("git foo")` or `git clone --config alias.x=!id …`
parks in the approval queue even though `git` is on the allowlist.
`uniq in .git/config` does the same.

**Cause:** task #1423 closed three bypasses. Content readers (`cat`, `grep`,
`head`, `tail`, `diff`, `uniq`, `sort`, `sed`, `awk`, `wc`) refuse the same
secret-bearing filenames `fs_read` refuses. A git subcommand that is not a
built-in may be `alias.<name>=!cmd` from a config the agent wrote, so it
forces approval. `clone`/`init --config` is checked like `-c`. A write into
`.git`, `.hg` or `.husky` (not only `hooks/`) is `allowlist-bypass`. `uniq IN
OUT` treats `OUT` as that write. Every git call also runs with
`safe.bareRepository=explicit`, so a bare repo laid out with `fs_write` is
not discovered.

**Fix:** read secrets with the managed-secret tools, not `run_cmd`. Use a
built-in git subcommand (`status`, `log`, `diff`). Do not point `--git-dir`
or `--work-tree` at a tree the agent created. `run_cmd_check` reports the
same codes without executing. See [security.md](security.md#shell-allowlist-and-no-shell-execution).

## `fastembed` lazy-load behavior

The embedding model is loaded lazily on first use (`embeddings.get_embedding_model()`),
not at server startup — this keeps `unified-agent`'s process start fast and avoids
blocking the MCP handshake on a model download. Consequences:

- The **first** call that needs an embedding (a `memory_save`, or the background
  backfill thread `storage.py` kicks off at `init_db()`) will be noticeably slower while
  the model downloads/loads, and will fail silently (falling back to FTS-only search, or
  skipping the embedding for that note) if there's no network and no cached model.
- Every embedding call (`memory_save`, `memory_search`, `fs_index_symbols`'s background
  backfill, `fs_search_symbols(semantic=True)`) is wrapped in a broad
  `try/except Exception: pass` specifically so a missing/broken `fastembed` install
  degrades gracefully instead of breaking the primary (non-semantic) code path. If
  semantic search never seems to return results, check whether the model ever
  successfully loaded (no exposed tool surfaces this directly — check host stderr/logs
  for the exception `fastembed` would otherwise raise).

## `http` vs `stdio` transport mismatches

Symptoms and their likely cause:

| Symptom | Likely cause |
|---|---|
| Host reports connection refused / can't reach the server | Server was started with `UA_TRANSPORT=http` but the host is configured for stdio (spawns the process expecting stdio framing), or vice versa — a host configured with `--transport http` pointed at a stdio-only process. |
| HTTP 421 from the hub | `UA_HTTP_ALLOWED_HOSTS` is unset and a non-localhost client (Tailscale IP/MagicDNS name) is connecting — the MCP SDK's DNS-rebinding protection only accepts `localhost`-shaped Host headers by default. Set `UA_HTTP_ALLOWED_HOSTS` to every hostname/IP clients will actually dial (see the systemd unit template, `deploy/unified-agent.service`, for the exact env line to uncomment). |
| Antigravity never connects in hub mode | Antigravity's HTTP config key must be `serverUrl`, not `url`/`httpUrl` — a wrong key name is silently ignored rather than erroring. |
| Tools "work" but state doesn't show up in the other IDE | Each host is pointed at a **separate stdio process** (its own `UA_HOME`/db), rather than both pointed at one hub. Confirm both hosts' MCP config target the same `UA_HOME` (stdio) or the same hub URL (HTTP) — see [installation.md](installation.md) for the registration patterns. |
| `unified-agent-tui` connects remotely when a local run was expected (or vice versa) | `tui.detect_remote_url()` checks, in order: `UA_TUI_REMOTE` env var, then `UA_TRANSPORT=http`, then `~/.claude.json`'s `unified-agent` entry for `type: "http"` + `url`. Any of these being set unexpectedly redirects the TUI to a remote hub. |

## Hermes MCP tools silently missing

**Symptom:** hermes-agent's MCP tools vanish — no error, no crash, the agent just
behaves as if it never had an MCP integration.

**Cause:** the hermes venv (`~/.hermes/hermes-agent/venv`) was rebuilt without the
`[mcp]` optional extra (e.g. a plain `pip install -e .`). hermes fails **open** when
the `mcp` package is missing: it silently disables the integration instead of raising,
so nothing surfaces the problem at startup.

**Detection:** the `hermes_env` rule in `check_anomalies` flags this state. It checks
the hermes install at `UA_ANOMALY_HERMES_DIR` (defaulting to the standard
`~/.hermes/hermes-agent` location) and reports when the venv exists but `mcp` is not
importable from it.

**Fix:** rebuild the venv with the wrapper instead of by hand:

```bash
./scripts/hermes-venv-rebuild.sh            # default: ~/.hermes/hermes-agent
./scripts/hermes-venv-rebuild.sh /path/to/hermes-agent
```

The script removes the venv only after sanity checks pass, reinstalls the project
editable **with** the `[mcp]` extra, and verifies `import mcp` succeeds before
declaring success — so a rebuild that would reintroduce the failure exits nonzero
instead of failing open.

## Hermes reports unified-agent unreachable after three tool errors

**Symptom:** agent-hermes (or any host using hermes-agent's native MCP client)
calls a unified-agent tool that fails application validation — typically
`delegate` rejected by the Delegation Spec Standard. After three such failures,
the next call (including a *valid* `delegate`, or an unrelated tool on the same
server) returns:

```text
MCP server 'unified-agent' is unreachable after 3 consecutive failures.
Auto-retry available in ~Ns. Do NOT retry this tool yet.
```

Read-only tools on other servers still work. The hub `/healthz` stays `{"ok":true}`.
A new Hermes session, or waiting out the 60 s cooldown, restores the same hub.

**Layer:** client circuit breaker, not hub death. hermes-agent
`tools/mcp_tool.py` `_make_tool_handler` counts a completed `CallToolResult` with
`isError=true` (MCPServer's mapping of `ToolError`) as a consecutive *server*
failure because it wraps that result in `tool_error()` JSON (`{"error": "..."}`)
and then runs `if "error" in parsed: _bump_server_error(...)`. Threshold is 3
(`_CIRCUIT_BREAKER_THRESHOLD`); the unreachable copy is minted locally. The hub
session is still open. Confirmed 2026-08-20 against hermes-agent at
`~/.hermes/hermes-agent` (task #487). Journal 2026-08-09 already described the
same sequence.

This is **not** the executor daemon breaker (`UA_EXECUTOR_CIRCUIT_BREAKER_*`).
That trips worker dispatch, not MCP `tools/call`.

**Do not:** disable spec enforcement (`UA_DELEGATION_SPEC_ENFORCE=0`), raise
retry limits, or restart the hub to "clear" it. Those mask a host bug and
weaken gates.

**Client fix (hermes-agent `tools/mcp_tool.py`, `_make_tool_handler`):** on any
completed RPC — including `isError` application errors — call
`_reset_server_error(server_name)` and return. Keep `_bump_server_error` for
transport failures only: server not connected, dead session, and exceptions from
the RPC. Regression: three `CallToolResult.isError` responses, then a successful
call, must hit the session and must not contain `unreachable`. Transport
`RuntimeError` still trips the breaker. See
`tests/tools/test_mcp_circuit_breaker.py` in hermes-agent and
`scripts/repro_hermes_mcp_breaker.py` in this repo.

**Operator recovery until the host is patched:** wait ~60 s, start a new Hermes
session, or reconnect the MCP server. unified-agent itself does not need a
restart.

## Review pipeline cannot find a worktree

**Symptom:** `unified-agent-review` or `git_merge_task` reports no matching
worktree for a project-scoped task, even though `.worktrees/task-<id>` exists
under a `~/dev/<project>` checkout.

**Cause:** the worktree is registered against the **project** git repo, not the
cockpit. Lookup (`merge.find_task_worktree`) searches `UA_WORKSPACE` first, then
each existing path in `config.project_repo_roots()`. A miss means the project
slug is unknown, the checkout directory is missing on this host, or the branch
name does not contain the task id.

**Fix:** confirm the task notes carry `project: <slug>` with a slug from
`unified-agent`, `browser`, `sample-store`, `client-portal`, and that
`$UA_DEV_ROOT/<dirname>` exists (`sample-store` maps to `sample-store`,
`unified-agent` to `unified_agent`, `client-portal` to `tap`). Do not point
`git_worktree_list` at the project path from an agent session — agent-facing
`git_*` tools stay inside `UA_WORKSPACE`.

If the worktree lives on another host and the branch is on origin, name it in
the notes (`branch: <name>`), or put the open PR in `evidence_ref` (task
#2321), instead of expecting the hub to find a local checkout. The scan then HOLDs for the merge decision and does **not** run
audit/pytest (UA #736). A rejection that still says `no git worktree found`
means the origin check missed: wrong name, not pushed, `ls-remote` failed, or
the name failed the branch check (`_is_safe_branch` / `_BRANCH_NOTE_RE`;
leading `-` refused, notes names cannot contain `+` or `@`). See
[automation.md](automation.md#policy-validate--hold).

## Remote executor refuses a run: worktree could not be localized

**Symptom:** a desktop executor claims a Git task from the Mac hub, then
fails immediately. The task note reads
`[executor provisioning failed: canonical run worktree could not be localized
safely]` (or `hub did not bind an isolated canonical checkout`). No child
starts. The executor log has
`no local git repo found above … below …; failing closed`.

**Cause:** task #2288. The hub records an absolute worktree on the hub host.
The remote daemon re-roots that path at this host's `~/dev`
(`Path.home() / "dev"` — `UA_DEV_ROOT` does not change it) and looks for the
project checkout **strictly below** `~/dev`. It will not walk into an
enclosing repository above that bound (a dotfile repo in `~`, or `~/dev`
itself if that directory is a git repo). A `..` after the `dev` segment, a
`~/dev/<repo>` symlink that aliases `~`, or a `.git` symlink that points
outside `~/dev` is the same refusal. A missing `~/dev/<repo>` clone is the
usual case.

**Fix:** clone the project under `~/dev/<repo>` on the executor host (same
directory name the hub path uses after `dev`), confirm `origin` names that
repo, then retry the delegation. Do not point the executor at a checkout
outside `~/dev` or expect `UA_DEV_ROOT` to move the bound. Details:
[automation.md — Remote worktree localization](automation.md#active-executor-backends-native-headless-persona).

## Shipping preflight holds a red-tier head that moved

**Symptom:** `python -m unified_agent.shipping_preflight_cli` exits non-zero
with `The head moved from the recorded read at <sha> to <sha> and changed
red-tier … with no delta read recorded for this head`, or `Recorded reads
exist for this pull request, but none is an ancestor`.

**Cause:** task #2259. After a steward, policy, or human read was recorded
for an earlier head, later commits added a red-tier path and no `delta-read`
row names those files at the live head. A rebase that left every recorded
read behind is the same class: GitHub's compare cannot prove a delta against
a base that is no longer an ancestor. A truncated compare (300 files) or an
"ahead" answer with no file list also refuses rather than passing as clean.
An unread PR is **not** this symptom: that is `review_head.status:
no-read-recorded` and the Ledger approval remains the authority.

**Fix:** record a read of the live head, or a delta read from the last
full read that lists every red-tier path the delta added (`unified-agent-review
record-read`), then rerun preflight immediately before
`gh pr merge --squash --match-head-commit`. A hold naming a delta read with
`no full read is recorded` means its `from_sha` has no full read: record a
full read instead. A Ledger approval is not a read and does not clear this
hold. See
[automation.md](automation.md#shipping-preflight-head-moved-red-tier-hold-2259).

## Review pipeline holds a PR as `awaiting-ci`

**Symptom:** `unified-agent-review scan` appends
`[review: awaiting-ci @ …] at <sha>` and runs no pytest. The task stays in
review until a later scan.

**Cause:** exact-head CI shards are missing, and the `direct` import-graph
selection (`affected_tests.select`, task #2231) either mapped nothing, asked
for the full suite, or selected more than `UA_REVIEW_TARGETED_MAX_FILES` (40)
files. A src change that is only covered by tests that walk the source tree as
text, or only by tests whose imports cannot be read statically, maps nothing.
`pyproject.toml`, `uv.lock`, `tests/conftest.py`, and a file no test names all
need the full suite. The hub does not run that.

**Fix:** dispatch the full suite on that branch
(`gh workflow run ci.yml --ref <branch>`), or leave it for a merge-queue run
that the selector cannot narrow (`ALL`). The reviewer's note still says
`label the PR ci:full`; that label is not a workflow trigger. Do not
treat `scripts/local-gate.sh --affected` as a substitute: that uses depth
`transitive`, which is broader than review, and skips pytest when the selector
prints `ALL`. Preview the review's set with
`uv run python scripts/affected_tests.py --depth direct --explain`. A green
batch run does not clear `awaiting-ci` by itself — it reports on the batch
head, not the PR head. See [ci-batch-verify.md](ci-batch-verify.md#the-review-gate-without-per-pr-ci).

## PR already merged, task still `waiting_review`

**Symptom:** GitHub shows the PR merged, but the hub task stays `doing` /
`waiting_review`. The next `unified-agent-review scan` then rejects with
`no git worktree found` because the worktree was already cleaned.

**Cause:** reconcile used to require a live `[waiting-for: review]` marker
and a PR URL in the notes. Cursor/handoff completions often write the PR
only to `evidence_ref` and never append the marker (UA #851 / #868). The
validation scan still requires the live marker; only reconcile is wider.

**Fix:** you usually do not need to backfill a review marker. The next scan
reconciles a markerless `doing` / `waiting_review` row when `gh` can confirm
the PR is `MERGED`, resolving the ref from **pipeline marker lines** in
notes (`[review:…]` / `[pr:…]` only — a prose `PR: <url>` is ignored, task
#986), then `evidence_ref`, then a unique `gh pr list --search "UA #<id>"`
title match. It will **not** close the task when:

- an `in_flight` delegation still owns the row (live-task guard, task #986)
- `gh` is missing from the cron PATH
- the title search returns zero or several PRs
- the PR is not actually `MERGED`
- the task is `[no-code]`

Put the URL on a `[pr: …]` / `[review: …]` line, or in `evidence_ref`, only
when those lookups cannot see the PR. Details:
[automation.md — The reconcile step](automation.md#the-reconcile-step).

## Desktop executor ignores a killed cycle, or will not start work

**Symptom:** `autonomous_cycle_kill` ran on the Mac hub, but a Desktop
executor keeps claiming the bound task — or, conversely, every delegated
task fails with `cycle gate unverifiable` / `blocked_by_cycle_gate_unverifiable`.

**Cause:** after cutover the Desktop must not own `memory.db`
(`NO_LOCAL_MEMORY_DB`, task #804). Local `active_cycle_for_task` therefore
cannot see hub cycles. The daemon falls back to
`autonomous_cycle_manage(action="get_for_task")` (task #992). If the hub
URL / token is wrong, that fallback raises and the daemon **refuses**
execution rather than treating the unread gate as "no cycle."

**Fix:** confirm the Desktop executor reaches
`http://<hub-host>:8765/mcp` with a valid
bearer token. A killed or blocked cycle should terminal-fail the
delegation (`blocked_by_cycle`), not bounce it to `pending`. Rehearse with
`scripts/kill_switch_rehearsal.py`. Topology:
mac-hub-topology.md.

## PR-watch writes nothing, or floods, or fails under launchd

**Symptom:** `unified-agent-pr-watch` prints `first run — baseline N PR(s)
recorded, no events`, or launchd's `pr-watch.err` says `gh failed`.

**Cause:** a first sighting of each repo seeds `$UA_HOME/pr-watch-cursor.json`
and emits no events by design. Later ticks only write on `opened` /
`reviewed` / `merged` / `closed`. launchd's default PATH has no Homebrew, so
`gh` is missing unless the wrapper prepends `/opt/homebrew/bin`
(`run-pr-watch.sh`, UA #738).

**Fix:** run `--dry-run` first to see pending events without advancing the
cursor. Confirm `gh auth status` as the same user the agent will use. Do not
delete the cursor to "force" history — that re-baselines silently, it does
not replay. `system_doctor` does not probe this timer; read
`~/.unified_agent/pr-watch.log`.

## Lane B report shows `unavailable` instead of `0`

**Symptom:** `python -m unified_agent.lane_b_telemetry` has
`"status": "unavailable"` and `"count": null` for a gate.

**Cause:** a reachable empty source is count `0` with `"status": "ok"`.
`unavailable` means the source could not be read: GitHub traffic/clones HTTP
error (needs a token with the traffic scope), missing vault root, or a hub
DB error. Totals also become unavailable if **any** repo's component of that
gate is unavailable.

**Fix:** set `GITHUB_TOKEN` or `GH_TOKEN` for clone uniques; pass `--vault`
or `UA_VAULT_PATH` for vault-backed gates. Do not treat `unavailable` as
zero progress — that is the failure the report exists to prevent.

## "Unit tests failed" vs "No tests were collected"

Validation gates (`merge.run_validation_gates`) run `python -m pytest` inside
the worktree. Pytest exit code **1** is `Unit tests failed`. Exit code **5** is
`No tests were collected` — often a worktree with no `tests/` directory, or
pytest run from the wrong tree. Both fail the gate unless `force=True`. A
cockpit-only checkout with no product tests is the usual source of exit 5.

## Research profile cannot run commands or escalate

**Symptom:** `run_cmd`, `fs_write`, `task_update`, `delegate`, or
`profile_activate` raises a profile-denied error after `profile_activate("research")`.

**Cause:** research is an allow list. Those tools are intentionally absent.
Research also cannot call `profile_activate` to switch to developer or
chief-of-staff.

**Fix:** `profile_reset` returns the session to the identity default. Research
is for reading, searching, planning, and Inbox/Logs notes. It is not a coding
profile. Stdio sessions never enter research unless you are on authenticated
HTTP.

## Mac hub executor reports DORMANT

**Symptom:** Control Center Executor Health or `system_doctor`'s
`active_executor_service` is DORMANT on a Mac hub even though
`com.unified-agent.executor-agy` is running.

**Cause (fixed, task #542):** the probe used `systemctl --user show
unified-agent-executor.service`. Darwin has no such unit. The probe now uses
`launchctl print gui/$UID/com.unified-agent.executor-agy`.

**Still true:** Codex is a separate LaunchAgent
(`com.unified-agent.executor-codex`) and is **not** in `MAC_LAUNCHD_LABELS`.
Doctor mapping `unified-agent-executor.service` → Agy only. Check Codex with
`launchctl print gui/$(id -u)/com.unified-agent.executor-codex`.

## Control Center delegations look failed for Desktop_Person

**Symptom:** Runs → Executor (`/ui/runs/executor`) shows a worktree survey error
(or used to fail the whole page) when logged in as the operator, even though
`delegate_list` succeeds.

**Cause:** `Desktop_Person` is `UA_AGENT_WORKSPACES` DENY. `workspace_root()`
raises for agent-facing file tools. The census now uses
`process_workspace_root()`. Remaining survey failures land in `worktree_errors`
and render on the worktree table only.

**Do not:** remove the DENY binding so the page can scan. The binding is the
sandbox. Cleanup still honors DENY.

## MCP gateway doctor says a secret is missing for a DENY login

**Symptom:** a Control Center or other `UA_AGENT_WORKSPACES` DENY identity
calls `mcp_gateway` and gets `secret_unavailable`, or `system_doctor`'s
`mcp_gateway` row looks MISCONFIGURED for that caller even though the
managed secret is stored.

**Cause (fixed, task #2357):** the gateway used to read its hub-held
downstream bearer through the caller's workspace binding. DENY raises
before the store opens, so the doctor treated a present secret as missing.
The gateway now lifts that binding only for its own store read. The secret
still never reaches the caller. Every other consumer of the store keeps the
DENY refusal.

**Still true:** DENY identities cannot use `fs_*` / `run_cmd`. Do not widen
the workspace to make the gateway work.

## Two agents keep bouncing short replies

**Symptom:** `message_send` raises `N short messages in a row between A and B`
or `reply chain reached hop N (limit 16)`, and the later reply is not stored.

**Cause:** loop guards (task #2387) apply between agents. A reply's hop is
one past its parent (the caller's `hop` is never trusted downward). The
default hop cap is 16 (`UA_MESSAGE_MAX_HOPS`). The fourth short reply in a
row (under 200 characters) between one pair is refused
(`UA_MESSAGE_PAIR_STREAK_MAX`, default 4; `0` disables). The last allowed
short reply returns `loop_warning`. Mail with the human operator is exempt.

**Fix:** do the work and send a longer result, drop `reply_to` for new work,
or stop. Do not raise the hop cap to keep a ping-pong going. See
`message_send` in [tools-reference.md](tools-reference.md) and
[configuration.md](configuration.md) (`UA_MESSAGE_MAX_HOPS`,
`UA_MESSAGE_PAIR_STREAK_MAX`).

## Dispatch Claude cannot self-report via the scheduler CLI

**Symptom:** an unattended Claude dispatch run stays `launched`/`running`, or
the child reports that `unified-agent-dispatch-scheduler report` cannot open
`~/.unified_agent/memory.db`.

**Cause:** Claude spawn writes a fail-closed sandbox settings file that denies
`~/.unified_agent` (task #623). Skip-permissions is attached only *inside*
that sandbox. The CLI report path needs the hub database; the MCP tool
`dispatch_report_self` does not.

**Fix:** the child must call `dispatch_report_self` with the `run_id` from the
dispatch prompt or `$UA_HOME/dispatch-runs/<run_id>.ua-dispatch.json`.
`finished` requires non-empty evidence. The caller identity must match the
run's `agent_label`. Do not set `UA_CLAUDE_SKIP_PERMISSIONS=1` to "fix"
this — dispatch spawn rejects that global flag.

## Steward `task_update` refused on another agent's claim

**Symptom:** a steward session calling `task_update` gets
`task N is claimed by 'Desktop_X' — steward may not update another agent's
claimed task`.

**Cause:** task #591 exposes `task_update` to steward so findings can be
closed, reparented, or annotated, but the tool compares
`config.verified_agent_identity()` to the row's `claimed_by`. Another agent's
live claim is refused. `task_claim` remains denied for steward.

**Fix:** groom unclaimed or self-claimed tasks. Ask the claimant to update
theirs, or use a developer / chief-of-staff session. Do not treat a
`claimed-by:` note line as ownership — the `claimed_by` column is
authoritative.

## Idle LaunchAgent timer looks DORMANT between firings

**Symptom:** `system_doctor` reports a Mac periodic job (StartInterval /
`run interval`) as inactive even though its last run exited 0.

**Cause (fixed, task #315):** `launchctl print` shows `state = not running`
between firings. That is expected for a timer, not a crashed daemon. The
Darwin probe now treats a periodic job as `active` when `last exit code = 0`.

**Still true:** a periodic job that has never run, or whose last exit is
non-zero, stays `inactive`. Persistent daemons still use `state = running`.

## Slack ingress returns 400 or 401

**Symptom:** Slack Events API or a slash command gets HTTP 401/400 from
`POST /api/slack/events`.

**Cause:** 401 is an HMAC/signature failure (`SLACK_SIGNING_SECRET`, replay
window, or unsigned body). 400 is adapter validation: empty allowlist, unknown
channel, body over 64 KiB (including a `Content-Length` that already exceeds
it), or invalid command text. The route does not use hub bearer tokens or CSRF.

**Fix:** confirm `UA_SLACK_ENABLED=1`, the signing secret, `UA_SLACK_USERS`, and
`UA_SLACK_CHANNEL_MAP` on the process that serves the route. A 500 with
`{"error":"Slack request could not be processed"}` is an unexpected failure;
the hub logs the exception type, not the Slack payload. See
[slack-coordination.md](slack-coordination.md).

## `task_claim` or `task_update` raises "WIP cap"

**Symptom:** `task_claim`, or `task_update(status="doing")` on a task not
already in `doing`, fails with `WIP cap: '<agent>' already holds N doing
task(s)`, `WIP cap: lane '…' already has N doing task(s) (mode hard)`, or
`WIP cap: fleet already has N live doing task(s)[, U with no claimant] (soft cap S, hard cap H)`.

**Cause:** `UA_AGENT_WIP_MAX` (default 20, or the agent's
`UA_AGENT_WIP_OVERRIDES` value; the message shows the effective `max`) and optional `UA_LANE_WIP`
(`lane:max:mode`) count `doing` rows whose lock has not expired. Soft lane
mode still claims and returns `wip_warnings`; hard mode and the per-agent cap
refuse. A `laptop` (or other non-default) task also needs
`expected_lane` matching that lane — omitting it looks like a claim-scope
error, not a WIP error. The fleet message comes from `UA_FLEET_WIP_HARD_MAX`
(default 3 × `UA_FLEET_WIP_MAX`), which counts live claims across all agents
plus `doing` rows with no claimant written within the last hour;
`observability_manage(action="runs_in_flight")` shows the count under
`wip.fleet` and which path each run came through. A `high`-priority or
`[urgency: now]` task is never refused by the fleet cap.

**Fix:** finish or cancel in-flight work, wait for lock expiry (hygiene will
also release expired claims on the next `unified-agent-review scan`), or raise
the cap in the hub environment and restart. See
[memory-tasks-vault.md](memory-tasks-vault.md#queue-lanes-and-wip-caps-task-705-actor-model-883).

## Review pipeline picks up a spec that is not ready

**Symptom:** an unclaimed `open` task whose notes mention `[waiting-for: review]`
enters `unified-agent-review scan`.

**Cause:** the live marker is `[waiting-for: review]` not followed by
` when done`. Boundary templates must use the `when done` suffix so
`reviewer._is_open_boundary_review_spec` skips them (task #709).
`[waiting-for: review] once shipped` is **not** skipped by scan/reconcile —
that qualifier only clears the compact `waiting_for_review` boolean.
A marker quoted as prose inside backticks
(`` `[waiting-for: review]` ``) is ignored by the review scanners only:
`monitor.py`'s `review_wait` rule requires the token *not* to be
backtick-wrapped (`(?<!`)…(?!`)`), and `reviewer.py` blanks backtick- and
double-quoted spans before scanning, so documenting the annotation does not
enqueue the task. Other readers still see it: the compact
`waiting_for_review` flag in `task_list` (`tools/tasks.py`) and executor
finish stamping (`delivery.py`) match the plain token, quoted or not.

**Fix:** write `[waiting-for: review] when done` in spec/checklist notes.
Only append the live (non-backticked) marker after the branch is actually
ready. When describing the rule in notes, quote it in backticks.

## `uv sync --extra langgraph` fails

**Symptom:** `uv` reports that extra `langgraph` is not defined.

**Cause:** LangGraph is a core dependency (`pyproject.toml`). The optional
extra was removed when the MCP tools registered on the live hub. CI runs
`uv sync` with no extra.

**Fix:** `uv sync` then `uv run pytest -q tests/test_langgraph_pilot.py`.

## Restarting the executor does nothing / returns approval_pending

**Symptom:** Restarting the executor on Agents → Hosts (`/ui/agents/hosts`) returns `approval_pending`,
`remote host control is not available`, or `unit … is not on the restart allowlist`.

**Cause:** `POST /api/action/fleet` is a prod action (`restart`). It needs a
verified identity scoped in `UA_AGENT_SCOPES` (when that variable is set), a
one-shot approval, a **local** registered host, and an allowlisted unit
(`unified-agent.service`, `unified-agent-executor.service`,
`unified-agent-zulip-worker.service`, or the matching `com.unified-agent.*`
launchd labels). Remote hosts and stub actions (`reap_executor`,
`release_worktree`, `reload_registration`) are denied.

**Fix:** Approve the pending `fleet_host_action` row, restart only the hub
host, and use an allowlisted unit. See
[Web control center](web-ui.md#fleet-restart-and-device-sign-in-revoke-tasks-799-1321).

## Evidence CLI or `/api/evidence` is 403

**Symptom:** `unified-agent evidence` or Settings → Audit reports the feature
is part of the Attribution & Evidence Layer.

**Cause:** `UA_EDITION=core` gates `evidence_reporting`, `audit_hash_chain`,
and `audit_reanchor`.

**Fix:** Leave the default (`enterprise`) or set `UA_EDITION=enterprise` and
restart the hub. Core is intentional when you want the free local hub only.

## PDF evidence export returns 422 or 503

**Symptom:** `GET /api/evidence?format=pdf` is 422 (`cannot export PDF: …`) or
503 (`PDF export requires the optional 'pdf' extra`).

**Cause:** PDF is hub-only and fail-closed. Empty record sets and
`chain_integrity.status=tampered` raise `ValueError` (422) so the file cannot
self-certify a false chain. Missing `fpdf2` raises `RuntimeError` (503).

**Fix:** Use `format=json` / `markdown` / `csv` for empty or tampered reports.
Install the extra with `uv sync --extra pdf` (or `uv sync --all-extras`) on
the hub venv. The CLI has no `--pdf` flag. See
[Evidence report](evidence-report.md#query-parameters).

## Mac hub ignores new `run-hub.sh` environment pins

**Symptom:** A pin in `deploy/mac-hub/run-hub.sh` (PATH, iMessage bridge,
forced `UA_ZULIP_CHAT=0`) does not appear in the live hub process.

**Cause:** `com.unified-agent.hub`'s `ProgramArguments:0` still points at a
duplicated `~/.unified_agent/run-hub-live-<sha>.sh` that does not `exec` the
repo script (task #1013).

**Fix:** On the Mac, as the LaunchAgent owner:

```bash
./deploy/mac-hub/install-hub-launcher.sh
launchctl kickstart -k "gui/$(id -u)/com.unified-agent.hub"
```

The installer does not restart the hub and does not edit the plist. See
Mac hub topology.

## Vault `state_*.md` files look truncated

**Symptom:** `Logs/state_memory.<host>.md` or `state_tasks.<host>.md` starts
(after its frontmatter) and ends with `[Truncated by unified-agent: 1500KB cap …]`.

**Cause:** `#408` caps Markdown mirrors at 1.5 MB so Obsidian's renderer does
not OOM. The memory mirror keeps the most recently updated notes; the tasks
mirror keeps its directory table and as much per-task detail as fits. The
notice names the full `.txt` JSON mirror, which is never capped and is the
only form code reads (`state_sync_verify`). Before task #1920 a capped file
also lost its frontmatter to a duplicated first line.

**Fix:** Read the matching `.txt` JSON mirror, or query `memory.db`. Do not
treat either file as a backup — use `unified-agent db backup`.

## Session capture returns 502

**Symptom:** Claude Code SessionEnd hook or `POST /api/session-capture` returns
`502` with a `route` list.

**Cause:** Every `llm_client` leg (headless Claude CLI → OpenRouter, with the
opt-in subscription pool between them when enabled) failed. The hub does not
store an empty note.

**Fix:** Read each leg's `reason` in the `route`. `claude_cli` with
`claude binary missing` needs the CLI installed (or `UA_CLAUDE_CLI_BIN`),
`unsupported CLI version` means `claude --help` does not list `--safe-mode`
and `--permission-prompts` (update the CLI),
`claude_cli_not_subscription` means `claude auth status` does not show a
claude.ai subscription login (log in with the subscription, not a Console or
API-key login), `auth rejected` needs `claude` logged in again as the hub's
user, and `model mismatch` means the models the CLI reported do not include
the exact `UA_CLAUDE_CLI_MODEL` id (default `claude-haiku-5-5`); `openrouter`
with `OPENROUTER_API_KEY unset` needs the key, and `model not found` means the
pinned `UA_OPENROUTER_MODEL` (default `deepseek/deepseek-v4.1-flash`) is no
longer listed. `model mismatch` on `openrouter` or `subscription_pool`
means the reply's `model` field named a model other than the requested one
(its dated snapshot `<id>-YYYYMMDD` is accepted). The usage ledger row for
that call shows the model the provider reported. A replay of the same
transcript tail is a no-op (idempotency key on the SHA-256).

## `task_update` refuses `status=done`

**Symptom:** `ToolError: task completion requires an attributed actor`.

**Cause:** Completion fails closed when `claimed_by` is empty and the caller
has no verified identity or explicit actor. A notes-only `claimed-by:` line
does not count.

**Fix:** Claim the task first (`task_claim`) or complete it from an
authenticated hub session so the wrapper can stamp `claimed_by`.

## Work-product release: `already in progress; retry shortly`

**Symptom:** `work_product_submit` / `unified-agent work-product` release
returns `work product <id> release is already in progress; retry shortly`.

**Cause:** `_work_product_release_lock` takes a non-blocking `flock` on
`.<db>.work-product-<id>.lock` next to `memory.db` (task #1297). A second
release of the same id that races the consume window is refused instead of
double-consuming the one-shot pass or writing a second rejection event.
The OS releases the flock when the owning process exits, including a
crash, so a later retry can distinguish an abandoned consumed pass from a
live release without a lease timeout.

**Fix:** wait and retry the *same* submission. Against a hub, retry by ID
with `unified-agent work-product release <id> --hub URL` (`release` requires
`--hub`). Locally, re-run the identical `unified-agent work-product submit`
command. Do not change title/tags/model/pack — that starts a fresh review. See
[work-product-release.md](work-product-release.md).

## Doctor reports package_checkout DEGRADED

**Symptom:** `system_doctor` / Control Center Doctor reports `package_checkout`
DEGRADED. The detail names a `.pth` or `__editable__*finder.py` under the main
checkout's `.venv`, or says this process imports `unified_agent` from
`.worktrees/…` or `.claude/worktrees/…`. Hub, ua-node, or executor behavior
does not match `main`.

**Cause:** the shared `.venv` editable install was pointed at a per-task
worktree (`uv pip install -e` of that tree into `~/dev/<repo>/.venv`, or a
`.pth` left over from it). Every service that imports through that venv then
runs the stale tree whatever `main` says (task #1944). A worktree with its
*own* `.venv` does not trip this row.

**Fix:** in the main checkout, `uv sync`, then restart the services that
import the package (`ua-node`, the hub, executors). Do not edit the `.pth` by
hand. Confirm the row returns to `LIVE`. See
[tools-reference](tools-reference.md#system-doctor-toolsdoctorpy).

## Control Center npm run dev has no live data

**Symptom:** `npm run dev` in `packages/web` serves `http://127.0.0.1:5173/ui/`
but pages are empty, sign-in never completes, or Vite exits because `--host`
is refused.

**Cause:** the Vite server proxies nothing on its own (task #1579). Live hub
data needs an explicit `UA_DEV_HUB_URL` (the production hub, for example over Tailscale). The
proxy carries the hub session cookie and OAuth flow, so while it is on the
dev server must bind loopback (`127.0.0.1`); `--host` or any non-loopback
`server.host` is refused. A URL with `user:password@` is refused. The
development machine never runs a second production hub; a loopback hub is only an
isolated test fixture.

**Fix:**

```bash
UA_DEV_HUB_URL=http://<hub-host>:8765 npm run dev
```

Sign in through the UI. If sign-in returns `403 {"error":"origin not
allowed"}`, the hub does not accept the dev server's `Origin`: add
`http://127.0.0.1:5173` to the hub's `UA_HTTP_ALLOWED_ORIGINS` (keep the
existing entries; a non-empty value replaces the default list) and restart the
hub. Drop `--host`, or unset `UA_DEV_HUB_URL` if you only need the static
shell. See [packages/web/README.md](../packages/web/README.md)
and [web-ui.md](web-ui.md#developing-against-a-live-hub).

## Dispatch accepted but the node never ran

**Symptom:** you queued work for a specific ua-node (or automatic selection
picked one). The enqueue succeeded, the item sits `waiting`, then later
fails. The receipt reason is `waiting_expired_node_offline`. A linked
dispatch run fails with the same reason. The work never starts when the
laptop comes back.

**Cause:** task #2110. Direct enqueue and automatic selection share one
liveness rule (`node_registry.heartbeat_is_live`). An offline or
never-heartbeat target is accepted only as waiting, with
`expires_at = now + UA_NODE_QUEUE_WAIT_S` (default 3600 s; bounds 60 s–7
days). After the deadline the hub fails the item instead of launching
days late. The HTTP hub runs `expire_waiting` every
`UA_NODE_WAIT_SWEEP_S` seconds (default 60; `0` disables) even with
dispatch off. A malformed setting stops the hub at start.

**Fix:** wake the node before the deadline. The original item stays
`queued` while it waits, so the node claims and runs it on its next poll;
do not enqueue it again. A second enqueue while the original is still
waiting leaves both claimable and can run the work twice. Re-enqueue (for
example to a different, live node) only after the original is terminal:
expired (`failed` with `waiting_expired_node_offline`) or `cancelled`.
The wait sweep fails only items that are still `queued`, so an item that
expired this way never reached a node and a new one cannot overlap it.

A `failed` item is not always safe to re-enqueue. An item the node had
already claimed, whose lease then expired, also ends `failed`, but with
`lease_expired_outcome_unknown`
([`_fail_expired`](../src/unified_agent/node_queue.py) at that node's next claim poll, or
[`settle_expired_run`](../src/unified_agent/node_queue.py) in the dispatch coordinator's lease sweep). The
lease lapses when the node stops heartbeating, and a lost heartbeat does not
prove the backend stopped: the old process may still be running, and a new
item can overlap it. `GET /api/nodes` marks such an item
`outcome_unknown: true` and `restartable: false`, Restart refuses it with
409, and the hub refuses the node's late receipt with 409, so the hub cannot
confirm the outcome for you. Confirm it on the node: stop or locate the old
`unified-agent node` process and its backend, then check the item's
`~/.unified_agent/node-sessions/<item_id>/`,
`~/.unified_agent/node-receipts/<item_id>.json` and the worktree changes,
as in [`unified-agent node`](cli-reference.md#unified-agent-node). Enqueue
new work only once that process is gone and you know what the old run did.

A successful cancel request is not that. [`node_queue.cancel`](../src/unified_agent/node_queue.py) (the Control
Center's `POST /api/action/node-session` with `"action": "cancel"`) only
sets `cancel_requested` and answers `{"item_id": ..., "cancel_requested":
true}`; the item stays `queued` until the hub's wait sweep
([`node_queue.expire_waiting`](../src/unified_agent/node_queue.py), run every `UA_NODE_WAIT_SWEEP_S` and by the
dispatch coordinator's lease sweep) or the node's next claim poll marks it
`cancelled`. If the node claimed the item before the cancel landed, the
item is `claimed` and the work is running; the cancel reaches the node on
its next heartbeat, and the item turns terminal only when the node's
receipt arrives or its lease expires. A governed `dispatch_cancel` on the
linked run also ends a still-queued item `cancelled`: at once when it
reaches the item through [`node_queue.cancel_for_run_evidence`](../src/unified_agent/node_queue.py), otherwise
at that same sweep or claim poll. Check the status before re-enqueueing:
`GET /api/nodes` ([`api.nodes_payload`](../src/unified_agent/webui/api.py))
lists only the newest 25 items, by creation time, under `recent_items`, each
with `status`, `cancel_requested` and `waiting`. Wait for `status` to be
`cancelled` (or another terminal status: `finished`, `failed`, `killed`),
not merely `cancel_requested: true`. A cancelled waiting item never reaches
the node. Once 25 newer items exist, an item drops off that list, and no
route or tool reads one item's status by id. On the hub, query the database
read-only instead:
`sqlite3 -readonly ~/.unified_agent/memory.db "SELECT status,
cancel_requested, receipt_json FROM node_dispatch WHERE id='nd-...'"`
(`$UA_HOME/memory.db` when `UA_HOME` is set).

Do not treat a late launch as a bug — late launch is what this gate
prevents. To wait longer, raise `UA_NODE_QUEUE_WAIT_S` on the hub
(then restart). Distinct from [device acceptance](fleet-console.md#device-acceptance-task-2112)
and [devices-only locality](deploy/execution-locality.md).

## Supervised node command never starts

**Symptom:** `coordination_manage("node_command_start", …)` parks on
`node_dispatch` approval, then the node refuses the item, or
`system_doctor` row `command_items_unsettled` is DEGRADED. A later session of
the same agent cannot see the command.

**Cause:** supervised argv commands (task #2413) are off until the node lists
`command` in `UA_NODE_BACKENDS` **and** `argv[0]` in `UA_NODE_COMMANDS`,
exactly as written (no basename match). An empty command list drops the
backend so the node never advertises it. `argv` must be a list, never a
shell string. The command runs on the node, not in the session, so a dead
session is expected; the owner label gets one `hub-node` message when it
ends. Status is owner, starter, or a human identity only. A completion from a
writer that no longer holds the claim's fence is `409 stale_fence`.

**Fix:** set both env vars on the node, restart it, and retry with
`argv: ["pytest", …]` matching the allowlist. After approval, poll
`node_command_status` or wait for the `hub-node` notice. A later session of
the same agent (or a lifecycle operator) can `node_command_adopt` an
unfinished command. See [harnesses.md](harnesses.md#supervised-long-commands-task-2413).

## Session mail vanished when the session ended

**Symptom:** `message_send(..., to_session=<id>)` succeeded, but a later
session of the same agent never sees the mail. The sender thinks it
dead-lettered, or both a stranded notice and a dead-letter notice arrived.

**Cause:** session mail is private to that session (task #2547). When the
session ends (`completed`, `failed`, or `abandoned`), other sessions of the
label cannot see it. Queue hygiene records each such message once as
`terminal: stranded` and, in `notify` mode (default), sends one `hub-mailbox`
inform to the recipient label per ended session (ids and how to read them)
and one to each sender. Nothing is re-sent and no body is quoted. A send to a
session the reaper already marked abandoned, or one past the live window,
falls back to the label instead (`session_fallback`). The stranded sweep
runs before dead letters. A sender who already got a dead-letter *notice* is
skipped so one message never gets both; a dead letter recorded in `report`
mode does not silence the stranded notice (tasks #2551, #2555).

**Fix:** read the stranded ids with
`message_list(to_agent=<label>, session_id=<ended-session>, unread_only=True)`
and ack with `coordination_manage("message_ack", {"session_id": ..., "through":
<seq>})`. That read does not revive the ended session. To reach every
session of an agent, send `to_agent` without `to_session`. Env:
`UA_MESSAGE_STRANDED` in [configuration.md](configuration.md).

## Pause or terminate refused: missing stop handlers

**Symptom:** `coordination_manage` `agent_pause` / `agent_terminate` (or
`POST /api/agent-lifecycle`) answers a `ToolError` naming missing stop
handler(s) `dispatch` and/or `node`. The agent's state is unchanged.

**Cause:** task #1723. Pause and terminate refuse before any write when
this process has not registered both
`agent_lifecycle.REQUIRED_STOP_HANDLERS`. The hub loads
`dispatch.coordinator` and `node_queue` (they call
`register_stop_handler` at import). A trimmed custom entrypoint or a
test that imported `agent_lifecycle` alone does not.

**Fix:** run the change through the live hub or Control Center, not a
partial process. See [agent lifecycle](agent-lifecycle.md#fail-closed-stop-handlers-1723).

## Every content edit of a chunked memory note fails

**Symptom:** `memory_save` on an existing note (a replace or an
`append=True` edit) raises: `could not drop the old chunk vectors of memory
note '<id>' (no such module: vec0); the edit was not saved, so the note and
its search vectors still match. Retry the save.` Retrying gives the same
error. New notes and tag-only edits still save.

**Cause:** task #2184. A text change must drop the note's old chunk
windows, including their `memory_chunk_vec` rows. That table is a sqlite-vec
(`vec0`) virtual table: it persists in `memory.db`, but the extension that
reads it loads per connection (`storage.connect()`) and fails silently. When
sqlite-vec stops loading (a broken or rebuilt venv, a Python built without
`enable_load_extension`), the table is still there but cannot be touched.
The delete fails closed: the whole edit is rolled back, so the old text,
its vector and its chunks stay consistent. Committing the new text over
the old windows would leave them scoring the old text indefinitely,
because repair and backfill skip notes that already have chunks.

**Fix:** restore sqlite-vec on the hub, then restart it. In the hub
checkout run `uv sync`, then check that the extension loads:

```bash
uv run python -c "import sqlite3, sqlite_vec
c = sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)
print(c.execute('select vec_version()').fetchone())"
```

An `AttributeError` on `enable_load_extension` means that Python cannot
load extensions; rebuild the venv on a Python that can. Retry the edit
after the restart. Do not drop `memory_chunk_vec` to get past the error:
dropping a `vec0` table also needs the extension, and the edit would then
lose the chunk mirror instead of updating it.

## Hub refuses to start: `UA_HTTP_PROXY_SOCKET` is locked or in use

**Symptom:** the hub exits on startup with one of:

- `UA_HTTP_PROXY_SOCKET <path> is locked by another process (another hub?); refusing to take it over`
- `UA_HTTP_PROXY_SOCKET <path> is in use by a live listener (another hub?); refusing to take it over`
- `UA_HTTP_PROXY_SOCKET <path> exists and a probe connect failed (...); refusing to replace a socket that may be live`
- `UA_HTTP_PROXY_SOCKET <path> exists and is not a socket; refusing to replace it`
- `UA_HTTP_PROXY_SOCKET parent <dir> is not owned by the hub user; refusing to bind the trusted-proxy socket there`
- `UA_HTTP_PROXY_SOCKET parent <dir> is group- or world-writable; refusing to bind the trusted-proxy socket there`

**Cause:** task #2190. The trusted-proxy Unix listener is never taken over
from a live hub, because that would silently steal Tailscale Serve (or
other reverse-proxy) traffic. The running hub holds an exclusive advisory
lock on `<path>.lock` for its whole life and listens before the ASGI
lifespan runs. A probe connect is the second guard, for an older hub that
does not take the lock: only `ECONNREFUSED` or `ENOENT` count as stale.
On macOS a full accept queue answers the same way a leftover crashed
socket does (`ECONNREFUSED`), so the lock, not the probe, is what proves
ownership.

**Fix:**

- If another hub is supposed to be running, stop that extra process. Do
  not delete `<path>.lock` while a hub still holds it — the kernel drops
  the flock when the holder exits.
- To restart the intended hub, use the unit (`launchctl kickstart -k` on
  the Mac hub, or `systemctl --user restart`). A crashed hub has already
  released the lock, so a leftover socket file is replaced as before.
- If the path exists and is not a socket (a regular file you created by
  hand, a directory), move it aside. The hub never deletes a non-socket.
- If the parent directory is not owned by the hub user, or is group- or
  world-writable, set
  `UA_HTTP_PROXY_SOCKET` to an absolute socket path inside a hub-owned
  private directory, for example `~/.unified_agent/run/hub.sock` (there is
  no default path; a missing parent is created with mode `0700`). Point the
  reverse proxy at the same socket path, then restart the intended hub with
  the coordinated procedure in
  mac-hub-topology.md. Moving the socket file
  alone does not help: the next start checks the configured path again.

**Source** (pinned to `fefd6847`; line numbers drift on later commits, so
follow the function names):
[`_check_trusted_proxy_socket_parent`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/server.py#L2443) (parent ownership
and mode), [`_lock_trusted_proxy_socket`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/server.py#L2488) (the
`<path>.lock` flock), [`_refuse_live_proxy_socket`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/server.py#L2516) (the
probe connect) and [`_bind_trusted_proxy_socket`](https://github.com/anonb3ll/unified-agent/blob/fefd684767bd78240da856b7edb3b49cd468ce50/src/unified_agent/server.py#L2548).

Do not set `UA_HTTP_PROXY_HEADERS=1` to work around a bind refusal: that
re-opens connecting-IP trust on the TCP listener. See
[configuration.md](configuration.md) (`UA_HTTP_PROXY_SOCKET`) and
mac-hub-topology.md.

## Security audit hangs on a FIFO or device

**Symptom:** `unified-agent-audit`, `security_audit`, `--remediate`, or a
vault re-anchor never returns. `ps` shows the process stuck in `open()`.

**Cause:** before task #2212 the walk called `open()` on every name
`os.walk` returned. A FIFO with no peer (including one named `.git`, which
`SKIP_DIRS` never covered) blocks forever. A character device reports
`st_size == 0`, so the 1 MB size cap did not refuse it.

**Fix:** pull a hub that includes #2212 and re-run. Every audit read or write
of a scanned path now goes through `audit/safe_read.py`: the type is checked
on the path and again on the descriptor, the open is non-blocking, and only
a regular file (or a symlink to one) is read. Name-based repo rules still
see the FIFO (a `.env` linked to `/dev/null` stays an exposed `.env`), but
it does not count toward the 5000-file cap and is never opened. A
remediation whose target is not a regular file is reported as
`[AUTO-FIX-FAILED]`. A vault note or ledger-anchor path that is a FIFO,
socket or device is refused as an unreadable lock rather than written past.
See [security.md](security.md#security_audit-rule-catalog). On an older
checkout, find the blocker with
`find <root> \( -type p -o -type s -o -type c -o -type b \)` and move it
aside, then upgrade.

## New tasks and memory notes are stamped with the hub hostname

**Symptom:** a desktop or laptop agent writes a task or memory note through
the hub, and `source_host` is the Mac hub's hostname, not the machine the
agent runs on. Agents → Hosts may still show the agent on the desktop.

**Cause:** task #1633. The hub stamps `source_host` from the verified
caller's `UA_AGENT_HOSTS` binding only (`config.write_source_host()`).
Request headers, the `heartbeat` tool's `host` argument and the ua-node's
self-chosen name are never used, because a caller chooses each of them.
`label:hub`, or no entry, stamps the hub. The binding is an in-memory
snapshot built at startup. A request never waits for a rebuild: when no
snapshot matches the current value, or the one it has is at least 30 s old,
it starts a background rebuild and still stamps the snapshot it already has
(the hub, when none matches). Work the request spawned after the request
ended also stamps the hub. `UA_SOURCE_HOST` is the fallback for anonymous
and unbound callers, not a per-agent override.

**Fix:** bind the agent's label to the machine in `UA_AGENT_HOSTS` on the
hub (`agent-claude:<desktop-host>`) and restart the hub, so the snapshot
exists before the next write. On a running hub the rebuild starts only when
a request arrives after the snapshot is missing or at least 30 s old, and
that request still uses the previous snapshot. The following write, once the
rebuild has finished, uses the new binding. Confirm with a new write;
existing rows are not rewritten. See [configuration.md](configuration.md)
(per-agent host binding). Distinct from
[device acceptance](fleet-console.md#device-acceptance-task-2112).

## Test-budget gate fails a small PR

**Symptom:** the `CI / gates` step `Test budget (net new test functions)`
reports many changed test files and a large net, for a PR that only touched a
few tests.

**Cause:** the job used to count the checked-out `HEAD` (the PR merged into the
*current* base tip) against `pull_request.base.sha` (the base tip at the PR's
last synchronize). Every commit the base gained in between was credited to the
PR (task #2430; #1481 was +31 tests, failed as +73).

**Fix:** the gate now uses `scripts/test_budget.py merge-parent --pr-head
<head>` on that merge commit and counts only `base-parent..HEAD`. Rebase or
merge current `origin/main` before reproducing locally with
`git merge-base origin/main HEAD`. A stacked PR still includes its stack, so
land the under-budget PRs first. See
[development.md](development.md#test-budget-task-2233).

## Local pytest attestation is skipped, or CI fails it

**Symptom:** the `Verify local test attestation` step prints
`no test attestation at attestation/local-tests.intoto.json; verification
skipped`, or fails with `missing test attestation`, an invalid signature, or
an unsafe test id.

**Cause:** `python -m unified_agent.pytest_attestation sign` was not run, the
envelope was not uploaded with the PR (it is gitignored), or
`UA_TEST_ATTESTATION_PUBLIC_KEY` does not match the signing key. Without
`UA_TEST_ATTESTATION_REQUIRED`, a missing file is a skip, not a fail. A
sample rerun (`ci --run`) only happens when the classifier asked for the
full suite; a cheap-gates PR verifies the signature and stops.

**Fix:** sign the tests that belong to the change before opening the PR:

```bash
uv run python -m unified_agent.pytest_attestation sign -- tests/test_area.py -q
```

Keep `UA_TEST_ATTESTATION_KEY` in the environment, never in the tree. Confirm
with `verify` locally. If CI must require the file, set the
`UA_TEST_ATTESTATION_REQUIRED` repo variable. See
[development.md](development.md#local-pytest-attestation).

## `ua-node` restart loop with `203/EXEC` after worktree cleanup

**Symptom:** `systemctl --user status ua-node` shows `status=203/EXEC` and the
unit restarts every few seconds, or a Mac node never comes back after a
restart.

**Cause:** the supervisor names a versioned release worktree
(`.worktrees/release-<date>-<sha8>/.venv/bin/unified-agent`) and that worktree
was removed. `systemctl --user cat` can hide this, because a drop-in overrides
the unit file's `ExecStart`.

**Fix:** run `scripts/node-release-roll.sh --check-only`. It prints the
effective `ExecStart` (or the LaunchAgent's program) and whether it names
`~/.unified_agent/release/current`. Install the drop-in from
[deploy/node-release-roll.md](deploy/node-release-roll.md), then roll with
`scripts/node-release-roll.sh --sha <sha>`. Supervisors name only that link,
and cleanup never removes the link's target.

## Related

[installation.md](installation.md) covers correct registration for both transports;
[configuration.md](configuration.md) documents every `UA_*` variable referenced above;
docs/mac-hub-topology.md is the Mac hub cutover runbook;
docs/desktop-migration.md is a separate, narrower guide for one
historical hub/spoke layout (Ubuntu desktop as hub, macOS laptop as spoke) and covers
some of the same HTTP/hub setup from that angle.
