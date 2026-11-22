# Public Slack TLS forwarder (task #558)

Stateless edge process for Slack Events → Mac hub. See **RUNBOOK.md**
for Request URL shape, env vars, health checks, rollback, and the operator-only
secret steps.

## Quick facts

| Item | Value |
| --- | --- |
| Public path | `POST /api/slack/events` |
| Health | `GET /healthz` |
| Auth at edge | Slack v0 HMAC (`SLACK_SIGNING_SECRET`) + 5‑minute replay window |
| Upstream | `MAC_HUB_URL` (Tailscale → Mac hub same path) |
| Local state | **none** (no SQLite, no tasks) |

## Files

| File | Role |
| --- | --- |
| `forwarder.py` | Stdlib HTTP forwarder (Python ≥ 3.11) |
| `env.example` | Redacted environment template |
| `ua-slack-gateway.service` | systemd unit (loopback bind) |
| `RUNBOOK.md` | Operator runbook |
| `evidence-template.md` | Live verification checklist placeholders |

## Local dry-run (no secrets committed)

```bash
export SLACK_SIGNING_SECRET=test-signing-secret   # local only
export MAC_HUB_URL=http://127.0.0.1:8765/api/slack/events
python3 deploy/gcp/slack-gateway/forwarder.py
```

Repo tests (mocked hub, no live Slack):

```bash
uv run pytest -q tests/test_slack_tls_forwarder.py
```
