# Security policy

unified-agent enforces a sandbox, an approval gate, and a tamper-evident
evidence ledger. A flaw in any of them matters. Thank you for reporting one
responsibly.

## Supported versions

unified-agent is pre-1.0. Only the current minor release line receives
security fixes.

| Version | Supported |
|---|---|
| 0.3.x | Yes |
| Earlier than 0.3 | No |

The current version is in [`pyproject.toml`](pyproject.toml). Fixes land on
`main` and ship in the next 0.3.x release. Interfaces may change between minor
versions before 1.0.

## Report a vulnerability

Report privately. Do not open a public issue, pull request, or discussion for
a suspected vulnerability.

1. Open the GitHub repository for the version you are using.
2. Choose the **Security** tab.
3. Choose **Report a vulnerability** and fill in the form.

This opens a private advisory that only you and the maintainers can see. If
you cannot use it, open a public issue that says only that you need a private
channel. Do not put details in it.

### What to include

- The affected version or commit, and the transport (stdio or HTTP hub).
- The component: for example `safety.py`, a named tool, the HTTP hub, or the
  approval queue.
- Steps to reproduce, or a proof of concept. A failing test is ideal.
- The impact: what an attacker can read, write, run, or bypass, and what
  access they need first.
- Your configuration: relevant `UA_*` settings and the agent profile in use.
  Remove real tokens and secrets before you send anything.
- Whether you plan to disclose publicly, and when.

### What to expect

These are goals, not guarantees. This is a small project.

- We aim to acknowledge a report within 5 business days.
- We aim to give an initial assessment within 14 days.
- We aim to fix confirmed high-severity issues quickly, and to agree a
  disclosure date with you before we publish.
- We credit reporters in the advisory unless you ask us not to.

Please give us reasonable time to fix an issue before you disclose it. Test
only against installations you own. Do not access data that is not yours.

## Scope

### In scope

- Escapes from the workspace sandbox (`UA_WORKSPACE`) or the vault sandbox
  (`UA_VAULT_PATH`, `UA_VAULT_WRITE_DIRS`), including symlink, `..`, and
  hook-directory (`.git`, `.husky`) bypasses.
- Bypasses of the `run_cmd` command allowlist, or execution through a shell.
- Authentication bypass on the HTTP hub, including access without a valid
  bearer token when `UA_ENFORCE_AUTH` is on.
- Bypass of the approval gate, or of separation of duties (an identity
  approving its own request, or an anonymous approver).
- Escalation of a session profile, for example `research` or `operator`
  reaching a tool it should not.
- Tampering with the evidence ledger that verification does not detect (see
  `event_log_verify` and `unified-agent-audit-ledger`).
- Server-side request forgery through `web_fetch` or the shared HTTP client,
  including DNS rebinding and redirect tricks.
- Leakage of secrets or tokens into events, reports, logs, or tool output.
- A `security_audit` rule that silently misses a high-severity pattern it
  claims to catch.

### Out of scope

- Issues that need an already-compromised host account or a stolen token.
- Misconfiguration that contradicts the documentation, such as binding the
  hub to `0.0.0.0` without authentication or setting `UA_ENFORCE_AUTH=0`.
- Stdio sessions acting without identity gates. This is documented behavior;
  see [Security posture](docs/security-posture.md).
- Bugs in third-party harnesses and agents (Claude Code, Codex, Cursor, and
  others) that are not caused by unified-agent.
- Vulnerabilities in third-party dependencies with no demonstrated impact on
  unified-agent. Report those upstream.
- Denial of service through resource exhaustion by an authenticated,
  authorized caller.
- Findings from automated scanners without a working proof of impact.

If you are unsure whether something is in scope, report it privately.

## Security model at a glance

Every call passes through the same layers, and each layer only narrows what
the layer before it allowed:

1. **Identity.** HTTP callers present a per-identity bearer token. HTTP hubs
   fail closed when `UA_ENFORCE_AUTH` is unset.
2. **Profile.** A session profile decides which tools a caller can see.
3. **Policy and approval.** Reserved actions wait in a queue for a human
   decision. The requester cannot approve their own request.
4. **Sandbox.** Paths and commands are checked at the chokepoints in
   [`safety.py`](src/unified_agent/safety.py): no shell, an allowlist, symlink
   resolution before containment, and an SSRF guard on fetches.
5. **Evidence.** Decisions and actions are recorded in a hash-chained ledger
   that can be verified afterwards.

Read more:

- [Security model](docs/security.md): sandboxes, allowlist, SSRF guard, token
  lifecycle, and the audit rule catalog.
- [Human in the loop and evidence](docs/human-in-the-loop.md): the approval
  gate, separation of duties, and the ledger.
- [Security posture](docs/security-posture.md): the auth and approval flags and
  their defaults.

The `unified-agent-audit` command checks a repository against the audit rule
catalog. Run it on your own deployment and on any change you propose:

```bash
uv run unified-agent-audit . --fail-on high
```

## Operator guidance: secrets

- Never commit `service.env`, tokens, or `.env` files. Keep the hub's
  `~/.unified_agent/service.env` readable only by its owner (mode 600).
- Issue one token per agent identity in `UA_AGENT_TOKENS`. Do not share a
  token between identities. A shared token defeats identity attribution and
  separation of duties.
- Keep `UA_ENFORCE_AUTH=1` on any hub that is reachable beyond loopback.
- Bind the hub to loopback or a private interface. Do not expose it to the
  public internet.
- Rotate a token when it may have been exposed, and on a regular schedule.
  `system_doctor` warns when an active token is older than
  `UA_TOKEN_MAX_AGE_DAYS` (default 90). The `agent_token_rotate` tool records
  the rotation in the evidence ledger. It does not edit `UA_AGENT_TOKENS` or
  restart the hub. You must replace the value in the service environment and
  restart the hub yourself. See the
  "Token lifecycle" section of the [security model](docs/security.md).
- If a secret reaches a log, transcript, or commit, treat it as exposed and
  rotate it. Removing it from the file is not enough.
- Set `UA_MANAGED_SECRET_KEYS` so stored secrets are encrypted at rest. See
  [Configuration](docs/configuration.md).
