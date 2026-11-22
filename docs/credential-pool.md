# Credential pool registry

The credential pool stores operator-facing CLI subscription metadata only. It
never stores OAuth tokens, API keys, passwords, or other secret material.

## Tools

- `credential_pool_register(provider, label, note)` creates a `registered` row.
  New rows have `active: false`; Part 2 must add an explicit human activation
  path before treating a row as usable.
- `credential_pool_list(include_revoked=false)` lists metadata rows and hides
  revoked rows by default.
- `credential_pool_revoke(id)` marks a row `revoked` without deleting it.
- `model_quota_status()` reports fleet lane availability derived from local
  observations — never from a provider probe and never inventing "available"
  when there is no source:
  - **Codex**: local rollout `rate_limits` under `~/.codex/sessions/YYYY/MM/DD/`
    (override with `UA_CODEX_SESSIONS_ROOT`); surfaces `used_percent`,
    `window_minutes`, `observed_at`, `source_path`. `resets_in_seconds` is
    always `null` (UNKNOWN in every verified sample).
  - **Every lane**: classified dispatch/executor failures
    (`provider_quota_exhausted` vs `iteration_budget_timeout` vs `code_error`).
  - A lane with no observations is `availability: "unknown"` with
    `source: "no source"`. Credential-pool registry rows remain visible with
    the same unknown posture when they carry no quota data.

Dispatch consumes this projection (task #1251): `claim_and_dispatch` refuses
a ready delegation whose agent maps to an `unavailable` lane, before the
atomic claim — the delegation stays `pending` for a later tick. `unknown`
never blocks; only positive exhaustion evidence gates. Every fail-open (lane
unknown, evidence stale, or no row) emits one `quota gate open` log line
naming the lane and its source, so the open is never silent.

**Exhaustion evidence has a freshness window.** Subscriptions reset
hourly/weekly, so `provider_quota_exhausted` observations and codex
`rate_limits` samples only produce `unavailable` while fresh. Evidence older
than `UA_QUOTA_EXHAUSTION_TTL_S` (default `86400` = 24h, seconds; `0` disables
blocking entirely) degrades the lane to `availability: "unknown"` with
`stale: true` and the aged evidence preserved — dispatch then proceeds as it
does for any lane with no current data. This is the recovery path: a lane
un-blocks either when fresher evidence supersedes the exhaustion or when the
observation ages out.

Supported providers are `anthropic`, `codex`, `gemini`, `google`, `openai`,
`openrouter`, `ollama`, and `xai`. Secret-looking metadata is rejected before
the SQLite insert.

## Optional subscription-pool fallback

`llm_client.complete(..., subscription_pool=True)` may use a configured
OpenAI-compatible subscription endpoint only when `UA_SUBSCRIPTION_POOL_ENABLED=1`
is also set. When both opt-ins are set, the leg is inserted immediately
before `openrouter`: `claude_cli → subscription_pool → openrouter`.
Configure its base URL with `UA_SUBSCRIPTION_POOL_URL`, the optional model
with `UA_SUBSCRIPTION_POOL_MODEL`, and its client auth key with
`UA_SUBSCRIPTION_POOL_API_KEY` (sent as `Authorization: Bearer <key>`).
Without both opt-ins, the default `claude_cli → openrouter` chain is
unchanged and the endpoint is never contacted — see
`docs/llm-subscriptions-leg-runbook.md` for the ToS-risk acknowledgment
(wrapping subscription auth into an API may violate provider terms; the
official CLIs remain the primary dev lanes; the product never depends on
this leg), setup, and live verification. This client-side hook does not
implement proxy deployment, OAuth login, or token pooling — those are
deployed separately (CLIProxyAPI on the Mac hub, task #1154).
