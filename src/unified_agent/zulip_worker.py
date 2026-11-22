"""Supervised Zulip coordination polling with optional live-chat replies.

The worker is primarily an ingestion boundary. It fetches bounded messages
through :mod:`unified_agent.tools.zulip`, writes informational or explicitly
targeted records to the durable event log, and advances a cursor only after
that write succeeds.

When ``UA_ZULIP_CHAT=1``, a successful persist may also trigger
:mod:`unified_agent.zulip_chat` to post a bounded LLM reply for ``[target:]``
messages (and topic follow-ups). Chat still never executes shell commands,
mutates tasks, or bypasses approval gates — replies are conversation only.

Target convention
-----------------

Actionable messages begin with ``[target: Desktop_Agent2]`` (``[to: ...]`` is
also accepted) and name exactly one configured bot. An explicit ``[broadcast]``
marker targets all five bots for Attention routing, but does not auto-reply.
Messages without a valid marker are retained as informational events with a
visible reason and an empty ``actionable_for`` list. After a successful chat
reply, the same topic continues with that bot without repeating ``[target:]``.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import events, post_init, storage
from .tools import zulip

log = logging.getLogger(__name__)

BOT_IDENTITIES = (
    "Desktop_Agent5",
    "Desktop_Agent1",
    "Desktop_Agent2",
    "Desktop_agent3",
    "Desktop_Agent4",
)
# An @-mention of one of the identities above, optionally bold (**...**). Built
# from BOT_IDENTITIES rather than a list of family names, so a deployment that
# renames its labels (the public export neutralises them) still resolves its
# own mentions. A label must not be the prefix of a longer word.
MENTION_RE = re.compile(
    r"@(?:\*\*)?("
    + "|".join(re.escape(label) for label in sorted(BOT_IDENTITIES, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])(?:\*\*)?",
    re.IGNORECASE,
)
POLL_CHANNELS = ("ops", "requests", "coord", "review", "meta")
DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_RETRY_BASE_S = 5.0
DEFAULT_RETRY_MAX_S = 300.0
STALE_MULTIPLIER = 3.0

_TARGET_RE = re.compile(r"^\s*\[(?:target|to):\s*([^\]]+)\]\s*", re.IGNORECASE)
BROADCAST_RE = re.compile(r"^\s*\[broadcast\]\s*", re.IGNORECASE)
_TASK_RE = re.compile(r"\btask-(\d+)\b", re.IGNORECASE)


def bounded_retry_delay(attempt: int, *, base_s: float, max_s: float) -> float:
    """Return exponential retry delay bounded by ``max_s``.

    The exponent is applied iteratively so a large ``attempt`` cannot overflow
    a float before the ``max_s`` cap is applied.
    """
    if attempt < 1:
        raise ValueError("attempt must be positive")
    if base_s <= 0 or max_s <= 0:
        raise ValueError("retry bounds must be positive")
    delay = float(base_s)
    for _ in range(attempt - 1):
        if delay >= max_s:
            return float(max_s)
        delay = min(max_s, delay * 2.0)
    return delay


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def plaintext_for_classification(content: str) -> str:
    """Normalize Zulip HTML or Markdown so start-anchored classifiers match."""
    text = content or ""
    if "<" in text and ">" in text:
        text = _HTML_TAG_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def _iso_plus(timestamp: str, seconds: float) -> str:
    value = datetime.fromisoformat(timestamp)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _seconds_since(timestamp: str | None, now: str) -> int | None:
    if not timestamp:
        return None
    then = datetime.fromisoformat(timestamp)
    current = datetime.fromisoformat(now)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return max(0, int((current - then).total_seconds()))


def classify_target(content: str) -> dict[str, Any]:
    """Classify a message without interpreting its body as instructions."""
    content = zulip.plain_text_content(content)
    if BROADCAST_RE.match(content):
        return {
            "target": None,
            "target_reason": "broadcast",
            "actionable_for": list(BOT_IDENTITIES),
        }
    match = _TARGET_RE.match(content)
    if match is None:
        return {"target": None, "target_reason": "missing-target", "actionable_for": []}
    target = match.group(1).strip()
    if target not in BOT_IDENTITIES:
        return {"target": target, "target_reason": "invalid-target", "actionable_for": []}
    return {"target": target, "target_reason": "explicit-target", "actionable_for": [target]}


def classify_request(content: str, target: dict[str, Any]) -> dict[str, Any]:
    """Recognize only the explicit, reviewable request envelope.

    The body remains inert text. A task ID and valid target are metadata for a
    human-reviewed request record, never authority to execute work.
    """
    match = _TASK_RE.search(content)
    if not target.get("actionable_for"):
        return {"request_kind": "informational", "request_reason": "no-valid-target"}
    if match is None:
        return {"request_kind": "informational", "request_reason": "missing-task-id"}
    return {
        "request_kind": "reviewable",
        "request_reason": "explicit-target-and-task-id",
        "task_id": int(match.group(1)),
    }


class ZulipPollingWorker:
    """Poll all configured bot/channel pairs under bounded retry supervision."""

    def __init__(
        self,
        *,
        agents: tuple[str, ...] = BOT_IDENTITIES,
        channels: tuple[str, ...] = POLL_CHANNELS,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        retry_base_s: float = DEFAULT_RETRY_BASE_S,
        retry_max_s: float = DEFAULT_RETRY_MAX_S,
        fetch_limit: int = zulip.MAX_FETCH_LIMIT,
    ) -> None:
        invalid_agents = set(agents) - set(BOT_IDENTITIES)
        invalid_channels = set(channels) - set(zulip.ALLOWED_CHANNELS)
        if invalid_agents:
            raise ValueError(f"unsupported Zulip bot identities: {sorted(invalid_agents)}")
        if invalid_channels:
            raise ValueError(f"unsupported Zulip channels: {sorted(invalid_channels)}")
        if not agents or not channels:
            raise ValueError("at least one agent and channel are required")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        if retry_base_s <= 0 or retry_max_s <= 0:
            raise ValueError("retry bounds must be positive")
        if not 1 <= fetch_limit <= zulip.MAX_FETCH_LIMIT:
            raise ValueError(f"fetch_limit must be from 1 to {zulip.MAX_FETCH_LIMIT}")
        self.agents = tuple(agents)
        self.channels = tuple(channels)
        self.poll_interval_s = poll_interval_s
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.fetch_limit = fetch_limit
        self._stop = threading.Event()
        self._retry_until: dict[tuple[str, str], float] = {}
        self._queue_retry_until: dict[str, float] = {}
        self._queue_attempts: dict[str, int] = {}

    def _record_error(self, agent_label: str, channel: str, error: str) -> dict[str, Any]:
        timestamp = storage.now()
        prior = storage.get_zulip_worker_health(agent_label, channel)
        attempt = int(prior["consecutive_errors"]) + 1 if prior else 1
        delay = bounded_retry_delay(attempt, base_s=self.retry_base_s, max_s=self.retry_max_s)
        next_retry_at = _iso_plus(timestamp, delay)
        health = storage.record_zulip_worker_error(
            agent_label,
            channel,
            error=error,
            next_retry_at=next_retry_at,
            timestamp=timestamp,
        )
        self._retry_until[(agent_label, channel)] = time.monotonic() + delay
        return {
            "status": "error",
            "agent": agent_label,
            "channel": channel,
            "messages_processed": 0,
            "cursor": storage.get_zulip_cursor(agent_label, channel),
            "error": health["last_error"],
            "next_retry_at": next_retry_at,
        }

    @staticmethod
    def _event_payload(message: dict[str, Any]) -> dict[str, Any]:
        content = str(message.get("content", ""))
        truncated = len(content) > zulip.MAX_CONTENT_CHARS
        content = content[: zulip.MAX_CONTENT_CHARS]
        classify_text = plaintext_for_classification(content)
        decision = classify_target(classify_text)
        request = classify_request(classify_text, decision)
        return {
            "source": "zulip",
            "message_id": message["id"],
            "channel": str(message.get("channel", ""))[: zulip.MAX_TOPIC_CHARS],
            "topic": str(message.get("topic", ""))[: zulip.MAX_TOPIC_CHARS],
            "sender_email": str(message.get("sender_email", ""))[: zulip.MAX_TOPIC_CHARS],
            "sender_full_name": str(message.get("sender_full_name", ""))[: zulip.MAX_TOPIC_CHARS],
            "content": content,
            "content_truncated": truncated,
            **decision,
            **request,
        }

    @staticmethod
    def _logical_channel(value: object) -> str:
        canonical = str(value or "")
        for logical, name in zulip._STREAM_CANONICAL.items():
            if canonical == name:
                return logical
        return canonical

    def _persist_message(self, agent_label: str, message: dict[str, Any]) -> bool:
        message_id = int(message["id"])
        channel = self._logical_channel(message.get("channel"))
        if channel not in self.channels:
            return False
        cursor = storage.get_zulip_cursor(agent_label, channel)
        if cursor is not None and message_id <= int(cursor["last_message_id"]):
            return False
        payload = self._event_payload({**message, "channel": channel})
        events.record_event(
            "zulip.inbound",
            payload=payload,
            originator=payload["sender_email"],
            notify=False,
            dedupe_key=f"zulip:{message_id}",
        )
        storage.save_zulip_cursor(agent_label, channel, message_id)
        try:
            from . import zulip_chat

            zulip_chat.maybe_reply(agent_label, {**message, "channel": channel})
        except Exception as exc:
            # Chat replies are best-effort; ingestion must still succeed.
            log.warning(
                "zulip chat reply failed agent=%s channel=%s message_id=%s: %s",
                agent_label,
                channel,
                message_id,
                type(exc).__name__,
            )
        return True

    def _poll_channel(self, agent_label: str, channel: str) -> dict[str, Any]:
        cursor = storage.get_zulip_cursor(agent_label, channel)
        last_id = int(cursor["last_message_id"]) if cursor else -1
        anchor = str(last_id) if last_id >= 0 else "newest"
        try:
            result = zulip.zulip_fetch_messages(
                agent_label, channel, anchor=anchor, limit=self.fetch_limit
            )
        except Exception as exc:
            return self._record_error(agent_label, channel, str(exc))
        if not isinstance(result, dict) or result.get("received") is not True:
            error = (
                result.get("error", "Zulip API returned an invalid response")
                if isinstance(result, dict)
                else "Zulip API returned an invalid response"
            )
            return self._record_error(agent_label, channel, str(error))

        messages = result.get("messages", [])
        if not isinstance(messages, list):
            return self._record_error(
                agent_label, channel, "Zulip API returned an invalid message list"
            )

        processed = 0
        try:
            for message in sorted(
                (m for m in messages if isinstance(m, dict) and isinstance(m.get("id"), int)),
                key=lambda item: item["id"],
            ):
                message_id = int(message["id"])
                if message_id <= last_id:
                    continue
                persisted = self._persist_message(agent_label, message)
                last_id = message_id
                processed += int(persisted)

            next_anchor = result.get("next_anchor")
            if isinstance(next_anchor, int) and next_anchor > last_id:
                storage.save_zulip_cursor(agent_label, channel, next_anchor)
                last_id = next_anchor
            health = storage.record_zulip_worker_success(
                agent_label,
                channel,
                messages_processed=processed,
            )
        except Exception as exc:
            return self._record_error(agent_label, channel, str(exc))

        self._retry_until.pop((agent_label, channel), None)
        return {
            "status": "healthy",
            "agent": agent_label,
            "channel": channel,
            "messages_processed": processed,
            "cursor": last_id if last_id >= 0 else None,
            "last_success_at": health["last_success_at"],
        }

    def _record_queue_error(
        self,
        agent_label: str,
        error: str,
        *,
        reset: bool = False,
        clear_queue: bool = False,
    ) -> dict[str, Any]:
        attempt = self._queue_attempts.get(agent_label, 0) + 1
        self._queue_attempts[agent_label] = attempt
        delay = bounded_retry_delay(attempt, base_s=self.retry_base_s, max_s=self.retry_max_s)
        next_retry_at = _iso_plus(storage.now(), delay)
        row = storage.record_zulip_queue_error(
            agent_label,
            error,
            status="reset" if reset else "error",
            reset=reset,
            next_retry_at=next_retry_at,
            clear_queue=clear_queue,
        )
        self._queue_retry_until[agent_label] = time.monotonic() + delay
        return {
            "status": "error",
            "agent": agent_label,
            "messages_processed": 0,
            "queue": row,
            "error": row["last_error"],
            "next_retry_at": next_retry_at,
        }

    def _register_queue(self, agent_label: str, *, reset: bool = False) -> dict[str, Any] | None:
        try:
            result = zulip.zulip_register_event_queue(agent_label, self.channels)
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("registered") is not True:
            return None
        prior = storage.get_zulip_queue(agent_label)
        reset_count = int(prior["reset_count"]) + (1 if reset else 0) if prior else 0
        row = storage.save_zulip_queue(
            agent_label,
            str(result["queue_id"]),
            int(result["last_event_id"]),
            reset_count=reset_count,
        )
        self._queue_retry_until.pop(agent_label, None)
        return row

    def _backfill_agent(self, agent_label: str) -> int:
        processed = 0
        for channel in self.channels:
            result = self._poll_channel(agent_label, channel)
            if result.get("status") == "error":
                raise RuntimeError(str(result.get("error", "REST backfill failed")))
            processed += int(result.get("messages_processed", 0))
        return processed

    def _poll_event_queue(self, agent_label: str) -> dict[str, Any]:
        queue = storage.get_zulip_queue(agent_label)
        if queue is None or not queue.get("queue_id"):
            queue = self._register_queue(agent_label)
            if queue is None:
                return self._record_queue_error(agent_label, "queue registration unavailable")
            try:
                processed = self._backfill_agent(agent_label)
            except Exception as exc:
                # Drop the new queue_id until REST backfill succeeds; otherwise the
                # next poll consumes the queue and silently skips the gap.
                return self._record_queue_error(agent_label, str(exc), clear_queue=True)
            self._queue_attempts.pop(agent_label, None)
            return {
                "status": "healthy",
                "agent": agent_label,
                "messages_processed": processed,
                "queue": storage.get_zulip_queue(agent_label),
            }

        try:
            result = zulip.zulip_fetch_events(
                agent_label,
                str(queue["queue_id"]),
                int(queue["last_event_id"]),
            )
        except Exception as exc:
            return self._record_queue_error(agent_label, str(exc))
        if not isinstance(result, dict) or result.get("received") is not True:
            code = result.get("code") if isinstance(result, dict) else None
            if code == "BAD_EVENT_QUEUE_ID":
                fresh = self._register_queue(agent_label, reset=True)
                if fresh is None:
                    return self._record_queue_error(
                        agent_label, "queue reset registration failed", reset=True
                    )
                try:
                    processed = self._backfill_agent(agent_label)
                except Exception as exc:
                    return self._record_queue_error(
                        agent_label, str(exc), reset=True, clear_queue=True
                    )
                self._queue_attempts.pop(agent_label, None)
                return {
                    "status": "recovered",
                    "agent": agent_label,
                    "messages_processed": processed,
                    "queue": storage.get_zulip_queue(agent_label),
                }
            error = (
                result.get("error", "Zulip event queue fetch failed")
                if isinstance(result, dict)
                else "Zulip event queue fetch failed"
            )
            return self._record_queue_error(agent_label, str(error))

        raw_events = result.get("events", [])
        if not isinstance(raw_events, list):
            return self._record_queue_error(
                agent_label, "Zulip event queue returned invalid events"
            )
        processed = 0
        highest_event_id = int(queue["last_event_id"])
        touched_channels: set[str] = set()
        try:
            for event in sorted(
                (
                    item
                    for item in raw_events
                    if isinstance(item, dict) and isinstance(item.get("id"), int)
                ),
                key=lambda item: int(item["id"]),
            ):
                highest_event_id = max(highest_event_id, int(event["id"]))
                if event.get("type") != "message" or not isinstance(event.get("message"), dict):
                    continue
                message = dict(event["message"])
                message.setdefault("channel", message.get("display_recipient"))
                message.setdefault("topic", message.get("subject", ""))
                if not isinstance(message.get("id"), int):
                    continue
                channel = self._logical_channel(message.get("channel"))
                touched_channels.add(channel)
                if self._persist_message(agent_label, message):
                    processed += 1
            storage.advance_zulip_queue(agent_label, highest_event_id)
            for channel in self.channels:
                storage.record_zulip_worker_success(agent_label, channel, messages_processed=0)
        except Exception as exc:
            return self._record_queue_error(agent_label, str(exc))
        self._queue_attempts.pop(agent_label, None)
        return {
            "status": "healthy",
            "agent": agent_label,
            "messages_processed": processed,
            "queue": storage.get_zulip_queue(agent_label),
            "channels": sorted(touched_channels),
        }

    def poll_once(
        self, *, agent_label: str | None = None, channel: str | None = None
    ) -> dict[str, Any]:
        """Poll one legacy REST pair or one/all event queues once."""
        agents = (agent_label,) if agent_label is not None else self.agents
        if channel is not None:
            results = [self._poll_channel(agent, channel) for agent in agents]
        else:
            results = [self._poll_event_queue(agent) for agent in agents]
        if len(results) == 1:
            return results[0]
        return {
            "status": "error" if any(row["status"] == "error" for row in results) else "healthy",
            "results": results,
            "messages_processed": sum(int(row["messages_processed"]) for row in results),
        }

    def health_payload(self, *, now: str | None = None) -> dict[str, Any]:
        """Return read-only health for every configured bot/channel pair."""
        timestamp = now or storage.now()
        queue_rows = {row["agent_label"]: row for row in storage.list_zulip_queues()}
        stored = {
            (row["agent_label"], row["channel"]): row for row in storage.list_zulip_worker_health()
        }
        cursors = {
            (row["agent_label"], row["channel"]): row for row in storage.list_zulip_cursors()
        }
        channels: list[dict[str, Any]] = []
        for agent_label in self.agents:
            for channel in self.channels:
                row = stored.get((agent_label, channel))
                cursor = cursors.get((agent_label, channel))
                if row is None:
                    row = {
                        "agent_label": agent_label,
                        "channel": channel,
                        "status": "not_started",
                        "last_poll_at": None,
                        "last_success_at": None,
                        "last_error": None,
                        "messages_processed": 0,
                        "consecutive_errors": 0,
                        "next_retry_at": None,
                        "updated_at": None,
                    }
                status = row["status"]
                if (
                    status == "healthy"
                    and _seconds_since(row["last_success_at"], timestamp) is not None
                ):
                    age = _seconds_since(row["last_success_at"], timestamp)
                    if age is not None and age >= self.poll_interval_s * STALE_MULTIPLIER:
                        status = "stale"
                item = dict(row)
                item["status"] = status
                item["cursor"] = cursor["last_message_id"] if cursor else None
                item["cursor_age_seconds"] = _seconds_since(
                    cursor["updated_at"] if cursor else None, timestamp
                )
                channels.append(item)

        counts = {
            status: sum(row["status"] == status for row in channels)
            for status in ("healthy", "error", "stale", "not_started")
        }
        overall = (
            "error"
            if counts["error"]
            else "stale"
            if counts["stale"]
            else "healthy"
            if counts["healthy"]
            else "not_started"
        )
        successful = [row["last_success_at"] for row in channels if row["last_success_at"]]
        errors = [row["last_error"] for row in channels if row["last_error"]]
        queues = []
        for agent_label in self.agents:
            row = queue_rows.get(agent_label) or {
                "agent_label": agent_label,
                "queue_id": None,
                "last_event_id": -1,
                "registered_at": None,
                "last_event_at": None,
                "status": "not_started",
                "last_error": None,
                "next_retry_at": None,
                "reset_count": 0,
                "updated_at": None,
            }
            item = dict(row)
            item["queue_age_seconds"] = _seconds_since(row.get("last_event_at"), timestamp)
            queues.append(item)
        queue_counts = {
            status: sum(row["status"] == status for row in queues)
            for status in ("healthy", "error", "reset", "not_started")
        }
        if queue_counts["error"] or queue_counts["reset"]:
            overall = "error"
        # A retired host must not page on a worker we stopped on purpose.
        # Leftover healthy rows would otherwise age into "stale".
        if zulip.zulip_retired():
            overall = "retired"
        queue_errors = [row["last_error"] for row in queues if row.get("last_error")]
        return {
            "status": overall,
            "poll_interval_s": self.poll_interval_s,
            "retry_base_s": self.retry_base_s,
            "retry_max_s": self.retry_max_s,
            "last_successful_poll": max(successful) if successful else None,
            "last_error": errors[-1] if errors else None,
            "summary": counts,
            "channels": channels,
            "queues": queues,
            "queue_summary": queue_counts,
            "queue_error": queue_errors[-1] if queue_errors else None,
        }

    def stop(self) -> None:
        """Request a supervised loop to stop after its current poll."""
        self._stop.set()

    def _retry_is_due(self, agent_label: str, channel: str) -> bool:
        if time.monotonic() < self._retry_until.get((agent_label, channel), 0.0):
            return False
        health = storage.get_zulip_worker_health(agent_label, channel)
        deadline = health.get("next_retry_at") if health else None
        if not deadline:
            return True
        try:
            current = datetime.fromisoformat(storage.now())
            retry_at = datetime.fromisoformat(str(deadline))
        except ValueError:
            return True
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return current >= retry_at

    def run_forever(self) -> None:
        """Consume filtered event queues until stopped; systemd supervises restarts."""
        while not self._stop.is_set():
            for agent_label in self.agents:
                if time.monotonic() >= self._queue_retry_until.get(agent_label, 0.0):
                    self._poll_event_queue(agent_label)
            self._stop.wait(self.poll_interval_s)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def main() -> None:
    """Run the worker as a foreground process for a user service."""
    # Standalone entry must migrate/create tables even when the hub is not up.
    post_init.register_default_hooks()
    storage.init_db()
    worker = ZulipPollingWorker(
        poll_interval_s=_env_float("UA_ZULIP_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S),
        retry_base_s=_env_float("UA_ZULIP_RETRY_BASE_S", DEFAULT_RETRY_BASE_S),
        retry_max_s=_env_float("UA_ZULIP_RETRY_MAX_S", DEFAULT_RETRY_MAX_S),
    )

    def stop(_signum: int, _frame: Any) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever()
