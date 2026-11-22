# Administrative agent access and managed secrets

Task #1097 adds opt-in platform controls. No live configuration changes are
included. Operator environment changes and any required restart are separate
human actions; agent tools cannot alter these settings.

## Agent access

| Server setting | Default | Effect |
| --- | --- | --- |
| `UA_AGENT_ACCESS_ENABLED` | `1` | `0` refuses agent reads and writes |
| `UA_AGENT_ACCESS_READ_ONLY` | `0` | `1` refuses mutations, including exports that write to the vault |
| `UA_AGENT_ACCESS_PRODUCTION_BLOCKED` | `0` | `1` refuses production and unknown-environment access, including reads |
| `UA_RUNTIME_ENV` | unset | Trusted server classification: `development`, `validation`, or `production` |

The three switches are independent. Boolean settings accept 1/0, true/false,
yes/no, or on/off. Invalid values fail closed. `profile_status.agent_access`
reports each value (null when unknown), `known`, and `source` (`configured` or
`legacy-default`). The authenticated MCP diagnostic remains callable when agent
access is disabled; a default is never represented as a verified configuration.

The common registered-tool boundary, MCP resource handlers, and authenticated
HTTP/API boundary enforce the controls before execution. HTTP denials return
403 with the named control. Public login/health routes remain reachable. A
verified `Desktop_Person` browser/bearer identity can reach the human UI recovery
surface and resolve a pending approval; it does not remove downstream tool,
profile, policy, or approval gates. Signed Slack mutations and accepted
elicitation responses run the same access check before changing a task or
approval row. An unverified agent-name string grants no exception.

A Control Center session signed in through WorkOS (task #2114,
[Railway runbook](deploy/railway.md#public-ingress-with-workos)) is a `cookie`
session whose requests are also bound as `workos`: the hub re-checks the
member's organization membership live on every write and at least once a
minute on reads, and an approval it resolves is stamped `workos-idp-v1`.

The recovery exemption needs the configured operator label on a human auth
channel: `bearer`, `oauth`, `cookie` or `workos`
(`agent_access.OPERATOR_RECOVERY_VIA`, the one set the hub middleware and the
routes share). With agent access disabled, the operator can still read the
event log through all three HTTP reads: `/api/ledger/rows`,
`/api/events/stream` and `/api/events` (task #1811). `/api/events` runs the
registered `event_log_query` under a grant that covers that one read-only tool
call for that one identity, so agents stay refused and no other tool is
exempted. A refused `/api/events` read answers 403 (429 when throttled, 202
when parked for approval), not an error inside a 200. MCP calls over `/mcp`
have no recovery exemption.

With production blocked, development/validation permit only server-classified
local operations. External and unclassified operations remain blocked even when
a client supplies a development-looking project, URL, or environment value.
Unknown/mixed tool operations are conservatively treated as writes. Ordinary
audit/telemetry recording still occurs when read-only requests are evaluated.

## Device sign-in (OAuth device flow)

Task #1320 (Epic J, #1318). Instead of copying a static bearer from
`UA_AGENT_TOKENS` onto each machine, a device can sign in the way a connector
does:

1. On the device: `unified-agent login --hub https://<hub>:8765 --agent agent-codex`.
   It prints `https://<hub>:8765/oauth/device?user_code=XXXX-XXXX`.
2. Open that link, check the device name and code, pick (or confirm) the agent
   label, approve as a verified human (see below). Deny needs no approver.
3. The device receives an access token plus a 30-day rotating refresh token for
   the built-in public client `unified-agent-cli`. Tokens authenticate like any
   hub OAuth token (`ua_identity_via = "oauth"`, `auth_method = "device_code"`).

What is bound and recorded:

- The requesting peer's tailnet node (`tailscale whois`, via `tailnet.py`) is
  captured at `/oauth/device_authorization` and stamped into the token's claims
  as `device_node` (`node`, `node_id` only — no keys). It survives refresh and is
  shown by `/oauth/whoami` and in request auth metadata.
- `hub.oauth.device_authorization`, `hub.oauth.device_approved` and
  `hub.oauth.device_denied` audit events carry the device node and the
  approver's node; never the device code.

Protocol details: user codes use RFC 8628 §6.1's consonant alphabet
(`XXXX-XXXX`, case and separators ignored); codes live 600 s; polling faster
than the 5 s interval returns `slow_down` and adds 5 s; approved codes are single
use; codes are bound to the client that requested them. On a `UA_HTTP_PUBLIC_HOSTS`
Host, approval is pinned to `UA_MCP_OAUTH_AGENT` exactly like `/oauth/consent`.

Who uses the saved sign-in: the executor daemon (`HubClient`) uses it only
when the saved hub origin **and** the approved agent label (recorded at login
via `/oauth/whoami`) match its own; otherwise it falls back to its configured
bearer. IDE MCP hosts (Claude Code, Cursor, Codex, …) do **not** read the file
yet: they still authenticate with their own OAuth or bearer configuration until
J6 (#1325) / I4 (#1330). `login` refuses plain `http://` except for loopback and
tailnet hosts (100.64.0.0/10, `*.ts.net`), where WireGuard already encrypts;
`--allow-insecure` overrides for lab networks. Device provenance comes from the
direct peer only: forwarded headers are never trusted. On the current Mac hub,
the HTTPS Tailscale Serve issuer preserves the client's tailnet peer, so the hub
can resolve its device node; the raw TCP tailnet endpoint presents loopback.

Where it works: the hub only serves `/oauth/*` when `UA_MCP_OAUTH=1` and
`UA_MCP_OAUTH_ISSUER` is HTTPS or loopback, so run `login` against that issuer
URL. A plain-HTTP tailnet URL passes `login`'s transport check but reaches the
device endpoints only where the issuer itself is that URL; otherwise `login`
reports that the OAuth server is off. `login --out PATH` writes elsewhere; point
executors at it with `UA_OAUTH_TOKEN_PATH=PATH` (default
`$UA_HOME/oauth-token.json`, `~` expanded). Bearer and refresh requests never
follow redirects. Manual entry at a bare `/oauth/device` first asks for the code
and only then renders an approval form whose token is bound to that one code.

Current limits (tracked under Epic J): the device binding is recorded but not
yet enforced against the calling peer (J2/J6).

## Who may approve a sign-in (verified-human consent)

Task #1322 (Epic J3). `/oauth/consent` and `/oauth/device` approve only when the
approver is a verified human. `oauth_approver.verify` tries, in order, and fails
closed:

1. **WorkOS** (enterprise edition, hub WorkOS leg on — `UA_HUB_WORKOS_ORGANIZATION_ID`
   + `UA_HUB_WORKOS_IDENTITY_MAP`). A sealed AuthKit session verified by the same
   `WorkOSSDKProvider` checks as the hub leg (JWKS, issuer, the one organization,
   live membership and session). It arrives as `Authorization: WorkOS <sealed>` or
   as the `ua_oauth_workos` HttpOnly cookie (path `/oauth`) that the approval page's
   "Sign in with WorkOS" link sets through `/oauth/approver/workos/login` →
   AuthKit → `/oauth/approver/workos/callback`. Register
   `<issuer>/oauth/approver/workos/callback` as a redirect URI in WorkOS. The
   approver recorded is the mapped UA label plus the WorkOS subject.
2. **Tailnet + passkey** (private edition, `UA_MCP_OAUTH_APPROVER_LOGINS`). The
   *direct* peer must resolve through the local tailscaled to a node owned by one
   of those logins, and the browser must present a WebAuthn assertion (user
   verification required) from a passkey that login registered. A valid passkey
   from any other node is refused, and a failed passkey is never downgraded to the
   passphrase. Use the HTTPS issuer origin for this Mac hub's approval page:
   Tailscale Serve preserves a bindable tailnet peer there (measured as
   `<peer-tailnet-ipv4>`). The raw TCP tailnet endpoint (`http://<hub-ip>:8765`)
   presents `127.0.0.1` to the hub and cannot bind an approver node. Other
   proxies must be checked for their actual peer address. The hub ignores
   `X-Forwarded-For` and `Tailscale-User-*` headers; a spoofed forwarded IP did
   not change the peer resolved on the issuer origin.
3. **Passphrase** — only with `UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1`.

Register the passkey once: open the Control Center **at the issuer origin**
(`UA_MCP_OAUTH_ISSUER`; WebAuthn needs a secure context and a domain, not an IP)
from a device owned by the approver login, signed in as a *human* identity
(`Desktop_Person`), then Devices → Approver passkeys → Register. The API is
`GET|POST /api/oauth/passkeys`, `POST /api/oauth/passkeys/options`,
`POST /api/oauth/passkeys/delete`; all three writes require the human identity,
the approver's own node, and the UI CSRF token. Registration uses
`attestation: "none"` and the browser's `getPublicKey()` (SPKI); ES256, EdDSA and
RS256 are accepted. Only public keys, credential ids and counters are stored
(`oauth_passkeys` table).

The passkey API remains available while OAuth is disabled so an operator can
register a passkey before enabling verified-human consent. Its human identity,
direct tailnet peer, and CSRF checks still apply; the approval pages remain
inactive until OAuth is enabled.

What is recorded: the consent/device-approval events and `hub.oauth.token_issue`
carry `human: {verified, verified_via, approver}` plus the non-secret approver
detail (`workos_subject`/org/session id, or tailnet login/node and a passkey id
prefix). The token's claims carry the same block as `human_approval`; it survives
refresh and appears in `/oauth/whoami` and in request auth metadata
(`config.current_auth_metadata()["human_approval"]`). `verified` is true only for
`workos` and `tailnet+passkey`.

What a passphrase holder can no longer do: approve a consent or a device, or mint a
token for any label, unless the operator turned the fallback on. Existing tokens
keep working either way; a hub with only the passphrase configured reports
`mcp_oauth` DEGRADED in the doctor.

## Write-only managed values

`UA_MANAGED_SECRETS=1` enables a deliberately narrow hub tool surface:
diagnostics, protected file reads, platform resource reads, and the approved
`managed_secret_set` sink. Arbitrary execution, indirect delegation/workflows,
and unclassified tools are refused. An invalid configured flag also retains
these restrictions. Turning this mode off intentionally removes its execution
restrictions; this is not an OS sandbox against another same-UID process.

`managed_secret_set(name, value, consumer="zulip")` requires verified identity,
a name containing 1–128 ASCII letters/digits/colon/underscore/hyphen, a nonempty
value of at most 65536 bytes, and the supported consumer. The existing human
approval queue receives only name, consumer, verified identity, and a SHA-256 digest. Generic policy
and the sink both enforce approval, sharing a consumed receipt only within that
single invocation. Subsequent requests need a fresh pass. Pending/denied calls
never store a value; no approval is restored or self-resolved.

Values live under `UA_HOME/managed-secrets`, outside configured workspace,
project, active-worktree, and vault roots. The directory is 0700; atomic files
are 0600. Symlink/hardlink/non-regular-file reads are rejected. Public setter
results contain metadata and a digest; public read-back raises `SecretReadDenied`.
No value serializer/export endpoint is registered. File tools cannot traverse
into the store, and the narrow execution surface prevents a hub tool from
running Python or shell code to read it indirectly.

The internal Zulip credential loader consumes `zulip:<agent-label>`, using the
existing protected Zulip configuration for email/site only. In managed mode the
stored key replaces the legacy key. Missing/mismatched stored keys fail closed.
No credentials are migrated, rotated, or copied automatically. Existing secrets
in arbitrary documents, provider responses, host files, other processes, or
previous transcripts are outside this guarantee; they are not silently scrubbed.

## Verification and rollout proposal

Run `uv run pytest -q tests/test_agent_access.py tests/test_managed_secrets.py tests/test_approvals.py tests/test_policy.py tests/test_docs_tool_count.py`.
Tests use disposable state and synthetic values. They exercise actual registered
tools and ASGI requests, denied reads/writes, unknown configuration, external
production targets, filesystem escape attempts, one-shot approval consumption,
and absence of the secret marker from approval/event/performance records.

Before opting in, the operator reviews the resulting restricted tool inventory,
provisions one synthetic key through the approval flow in validation, verifies
internal consumption and denied read-back, and retains a separate human recovery
session. Repeat with real credentials only through the protected input channel.
No deployment or real credential exercise was performed for this PR.
