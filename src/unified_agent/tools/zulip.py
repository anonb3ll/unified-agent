"""Bounded outbound messages to the private Zulip coordination workspace."""

from __future__ import annotations

import configparser
import html
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .. import config

MAX_CONTENT_CHARS = 4000
MAX_TOPIC_CHARS = 200
MAX_FETCH_LIMIT = 100
ALLOWED_CHANNELS = frozenset({"ops", "requests", "coord", "review", "meta"})
DEFAULT_EVENT_CHANNELS = ("ops", "requests", "coord", "review", "meta")
EVENT_TYPES = ("message",)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Logical allowlist keys map to the canonical Zulip stream names on
# the configured site (some streams use Title Case in the UI).
_STREAM_CANONICAL: dict[str, str] = {
    "ops": "ops",
    "requests": "Requests",
    "coord": "coord",
    "review": "Review",
    "meta": "meta",
}

_BOT_CONFIG_FILES = {
    "Desktop_Agent5": "zuliprc-desktop-agent5",
    "Desktop_Agent1": "zuliprc-desktop-agent1",
    "Desktop_Agent2": "zuliprc-desktop-agent2",
    "Desktop_agent3": "zuliprc-desktop-agent3",
    "Desktop_Agent4": "zuliprc-desktop-agent4",
}


def plain_text_content(content: str) -> str:
    """Normalize Zulip rendered HTML/Markdown-ish content to plain text.

    Zulip's messages API returns rendered HTML by default (for example
    ``<p>[target: Desktop_Agent4] hello</p>``). Target markers and mentions must
    be classified against the plain-text form or routing silently fails.
    """
    text = html.unescape(content or "")
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


class ZulipRelayError(ToolError):
    """A safe, operator-actionable relay configuration or request error."""


@dataclass(frozen=True)
class ZulipCredentials:
    """Credentials loaded from one owner-only ``zuliprc`` file."""

    email: str
    api_key: str
    site: str


def zulip_retired() -> bool:
    """Whether outbound Zulip sends are refused.

    Fail-closed: an unset, empty, or unrecognized ``UA_ZULIP_RETIRED`` is
    retired. Only ``0``, ``false``, ``no``, or ``off`` re-opens outbound
    sends, and only for an explicitly approved rollback.
    Distinct from ``UA_ZULIP_CHAT`` (auto-reply gate, default off). Hub
    wrappers still export ``UA_ZULIP_RETIRED=1`` as belt-and-braces; the
    Python default is what refuses a send when that export is absent.
    """
    raw = os.environ.get("UA_ZULIP_RETIRED")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _site_allowlist() -> str:
    site = os.environ.get("UA_ZULIP_SITE", "").strip().rstrip("/")
    if not site:
        raise ZulipRelayError("configure UA_ZULIP_SITE (chat.zulip_site) before using Zulip")
    try:
        parsed = urlsplit(site)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(ch.isspace() for ch in site)
        )
        _ = parsed.port  # validate malformed port syntax without exposing the URL
    except ValueError:
        valid = False
    if not valid:
        raise ZulipRelayError(
            "UA_ZULIP_SITE must be an HTTPS site without credentials or query/fragment"
        )
    return site


def zulip_site_status() -> dict[str, str | None]:
    """Validate display configuration without loading credentials or probing Zulip."""
    try:
        return {"site": _site_allowlist(), "configuration_error": None}
    except ZulipRelayError as exc:
        return {"site": "", "configuration_error": str(exc)}


def _canonical_channel(channel: str) -> str:
    """Map a logical allowlist channel to the Zulip stream name used by the API."""
    key = channel.strip()
    if key not in ALLOWED_CHANNELS:
        raise ZulipRelayError(f"channel must be one of {', '.join(sorted(ALLOWED_CHANNELS))}")
    return _STREAM_CANONICAL[key]


def _logical_channel(channel: str) -> str:
    """Map a canonical Zulip stream name back to its logical allowlist key."""
    canonical = str(channel or "").strip()
    for logical, name in _STREAM_CANONICAL.items():
        if canonical == name or canonical == logical:
            return logical
    return canonical


def _require_agent_credentials(agent_label: str) -> str:
    """Bind Zulip credential selection to the verified HTTP caller when present."""
    label = (agent_label or "").strip()
    if label not in _BOT_CONFIG_FILES:
        raise ZulipRelayError(f"agent_label must be one of {', '.join(sorted(_BOT_CONFIG_FILES))}")
    verified = config.verified_agent_identity()
    if verified and verified != label:
        raise ToolError(f"Auth identity mismatch: claimed {label!r} but verified as {verified!r}")
    return label


def zulip_credential_status() -> dict[str, object]:
    """Report whether each mapped agent bot has a loadable owner-only zuliprc."""
    site_status = zulip_site_status()
    site = site_status["site"]
    if site_status["configuration_error"]:
        error = site_status["configuration_error"]
        return {
            "site": "",
            "agents": {label: error for label in sorted(_BOT_CONFIG_FILES)},
            "ready": False,
            "missing": sorted(_BOT_CONFIG_FILES),
            "configuration_error": error,
        }
    agents: dict[str, str] = {}
    for label in sorted(_BOT_CONFIG_FILES):
        try:
            load_zuliprc(label)
            agents[label] = "ok"
        except ZulipRelayError as exc:
            agents[label] = str(exc)
    missing = [label for label, state in agents.items() if state != "ok"]
    return {
        "site": site,
        "agents": agents,
        "ready": not missing,
        "missing": missing,
    }


def _config_path(agent_label: str) -> Path:
    try:
        filename = _BOT_CONFIG_FILES[agent_label]
    except KeyError:
        raise ZulipRelayError(f"no Zulip credential mapping for agent {agent_label!r}") from None
    return config.data_home() / filename


def load_zuliprc(agent_label: str) -> ZulipCredentials:
    """Load one mapped, owner-only Zulip credential file without exposing its key."""
    allowed_site = _site_allowlist()
    path = _config_path(agent_label)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise ZulipRelayError(f"Zulip credential file is missing: {path}") from None
    except OSError as exc:
        raise ZulipRelayError(f"cannot inspect Zulip credential file: {path}") from exc

    if mode & 0o077:
        raise ZulipRelayError(f"Zulip credential file must be owner-only (600): {path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ZulipRelayError(f"cannot read Zulip credential file: {path}") from exc

    if not parser.has_section("api"):
        raise ZulipRelayError(f"Zulip credential file has no [api] section: {path}")

    section = parser["api"]
    email = section.get("email", "").strip()
    api_key = section.get("key", "").strip()
    site = section.get("site", "").strip().rstrip("/")
    if site != allowed_site:
        raise ZulipRelayError("Zulip credential site is not in the configured allowlist")
    from .. import agent_access

    if agent_access.managed_secret_mode():
        from .. import edition
        from ..managed_secrets import _load_for_consumer

        api_key = _load_for_consumer(
            f"zulip:{agent_label}", "zulip", org_id=edition.tenant_org_id()
        )
    if not email or not api_key or not site:
        raise ZulipRelayError(f"Zulip credential file is missing required fields: {path}")
    return ZulipCredentials(email=email, api_key=api_key, site=site)


def zulip_send_message(
    agent_label: str,
    channel: str,
    topic: str,
    content: str,
) -> dict[str, object]:
    """Send one bounded channel message using the named agent's Zulip bot.

    This is an explicit outbound primitive. It does not poll Zulip, interpret
    messages as commands, or automatically mirror Unified-Agent events.
    """
    if zulip_retired():
        raise ZulipRelayError(
            "Zulip outbound sending is retired (UA_ZULIP_RETIRED defaults to on); "
            "Slack is now the coordination channel. Set UA_ZULIP_RETIRED=0 only "
            "for an explicitly approved rollback investigation."
        )
    agent_label = _require_agent_credentials(agent_label)
    logical_channel = channel.strip()
    channel = _canonical_channel(channel)
    topic = topic.strip()
    content = content.strip()
    if not topic:
        raise ZulipRelayError("topic must not be empty")
    if len(topic) > MAX_TOPIC_CHARS:
        raise ZulipRelayError(f"topic exceeds maximum length of {MAX_TOPIC_CHARS} characters")
    if not content:
        raise ZulipRelayError("content must not be empty")
    if len(content) > MAX_CONTENT_CHARS:
        raise ZulipRelayError(f"content exceeds maximum length of {MAX_CONTENT_CHARS} characters")

    credentials = load_zuliprc(agent_label)
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{credentials.site}/api/v1/messages",
                auth=(credentials.email, credentials.api_key),
                data={
                    "type": "stream",
                    "to": channel,
                    "topic": topic,
                    "content": content,
                },
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "sent": False,
            "agent": agent_label,
            "channel": logical_channel,
            "topic": topic,
            "error": "Zulip API request failed",
        }

    if not isinstance(body, dict) or body.get("result") != "success":
        return {
            "sent": False,
            "agent": agent_label,
            "channel": logical_channel,
            "topic": topic,
            "error": "Zulip API rejected the message",
        }

    return {
        "sent": True,
        "message_id": body.get("id"),
        "agent": agent_label,
        "channel": logical_channel,
        "topic": topic,
    }


def _message_narrow(channel: str, topic: str | None) -> str:
    narrow: list[dict[str, str]] = [{"operator": "channel", "operand": channel}]
    if topic is not None:
        narrow.append({"operator": "topic", "operand": topic})
    return json.dumps(narrow)


def _inbound_message(message: object, *, channel: str, own_email: str) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    sender_email = message.get("sender_email")
    content = message.get("content")
    if not isinstance(message_id, int) or not isinstance(sender_email, str):
        return None
    if sender_email == own_email or not isinstance(content, str):
        return None
    return {
        "id": message_id,
        "sender_email": sender_email,
        "sender_full_name": message.get("sender_full_name", ""),
        "type": message.get("type", "stream"),
        "channel": channel,
        "topic": message.get("subject", ""),
        "content": content,
        "timestamp": message.get("timestamp"),
    }


def zulip_fetch_messages(
    agent_label: str,
    channel: str,
    topic: str | None = None,
    anchor: str = "newest",
    limit: int = 20,
) -> dict[str, object]:
    """Fetch bounded inbound messages from one allowlisted Zulip channel.

    ``anchor="newest"`` returns up to ``limit`` recent messages. Passing a
    numeric anchor returns only messages after that message ID. The result
    includes ``next_anchor`` for the next poll. Messages sent by the polling
    bot are excluded to prevent self-echo loops. This tool only reads; it does
    not mark messages read, execute commands, call a model, or send a reply.
    """
    agent_label = _require_agent_credentials(agent_label)
    logical_channel = channel.strip()
    channel = _canonical_channel(channel)
    if topic is not None:
        topic = topic.strip()
        if not topic:
            raise ZulipRelayError("topic must not be empty when provided")
        if len(topic) > MAX_TOPIC_CHARS:
            raise ZulipRelayError(f"topic exceeds maximum length of {MAX_TOPIC_CHARS} characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_FETCH_LIMIT:
        raise ZulipRelayError(f"limit must be an integer from 1 to {MAX_FETCH_LIMIT}")

    anchor = str(anchor).strip()
    if anchor == "newest":
        anchor_value: str | int = anchor
        num_before, num_after = limit, 0
    elif anchor.isdigit():
        anchor_value = int(anchor)
        num_before, num_after = 0, limit
    else:
        raise ZulipRelayError("anchor must be 'newest' or a non-negative message ID")

    credentials = load_zuliprc(agent_label)
    params = {
        "anchor": anchor_value,
        "num_before": num_before,
        "num_after": num_after,
        "narrow": _message_narrow(channel, topic),
        # Classifiers are start-anchored on Markdown markers; default HTML wraps them.
        "apply_markdown": "false",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{credentials.site}/api/v1/messages",
                auth=(credentials.email, credentials.api_key),
                params=params,
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "received": False,
            "agent": agent_label,
            "channel": logical_channel,
            "topic": topic,
            "anchor": anchor,
            "error": "Zulip API request failed",
        }

    if not isinstance(body, dict) or body.get("result") != "success":
        return {
            "received": False,
            "agent": agent_label,
            "channel": logical_channel,
            "topic": topic,
            "anchor": anchor,
            "error": "Zulip API rejected the message fetch",
        }

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        return {
            "received": False,
            "agent": agent_label,
            "channel": logical_channel,
            "topic": topic,
            "anchor": anchor,
            "error": "Zulip API returned an invalid message list",
        }

    message_ids = [
        message.get("id")
        for message in raw_messages
        if isinstance(message, dict) and isinstance(message.get("id"), int)
    ]
    messages = [
        normalized
        for message in raw_messages
        if (
            normalized := _inbound_message(
                message, channel=logical_channel, own_email=credentials.email
            )
        )
        is not None
    ]
    if message_ids:
        next_anchor: int | None = max(message_ids)
    elif isinstance(anchor_value, int):
        # Empty numeric-anchor polls must retain the cursor, not reset it.
        next_anchor = anchor_value
    else:
        next_anchor = None
    return {
        "received": True,
        "agent": agent_label,
        "channel": logical_channel,
        "topic": topic,
        "anchor": anchor,
        "next_anchor": next_anchor,
        "messages": messages,
    }


def zulip_register_event_queue(
    agent_label: str,
    channels: tuple[str, ...] = DEFAULT_EVENT_CHANNELS,
) -> dict[str, object]:
    """Register one filtered message event queue for a bot identity.

    The queue is deliberately scoped to the five coordination channels. The
    caller persists the returned queue ID and event cursor; credentials never
    enter the returned state.
    """
    agent_label = _require_agent_credentials(agent_label)
    if not channels:
        raise ZulipRelayError("at least one event channel is required")
    canonical = [_canonical_channel(channel) for channel in channels]
    credentials = load_zuliprc(agent_label)
    data = {
        "event_types": json.dumps(list(EVENT_TYPES)),
        "fetch_event_types": json.dumps(list(EVENT_TYPES)),
        "narrow": json.dumps([["channel", channel] for channel in canonical]),
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{credentials.site}/api/v1/register",
                auth=(credentials.email, credentials.api_key),
                data=data,
            )
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "registered": False,
            "agent": agent_label,
            "error": "Zulip event queue registration failed",
        }

    if not isinstance(body, dict) or body.get("result") != "success":
        return {
            "registered": False,
            "agent": agent_label,
            "code": body.get("code") if isinstance(body, dict) else None,
            "error": "Zulip event queue registration was rejected",
        }
    queue_id = body.get("queue_id")
    last_event_id = body.get("last_event_id")
    if not isinstance(queue_id, str) or not queue_id or not isinstance(last_event_id, int):
        return {
            "registered": False,
            "agent": agent_label,
            "error": "Zulip event queue registration returned invalid state",
        }
    return {
        "registered": True,
        "agent": agent_label,
        "queue_id": queue_id,
        "last_event_id": last_event_id,
        "idle_queue_timeout_secs": body.get("idle_queue_timeout_secs"),
    }


def zulip_fetch_events(
    agent_label: str,
    queue_id: str,
    last_event_id: int,
    *,
    dont_block: bool = False,
) -> dict[str, object]:
    """Fetch events from a registered queue with bounded long polling."""
    agent_label = _require_agent_credentials(agent_label)
    if not queue_id.strip():
        raise ZulipRelayError("queue_id must not be empty")
    if not isinstance(last_event_id, int) or isinstance(last_event_id, bool):
        raise ZulipRelayError("last_event_id must be an integer")

    credentials = load_zuliprc(agent_label)
    params: dict[str, object] = {"queue_id": queue_id, "last_event_id": last_event_id}
    if dont_block:
        params["dont_block"] = "true"
    try:
        with httpx.Client(timeout=65.0) as client:
            response = client.get(
                f"{credentials.site}/api/v1/events",
                auth=(credentials.email, credentials.api_key),
                params=params,
            )
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "received": False,
            "agent": agent_label,
            "queue_id": queue_id,
            "error": "Zulip event queue fetch failed",
        }

    if not isinstance(body, dict) or body.get("result") != "success":
        return {
            "received": False,
            "agent": agent_label,
            "queue_id": queue_id,
            "code": body.get("code") if isinstance(body, dict) else None,
            "error": "Zulip event queue fetch was rejected",
        }
    events = body.get("events")
    if not isinstance(events, list):
        return {
            "received": False,
            "agent": agent_label,
            "queue_id": queue_id,
            "error": "Zulip event queue returned an invalid event list",
        }
    return {
        "received": True,
        "agent": agent_label,
        "queue_id": body.get("queue_id", queue_id),
        "events": events,
    }
