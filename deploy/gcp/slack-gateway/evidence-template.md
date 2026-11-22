# Task #558 — Slack TLS gateway evidence packet (template)

Fill after the operator places secrets and the public Request URL. Mark `_TBD_`
until measured. **No secret values** in this file.

## Identity

| Field | Value |
| --- | --- |
| Date (UTC) | `_TBD_` |
| Operator | The operator |
| PR / commit | `_TBD_` |
| Public Request URL | `https://_TBD_/api/slack/events` |
| Worker host | `ua-worker` (or `_TBD_`) |
| Mac hub MagicDNS | `_TBD_` |
| `MAC_HUB_URL` host (no secret) | `_TBD_` |

## Checks

| Check | Expected | Result | Evidence pointer |
| --- | --- | --- | --- |
| `GET /healthz` (loopback) | `ok: true` | `_TBD_` | `_TBD_` |
| `GET /healthz` (public TLS) | `ok: true` | `_TBD_` | `_TBD_` |
| Invalid Slack signature | HTTP 401 at gateway | `_TBD_` | `_TBD_` |
| Replay / stale timestamp | HTTP 401 at gateway | `_TBD_` | `_TBD_` |
| Valid signed event | hub 200, `dispatched` or challenge OK | `_TBD_` | `_TBD_` |
| One `slack.inbound` event | count +1 on Mac only | `_TBD_` | `_TBD_` |
| One allowlisted mutation | task/delegation row on Mac | `_TBD_` | `_TBD_` |
| Duplicate delivery | `duplicate: true`, no second mutation | `_TBD_` | `_TBD_` |
| Worker has no memory.db authority | no UA SQLite writes on worker | `_TBD_` | `_TBD_` |

## Rollback drill

| Step | Done? |
| --- | --- |
| Slack Request URL cleared/disabled | `_TBD_` |
| `ua-slack-gateway` stopped | `_TBD_` |
| Mac `UA_SLACK_ENABLED=0` + hub restart (the operator) | `_TBD_` |

## Notes

_TBD_
