# Task-space chat worker (Discord + Telegram)

`ua-worker` gives the hub two inbound chat surfaces — Discord and Telegram
— as a supervised ingestion boundary, mirroring the [Zulip coordination
worker](../deploy/systemd/unified-agent-zulip-worker.service) pattern. It is
the operator's chat-hub replacement lane (hub task #770, resumed from the blocked
#690 Cloud Agent5 lane).

Off by default. The code ships dormant until tokens land on the hub host via
env (task #727/#811 — never in chat, never in SQLite).

## What it does

1. **Ingest**: polls Discord channel messages and Telegram `getUpdates`,
   classifies each against the shared `[target: <label>]` convention (same as
   the Zulip worker), and writes every message durably to the event log
   (`ts.inbound`) with a surface-scoped dedupe key `ts:<surface>:<id>`.
2. **Cursor discipline**: a per-surface/channel cursor in `ts_cursors`
   advances only after the durable write succeeds; a crash replays safely.
3. **Bounded coordination verbs**: an authorized
   `[target: <agent-label>] /<verb> ...` message may use exactly `task`,
   `assign`, `progress`, `blocker`, or `review`. Each is parsed locally and
   routed through `server.REGISTERED_TOOLS`, the same policy/profile-wrapped
   callables used by Slack. The inbound event is durable before execution; a
   second `ts.inbound` event records the verb, verified transport actor, and
   `succeeded` or `failed` outcome. There is no free-form command execution.
4. **Attributed reply**: targeted messages get an attributed chat reply,
   recorded first as an `agent.message` event from the worker's own identity
   (`WORKER_AGENT_LABEL`, currently `agent-gcp-agent5`, the executive office's lead
   identity that owns chat-surface intake) via the hub's messaging
   vocabulary (act `inform`), then posted back to the chat. When the target is
   the worker's own label, the hub refuses a self-message, so the ack is
   recorded as a `self-ack` `ts.inbound` event instead and still posted to
   chat. `/assign <worker label> ...` is refused with a message saying so.
   Refused verbs (unauthorized, unsupported, or malformed) receive a visible
   refusal reply and a `ts.inbound` record; they never mutate tasks.
5. **Everything else is metadata**: non-verb messages retain the existing
   conversation-only behavior. Non-targeted, unauthorized, or invalid-target
   messages persist with a visible `target_reason` and an empty
   `actionable_for` list.

## Enable on the hub host

Land these in `~/.unified_agent/service.env` (values provided by the operator —
#727; do not paste tokens into chat):

```
# Discord (required for the Discord surface)
UA_TS_DISCORD_TOKEN=<bot token>
UA_TS_DISCORD_ALLOWED_USER_IDS=<the operator's Discord user ID>
UA_TS_DISCORD_CHANNELS=<channel-id>:<channel-id>

# Telegram (optional second surface)
UA_TS_TELEGRAM_TOKEN=<bot token>
UA_TS_TELEGRAM_ALLOWED_USER_IDS=<the operator's Telegram user ID>

# Optional tuning
UA_TS_POLL_INTERVAL_S=30
UA_TS_FETCH_LIMIT=10
```

Then install and start the unit:

```
cp deploy/systemd/unified-agent-ts-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unified-agent-ts-worker
```

Without tokens both surfaces report `disabled` and the loop idles — installing
the unit before tokens exist is safe.

## Authorization posture (locked to the operator)

Only senders whose platform user ID appears in
`UA_TS_DISCORD_ALLOWED_USER_IDS` / `UA_TS_TELEGRAM_ALLOWED_USER_IDS` can ever
produce an actionable record or a reply. Everything else is retained as
informational with `target_reason: unauthorized-sender`. The bot's own and
other bots' Discord messages are never ingested (`author.bot` filter).

## Verify

```
unified-agent doctor
```

Expect capability `ts_worker`:

- `DORMANT` — no tokens on this host (default, surfaces disabled)
- `MISCONFIGURED` — a token is set without its sender allowlist (or Discord
  lacks `UA_TS_DISCORD_CHANNELS`)
- `LIVE` — token + allowlist (+ channels) configured

Then a live round trip: from an allowed sender, post
`[target: agent-codex] ping` (any fleet-catalog agent label) to the configured
Discord channel and confirm the worker records `ts.inbound` (event log) plus
the attributed `agent.message` ack from `agent-gcp-agent5`, and the ack arrives back
in the chat. To create a task, use `[target: agent-codex] /task <title>`; confirm the wrapped task call and
the `ts.inbound` outcome event before relying on it.

## Design notes & deviations from #690

- #690's original notes described a Hermes-side bot on the GCP VPS. The
  current architecture runs the worker inside unified-agent as a supervised
  systemd service on the hub host (the zulip-worker pattern); that deviation
  is recorded on the kanban card per the task spec.
- Chat targets are Discord + Telegram only; Slack Socket Mode was dropped
  from the lane on 09-04 (Slack parked for external users).
- Discord uses REST polling (`GET /channels/{id}/messages?after=<cursor>`) —
  no websocket gateway, no intents, no public ingress — to keep the e2-micro
  class host within budget and the security posture outbound-only.
- Telegram long-polls `getUpdates` with an offset cursor; the optional
  `telegram` extra flagged missing by #690's live verification is not needed —
  the transport is plain `httpx` REST, no SDK.

## Tests

```
pytest tests/test_ts_worker.py -q
```

All transport interaction in tests is via stub gateways/recorded requests —
no live Discord/Telegram connection is ever opened.
