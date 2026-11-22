"""Slack configuration shared by the inbound gateway, outbound replies and live chat.

Everything here is read from the environment and validated fail-closed:
channel and user allowlists, the frontier provider policy, and the
default-disabled ``status task-<id>`` identity map. ``slack_gateway`` (the
inbound HTTP adapter), ``slack_outbound`` and ``slack_chat`` all build on it,
so it sits below them.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import agent_tokens, operator_label, revoked_agents

SLACK_CHANNELS = frozenset({"requests", "coord", "plans", "review", "ops"})
MAX_TEXT_CHARS = 4000

# Hub identity the Slack adapter coordinates as when UA_SLACK_COORDINATOR_AGENT
# is unset: the /contact target, the from_agent of /assign delegations, the
# owner and author of /plan plans, and the label on outbound Slack posts. It is
# the chief of staff's seat, which owns dispatch coordination. It must be on
# the hub's agent roster, because delegate() refuses an unknown from_agent on a
# closed roster (task #1879).
DEFAULT_COORDINATOR_AGENT = "cloud_agent17"

log = logging.getLogger(__name__)


def _known_status_identities() -> frozenset[str]:
    """Identities a Slack status-lookup mapping entry may target (task #1104).

    This is ``UA_AGENT_TOKENS`` — the same bearer-token registry
    ``HubAuthASGIMiddleware`` resolves a caller's verified identity against
    (``server.py``'s ``tokens = config.agent_tokens()``) — read fresh on every
    call so a runtime token-registry change (or a test's monkeypatched env)
    is honored without a process restart. An entry naming anything else (a
    coordinator label, a made-up handle, an agent with no hub token) fails
    config validation and the feature stays off. The human Web UI operator
    (``config.operator_label()``, ``UA_OPERATOR_LABEL``), who authenticates via
    OAuth/WorkOS rather than a bearer token and so has no ``UA_AGENT_TOKENS``
    entry of their own, is added explicitly; ``profiles.default_for_identity`` still decides the
    profile the read runs under, this only decides who may be targeted.
    """
    return frozenset(agent_tokens().values()) | {operator_label()}


class SlackGatewayError(ValueError):
    """A rejected Slack request or unsafe coordination command."""


class SlackGatewayOverloadError(SlackGatewayError):
    """Background Slack outbound queue refused work; Slack should retry the event."""


def _frontier_number(name: str, default: Any, kind: type) -> Any:
    """Parse a ``UA_FRONTIER_*`` numeric limit; a bad value warns and uses the default.

    ``SlackConfig.from_env`` builds the frontier policy eagerly, so raising here
    would break every Slack tool over an unused, optional surface. The defaults
    are the most restrictive-or-documented values (budget 0 keeps the frontier
    disabled), so falling back never widens what the policy allows.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = kind(raw)
        if kind is float and not math.isfinite(value):
            raise ValueError("non-finite")
    except (ValueError, OverflowError):
        log.warning("%s=%r is not a valid %s; using default %s", name, raw, kind.__name__, default)
        return default
    return value


@dataclass(frozen=True)
class FrontierPolicy:
    """Server-side frontier provider policy; no provider call is made here."""

    models: frozenset[str]
    provider: str
    credential_env: str
    max_budget_usd: float
    timeout_s: int
    max_prompt_chars: int
    max_output_tokens: int
    kill_switch: bool
    credential_present: bool

    @classmethod
    def from_env(cls) -> FrontierPolicy:
        provider = os.environ.get("UA_FRONTIER_PROVIDER", "").strip().lower()
        credentials = {
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        credential_env = credentials.get(provider, "")
        budget = _frontier_number("UA_FRONTIER_MAX_BUDGET_USD", 0.0, float)
        timeout = _frontier_number("UA_FRONTIER_TIMEOUT_S", 120, int)
        prompt_chars = _frontier_number("UA_FRONTIER_MAX_PROMPT_CHARS", 12000, int)
        output_tokens = _frontier_number("UA_FRONTIER_MAX_OUTPUT_TOKENS", 4096, int)
        return cls(
            models=frozenset(_csv("UA_FRONTIER_MODELS")),
            provider=provider,
            credential_env=credential_env,
            max_budget_usd=max(0.0, budget),
            timeout_s=max(1, min(timeout, 3600)),
            max_prompt_chars=max(1, min(prompt_chars, 100_000)),
            max_output_tokens=max(1, min(output_tokens, 32_000)),
            kill_switch=_truthy(os.environ.get("UA_FRONTIER_KILL_SWITCH", "0")),
            credential_present=bool(credential_env and os.environ.get(credential_env)),
        )

    def authorize(self, model: str, *, budget_usd: float | None = None) -> dict[str, Any]:
        requested_budget = self.max_budget_usd if budget_usd is None else budget_usd
        if not math.isfinite(requested_budget):
            raise SlackGatewayError("frontier budget must be finite")
        reasons: list[str] = []
        if self.kill_switch:
            reasons.append("kill switch enabled")
        if not self.models or model not in self.models:
            reasons.append("model is not allowlisted")
        if not self.provider or not self.credential_present:
            reasons.append("server-side provider credential is unavailable")
        if self.max_budget_usd <= 0 or requested_budget <= 0:
            reasons.append("frontier budget is disabled")
        if requested_budget > self.max_budget_usd:
            reasons.append("requested budget exceeds configured limit")
        return {
            "allowed": not reasons,
            "model": model,
            "provider": self.provider,
            "budget_usd": min(max(requested_budget, 0.0), self.max_budget_usd),
            "timeout_s": self.timeout_s,
            "max_prompt_chars": self.max_prompt_chars,
            "max_output_tokens": self.max_output_tokens,
            "reason": "ok" if not reasons else "; ".join(reasons),
        }


@dataclass(frozen=True)
class SlackStatusLookupConfig:
    """DEFAULT-DISABLED ``status task-<id>`` identity mapping (task #1104).

    An explicit, admin-maintained Slack-user-ID -> registered UA identity map.
    There is no coordinator fallback and no profile derived from env vars or
    message content: a Slack user who is not a key in ``identity_map`` is
    denied, full stop. ``error`` is set when ``UA_SLACK_STATUS_IDENTITY_MAP``
    itself is malformed (a duplicate Slack user id, or an entry naming an
    identity outside ``_known_status_identities()``) — that is a fail-closed
    config validation error, so ``resolve_identity`` denies unconditionally
    even if ``enabled=1`` was also set; it never partially trusts a broken map.
    """

    enabled: bool
    identity_map: Mapping[str, str]
    error: str | None

    @classmethod
    def from_env(cls) -> SlackStatusLookupConfig:
        enabled = _truthy(os.environ.get("UA_SLACK_STATUS_LOOKUP_ENABLED", "0"))
        try:
            identity_map: Mapping[str, str] = _status_identity_mapping(
                os.environ.get("UA_SLACK_STATUS_IDENTITY_MAP", "")
            )
        except SlackGatewayError as exc:
            return cls(enabled=False, identity_map={}, error=str(exc))
        return cls(enabled=enabled, identity_map=identity_map, error=None)

    def resolve_identity(self, slack_user_id: str) -> str:
        """Return the mapped, non-revoked UA identity, or deny fail-closed."""
        if self.error:
            # Checked before ``enabled``: a malformed map forces enabled=False
            # in ``from_env``, and the invalid-config reason is more useful
            # than a generic "disabled" here.
            raise SlackGatewayError(
                f"Slack task status identity map is invalid, feature stays off: {self.error}"
            )
        if not self.enabled:
            raise SlackGatewayError("Slack task status lookup is disabled")
        identity = self.identity_map.get(slack_user_id)
        if not identity:
            raise SlackGatewayError("Slack user is not mapped to a Unified-Agent identity")
        if identity in revoked_agents():
            raise SlackGatewayError(f"identity {identity!r} is revoked")
        return identity


@dataclass(frozen=True)
class SlackConfig:
    signing_secret: str
    bot_token: str | None
    app_token: str | None
    channel_ids: frozenset[str]
    channel_names_by_id: Mapping[str, str]
    users: frozenset[str]
    coordinator_agent: str
    enabled: bool
    frontier: FrontierPolicy
    # Defaulted (task #1104 follow-up): every pre-existing SlackConfig(...)
    # construction in the repo predates this field. A disabled instance is
    # the correct default anyway -- the feature is opt-in -- so giving it one
    # keeps those call sites correct without editing each of them.
    status_lookup: SlackStatusLookupConfig = field(
        default_factory=lambda: SlackStatusLookupConfig(enabled=False, identity_map={}, error=None)
    )

    @classmethod
    def from_env(cls) -> SlackConfig:
        mapping = _channel_mapping(os.environ.get("UA_SLACK_CHANNEL_MAP", ""))
        channel_ids = frozenset(mapping) or frozenset(_csv("UA_SLACK_CHANNEL_IDS"))
        return cls(
            signing_secret=os.environ.get("SLACK_SIGNING_SECRET", ""),
            bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
            app_token=os.environ.get("SLACK_APP_TOKEN") or None,
            channel_ids=channel_ids,
            channel_names_by_id=mapping,
            users=frozenset(_csv("UA_SLACK_USERS")),
            coordinator_agent=os.environ.get(
                "UA_SLACK_COORDINATOR_AGENT", DEFAULT_COORDINATOR_AGENT
            ),
            enabled=_truthy(os.environ.get("UA_SLACK_ENABLED", "0")),
            frontier=FrontierPolicy.from_env(),
            status_lookup=SlackStatusLookupConfig.from_env(),
        )

    def validate(self) -> None:
        if not self.enabled:
            raise SlackGatewayError("Slack gateway is disabled")
        if not self.signing_secret:
            raise SlackGatewayError("SLACK_SIGNING_SECRET is required")
        if not self.users:
            raise SlackGatewayError("UA_SLACK_USERS must be a non-empty Slack user-ID allowlist")
        if not self.channel_ids:
            raise SlackGatewayError("UA_SLACK_CHANNEL_IDS or UA_SLACK_CHANNEL_MAP is required")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> set[str]:
    return {part.strip() for part in os.environ.get(name, "").split(",") if part.strip()}


def _channel_mapping(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            channel_id, logical_name = entry.split("=", 1)
        elif ":" in entry:
            channel_id, logical_name = entry.split(":", 1)
        else:
            raise SlackGatewayError("UA_SLACK_CHANNEL_MAP entries must be CHANNEL_ID=channel")
        channel_id, logical_name = channel_id.strip(), logical_name.strip().lower()
        if not channel_id or logical_name not in SLACK_CHANNELS:
            raise SlackGatewayError("Slack channel map contains an invalid ID or protocol channel")
        result[channel_id] = logical_name
    return result


def _status_identity_mapping(raw: str) -> dict[str, str]:
    """Parse ``UA_SLACK_STATUS_IDENTITY_MAP`` (``SLACK_USER_ID=UaIdentity, ...``).

    Fails closed on anything ambiguous or unrecognized: a duplicate Slack
    user id, a malformed entry, or an identity outside
    ``_known_status_identities()`` all raise, and the caller
    (``SlackStatusLookupConfig.from_env``) turns that into a disabled,
    error-carrying config rather than letting a bad map silently resolve one
    of two entries or accept an unregistered identity label.
    """
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SlackGatewayError(
                "UA_SLACK_STATUS_IDENTITY_MAP entries must be SLACK_USER_ID=UaIdentity"
            )
        slack_user_id, identity = entry.split("=", 1)
        slack_user_id, identity = slack_user_id.strip(), identity.strip()
        if not slack_user_id or not identity:
            raise SlackGatewayError("Slack status identity map entries must not be blank")
        if slack_user_id in result:
            raise SlackGatewayError(
                f"duplicate Slack user id in status identity map: {slack_user_id}"
            )
        if identity not in _known_status_identities():
            raise SlackGatewayError(
                f"{identity!r} is not a known registered Unified-Agent identity"
            )
        result[slack_user_id] = identity
    return result
