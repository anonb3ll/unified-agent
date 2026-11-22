# Memory agent provider and model (task #2021)

The memory agent is the sleep pass (cluster adjudication and journal digest),
including `unified-agent-consolidate` adjudication. It uses the existing
`llm_client` adapters and observed-usage ledger. This does not configure other
agents, retrieval embeddings, or dot's private memory.

| Setting | Meaning | Default |
| --- | --- | --- |
| `UA_MEMORY_PROVIDER` | Pin `claude_cli`, `openrouter`, or `subscription_pool` | Unset: existing Claude CLI → OpenRouter chain |
| `UA_MEMORY_MODEL` | Exact identifier for the selected provider | Selected provider's existing model setting/default |

Model-only configuration is rejected because it does not identify a provider.
Unknown providers and malformed identifiers fail before inference. A model ID
must be 1–200 characters, start with an ASCII letter/digit, and contain only
letters/digits plus `. _ : / @ + -` (without spaces). Syntax validation does not
prove provider availability. Empty values mean unset.

Precedence: memory-specific model > existing selected-provider model environment
setting > existing provider default. Selection is resolved on every memory call,
without mutating process-wide provider settings. Other consumers keep their
existing chain. Without a memory provider, all current defaults and subscription
opt-ins remain unchanged. An explicit memory provider pins a single leg: errors,
unavailable models, disabled routes or invalid replies never substitute another
provider/model. Failures retain sanitized route evidence and the requested model.

A pinned route also checks which model answered. The CLI leg needs its own
report to name exactly the requested id. The OpenRouter and subscription-pool
routes read the reply's `model` field. It must name the pin exactly, or under
one of two mappings: the native and OpenRouter spellings of a Claude id in the
table below, or the pin's dated snapshot (`<id>-YYYYMMDD`, the form of
OpenRouter's canonical slugs, such as `anthropic/claude-haiku-5.5-20261007`).
A reply that names another model, or no model, fails the route with reason
`model mismatch`. The ledger records the model each reply reported, never the
requested one. A rejected reply that reports token usage, whether for a model
mismatch or a schema failure, is still recorded once. The CLI and pool rows
keep a NULL cost, so subscription work that preceded a failure stays visible.

Doctor judges a memory pin in the hub's own environment as a separate route.
Its `<leg>[memory]` rows show the pinned leg. When that leg is down or
unconfigured, `llm_provider_chain` is DEGRADED even if another hub leg is up,
because every hub memory call fails. Invalid memory settings make the row
MISCONFIGURED.

For scheduled runs, persist memory settings in `<UA_HOME>/sleep-pass.env`. That
file is sourced by the existing scheduler. A setting it leaves unset is then
seeded from the `settings.toml` `[env]` table at startup, as in every process,
and a setting both leave unset inherits from `service.env` through the existing
non-secret inheritance mechanism. Doctor's scheduled environment uses the same
precedence (`sleep-pass.env`, then `settings.toml`, then `service.env`), so a
pin in `settings.toml` shows as the one pinned `[sleep-pass]` leg (task #2175).
If `sleep-pass.env` sets `UA_HOME`, the scheduled pass reads `service.env` and
`settings.toml` from that home, and so does doctor (task #2196). The settings
file is `UA_SETTINGS_FILE` from `sleep-pass.env`, else `<UA_HOME>/settings.toml`;
the scheduler sets no `UA_SETTINGS_FILE` of its own, so doctor ignores the one in
its own process environment (task #2193). A process's explicit
settings win over inheritance. Selecting a provider in the higher layer also
resets the inherited memory model: a provider-only override uses that provider's
model setting/default, never the hub's memory model for another provider. A restart/relaunch reads the persisted files again.
Provider credentials and opt-in flags are not inherited from `service.env`.
No credential creation or budget increase is part of this mechanism.

The two Haiku 5.5 routes use different native IDs:

| Provider | `UA_MEMORY_MODEL` |
| --- | --- |
| `claude_cli` | `claude-haiku-5-5` |
| `openrouter` | `anthropic/claude-haiku-5.5` |

`subscription_pool` still requires the existing global
`UA_SUBSCRIPTION_POOL_ENABLED=1` and the memory callsite's existing opt-in. This
change does not enable the pool or change its URL/auth/network boundary. OpenRouter
availability is checked with its unbilled authenticated metadata endpoint and exact
model catalog listing. Claude CLI availability is checked with local help and auth
status probes. None of these checks generates text or validates quota.

Anthropic's [Haiku 5.5 migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide)
lists `claude-haiku-5-5` for the Claude API, without a dated suffix. The existing
subscription adapter sends an OpenAI-compatible request with no sampling/thinking
parameters or assistant prefill; the proxy owns translation to Claude responses.
A proxy catalog must confirm that identifier before applying a hub-only selection.

Example memory-only pins (choose one after the route and provider are verified;
these are examples, not applied configuration):

```dotenv
UA_MEMORY_PROVIDER=claude_cli
UA_MEMORY_MODEL=claude-haiku-5-5
```

```dotenv
UA_MEMORY_PROVIDER=openrouter
UA_MEMORY_MODEL=anthropic/claude-haiku-5.5
```

No example settings are applied by this PR. The exact provider choice, OpenRouter
daily cap, credential readiness and any runtime activation remain separate human
decisions. Do not use inference merely to validate a model identifier. Fixture
evidence proves model propagation, not live inference.
