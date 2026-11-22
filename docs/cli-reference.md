# CLI reference

`unified-agent` is an agent-operations platform: one authority store (identity,
policy), an approval gate, domain objects, a tamper-evident evidence ledger,
executors, and a web control center. MCP is one adapter over that platform,
alongside HTTP/JSON, CLI, Web UI, and SDK — this page documents the command-line
surface.

`pyproject.toml` installs `ua`, `unified-agent`, 32 `unified-agent-*` console scripts, the alias `ua-session-end`, and `ua-usage`.
The MCP inspector is a non-installed `uv run` command. All scripts are in
`.venv/bin/` after `uv sync`. Module CLIs that are not console scripts include
`python -m unified_agent.lane_b_telemetry`,
`python -m unified_agent.shipping_preflight_cli`, and
`python -m unified_agent.pytest_attestation`.

| Script | Entry point | Purpose |
|---|---|---|
| `ua` | `unified_agent.cli:entrypoint` | Lightweight operator and agent client over the authenticated hub MCP gateway: reads, explicit task and mail writes, and controlled delegation, workflow and intervention verbs with retry receipts, bounded waits and a resumable JSONL event stream; live schemas, strict JSON results and no local database fallback. See [Operator and agent CLI](cli-expansion.md). |
| `unified-agent` | `unified_agent.server:main` | The platform's MCP adapter (stdio or HTTP); also `unified-agent init` / `unified-agent doctor` / `unified-agent audit` / `unified-agent platform` / `unified-agent work-product` / `unified-agent login` / `unified-agent node` |
| `unified-agent-init` | `unified_agent.bootstrap:init_cli` | Cold-start bootstrap: UA_HOME, empty DB, starter env file |
| `unified-agent-doctor` | `unified_agent.bootstrap:doctor_cli` | `system_doctor` capability report for a shell; exit 1 on MISCONFIGURED or DEGRADED |
| `unified-agent-backup-loop` | `unified_agent.backup_loop:main` | Scheduled database backup beside a hosted hub; the container entrypoint starts it when `UA_BACKUP_INTERVAL_S` is set (task #2585) |
| `unified-agent-audit` | `unified_agent.audit.cli:main` | Security-audit CLI (CI/pre-commit gate) |
| `unified-agent-review` | `unified_agent.review_cli:main` | Review pipeline runner & scan orchestrator; `queue health\|stale\|sweep\|close-merged` are queue-closure reads and dry runs (`close-merged --apply` closes unambiguous merged-PR matches; see [work-queue.md](work-queue.md#closure-stale-parked-folded-merged)); `ledger poll` asks the hub over HTTP to run one Approval Ledger poll now (operators only; see [connector-activation.md](connector-activation.md)); `record-read` records the steward's own read of a PR head for the release preflight, directly on the hub or through `POST /api/review/reads` from a hub client (see [automation.md](automation.md#shipping-preflight-head-moved-red-tier-hold-2259)) |
| `unified-agent-tool-classification` | `unified_agent.tool_classification:main` | MCP allowlist classification + drift check (every served tool is REACHABLE, WITHHELD, or OMITTED) |
| `unified-agent-tui` | `unified_agent.tui:main` | Textual dashboard over memory/tasks/worktrees/journal |
| `unified-agent-merge` | `unified_agent.merge:main` | Task-branch merge orchestrator |
| `unified-agent-wiki` | `unified_agent.wiki:main` | Vault wiki custodian (link/orphan audit + `Health.md` rebuild) |
| `unified-agent-sync` | `unified_agent.sync_cli:main` | Workspace context sync (role detection + tasks/memory refs → `ua-sync` block) |
| `unified-agent-new-project` | `unified_agent.scaffold_cli:main` | Project scaffolding tool |
| `unified-agent-distro` | `unified_agent.distro_cli:main` | Portable project distro (task #1733): `init <path>` plans (dry run by default; `--write` applies, `--force` replaces drifted files) a constitution `AGENTS.md` with `CLAUDE.md`/`GEMINI.md` pointers, per-harness hub config that names token environment variables only (`--harness` claude, codex, cursor, gemini, opencode, copilot), `.unified-agent/operating.yaml` and a starter skill (`--write` needs `--hub-url` or `UA_HTTP_HUB_URL`, with no local default, and `--allow-dev-root` under `~/dev`; `--force` replaces only the `unified-agent` entry in existing MCP config files); `check <path>` reports missing or drifted files and literal secrets |
| `unified-agent-consolidate` | `unified_agent.consolidate:main` | Memory consolidation CLI: `propose`, `apply`, `restore`, `apply-batch`, `auto-apply`. `restore <id>` re-promotes a superseded note or undeletes a soft-deleted one through `memory_restore` (task #2520; owner, human-delete and tenant rules unchanged). See [memory-tasks-vault.md](memory-tasks-vault.md#restoring-a-deleted-note-in-place-task-2515) |
| `unified-agent-sleep-pass` | `unified_agent.sleep_pass:main` | Maintenance & sleep pass generator |
| `unified-agent-planning-loop` | `unified_agent.planning_loop:main` | Weekly ledger plan, decision packet, and goal proposal generator; `--next-work` projects `docs/queue/NEXT-WORK.json` onto live hub state (task #1468, read-only). That manifest is maintained in the separately checked-out cockpit workspace (`UA_WORKSPACE`, default `~/agent-workspace`), not this repo — `--next-work` errors clearly if it isn't found there, or pass `--manifest-path` to point at a copy. `--format json` prints only the JSON view (pipe-safe); `--format markdown` prints only the rendered section; the default `--format both` keeps the original combined JSON-then-markdown stdout output |
| `unified-agent-evolve` | `unified_agent.evolve:main` | Skill-cluster proposals from high-trust memories (Inbox only) |
| `unified-agent-executor` | `unified_agent.executor.daemon:main` | Background task executor daemon |
| `unified-agent-bug-worker` | `unified_agent.bug_worker.cli:main` | Bounded SYSTEM bug-to-patch worker (opt-in; see [bug-worker.md](bug-worker.md)) |
| `unified-agent-dispatch-scheduler` | `unified_agent.dispatch.cli:main` | Approved-dispatch poller and completion report |
| `unified-agent-eventbus` | `unified_agent.events:main` | Swarm event bus CLI |
| `unified-agent-zulip-worker` | `unified_agent.zulip_worker:main` | Retained Zulip poller (Slack is authoritative; see [zulip-coordination.md](zulip-coordination.md)) |
| `unified-agent-ts-worker` | `unified_agent.ts_worker.worker:main` | Discord/Telegram chat ingestion (off until tokens; see [ts-worker.md](ts-worker.md)) |
| `unified-agent-pr-watch` | `unified_agent.pr_watch:main` | Poll GitHub PR events into hub memory + vault journal |
| `unified-agent-zulip-automation` | `unified_agent.zulip_automation:main` | Bounded Zulip morning/evening/bridge posts (`kind` is required) |
| `unified-agent-session-end` | `unified_agent.session_end:main` | Session wrap-up generator (`RESUME.md`) |
| `ua-session-end` | `unified_agent.session_end:main` | Alias for `unified-agent-session-end` |
| `ua-usage` | `unified_agent.harness_usage:main` | Prints the `run_usage` of the current Claude Code or Codex session since a run's claim, for `task_update(run_usage=...)`; `billing` only when stated with `--billing`. `--report --task --run --agent` also records it on the run as late accounting (`scripts/hook_usage_report.py` runs this automatically in Claude Code). Reads the local session store only; calls no hub or provider. See [budgets.md](budgets.md#interactive-sessions-ua-usage) |
| `unified-agent-audit-ledger` | `unified_agent.audit.ledger_cli:main` | Standalone hash-chain verify (same as `unified-agent audit verify`) |
| `unified-agent-vault-sync` | `unified_agent.vault_sync:main` | Hub→desktop vault pull (same `main()` as `python -m unified_agent.vault_sync`) |
| `unified-agent-evidence-export` | `unified_agent.audit.evidence_export_sink:main` | Scheduled signed evidence export (env-driven; same `main()` as the module CLI) |
| `unified-agent-bridge` | `unified_agent.messaging.bridge_cli:main` | `run-once [--brain] [--a2a] [--dry-run] [--baseline] [--limit N]`: one pass of the messaging bridges (unified_brain threads and Hermes A2A gateways to and from hub mail), JSON report on stdout. Runs in process on the hub host; no timer ships. See [messaging-bridges.md](messaging-bridges.md) |
| `unified-agent-routines` | `unified_agent.routine_cli:main` | Routine runner (`tick` fires due routines; run it from a timer). See [routines.md](routines.md) |
| `unified-agent-slack-slots` | `unified_agent.slack_outbound:slots_main` | List or release Slack send slots abandoned mid-send (`list`, `release KEY --by LABEL`). See [slack-coordination.md](slack-coordination.md) |
| `unified-agent-lane-reaper` | `unified_agent.lane_reaper:main` | Cleans up orphaned lane processes. See [automation.md](automation.md#lane-orphan-reaper-task-1316) |
| `unified-agent-supervisor` | `unified_agent.supervisor_loop:main` | First-mate supervisor inbox (task #1918). `--once --dry-run` drains the supervisor inbox and prints the wakes, routes, review hand-offs and reconciles it would make; both flags are required, and it sends, wakes and commits nothing. See [the design](designs/2026-10-06-first-mate-supervisor-loop.md) |

---

## Source-only module commands

`python -m unified_agent.cloud_bridge_cli` is an explicit hub host relay for `agent-cloud-agent17`
(task #1789). It runs from a reviewed source checkout with installed dependencies;
there is deliberately no installed console-script entry or automatic hook.
`snapshot` reads bounded context, inbox and queue. `snapshot --onboarding` adds
explicit Wiki notes, one `--vault-source Wiki/<note>.md` each (1–16 notes);
`consume` commits only the local packet cursor. `stage-message` records a local
intent, and separately authorized `send-message` sends that exact intent with
idempotent reconciliation. Every command requires a private `--state`, `--issuer`,
`--parent-thread`, `--parent-request` and `--executor`. The dedicated credential
stays on the hub host; direct root-cloud tools and network reachability are not added.
See [cloud-agent17-bridge.md](cloud-agent17-bridge.md) for authentication, retry and human
gate requirements.

---

## `unified-agent`

Starts the platform's MCP adapter. No CLI flags — behavior is entirely environment-driven (see
[configuration.md](configuration.md)).

```bash
$ unified-agent                                   # stdio transport (default)
$ UA_TRANSPORT=http UA_HTTP_PORT=8765 unified-agent  # streamable-HTTP hub
```

`main()` calls `config.ensure_dirs()`, `storage.init_db()` (creates the schema, migrates
columns, and kicks off background embedding-backfill and vault-state-sync threads), then
runs the FastMCP server with `transport="streamable-http"` if `UA_TRANSPORT=http`,
otherwise the default stdio transport.

### `unified-agent platform`

Reads the canonical hub's platform object surface without opening the local database:

```bash
$ unified-agent platform manifest
$ unified-agent platform get task 123
$ unified-agent platform get board
```

The command requires a nonblank explicit `UA_HTTP_HUB_URL` ending in `/mcp` and a matching
bearer token for every executor label. It never derives a consumer URL from `UA_HTTP_HOST`,
`UA_HTTP_PORT`, or executor-label classification; an explicit loopback URL remains supported.
It fails closed on missing hub configuration, authentication or profile denial, redirects,
malformed object paths, unknown/unserved objects, and oversized or invalid responses. It never
falls back to local SQLite state.

### `unified-agent init`

Bootstraps a fresh install (also available as the standalone `unified-agent-init`):

```bash
$ unified-agent init                  # create ~/.unified_agent, empty DB, service.env
$ unified-agent init --json            # machine-readable summary for a provisioning script
$ unified-agent init --print-token     # also echo the generated bearer token
$ unified-agent init --force           # rotate the bearers; keep transport/auth settings
```

Idempotent: an existing `service.env` is kept (token not rotated) unless `--force`;
an existing database is never touched. `--force` rotates this host's bearers and
re-asserts the operator identity and workspace, but keeps an existing
`UA_TRANSPORT`, `UA_ENFORCE_AUTH`, `UA_APPROVAL_TOOLS` and `UA_MCP_OAUTH`. The
agent token label is `<hostname>_agent`, or `local_<hostname>_agent` when that
would look like a fleet label (`desktop_`, `laptop_`, `server_`) the hub refuses.
Pins `UA_WORKSPACE` to `~/.unified_agent/workspace` so a foreign machine does not
depend on `~/agent-workspace`.

### `unified-agent doctor`

Runs `system_doctor` from a shell (also standalone `unified-agent-doctor`):

```bash
$ unified-agent doctor                 # human-readable; exit 1 if any capability is MISCONFIGURED or DEGRADED
$ unified-agent doctor --json          # full report
$ unified-agent doctor --strict        # also exit 1 on DORMANT or UNKNOWN (whole surface must be live)
$ unified-agent doctor --allow-unbuilt-ui  # installer: do not fail on web_ui for an install without a UI build
```

Exit code is the gate for a cold-install CI check: DORMANT (opt-in feature not
wired) is not a failure; MISCONFIGURED and DEGRADED are.

### Inspector

Not a console script — run directly with `uv`:

```bash
$ uv run mcp dev src/unified_agent/server.py
```

Launches the MCP Inspector web UI to call tools interactively against a live server
instance for manual testing.

---

## `unified-agent-audit`

```
usage: unified-agent-audit [-h] [--json]
                           [--fail-on {high,medium,low,info,none}]
                           [--exclude EXCLUDE] [--baseline]
                           [path]

Audit a project for AI-assisted-development security best practices.

positional arguments:
  path                  project root (default: cwd)

options:
  -h, --help            show this help message and exit
  --json                emit the full report as JSON
  --fail-on {high,medium,low,info,none}
                        exit 1 if any finding is at least this severe
                        (default: high)
  --exclude EXCLUDE     glob pattern of paths to exclude from auditing (can
                        specify multiple times)
  --baseline            apply the operator baseline
                        ($UA_HOME/audit-baseline.json); never applied by
                        default, and never to a file inside a git work tree
```

Subcommands: `verify`, `re-anchor`, `evidence`, `surfaces`, and `baseline`.
`unified-agent-audit baseline accept <root> --reason "..." [--path REL] [--exclude GLOB] [--yes]`
previews the current high and medium `SECRETS-KEY` findings under `<root>`
outside every git work tree (paths and detectors, never values) and, on `--yes`
or an interactive yes, records exactly that set in the operator baseline by
value hash. `unified-agent-audit baseline list [--json]` shows its entries with
hashes truncated. `surfaces` applies the baseline by default (`--no-baseline`);
this plain command applies it only with `--baseline`, so CI and the hooks never
see it. Rules, scope and limits:
[security.md](security.md#operator-reviewed-baseline-task-2323).

Same engine as the `security_audit` MCP tool (`audit.engine.run_audit`) — see the rule
catalog in [security.md](security.md). Unlike the MCP tool, the CLI's `path` is not
sandboxed to a workspace; it resolves relative to the process's cwd and accepts any
directory. A FIFO, socket or device in the tree is never opened (task #2212);
name-based repo rules still see it. Each report includes a stable **A–F `grade`**: A clean, B info-only, C low,
D medium, E truncated scan with no high finding, F any high (this engine has no
separate `critical` band — high is the gate).

**Exit codes:** `0` if no finding at/above `--fail-on` severity is present (or
`--fail-on none`); `1` if the gate is tripped; `2` on bad usage (argparse error, or
`path` is not a directory).

```bash
$ uv run unified-agent-audit . --fail-on high              # human-readable, gate at high
$ uv run unified-agent-audit . --json --fail-on none        # full JSON report, always exit 0
$ uv run unified-agent-audit . --exclude "tests/*" --exclude "*.lock"
```

### Pre-commit hook

`scripts/install-hooks.sh` installs a `pre-commit` hook that runs this exact gate:

```bash
$ ./scripts/install-hooks.sh
```

The hook first runs the staged conflict-marker guard, then the audit through
`scripts/run_audit.py`, which imports the committing checkout's own `src`
(task #2204). The shared venv's `unified-agent-audit` is an editable install of
one checkout, so from a linked worktree it would audit with that checkout's
code instead. The audit part of the hook body, abridged:

```bash
commit_root="$(git rev-parse --show-toplevel)"
py="$commit_root/.venv/bin/python3"   # else the venv found at install time
exec "$py" "$commit_root/scripts/run_audit.py" . --fail-on high
```

To run the same audit by hand from any checkout or worktree:

```bash
$ <venv>/bin/python scripts/run_audit.py . --fail-on high
```

Re-run `./scripts/install-hooks.sh` after pulling this change: the hook is
generated, so an older installed body keeps its old behavior.

`.git/hooks` is not version-controlled, so this must be re-run after every fresh clone.

### CI usage

`.github/workflows/ci.yml` runs, on every push to `main` and every PR:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
make smoke
uv run unified-agent-audit . --fail-on high
uv run pytest tests/test_audit_ledger_chain.py::test_cli_verify_rejects_tampered_fixture -q
```

`unified-agent audit verify` (also `unified-agent-audit verify`) checks the
tamper-evident event-log hash chain. `unified-agent audit re-anchor` snapshots
today's chain head into `Logs/audit-ledger-heads/YYYY-MM-DD.md` without
mutating `event_log`. A witness path that is not a regular file is refused
and left untouched (task #2212). Both exit `0` when the chain is valid or empty, `1` when
tampered, and `2` on bad usage. CI re-runs the tampered-fixture pytest so a
broken verify gate cannot hide behind the full suite.

`unified-agent evidence` (also `unified-agent audit evidence`) emits an
attribution-grade evidence report tracing `human -> device -> agent -> action -> proof`
for a task, PR, or time-window in JSON, Markdown, or text formats.
`unified-agent evidence render <export.json> [--format markdown|text|csv|json]`
re-renders a saved JSON export of any schema version without the hub database
(a pre-1.7 `human_authority: verified` reads as `credential-attributed`).
`unified-agent evidence receipt --pr <n> [--repo owner/name] [--out receipt.json]`
renders the merge receipt for one merged PR, and
`unified-agent evidence verify receipt.json` checks it offline (task #2307). See
[docs/evidence-report.md](evidence-report.md) for complete options and schema.

`unified-agent db` manages SQLite `memory.db` online backup, restore, and integrity:
`unified-agent db backup [--out <path>]` takes an atomic non-blocking snapshot;
`unified-agent db restore <path>` safely validates and restores a snapshot;
`unified-agent db verify` and `status` inspect database health;
`unified-agent db migrate-constraints [--dry-run] [--path <file>] [--json]`
previews (read-only) or applies the one-shot key-constraint rebuild of task
#2326; `unified-agent db org-partition [--apply | --downgrade] [--path <file>]
[--json]` reports (read-only), applies or undoes the optional org_id sentinel
backfill of task #1433. See
[docs/database-operations.md](database-operations.md) for runbooks and details.

`unified-agent agent` adds and removes an agent with one command each (task
#2453). It runs on the hub host, in-process against the hub database (it
refuses, exit 2, where no hub database exists and never creates one), acting as
the configured operator label or `--as <label>`; every write needs a lifecycle
operator (`UA_LIFECYCLE_OPERATORS`), the authority `agent_retire` requires.

```bash
$ unified-agent agent add <label> --office <office-id> [--profile developer|steward|...] \
    [--kind local|cloud] [--expires-days N] [--harness claude|codex|...] [--hub <url>] \
    [--wait SECONDS | --no-wait] [--allow-plaintext-at-rest]
$ unified-agent agent remove <label> --reason "<text>" [--force-release]
$ unified-agent agent list [--json]
```

`add` checks the label, office and profile (a seat may keep or lower the
label's code default, never raise it), refuses when `UA_MANAGED_SECRET_KEYS`
is unset (the stored bearer would be unencrypted; `--allow-plaintext-at-rest`
accepts a mode-0600 plaintext file instead), mints a bearer, records its digest,
issuer, issue date and expiry in `agent_tokens`, seats the label in the office,
prints the bearer once on stdout with the harness's hub entry (from
`ua mcp-entry`), then waits up to 120 seconds for the first authenticated
request. The hub accepts the bearer from the next request: no `service.env`
edit, no restart. `remove` refuses while the agent holds `doing` claims unless
`--force-release` (each release is a `task_claim_release` with the reason),
then retires the label, which revokes its managed tokens, session credentials
and device sign-ins, deletes the stored bearer and drops the seat. `list` shows
each agent's office, profile, kind, lifecycle state, token source and dates, and
last-seen time. Details: [agent-lifecycle.md](agent-lifecycle.md#adding-and-removing-an-agent).

`unified-agent login --hub <url> [--agent LABEL] [--out PATH]` signs a device
in to the hub with the OAuth device authorization grant (RFC 8628, task #1320):
it prints a short code and a link, you approve the code in any browser (phone
included) at `<hub>/oauth/device`, and the refreshable, device-bound token is
written to `~/.unified_agent/oauth-token.json` (mode 600). Nothing secret is
printed. Requires `UA_MCP_OAUTH=1` on the hub. See
[docs/agent-access.md](agent-access.md#device-sign-in-oauth-device-flow).

### `unified-agent node`

`unified-agent node [--hub URL] [--name NODE] [--token-file PATH] [--heartbeat-s S]
[--claim-wait-s S] [--once] [--allow-insecure]` runs this device as a **ua-node**
(task #1327, I1). The node connects *out* to the hub over HTTP on the tailnet and
opens no listening port. It authenticates with the device token that
`unified-agent login` saved, refreshing it as it nears expiry; the hub URL
defaults to `$UA_HUB_URL`, then to the hub recorded in that sign-in. Plain HTTP is
refused unless the host is loopback or on the tailnet (`--allow-insecure`
overrides this for lab networks).

What it does, in order:

1. **Registers** measured host facts: OS, the harness binaries found on `PATH`
   with their `--version` output, the backends it will run (`UA_NODE_BACKENDS`,
   default `codex,agy`, filtered to binaries that exist; `pi` is opt-in because its
   backend cannot yet be interrupted by `kill_check`), and its worktree
   roots (`UA_NODE_WORKTREE_ROOTS`, default `~/dev`, `os.pathsep`-separated). The
   node name is `--name`, else `$UA_NODE_NAME`, else the same host key
   `system_doctor` uses. The name is bound to the agent identity that first
   registers it, and to the tailnet device its token was issued to (on a hosted
   hub with no tailnet peer, to the device-key thumbprint from
   `unified-agent login`; see
   [Enrolling a device](deploy/railway.md#enrolling-a-device)); no other
   identity or device can register, heartbeat, claim, or post receipts for it.
   The node routes accept only a device-flow credential (`auth_method:
   device_code`); set `UA_NODE_ALLOW_BEARER=1` on the hub to admit static bearers.
2. **Heartbeats** every `--heartbeat-s` (default 30) from a background thread. The
   node's row on Agents → Hosts (`/ui/agents/hosts`) then reads `host_source: node` with
   `executor.reported: true` and `worktrees.reported: true`, and the node's agent
   is grouped under it instead of under an identity-prefix guess. A heartbeat also
   renews the running item's lease and carries optional progress notes onto the
   `agent.node` event channel.
3. **Long-polls** its own queue (`--claim-wait-s`, default 25, hub cap 30).
4. **Runs** each claimed item through the item's backend as the node's bound
   agent. Native backend names (`codex`, `agy`, ...) go through
   `executor.backends.get_backend`; an `acp:<harness>` name runs through the ACP
   adapter (task #1328, I2): the harness binary is spawned as an ACP *agent*
   subprocess and driven over JSON-RPC stdio (`initialize` → `session/new` →
   `session/prompt`), with `session/update` notifications streamed to the hub
   as `acp_event` evidence and `session/request_permission` calls relayed
   through the hub's global approval queue (`acp_permission` action — always
   gated, never auto-approved; the node re-polls `/api/node/permission` until a
   human resolves the approval row or the 5-minute cap expires, and any
   non-approved outcome answers the agent `cancelled`). The ACP-negotiated
   session id is returned on the receipt and persisted on
   `node_dispatch.session_id`, which is what joins the dispatch to its mirrored
   `agent_sessions` row (`kind='acp'`, `external_session_id`). ACP harnesses
   live in `acp_matrix.ACP_HARNESSES` — `gemini`, `codex` (`codex-acp`),
   `claude` (`claude-agent-acp`), `copilot`, `cursor` (`agent acp`),
   `opencode`, `hermes` — each with a `UA_*_BIN`-style env override; a node
   advertises an `acp:*` backend only when its entrypoint resolves, and
   `UA_NODE_BACKENDS` entries naming an unknown harness are dropped with a
   warning rather than advertised. The item's `cwd` must resolve inside a
   worktree root and its backend must be allowed on the node; otherwise the
   item fails without running. The backend's `kill_check` fires on SIGTERM and
   on a hub-side cancellation (`node_queue.cancel`, or a governed
   `dispatch_cancel` on a linked dispatch run); for ACP items that maps to
   `session/cancel` and the turn ends with `stopReason: cancelled`.
   An item with `resume_session_id` (task #1741) reopens that session instead:
   ACP `session/resume` or `session/load` as the agent advertised at
   `initialize` (refused as `resume_unsupported` otherwise, never a fresh
   session), or for `claude`/`codex` the CLI's own `--resume <id>` /
   `exec resume <id>`. After the last turn an interactive ACP session (the
   hub's `hold_for_followups`, or one that already took a follow-up) waits
   `UA_NODE_ACP_IDLE_S` seconds (default 60) for a follow-up, then is
   reaped and stays resumable; a scheduler run ends at once. Claude and Codex
   CLI prompts are passed after `--`, so a prompt starting with `-` is never
   read as a flag. The node also reports native Claude Code and
   Codex sessions under its roots (metadata only) at registration and every
   `UA_NODE_DISCOVERY_S` seconds (default 300, `0` off); stores default to
   `~/.claude/projects` and `~/.codex/sessions` (`UA_NODE_CLAUDE_PROJECTS`,
   `UA_NODE_CODEX_HOME` override). Its facts advertise
   `node_features: ["session_resume"]`.
5. **Posts one receipt** per item. The receipt is spooled to
   `~/.unified_agent/node-receipts/` (mode 600) before it is sent and removed once
   the hub acknowledges it, so a result survives a crash or restart. Receipts are
   idempotent on the hub: a resend returns the first receipt with
   `duplicate: true` and records nothing new. Each receipt carries its claim's
   `attempt`, and the hub refuses one from an attempt that is no longer the latest
   claim. A receipt for an item linked to a dispatch run closes that run through
   `record_run_outcome` (a duplicate retries a close that failed); an item whose
   lease expires fails with `lease_expired_outcome_unknown` and is never
   automatically requeued; and a queued item whose
   linked run was cancelled never reaches the node.

For `lease_expired_outcome_unknown`, first stop or locate the old node process.
Inspect its `~/.unified_agent/node-sessions/<item_id>/` and
`~/.unified_agent/node-receipts/<item_id>.json`, plus the worktree changes and
linked dispatch evidence. Reconcile whether the work completed before approving
a **new** dispatch. The hub rejects a late receipt with 409 because its outcome
cannot safely be treated as success or used to retry the same item.

**Claim reconciliation (task #2136).** A claim whose response is lost (the hub
committed item A, the connection dropped) is reconciled before the node claims
anything else, so it can no longer take item B while A sits claimed and
unobserved. This is fail-closed recovery, **not** an exactly-once guarantee:
the hub binds at most one item attempt to each logical claim operation and
never reissues one implicitly, but whether a backend actually ran is still
established only by the node's receipt.

- Before it sends a claim, the node durably publishes a random operation id
  in `~/.unified_agent/node-receipts/claim-journal/<key>.json` (mode 600, one
  file per hub and node, written with file and directory fsync). The claim
  carries `claim_op: {id, mode: "claim", issued_at}`.
- The hub reads and settles that operation inside the same `BEGIN IMMEDIATE`
  transaction that claims the item (`node_claim_ops`, table
  `node_claim_ops`). It ends either `bound` to one item attempt, with the
  exact payload it was handed, or terminal `no_claim` (an empty long poll
  closes its operation this way). A disconnect leaves it `pending`.
- After any failed claim the node sends `mode: "reconcile"` for the same id.
  Each request is a new HTTP call with a fresh signed device proof; replaying
  an exact proof is still refused with 403. Reconciliation never selects work:
  it returns the bound attempt, or closes a `pending` or unknown operation as
  `no_claim`, so a delayed original handler that has not committed yet finds
  the closed row and claims nothing. Only a well-formed disposition from the
  hub clears the journal; a timeout, refusal or missing answer never counts as
  "nothing was claimed".
- A bound attempt is history, not a fresh grant. The hub reports it
  `executable` only while the item is still claimed by exactly that attempt,
  under a live lease, by the same device binding, with no cancellation,
  receipt, linked-run termination or trust-policy change since. Otherwise the
  node drops it without running it. Nothing in reconciliation renews a lease
  or adds an attempt.
- The node writes `ready` when it accepts a binding and `start_intent`
  immediately **before** invoking the backend. It removes the journal once
  the receipt is durable on disk (file and directory fsynced) or the hub has
  acknowledged it. If the receipt cannot be written, the journal stays at
  `start_intent` while the node resends the receipt, and it claims nothing
  until the hub answers. A `start_intent` journal with no spooled receipt for
  that attempt (the node died before either happened) is ambiguous, so the node
  **holds**: it logs `claiming held`, does not relaunch the item, and does not
  claim new work. To release the hold, reconcile the item as for
  `lease_expired_outcome_unknown` above, then delete the journal file the log
  names.
- One worker process owns a journal at a time: it holds an exclusive `flock`
  on `<key>.lock` from the journal read until the journal is cleared. A second
  `unified-agent node` for the same hub and node name reports `held` and
  claims nothing. A journal that cannot be written, is corrupt, or names
  another hub or node also holds instead of being replaced.
- A hub that does not advertise `node_claim_op_v1` at registration is not
  claimed from (the node re-registers to pick up a hub upgrade). Older nodes
  that send no `claim_op` keep the previous, unreconciled claim behaviour.
- Retention: a `claim` request is admitted only within 600 s of its
  `issued_at`, so the device clock must be roughly right. Pending and closed
  rows are kept an hour past that point and bound rows at least a day, and
  longer while their attempt is still the item's live claim, so deleting a
  record never reopens admission for its id. At 5,000 rows for one node the
  hub refuses new operations (503) rather than evicting unresolved ones.

Limits: the queue carries no tenant column, so the binding checked is the
node's agent identity and device. A process lock does not prove that an older
backend process died, and a deleted or replaced journal file cannot be told
apart from a first start; a second host or a cloned journal for the same node
name is not fenced. The existing cancellation-versus-launch race is unchanged.

**Scheduler routing.** With `UA_DISPATCH_NODE_ROUTING=1` (off by default,
independent of `UA_DISPATCH_REAL_SPAWN`), approved-dispatch scheduler ticks
launch governed runs for an agent label onto that label's registered node: the
tick resolves the label's route, picks the live node (heartbeat fresher than
90 s) that advertises the route's backend — freshest heartbeat wins, node name
breaks ties — and enqueues one `node_dispatch` item carrying the run id. A
label with no registered node falls back to the ordinary local boundary, but a
label that HAS one is fail-closed: all-offline nodes return `node_offline:*`
and live nodes missing the backend return `node_backend_unavailable:*`, so
work never silently runs on the hub instead of the device. The item `cwd` is
the task's worktree when it sits inside a node-reported worktree root,
otherwise the first reported root; a node reporting no roots fails the launch
with `node_no_worktree_root`. Once claimed, the item's claim and the node's
heartbeats renew the linked run's lease (claim also moves it `launched` →
`running`), and the receipt closes the run through the normal outcome path.
Lease expiry is fail-closed both ways: an item never claimed is cancelled and
the run goes `retryable` (`node_unclaimed`), while an expired claimed item —
a lost heartbeat does not prove the backend stopped — fails the run
`lease_expired_outcome_unknown` for operator reconciliation, never relaunching
silently. A run-linked item is invisible to `claim_next` until the run itself
is `launched` — the boundary commits the item before the coordinator marks the
run, so a node must never claim work whose run has not launched. A governed
`dispatch_cancel` on a node-routed run cancels the queue item itself: queued →
cancelled (cessation confirmed, the slot releases), claimed → `cancel_requested`
relayed on the node's next heartbeat with cessation left *unconfirmed* — only
the node's receipt proves the backend exited and finalizes the cancellation.
The same rule binds resume: a run failed `lease_expired_outcome_unknown` cannot
be `dispatch_resume`d, because the old node execution's outcome was never
reconciled and a second run must not overlap it. And the routed boundary
applies the same workspace-binding policy as the local one — to route a
device-scoped label (e.g. `Laptop_*`) to its node, give it an explicit
`UA_AGENT_WORKSPACES` binding; a denied binding fails the launch closed.

Connection failures back off exponentially with equal jitter (1 s base, 60 s cap);
a refused credential waits 5 minutes and asks you to run `unified-agent login`.
`--once` registers, claims at most one item, and exits (a smoke test).

Hub routes (device-flow unless `UA_NODE_ALLOW_BEARER=1`): `/api/node/register`,
`/api/node/heartbeat`, `/api/node/claim`, `/api/node/receipt`,
`/api/node/dispatch` (POST), plus the ACP relays `/api/node/events` (bounded
batches of normalized session events, recorded as evidence and used to
open/close the mirrored `agent_sessions` row) and `/api/node/permission`
(the approval-bridge poll — always a 200 status body of
`pending`/`approved`/`denied` because the re-poll loop *is* the protocol),
`/api/node/session_credential` (device-code OAuth only; see below),
`/api/node/followups` (operator follow-ups for the node's claimed ACP item,
polled while a turn runs and once after each turn; a follow-up is handed out
until the node lists its id in `ack` on a later poll, which moves it to
`claimed`, and the node reports `followup_delivered`/`followup_dropped` on its
event stream — task #1740),
the idle-session wake routes `/api/node/wake` (long poll; ids and unread counts
for opted-in sessions on the node's host; an optional `live_sessions` list keeps idle granted sessions open, task #2428) with `offer_version: 3`, `/api/node/wake/check` (one-shot consent, lease and conservative attempt-budget admission), and
`/api/node/wake/receipt` (`delivered`/`failed`/`skipped`, echoing `offer_id`, `expires_at`, and successful `delivered_at`), polled only with `UA_NODE_WAKE=1` (task #1873),
and harness config `/api/node/config` (GET `?node=` or POST `action=get`
returns the active bundle; POST `action=report` parks drift proposals;
`action=applied` consumes an apply grant). The node pulls that bundle every
`UA_HARNESS_CONFIG_SYNC_S` seconds (default 120) and writes a surface only
after a `harness_config_apply` pass — see
[Web control center — harness config](web-ui.md#hosts-devices-acceptance-and-harness-config).
Queuing work with `/api/node/dispatch` (or Control Center
`POST /api/action/node-session` `start`) is the `node_dispatch` prod action
(`dispatch` scope): it parks a one-shot approval and returns `202` until a
human approves that exact payload, then re-POSTing it queues the item.
Service templates: `deploy/systemd/ua-node.service` (with a cgroup memory
ceiling covering spawned agent CLIs) and
`deploy/mac-hub/com.unified-agent.node.plist`. On the Linux desktop desktop,
`deploy/desktop-bootstrap.sh` installs and enables that unit after device
login.

#### Session-scoped hub MCP for ACP sessions (task #1744)

Off by default. With `UA_ACP_SESSION_MCP=on` on the hub, an ACP session the
node launches can get its own hub credential instead of relying on whatever
standing token the harness happens to have. After `initialize`, if the agent
advertises `agentCapabilities.mcpCapabilities.http` and the hub listed
`acp_session_mcp` in its registration reply, the node calls
`POST /api/node/session_credential` with `{node, item_id, attempt}` and passes
one HTTP MCP server in `session/new` (or `session/resume` / `session/load`):
`{"type": "http", "name": "unified-agent", "url": "<hub>/mcp", "headers":
[{"name": "Authorization", "value": "Bearer <credential>"}]}`. Otherwise the
session gets `mcpServers: []`, as before, and its `session_open` event records
`hub_mcp: "none"` with a `hub_mcp_reason` (`agent_lacks_mcp_http`,
`hub_unsupported`, `hub_disabled`, `node_not_allowed`, `node_opt_out`,
`mint_refused`, `mint_failed`). A failed mint never fails the run.

The credential (`uas_` plus 43 base64url characters) is minted only for a
device-code OAuth sign-in (never a static bearer, even with
`UA_NODE_ALLOW_BEARER=1`), only for the node's own claimed ACP item at its
current attempt, and the hub stores only its SHA-256. It works on `/mcp` only
(any other path answers 403), from the node's own tailnet device, for nine
tools: `profile_status`, `memory_search`, `memory_get`, `memory_list`,
`memory_save`, `task_list`, `task_comment` (on the bound task only),
`session_packet` and `evidence_report`, never more than the label's own
default profile allows. `profile_activate` and `profile_reset` are refused.
It lapses after 10 minutes without a node heartbeat for the running item and
never outlives the item's lease deadline at claim time. It is revoked when the
receipt is posted, when cancellation is requested, when the bound task is
closed or deleted, when the lease expires, when the device's sign-in is
revoked (every request also checks that the device sign-in it was minted
under, identified by the `signin_id` it keeps across refresh rotations, still
exists, so this holds even if the revocation write fails; the OAuth revoke
then answers `status: "partial"` with `session_credentials: "failed"`, and
the node-wide revoke below finishes the job), or by the operator (`POST /api/action/session-credential-revoke` with
one of `credential_id`, `item_id` or `node`). Every check reads the database on
each request, so the next call after any of these answers 401. A device
sign-in made before this change carries no `signin_id`, so its node cannot
mint: run `unified-agent login` again on that node before turning the flag on.

Each call is recorded on `agent.gateway.decision` with a `session` block
(`credential_id`, `item_id`, `attempt`, `node`, `task_id`, `run_id`,
`acp_session_id`, `device_agent`, `device_check`) and `episode_id`
`acp:<item_id>`; lifecycle events are `hub.session_credential.minted`,
`.revoked` (with `reason`) and `.refused` (rate-limited, with the failed
check). None carries the value. The node keeps the value only in the running
backend call, redacts any exact occurrence from events, the receipt and the
agent's stderr log, and the hub redacts `uas_` shapes from every node event.

What a session can read: `task_list`, `session_packet`, `evidence_report` and
the `memory_*` tools reach what the label's own ceiling allows, not only the
session's task; only `task_comment` and comment resources are bound to it. The
harness's stderr log under the node's session directory may hold the value in
plaintext while the run is live (or if the node is killed mid-run); the node
scrubs it when the run ends.

Rollback and incident response: `UA_ACP_SESSION_MCP=off` is a kill switch, not a
revocation. Every outstanding credential stops working while the flag is off,
and turning it back on revives the ones whose item is still running and whose
windows have not lapsed. To end access for good, revoke: `POST
/api/action/session-credential-revoke` with `{"node": "<node>"}` (or
`credential_id` / `item_id`) as the operator. For a graceful rollback, remove a
node from `UA_ACP_SESSION_MCP_NODES`, which is read at mint only. The harness still inherits the node's environment in this phase;
removing `UA_AGENT_BEARER_TOKEN` from it is a later, separately approved step.
See [the design](designs/session-scoped-mcp-credentials.md).

`unified-agent work-product` submits a document/file for named-reviewer
approval and evidence-backed release (task #1278; off by default,
`UA_WORK_PRODUCT_ENABLED=1` to enable):
`unified-agent work-product submit <file> --pack <id> [--title T] [--tag k=v]... [--model M]`
parks a `require_approval` request (or, on an already-approved retry,
releases); `unified-agent work-product verify <id>` re-hashes the
source/released copy and flags `tampered`; `unified-agent work-product list
[--status S] [--pack P]` and `unified-agent work-product packs` list rows and
available template packs; `unified-agent work-product evidence <id>` prints the
human -> device -> agent -> model -> hash -> approval chain read from the
ledger (exit 1 if the ledger chain does not verify); `unified-agent
work-product diff <id>` prints a unified diff against the version it
supersedes (text documents only). `release` was already taken by
`unified_agent/release.py`'s versioned software artifacts, hence the separate
`work-product` namespace. See [docs/work-product-release.md](work-product-release.md).

Separate jobs also test `packages/protocol` (schema codegen must stay
reproducible) and `packages/client` (typed hub SDK). See
[development.md](development.md#pre-commit-hook-and-ci).

### Scheduled surface audit (`surfaces` / steward cron)

`unified-agent-audit surfaces` is the AgentShield-lite orchestrator: the same rule
engine, over **allowlisted** agent-config roots, read-only (never `--remediate`).

```
unified-agent-audit surfaces [-h] [--json]
                             [--fail-on {high,medium,low,info,none}]
                             [--exclude EXCLUDE]
                             [--allowlist ALLOWLIST]
                             [--file-tasks] [--dry-run]
                             [roots ...]
```

- **Roots:** positional directories, or every existing directory on the allowlist.
- **Allowlist:** `--allowlist` (repeatable). If omitted: `UA_AUDIT_ROOTS` when set
  (replace-not-append, same as the MCP tool), otherwise the built-in surface list
  under `$HOME` that exists on disk — `dev/agent`, `dev/unified_agent`, `.claude`,
  `.gemini`, `.hermes`, `.unified_agent`, `.cursor`, `.codex`. A requested root
  outside the allowlist is a usage error (exit 2). A surfaces walk of the
  configured Codex home (`$UA_NODE_CODEX_HOME`, `$CODEX_HOME`, or `~/.codex`)
  skips that home's `.tmp` plugin/marketplace cache when the home is outside
  every git work tree
  ([`_codex_home_cache`](../src/unified_agent/audit/engine.py), task #2316);
  `auth.json` and the rest of the home stay in scope. A `.codex/.tmp` inside
  a repository is still scanned.
- **`--file-tasks`:** files findings at/above `--fail-on` through
  `audit_triage_tasks` dedup (`[audit-triage: <root>:<rule>:<class>]` markers;
  the class is the first segment of the finding's path under the root, such as
  `sessions` or `auth.json`, or `(repo)` for a finding with no path). `--dry-run`
  reports what would be filed without writing. A closed task whose notes also
  carry `[accepted-risk]` keeps its group from being refiled, for its own class
  only: a new class under the same root and rule files a new task (task #2157).
  An older task with a classless `<root>:<rule>` marker covers the classes of the
  finding lines its filer wrote under the `Auto-filed from ...` header (bounded by
  the count in its title); lines appended later do not widen it.
- **Exit codes:** `0` if no finding at/above `--fail-on` (default `high`); `1` if
  the gate trips; `2` on usage / allowlist errors.

```bash
$ uv run unified-agent-audit surfaces --fail-on high --file-tasks
$ uv run unified-agent-audit surfaces --json --dry-run --file-tasks --allowlist "$HOME"
```

`deploy/mac-hub/run-steward-audit.sh` (LaunchAgent `com.unified-agent.steward-audit` at
09:00, systemd `ua-steward-audit.timer`) runs that command, appends
`PASS|FAIL surfaces` to `~/.unified_agent/audit.log`, writes the vault inbox note
on failure, and **exits 1** when the high gate trips. Delivery failures exit 2.
Inbox-note content is still
hash-deduped per day (task #119). Host MCP/hook dirs are scanned only when they
are on `UA_AUDIT_ROOTS` or the built-in default list.

```bash
0 9 * * * $HOME/dev/unified_agent/deploy/mac-hub/run-steward-audit.sh
```

### Standalone ledger binary

`unified-agent-audit-ledger` is the same `run_verify_cli` as
`unified-agent audit verify` (task #746). It does **not** dispatch
`re-anchor` / `evidence` / `surfaces` — those stay on `unified-agent audit …`
/ `unified-agent-audit …`. Enterprise-only (`edition.require_feature("audit_hash_chain")`);
`UA_EDITION=core` exits 2. Exit `0` when the chain is valid or empty, `1`
when tampered, `2` on usage / edition refusal.

```bash
$ unified-agent-audit-ledger
$ unified-agent-audit-ledger --json
```

---

## `unified-agent-tui`

```
$ unified-agent-tui
```

> **Note:** this script has **no argument parsing at all** — `tui.main()` takes no CLI
> flags and does not support `--help`; invoking it (with or without `--help`) launches
> the full Textual dashboard immediately. Confirmed by inspecting `main()` in
> `src/unified_agent/tui.py`, not by running it interactively here.

Behavior from source: `main()` checks `detect_remote_url()` — if `UA_TUI_REMOTE` is set,
or `UA_TRANSPORT=http`, or `~/.claude.json` has an `unified-agent` server configured with
`type: "http"` and a `url`, the TUI connects to that remote hub over the MCP streamable-
HTTP client instead of the local SQLite DB directly. Otherwise it calls
`storage.init_db()` and drives the local database in-process.

Seven tabs: **System Operations** (hub and executor systemd user units with
start/stop/restart, beside a `system_doctor` table), **Tasks Queue**
(claim/status/delete via a `claimed-by:` marker parsed out of task notes),
**Memory & Vault** (`memory_search` + `memory_get` detail view), **MCP Playground**
(run a short fixed list of tools with JSON arguments), **Live Logs Streamer**
(`journalctl` for the hub and executor units), **Collaboration Journal** (renders the
latest `Logs/YYYY-MM-DD.md` vault entries), and **Git Worktrees**
(`git_worktree_list`). Key bindings: `q` quit, `r` refresh, `a` add task,
`m` System Operations, `t` Tasks Queue, `l` Live Logs.

```bash
$ UA_TUI_REMOTE=http://127.0.0.1:8765/mcp unified-agent-tui   # force remote hub mode
```

---

## `unified-agent-merge`

```
usage: unified-agent-merge [-h] [-f] [-d] task_id

Merge and clean up completed task branches and worktrees.

positional arguments:
  task_id        ID of the task to merge.

options:
  -h, --help     show this help message and exit
  -f, --force    Force merge even if task is not marked done, or if there are
                 uncommitted changes.
  -d, --dry-run  Dry run: validate everything but do not merge or delete.
```

Shares `merge.orchestrate_merge()` with the `git_merge_task` MCP tool (see
[tools-reference.md](tools-reference.md#git-toolsgitpy) for the full validation/merge
sequence). `--dry-run` runs every validation step (worktree lookup across the
workspace and known project repos, cleanliness check, audit, pytest) without merging
or deleting anything — equivalent to the MCP tool's `dry_run=True`. Pytest exit
code 5 is reported as "no tests collected", not as a unit-test failure.

After a successful merge the CLI prints a warning and keeps the source when
cleanup is skipped (task #2373): the worktree or branch moved after admission,
the expected-SHA `update-ref -d` was refused, or the branch is still checked
out. `--force` does not delete a source that is no longer the merged commit.
The JSON result (MCP) carries `cleanup_skipped` and/or `branch_retained`.

```bash
$ uv run unified-agent-merge 42                 # merge task 42's worktree into main
$ uv run unified-agent-merge 42 --dry-run        # validate only
$ uv run unified-agent-merge 42 --force          # merge despite dirty worktree / non-done status
```

Exit codes: `0` on success (including a clean dry run); `1` on any error (task not
found/not done, no matching worktree, uncommitted changes, audit/test failure, git
failure) — the CLI prints `❌ Error: ...` to stderr and calls `sys.exit(1)`.

---

## `unified-agent-wiki`

```
usage: unified-agent-wiki [-h] [-d]

Wiki Custodian - Maintains indices and audits vault health.

options:
  -h, --help     show this help message and exit
  -d, --dry-run  Dry run: perform analysis but do not write Health.md.
```

Shares `wiki.run_custodian()` with the `vault_wiki_custodian` MCP tool. Prints a summary
(notes/inbox counts, broken-link count, orphan count) and, unless `--dry-run`, rewrites
`Wiki/Health.md`. `--dry-run` also skips creating `Wiki/` and `00 - Inbox`;
it is a write-free audit (it still reads the vault). A hosted empty vault is refused before the
walk. The JSON/tool payload also includes duplicate candidates,
aliases, empty/stale pages, ambiguous names, and index coverage — see
[memory-tasks-vault.md](memory-tasks-vault.md#wiki-custodian-wikihealthmd).

```bash
$ uv run unified-agent-wiki               # rebuild Wiki/Health.md
$ uv run unified-agent-wiki --dry-run      # report only
```

Exit codes: `0` on success; `1` if the vault path is unset/missing (`ValueError`) or any
other exception occurs.

---

## `unified-agent-sync`

```
usage: unified-agent-sync [-h] [--path PATH] [--client {agy,claude,codex,copilot,cursor,devin,hermes,omp}]
                          [--role ROLE] [--project PROJECT] [--verbose]
                          [--dry-run] [--include-snapshots]

Sync active agent workspace with designated role, tasks, and memory.

options:
  -h, --help            show this help message and exit
  --path, -p PATH       Path to the workspace repository root (default:
                        current directory)
  --client, -c {agy,claude,codex,copilot,cursor,devin,hermes,omp}
                        Target client agent (defaults to auto-detected process
                        tree/env)
  --role, -r ROLE       Manually override the designated agent role (e.g.
                        agent-laptop-agent3)
  --project, -j PROJECT
                        Manually override the project name (defaults to
                        folder/git name)
  --verbose, -v         Print verbose logging/status info
  --dry-run, -d         Dry run: print generated instructions without writing
                        any files or logging to vault
  --include-snapshots   Embed frozen Active Tasks and Recent Project Decisions
                        lists. Off by default (task #564).
```

Detects the running agent's role (`{Location}_{Client}`, e.g. `agent-claude`) from
`UA_LOCATION`/`UA_CLIENT`, the process tree, or platform heuristics, and writes
a **managed context block** into the target workspace's `CLAUDE.md` and
`AGENTS.md`. The default block is stable topology/protocol plus Startup Grounding
that tells agents to call `task_list` / `memory_search` live. Frozen Active Tasks
and Recent Project Decisions lists are **opt-in** (`--include-snapshots`); they
go stale immediately and contradict Verification Before Assertion (task #564).

The write is marker-scoped, never wholesale: only the region between
`<!-- ua-sync:begin … -->` and `<!-- ua-sync:end -->` is managed. Existing file content
outside the markers is preserved byte-for-byte; a file without markers gets the block
appended; re-runs are idempotent (the block is replaced in place). When `--include-snapshots` is set, memory notes are listed as `memory:<id>`
references with tags/author/`updated_at` — titles and bodies are never inlined,
since these files are typically committed. A successful sync appends a
`vault_journal` entry under the detected role.

Two places are never written. The cockpit (`~/agent-workspace`) keeps a
hand-maintained `AGENTS.md` (since 2026-09-27). Any `CLAUDE.md` or `AGENTS.md`
carrying `<!-- ua-sync: off -->` is skipped and left byte-identical (task
#1642); when both targets carry it, sync exits 0 without touching the database
or the journal. This repo's own instruction files carry the marker, because the
generated roster and session-start steps duplicate `~/dev/AGENTS.md`.

```bash
$ uv run unified-agent-sync --dry-run             # preview the generated block
$ uv run unified-agent-sync -p ~/dev/<repo>  # sync a satellite workspace
$ UA_LOCATION=desktop uv run unified-agent-sync -c claude -j unified-agent
```

Exit codes: `0` on success; `1` when the target path is missing, `--include-snapshots`
cannot reach the memory DB, or a file write fails (a failed journal append only
warns). Default sync (no snapshots) does not require a database.

---

## `unified-agent-new-project`

```
usage: unified-agent-new-project [-h] [--path PATH] [--client {claude,agy,codex,hermes}]
                                  [--dry-run]
                                  name

Scaffold a new unified-agent satellite project: a project-scoped .mcp.json plus
the ua-sync managed context block, in one command.

positional arguments:
  name                  Short project name (default dir name + MCP server name)

options:
  -h, --help            show this help message and exit
  --path PATH           Target directory (default: ~/dev/<name>). May already
                        exist — existing content is left alone.
  --client {claude,agy,hermes}
                        Passed through to unified-agent-sync; omit to
                        auto-detect
  --dry-run, -d         Print what would happen; write nothing
```

Wraps the same pattern already used by hand for satellite projects like
`~/dev/sample-myagent`: writes `.mcp.json` with a project-scoped MCP server
(`unified-agent-<name>`, `UA_WORKSPACE` = the target directory — see the
"Scope convention" in `AGENTS.md` for why satellites get their own stdio
registration instead of a redundant user-scoped one), then runs
`unified-agent-sync` against the same path so the ua-sync block in
`CLAUDE.md`/`.agents/AGENTS.md` is populated immediately. Refuses to run if
the target already has a `.mcp.json` — edit that by hand instead of
re-running this. If the target directory doesn't exist yet it's created and
`git init`-ed (no remote); an existing directory is reused as-is.

```bash
$ uv run unified-agent-new-project myagent-two --dry-run        # preview only
$ uv run unified-agent-new-project experiment --client claude   # ~/dev/experiment
$ uv run unified-agent-new-project foo --path /some/other/dir
```

Exit codes: `0` on success (including dry runs); `1` if `.mcp.json` already
exists at the target, the `unified-agent` binary can't be found alongside this
one, or the underlying `git init`/`unified-agent-sync` call fails.

---

## `unified-agent-dispatch-scheduler`

```
usage: unified-agent-dispatch-scheduler {status,ready,tick,observe,runs,report} ...
```

Opt-in poller over the operator-approved delegations. Disabled unless
`UA_DISPATCH_SCHEDULER_ENABLED=1` (or `tick --force` for a one-shot). Does
not install timers; see
[Approved-dispatch enablement](architecture/approved-dispatch-enablement.md).

| Subcommand | Mutates | Notes |
|---|---|---|
| `status` | no | Enablement and tuning (`enabled`, `max_attempts`, `batch_limit`, `lease_ttl_s`) |
| `ready [--limit N]` | no | Approved-dispatch candidates the next tick would claim |
| `runs [--task-id] [--state] [--limit]` | no | Recent `dispatch_runs` rows |
| `observe <run_id>` | no | Operator view: state, retry_count, last_error, evidence |
| `tick [--force]` | yes | One reclaim + claim poll. `--force` does not persist enablement |
| `report {finished,failed,retryable,blocked}` | yes | Terminal outcome for **this** dispatched session |

`report` reads `UA_DISPATCH_RUN_ID` from the environment and refuses (exit 2)
rather than guessing when that var is missing. `--run-id` overrides.
`--evidence` is the SHA/PR/log an exit code cannot carry. `--reason` is
`failure_reason` for a non-finished state.

```bash
$ unified-agent-dispatch-scheduler status --json
$ unified-agent-dispatch-scheduler ready --json
$ unified-agent-dispatch-scheduler report finished --evidence "$PR_URL"
$ unified-agent-dispatch-scheduler report failed --reason "tests failed"
```

**Exit codes for `report`:** `0` recorded; `1` refused without mutating
(`already_terminal` — supervisor and agent both reported is expected);
`2` not inside a dispatched session (`NotDispatchedError`).

**Claude sandbox:** unattended Claude spawn denies `~/.unified_agent`, so this
CLI cannot write `memory.db`. Those children must call the hub MCP tool
`dispatch_report_self` instead. The child env is an allowlist and does not
inherit `UA_AGENT_TOKENS`. See
[Security model](security.md#approved-dispatch-child-isolation).

`--json` is available on every subcommand.

---

## `unified-agent-bug-worker`

Bounded SYSTEM-stream bug-to-patch worker. Off unless `UA_BUG_WORKER_ENABLED=1`.
Subcommands: `status`, `discover`, `run-once`. Full contract:
[bug-worker.md](bug-worker.md).

```bash
$ unified-agent-bug-worker status
$ unified-agent-bug-worker discover --json
$ UA_BUG_WORKER_ENABLED=1 unified-agent-bug-worker run-once --agent agent-agy
```

---

## `unified-agent-sleep-pass`

Nightly memory maintenance. Read-only over `memory.db` (opens `mode=ro`).
Writes one Inbox proposal — it never merges, deletes, or applies. Skips with
exit 0 on battery. Hard-fails (exit 1) when every LLM provider is unreachable
and there was real cluster work to review.

```bash
$ unified-agent-sleep-pass                 # write Inbox proposal
$ unified-agent-sleep-pass --no-write      # print the summary only
$ unified-agent-sleep-pass --threshold 0.6
```

Reviews at most `UA_SLEEP_PASS_MAX_CLUSTERS` clusters per run (default 100).
Deferred clusters stay queued for a later night; the proposal says how many
were deferred. Then `propose_skill_clusters` runs before any LLM work. See
[configuration.md](configuration.md) (`UA_SLEEP_PASS_*`) and
[memory-tasks-vault.md](memory-tasks-vault.md#skill-cluster-proposals-validate-and-hold).

The proposal groups reviewed clusters as Duplicates, Contradictions, Needs
review, Unparsed and Unrelated; only Duplicates carry an apply command. A
contradiction whose judge reason plainly denies any conflict ("not a
contradiction", "same information but differ in format") stays under
Contradictions with a "judge's reason disputes this label" marker and the
matched phrase. The marker appears only when the check is sure: a two-note
cluster, a judge reply read in full, and a short one-line reason with one of a
few exact phrases and nothing else but a closed set of filler words. The reason
text never moves a cluster (task #1778).

Only the judge's structured answer can move a contradiction to Needs review
(task #2022). The judge schema has two optional fields, `self_consistent` (a
JSON boolean) and `conflicting_attributes` (a JSON array of strings). A cluster
moves from CONTRADICTION to NEEDS_REVIEW only when all of these hold: it has
exactly two notes; the reply is one JSON object of at most 1000 characters with
exactly the keys `verdict`, `reason`, `self_consistent` and
`conflicting_attributes`, and no duplicate key anywhere; `verdict` is
`"CONTRADICTION"`; `self_consistent` is `true` (not `"true"`, `1` or `null`); and
`conflicting_attributes` is `[]`. Any other reply, including one that omits the
new fields, keeps CONTRADICTION. These rules apply to the provider leg's
normalized completion text, not the raw provider reply: with a response schema
the `claude_cli` leg strips one ```` ```json ```` (or bare ```` ``` ````) fence and
the whitespace around it before the text reaches the check. A fenced reply, or
one over 1000 characters only because of that padding, can therefore still
qualify. Nothing but the fence lines and whitespace is removed; any other text
before, after or inside the fence fails the schema or the check. Each Needs
review entry shows the judge's original verdict and "downgraded on structured
self_consistent=true, conflicting_attributes=[]". Needs review is never a merge
candidate. Unparsed lists clusters whose model reply could not be parsed,
with the provider and the first 200 characters of the reply on one line as
inline code, so nothing in it renders as a link, image or HTML: a sign the
adjudicator failed, not a finding about the notes.

---

## `unified-agent-ts-worker`

Discord/Telegram ingestion daemon. Off until tokens land in
`~/.unified_agent/service.env`. Replies are conversation only — they never
run commands or mutate tasks. Full enablement, doctor states, and auth
posture: [ts-worker.md](ts-worker.md).

```bash
$ unified-agent-ts-worker    # blocking poll loop; prefer the systemd unit
```

---

## `unified-agent-zulip-worker` / `unified-agent-zulip-automation`

Retained Zulip surfaces. Slack is the current coordination path
([zulip-coordination.md](zulip-coordination.md)). The worker polls and
optionally posts live-chat replies when `UA_ZULIP_CHAT=1`. Automation posts
one bounded digest:

```bash
$ unified-agent-zulip-automation morning
$ unified-agent-zulip-automation evening
$ unified-agent-zulip-automation bridge
```

---

## `unified-agent-pr-watch`

```
usage: unified-agent-pr-watch [-h] [--dry-run] [--repos [REPOS ...]] [--author AUTHOR] [--force]
```

Polls `gh` for PR state across the authenticated user's non-fork repos and
surfaces meaningful transitions into hub memory plus one journal pointer line
per event in the day's pr-watch note (UA #702, task #1920). Source: [`pr_watch.py`](../src/unified_agent/pr_watch.py).

| Flag | Effect |
|---|---|
| `--dry-run` | Print events that would be written. Does not touch memory, the vault, or the cursor. |
| `--repos owner/name …` | Limit the poll. Default: `gh repo list` non-forks (up to 100). |
| `--author` | Memory author + journal agent label. Default `agent-hermes`. |
| `--force` | Run the journal and the Approval Ledger poll even if neither is due. |

**Events** (at most the steps a PR crossed since the last cursor, in order):
`opened`, `reviewed`, `merged`, `closed`. Title edits, label churn, and
`updatedAt`-only CI noise are ignored. A first sighting of a repo seeds
`$UA_HOME/pr-watch-cursor.json` with no events so historical PRs do not flood
memory. The cursor advances only after live writes finish; a crash mid-run
re-emits the same events next tick.

Each live event writes one memory note tagged `project:<owner/name>` and
`pr-watch`, then a journal-format line (capped like `vault_journal`, task #379)
appended to `Logs/pr-watch/<YYYY-MM-DD>.md`. The shared daily journal
`Logs/<YYYY-MM-DD>.md` gets at most one pr-watch line per day: a pointer to that
note, written by the first run of the day that has events (task #1920). Before
that change, per-event lines filled most of the daily journal.
`--dry-run` skips `storage.init_db()`; a real run initializes the schema first
so a fresh `UA_HOME` does not fail with `no such table: memory` (UA #738).

`gh` must be on `PATH`. The Mac LaunchAgent wrapper
(`deploy/mac-hub/run-pr-watch.sh`) prepends `/opt/homebrew/bin` because
launchd's default PATH does not include Homebrew. Each launch runs only what
is due (task #1867, `$UA_HOME/pr-watch-schedule.json`): the journal and the
Approval Ledger poll every 1800 s, and the ledger poll every
`UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S` (120 s) while a ledger row is pending, or
approved inside its TTL, for a PR head the last poll saw open, or when one
conditional request (`If-None-Match`; a 304 is free) shows an open PR head
moved (task #2234). A launch with nothing due, or one that finds another run holding `$UA_HOME/pr-watch.lock`,
prints `skipping this tick`, exits 0 and calls nothing. Launch interval: 1800 s (120 s for the fast ledger cadence). Logs:
`~/.unified_agent/pr-watch.log` / `.err`. This timer is **not** in
`MAC_LAUNCHD_LABELS`, so `system_doctor` does not probe it.

```bash
$ unified-agent-pr-watch --dry-run
$ unified-agent-pr-watch --repos anonb3ll/unified-agent
```

Exit `0` on a clean poll (including "no new events"). Exit `1` on a `gh`
failure or unexpected exception so launchd records the miss.

---

## `unified-agent-sleep-pass`

```bash
unified-agent-sleep-pass --no-write --json
```

Requires an existing initialized `memory.db`; this maintenance command does not
create/migrate the database or start embedding backfills. Battery detection runs
before database access. All memory reads, including evolve, share the preflight's
read-only SQLite snapshot so a concurrent import cannot bypass admission checks.
Admitted runs still run evolve before any LLM call.

Preflight aggregates active (non-index, non-superseded) candidate counts, recent
creation/update counts, invalid timestamps, embedded candidate counts, worst-case
embedded pair comparisons, candidate payload bytes (content/title/tags/embeddings),
and today's journal file size. Candidate selection matches consolidation: expired
notes (`expires_at` in the past) are **still included** in admission counts and
pair-load estimates — `consolidate` does not filter `expires_at`, so sleep-pass
must not either (otherwise preflight would under-count the clustering workload).
Recent counts are distinct notes based on
stored timestamps, **not** an import event ledger; backdated imports cannot be
identified as recent. Future timestamps count as recent; invalid timestamps defer.
Capacity signals are workload estimates, not a host RAM/CPU availability probe.

All settings below have the `UA_SLEEP_PASS_` prefix. Defaults are also hard
ceilings; positive integers may lower them. Invalid, empty, zero, negative, or
above-ceiling values defer the run, rather than disabling protection.

| Setting suffix | Default / ceiling | Meaning |
|---|---:|---|
| `MAX_CANDIDATES` | 6,000 | Active candidate rows |
| `MAX_RECENT_CHANGES` | 500 | Either recent-created or recent-updated count |
| `RECENT_WINDOW_HOURS` | 24 | Lookback window for both recent counts |
| `MAX_PAIRS` | 18,000,000 | Embedded candidate pairs before clustering |
| `MAX_PAYLOAD_BYTES` | 33,554,432 | Candidate payload bytes before loading bodies |
| `MAX_JOURNAL_BYTES` | 65,536 | Journal input size; bounded read checks growth again |
| `MAX_CLUSTERS` | 100 | LLM cluster reviews per admitted run |

Limits are inclusive. `UA_SLEEP_PASS_THRESHOLD` / `--threshold` must be finite
and in `[0, 1]` (default `0.60`). Preflight does no pairwise or LLM work. Any
violated preflight limit defers clusters, digest, pruning, and evolve together;
the report explicitly says they were **not evaluated**. Unknown cluster counts
are `null`, not zero. Deferral writes a report unless `--no-write` is set.
An unchanged oversized workload will defer again; retrying does not drain it.
Review imports or propose a bounded batching change before raising hard ceilings.
`MAX_CANDIDATES` / `MAX_PAIRS` were raised 2000 / 250,000 → 6000 / 18,000,000
(task #997) after the live corpus passed 2000 notes and every nightly run
deferred at preflight. That raise is safe only because
`consolidate.cluster_duplicates` now computes pairwise cosine as one BLAS
matmul (`_pairwise_cosine_matrix`) instead of a Python double loop; a 6000×6000
float32 matrix is ~144 MB and finishes in a fraction of a second. Operators
may lower a ceiling via env; raising the worst-case still requires a reviewed
code change. Existing prompt budgets, compact cluster rendering, and apply
commands remain.

The API result includes `preflight` and content-free `telemetry`. Default CLI
output ends with one JSON completion record; `--json` prints only that record.
Schema version `1`, event `sleep_pass.completed`, includes status, timing, failed
phase, preflight measurements/limits/reason codes, review/defer counts
(`duplicate_count`, `contradiction_count`, `contradictions_disputed`,
`unparsed_count`, `needs_review_count` (pairs moved on the judge's structured
answer), ...), evolve
proposal count, and report requested/written flags. It excludes note contents,
IDs, artifact paths, raw config values, and provider exception messages.
Handled failures preserve completed preflight/evolve progress; API exceptions
still propagate with the record on `sleep_pass_telemetry`. No telemetry DB writes
or external exports are added. Timer stdout/stderr remains the durable log.

| Exit | Status | Automation meaning |
|---|---|---|
| `0` | `ok`, `skipped-battery` | Completed, or intentionally skipped on battery |
| `2` | `deferred-preflight`, `partial` | Incomplete: workload gate or cluster review cap. LaunchAgent/systemd treat this as failure — desired pager signal when incomplete runs persist |
| `1` | `error` | Provider/data/read failure, or requested report could not be written |

`--no-write` suppresses artifacts, not LLM work. The read-only contract also
applies without that flag; reports remain proposals for human review.

---

## `unified-agent-evolve`

Cluster high-trust promoted memories into ranked skill-outline proposals
(UA #839 / ECC-6). Source: [`evolve.py`](../src/unified_agent/evolve.py).
Read-only over `memory.db`. Writes at most `00 - Inbox/Skill-Cluster-Proposals.md`
(or `$UA_HOME/reports/` if the vault is unavailable). Never writes skills or
rules.

```bash
$ unified-agent-evolve            # write Inbox proposal when clusters exist
$ unified-agent-evolve --dry-run  # print markdown; write nothing
```

The nightly sleep pass calls the same function before any LLM work. Human
review is the Inbox note; `skill_review` runs only after a human authors a
`SKILL.md`.

---

## `unified-agent-tool-classification`

Inspects the canonical MCP allowlist and checks it for drift against
`server.REGISTERED_TOOLS`. Source:
[`tool_classification.py`](../src/unified_agent/tool_classification.py).
Does not mutate hub or vault state.

```
usage: unified-agent-tool-classification [-h] [--json] [--markdown] [--check]
```

`--check` exits `1` when classification drift is detected, else prints
`PASS: 0 drift across N served tools: R REACHABLE, W WITHHELD, O OMITTED`
and exits `0`. Without `--check`, `--json` / `--markdown` emit the full
report; the default human summary is the four counts (including
`UNCLASSIFIED`). Tests pin the served count at 186
(`tests/test_tool_classification.py`).

```bash
$ unified-agent-tool-classification
$ unified-agent-tool-classification --check
$ unified-agent-tool-classification --json
```

---

## `unified-agent-vault-sync`

Pulls hub-authoritative vault trees (`Wiki/`, `Logs/`, `00 - Inbox/`) onto
this host. Same `main()` as `python -m unified_agent.vault_sync` — the
installed timer still execs the module form
(`deploy/systemd/ua-vault-sync.service`); the console script is the
operator-facing twin. Prints a JSON payload; exit `0` when
`ok` is true, else `1`.

```
usage: unified-agent-vault-sync [-h] [--dry-run]
```

`--dry-run` (or `UA_VAULT_SYNC_DRY_RUN=1`) reports what would be pulled or
flagged without writing conflict copies. Full contract:
vault-sync-operations.md.

```bash
$ unified-agent-vault-sync --dry-run
$ unified-agent-vault-sync
```

---

## `unified-agent-evidence-export`

Scheduler-friendly signed evidence export. Same `main()` as
`python -m unified_agent.audit.evidence_export_sink` — the inert systemd
example (`deploy/systemd/ua-evidence-export.service`) still execs the
module form. No flags: destination, window, retention, and enablement are
env-var driven. Off by default (`UA_EVIDENCE_EXPORT_ENABLED` unset/`0`).

Exit `0` when `status` is `exported`, `skipped`, or `disabled`; `1` on
other results; `2` on destination / signing errors. Prints JSON to stdout.

The human-facing twin is `unified-agent evidence export`
([evidence-report.md](evidence-report.md),
[connectors.md](connectors.md#evidence-export-a-scheduled-outbound-connector-task-1266)).

```bash
$ unified-agent-evidence-export
```

---

## Module CLI: Lane B evidence (`python -m unified_agent.lane_b_telemetry`)

Read-only validation-gate report (UA #731). Not a `[project.scripts]` entry.
Prints JSON to stdout and never writes hub or vault state.

```
usage: python -m unified_agent.lane_b_telemetry [--vault PATH] [--json]
```

`--vault` defaults to `UA_VAULT_PATH` / `config.vault_root()`. `--json` is the
default. Gate mapping, evidence formats, and the honest-zero vs unavailable
rule live in
[market-validation-evidence.md](market-validation-evidence.md#shipped-evidence-view-lane-b-ua-731).


## Optional exact-head approval preflight (hub-local module)

Immediately before a steward's pinned squash merge, check the selected live PR
head against its authoritative human Approval Ledger row on the hub host, using
the configured hub UA_HOME and operator-identity settings:

```bash
python -m unified_agent.shipping_preflight_cli --repo OWNER/REPO --pr NUMBER \
  --head FULL_HEAD_SHA --approval APPROVAL_ID
```

Exit 0 checks human release approval and the human-attested independent-review
prerequisite. It also holds unless that approval's commit is the live head
and every required CI check is SUCCESS on that exact head: the four
`CI / test (shard=N)` jobs (or `UA_REVIEW_CI_REQUIRED_CHECKS`) plus
`CI / gates` and `CI / full-verification`. A failing, skipped,
pending or missing required check does not pass. When a read is
recorded for an earlier head, it also holds a later head that added
red-tier files with no delta read (task #2259; `review_head` in the JSON
result). An unread PR is not refused for being unread. Unresolved
review threads, steward review, approved scope, held-PR checks and the
serialized deployment procedure remain separate gates.
The selected PR must still be open, non-draft and target main. The Ledger row
must bind that repo, PR, full head and main target branch and have an independent
human resolver. It must be the newest approval request for that PR and have no
worker superseded marker. Returning to an earlier head requires a fresh binding;
an old consumed row cannot clear it.
Its native approval-resolution event must record that same verified resolver
and originator, match the original payload hash, decision and resolution time,
and have a valid entry hash. A human-looking row label without matching verified
resolution evidence is refused. The pass must be approved and unexpired, or
already consumed; consumed bindings intentionally do not use pass expiry. Missing, pending,
denied, revoked, expired, unrelated or agent-only grants fail closed. The check
refreshes the live head and Ledger before returning; rerun after any head change
and immediately before `gh pr merge --squash --match-head-commit FULL_HEAD_SHA`.

The same selected release payload must contain version 1 `independent_review`
metadata, bound by that native verified-human resolution. It names every
contributing author execution/model, the independent review execution/model,
actual execution references, a substantive review reference and an `approve`
verdict for the exact repo/PR/full SHA. The versioned statement in
[`independent_review.py`](../src/unified_agent/independent_review.py) explicitly
attests that the human inspected these references, resolved aliases to canonical
underlying models, verified complete authorship coverage and independent actors,
and confirmed full-diff review without unresolved blocking findings. There is
one release decision, not a second approval. These are **human-attested claims**,
not automatically collected execution telemetry or machine proof that a human
read the evidence.

The reviewer subject and canonical model must differ from **every** author
subject/model. A subject identifies an actual provider task/run/thread or native
agent execution; the label is shared fleet attribution. Distinct child executions
may share a fleet label only when the human verifies their independent actors.
Different actual models within one family pass; switching runtime providers does
not distinguish the same underlying model. Roster preferences, prose, usage
reports, invented subjects and unresolved aliases do not establish independence.
Multiple models for an author must be enumerated. Version 1 supports a reviewer
execution using one actual model; mixed-model review and human-only review hold
as unsupported. This is a v1 scope limit, not a change to the fleet's human
review fallback policy.

After deployment, an authorized lifecycle operator or verified human can submit
the proposal through the **existing** MCP request path:

```python
from unified_agent.independent_review import ATTESTATION_STATEMENT

review = {
    "version": 1,
    "repo": "OWNER/REPO",
    "number": NUMBER,
    "head_sha": FULL_HEAD_SHA,
    "statement": ATTESTATION_STATEMENT,
    "authors": [
        {
            "label": "agent-codex",
            "subject": "t3:actual-author-task-id",
            "provider": "codex",
            "model": "openai/gpt-6.1-sol",
            "execution_ref": "t3://tasks/actual-author-task-id",
        }
    ],
    "reviewer": {
        "label": "agent-codex",
        "subject": "t3:actual-review-task-id",
        "provider": "codex",
        "model": "openai/gpt-6-astra",
        "execution_ref": "t3://tasks/actual-review-task-id",
    },
    "verdict": "approve",
    "review_ref": "https://github.com/OWNER/REPO/pull/NUMBER#issuecomment-ID",
}
coordination_manage(
    action="approval_ledger_rerequest",
    params={
        "number": NUMBER,
        "head_sha": FULL_HEAD_SHA,
        "independent_review": review,
    },
)
```

Use actual inspected references and enumerate all author executions/models; the
example strings are not execution evidence. The existing connector must be
enabled, its repo/base configured, and its native policy must require approval.
Existing operator authorization, service identity, worker/status locks and fresh
GitHub head checks still apply. No new tool, credential, privileged caller knob
or approval type is introduced; HTTP request parity is outside this change.

An enriched proposal replaces only an undecided pending row, after a database
write transaction rechecks the newest row, historical fences and exact payload.
It expires the old proposal and queues a new immutable payload; retries reuse
that row. Concurrent approve/deny/consume wins preserve their decision and refuse
enrichment. Approved rows, including TTL-expired approved rows, are not silently
extended. Denied, consumed and settled outcomes retain their existing fences;
native revoke/re-request is available only where already supported, and a
consumed/settled head may require a new head. A failed status POST can be retried
with the stored complete payload. No past approval is retroactively an
attestation of fields it did not contain.

The Control Center table opens release details before approval. The drawer shows
the full bounded statement, exact PR/SHA and all authors/reviewer subjects,
models and evidence refs, with one **Approve release and attest review** action.
Invalid or redacted evidence disables that UI action. Native API/MCP human
resolution of an enriched payload affirms the same statement: the human must
inspect its exact immutable payload and evidence first. This convention does not
claim machine-enforced proof of reading through every adapter.

Rollout uses the currently deployed shipping rules and preflight for this change
itself. Do not run candidate mutations against the hub to bootstrap approval or
pretend an old payload includes this new attestation. The new gate applies once
deployed. Its result scope is `exact-head-human-attested-review`; it returns
redacted actor/model metadata and SHA-256 evidence-reference digests. Public
GitHub comments expose only a generic version/count/verdict summary and the
existing native approval pointer, with no supplied actor/model/ref strings or
their guessable digests. Complete named models and references remain in
the native Ledger: human approval does not waive their privacy. Legacy release
payloads without review metadata hold on the new gate.

This module opens the existing hub store read-only. A missing or retired local
store is refused; satellites must use the hub host. It creates no approval,
consumes no pass, publishes no status, merges nothing and changes no GitHub
rules. No existing shipping entrypoint invokes this module. Adoption and
verification in the steward's shipping workflow are a separate delivery gate;
this helper alone does not fix release control. An unprotected GitHub branch
still permits out-of-band `gh` merges. This check is not repository-enforced
protection. Prior PR-level comments or nominal green
statuses do not replace the exact-head Ledger check. New Ledger comments name
the approved head explicitly; the authoritative row remains the binding.

A ready result includes `review_head` (`status`, `read_head`,
`red_tier_delta`): `read-at-head`, `delta-read`, `delta-clean`, or
`no-read-recorded`. See
[automation.md](automation.md#shipping-preflight-head-moved-red-tier-hold-2259).


## Module CLI: local pytest attestation (`python -m unified_agent.pytest_attestation`)

Sign a local pytest run as an in-toto test-result statement in a DSSE
envelope, or verify one. Not a `[project.scripts]` entry. Keys stay in the
environment (`UA_TEST_ATTESTATION_KEY` / `UA_TEST_ATTESTATION_PUBLIC_KEY`);
the file `attestation/local-tests.intoto.json` is gitignored. Usage,
sampling, and the CI skip-vs-required switch live in
[development.md](development.md#local-pytest-attestation).

```
python -m unified_agent.pytest_attestation sign -- tests/test_area.py -q
python -m unified_agent.pytest_attestation verify attestation/local-tests.intoto.json
python -m unified_agent.pytest_attestation ci [--seed HEAD_SHA] [--run]
```
