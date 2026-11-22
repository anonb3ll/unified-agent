"""Slack Block Kit cards for hub approvals (task #2303). Outbound only.

Off unless ``UA_APPROVAL_SLACK_CHANNEL`` names a Slack channel ID. That channel
must be in the Slack allowlist (``UA_SLACK_CHANNEL_IDS`` or
``UA_SLACK_CHANNEL_MAP``), the gateway kill switch ``UA_SLACK_ENABLED`` must be
on, and ``SLACK_BOT_TOKEN`` must be set (all read through ``SlackConfig``).

A new pending approval becomes one card: approval id, tool, requesting agent,
run id, created time, the pass lifetime, the redacted ``args_sha256`` and one
URL button to the Control Center Approvals pane. The card is a notification,
not an authority path. It has no approve or deny button, and nothing reads a
Slack reply as a decision: the human approves in the Control Center through
the existing verified-human path. A decision posts a follow-up threaded under
the card. The raw payload, tokens and secrets never reach Slack.

Durability. Each card or follow-up is first written as a delivery intent to
``approval_slack_outbox`` (after the approval row has committed; the Slack
call itself never runs on the approval path). One worker per process claims
intents atomically and drains them; a hub restart resumes unfinished ones
(``startup_drain``, and stale claims from a dead process are reclaimed). A
decision waits until its card is confirmed and is never posted unthreaded:
if the card failed or cannot be threaded, the follow-up is dead-lettered.

Retries. Transport errors, HTTP 429 and 5xx, and the Slack API's rate-limit
and server errors are rescheduled with backoff, honouring ``Retry-After`` up
to ``MAX_RETRY_AFTER_S``. Every attempt and every terminal loss (queue
overflow, worker exception, refusal, retries exhausted) appends an
``approval.delivery`` evidence event with the channel, approval id and
outcome, never a URL or token. Terminal losses carry ``dead_letter: true``,
which ``system_doctor``'s ``approval_slack_cards`` row reports.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import approval_push, config, edition, events, gateway, object_model, storage
from .slack_config import SlackConfig, SlackGatewayError, _channel_mapping, _truthy
from .slack_outbound import MIRROR_TIMEOUT_S, SlackOutboundError, send_message

logger = logging.getLogger(__name__)

CHANNEL_ENV = "UA_APPROVAL_SLACK_CHANNEL"
EVIDENCE_CHANNEL = "approval.delivery"
AGENT_LABEL = "approvals"
ORIGINATOR = "approval-slack"
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 4.0)
MAX_RETRY_AFTER_S = 300.0
# Outstanding (pending or sending) intents beyond this are dead-lettered at
# once, so a Slack outage cannot grow the outbox without bound.
MAX_PENDING = 64
# A decision waits this long between checks for its card, and gives up after
# MAX_DEFER_S rather than post a follow-up that cannot be threaded.
DEFER_S = 2.0
MAX_DEFER_S = 600.0
# A claim older than this belongs to a process that died mid-delivery. One
# claim covers a single send (MIRROR_TIMEOUT_S), so this is generous.
STALE_CLAIM_S = 600.0
DEAD_LETTER_WINDOW_H = 24

# Slack API error codes worth another attempt: rate limiting and server-side
# failures. Anything else (channel_not_found, not_in_channel, invalid_auth...)
# will fail the same way again, so it dead-letters at once.
RETRYABLE_API_ERRORS = frozenset(
    {
        "ratelimited",
        "rate_limited",
        "internal_error",
        "fatal_error",
        "service_unavailable",
        "request_timeout",
    }
)

# Block Kit limits (characters). A card over any of them is rejected whole by
# Slack, so every rendered string is cut to its block's bound.
HEADER_MAX = 150
SECTION_TEXT_MAX = 3000
FIELD_MAX = 2000
CONTEXT_MAX = 2000
BUTTON_TEXT_MAX = 75
URL_MAX = 3000

_ERROR_CODE_RE = re.compile(r"[a-z0-9_.-]{1,64}")
_POST_URL = "https://slack.com/api/chat.postMessage"

_lock = threading.Lock()
_worker_running = False
_wakeup = threading.Event()


# ---------------------------------------------------------------- test seams


def _now() -> float:
    return time.time()


def _sleep(delay: float) -> None:
    """Idle until the next intent is due; a new intent wakes the worker early."""
    _wakeup.wait(delay)
    _wakeup.clear()


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="approval-slack", daemon=True).start()


def _transport(
    url: str, data: Mapping[str, str], headers: Mapping[str, str], timeout_s: float
) -> tuple[int, Mapping[str, str], dict[str, Any] | None]:
    """One HTTP POST: ``(status, response headers, JSON object body or None)``."""
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, data=dict(data), headers=dict(headers))
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, response.headers, body if isinstance(body, dict) else None


# ------------------------------------------------------------ configuration


def configured_channel() -> str | None:
    """The card channel from ``UA_APPROVAL_SLACK_CHANNEL``, or None when off."""
    value = os.environ.get(CHANNEL_ENV, "").strip()
    return value or None


def allowlisted_channels(channel_ids_csv: str | None, channel_map: str | None) -> frozenset[str]:
    """The Slack allowlist exactly as ``SlackConfig.from_env`` derives it.

    Raises ``SlackGatewayError`` for a malformed ``UA_SLACK_CHANNEL_MAP``.
    """
    mapping = _channel_mapping(channel_map or "")
    if mapping:
        return frozenset(mapping)
    return frozenset(p.strip() for p in (channel_ids_csv or "").split(",") if p.strip())


def config_problem(
    channel: str, *, enabled: bool, bot_token: str | None, channel_ids: frozenset[str]
) -> str | None:
    """Why cards cannot post to ``channel``, or None. Shared with the doctor row."""
    if not enabled:
        return "UA_SLACK_ENABLED is off (the Slack kill switch)"
    if not (bot_token or "").strip():
        return "SLACK_BOT_TOKEN is unset"
    if channel not in channel_ids:
        return f"{CHANNEL_ENV} is not in UA_SLACK_CHANNEL_IDS / UA_SLACK_CHANNEL_MAP"
    return None


def channel_problem_from_settings(
    channel: str,
    *,
    enabled_raw: str | None,
    bot_token: str | None,
    channel_ids_csv: str | None,
    channel_map: str | None,
) -> str | None:
    """``config_problem`` over raw settings, for a doctor that reads the hub unit.

    A malformed channel map is a problem, not an exception.
    """
    try:
        channel_ids = allowlisted_channels(channel_ids_csv, channel_map)
    except SlackGatewayError as exc:
        return f"UA_SLACK_CHANNEL_MAP is malformed: {exc}"
    return config_problem(
        channel, enabled=_truthy(enabled_raw or "0"), bot_token=bot_token, channel_ids=channel_ids
    )


def _refusal_code(problem: str) -> str:
    if problem.startswith("UA_SLACK_ENABLED"):
        return "slack-disabled"
    if problem.startswith("SLACK_BOT_TOKEN"):
        return "bot-token-missing"
    if problem.startswith("UA_SLACK_CHANNEL_MAP"):
        return "slack-config-invalid"
    return "channel-not-allowlisted"


# ------------------------------------------------------------------ evidence


def _record(
    approval_id: int,
    event: str,
    channel: str | None,
    attempt: int,
    outcome: str,
    *,
    dead_letter: bool = False,
    error: str | None = None,
    ts: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "approval_id": approval_id,
        "event": event,
        "channel_id": channel,
        "attempt": attempt,
        "outcome": outcome,
        "dead_letter": dead_letter,
    }
    if error:
        payload["error"] = error if _ERROR_CODE_RE.fullmatch(error) else "unrecognised"
    if ts:
        payload["ts"] = ts
    try:
        object_model.by_kind("evidence_event").writer("append")(
            EVIDENCE_CHANNEL, payload=payload, originator=ORIGINATOR, notify=False
        )
    except Exception as exc:  # noqa: BLE001 - recording must not kill the worker
        logger.warning(
            "approval slack evidence not recorded approval=%s error=%s",
            approval_id,
            type(exc).__name__,
        )
    if dead_letter:
        logger.warning(
            "approval slack delivery dead-lettered approval=%s event=%s error=%s",
            approval_id,
            event,
            payload.get("error"),
        )


def dead_letters_since(hours: float = DEAD_LETTER_WINDOW_H) -> int:
    """How many deliveries dead-lettered in the last ``hours``."""
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE channel=? AND created_at>=? "
            "AND json_extract(payload, '$.dead_letter')=1",
            (EVIDENCE_CHANNEL, cutoff),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


def last_delivery(channel: str) -> str | None:
    """When a card or follow-up was last delivered to ``channel``, or None."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS at FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.outcome')='delivered' "
            "AND json_extract(payload, '$.channel_id')=?",
            (EVIDENCE_CHANNEL, channel),
        ).fetchone()
    finally:
        conn.close()
    return row["at"] if row is not None else None


def stuck_intents(older_than_s: float = STALE_CLAIM_S) -> int:
    """Unfinished intents older than ``older_than_s``: a worker that is not draining."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM approval_slack_outbox "
            "WHERE status IN ('pending','sending') AND created_epoch < ?",
            (time.time() - older_than_s,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


# ---------------------------------------------------------------------- card


def _bounded(value: object, limit: int = 64) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit] if text else "unknown"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str, limit: int) -> dict[str, Any]:
    return {"type": "plain_text", "text": _cut(text, limit), "emoji": False}


def _args_sha256(raw_payload: str | None) -> str | None:
    try:
        decoded = json.loads(raw_payload or "{}")
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return gateway.arguments_sha256(decoded)


def approval_url(approval_id: int) -> str | None:
    """Control Center link for one approval, or None without a usable base URL."""
    base = approval_push._base_url()
    if base is None:
        return None
    url = f"{base}/ui/inbox?kind=approval&item={int(approval_id)}"
    # A cut URL would point somewhere else; drop the button instead.
    return url if len(url) <= URL_MAX else None


def build_card(row: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """``(fallback text, blocks)`` for one pending approval row.

    Every field is plain text, so no requester-supplied string can render as a
    Slack mention or link, and each is cut to its block's Slack limit. The
    payload is represented only by its redacted digest.
    """
    approval_id = int(row["id"])
    tool = events.approval_notification_tool(str(row["tool"] or "")) or "unlisted tool"
    requester = _bounded(row["requested_by"])
    run_id = events.bounded_run_id(row["run_id"]) or "none"
    created = _bounded(row["created_at"], 40)
    ttl = config.approval_pass_ttl_hours()
    digest = _args_sha256(row["payload"]) or "unavailable"
    title = f"Approval #{approval_id} waiting: {tool}"
    fields = [
        f"Approval: #{approval_id}",
        f"Tool: {tool}",
        f"Requested by: {requester}",
        f"Run: {run_id}",
        f"Created: {created}",
        f"Pass expiry: {ttl:g}h after approval",
    ]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(title, HEADER_MAX)},
        {"type": "section", "fields": [_plain(f, FIELD_MAX) for f in fields]},
        {"type": "section", "text": _plain(f"args_sha256: {digest}", SECTION_TEXT_MAX)},
    ]
    url = approval_url(approval_id)
    if url is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "open_approval",
                        "text": _plain("Open in Control Center", BUTTON_TEXT_MAX),
                        "url": url,
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                _plain(
                    "Approve or deny in the Control Center Approvals pane. "
                    "A Slack reply is never read as an approval.",
                    CONTEXT_MAX,
                )
            ],
        }
    )
    return title, blocks


# -------------------------------------------------------------------- outbox


def _key(approval_id: int, event: str, status: str | None = None) -> str:
    if event == "requested":
        return f"approval:{approval_id}:requested"
    return f"approval:{approval_id}:decided:{status}"


def _persist_intent(
    approval_id: int, event: str, decision_status: str | None, resolved_by: str | None
) -> bool:
    """Write one delivery intent. Returns True when the worker has work to do."""
    key = _key(approval_id, event, decision_status)
    now = _now()
    iso = storage.now()
    overflow = False
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT status FROM approval_slack_outbox WHERE dedupe_key=?", (key,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            # Already queued: wake the worker if it is still waiting to go out.
            return existing["status"] == "pending"
        outstanding = conn.execute(
            "SELECT COUNT(*) FROM approval_slack_outbox WHERE status IN ('pending','sending')"
        ).fetchone()[0]
        overflow = int(outstanding) >= MAX_PENDING
        conn.execute(
            "INSERT INTO approval_slack_outbox (dedupe_key, approval_id, event, status, "
            "decision_status, resolved_by, created_epoch, created_at, updated_at, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                approval_id,
                event,
                "dead" if overflow else "pending",
                decision_status,
                resolved_by,
                now,
                iso,
                iso,
                edition.tenant_org_id(),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if overflow:
        _record(
            approval_id,
            event,
            configured_channel(),
            0,
            "dropped",
            dead_letter=True,
            error="queue-overflow",
        )
        return False
    return True


def _update(key: str, **fields: Any) -> None:
    fields["updated_at"] = storage.now()
    cols = ", ".join(f"{name}=?" for name in fields)
    conn = storage.connect()
    try:
        conn.execute(
            f"UPDATE approval_slack_outbox SET {cols} WHERE dedupe_key=?",
            (*fields.values(), key),
        )
        conn.commit()
    finally:
        conn.close()


def _reclaim_stale() -> None:
    """Return intents claimed by a process that died mid-delivery to pending."""
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approval_slack_outbox SET status='pending', claimed_at=NULL, updated_at=? "
            "WHERE status='sending' AND claimed_at < ?",
            (storage.now(), _now() - STALE_CLAIM_S),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_next() -> dict[str, Any] | None:
    """Atomically claim the oldest due intent, or None."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM approval_slack_outbox WHERE status='pending' AND not_before<=? "
            "ORDER BY created_epoch, rowid LIMIT 1",
            (_now(),),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE approval_slack_outbox SET status='sending', claimed_at=?, updated_at=? "
            "WHERE dedupe_key=? AND status='pending'",
            (_now(), storage.now(), row["dedupe_key"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _next_due_in() -> float | None:
    """Seconds until the next pending intent is due, or None when none are pending."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT MIN(not_before) AS due FROM approval_slack_outbox WHERE status='pending'"
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["due"] is None:
        return None
    return max(0.0, float(row["due"]) - _now())


def _intent(key: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM approval_slack_outbox WHERE dedupe_key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _approval_row(approval_id: int, org_id: str | None) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, tool, payload, requested_by, created_at, run_id "
            "FROM approvals WHERE id=? AND org_id IS ?",
            (approval_id, org_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


# ------------------------------------------------------------------ delivery


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = None
    for name, value in headers.items():
        if str(name).lower() == "retry-after":
            raw = value
            break
    try:
        seconds = float(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None  # An HTTP-date; the fixed backoff applies.
    return seconds if seconds is not None and seconds >= 0 else None


def _attempt_send(
    *,
    key: str,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None,
    thread_ts: str | None,
    slack_config: SlackConfig,
) -> tuple[str, str | None, str | None, float | None]:
    """One send: ``(outcome, error, ts, retry_after)``.

    ``outcome`` is delivered, retry, failed, duplicate or unconfirmed.
    """
    seen: dict[str, Any] = {}

    def post(url: str, data: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        status, response_headers, body = _transport(url, data, headers, MIRROR_TIMEOUT_S)
        seen["status"] = int(status)
        seen["retry_after"] = _retry_after(response_headers)
        if body is None:
            raise SlackOutboundError(f"Slack returned HTTP {int(status)} without a JSON body")
        return body

    try:
        result = send_message(
            channel_id=channel,
            text=text,
            agent_label=AGENT_LABEL,
            thread_ts=thread_ts,
            dedupe_key=key,
            config=slack_config,
            post_fn=post,
            allow_unthreaded=thread_ts is None,
            timeout_s=MIRROR_TIMEOUT_S,
            blocks=blocks,
        )
    except SlackOutboundError as exc:
        logger.warning("approval slack send refused key=%s error=%s", key, type(exc).__name__)
        return "failed", "rejected", None, None
    status = seen.get("status")
    retry_after = seen.get("retry_after")
    if result.get("sent"):
        return "delivered", None, str(result.get("ts") or "") or None, None
    if result.get("duplicate"):
        # A ledger row means an earlier attempt delivered it. No row means a
        # send that died mid-POST: Slack may or may not have it (at-most-once).
        if result.get("in_flight"):
            return "unconfirmed", "unconfirmed-send", None, None
        return "duplicate", None, None, None
    reason = result.get("reason")
    server_side = status is not None and (status == 429 or status >= 500)
    if reason == "transport":
        if status is not None and not server_side:
            return "failed", f"http-{status}", None, None
        return "retry", str(result.get("error_type") or "transport"), None, retry_after
    if reason == "api":
        error = str(result.get("error") or "slack-api")
        if server_side or error in RETRYABLE_API_ERRORS:
            return "retry", error, None, retry_after
        return "failed", error, None, None
    # The hub's own hourly cap: it will not free within the retry window.
    return "failed", "hub-rate-limited", None, None


def _dead(row: Mapping[str, Any], channel: str | None, error: str, outcome: str = "failed") -> None:
    _update(row["dedupe_key"], status="dead", claimed_at=None)
    _record(
        int(row["approval_id"]),
        row["event"],
        channel,
        int(row["attempts"]),
        outcome,
        dead_letter=True,
        error=error,
    )


def _defer_decision(row: Mapping[str, Any], channel: str) -> bool:
    """Hold a decision until its card is confirmed. Returns True when it can send.

    Sets ``row["_thread_ts"]`` when it can. Never posts unthreaded: a card that
    failed, cannot be threaded, or stays unconfirmed past MAX_DEFER_S
    dead-letters the follow-up instead.
    """
    parent = _intent(_key(int(row["approval_id"]), "requested"))
    if parent is None:
        _dead(row, channel, "no-parent-card")
        return False
    if parent["status"] in ("pending", "sending"):
        if _now() - float(row["created_epoch"]) > MAX_DEFER_S:
            _dead(row, channel, "parent-card-unconfirmed")
        else:
            _update(
                row["dedupe_key"], status="pending", claimed_at=None, not_before=_now() + DEFER_S
            )
        return False
    if parent["status"] == "dead":
        _dead(row, channel, "parent-card-failed")
        return False
    if not parent["ts"] or parent["channel_id"] != channel:
        _dead(row, channel, "parent-card-not-threadable")
        return False
    row["_thread_ts"] = parent["ts"]  # type: ignore[index]
    return True


def _process(row: dict[str, Any]) -> None:
    approval_id = int(row["approval_id"])
    event = row["event"]
    channel = configured_channel()
    if channel is None:
        _dead(row, None, "cards-disabled", outcome="refused")
        return
    try:
        slack_config = SlackConfig.from_env()
    except SlackGatewayError:
        _dead(row, channel, "slack-config-invalid", outcome="refused")
        return
    problem = config_problem(
        channel,
        enabled=slack_config.enabled,
        bot_token=slack_config.bot_token,
        channel_ids=slack_config.channel_ids,
    )
    if problem is not None:
        logger.warning("approval slack card refused: %s", problem)
        _dead(row, channel, _refusal_code(problem), outcome="refused")
        return

    blocks: list[dict[str, Any]] | None = None
    thread_ts: str | None = None
    if event == "requested":
        approval = _approval_row(approval_id, row["org_id"])
        if approval is None:
            _dead(row, channel, "approval-not-found")
            return
        text, blocks = build_card(approval)
    else:
        if not _defer_decision(row, channel):
            return
        thread_ts = row["_thread_ts"]
        resolver = _escape(_bounded(row["resolved_by"]))
        text = f"Approval #{approval_id} {_bounded(row['decision_status'], 16)} by {resolver}"

    attempts = int(row["attempts"]) + 1
    row["attempts"] = attempts
    outcome, error, ts, retry_after = _attempt_send(
        key=row["dedupe_key"],
        channel=channel,
        text=text,
        blocks=blocks,
        thread_ts=thread_ts,
        slack_config=slack_config,
    )
    if outcome in ("delivered", "duplicate"):
        _update(
            row["dedupe_key"],
            status="done",
            attempts=attempts,
            channel_id=channel,
            ts=ts,
            claimed_at=None,
        )
        _record(approval_id, event, channel, attempts, outcome, ts=ts)
        return
    if outcome == "retry":
        delay = BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]
        if retry_after is not None:
            if retry_after > MAX_RETRY_AFTER_S:
                _update(row["dedupe_key"], attempts=attempts)
                _dead(row, channel, "retry-after-exceeds-cap")
                return
            delay = max(delay, retry_after)
        if attempts < MAX_ATTEMPTS:
            _update(
                row["dedupe_key"],
                status="pending",
                attempts=attempts,
                claimed_at=None,
                not_before=_now() + delay,
            )
            _record(approval_id, event, channel, attempts, "retry", error=error)
            return
    _update(row["dedupe_key"], attempts=attempts)
    _dead(row, channel, error or "failed")


def _drain_once() -> None:
    _reclaim_stale()
    while True:
        row = _claim_next()
        if row is None:
            wait = _next_due_in()
            if wait is None:
                return
            _sleep(wait)
            continue
        try:
            _process(row)
        except Exception as exc:  # noqa: BLE001 - one bad delivery must not stop the rest
            error = f"worker-{type(exc).__name__.lower()}"[:64]
            try:
                _dead(row, configured_channel(), error)
            except Exception as inner:  # noqa: BLE001
                logger.warning(
                    "approval slack dead letter not recorded error=%s", type(inner).__name__
                )


def _has_pending() -> bool:
    try:
        return _next_due_in() is not None
    except Exception:  # noqa: BLE001
        return False


def _drain() -> None:
    global _worker_running
    while True:
        try:
            _drain_once()
        except Exception as exc:  # noqa: BLE001 - keep the flag consistent
            logger.warning("approval slack worker failed error=%s", type(exc).__name__)
        with _lock:
            _worker_running = False
        # An intent written between the last empty claim and the flag reset
        # would otherwise wait for the next notification.
        if not _has_pending():
            return
        with _lock:
            if _worker_running:
                return
            _worker_running = True


def _wake() -> None:
    global _worker_running
    with _lock:
        _wakeup.set()
        if _worker_running:
            return
        _worker_running = True
    try:
        _spawn(_drain)
    except BaseException:
        with _lock:
            _worker_running = False
        raise


def _emit(
    approval_id: int, event: str, decision_status: str | None, resolved_by: str | None
) -> None:
    """Persist one intent and wake the worker. Never raises; never calls Slack."""
    if os.environ.get("UA_SYNTHETIC_WORKFLOW") == "1":
        return
    try:
        if configured_channel() is None:
            return
        if _persist_intent(approval_id, event, decision_status, resolved_by):
            _wake()
    except Exception as exc:  # noqa: BLE001 - the approval must not notice
        logger.warning("approval slack intent not persisted error=%s", type(exc).__name__)
        _record(
            approval_id,
            event,
            configured_channel(),
            0,
            "dropped",
            dead_letter=True,
            error="intent-not-persisted",
        )


def notify_requested(approval_id: int, tool: str, requested_by: str | None) -> None:
    """A pending approval row was committed: queue its card.

    ``tool`` and ``requested_by`` match the webhook's signature; the card reads
    the committed row on the worker so it shows exactly what was stored.
    """
    del tool, requested_by
    _emit(int(approval_id), "requested", None, None)


def notify_decided(approval_id: int, status: str, resolved_by: str | None) -> None:
    """A human approved, denied or revoked: queue the threaded follow-up."""
    _emit(int(approval_id), "decided", _bounded(status, 16), resolved_by)


def startup_drain() -> None:
    """Resume intents a previous hub process left unfinished (post-init hook)."""
    try:
        _reclaim_stale()
        if _has_pending():
            _wake()
    except Exception as exc:  # noqa: BLE001 - start-up must not fail on this
        logger.warning("approval slack startup drain failed error=%s", type(exc).__name__)
