# llm_client subscriptions leg (task #1154)

Status: shipped, off by default. Operator runbook for the optional
subscription-pool leg in `llm_client`'s fallback chain
(`src/unified_agent/llm_client.py`).

## ToS risk — read before enabling

**Wrapping subscription-backed CLI auth into an OpenAI-compatible HTTP API
may violate the provider's terms of service.** CLIProxyAPI (or any similar
proxy) reuses OAuth credentials issued for interactive CLI/IDE use to serve
programmatic API traffic; providers can and do treat that as against their
terms, and enforcement (rate limiting, account flags, suspension) is at the
provider's discretion and can change without notice.

Consequences of that risk:

- **The official CLIs (`claude`, `codex`, `gemini`, etc.) remain the primary
  dev lanes.** This leg is a convenience for zero-marginal-cost background
  work (sleep-pass contradiction detection, injection-triage classification),
  not a replacement for them.
- **The product never depends on this proxy.** Every consumer of
  `llm_client.complete()` runs the default `claude_cli -> openrouter` chain
  (task #2025) without this leg; nothing calls with
  `subscription_pool=True` by default, and no caller's behavior changes
  without an explicit code change at that call site. If the proxy
  disappears, is revoked, or is intentionally torn down, nothing regresses.
- **This is why the leg is off by default** — a fresh install, and every
  existing caller until it opts in, behaves exactly as it did before task
  #1154. Enabling it is the operator's call, made per-environment, not a default
  posture for the codebase.

## Architecture (already shipped, task #1154 infra half — 2026-09-19)

- CLIProxyAPI core runs on the Mac hub, `127.0.0.1:8317`, loopback-only bind
  (`host: 127.0.0.1` in its config — never `0.0.0.0`), client api-key auth
  enforced on `/v1` (`api-keys:`, task #1253),
  `remote-management allow-remote=false`. No public exposure, no Tailscale
  Funnel.
- Published to the tailnet as plain TCP passthrough —
  `tailscale serve --tcp 8317 -> 127.0.0.1:8317` — the same pattern as hub
  8765 / gbrain 8766. Reachable from any device on the tailnet, never from
  the public internet.
- The core is managed by the EasyCLIProxyAPI GUI on the Mac; its config
  lives at `~/Library/Application Support/com.cpa.gui/cpa-core/config.yaml`
  on that host, **not** `~/dev/sample-cliproxyapi/config.yaml`.

## What `llm_client` adds (this task)

- New leg `subscription_pool`, calling `_call_subscription_pool()`, an
  OpenAI-compatible `POST {base_url}/chat/completions`, same request shape as
  the `openrouter` leg.
- **Position:** inserted immediately before `openrouter` in the chain —
  `claude_cli -> subscription_pool -> openrouter` — only when
  enabled (task #2025 order; before it, `ollama -> ai_studio ->
  subscription_pool -> openrouter`). The `claude_cli` leg — the official CLI
  itself, run headless — is never skipped ahead of it; it competes with `openrouter` for "last resort before
  giving up," on the reasoning that zero-marginal-cost subscription capacity
  should be tried before spending (free-tier) OpenRouter quota.
- **Off by default**, gated on two independent opt-ins, both required:
  1. Global: `UA_SUBSCRIPTION_POOL_ENABLED=1` (process env).
  2. Per-call: `llm_client.complete(..., subscription_pool=True)` — an
     existing call site must be edited to pass this. As of task #1252 the
     three background research call sites carry the flag (see "Opted-in
     call sites" below); everything else stays on the default chain.
- **Failure mode:** any error (connection refused/reset, timeout, non-2xx,
  malformed body) makes `_call_subscription_pool` return `None`, exactly like
  every other leg's degrade-don't-raise contract. `complete()` then falls
  through to `openrouter`. The per-call `route` list records both attempts
  in order with `ok`/`elapsed_s`, so the fallthrough is visible to any caller
  or log line — see `tests/test_llm_client_subscription_pool.py::test_subscription_pool_down_falls_through_to_openrouter_and_logs_it`.

## Configuration

| Env var | Purpose | Default |
| --- | --- | --- |
| `UA_SUBSCRIPTION_POOL_ENABLED` | Global opt-in. Must be `"1"`. | unset (off) |
| `UA_SUBSCRIPTION_POOL_URL` | Base URL, e.g. `http://<hub-ip>:8317/v1` (Tailscale IP) or the MagicDNS name, `:8317`. | unset |
| `UA_SUBSCRIPTION_POOL_MODEL` | Model name to request. Must be an id the proxy actually serves (`GET /v1/models`); the code default is a placeholder. Mac hub envs are configured with `claude-haiku-4-5-20251001` (verified 200). | `subscription-pool` |
| `UA_SUBSCRIPTION_POOL_API_KEY` | CLIProxyAPI's inbound client key (the dedicated `api-keys:` entry, not the management secret). Sent as `Authorization: Bearer <key>`, same scheme as the `openrouter` leg. The proxy enforces it on `/v1` since task #1253. | unset (no auth header sent — the call gets `401` and the leg degrades) |

The key is read from the environment at call time in `_call_subscription_pool`
/ `leg_status` and is never logged, returned in `route`, or written to any
file this repo controls. Standard secrets hygiene applies: set it via the
hub's `EnvironmentFile`, never inline in a command or committed file.

## Verifying the leg (read-only / no spend)

`llm_client.leg_status()` probes `GET {pool_url}/models` with the configured
key (never a generation call) and reports `configured` / `explicit` /
`reachable` / `detail` for `subscription_pool` alongside the other three
legs. Wired into `system_doctor`'s `llm_provider_chain` check. When the hub's
`UA_MEMORY_PROVIDER=subscription_pool` pins memory calls to this leg, doctor
lists it again as `subscription_pool[memory]`. With the pool disabled or down,
the check is DEGRADED, even if OpenRouter is healthy.

At call time the leg reads the reply's `model` field. A reply that names a
model other than the configured one fails the leg with reason
`model mismatch`, and the usage ledger records the model the proxy reported.

## Opted-in call sites (task #1252)

The per-call flag is wired at the three non-interactive background research
call sites — each still requires `UA_SUBSCRIPTION_POOL_ENABLED=1` in its own
process env:

| Call site | Process that runs it | Env file that must carry the vars |
| --- | --- | --- |
| `sleep_pass._generate_journal_digest` | `unified-agent-sleep-pass` LaunchAgent | `~/.unified_agent/sleep-pass.env` |
| `consolidate` cluster adjudication | inside the sleep-pass run (same process) | `~/.unified_agent/sleep-pass.env` |
| `session_capture.summarize` | hub process (web `/api` session routes) | `~/.unified_agent/service.env` |

Interactive chat callers (`slack_chat`, `zulip_chat`) deliberately do **not**
opt in — user-facing replies stay on the default chain.
`tests/test_subscription_pool_callsites.py` pins both directions of this
contract.

## Live verification (2026-09-22, agent-devin, task #1252)

- **Authenticated 200 through the leg** — the step #1154 could not prove.
  Ran `_call_subscription_pool` on the Mac hub venv with
  `UA_SUBSCRIPTION_POOL_ENABLED=1`, `UA_SUBSCRIPTION_POOL_URL=http://127.0.0.1:8317/v1`,
  `UA_SUBSCRIPTION_POOL_MODEL=claude-sonnet-4-5-20250929`, and
  `UA_SUBSCRIPTION_POOL_API_KEY` sourced server-side from the EasyCLIProxyAPI
  GUI's `config.toml` `management-secret-key` at that time (before #1253,
  `/v1` accepted requests without a client key; this was not proof that the
  management secret authenticated `/v1`). The dedicated `api-keys:` client
  key replaced it in both hub env files under #1253. Result:
  `provider=subscription_pool model=claude-sonnet-4-5-20250929 text='pong'`.
  `claude-haiku-4-5-20251001` also verified 200 and is the configured model.
- **Historical credential materialized** into `~/.unified_agent/service.env`
  and `~/.unified_agent/sleep-pass.env` on the Mac (chmod 600; value never
  printed — length-verified only). Task #1253 later replaced it with the
  dedicated client key and restarted the hub.
- **Finding worth noting (fixed by task #1253, same day):** this CPA build
  answered `/v1/chat/completions` with 200 even *without* the Bearer header —
  the config had no `api-keys:` section, so client auth was open on the
  loopback/tailnet forward. See "Client auth (task #1253)" below.
- The proxy core was found stopped (GUI quit since #1154's verification);
  relaunched via `open -a EasyCLIProxyAPI`, which spawned the managed
  `cli-proxy-api` core per `start-core-on-launch`.

## Client auth (task #1253, 2026-09-22)

CLIProxyAPI only enforces a Bearer on `/v1/*` when its config carries an
`api-keys:` list. `remote-management.secret-key` gates `/v0/management` and
nothing else. Until #1253 the list was empty, so any process on the Mac or
any tailnet peer could spend subscription quota without a key.

The client key lives in four places on the Mac. All four must hold the same
value:

| File | Field | Role |
| --- | --- | --- |
| `~/Library/Application Support/com.cpa.gui/config.toml` | `api-keys = ["…"]` | The GUI's source of truth. The GUI regenerates the core config from it. |
| `~/Library/Application Support/com.cpa.gui/cpa-core/config.yaml` | top-level `api-keys:` list | What the running core reads. It hot-reloads on write, so no restart is needed. |
| `~/.unified_agent/service.env` | `UA_SUBSCRIPTION_POOL_API_KEY` | The hub process. Takes effect on `launchctl kickstart -k gui/$(id -u)/com.unified-agent.hub`, because the live launcher sources this file at start. |
| `~/.unified_agent/sleep-pass.env` | `UA_SUBSCRIPTION_POOL_API_KEY` | The sleep-pass LaunchAgent. Read on its next run. |

The key is dedicated to clients and separate from the management secret.

**Rotating the key.** Generate a new value inside one script that writes all
four files and prints only its length. Never echo the key or pass it on a
command line. Restart the hub afterwards.

**Verifying.** Run a script on the Mac that reads the key from
`service.env` and prints only status codes:

- no header → `401` on `127.0.0.1:8317/v1/models` and on
  `<hub-ip>:8317/v1/models` (tailnet)
- wrong key → `401`
- right key → `200` on `/v1/models`, and `200` on a 5-token
  `/v1/chat/completions` call

Then `system_doctor` should show `llm_provider_chain` with
`subscription_pool:up`.

Result on 2026-09-22 (agent-claude): every check above passed. The hub
restarted with healthz 200, and doctor showed `subscription_pool:up` with 0
MISCONFIGURED. Timestamped backups of the four files, named
`*.bak-1253-20260922` and chmod 600, sit next to the originals.

## Historical live verification (2026-09-21, agent-claude, task #1154)

The observations below predate #1253. The client-auth verification above is
the current result; an unauthenticated request now returns 401.

Re-verified from the desktop, over Tailscale, same day as this leg landed:

- **No public exposure / no Funnel** — `ssh <user>@<hub-ip> 'tailscale
  serve status --json'` → `AllowFunnel: None`; the `8317` entry is
  `{"TCPForward": "127.0.0.1:8317"}`, identical shape to `8765`/`8766`. Config
  file confirms `host: 127.0.0.1` (not `0.0.0.0`).
- **Reachability of the new leg's code path** — calling the real
  `llm_client._call_subscription_pool()` with `UA_SUBSCRIPTION_POOL_URL`
  pointed at `http://<hub-ip>:8317/v1` (no key configured) made a real
  network round trip (~90ms) to the Mac, the proxy reset the unauthenticated
  connection, and the function returned `None` without raising — the correct
  degrade-don't-raise behavior. Running `complete(subscription_pool=True)`
  end to end (ollama and ai_studio also unreachable/unconfigured) produced
  route `[ollama, ai_studio, subscription_pool, openrouter]`, all `ok=False`,
  confirming the leg's real chain position and that it falls through cleanly.
- **Not verified live in this task:** a full authenticated 200 through the
  new leg. That requires `UA_SUBSCRIPTION_POOL_API_KEY`, the proxy's real
  management/client key, which per secrets hygiene this agent did not read
  from the Mac's `config.yaml` or otherwise materialize into its context.
  The request shape that *would* succeed (URL, model, `Authorization: Bearer
  <key>` header) is asserted byte-for-byte in
  `tests/test_llm_client_subscription_pool.py::test_subscription_pool_is_inserted_before_openrouter_when_explicitly_enabled`.
  A full 200 needs a human (the operator) to set the real key in the
  hub/executor environment and either rerun `leg_status()` or make one live
  `complete(subscription_pool=True)` call.
