"""Poll the one dogfood repository through the Approval Ledger connector."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import (
    agent_lifecycle,
    config,
    connector_oauth,
    credential_fence,
    cycle_eligibility,
    edition,
    events,
    github_app,
    independent_review,
    policy,
    post_init,
    pr_watch,
    required_settings,
    review_head_status,
    review_reads,
    storage,
)
from .tools import approvals

# pr-watch is this worker's scheduled consumer. Its idle interval bounds how long an
# approved pass may wait; while a row is in flight the schedule polls at
# pr_watch.ledger_active_interval_s() instead (task #1867, ``ledger_in_flight``).
POLL_INTERVAL_S = pr_watch.SCHEDULE_INTERVAL_S
_TERMINAL_CHANNELS = (
    github_app.EVIDENCE_CHANNEL,
    github_app.DENIED_CHANNEL,
    github_app.SUPERSEDED_CHANNEL,
    github_app.CLOSED_UNAPPROVED_CHANNEL,
    github_app.POST_MERGE_CHANNEL,
    github_app.POLICY_AUTHORIZED_CHANNEL,
)
# GitHub lists at most 3000 files for a pull request; a list that long may be
# truncated, so the red-tier check cannot clear it.
_MAX_PR_FILES = 3000

_POLL_REPO: ContextVar[str | None] = ContextVar("github_app_poll_repo", default=None)


def _repo() -> str:
    repo = _POLL_REPO.get() or os.environ.get("UA_GITHUB_CONNECTOR_REPO", "").strip()
    if not required_settings.is_owner_repo(repo):
        raise ValueError("set UA_GITHUB_CONNECTOR_REPO to an owner/repository")
    return repo


_POLL_BASE: ContextVar[str | None] = ContextVar("github_app_poll_base", default=None)
_INVALID_BRANCH_CHAR = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _base_branch() -> str:
    """Base branch the Approval Ledger polls (UA_GITHUB_CONNECTOR_BASE, default main).

    A connector repository whose default branch is not ``main`` would otherwise
    have open pull requests that neither poll transport returns.
    """
    base = _POLL_BASE.get() or os.environ.get("UA_GITHUB_CONNECTOR_BASE", "").strip() or "main"
    parts = base.split("/")
    if (
        base.startswith("-")
        or base == "@"
        or base.endswith(".")
        or ".." in base
        or "@{" in base
        or _INVALID_BRANCH_CHAR.search(base)
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
    ):
        raise ValueError("set UA_GITHUB_CONNECTOR_BASE to a valid branch name")
    return base


TOKEN_VAR = "UA_GITHUB_CONNECTOR_AGENT_TOKEN"
#: The worker's own service identity (task #1867). Approval rows, pending-status
#: records and signed evidence it creates are attributed to this label, so the
#: Approval Ledger no longer shows them as requested by a developer agent. The
#: label needs its own bearer in ``UA_AGENT_TOKENS`` (evidence is HMAC-signed
#: with it); ``TOKEN_VAR`` holds that bearer.
SERVICE_IDENTITY = "approval_ledger_worker"
#: The developer bearer the worker borrowed before task #1867. Still accepted so
#: a hub whose service.env has not been migrated keeps polling, with a warning
#: on every poll; remove once the service bearer is registered on the hub.
LEGACY_WORKER_IDENTITY = "Desktop_Agent2"
WORKER_IDENTITY = SERVICE_IDENTITY
_ACCEPTED_IDENTITIES = (SERVICE_IDENTITY, LEGACY_WORKER_IDENTITY)
LOGGER = logging.getLogger(__name__)
_PR_FIELDS = (
    "number,title,body,url,state,headRefOid,headRefName,baseRefName,"
    "isDraft,author,labels,mergedAt,closedAt,reviews"
)


@contextmanager
def _authenticated_service_identity():
    """Bind only an agent whose bearer matches the hub's own token map."""
    bearer = os.environ.get(TOKEN_VAR, "").strip()
    identity = config.agent_tokens().get(bearer) if bearer else None
    if identity not in _ACCEPTED_IDENTITIES:
        raise PermissionError(
            f"{TOKEN_VAR} must match the {SERVICE_IDENTITY} bearer in UA_AGENT_TOKENS "
            f"(the legacy {LEGACY_WORKER_IDENTITY} bearer is accepted until it is registered)"
        )
    if identity == LEGACY_WORKER_IDENTITY:
        LOGGER.warning(
            "Approval Ledger worker is running under the legacy %s bearer, so its approval "
            "rows show requested_by=%s; register a %s bearer in UA_AGENT_TOKENS and point "
            "%s at it",
            LEGACY_WORKER_IDENTITY,
            LEGACY_WORKER_IDENTITY,
            SERVICE_IDENTITY,
            TOKEN_VAR,
        )
    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@contextmanager
def _poll_lock():
    """Serialize cursorless scans with manual catch-up and scheduled polls."""
    lock_dir = config.data_home() / "github-status-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_dir / "dogfood-worker.lock").open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


#: How long a recorded read waits behind a running poll before deferring to it.
_PUBLISH_LOCK_WAIT_S = 5.0


@contextmanager
def _publish_lock():
    """Share ``_poll_lock``'s file with a bounded wait; yields whether it is held.

    A publish outside the poll (task #2410) must stay serialized with it: a
    poll that judged a head before the read committed would otherwise overwrite
    the success with its older verdict, and a merged head gets no correcting
    poll. So a request that cannot get the lock in time publishes nothing. The
    row is already durable and the next poll delivers it
    (``review_head_status.unpublished_read_heads``).
    """
    lock_dir = config.data_home() / "github-status-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_dir / "dogfood-worker.lock").open("a+") as lock_file:
        deadline = time.monotonic() + _PUBLISH_LOCK_WAIT_S
        held = False
        while not held:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _open_main_prs(run_gh: Callable[..., Any]) -> dict[int, pr_watch.PRSnapshot]:
    proc = run_gh(
        [
            "gh",
            "pr",
            "list",
            "-R",
            _repo(),
            "--state",
            "open",
            "--base",
            _base_branch(),
            "--limit",
            "1000",
            "--json",
            _PR_FIELDS,
        ],
        capture_output=True,
        text=True,
        timeout=pr_watch.GH_TIMEOUT_SECONDS,
    )
    if proc.returncode:
        raise RuntimeError("GitHub PR poll failed")
    return pr_watch.parse_prs(json.loads(proc.stdout))


def _brokered_github_json(token: str, path: str, *, params: dict[str, object] | None = None):
    """Fixed GitHub REST read; a brokered token never enters a child process."""
    transport = github_app.GitHubUserTokenTransport(lambda: token, brokered=True)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        transport._refuse_while_backing_off()
        response = client.get(
            f"https://api.github.com{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if response.status_code in (403, 429):
        wait = github_app.rate_limit_backoff_s(
            response.status_code,
            response.headers,
            body=response.content[: github_app._RATE_LIMIT_BODY_BYTES].decode("utf-8", "replace"),
            now=github_app._wall_clock(),
        )
        if wait is not None:
            github_app._record_backoff(transport.backoff_scope(), wait)
            raise github_app.GitHubBackoff(wait, sent=True)
    if response.status_code == 401:
        connector_oauth.mark_invalid_credential(
            "github",
            actor=config.verified_agent_identity() or SERVICE_IDENTITY,
            purpose="github_app",
            org_id=edition.tenant_org_id(),
            rejected_token=token,
        )
    if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
        raise RuntimeError("GitHub PR read failed")
    return response.json()


def _brokered_gh(token: str) -> Callable[..., Any]:
    """A ``gh api <path>`` stand-in, so a brokered token never enters a child process."""

    def run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        try:
            payload = _brokered_github_json(token, f"/{args[2]}")
        except github_app.GitHubBackoff:
            raise
        except (RuntimeError, ValueError, httpx.HTTPError):
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    return run


def _rest_snapshot(payload: dict[str, Any]) -> pr_watch.PRSnapshot:
    """Normalize GitHub REST pull shape to the existing PR snapshot contract."""
    return pr_watch.PRSnapshot(
        number=int(payload["number"]),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or "").strip(),
        url=str(payload.get("html_url") or ""),
        state="MERGED" if payload.get("merged_at") else str(payload.get("state") or "").upper(),
        head_sha=str((payload.get("head") or {}).get("sha") or ""),
        head_branch=str((payload.get("head") or {}).get("ref") or ""),
        base_branch=str((payload.get("base") or {}).get("ref") or ""),
        merged_at=payload.get("merged_at"),
        closed_at=payload.get("closed_at"),
        merge_sha=str(payload.get("merge_commit_sha") or "") if payload.get("merged_at") else "",
        draft=bool(payload.get("draft")),
        author_login=str((payload.get("user") or {}).get("login") or ""),
        labels=tuple(
            sorted(str(item["name"]) for item in payload.get("labels", []) if item.get("name"))
        ),
    )


def _brokered_open_main_prs(token: str) -> dict[int, pr_watch.PRSnapshot]:
    out: dict[int, pr_watch.PRSnapshot] = {}
    for page in range(1, 11):
        payload = _brokered_github_json(
            token,
            f"/repos/{_repo()}/pulls",
            params={"state": "open", "base": _base_branch(), "per_page": 100, "page": page},
        )
        if not isinstance(payload, list):
            raise RuntimeError("GitHub PR list was invalid")
        for item in payload:
            snapshot = _rest_snapshot(item)
            out[snapshot.number] = snapshot
        if len(payload) < 100:
            break
    return out


def _brokered_view_pr(token: str, number: int) -> pr_watch.PRSnapshot:
    payload = _brokered_github_json(token, f"/repos/{_repo()}/pulls/{number}")
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub PR detail was invalid")
    return _rest_snapshot(payload)


def _event(snapshot: pr_watch.PRSnapshot) -> github_app.NormalizedPREvent:
    # One approval per PR head. Title, review, and label edits do not mint a
    # new approval or overwrite the visible status on the same commit.
    return github_app.NormalizedPREvent(
        repo=_repo(),
        number=snapshot.number,
        event_type="synchronize",
        title=snapshot.title,
        url=snapshot.url,
        state=snapshot.state,
        head_sha=snapshot.head_sha,
        base_branch=snapshot.base_branch,
        head_branch=snapshot.head_branch,
        author_login=snapshot.author_login,
        labels=snapshot.labels,
        source="github_app_worker",
    )


def _record(channel: str, event_id: str) -> dict[str, Any] | None:
    event_path = "$.event_id" if channel in _TERMINAL_CHANNELS else "$.event.event_id"
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, ?)=? ORDER BY ulid DESC LIMIT 1",
            (channel, event_path, event_id),
        ).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


def _terminal(event_id: str) -> bool:
    return any(_record(channel, event_id) for channel in _TERMINAL_CHANNELS)


def _settled(event_id: str) -> bool:
    """A head is finished unless the only marker is that it was superseded."""
    return any(
        _record(channel, event_id)
        for channel in _TERMINAL_CHANNELS
        if channel != github_app.SUPERSEDED_CHANNEL
    )


def _unresolved_pending() -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ulid DESC",
            (github_app.PENDING_CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    pending: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload"])
        event = payload.get("event") or {}
        event_id = event.get("event_id")
        if event.get("repo") != _repo() or not event_id or _terminal(event_id):
            continue
        pending.append(payload)
    return pending


def _retire_approval(approval_id: int, reason: str) -> None:
    approval = approvals.get_approval(approval_id)
    if approval is None:
        return
    if approval["status"] == "approved":
        approvals.approval_resolve(approval_id, "revoke", reason=reason)
        return
    if approval["status"] != "pending":
        return
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET status='expired', updated_at=?, reason=? "
            "WHERE id=? AND status='pending'",
            (storage.now(), reason, approval_id),
        )
        conn.commit()
    finally:
        conn.close()


def _finish_check(
    transport: github_app.GitHubAppTransport,
    pending: dict[str, Any],
    conclusion: str,
    *,
    description: str | None = None,
) -> None:
    event = pending["event"]

    def post() -> None:
        extra = {"description": description} if description else {}
        transport.update_check_run(
            repo=event["repo"],
            check_run_id=pending["check_run_id"],
            status="completed",
            conclusion=conclusion,
            **extra,
        )

    if isinstance(transport, github_app.GitHubUserTokenTransport):
        recorded = {key: value for key, value in event.items() if key != "connector"}
        recorded["labels"] = tuple(recorded.get("labels") or ())
        with github_app._user_status_lock(github_app.NormalizedPREvent(**recorded)):
            post()
        return
    post()


def _record_outcome(channel: str, event_id: str, approval_id: int, outcome: str) -> None:
    events.record_event(
        channel,
        payload={"event_id": event_id, "approval_id": approval_id, "outcome": outcome},
        originator=config.verified_agent_identity(),
        dedupe_key=f"github-app:{outcome}:{event_id}",
    )


def _retire_older_heads(
    transport: github_app.GitHubAppTransport,
    number: int,
    head_sha: str,
    *,
    include_unbound: bool = False,
) -> None:
    """Drop approval rows for earlier heads of the same pull request.

    ``include_unbound`` also retires rows that never got a pending marker. Only
    callers holding the open PR's live head pass it: a closed PR's newest
    marker is not necessarily its last head.
    """
    reason = "superseded by a newer pull request head"
    for pending in _unresolved_pending():
        event = pending["event"]
        if int(event.get("number") or 0) != number or event.get("head_sha") == head_sha:
            continue
        approval_id = int(pending["approval_id"])
        _retire_approval(approval_id, reason)
        _finish_check(transport, pending, "stale")
        _record_outcome(github_app.SUPERSEDED_CHANNEL, event["event_id"], approval_id, "superseded")
    if not include_unbound:
        return
    # A row whose pending-status POST failed has no marker above, but it is
    # still actionable in the queue. Retire it from the approval row itself.
    for approval_id, event_id in _unbound_older_head_rows(number, head_sha):
        _retire_approval(approval_id, reason)
        _record_outcome(github_app.SUPERSEDED_CHANNEL, event_id, approval_id, "superseded")


def _unbound_older_head_rows(number: int, head_sha: str) -> list[tuple[int, str]]:
    """Live (pending or approved) rows of this tenant for other heads of the PR."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, json_extract(payload, '$.event_id') AS event_id FROM approvals "
            "WHERE tool=? AND status IN ('pending', 'approved') AND org_id IS ? "
            "AND json_extract(payload, '$.repo')=? "
            "AND json_extract(payload, '$.number')=? "
            "AND json_extract(payload, '$.head_sha')!=? ORDER BY id",
            (github_app.TOOL_NAME, edition.tenant_org_id(), _repo(), number, head_sha),
        ).fetchall()
    finally:
        conn.close()
    return [(int(row["id"]), str(row["event_id"] or "")) for row in rows]


def _newest_pending(number: int) -> dict[str, Any] | None:
    for pending in _unresolved_pending():
        if int(pending["event"].get("number") or 0) == number:
            return pending
    return None


def _pending_for(number: int, head_sha: str) -> dict[str, Any] | None:
    for pending in _unresolved_pending():
        event = pending["event"]
        if int(event.get("number") or 0) == number and event.get("head_sha") == head_sha:
            return pending
    return None


def _view_pr(run_gh: Callable[..., Any], number: int) -> pr_watch.PRSnapshot | None:
    proc = run_gh(
        ["gh", "pr", "view", str(number), "-R", _repo(), "--json", _PR_FIELDS],
        capture_output=True,
        text=True,
        timeout=pr_watch.GH_TIMEOUT_SECONDS,
    )
    if proc.returncode:
        raise RuntimeError(f"GitHub PR view failed for #{number}")
    payload = json.loads(proc.stdout)
    parsed = pr_watch.parse_prs(payload if isinstance(payload, list) else [payload])
    return parsed.get(number)


def live_pr_reader(
    read_pr: Callable[[int], pr_watch.PRSnapshot | None],
) -> Callable[[github_app.NormalizedPREvent], pr_watch.PRSnapshot | None]:
    """A ``github_app.live_pr`` reader over a by-number PR read (#2234)."""

    def reader(event: github_app.NormalizedPREvent) -> pr_watch.PRSnapshot | None:
        return read_pr(event.number)

    return reader


def _advance_snapshot(
    snapshot: pr_watch.PRSnapshot,
    transport: github_app.GitHubAppTransport,
    policy_config: policy.PolicyConfig | None,
    *,
    allow_closed: bool,
    merged_head_sha: str | None = None,
    changed_files: Callable[[int], list[str]] | None = None,
) -> dict[str, Any] | None:
    if not snapshot.head_sha or (snapshot.state != "OPEN" and not allow_closed):
        return None
    event = _event(snapshot)
    if _settled(event.event_id):
        return None
    pending = _record(github_app.PENDING_CHANNEL, event.event_id)
    if not allow_closed:
        # The head's newest row is the authority (task #2010). A newer live
        # row without a status (a failed pending-status POST, or one queued
        # after the bound row) is bound before anything can consume a pass.
        # When the newest row is expired or revoked, no older decision for
        # the head may be bound or spent: only a fresh row may follow it.
        latest = _head_approval(event)
        bound_id = int(pending["approval_id"]) if pending is not None else None
        if latest is not None and latest["status"] not in _FENCED:
            if latest["id"] != bound_id:
                return _bind_head_status(event, latest, transport)
        elif latest is not None:
            _refuse_fenced_decisions(latest)
    if pending is None:
        if not allow_closed:
            return github_app.process_pr_event(event, transport, policy_config=policy_config)
        return None
    approval = approvals.get_approval(int(pending["approval_id"]))
    if approval is None:
        raise PermissionError("pending GitHub status has no approval row")
    if allow_closed:
        # A policy decision committed by an earlier poll is finished, not
        # re-decided: reconciled against GitHub, and if it never reached
        # GitHub, re-validated first (withdrawn when it no longer holds).
        intent = _policy_intent(str(pending["event"]["event_id"]))
        if intent is not None and _intent_open(intent):
            return _publish_policy_intent(transport, intent, reconcile=True)
    # A closed head whose newest row is expired or revoked, newer than the
    # bound one, has no human pass left: the bound older decision is fenced
    # and must not be spent (or reported) after the close either.
    fenced = False
    if allow_closed and approval["status"] in {"pending", "approved", "denied"}:
        latest = _head_approval(event)
        fenced = (
            latest is not None
            and latest["status"] in _FENCED
            and latest["id"] > int(approval["id"])
        )
    if allow_closed and (fenced or _closes_without_human_pass(approval)):
        authorization = _policy_authorization(
            snapshot,
            str(pending["event"]["head_sha"]),
            merged_head_sha=merged_head_sha,
            changed_files=changed_files,
        )
        if authorization is not None:
            closed = _close_policy_authorized(transport, pending, authorization, approval)
            if closed is not None:
                return closed
            # A human decided meanwhile: carry on with the row as it is now.
            approval = approvals.get_approval(int(pending["approval_id"]))
            if approval is None:
                raise PermissionError("pending GitHub status has no approval row")
    if fenced:
        return _close_without_approval(transport, pending, snapshot)
    if approval["status"] == "pending":
        if allow_closed:
            return _close_without_approval(transport, pending, snapshot)
        return None
    if allow_closed and approval["status"] == "approved" and not _pass_still_valid(approval):
        _expire_approved(
            int(approval["id"]), "one-shot pass expired before the pull request closed"
        )
        return _close_without_approval(transport, pending, snapshot, conclusion="expired")
    if allow_closed and approval["status"] not in {"approved", "consumed", "denied"}:
        return _close_without_approval(transport, pending, snapshot)
    recorded = {k: v for k, v in pending["event"].items() if k != "connector"}
    recorded["labels"] = tuple(recorded.get("labels") or ())
    event = github_app.NormalizedPREvent(**recorded)
    if approval["status"] == "consumed":
        result = github_app.resume_consumed_pr_event(
            event, transport, pending_check_run_id=pending["check_run_id"]
        )
    elif approval["status"] in {"expired", "revoked"}:
        result = github_app.process_pr_event(event, transport, policy_config=policy_config)
    else:
        result = github_app.process_pr_event(
            event,
            transport,
            policy_config=policy_config,
            pending_check_run_id=pending["check_run_id"],
        )
    if allow_closed and result.get("status") == "approved":
        merged = snapshot.state == "MERGED" or bool(snapshot.merged_at)
        outcome = "approved_post_merge" if merged else "approved_after_close"
        _record_outcome(
            github_app.POST_MERGE_CHANNEL,
            event.event_id,
            int(result["approval_id"]),
            outcome,
        )
        return {**result, "status": outcome}
    return result


def _closes_without_human_pass(approval: dict[str, Any]) -> bool:
    """A closed head whose approval row would otherwise end unapproved.

    A consumed approval is recorded human evidence and a denial is a human
    "no"; a policy authorization never stands in for either.
    """
    status = approval["status"]
    if status == "approved":
        return not _pass_still_valid(approval)
    return status not in {"consumed", "denied"}


def _is_merged(snapshot: pr_watch.PRSnapshot) -> bool:
    return snapshot.state == "MERGED" or bool(snapshot.merged_at)


def _policy_authorization(
    snapshot: pr_watch.PRSnapshot,
    head_sha: str,
    *,
    merged_head_sha: str | None,
    changed_files: Callable[[int], list[str]] | None,
) -> dict[str, Any] | None:
    """A verified, non-red-tier policy authorization for this exact merged head.

    Fails closed (None) when the pull request was closed without merging,
    the merged head differs from ``head_sha``, no valid authorization is
    bound to that head, or the changed files cannot be read or touch
    red-tier scope.
    """
    if not _is_merged(snapshot) or changed_files is None:
        return None
    merged_head = merged_head_sha or snapshot.head_sha
    if merged_head != head_sha:
        return None
    authorization = github_app.verified_policy_authorization(
        repo=_repo(), number=snapshot.number, head_sha=head_sha
    )
    if authorization is None:
        return None
    if not snapshot.head_branch or authorization.get("task_branch") != snapshot.head_branch:
        # The cited task is not this pull request's work, so its claimant
        # says nothing about who authored it.
        LOGGER.warning(
            "policy authorization ignored for PR #%s: task #%s is bound to another branch",
            snapshot.number,
            authorization.get("task_id"),
        )
        return None
    if _human_denied(snapshot.number):
        return None
    try:
        files = changed_files(snapshot.number)
    except Exception:
        LOGGER.warning(
            "policy authorization ignored for PR #%s: changed files unreadable",
            snapshot.number,
            exc_info=True,
        )
        return None
    if not files or len(files) >= _MAX_PR_FILES:
        LOGGER.warning(
            "policy authorization ignored for PR #%s: changed-file list empty or truncated",
            snapshot.number,
        )
        return None
    red_tier = cycle_eligibility._is_red_tier([{"file": path} for path in files])
    if red_tier:
        LOGGER.warning(
            "policy authorization ignored for PR #%s: red-tier files need a human approval: %s",
            snapshot.number,
            ", ".join(sorted(red_tier)[:10]),
        )
        return None
    return authorization


def _human_denied(number: int) -> bool:
    """Whether a human denied any recorded head of this pull request.

    A policy citation never replaces a human "no", including on a later head
    that merged before any poll saw it, or on a row whose pending-status POST
    failed, so no marker names it (task #2103).
    """
    conn = storage.connect()
    try:
        unbound_denial = conn.execute(
            f"SELECT 1 FROM approvals WHERE {approvals._TENANT_SQL} AND tool=? "
            "AND status='denied' AND json_extract(payload, '$.repo')=? "
            "AND json_extract(payload, '$.number')=? LIMIT 1",
            (approvals._tenant(), github_app.TOOL_NAME, _repo(), int(number)),
        ).fetchone()
        if unbound_denial is not None:
            return True
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.event.repo')=? "
            "AND json_extract(payload, '$.event.number')=?",
            (github_app.PENDING_CHANNEL, _repo(), int(number)),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        approval_id = json.loads(row["payload"]).get("approval_id")
        approval = approvals.get_approval(int(approval_id)) if approval_id else None
        if approval is not None and approval["status"] == "denied":
            return True
    return False


def _record_policy_outcome(
    event_id: str, approval_id: int | None, authorization: dict[str, Any]
) -> str:
    return events.record_event(
        github_app.POLICY_AUTHORIZED_CHANNEL,
        payload={
            "event_id": event_id,
            "approval_id": approval_id,
            "outcome": "policy_authorized",
            "evidence_class": github_app.EVIDENCE_CLASS_POLICY,
            "human_approval": False,
            "authorization_id": authorization["authorization_id"],
            "policy_ref": authorization["policy_ref"],
            "authorized_by": authorization["authorized_by"],
            "repo": authorization["repo"],
            "number": authorization["number"],
            "head_sha": authorization["head_sha"],
            "task_id": authorization["task_id"],
        },
        originator=config.verified_agent_identity(),
        dedupe_key=f"github-app:policy_authorized:{event_id}",
    )


def _policy_result(
    event_id: str, approval_id: int | None, authorization: dict[str, Any], outcome_id: str
) -> dict[str, Any]:
    return {
        "status": "policy_authorized",
        "event_id": event_id,
        "approval_id": approval_id,
        "evidence_class": github_app.EVIDENCE_CLASS_POLICY,
        "authorization_id": authorization["authorization_id"],
        "policy_ref": authorization["policy_ref"],
        "authorized_by": authorization["authorized_by"],
        "outcome_id": outcome_id,
    }


def _close_policy_authorized(
    transport: github_app.GitHubAppTransport,
    pending: dict[str, Any],
    authorization: dict[str, Any],
    approval: dict[str, Any],
) -> dict[str, Any] | None:
    """Settle a merged head as policy-authorized (success, its own description).

    The bound row is fenced and the decision committed
    (``_commit_policy_intent``) before the bound status is completed, as for an
    unseen merged head (PR #1302 review). None when a human decided the row,
    or denied any row of the PR, after ``_policy_authorization`` read it; the
    caller then proceeds with the row as it now is.
    """
    recorded = {key: value for key, value in pending["event"].items() if key != "connector"}
    recorded["labels"] = tuple(recorded.get("labels") or ())
    event = github_app.NormalizedPREvent(**recorded)
    committed = _commit_policy_intent(
        event, authorization, row=approval, check_run_id=pending["check_run_id"]
    )
    if committed is None:
        return None
    intent, _orphan_result = committed
    return _publish_policy_intent(transport, intent, reconcile=False)


#: Write-ahead record of a policy-authorized success the worker has committed to
#: post (PR #1302 review). It is written, fenced against a human denial, before
#: the POST, and finished by the policy outcome. A retry with no outcome yet
#: reconciles against GitHub before posting, so a lost response or a failed
#: outcome write never produces a second success.
POLICY_INTENT_CHANNEL = "connector.github.policy_publication"
#: A committed but unposted policy publication cancelled because its
#: authorization no longer verifies, or a human denied the PR (PR #1302 review).
POLICY_WITHDRAWN_CHANNEL = "connector.github.policy_withdrawn"
#: A success posted while its authorization was being withdrawn outside hub
#: code; recorded before the corrective conclusion is published (round 4).
POLICY_WITHDRAWN_AFTER_POST_CHANNEL = "connector.github.policy_withdrawn_after_post"
POLICY_CORRECTION_DESCRIPTION = "Policy authorization withdrawn after publication"
#: A success POST that returned and whose authorization still verified straight
#: after it: the durable "verified publication" state. Without this record a
#: success found on GitHub is an uncertain publication (its response, its
#: post-POST check or its marker write was lost), so reconciliation re-verifies
#: it and corrects it if withdrawn, rather than completing its evidence.
POLICY_POSTVERIFIED_CHANNEL = "connector.github.policy_postverified"
#: Request timeout while the credential fence is held for a policy POST, so
#: a slow GitHub never keeps hub bearer changes waiting long.
POLICY_POST_TIMEOUT_S = 10
#: Unfinished publications the orphan sweep retries per poll (GitHub reads).
POLICY_PUBLICATION_RETRIES_PER_POLL = 5
_AUTHORIZATION_FIELDS = (
    "authorization_id",
    "policy_ref",
    "authorized_by",
    "repo",
    "number",
    "head_sha",
    "task_id",
)
# Event ids whose publication this poll already attempted (set per poll).
_PUBLISH_ATTEMPTED: ContextVar[set[str] | None] = ContextVar(
    "github_app_publish_attempted", default=None
)


def _policy_intent(event_id: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.event_id')=? ORDER BY ulid DESC LIMIT 1",
            (POLICY_INTENT_CHANNEL, event_id),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["payload"]) if row else None


def _commit_policy_intent(
    event: github_app.NormalizedPREvent,
    authorization: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
    orphan_snapshot: pr_watch.PRSnapshot | None = None,
    check_run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Commit the decision to publish a policy success; None if a human got there first.

    One ``BEGIN IMMEDIATE`` transaction, so it serializes with
    ``approval_resolve`` (itself a status/``updated_at`` CAS). It reads every
    row of the PR and refuses on any denial; fences the head's own approval
    ``row`` (a bound row with a status marker, or an orphan, task #2103) out of
    ``pending`` or ``approved``; and expires every other ``pending`` row of the
    PR (``fenced_rows``), all with status CASes, before writing the intent. A
    denial that commits first is seen here and nothing is published; any later
    one finds no pending row and is refused. The intent is unique per head and
    authorization, so a withdrawn decision is never made again. ``orphan_snapshot`` marks ``row`` as
    an orphan, whose ``orphan_retired`` event joins the same transaction.
    ``check_run_id`` is the bound status to complete instead of a new check.
    Returns ``(intent, orphan_result)``.
    """
    intent = {
        "event_id": event.event_id,
        "event": event.to_payload(),
        "repo": event.repo,
        "number": event.number,
        "head_sha": event.head_sha,
        "approval_id": int(row["id"]) if row is not None else None,
        "check_run_id": check_run_id,
        "description": github_app.policy_authorized_description(authorization),
        "authorization": {field: authorization[field] for field in _AUTHORIZATION_FIELDS},
    }
    # The authorization was verified before the changed-files read, which can
    # be slow: verify it again now, so a withdrawal in that window commits
    # nothing (round 3). The POST re-checks once more (_publish_policy_intent).
    current = github_app.verified_policy_authorization(
        repo=event.repo, number=event.number, head_sha=event.head_sha
    )
    if current is None or current.get("authorization_id") != authorization["authorization_id"]:
        return None
    originator = config.verified_agent_identity()
    published: list[dict[str, Any]] = []
    orphan_result = None
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pr_rows = conn.execute(
            f"SELECT id, status, json_extract(payload, '$.head_sha') AS head_sha "
            f"FROM approvals WHERE {approvals._TENANT_SQL} AND tool=? "
            "AND json_extract(payload, '$.repo')=? AND json_extract(payload, '$.number')=? "
            "ORDER BY id",
            (approvals._tenant(), github_app.TOOL_NAME, _repo(), int(event.number)),
        ).fetchall()
        if any(item["status"] == "denied" for item in pr_rows):
            conn.rollback()
            return None
        reason = f"merged under policy authorization {authorization['policy_ref']}"
        # Only a live row can still be decided by a human; an expired or
        # revoked one is already terminal.
        if row is not None and row["status"] in {"pending", "approved"}:
            if not _expire_row_on(conn, row, reason):
                conn.rollback()
                return None
        # Fence every other row a human could still deny (round 2): a denial
        # needs a pending row and approval_resolve is a status CAS, so once
        # this commits no row of the PR can be denied ahead of the post. A
        # merged PR gets no new rows. Approved rows cannot be denied.
        fence_reason = (
            f"fenced by policy authorization {authorization['policy_ref']} "
            f"for merged head {event.head_sha}"
        )
        fenced = []
        for item in pr_rows:
            if item["status"] != "pending" or (row is not None and item["id"] == row["id"]):
                continue
            if not _expire_row_on(conn, {"id": item["id"], "status": "pending"}, fence_reason):
                conn.rollback()
                return None
            fenced.append(
                {"id": int(item["id"]), "status": "pending", "head_sha": item["head_sha"]}
            )
        intent["fenced_rows"] = fenced
        if row is not None and orphan_snapshot is not None:
            payload = _orphan_payload(
                row, orphan_snapshot, "policy_authorized", reason, intent["authorization"]
            )
            ulid, _created, event_row = events.record_event_once_on_connection(
                conn,
                ORPHAN_RETIRED_CHANNEL,
                payload=payload,
                originator=originator,
                dedupe_key=f"github-app:orphan_retired:{row['id']}",
            )
            published.append(event_row)
            orphan_result = _orphan_result(row, orphan_snapshot, "policy_authorized", ulid)
        _ulid, created, intent_row = events.record_event_once_on_connection(
            conn,
            POLICY_INTENT_CHANNEL,
            payload=intent,
            originator=originator,
            dedupe_key=(
                f"github-app:policy_intent:{event.event_id}:"
                f"{intent['authorization']['authorization_id']}"
            ),
        )
        if not created:
            # This authorization already produced an intent for the head
            # (withdrawn, or another poll's): never decide it twice.
            conn.rollback()
            return None
        published.append(intent_row)
        conn.commit()
    finally:
        conn.close()
    for item in published:
        if item is not None:
            events.publish(item)
    return intent, orphan_result


def _publish_policy_intent(
    transport: github_app.GitHubAppTransport, intent: dict[str, Any], *, reconcile: bool
) -> dict[str, Any]:
    """Post the committed policy success at most once, then record its outcome.

    ``reconcile`` (any attempt after the first) reads GitHub first and posts
    only when the success is not already on the head. A transport that cannot
    be read raises, so an unknown outcome is retried, never re-posted blind.

    Every POST is re-authorized immediately before it is sent, while holding
    the hub's credential fence (``credential_fence``, also taken by every hub
    bearer change) with a request timeout of ``POLICY_POST_TIMEOUT_S``. A
    remote POST cannot be atomic with a local check, so the authorization is
    verified once more straight after the POST. If a change outside hub code
    (a hand-edited token file, say) withdrew it in that window, the poll
    records ``POLICY_WITHDRAWN_AFTER_POST_CHANNEL`` first and then publishes
    a corrective non-success conclusion on the same status or check run. The
    residual exposure is that one request, and it is visible and audited.

    A POST that returned and still verified afterwards is recorded on
    ``POLICY_POSTVERIFIED_CHANNEL`` before its outcome. On a retry, a success
    found on GitHub with that record only has its evidence completed. One
    without it is uncertain (the response was lost, the post-POST check
    raised, or the marker write failed): it is re-verified now, and if the
    authorization no longer holds it is marked and corrected in place (the
    App run id is recovered from the lookup), never recorded as
    ``policy_authorized``.
    """
    event_id = str(intent["event_id"])
    attempted = _PUBLISH_ATTEMPTED.get()
    if attempted is not None:
        attempted.add(event_id)
    recorded = {key: value for key, value in intent["event"].items() if key != "connector"}
    recorded["labels"] = tuple(recorded.get("labels") or ())
    event = github_app.NormalizedPREvent(**recorded)
    marker = _after_post_marker(intent)
    if marker is not None:
        # An earlier poll posted, found the authorization withdrawn, and did
        # not finish its correction: finish it; never post success again.
        return _correct_after_post(transport, intent, event, marker)
    check = {
        "repo": event.repo,
        "head_sha": event.head_sha,
        "name": github_app.CHECK_RUN_NAME,
        "conclusion": "success",
        "description": intent["description"],
        "check_run_id": intent.get("check_run_id"),
        "external_id": _intent_external_id(intent),
    }
    state: dict[str, Any] = {}

    def publish() -> None:
        if reconcile:
            # Raises when absence cannot be proven: the intent stays open.
            found, found_id = _locate_published(transport, check)
            if found:
                if _postverified(intent) is not None:
                    state["public"] = True  # verified: only its evidence is missing
                    return
                # Uncertain: posted, but never verified afterwards. Re-verify
                # now; a withdrawn success is corrected, not completed.
                run_id = intent.get("check_run_id") or found_id or None
                reason = _withdrawal_reason(intent)
                if reason is not None:
                    state["after_post"] = (reason, run_id)
                else:
                    state["verified"] = run_id
                return
        with (
            credential_fence.authority_fence(),
            github_app.request_timeout(POLICY_POST_TIMEOUT_S),
        ):
            # Every POST, the first included, is re-authorized right before it
            # is sent (round 3), with hub bearer changes fenced out (round 4).
            reason = _withdrawal_reason(intent)
            if reason is not None:
                state["withdrawn"] = reason
                return
            if intent.get("check_run_id"):
                transport.update_check_run(
                    repo=event.repo,
                    check_run_id=intent["check_run_id"],
                    status="completed",
                    conclusion="success",
                    description=intent["description"],
                )
                run_id = intent["check_run_id"]
            else:
                run_id = transport.create_check_run(
                    repo=event.repo,
                    head_sha=event.head_sha,
                    name=github_app.CHECK_RUN_NAME,
                    status="completed",
                    conclusion="success",
                    details_url=event.url,
                    description=intent["description"],
                    external_id=check["external_id"],
                )
            # Residual window: a change outside hub code may have landed
            # between the check above and GitHub accepting the POST.
            after = _withdrawal_reason(intent)
            if after is not None:
                state["after_post"] = (after, run_id)
            else:
                state["verified"] = run_id

    if isinstance(transport, github_app.GitHubUserTokenTransport):
        with github_app._user_status_lock(event):
            publish()
    else:
        publish()
    approval_id = intent.get("approval_id")
    authorization = intent["authorization"]
    if "withdrawn" in state:
        return _withdraw_policy_intent(intent, state["withdrawn"])
    if "after_post" in state:
        reason, run_id = state["after_post"]
        marker = _record_after_post(intent, reason, run_id)
        return _correct_after_post(transport, intent, event, marker)
    if "verified" in state:
        # Durable before the outcome: if the outcome write fails, the retry
        # completes the evidence of a verified publication.
        _record_postverified(intent, state["verified"])
    outcome_id = _record_policy_outcome(event_id, approval_id, authorization)
    return _policy_result(event_id, approval_id, authorization, outcome_id)


def _locate_published(
    transport: github_app.GitHubAppTransport, check: dict[str, Any]
) -> tuple[bool, str | None]:
    """Whether the success is on GitHub, and the App run id when one is known."""
    locate = getattr(transport, "locate_completed_check", None)
    if locate is not None:
        run_id = locate(**check)
        return run_id is not None, (run_id or None)
    finder = getattr(transport, "find_completed_check", None)
    if finder is None:
        raise RuntimeError("transport cannot reconcile an unfinished policy publication")
    return bool(finder(**check)), None


def _postverified_key(intent: dict[str, Any]) -> str:
    return (
        f"github-app:policy_postverified:{intent['event_id']}:"
        f"{intent['authorization']['authorization_id']}"
    )


def _postverified(intent: dict[str, Any]) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE dedupe_key=? LIMIT 1",
            (_postverified_key(intent),),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["payload"]) if row else None


def _record_postverified(intent: dict[str, Any], run_id: Any) -> None:
    """Durably note a success whose authorization still verified after its POST."""
    authorization = intent["authorization"]
    events.record_event(
        POLICY_POSTVERIFIED_CHANNEL,
        payload={
            "event_id": str(intent["event_id"]),
            "approval_id": intent.get("approval_id"),
            "repo": intent["repo"],
            "number": intent["number"],
            "head_sha": intent["head_sha"],
            "authorization_id": authorization["authorization_id"],
            "policy_ref": authorization["policy_ref"],
            "check_run_id": None if run_id is None else str(run_id),
        },
        originator=config.verified_agent_identity(),
        dedupe_key=_postverified_key(intent),
    )


def _after_post_key(intent: dict[str, Any]) -> str:
    return (
        f"github-app:policy_withdrawn_after_post:{intent['event_id']}:"
        f"{intent['authorization']['authorization_id']}"
    )


def _after_post_marker(intent: dict[str, Any]) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE dedupe_key=? LIMIT 1", (_after_post_key(intent),)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["payload"]) if row else None


def _record_after_post(intent: dict[str, Any], reason: str, run_id: Any) -> dict[str, Any]:
    """Durably note a success posted under a since-withdrawn authorization."""
    authorization = intent["authorization"]
    payload = {
        "event_id": str(intent["event_id"]),
        "approval_id": intent.get("approval_id"),
        "repo": intent["repo"],
        "number": intent["number"],
        "head_sha": intent["head_sha"],
        "authorization_id": authorization["authorization_id"],
        "policy_ref": authorization["policy_ref"],
        "reason": reason,
        "check_run_id": None if run_id is None else str(run_id),
    }
    events.record_event(
        POLICY_WITHDRAWN_AFTER_POST_CHANNEL,
        payload=payload,
        originator=config.verified_agent_identity(),
        dedupe_key=_after_post_key(intent),
    )
    return payload


def _correct_after_post(
    transport: github_app.GitHubAppTransport,
    intent: dict[str, Any],
    event: github_app.NormalizedPREvent,
    marker: dict[str, Any],
) -> dict[str, Any]:
    """Publish the corrective non-success conclusion, then close the intent.

    The same bound status, the same App check run (by the id the success POST
    returned), or a newer commit status on the same context. Idempotent, so a
    failed correction is simply repeated by the next poll.
    """

    def post() -> None:
        bound = intent.get("check_run_id")
        run_id = marker.get("check_run_id")
        if bound or (run_id and not isinstance(transport, github_app.GitHubUserTokenTransport)):
            transport.update_check_run(
                repo=event.repo,
                check_run_id=bound or run_id,
                status="completed",
                conclusion="cancelled",
                description=POLICY_CORRECTION_DESCRIPTION,
            )
            return
        transport.create_check_run(
            repo=event.repo,
            head_sha=event.head_sha,
            name=github_app.CHECK_RUN_NAME,
            status="completed",
            conclusion="cancelled",
            details_url=event.url,
            description=POLICY_CORRECTION_DESCRIPTION,
            external_id=_intent_external_id(intent),
        )

    if isinstance(transport, github_app.GitHubUserTokenTransport):
        with github_app._user_status_lock(event):
            post()
    else:
        post()
    return _withdraw_policy_intent(
        intent,
        f"{marker['reason']}; corrective status published after the success POST",
        status="policy_withdrawn_after_post",
    )


def _intent_external_id(intent: dict[str, Any]) -> str:
    """The stable ``external_id`` a policy check run carries for its intent."""
    key = f"{intent['event_id']}\0{intent['authorization']['authorization_id']}"
    return "ua-policy-" + hashlib.sha256(key.encode()).hexdigest()[:40]


def _withdrawal_reason(intent: dict[str, Any]) -> str | None:
    """Why an intent may not be posted now, or None if it still may (checked before every POST).

    Re-verified now, not trusted from the saved fields: the signed
    authorization must still verify for this exact head with the same id
    (removing its authorizer from ``UA_POLICY_AUTHORIZERS`` withdraws it), and
    no human may have denied a row of the PR.
    """
    current = github_app.verified_policy_authorization(
        repo=str(intent["repo"]), number=int(intent["number"]), head_sha=str(intent["head_sha"])
    )
    if (
        current is None
        or current.get("authorization_id") != (intent["authorization"]["authorization_id"])
    ):
        return "policy authorization no longer verifies for this head"
    if _human_denied(int(intent["number"])):
        return "a human denied a row of this pull request"
    return None


def _withdraw_policy_intent(
    intent: dict[str, Any], reason: str, *, status: str = "policy_withdrawn"
) -> dict[str, Any]:
    """Record that an intent is cancelled; no (further) success will be posted."""
    authorization = intent["authorization"]
    event_id = str(intent["event_id"])
    outcome_id = events.record_event(
        POLICY_WITHDRAWN_CHANNEL,
        payload={
            "event_id": event_id,
            "approval_id": intent.get("approval_id"),
            "repo": intent["repo"],
            "number": intent["number"],
            "head_sha": intent["head_sha"],
            "authorization_id": authorization["authorization_id"],
            "policy_ref": authorization["policy_ref"],
            "reason": reason,
        },
        originator=config.verified_agent_identity(),
        dedupe_key=f"github-app:policy_withdrawn:{event_id}:{authorization['authorization_id']}",
    )
    return {
        "status": status,
        "event_id": event_id,
        "approval_id": intent.get("approval_id"),
        "authorization_id": authorization["authorization_id"],
        "reason": reason,
        "outcome_id": outcome_id,
    }


def _intent_open(intent: dict[str, Any]) -> bool:
    """Committed, and neither published (an outcome) nor withdrawn."""
    event_id = str(intent.get("event_id"))
    if _record(github_app.POLICY_AUTHORIZED_CHANNEL, event_id) is not None:
        return False
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM event_log WHERE dedupe_key=? LIMIT 1",
            (
                f"github-app:policy_withdrawn:{event_id}:"
                f"{intent['authorization']['authorization_id']}",
            ),
        ).fetchone()
    finally:
        conn.close()
    return row is None


def _unfinished_policy_intent_rows() -> list[tuple[str, dict[str, Any]]]:
    """``(ulid, intent)`` for this repository's open publications, in commit order."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid, payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.repo')=? ORDER BY ulid",
            (POLICY_INTENT_CHANNEL, _repo()),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        intent = json.loads(row["payload"])
        if _intent_open(intent):
            out.append((str(row["ulid"]), intent))
    return out


def _unfinished_policy_intents() -> list[dict[str, Any]]:
    """This repository's committed publications with no policy outcome yet, oldest first."""
    return [intent for _ulid, intent in _unfinished_policy_intent_rows()]


#: Where each retry queue stands (PR #1302 review). Unfinished publications are
#: retried in rounds, in commit (event-log ULID) order. A round covers the
#: publications open when it started (``round_end`` is the newest of them), so
#: one committed later waits for the next round and arrivals can never overtake
#: older work. ``cursor`` is the last publication the round attempted; it is
#: saved immediately before each attempt, so a retry that fails, or even
#: kills the process, costs only its own turn in the round. Which queue is
#: attempted first alternates every poll (``lead``, saved before the first
#: attempt), so a retry that keeps killing the process in one queue cannot keep
#: the other queue from its turn. The state is two (cursor, round_end) pairs
#: and the lead, so its size does not grow with the backlog, and it uses no
#: clock, so a wall-clock step cannot reorder it.
POLICY_RETRIES_FILE = "approval-ledger-policy-retries.json"
#: The one size limit for that file, enforced by reader and writer alike.
POLICY_RETRIES_MAX_BYTES = 4096
#: Of each poll's retry slots, at most this many go first to pending
#: corrections (a success already public under a withdrawn authorization).
#: The rest go to plain retries, so whenever one is open it gets at least one
#: slot every poll; with none open, corrections may use every slot.
POLICY_CORRECTION_SLOTS_PER_POLL = POLICY_PUBLICATION_RETRIES_PER_POLL - 1
_RETRY_QUEUES = ("corrections", "plain")

_Row = tuple[str, dict[str, Any]]
_RetryState = dict[str, Any]


def policy_retries_path():
    return config.data_home() / POLICY_RETRIES_FILE


def _empty_retry_state() -> _RetryState:
    state: _RetryState = {queue: {"cursor": None, "round_end": None} for queue in _RETRY_QUEUES}
    state["lead"] = _RETRY_QUEUES[0]
    return state


def _load_retry_state(repo: str) -> _RetryState:
    """Each queue's ``{cursor, round_end}`` for ``repo``; bad state starts a fresh round.

    Disposable scheduling state: a file over ``POLICY_RETRIES_MAX_BYTES``,
    unparseable, or for another repository, and any value that is not a
    canonical ULID, read as no round in progress.
    """
    state = _empty_retry_state()
    path = policy_retries_path()
    try:
        if path.stat().st_size > POLICY_RETRIES_MAX_BYTES:
            return state
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return state
    if not isinstance(record, dict) or record.get("repo") != repo:
        return state
    if record.get("lead") in _RETRY_QUEUES:
        state["lead"] = record["lead"]
    for queue in _RETRY_QUEUES:
        saved = record.get(queue)
        if not isinstance(saved, dict):
            continue
        for field in ("cursor", "round_end"):
            if events.is_ulid(saved.get(field)):
                state[queue][field] = saved[field]
    return state


def _save_retry_state(repo: str, state: _RetryState) -> None:
    """Atomically record the queue positions; a failed write only costs fairness."""
    data = json.dumps({"repo": repo, **state})
    if len(data.encode("utf-8")) > POLICY_RETRIES_MAX_BYTES:
        # Only an oversized repository name can get here; the reader would
        # discard such a file, so keep the previous one instead.
        LOGGER.warning("policy publication retry state exceeds its size limit; not saved")
        return
    path = policy_retries_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(data, encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        LOGGER.warning("could not record the policy publication retry state", exc_info=True)


def _round_pick(
    rows: list[_Row], position: dict[str, str | None], limit: int
) -> list[tuple[_Row, str]]:
    """Up to ``limit`` rows of one queue in round order, each with its round's end.

    Serves the current round (after ``cursor``, up to ``round_end``) in ULID
    order. When it is exhausted a new round starts over every row open now,
    at most once per call, so no row is picked twice in one poll.
    """
    picked: list[tuple[_Row, str]] = []
    if limit <= 0 or not rows:
        return picked
    cursor, end = position["cursor"], position["round_end"]
    taken: set[str] = set()
    restarted = False
    while len(picked) < limit:
        row = next(
            (
                row
                for row in rows
                if row[0] not in taken
                and end is not None
                and row[0] <= end
                and (cursor is None or row[0] > cursor)
            ),
            None,
        )
        if row is None:
            if restarted:
                break
            restarted = True
            cursor, end = None, rows[-1][0]
            continue
        picked.append((row, end))
        taken.add(row[0])
        cursor = row[0]
    return picked


def _needs_correction(intent: dict[str, Any]) -> bool:
    """Whether a retry belongs in the priority (correction) queue.

    A durable after-post marker means a withdrawn success is public. An
    intent whose authorization no longer verifies may be one too (its POST
    response or marker write was lost), or else needs only its withdrawal
    recorded; either way it should be settled promptly. A check that cannot
    be made errs toward priority.
    """
    if _after_post_marker(intent) is not None:
        return True
    try:
        return _withdrawal_reason(intent) is not None
    except Exception:
        LOGGER.warning("could not re-verify policy publication %s", intent.get("event_id"))
        return True


def _retry_order(rows: list[_Row], state: _RetryState) -> list[tuple[str, _Row, str]]:
    """This poll's retries as ``(queue, row, round_end)``, at most the per-poll budget.

    Selection: retries that need a correction (``_needs_correction``) get up
    to ``POLICY_CORRECTION_SLOTS_PER_POLL`` slots; plain retries take the
    rest, and every slot when no correction is pending. With no plain retry
    open, corrections may use every slot.

    Attempt order: the queue named by ``state["lead"]`` goes first, and the
    lead flips for the next poll (the caller saves that before the first
    attempt). A retry that kills the process every time therefore cannot
    keep the other queue from being attempted: each queue leads every other
    poll.

    Latency, counted in polls that complete their attempts in that queue: a
    retry is attempted within ceil(R / s) such polls of its round starting,
    where R is the round's size and s the queue's slots per poll (4 for
    corrections while plain retries are open, at least 1 for plain retries).
    One committed mid-round first waits out the A members left in the current
    round, then its position B in the next, so at most ceil(A / s) +
    ceil(B / s) such polls; new arrivals never overtake it. When attempts
    kill the process the guarantee is weaker: each non-empty queue leads at
    least every other poll and its leading turn attempts at least one retry,
    so the bound becomes A + B leading turns (up to 2 x (A + B) polls),
    provided the retry state saves. These are bounds in polls, not a
    wall-clock guarantee: the poll cadence sets the time.
    """
    corrections, plain = [], []
    # One hash-chain verification for the whole classification, not one per
    # intent (PR #1302 review: O(intents x ledger) before any retry ran).
    # Scheduling only: every POST still re-verifies afresh.
    with github_app.shared_chain_verification():
        for row in rows:
            (corrections if _needs_correction(row[1]) else plain).append(row)
    budget = POLICY_PUBLICATION_RETRIES_PER_POLL
    correction_slots = (
        budget if not plain else min(len(corrections), POLICY_CORRECTION_SLOTS_PER_POLL)
    )
    first = [
        ("corrections", row, end)
        for row, end in _round_pick(corrections, state["corrections"], correction_slots)
    ]
    second = [
        ("plain", row, end) for row, end in _round_pick(plain, state["plain"], budget - len(first))
    ]
    lead = state.get("lead") if state.get("lead") in _RETRY_QUEUES else _RETRY_QUEUES[0]
    state["lead"] = "plain" if lead == "corrections" else "corrections"
    return first + second if lead == "corrections" else second + first


def _finish_policy_publications(transport: github_app.GitHubAppTransport) -> list[dict[str, Any]]:
    """Complete publications an earlier poll committed but did not finish.

    Retried in ``_retry_order``. Each queue's position, and the flipped lead,
    is saved immediately before that publication's attempt, never ahead of
    it, so a retry that fails or kills the process passes only its own turn
    on, the rest of the batch is still due, and the next poll starts with the
    other queue. Each retry is still the fail-closed
    reconcile-first publication. A GitHub backoff (task #2237) stops the
    pass: the request that started it keeps its turn, and an attempt refused
    unsent puts the saved position back, so it is next once the window ends.
    """
    attempted = _PUBLISH_ATTEMPTED.get() or set()
    repo = _repo()
    rows = [
        row
        for row in _unfinished_policy_intent_rows()
        if str(row[1].get("event_id")) not in attempted
    ]
    if not rows:
        return []
    state = _load_retry_state(repo)
    # What to save back if an attempt is refused unsent (task #2237): the
    # state before that attempt, so a refusal never uses up a turn.
    restore = _copy_retry_state(state)
    results: list[dict[str, Any]] = []
    # _retry_order flips the lead in ``state``; the first save below records
    # it before any attempt can kill the process.
    for queue, (ulid, intent), round_end in _retry_order(rows, state):
        state[queue] = {"cursor": ulid, "round_end": round_end}
        _save_retry_state(repo, state)
        try:
            results.append(_publish_policy_intent(transport, intent, reconcile=True))
            _retire_older_heads(transport, int(intent["number"]), str(intent["head_sha"]))
        except github_app.GitHubBackoff as exc:
            # Stop the pass. The request that started the backoff keeps its
            # turn; a refused one sent nothing and stays next in its round.
            if not exc.sent:
                _save_retry_state(repo, restore)
            results.append(_backoff_result(exc, event_id=intent.get("event_id")))
            break
        except Exception as exc:
            LOGGER.exception("policy publication for %s is still unfinished", intent["event_id"])
            results.append(
                {
                    "status": "error",
                    "event_id": intent.get("event_id"),
                    "approval_id": intent.get("approval_id"),
                    "error": type(exc).__name__,
                }
            )
        restore = _copy_retry_state(state)
    return results


def _copy_retry_state(state: _RetryState) -> _RetryState:
    return {key: dict(value) if isinstance(value, dict) else value for key, value in state.items()}


def _backoff_result(exc: github_app.GitHubBackoff, **fields: Any) -> dict[str, Any]:
    """The poll result for work stopped because GitHub has the credential backing off."""
    return {"status": "backoff", **fields, "retry_after_s": exc.retry_after_s}


def _backing_off(transport: github_app.GitHubAppTransport) -> dict[str, Any] | None:
    wait = github_app.github_backoff_remaining(transport)
    return {"status": "backoff", "retry_after_s": math.ceil(wait)} if wait > 0 else None


def _settle_unseen_merged_head(
    transport: github_app.GitHubAppTransport,
    snapshot: pr_watch.PRSnapshot,
    changed_files: Callable[[int], list[str]],
    *,
    orphan: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """A head merged before any poll recorded it, covered by a policy.

    The merged head gets a terminal policy-authorized status of its own, then
    older heads of the PR are retired as superseded. That order matters: the
    older pending rows are how the next poll finds this closed PR again, so
    they are retired only once the merged head's outcome is durable, and a
    failed status post or outcome write is retried. Without a valid
    authorization this does nothing and the existing close-out runs.

    The decision is committed (``_commit_policy_intent``, fenced against a
    human denial) before anything is posted, and the post itself is
    reconciled against GitHub on any retry (PR #1302 review). ``orphan`` is
    the head's own row whose pending-status POST failed (task #2103): the same
    transaction retires it, its ``orphan_retired`` result is appended to
    ``results``, and the outcome cites it.
    """
    event = _event(snapshot)
    intent = _policy_intent(event.event_id)
    if intent is not None and _intent_open(intent) and not _settled(event.event_id):
        # Committed by an earlier poll: reconcile, re-validate if unposted.
        result = _publish_policy_intent(transport, intent, reconcile=True)
        _retire_older_heads(transport, snapshot.number, snapshot.head_sha)
        return result
    authorization = _policy_authorization(
        snapshot, snapshot.head_sha, merged_head_sha=snapshot.head_sha, changed_files=changed_files
    )
    if authorization is None:
        return None
    if _settled(event.event_id):
        # An earlier poll recorded the outcome but did not finish retiring.
        _retire_older_heads(transport, snapshot.number, snapshot.head_sha)
        return None
    committed = _commit_policy_intent(
        event,
        authorization,
        row=orphan,
        orphan_snapshot=snapshot if orphan is not None else None,
    )
    if committed is None:
        return None
    intent, orphan_result = committed
    if orphan_result is not None and results is not None:
        results.append(orphan_result)
    result = _publish_policy_intent(transport, intent, reconcile=False)
    _retire_older_heads(transport, snapshot.number, snapshot.head_sha)
    return result


def _gh_pr_files(run_gh: Callable[..., Any], number: int) -> list[str]:
    proc = run_gh(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{_repo()}/pulls/{number}/files",
            "--jq",
            ".[].filename",
        ],
        capture_output=True,
        text=True,
        timeout=pr_watch.GH_TIMEOUT_SECONDS,
    )
    if proc.returncode:
        raise RuntimeError(f"GitHub PR files read failed for #{number}")
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _brokered_pr_files(token: str, number: int) -> list[str]:
    files: list[str] = []
    for page in range(1, _MAX_PR_FILES // 100 + 1):
        payload = _brokered_github_json(
            token,
            f"/repos/{_repo()}/pulls/{number}/files",
            params={"per_page": 100, "page": page},
        )
        if not isinstance(payload, list):
            raise RuntimeError("GitHub PR files were invalid")
        files.extend(str(item["filename"]) for item in payload if item.get("filename"))
        if len(payload) < 100:
            break
    return files


def _pass_still_valid(approval: dict[str, Any]) -> bool:
    from datetime import UTC, datetime

    expires_at = approvals._pass_expires_at(approval)
    return expires_at is not None and datetime.now(UTC) < expires_at


def _expire_approved(approval_id: int, reason: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET status='expired', updated_at=?, reason=? "
            "WHERE id=? AND status='approved'",
            (storage.now(), reason, approval_id),
        )
        conn.commit()
    finally:
        conn.close()


def _close_without_approval(
    transport: github_app.GitHubAppTransport,
    pending: dict[str, Any],
    snapshot: pr_watch.PRSnapshot,
    *,
    conclusion: str | None = None,
) -> dict[str, Any]:
    event = pending["event"]
    approval_id = int(pending["approval_id"])
    merged = snapshot.state == "MERGED" or bool(snapshot.merged_at)
    outcome = "merged_without_approval" if merged else "closed_without_approval"
    reason = "merged without human approval" if merged else "closed without human approval"
    _retire_approval(approval_id, reason)
    _finish_check(transport, pending, conclusion or ("neutral" if merged else "skipped"))
    _record_outcome(github_app.CLOSED_UNAPPROVED_CHANNEL, event["event_id"], approval_id, outcome)
    return {"status": outcome, "event_id": event["event_id"], "approval_id": approval_id}


def _approved_github_passes() -> list[tuple[datetime | None, bool, bool]]:
    """``(granted_at, expired, unreachable)`` for each approved, unconsumed github pass.

    The rows ``approval_pending(tool=TOOL_NAME)`` reads: same table,
    ``status='approved'``, same tenant filter, and the same TTL rule
    (``approvals._pass_expires_at``; an undateable pass counts as expired).

    ``unreachable`` (task #2616) is true when the last poll's open-head
    snapshot is fresh and does not list the pass's PR at the pass's head: the
    PR merged, closed or moved on, so no poll can spend the pass. The orphan
    sweep retires such a pass once its TTL ends (task #2103), never before. With no
    fresh snapshot nothing is marked unreachable, so a broken poller still
    shows its backlog.
    """
    post_init.register_default_hooks()
    storage.init_db()
    now = datetime.now(UTC)
    repo: str | None
    try:
        repo = _repo()
    except ValueError:
        repo = None
    snapshot = _open_heads_snapshot(repo) if repo else None
    heads = snapshot[1] if snapshot is not None else None
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, created_at, resolved_at FROM approvals "
            f"WHERE {approvals._TENANT_SQL} AND status='approved' AND tool=?",
            (approvals._tenant(), github_app.TOOL_NAME),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        expires_at = approvals._pass_expires_at(row)
        granted = approvals._parse_ts(row["resolved_at"]) or approvals._parse_ts(row["created_at"])
        expired = expires_at is None or now >= expires_at
        out.append((granted, expired, not expired and _pass_unreachable(row, repo, heads)))
    return out


def _pass_unreachable(row: Any, repo: str | None, heads: dict[str, str] | None) -> bool:
    """Whether a fresh open-head snapshot shows the pass's PR is no longer at its head."""
    if heads is None:
        return False
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("repo") != repo:
        return False
    number, head_sha = payload.get("number"), payload.get("head_sha")
    if isinstance(number, bool) or not isinstance(number, int) or not head_sha:
        return False
    return heads.get(str(number)) != head_sha


def unconsumed_github_passes(*, interval_s: int = POLL_INTERVAL_S) -> dict[str, int]:
    """Approved, unconsumed github passes for this tenant, by age (task #1804).

    Split the way ``approval_pending`` splits them: ``fresh``, ``stale`` and
    ``unreachable`` together are its ``approved_unconsumed`` (live passes),
    ``expired`` its ``expired_passes``. ``stale`` is a live pass granted longer
    than one poll interval ago, which the poller should already have consumed;
    ``fresh`` is one it has not had a poll to reach yet. ``unreachable``
    (task #2616) is a live pass whose PR the last poll saw closed or at another
    head: no poll can consume it, and it is not a poller backlog.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=interval_s)
    counts = {"fresh": 0, "stale": 0, "unreachable": 0, "expired": 0}
    for granted, expired, unreachable in _approved_github_passes():
        if expired:
            counts["expired"] += 1
        elif unreachable:
            counts["unreachable"] += 1
        elif granted is None or granted <= cutoff:
            counts["stale"] += 1
        else:
            counts["fresh"] += 1
    return counts


def stale_unconsumed_github_passes(*, interval_s: int = POLL_INTERVAL_S) -> int:
    """Approved github passes older than one poll interval, still unconsumed.

    Live or past their TTL: a pass that outlived its TTL unconsumed is a
    backlog the poller never cleared, even though it no longer authorizes.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=interval_s)
    return sum(
        1
        for granted, _expired, _unreachable in _approved_github_passes()
        if granted is None or granted <= cutoff
    )


#: Open PR heads the last successful poll saw (task #1867): ``ledger_in_flight``
#: counts only rows for these heads, so a row the worker can no longer reach (its
#: PR closed, or a newer head was pushed) cannot pin the fast cadence.
OPEN_HEADS_FILE = "approval-ledger-open-heads.json"
#: A snapshot older than this no longer vouches for any head: two idle intervals.
OPEN_HEADS_MAX_AGE_S = 2 * pr_watch.SCHEDULE_INTERVAL_S


def open_heads_path():
    return config.data_home() / OPEN_HEADS_FILE


def _save_open_heads(repo: str, heads: dict[int, str], *, at: datetime | None = None) -> None:
    """Atomically record the open PR heads one poll saw; a failed write is only logged."""
    path = open_heads_path()
    record = {
        "repo": repo,
        "at": (at or datetime.now(UTC)).isoformat(),
        "heads": {str(number): sha for number, sha in heads.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        LOGGER.warning("could not record the Approval Ledger open-head snapshot", exc_info=True)


def _load_open_heads(repo: str) -> dict[str, str] | None:
    """The last poll's ``{number: head_sha}`` for ``repo``, or None if absent or stale."""
    snapshot = _open_heads_snapshot(repo)
    return None if snapshot is None else snapshot[1]


def _open_heads_snapshot(repo: str) -> tuple[str, dict[str, str]] | None:
    """When the last poll started and the heads it saw; None if absent or stale."""
    try:
        record = json.loads(open_heads_path().read_text(encoding="utf-8"))
        at = datetime.fromisoformat(record["at"])
        heads = record["heads"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if record.get("repo") != repo or not isinstance(heads, dict):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - at).total_seconds()
    if age < 0 or age > OPEN_HEADS_MAX_AGE_S:
        return None
    return at.isoformat(), {str(number): str(sha) for number, sha in heads.items()}


def _correction_pending() -> bool:
    """Whether an open publication has a public success under a withdrawn authorization.

    Its durable after-post marker says so; such a correction keeps the
    active cadence until it lands (PR #1302 review). Local, indexed reads
    only: no chain verification and no GitHub call.
    """
    return any(
        _after_post_marker(intent) is not None for _ulid, intent in _unfinished_policy_intent_rows()
    )


def ledger_in_flight() -> bool:
    """Whether the Approval Ledger has a row the next poll can still act on (task #1867).

    A ``github_app.process_pr_event`` row of this tenant and the configured
    repository whose head the last poll saw open and current
    (``OPEN_HEADS_FILE``, at most ``OPEN_HEADS_MAX_AGE_S`` old), and that is
    ``pending`` (a human has not decided; a push may supersede it) or an
    ``approved`` pass still inside its TTL (the worker must consume it). The
    pr-watch schedule polls at its active interval while this holds.

    A pending correction (an open publication whose success is public under a
    withdrawn authorization, ``_correction_pending``) also counts, so it is
    retried at the active interval rather than the idle one.

    Rows the worker cannot reach do not count: a row for a closed PR or an
    older head, an approved pass past its TTL, or any row when the snapshot is
    missing or stale (polls keep failing). Those fall back to the idle
    cadence. Local reads only: no GitHub call. False when the connector is off
    or has no repository.

    A review read recorded since that poll also counts, for an open PR
    (task #2320): the read changes the ``unified-agent/review-head`` answer,
    and nothing else would wake an idle schedule to publish it. The next poll
    rewrites the snapshot, so one read buys one poll.
    """
    if not github_app.enabled():
        return False
    try:
        repo = _repo()
    except ValueError:
        return False
    if _correction_pending():
        return True
    snapshot = _open_heads_snapshot(repo)
    if snapshot is None or not snapshot[1]:
        return False
    polled_at, heads = snapshot
    if config.review_head_check_enabled() and any(
        str(number) in heads for number in review_reads.prs_read_since(repo, polled_at)
    ):
        return True
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT status, payload, created_at, resolved_at FROM approvals "
            f"WHERE {approvals._TENANT_SQL} AND tool=? AND status IN ('pending', 'approved') "
            "AND json_extract(payload, '$.repo')=?",
            (approvals._tenant(), github_app.TOOL_NAME, repo),
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(UTC)
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if heads.get(str(payload.get("number"))) != payload.get("head_sha"):
            continue
        if row["status"] == "approved":
            expires_at = approvals._pass_expires_at(row)
            if expires_at is None or now >= expires_at:
                continue
        return True
    return False


# --- orphan rows (task #2103) ----------------------------------------------------------

#: Ledger channel for a live approval row retired because its PR closed, or moved
#: past the row's head, while no pending-status marker named the row (task #2103).
ORPHAN_RETIRED_CHANNEL = "connector.github.orphan_retired"
#: GitHub PR reads the orphan sweep may make per poll; the rest wait a poll.
ORPHAN_SWEEP_MAX_PRS = 10
_ORPHAN_REASONS = {
    "closed": "pull request closed before its Approval Ledger status was recorded",
    "merged": "pull request merged before its Approval Ledger status was recorded",
    "superseded": "superseded by a newer pull request head before its status was recorded",
}
#: When the sweep last read each candidate PR (task #2103). The sweep reads the
#: least recently checked PRs first, so orphans it can never retire (a PR read
#: that keeps failing, a PR open at the row's head off the open list) rotate to
#: the back instead of spending every poll's reads ahead of newer orphans.
ORPHAN_CHECKS_FILE = "approval-ledger-orphan-checks.json"
#: A larger rotation file is ignored rather than parsed.
ORPHAN_CHECKS_MAX_BYTES = 1024 * 1024
#: Clock skew tolerated for a recorded read time; later ones count as never read.
_ORPHAN_CHECK_SKEW_S = 24 * 3600


def orphan_checks_path():
    return config.data_home() / ORPHAN_CHECKS_FILE


def _load_orphan_checks(repo: str) -> dict[int, float]:
    """``{number: last read, epoch seconds}`` for ``repo``; bad state is dropped.

    The file is disposable rotation state (PR #1302 review): one over
    ``ORPHAN_CHECKS_MAX_BYTES``, unparseable, or for another repository reads
    as empty, and any entry that is not a positive PR number with a finite
    time between the epoch and a day from now is treated as never checked.
    """
    path = orphan_checks_path()
    try:
        if path.stat().st_size > ORPHAN_CHECKS_MAX_BYTES:
            return {}
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(record, dict) or record.get("repo") != repo:
        return {}
    checked = record.get("checked")
    if not isinstance(checked, dict):
        return {}
    ceiling = datetime.now(UTC).timestamp() + _ORPHAN_CHECK_SKEW_S
    out: dict[int, float] = {}
    for key, value in checked.items():
        if not (isinstance(key, str) and key.isascii() and key.isdigit() and len(key) <= 12):
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        try:
            at = float(value)
        except OverflowError:
            continue
        number = int(key)
        if number <= 0 or not math.isfinite(at) or at < 0 or at > ceiling:
            continue
        out[number] = at
    return out


def _save_orphan_checks(repo: str, checked: dict[int, float]) -> None:
    """Atomically record the sweep's read times; a failed write is only logged.

    Losing the file costs fairness for one poll, never safety: the sweep then
    falls back to oldest rows first.
    """
    path = orphan_checks_path()
    record = {"repo": repo, "checked": {str(number): at for number, at in checked.items()}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        LOGGER.warning("could not record the Approval Ledger orphan-check times", exc_info=True)


def _orphan_candidates() -> dict[int, list[dict[str, Any]]]:
    """Unbound live rows of this tenant and repository, by PR number, oldest first.

    The rows ``_unresolved_pending`` cannot reach: ``pending``, or ``approved``
    past its pass TTL (an undateable pass counts as expired), for which no
    unresolved pending-status marker names the row. A row a marker binds stays
    with the existing close-out, and a live human pass is never discarded
    before its TTL.
    """
    bound = {
        int(marker["approval_id"])
        for marker in _unresolved_pending()
        if marker.get("approval_id") is not None
    }
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, status, payload, created_at, resolved_at FROM approvals "
            f"WHERE {approvals._TENANT_SQL} AND tool=? AND status IN ('pending', 'approved') "
            "AND json_extract(payload, '$.repo')=? ORDER BY id",
            (approvals._tenant(), github_app.TOOL_NAME, _repo()),
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(UTC)
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        approval_id = int(row["id"])
        if approval_id in bound:
            continue
        if row["status"] == "approved":
            expires_at = approvals._pass_expires_at(row)
            if expires_at is not None and now < expires_at:
                continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        number = payload.get("number")
        head_sha = payload.get("head_sha")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            continue
        out.setdefault(number, []).append(
            {
                "id": approval_id,
                "status": row["status"],
                "head_sha": head_sha,
                "event_id": str(payload.get("event_id") or ""),
            }
        )
    return out


def _orphan_outcome(snapshot: pr_watch.PRSnapshot, head_sha: str) -> str | None:
    """Why a row for ``head_sha`` can be retired, or None to keep it.

    Only GitHub's own word retires a row: the PR merged or closed, or it is
    open at another head. A PR open at the row's head, or in any state this
    does not recognise, keeps the row.
    """
    if _is_merged(snapshot):
        return "merged"
    if snapshot.state == "CLOSED":
        return "closed"
    if snapshot.state == "OPEN" and snapshot.head_sha and snapshot.head_sha != head_sha:
        return "superseded"
    return None


def _orphan_payload(
    row: dict[str, Any],
    snapshot: pr_watch.PRSnapshot,
    outcome: str,
    reason: str,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "approval_id": row["id"],
        "event_id": row["event_id"],
        "repo": _repo(),
        "number": snapshot.number,
        "head_sha": row["head_sha"],
        "previous_status": row["status"],
        "outcome": outcome,
        "reason": reason,
        "pr_state": "MERGED" if _is_merged(snapshot) else snapshot.state,
        "pr_head_sha": snapshot.head_sha,
    }
    if policy is not None:
        payload["policy_ref"] = policy.get("policy_ref")
        payload["authorization_id"] = policy.get("authorization_id")
    return payload


def _orphan_result(
    row: dict[str, Any], snapshot: pr_watch.PRSnapshot, outcome: str, ulid: str
) -> dict[str, Any]:
    return {
        "status": "orphan_retired",
        "approval_id": row["id"],
        "event_id": row["event_id"],
        "number": snapshot.number,
        "head_sha": row["head_sha"],
        "outcome": outcome,
        "outcome_id": ulid,
    }


def _expire_row_on(conn: Any, row: dict[str, Any], reason: str) -> bool:
    """Status CAS inside ``conn``'s transaction: expire the row only as the sweep read it."""
    changed = conn.execute(
        "UPDATE approvals SET status='expired', updated_at=?, reason=? "
        f"WHERE id=? AND status=? AND tool=? AND {approvals._TENANT_SQL}",
        (
            storage.now(),
            reason,
            row["id"],
            row["status"],
            github_app.TOOL_NAME,
            approvals._tenant(),
        ),
    )
    return changed.rowcount == 1


def _retire_orphan_row(
    row: dict[str, Any],
    snapshot: pr_watch.PRSnapshot,
    outcome: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Expire one row and record why, in one transaction; None if it changed first.

    The update is fenced on the status the sweep read, so a human decision
    made meanwhile (HTTP resolution does not take the poll lock) wins.
    ``policy`` is the head's recorded policy-authorized outcome, when another
    path already settled the merged head.
    """
    if policy is not None:
        outcome = "policy_authorized"
        reason = f"merged under policy authorization {policy.get('policy_ref')}"
    else:
        reason = _ORPHAN_REASONS[outcome]
    payload = _orphan_payload(row, snapshot, outcome, reason, policy)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not _expire_row_on(conn, row, reason):
            conn.rollback()
            return None
        ulid, _created, published = events.record_event_once_on_connection(
            conn,
            ORPHAN_RETIRED_CHANNEL,
            payload=payload,
            originator=config.verified_agent_identity(),
            dedupe_key=f"github-app:orphan_retired:{row['id']}",
        )
        conn.commit()
    finally:
        conn.close()
    if published is not None:
        events.publish(published)
    return _orphan_result(row, snapshot, outcome, ulid)


def _settle_orphan_under_policy(
    row: dict[str, Any],
    snapshot: pr_watch.PRSnapshot,
    transport: github_app.GitHubAppTransport,
    changed_files: Callable[[int], list[str]],
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Settle an orphan at the merged head under a policy; the head's outcome, if any.

    The row's own head merged, so it goes through ``_settle_unseen_merged_head``
    as a head no poll recorded. Under a verified, non-red-tier policy
    authorization, with no human denial on the PR, that transaction retires
    the row, commits the publication, then posts the policy-authorized
    success (appending both results). A head another path already settled
    returns its outcome, so the caller retires the row citing it; anything
    else returns None and the row is retired as merged (or kept, if a human
    decided it meanwhile).
    """
    if not _is_merged(snapshot) or row["head_sha"] != snapshot.head_sha:
        return None
    event_id = _event(snapshot).event_id
    if row["event_id"] != event_id:
        return None
    record = _record(github_app.POLICY_AUTHORIZED_CHANNEL, event_id)
    if record is not None:
        return record
    settled = _settle_unseen_merged_head(
        transport, snapshot, changed_files, orphan=row, results=results
    )
    if settled is not None:
        results.append(settled)
    return _record(github_app.POLICY_AUTHORIZED_CHANNEL, event_id)


def _sweep_orphan_rows(
    open_prs: dict[int, pr_watch.PRSnapshot],
    viewed: dict[int, pr_watch.PRSnapshot],
    read_pr: Callable[[int], pr_watch.PRSnapshot | None],
    transport: github_app.GitHubAppTransport,
    changed_files: Callable[[int], list[str]],
) -> list[dict[str, Any]]:
    """Retire live rows the poll cannot otherwise reach (task #2103).

    A row whose pending-status POST failed has no marker, so the closed-PR
    pass above never finds it once its PR leaves the open list. For each such
    PR not in this poll's open list, the sweep makes at most
    ``ORPHAN_SWEEP_MAX_PRS`` GitHub reads per poll, least recently checked PR
    first (``ORPHAN_CHECKS_FILE``; never-checked PRs, then oldest rows, lead),
    and reuses reads this poll already made. The row is expired with an
    ``ORPHAN_RETIRED_CHANNEL`` event when GitHub says the PR closed or moved
    past the row's head. A row for the merged head itself is first settled
    under a policy authorization when one covers it
    (``_settle_orphan_under_policy``). Rows for PRs in the open list are left
    to the open-head path, which binds or supersedes them. A failed or empty
    read keeps the row and is reported as ``orphan_unverified``, not as a poll
    error, so a sweep that cannot read GitHub never fails the poll or pins
    pr-watch's retry cadence. No other GitHub status is posted: the row never
    had one.
    """
    results = _finish_policy_publications(transport)
    if _backing_off(transport) is not None:
        return results  # task #2237: the sweep's reads wait for the window too
    candidates = {
        number: rows for number, rows in _orphan_candidates().items() if number not in open_prs
    }
    repo = _repo()
    try:
        checked = _load_orphan_checks(repo)
    except Exception:  # disposable rotation state never fails a poll
        LOGGER.warning("ignoring unreadable Approval Ledger orphan-check times", exc_info=True)
        checked = {}
    order = sorted(
        candidates,
        key=lambda number: (checked.get(number, float("-inf")), candidates[number][0]["id"]),
    )
    now = datetime.now(UTC).timestamp()
    reads = 0
    for number in order:
        rows = candidates[number]
        snapshot = viewed.get(number)
        if snapshot is None:
            if reads >= ORPHAN_SWEEP_MAX_PRS:
                continue
            reads += 1
            checked[number] = now
            try:
                snapshot = read_pr(number)
            except Exception as exc:
                LOGGER.warning(
                    "Approval Ledger orphan check could not read PR #%s; keeping its rows",
                    number,
                    exc_info=True,
                )
                results.append(
                    {"status": "orphan_unverified", "number": number, "error": type(exc).__name__}
                )
                continue
        if snapshot is None or snapshot.number != number:
            results.append(
                {"status": "orphan_unverified", "number": number, "error": "pr_not_found"}
            )
            continue
        for row in rows:
            outcome = _orphan_outcome(snapshot, row["head_sha"])
            if outcome is None:
                continue
            try:
                policy = None
                if outcome == "merged":
                    policy = _settle_orphan_under_policy(
                        row, snapshot, transport, changed_files, results
                    )
                retired = _retire_orphan_row(row, snapshot, outcome, policy)
            except Exception as exc:
                LOGGER.exception("could not retire orphan approval #%s", row["id"])
                results.append(
                    {
                        "status": "error",
                        "event_id": row["event_id"],
                        "approval_id": row["id"],
                        "error": type(exc).__name__,
                    }
                )
                continue
            if retired is not None:
                results.append(retired)
    # Keep read times only for PRs that are still candidates.
    _save_orphan_checks(repo, {n: at for n, at in checked.items() if n in candidates})
    return results


def _run_once_configured(
    *,
    run_gh: Callable[..., Any] = subprocess.run,
    transport: github_app.GitHubAppTransport | None = None,
    policy_config: policy.PolicyConfig | None = None,
) -> list[dict[str, Any]]:
    """Advance open PR heads, close out merged heads that still have a pass, then
    retire orphan rows whose PR closed without a status marker (task #2103)."""
    if not github_app.enabled():
        return []
    post_init.register_default_hooks()
    storage.init_db()
    # Every approval this poll settles re-reads its PR through this poll's own
    # reader (task #2234), bound once the credential path is known below.
    poll_reader: dict[str, Callable[[int], pr_watch.PRSnapshot | None]] = {}

    def live(event: github_app.NormalizedPREvent) -> pr_watch.PRSnapshot | None:
        return poll_reader["read"](event.number)

    with _poll_lock(), _authenticated_service_identity(), github_app.live_pr(live):
        active_transport = transport or github_app.build_transport(repo=_repo())
        # Task #2237: while GitHub has this credential backing off, the whole
        # pass is skipped (reads share the bucket in user mode). Nothing is
        # published; unfinished work stays queued for the next poll.
        backoff = _backing_off(active_transport)
        if backoff is not None:
            return [backoff]
        brokered_token = None
        if (
            github_app.token_mode() == github_app.TOKEN_MODE_USER
            and github_app.user_token_source() == "connector_oauth"
        ):
            brokered_token = github_app.resolve_user_token()
        results: list[dict[str, Any]] = []

        def changed_files(number: int) -> list[str]:
            if brokered_token is not None:
                return _brokered_pr_files(brokered_token, number)
            return _gh_pr_files(run_gh, number)

        def read_open_pr(number: int) -> pr_watch.PRSnapshot | None:
            if brokered_token is not None:
                return _brokered_view_pr(brokered_token, number)
            return _view_pr(run_gh, number)

        poll_reader["read"] = read_open_pr

        open_prs = (
            _brokered_open_main_prs(brokered_token)
            if brokered_token is not None
            else _open_main_prs(run_gh)
        )
        _save_open_heads(
            _repo(),
            {
                snapshot.number: snapshot.head_sha
                for snapshot in open_prs.values()
                if snapshot.head_sha and snapshot.state == "OPEN"
            },
        )
        # Task #2234: settle consumed approvals that no step finished, by PR
        # number rather than through the base-filtered open list.
        from . import github_app_fastpath

        github_app_fastpath.reconcile_unsettled(active_transport, set(open_prs))
        for snapshot in open_prs.values():
            if not snapshot.head_sha or snapshot.state != "OPEN":
                continue
            # A backoff started during this pass stops it (task #2237).
            backoff = _backing_off(active_transport)
            if backoff is not None:
                results.append(backoff)
                return results
            try:
                _retire_older_heads(
                    active_transport, snapshot.number, snapshot.head_sha, include_unbound=True
                )
                result = _advance_snapshot(
                    snapshot, active_transport, policy_config, allow_closed=False
                )
                if result:
                    results.append(result)
            except github_app.GitHubBackoff as exc:
                results.append(_backoff_result(exc, event_id=_event(snapshot).event_id))
                return results
            except Exception as exc:
                LOGGER.exception("github-app poll failed for PR #%s", snapshot.number)
                results.append(
                    {
                        "status": "error",
                        "event_id": _event(snapshot).event_id,
                        "error": type(exc).__name__,
                    }
                )

        def read_pr(number: int) -> pr_watch.PRSnapshot | None:
            if brokered_token is not None:
                return _brokered_view_pr(brokered_token, number)
            return _view_pr(run_gh, number)

        seen: set[int] = set()
        # PR reads this poll already made, reused by the orphan sweep below.
        viewed: dict[int, pr_watch.PRSnapshot] = {}
        for pending in _unresolved_pending():
            number = int(pending["event"]["number"])
            if number in open_prs or number in seen:
                continue
            backoff = _backing_off(active_transport)
            if backoff is not None:
                results.append(backoff)
                return results
            seen.add(number)
            try:
                snapshot = read_pr(number)
                if snapshot is not None:
                    viewed[number] = snapshot
                if snapshot is None or snapshot.state == "OPEN" or not snapshot.head_sha:
                    continue
                merged_head = snapshot.head_sha
                prior = _pending_for(number, snapshot.head_sha)
                if prior is None:
                    settled = _settle_unseen_merged_head(active_transport, snapshot, changed_files)
                    if settled is not None:
                        results.append(settled)
                        continue
                    prior = _newest_pending(number)
                    if prior is not None:
                        snapshot = replace(snapshot, head_sha=str(prior["event"]["head_sha"]))
                if prior is not None:
                    _retire_older_heads(active_transport, snapshot.number, snapshot.head_sha)
                result = _advance_snapshot(
                    snapshot,
                    active_transport,
                    policy_config,
                    allow_closed=True,
                    merged_head_sha=merged_head,
                    changed_files=changed_files,
                )
                if result:
                    results.append(result)
            except github_app.GitHubBackoff as exc:
                results.append(_backoff_result(exc, event_id=pending["event"].get("event_id", "")))
                return results
            except Exception as exc:
                LOGGER.exception("github-app poll failed for closed PR #%s", number)
                results.append(
                    {
                        "status": "error",
                        "event_id": pending["event"].get("event_id", ""),
                        "error": type(exc).__name__,
                    }
                )
        backoff = _backing_off(active_transport)
        if backoff is not None:
            results.append(backoff)
            return results
        results.extend(
            _sweep_orphan_rows(open_prs, viewed, read_pr, active_transport, changed_files)
        )
        if config.review_head_check_enabled():
            # Last, so nothing here can hold up the ledger work above. Every
            # open head is covered: the list has no path filter, which is why
            # a markdown-only PR that CI skips still gets this check (task #2320).
            # So is a recent read whose own publish never reached GitHub (#2410).
            try:
                heads = {
                    snapshot.number: snapshot.head_sha
                    for snapshot in open_prs.values()
                    if snapshot.head_sha and snapshot.state == "OPEN"
                }
                retried = review_head_status.unpublished_read_heads(
                    _repo(), open_numbers=set(open_prs)
                )
                heads.update(retried)
                results.extend(
                    review_head_status.sync_open_heads(
                        heads,
                        active_transport,
                        repo=_repo(),
                        run_gh=run_gh if brokered_token is None else _brokered_gh(brokered_token),
                        retried=set(retried.values()),
                    )
                )
            except github_app.GitHubBackoff as exc:
                results.append(_backoff_result(exc, event_id=review_head_status.EVENT_ID))
        return results


def publish_review_head(
    *,
    repo: str,
    number: int,
    head_sha: str,
    transport: github_app.GitHubAppTransport | None = None,
    run_gh: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Publish ``unified-agent/review-head`` for one head, outside the open-PR poll.

    The poll covers OPEN pull requests (task #2320). A read recorded after the
    merge therefore had no publisher at all: the head had left the poll's set,
    so its check kept the conclusion it carried before the read — a permanent
    false ERROR for every steward who reads a batch after merging it (task
    #2410).

    The check belongs to the head, so the answer is the poll's: the worst
    across every open PR on that head, plus the PR just read. A head whose
    siblings cannot be listed publishes nothing.

    The caller records the read first and treats this as best-effort: a
    failure is raised to the caller to report, never allowed to undo the row.
    Answer shapes are ``sync_open_heads``'s own (``review_head_posted`` /
    ``review_head_unchanged`` / ``error``), ``{"status": "skipped", ...}`` when
    the switch is off, the credential is backing off or the connector does not
    watch ``repo``, and ``{"status": "deferred", ...}`` when a poll holds the
    lock past the wait; the next poll then delivers this head.
    """
    if not (config.review_head_check_enabled() and github_app.enabled()):
        return {"status": "skipped", "detail": "review-head publishing is off"}
    if repo != _repo():
        return {"status": "skipped", "detail": "the connector does not watch this repository"}
    active_transport = transport or github_app.build_transport(repo=repo)
    backoff = _backing_off(active_transport)
    if backoff is not None:
        return {**backoff, "status": "skipped", "detail": "the GitHub credential is backing off"}
    brokered_token = None
    if (
        github_app.token_mode() == github_app.TOKEN_MODE_USER
        and github_app.user_token_source() == "connector_oauth"
    ):
        brokered_token = github_app.resolve_user_token()
    with _publish_lock() as held:
        if not held:
            return {
                "status": "deferred",
                "detail": "a poll holds the lock; the next poll publishes this head",
            }
        open_prs = (
            _open_main_prs(run_gh)
            if brokered_token is None
            else _brokered_open_main_prs(brokered_token)
        )
        heads = {
            snapshot.number: head_sha
            for snapshot in open_prs.values()
            if snapshot.head_sha == head_sha and snapshot.state == "OPEN"
        }
        heads[number] = head_sha
        results = list(
            review_head_status.sync_open_heads(
                heads,
                active_transport,
                repo=repo,
                run_gh=run_gh if brokered_token is None else _brokered_gh(brokered_token),
            )
        )
    if not results:
        return {"status": "skipped", "detail": "nothing to publish"}
    return results[0]


def run_once(
    *,
    run_gh: Callable[..., Any] = subprocess.run,
    transport: github_app.GitHubAppTransport | None = None,
    policy_config: policy.PolicyConfig | None = None,
) -> list[dict[str, Any]]:
    if not github_app.enabled():
        return []
    try:
        repo = _repo()
        base = _base_branch()
    except ValueError as exc:
        return [{"status": "unconfigured", "detail": str(exc)}]
    token = _POLL_REPO.set(repo)
    base_token = _POLL_BASE.set(base)
    attempted_token = _PUBLISH_ATTEMPTED.set(set())
    try:
        return _run_once_configured(run_gh=run_gh, transport=transport, policy_config=policy_config)
    finally:
        _PUBLISH_ATTEMPTED.reset(attempted_token)
        _POLL_BASE.reset(base_token)
        _POLL_REPO.reset(token)


# --- operator re-request (task #1854) ------------------------------------------

#: Ledger channel recording who re-requested a head's approval row and the outcome.
RE_REQUEST_CHANNEL = "agent.github_app.rerequested"
_HEAD_SHA = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# The identity an HTTP route admitted as the verified human operator.
_HUMAN_GRANT: ContextVar[str | None] = ContextVar("github_app_rerequest_human", default=None)


class RerequestAuthorityError(ToolError):
    """The caller may not re-request an Approval Ledger row (HTTP 403)."""


@contextmanager
def human_operator_grant(identity: str):
    """Let one HTTP request act on the ledger as the verified human operator.

    A route enters this only after ``routes._operator_recovery`` held for the
    request; it is honoured only while the verified identity is ``identity`` and
    that identity is the operator label, so it never covers an MCP call. It
    covers the re-request (task #1854) and the poll-now (task #1867) actions.
    """
    token = _HUMAN_GRANT.set(identity)
    try:
        yield
    finally:
        _HUMAN_GRANT.reset(token)


#: The task #1854 name for ``human_operator_grant``.
human_operator_rerequest = human_operator_grant


def _authorize_operator(who: str | None, verb: str) -> str:
    """``lifecycle_operator`` or ``human_operator``; raise for anyone else."""
    if not who:
        raise RerequestAuthorityError(f"an authenticated identity is required to {verb}")
    if who in agent_lifecycle.operators():
        return "lifecycle_operator"
    if _HUMAN_GRANT.get() == who and who == config.operator_label():
        return "human_operator"
    raise RerequestAuthorityError(
        f"{who!r} may not {verb}: that is an operator action "
        "(UA_LIFECYCLE_OPERATORS, or the verified operator in the Control Center)"
    )


def _authorize_rerequest(who: str | None) -> str:
    return _authorize_operator(who, "re-request an Approval Ledger row")


def rerequest_head(
    number: int,
    head_sha: str,
    *,
    run_gh: Callable[..., Any] = subprocess.run,
    transport: github_app.GitHubAppTransport | None = None,
    policy_config: policy.PolicyConfig | None = None,
    independent_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open the Approval Ledger row for one open PR head now (task #1854).

    For a head the scheduled poll missed or has not reached yet. Operators only
    (``UA_LIFECYCLE_OPERATORS``, or the verified human operator over HTTP). The
    PR is read fresh from GitHub and ``head_sha`` must still be its head, so a
    stale request cannot open a row for an old commit. The worker then does
    what a scheduled poll does for that head, under its own service identity
    and poll lock: it retires rows for older heads of the PR (bound or not)
    and, through connector policy, queues one ``github_app.process_pr_event``
    approval with its pending GitHub status. It never resolves or spends an
    approval: the operator still approves the queued row. A head that already
    has a live row is reported (``already_requested``); a row whose
    pending-status POST failed is found by the head's ``event_id`` (title and
    label edits do not hide it) and bound with its own stored payload, without
    changing its approval state. A head whose newest row expired or was
    revoked, or is an approved pass past its TTL (expired here), gets a fresh row, unless an older decision on the same payload
    would answer instead, which is refused. A policy that denies the
    connector or does not require an approval queues nothing. Every call that
    reaches GitHub is recorded on ``agent.github_app.rerequested`` with the
    operator and the outcome, including a refused stale, closed or off-base head
    and a failed PR read (``read_failed``, or ``token_unavailable`` when the
    GitHub credential could not be resolved).

    ``independent_review`` proposes the bounded v1 human attestation in the
    SAME release payload. Only a pending proposal can be replaced (DB-CAS
    fenced against native resolution and newer decisions). Existing approved,
    denied, consumed or settled evidence is never silently extended, including
    an approved row past TTL. The subsequent human resolution attests the
    complete immutable proposal; supplying this argument is not authority.
    """
    who = config.verified_agent_identity()
    authority = _authorize_rerequest(who)
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ToolError("number must be a positive pull request number")
    if not isinstance(head_sha, str) or not _HEAD_SHA.fullmatch(head_sha.strip()):
        raise ToolError("head_sha must be the pull request head commit")
    head_sha = head_sha.strip()
    if not github_app.enabled():
        raise ToolError("the Approval Ledger connector is not enabled (UA_GITHUB_APP_ENABLED)")
    try:
        repo = _repo()
        base = _base_branch()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    repo_token = _POLL_REPO.set(repo)
    base_token = _POLL_BASE.set(base)
    # Set while the PR is read from GitHub (``reading``: a failed read), once it
    # has been read (``refused``: the outcome of a stale, closed or off-base
    # head) and once the request starts changing
    # state (retiring rows, queuing or binding one); from either point the call
    # is audited even when it fails.
    progress: dict[str, Any] = {"changed": False}

    def audit(payload: dict[str, Any]) -> None:
        events.record_event(
            RE_REQUEST_CHANNEL,
            payload={
                "repo": repo,
                "number": number,
                "head_sha": head_sha,
                "actor": who,
                "authority": authority,
                **payload,
            },
            originator=who,
        )

    try:
        post_init.register_default_hooks()
        storage.init_db()
        with _poll_lock(), _authenticated_service_identity():
            result = _rerequest_locked(
                number, head_sha, run_gh, transport, policy_config, progress, independent_review
            )
    except Exception as exc:
        if progress.get("refused"):
            audit({"outcome": progress["refused"], "detail": str(exc)[:300]})
        elif progress.get("reading"):
            audit(
                {
                    "outcome": progress["reading"],
                    "error": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            )
        elif progress["changed"]:
            audit(
                {
                    "outcome": "error",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:300],
                    "event_id": progress.get("event_id"),
                }
            )
        raise
    finally:
        _POLL_BASE.reset(base_token)
        _POLL_REPO.reset(repo_token)
    audit(
        {
            "outcome": result.get("status"),
            "approval_id": result.get("approval_id"),
            "event_id": result.get("event_id"),
        }
    )
    return {**result, "requested_by": who}


def _rerequest_locked(
    number: int,
    head_sha: str,
    run_gh: Callable[..., Any],
    transport: github_app.GitHubAppTransport | None,
    policy_config: policy.PolicyConfig | None,
    progress: dict[str, Any] | None = None,
    review_proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = {} if progress is None else progress
    progress["reading"] = "configuration_invalid"
    mode = github_app.token_mode()
    source = github_app.user_token_source() if mode == github_app.TOKEN_MODE_USER else None
    active_transport = transport or github_app.build_transport(repo=_repo())
    # Credential resolution and GitHub reads have distinct remediation paths.
    if mode == github_app.TOKEN_MODE_USER and source == "connector_oauth":
        progress["reading"] = "token_unavailable"
        token = github_app.resolve_user_token()
        progress["reading"] = "read_failed"
        snapshot = _brokered_view_pr(token, number)
    else:
        progress["reading"] = "read_failed"
        snapshot = _view_pr(run_gh, number)
    progress["reading"] = False
    if snapshot is None or snapshot.state != "OPEN":
        progress["refused"] = "not_open"
        raise ToolError(f"pull request #{number} is not open; nothing to request")
    if snapshot.base_branch != _base_branch():
        progress["refused"] = "wrong_base"
        raise ToolError(
            f"pull request #{number} targets {snapshot.base_branch!r}, not the Approval "
            f"Ledger base {_base_branch()!r}"
        )
    if snapshot.head_sha != head_sha:
        progress["refused"] = "stale_head"
        raise ToolError(
            f"pull request #{number} head is {snapshot.head_sha!r}, not {head_sha!r}; "
            "re-request the current head"
        )
    event = _event(snapshot)
    if review_proposal is not None:
        try:
            review = independent_review.validate(
                review_proposal, repo=event.repo, number=number, head_sha=head_sha
            )
        except independent_review.ReviewEvidenceError as exc:
            progress["refused"] = "invalid_review"
            raise ToolError(str(exc)) from None
        event = replace(event, independent_review=review)
    if _settled(event.event_id):
        if review_proposal is not None:
            progress["refused"] = "settled"
            raise ToolError(
                "settled approval evidence cannot be extended with a new review proposal"
            )
        return {"status": "settled", "event_id": event.event_id}
    progress.update(changed=True, event_id=event.event_id)
    _retire_older_heads(active_transport, number, head_sha, include_unbound=True)
    # The head's newest row decides, whatever an older status binding names.
    latest = _head_approval(event)
    if (
        latest is not None
        and review_proposal is not None
        and latest["payload"].get("independent_review") != event.independent_review
        and latest["status"] not in _FENCED
    ):
        if latest["status"] != "pending":
            progress["refused"] = "immutable_decision"
            raise ToolError("existing approval decision cannot attest a changed review proposal")
        # Replace only an undecided proposal. The native human decision must
        # bind the new complete payload; no approved row is ever enriched.
        _expire_pending_review_proposal(latest)
        latest = {**latest, "status": "expired"}
    if latest is not None and latest["status"] == "approved":
        # A pass past its TTL can no longer be spent: expire it, so a
        # replacement is queued below instead of reporting a dead row.
        row = approvals.get_approval(latest["id"])
        if row is not None and not _pass_still_valid(row):
            _expire_approved(latest["id"], "one-shot pass expired before it was spent")
            latest = {**latest, "status": "expired"}
    if latest is not None and latest["status"] not in _FENCED:
        pending = _record(github_app.PENDING_CHANNEL, event.event_id)
        if pending is not None and int(pending["approval_id"]) == latest["id"]:
            return {
                "status": "already_requested",
                "event_id": event.event_id,
                "approval_id": latest["id"],
                "approval_status": latest["status"],
            }
        return _bind_head_status(event, latest, active_transport)
    if latest is not None:
        _refuse_fenced_decisions(latest)
    return _request_fresh_row(event, active_transport, policy_config)


#: Approval states that end a row's authority. The newest such row for a head
#: fences every older decision for it: a fresh row is requested instead.
_FENCED = frozenset({"expired", "revoked"})


def _expire_pending_review_proposal(latest: dict[str, Any]) -> None:
    """Fence HTTP resolution, which does not share the worker's poll lock."""
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        newest = conn.execute(
            "SELECT id FROM approvals WHERE tool=? AND org_id IS ? "
            "AND json_extract(payload, '$.event_id')=? ORDER BY id DESC LIMIT 1",
            (github_app.TOOL_NAME, edition.tenant_org_id(), latest["payload"].get("event_id")),
        ).fetchone()
        older = conn.execute(
            "SELECT id FROM approvals WHERE tool=? AND org_id IS ? "
            "AND json_extract(payload, '$.event_id')=? AND id<? "
            "AND status IN ('pending','approved','denied') LIMIT 1",
            (
                github_app.TOOL_NAME,
                edition.tenant_org_id(),
                latest["payload"].get("event_id"),
                latest["id"],
            ),
        ).fetchone()
        if newest is None or newest["id"] != latest["id"] or older is not None:
            raise ToolError(
                "newest approval or historical decision changed; re-read the native Ledger"
            )
        current = conn.execute(
            "SELECT status, payload FROM approvals WHERE id=? AND tool=? AND org_id IS ?",
            (latest["id"], github_app.TOOL_NAME, edition.tenant_org_id()),
        ).fetchone()
        if (
            current is None
            or current["status"] != "pending"
            or json.loads(current["payload"]) != latest["payload"]
        ):
            raise ToolError("approval proposal changed concurrently; re-read the native decision")
        changed = conn.execute(
            "UPDATE approvals SET status='expired', updated_at=?, reason=? "
            "WHERE id=? AND status='pending' AND payload=? AND org_id IS ?",
            (
                storage.now(),
                "replaced by an explicit independent-review proposal",
                latest["id"],
                current["payload"],
                edition.tenant_org_id(),
            ),
        )
        if changed.rowcount != 1:
            raise ToolError("approval proposal changed concurrently; re-read the native decision")
        conn.commit()
    finally:
        conn.close()


def _head_approval(event: github_app.NormalizedPREvent) -> dict[str, Any] | None:
    """The newest approval row of this tenant for ``event``'s PR head.

    Matched on the head's stable ``event_id`` rather than the whole payload, so
    a title, label or review edit after the row was queued still finds it. The
    row's own stored payload, not the edited snapshot, is what gets bound and
    later processed, so creation, binding and execution name one approval.
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, status, payload FROM approvals WHERE tool=? AND org_id IS ? "
            "AND json_extract(payload, '$.event_id')=? ORDER BY id DESC LIMIT 1",
            (github_app.TOOL_NAME, edition.tenant_org_id(), event.event_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": int(row["id"]), "status": row["status"], "payload": json.loads(row["payload"])}


def _stored_event(
    event: github_app.NormalizedPREvent, payload: dict[str, Any]
) -> github_app.NormalizedPREvent:
    """Rebuild the event an approval row was queued for, checked against ``event``."""
    try:
        recorded = {key: value for key, value in payload.items() if key != "connector"}
        recorded["labels"] = tuple(recorded.get("labels") or ())
        stored = github_app.NormalizedPREvent(**recorded)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"approval row for this head has an unreadable payload: {exc}") from exc
    if (
        payload.get("connector") != github_app.GITHUB_APP_CONNECTOR
        or approvals.canonical_approval_payload(stored.to_payload())
        != approvals.canonical_approval_payload(payload)
        or (stored.event_id, stored.repo, stored.number, stored.head_sha, stored.base_branch)
        != (event.event_id, event.repo, event.number, event.head_sha, event.base_branch)
    ):
        raise ToolError(
            "approval row for this head was queued for a different pull request, head or "
            "base; resolve it in the approval queue before re-requesting"
        )
    return stored


def _bind_head_status(
    event: github_app.NormalizedPREvent,
    approval: dict[str, Any],
    active_transport: github_app.GitHubAppTransport,
) -> dict[str, Any]:
    """Post and record the pending status for an existing row; never resolve it.

    A row can outlive a failed pending-status POST with no marker. This only
    binds a status to it: the normal worker owns one-shot consumption.
    """
    stored = _stored_event(event, approval["payload"])
    if isinstance(active_transport, github_app.GitHubUserTokenTransport):
        # The same cross-process shared-status lock as process_pr_event.
        with github_app._user_status_lock(stored):
            return _bind_head_status_locked(stored, approval, active_transport)
    return _bind_head_status_locked(stored, approval, active_transport)


def _bind_head_status_locked(
    stored: github_app.NormalizedPREvent,
    approval: dict[str, Any],
    active_transport: github_app.GitHubAppTransport,
) -> dict[str, Any]:
    check_run_id = active_transport.create_check_run(
        repo=stored.repo,
        head_sha=stored.head_sha,
        name=github_app.CHECK_RUN_NAME,
        status="queued",
        details_url=stored.url,
    )
    approval_id = int(approval["id"])
    events.record_event(
        github_app.PENDING_CHANNEL,
        payload={
            "event": stored.to_payload(),
            "approval_id": approval_id,
            "check_run_id": check_run_id,
        },
        originator=config.verified_agent_identity(),
        dedupe_key=f"github-app:pending:{stored.event_id}:{approval_id}",
    )
    return {
        "status": "already_requested",
        "event_id": stored.event_id,
        "approval_id": approval_id,
        "approval_status": approval["status"],
        "check_run_id": check_run_id,
    }


def _refuse_fenced_decisions(latest: dict[str, Any]) -> None:
    """Refuse when an older live decision exists for a head whose newest row
    is expired or revoked.

    The newest row fences every older one for the same head (``event_id``):
    an older pending, approved or denied row must be neither bound nor spent.
    The approval gate would otherwise answer a fresh request with it (it
    matches the exact payload and prefers any approved, denied or pending
    row, however old), and a status already bound to it would let the worker
    consume it. The operator resolves or retires it in the queue first.
    """
    conn = storage.connect()
    try:
        older = conn.execute(
            "SELECT id, status FROM approvals WHERE tool=? AND org_id IS ? "
            "AND json_extract(payload, '$.event_id')=? "
            "AND status IN ('pending', 'approved', 'denied') AND id<? ORDER BY id DESC LIMIT 1",
            (
                github_app.TOOL_NAME,
                edition.tenant_org_id(),
                latest["payload"].get("event_id"),
                int(latest["id"]),
            ),
        ).fetchone()
    finally:
        conn.close()
    if older is not None:
        raise ToolError(
            f"approval #{older['id']} ({older['status']}) is an earlier decision on this head "
            f"than the {latest['status']} approval #{latest['id']}; resolve or retire it in "
            "the approval queue before re-requesting"
        )


def _request_fresh_row(
    event: github_app.NormalizedPREvent,
    active_transport: github_app.GitHubAppTransport,
    policy_config: policy.PolicyConfig | None,
) -> dict[str, Any]:
    """Queue a row exactly as a scheduled poll would, through connector policy.

    No live row exists for this head (``_head_approval``), so the approval gate
    has no pass to spend: a ``require_approval`` rule queues a pending row and
    its status. A policy that denies the connector, or does not require an
    approval for it, is reported and queues nothing.
    """
    try:
        result = github_app.process_pr_event(event, active_transport, policy_config=policy_config)
    except policy.PolicyViolation as exc:
        raise ToolError(f"connector policy refused github_app.process_pr_event: {exc}") from exc
    except PermissionError as exc:
        raise ToolError(
            "connector policy does not require an approval for github_app.process_pr_event, "
            "so there is no Approval Ledger row to request"
        ) from exc
    return result


# --- operator poll-now (task #1867) ---------------------------------------------

#: Ledger channel recording who asked for an immediate poll and what it did.
POLL_REQUEST_CHANNEL = "agent.github_app.poll_requested"


def poll_now(
    *,
    run_gh: Callable[..., Any] = subprocess.run,
    transport: github_app.GitHubAppTransport | None = None,
    policy_config: policy.PolicyConfig | None = None,
) -> dict[str, Any]:
    """Run one Approval Ledger poll on the hub now, instead of waiting for the schedule.

    Operators only (``UA_LIFECYCLE_OPERATORS``, or the verified human operator
    over HTTP), as for ``rerequest_head``. It is exactly the scheduled poll
    (``run_once``), under the worker's own service identity and poll lock: it
    requests approval rows for open PR heads, retires superseded ones, and
    consumes only passes a human has already approved. It never resolves an
    approval. Every call is recorded on ``agent.github_app.poll_requested``
    with the operator and a per-status summary, including a poll that raised.
    """
    who = config.verified_agent_identity()
    authority = _authorize_operator(who, "poll the Approval Ledger")
    if not github_app.enabled():
        raise ToolError("the Approval Ledger connector is not enabled (UA_GITHUB_APP_ENABLED)")

    def audit(payload: dict[str, Any]) -> None:
        events.record_event(
            POLL_REQUEST_CHANNEL,
            payload={"actor": who, "authority": authority, **payload},
            originator=who,
        )

    try:
        results = run_once(run_gh=run_gh, transport=transport, policy_config=policy_config)
    except Exception as exc:
        audit({"outcome": "error", "error": type(exc).__name__, "detail": str(exc)[:300]})
        raise
    summary: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    audit({"outcome": "polled", "summary": summary})
    return {"status": "polled", "requested_by": who, "results": results, "summary": summary}


def main() -> None:
    results = run_once()
    for result in results:
        print(json.dumps(result, sort_keys=True))
    if any(result.get("status") in {"error", "unconfigured"} for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
