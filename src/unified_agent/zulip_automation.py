"""Bounded outbound Zulip automation: digests and high-signal event notices."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from . import config, events, storage, zulip_worker
from .tools import zulip

# Zulip is retired. This default is a bot credential, not a role: the sender
# must be a key of tools.zulip._BOT_CONFIG_FILES (each maps to its own
# zuliprc), and no chief-of-staff identity has one, so pointing it at the
# current coordinator would make every send fail. Override with
# UA_ZULIP_REPORT_AGENT (task #1879).
REPORT_AGENT_DEFAULT = "Desktop_Agent5"
REPORT_CHANNEL = "ops"
BRIDGE_STATE_NAME = "high-signal-events"
BRIDGE_CHANNELS = ("agent.health.*", "agent.task.*", "agent.approval.*")
ALLOWED_NOTIFICATION_KINDS = frozenset({"event", "morning_digest", "evening_digest"})
MAX_RATE_PER_HOUR_DEFAULT = 20


def _report_agent() -> str:
    return os.environ.get("UA_ZULIP_REPORT_AGENT", REPORT_AGENT_DEFAULT).strip()


def _max_rate() -> int:
    try:
        value = int(
            os.environ.get("UA_ZULIP_MAX_OUTBOUND_PER_HOUR", str(MAX_RATE_PER_HOUR_DEFAULT))
        )
    except ValueError:
        return MAX_RATE_PER_HOUR_DEFAULT
    return value if value > 0 else MAX_RATE_PER_HOUR_DEFAULT


def _send_once(
    *,
    kind: str,
    dedupe_key: str,
    channel: str,
    topic: str,
    content: str,
) -> dict[str, Any]:
    if kind not in ALLOWED_NOTIFICATION_KINDS:
        raise ValueError(f"unsupported Zulip notification kind: {kind}")
    prior = storage.get_zulip_notification(dedupe_key)
    if prior and prior.get("status") == "sent":
        return {"sent": True, "duplicate": True, "dedupe_key": dedupe_key}
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    if storage.count_recent_zulip_notifications(since) >= _max_rate():
        return {"sent": False, "rate_limited": True, "dedupe_key": dedupe_key}

    result = zulip.zulip_send_message(
        _report_agent(), channel, topic, content[: zulip.MAX_CONTENT_CHARS]
    )
    sent = bool(result.get("sent")) if isinstance(result, dict) else False
    row = storage.record_zulip_notification(
        dedupe_key,
        kind=kind,
        agent_label=_report_agent(),
        channel=channel,
        topic=topic,
        status="sent" if sent else "failed",
        error=None if sent else str(result.get("error", "Zulip send failed")),
    )
    return {"sent": sent, "dedupe_key": dedupe_key, "notification": row}


def _item_line(item: dict[str, Any]) -> str:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    task_id = source.get("task_id") or item.get("id")
    prefix = f"task-{task_id}: " if task_id else ""
    title = item.get("title") or item.get("reason") or item.get("kind") or "item"
    owner = item.get("owner") or item.get("claimed_by")
    return f"- {prefix}{title}" + (f" (owner: {owner})" if owner else "")


def render_digest(kind: str, payload: dict[str, Any], worker_health: dict[str, Any]) -> str:
    title = "Morning work digest" if kind == "morning_digest" else "Evening work handoff"
    lines = [f"{title} — {payload.get('generated_at', 'unknown time')}", ""]
    lines.append(f"Status: {payload.get('status', 'unknown')} — {payload.get('evidence', '')}")
    lines.append("")

    waiting = payload.get("waiting_on_operator", payload.get(config.LEGACY_OPERATOR_DIGEST_KEY, []))
    stuck = payload.get("stuck", [])
    running = payload.get("running", {}).get("tasks", [])
    changed = payload.get("changed_today", {}).get("tasks_completed")
    if not isinstance(changed, list):
        # Older fixtures / callers may still use the pre-digest key.
        changed = payload.get("changed_today", {}).get("tasks", [])
        if not isinstance(changed, list):
            changed = []
    sections = (
        ("Waiting on the operator", waiting),
        ("Stuck or degraded", stuck),
        ("Running work", running),
        ("Completed today", changed),
    )
    for label, items in sections:
        lines.append(f"{label} ({len(items)}):")
        if items:
            lines.extend(_item_line(item) for item in items[:8])
        else:
            lines.append("- none")
        lines.append("")

    queue_summary = worker_health.get("queue_summary", {})
    lines.append(
        "Zulip worker: "
        f"healthy={queue_summary.get('healthy', 0)} "
        f"error={queue_summary.get('error', 0)} "
        f"reset={queue_summary.get('reset', 0)}"
    )
    errors = payload.get("errors", [])
    if errors:
        lines.append("Data errors: " + "; ".join(str(error)[:160] for error in errors[:3]))
    return "\n".join(lines)[: zulip.MAX_CONTENT_CHARS]


def run_digest(kind: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Build and send one idempotent morning or evening digest."""
    if kind not in ("morning_digest", "evening_digest"):
        raise ValueError("kind must be morning_digest or evening_digest")
    from .webui import api

    payload = asyncio.run(api.digest_payload())
    worker_health = zulip_worker.ZulipPollingWorker().health_payload()
    stamp = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    content = render_digest(kind, payload, worker_health)
    result = _send_once(
        kind=kind,
        dedupe_key=f"zulip:{kind}:{stamp}",
        channel=REPORT_CHANNEL,
        topic="daily-work",
        content=content,
    )
    return {"kind": kind, "date": stamp, "content": content, **result}


def _high_signal(event: dict[str, Any]) -> bool:
    channel = str(event.get("channel", ""))
    if channel == "agent.health.alert" or channel == "agent.approval.requested":
        return True
    if not channel.startswith("agent.task."):
        return False
    payload = event.get("payload", {})
    text = json.dumps(payload, sort_keys=True).lower()
    return any(word in text for word in ("blocked", "stale", "review", "failure", "recovery"))


def bridge_once() -> dict[str, Any]:
    """Send allowlisted high-signal event notices and advance its ULID cursor."""
    cursor = storage.get_zulip_bridge_cursor(BRIDGE_STATE_NAME)
    if cursor is None:
        latest = events.latest_ulid(channels=list(BRIDGE_CHANNELS))
        if latest:
            storage.save_zulip_bridge_cursor(BRIDGE_STATE_NAME, latest)
        return {"status": "initialized", "cursor": latest, "sent": 0}

    rows = events.query_events(channels=list(BRIDGE_CHANNELS), since=cursor, limit=100)
    sent = 0
    for event in rows:
        if _high_signal(event):
            payload = event.get("payload", {})
            content = (
                f"{event.get('channel')}\n"
                f"{payload.get('summary') or payload.get('detail') or json.dumps(payload, sort_keys=True)[:1800]}"
            )
            result = _send_once(
                kind="event",
                dedupe_key=f"zulip:event:{event['ulid']}",
                channel="ops",
                topic="automation",
                content=content,
            )
            if result.get("rate_limited") or not result.get("sent"):
                return {"status": "deferred", "cursor": cursor, "sent": sent, "result": result}
            sent += 1
        storage.save_zulip_bridge_cursor(BRIDGE_STATE_NAME, event["ulid"])
        cursor = event["ulid"]
    return {"status": "healthy", "cursor": cursor, "sent": sent, "scanned": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send bounded Zulip daily automation")
    parser.add_argument("kind", choices=("morning", "evening", "bridge"))
    args = parser.parse_args()
    if args.kind == "bridge":
        result = bridge_once()
    else:
        result = run_digest(f"{args.kind}_digest")
    print(json.dumps(result, default=str))
