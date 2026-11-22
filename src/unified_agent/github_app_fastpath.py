"""Prompt Approval Ledger statuses between scheduled polls (task #2234).

The Approval Ledger is poll-only. The hub is reachable on the tailnet only (its
public Funnel slice serves ``/mcp`` and the OAuth endpoints, nothing else), and
the github-app connector declares no webhook ingress, so GitHub cannot tell the
hub about a push or a new pull request. The ``pr-watch`` job's poll
(``github_app_worker.run_once``) is the authority and stays the backstop. This
module shortens two waits around it:

1. **After a decision.** When a human approves or denies a
   ``github_app.process_pr_event`` row on the HTTP hub,
   ``tools.approvals.approval_resolve`` calls :func:`on_approval_resolved`
   after its commit. A background worker
   then runs the poll's own per-PR step for that pull request at once, under
   the worker's service identity and poll lock: read the PR fresh from GitHub,
   retire rows for older heads, and advance the live head (consume an approved
   pass and post success with the signed evidence comment, or post the denial).
   Before this, an approved head stayed ``pending`` on GitHub until the next
   poll. A head that is no longer the PR's head is retired as superseded,
   never approved: only the live head is advanced, exactly as the poll does.
   A closed PR is left to the poll's close-out. Every step is the poll's
   idempotent step, so a later poll re-posting the same state is harmless.

2. **Before a poll.** :func:`open_heads_changed` is one conditional GitHub
   request (``If-None-Match`` on the open-PR list, newest update first). A
   ``304 Not Modified`` costs no rate limit. ``pr_watch.plan_tick`` asks it on
   each tick that is past the active interval with no row in flight, and runs
   the full poll only when the open heads differ from what the last poll saw.
   That lets the job launch every two minutes without listing every PR each
   time. It takes effect once the LaunchAgent's ``StartInterval`` is lowered;
   at the shipped 1800 s every launch is an idle-interval poll anyway.

3. **Reconcile.** :func:`reconcile_unsettled` runs inside every scheduled poll
   and settles consumed approvals that no step finished (a publication that
   failed after its success POST, or a head held while its PR was on another
   base), by PR number, least recently attempted first. Every path settles
   through ``github_app._settle_approved``, one pure decision
   (``github_app.decide``) over the approved and the live head and base, so
   every consumed approval converges to success with evidence, or to
   ``stale``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config, edition, github_app, github_app_worker, policy, pr_watch, storage
from .tools import approvals

LOGGER = logging.getLogger(__name__)

#: Approval decisions that settle a head's GitHub status.
_SETTLING = frozenset({"approved", "denied"})
#: Approvals queued for a prompt status at once; more wait for the next poll.
QUEUE_MAX = 32

_queue_lock = threading.Lock()
_queued: dict[int, None] = {}
_worker: threading.Thread | None = None


# --- after a decision -----------------------------------------------------------


def on_approval_resolved(approval_id: int, tool: str, status: str) -> bool:
    """Queue a prompt GitHub status for a just-resolved ledger row. Never raises.

    Returns True when the row was queued (or already was). Rows of other tools,
    other decisions (a revoke), a disabled connector and a full queue return
    False; the scheduled poll still reaches them. Only the HTTP hub queues:
    it is the long-lived process every human decision goes through, and a
    short-lived CLI or stdio child would exit before a daemon worker finished.
    """
    try:
        if (
            tool != github_app.TOOL_NAME
            or status not in _SETTLING
            or config.transport() != "http"
            or not github_app.enabled()
        ):
            return False
        return _enqueue(int(approval_id))
    except Exception:  # noqa: BLE001 - the resolution has committed; never fail it
        LOGGER.warning("approval ledger fast path could not queue #%s", approval_id, exc_info=True)
        return False


def _enqueue(approval_id: int) -> bool:
    global _worker
    with _queue_lock:
        if approval_id in _queued:
            return True
        if len(_queued) >= QUEUE_MAX:
            LOGGER.warning(
                "approval ledger fast path queue full; approval #%s waits for the next poll",
                approval_id,
            )
            return False
        _queued[approval_id] = None
        if _worker is None or not _worker.is_alive():
            # An empty context: the resolving caller's identity, tenant and
            # request state never reach the worker, which binds its own.
            context = contextvars.Context()
            _worker = threading.Thread(
                target=context.run,
                args=(_drain,),
                name="approval-ledger-fastpath",
                daemon=True,
            )
            _worker.start()
    return True


def _drain() -> None:
    global _worker
    while True:
        with _queue_lock:
            if not _queued:
                _worker = None
                return
            approval_id = next(iter(_queued))
            del _queued[approval_id]
        try:
            result = advance_resolved(approval_id)
            LOGGER.info("approval ledger fast path #%s: %s", approval_id, result.get("status"))
        except Exception:  # noqa: BLE001 - the scheduled poll is the backstop
            LOGGER.warning(
                "approval ledger fast path failed for approval #%s; the next poll retries",
                approval_id,
                exc_info=True,
            )


def _read_pr(run_gh: Callable[..., Any], number: int) -> pr_watch.PRSnapshot | None:
    if (
        github_app.token_mode() == github_app.TOKEN_MODE_USER
        and github_app.user_token_source() == "connector_oauth"
    ):
        return github_app_worker._brokered_view_pr(github_app.resolve_user_token(), number)
    return github_app_worker._view_pr(run_gh, number)


def advance_resolved(
    approval_id: int,
    *,
    run_gh: Callable[..., Any] = subprocess.run,
    transport: github_app.GitHubAppTransport | None = None,
    policy_config: policy.PolicyConfig | None = None,
) -> dict[str, Any]:
    """Run the poll's per-PR step now for the pull request a ledger row names.

    The same step ``run_once`` takes for each open PR, for one PR: retire rows
    for other heads (bound or not), then ``_advance_snapshot`` on the live head.
    The row's own decision is never changed here; only what the poll would
    publish is published sooner. A PR that is closed, retargeted off the
    ledger base or unreadable, or a credential GitHub has backing off
    (task #2237), is ``deferred`` to the scheduled poll.
    """
    approval = approvals.get_approval(int(approval_id))
    if approval is None or approval.get("tool") != github_app.TOOL_NAME:
        return {"status": "skipped", "reason": "not_a_ledger_row", "approval_id": approval_id}
    if not github_app.enabled():
        return {"status": "disabled", "approval_id": approval_id}
    payload = approval.get("payload") or {}
    try:
        repo = github_app_worker._repo()
        base = github_app_worker._base_branch()
    except ValueError as exc:
        return {"status": "unconfigured", "detail": str(exc), "approval_id": approval_id}
    number = payload.get("number")
    if payload.get("repo") != repo or isinstance(number, bool) or not isinstance(number, int):
        return {"status": "skipped", "reason": "other_repository", "approval_id": approval_id}
    repo_token = github_app_worker._POLL_REPO.set(repo)
    base_token = github_app_worker._POLL_BASE.set(base)
    try:
        with (
            github_app_worker._poll_lock(),
            github_app_worker._authenticated_service_identity(),
        ):
            active_transport = transport or github_app.build_transport(repo=repo)
            outcome = {"approval_id": approval_id, "number": number}
            if github_app.github_backoff_remaining(active_transport) > 0:
                # Task #2237: the scheduled poll publishes once the window ends.
                return {**outcome, "status": "deferred", "reason": "github_backoff"}
            snapshot = _read_pr(run_gh, number)
            if snapshot is None or snapshot.state != "OPEN" or not snapshot.head_sha:
                return {**outcome, "status": "deferred", "reason": "not_open"}
            if snapshot.base_branch != base:
                return {**outcome, "status": "deferred", "reason": "wrong_base"}
            github_app_worker._retire_older_heads(
                active_transport, number, snapshot.head_sha, include_unbound=True
            )
            reader = github_app_worker.live_pr_reader(lambda n: _read_pr(run_gh, n))
            with github_app.live_pr(reader):
                result = github_app_worker._advance_snapshot(
                    snapshot, active_transport, policy_config, allow_closed=False
                )
            return {
                **outcome,
                "status": "advanced",
                "head_sha": snapshot.head_sha,
                "superseded": snapshot.head_sha != payload.get("head_sha"),
                "result": result,
            }
    finally:
        github_app_worker._POLL_BASE.reset(base_token)
        github_app_worker._POLL_REPO.reset(repo_token)


# --- reconcile: consumed but unsettled ------------------------------------------

#: Consumed approvals the reconcile step settles per poll.
RECONCILE_LIMIT = 10
#: The order unsettled approvals were last attempted in (``$UA_HOME``): a
#: monotonic sequence, not a clock, so a clock change cannot reorder it. Every
#: approval is revisited within ceil(N / RECONCILE_LIMIT) polls, failures
#: included.
RECONCILE_CHECKS_FILE = "approval-ledger-settle-checks.json"
_SEQ_LIMIT = 2**63


def reconcile_checks_path():
    return config.data_home() / RECONCILE_CHECKS_FILE


def _valid_seq(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < _SEQ_LIMIT


def _load_checks(repo: str) -> dict[int, int]:
    """``{approval_id: seq}``; never raises on file content.

    An unreadable or malformed file counts as empty, and an entry whose key is
    not an approval id or whose seq is not a non-negative int below 2**63
    counts as 0 (attempted first). Either is logged once per load and the
    file is rewritten clean on the next save.
    """
    path = reconcile_checks_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        LOGGER.warning("Approval Ledger settle-checks file unreadable; starting fresh")
        return {}
    if not isinstance(record, dict) or not isinstance(record.get("checked"), dict):
        LOGGER.warning("Approval Ledger settle-checks file malformed; starting fresh")
        return {}
    if record.get("repo") != repo:
        return {}
    out: dict[int, int] = {}
    reset = 0
    for key, value in record["checked"].items():
        try:
            approval_id = int(key)
        except (TypeError, ValueError):
            reset += 1
            continue
        if _valid_seq(value):
            out[approval_id] = value
        else:
            out[approval_id] = 0
            reset += 1
    if reset:
        LOGGER.warning("Approval Ledger settle-checks: reset %d invalid entr(y/ies)", reset)
    return out


def _save_checks(repo: str, checked: dict[int, int]) -> None:
    path = reconcile_checks_path()
    record = {"repo": repo, "checked": {str(k): v for k, v in checked.items()}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        LOGGER.warning("could not record the Approval Ledger reconcile checks", exc_info=True)


def _unsettled_consumed(repo: str) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """``(approval_id, payload, pending)`` for every consumed row with no terminal record.

    A consumed approval is settled once its head has approval evidence or any
    other terminal outcome. The retargeted marker is not terminal. Rows with no
    pending-status record of their own have nothing to reconcile.
    """
    terminal = github_app_worker._TERMINAL_CHANNELS
    placeholders = ",".join("?" for _ in terminal)
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, payload FROM approvals WHERE tool=? AND status='consumed' "
            "AND approvals.org_id IS ? AND json_extract(payload, '$.repo')=? "
            "AND NOT EXISTS (SELECT 1 FROM event_log "
            f"WHERE event_log.channel IN ({placeholders}) "
            "AND json_extract(event_log.payload, '$.event_id')="
            "json_extract(approvals.payload, '$.event_id')) ORDER BY id",
            (github_app.TOOL_NAME, edition.tenant_org_id(), repo, *terminal),
        ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            pending = conn.execute(
                "SELECT payload FROM event_log WHERE channel=? "
                "AND json_extract(payload, '$.event.event_id')=? "
                "AND json_extract(payload, '$.approval_id')=? ORDER BY ulid DESC LIMIT 1",
                (github_app.PENDING_CHANNEL, payload.get("event_id"), int(row["id"])),
            ).fetchone()
            if pending is not None:
                out.append((int(row["id"]), payload, json.loads(pending["payload"])))
        return out
    finally:
        conn.close()


def _stored_event(pending: dict[str, Any]) -> github_app.NormalizedPREvent:
    recorded = {k: v for k, v in pending["event"].items() if k != "connector"}
    recorded["labels"] = tuple(recorded.get("labels") or ())
    return github_app.NormalizedPREvent(**recorded)


def _reconcile_one(
    approval_id: int, pending: dict[str, Any], transport: github_app.GitHubAppTransport
) -> dict[str, Any]:
    """Settle one consumed approval through the single settle path."""
    event = _stored_event(pending)
    result = github_app.resume_consumed_pr_event(
        event, transport, pending_check_run_id=pending["check_run_id"]
    )
    state = result.get("live_state")
    if result.get("status") == "approved" and state not in (None, "OPEN"):
        github_app_worker._record_outcome(
            github_app.POST_MERGE_CHANNEL,
            event.event_id,
            approval_id,
            "approved_post_merge" if state == "MERGED" else "approved_after_close",
        )
    return result


def reconcile_unsettled(
    transport: github_app.GitHubAppTransport,
    open_numbers: set[int],
    *,
    now: Callable[[], float] | None = None,  # noqa: ARG001 - kept for callers; order is by seq
) -> list[dict[str, Any]]:
    """Settle consumed approvals that no step finished, by PR number (task #2234).

    Run by the scheduled poll, under its lock, service identity and live PR
    reader, before the open-PR step. A PR in this poll's open list is left to
    that step, which settles it the same way. Of the rest, the
    ``RECONCILE_LIMIT`` least recently attempted (lowest persisted sequence
    number) are settled through ``resume_consumed_pr_event``
    (``github_app._settle_approved``). Each attempt takes the next sequence
    number, saved before it runs, so a failure, or a crash, costs only that
    approval's turn, and no clock is involved. Entries for approvals that are
    settled or gone are pruned. An unreadable PR is retried on its next turn.

    Never raises; outcomes are logged and returned, not added to the poll's
    results (they are recovery, not new work).
    """
    try:
        repo = github_app_worker._repo()
        candidates = [
            c for c in _unsettled_consumed(repo) if int(c[1].get("number") or 0) not in open_numbers
        ]
        checks = _load_checks(repo)
    except Exception:  # noqa: BLE001 - the rest of the poll still runs
        LOGGER.warning("Approval Ledger reconcile could not list approvals", exc_info=True)
        return []
    live_ids = {approval_id for approval_id, _payload, _pending in candidates}
    checks = {k: v for k, v in checks.items() if k in live_ids}
    candidates.sort(key=lambda c: (checks.get(c[0], 0), c[0]))
    outcomes = []
    for approval_id, _payload, pending in candidates[:RECONCILE_LIMIT]:
        previous = checks.get(approval_id)
        checks[approval_id] = min(max(checks.values(), default=0) + 1, _SEQ_LIMIT - 1)
        _save_checks(repo, checks)
        try:
            outcome = _reconcile_one(approval_id, pending, transport)
        except github_app.GitHubBackoff as exc:
            # Task #2237: stop. A refused attempt sent nothing, so it keeps
            # its place; the rest are untouched and stay due.
            if not exc.sent:
                if previous is None:
                    checks.pop(approval_id)
                else:
                    checks[approval_id] = previous
                _save_checks(repo, checks)
            LOGGER.info("Approval Ledger reconcile paused: %s", exc)
            outcomes.append(
                {
                    "status": "backoff",
                    "approval_id": approval_id,
                    "retry_after_s": exc.retry_after_s,
                }
            )
            break
        except Exception as exc:  # noqa: BLE001 - retried on its next turn
            LOGGER.warning(
                "Approval Ledger reconcile left approval #%s for a later poll",
                approval_id,
                exc_info=True,
            )
            outcome = {"status": "reconcile_unverified", "error": type(exc).__name__}
        LOGGER.info("Approval Ledger reconcile #%s: %s", approval_id, outcome.get("status"))
        outcomes.append({**outcome, "approval_id": approval_id})
    if not candidates:
        _save_checks(repo, checks)
    return outcomes


# --- before a poll --------------------------------------------------------------

#: The last open-PR page the probe saw, with its ETag (``$UA_HOME``).
PROBE_FILE = "approval-ledger-probe.json"
PROBE_PAGE = 100
_PROBE_MAX_BYTES = 4 * 1024 * 1024


def probe_path():
    return config.data_home() / PROBE_FILE


def _load_probe(repo: str, base: str) -> dict[str, Any]:
    try:
        record = json.loads(probe_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or (record.get("repo"), record.get("base")) != (repo, base):
        return {}
    if not isinstance(record.get("heads"), dict) or not isinstance(record.get("etag"), str):
        return {}
    return record


def _save_probe(repo: str, base: str, etag: str | None, heads: dict[str, str], complete: bool):
    path = probe_path()
    record = {
        "repo": repo,
        "base": base,
        "etag": etag or "",
        "heads": heads,
        "complete": complete,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}")
        temp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        LOGGER.warning("could not record the Approval Ledger probe state", exc_info=True)


def _probe_params(base: str) -> dict[str, object]:
    # Newest update first: a push or a new PR always lands on the first page,
    # so one page decides whether any head moved.
    return {
        "state": "open",
        "base": base,
        "sort": "updated",
        "direction": "desc",
        "per_page": PROBE_PAGE,
    }


def _has_next(link: str | None) -> bool:
    """Whether a ``Link`` header names a next page."""
    return any('rel="next"' in part for part in (link or "").split(","))


def _parse_included(stdout: str) -> tuple[int, str | None, str, bool]:
    """Split ``gh api --include`` output into status, ETag, body and next-page flag."""
    head, _sep, body = (stdout or "").replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    parts = lines[0].split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise RuntimeError("GitHub probe response had no status line")
    etag = None
    link = None
    for line in lines[1:]:
        name, _colon, value = line.partition(":")
        if name.strip().lower() == "etag":
            etag = value.strip()
        elif name.strip().lower() == "link":
            link = value.strip()
    return int(parts[1]), etag, body, _has_next(link)


def _gh_conditional_list(
    run_gh: Callable[..., Any], repo: str, base: str, etag: str | None
) -> tuple[int, str | None, Any, bool]:
    cmd = [
        "gh",
        "api",
        "--include",
        "--method",
        "GET",
        f"repos/{repo}/pulls?{urlencode(_probe_params(base))}",
    ]
    if etag:
        cmd += ["-H", f"If-None-Match: {etag}"]
    proc = run_gh(cmd, capture_output=True, text=True, timeout=pr_watch.GH_TIMEOUT_SECONDS)
    # gh exits 1 on a 304; the status line, not the exit code, decides.
    status, new_etag, body, more = _parse_included(proc.stdout or "")
    if status != 200:
        return status, new_etag, None, more
    if len(body.encode()) > _PROBE_MAX_BYTES:
        raise RuntimeError("GitHub probe response too large")
    return status, new_etag, json.loads(body), more


def _brokered_conditional_list(
    token: str, repo: str, base: str, etag: str | None
) -> tuple[int, str | None, Any, bool]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    transport = github_app.GitHubUserTokenTransport(lambda: token, brokered=True)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        transport._refuse_while_backing_off()
        response = client.get(
            f"https://api.github.com/repos/{repo}/pulls",
            params=_probe_params(base),
            headers=headers,
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
    new_etag = response.headers.get("etag")
    more = _has_next(response.headers.get("link"))
    if response.status_code != 200:
        return response.status_code, new_etag, None, more
    if len(response.content) > _PROBE_MAX_BYTES:
        raise RuntimeError("GitHub probe response too large")
    return 200, new_etag, response.json(), more


def _page_heads(page: Any) -> dict[str, str]:
    if not isinstance(page, list):
        raise RuntimeError("GitHub probe page was not a list")
    heads: dict[str, str] = {}
    for item in page:
        number = item.get("number") if isinstance(item, dict) else None
        sha = ((item.get("head") or {}).get("sha") if isinstance(item, dict) else None) or ""
        if isinstance(number, int) and not isinstance(number, bool) and sha:
            heads[str(number)] = str(sha)
    return heads


def _differs(heads: dict[str, str], complete: bool, seen: dict[str, str]) -> bool:
    # One page can vouch for the whole list only when it is the only page: a
    # 304 on the first of several pages says nothing about the others.
    if not complete:
        return True
    if any(seen.get(number) != sha for number, sha in heads.items()):
        return True
    # The page lists every open PR, so a missing one has closed.
    return bool(set(seen) - set(heads))


def open_heads_changed(*, run_gh: Callable[..., Any] = subprocess.run) -> bool:
    """Whether the open PR heads may differ from what the last poll saw.

    One conditional request for the first page of open PRs on the ledger base,
    newest update first. A ``304`` answers from the page stored with its ETag;
    a ``200`` stores the new page. Either way the page is compared with the
    last poll's open-head snapshot: a new PR, a moved head or a closed PR is a
    change. A full first page, or one whose ``Link`` names a next page, cannot
    vouch for the rest of the list, so it always counts as a change (the full
    poll runs). No snapshot (no recent successful poll) counts as a change. False when the connector is off or has no repository.
    Raises on a failed read; ``plan_tick`` treats that as a change too.
    """
    if not github_app.enabled():
        return False
    try:
        repo = github_app_worker._repo()
        base = github_app_worker._base_branch()
    except ValueError:
        return False
    seen = github_app_worker._load_open_heads(repo)
    if seen is None:
        return True
    stored = _load_probe(repo, base)
    etag = stored.get("etag") or None
    if (
        github_app.token_mode() == github_app.TOKEN_MODE_USER
        and github_app.user_token_source() == "connector_oauth"
    ):
        with github_app_worker._authenticated_service_identity():
            token = github_app.resolve_user_token()
        status, new_etag, page, more = _brokered_conditional_list(token, repo, base, etag)
    else:
        status, new_etag, page, more = _gh_conditional_list(run_gh, repo, base, etag)
    if status == 304 and stored:
        return _differs(stored["heads"], bool(stored.get("complete")) and not more, seen)
    if status != 200:
        raise RuntimeError(f"GitHub probe returned HTTP {status}")
    heads = _page_heads(page)
    complete = len(page) < PROBE_PAGE and not more
    _save_probe(repo, base, new_etag, heads, complete)
    return _differs(heads, complete, seen)


def _on_resolved(approval_id: int, tool: str, status: str) -> bool:
    # Looks the name up at call time, so a patch of on_approval_resolved holds.
    return on_approval_resolved(approval_id, tool, status)


# The kernel approval gate calls this after a ledger row resolves (#2450).
approvals.register_ledger_fast_path(_on_resolved)
