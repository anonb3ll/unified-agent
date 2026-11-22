"""Prompt Approval Ledger statuses between polls (task #2234). GitHub is mocked.

Before: the Approval Ledger status for a PR head changed only when the
``pr-watch`` poll ran, so an approved head stayed ``pending`` on GitHub until
the next poll, and the job (launched every 30 minutes) could not poll more
often without listing every open PR each launch. Now:

(a) approving or denying a ``github_app.process_pr_event`` row in the HTTP hub
    runs the poll's own per-PR step for that PR at once, on a background worker
    under the worker's service identity;
(b) ``open_heads_changed`` is one conditional GitHub request that tells the
    tick planner whether open heads moved since the last poll.

This file covers (a); (b) is in ``test_github_app_fastpath_probe.py``.
"""

from __future__ import annotations

import json
import random
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_github_app_worker import RecordingTransport, _policy, _pr

from unified_agent import (
    config,
    github_app,
    github_app_fastpath,
    github_app_worker,
    review_reads,
    storage,
)
from unified_agent.audit import producer_sign
from unified_agent.tools import approvals

SERVICE = github_app_worker.SERVICE_IDENTITY
#: The unpatched fast path (the ``ledger`` fixture wraps it).
ADVANCE = github_app_fastpath.advance_resolved
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _human():
    return _as(config.operator_label())


@pytest.fixture
def ledger(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "service-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": SERVICE})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "service-bearer")
    state = SimpleNamespace(prs=[_pr(sha="head-1")], closed=None, calls=[])

    def gh(cmd, **_kwargs):
        state.calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "view"]:
            view = state.closed or next(p for p in state.prs if p["number"] == int(cmd[3]))
            return SimpleNamespace(returncode=0, stdout=json.dumps(view))
        return SimpleNamespace(returncode=0, stdout=json.dumps(state.prs))

    state.gh = gh
    state.transport = RecordingTransport()
    advanced: list[dict] = []
    identities: list[str | None] = []
    real = github_app_fastpath.advance_resolved

    def advance(approval_id):
        # The worker starts from an empty context: no caller identity leaks in.
        identities.append(config.verified_agent_identity())
        result = real(approval_id, run_gh=gh, transport=state.transport, policy_config=_policy())
        advanced.append(result)
        return result

    monkeypatch.setattr(github_app_fastpath, "advance_resolved", advance)
    state.advanced = advanced
    state.identities = identities
    yield state
    _join()


def _join():
    worker = github_app_fastpath._worker
    if worker is not None:
        worker.join(timeout=10)
        assert not worker.is_alive()


def _poll(c):
    return github_app_worker.run_once(run_gh=c.gh, transport=c.transport, policy_config=_policy())


def _resolve(approval_id: int, decision: str) -> None:
    with _human():
        approvals.approval_resolve(approval_id, decision)
    _join()


# --- (a) after a decision ------------------------------------------------------------


def test_ledger_tool_name_matches_the_gate_constant():
    assert approvals.LEDGER_TOOL == github_app.TOOL_NAME


def test_approval_posts_success_at_once_and_the_poll_finds_nothing_left(ledger):
    [pending] = _poll(ledger)
    assert pending["status"] == "pending_approval"
    _resolve(pending["approval_id"], "approve")

    assert ledger.identities == [None]
    [advanced] = ledger.advanced
    assert advanced["status"] == "advanced" and advanced["superseded"] is False
    assert advanced["result"]["status"] == "approved"
    assert ledger.transport.updated[-1]["conclusion"] == "success"
    assert len(ledger.transport.comments) == 1
    assert approvals.get_approval(pending["approval_id"])["status"] == "consumed"
    # The poll stays the backstop, and repeats nothing.
    assert _poll(ledger) == []
    assert len(ledger.transport.updated) == 1
    assert len(ledger.transport.comments) == 1


def test_denial_posts_failure_at_once(ledger):
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "deny")
    [advanced] = ledger.advanced
    assert advanced["result"]["status"] == "denied"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["failure"]
    assert ledger.transport.comments == []
    assert _poll(ledger) == []
    assert len(ledger.transport.updated) == 1


def test_superseded_head_is_retired_never_approved(ledger):
    [pending] = _poll(ledger)
    ledger.prs[0] = _pr(sha="head-2")
    _resolve(pending["approval_id"], "approve")

    [advanced] = ledger.advanced
    assert advanced["superseded"] is True and advanced["head_sha"] == "head-2"
    assert advanced["result"]["status"] == "pending_approval"
    conclusions = [u["conclusion"] for u in ledger.transport.updated]
    assert conclusions == ["stale"]
    assert ledger.transport.comments == []
    assert approvals.get_approval(pending["approval_id"])["status"] == "revoked"
    assert ledger.transport.created[-1]["head_sha"] == "head-2"


def test_closed_pull_request_is_left_to_the_poll(ledger):
    [pending] = _poll(ledger)
    ledger.closed = {**_pr(sha="head-1"), "state": "CLOSED", "closedAt": "2026-10-15T12:00:00Z"}
    _resolve(pending["approval_id"], "approve")
    [advanced] = ledger.advanced
    assert (advanced["status"], advanced["reason"]) == ("deferred", "not_open")
    assert ledger.transport.updated == []
    assert approvals.get_approval(pending["approval_id"])["status"] == "approved"


def test_backing_off_defers_to_the_poll_and_publishes_after_the_window(ledger, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(github_app, "_wall_clock", lambda: clock[0])
    monkeypatch.setattr(github_app, "_monotonic", lambda: clock[0])
    [pending] = _poll(ledger)
    github_app._record_backoff(ledger.transport.backoff_scope(), 300)
    reads = len(ledger.calls)
    _resolve(pending["approval_id"], "approve")

    [advanced] = ledger.advanced
    assert (advanced["status"], advanced["reason"]) == ("deferred", "github_backoff")
    assert _poll(ledger) == [{"status": "backoff", "retry_after_s": 300}]
    # Fail closed: no read, no status, the pass is still unconsumed.
    assert len(ledger.calls) == reads and ledger.transport.updated == []
    assert approvals.get_approval(pending["approval_id"])["status"] == "approved"

    clock[0] += 300
    [approved] = _poll(ledger)
    assert approved["status"] == "approved"
    assert ledger.transport.updated[-1]["conclusion"] == "success"


def test_reconcile_stops_on_backoff_without_using_up_turns(ledger, monkeypatch):
    """Review B1 for the #1317 reconcile: only an attempt that reached GitHub takes a turn."""
    candidates = [(n, {"number": 900 + n}, {}) for n in (1, 2, 3)]
    monkeypatch.setattr(github_app_fastpath, "_unsettled_consumed", lambda _repo: candidates)
    attempts = []

    def limited(approval_id, _pending, _transport):
        attempts.append(approval_id)
        raise github_app.GitHubBackoff(600, sent=approval_id == 1)

    monkeypatch.setattr(github_app_fastpath, "_reconcile_one", limited)
    [first] = github_app_fastpath.reconcile_unsettled(ledger.transport, set())
    assert (first["status"], first["approval_id"]) == ("backoff", 1)
    assert github_app_fastpath._load_checks("example/project") == {1: 1}
    # Refused unsent: #2 keeps its place at the front, #3 is never touched.
    [second] = github_app_fastpath.reconcile_unsettled(ledger.transport, set())
    assert second["approval_id"] == 2 and attempts == [1, 2]
    assert github_app_fastpath._load_checks("example/project") == {1: 1}


def test_retargeted_pull_request_is_left_to_the_poll(ledger):
    [pending] = _poll(ledger)
    ledger.prs[0] = {**_pr(sha="head-1"), "baseRefName": "release"}
    _resolve(pending["approval_id"], "approve")
    [advanced] = ledger.advanced
    assert (advanced["status"], advanced["reason"]) == ("deferred", "wrong_base")
    assert ledger.transport.updated == []


def test_revoke_other_tools_stdio_and_a_disabled_connector_queue_nothing(ledger, monkeypatch):
    queued = []
    monkeypatch.setattr(github_app_fastpath, "_enqueue", lambda i: queued.append(i) or True)
    tool = github_app.TOOL_NAME
    assert github_app_fastpath.on_approval_resolved(1, tool, "revoked") is False
    assert github_app_fastpath.on_approval_resolved(1, "delegate", "approved") is False
    monkeypatch.setenv("UA_TRANSPORT", "stdio")
    assert github_app_fastpath.on_approval_resolved(1, tool, "approved") is False
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    assert github_app_fastpath.on_approval_resolved(1, tool, "approved") is False
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    assert github_app_fastpath.on_approval_resolved(1, tool, "denied") is True
    assert queued == [1]


def test_a_failing_fast_path_never_fails_the_resolution(ledger, monkeypatch):
    [pending] = _poll(ledger)

    def boom(_approval_id):
        raise RuntimeError("github unavailable")

    monkeypatch.setattr(github_app_fastpath, "advance_resolved", boom)
    _resolve(pending["approval_id"], "approve")
    assert approvals.get_approval(pending["approval_id"])["status"] == "approved"
    # The next poll still publishes it.
    [completed] = _poll(ledger)
    assert completed["status"] == "approved"


def test_a_ledger_approval_is_not_a_read_of_an_unread_red_tier_delta(ledger):
    """#1327: an approval at the final head is not a read of what it added."""
    from test_shipping_preflight import compare

    from unified_agent.shipping_preflight_cli import PreflightError, review_head_gate

    read, pushed, pr = "d" * 40, "e" * 40, {"repo": "example/project", "number": 123}
    review_reads.record(**pr, head_sha=read, kind="steward-read", reader="s", verdict="pass")
    ledger.prs = [_pr(sha=pushed)]
    _resolve(_poll(ledger)[0]["approval_id"], "approve")
    assert [r["head_sha"] for r in review_reads.for_pr(**pr)] == [read]
    red = {(read, pushed): {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}}
    with pytest.raises(PreflightError, match="deploy/x.sh"):
        review_head_gate(**pr, head_sha=pushed, run_gh=compare(red))


def test_queue_dedupes_and_is_bounded(monkeypatch):
    started = []

    class Thread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(github_app_fastpath.threading, "Thread", Thread)
    monkeypatch.setattr(github_app_fastpath, "_worker", None)
    monkeypatch.setattr(github_app_fastpath, "_queued", {})
    assert github_app_fastpath._enqueue(7) is True
    assert github_app_fastpath._enqueue(7) is True
    assert len(started) == 1 and list(github_app_fastpath._queued) == [7]
    for approval_id in range(100, 100 + github_app_fastpath.QUEUE_MAX - 1):
        assert github_app_fastpath._enqueue(approval_id) is True
    assert github_app_fastpath._enqueue(999) is False
    assert len(started) == 1


def test_rows_of_another_repository_are_skipped(ledger, monkeypatch):
    [pending] = _poll(ledger)
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/other")
    result = github_app_fastpath.advance_resolved(pending["approval_id"])
    assert (result["status"], result["reason"]) == ("skipped", "other_repository")
    assert approvals.get_approval(pending["approval_id"])["status"] == "pending"


class _PushingTransport(RecordingTransport):
    """Moves the PR to a new head at a chosen point of the publication."""

    def __init__(self, prs, *, on):
        super().__init__()
        self.prs = prs
        self.on = on

    def _push(self):
        self.prs[0] = _pr(sha="head-2")

    def create_evidence_comment(self, **kwargs):
        result = super().create_evidence_comment(**kwargs)
        if self.on == "comment":
            self._push()
        return result

    def update_check_run(self, **kwargs):
        super().update_check_run(**kwargs)
        if self.on == "success" and kwargs.get("conclusion") == "success":
            self.on = None
            self._push()


def _channel(channel: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ulid", (channel,)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload"]) for r in rows]


class _ClosingTransport(RecordingTransport):
    """Closes or merges the PR, at the approved head, at a chosen point."""

    def __init__(self, ledger, *, on, state):
        super().__init__()
        self.ledger = ledger
        self.on = on
        self.state = state

    def _close(self):
        closed = {**_pr(sha="head-1"), "state": self.state, "closedAt": "2026-10-26T12:00:00Z"}
        if self.state == "MERGED":
            closed["mergedAt"] = "2026-10-26T12:00:00Z"
        self.ledger.closed = closed
        self.ledger.prs.clear()

    def create_evidence_comment(self, **kwargs):
        result = super().create_evidence_comment(**kwargs)
        if self.on == "comment":
            self._close()
        return result

    def update_check_run(self, **kwargs):
        super().update_check_run(**kwargs)
        if self.on == "success" and kwargs.get("conclusion") == "success":
            self.on = None
            self._close()


def _assert_settled_as_approved(ledger, approval_id):
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success"]
    [evidence] = _channel(github_app.EVIDENCE_CHANNEL)
    assert evidence["approval_id"] == approval_id
    assert _channel(github_app.SUPERSEDED_CHANNEL) == []
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    # Settled: later polls (open list empty, PR closed) have nothing left to do.
    assert _poll(ledger) == [] and _poll(ledger) == []
    assert len(ledger.transport.updated) == 1 and len(ledger.transport.comments) == 1


@pytest.mark.parametrize("closed_state", ["CLOSED", "MERGED"])
@pytest.mark.parametrize("close_on", ["comment", "success"])
def test_close_at_the_approved_head_during_publication_is_not_superseded(
    ledger, closed_state, close_on
):
    # Review round 2: a close or merge at the same head is not a supersession;
    # the approval must still end with its success and evidence.
    ledger.transport = _ClosingTransport(ledger, on=close_on, state=closed_state)
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "approve")
    [advanced] = ledger.advanced
    assert advanced["result"]["status"] == "approved"
    _assert_settled_as_approved(ledger, pending["approval_id"])


@pytest.mark.parametrize("closed_state", ["CLOSED", "MERGED"])
def test_close_at_the_approved_head_during_a_scheduled_poll(ledger, monkeypatch, closed_state):
    monkeypatch.setenv("UA_TRANSPORT", "stdio")  # no fast path: the poll publishes
    ledger.transport = _ClosingTransport(ledger, on="comment", state=closed_state)
    [pending] = _poll(ledger)
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    [result] = _poll(ledger)
    assert result["status"] == "approved"
    _assert_settled_as_approved(ledger, pending["approval_id"])


def test_unreadable_pr_publishes_nothing_and_the_poll_resumes(ledger, monkeypatch):
    monkeypatch.setenv("UA_TRANSPORT", "stdio")
    [pending] = _poll(ledger)
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    real_gh = ledger.gh
    reads = {"n": 0}

    def flaky(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            reads["n"] += 1
            return SimpleNamespace(returncode=0, stdout="[]")
        return real_gh(cmd, **kwargs)

    [failed] = github_app_worker.run_once(
        run_gh=flaky, transport=ledger.transport, policy_config=_policy()
    )
    assert failed["status"] == "error" and reads["n"] == 1
    assert ledger.transport.updated == [] and _channel(github_app.EVIDENCE_CHANNEL) == []
    [resumed] = _poll(ledger)
    assert resumed["status"] == "approved"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success"]


def _fail_reads_after_success(ledger, *, retarget: bool):
    """After the success POST, optionally retarget the PR, and time out one re-read."""
    live = {"posted": False, "fail": True}
    transport = ledger.transport
    update = transport.update_check_run

    def tracked(**kwargs):
        update(**kwargs)
        if kwargs.get("conclusion") == "success" and not live["posted"]:
            live["posted"] = True
            if retarget:
                ledger.closed = {**_pr(sha="head-1"), "baseRefName": "release"}
                ledger.prs.clear()  # open, but off the ledger base's list

    real_gh = ledger.gh

    def gh(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"] and live["posted"] and live["fail"]:
            live["fail"] = False
            raise TimeoutError("post-success PR re-read timed out")
        return real_gh(cmd, **kwargs)

    transport.update_check_run = tracked
    ledger.gh = gh
    return gh


def test_failed_post_success_read_withdraws_the_success_until_reverified(ledger, monkeypatch):
    # Review round 3: the PR is retargeted during the success POST and the
    # verifying re-read times out. No unverified success may stay visible.
    monkeypatch.setattr(github_app_fastpath, "_enqueue", lambda _id: False)
    [pending] = _poll(ledger)
    approval_id = pending["approval_id"]
    gh = _fail_reads_after_success(ledger, retarget=True)
    with _human():
        approvals.approval_resolve(approval_id, "approve")
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=gh, transport=ledger.transport, policy_config=_policy())
    conclusions = [u["conclusion"] for u in ledger.transport.updated]
    assert conclusions == ["success", "action_required"]
    assert ledger.transport.updated[-1]["description"] == github_app.UNVERIFIED_DESCRIPTION
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    assert approvals.get_approval(approval_id)["status"] == "consumed"
    # Off the ledger base the reconcile step holds the head at stale with a
    # non-terminal retargeted marker: not settled, never success.
    assert _poll(ledger) == [] and _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "stale"
    assert ledger.transport.updated[-1]["description"] == github_app.RETARGETED_DESCRIPTION
    # Each settle sets the decided status, compare-and-set: posted once.
    assert [u["conclusion"] for u in ledger.transport.updated] == [
        "success",
        "action_required",
        "stale",
    ]
    assert _channel(github_app.SUPERSEDED_CHANNEL) == []
    assert len(_channel(github_app.RETARGETED_CHANNEL)) == 1
    # Back on the base, the poll finds the consumed approval and re-verifies it.
    ledger.closed = None
    ledger.prs.append(_pr(sha="head-1"))
    [resumed] = _poll(ledger)
    assert resumed["status"] == "approved"
    assert ledger.transport.updated[-1]["conclusion"] == "success"
    assert len(_channel(github_app.EVIDENCE_CHANNEL)) == 1
    assert len(ledger.transport.comments) == 1


def test_failed_post_success_read_on_the_scheduled_poll_is_retried(ledger, monkeypatch):
    monkeypatch.setenv("UA_TRANSPORT", "stdio")  # no fast path: the poll publishes
    [pending] = _poll(ledger)
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    _fail_reads_after_success(ledger, retarget=False)
    [failed] = _poll(ledger)
    assert failed["status"] == "error"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success", "action_required"]
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    [resumed] = _poll(ledger)
    assert resumed["status"] == "approved"
    assert ledger.transport.updated[-1]["conclusion"] == "success"
    assert len(_channel(github_app.EVIDENCE_CHANNEL)) == 1
    assert len(ledger.transport.comments) == 1


def test_a_failed_withdrawal_still_raises_the_original_error(ledger, monkeypatch):
    monkeypatch.setattr(github_app_fastpath, "_enqueue", lambda _id: False)
    [pending] = _poll(ledger)
    gh = _fail_reads_after_success(ledger, retarget=False)
    tracked = ledger.transport.update_check_run

    def update(**kwargs):
        if kwargs.get("conclusion") == "action_required":
            raise ConnectionError("github unavailable")
        tracked(**kwargs)

    ledger.transport.update_check_run = update
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    with pytest.raises(TimeoutError):
        ADVANCE(
            pending["approval_id"], run_gh=gh, transport=ledger.transport, policy_config=_policy()
        )
    assert _channel(github_app.EVIDENCE_CHANNEL) == []


def _approved(ledger, monkeypatch):
    monkeypatch.setattr(github_app_fastpath, "_enqueue", lambda _id: False)
    [pending] = _poll(ledger)
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    return pending["approval_id"]


def _retarget_on_success(ledger, failure=None):
    update = ledger.transport.update_check_run

    def failing(**kwargs):
        if kwargs["conclusion"] == "stale" and failure == "stale_correction_failed":
            raise ConnectionError("stale request failed before write")
        update(**kwargs)
        if kwargs["conclusion"] == "success":
            ledger.closed = {**_pr(sha="head-1"), "baseRefName": "release"}
            ledger.prs.clear()
            if failure == "success_response_lost":
                raise TimeoutError("success accepted, response lost")

    ledger.transport.update_check_run = failing
    return update


@pytest.mark.parametrize("failure", ["success_response_lost", "stale_correction_failed"])
def test_any_failure_after_the_success_post_withdraws_it(ledger, monkeypatch, failure):
    # Review round 4, P2-1: the invariant covers the POST itself and the stale
    # correction, not only the re-read.
    approval_id = _approved(ledger, monkeypatch)
    update = _retarget_on_success(ledger, failure)
    with pytest.raises((TimeoutError, ConnectionError)):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    assert ledger.transport.updated[-1]["conclusion"] == "action_required"
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    ledger.transport.update_check_run = update
    # Off-base and open: the reconcile step holds it at stale, never success.
    assert _poll(ledger) == [] and _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "stale"
    assert _channel(github_app.SUPERSEDED_CHANNEL) == []
    assert approvals.get_approval(approval_id)["status"] == "consumed"


def test_retargeted_head_that_returns_and_merges_is_settled_by_reconcile(ledger, monkeypatch):
    # Review round 4, P2-2: a retarget during publication is not terminal; the
    # PR returns to the base and merges at the approved head before any poll.
    approval_id = _approved(ledger, monkeypatch)
    update = _retarget_on_success(ledger)
    result = ADVANCE(
        approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy()
    )
    assert (result["result"]["status"], result["result"]["reason"]) == (
        "superseded",
        github_app.BASE_OPEN,
    )
    assert _channel(github_app.SUPERSEDED_CHANNEL) == []
    ledger.transport.update_check_run = update
    ledger.closed = {
        **_pr(sha="head-1"),
        "state": "MERGED",
        "closedAt": "2026-10-26T12:00:00Z",
        "mergedAt": "2026-10-26T12:00:00Z",
    }
    assert _poll(ledger) == [] and _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "success"
    [evidence] = _channel(github_app.EVIDENCE_CHANNEL)
    assert evidence["approval_id"] == approval_id
    [outcome] = _channel(github_app.POST_MERGE_CHANNEL)
    assert outcome["outcome"] == "approved_post_merge"
    assert len(ledger.transport.comments) == 1


def test_retargeted_head_that_closes_on_the_other_base_is_superseded(ledger, monkeypatch):
    approval_id = _approved(ledger, monkeypatch)
    update = _retarget_on_success(ledger)
    ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    ledger.transport.update_check_run = update
    ledger.closed = {**ledger.closed, "state": "MERGED", "mergedAt": "2026-10-26T12:00:00Z"}
    assert _poll(ledger) == []
    assert [o["reason"] for o in _channel(github_app.SUPERSEDED_CHANNEL)] == [
        github_app.BASE_CLOSED
    ]
    assert ledger.transport.updated[-1]["conclusion"] == "stale"
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    # Terminal now: later polls neither re-read nor re-post.
    posts = len(ledger.transport.updated)
    ledger.calls.clear()
    assert _poll(ledger) == []
    assert len(ledger.transport.updated) == posts
    assert not [c for c in ledger.calls if c[:3] == ["gh", "pr", "view"]]


def test_failed_evidence_write_withdraws_and_reconcile_republishes(ledger, monkeypatch):
    approval_id = _approved(ledger, monkeypatch)
    real = github_app.events.record_event

    def record(channel, **kwargs):
        if channel == github_app.EVIDENCE_CHANNEL:
            raise OSError("database is locked")
        return real(channel, **kwargs)

    monkeypatch.setattr(github_app.events, "record_event", record)
    with pytest.raises(OSError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success", "action_required"]
    monkeypatch.setattr(github_app.events, "record_event", real)
    ledger.closed = {**_pr(sha="head-1"), "state": "CLOSED", "closedAt": "2026-10-26T12:00:00Z"}
    ledger.prs.clear()
    assert _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "success"
    assert len(_channel(github_app.EVIDENCE_CHANNEL)) == 1
    assert len(ledger.transport.comments) == 1


def test_reconcile_read_failure_is_retried_next_poll(ledger, monkeypatch):
    approval_id = _approved(ledger, monkeypatch)
    _fail_reads_after_success(ledger, retarget=True)
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    real_gh = ledger.gh
    down = {"on": True}

    def gh(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"] and down["on"]:
            raise TimeoutError("still down")
        return real_gh(cmd, **kwargs)

    ledger.gh = gh
    # The read fails for the reconcile step (and the closed-PR step reports it).
    assert [r["status"] for r in _poll(ledger)] == ["error"]
    assert ledger.transport.updated[-1]["conclusion"] == "action_required"
    down["on"] = False
    assert _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "stale"


def test_push_during_publication_never_leaves_a_success_on_the_old_head(ledger):
    # Review P2: the PR moves to head-2 while the evidence comment is posted.
    ledger.transport = _PushingTransport(ledger.prs, on="comment")
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "approve")

    [advanced] = ledger.advanced
    assert advanced["result"]["status"] == "superseded"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale"]
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    assert [o["outcome"] for o in _channel(github_app.SUPERSEDED_CHANNEL)] == ["superseded"]
    # The next poll asks for head-2 and never publishes head-1 as approved.
    [nxt] = _poll(ledger)
    assert nxt["status"] == "pending_approval"
    assert ledger.transport.created[-1]["head_sha"] == "head-2"
    assert "success" not in [u["conclusion"] for u in ledger.transport.updated]


def test_push_right_after_the_success_post_is_corrected_on_the_fast_path(ledger):
    # A push landing between the head check and the success POST is corrected
    # to stale before the head is recorded as settled.
    ledger.transport = _PushingTransport(ledger.prs, on="success")
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "approve")
    [advanced] = ledger.advanced
    assert advanced["result"]["status"] == "superseded"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success", "stale"]
    assert _channel(github_app.EVIDENCE_CHANNEL) == []


def test_scheduled_poll_rechecks_the_head_before_and_after_success(ledger, monkeypatch):
    monkeypatch.setenv("UA_TRANSPORT", "stdio")  # no fast path: the poll publishes
    ledger.transport = _PushingTransport(ledger.prs, on="success")
    [pending] = _poll(ledger)
    with _human():
        approvals.approval_resolve(pending["approval_id"], "approve")
    assert ledger.advanced == []
    [result] = _poll(ledger)
    assert result["status"] == "superseded"
    assert [u["conclusion"] for u in ledger.transport.updated] == ["success", "stale"]
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    # Head-1 is not settled as approved; the next poll moves on to head-2.
    [nxt] = _poll(ledger)
    assert (nxt["status"], ledger.transport.created[-1]["head_sha"]) == (
        "pending_approval",
        "head-2",
    )


def test_unmoved_head_is_read_before_the_comment_and_around_the_success(ledger):
    [pending] = _poll(ledger)
    ledger.calls.clear()
    _resolve(pending["approval_id"], "approve")
    views = [c for c in ledger.calls if c[:3] == ["gh", "pr", "view"]]
    # One read to start the fast path, then the settle's decision before the
    # comment, right before the success POST, and right after it.
    assert len(views) == 4
    assert ledger.transport.updated[-1]["conclusion"] == "success"


# --- round 5: one settle path, converging ---------------------------------------


def test_decide_is_a_pure_function_of_the_approved_and_live_pull_request():
    event = github_app_fastpath._stored_event(
        {
            "event": {
                "repo": "example/project",
                "number": 1,
                "event_type": "synchronize",
                "title": "t",
                "url": "u",
                "state": "OPEN",
                "head_sha": "a",
                "base_branch": "main",
                "head_branch": "topic",
                "author_login": "someone",
            }
        }
    )

    def live(head="a", base="main", state="OPEN"):
        return SimpleNamespace(head_sha=head, base_branch=base, state=state)

    table = [
        (live(), (github_app.SETTLE_SUCCESS, None)),
        (live(state="MERGED"), (github_app.SETTLE_SUCCESS, None)),
        (live(state="CLOSED"), (github_app.SETTLE_SUCCESS, None)),
        (live(head="b"), (github_app.SETTLE_FINAL, github_app.HEAD_MOVED)),
        (live(head="b", state="MERGED"), (github_app.SETTLE_FINAL, github_app.HEAD_MOVED)),
        (live(base="release"), (github_app.SETTLE_HELD, github_app.BASE_OPEN)),
        (live(base="release", state="MERGED"), (github_app.SETTLE_FINAL, github_app.BASE_CLOSED)),
    ]
    for snapshot, expected in table:
        assert github_app.decide(event, snapshot) == expected
    for unreadable in (None, live(head="")):
        with pytest.raises(github_app.LivePullRequestUnavailable):
            github_app.decide(event, unreadable)


def test_publishing_an_approval_needs_a_bound_live_reader(ledger, monkeypatch):
    approval_id = _approved(ledger, monkeypatch)
    pending = github_app_worker._record(
        github_app.PENDING_CHANNEL, approvals.get_approval(approval_id)["payload"]["event_id"]
    )
    event = github_app_fastpath._stored_event(pending)
    ledger.transport.fail_before_comment = True
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    with pytest.raises(PermissionError, match="live pull-request reader"):
        github_app.resume_consumed_pr_event(
            event, ledger.transport, pending_check_run_id=pending["check_run_id"]
        )
    assert ledger.transport.updated == []


@pytest.mark.parametrize("lagging_read", [False, True])
def test_an_old_retargeted_marker_never_suppresses_the_correction(
    ledger, monkeypatch, lagging_read
):
    # Round 5, finding 1: retarget, return, then a retarget during the
    # republish with a lost success response and a failed withdrawal.
    # Task #2242: a lagging read that still reports the old ``stale`` must not
    # match the earlier record either; the uncertain POST invalidated it.
    approval_id = _approved(ledger, monkeypatch)
    if lagging_read:
        monkeypatch.setattr(ledger.transport, "status_matches", lambda **_: True, raising=False)
    update = _retarget_on_success(ledger)
    ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    assert len(_channel(github_app.RETARGETED_CHANNEL)) == 1
    ledger.closed = None
    ledger.prs.append(_pr(sha="head-1"))

    def lost_then_unwithdrawn(**kwargs):
        if kwargs["conclusion"] == "action_required":
            raise ConnectionError("withdrawal unavailable")
        update(**kwargs)
        if kwargs["conclusion"] == "success":
            ledger.closed = {**_pr(sha="head-1"), "baseRefName": "release"}
            ledger.prs.clear()
            raise TimeoutError("success accepted; response lost")

    ledger.transport.update_check_run = lost_then_unwithdrawn
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    assert ledger.transport.updated[-1]["conclusion"] == "success"  # left visible
    ledger.transport.update_check_run = update
    _poll(ledger)
    assert ledger.transport.updated[-1]["conclusion"] == "stale"
    assert _channel(github_app.EVIDENCE_CHANNEL) == []


def test_reconcile_is_fair_and_persists_every_attempt(ledger, monkeypatch):
    # Round 5, finding 2: more unsettled approvals than one poll's limit.
    monkeypatch.setattr(github_app_fastpath, "_enqueue", lambda _id: False)
    count = github_app_fastpath.RECONCILE_LIMIT + 1
    ledger.prs[:] = [
        {**_pr(sha=f"head-{n}"), "number": n, "url": f"https://github.com/example/project/pull/{n}"}
        for n in range(101, 101 + count)
    ]
    snapshots = {p["number"]: {**p, "baseRefName": "release"} for p in ledger.prs}
    pending = _poll(ledger)
    for row in pending:
        with _human():
            approvals.approval_resolve(row["approval_id"], "approve")
        ledger.transport.fail_before_comment = True
        with pytest.raises(TimeoutError):
            ADVANCE(
                row["approval_id"],
                run_gh=ledger.gh,
                transport=ledger.transport,
                policy_config=_policy(),
            )
    ledger.prs.clear()
    reads: list[int] = []

    def gh(cmd, **_kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            reads.append(int(cmd[3]))
            if len(reads) == 1:
                raise TimeoutError("the first read fails; its attempt still counts")
            return SimpleNamespace(returncode=0, stdout=json.dumps(snapshots[int(cmd[3])]))
        return SimpleNamespace(returncode=0, stdout="[]")

    ledger.gh = gh
    number_of = {
        row["approval_id"]: approvals.get_approval(row["approval_id"])["payload"]["number"]
        for row in pending
    }
    _poll(ledger)
    checked = github_app_fastpath._load_checks("example/project")
    # One poll attempts the limit, and records every attempt, the failed one too.
    assert len(checked) == github_app_fastpath.RECONCILE_LIMIT
    failed = next(a for a, n in number_of.items() if n == reads[0])
    assert failed in checked
    held = {p["approval_id"] for p in _channel(github_app.RETARGETED_CHANNEL)}
    assert held == set(checked) - {failed}
    left_out = set(number_of) - set(checked)
    assert len(left_out) == 1  # the newest row; the oldest was not starved
    assert min(number_of) in checked
    _poll(ledger)
    # Within ceil(N / limit) = 2 polls every approval has been settled.
    held = {p["approval_id"] for p in _channel(github_app.RETARGETED_CHANNEL)}
    assert held == set(number_of)


def test_closed_pr_step_settles_through_the_same_decision(ledger, monkeypatch):
    # Round 5, finding 3: reconcile's read fails, then the closed-PR step
    # reaches a PR merged on another base. It must not publish success.
    approval_id = _approved(ledger, monkeypatch)
    ledger.transport.fail_before_comment = True
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    ledger.prs.clear()
    ledger.closed = {
        **_pr(sha="head-1"),
        "baseRefName": "release",
        "state": "MERGED",
        "mergedAt": "2026-10-27T12:00:00Z",
        "closedAt": "2026-10-27T12:00:00Z",
    }
    real_gh = ledger.gh
    reads = []

    def gh(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            reads.append(cmd)
            if len(reads) == 1:
                raise TimeoutError("reconcile read fails")
        return real_gh(cmd, **kwargs)

    ledger.gh = gh
    _poll(ledger)
    assert len(reads) >= 2  # the closed-PR step read it too
    assert _channel(github_app.EVIDENCE_CHANNEL) == []
    assert ledger.transport.updated[-1]["conclusion"] == "stale"
    assert [o["reason"] for o in _channel(github_app.SUPERSEDED_CHANNEL)] == [
        github_app.BASE_CLOSED
    ]


def _held_off_base(ledger, monkeypatch):
    """A consumed approval whose PR is open at the approved head on another base."""
    approval_id = _approved(ledger, monkeypatch)
    ledger.transport.fail_before_comment = True
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    ledger.prs.clear()
    ledger.closed = {**_pr(sha="head-1"), "baseRefName": "release"}
    return approval_id


def test_repeated_settles_of_a_held_head_post_once(ledger, monkeypatch):
    _held_off_base(ledger, monkeypatch)
    for _ in range(5):
        _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale"]
    assert ledger.transport.updated[0]["description"] == github_app.RETARGETED_DESCRIPTION


def test_a_status_changed_by_someone_else_is_corrected_on_the_next_settle(ledger, monkeypatch):
    _held_off_base(ledger, monkeypatch)
    _poll(ledger)
    # Someone else posts success on the held head.
    check_run_id = ledger.transport.updated[-1]["check_run_id"]
    ledger.transport.updated.append(
        {"check_run_id": check_run_id, "status": "completed", "conclusion": "success"}
    )
    _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale", "success", "stale"]
    _poll(ledger)
    assert len(ledger.transport.updated) == 3


def test_a_failed_status_read_posts_anyway(ledger, monkeypatch):
    _held_off_base(ledger, monkeypatch)
    _poll(ledger)

    def unreadable(**_kwargs):
        raise TimeoutError("status read failed")

    monkeypatch.setattr(ledger.transport, "status_matches", unreadable)
    _poll(ledger)
    _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale", "stale", "stale"]


def test_real_transports_compare_against_the_live_status(monkeypatch):
    user = github_app.GitHubUserTokenTransport(lambda: "synthetic")
    sha = "a" * 40
    combined = {
        "statuses": [
            {"context": "other", "state": "success", "description": "x"},
            {
                "context": github_app.COMMIT_STATUS_CONTEXT,
                "state": "error",
                "description": github_app.RETARGETED_DESCRIPTION,
            },
        ]
    }
    seen = []
    monkeypatch.setattr(user, "_get_json", lambda path: seen.append(path) or combined)
    run_id = f"status:{sha}|https://example.invalid/pr/1"
    kwargs = {"repo": "example/project", "check_run_id": run_id, "status": "completed"}
    assert user.status_matches(
        **kwargs, conclusion="stale", description=github_app.RETARGETED_DESCRIPTION
    )
    assert not user.status_matches(**kwargs, conclusion="stale")  # other description
    assert not user.status_matches(**kwargs, conclusion="success")
    assert seen[0] == f"/repos/example/project/commits/{sha}/status?per_page=100"

    app = github_app.GitHubAppHttpTransport(lambda: "synthetic")
    run = {"status": "completed", "conclusion": "stale", "output": {"title": "t"}}
    monkeypatch.setattr(app, "_get_json", lambda path: seen.append(path) or run)
    app_kwargs = {"repo": "example/project", "check_run_id": "42", "status": "completed"}
    assert app.status_matches(**app_kwargs, conclusion="stale")
    assert app.status_matches(**app_kwargs, conclusion="stale", description="t")
    assert not app.status_matches(**app_kwargs, conclusion="success")
    assert seen[-1] == "/repos/example/project/check-runs/42"


def test_a_lagging_read_never_finalizes_a_withdrawn_success(ledger, monkeypatch):
    # Round 6, finding 1: the success POST's response is lost, the withdrawal
    # posts action_required, and a lagging GET still shows success.
    approval_id = _approved(ledger, monkeypatch)
    real_update = ledger.transport.update_check_run

    def response_lost(**kwargs):
        real_update(**kwargs)
        if kwargs["conclusion"] == "success":
            raise TimeoutError("success accepted, response lost")

    monkeypatch.setattr(ledger.transport, "update_check_run", response_lost)
    with pytest.raises(TimeoutError):
        ADVANCE(approval_id, run_gh=ledger.gh, transport=ledger.transport, policy_config=_policy())
    monkeypatch.setattr(ledger.transport, "update_check_run", real_update)
    lagging = {
        "statuses": [
            {
                "context": github_app.COMMIT_STATUS_CONTEXT,
                "state": "success",
                "description": "Human approval recorded",
            }
        ]
    }
    monkeypatch.setattr(ledger.transport, "_get_json", lambda _path: lagging)
    real_matcher = github_app.GitHubUserTokenTransport.status_matches.__get__(ledger.transport)
    monkeypatch.setattr(ledger.transport, "status_matches", real_matcher)
    _poll(ledger)
    # A success is never skipped on a read: it is posted, and evidence follows
    # that POST's own return.
    assert [u["conclusion"] for u in ledger.transport.updated] == [
        "success",
        "action_required",
        "success",
    ]
    assert len(_channel(github_app.EVIDENCE_CHANNEL)) == 1
    assert _poll(ledger) == []
    assert ledger.transport.updated[-1]["conclusion"] == "success"


def test_stale_is_skipped_only_when_our_record_and_github_agree(ledger, monkeypatch):
    _held_off_base(ledger, monkeypatch)
    _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale"]
    posted = _channel(github_app.STATUS_POSTED_CHANNEL)
    assert [p["conclusion"] for p in posted] == ["stale"]
    # GitHub (lagging, or someone else) says stale, but our last post differs:
    # post. Here our record is withdrawn to action_required first.
    check_run_id = ledger.transport.updated[-1]["check_run_id"]
    pending = _channel(github_app.PENDING_CHANNEL)[-1]
    event = github_app_fastpath._stored_event(pending)
    github_app._post_and_record(
        event,
        ledger.transport,
        check_run_id=check_run_id,
        conclusion="action_required",
        description=github_app.UNVERIFIED_DESCRIPTION,
    )
    monkeypatch.setattr(ledger.transport, "status_matches", lambda **_kwargs: True)
    _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == [
        "stale",
        "action_required",
        "stale",
    ]
    _poll(ledger)  # now record and GitHub agree: skipped
    assert len(ledger.transport.updated) == 3


def test_a_lost_stale_response_leaves_no_valid_record_so_the_next_settle_posts(ledger, monkeypatch):
    _held_off_base(ledger, monkeypatch)
    real_update = ledger.transport.update_check_run

    def lost(**kwargs):
        real_update(**kwargs)
        raise TimeoutError("stale accepted, response lost")

    monkeypatch.setattr(ledger.transport, "update_check_run", lost)
    _poll(ledger)
    # Only uncertain markers (task #2242), which match no decided status.
    posted = _channel(github_app.STATUS_POSTED_CHANNEL)
    assert posted and all(p["uncertain"] and p["conclusion"] == "" for p in posted)
    monkeypatch.setattr(ledger.transport, "update_check_run", real_update)
    _poll(ledger)
    _poll(ledger)
    assert [u["conclusion"] for u in ledger.transport.updated] == ["stale", "stale"]


def _rotation(monkeypatch, count=11):
    candidates = [(i, {"number": i}, {}) for i in range(1, count + 1)]
    monkeypatch.setattr(github_app_fastpath, "_unsettled_consumed", lambda _repo: list(candidates))
    monkeypatch.setattr(github_app_fastpath.github_app_worker, "_repo", lambda: "example/project")
    attempted: list[int] = []
    monkeypatch.setattr(
        github_app_fastpath,
        "_reconcile_one",
        lambda aid, _pending, _transport: attempted.append(aid) or {"status": "held"},
    )
    return candidates, attempted


def test_rotation_ignores_the_clock(isolated_env, monkeypatch):
    # Round 6, finding 2: a clock rollback starved one approval. Order is a
    # persisted sequence now; any clock value passed in changes nothing.
    _candidates, attempted = _rotation(monkeypatch)
    github_app_fastpath.reconcile_unsettled(None, set(), now=lambda: 10_000.0)
    assert attempted == list(range(1, 11))
    attempted.clear()
    github_app_fastpath.reconcile_unsettled(None, set(), now=lambda: 100.0)
    assert attempted == [11, *range(1, 10)]
    attempted.clear()
    github_app_fastpath.reconcile_unsettled(None, set(), now=lambda: -5.0)
    assert attempted[0] == 10
    seqs = github_app_fastpath._load_checks("example/project")
    assert sorted(seqs.values()) == list(range(20, 31))  # 30 attempts, one seq each


@pytest.mark.parametrize(
    "content",
    [
        '{"repo": "example/project", "checked": {"1": 1e400}}',
        '{"repo": "example/project", "checked": {"1": ' + str(10**400) + "}}",
        '{"repo": "example/project", "checked": {"1": -3, "2": "x", "3": true, "x": 4}}',
        '{"repo": "example/project", "checked": {"1": 9223372036854775808}}',
        '{"repo": "example/project", "checked": []}',
        "not json",
        "",
    ],
)
def test_a_corrupt_checks_file_never_stops_reconcile(isolated_env, monkeypatch, content):
    # Round 6, finding 3: a corrupt entry raised OverflowError on every poll.
    _candidates, attempted = _rotation(monkeypatch, count=3)
    path = github_app_fastpath.reconcile_checks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    github_app_fastpath.reconcile_unsettled(None, set())
    assert sorted(attempted) == [1, 2, 3]
    record = json.loads(path.read_text(encoding="utf-8"))  # rewritten clean
    assert record["repo"] == "example/project"
    assert all(isinstance(v, int) and 0 < v < 2**63 for v in record["checked"].values())
    assert set(record["checked"]) == {"1", "2", "3"}  # unknown entries pruned


def test_the_checks_file_prunes_settled_and_gone_approvals(isolated_env, monkeypatch):
    candidates, _attempted = _rotation(monkeypatch, count=3)
    github_app_fastpath.reconcile_unsettled(None, set())
    candidates.pop(0)  # approval 1 settled (or deleted)
    github_app_fastpath.reconcile_unsettled(None, set())
    assert set(github_app_fastpath._load_checks("example/project")) == {2, 3}


def test_the_corrupt_numeric_repro_still_recovers_off_base_rows(ledger, monkeypatch):
    approval_id = _held_off_base(ledger, monkeypatch)
    path = github_app_fastpath.reconcile_checks_path()
    path.write_text(
        json.dumps({"repo": "example/project", "checked": {str(approval_id): 10**400}}),
        encoding="utf-8",
    )
    _poll(ledger)
    assert len(_channel(github_app.RETARGETED_CHANNEL)) == 1


_FAILURES = ("none", "lost_response", "failed_withdrawal", "failed_read", "failed_evidence")
_MOVES = ("retarget", "return", "push", "merge", "close", "race_retarget", "race_push")


def _drive(ledger, monkeypatch, seed: int, *, steps: int = 8, settle_polls: int = 3):
    """One seeded run of moves and failures through the real fast path and poll."""
    rng = random.Random(seed)
    approval_id = _approved(ledger, monkeypatch)
    event_id = approvals.get_approval(approval_id)["payload"]["event_id"]
    pr = {"head": "head-1", "base": "main", "state": "OPEN", "pushes": 1}

    def apply():
        view = {**_pr(sha=pr["head"]), "baseRefName": pr["base"], "state": pr["state"]}
        if pr["state"] != "OPEN":
            view["closedAt"] = "2026-10-27T12:00:00Z"
        if pr["state"] == "MERGED":
            view["mergedAt"] = "2026-10-27T12:00:00Z"
        on_list = pr["state"] == "OPEN" and pr["base"] == "main"
        ledger.prs[:] = [view] if on_list else []
        ledger.closed = None if on_list else view

    def move(kind):
        if pr["state"] != "OPEN":
            return
        if kind in ("retarget", "race_retarget"):
            pr["base"] = "release"
        elif kind == "return":
            pr["base"] = "main"
        elif kind in ("push", "race_push"):
            pr["pushes"] += 1
            pr["head"] = f"head-{pr['pushes']}"
        elif kind == "merge":
            pr["state"] = "MERGED"
        elif kind == "close":
            pr["state"] = "CLOSED"
        apply()

    transport = ledger.transport
    base_update = RecordingTransport.update_check_run.__get__(transport)
    real_record = github_app.events.record_event
    real_gh = ledger.gh
    fault = {"kind": "none", "race": None}

    def update(**kwargs):
        if fault["kind"] == "failed_withdrawal" and kwargs["conclusion"] == "action_required":
            raise ConnectionError("withdrawal failed")
        base_update(**kwargs)
        if kwargs["conclusion"] == "success":
            if fault["race"]:
                move(fault["race"])
                fault["race"] = None
            if fault["kind"] in ("lost_response", "failed_withdrawal"):
                raise TimeoutError("success accepted; response lost")

    stored = github_app_fastpath._stored_event(
        github_app_worker._record(github_app.PENDING_CHANNEL, event_id)
    )
    at_evidence: list[str] = []

    def record(channel, **kwargs):
        if channel == github_app.EVIDENCE_CHANNEL:
            if fault["kind"] == "failed_evidence":
                raise OSError("evidence write failed")
            # What the PR really is at the moment evidence is written.
            truth = SimpleNamespace(head_sha=pr["head"], base_branch=pr["base"], state=pr["state"])
            at_evidence.append(github_app.decide(stored, truth)[0])
        return real_record(channel, **kwargs)

    def gh(cmd, **kwargs):
        if fault["kind"] == "failed_read" and cmd[:3] == ["gh", "pr", "view"]:
            if rng.random() < 0.5:
                raise TimeoutError("read failed")
        return real_gh(cmd, **kwargs)

    transport.update_check_run = update
    monkeypatch.setattr(github_app.events, "record_event", record)
    ledger.gh = gh
    for _ in range(steps):
        kind = rng.choice(_MOVES)
        fault["kind"] = rng.choice(_FAILURES)
        if kind.startswith("race_"):
            fault["race"] = kind
        else:
            move(kind)
        try:
            if rng.random() < 0.5:
                ADVANCE(approval_id, run_gh=gh, transport=transport, policy_config=_policy())
            else:
                _poll(ledger)
        except Exception:  # noqa: BLE001 - injected failures are the point
            pass
        fault["race"] = None
    fault["kind"] = "none"
    for _ in range(settle_polls):
        _poll(ledger)
    converged = len(transport.updated)
    # Converged means idempotent: more settles post nothing (compare-and-set).
    for _ in range(2):
        _poll(ledger)
    assert len(transport.updated) == converged, (seed, transport.updated[converged:])

    live = SimpleNamespace(head_sha=pr["head"], base_branch=pr["base"], state=pr["state"])
    decision, _reason = github_app.decide(stored, live)
    head_1 = [
        u["conclusion"]
        for u in transport.updated
        if str(u.get("check_run_id", "")).startswith("status:head-1|")
    ]
    evidence = [e for e in _channel(github_app.EVIDENCE_CHANNEL) if e["event_id"] == event_id]
    # Posts per sha stay bounded by the work done, not by the polls run: at
    # most a success, a withdrawal and a correction per step, plus one to settle.
    assert len(head_1) <= 3 * steps + 1, (seed, head_1)
    return decision, (head_1[-1] if head_1 else None), evidence, at_evidence


@pytest.mark.parametrize("seed", range(24))
def test_every_sequence_converges_once_failures_stop(ledger, monkeypatch, seed):
    decision, last, evidence, at_evidence = _drive(ledger, monkeypatch, seed)
    # Evidence is only ever written while the PR really is at the approved
    # head and base (a race before or after the success POST never settles).
    assert at_evidence in ([], [github_app.SETTLE_SUCCESS]), (seed, at_evidence)
    # A success is visible exactly when its evidence is written.
    assert (last == "success") == bool(evidence), (seed, decision, last)
    # A head the decision says is approvable ends approved, with evidence.
    if decision == github_app.SETTLE_SUCCESS:
        assert evidence, (seed, last)


def test_the_approval_gate_reaches_the_fast_path_through_its_registered_handler(
    ledger, monkeypatch
):
    """#2450: tools.approvals no longer imports the fast path; it calls the handler."""
    assert approvals.ledger_fast_path() is github_app_fastpath._on_resolved
    calls = []
    monkeypatch.setattr(
        github_app_fastpath, "on_approval_resolved", lambda *a: calls.append(a) or True
    )
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "approve")
    assert calls == [(pending["approval_id"], github_app.TOOL_NAME, "approved")]


def test_without_the_fast_path_a_resolution_commits_and_the_poll_publishes(ledger, monkeypatch):
    monkeypatch.setattr(approvals, "_ledger_fast_path", None)
    [pending] = _poll(ledger)
    _resolve(pending["approval_id"], "approve")
    assert approvals.get_approval(pending["approval_id"])["status"] == "approved"
    assert ledger.advanced == []
    [completed] = _poll(ledger)
    assert completed["status"] == "approved"
