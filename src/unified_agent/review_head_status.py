"""Publish the ``unified-agent/review-head`` check at every open PR head (task #2320).

``shipping_preflight_cli.review_head_check`` answers whether a head was read.
This module carries that answer to GitHub, once per head, from the Approval
Ledger poll. It runs in the poll, not in CI, because ``.github/workflows/ci.yml``
filters out markdown-only pull requests and this check must exist on those too.

Idempotence rests on two records. ``connector.github.review_head_posted`` holds
what GitHub last accepted for a head; a poll whose answer matches it makes no
GitHub write, and a different answer updates the same check. The transport owns
the other half (``publish_head_check``): it creates a check run only after
proving none with this head's ``external_id`` exists, so a POST whose response
was lost is adopted rather than duplicated. A commit status is keyed by
``(sha, context)`` and cannot duplicate.

A failed publish records an empty marker that clears both the answer and the
remembered id, because GitHub may have applied it and the id may be the reason
it failed. The next poll then publishes again from a lookup.

When GitHub cannot say what a head's delta contains
(``ReviewHeadUnavailable``), nothing is published for that head: no check at
all keeps a required check unmet, and a guess in either direction would be a
claim the hub cannot back. Each head's failure is reported as an ``error``
result, so pr-watch retries the poll and the doctor names it. The one exception
is a head two open PRs share: it may already hold a sibling's success, so the PR
that could not be checked counts as unread (``_answer``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from . import config, events, github_app, pr_models, review_reads, storage

LOGGER = logging.getLogger(__name__)

CHECK_NAME = pr_models.REVIEW_HEAD_CHECK_NAME
POSTED_CHANNEL = "connector.github.review_head_posted"
#: Names this pass in a poll result that is not about one head.
EVENT_ID = "review-head"
#: Worst first: two open PRs on one head share one check, and it shows the worst.
_SEVERITY = ("failure", "action_required", "success")
#: How long the poll keeps retrying a read on a closed PR that did not reach GitHub.
_CLOSED_HEAD_RETRY_WINDOW = timedelta(hours=24)


def external_id(head_sha: str) -> str:
    """The id that marks a check run on this head as ours."""
    return f"{EVENT_ID}:{head_sha}"


def _last_posted(repo: str, head_sha: str) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.repo')=? AND json_extract(payload, '$.head_sha')=? "
            "ORDER BY ulid DESC LIMIT 1",
            (POSTED_CHANNEL, repo, head_sha),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else json.loads(row["payload"] or "{}")


def _record(repo: str, number: int, head_sha: str, **fields: Any) -> None:
    """Note what GitHub holds now. A failed write costs one repeated, idempotent
    publish on the next poll, so it is logged and never fails this one."""
    try:
        events.record_event(
            POSTED_CHANNEL,
            payload={"repo": repo, "number": number, "head_sha": head_sha, **fields},
            originator=config.verified_agent_identity(),
        )
    except Exception:  # noqa: BLE001 - see the docstring
        LOGGER.warning("could not record a review-head post", exc_info=True)


def _newest_markers(repo: str, heads: Collection[str]) -> dict[str, tuple[dict[str, Any], str]]:
    """``{head_sha: (payload, created_at)}`` of each head's newest post, in one query."""
    if not heads:
        return {}
    wanted = sorted(heads)
    slots = ",".join("?" * len(wanted))
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, created_at FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.repo')=? "
            f"AND json_extract(payload, '$.head_sha') IN ({slots}) ORDER BY ulid",
            (POSTED_CHANNEL, repo, *wanted),
        ).fetchall()
    finally:
        conn.close()
    markers: dict[str, tuple[dict[str, Any], str]] = {}
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        markers[str(payload.get("head_sha"))] = (payload, str(row["created_at"]))
    return markers


def _is_after(later: str, earlier: str) -> bool:
    """Strictly, so a post in the same instant as a read counts as before it:
    that costs one repeat recompute, where the reverse could strand the read."""
    try:
        return datetime.fromisoformat(later) > datetime.fromisoformat(earlier)
    except ValueError:
        return False


def unpublished_read_heads(repo: str, *, open_numbers: Collection[int]) -> dict[int, str]:
    """Closed PRs read recently whose head still owes GitHub an answer.

    A read publishes its own head (task #2410), but that publish is best-effort:
    it can fail, back off, or defer to a running poll, and once a PR has closed
    the poll would otherwise never look at it again. A head settles at
    ``success``, or at any other real answer given for that PR after its newest
    read: only a new read can change that answer, so recomputing it would cost
    a GitHub compare per poll for nothing (task #2417). An empty marker is an
    uncertain post and stays owed. Bounded by the window and the read store's limit.
    Never raises: this must not hold up the open heads.
    """
    since = (datetime.now(UTC) - _CLOSED_HEAD_RETRY_WINDOW).isoformat()
    try:
        recent = {
            number: read
            for number, read in review_reads.latest_reads_since(repo, since).items()
            if number not in open_numbers
        }
        markers = _newest_markers(repo, {head_sha for head_sha, _ in recent.values()})
    except (sqlite3.Error, storage.LiveStoreError):
        LOGGER.warning("could not list recently read closed heads", exc_info=True)
        return {}

    def settled(number: int, head_sha: str, read_at: str) -> bool:
        payload, posted_at = markers.get(head_sha, ({}, ""))
        conclusion = payload.get("conclusion")
        if conclusion == "success":
            return True
        # An open sibling can give the head its worst answer, and that answer
        # goes when the sibling moves, so only this PR's own answer settles it.
        return (
            bool(conclusion) and payload.get("number") == number and _is_after(posted_at, read_at)
        )

    return {
        number: head_sha
        for number, (head_sha, read_at) in recent.items()
        if not settled(number, head_sha, read_at)
    }


def _answer(
    repo: str, numbers: list[int], head_sha: str, run_gh: Callable[..., Any]
) -> tuple[int, dict[str, Any]]:
    """The check's answer for a head: the worst across the open PRs that share it.

    A read is recorded against a pull request, so a head that two PRs share is
    only as read as the less-read of them. On a shared head a PR GitHub cannot
    answer for must not leave a sibling's success standing, so it counts as
    unread; a head with one PR publishes nothing, as the module docstring says.
    """
    from . import shipping_preflight_cli

    answers = []
    for number in numbers:
        try:
            answer = shipping_preflight_cli.review_head_check(
                repo=repo, number=number, head_sha=head_sha, run_gh=run_gh
            )
        except shipping_preflight_cli.ReviewHeadUnavailable:
            if len(numbers) == 1:
                raise
            answer = {
                "status": "unavailable",
                "conclusion": "action_required",
                "summary": f"PR #{number} on this head could not be checked",
                "read_head": None,
            }
        answers.append((number, answer))
    return min(answers, key=lambda pair: _SEVERITY.index(pair[1]["conclusion"]))


def _publish(
    transport: github_app.GitHubAppTransport,
    *,
    repo: str,
    number: int,
    head_sha: str,
    answer: dict[str, Any],
    note_unchanged: bool = False,
) -> dict[str, Any]:
    conclusion, summary = answer["conclusion"], answer["summary"]
    result = {"number": number, "head_sha": head_sha, "conclusion": conclusion}
    last = _last_posted(repo, head_sha)
    if last and last.get("conclusion") == conclusion and last.get("summary") == summary:
        if note_unchanged:  # nothing to send, but this read now has its answer (#2417)
            _record(
                repo,
                number,
                head_sha,
                conclusion=conclusion,
                summary=summary,
                check_run_id=last.get("check_run_id") or "",
                gate_status=answer["status"],
            )
        return {"status": "review_head_unchanged", **result}
    try:
        check_run_id = transport.publish_head_check(
            repo=repo,
            head_sha=head_sha,
            name=CHECK_NAME,
            conclusion=conclusion,
            description=summary,
            external_id=external_id(head_sha),
            details_url=f"https://github.com/{repo}/pull/{number}",
            check_run_id=(last or {}).get("check_run_id") or None,
        )
    except BaseException:
        _record(repo, number, head_sha, conclusion="", summary="", check_run_id="", uncertain=True)
        raise
    _record(
        repo,
        number,
        head_sha,
        conclusion=conclusion,
        summary=summary,
        check_run_id=check_run_id,
        gate_status=answer["status"],
    )
    return {"status": "review_head_posted", "check_run_id": check_run_id, **result}


def sync_open_heads(
    heads: Mapping[int, str],
    transport: github_app.GitHubAppTransport,
    *,
    repo: str,
    run_gh: Callable[..., Any],
    retried: Collection[str] = (),
) -> Iterator[dict[str, Any]]:
    """Publish the check for ``{number: head_sha}``, yielding one result per head.

    ``retried`` names closed heads from ``unpublished_read_heads``: an unchanged
    answer on one is still noted, or that head would be recomputed every poll.
    One head's failure does not stop the others. ``GitHubBackoff`` does, and
    propagates: nothing may be sent while the credential backs off.
    """
    numbers_by_head: dict[str, list[int]] = {}
    for number, head_sha in sorted(heads.items()):
        numbers_by_head.setdefault(head_sha, []).append(number)
    for head_sha, numbers in numbers_by_head.items():
        try:
            number, answer = _answer(repo, numbers, head_sha, run_gh)
            yield _publish(
                transport,
                repo=repo,
                number=number,
                head_sha=head_sha,
                answer=answer,
                note_unchanged=head_sha in retried,
            )
        except github_app.GitHubBackoff:
            raise
        except Exception as exc:  # noqa: BLE001 - reported per head; the poll goes on
            LOGGER.warning("review-head check failed for %s", head_sha[:12], exc_info=True)
            yield {
                "status": "error",
                "event_id": external_id(head_sha),
                "number": numbers[0],
                "head_sha": head_sha,
                "error": type(exc).__name__,
                "detail": f"{CHECK_NAME}: {type(exc).__name__}: {str(exc)[:200]}",
            }
