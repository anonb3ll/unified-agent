"""Env-gated HTTP transport adapters for the Discord and Telegram bot surfaces.

No SDK: each surface is a handful of REST calls over ``httpx``. Credentials are
read from the environment at call time — never hardcoded, never logged. When a
token is absent the surface is *unavailable* (disabled), which is the default
state until the operator lands tokens on the hub host (tasks #727/#690 lane).

Tests never touch the network: they drive :class:`TsBotTransport` subclasses or
monkeypatched stubs whose request log is asserted on directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"
TELEGRAM_API = "https://api.telegram.org"

DEFAULT_TIMEOUT_S = 10.0

# Discord hard-caps message content at 2000 chars; keep headroom for the
# attribution suffix added by the reply path.
DISCORD_MAX_MESSAGE_CHARS = 2000
# Telegram allows 4096 per message.
TELEGRAM_MAX_MESSAGE_CHARS = 4096


def discord_token() -> str | None:
    """Bot token from the environment, or None when the surface is disabled."""
    token = (os.environ.get("UA_TS_DISCORD_TOKEN") or "").strip()
    return token or None


def _colon_list(raw: str | None) -> tuple[str, ...]:
    """Split a colon-separated env value, dropping blank entries."""
    return tuple(part.strip() for part in (raw or "").split(":") if part.strip())


def discord_allowed_user_ids() -> tuple[str, ...]:
    """The operator's authorized Discord user IDs (``UA_TS_DISCORD_ALLOWED_USER_IDS``)."""
    return _colon_list(os.environ.get("UA_TS_DISCORD_ALLOWED_USER_IDS"))


def discord_manifest_secret_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Discord connector readiness, normalized the way the worker reads it.

    A plain non-empty check would count a whitespace-only token or a
    separator-only list (``":"``) as present while the worker sees nothing,
    so ``connector:discord`` could read LIVE while ``ts_worker`` reads
    MISCONFIGURED. Same ``{present, missing, satisfied}`` shape as
    ``connectors.secret_status``; names only, never values.
    """
    source = os.environ if env is None else env
    ready = {
        "UA_TS_DISCORD_TOKEN": bool((source.get("UA_TS_DISCORD_TOKEN") or "").strip()),
        "UA_TS_DISCORD_ALLOWED_USER_IDS": bool(
            _colon_list(source.get("UA_TS_DISCORD_ALLOWED_USER_IDS"))
        ),
        "UA_TS_DISCORD_CHANNELS": bool(_colon_list(source.get("UA_TS_DISCORD_CHANNELS"))),
    }
    missing = sorted(name for name, ok in ready.items() if not ok)
    return {
        "present": sorted(name for name, ok in ready.items() if ok),
        "missing": missing,
        "satisfied": not missing,
    }


def telegram_token() -> str | None:
    """Bot token from the environment, or None when the surface is disabled."""
    token = (os.environ.get("UA_TS_TELEGRAM_TOKEN") or "").strip()
    return token or None


def telegram_allowed_user_ids() -> tuple[str, ...]:
    """The operator's authorized Telegram user IDs (``UA_TS_TELEGRAM_ALLOWED_USER_IDS``)."""
    raw = (os.environ.get("UA_TS_TELEGRAM_ALLOWED_USER_IDS") or "").strip()
    return tuple(part.strip() for part in raw.split(":") if part.strip())


@dataclass
class TransportRequest:
    """One outbound API call, kept inspectable for tests."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None


class TsBotTransport:
    """Minimal REST adapter for both surfaces; stubbed out in tests.

    The transport never stores the token — each call receives it, so a test
    stub sees exactly what the worker would have put on the wire (and can
    assert the Authorization header never appears in payloads or logs).
    """

    def request(self, req: TransportRequest) -> dict[str, Any]:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
            response = client.request(
                req.method,
                req.url,
                headers=req.headers,
                params=req.params or None,
                json=req.json_body,
            )
            response.raise_for_status()
            data: dict[str, Any] | Any = None
            if response.content:
                data = response.json()
            return data if isinstance(data, dict) else {"ok": True, "raw": data}


class DiscordGateway:
    """Discord REST polling adapter (pull channel messages; post replies).

    Polling ``GET /channels/{id}/messages`` with a per-channel ``after`` cursor
    keeps this a pure outbound-HTTPS client — no websocket, no gateway intents,
    no public ingress — which matches the #690 lane's security posture and the
    e2-micro host's budget. Receiving the bot's own messages is avoided by
    filtering ``author.bot`` at the classify layer.
    """

    def __init__(self, transport: TsBotTransport | None = None) -> None:
        self._transport = transport or TsBotTransport()

    def fetch_messages(
        self, channel_id: str, after: int | None, limit: int = 10
    ) -> list[dict[str, Any]]:
        token = discord_token()
        if token is None:
            raise RuntimeError("UA_TS_DISCORD_TOKEN is not configured")
        limit = max(1, min(int(limit), 50))
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        req = TransportRequest(
            method="GET",
            url=f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            params=params,
        )
        data = self._transport.request(req)
        messages = data.get("raw") if "raw" in data else data
        if not isinstance(messages, list):
            return []
        return [m for m in messages if isinstance(m, dict)]

    def send_message(self, channel_id: str, content: str) -> dict[str, Any]:
        token = discord_token()
        if token is None:
            raise RuntimeError("UA_TS_DISCORD_TOKEN is not configured")
        req = TransportRequest(
            method="POST",
            url=f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json_body={"content": content[:DISCORD_MAX_MESSAGE_CHARS]},
        )
        return self._transport.request(req)


class TelegramGateway:
    """Telegram Bot API adapter (long-poll ``getUpdates``; ``sendMessage``)."""

    def __init__(self, transport: TsBotTransport | None = None) -> None:
        self._transport = transport or TsBotTransport()

    def get_updates(self, offset: int | None, timeout_s: int = 0) -> list[dict[str, Any]]:
        token = telegram_token()
        if token is None:
            raise RuntimeError("UA_TS_TELEGRAM_TOKEN is not configured")
        req = TransportRequest(
            method="GET",
            url=f"{TELEGRAM_API}/bot{token}/getUpdates",
            params={"offset": offset or 0, "timeout": max(0, int(timeout_s))},
        )
        data = self._transport.request(req)
        if not data.get("ok"):
            return []
        updates = data.get("result") or data.get("raw") or []
        if not isinstance(updates, list):
            return []
        return [u for u in updates if isinstance(u, dict)]

    def send_message(self, chat_id: str | int, text: str) -> dict[str, Any]:
        token = telegram_token()
        if token is None:
            raise RuntimeError("UA_TS_TELEGRAM_TOKEN is not configured")
        req = TransportRequest(
            method="POST",
            url=f"{TELEGRAM_API}/bot{token}/sendMessage",
            json_body={
                "chat_id": chat_id,
                "text": text[:TELEGRAM_MAX_MESSAGE_CHARS],
            },
        )
        return self._transport.request(req)
