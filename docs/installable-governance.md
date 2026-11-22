# Installable governance

This is the shortest path for an operator who is not the operator to run a
governed Unified-Agent hub and connect the web Control Center.
It uses local storage and an optional Tailscale exposure. It does not require a
hosted backend.

## 1. Install and bootstrap

On the machine that will own the hub:

```bash
git clone https://github.com/anonb3ll/unified-agent ~/unified_agent
cd ~/unified_agent
uv tool install .
unified-agent init
chmod 600 ~/.unified_agent/service.env
```

> The GitHub remote is private: an anonymous `git clone` (or
> `uv tool install git+https://…`) fails on a machine without credentials for
> it. External operators install from a source archive of this repository
> instead (`uv tool install /path/to/unified_agent`), or ask the owner for
> access. Verified cold-start path: transfer the source tree, then
> `uv tool install .` from it.

`init` creates an empty database, a private workspace, and one generated bearer
token in `~/.unified_agent/service.env`. It is idempotent and does not rotate an
existing token unless `--force` is supplied.

After pulling source changes, re-run `uv tool install .` before restarting the
service: `ExecStart` runs the installed console script, not the checkout, so a
plain restart does not pick up new code.

## 2. Configure the hub boundary

Edit `~/.unified_agent/service.env` and keep the generated token, then set the
transport and roster for this installation:

```dotenv
UA_TRANSPORT=http
UA_HTTP_HOST=127.0.0.1
UA_HTTP_PORT=8765
UA_ENFORCE_AUTH=1
UA_AGENT_LABELS=operator
UA_AGENT_TOKENS=operator:replace-with-a-long-random-token
UA_APPROVAL_TOOLS=run_cmd:fs_write:git_merge_task:task_delete
```

Use a token generated outside the repository, for example:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The label in `UA_AGENT_TOKENS` is the identity recorded in the audit trail.
Only a human identity can approve, and in 0.3.2 the hub recognizes humans from
a built-in list. Use `Desktop_Person` instead of `operator` for the person who
will approve. See [Getting started](getting-started.md#step-3-define-who-is-who).
`UA_APPROVAL_TOOLS` is a hub-side, global approval queue. It applies to every
listed registered tool, on MCP, Control Center and workflow calls alike, except
that a tool that gates itself (such as `gcloud_ops`) applies its own per-action
rules; see [the approval gate](human-in-the-loop.md#the-approval-gate). Listed calls pause
until an operator approves the exact request in the Control Center or through
`approval_resolve`. It is not a client-side opt-out.

Do not commit `service.env`, put tokens in a vault note, or bind the hub to
`0.0.0.0`.

## 3. Start the persistent hub

Install the user service from the checkout:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/unified-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unified-agent
unified-agent doctor
```

The expected result is no `MISCONFIGURED` checks. `DORMANT` optional features
such as vault, backup, or notifications are normal on a first install.

On a headless server (SSH-only), enable lingering for your user so the hub
survives after you log out — without it systemd tears down the user manager and
the unit dies with the session:

```bash
sudo loginctl enable-linger $USER
```

Verified on a clean Ubuntu 24.04 cloud image: without lingering, the hub came
up and served requests only while an SSH session was open, then died silently
between sessions.

For remote clients, expose the loopback hub through the machine's tailnet:

```bash
tailscale serve --bg --tcp 8765 127.0.0.1:8765
```

Then add the client hostnames to `UA_HTTP_ALLOWED_HOSTS` in `service.env`,
restart the unit, and use the resulting tailnet URL with `/mcp`.

## 4. Open the Control Center

The hub UI is available at:

```text
http://127.0.0.1:8765/ui
```

The browser session uses the hub's `ua_ui` cookie. It is separate from MCP
bearer authentication. The UI shows agent identity, approvals, task activity,
and audit evidence from this hub's database.

Embedding the Control Center in T3 Code was a 2026-08 experiment that was never
merged upstream. Upstream T3 Code does not read a Control Center URL, so this is
not supported there. Open the `/ui` URL in a browser; for a tailnet hub, use
that hub's `/ui` endpoint. No the operator-specific address is required.

## 5. Verify the install

Run these checks on the hub machine:

```bash
systemctl --user is-active unified-agent
unified-agent doctor
```

Then open `/ui`, confirm the authenticated agent label appears, and submit one
small approval-gated action. It should appear as pending before any side effect
occurs. Resolve it explicitly, then confirm the result and actor in the audit
view.

This setup covers the installable local governance layer. Hosted multi-user
accounts, SSO, and autonomous dispatch remain separate product decisions.
