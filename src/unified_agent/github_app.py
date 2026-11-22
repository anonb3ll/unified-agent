"""Poll-first GitHub App connector slice (task #1201).

Polling is delegated to :mod:`pr_watch`; this module normalizes its events
and applies the existing policy, approval, and evidence primitives before a
GitHub App installation-token transport mutates GitHub.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.mcpserver.exceptions import ToolError

from . import config, connectors, events, independent_review, policy, pr_models, storage
from .audit import producer_sign
from .tools import approvals

LOGGER = logging.getLogger(__name__)

GITHUB_APP_CONNECTOR = "github-app"
CHECK_RUN_NAME = "Approval Ledger: awaiting human approval"
COMMIT_STATUS_CONTEXT = "Approval Ledger"
TOOL_NAME = "github_app.process_pr_event"
EVIDENCE_CHANNEL = "connector.github.evidence"
PENDING_CHANNEL = "connector.github.approval_pending"
DENIED_CHANNEL = "connector.github.approval_denied"
SUPERSEDED_CHANNEL = "connector.github.superseded"
CLOSED_UNAPPROVED_CHANNEL = "connector.github.closed_without_approval"
POST_MERGE_CHANNEL = "connector.github.approved_post_merge"
# Policy-authorized merges (task #1407). The authorization is its own signed
# ledger row; the outcome channel is a terminal ledger result distinct from
# human approval evidence (EVIDENCE_CHANNEL).
POLICY_AUTHORIZATION_CHANNEL = "connector.github.policy_authorization"
POLICY_AUTHORIZED_CHANNEL = "connector.github.policy_authorized"
POLICY_AUTHORIZATION_KIND = "policy_authorization"
EVIDENCE_CLASS_HUMAN = "human-approved"
EVIDENCE_CLASS_POLICY = "policy-authorized"
POLICY_AUTHORIZERS_VAR = "UA_POLICY_AUTHORIZERS"
POLICY_SELF_AUTHORIZATION_VAR = "UA_POLICY_AUTHORIZATION_ALLOW_SELF"
_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_POLICY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,79}$")
# Keep the legacy mapping for callers that index GITHUB_APP_MANIFEST. The
# registered, structural declaration lives separately in connectors.py, where
# system_doctor and the Connectors pane discover it.
GITHUB_APP_MANIFEST = {
    "name": GITHUB_APP_CONNECTOR,
    "version": connectors.GITHUB_APP_MANIFEST.version,
    "capabilities": ("poll", "check_runs", "evidence_comments"),
    "required_secrets": ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"),
    "default_enabled": False,
}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# --- Token-provider mode (task #1261) ---------------------------------------
#
# A real GitHub App requires a human to click through GitHub's app-creation
# flow, which blocks dogfooding the Approval Ledger end to end. This lets an
# operator user token stand in: the Checks API needs an App installation
# token, but commit statuses (`POST /repos/{o}/{r}/statuses/{sha}`) and issue
# comments both work with an ordinary token scoped `repo:status` plus
# issue/PR comment write. `UA_GITHUB_TOKEN_MODE` selects between them; the
# token *value* is never logged, put in an exception, or written into a
# manifest or evidence comment -- only the env var NAME it came from is ever
# surfaced, matching the `required_secrets` convention in
# docs/connectors.md.
TOKEN_MODE_APP = "app"
TOKEN_MODE_USER = "user"
DEFAULT_USER_TOKEN_ENV_VAR = "UA_GITHUB_USER_TOKEN"
GH_AUTH_TIMEOUT_SECONDS = 10

# Conclusions a commit status must fold into GitHub's `error` state rather
# than `failure`. `failure` means the Approval Ledger ran to a normal
# terminal outcome and it was a denial -- a clean, expected result callers
# should treat the same as any other rejected review. `error` is reserved for
# a check that could not reach a normal conclusion at all (aborted, timed
# out, superseded, or -- in our own approval vocabulary -- expired unconsumed
# or revoked after the fact): GitHub's own third-party status integrations
# draw this same line, and collapsing it into `failure` would make an
# infrastructure hiccup look identical to a human saying no.
_ERROR_CONCLUSIONS = frozenset(
    {
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "neutral",
        "skipped",
        "expired",
        "revoked",
    }
)


def token_mode(env: Mapping[str, str] | None = None) -> str:
    """The active outbound GitHub credential path. Defaults to the App path."""
    source = os.environ if env is None else env
    raw = (source.get("UA_GITHUB_TOKEN_MODE") or TOKEN_MODE_APP).strip().lower()
    if raw not in {TOKEN_MODE_APP, TOKEN_MODE_USER}:
        raise ValueError(f"invalid UA_GITHUB_TOKEN_MODE {raw!r}; expected app or user")
    return raw


def user_token_env_var(env: Mapping[str, str] | None = None) -> str:
    """Name of the env var holding the operator user token (configurable)."""
    source = os.environ if env is None else env
    name = (source.get("UA_GITHUB_USER_TOKEN_VAR") or DEFAULT_USER_TOKEN_ENV_VAR).strip()
    return name or DEFAULT_USER_TOKEN_ENV_VAR


def user_token_source(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = (source.get("UA_GITHUB_USER_TOKEN_SOURCE") or "legacy").strip().lower()
    if value not in {"legacy", "connector_oauth"}:
        raise ValueError("UA_GITHUB_USER_TOKEN_SOURCE must be legacy or connector_oauth")
    return value


def resolve_user_token(
    env: Mapping[str, str] | None = None,
    *,
    run_gh: Callable[..., Any] = subprocess.run,
) -> str:
    """Resolve the operator user token: the named env var, else `gh auth token`.

    Never returns, logs, or raises with the token value itself -- only the
    configured env var NAME appears in any error this raises.
    """
    source = os.environ if env is None else env
    if user_token_source(source) == "connector_oauth":
        from . import connector_oauth, edition

        metadata = config.current_auth_metadata() or {}
        return connector_oauth.access_token_for(
            "github",
            actor=config.verified_agent_identity() or "hub:github_app",
            purpose="github_app",
            org_id=edition.tenant_org_id(),
            device_node=metadata.get("device_node"),
        )
    var_name = user_token_env_var(source)
    token = (source.get(var_name) or "").strip()
    if token:
        LOGGER.warning("github_user_token_source=env:%r", var_name)
        return token
    try:
        proc = run_gh(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=GH_AUTH_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(
            f"no GitHub user token available: {var_name} is unset and `gh auth token` could not run"
        ) from exc
    candidate = (proc.stdout or "").strip()
    if proc.returncode != 0 or not candidate:
        raise PermissionError(
            f"no GitHub user token available: {var_name} is unset and `gh auth token` failed"
        )
    LOGGER.warning("github_user_token_source=gh_auth_token")
    return candidate


def manifest_secret_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Mode-aware secret readiness for the github-app connector manifest.

    Only the active ``UA_GITHUB_TOKEN_MODE``'s credentials count: App mode
    needs ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY``; user mode needs the
    env var named by ``UA_GITHUB_USER_TOKEN_VAR`` (default
    ``UA_GITHUB_USER_TOKEN``). The ``gh auth token`` fallback is not probed
    here (no subprocess in a status read): a user-mode host relying on it
    reads as missing its token var. App credentials satisfy the named-secret
    requirement, but provider readiness is UNKNOWN because only a caller of
    build_transport can supply the installation-token provider. An invalid mode reports
    ``UA_GITHUB_TOKEN_MODE`` itself as missing. Names only, and the same
    ``{present, missing, satisfied}`` shape as ``connectors.secret_status``.
    """
    source = os.environ if env is None else env
    if user_token_source(source) == "connector_oauth" and token_mode(source) == TOKEN_MODE_USER:
        from . import connector_oauth, edition

        name = "oauth:github:grant"
        try:
            connected = connector_oauth.grant_ready("github", org_id=edition.tenant_org_id())
        except Exception:
            connected = False
        return {
            "present": [name] if connected else [],
            "missing": [] if connected else [name],
            "satisfied": connected,
        }
    try:
        mode = token_mode(source)
    except ValueError:
        return {"present": [], "missing": ["UA_GITHUB_TOKEN_MODE"], "satisfied": False}
    if mode == TOKEN_MODE_APP:
        names = ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"]
    else:
        names = [user_token_env_var(source)]
    present = sorted(n for n in names if (source.get(n) or "").strip())
    missing = sorted(n for n in names if not (source.get(n) or "").strip())
    if mode == TOKEN_MODE_APP and not missing:
        return {
            "present": present,
            "missing": [],
            "satisfied": True,
            "error": "App credentials are present; installation token provider readiness is unknown",
        }
    return {"present": present, "missing": missing, "satisfied": not missing}


def _commit_status_state(status: str, conclusion: str | None) -> str:
    """Map a Checks-API-shaped (status, conclusion) pair to a commit-status state.

    Commit statuses only have four states (`pending`/`success`/`failure`/
    `error`) versus the Checks API's separate status+conclusion pair. See the
    `_ERROR_CONCLUSIONS` module comment for the failure-vs-error split.
    """
    if status in {"queued", "in_progress"} and conclusion is None:
        return "pending"
    if status == "completed":
        if conclusion == "success":
            return "success"
        if conclusion == "failure":
            return "failure"
        if conclusion in _ERROR_CONCLUSIONS:
            return "error"
    raise ValueError(
        f"cannot map check-run status={status!r} conclusion={conclusion!r} to a commit-status state"
    )


def _check_output(description: str) -> dict[str, str]:
    """Checks-API ``output`` carrying an outcome description override."""
    text = description[:140]
    return {"title": text, "summary": text}


def _status_description(status: str, conclusion: str | None) -> str:
    """Human-readable `description` for a commit status, capped at GitHub's 140 chars."""
    if status == "queued":
        text = "Awaiting human approval"
    elif status == "in_progress":
        text = "Approval granted; recording evidence"
    elif status == "completed" and conclusion == "success":
        text = "Human approval recorded"
    elif status == "completed" and conclusion == "failure":
        text = "Human approval denied"
    elif status == "completed" and conclusion == "neutral":
        text = "Merged without human approval"
    elif status == "completed" and conclusion == "skipped":
        text = "Closed without human approval"
    elif status == "completed" and conclusion == "stale":
        text = "Superseded by a newer pull request head"
    elif status == "completed" and conclusion in _ERROR_CONCLUSIONS:
        text = f"Approval Ledger check {conclusion.replace('_', ' ')}"
    else:
        text = ""
    return text[:140]


#: Per-request timeout override for outbound GitHub calls (seconds). The policy
#: publisher sets a short one while it holds the credential fence, so a slow
#: GitHub cannot keep hub credential changes waiting (PR #1302 review).
_REQUEST_TIMEOUT_S: ContextVar[float | None] = ContextVar("github_request_timeout", default=None)
DEFAULT_REQUEST_TIMEOUT_S = 30


@contextmanager
def request_timeout(seconds: float):
    token = _REQUEST_TIMEOUT_S.set(float(seconds))
    try:
        yield
    finally:
        _REQUEST_TIMEOUT_S.reset(token)


def current_request_timeout() -> float:
    return _REQUEST_TIMEOUT_S.get() or DEFAULT_REQUEST_TIMEOUT_S


#: Pages a reconcile lookup reads before it gives up proving a status absent.
RECONCILE_MAX_PAGES = 10
#: GitHub's check-runs-for-ref endpoint only covers a head's newest 1,000 check
#: suites; with more, a missing run proves nothing.
CHECK_SUITE_WINDOW = 1000


class ReconcileIncomplete(RuntimeError):
    """A reconcile lookup could not prove a status absent, so nothing may be posted."""


class GitHubAppTransport(Protocol):
    """Outbound GitHub seam for App mode and test doubles.

    User-token mode must use an actual ``GitHubUserTokenTransport`` from
    ``build_transport``: the commit-status lock and supersession guard
    deliberately use ``isinstance`` to distinguish that transport. A
    structural substitute would satisfy this Protocol but bypass them.
    """

    def create_check_run(self, **kwargs: Any) -> str: ...

    # Both lifecycle calls accept an optional ``description`` override for a
    # terminal outcome whose text is not the fixed human-approval vocabulary
    # (the policy-authorized class, task #1407).
    def update_check_run(self, **kwargs: Any) -> None: ...

    def create_evidence_comment(self, **kwargs: Any) -> str: ...

    def find_evidence_comment(self, **kwargs: Any) -> dict[str, Any] | None: ...

    # Read-only reconcile for a terminal status whose POST outcome is unknown
    # (PR #1302 review): whether a completed check with this conclusion and
    # description is already on the exact head. False only once absence is
    # proven; raises (``ReconcileIncomplete`` when the search cannot finish)
    # rather than guess. The App transport also offers
    # ``locate_completed_check`` (same arguments), which returns the matching
    # run's id so a lost-response run can be corrected in place.
    def find_completed_check(self, **kwargs: Any) -> bool: ...

    # Put one terminal hub-owned check on a head, creating or updating it so a
    # head never carries two (task #2320); returns the id to update it by.
    def publish_head_check(self, **kwargs: Any) -> str: ...


# --- GitHub rate-limit backoff (task #2237) ----------------------------------
#
# A 429, or a 403 that carries a rate-limit signal, records how long this
# credential must wait. Until then every send raises ``GitHubBackoff`` (the
# check sits on ``_open``, the one send chokepoint), and the poll and fast path
# stop their pass, so nothing is published while backing off. Unfinished work
# keeps its turn and retries after the window. A 403 without a signal is a real
# permission error and is raised unchanged.
#
# The deadline is kept twice: in this process on the monotonic clock, and in
# a shared state file on the wall clock for the other processes (poll worker,
# hub fast path). A file that cannot be written leaves the process-local
# deadline in force; one that cannot be read counts as a bounded backoff, not
# as clear. A stored deadline is renormalized on read so the wait it imposes
# never exceeds ``GITHUB_BACKOFF_MAX_S`` of elapsed time, even after the wall
# clock steps back or the file holds a far-future value.

#: Per-credential backoff deadlines (``$UA_HOME``), shared by the poll worker
#: and the hub's fast path, which run in different processes.
GITHUB_BACKOFF_FILE = "approval-ledger-github-backoff.json"
_GITHUB_BACKOFF_MAX_BYTES = 4096
#: The longest wait any header or stored value can impose; larger is capped.
GITHUB_BACKOFF_MAX_S = 3600
#: GitHub's documented wait for a rate limit that names no time; also the
#: wait an unreadable state file imposes (from its last write).
GITHUB_BACKOFF_DEFAULT_S = 60
_HEADER_DIGITS_RE = re.compile(r"[0-9]+")
_RATE_LIMIT_BODY_BYTES = 2048
# Stored timestamps outside this range are corrupt (rejected before float()).
_MAX_STORED_TIMESTAMP = 10**12
#: Process-local deadlines on the monotonic clock, by (state file, scope).
_LOCAL_BACKOFF: dict[tuple[str, str], float] = {}
#: When this process first failed to even stat a state file, by path.
_UNREADABLE_SINCE: dict[str, float] = {}
#: The stored entry each bounded deadline was derived from, and that bound on
#: the monotonic clock, by (state file, scope).
_NORMALIZED_ANCHORS: dict[tuple[str, str], tuple[tuple[float, float], float]] = {}
_BACKOFF_LOCAL_LOCK = threading.Lock()
_BACKOFF_FILE_LOCK = threading.Lock()


class GitHubBackoff(RuntimeError):
    """GitHub asked this credential to back off.

    ``sent`` is True only for the response that started the backoff; a call
    refused while backing off sent nothing, so it must not use up a turn.
    """

    def __init__(self, seconds: float, *, sent: bool) -> None:
        self.retry_after_s = max(1, math.ceil(seconds))
        self.sent = sent
        super().__init__(
            f"GitHub rate limit: backing off for {self.retry_after_s}s"
            + ("" if sent else "; no request was sent")
        )


def _wall_clock() -> float:
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _header_int(value: str | None) -> int | None:
    """A non-negative ASCII integer header; absurdly long values saturate."""
    value = (value or "").strip()
    if not _HEADER_DIGITS_RE.fullmatch(value):
        return None
    return int(value) if len(value) <= 12 else 10**12


def _retry_after_s(value: str | None, now: float) -> float | None:
    """Seconds named by a ``Retry-After`` (delta or HTTP-date); None when malformed."""
    if value is None:
        return None
    seconds = _header_int(value)
    if seconds is not None:
        return float(seconds)
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return when.timestamp() - now


def rate_limit_backoff_s(
    status: int, headers: Mapping[str, str] | Any, *, body: str = "", now: float
) -> float | None:
    """How long a 403/429 response asks the caller to wait, or None if it does not.

    Signals: any 429; a ``Retry-After`` header; ``x-ratelimit-remaining: 0``;
    or GitHub's "rate limit" error message. The wait is the longest of
    ``Retry-After`` and, when the quota is exhausted, ``x-ratelimit-reset``;
    without a usable value it is ``GITHUB_BACKOFF_DEFAULT_S``. Malformed values
    are ignored, and the result is clamped to [1 s, ``GITHUB_BACKOFF_MAX_S``].
    """
    if status not in (403, 429):
        return None
    headers = headers if headers is not None else {}
    retry_after = headers.get("Retry-After")
    exhausted = (headers.get("X-RateLimit-Remaining") or "").strip() == "0"
    if not (status == 429 or retry_after is not None or exhausted or "rate limit" in body.lower()):
        return None
    waits = [_retry_after_s(retry_after, now)]
    reset = _header_int(headers.get("X-RateLimit-Reset"))
    if exhausted and reset is not None:
        waits.append(reset - now)
    usable = [wait for wait in waits if wait is not None]
    wait = max(usable) if usable else GITHUB_BACKOFF_DEFAULT_S
    return float(min(max(wait, 1), GITHUB_BACKOFF_MAX_S))


def github_backoff_path():
    return config.data_home() / GITHUB_BACKOFF_FILE


class _BackoffStateUnreadable(Exception):
    """The shared backoff file exists but cannot be trusted."""


def _read_backoff_entries(path) -> dict[str, Any]:
    """The raw ``{scope: entry}`` map; missing file is empty, anything else bad raises."""
    try:
        if path.stat().st_size > _GITHUB_BACKOFF_MAX_BYTES:
            raise _BackoffStateUnreadable("oversized")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        raise _BackoffStateUnreadable(type(exc).__name__) from exc
    if not isinstance(record, dict):
        raise _BackoffStateUnreadable("not an object")
    return record


def _parse_entry(entry: Any) -> tuple[float, float] | None:
    """``(recorded_at, until)`` from a stored entry; None when malformed."""
    if not isinstance(entry, dict):
        return None
    values = []
    for field in ("at", "until"):
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, int) and abs(value) > _MAX_STORED_TIMESTAMP:
            return None
        if not math.isfinite(value) or abs(value) > _MAX_STORED_TIMESTAMP:
            return None
        values.append(float(value))
    return values[0], values[1]


def _normalized(at: float, until: float, now: float) -> tuple[float, float]:
    """Bound a stored deadline to ``GITHUB_BACKOFF_MAX_S`` of elapsed time from ``now``.

    A wall clock that stepped back since the entry was recorded re-anchors the
    original wait at ``now``; a deadline beyond ``now + cap`` becomes exactly
    that. Persisting the result makes the wait count down from here.
    """
    if at > now:
        until, at = now + min(max(until - at, 0.0), GITHUB_BACKOFF_MAX_S), now
    return at, min(until, now + GITHUB_BACKOFF_MAX_S)


@contextmanager
def _backoff_state_lock(path):
    """Serialize read-modify-write of the shared file across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _BACKOFF_FILE_LOCK, path.with_name(f".{path.name}.lock").open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _update_shared(path, scope: str, until_for: Callable[[float, float | None], float]) -> float:
    """Under the lock: prune, normalize, set ``scope`` to ``until_for(now, current)``.

    Every other scope keeps its own (normalized) deadline, and an unreadable
    file is replaced. Returns the deadline written. Raises ``OSError`` when the
    state cannot be written.
    """
    with _backoff_state_lock(path):
        now = _wall_clock()
        try:
            raw = _read_backoff_entries(path)
        except _BackoffStateUnreadable:
            raw = {}
        kept: dict[str, dict[str, float]] = {}
        for key, entry in raw.items():
            parsed = _parse_entry(entry)
            if isinstance(key, str) and parsed is not None:
                at, until = _normalized(*parsed, now)
                if until > now:
                    kept[key] = {"at": at, "until": until}
        current = kept.get(scope, {}).get("until")
        until = until_for(now, current)
        kept[scope] = {"at": now, "until": until}
        data = json.dumps(kept)
        if len(data.encode("utf-8")) > _GITHUB_BACKOFF_MAX_BYTES:
            data = json.dumps({scope: kept[scope]})
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        temp.write_text(data, encoding="utf-8")
        os.replace(temp, path)
        return until


def _hold_locally(path, scope: str, seconds: float) -> None:
    """Extend this process's own deadline; it never shortens and never outlives the cap."""
    key = (str(path), scope)
    deadline = _monotonic() + min(max(seconds, 0.0), GITHUB_BACKOFF_MAX_S)
    with _BACKOFF_LOCAL_LOCK:
        _LOCAL_BACKOFF[key] = max(_LOCAL_BACKOFF.get(key, 0.0), deadline)


def _unreadable_wait(path) -> float:
    """The bounded wait an untrustworthy state file imposes: until its write + default."""
    try:
        written = path.stat().st_mtime
    except FileNotFoundError:
        return 0.0
    except OSError:
        # Cannot even stat it: back off from when this process first saw that.
        with _BACKOFF_LOCAL_LOCK:
            since = _UNREADABLE_SINCE.setdefault(str(path), _monotonic())
        return max(GITHUB_BACKOFF_DEFAULT_S - (_monotonic() - since), 0.0)
    wait = min(written + GITHUB_BACKOFF_DEFAULT_S - _wall_clock(), GITHUB_BACKOFF_DEFAULT_S)
    return max(wait, 0.0)


def _shared_remaining(path, scope: str) -> float:
    try:
        entry = _read_backoff_entries(path).get(scope)
    except _BackoffStateUnreadable:
        return _unreadable_wait(path)
    if entry is None:
        return 0.0
    parsed = _parse_entry(entry)
    if parsed is None:
        return _unreadable_wait(path)
    now = _wall_clock()
    at, until = _normalized(*parsed, now)
    if (at, until) == parsed or until <= now:
        return max(until - now, 0.0)
    # The entry needs bounding. Anchor that bound once per stored entry on
    # the monotonic clock, so it counts down even if it cannot be written
    # back: a later read of the same entry must not re-bound from a new now.
    key = (str(path), scope)
    with _BACKOFF_LOCAL_LOCK:
        anchor = _NORMALIZED_ANCHORS.get(key)
        if anchor is not None and anchor[0] == parsed:
            return min(max(anchor[1] - _monotonic(), 0.0), float(GITHUB_BACKOFF_MAX_S))
        _NORMALIZED_ANCHORS[key] = (parsed, _monotonic() + (until - now))
    # Persist the bound so other processes count down too; if that fails,
    # this process still holds it.
    _hold_locally(path, scope, until - now)
    try:
        until = _update_shared(
            path, scope, lambda _now, current: until if current is None else current
        )
    except OSError:
        LOGGER.warning("could not renormalize the GitHub rate-limit backoff", exc_info=True)
    return max(until - now, 0.0)


def _backoff_remaining_for(scope: str) -> float:
    """Seconds left on ``scope``'s backoff, at most ``GITHUB_BACKOFF_MAX_S``."""
    path = github_backoff_path()
    with _BACKOFF_LOCAL_LOCK:
        local = _LOCAL_BACKOFF.get((str(path), scope), 0.0) - _monotonic()
    return min(max(local, _shared_remaining(path, scope), 0.0), float(GITHUB_BACKOFF_MAX_S))


def _record_backoff(scope: str, seconds: float) -> None:
    """Extend ``scope``'s deadline by ``seconds`` here and in the shared file.

    The process-local deadline is set first, so a failed write still backs
    this process off; the shared deadline only ever extends.
    """
    seconds = min(max(seconds, 0.0), GITHUB_BACKOFF_MAX_S)
    path = github_backoff_path()
    _hold_locally(path, scope, seconds)
    try:
        _update_shared(
            path,
            scope,
            lambda now, current: max(current or 0.0, now + seconds),
        )
    except OSError:
        LOGGER.warning("could not record the GitHub rate-limit backoff", exc_info=True)


def github_backoff_remaining(transport: object) -> float:
    """Seconds ``transport``'s credential must still wait; 0 for a test double."""
    scope = getattr(transport, "backoff_scope", None)
    if not callable(scope):
        return 0.0
    return _backoff_remaining_for(scope())


class _NoCrossHostRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects that would leave the configured API host.

    urlopen() auto-follows redirects and replays the Authorization header to
    the redirect target. api_base is caller-supplied, so a misconfigured or
    proxied base that 302s cross-host would leak the bearer token to that
    other host. GitHub's API does not cross-host redirect these endpoints;
    any attempt is treated as a configuration error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        from urllib.parse import urlsplit

        origin = urlsplit(req.full_url)
        target = urlsplit(newurl)
        if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
            raise PermissionError(
                f"refusing cross-host redirect from {req.full_url} to {newurl}: "
                "it would replay the Authorization header to a different host"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_REDIRECT_SAFE_OPENER = build_opener(_NoCrossHostRedirectHandler)


class _GitHubHttpTransportBase:
    """Shared stdlib HTTP plumbing for GitHub REST transports.

    Subclasses supply the credential via `_token()`; everything else --
    request construction, repo validation, and evidence-comment posting -- is
    identical whether the caller ends up authenticating as a GitHub App
    installation or an operator user token.
    """

    #: Which GitHub rate-limit bucket this transport's credential draws on.
    _BACKOFF_KIND = "github"

    def __init__(self, *, api_base: str = "https://api.github.com") -> None:
        self._api_base = api_base.rstrip("/")

    def _token(self) -> str:
        raise NotImplementedError

    def _credential_rejected(self, token: str) -> None:
        """Subclass hook; an API 401 is not a permission or rate-limit 403."""

    def backoff_scope(self) -> str:
        """The rate-limit backoff key (task #2237).

        GitHub limits an App per installation and a token per user. The hub
        holds one connector credential of each kind, so the kind and the API
        origin name the bucket.
        """
        return f"{self._BACKOFF_KIND}:{self._api_base}"

    def _refuse_while_backing_off(self) -> None:
        """Raise ``GitHubBackoff(sent=False)`` while this credential backs off (task #2237).

        ``_open`` checks on every send; the request entry points also check
        before resolving a credential, which may itself be slow or remote.
        """
        remaining = _backoff_remaining_for(self.backoff_scope())
        if remaining > 0:
            raise GitHubBackoff(remaining, sent=False)

    def _open(self, request: Request, token: str):
        # The one send chokepoint: a backoff recorded at any point before this
        # (another thread, another process, during credential resolution)
        # stops the request here.
        self._refuse_while_backing_off()
        try:
            return _REDIRECT_SAFE_OPENER.open(request, timeout=current_request_timeout())
        except HTTPError as exc:
            if exc.code == 401:
                self._credential_rejected(token)
            if exc.code in (403, 429):
                try:
                    body = exc.read(_RATE_LIMIT_BODY_BYTES).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - the headers still decide
                    body = ""
                wait = rate_limit_backoff_s(exc.code, exc.headers, body=body, now=_wall_clock())
                if wait is not None:
                    _record_backoff(self.backoff_scope(), wait)
                    raise GitHubBackoff(wait, sent=True) from exc
            raise

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._refuse_while_backing_off()
        token = self._token()
        if not isinstance(token, str) or not token.strip():
            raise PermissionError("GitHub credential is required")
        request = Request(
            f"{self._api_base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        with self._open(request, token) as response:  # noqa: S310 - fixed HTTPS default
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ToolError("GitHub returned a non-object response")
        return payload

    def _get_json(self, path: str) -> Any:
        """One read-only GET; the response is parsed, never trusted for shape."""
        self._refuse_while_backing_off()
        token = self._token()
        if not isinstance(token, str) or not token.strip():
            raise PermissionError("GitHub credential is required")
        request = Request(
            f"{self._api_base}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        with self._open(request, token) as response:  # noqa: S310 - fixed HTTPS default
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _repo(repo: str) -> str:
        if not _REPO_RE.fullmatch(repo):
            raise ValueError("repo must be an owner/name GitHub repository")
        return quote(repo, safe="/")

    def create_evidence_comment(self, *, repo: str, number: int, body: str) -> str:
        if number < 1:
            raise ValueError("pull request number must be positive")
        result = self._request(
            "POST", f"/repos/{self._repo(repo)}/issues/{number}/comments", {"body": body}
        )
        return str(result["id"])

    def find_comment_with_marker(
        self, *, repo: str, number: int, marker: str
    ) -> dict[str, Any] | None:
        """The newest-first scan for a comment whose body contains ``marker``.

        Used by the public-reply relay (task #1734) to reconcile a delivery
        whose outcome is unknown before anything is posted again.
        """
        if number < 1:
            raise ValueError("pull request number must be positive")
        if not marker:
            raise ValueError("marker must be non-empty")
        self._refuse_while_backing_off()
        token = self._token()
        if not token:
            raise PermissionError("GitHub credential is required")
        page = 1
        while True:
            request = Request(
                f"{self._api_base}/repos/{self._repo(repo)}/issues/{number}/comments"
                f"?per_page=100&page={page}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            )
            with self._open(request, token) as response:  # noqa: S310
                comments = json.loads(response.read().decode("utf-8"))
            if not isinstance(comments, list):
                raise ToolError("GitHub returned non-list issue comments")
            for comment in comments:
                if isinstance(comment, dict) and marker in str(comment.get("body") or ""):
                    return {"id": str(comment["id"])}
            if len(comments) < 100:
                return None
            page += 1

    def find_evidence_comment(
        self, *, repo: str, number: int, event_id: str, approval_id: int
    ) -> dict[str, Any] | None:
        """Find a prior comment after an uncertain POST outcome, newest first."""
        if number < 1:
            raise ValueError("pull request number must be positive")
        self._refuse_while_backing_off()
        token = self._token()
        if not token:
            raise PermissionError("GitHub credential is required")
        page = 1
        while True:
            request = Request(
                f"{self._api_base}/repos/{self._repo(repo)}/issues/{number}/comments"
                f"?per_page=100&page={page}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="GET",
            )
            with self._open(request, token) as response:  # noqa: S310
                comments = json.loads(response.read().decode("utf-8"))
            if not isinstance(comments, list):
                raise ToolError("GitHub returned non-list issue comments")
            for comment in comments:
                if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                    continue
                marker = re.search(
                    r"<!-- unified-agent github-evidence (\{.*?\}) -->", comment["body"]
                )
                if marker is None:
                    continue
                try:
                    evidence = json.loads(marker.group(1))
                except json.JSONDecodeError:
                    continue
                if (
                    evidence.get("event_id") == event_id
                    and evidence.get("approval_id") == approval_id
                ):
                    return {"id": str(comment["id"]), "evidence": evidence}
            if len(comments) < 100:
                return None
            page += 1


class GitHubAppHttpTransport(_GitHubHttpTransportBase):
    """Check-runs + issue-comments transport authenticated as a GitHub App installation."""

    _BACKOFF_KIND = "app"

    def __init__(
        self,
        installation_token: Callable[[], str],
        *,
        api_base: str = "https://api.github.com",
    ) -> None:
        super().__init__(api_base=api_base)
        self._installation_token = installation_token

    def _token(self) -> str:
        return self._installation_token()

    def _credential_rejected(self, token: str) -> None:
        invalidate = getattr(self._installation_token, "invalidate", None)
        if invalidate is not None:
            invalidate(token)

    def create_check_run(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str | None = None,
        details_url: str | None = None,
        description: str | None = None,
        external_id: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"name": name, "head_sha": head_sha, "status": status}
        if external_id:
            body["external_id"] = external_id
        if conclusion is not None:
            body["conclusion"] = conclusion
        if details_url:
            body["details_url"] = details_url
        if description:
            body["output"] = _check_output(description)
        result = self._request("POST", f"/repos/{self._repo(repo)}/check-runs", body)
        return str(result["id"])

    def update_check_run(
        self,
        *,
        repo: str,
        check_run_id: str,
        status: str,
        conclusion: str,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status, "conclusion": conclusion}
        if description:
            body["output"] = _check_output(description)
        self._request(
            "PATCH",
            f"/repos/{self._repo(repo)}/check-runs/{quote(str(check_run_id), safe='')}",
            body,
        )

    def status_matches(
        self,
        *,
        repo: str,
        check_run_id: str,
        status: str,
        conclusion: str | None,
        description: str | None = None,
    ) -> bool:
        """Whether the bound check run already shows this outcome (task #2234).

        One GET of the run by id. ``description`` is compared with the
        ``output.title`` this transport writes for an override.
        """
        run = self._get_json(
            f"/repos/{self._repo(repo)}/check-runs/{quote(str(check_run_id), safe='')}"
        )
        if not isinstance(run, dict):
            return False
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        return (
            run.get("status") == status
            and run.get("conclusion") == conclusion
            and (not description or output.get("title") == description[:140])
        )

    def find_completed_check(self, **kwargs: Any) -> bool:
        """Whether a completed check run with this outcome is on exactly ``head_sha``.

        ``locate_completed_check`` as a yes/no answer; same arguments.
        """
        return self.locate_completed_check(**kwargs) is not None

    def locate_completed_check(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        description: str | None = None,
        check_run_id: str | None = None,
        external_id: str | None = None,
    ) -> str | None:
        """The id of a completed check run with this outcome on exactly ``head_sha``.

        None once absence is proven. The id lets a caller correct the very
        run whose creation response was lost (PR #1302 review); it is ``""``
        when GitHub returned a matching run without one.

        A known ``check_run_id`` (a bound check) is read directly. Otherwise
        ``external_id`` is required and only runs carrying it count (round 4:
        the installation token cannot learn its own App id, so a matching
        title alone could be another App's). The runs named ``name`` (any
        status, ``filter=all``) are paged until ``total_count`` is covered,
        and only when the head has at most ``CHECK_SUITE_WINDOW`` check suites
        before and after the listing: the ref endpoint does not see older
        suites, so with more of them absence cannot be proven. Our run that is
        not a completed success with the expected title is ambiguous. Every case that cannot prove presence
        or absence raises ``ReconcileIncomplete``. ``description`` matches
        the ``output.title`` this transport writes for an override.
        """
        if not head_sha:
            raise ValueError("head_sha is required to look up a check run")
        title = description[:140] if description else None

        def matches(run: Any) -> bool:
            if not isinstance(run, dict):
                return False
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            return (
                run.get("head_sha") == head_sha
                and run.get("status") == "completed"
                and run.get("conclusion") == conclusion
                and (title is None or output.get("title") == title)
            )

        if not check_run_id and not external_id:
            # Without our own identity a run with a matching title could be
            # another App's (round 4): never take it as proof of our post.
            raise ValueError("an unbound check-run lookup needs the run's external_id")
        if check_run_id:
            run = self._get_json(
                f"/repos/{self._repo(repo)}/check-runs/{quote(str(check_run_id), safe='')}"
            )
            if not isinstance(run, dict):
                raise ToolError("GitHub returned a malformed check run")
            return str(check_run_id) if matches(run) else None
        for run in self._named_check_runs(repo, head_sha, name):
            # Only a run carrying our external_id is ours; a foreign run
            # with the same name and title proves nothing either way.
            if not isinstance(run, dict) or run.get("external_id") != external_id:
                continue
            if matches(run):
                return "" if run.get("id") is None else str(run["id"])
            if run.get("head_sha") == head_sha:
                raise ReconcileIncomplete(
                    f"check run {run.get('id')!r} for {external_id!r} is "
                    f"{run.get('status')!r}/{run.get('conclusion')!r}, not a completed success"
                )
        return None

    def _named_check_runs(self, repo: str, head_sha: str, name: str):
        """Every run named ``name`` on the head, any status, across pages.

        Reaching the end of this iterator proves the listing was complete; a
        listing that cannot be raises ``ReconcileIncomplete`` instead.
        """
        self._require_suite_window(repo, head_sha)
        seen = 0
        for page in range(1, RECONCILE_MAX_PAGES + 1):
            payload = self._get_json(
                f"/repos/{self._repo(repo)}/commits/{quote(head_sha, safe='')}/check-runs"
                f"?check_name={quote(name, safe='')}&filter=all&per_page=100&page={page}"
            )
            runs = payload.get("check_runs") if isinstance(payload, dict) else None
            total = payload.get("total_count") if isinstance(payload, dict) else None
            if not isinstance(runs, list) or isinstance(total, bool) or not isinstance(total, int):
                raise ToolError("GitHub returned malformed check runs")
            yield from runs
            seen += len(runs)
            if not runs or seen >= total:
                # Suites added while paging may have pushed a run out of view.
                self._require_suite_window(repo, head_sha)
                return
        raise ReconcileIncomplete(
            f"more than {RECONCILE_MAX_PAGES} pages of {name!r} check runs on {head_sha}"
        )

    def publish_head_check(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        description: str,
        external_id: str,
        details_url: str | None = None,
        check_run_id: str | None = None,
    ) -> str:
        """Put one completed check run on ``head_sha``, updating ours in place.

        The run to update is ``check_run_id`` when the caller knows it, else
        the run on this head that carries ``external_id``. A run is created
        only after the listing proved none exists, so a POST whose response
        was lost is adopted on the next call rather than duplicated. A lookup
        that cannot prove absence raises and creates nothing.
        """
        run_id = check_run_id
        if not run_id:
            for run in self._named_check_runs(repo, head_sha, name):
                if (
                    isinstance(run, dict)
                    and run.get("external_id") == external_id
                    and run.get("head_sha") == head_sha
                    and run.get("id") is not None
                ):
                    run_id = str(run["id"])
                    break
        if run_id:
            self.update_check_run(
                repo=repo,
                check_run_id=run_id,
                status="completed",
                conclusion=conclusion,
                description=description,
            )
            return run_id
        return self.create_check_run(
            repo=repo,
            head_sha=head_sha,
            name=name,
            status="completed",
            conclusion=conclusion,
            details_url=details_url,
            description=description,
            external_id=external_id,
        )

    def _require_suite_window(self, repo: str, head_sha: str) -> None:
        payload = self._get_json(
            f"/repos/{self._repo(repo)}/commits/{quote(head_sha, safe='')}/check-suites?per_page=1"
        )
        total = payload.get("total_count") if isinstance(payload, dict) else None
        if isinstance(total, bool) or not isinstance(total, int):
            raise ToolError("GitHub returned malformed check suites")
        if total > CHECK_SUITE_WINDOW:
            raise ReconcileIncomplete(
                f"{head_sha} has {total} check suites; the ref endpoint sees only the "
                f"newest {CHECK_SUITE_WINDOW}"
            )


class GitHubUserTokenTransport(_GitHubHttpTransportBase):
    """Commit-status transport for `UA_GITHUB_TOKEN_MODE=user` (task #1261).

    The Checks API needs a GitHub App installation token; this maps the same
    check-run lifecycle calls `process_pr_event` already issues onto
    `POST /repos/{o}/{r}/statuses/{sha}`, which an ordinary user token scoped
    `repo:status` can drive. Commit statuses have no "update by id" endpoint
    -- every transition is a fresh POST to the same sha -- so `check_run_id`
    here is an opaque `status:<head_sha>|<url-encoded details_url>` token this
    transport mints and later decodes itself (preserving the original
    `target_url` across the pending -> terminal transition), rather than a
    real GitHub-assigned id. Evidence comments (`create_evidence_comment`,
    inherited from the base class) are unchanged: issue comments already work
    with a user token.

    Commit statuses are keyed by `(sha, context)`, unlike check-runs, which
    are addressed by their own id -- so two distinct events sharing one
    head_sha (e.g. `ready_for_review` then `review_submitted` with no new
    commit) would otherwise collide on this transport's fixed
    `COMMIT_STATUS_CONTEXT`, letting an older event's completion silently
    overwrite a newer event's pending status. That ordering guard lives in
    `_bound_pending_check_run_id`, upstream of this transport, so it applies
    before any request is built here.
    """

    _CHECK_RUN_ID_PREFIX = "status:"
    _CHECK_RUN_ID_URL_SEP = "|"
    _BACKOFF_KIND = "user"

    def __init__(
        self,
        user_token: Callable[[], str],
        *,
        api_base: str = "https://api.github.com",
        brokered: bool = False,
    ) -> None:
        super().__init__(api_base=api_base)
        self._user_token = user_token
        self._brokered = brokered

    def _token(self) -> str:
        return self._user_token()

    def _credential_rejected(self, token: str) -> None:
        if not self._brokered:
            return
        from . import connector_oauth, edition

        metadata = config.current_auth_metadata() or {}
        connector_oauth.mark_invalid_credential(
            "github",
            actor=config.verified_agent_identity() or "hub:github_app",
            purpose="github_app",
            org_id=edition.tenant_org_id(),
            device_node=metadata.get("device_node"),
            rejected_token=token,
        )

    @classmethod
    def _decode_check_run_id(cls, check_run_id: str) -> tuple[str, str | None]:
        if not check_run_id.startswith(cls._CHECK_RUN_ID_PREFIX):
            raise ValueError(f"not a commit-status check_run_id: {check_run_id!r}")
        rest = check_run_id[len(cls._CHECK_RUN_ID_PREFIX) :]
        sha, _, encoded_url = rest.partition(cls._CHECK_RUN_ID_URL_SEP)
        if not sha:
            raise ValueError("commit-status check_run_id is missing its head_sha")
        return sha, (unquote(encoded_url) if encoded_url else None)

    def _post_status(
        self,
        *,
        repo: str,
        sha: str,
        status: str,
        conclusion: str | None,
        details_url: str | None,
        description: str | None = None,
        context: str = COMMIT_STATUS_CONTEXT,
    ) -> None:
        state = _commit_status_state(status, conclusion)
        body: dict[str, Any] = {"state": state, "context": context}
        if details_url:
            body["target_url"] = details_url
        description = (description or "")[:140] or _status_description(status, conclusion)
        if description:
            body["description"] = description
        self._request("POST", f"/repos/{self._repo(repo)}/statuses/{quote(sha, safe='')}", body)

    def create_check_run(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,  # noqa: ARG002 - Protocol parity; context is fixed, see class docstring
        status: str,
        conclusion: str | None = None,
        details_url: str | None = None,
        description: str | None = None,
        external_id: str | None = None,  # noqa: ARG002 - statuses have no external id
    ) -> str:
        if not head_sha:
            raise ValueError("head_sha is required to post a commit status")
        self._post_status(
            repo=repo,
            sha=head_sha,
            status=status,
            conclusion=conclusion,
            details_url=details_url,
            description=description,
        )
        return self._status_token(head_sha, details_url)

    def _status_token(self, head_sha: str, details_url: str | None) -> str:
        encoded_url = quote(details_url, safe="") if details_url else ""
        return f"{self._CHECK_RUN_ID_PREFIX}{head_sha}{self._CHECK_RUN_ID_URL_SEP}{encoded_url}"

    def publish_head_check(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        description: str,
        external_id: str,  # noqa: ARG002 - statuses are keyed by (sha, context)
        details_url: str | None = None,
        check_run_id: str | None = None,  # noqa: ARG002 - statuses are keyed by (sha, context)
    ) -> str:
        """Set this head's status under its own context ``name`` (task #2320).

        Unlike the ledger lifecycle, the context is the check's own name: a
        review verdict must never overwrite the ``Approval Ledger`` status.
        GitHub keys a status by ``(sha, context)``, so a repeat replaces it.
        """
        if not head_sha:
            raise ValueError("head_sha is required to post a commit status")
        self._post_status(
            repo=repo,
            sha=head_sha,
            status="completed",
            conclusion=conclusion,
            details_url=details_url,
            description=description,
            context=name,
        )
        return self._status_token(head_sha, details_url)

    def update_check_run(
        self,
        *,
        repo: str,
        check_run_id: str,
        status: str,
        conclusion: str,
        description: str | None = None,
    ) -> None:
        sha, details_url = self._decode_check_run_id(check_run_id)
        self._post_status(
            repo=repo,
            sha=sha,
            status=status,
            conclusion=conclusion,
            details_url=details_url,
            description=description,
        )

    @staticmethod
    def status_key(
        status: str, conclusion: str | None, description: str | None = None
    ) -> tuple[str, str]:
        """The ``(state, description)`` ``_post_status`` sends for this outcome."""
        state = _commit_status_state(status, conclusion)
        return state, (description or "")[:140] or _status_description(status, conclusion)

    def status_matches(
        self,
        *,
        repo: str,
        check_run_id: str,
        status: str,
        conclusion: str | None,
        description: str | None = None,
    ) -> bool:
        """Whether the newest ledger status on the head already says this (task #2234).

        One GET of the combined status, which carries the newest status per
        context. Compared on state and description, as ``_post_status`` sends.
        """
        sha, _details_url = self._decode_check_run_id(check_run_id)
        combined = self._get_json(
            f"/repos/{self._repo(repo)}/commits/{quote(sha, safe='')}/status?per_page=100"
        )
        statuses = combined.get("statuses") if isinstance(combined, dict) else None
        if not isinstance(statuses, list):
            return False
        for item in statuses:
            if isinstance(item, dict) and item.get("context") == COMMIT_STATUS_CONTEXT:
                return (item.get("state"), item.get("description")) == self.status_key(
                    status, conclusion, description
                )
        return False

    def find_completed_check(
        self,
        *,
        repo: str,
        head_sha: str,
        name: str,  # noqa: ARG002 - Protocol parity; context is fixed, see class docstring
        conclusion: str,
        description: str | None = None,
        check_run_id: str | None = None,  # noqa: ARG002 - statuses are keyed by sha
        external_id: str | None = None,  # noqa: ARG002 - statuses have no external id
    ) -> bool:
        """Whether this transport's terminal status is on exactly ``head_sha``.

        Matched on the fixed context, the mapped state and the exact
        description ``_post_status`` would have sent. Statuses are paged newest
        first to the last page; a history longer than ``RECONCILE_MAX_PAGES``
        raises ``ReconcileIncomplete`` instead of claiming absence.
        """
        if not head_sha:
            raise ValueError("head_sha is required to look up a commit status")
        state = _commit_status_state("completed", conclusion)
        text = (description or "")[:140] or _status_description("completed", conclusion)
        for page in range(1, RECONCILE_MAX_PAGES + 1):
            statuses = self._get_json(
                f"/repos/{self._repo(repo)}/commits/{quote(head_sha, safe='')}/statuses"
                f"?per_page=100&page={page}"
            )
            if not isinstance(statuses, list):
                raise ToolError("GitHub returned malformed commit statuses")
            if any(
                isinstance(item, dict)
                and item.get("context") == COMMIT_STATUS_CONTEXT
                and item.get("state") == state
                and item.get("description") == text
                for item in statuses
            ):
                return True
            if len(statuses) < 100:
                return False
        raise ReconcileIncomplete(
            f"more than {RECONCILE_MAX_PAGES} pages of commit statuses on {head_sha}"
        )


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubAppInstallationToken(_GitHubHttpTransportBase):
    """Mint and cache the installation token for one repository (task #2339).

    The callable a ``GitHubAppHttpTransport`` takes. It signs a short App JWT,
    finds the App's installation on ``repo``, and exchanges the JWT for a token
    scoped to that repository alone. The token is reused until shortly before
    GitHub's stated expiry, and dropped when the transport reports a 401.

    Minting shares the transport's ``app`` backoff bucket and its send
    chokepoint. Neither the key, the JWT nor a token appears in an exception.
    """

    _BACKOFF_KIND = "app"
    #: GitHub refuses an App JWT that lives longer than 10 minutes, and a
    #: backdated ``iat`` absorbs clock drift.
    _JWT_BACKDATE_S = 60
    _JWT_LIFETIME_S = 540
    #: A token is replaced this long before it expires, so a poll in progress
    #: is not caught by the expiry.
    _REFRESH_MARGIN_S = 300

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        repo: str,
        api_base: str = "https://api.github.com",
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(api_base=api_base)
        self._app_id = app_id
        self._target = self._repo(repo)
        self._clock = clock
        self._lock = threading.Lock()
        self._installation_id: str | None = None
        self._cached = ""
        self._refresh_at = 0.0
        try:
            key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
        except (TypeError, ValueError):
            key = None
        if not isinstance(key, rsa.RSAPrivateKey):
            # Raised outside the handler so the parser's error is never chained.
            raise PermissionError("GITHUB_APP_PRIVATE_KEY is not an unencrypted RSA PEM key")
        self._key = key

    def _token(self) -> str:
        """The App JWT, which authenticates only the two minting requests."""
        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iat": now - self._JWT_BACKDATE_S,
            "exp": now + self._JWT_LIFETIME_S,
            "iss": self._app_id,
        }
        signing_input = ".".join(
            _base64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
            for part in (header, claims)
        )
        signature = self._key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_base64url(signature)}"

    def _installation(self) -> str:
        if self._installation_id is None:
            payload = self._get_json(f"/repos/{self._target}/installation")
            found = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(found, bool) or not isinstance(found, int):
                raise ToolError("GitHub returned no App installation for this repository")
            self._installation_id = str(found)
        return self._installation_id

    def _mint(self) -> str:
        result = self._request(
            "POST",
            f"/app/installations/{self._installation()}/access_tokens",
            {"repositories": [self._target.rsplit("/", 1)[1]]},
        )
        token, expires_at = result.get("token"), result.get("expires_at")
        if not isinstance(token, str) or not token.strip() or not isinstance(expires_at, str):
            raise ToolError("GitHub returned an incomplete installation token response")
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise ToolError("GitHub returned an unreadable installation token expiry") from None
        self._cached, self._refresh_at = token, expires - self._REFRESH_MARGIN_S
        return token

    def __call__(self) -> str:
        with self._lock:
            if self._cached and self._clock() < self._refresh_at:
                return self._cached
            self._cached = ""
            return self._mint()

    def invalidate(self, token: str) -> None:
        """Forget ``token``, which GitHub rejected; a newer one stays."""
        with self._lock:
            if token == self._cached:
                self._cached = ""


def app_installation_token_provider(
    repo: str,
    *,
    env: Mapping[str, str] | None = None,
    api_base: str = "https://api.github.com",
) -> GitHubAppInstallationToken:
    """The provider for ``repo`` from ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY``.

    An env file holds one line per value, so a key may carry literal ``\\n``.
    Only the variable names ever appear in an error.
    """
    source = os.environ if env is None else env
    app_id = (source.get("GITHUB_APP_ID") or "").strip()
    private_key = (source.get("GITHUB_APP_PRIVATE_KEY") or "").strip().replace("\\n", "\n")
    missing = [
        name
        for name, value in (("GITHUB_APP_ID", app_id), ("GITHUB_APP_PRIVATE_KEY", private_key))
        if not value
    ]
    if missing:
        raise PermissionError(
            "UA_GITHUB_TOKEN_MODE=app requires an app_installation_token provider: "
            f"{', '.join(missing)} unset"
        )
    return GitHubAppInstallationToken(
        app_id=app_id, private_key=private_key, repo=repo, api_base=api_base
    )


def build_transport(
    *,
    env: Mapping[str, str] | None = None,
    app_installation_token: Callable[[], str] | None = None,
    run_gh: Callable[..., Any] = subprocess.run,
    api_base: str = "https://api.github.com",
    repo: str | None = None,
) -> GitHubAppTransport:
    """Select the outbound GitHub transport for the active `UA_GITHUB_TOKEN_MODE`.

    App mode (the default) uses `app_installation_token`, else mints from the
    App credentials for `repo`; with neither it refuses. User mode resolves
    an operator token via `resolve_user_token` (the configured env var, else
    `gh auth token`) and never needs one.
    """
    if token_mode(env) == TOKEN_MODE_USER:
        brokered = user_token_source(env) == "connector_oauth"
        if brokered and api_base.rstrip("/") != "https://api.github.com":
            raise ValueError("brokered GitHub grants require the fixed GitHub API origin")
        return GitHubUserTokenTransport(
            lambda: resolve_user_token(env, run_gh=run_gh), api_base=api_base, brokered=brokered
        )
    if app_installation_token is None:
        if repo is None:
            raise PermissionError(
                "UA_GITHUB_TOKEN_MODE=app requires an app_installation_token provider"
            )
        app_installation_token = app_installation_token_provider(repo, env=env, api_base=api_base)
    return GitHubAppHttpTransport(app_installation_token, api_base=api_base)


def github_app_manifest(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The connector manifest for the active `UA_GITHUB_TOKEN_MODE` (task #1261).

    App mode returns the #1201 shape. User mode swaps the Checks-API
    `check_runs` capability for `commit_statuses` -- the only lifecycle
    surface an ordinary user token can drive -- and names the configured
    active credential source instead of the App's installation secrets.
    `required_secrets` names a credential, never its value.

    This is a dynamic, mode-specific dict projection -- a different thing
    from the registered `connectors.GITHUB_APP_MANIFEST` (`connectors.ConnectorManifest`,
    task #1264), which is a single static declaration describing what the
    connector's code *can* do across both modes at once (it is what
    `system_doctor` and the Connectors pane read). This function keeps
    returning the narrower, mode-specific dict shape every existing caller
    already expects.

    Caveat for a future manifest validator: legacy user mode names only the
    env-var path. `resolve_user_token` also accepts a working `gh auth token`
    with no env var set at all (see its docstring), so
    `connectors.ensure_can_start`-style "is this secret present" checking
    would reject a configuration that actually works. Treat this manifest's
    `required_secrets` as "one sufficient path," not the only one.
    """
    if token_mode(env) == TOKEN_MODE_USER:
        return {
            "name": GITHUB_APP_CONNECTOR,
            "version": GITHUB_APP_MANIFEST["version"],
            "capabilities": ("poll", "commit_statuses", "evidence_comments"),
            "required_secrets": (
                "oauth:github:grant"
                if user_token_source(env) == "connector_oauth"
                else user_token_env_var(env),
            ),
            "default_enabled": False,
        }
    return {
        "name": GITHUB_APP_CONNECTOR,
        "version": GITHUB_APP_MANIFEST["version"],
        "capabilities": ("poll", "check_runs", "evidence_comments"),
        "required_secrets": ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"),
        "default_enabled": False,
    }


@dataclass(frozen=True)
class NormalizedPREvent:
    repo: str
    number: int
    event_type: str
    title: str
    url: str
    state: str
    head_sha: str
    base_branch: str
    head_branch: str
    author_login: str
    labels: tuple[str, ...] = ()
    occurred_at: str | None = None
    merged: bool = False
    review_state: str | None = None
    source: str = "pr_watch"
    event_id: str = ""
    independent_review: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.event_type not in {
            "opened",
            "synchronize",
            "ready_for_review",
            "review_submitted",
            "closed",
        }:
            raise ValueError(f"unsupported normalized PR event: {self.event_type!r}")
        if self.number < 1 or not self.repo or not self.head_sha:
            raise ValueError("normalized PR events require repo, positive number, and head_sha")
        if self.independent_review is not None:
            object.__setattr__(
                self,
                "independent_review",
                independent_review.validate(
                    self.independent_review,
                    repo=self.repo,
                    number=self.number,
                    head_sha=self.head_sha,
                ),
            )
        if not self.event_id:
            marker = {
                "repo": self.repo,
                "number": self.number,
                "event_type": self.event_type,
                "head_sha": self.head_sha,
                "occurred_at": self.occurred_at,
                "merged": self.merged,
            }
            digest = hashlib.sha256(
                json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "event_id", f"github-pr:{digest[:32]}")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "connector": GITHUB_APP_CONNECTOR,
            "source": self.source,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "state": self.state,
            "head_sha": self.head_sha,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "author_login": self.author_login,
            "labels": list(self.labels),
            "occurred_at": self.occurred_at,
            "merged": self.merged,
            "review_state": self.review_state,
        }
        # Keep legacy payload hashes and stored-event reconstruction unchanged
        # until an operator explicitly submits a new review-bearing proposal.
        if self.independent_review is not None:
            payload["independent_review"] = independent_review.validate(
                self.independent_review, repo=self.repo, number=self.number, head_sha=self.head_sha
            )
        return payload


def normalize_event(event: pr_models.PREvent) -> NormalizedPREvent:
    """Map legacy poller lifecycle names into the connector contract."""
    snapshot = event.snapshot
    event_type = {"reviewed": "review_submitted", "merged": "closed"}.get(event.kind, event.kind)
    occurred_at = {
        "reviewed": snapshot.last_review_at,
        "merged": snapshot.merged_at,
        "closed": snapshot.closed_at,
    }.get(event.kind)
    return NormalizedPREvent(
        repo=event.repo,
        number=snapshot.number,
        event_type=event_type,
        title=snapshot.title,
        url=snapshot.url,
        state=snapshot.state,
        head_sha=snapshot.head_sha,
        base_branch=snapshot.base_branch,
        head_branch=snapshot.head_branch,
        author_login=snapshot.author_login,
        labels=snapshot.labels,
        occurred_at=occurred_at,
        merged=event.kind == "merged" or bool(snapshot.merged_at),
        review_state=snapshot.last_review_state if event.kind == "reviewed" else None,
    )


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """The connector is inert unless explicitly enabled by the host."""
    source = os.environ if env is None else env
    return source.get("UA_GITHUB_APP_ENABLED", "0").strip() == "1"


def _approval_row(event: NormalizedPREvent) -> dict[str, Any] | None:
    payload = approvals.canonical_approval_payload(event.to_payload())
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM approvals WHERE tool=? AND payload=? ORDER BY id DESC LIMIT 1",
            (TOOL_NAME, payload),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = {key: row[key] for key in row.keys()}
    result["payload"] = json.loads(result["payload"] or "{}")
    return result


def _bound_pending_check_run_id(
    event: NormalizedPREvent,
    approval: Mapping[str, Any],
    candidate: str,
    *,
    shared_status_context: bool = False,
) -> str:
    """Accept a retry check only when its durable pending record matches exactly
    and, for shared commit statuses, no newer event on the same head_sha has
    superseded it.

    Distinct PR events can share one head_sha (e.g. `ready_for_review` then
    `review_submitted` with no new commit in between), each getting its own
    approval and its own pending check/status. A commit status is keyed by
    `(sha, context)` rather than by its own id, so posting a stale event's
    terminal status after a newer one has gone pending would silently
    overwrite the newer event's visible state with an unrelated "success"
    (task #1261 / Devin review). Scanning newest-first and rejecting as soon
    as we see a DIFFERENT approval's pending record for the same (repo,
    head_sha) before our own catches that case for user-token mode, ahead
    of any transport call. App check runs have independent IDs, so their
    approvals can complete in either order without overwriting each other.
    """
    approval_id = int(approval["id"])
    expected_event = event.to_payload()
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.event.repo')=? "
            "AND json_extract(payload, '$.event.head_sha')=? "
            "ORDER BY ulid DESC",
            (PENDING_CHANNEL, event.repo, event.head_sha),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        pending = json.loads(row["payload"] or "{}")
        pending_event = pending.get("event") or {}
        if (
            pending.get("approval_id") == approval_id
            and pending.get("check_run_id") == candidate
            and pending.get("event") == expected_event
        ):
            return candidate
        if shared_status_context and (
            pending.get("approval_id") != approval_id
            and pending_event.get("repo") == event.repo
            and pending_event.get("head_sha") == event.head_sha
        ):
            raise PermissionError(
                "a newer PR event on this head_sha has since gone pending; "
                "refusing to post a stale terminal check/status"
            )
    raise PermissionError(
        "pending GitHub check run is not bound to this normalized event and approval"
    )


def _link(kind: str, value: str | None) -> str:
    safe = quote((value or "unknown").strip() or "unknown", safe="._-@")
    return f"ua://{kind}/{safe}"


def _human_role(resolver: object) -> str:
    """The resolver's role, never their login: this comment is public (#1777)."""
    label = str(resolver or "").strip().lower()
    operator = {config.operator_label().lower(), *(a.lower() for a in config.operator_aliases())}
    return "operator" if label and label in operator else "human"


def _device_pseudonym(identity: str | None) -> str:
    """A stable per-hub pseudonym for the signing host, not its hostname (#1777).

    Keyed by the signer's bearer token, so the public comment cannot be
    matched back to a hostname by guessing; a token rotation changes it.
    """
    key = producer_sign.resolve_agent_bearer_token(identity or "") or ""
    if not key:
        # An unkeyed digest of a hostname can be reversed by guessing.
        return "unknown"
    host = config.current_source_host() or "unknown"
    digest = hmac.new(key.encode(), f"ua-device:{host}".encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


def _expected_review_summary(
    event: NormalizedPREvent, approval: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Derive public review claims only from the selected native decision.

    The producer signature authenticates an approval reference, not arbitrary
    comment fields. Both new publication and recovery must bind this optional
    claim independently, including after callbacks that can mutate the event.
    """
    stored_payload = approval.get("payload")
    if isinstance(stored_payload, dict):
        if stored_payload != event.to_payload():
            raise PermissionError("event is not bound to the native approval payload")
    elif event.independent_review is not None:
        raise PermissionError("independent-review event has no native approval payload")
    else:
        # Legacy direct comment callers contain no review claim. Actual
        # processing always selects the complete native payload above.
        return None
    if "independent_review" not in stored_payload:
        return None
    review = independent_review.validate(
        stored_payload["independent_review"],
        repo=event.repo,
        number=event.number,
        head_sha=event.head_sha,
    )
    # #1777: arbitrary names, models and refs remain in the native Ledger.
    return {
        "version": review["version"],
        "author_count": len(review["authors"]),
        "verdict": review["verdict"],
        "source": "human-attested-native-approval",
    }


def _validate_recovered_review_summary(
    event: NormalizedPREvent, approval: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    expected = _expected_review_summary(event, approval)
    if expected is None:
        matches = "independent_review" not in evidence
    else:
        actual = evidence.get("independent_review")
        matches = (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(
                type(actual[key]) is type(value) and actual[key] == value
                for key, value in expected.items()
            )
        )
    if not matches:
        raise PermissionError("existing GitHub review summary is not bound to native approval")


def _evidence_comment(
    event: NormalizedPREvent,
    approval: Mapping[str, Any],
    *,
    signed_event_id: str,
    signature: str,
    chain_head: str,
) -> str:
    identity = config.verified_agent_identity()
    evidence = {
        "agent": _link("agent", event.author_login or identity),
        "approval_id": int(approval["id"]),
        "approval_ref": _link("approval", str(approval["id"])),
        "chain_head": chain_head,
        "device": _link("device", _device_pseudonym(identity)),
        "event_id": event.event_id,
        "human": _link("human", _human_role(approval.get("resolved_by"))),
        "identity": identity,
        "kind": "approval",
        "pr_url": event.url,
        "head_sha": event.head_sha,
        "ref_id": str(approval["id"]),
        "report": "ua://control-center/evidence",
        "scheme": producer_sign.SCHEME_HMAC_SHA256_V1,
        "signature": signature,
        "signed_event_id": signed_event_id,
    }
    review_statement = ""
    review_summary = _expected_review_summary(event, approval)
    if review_summary is not None:
        evidence["independent_review"] = review_summary
        review_statement = "Human-attested independent review is recorded in the native approval payload referenced below.\n\n"
    return (
        f"Approval Ledger: human approval recorded for PR #{event.number}.\n\n"
        f"Approved head: `{event.head_sha}`.\n\n"
        f"{review_statement}"
        f"Evidence: {json.dumps(evidence, sort_keys=True, separators=(',', ':'))}\n"
        f"<!-- unified-agent github-evidence {json.dumps(evidence, sort_keys=True)} -->"
    )


def _signed_evidence(approval: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = config.verified_agent_identity()
    bearer = producer_sign.resolve_agent_bearer_token(identity or "")
    if not identity or not bearer:
        raise PermissionError("signed GitHub evidence requires a verified bearer identity")
    approval_id = int(approval["id"])
    signature = producer_sign.sign_producer_claim(
        identity=identity,
        kind="approval",
        ref_id=str(approval_id),
        task_id=None,
        bearer_token=bearer,
    )
    # A crash after stamping but before the GitHub comment must reuse the
    # original signed row when the consumed approval is resumed.
    conn = storage.connect()
    try:
        existing = conn.execute(
            "SELECT ulid FROM event_log WHERE channel=? "
            "AND originator=? AND json_extract(payload, '$.kind')='approval' "
            "AND json_extract(payload, '$.ref_id')=? ORDER BY ulid DESC LIMIT 1",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED, identity, str(approval_id)),
        ).fetchone()
    finally:
        conn.close()
    signed_event_id = (
        str(existing["ulid"])
        if existing
        else producer_sign.stamp_authenticated_producer_event(kind="approval", ref_id=approval_id)
    )
    if not signed_event_id:
        raise PermissionError("signed GitHub evidence could not be recorded")
    conn = storage.connect()
    try:
        signed_row = conn.execute(
            "SELECT payload, entry_hash FROM event_log WHERE ulid=?", (signed_event_id,)
        ).fetchone()
    finally:
        conn.close()
    if signed_row is None or not signed_row["entry_hash"]:
        raise PermissionError("signed GitHub evidence has no hash-chain entry")
    if not producer_sign.verify_producer_signature(json.loads(signed_row["payload"])):
        raise PermissionError("signed GitHub evidence signature is invalid")
    chain = events.verify_event_chain()
    if chain.get("status") != "valid":
        raise PermissionError("GitHub evidence chain is invalid")
    return signed_event_id, signature, str(chain["head_hash"])


# --- Settling a consumed approval (task #2234) ---------------------------------
#
# Every path that publishes an approved head (the resolution fast path, the
# open-PR poll, the closed-PR poll and the reconcile step) ends in
# ``_settle_approved``. It re-reads the pull request through the reader the
# caller binds with ``live_pr`` and decides, as a pure function of the approved
# head and base and the live head, base and state (``decide``), one of:
#
# - ``SETTLE_SUCCESS``: success, then approval evidence (final);
# - ``SETTLE_HELD``: ``stale``, not final (open at the approved head, on
#   another base; the PR may come back);
# - ``SETTLE_FINAL``: ``stale`` and superseded (final: the head moved, or the
#   PR closed on another base);
# - retry: the PR could not be read; nothing changes and the approval stays
#   unsettled.
#
# The decided status is set on every settle until a final record exists. A
# success is always posted, and evidence follows only that POST's own return.
# A ``stale`` post is skipped only when our last-posted record and the GitHub
# status both already match (compare-and-set); anything else posts. Markers
# are records, never a reason to skip a post. From the success POST to
# the evidence write, any failure withdraws the success before raising, so a
# success stays visible only once its evidence is written; if the withdrawal
# fails too, the next settle posts the decided status again.

SETTLE_SUCCESS = "success"
SETTLE_HELD = "stale_held"
SETTLE_FINAL = "stale_final"
#: Reasons recorded with a ``stale`` decision.
HEAD_MOVED = "head_moved"
BASE_CLOSED = "base_changed_closed"
BASE_OPEN = "base_changed_open"
#: Record of a ``SETTLE_HELD`` decision; never terminal.
RETARGETED_CHANNEL = "connector.github.retargeted"
RETARGETED_DESCRIPTION = "Pull request moved off the approved base; awaiting re-verification"
UNVERIFIED_DESCRIPTION = "Approval recorded; head not re-verified yet, retrying"


class LivePullRequestUnavailable(RuntimeError):
    """The pull request could not be re-read; the settle is retried later."""


#: ``reader(event)`` returns the live pull request (``head_sha``, ``base_branch``
#: and ``state``) or None. Bound by every caller that may publish an approval.
_LIVE_PR: ContextVar[Callable[[NormalizedPREvent], Any] | None] = ContextVar(
    "github_app_live_pr", default=None
)


@contextmanager
def live_pr(reader: Callable[[NormalizedPREvent], Any]):
    """Bind the live pull-request reader every approval settle re-reads through."""
    token = _LIVE_PR.set(reader)
    try:
        yield
    finally:
        _LIVE_PR.reset(token)


def decide(event: NormalizedPREvent, live: Any) -> tuple[str, str | None]:
    """The settle decision for ``event`` given the live pull request (pure).

    Returns ``(decision, reason)``. A close or merge at the approved head and
    base is a success: the human approved exactly that commit.
    """
    head = str(getattr(live, "head_sha", "") or "") if live is not None else ""
    if not head:
        raise LivePullRequestUnavailable(f"pull request #{event.number} could not be re-read")
    if head != event.head_sha:
        return SETTLE_FINAL, HEAD_MOVED
    if getattr(live, "base_branch", None) != event.base_branch:
        if getattr(live, "state", None) == "OPEN":
            return SETTLE_HELD, BASE_OPEN
        return SETTLE_FINAL, BASE_CLOSED
    return SETTLE_SUCCESS, None


def _read_live(event: NormalizedPREvent) -> Any:
    reader = _LIVE_PR.get()
    if reader is None:
        raise PermissionError(
            "publishing an approval needs a live pull-request reader (github_app.live_pr)"
        )
    return reader(event)


#: Our own record of each ledger status this process posted, after GitHub
#: accepted it (task #2234): the second key of the compare-and-set.
STATUS_POSTED_CHANNEL = "connector.github.status_posted"


def _last_posted(check_run_id: str) -> tuple[str, str] | None:
    """``(conclusion, description)`` of the last ledger status we posted for this check."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.check_run_id')=? ORDER BY ulid DESC LIMIT 1",
            (STATUS_POSTED_CHANNEL, check_run_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    return str(payload.get("conclusion") or ""), str(payload.get("description") or "")


def _post_and_record(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    *,
    check_run_id: str,
    conclusion: str,
    description: str | None = None,
) -> None:
    """POST one terminal ledger status, then record that GitHub accepted it.

    The record is written only after the POST returned. A POST that raised has
    an uncertain outcome (GitHub may have applied it), so it records an empty
    marker that invalidates any earlier record for this check (task #2242):
    otherwise a lagging read still showing that earlier status would match it
    and the compare-and-set would skip the correction. A failed record write
    is logged; the next compare-and-set then posts again unless the earlier
    record and a matching read both survive.
    """
    try:
        transport.update_check_run(
            repo=event.repo,
            check_run_id=check_run_id,
            status="completed",
            conclusion=conclusion,
            **({"description": description} if description else {}),
        )
    except BaseException:
        _record_posted(event, check_run_id, conclusion="", description="", uncertain=True)
        raise
    _record_posted(event, check_run_id, conclusion=conclusion, description=description or "")


def _record_posted(
    event: NormalizedPREvent,
    check_run_id: str,
    *,
    conclusion: str,
    description: str,
    uncertain: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "event_id": event.event_id,
        "check_run_id": check_run_id,
        "conclusion": conclusion,
        "description": description,
    }
    if uncertain:
        payload["uncertain"] = True
    try:
        events.record_event(
            STATUS_POSTED_CHANNEL, payload=payload, originator=config.verified_agent_identity()
        )
    except Exception:  # noqa: BLE001 - see _post_and_record
        LOGGER.warning("could not record an Approval Ledger status post", exc_info=True)


def _post_decided(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    *,
    check_run_id: str,
    conclusion: str,
    description: str | None = None,
) -> bool:
    """Set a decided terminal status, compare-and-set for non-success only.

    A success is always posted: evidence follows only a success POST that
    returned in the same settle, never a read. A ``stale`` decision is skipped
    only when both our own last-posted record for this check and the current
    GitHub status (``status_matches``) already say exactly this, so a held
    head re-settled every poll adds no status (GitHub keeps at most 1000 per
    commit and context). No record, a different record, a different or
    failed read, or a transport without the read: post. Returns whether it
    posted.
    """
    if conclusion != "success" and _last_posted(check_run_id) == (conclusion, description or ""):
        probe = getattr(transport, "status_matches", None)
        if probe is not None:
            try:
                if probe(
                    repo=event.repo,
                    check_run_id=check_run_id,
                    status="completed",
                    conclusion=conclusion,
                    **({"description": description} if description else {}),
                ):
                    return False
            except Exception:  # noqa: BLE001 - unknown current status: post
                LOGGER.info(
                    "Approval Ledger status read failed for PR #%s; posting",
                    event.number,
                    exc_info=True,
                )
    _post_and_record(
        event,
        transport,
        check_run_id=check_run_id,
        conclusion=conclusion,
        description=description,
    )
    return True


def _publish_stale(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    approval: Mapping[str, Any],
    decision: str,
    reason: str | None,
    *,
    check_run_id: str,
    comment_id: str | None,
    live_state: str | None,
) -> dict[str, Any]:
    """Post ``stale`` for the approved head and record the decision.

    ``SETTLE_FINAL`` records superseded (terminal); ``SETTLE_HELD`` records the
    non-terminal retargeted marker. The post is unconditional: a status left
    at success by an earlier attempt is corrected here. The evidence comment,
    if posted, names the approved head and stays true.
    """
    held = decision == SETTLE_HELD
    _post_decided(
        event,
        transport,
        check_run_id=check_run_id,
        conclusion="stale",
        description=RETARGETED_DESCRIPTION if held else None,
    )
    events.record_event(
        RETARGETED_CHANNEL if held else SUPERSEDED_CHANNEL,
        payload={
            "event_id": event.event_id,
            "approval_id": int(approval["id"]),
            "outcome": "retargeted" if held else "superseded",
            "reason": reason,
        },
        originator=config.verified_agent_identity(),
        dedupe_key=f"github-app:{'retargeted' if held else 'superseded'}:{event.event_id}",
    )
    return {
        "status": "superseded",
        "final": not held,
        "reason": reason,
        "event_id": event.event_id,
        "approval_id": int(approval["id"]),
        "check_run_id": check_run_id,
        "comment_id": comment_id,
        "live_state": live_state,
    }


def _withdraw_unverified_success(
    event: NormalizedPREvent, transport: GitHubAppTransport, check_run_id: str
) -> None:
    """Replace a success that may be public but is not settled with a non-success.

    ``action_required`` (a commit-status ``error``) keeps the head unmergeable.
    A failure here is logged; the caller re-raises the original error, and the
    next settle posts the decided status again.
    """
    try:
        _post_and_record(
            event,
            transport,
            check_run_id=check_run_id,
            conclusion="action_required",
            description=UNVERIFIED_DESCRIPTION,
        )
    except Exception:  # noqa: BLE001 - the original error is what the caller raises
        LOGGER.warning(
            "could not withdraw an unverified Approval Ledger success for PR #%s",
            event.number,
            exc_info=True,
        )


def _settle_approved(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    approval: Mapping[str, Any],
    *,
    check_run_id: str,
    publish_comment: Callable[[], tuple[str, str, str]],
) -> dict[str, Any]:
    """The one settle path for a consumed approval (see the section comment).

    ``publish_comment`` finds or posts the signed evidence comment and returns
    ``(comment_id, signed_event_id, chain_head)``; it runs only on a success
    decision.
    """
    _expected_review_summary(event, approval)
    live = _read_live(event)
    decision, reason = decide(event, live)
    state = getattr(live, "state", None)
    if decision != SETTLE_SUCCESS:
        return _publish_stale(
            event,
            transport,
            approval,
            decision,
            reason,
            check_run_id=check_run_id,
            comment_id=None,
            live_state=state,
        )
    comment_id, signed_event_id, chain_head = publish_comment()
    # Decide again immediately before the success POST: the comment may have
    # taken long enough for the PR to move.
    live = _read_live(event)
    decision, reason = decide(event, live)
    state = getattr(live, "state", None)
    if decision != SETTLE_SUCCESS:
        return _publish_stale(
            event,
            transport,
            approval,
            decision,
            reason,
            check_run_id=check_run_id,
            comment_id=comment_id,
            live_state=state,
        )
    try:
        _post_decided(event, transport, check_run_id=check_run_id, conclusion="success")
        live = _read_live(event)
        decision, reason = decide(event, live)
        state = getattr(live, "state", None)
        if decision != SETTLE_SUCCESS:
            # Moved between the decision and the POST: correct the success
            # before anything records this head as settled.
            return _publish_stale(
                event,
                transport,
                approval,
                decision,
                reason,
                check_run_id=check_run_id,
                comment_id=comment_id,
                live_state=state,
            )
        _expected_review_summary(event, approval)
        evidence_id = events.record_event(
            EVIDENCE_CHANNEL,
            payload={
                "event_id": event.event_id,
                "approval_id": int(approval["id"]),
                "check_run_id": check_run_id,
                "comment_id": comment_id,
                "signed_event_id": signed_event_id,
                "chain_head": chain_head,
            },
            originator=config.verified_agent_identity(),
            dedupe_key=f"github-app:evidence:{event.event_id}",
        )
    except Exception:
        _withdraw_unverified_success(event, transport, check_run_id)
        raise
    return {
        "status": "approved",
        "event_id": event.event_id,
        "approval_id": int(approval["id"]),
        "check_run_id": check_run_id,
        "comment_id": comment_id,
        "evidence_id": evidence_id,
        "signed_event_id": signed_event_id,
        "chain_head": chain_head,
        "live_state": state,
    }


@contextmanager
def _user_status_lock(event: NormalizedPREvent):
    """Serialize shared-context status publication across hub processes.

    The lock covers both the pending GitHub POST and a terminal POST. A
    separate `process_pr_event` invocation cannot pass the pending-record
    ordering check between those side effects and overwrite a newer pending
    status. The kernel releases the advisory lock if a process dies.
    """
    lock_dir = config.data_home() / "github-status-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(f"{event.repo}\0{event.head_sha}".encode()).hexdigest()
    lock_path = lock_dir / f"{key}.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def process_pr_event(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    *,
    policy_config: policy.PolicyConfig | None = None,
    pending_check_run_id: str | None = None,
) -> dict[str, Any]:
    if not enabled():
        return {"status": "disabled", "event_id": event.event_id}
    if isinstance(transport, GitHubUserTokenTransport):
        with _user_status_lock(event):
            return _process_pr_event(event, transport, policy_config, pending_check_run_id)
    return _process_pr_event(event, transport, policy_config, pending_check_run_id)


def _process_pr_event(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    policy_config: policy.PolicyConfig | None,
    pending_check_run_id: str | None,
) -> dict[str, Any]:
    """Process one normalized poll event through policy and GitHub side effects.

    The caller owns polling and cursor advancement. This function is deliberately
    inert by default and only reaches GitHub after a consumed approval pass.
    """
    payload = event.to_payload()
    try:
        policy.evaluate_policy(
            TOOL_NAME,
            payload=payload,
            originator=event.author_login or None,
            policy_config=policy_config,
        )
    except approvals.ApprovalPendingError as exc:
        check_run_id = transport.create_check_run(
            repo=event.repo,
            head_sha=event.head_sha,
            name=CHECK_RUN_NAME,
            status="queued",
            details_url=event.url,
        )
        events.record_event(
            PENDING_CHANNEL,
            payload={
                "event": payload,
                "approval_id": exc.approval_id,
                "check_run_id": check_run_id,
            },
            originator=config.verified_agent_identity(),
            dedupe_key=f"github-app:pending:{event.event_id}:{exc.approval_id}",
        )
        return {
            "status": "pending_approval",
            "event_id": event.event_id,
            "approval_id": exc.approval_id,
            "check_run_id": check_run_id,
        }
    except policy.PolicyViolation:
        # A denial is a terminal approval outcome. Check the exact stored
        # approval before posting a failure; other policy violations must not
        # be mistaken for a human denial.
        approval = _approval_row(event)
        if approval is None or approval.get("status") != "denied" or pending_check_run_id is None:
            raise
        check_run_id = _bound_pending_check_run_id(
            event,
            approval,
            pending_check_run_id,
            shared_status_context=isinstance(transport, GitHubUserTokenTransport),
        )
        transport.update_check_run(
            repo=event.repo,
            check_run_id=check_run_id,
            status="completed",
            conclusion="failure",
        )
        events.record_event(
            DENIED_CHANNEL,
            payload={"event_id": event.event_id, "approval_id": int(approval["id"])},
            originator=config.verified_agent_identity(),
            dedupe_key=f"github-app:denied:{event.event_id}",
        )
        return {
            "status": "denied",
            "event_id": event.event_id,
            "approval_id": int(approval["id"]),
            "check_run_id": check_run_id,
        }

    approval = _approval_row(event)
    if approval is None or approval.get("status") != "consumed":
        raise PermissionError("GitHub App processing requires a consumed approval")

    if pending_check_run_id is None:
        if isinstance(transport, GitHubUserTokenTransport):
            raise PermissionError("user-token mode requires the bound pending status id")
        check_run_id = transport.create_check_run(
            repo=event.repo,
            head_sha=event.head_sha,
            name=CHECK_RUN_NAME,
            status="in_progress",
            details_url=event.url,
        )
    else:
        check_run_id = _bound_pending_check_run_id(
            event,
            approval,
            pending_check_run_id,
            shared_status_context=isinstance(transport, GitHubUserTokenTransport),
        )

    def publish_comment() -> tuple[str, str, str]:
        signed_event_id, signature, chain_head = _signed_evidence(approval)
        comment = _evidence_comment(
            event,
            approval,
            signed_event_id=signed_event_id,
            signature=signature,
            chain_head=chain_head,
        )
        comment_id = transport.create_evidence_comment(
            repo=event.repo,
            number=event.number,
            body=comment,
        )
        return comment_id, signed_event_id, chain_head

    return _settle_approved(
        event,
        transport,
        approval,
        check_run_id=check_run_id,
        publish_comment=publish_comment,
    )


def resume_consumed_pr_event(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    *,
    pending_check_run_id: str,
) -> dict[str, Any]:
    """Recover a consumed approval without spending another pass or duplicating a comment."""
    if not enabled():
        return {"status": "disabled", "event_id": event.event_id}
    if isinstance(transport, GitHubUserTokenTransport):
        with _user_status_lock(event):
            return _resume_consumed_pr_event(event, transport, pending_check_run_id)
    return _resume_consumed_pr_event(event, transport, pending_check_run_id)


def _resume_consumed_pr_event(
    event: NormalizedPREvent,
    transport: GitHubAppTransport,
    pending_check_run_id: str,
) -> dict[str, Any]:
    approval = _approval_row(event)
    if approval is None or approval["status"] != "consumed":
        raise PermissionError("GitHub recovery requires a consumed approval")
    check_run_id = _bound_pending_check_run_id(
        event,
        approval,
        pending_check_run_id,
        shared_status_context=isinstance(transport, GitHubUserTokenTransport),
    )

    def publish_comment() -> tuple[str, str, str]:
        found = transport.find_evidence_comment(
            repo=event.repo,
            number=event.number,
            event_id=event.event_id,
            approval_id=int(approval["id"]),
        )
        _expected_review_summary(event, approval)
        if found is None:
            signed_event_id, signature, chain_head = _signed_evidence(approval)
            comment_id = transport.create_evidence_comment(
                repo=event.repo,
                number=event.number,
                body=_evidence_comment(
                    event,
                    approval,
                    signed_event_id=signed_event_id,
                    signature=signature,
                    chain_head=chain_head,
                ),
            )
            return comment_id, signed_event_id, chain_head
        evidence = found["evidence"]
        _validate_recovered_review_summary(event, approval, evidence)
        if not producer_sign.verify_producer_signature(evidence):
            raise PermissionError("existing GitHub evidence comment has an invalid signature")
        signed_event_id = str(evidence["signed_event_id"])
        chain_head = str(evidence["chain_head"])
        conn = storage.connect()
        try:
            signed_row = conn.execute(
                "SELECT payload FROM event_log WHERE ulid=? AND channel=?",
                (signed_event_id, producer_sign.CHANNEL_PRODUCER_AUTHENTICATED),
            ).fetchone()
            anchored = conn.execute(
                "SELECT 1 FROM event_log WHERE entry_hash=?", (chain_head,)
            ).fetchone()
        finally:
            conn.close()
        if (
            signed_row is None
            or not anchored
            or json.loads(signed_row["payload"]).get("ref_id") != str(approval["id"])
            or events.verify_event_chain().get("status") != "valid"
        ):
            raise PermissionError("existing GitHub evidence comment is not anchored to approval")
        return str(found["id"]), signed_event_id, chain_head

    return _settle_approved(
        event,
        transport,
        approval,
        check_run_id=check_run_id,
        publish_comment=publish_comment,
    )


# --- Policy-authorized merges (task #1407) ------------------------------------
#
# The operator decided (2026-10-02, option b) that a merge covered by a
# recorded standing authorization -- the deploy-steward policy, or an
# operator go given in conversation and cited by reference -- is its own
# evidence class, distinct from a human approval, instead of reading as
# "Merged without human approval".
#
# The authorization is a signed row on the hash-chained event log, not an
# approvals row: approvals are human decisions with a pending/approved state
# machine and queue, and a policy citation must never surface there or be
# counted as one. The row is bound to one (repo, PR, head SHA), names the
# policy by reference (never free-form chat text), and is HMAC-signed with the
# recording agent's own bearer exactly like producer evidence, so the ledger
# re-verifies the recorder rather than trusting the originator column.
#
# Only verified agent identities listed in UA_POLICY_AUTHORIZERS may record
# one (default: nobody). Separation of duties: the hub task the merge belongs
# to must be claimed by a different agent unless
# UA_POLICY_AUTHORIZATION_ALLOW_SELF=1. Red-tier scope is checked again by the
# ledger at merge time against the PR's changed files; a policy authorization
# never satisfies it.
#
# Recording is a pre-merge step for live work: the task must still be open
# (not done or cancelled) and still carry its worktree branch. Completing a
# task releases its worktree and clears ``tasks.branch``, and a closer is
# stamped into ``claimed_by``, so a terminal row no longer says which branch
# or which author the merge belongs to. The record signs both at recording
# time, so a task closed after its authorization was recorded keeps it.


def policy_authorizers(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Verified agent labels allowed to record a policy authorization.

    ``UA_POLICY_AUTHORIZERS`` (comma list), default empty. Human labels are
    dropped: a human records a human approval, never a policy citation, so
    the two classes cannot be conflated. Unknown agent labels are dropped.
    """
    source = os.environ if env is None else env
    raw = source.get(POLICY_AUTHORIZERS_VAR) or ""
    labels = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(
        label
        for label in labels
        if config.is_known_agent_label(label) and config.actor_kind_for_identity(label) != "human"
    )


def self_authorization_allowed(env: Mapping[str, str] | None = None) -> bool:
    """Whether an authorizer may cite a policy for a task it claims itself."""
    source = os.environ if env is None else env
    return (source.get(POLICY_SELF_AUTHORIZATION_VAR) or "").strip() == "1"


# Mirrors tools.tasks.TERMINAL_STATUSES without importing the task tool module.
_TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled"})


@dataclass(frozen=True)
class _TaskBinding:
    """The live task row a policy authorization is bound to."""

    status: str
    claimant: str | None
    branch: str | None


def _task_binding(conn: Any, task_id: int) -> _TaskBinding | None:
    """The task's status, claimant and worktree branch, or None if it does not exist.

    Read on the caller's connection, so a caller holding the write lock sees
    the row as it stands when its own append commits.
    """
    row = conn.execute(
        "SELECT status, claimed_by, branch FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    claimant = str(row["claimed_by"] or "").strip()
    branch = str(row["branch"] or "").strip()
    return _TaskBinding(
        status=str(row["status"] or "").strip(),
        claimant=claimant or None,
        branch=branch or None,
    )


def _separation_refusal(authorizer: str, task_id: int, claimant: str | None) -> str | None:
    """Why ``authorizer`` may not cite a policy for work claimed by ``claimant``."""
    if authorizer not in policy_authorizers():
        return f"{authorizer!r} is not a configured policy authorizer ({POLICY_AUTHORIZERS_VAR})"
    if self_authorization_allowed():
        return None
    if claimant is None:
        return (
            f"task #{task_id} has no claimant, so the author cannot be told apart "
            "from the authorizer"
        )
    if claimant.lower() == authorizer.lower():
        return (
            f"{authorizer!r} claims task #{task_id} and may not authorize its own work "
            f"({POLICY_SELF_AUTHORIZATION_VAR} is off)"
        )
    return None


def _policy_binding(
    *,
    repo: str,
    number: int,
    head_sha: str,
    policy_ref: str,
    task_id: int,
    task_claimant: str | None,
    task_branch: str,
) -> str:
    """Canonical text the authorizer's signature covers.

    The task's claimant at recording time is part of it: closing a task later
    stamps the closer into ``claimed_by``, so re-reading the live row at merge
    time would misreport who authored the work. So is the task's branch: the
    ledger accepts the record only for a pull request whose head branch is
    that branch, so an unrelated task's claimant cannot stand in for the
    author of this pull request.
    """
    return json.dumps(
        {
            "head_sha": head_sha,
            "number": number,
            "policy_ref": policy_ref,
            "red_tier": False,
            "repo": repo,
            "task_branch": task_branch,
            "task_claimant": task_claimant,
            "task_id": task_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sign_policy_binding(identity: str, binding: str, task_id: int) -> str | None:
    bearer = producer_sign.resolve_agent_bearer_token(identity)
    if not bearer:
        return None
    return producer_sign.sign_producer_claim(
        identity=identity,
        kind=POLICY_AUTHORIZATION_KIND,
        ref_id=binding,
        task_id=str(task_id),
        bearer_token=bearer,
    )


def record_policy_authorization(
    *,
    repo: str,
    number: int,
    head_sha: str,
    policy_ref: str,
    task_id: int,
) -> dict[str, Any]:
    """Record that a standing policy covers merging one exact PR head.

    The caller's verified identity is the authorizer; nothing in the
    arguments can name someone else. ``policy_ref`` is a reference such as
    ``deploy-steward-2026-10-01`` or ``operator-chat:<ref>``, never the text
    of a conversation. Raises ``ValueError`` for a malformed binding and
    ``PermissionError`` when the caller may not authorize this merge.

    This is a pre-merge step: record it while the task is still live (open,
    doing, waiting_review or blocked) and still carries its worktree branch.
    A done or cancelled task is refused, as is a task with no branch:
    completion releases the worktree and clears the branch, and the closer
    is stamped into ``claimed_by``, so the row no longer identifies the pull
    request or its author. The branch is never taken from the caller. An
    authorization recorded before the task closes stays valid, because the
    claimant and branch are signed into the record.
    """
    identity = (config.verified_agent_identity() or "").strip()
    if not identity:
        raise PermissionError("a policy authorization requires a verified agent identity")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise ValueError("repo must be an owner/name GitHub repository")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("number must be a positive pull request number")
    if not isinstance(head_sha, str) or not _HEAD_SHA_RE.fullmatch(head_sha):
        raise ValueError("head_sha must be the full 40-character lowercase commit SHA")
    if not isinstance(policy_ref, str) or not _POLICY_REF_RE.fullmatch(policy_ref):
        raise ValueError(
            "policy_ref must be a short reference (3-80 chars of letters, digits and ._:/-)"
        )
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
        raise ValueError("task_id must be the hub task this merge belongs to")
    # Validation, signing and the append share one write transaction. A
    # separate read would let task completion commit in between (status set
    # to done, branch cleared) and the signed record would then bind a task
    # that a fresh request is refused for.
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        authorization_id, publish_payload, payload = _record_policy_authorization_locked(
            conn,
            identity=identity,
            repo=repo,
            number=number,
            head_sha=head_sha,
            policy_ref=policy_ref,
            task_id=task_id,
        )
        conn.commit()
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    public = {key: value for key, value in payload.items() if key not in {"scheme", "signature"}}
    return {"status": "recorded", "authorization_id": authorization_id, **public}


def _record_policy_authorization_locked(
    conn: Any,
    *,
    identity: str,
    repo: str,
    number: int,
    head_sha: str,
    policy_ref: str,
    task_id: int,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Validate the live task row and append the signed authorization on ``conn``.

    ``conn`` must already hold the write lock (``BEGIN IMMEDIATE``): the task's
    status, claimant and branch are read, checked and signed under the same
    transaction as the append, so task completion cannot commit between the
    check and the record. Raising leaves the caller to roll back, so a refusal
    writes nothing.
    """
    task = _task_binding(conn, task_id)
    if task is None:
        raise PermissionError(f"task #{task_id} does not exist")
    # Checked before separation of duties: closing a task stamps the closer
    # into claimed_by, which would otherwise surface as a misleading
    # self-authorization refusal.
    if task.status in _TERMINAL_TASK_STATUSES:
        raise PermissionError(
            f"task #{task_id} is {task.status}; a policy authorization must be recorded "
            "before the merge, while the task is still open and bound to its branch"
        )
    claimant, branch = task.claimant, task.branch
    refusal = _separation_refusal(identity, task_id, claimant)
    if refusal:
        raise PermissionError(refusal)
    if branch is None:
        raise PermissionError(
            f"task #{task_id} has no branch recorded, so it cannot be bound to a pull request "
            "(its worktree was never provisioned or has been released)"
        )
    binding = _policy_binding(
        repo=repo,
        number=number,
        head_sha=head_sha,
        policy_ref=policy_ref,
        task_id=task_id,
        task_claimant=claimant,
        task_branch=branch,
    )
    signature = _sign_policy_binding(identity, binding, task_id)
    if not signature:
        raise PermissionError(
            f"{identity!r} has no registered bearer, so its authorization cannot be signed"
        )
    payload = {
        "kind": POLICY_AUTHORIZATION_KIND,
        "evidence_class": EVIDENCE_CLASS_POLICY,
        "human_approval": False,
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "policy_ref": policy_ref,
        "task_id": task_id,
        "task_claimant": claimant,
        "task_branch": branch,
        "authorized_by": identity,
        "red_tier": False,
        "scheme": producer_sign.SCHEME_HMAC_SHA256_V1,
        "signature": signature,
    }
    authorization_id, _inserted, publish_payload = events.record_event_once_on_connection(
        conn,
        POLICY_AUTHORIZATION_CHANNEL,
        payload=payload,
        originator=identity,
        # The whole signed binding: the same head and policy recorded for a
        # second task is a second record, not a dedupe hit on the first.
        dedupe_key=(
            f"github-app:policy-authorization:{repo}#{number}@{head_sha}:{identity}:"
            f"{policy_ref}:task-{task_id}:{claimant or ''}:{branch}"
        ),
    )
    return authorization_id, publish_payload, payload


def _policy_record_valid(payload: Mapping[str, Any], originator: str | None) -> bool:
    identity = payload.get("authorized_by")
    task_id = payload.get("task_id")
    if not isinstance(identity, str) or not identity or identity != originator:
        return False
    if payload.get("kind") != POLICY_AUTHORIZATION_KIND or payload.get("red_tier") is not False:
        return False
    if payload.get("scheme") != producer_sign.SCHEME_HMAC_SHA256_V1:
        return False
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return False
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    claimant = payload.get("task_claimant")
    if claimant is not None and not isinstance(claimant, str):
        return False
    branch = payload.get("task_branch")
    if not isinstance(branch, str) or not branch:
        return False
    try:
        binding = _policy_binding(
            repo=str(payload["repo"]),
            number=int(payload["number"]),
            head_sha=str(payload["head_sha"]),
            policy_ref=str(payload["policy_ref"]),
            task_id=task_id,
            task_claimant=claimant,
            task_branch=branch,
        )
    except (KeyError, TypeError, ValueError):
        return False
    expected = _sign_policy_binding(identity, binding, task_id)
    if not expected or not hmac.compare_digest(expected, signature):
        return False
    # Re-evaluated against current configuration: removing a label from
    # UA_POLICY_AUTHORIZERS, or turning self-authorization back off,
    # withdraws the authorizations it covered.
    return _separation_refusal(identity, task_id, claimant) is None


class _ChainScope:
    """One ``shared_chain_verification`` scope: its owner, liveness and result.

    The result is used only by the thread and asyncio task that opened the
    scope, and only while it is open. A context copied into child work (an
    asyncio task, ``asyncio.to_thread``) carries this object, but it sees a
    different owner, or a closed scope once the parent exits, and verifies
    afresh (PR #1302 review).
    """

    __slots__ = ("closed", "error", "owner", "valid")

    def __init__(self) -> None:
        self.owner = _chain_owner()
        self.closed = False
        self.valid: bool | None = None
        self.error: BaseException | None = None

    def usable(self) -> bool:
        return not self.closed and self.owner == _chain_owner()


def _chain_owner() -> tuple[int, int | None]:
    import asyncio
    import threading

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), None if task is None else id(task)


_CHAIN_MEMO: ContextVar[_ChainScope | None] = ContextVar("github_chain_memo", default=None)


@contextmanager
def shared_chain_verification():
    """Verify the event hash chain at most once for the checks in this scope.

    For scheduling decisions over many authorizations (the retry queue's
    classification, PR #1302 review): without it each check rescans the whole
    ledger, O(checks x ledger). The one result, valid, tampered or an
    exception, is reused by every check in the scope, so a damaged ledger is
    scanned once too and every check fails closed. Only the opening thread
    and task use it, and only while the scope is open. Never used around a
    check that grants a POST; those verify the chain afresh.
    """
    scope = _ChainScope()
    token = _CHAIN_MEMO.set(scope)
    try:
        yield
    finally:
        scope.closed = True
        _CHAIN_MEMO.reset(token)


def _event_chain_valid() -> bool:
    scope = _CHAIN_MEMO.get()
    if scope is None or not scope.usable():
        return events.verify_event_chain().get("status") == "valid"
    if scope.error is not None:
        # A fresh exception each time: re-raising the stored one would grow
        # its traceback with every check in the scope.
        raise RuntimeError("the event chain could not be verified in this scope") from scope.error
    if scope.valid is None:
        try:
            scope.valid = events.verify_event_chain().get("status") == "valid"
        except Exception as exc:
            scope.error = exc
            raise
    return scope.valid


def verified_policy_authorization(
    *, repo: str, number: int, head_sha: str
) -> dict[str, Any] | None:
    """The newest valid policy authorization for exactly this PR head, or None.

    Exact-head binding: an authorization recorded for any other head of the
    same pull request does not match. Fails closed on an invalid hash chain,
    a signature that no longer verifies (rotated or removed bearer), an
    authorizer no longer configured, or a separation-of-duties violation.
    The caller still owns the red-tier check against the changed files.
    """
    if not _HEAD_SHA_RE.fullmatch(head_sha or ""):
        return None
    from . import edition

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid, payload, originator, entry_hash FROM event_log WHERE channel=? "
            "AND org_id IS ? "
            "AND json_extract(payload, '$.repo')=? "
            "AND json_extract(payload, '$.number')=? "
            "AND json_extract(payload, '$.head_sha')=? "
            "ORDER BY ulid DESC",
            (POLICY_AUTHORIZATION_CHANNEL, edition.tenant_org_id(), repo, int(number), head_sha),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    if not _event_chain_valid():
        return None
    for row in rows:
        if not row["entry_hash"]:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("head_sha") != head_sha:
            continue
        if not _policy_record_valid(payload, row["originator"]):
            continue
        return {
            "authorization_id": str(row["ulid"]),
            "evidence_class": EVIDENCE_CLASS_POLICY,
            "human_approval": False,
            "repo": repo,
            "number": int(number),
            "head_sha": head_sha,
            "policy_ref": payload["policy_ref"],
            "authorized_by": payload["authorized_by"],
            "task_id": payload["task_id"],
            "task_branch": payload["task_branch"],
        }
    return None


def list_policy_authorizations(
    *, repo: str, number: int, head_sha: str | None = None
) -> list[dict[str, Any]]:
    """Every policy authorization recorded for one pull request, newest first.

    Each row carries ``valid``: the same checks the ledger applies at merge
    time — a live hash chain, a signature that still verifies against the
    authorizer's current bearer, the authorizer's current membership in
    ``UA_POLICY_AUTHORIZERS``, and the self-authorization rule — minus the
    red-tier check, which reads the pull request's changed files and so
    stays with the merge-time caller. ``head_sha`` narrows the list to one
    exact head. Signatures are never returned.
    """
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise ValueError("repo must be an owner/name GitHub repository")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("number must be a positive pull request number")
    if head_sha is not None and (
        not isinstance(head_sha, str) or not _HEAD_SHA_RE.fullmatch(head_sha)
    ):
        raise ValueError("head_sha must be the full 40-character lowercase commit SHA")
    from . import edition

    sql = (
        "SELECT ulid, payload, originator, created_at, entry_hash FROM event_log "
        "WHERE channel=? AND org_id IS ? AND json_extract(payload, '$.repo')=? "
        "AND json_extract(payload, '$.number')=?"
    )
    params: tuple[Any, ...] = (
        POLICY_AUTHORIZATION_CHANNEL,
        edition.tenant_org_id(),
        repo,
        int(number),
    )
    if head_sha is not None:
        sql += " AND json_extract(payload, '$.head_sha')=?"
        params += (head_sha,)
    sql += " ORDER BY ulid DESC"
    conn = storage.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    # One chain verification for the whole list, as at merge time: a broken
    # chain fails every row closed rather than hiding the newest tampering.
    chain_valid = events.verify_event_chain().get("status") == "valid"
    listed: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        valid = (
            bool(payload)
            and chain_valid
            and bool(row["entry_hash"])
            and (head_sha is None or payload.get("head_sha") == head_sha)
            and _policy_record_valid(payload, row["originator"])
        )
        public = {
            key: value for key, value in payload.items() if key not in {"scheme", "signature"}
        }
        listed.append(
            {
                "authorization_id": str(row["ulid"]),
                "originator": row["originator"],
                "created_at": row["created_at"],
                "valid": valid,
                **public,
            }
        )
    return listed


def policy_authorized_description(authorization: Mapping[str, Any]) -> str:
    """Commit-status text for the policy-authorized class (GitHub's 140 cap)."""
    ref = str(authorization.get("policy_ref") or "unknown policy")
    label = str(authorization.get("authorized_by") or "unknown")
    return f"Policy-authorized: {ref} by {label}"[:140]
