"""Secure Slack ingress for the canonical Unified-Agent hub.

This is an HTTP adapter seam, not a second control plane. A cloud Slack
gateway may forward the signed request to ``handle_http_request`` on the
canonical Mac hub, or call it in the hub process. The adapter never stores
Slack state outside the hub SQLite database, never accepts credentials from a
Slack message, and never resolves a human approval itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

from . import (
    agent_access,
    agent_lifecycle,
    completion_return,
    object_model,
    platform_http,
    storage,
)
from .config import agent_identity_var
from .slack_config import (  # noqa: F401 - shared Slack config; re-exported here
    DEFAULT_COORDINATOR_AGENT,
    MAX_TEXT_CHARS,
    SLACK_CHANNELS,
    FrontierPolicy,
    SlackConfig,
    SlackGatewayError,
    SlackGatewayOverloadError,
    SlackStatusLookupConfig,
    _channel_mapping,
    _csv,
    _known_status_identities,
    _status_identity_mapping,
    _truthy,
)

log = logging.getLogger(__name__)

COMMANDS = frozenset(
    {"contact", "task", "assign", "progress", "blocker", "review", "plan", "approve", "status"}
)
MAX_BODY_BYTES = 64 * 1024
SIGNATURE_MAX_AGE_S = 300
_MENTION = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>\s*", re.IGNORECASE)
_MENTION_TOKEN = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>", re.IGNORECASE)
_TASK_STATUS_ARG = re.compile(r"^task-([1-9][0-9]*)$")


def verify_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
) -> bool:
    """Verify Slack v0 authentication and its five-minute replay window."""
    if (
        not signing_secret
        or not timestamp
        or not timestamp.isascii()
        or not timestamp.isdigit()
        or not signature
        or len(body) > MAX_BODY_BYTES
    ):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > SIGNATURE_MAX_AGE_S:
        return False
    base = f"v0:{timestamp}:".encode("ascii", "strict") + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def authorize(
    config: SlackConfig,
    *,
    user_id: str,
    channel_id: str,
    channel_name: str | None = None,
) -> str:
    """Authorize exact Slack user and channel IDs, returning the protocol channel."""
    config.validate()
    if user_id not in config.users:
        raise SlackGatewayError("Slack user is not allowlisted")
    if channel_id not in config.channel_ids:
        raise SlackGatewayError("Slack channel ID is not allowlisted")
    mapped_name = config.channel_names_by_id.get(channel_id)
    if mapped_name:
        logical_name = mapped_name
    else:
        # An ID-only allowlist is authoritative. Slack's display name is
        # metadata and may be a human channel name outside our protocol names.
        return "unknown"
    if logical_name and logical_name not in SLACK_CHANNELS:
        raise SlackGatewayError("Slack channel is outside the coordination protocol")
    return logical_name or "unknown"


def _strip_leading_mentions(text: str) -> str:
    clean = text.strip()
    while True:
        match = _MENTION.match(clean)
        if not match:
            break
        clean = clean[match.end() :].strip()
    return clean


def parse_request(text: str) -> dict[str, Any]:
    """Parse mentions and slash-like command text into an inert typed request."""
    clean = _strip_leading_mentions(text)
    if not clean:
        raise SlackGatewayError("empty Slack request")
    parts = clean.split(maxsplit=2)
    command = parts[0].lstrip("/").lower()
    if command not in COMMANDS:
        raise SlackGatewayError(f"unsupported Slack command; use one of {sorted(COMMANDS)}")
    args = parts[1:]
    if command in {"task", "plan"} and not args:
        raise SlackGatewayError(f"/{command} requires text")
    if command == "assign" and len(args) < 2:
        raise SlackGatewayError("/assign requires an agent and task text")
    if command in {"progress", "blocker", "review"} and len(args) < 2:
        raise SlackGatewayError(f"/{command} requires task id and update")
    if command == "approve" and not args:
        raise SlackGatewayError("/approve requires an approval id")
    if command == "status" and not args:
        raise SlackGatewayError("/status requires a task id, such as status task-42")
    return {"command": command, "args": args}


def parse_event_request(text: str, *, event_type: str = "") -> dict[str, Any]:
    """Parse an Events API message into a coordination command or live-chat request.

    Slash-form commands stay on the coordination path. Everything else becomes
    inert ``chat`` request so continuous Slack conversation can ride the
    same signed ingress without inventing a second authority.
    """
    stripped = _strip_leading_mentions(text)
    if not stripped:
        raise SlackGatewayError("empty Slack request")
    first_raw = stripped.split(maxsplit=1)[0]
    # Events API chat is inert unless the author used an explicit slash command.
    # Natural language like "task status is confusing" must not mutate durable state.
    if first_raw.startswith("/") and first_raw.lstrip("/").lower() in COMMANDS:
        return parse_request(text)
    # Narrow exception (task #1104 follow-up, Devin review): Slack marks a
    # message directed at this app as ``app_mention``. A leading mention alone
    # is not enough: it may name another user, so only that event type can
    # authorize bare ``status`` routing. Slash commands above remain unchanged.
    leading_mention = _MENTION.match(text.strip())
    mention_count = len(_MENTION_TOKEN.findall(text))
    if (
        event_type == "app_mention"
        and leading_mention
        and mention_count == 1
        and first_raw.lower() == "status"
    ):
        return parse_request(text)
    # Keep native Slack mentions (<@U123|Desktop_Agent4>) for chat routing; slash
    # commands still strip leading bot mentions via parse_request().
    return {"command": "chat", "args": [], "text": text.strip()[:MAX_TEXT_CHARS]}


def parse_slash_payload(fields: Mapping[str, str]) -> dict[str, Any]:
    """Translate Slack's form-encoded slash command payload."""
    command = fields.get("command", "").lstrip("/").lower()
    text = fields.get("text", "").strip()
    if command not in COMMANDS:
        raise SlackGatewayError("unsupported Slack slash command")
    return parse_request(f"/{command} {text}".strip())


def _task_spec(text: str, event_id: str) -> str:
    return (
        "### Objective\n"
        + text[:MAX_TEXT_CHARS]
        + "\n### Scope\n"
        + "Handle this Slack /assign request through the existing delegation primitive."
        + "\n### Definition of Done\n"
        + "- The delegation call returns a durable task assigned to the requested agent, and the Slack event is traceable in task evidence.\n"
        + "- A malformed or unauthorized Slack request is rejected and never creates a task."
        + "\n### TDD Constraints\n"
        + "Add or update focused tests for the observable delegation behaviour before changing unrelated code."
        + "\n### Boundaries\n"
        + "Do not execute commands, invoke an LLM, approve work, or mutate unrelated task state from Slack text."
        + f"\n### Proof\nSlack event {event_id} and Unified-Agent task evidence"
    )


def _record_status_denial(*, user_id: str, config: SlackConfig) -> None:
    """Quietly record an expected status-lookup denial (task #1104 follow-up).

    Mirrors ``record_frontier_usage``'s ``frontier.usage`` telemetry: an
    informational evidence event (``notify=False``, no ``.failed`` suffix,
    never routed to per-request human review) rather than
    ``slack.dispatch.failed``. Feature-disabled, unmapped, and revoked are
    all ordinary, by-design outcomes, not "something broke" — a Slack user
    without an entry in the map is the *expected* common case, not an
    incident. A malformed/duplicate config map is different: that is an
    operator mistake, so it is additionally logged (visible in hub logs,
    per Devin review) even though it still does not become a per-request
    review item.
    """
    if config.status_lookup.error:
        log.warning(
            "Slack status identity map is invalid, feature disabled: %s",
            config.status_lookup.error,
        )
    append_evidence_event = object_model.by_kind("evidence_event").writer("append")
    try:
        append_evidence_event(
            "slack.status.denied",
            payload={"slack_user_id": user_id, "config_invalid": bool(config.status_lookup.error)},
            originator=user_id,
            notify=False,
        )
    except (OSError, sqlite3.Error):
        log.exception("could not record Slack status-lookup denial for user %s", user_id)


def _slack_task_status(args: list[str], *, user_id: str, config: SlackConfig) -> dict[str, Any]:
    """Read-only ``status task-<id>`` lookup (task #1104).

    Identity resolution — feature enabled, map valid, Slack user mapped, and
    mapped identity not revoked — happens before anything else, so an
    unmapped/revoked/unknown caller or a disabled/invalid map is denied
    before a single row is read. That denial is *expected* traffic (a Slack
    user with no map entry is the common case), so it is caught here and
    turned into a quiet ``reason: "denied"`` result — recorded via
    ``_record_status_denial`` rather than propagating as ``SlackGatewayError``
    into ``handle_http_request``'s broad ``except``, which would otherwise
    record a ``slack.dispatch.failed`` event and mark it
    ``requires_review: True`` for every single unmapped Slack user, drowning
    real dispatch failures. The reply text stays generic (see
    ``slack_outbound._task_status_ack_text``) so it cannot be used to probe
    which specific case applied.

    Only a request that clears that gate reaches ``platform_http.read_object``
    — the same read path, gate, and ``ResourceNotFoundError``/``ResourceError``
    semantics MCP resources use, so this adapter cannot see anything an MCP
    client with that identity's profile could not.

    The read runs with ``config.agent_identity_var`` bound to the mapped
    identity for the duration of the call only, mirroring how
    ``HubAuthASGIMiddleware`` scopes a verified bearer identity to one HTTP
    request (``server.py``'s ``token_token = config.agent_identity_var.set(...)``
    / ``reset(token_token)`` in its ``finally``) — the same pattern, applied to
    one in-process read instead of one ASGI request.
    """
    try:
        identity = config.status_lookup.resolve_identity(user_id)
        # slack_config sits below the lifecycle layer, so the retired-identity
        # refusal (task #2305) is applied here, next to the env revocation.
        agent_lifecycle.refuse_if_retired(identity, site="slack.status_lookup")
    except (SlackGatewayError, agent_lifecycle.AgentRetiredError):
        _record_status_denial(user_id=user_id, config=config)
        return {"kind": "task_status", "reason": "denied"}
    match = _TASK_STATUS_ARG.match(args[0]) if args else None
    if match is None:
        return {"kind": "task_status", "reason": "usage"}
    task_id = match.group(1)
    token = agent_identity_var.set(identity)
    try:
        try:
            payload = platform_http.read_object("task", task_id)
        except ResourceNotFoundError:
            return {"kind": "task_status", "reason": "not_found", "task_id": task_id}
    finally:
        agent_identity_var.reset(token)
    task = payload["task"]
    return {
        "kind": "task_status",
        "reason": "ok",
        # Projection is deliberately narrow: id/title/status/project/owner
        # only. Never notes, memory, approvals, or evidence payloads, even
        # though the underlying task row carries them — this dict is the only
        # thing slack_outbound._ack_text ever sees for a status reply.
        "task": {
            "id": task.get("id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "project": task.get("project"),
            "owner": task.get("claimed_by"),
        },
    }


def dispatch(
    request: dict[str, Any],
    *,
    user_id: str,
    event_id: str = "local",
    channel_id: str = "unknown",
    coordinator_agent: str = DEFAULT_COORDINATOR_AGENT,
    callbacks: dict[str, Callable[..., dict[str, Any]]] | None = None,
    config: SlackConfig | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Translate one authenticated request through existing hub primitives.

    ``thread_ts`` is the verified event's thread root. A ``/task`` created in
    a thread records it as the task's origin, so the completion is posted
    back there (task #1751).
    """
    from .server import REGISTERED_TOOLS

    active_callbacks: dict[str, Any] = {
        "task_add": REGISTERED_TOOLS["task_add"],
        "task_update": REGISTERED_TOOLS["task_update"],
        "delegate": REGISTERED_TOOLS["delegate"],
        "plan_create": REGISTERED_TOOLS["plan_create"],
        "approval_pending": REGISTERED_TOOLS["approval_pending"],
    }
    if callbacks:
        active_callbacks.update(callbacks)
    command = request["command"]
    args = request.get("args", [])
    marker = f"[slack-event: {event_id}]"
    if command not in {"contact", "approve", "status"}:
        agent_access.enforce(f"slack:{command}", read_only=False, production=True)
    if command == "status":
        # No agent_access.enforce() preflight here (task #1104 follow-up,
        # Devin review): _slack_task_status -> platform_http.read_object
        # already calls agent_access.enforce("platform_object:task",
        # read_only=True), and that operation name is the one
        # agent_access.reject_arbitrary_execution() allowlists under managed
        # secret mode (see platform_http.py's own comment on the same
        # prefix). An extra "slack:status" enforce() here would not be on
        # that allowlist and would block the read even in managed secret
        # mode, which is exactly the read-only diagnostics/status class that
        # mode is meant to keep working.
        return _slack_task_status(args, user_id=user_id, config=config or SlackConfig.from_env())
    if command == "contact":
        return {"kind": "contact", "agent": args[0] if args else coordinator_agent}
    if command == "task":
        # The origin comes from the signed, allowlisted request (user, channel,
        # thread root), never from the message text. A slash command with no
        # thread has nowhere to post back, so it records none.
        root = (thread_ts or "").strip()
        origin = None
        if root and user_id and channel_id and channel_id != "unknown":
            try:
                origin = completion_return.Origin(
                    surface="slack", channel=channel_id, thread=root, requester=user_id
                )
            except ToolError:
                log.warning("Slack /task origin not recordable for event %s", event_id)
        with completion_return.creation_origin(origin):
            return active_callbacks["task_add"](
                title=" ".join(args)[:160],
                notes=f"[slack-user: {user_id}] [slack-channel: {channel_id}] {marker}",
                priority="med",
                project="unified-agent",
                idempotency_key=f"slack:{event_id}:task",
            )
    if command == "assign":
        return active_callbacks["delegate"](
            task_spec=_task_spec(" ".join(args[1:]), event_id),
            from_agent=coordinator_agent,
            to_agent=args[0],
            callback="none",
            priority="med",
            project="unified-agent",
        )
    if command in {"progress", "blocker", "review"}:
        try:
            task_id = int(args[0].removeprefix("task-"))
        except ValueError as exc:
            raise SlackGatewayError("task id must be numeric, such as task-42") from exc
        status = {"progress": "doing", "blocker": "blocked", "review": "waiting_review"}[command]
        return active_callbacks["task_update"](
            id=task_id,
            status=status,
            append_note=f"[slack-user: {user_id}] {marker} {args[1][:MAX_TEXT_CHARS]}",
            idempotency_key=f"slack:{event_id}:task-update",
        )
    if command == "plan":
        return active_callbacks["plan_create"](
            goal=" ".join(args)[:160],
            steps=[
                {"description": "Confirm Slack plan and next owner", "owner": coordinator_agent}
            ],
            scope="unified-agent",
            author=coordinator_agent,
        )
    if command == "approve":
        # The 'approve' handoff is deliberately a stub: Slack never resolves approvals
        # directly; the operator resolves approvals via the authenticated Web UI or approval_resolve.
        approval_id = int(args[0]) if args[0].isdigit() else None
        if approval_id is None:
            raise SlackGatewayError("approval id must be numeric")
        pending = active_callbacks["approval_pending"]()
        known_ids = {row.get("id") for row in pending.get("pending", [])}
        return {
            "kind": "approval_handoff",
            "approval_id": approval_id,
            "pending": approval_id in known_ids,
            "requires_human_resolution": True,
            "next_action": "The operator resolves this in the authenticated Web UI or approval_resolve",
        }
    raise SlackGatewayError("unreachable Slack command")


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), "")


def _form_fields(body: bytes) -> dict[str, str]:
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as exc:
        raise SlackGatewayError("Slack form body is not UTF-8") from exc
    return {key: vals[0] for key, vals in values.items() if vals}


def _request_parts(
    body: bytes, content_type: str
) -> tuple[dict[str, Any], str, str, str, str | None, str | None, str | None]:
    if content_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded":
        fields = _form_fields(body)
        request = parse_slash_payload(fields)
        event_id = "slash:" + hashlib.sha256(body).hexdigest()
        return (
            request,
            fields.get("user_id", ""),
            fields.get("channel_id", ""),
            event_id,
            fields.get("channel_name"),
            fields.get("thread_ts") or None,
            fields.get("message_ts") or None,
        )
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise SlackGatewayError("invalid Slack JSON") from exc
    if not isinstance(envelope, dict):
        raise SlackGatewayError("Slack envelope must be an object")
    if envelope.get("type") == "url_verification":
        return {"command": "contact", "args": []}, "", "", "url-verification", None, None, None
    event = envelope.get("event")
    if not isinstance(event, dict):
        raise SlackGatewayError("Slack event callback is missing event object")
    event_id = str(envelope.get("event_id") or event.get("event_ts") or "")
    if not event_id:
        raise SlackGatewayError("Slack event_id is required")
    # Ignore bot/app echoes so live-chat replies do not recurse.
    if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_changed"}:
        return (
            {"command": "_ignore", "args": []},
            str(event.get("user") or ""),
            str(event.get("channel") or ""),
            event_id,
            event.get("channel_name"),
            str(event.get("thread_ts") or "") or None,
            str(event.get("ts") or event.get("event_ts") or "") or None,
        )
    request = parse_event_request(
        str(event.get("text") or ""), event_type=str(event.get("type") or "")
    )
    return (
        request,
        str(event.get("user") or ""),
        str(event.get("channel") or ""),
        event_id,
        event.get("channel_name"),
        str(event.get("thread_ts") or "") or None,
        str(event.get("ts") or event.get("event_ts") or "") or None,
    )


def handle_http_request(
    body: bytes,
    *,
    headers: Mapping[str, str],
    config: SlackConfig | None = None,
    callbacks: dict[str, Callable[..., dict[str, Any]]] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify, authorize, durably ingest, and dispatch one Slack HTTP request.

    The event is committed before mutation dispatch. A duplicate is returned
    without dispatching, and a dispatch failure is recorded as a separate
    durable event with only error type (never request text or credentials).
    """
    config = config or SlackConfig.from_env()
    if len(body) > MAX_BODY_BYTES:
        raise SlackGatewayError("Slack request body exceeds limit")
    if not verify_signature(
        config.signing_secret,
        _header(headers, "X-Slack-Request-Timestamp"),
        body,
        _header(headers, "X-Slack-Signature"),
        now=now,
    ):
        raise SlackGatewayError("invalid or replayed Slack signature")
    config.validate()
    request, user_id, channel_id, event_id, channel_name, thread_ts, event_ts = _request_parts(
        body, _header(headers, "Content-Type") or "application/json"
    )
    root = (thread_ts or event_ts or "").strip()
    conv = f"slack:{channel_id}:{root}" if root else None

    def _finish(response: dict[str, Any], *, ack: bool = True) -> dict[str, Any]:
        if conv:
            response["conversation_id"] = conv
            response["thread_ts"] = root
        if not ack:
            return response
        from . import slack_outbound

        try:
            if slack_outbound.schedule_reply_to_inbound(
                handle_result=response,
                channel_id=channel_id,
                thread_ts=thread_ts,
                event_ts=event_ts,
                event_id=event_id,
                config=config,
            ):
                response["slack_reply"] = {"scheduled": True}
        except (ImportError, RuntimeError) as exc:
            log.warning(
                "Slack ack scheduling failed after dispatch event_id=%s: %s",
                event_id,
                type(exc).__name__,
            )
        return response

    try:
        envelope = (
            json.loads(body)
            if "json" in (_header(headers, "Content-Type") or "json").lower()
            else {}
        )
    except ValueError:
        envelope = {}
    if isinstance(envelope, dict) and envelope.get("type") == "url_verification":
        return {"challenge": str(envelope.get("challenge", ""))}
    if request.get("command") == "_ignore":
        return {"accepted": True, "ignored": True, "event_id": event_id}
    protocol_channel = authorize(
        config, user_id=user_id, channel_id=channel_id, channel_name=channel_name
    )
    payload = {
        "source": "slack",
        "event_id": event_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "channel": protocol_channel,
        "command": request["command"],
        "thread_ts": thread_ts,
        "event_ts": event_ts,
        "conversation_id": conv,
    }
    if request.get("command") == "chat":
        payload["text_chars"] = len(str(request.get("text") or ""))
    append_evidence_event = object_model.by_kind("evidence_event").writer("append")
    event_ulid, inserted = append_evidence_event(
        "slack.inbound",
        payload=payload,
        originator=user_id,
        notify=False,
        dedupe_key=f"slack:{event_id}",
    )
    if not inserted:
        if request.get("command") == "chat" and storage.slack_chat_schedule_failure_retryable(
            event_id
        ):
            storage.clear_slack_chat_reply(event_id)
        elif request.get("command") == "chat":
            from . import slack_chat

            reply_row = storage.get_slack_chat_reply(event_id)
            terminal = reply_row and reply_row.get("status") in ("sent", "failed", "skipped")
            if not terminal:
                scheduled = slack_chat.schedule_maybe_reply(
                    text=str(request.get("text") or ""),
                    event_id=event_id,
                    channel_id=channel_id,
                    protocol_channel=protocol_channel,
                    thread_ts=thread_ts,
                    event_ts=event_ts,
                    user_id=user_id,
                    config=config,
                )
                return _finish(
                    {
                        "accepted": True,
                        "duplicate": True,
                        "dispatched": False,
                        "kind": "chat",
                        "event_id": event_id,
                        "event_ulid": event_ulid,
                        "channel": protocol_channel,
                        "slack_chat": {"scheduled": scheduled, "rescheduled": True},
                    },
                    ack=False,
                )
            return _finish(
                {
                    "accepted": True,
                    "duplicate": True,
                    "event_id": event_id,
                    "event_ulid": event_ulid,
                },
                ack=False,
            )
        else:
            return _finish(
                {
                    "accepted": True,
                    "duplicate": True,
                    "event_id": event_id,
                    "event_ulid": event_ulid,
                },
                ack=True,
            )
    if request.get("command") == "chat":
        from . import slack_chat

        scheduled = slack_chat.schedule_maybe_reply(
            text=str(request.get("text") or ""),
            event_id=event_id,
            channel_id=channel_id,
            protocol_channel=protocol_channel,
            thread_ts=thread_ts,
            event_ts=event_ts,
            user_id=user_id,
            config=config,
        )
        if not scheduled:
            storage.record_slack_chat_schedule_failure(
                event_id,
                channel_id=channel_id,
                conversation_id=conv,
            )
            raise SlackGatewayOverloadError("Slack chat outbound queue is overloaded")
        return _finish(
            {
                "accepted": True,
                "duplicate": False,
                "dispatched": False,
                "kind": "chat",
                "event_id": event_id,
                "event_ulid": event_ulid,
                "channel": protocol_channel,
                "slack_chat": {"scheduled": scheduled},
            },
            ack=False,
        )
    try:
        result = dispatch(
            request,
            user_id=user_id,
            event_id=event_id,
            channel_id=channel_id,
            coordinator_agent=config.coordinator_agent,
            callbacks=callbacks,
            config=config,
            thread_ts=root or None,
        )
    except agent_access.AgentAccessDenied:
        # Governance denials must PROPAGATE, not be converted into a generic
        # dispatch failure: AgentAccessDenied subclasses ToolError, so the broad
        # handler below would otherwise swallow it (task #585's fail-closed
        # contract) and record it as slack.dispatch.failed, erasing the audit
        # distinction between "policy denied" and "the call broke".
        raise
    except (
        KeyError,
        LookupError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
        ToolError,
    ) as exc:
        # A WIP cap refusal (task #1886: `/progress` moves a task into doing
        # and passes the same caps as task_claim) is a definite answer, not a
        # breakage to review: say so instead of "queued for human review".
        # Only the fixed reason is surfaced, never the message (it can name
        # agent labels or lanes).
        wip_refused = isinstance(exc, ToolError) and str(exc).startswith("WIP cap:")
        failure: dict[str, Any] = {"event_id": event_id, "error_type": type(exc).__name__}
        if wip_refused:
            failure["reason"] = "wip_cap"
        try:
            append_evidence_event(
                "slack.dispatch.failed",
                payload=failure,
                originator=user_id,
                notify=False,
                dedupe_key=f"slack:dispatch-failed:{event_id}",
            )
        except (OSError, sqlite3.Error):
            log.exception("could not record Slack dispatch failure for %s", event_id)
        response: dict[str, Any] = {
            "accepted": True,
            "duplicate": False,
            "dispatched": False,
            "event_id": event_id,
            "event_ulid": event_ulid,
        }
        if wip_refused:
            response["reason"] = "wip_cap"
        else:
            response["requires_review"] = True
        return _finish(response)
    return _finish(
        {
            "accepted": True,
            "duplicate": False,
            "dispatched": True,
            "event_id": event_id,
            "event_ulid": event_ulid,
            "channel": protocol_channel,
            "result": result,
        }
    )


def frontier_policy(
    model: str, *, budget_usd: float | None = None, config: SlackConfig | None = None
) -> dict[str, Any]:
    """Return a fail-closed frontier decision without invoking a provider."""
    policy = (config or SlackConfig.from_env()).frontier
    return policy.authorize(model, budget_usd=budget_usd)


def record_frontier_usage(
    *,
    model: str,
    agent: str,
    project: str,
    duration_s: float,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    outcome: str,
    provider: str | None = None,
    request_id: str | None = None,
) -> str:
    """Record only bounded frontier usage metadata; prompts and responses are rejected."""
    values = (duration_s, input_tokens, output_tokens, cost_usd)
    if not all(math.isfinite(float(value)) for value in values):
        raise SlackGatewayError("frontier telemetry values must be finite")
    if (
        duration_s < 0
        or any(int(value) < 0 for value in (input_tokens, output_tokens))
        or cost_usd < 0
    ):
        raise SlackGatewayError(
            "frontier telemetry duration, counts, and cost must be non-negative"
        )
    payload = {
        "provider": provider or "unknown",
        "model": model[:120],
        "agent": agent[:120],
        "project": project[:120],
        "duration_s": round(float(duration_s), 3),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": round(float(cost_usd), 6),
        "outcome": outcome[:80],
    }
    dedupe = f"frontier:usage:{request_id}" if request_id else None
    append_evidence_event = object_model.by_kind("evidence_event").writer("append")
    return append_evidence_event(
        "frontier.usage",
        payload=payload,
        originator=agent,
        notify=False,
        dedupe_key=dedupe,
    )[0]
