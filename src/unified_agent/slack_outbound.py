"""Bounded outbound Slack replies for the canonical Unified-Agent hub.

Server-side ``SLACK_BOT_TOKEN`` only. This module never logs credentials,
never executes tools/merges/approvals, and never dual-writes to Zulip.
Live posting is opt-in: ``UA_SLACK_REPLY=1`` for inbound acks and
``UA_SLACK_MIRROR_MESSAGES=1`` for hub message_send mirrors. Tests inject
``post_fn`` so no network or secrets are required (task #559).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import events, object_model, storage
from .llm_client import LLMConnectionError
from .slack_config import MAX_TEXT_CHARS, SlackConfig, SlackGatewayError, _truthy

log = logging.getLogger(__name__)

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
DEFAULT_MAX_PER_HOUR = 30
CHANNEL_OUTBOUND = "slack.outbound"
CHANNEL_OUTBOUND_FAILED = "slack.outbound.failed"
CHANNEL_OUTBOUND_RELEASED = "slack.outbound.released"
ACK_TIMEOUT_S = 2.0
MIRROR_TIMEOUT_S = 10.0

PostFn = Callable[[str, dict[str, str], dict[str, str]], dict[str, Any]]


class SlackOutboundError(SlackGatewayError):
    """A rejected or failed outbound Slack send."""


def replies_enabled() -> bool:
    return _truthy(os.environ.get("UA_SLACK_REPLY", "0"))


def mirror_enabled() -> bool:
    return _truthy(os.environ.get("UA_SLACK_MIRROR_MESSAGES", "0"))


def max_sends_per_hour() -> int:
    try:
        value = int(os.environ.get("UA_SLACK_MAX_PER_HOUR", str(DEFAULT_MAX_PER_HOUR)))
    except ValueError:
        return DEFAULT_MAX_PER_HOUR
    return value if value > 0 else DEFAULT_MAX_PER_HOUR


def conversation_id(channel_id: str, root_ts: str) -> str:
    """Stable hub correlation key for one Slack thread."""
    return f"slack:{channel_id}:{root_ts}"


def thread_root(thread_ts: str | None, event_ts: str | None) -> str:
    """Prefer Slack thread_ts; fall back to the message ts as the root."""
    return str(thread_ts or event_ts or "").strip()


def _http_post(
    url: str,
    data: dict[str, str],
    headers: dict[str, str],
    timeout_s: float = ACK_TIMEOUT_S,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, data=data, headers=headers)
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
            raise
        if not isinstance(body, dict):
            raise SlackOutboundError("Slack API returned a non-object body")
        return body


def _reserve_outbound(
    *,
    dedupe_key: str,
    cap: int,
    cutoff: str,
) -> tuple[str | None, str]:
    """Atomically claim a Slack outbound slot (dedupe + hourly cap).

    Returns ``(ulid, status)`` where status is ``ok``, ``duplicate``, or
    ``rate-limited``; ``ulid`` is the ledger row of an earlier completed send
    (None while that send is still in flight). The caller must release the
    reservation if the Slack post fails so the same ``dedupe_key`` can retry.

    The slot lives in ``slack_outbound_slots``, not ``event_log`` (task #1640).
    It used to be a hashed ledger row that ``_release_reservation`` DELETEd,
    which left any event appended in the meantime chained to a row that no
    longer existed. Ledger rows from that path still count: a key already in
    ``event_log`` is a duplicate, and its ``slack.outbound`` rows count toward
    the cap (``UNION`` de-duplicates keys present in both).
    """
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sent = conn.execute(
            "SELECT ulid FROM event_log WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if sent is not None:
            conn.commit()
            return str(sent["ulid"]), "duplicate"
        slot = conn.execute(
            "SELECT event_ulid FROM slack_outbound_slots WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if slot is not None:
            conn.commit()
            return slot["event_ulid"], "duplicate"
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "SELECT dedupe_key FROM slack_outbound_slots WHERE reserved_at>=? "
            "UNION SELECT dedupe_key FROM event_log WHERE channel=? AND created_at>=?)",
            (cutoff, CHANNEL_OUTBOUND, cutoff),
        ).fetchone()
        if int(count_row["n"]) >= cap:
            conn.commit()
            return None, "rate-limited"
        conn.execute(
            "INSERT INTO slack_outbound_slots (dedupe_key, reserved_at) VALUES (?, ?)",
            (dedupe_key, ts),
        )
        conn.commit()
        return None, "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_reservation(dedupe_key: str) -> None:
    """Free a slot whose send failed. Touches only the non-ledger slot table."""
    conn = storage.connect()
    try:
        conn.execute(
            "DELETE FROM slack_outbound_slots WHERE dedupe_key=? AND event_ulid IS NULL",
            (dedupe_key,),
        )
        conn.commit()
    finally:
        conn.close()


def _record_sent(
    *, dedupe_key: str, payload: dict[str, Any], originator: str, episode_id: str
) -> str:
    """Append the completed send to the ledger and pin its ULID on the slot.

    One transaction: the ``slack.outbound`` row (ULID minted under the write
    lock) and the slot's ``event_ulid`` commit together, so a later duplicate
    reports the same ULID the ledger holds.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ulid, _, published = events.record_event_once_on_connection(
            conn,
            CHANNEL_OUTBOUND,
            payload=payload,
            originator=originator,
            episode_id=episode_id,
            dedupe_key=dedupe_key,
        )
        conn.execute(
            "UPDATE slack_outbound_slots SET event_ulid=? WHERE dedupe_key=?",
            (ulid, dedupe_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return ulid


# A slot lives for one chat.postMessage call, bounded by MIRROR_TIMEOUT_S. One
# older than this with no ledger row belongs to a process that died mid-send;
# a younger one may still be sending and must never be released.
STALE_SLOT_S = 600.0


def unconfirmed_slots(older_than_s: float = STALE_SLOT_S) -> list[dict[str, Any]]:
    """Send slots reserved more than ``older_than_s`` ago with no ledger row.

    A slot normally lives for one ``chat.postMessage`` call (seconds). One that
    is older and still has no ``event_ulid`` belongs to a process that died
    mid-send: Slack may or may not have delivered it, and nothing local can
    tell which. Delivery is at-most-once, so such a key stays a duplicate until
    an operator who has checked the Slack thread releases it.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_s)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT dedupe_key, reserved_at FROM slack_outbound_slots "
            "WHERE event_ulid IS NULL AND reserved_at <= ? ORDER BY reserved_at",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [{"dedupe_key": r["dedupe_key"], "reserved_at": r["reserved_at"]} for r in rows]


def release_unconfirmed_slot(
    dedupe_key: str, *, released_by: str, min_age_s: float = STALE_SLOT_S
) -> bool:
    """Operator action: free an abandoned send slot so its key can be retried.

    Only a slot with no ledger row, reserved at least ``min_age_s`` ago, is
    releasable. An in-progress send looks exactly like a crashed one in the
    slot table, so the age guard is what keeps a release from freeing a live
    POST's key and letting a retry deliver the message twice. A delivered send
    is never releasable. The release is appended to the ledger
    (``slack.outbound.released``) in the same transaction, so a later resend
    under that key is explained. Returns False when nothing was released.
    """
    if not (released_by or "").strip():
        raise ValueError("released_by is required")
    cutoff = (datetime.now(UTC) - timedelta(seconds=min_age_s)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        slot = conn.execute(
            "SELECT reserved_at FROM slack_outbound_slots "
            "WHERE dedupe_key=? AND event_ulid IS NULL AND reserved_at <= ?",
            (dedupe_key, cutoff),
        ).fetchone()
        sent = conn.execute("SELECT 1 FROM event_log WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if slot is None or sent is not None:
            conn.commit()
            return False
        conn.execute("DELETE FROM slack_outbound_slots WHERE dedupe_key=?", (dedupe_key,))
        _, _, published = events.record_event_once_on_connection(
            conn,
            CHANNEL_OUTBOUND_RELEASED,
            payload={"dedupe_key": dedupe_key, "reserved_at": slot["reserved_at"]},
            originator=released_by,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    log.warning("released unconfirmed Slack send slot %s (by %s)", dedupe_key, released_by)
    return True


_pool_lock = threading.Lock()
_global_pool: BoundedOutboundPool | None = None

DEFAULT_WORKERS = 4
DEFAULT_MAX_QUEUE = 100


def _get_worker_config() -> tuple[int, int]:
    try:
        workers = int(os.environ.get("UA_SLACK_OUTBOUND_WORKERS", str(DEFAULT_WORKERS)))
    except ValueError:
        workers = DEFAULT_WORKERS
    try:
        max_queue = int(os.environ.get("UA_SLACK_OUTBOUND_MAX_QUEUE", str(DEFAULT_MAX_QUEUE)))
    except ValueError:
        max_queue = DEFAULT_MAX_QUEUE
    return max(1, workers), max(1, max_queue)


class BoundedOutboundPool:
    """Bounded, managed worker pool for Slack outbound execution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue | None = None
        self._workers: list[threading.Thread] = []
        self._shutdown = False

    def _ensure_workers(self) -> tuple[queue.Queue | None, bool]:
        with self._lock:
            if self._shutdown:
                return None, False
            if self._queue is None:
                workers_count, max_queue = _get_worker_config()
                self._queue = queue.Queue(maxsize=max_queue)
                self._workers = []
                for i in range(workers_count):
                    t = threading.Thread(
                        target=self._worker_loop,
                        name=f"slack-outbound-{i}",
                        daemon=True,
                    )
                    t.start()
                    self._workers.append(t)
            return self._queue, True

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                q = self._queue
            if q is None:
                break
            try:
                item = q.get()
            except Exception:
                break
            if item is None:
                q.task_done()
                break
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except (httpx.HTTPError, OSError, TimeoutError, SlackOutboundError, LLMConnectionError):
                log.exception("background Slack outbound failed")
            except Exception:
                log.exception("unexpected background Slack outbound error")
                raise
            finally:
                q.task_done()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        q, ok = self._ensure_workers()
        if not ok or q is None:
            return False
        try:
            q.put_nowait((fn, args, kwargs))
            return True
        except queue.Full:
            log.warning("background Slack outbound queue is full (overloaded)")
            return False

    def shutdown(self, wait: bool = True, timeout_s: float = 5.0) -> None:
        with self._lock:
            if self._shutdown or self._queue is None:
                return
            self._shutdown = True
            workers = list(self._workers)
            q = self._queue

        for _ in workers:
            try:
                q.put_nowait(None)
            except Exception:
                pass

        if wait:
            deadline = time.monotonic() + timeout_s
            for t in workers:
                remaining = max(0.0, deadline - time.monotonic())
                t.join(timeout=remaining)

        with self._lock:
            self._queue = None
            self._workers = []
            self._shutdown = False


_stale_claims_recovered = False


def _recover_stale_chat_claims_once() -> None:
    """Fail stale in-flight chat claims after a hub restart (task #580)."""
    global _stale_claims_recovered
    if _stale_claims_recovered:
        return
    _stale_claims_recovered = True
    try:
        from . import storage

        recovered = storage.recover_stale_slack_chat_claims()
        if recovered:
            log.info("recovered %d stale Slack chat claim(s) after restart", len(recovered))
    except Exception:
        log.exception("stale Slack chat claim recovery failed")


def _get_pool() -> BoundedOutboundPool:
    _recover_stale_chat_claims_once()
    global _global_pool
    with _pool_lock:
        if _global_pool is None:
            _global_pool = BoundedOutboundPool()
        return _global_pool


def schedule_background(target: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    """Run outbound Slack work off the inbound HTTP / tool execution path.

    Returns True if scheduled successfully, False if rejected due to queue overload or shutdown.
    """
    return _get_pool().submit(target, *args, **kwargs)


def shutdown_background_workers(wait: bool = True, timeout_s: float = 5.0) -> None:
    """Drain and shutdown background Slack workers."""
    global _global_pool
    with _pool_lock:
        pool = _global_pool
        _global_pool = None
    if pool is not None:
        pool.shutdown(wait=wait, timeout_s=timeout_s)


# Backward-compatible private alias (call sites / older patches).
_schedule_background = schedule_background


def _transport_error_type(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, OSError):
        return "os"
    if isinstance(exc, SlackOutboundError):
        return "rejected"
    return "transport"


# Slack allows 50 blocks per message; the character bound keeps a card well
# under the API's request size and refuses an accidental payload dump.
MAX_BLOCKS = 50
MAX_BLOCKS_JSON_CHARS = 12000


def _blocks_json(blocks: list[dict[str, Any]] | None) -> str | None:
    if blocks is None:
        return None
    if not isinstance(blocks, list) or not blocks:
        raise SlackOutboundError("blocks must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise SlackOutboundError(f"outbound Slack blocks exceed {MAX_BLOCKS}")
    if not all(isinstance(block, dict) for block in blocks):
        raise SlackOutboundError("every Slack block must be an object")
    encoded = json.dumps(blocks, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) > MAX_BLOCKS_JSON_CHARS:
        raise SlackOutboundError(f"outbound Slack blocks exceed {MAX_BLOCKS_JSON_CHARS} characters")
    return encoded


def send_message(
    *,
    channel_id: str,
    text: str,
    agent_label: str,
    thread_ts: str | None = None,
    event_ts: str | None = None,
    dedupe_key: str,
    config: SlackConfig | None = None,
    post_fn: PostFn | None = None,
    now: datetime | None = None,
    allow_unthreaded: bool = False,
    timeout_s: float = ACK_TIMEOUT_S,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Post one bounded Slack message. Duplicate ``dedupe_key`` is a no-op.

    ``blocks`` (task #2303) adds a Block Kit layout; ``text`` stays required as
    the notification fallback. Blocks go through the same allowlist, dedupe
    slot and hourly cap as plain text. They are capped at ``MAX_BLOCKS`` and
    ``MAX_BLOCKS_JSON_CHARS`` and are never written to the ledger.
    """
    config = config or SlackConfig.from_env()
    if not config.enabled:
        raise SlackOutboundError("Slack gateway is disabled")
    token = (config.bot_token or "").strip()
    if not token:
        raise SlackOutboundError("SLACK_BOT_TOKEN is required for outbound Slack")
    if channel_id not in config.channel_ids:
        raise SlackOutboundError("Slack channel ID is not allowlisted")
    body = (text or "").strip()
    if not body:
        raise SlackOutboundError("outbound Slack text must not be empty")
    label = (agent_label or "").strip() or config.coordinator_agent
    rendered = f"[{label}] {body}"
    if len(rendered) > MAX_TEXT_CHARS:
        raise SlackOutboundError(f"outbound Slack text exceeds {MAX_TEXT_CHARS} characters")
    key = (dedupe_key or "").strip()
    if not key:
        raise SlackOutboundError("dedupe_key is required")
    blocks_json = _blocks_json(blocks)
    root = thread_root(thread_ts, event_ts)
    if not root and not allow_unthreaded:
        raise SlackOutboundError("thread_ts or event_ts is required for conversation correlation")
    conv = conversation_id(channel_id, root or key)
    current = now or datetime.now(UTC)
    cutoff = (current - timedelta(hours=1)).isoformat(timespec="seconds")
    payload = {
        "channel_id": channel_id,
        "agent": label,
        "conversation_id": conv,
        "thread_ts": root or None,
        "chars": len(rendered),
    }
    if blocks_json is not None:
        payload["blocks"] = len(blocks or ())
    event_ulid, status = _reserve_outbound(
        dedupe_key=key,
        cap=max_sends_per_hour(),
        cutoff=cutoff,
    )
    if status == "duplicate":
        return {
            "sent": False,
            "duplicate": True,
            # No ledger row yet: either a concurrent send is mid-POST, or a
            # process died mid-send (see unconfirmed_slots).
            "in_flight": event_ulid is None,
            "conversation_id": conv,
            "thread_ts": root or None,
            "event_ulid": event_ulid,
        }
    if status == "rate-limited":
        object_model.by_kind("evidence_event").writer("append")(
            CHANNEL_OUTBOUND_FAILED,
            payload={"reason": "rate-limited", "channel_id": channel_id, "agent": label},
            originator=label,
            notify=False,
        )
        return {"sent": False, "reason": "rate-limited", "conversation_id": conv}

    form = {
        "channel": channel_id,
        "text": rendered,
        "unfurl_links": "false",
        "unfurl_media": "false",
    }
    if root:
        form["thread_ts"] = root
    if blocks_json is not None:
        form["blocks"] = blocks_json
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    poster = post_fn or (lambda url, data, hdrs: _http_post(url, data, hdrs, timeout_s=timeout_s))
    try:
        response = poster(SLACK_POST_URL, form, headers)
    except (httpx.HTTPError, OSError, TimeoutError, SlackOutboundError) as exc:
        _release_reservation(key)
        error_type = _transport_error_type(exc)
        object_model.by_kind("evidence_event").writer("append")(
            CHANNEL_OUTBOUND_FAILED,
            payload={
                "reason": "transport",
                "error_type": error_type,
                "channel_id": channel_id,
            },
            originator=label,
            notify=False,
            dedupe_key=f"{key}:failed",
        )
        return {
            "sent": False,
            "reason": "transport",
            "conversation_id": conv,
            "error_type": error_type,
        }

    if not isinstance(response, dict) or not response.get("ok"):
        _release_reservation(key)
        error = "slack-api"
        if isinstance(response, dict):
            error = str(response.get("error") or error)
        object_model.by_kind("evidence_event").writer("append")(
            CHANNEL_OUTBOUND_FAILED,
            payload={"reason": "api", "error": error, "channel_id": channel_id},
            originator=label,
            notify=False,
            dedupe_key=f"{key}:failed",
        )
        return {"sent": False, "reason": "api", "error": error, "conversation_id": conv}

    ts = str(response.get("ts") or root or "")
    # The message is out: the slot stays held (dedupe + cap) whatever happens
    # next, and a ledger failure is reported rather than raised, so the caller
    # cannot mistake a delivered message for a failed one and resend it.
    try:
        event_ulid = _record_sent(
            dedupe_key=key, payload=payload, originator=label, episode_id=conv
        )
    except (sqlite3.Error, storage.LiveStoreError):
        log.exception("Slack send %s delivered but its ledger append failed", key)
        event_ulid = None
    if not root:
        root = ts
        conv = conversation_id(channel_id, root or key)
    return {
        "sent": True,
        "duplicate": False,
        "conversation_id": conv,
        "thread_ts": root,
        "ts": ts,
        "event_ulid": event_ulid,
        "agent": label,
        "channel_id": channel_id,
    }


def _escape_slack_mrkdwn(text: str) -> str:
    """Escape Slack mrkdwn control characters (task #1104). Order matters: '&' first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _task_status_ack_text(result: dict[str, Any]) -> str:
    """Render a ``status task-<id>`` reply from the narrow projection only.

    ``result["task"]`` (when present) carries exactly id/title/status/project/
    owner — see ``slack_gateway._slack_task_status`` — never notes, memory,
    approvals, or evidence. Every field that can contain Slack-user text
    (title, project, owner) is escaped before it reaches ``chat.postMessage``.
    """
    reason = result.get("reason")
    if reason == "denied":
        # Deliberately generic (task #1104 follow-up): feature-disabled,
        # unmapped, revoked, and invalid-config-map all render identically so
        # the reply cannot be used to enumerate which case applied.
        return "status lookup not available for this user"
    if reason == "usage":
        return "usage: status task-<id>"
    if reason == "not_found":
        return f"task-{result.get('task_id')} not found"
    task = result.get("task") or {}
    title = _escape_slack_mrkdwn(str(task.get("title") or "")[:200])
    project = _escape_slack_mrkdwn(str(task.get("project") or ""))
    owner = _escape_slack_mrkdwn(str(task.get("owner") or "unclaimed"))
    text = (
        f"task-{task.get('id')} [{task.get('status')}] {title} (project: {project}, owner: {owner})"
    )
    return text[:MAX_TEXT_CHARS]


WIP_CAP_ACK = (
    "not applied: a WIP cap is reached (finish or release in-flight work first; "
    "high-priority or [urgency: now] tasks pass only the fleet cap)"
)


def _ack_text(handle_result: dict[str, Any]) -> str:
    if handle_result.get("dispatched") is False and handle_result.get("reason") == "wip_cap":
        return WIP_CAP_ACK
    if handle_result.get("dispatched") is False:
        return "accepted; queued for human review"
    result = handle_result.get("result") if isinstance(handle_result.get("result"), dict) else {}
    if result.get("kind") == "task_status":
        return _task_status_ack_text(result)
    if result.get("requires_human_resolution"):
        return "approval listed for human resolution; chat cannot approve"
    if "id" in result:
        return f"accepted task-{result['id']}"
    if "plan_id" in result:
        return f"accepted plan {result['plan_id']}"
    if result.get("kind") == "contact":
        return f"contact routed to {result.get('agent')}"
    return "accepted"


def maybe_reply_to_inbound(
    *,
    handle_result: dict[str, Any],
    channel_id: str,
    thread_ts: str | None,
    event_ts: str | None,
    event_id: str,
    config: SlackConfig | None = None,
    post_fn: PostFn | None = None,
) -> dict[str, Any] | None:
    """Best-effort in-thread ack. Never raises into the inbound adapter.

    Slash commands often have no ``thread_ts``/``message_ts``; those acks
    post unthreaded rather than treating ``event_id`` as a Slack timestamp.
    """
    if not replies_enabled() or handle_result.get("duplicate"):
        return None
    root = thread_root(thread_ts, event_ts)
    try:
        return send_message(
            channel_id=channel_id,
            text=_ack_text(handle_result),
            agent_label=(config or SlackConfig.from_env()).coordinator_agent,
            thread_ts=thread_ts,
            event_ts=event_ts,
            dedupe_key=f"slack:reply:{event_id}",
            config=config,
            post_fn=post_fn,
            allow_unthreaded=not bool(root),
            timeout_s=ACK_TIMEOUT_S,
        )
    except SlackOutboundError:
        log.exception("outbound Slack ack rejected for %s", event_id)
        return {"sent": False, "reason": "ack-error"}
    except (httpx.HTTPError, OSError, TimeoutError):
        log.exception("outbound Slack ack failed for %s", event_id)
        return {"sent": False, "reason": "ack-error"}


def schedule_reply_to_inbound(
    *,
    handle_result: dict[str, Any],
    channel_id: str,
    thread_ts: str | None,
    event_ts: str | None,
    event_id: str,
    config: SlackConfig | None = None,
    post_fn: PostFn | None = None,
) -> bool:
    """Queue a best-effort inbound ack so the HTTP handler can return first."""
    if not replies_enabled() or handle_result.get("duplicate"):
        return False
    return schedule_background(
        maybe_reply_to_inbound,
        handle_result=handle_result,
        channel_id=channel_id,
        thread_ts=thread_ts,
        event_ts=event_ts,
        event_id=event_id,
        config=config,
        post_fn=post_fn,
    )


def maybe_mirror_hub_message(
    *,
    message_result: dict[str, Any],
    body: str,
    config: SlackConfig | None = None,
    post_fn: PostFn | None = None,
) -> dict[str, Any] | None:
    """Best-effort Slack mirror of a hub message_send. Never fails the hub send."""
    if not mirror_enabled() or message_result.get("duplicate"):
        return None
    channel = os.environ.get("UA_SLACK_MIRROR_CHANNEL_ID", "").strip()
    if not channel:
        return None
    thread = os.environ.get("UA_SLACK_MIRROR_THREAD_TS", "").strip() or None
    agent = str(message_result.get("from_agent") or "")
    mid = str(message_result.get("message_id") or "")
    preview = (body or "").strip()[:500]
    act = str(message_result.get("act") or "")
    dest = str(message_result.get("to_agent") or "")
    text = f"{agent} -> {dest} [{act}] {preview}".strip()
    try:
        return send_message(
            channel_id=channel,
            text=text,
            agent_label=agent,
            thread_ts=thread,
            event_ts=thread,
            dedupe_key=f"slack:mirror:{mid}",
            config=config,
            post_fn=post_fn,
            allow_unthreaded=not bool(thread),
            timeout_s=MIRROR_TIMEOUT_S,
        )
    except SlackOutboundError:
        log.info("Slack message mirror skipped for %s", mid)
        return None
    except (httpx.HTTPError, OSError, TimeoutError):
        log.exception("Slack message mirror failed for %s", mid)
        return {"sent": False, "reason": "mirror-error"}
    except Exception:
        log.exception("unexpected Slack message mirror failure for %s", mid)
        return {"sent": False, "reason": "mirror-error"}


def schedule_mirror_hub_message(
    *,
    message_result: dict[str, Any],
    body: str,
    config: SlackConfig | None = None,
    post_fn: PostFn | None = None,
) -> bool:
    """Queue a best-effort hub mirror so message_send can return first."""
    if not mirror_enabled() or message_result.get("duplicate"):
        return False
    channel = os.environ.get("UA_SLACK_MIRROR_CHANNEL_ID", "").strip()
    if not channel:
        return False
    return schedule_background(
        maybe_mirror_hub_message,
        message_result=message_result,
        body=body,
        config=config,
        post_fn=post_fn,
    )


def slots_main(argv: list[str] | None = None) -> int:
    """``unified-agent-slack-slots``: list or release abandoned Slack send slots.

    ``list`` prints unconfirmed slots older than ``--older-than-s`` (default
    600s). ``release KEY --by LABEL`` frees one after the operator has checked
    the Slack thread for the message; it refuses slots younger than
    ``--min-age-s`` (default 600s), which may still be sending. Exit 1 when
    nothing was released.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="unified-agent-slack-slots")
    sub = parser.add_subparsers(dest="command", required=True)
    list_cmd = sub.add_parser("list", help="unconfirmed send slots")
    list_cmd.add_argument("--older-than-s", type=float, default=STALE_SLOT_S)
    release_cmd = sub.add_parser("release", help="free one abandoned slot")
    release_cmd.add_argument("dedupe_key")
    release_cmd.add_argument("--by", required=True, help="operator label, recorded on the ledger")
    release_cmd.add_argument("--min-age-s", type=float, default=STALE_SLOT_S)
    args = parser.parse_args(argv)

    if args.command == "list":
        print(json.dumps({"unconfirmed": unconfirmed_slots(older_than_s=args.older_than_s)}))
        return 0
    released = release_unconfirmed_slot(
        args.dedupe_key, released_by=args.by, min_age_s=args.min_age_s
    )
    print(json.dumps({"dedupe_key": args.dedupe_key, "released": released}))
    return 0 if released else 1
