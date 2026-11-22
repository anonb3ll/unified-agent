# Hub-brokered connector OAuth (J5)

This opt-in client flow starts from the Control Center's Settings → Integrations
page (`/ui/settings/integrations`). The hub
generates one-use OAuth state and PKCE S256 verifier, exchanges the provider's
authorization code, and stores the grant as an encrypted managed secret. The
browser receives connection status, never access or refresh tokens.
The connect dialog sends the tab to the provider's sign-in page, and the hub's
callback answers with a plain status page. Open Settings → Integrations again
to see the new connection.
Connect and Disconnect controls appear only after a human operator session
can read the account status; agent-labelled sessions receive HTTP 403.

## Configuration

1. Enable hub OAuth with a reachable HTTPS `UA_MCP_OAUTH_ISSUER`. Register
   `<issuer>/oauth/connectors/callback` as the callback URL in each provider's
   OAuth application. The exact callback is on the OAuth-only public slice;
   Connectors and other `/api` routes stay outside that slice.
2. Enable `UA_MANAGED_SECRETS=1` and configure `UA_MANAGED_SECRET_KEYS` with a
   Fernet key kept in the hub service environment. Connector OAuth refuses to
   begin without encryption. Keep the old key in the comma-separated list
   during rotation; the first key encrypts new writes.
3. The operator creates the GitHub and Google OAuth applications. Enter their IDs
   and secrets through `managed_secret_set` with `consumer=connector`. Names:
   `oauth:github:client_id`, `oauth:github:client_secret`,
   `oauth:google:client_id`, and `oauth:google:client_secret`. On an enterprise
   hub, pass its `org_id`; the connection and grant are scoped to that org.
4. For the existing GitHub user-token transport, set
   `UA_GITHUB_TOKEN_MODE=user` and
   `UA_GITHUB_USER_TOKEN_SOURCE=connector_oauth` after a GitHub connection is
   established. This is explicit opt-in and has no env/`gh auth` fallback.
   Poll discovery and detail reads use fixed in-process GitHub REST requests;
   the grant never enters a `gh` child process.
   The scheduled PR watcher skips its separate legacy multi-repo journal in
   broker-only mode because that journal still requires a local `gh` login.
   Its Approval Ledger connector poll continues. This is a visible journal
   coverage gap until a separately reviewed in-process multi-repo reader exists.
   A live `pr-watch --repos` invocation fails in broker-only mode rather than
   switching to the legacy local `gh` credential path.

The Google grant is available only through the in-process
`connector_oauth.access_token_for("google", actor=…, purpose=…, org_id=…)`
provider-consumer path. Drive, Calendar, and Gmail hub tools do not consume it
yet. The Integrations page withholds the Google action until that consumer and
scope review are complete. The backend also refuses Google begin, callback,
and use unless `UA_GOOGLE_CONNECTOR_OAUTH_ENABLED=1`; leave it unset before
that review. Do not call the Google connection live before then.

## Behavior and evidence

- GitHub OAuth App user tokens are long-lived; there is no refresh. Google
  requires offline consent and refreshes with a five-minute expiry margin.
- A replayed or expired callback state fails. The public callback shows a
  completion message that directs the operator back to the private Control
  Center tab; Connect opens the provider in a separate tab, and the callback
  never redirects `/ui` on the public-only OAuth host.
  A Google `invalid_grant` marks
  the connection `needs_reconnect` and stops retrying. The Integrations page has
  Connect/Reconnect and Disconnect controls. Disconnect deletes the local
  grant; revoke the provider-side authorization in the provider account when
  required.
- Connect, use, reconnect-needed, and disconnect events include actor, device
  when known, org, provider, scopes, purpose, and the managed-secret digest for
  grant-bearing actions. Provider token values are excluded from event payloads,
  responses, and logs.
- Disconnect first commits a durable request event and disables grant use. It
  then deletes the grant and commits disconnected status with its completion
  event in one transaction. A failed completion can be retried safely.
- A connection is marked usable in the same SQLite transaction as its connected
  event. If the ledger append fails after encrypted grant storage, status stays
  `needs_reconnect` and consumers refuse the grant until a fresh authorization.

## Acceptance remaining

Run the [J5 live checklist](designs/2026-09-24-spike-nango-connector-oauth.md#live-verification-checklist-once-operator-creates-the-oauth-apps)
after the operator provisions the OAuth apps and service encryption key. Test
GitHub/Google consent, refresh rotation, revocation, tenant isolation, and a
real tool use. Code merge alone does not prove those gates.
