"""Connector manifest contract — what "connector-shaped" means (task #1204, Epic G).

A ``ConnectorManifest`` is a structural declaration, never a live grant: it
names a connector's capabilities, the secrets required to exercise them (by
NAME only, never by value), the dotted entry points that implement those
capabilities, and defaults inert (``default_enabled=False``). It is deliberately
separate from the Horizon 2 scope catalog (``horizon2.CONNECTOR_PACKAGES``),
which governs approval-scoped *data access* grants (``read``/``search``/...)
for a live MCP connection. The two are different axes of the same connector:
a manifest's capabilities describe what code actually runs; a package's scopes
describe what an authorized caller may fetch through it. Conflating them would
force fabricating capabilities a connector does not really have (see
``docs/connectors.md`` for the Slack write-up of this exact seam).

This module never registers a server, mints a credential, or reads a secret's
*value*. It only validates shape, checks whether named secrets exist as
environment variables, and resolves declared entry points to real, importable
callables so a malformed manifest is caught before anything downstream trusts
it.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_ENTRY_POINT_RE = re.compile(r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*$")


@dataclass(frozen=True)
class OAuthClientSpec:
    """Fixed outbound OAuth endpoints and scopes; never carries credentials."""

    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    refresh_supported: bool = True

    def __post_init__(self) -> None:
        for url in (self.authorize_url, self.token_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "OAuth endpoints must be fixed HTTPS URLs without credentials or query"
                )
        if not self.scopes or any(not scope or " " in scope for scope in self.scopes):
            raise ValueError("OAuth scopes must be non-empty tokens")
        if any(
            not key
            or not value
            or key
            in {
                "state",
                "redirect_uri",
                "code_challenge",
                "code_challenge_method",
                "client_id",
                "response_type",
                "scope",
            }
            for key, value in self.extra_authorize_params.items()
        ):
            raise ValueError("OAuth authorize parameters cannot override broker security fields")
        object.__setattr__(
            self, "extra_authorize_params", MappingProxyType(dict(self.extra_authorize_params))
        )


GITHUB_OAUTH = OAuthClientSpec(
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    scopes=("read:user", "repo"),
    refresh_supported=False,
)

GOOGLE_OAUTH = OAuthClientSpec(
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes=(
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    ),
    extra_authorize_params={"access_type": "offline", "prompt": "consent"},
)


@dataclass(frozen=True)
class ConnectorManifest:
    """The contract every Horizon 2 connector declares about itself.

    ``required_secrets`` names environment variables by NAME only — this
    object never carries or logs a secret value — and every named secret is
    unconditionally required (AND semantics): a connector with two disjoint
    ways to authenticate (e.g. a GitHub App installation token vs. an
    operator user token) does not need *all* of them at once, so listing
    both sets under ``required_secrets`` would make every valid single-path
    configuration read as missing/misconfigured. ``required_secret_alternatives``
    exists for exactly that case: a tuple of secret-name groups where *at
    least one group* must be fully present (each group's own names are
    still ANDed together). ``required_secrets`` and
    ``required_secret_alternatives`` compose: both conditions must hold when
    both are non-empty. Use ``describe_required_secrets(manifest)`` for a
    human-readable rendering of the combined requirement (used by
    ``ensure_can_start``'s error and ``system_doctor``'s per-connector gate).

    ``entry_points`` maps each declared capability to a
    ``"module.path:callable"`` string; every capability must have exactly
    one entry point, and every entry point must name a declared capability.
    ``default_enabled`` defaults to ``False``: a manifest describes what a
    connector *can* do, never that it is live.
    ``horizon2_registration_supported`` defaults to ``True``; manifest-only
    contracts set it to ``False`` when a Horizon 2 registration cannot provide
    a real transport or consumer.
    """

    name: str
    version: str
    capabilities: frozenset[str]
    required_secrets: frozenset[str] = frozenset()
    required_secret_alternatives: tuple[frozenset[str], ...] = ()
    # Optional "module:callable" taking the env mapping and returning a
    # secret_status-shaped dict. For connectors whose real requirement
    # depends on runtime config a static list cannot express (github-app's
    # UA_GITHUB_TOKEN_MODE). Names only, never values — same contract.
    secret_status_hook: str | None = None
    entry_points: Mapping[str, str] = field(default_factory=dict)
    default_enabled: bool = False
    horizon2_registration_supported: bool = True
    oauth_client: OAuthClientSpec | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "required_secrets", frozenset(self.required_secrets))
        object.__setattr__(
            self,
            "required_secret_alternatives",
            tuple(frozenset(group) for group in self.required_secret_alternatives),
        )
        object.__setattr__(self, "entry_points", MappingProxyType(dict(self.entry_points)))
        if not _IDENTIFIER_RE.fullmatch(self.name):
            raise ValueError(f"connector name must match {_IDENTIFIER_RE.pattern}: {self.name!r}")
        if not _VERSION_RE.fullmatch(self.version):
            raise ValueError(
                f"connector {self.name}: version must be semver x.y.z, got {self.version!r}"
            )
        if not self.capabilities:
            raise ValueError(f"connector {self.name}: must declare at least one capability")
        for capability in self.capabilities:
            if not _IDENTIFIER_RE.fullmatch(capability):
                raise ValueError(f"connector {self.name}: invalid capability name {capability!r}")
        for secret in self.required_secrets:
            if not _SECRET_NAME_RE.fullmatch(secret):
                raise ValueError(
                    f"connector {self.name}: required secret {secret!r} must be an "
                    "UPPER_SNAKE_CASE environment variable NAME, never a value"
                )
        for group in self.required_secret_alternatives:
            if not group:
                raise ValueError(
                    f"connector {self.name}: required_secret_alternatives group must not be empty"
                )
            for secret in group:
                if not _SECRET_NAME_RE.fullmatch(secret):
                    raise ValueError(
                        f"connector {self.name}: required_secret_alternatives name {secret!r} "
                        "must be an UPPER_SNAKE_CASE environment variable NAME, never a value"
                    )
        if self.secret_status_hook is not None and ":" not in self.secret_status_hook:
            raise ValueError(
                f"connector {self.name}: secret_status_hook must be 'module.path:callable'"
            )
        entry_points = dict(self.entry_points)
        missing = set(self.capabilities) - entry_points.keys()
        if missing:
            raise ValueError(
                f"connector {self.name}: capabilities without an entry point: {sorted(missing)}"
            )
        extra = entry_points.keys() - set(self.capabilities)
        if extra:
            raise ValueError(
                f"connector {self.name}: entry points for undeclared capabilities: {sorted(extra)}"
            )
        for capability, dotted in entry_points.items():
            if not _ENTRY_POINT_RE.fullmatch(dotted):
                raise ValueError(
                    f"connector {self.name}: entry point for {capability!r} must be "
                    f"'module.path:callable', got {dotted!r}"
                )
        if not isinstance(self.default_enabled, bool):
            raise ValueError(f"connector {self.name}: default_enabled must be a bool")
        if not isinstance(self.horizon2_registration_supported, bool):
            raise ValueError(
                f"connector {self.name}: horizon2_registration_supported must be a bool"
            )
        if self.oauth_client is not None and not isinstance(self.oauth_client, OAuthClientSpec):
            raise ValueError(f"connector {self.name}: oauth_client must be an OAuthClientSpec")

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "required_secrets": sorted(self.required_secrets),
            "entry_points": dict(self.entry_points),
            "default_enabled": self.default_enabled,
        }
        # Only connectors that use the newer fields carry them, so existing
        # serialized manifests (Slack, Zulip) keep their exact shape.
        if self.required_secret_alternatives:
            out["required_secret_alternatives"] = [
                sorted(group) for group in sorted(self.required_secret_alternatives, key=sorted)
            ]
        if self.secret_status_hook:
            out["secret_status_hook"] = self.secret_status_hook
        if not self.horizon2_registration_supported:
            out["horizon2_registration_supported"] = False
        if self.oauth_client is not None:
            out["oauth_client"] = {
                "authorize_url": self.oauth_client.authorize_url,
                "token_url": self.oauth_client.token_url,
                "scopes": list(self.oauth_client.scopes),
                "pkce": "S256",
                "extra_authorize_params": dict(self.oauth_client.extra_authorize_params),
                "refresh_supported": self.oauth_client.refresh_supported,
            }
        return out

    def resolve_entry_point(self, capability: str):
        """Import and return the callable declared for ``capability``.

        Raises ``ValueError`` with a clear message if the capability is
        undeclared, the module does not import, or the attribute is missing —
        this is what makes an "invalid manifest" concrete rather than merely
        theoretical: a manifest whose entry points do not resolve to real code
        is refused, not silently trusted.
        """
        try:
            dotted = self.entry_points[capability]
        except KeyError as exc:
            raise ValueError(
                f"connector {self.name} has no entry point for capability {capability!r}"
            ) from exc
        module_name, _, attr = dotted.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ValueError(
                f"connector {self.name}: entry point module {module_name!r} "
                f"for capability {capability!r} does not import: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize connector init failures
            raise ValueError(
                f"connector {self.name}: entry point module {module_name!r} "
                f"for capability {capability!r} failed to import: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            resolved = getattr(module, attr)
        except AttributeError as exc:
            raise ValueError(
                f"connector {self.name}: entry point {dotted!r} for capability "
                f"{capability!r} does not resolve to an attribute of {module_name}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize connector init failures
            raise ValueError(
                f"connector {self.name}: entry point {dotted!r} for capability "
                f"{capability!r} failed during attribute resolution: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not callable(resolved):
            raise ValueError(
                f"connector {self.name}: entry point {dotted!r} for capability "
                f"{capability!r} is not callable"
            )
        return resolved


def validate_entry_points_resolve(manifest: ConnectorManifest) -> dict[str, object]:
    """Resolve every declared entry point; raise ValueError naming all failures.

    Returns ``{capability: callable}`` on success. Used as the "invalid
    manifest is refused with a clear error" gate at Horizon 2 registration.
    """
    resolved: dict[str, object] = {}
    failures: list[str] = []
    for capability in sorted(manifest.capabilities):
        try:
            resolved[capability] = manifest.resolve_entry_point(capability)
        except ValueError as exc:
            failures.append(str(exc))
    if failures:
        raise ValueError("; ".join(failures))
    return resolved


_MANIFESTS: dict[str, ConnectorManifest] = {}


def register_manifest(manifest: ConnectorManifest, *, replace: bool = False) -> ConnectorManifest:
    """Add a manifest to the process-wide registry. Refuses a silent overwrite."""
    if not replace and manifest.name in _MANIFESTS:
        raise ValueError(f"connector manifest already registered: {manifest.name}")
    _MANIFESTS[manifest.name] = manifest
    return manifest


def unregister_manifest(name: str) -> None:
    """Remove a manifest. Test-only seam — production connectors never unregister."""
    _MANIFESTS.pop(name, None)


def get_manifest(name: str) -> ConnectorManifest | None:
    return _MANIFESTS.get(name)


def require_manifest(name: str) -> ConnectorManifest:
    manifest = get_manifest(name)
    if manifest is None:
        raise ValueError(f"no connector manifest registered for {name!r}")
    return manifest


def all_manifests() -> dict[str, ConnectorManifest]:
    return dict(_MANIFESTS)


def secret_status(
    manifest: ConnectorManifest,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Report which required secrets exist as environment variables.

    Reports NAMES only — present/missing — and never a secret's value.
    ``required_secrets`` is a plain AND: every name must be present.
    ``required_secret_alternatives`` (when declared) is an OR of ANDs: at
    least one whole group must be present; a manifest with no alternatives
    behaves exactly as it always has (unchanged for Slack/Zulip).

    When at least one alternative group is fully satisfied, ``missing``
    reports only unmet names from ``required_secrets`` — the connector does
    not also need every OTHER alternative's secrets. When no group is
    satisfied, ``missing`` is the union of every unmet name across
    ``required_secrets`` and every alternative group, so a caller still sees
    every concrete env var name that could help; see
    ``describe_required_secrets`` for a human-readable rendering of the
    actual "one of X or Y" requirement.
    """
    source = os.environ if env is None else env

    if manifest.secret_status_hook:
        try:
            module_name, _, attr = manifest.secret_status_hook.partition(":")
            hook = getattr(importlib.import_module(module_name), attr)
            result = hook(source)
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("present"), list)
                or not all(isinstance(name, str) for name in result["present"])
                or not isinstance(result.get("missing"), list)
                or not all(isinstance(name, str) for name in result["missing"])
                or not isinstance(result.get("satisfied"), bool)
                or ("error" in result and not isinstance(result["error"], str))
                or ("error" not in result and result["satisfied"] == bool(result["missing"]))
            ):
                raise TypeError("invalid secret status hook result")
            return result
        except Exception as exc:  # noqa: BLE001 - isolate a broken connector hook
            # Do not expose exception text: a hook may include a secret value
            # in its error. Callers can report this connector as UNKNOWN while
            # continuing to inspect the rest of the registry.
            return {
                "present": [],
                "missing": [],
                "satisfied": False,
                "error": f"secret status hook failed ({type(exc).__name__})",
            }

    def _present(name: str) -> bool:
        return bool(source.get(name))

    base_present = sorted(name for name in manifest.required_secrets if _present(name))
    base_missing = sorted(name for name in manifest.required_secrets if not _present(name))

    if not manifest.required_secret_alternatives:
        return {"present": base_present, "missing": base_missing, "satisfied": not base_missing}

    satisfied_group = next(
        (
            group
            for group in manifest.required_secret_alternatives
            if all(_present(n) for n in group)
        ),
        None,
    )
    if satisfied_group is not None:
        # One whole alternative is enough; only base requirements can still
        # block, so the unused alternatives are never reported as missing.
        present = sorted(set(base_present) | satisfied_group)
        return {"present": present, "missing": base_missing, "satisfied": not base_missing}

    alt_names = {name for group in manifest.required_secret_alternatives for name in group}
    alt_present = sorted(name for name in alt_names if _present(name))
    alt_missing = sorted(name for name in alt_names if not _present(name))
    return {
        "present": sorted(set(base_present) | set(alt_present)),
        "missing": sorted(set(base_missing) | set(alt_missing)),
        "satisfied": False,
    }


def describe_required_secrets(manifest: ConnectorManifest) -> str:
    """Human-readable rendering of what ``secret_status``/``ensure_can_start`` require.

    ``required_secrets`` renders as a flat AND list, matching every existing
    connector's ``gate`` text byte-for-byte (Slack/Zulip are unaffected).
    ``required_secret_alternatives`` renders as ``one of (A and B) or C`` —
    the message ``ensure_can_start`` and ``system_doctor``'s per-connector
    gate use so a "neither mode configured" state names both options instead
    of implying every name must be set at once.
    """
    parts: list[str] = []
    if manifest.required_secrets:
        parts.append(f"required secrets: {', '.join(sorted(manifest.required_secrets))}")
    if manifest.required_secret_alternatives:
        options = []
        for group in sorted(manifest.required_secret_alternatives, key=sorted):
            names = sorted(group)
            options.append(f"({' and '.join(names)})" if len(names) > 1 else names[0])
        parts.append("one of " + " or ".join(options))
    return "; ".join(parts) if parts else "no required secrets"


class ConnectorSecretsMissingError(PermissionError):
    """Raised when a connector is asked to start without its named secrets."""


def ensure_can_start(manifest: ConnectorManifest) -> dict:
    """Refuse to start a connector whose named secrets are not present.

    This is the manifest-level, connector-agnostic half of "refuses to start
    without its named secrets" — every connector gets it for free by
    declaring ``required_secrets`` and/or ``required_secret_alternatives``.
    A connector's own runtime may (and, for Slack, already does) enforce the
    same thing again at its own entry points; this function never inspects
    or logs a secret's value.
    """
    status = secret_status(manifest)
    if not status["satisfied"]:
        if error := status.get("error"):
            raise ConnectorSecretsMissingError(f"connector {manifest.name!r} cannot start: {error}")
        detail = f"missing required secret(s) {', '.join(status['missing'])}"
        if manifest.required_secret_alternatives:
            detail = f"needs {describe_required_secrets(manifest)} (currently {detail})"
        raise ConnectorSecretsMissingError(f"connector {manifest.name!r} cannot start: {detail}")
    return status


# ---------------------------------------------------------------------------
# Slack — the reference implementation (task #1204).
#
# Capabilities describe Slack's real, dedicated code paths, not an idealized
# connector: inbound webhook ingress (signature-verified HTTP), outbound
# message posting, and in-thread chat replies. This is a different vocabulary
# than horizon2.CONNECTOR_PACKAGES["slack"] = {"read", "search"}, which is
# the generic Horizon 2 MCP-server scope catalog shared across all five
# packages (see module docstring) — Slack has no "search" capability in this
# checkout (docs/connector-activation.md), so it is deliberately absent here
# rather than fabricated to match that catalog.
#
# Both required secrets are already enforced at their real entry points:
# ``SlackConfig.validate()`` (slack_gateway.py) raises when
# SLACK_SIGNING_SECRET is unset, and ``send_message()`` (slack_outbound.py)
# raises when SLACK_BOT_TOKEN is unset. The manifest names them; it does not
# change that existing runtime behavior.
# ---------------------------------------------------------------------------
SLACK_MANIFEST = ConnectorManifest(
    name="slack",
    version="1.0.0",
    capabilities=frozenset({"ingress", "outbound", "chat"}),
    required_secrets=frozenset({"SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN"}),
    entry_points={
        "ingress": "unified_agent.slack_gateway:handle_http_request",
        "outbound": "unified_agent.slack_outbound:send_message",
        "chat": "unified_agent.slack_chat:maybe_reply",
    },
    default_enabled=False,
)
register_manifest(SLACK_MANIFEST)


# Zulip credentials are stored in owner-only ``zuliprc-desktop-*`` files under
# ``UA_HOME``. ``UA_ZULIP_SITE`` is a required explicit site allowlist, but
# contains no credential and must not appear in ``required_secrets``. The
# dedicated Zulip doctor check still reports zuliprc presence without exposing
# file contents. Registering the manifest is descriptive only: the polling
# worker and chat reply path remain disabled unless their existing runtime
# settings enable them.
ZULIP_MANIFEST = ConnectorManifest(
    name="zulip",
    version="1.0.0",
    capabilities=frozenset({"chat", "worker"}),
    required_secrets=frozenset(),
    entry_points={
        "chat": "unified_agent.zulip_chat:maybe_reply",
        "worker": "unified_agent.zulip_worker:ZulipPollingWorker",
    },
    default_enabled=False,
)
register_manifest(ZULIP_MANIFEST)


# GitHub App connector (task #1201) has run two outbound credential paths
# since task #1261: an actual GitHub App installation token (``app``, the
# default) or an operator user token standing in for one (``user``,
# `UA_GITHUB_TOKEN_MODE=user`) so the Approval Ledger can be dogfooded without
# a human clicking through GitHub's app-creation flow first. A
# ``ConnectorManifest`` is a structural declaration, not a live snapshot of
# whichever mode happens to be active, so this registers the *union* of what
# both modes actually do: `check_runs` (Checks API, app mode) and
# `commit_statuses` (`POST .../statuses/{sha}`, user mode) are both real,
# resolvable code paths.
#
# The two modes' secrets are declared under `required_secret_alternatives`,
# not `required_secrets` -- `required_secrets` is an unconditional AND, and
# the App path (`GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`) and the user-token
# path (`UA_GITHUB_USER_TOKEN`) are alternatives, never both needed at once.
# Declaring all three under `required_secrets` (the original #1264 shape)
# made every valid single-mode configuration read as missing/MISCONFIGURED --
# caught in PR #703 review. `required_secret_alternatives` is an OR of ANDs:
# `secret_status`/`ensure_can_start` treat the connector as satisfied once
# *either whole group* is present. This still cannot represent every real
# path -- `UA_GITHUB_USER_TOKEN_VAR` lets an operator point user mode at a
# differently-named env var, and `github_app.resolve_user_token` also accepts
# a bare `gh auth token` with no env var at all -- so a manifest reporting
# "missing" with the default names is not proof the connector cannot start;
# see `github_app.github_app_manifest()`'s docstring for that residual
# caveat, and it still returns the exact per-mode dict shape (two
# capabilities, one secret name) for its own callers. The connector stays
# off by default at two layers regardless of this manifest:
# `github_app.enabled()` gates on `UA_GITHUB_APP_ENABLED` (default
# unset/"0"), and `default_enabled=False` here.
GITHUB_APP_MANIFEST = ConnectorManifest(
    name="github-app",
    version="1.0.0",
    capabilities=frozenset({"poll", "check_runs", "commit_statuses", "evidence_comments"}),
    required_secret_alternatives=(
        frozenset({"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"}),
        frozenset({"UA_GITHUB_USER_TOKEN"}),
    ),
    # The alternatives above document the names; readiness itself depends on
    # UA_GITHUB_TOKEN_MODE, so the mode-aware hook decides.
    secret_status_hook="unified_agent.github_app:manifest_secret_status",
    entry_points={
        "poll": "unified_agent.pr_watch:run",
        "check_runs": "unified_agent.github_app:GitHubAppHttpTransport",
        "commit_statuses": "unified_agent.github_app:GitHubUserTokenTransport",
        "evidence_comments": "unified_agent.github_app:process_pr_event",
    },
    default_enabled=False,
    oauth_client=GITHUB_OAUTH,
)
register_manifest(GITHUB_APP_MANIFEST)


# Discord (task #1269, Epic G) already has real, dedicated code in
# `unified_agent.ts_worker` (task #770's "task-space" worker — the `ts` in
# the package name is task-space, not TypeScript; there is no separate
# TypeScript Discord implementation in this checkout) gated off by the
# absence of `UA_TS_DISCORD_TOKEN`. That code is pure outbound REST over
# `httpx` (`GET`/`POST /channels/{id}/messages`, no websocket, no gateway
# intents, no public ingress — see `ts_worker/transport.py`'s module
# docstring), so the two capabilities below name real, resolvable entry
# points rather than an aspirational connector shape:
#
# - `gateway` is `DiscordGateway` itself, the send/receive adapter
#   (`fetch_messages`/`send_message`). It takes an injectable
#   `TsBotTransport` in its constructor, so the conformance harness and
#   `tests/test_ts_worker.py`'s `StubDiscord` can exercise it against a fake
#   transport — no network, no token, no live gateway connection.
# - `worker` is `TaskSpaceWorker`, the polling/classify/dispatch loop
#   (`poll_discord`) that durably logs inbound messages and posts attributed
#   replies for `UA_TS_DISCORD_ALLOWED_USER_IDS`-authorized senders only.
#
# `required_secrets` names every environment variable
# `tools/doctor.py::_ts_worker_check`'s Discord gate already requires for a
# non-DORMANT, non-MISCONFIGURED posture: the bot token, the sender
# allowlist (without it a token is live but never actionable), and the
# channel list (without it there is nothing to poll). Credentials
# themselves stay parked (tasks #725-#727) — this manifest only declares
# names, never mints or reads a value. `default_enabled=False`, and the
# worker's own runtime gate (`ts_transport.discord_token() is None`)
# independently keeps the surface off until a token lands.
DISCORD_MANIFEST = ConnectorManifest(
    name="discord",
    version="1.0.0",
    capabilities=frozenset({"gateway", "worker"}),
    required_secrets=frozenset(
        {"UA_TS_DISCORD_TOKEN", "UA_TS_DISCORD_ALLOWED_USER_IDS", "UA_TS_DISCORD_CHANNELS"}
    ),
    # Same names, normalized like the worker (blank tokens, ":" lists).
    secret_status_hook="unified_agent.ts_worker.transport:discord_manifest_secret_status",
    entry_points={
        "gateway": "unified_agent.ts_worker.transport:DiscordGateway",
        "worker": "unified_agent.ts_worker.worker:TaskSpaceWorker",
    },
    default_enabled=False,
)
register_manifest(DISCORD_MANIFEST)

# GCP agent20 operations use ambient ``gcloud`` credentials rather than an
# environment token, so the manifest deliberately declares no named secrets.
# The approval-gated operation entry point describes mutating capability; it
# does not establish runtime health. ``gcp_Agent20_health`` proves health with a
# read-only inventory of the configured UA_GCP_WORKER instance. The package id
# intentionally matches the existing ``int-gcp-agent20`` integration catalog.
GCP_Agent20_MANIFEST = ConnectorManifest(
    name="int-gcp-agent20",
    version="1.0.0",
    capabilities=frozenset({"inventory", "operations", "worker_recovery"}),
    entry_points={
        "inventory": "unified_agent.tools.gcloud:gcloud_inventory",
        "operations": "unified_agent.tools.gcloud:gcloud_ops",
        "worker_recovery": "unified_agent.tools.cloud_recovery:cloud_worker_recover",
    },
    default_enabled=False,
)
register_manifest(GCP_Agent20_MANIFEST)

# Mobile projection/action routes are an authenticated iPhone-relay surface.
# The Watch does not call these routes; they do not imply a Watch consumer or
# push transport. Both handlers enforce the explicit opt-in in addition to the
# hub's ordinary auth/CSRF middleware.
MOBILE_WATCH_MANIFEST = ConnectorManifest(
    name="mobile-watch",
    version="1.0.0",
    capabilities=frozenset({"projection", "action"}),
    required_secrets=frozenset({"UA_MOBILE_WATCH_ENABLED"}),
    secret_status_hook="unified_agent.config:mobile_watch_manifest_secret_status",
    entry_points={
        "projection": "unified_agent.webui.routes:_mobile_projection",
        "action": "unified_agent.webui.routes:action_mobile",
    },
    default_enabled=False,
)
register_manifest(MOBILE_WATCH_MANIFEST)

# Cloud mirror PoC (task #1285, Epic G): a metadata-only, one-way shipper of
# an allowlisted subset of the local event_log (unified_agent.cloud_mirror)
# to a read-only remote receiver (unified_agent.cloud_mirror_receiver). No
# notes/message bodies/file contents/diffs/secrets ever leave the box --
# see cloud_mirror.py's module docstring and docs/cloud-mirror.md for the
# full allowlist and acceptance mapping. `required_secrets` is
# deliberately empty: the default sink (FileSink, local JSONL) needs no
# secret at all, while the optional HTTPS sink uses
# UA_CLOUD_MIRROR_INGEST_TOKEN. That credential is not required for a
# FileSink-only deployment.
# Gated off at two layers regardless of this manifest: `cloud_mirror.enabled()`
# reads UA_CLOUD_MIRROR_ENABLED (default unset/"0"), and `default_enabled=False`
# here. The receiver is not started by registering this manifest -- it is a
# separate deployable (see docs/cloud-mirror.md) never run by this process.
CLOUD_MIRROR_MANIFEST = ConnectorManifest(
    name="cloud-mirror",
    version="1.0.0",
    capabilities=frozenset({"ship", "receive"}),
    entry_points={
        "ship": "unified_agent.cloud_mirror:run_once",
        "receive": "unified_agent.cloud_mirror_receiver:create_app",
    },
    default_enabled=False,
)
register_manifest(CLOUD_MIRROR_MANIFEST)

# Scheduled signed evidence export (task #1266, Epic G/F3) writes the existing
# signed evidence export (audit/evidence_report.py + audit/evidence_signing.py,
# task #1267/F1) to a customer-controlled storage destination on a timer. Its
# one capability, "export", is a single real entry point:
# audit.evidence_export_sink.run_once, the same call the scheduler's systemd
# timer and `unified-agent evidence export` both invoke.
#
# Which destination (local path / GCS / S3) is active, and which secret name
# that implies, depends on UA_EVIDENCE_EXPORT_DESTINATION — a static
# required_secrets list would either demand all three destinations' settings
# at once or none of them, so this delegates to a mode-aware hook exactly like
# github-app's UA_GITHUB_TOKEN_MODE above. Off by default at two layers:
# evidence_export_sink.enabled() gates on UA_EVIDENCE_EXPORT_ENABLED (default
# unset/"0"), and default_enabled=False here.
EVIDENCE_EXPORT_MANIFEST = ConnectorManifest(
    name="evidence-export",
    version="1.0.0",
    capabilities=frozenset({"export"}),
    secret_status_hook="unified_agent.audit.evidence_export_sink:manifest_secret_status",
    entry_points={
        "export": "unified_agent.audit.evidence_export_sink:run_once",
    },
    default_enabled=False,
)
register_manifest(EVIDENCE_EXPORT_MANIFEST)

# Browser can produce contract-v1 focus-anchor records, and this pure
# receiver validates one against a real task before deriving a task reference.
# It does not persist anything, and no Browser client is currently wired to
# call it. Keep it manifest-only (no Horizon 2 package scopes or connector-local
# secret), non-registerable through Horizon 2, and explicitly off until a named,
# authenticated consumer and real transport exist.
BROWSER_PROJECTION_MANIFEST = ConnectorManifest(
    name="browser-projection",
    version="1.0.0",
    capabilities=frozenset({"projection"}),
    entry_points={
        "projection": "unified_agent.tools.projection:browser_derive_task_reference",
    },
    default_enabled=False,
    horizon2_registration_supported=False,
)
register_manifest(BROWSER_PROJECTION_MANIFEST)
