# Hosted hub capability boundary

Task #1442. A containerised hub does not have a local checkout, a vault, or
the iMessage bridge. Tools that would otherwise return an empty success in
that state fail closed. This page does not enable a deployment mode. The
mode enum (local-trusted / authenticated-private / authenticated-public)
stays with #1422.

A different boundary, which agent work may run on the hub at all, is
`UA_EXECUTION_LOCALITY` ([execution-locality.md](execution-locality.md), #2109).
A hosted hub defaults to `devices`: it coordinates and never starts an agent.

## What the image actually sets

`Dockerfile` creates `/data/workspace`, runs as uid 10001, and sets:

| Variable | Image value |
|---|---|
| `UA_HOME` | `/data` |
| `UA_WORKSPACE` | `/data/workspace` (empty until a volume is mounted over it) |
| `HOME` | `/data` (so the default vault `~/vaults/main` is `/data/vaults/main`, which the image does not create) |

`projects/imessage-bridge/src/send.ts` is not copied into the image.
`notify` resolves that script from `UA_BRIDGE_DIR` or
`<process workspace>/projects/imessage-bridge` (`process_workspace_root()`,
not a per-agent workspace). Set `UA_BRIDGE_DIR` when the bridge does not
live there. `fs_write` refuses the resolved bridge tree.

## What still works

Identity, tasks, memory, approvals, the evidence ledger, and `/healthz`
do not need a workspace, a vault, or the bridge. They use `UA_HOME`.

## What fails closed

The guard is active when the workspace directory is empty **and** either
the resolved root is `/data/workspace` or `UA_HOSTED_WORKSPACE_GUARD=1`.
That variable is read in every process, not only under pytest. Tests set it
because a temp directory is not `/data/workspace`. An operator with some
other empty workspace path can set the same variable and get the same
refusal. Setting it does not enable a deployment mode. A populated tree,
including a normal single-tenant host, is unchanged. An ordinary empty temp
directory is unchanged unless the variable is set.

| Family | Empty success this closes | What you get instead |
|---|---|---|
| `fs_list`, `fs_search` | `{entries: []}` / `{matches: []}` when the resolved path is inside an empty hosted workspace | `ToolError` naming the empty workspace. An absolute read of a populated project checkout is unchanged |
| `run_cmd` for `ls`, `find`, `rg`, `grep`, and git subcommands other than `init` / `clone` | exit 0 and empty output from a census of that tree | `ToolError`. `run_cmd_check` reports the same refusal as a block. `git init` and `git clone` still run, as do commands such as `echo`, and a listing aimed at an absolute path outside the workspace. A `grep`/`rg` pattern is not a path (`rg --files` has no pattern). `git -C`, `--git-dir`, and `--work-tree` are resolved before the target is classified |
| `git_*` | a non-repo hosted workspace | `ToolError` from the same guard, before the generic "not a git repository" error |
| `vault_list`, `vault_search` | `{entries: []}` when the vault directory exists and is empty, under the same guard | `ToolError`. A missing vault already raises from `resolve_in_vault` on every host. `UA_VAULT_PATH=""` still means vault tools are disabled on purpose |
| `notify` | (already an error) | `ToolError`: notification bridge is absent, `send.ts` not found |

`system_doctor` adds `hosted_local_dependencies`. Status is `LIVE` when the
workspace, a vault directory, and `send.ts` bridge are all present.
`UA_VAULT_PATH=""` disables vault tools on purpose; that is not a present
vault, and the row is not `LIVE` (the family is `vault (disabled)`). It is
`DEGRADED`, naming the absent families, only while the hosted guard is
active. On a normal host where those trees are simply not all present, the
row is `DORMANT`, so `unified-agent doctor` does not fail a fresh install.
The probe reads the configured `UA_WORKSPACE`, not a request-scoped agent
binding, so a DENY identity and an agent bound to another directory get the
same family names rather than `UNKNOWN` or a false all-clear. A custom
empty workspace path stays off this guard unless `UA_HOSTED_WORKSPACE_GUARD=1`
is set. That opt-in is available to an operator and is what tests use; it
is not a deployment mode. That row is a report. It does not switch the hub
into a new mode.

## What a normal host should see

A host whose `UA_WORKSPACE` is a populated checkout, whose
`UA_VAULT_PATH` is a populated vault, and whose bridge script is on disk
stays on the previous tool behaviour, and the doctor row is `LIVE`.
