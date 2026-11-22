# Codex Remote stdio session

Use this procedure for an isolated Codex Remote session. It is not the desktop
hub setup. It creates a disposable local server process for one remote session.

## Use this mode when

- The session must not connect to the operator's desktop HTTP hub.
- The session needs a local, temporary workspace boundary.
- The operator accepts that state is local to the remote environment.

Do not use this procedure on the desktop. The desktop uses the authenticated
HTTP hub in the operator desktop manual.

## Configure Codex

Add a `unified-agent` entry to the remote user's `~/.codex/config.toml`. Use
TOML. Do not copy a Claude JSON configuration.

```toml
[mcp_servers.unified-agent]
command = "/absolute/path/to/unified-agent-stdio-wrapper"
args = []

[mcp_servers.unified-agent.env]
UA_TRANSPORT = "stdio"
UA_WORKSPACE = "/absolute/path/to/workspace"
UA_HOME = "/absolute/path/to/disposable-ua-home"
UA_VAULT_PATH = "/absolute/path/to/vault"
UA_VAULT_WRITE_DIRS = "00 - Inbox:Logs:Wiki"
```

The wrapper should start this checkout's `.venv/bin/unified-agent`. It should
unset HTTP token and hub variables. Do not put a bearer token in this file.
Stdio does not listen on a network port.

## Verify before work

Start a fresh Codex session. Ask the agent to call `system_doctor`, then call
`profile_status`. Confirm the workspace and vault paths are correct. Do not
edit, commit, push, or create a task until this check passes.

## Limits

- `profile_activate` needs an authenticated HTTP session. It is unavailable here.
- `vault_move` is unavailable in this isolated operating mode.
- `/ui` needs the HTTP hub. It is unavailable here.
- Memory and tasks stay in the remote `UA_HOME`. They do not reach the desktop database.

Use a Git repository for source handoff. Use the approved vault inbox for
reader-facing notes. End the remote session after the handoff is complete.

## Troubleshooting

If Codex does not list the server, check the TOML path and the wrapper's
absolute executable path. Start a new session after every configuration edit.
If a tool sees the wrong workspace, stop and correct `UA_WORKSPACE`. Do not
expand a sandbox boundary as a quick fix.
