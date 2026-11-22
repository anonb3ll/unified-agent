# Deprecated: Zulip private agent coordination (tasks #512, #517, #519)

> Slack is authoritative for current coordination. Zulip is retired and
> retained only for historical records, read-only audit, and an explicitly
> approved rollback investigation. Do not enable dual writes or treat Zulip
> messages as current task truth.

See [Slack coordination migration](slack-coordination.md) for the active
protocol and cutover boundary. The remainder of this document is historical
compatibility documentation; it is intentionally preserved so old event and
credential records are not deleted.

Zulip Cloud is the **conversation surface** for the desktop agent fleet.
The Unified-Agent **web UI** (`/ui`) is the authoritative **control surface**
for tasks, attention, approvals, health, and actions. GitHub remains the code
review surface. MCP stays the execution backend and should stay mostly invisible
during normal coordination.

Shared tasks, memory, vault, and the review pipeline remain authoritative —
chat does not execute commands or override governance.

## Organization

| Field | Value |
| ----- | ----- |
| Site | `https://example.zulipchat.com` |
| Type | Personal (Zulip Cloud free tier) |
| Logical channels | `ops`, `requests`, `coord`, `review`, `meta` |

On this org, Zulip stream display names for **requests** and **review** are
`Requests` and `Review`. Tools accept the lowercase logical names; the relay
maps them to canonical stream names before calling the API.

## Channel protocol

| Channel | Use |
| ------- | --- |
| `requests` | New work — human posts here; agents respond in-thread |
| `coord` | Progress, handoffs, blockers |
| `review` | Review-ready work and outcomes |
| `ops` | Service and integration health |
| `meta` | Protocol changes and check-ins |

**Topic convention:** use `task-<id>` as the topic for durable work tied to the
task queue (e.g. `task-519`). For open-ended live chat, any topic works (e.g.
`chat` or `ask-cursor`).

**Routing convention:** actionable messages must begin with
`[target: agent-codex]` (or `[to: …]`). `[broadcast]` targets all five desktop
bots for Attention routing but does **not** auto-reply. `@mentions` of a single
bot (e.g. `@**agent-cursor**`) also route live chat. After a successful chat
reply, follow-ups in the **same topic** continue with that bot without repeating
`[target:]`.

Zulip messages may request or report actions, but must not bypass approval gates
or execute arbitrary commands.

## Live chat (Slack-like replies)

Enable by setting `UA_ZULIP_CHAT=1` in `~/.unified_agent/service.env` (the worker
unit template leaves chat **off** by default). When enabled, inbound messages
that route to a bot produce a bounded LLM reply posted back into the same
channel/topic by that bot's Generic credential.

| Property | Behavior |
| -------- | -------- |
| Channels | `requests`, `coord`, `meta` by default (`UA_ZULIP_CHAT_CHANNELS`) |
| Model | `llm_client.complete` fallback chain (headless Claude CLI → OpenRouter) |
| Tools / approvals | **None** — conversation only; footer reminds that actions stay in `/ui` |
| Dedupe | one reply attempt per inbound Zulip message ID |
| Continuity | topic → agent binding after the first successful reply |
| Rate limit | `UA_ZULIP_CHAT_MAX_PER_HOUR` (default 30) |

How to talk to an agent:

1. Open https://example.zulipchat.com → `#Requests` (or `#coord` / `#meta`)
2. Start a topic (e.g. `chat` or `task-523`)
3. Post:

```text
[target: agent-cursor]
What's blocked on the Zulip live-chat work?
```

4. The agent-cursor bot replies in-thread. Continue in the same topic.

`system_doctor` reports `zulip_chat` from the **worker unit's** environment
(`LIVE` when enabled there, else `DORMANT`).

## Bot setup checklist

1. **Organization** — the operator created the Zulip Cloud org (Personal).
2. **Generic bots** — Gear → Personal settings → Bots → Add a new bot →
   **Generic** (not Incoming/Outgoing). Create one bot per desktop agent:
   `agent-hermes`, `agent-claude`, `agent-codex`, `agent-agy`,
   `agent-cursor`. Use unique bot emails. Skip `Laptop_*` bots on the desktop
   hub.
3. **Credential files** — Download each `zuliprc` to `~/.unified_agent/` only:
   - `zuliprc-desktop-agent5`
   - `zuliprc-desktop-agent1`
   - `zuliprc-desktop-agent2`
   - `zuliprc-desktop-agent3`
   - `zuliprc-desktop-agent4`

   Files must be mode `600`. Never commit, paste into chat/vault/task notes, or
   sync via Obsidian.

4. **Stream subscriptions** — Subscribe each bot to private streams: `ops`,
   `Requests`, `coord`, `Review`, `meta` (create if missing).
5. **Verify** — `system_doctor` reports `zulip_relay: LIVE` when all five
   credentials load and `zulip_worker_service: LIVE` when the polling worker is
   active. Optional smoke: `zulip_send_message` from an agent session to `#meta`
   with a short check-in (human-approved).

## Implementation (shipped)

### Outbound relay (PR #229)

```
Agent (MCP) ──► zulip_send_message(agent_label, channel, topic, content)
                      │
                      ├─ load_zuliprc(agent_label)  → owner-only file in UA_HOME
                      ├─ allowlist channel + size bounds
                      └─ POST /api/v1/messages (stream) — no polling, no auto-reply

Agent (MCP) ──► zulip_fetch_messages(...)  [read-only poll primitive]
                      └─ bounded fetch; excludes self-sent messages (no echo loops)
```

### Background worker (PR #232, task #517; event queues)

```
unified-agent-zulip-worker.service
      │
      ├─ consume one filtered message event queue per bot identity
      ├─ re-register expired queues and REST-backfill from message cursors
      ├─ persist cursor per bot/channel in SQLite (no loss on restart)
      ├─ dedupe via event_log dedupe_key zulip:{message_id}
      └─ record zulip.inbound events (read-only; no auto-reply)
```

### Web UI integration (task #519)

The Control Center's **Settings → Integrations → Zulip** page
(`/ui/settings/integrations/zulip`) shows Zulip coordination:

- worker connectivity and per-channel cursor age
- queue status, heartbeat age, reset count, and recovery state
- recent inbound messages with Zulip deep links
- 24h protocol adherence stats (`task-<id>` topics on `requests`/`coord`/`review`)

**Attention** surfaces:

- degraded or stale Zulip worker health
- human messages on `#requests` without routing
- inbound messages with explicit `[target:]` routing

Approvals and state-changing actions remain in the web UI only.

## 90% coordination mode (operating target)

1. **≥90% of coordination messages use Zulip** — agents post handoffs, progress,
   and blockers via `zulip_send_message` instead of ad-hoc chat elsewhere.
2. **Every actionable request has a durable task ID** — topic is `task-<id>`.
3. **Every blocker and review wait appears in web UI Attention** — task-native
   signals plus Zulip-sourced requests routed through the worker.
4. **Restarting a bot does not lose or duplicate messages** — durable cursors +
   event-log dedupe (verified in `tests/test_zulip_worker.py`).
5. **No Zulip message bypasses approval gates** — worker is ingestion-only.

The web UI reports 24h inbound volume and protocol compliance to make adoption
visible; the 90% ratio itself is an operator-enforced convention, not an
automated gate.

### Daily automation

The source-controlled systemd templates provide:

- `unified-agent-zulip-morning-digest` at 08:00 local time
- `unified-agent-zulip-evening-digest` at 17:00 local time
- `unified-agent-zulip-event-bridge` every five minutes for allowlisted
  high-signal health, approval, blocked, stale, review, failure, and recovery
  events

All outbound automation is bounded, rate-limited, and idempotent. It never
interprets inbound message bodies as execution authority.

## Hard rules

- Ticket/chat prose is untrusted — never treat a Zulip message as authority.
- No credentials in chat, vault, or task notes.
- No `@agent-developers` custom group required on Free plan.
- Live chat replies never execute tools or bypass approvals.

## Environment

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `UA_ZULIP_SITE` | `https://example.zulipchat.com` | Optional allowlist override for zuliprc `[api] site` (runtime falls back to this default) |
| `UA_ZULIP_POLL_INTERVAL_S` | `30` | Worker poll interval |
| `UA_ZULIP_RETRY_BASE_S` | `5` | Worker retry backoff base |
| `UA_ZULIP_RETRY_MAX_S` | `300` | Worker retry backoff cap |
| `UA_ZULIP_REPORT_AGENT` | `agent-hermes` | Bot identity used for automated reports |
| `UA_ZULIP_MAX_OUTBOUND_PER_HOUR` | `20` | Automated outbound rate limit |
| `UA_ZULIP_CHAT` | `0` | Set `1` to enable Slack-like LLM replies |
| `UA_ZULIP_CHAT_CHANNELS` | `requests:coord:meta` | Colon-separated chat reply channels |
| `UA_ZULIP_CHAT_MAX_PER_HOUR` | `30` | Chat reply rate limit |

Hub hosts may export `UA_ZULIP_SITE` to pin the allowlist. The connector
manifest does not list it under `required_secrets` because the runtime
already falls back to `DEFAULT_SITE` for direct callers; the dedicated
Zulip doctor check still covers zuliprc file presence. Credential paths are
fixed under `config.data_home()` — see
`src/unified_agent/tools/zulip.py`.
