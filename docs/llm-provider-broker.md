# LLM provider broker (J7 #1326)

The hub exposes an opt-in OpenAI-compatible chat completion route at
`POST /api/llm/v1/chat/completions`. A client authenticates with its saved
device-flow OAuth token. The hub checks that the current direct tailnet peer
matches the token's `device_node` claim. A static bearer, a browser cookie, an
unbound OAuth token, or a proxy whose direct peer is loopback cannot spend.

OpenRouter models use the hub managed secret `llm:openrouter`. Direct OpenAI
models use an allowlisted client ID such as `openai-direct/gpt-4o-mini`, the
fixed OpenAI Chat Completions endpoint, and a separate `llm:openai` managed
secret. Both keys stay in the hub's write-only store with consumer `llm`.
`managed_secret_set` requires its existing approval gate and a verified
operator; the key is never returned to clients, logged, or stored in a device
config. Set `UA_LLM_BROKER_ENABLED=1` and an explicit comma-separated
`UA_LLM_BROKER_MODELS` allowlist on the hub to make calls possible. Both are
off/empty by default. No live key, service environment, or deployment is
changed by this code.

On an enterprise hub with an enforced organization boundary, the key is read
only from that organization's managed-secret scope. An org-scoped read never
falls back to the hub root key.

The route caps the JSON request at 256 KiB, `max_tokens` at 4096, and calls at
10 per agent per minute. Streaming chat completions use server-sent events and
request a final usage chunk. Responses come from fixed HTTPS provider endpoints without redirects
or environment proxy settings. Provider errors return generic text; neither
their body nor the hub key is forwarded. An audit attempt is committed before
the billable call; a result event records the agent, device node, model,
outcome, and provider-reported token/cost usage when supplied. Missing cost is
unknown, never treated as zero.

Set positive `UA_LLM_BROKER_DAILY_USD` and `UA_LLM_BROKER_RESERVE_USD` on the
hub before enabling calls. Each attempted call reserves the configured USD
amount in SQLite in the same transaction as its audit attempt. The daily cap
is per tenant and agent in UTC. Provider-reported cost replaces the reservation
only when the result audit and reconciliation commit together. An interrupted
stream, missing cost, or failed result write retains the full reservation.
This is a conservative admission budget, not a guaranteed billing maximum:
the provider can charge more than the configured reservation. Set a separate
provider-key credit limit for a hard external bound. OpenAI Chat Completions
reports token usage but no USD cost, so direct OpenAI calls retain the full
configured reservation until an independent cost reconciler is available.

For an OpenAI-compatible harness, sign the device in with
`unified-agent login --hub <hub-url> --configure openai-compatible --model <allowed-model>`.
For OpenCode v1, use `--configure opencode` instead. The command writes a
private 0600 config snippet under `~/.unified_agent/llm-proxy/` and prints the
`unified-agent llm-proxy --hub <hub-url>` command. Run that proxy on the device;
it listens on loopback, requires a separate local bearer, refreshes the saved
device OAuth token for every request, and forwards only chat completions to the
hub. The emitted key is local proxy authorization, not a provider API key.
Snippets for the same hub and proxy port share that local bearer because they
connect to one local proxy. Revoking one snippet requires rotating the proxy
bearer and regenerating every snippet for that proxy. Each hub and port pair
has its own private configuration directory and bearer.
Review and copy the emitted snippet into the harness configuration; this
command does not overwrite a running harness config.

Remaining J7 rollout work: live device/harness test and an operator-set hub key,
model allowlist, daily budget, and provider credit limit. More direct providers
can use the same broker boundary in later provider-specific PRs.
The separate CLIProxyAPI subscription service
is not the hub broker and remains outside this provider-key route.
