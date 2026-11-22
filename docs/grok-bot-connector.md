# Grok Bot hub path

Cloud bots without tailnet access reach the hub over the authorized Tailscale
Funnel HTTPS route on port 443 (#2289). Tailnet clients can use the private
routes. Each provisioned bot is its own
cloud-hosted identity. `agent-cloud-coord` is chief of staff. `agent-cloud-agent8` stays
registered and defaults to research. The other bot labels and their profiles
are in [architecture.md](architecture.md).

The public 443 route forwards to the Mac hub on loopback port 8765. Public
ingress is restricted to MCP and OAuth by the hub's public-request guard;
it does not expose the Control Center UI or general API. Other Serve routes,
including the WorkOS pilot on 8445, remain tailnet-only. The live map belongs
in `~/agent-workspace/docs/topology.md`; changing public exposure needs the operator's
explicit approval.

Interactive public OAuth grants still use `agent-server-agent8` on the restricted
`public` profile. Provisioned client-credentials grants use their configured
bot label instead. Using the public route never promotes that label's profile.

## Non-interactive cloud bots (task #870)

Bots that cannot complete a browser consent use pre-provisioned client
credentials. The grant maps onto that bot's existing profile. It does not
create a new one. Use the configured HTTPS issuer reachable from the bot's
environment; for off-tailnet bots this is the published 443 origin.

```
UA_MCP_OAUTH_CLIENT_CREDENTIALS=bot-id|<secret>|cloud_agent15
```

```
curl -sS -X POST "$ISSUER/oauth/client-credentials" \
  -d grant_type=client_credentials \
  -d client_id=bot-id \
  -d client_secret='<secret>'
```

Refresh tokens rotate on use; `/revoke` cascades access and refresh; audit
events land on `hub.oauth.*`. Verify a token with:

```
unified-agent doctor --whoami --bearer "$ACCESS_TOKEN"
```

Expect `mcp_oauth` LIVE when OAuth is configured. Then a live tool call from
the bot from its own environment, so the check proves both reachability and
the intended identity/profile.
