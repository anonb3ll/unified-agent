"""Task-space chat ingestion for Discord and Telegram (hub task #770).

The worker is an ingestion boundary first, mirroring
:mod:`unified_agent.zulip_worker`:

- fetch bounded messages through :mod:`unified_agent.ts_worker.transport`,
- classify them against the shared ``[target: <label>]`` convention,
- write each message durably to the event log (``ts.inbound``) with a
  surface-scoped dedupe key, and
- advance a per-surface/channel cursor **only after** the durable write
  succeeds (crash-safe replay through the event log's dedupe key).

Only explicitly-targeted messages from authorized senders get a reply, and the
reply is attributed: it is recorded as an ``agent.message`` event through
:mod:`unified_agent.messaging.send` (from the worker's bot identity) in
addition to being posted back to the originating chat. Explicit slash forms
for five bounded coordination verbs dispatch through the same policy-wrapped
tool registry as Slack; all other chat remains conversation-only.

Design notes from the blocked lane task #690, applied here:
- chat targets are Discord + Telegram (Slack dropped 09-04);
- auth is locked to the operator: only sender IDs in
  ``UA_TS_DISCORD_ALLOWED_USER_IDS`` / ``UA_TS_TELEGRAM_ALLOWED_USER_IDS``
  are ever considered actionable;
- tokens arrive via env on the hub host (tasks #727/#811); without a token
  the surface is disabled and the poll loop skips it.

Deviation from #690's original #763-lineage notes: that lane described a
Hermes-side bot on the GCP host; the current architecture runs the worker as
part of unified-agent (the zulip-worker pattern, a supervised systemd service
on the hub host). Recorded per the spec's boundaries.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import object_model, post_init, storage
from ..messaging import schema as messaging_schema
from ..messaging import send as messaging_send
from . import cursors as ts_cursors
from . import transport as ts_transport

log = logging.getLogger(__name__)

# The identity this worker speaks with. Attribution requirement: outbound
# replies are recorded from this label, and send_message refuses unknown
# labels on a closed roster, so keep the default inside the fleet catalog.
# Chat-surface intake is the executive office's lane (#690 placed this bot with
# the GCP Agent5 surface), so the worker speaks as that office's lead identity
# (task #1879).
WORKER_AGENT_LABEL = "gcp_agent5"

# Event-log channel for durably persisted chat messages.
INBOUND_CHANNEL = "ts.inbound"

SURFACES = ("discord", "telegram")

# Shared with the zulip worker's `[target: ...]` convention (zulip_worker.py).
_TARGET_RE_TIP = "Messages may start with [target: <agent-label>]"

DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_FETCH_LIMIT = 10
MAX_FETCH_LIMIT = 50

DISCORD = "discord"
TELEGRAM = "telegram"

# Telegram's getUpdates offset is global across chats, so the worker keeps one
# cursor under this reserved channel key instead of per-chat rows.
TELEGRAM_OFFSET_CHANNEL = "*"

# These are the only chat mutations accepted by the worker. Slash syntax is
# deliberate: natural-language chat remains inert even when it mentions a verb.
VERBS = frozenset({"task", "assign", "progress", "blocker", "review"})


class VerbError(ValueError):
    """A malformed or unsupported bounded chat verb."""

    def __init__(self, message: str, *, verb: str | None = None) -> None:
        super().__init__(message)
        self.verb = verb


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(
    name: str, name_unused: str = "", default: int = 10, *, maximum: int | None = None
) -> int:
    """Positive int env value; bad values fall back, values above ``maximum`` clamp."""
    del name_unused
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    if value <= 0:
        return default
    if maximum is not None and value > maximum:
        log.warning("%s=%d exceeds the maximum %d; clamping", name, value, maximum)
        return maximum
    return value


def surface_enabled(surface: str) -> bool:
    """True when the surface has a token configured (env-gated, no default)."""
    if surface == DISCORD:
        return ts_transport.discord_token() is not None
    if surface == TELEGRAM:
        return ts_transport.telegram_token() is not None
    return False


def enabled_surfaces() -> tuple[str, ...]:
    return tuple(s for s in SURFACES if surface_enabled(s))


def classify_target(
    content: str,
    *,
    allowed_user_ids: tuple[str, ...],
    sender_id: str | None,
) -> dict[str, Any]:
    """Classify one chat message without interpreting its body as instructions.

    Returns ``{target, target_reason, actionable_for, authorized}``:

    - ``missing-target``: no leading ``[target: ...]`` — informational only.
    - ``invalid-target``: target is not a well-formed/fleet-known label.
    - ``unauthorized-sender``: valid target but sender is not on the allowlist.
    - ``explicit-target``: valid target AND authorized sender → actionable.

    The body remains inert text; a valid marker is metadata for a
    human-reviewed action record, never authority to execute work.
    """
    text = (content or "").strip()
    match = _TARGET_LINE_RE.match(text)
    if match is None:
        return {
            "target": None,
            "target_reason": "missing-target",
            "actionable_for": [],
            "authorized": False,
        }
    target = match.group("target").strip()
    from .. import config

    if (
        not config.agent_label_is_well_formed(target)
        or config.canonical_agent_label(target) is None
    ):
        return {
            "target": target,
            "target_reason": "invalid-target",
            "actionable_for": [],
            "authorized": False,
        }
    if sender_id is None or sender_id not in set(allowed_user_ids or ()):
        return {
            "target": target,
            "target_reason": "unauthorized-sender",
            "actionable_for": [],
            "authorized": False,
        }
    return {
        "target": target,
        "target_reason": "explicit-target",
        "actionable_for": [target],
        "authorized": True,
    }


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _is_worker_label(label: str | None) -> bool:
    """True when ``label`` names this worker's own identity (any casing)."""
    from .. import config

    text = (label or "").strip()
    return text.lower() == WORKER_AGENT_LABEL.lower() or (
        config.canonical_agent_label(text) == WORKER_AGENT_LABEL
    )


def parse_verb(content: str) -> dict[str, Any] | None:
    """Return an explicit slash verb after the target marker, else ``None``."""
    text = _TARGET_LINE_RE.sub("", content or "", count=1).strip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=2)
    verb = parts[0].lstrip("/").lower()
    if verb not in VERBS:
        raise VerbError(f"unsupported command; use one of {sorted(VERBS)}", verb=verb)
    args = parts[1:]
    if verb == "task" and not args:
        raise VerbError("/task requires text", verb=verb)
    if verb == "assign" and len(args) < 2:
        raise VerbError("/assign requires an agent and task text", verb=verb)
    if verb == "assign" and _is_worker_label(args[0]):
        # delegate() refuses from_agent == to_agent; say so up front (task #2290).
        raise VerbError(
            f"/assign cannot name the chat worker itself ({WORKER_AGENT_LABEL}); "
            "name the agent that should do the work",
            verb=verb,
        )
    if verb in {"progress", "blocker", "review"} and len(args) < 2:
        raise VerbError(f"/{verb} requires task id and update", verb=verb)
    return {"verb": verb, "args": args}


def _task_spec(text: str, message_id: str) -> str:
    return (
        "### Objective\n"
        + text[:_MAX_CONTENT_CHARS]
        + "\n### Scope\n"
        + "Handle this bounded chat /assign request through the existing delegation primitive."
        + "\n### Definition of Done\n"
        + "- The delegation call returns a durable task assigned to the requested agent.\n"
        + "- A malformed or unauthorized chat request is rejected without mutation."
        + "\n### Boundaries\n"
        + "Do not execute commands, invoke an LLM, approve work, or mutate unrelated task state."
        + f"\n### Proof\nts.inbound message {message_id}"
    )


def dispatch_verb(
    request: dict[str, Any], *, actor: str | None, message_id: str, channel_id: str
) -> dict[str, Any]:
    """Route a parsed verb through the same policy-wrapped tool registry as Slack."""
    from .. import tool_surface

    REGISTERED_TOOLS = tool_surface.registry()
    verb = request["verb"]
    args = request["args"]
    marker = f"[ts-event: {message_id}]"
    if verb == "task":
        return REGISTERED_TOOLS["task_add"](
            title=" ".join(args)[:160],
            notes=f"[ts-user: {actor or ''}] [ts-channel: {channel_id}] {marker}",
            priority="med",
            project="unified-agent",
            idempotency_key=f"ts:{message_id}:task",
        )
    if verb == "assign":
        return REGISTERED_TOOLS["delegate"](
            task_spec=_task_spec(" ".join(args[1:]), message_id),
            from_agent=WORKER_AGENT_LABEL,
            to_agent=args[0],
            callback="none",
            priority="med",
            project="unified-agent",
        )
    if verb in {"progress", "blocker", "review"}:
        try:
            task_id = int(args[0].removeprefix("task-"))
        except ValueError as exc:
            raise VerbError("task id must be numeric, such as task-42", verb=verb) from exc
        status = {"progress": "doing", "blocker": "blocked", "review": "waiting_review"}[verb]
        return REGISTERED_TOOLS["task_update"](
            id=task_id,
            status=status,
            append_note=f"[ts-user: {actor or ''}] {marker} {args[1][:_MAX_CONTENT_CHARS]}",
            idempotency_key=f"ts:{message_id}:task-update",
        )
    raise VerbError("unreachable bounded chat verb", verb=verb)


class TaskSpaceWorker:
    """Poll Discord/Telegram, persist durably, reply to authorized targets."""

    def __init__(
        self,
        *,
        surfaces: tuple[str, ...] = SURFACES,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        fetch_limit: int = DEFAULT_FETCH_LIMIT,
        discord: ts_transport.DiscordGateway | None = None,
        telegram: ts_transport.TelegramGateway | None = None,
        reply_sender: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        unknown = set(surfaces) - set(SURFACES)
        if unknown:
            raise ValueError(f"unsupported surfaces: {sorted(unknown)}")
        if not surfaces:
            raise ValueError("at least one surface is required")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        if not 1 <= fetch_limit <= MAX_FETCH_LIMIT:
            raise ValueError("fetch_limit must be 1..50")
        self.surfaces = tuple(surfaces)
        self.poll_interval_s = poll_interval_s
        self.fetch_limit = fetch_limit
        self.discord = discord or ts_transport.DiscordGateway()
        self.telegram = telegram or ts_transport.TelegramGateway()
        # Reply recording goes through messaging.send so the outbound reply is
        # an attributed, inspectable agent.message event — same abstraction the
        # hub's inter-agent messaging uses (task #483 vocabulary).
        self._reply_sender = reply_sender or messaging_send.send_message
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # durable persist + cursor

    def _persist_message(
        self,
        payload: dict[str, Any],
        surface: str,
        external_id: str,
        *,
        cursor_channel: str,
    ) -> bool:
        """Write one inbound message durably; True when newly persisted.

        Dedupe key is surface-scoped: ``ts:<surface>:<external message id>``,
        matching the zulip worker's ``zulip:<id>`` convention. The cursor
        channel is the surface's resume key — the Discord channel id, or the
        Telegram global offset key ``*``.
        """
        dedupe_key = f"ts:{surface}:{external_id}"
        _, inserted = object_model.by_kind("evidence_event").writer("append")(
            INBOUND_CHANNEL,
            payload=payload,
            originator=payload.get("sender_id") or None,
            notify=False,
            dedupe_key=dedupe_key,
        )
        cursor = ts_cursors.get_ts_cursor(surface, cursor_channel)
        current = int(cursor["last_message_id"]) if cursor else -1
        try:
            incoming = int(external_id)
            current = max(current, incoming)
        except (TypeError, ValueError):
            pass
        ts_cursors.save_ts_cursor(surface, cursor_channel, current)
        return inserted

    def _event_payload(
        self,
        *,
        surface: str,
        message_id: str,
        channel_id: str,
        sender_id: str | None,
        sender_name: str,
        content: str,
        decision: dict[str, Any],
        content_truncated: bool,
    ) -> dict[str, Any]:
        return {
            "source": surface,
            "message_id": message_id,
            "channel_id": channel_id,
            "sender_id": sender_id,
            "sender_name": (sender_name or "")[:200],
            "content": content,
            "content_truncated": content_truncated,
            "target": decision.get("target"),
            "target_reason": decision.get("target_reason"),
            "actionable_for": decision.get("actionable_for") or [],
            "authorized": bool(decision.get("authorized")),
        }

    # ------------------------------------------------------------------
    # attributed reply path

    def _reply(
        self,
        *,
        surface: str,
        channel_id: str,
        content: str,
        target: str,
        message_id: str,
        body: str | None = None,
    ) -> dict[str, Any] | None:
        """Post an attributed reply for an authorized targeted message.

        Two-step: record the outbound as an attributed ``agent.message`` event
        first (the durable, inspectable act), then post to the chat API.
        A chat-post failure after the record is logged, not fatal — the record
        is the source of truth; delivery is best-effort (mirrors the zulip
        worker's chat posture).
        """
        if body is None:
            body = (
                f"[{WORKER_AGENT_LABEL}] ack: received your request targeted at "
                f"'{target}' via {surface}. {_TARGET_RE_TIP}"
            )
        try:
            if _is_worker_label(target):
                # send_message refuses a self-message, which used to drop the
                # chat ack too. Record the ack as an evidence event instead and
                # still post to chat (task #2290).
                record = object_model.by_kind("evidence_event").writer("append")(
                    INBOUND_CHANNEL,
                    payload={
                        "source": surface,
                        "message_id": message_id,
                        "kind": "self-ack",
                        "agent": WORKER_AGENT_LABEL,
                        "body": body,
                    },
                    originator=WORKER_AGENT_LABEL,
                    notify=False,
                    dedupe_key=f"ts:{surface}:{message_id}:self-ack",
                )
            else:
                record = self._reply_sender(
                    act=messaging_schema.MessageAct.INFORM.value,
                    to_agent=target,
                    body=body,
                    from_agent=WORKER_AGENT_LABEL,
                    reply_to=f"ts:{surface}:{message_id}",
                )
        except Exception as exc:
            # Unknown-label / byte-limit refusals must not break ingestion.
            log.warning(
                "ts reply record failed surface=%s message_id=%s: %s",
                surface,
                message_id,
                type(exc).__name__,
            )
            return None
        try:
            if surface == DISCORD:
                self.discord.send_message(channel_id, body)
            elif surface == TELEGRAM:
                self.telegram.send_message(channel_id, body)
        except Exception as exc:
            log.warning(
                "ts reply post failed surface=%s message_id=%s: %s",
                surface,
                message_id,
                type(exc).__name__,
            )
        return record

    def _handle_verb(
        self,
        *,
        payload: dict[str, Any],
        decision: dict[str, Any],
        surface: str,
        channel_id: str,
        sender_id: str | None,
        message_id: str,
        cursor_channel: str,
    ) -> bool:
        """Persist and execute one bounded verb; ``False`` leaves chat unchanged."""
        try:
            request = parse_verb(str(payload["content"]))
        except VerbError as exc:
            request = {"verb": exc.verb or "unknown", "args": []}
            refusal = str(exc)
        else:
            refusal = None
        if request is None:
            return False

        payload.update(verb=request["verb"], actor=sender_id)
        target = (
            str(decision.get("target"))
            if decision.get("target_reason") != "invalid-target" and decision.get("target")
            else WORKER_AGENT_LABEL
        )
        if refusal is None and not decision.get("authorized"):
            refusal = str(decision.get("target_reason") or "unauthorized-sender")
        if refusal is not None:
            payload.update(outcome="refused", reason=refusal)
            self._persist_message(payload, surface, message_id, cursor_channel=cursor_channel)
            self._reply(
                surface=surface,
                channel_id=channel_id,
                content=str(payload["content"]),
                target=target,
                message_id=message_id,
                body=f"[{WORKER_AGENT_LABEL}] command refused: {refusal}",
            )
            return True

        payload["outcome"] = "pending"
        if not self._persist_message(payload, surface, message_id, cursor_channel=cursor_channel):
            return True
        try:
            dispatch_verb(request, actor=sender_id, message_id=message_id, channel_id=channel_id)
        except (
            KeyError,
            LookupError,
            OSError,
            RuntimeError,
            TypeError,
            ToolError,
            VerbError,
            ValueError,
        ) as exc:
            outcome = "failed"
            reason = type(exc).__name__
        else:
            outcome = "succeeded"
            reason = None
        audit = {
            "source": surface,
            "message_id": message_id,
            "channel_id": channel_id,
            "verb": request["verb"],
            "actor": sender_id,
            "outcome": outcome,
        }
        if reason is not None:
            audit["reason"] = reason
        object_model.by_kind("evidence_event").writer("append")(
            INBOUND_CHANNEL,
            payload=audit,
            originator=sender_id,
            notify=False,
            dedupe_key=f"ts:{surface}:{message_id}:verb-outcome",
        )
        if outcome == "failed":
            self._reply(
                surface=surface,
                channel_id=channel_id,
                content=str(payload["content"]),
                target=target,
                message_id=message_id,
                body=f"[{WORKER_AGENT_LABEL}] command refused: {reason}",
            )
            return True
        self._reply(
            surface=surface,
            channel_id=channel_id,
            content=str(payload["content"]),
            target=target,
            message_id=message_id,
        )
        return True

    # ------------------------------------------------------------------
    # surface polls

    def poll_discord(self) -> dict[str, Any]:
        """Poll all configured Discord channels once (bounded, cursor-gated)."""
        token = ts_transport.discord_token()
        if token is None:
            return {"surface": DISCORD, "status": "disabled", "messages_processed": 0}
        channels = [
            c.strip()
            for c in (os.environ.get("UA_TS_DISCORD_CHANNELS") or "").split(":")
            if c.strip()
        ]
        if not channels:
            return {
                "surface": DISCORD,
                "status": "misconfigured",
                "messages_processed": 0,
                "error": "UA_TS_DISCORD_CHANNELS is not configured",
            }
        processed = 0
        errors: list[str] = []
        for channel_id in channels:
            try:
                cursor = ts_cursors.get_ts_cursor(DISCORD, channel_id)
                after = int(cursor["last_message_id"]) if cursor else None
                messages = self.discord.fetch_messages(
                    channel_id, after=after, limit=self.fetch_limit
                )
            except Exception as exc:  # noqa: BLE001 — one bad channel must not stop the rest
                errors.append(f"{channel_id}: {type(exc).__name__}")
                continue
            for message in sorted(messages, key=lambda m: _snowflake(m.get("id"))):
                if _snowflake(message.get("id")) <= (after or 0):
                    continue
                if message.get("author", {}).get("bot"):
                    continue  # never ingest our own or other bots' messages
                content = str(message.get("content", ""))
                truncated = len(content) > _MAX_CONTENT_CHARS
                content = content[:_MAX_CONTENT_CHARS]
                allowed = ts_transport.discord_allowed_user_ids()
                sender_id = str(message.get("author", {}).get("id") or "") or None
                decision = classify_target(
                    content,
                    allowed_user_ids=allowed,
                    sender_id=sender_id,
                )
                payload = self._event_payload(
                    surface=DISCORD,
                    message_id=str(message.get("id")),
                    channel_id=channel_id,
                    sender_id=sender_id,
                    sender_name=str(message.get("author", {}).get("username") or ""),
                    content=content,
                    decision=decision,
                    content_truncated=truncated,
                )
                handled_verb = self._handle_verb(
                    payload=payload,
                    decision=decision,
                    surface=DISCORD,
                    channel_id=channel_id,
                    sender_id=sender_id,
                    message_id=str(message.get("id")),
                    cursor_channel=channel_id,
                )
                if not handled_verb:
                    self._persist_message(
                        payload, DISCORD, str(message.get("id")), cursor_channel=channel_id
                    )
                processed += 1
                if not handled_verb and decision.get("authorized") and decision.get("target"):
                    self._reply(
                        surface=DISCORD,
                        channel_id=channel_id,
                        content=content,
                        target=str(decision["target"]),
                        message_id=str(message.get("id")),
                    )
        if errors:
            return {
                "surface": DISCORD,
                "status": "error",
                "messages_processed": processed,
                "error": "; ".join(errors)[:500],
            }
        return {"surface": DISCORD, "status": "healthy", "messages_processed": processed}

    def poll_telegram(self) -> dict[str, Any]:
        """Long-poll Telegram updates once (bounded, offset-gated)."""
        token = ts_transport.telegram_token()
        if token is None:
            return {"surface": TELEGRAM, "status": "disabled", "messages_processed": 0}
        allowed = ts_transport.telegram_allowed_user_ids()
        try:
            cursor = ts_cursors.get_ts_cursor(TELEGRAM, TELEGRAM_OFFSET_CHANNEL)
            offset = int(cursor["last_message_id"]) + 1 if cursor else None
            updates = self.telegram.get_updates(
                offset=offset, timeout_s=min(int(self.poll_interval_s), 25)
            )
        except Exception as exc:  # noqa: BLE001 — transport errors are health, not crashes
            return {
                "surface": TELEGRAM,
                "status": "error",
                "messages_processed": 0,
                "error": f"{type(exc).__name__}"[:200],
            }
        processed = 0
        highest_update_id = (offset or 1) - 1
        for update in sorted(updates, key=lambda u: int(u.get("update_id", 0))):
            update_id = int(update.get("update_id", 0))
            highest_update_id = max(highest_update_id, update_id)
            message = update.get("message") or update.get("edited_message") or {}
            if not isinstance(message, dict) or "text" not in message:
                continue
            content = str(message.get("text", ""))
            content = content[:_MAX_CONTENT_CHARS]
            sender = message.get("from") or {}
            sender_id = str(sender.get("id") or "") or None
            chat_id = str(message.get("chat", {}).get("id") or "")
            decision = classify_target(
                content,
                allowed_user_ids=allowed,
                sender_id=sender_id,
            )
            payload = self._event_payload(
                surface=TELEGRAM,
                message_id=str(update_id),
                channel_id=chat_id,
                sender_id=sender_id,
                sender_name=str(sender.get("username") or sender.get("first_name") or ""),
                content=content,
                decision=decision,
                content_truncated=_truncate(content, _MAX_CONTENT_CHARS)[1],
            )
            handled_verb = self._handle_verb(
                payload=payload,
                decision=decision,
                surface=TELEGRAM,
                channel_id=chat_id,
                sender_id=sender_id,
                message_id=str(update_id),
                cursor_channel=TELEGRAM_OFFSET_CHANNEL,
            )
            if not handled_verb:
                self._persist_message(
                    payload, TELEGRAM, str(update_id), cursor_channel=TELEGRAM_OFFSET_CHANNEL
                )
            processed += 1
            if not handled_verb and decision.get("authorized") and decision.get("target"):
                self._reply(
                    surface=TELEGRAM,
                    channel_id=chat_id,
                    content=content,
                    target=str(decision["target"]),
                    message_id=str(update_id),
                )
        return {"surface": TELEGRAM, "status": "healthy", "messages_processed": processed}

    def poll_once(self) -> dict[str, Any]:
        """Poll every configured surface once; aggregated health result."""
        results = []
        for surface in self.surfaces:
            if surface == DISCORD:
                results.append(self.poll_discord())
            elif surface == TELEGRAM:
                results.append(self.poll_telegram())
        processed = sum(int(r.get("messages_processed") or 0) for r in results)
        statuses = {r.get("status") for r in results}
        if "error" in statuses:
            overall = "error"
        elif "misconfigured" in statuses:
            overall = "misconfigured"
        elif statuses <= {"disabled"}:
            overall = "disabled"
        elif "healthy" in statuses:
            overall = "healthy"
        else:
            overall = "disabled"
        return {"status": overall, "messages_processed": processed, "results": results}

    # ------------------------------------------------------------------
    # supervised loop

    def stop(self) -> None:
        """Request the supervised loop to stop after its current poll."""
        self._stop.set()

    def run_forever(self) -> None:
        """Poll until stopped; systemd supervises restarts (zulip pattern)."""
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                log.warning("ts poll_once failed: %s", type(exc).__name__)
            self._stop.wait(self.poll_interval_s)


def _snowflake(value: Any) -> int:
    """Discord message id (snowflake string) as int; 0 when unusable."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


# Shared inbound content cap (zulip's MAX_CONTENT_CHARS), applied before the
# event payload is written so the durable record stays bounded.
_MAX_CONTENT_CHARS = 4000

# Shared with the zulip worker's `[target: ...]` convention (zulip_worker.py).
_TARGET_LINE_RE = re.compile(r"^\s*\[(?:target|to):\s*(?P<target>[^\]]+)\]\s*", re.IGNORECASE)


def main() -> None:
    """Run the worker as a foreground process for a user service."""
    # Standalone entry must migrate/create tables even when the hub is not up.
    post_init.register_default_hooks()
    storage.init_db()
    worker = TaskSpaceWorker(
        poll_interval_s=_env_float("UA_TS_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S),
        fetch_limit=_env_int(
            "UA_TS_FETCH_LIMIT", default=DEFAULT_FETCH_LIMIT, maximum=MAX_FETCH_LIMIT
        ),
    )

    def stop(_signum: int, _frame: Any) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever()
