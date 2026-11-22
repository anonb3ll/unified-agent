"""Approval Ledger rows orphaned by a failed pending-status POST (task #2103).

The worker finds closed pull requests only through their pending-status
markers. A row whose status POST failed has no marker, so before this task it
stayed ``pending`` (or ``approved``) in the approval queue forever once its PR
closed. PR #1228 stopped such rows from pinning the fast poll cadence; this
retires them: each poll confirms from GitHub that the row's PR closed, or moved
past the row's head, and expires the row with a ledger event naming the PR,
head and reason. A PR still open at the row's head, a live approved pass, or a
GitHub read that fails leaves the row untouched.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import test_github_app_policy_authorization as policy_tests
from mcp.server.mcpserver.exceptions import ToolError
from test_github_app_worker import RecordingTransport, _policy, _pr

from unified_agent import config, embeddings, github_app, github_app_worker, storage
from unified_agent.tools import approvals

OPERATOR = "Operator"
SERVICE = github_app_worker.SERVICE_IDENTITY
CHANNEL = github_app_worker.ORPHAN_RETIRED_CHANNEL


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network and no embedding model in these tests (PR #1302 review)."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("network disabled in tests")
        return real_connect(self, address)

    def no_model():
        raise RuntimeError("embedding model disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", connect)

    def create_connection(*_args, **_kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(embeddings, "get_embedding_model", no_model)


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _numbered(number: int, sha: str = "head-1") -> dict:
    pr = _pr(sha=sha)
    pr["number"] = number
    pr["url"] = f"https://github.com/example/project/pull/{number}"
    return pr


@pytest.fixture
def ledger(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "service-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": SERVICE})
    state = SimpleNamespace(
        open=[_pr(sha="head-1")],
        detail={},  # number -> PR dict `gh pr view` returns
        view_fails=set(),
        views=[],
        transport=RecordingTransport(),
    )

    def gh(cmd, **_kwargs):
        if "view" in cmd:
            number = int(cmd[cmd.index("view") + 1])
            state.views.append(number)
            if number in state.view_fails:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(state.detail.get(number, [])))
        return SimpleNamespace(returncode=0, stdout=json.dumps(state.open))

    state.gh = gh
    return state


def _poll(state):
    return github_app_worker.run_once(
        run_gh=state.gh, transport=state.transport, policy_config=_policy()
    )


def _queue_unbound_row(state, monkeypatch) -> int:
    """Queue rows for the open PRs while every pending-status POST fails."""
    original = state.transport.create_check_run

    def fail(**_kwargs):
        raise TimeoutError("status POST lost")

    monkeypatch.setattr(state.transport, "create_check_run", fail)
    statuses = {r["status"] for r in _poll(state)}
    # The sweep may also report orphans queued earlier that it cannot verify.
    assert "error" in statuses and statuses <= {"error", "orphan_unverified"}
    monkeypatch.setattr(state.transport, "create_check_run", original)
    rows = [
        a
        for a in approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"]
        if a["tool"] == github_app.TOOL_NAME
    ]
    assert rows, "the failed POST must still leave a queued row"
    return max(int(a["id"]) for a in rows)


def _closed(number: int = 123, sha: str = "head-1", *, merged: bool = False) -> dict:
    pr = _numbered(number, sha)
    pr["state"] = "MERGED" if merged else "CLOSED"
    pr["closedAt"] = "2026-10-20T12:00:00Z"
    if merged:
        pr["mergedAt"] = "2026-10-20T12:00:00Z"
    return pr


def _expire_pass(approval_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _retired_events() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ulid", (CHANNEL,)
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload"]) for row in rows]


def test_pending_orphan_on_a_closed_pr_is_retired_with_an_event(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()

    results = _poll(ledger)

    row = approvals.get_approval(approval_id)
    assert row["status"] == "expired"
    assert "closed" in row["reason"]
    [event] = _retired_events()
    assert event["approval_id"] == approval_id
    assert event["repo"] == "example/project"
    assert event["number"] == 123
    assert event["head_sha"] == "head-1"
    assert event["previous_status"] == "pending"
    assert event["outcome"] == "closed"
    assert event["pr_state"] == "CLOSED"
    assert event["reason"] == row["reason"]
    assert [r["status"] for r in results] == ["orphan_retired"]
    assert results[0]["approval_id"] == approval_id
    # No status is posted for a row that never had one.
    assert ledger.transport.created == []
    assert ledger.transport.updated == []
    assert approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"] == []


def test_retirement_is_idempotent(ledger, monkeypatch):
    _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()
    _poll(ledger)
    views = len(ledger.views)
    assert _poll(ledger) == []
    assert len(_retired_events()) == 1
    # A retired row is no longer a candidate, so GitHub is not asked again.
    assert len(ledger.views) == views


def test_expired_approved_orphan_on_a_merged_pr_is_retired(ledger, monkeypatch):
    # The Codex reproduction from PR #1228: the status POST fails, a human
    # approves, the PR merges, and the pass outlives its TTL.
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    with _as(OPERATOR):
        approvals.approval_resolve(approval_id, "approve")
    ledger.open.clear()
    ledger.detail[123] = _closed(merged=True)
    _expire_pass(approval_id)

    [result] = _poll(ledger)

    assert result["status"] == "orphan_retired"
    assert approvals.get_approval(approval_id)["status"] == "expired"
    [event] = _retired_events()
    assert event["previous_status"] == "approved"
    assert event["outcome"] == "merged"
    assert event["pr_state"] == "MERGED"
    assert github_app_worker.stale_unconsumed_github_passes() == 0


def test_live_approved_pass_on_a_closed_pr_is_kept_until_its_ttl(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    with _as(OPERATOR):
        approvals.approval_resolve(approval_id, "approve")
    ledger.open.clear()
    ledger.detail[123] = _closed()

    assert _poll(ledger) == []
    assert approvals.get_approval(approval_id)["status"] == "approved"
    assert _retired_events() == []
    assert ledger.views == []  # not a candidate, so no GitHub read

    _expire_pass(approval_id)
    [result] = _poll(ledger)
    assert result["status"] == "orphan_retired"
    assert approvals.get_approval(approval_id)["status"] == "expired"


def test_github_read_failure_leaves_the_row_untouched(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.view_fails.add(123)

    results = _poll(ledger)

    assert approvals.get_approval(approval_id)["status"] == "pending"
    assert _retired_events() == []
    # Reported, but not as a poll error: a sweep that cannot read GitHub must
    # not fail the poll and pin pr-watch's retry cadence.
    assert [r["status"] for r in results] == ["orphan_unverified"]
    assert results[0]["number"] == 123

    ledger.view_fails.clear()
    ledger.detail[123] = _closed()
    assert [r["status"] for r in _poll(ledger)] == ["orphan_retired"]


def test_unreadable_pr_detail_leaves_the_row_untouched(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()  # `gh pr view` returns [] for #123: no snapshot

    results = _poll(ledger)

    assert approvals.get_approval(approval_id)["status"] == "pending"
    assert _retired_events() == []
    assert [r["status"] for r in results] == ["orphan_unverified"]


def test_pr_still_open_at_the_row_head_is_never_retired(ledger, monkeypatch):
    # Missing from the open list (retargeted off the ledger base, say), but
    # GitHub says it is open at the row's head.
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    still_open = _pr(sha="head-1")
    still_open["baseRefName"] = "release"
    ledger.detail[123] = still_open

    assert _poll(ledger) == []
    assert approvals.get_approval(approval_id)["status"] == "pending"
    assert _retired_events() == []


def test_open_pr_that_moved_past_the_row_head_retires_it_as_superseded(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    moved = _pr(sha="head-2")
    moved["baseRefName"] = "release"
    ledger.detail[123] = moved

    [result] = _poll(ledger)

    assert result["status"] == "orphan_retired"
    assert approvals.get_approval(approval_id)["status"] == "expired"
    [event] = _retired_events()
    assert event["outcome"] == "superseded"
    assert event["head_sha"] == "head-1"
    assert event["pr_head_sha"] == "head-2"


def test_row_on_a_listed_open_pr_is_left_to_the_open_head_path(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    # The next poll binds the row's status (task #2010) and never views the PR.
    [result] = _poll(ledger)
    assert result["status"] == "already_requested"
    assert result["approval_id"] == approval_id
    assert approvals.get_approval(approval_id)["status"] == "pending"
    assert ledger.views == []
    assert _retired_events() == []


def test_bound_row_on_a_closed_pr_keeps_the_existing_close_out(ledger):
    [pending] = _poll(ledger)
    assert pending["status"] == "pending_approval"
    ledger.open.clear()
    ledger.detail[123] = _closed()

    [closed] = _poll(ledger)

    assert closed["status"] == "closed_without_approval"
    assert approvals.get_approval(pending["approval_id"])["status"] == "expired"
    assert ledger.transport.updated[-1]["conclusion"] == "skipped"
    assert _retired_events() == []


def test_sweep_reads_at_most_the_bound_per_poll(ledger, monkeypatch):
    limit = 2
    monkeypatch.setattr(github_app_worker, "ORPHAN_SWEEP_MAX_PRS", limit)
    ledger.open[:] = [_numbered(n) for n in (201, 202, 203)]
    _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    for n in (201, 202, 203):
        ledger.detail[n] = _closed(n)

    first = _poll(ledger)
    assert len(ledger.views) == limit
    assert [r["status"] for r in first] == ["orphan_retired"] * limit
    second = _poll(ledger)
    assert [r["status"] for r in second] == ["orphan_retired"]
    assert sorted(e["number"] for e in _retired_events()) == [201, 202, 203]
    assert approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"] == []


def test_a_concurrent_human_decision_wins_over_the_retirement(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()
    real_read = github_app_worker._view_pr

    def approve_during_read(run_gh, number):
        # The human approves between the candidate scan and the retirement.
        with _as(OPERATOR):
            approvals.approval_resolve(approval_id, "approve")
        return real_read(run_gh, number)

    monkeypatch.setattr(github_app_worker, "_view_pr", approve_during_read)
    assert _poll(ledger) == []
    assert approvals.get_approval(approval_id)["status"] == "approved"
    assert _retired_events() == []


def test_other_repositories_rows_are_not_swept(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/other")

    assert _poll(ledger) == []
    assert ledger.views == []
    assert approvals.get_approval(approval_id)["status"] == "pending"


def test_reopened_pr_at_the_same_head_gets_a_fresh_row(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()
    _poll(ledger)
    assert approvals.get_approval(approval_id)["status"] == "expired"

    ledger.open[:] = [_pr(sha="head-1")]
    [fresh] = _poll(ledger)
    assert fresh["status"] == "pending_approval"
    assert fresh["approval_id"] != approval_id


# --- review round 1: rotation, so stuck orphans cannot starve newer ones ---------------


def _queue_unbound_rows(state, monkeypatch, prs: list[dict]) -> None:
    state.open[:] = prs
    _queue_unbound_row(state, monkeypatch)
    state.open.clear()


def test_stuck_orphans_cannot_starve_a_newer_one(ledger, monkeypatch):
    # Eleven orphans the sweep can never retire: six whose PR read always
    # fails and five whose PR is open at the row's head but off the open list.
    unreadable = list(range(301, 307))
    open_at_head = list(range(307, 312))
    _queue_unbound_rows(ledger, monkeypatch, [_numbered(n) for n in unreadable + open_at_head])
    ledger.view_fails.update(unreadable)
    for n in open_at_head:
        pr = _numbered(n)
        pr["baseRefName"] = "release"
        ledger.detail[n] = pr
    budget = github_app_worker.ORPHAN_SWEEP_MAX_PRS
    assert len(unreadable + open_at_head) > budget
    _poll(ledger)
    _poll(ledger)

    # A newer orphan arrives behind them.
    _queue_unbound_rows(ledger, monkeypatch, [_numbered(400)])
    ledger.detail[400] = _closed(400)
    candidates = len(unreadable + open_at_head) + 1
    retired = []
    for _ in range(-(-candidates // budget)):
        retired += [r for r in _poll(ledger) if r["status"] == "orphan_retired"]
        if retired:
            break
    assert [r["number"] for r in retired] == [400]
    # Every poll stays inside the read bound.
    ledger.views.clear()
    _poll(ledger)
    assert len(ledger.views) <= budget


def test_rotation_reads_every_candidate_within_bounded_polls(ledger, monkeypatch):
    numbers = list(range(501, 513))  # 12 unreadable orphans, budget 10
    _queue_unbound_rows(ledger, monkeypatch, [_numbered(n) for n in numbers])
    ledger.view_fails.update(numbers)
    ledger.views.clear()
    budget = github_app_worker.ORPHAN_SWEEP_MAX_PRS
    for _ in range(-(-len(numbers) // budget)):
        _poll(ledger)
    assert set(ledger.views) == set(numbers)


def test_rotation_state_is_scoped_to_the_repository(ledger, monkeypatch):
    github_app_worker._save_orphan_checks("example/other", {123: 1.0})
    assert github_app_worker._load_orphan_checks("example/project") == {}
    github_app_worker.orphan_checks_path().write_text("not json", encoding="utf-8")
    assert github_app_worker._load_orphan_checks("example/project") == {}


# --- review round 1: a policy-authorized merge of an orphan row --------------------------


@pytest.fixture
def policy_ledger(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, policy_tests.STEWARD)
    monkeypatch.delenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, raising=False)
    monkeypatch.setattr(config, "agent_tokens", lambda *a, **k: dict(policy_tests.BEARERS))
    by_label = {label: bearer for bearer, label in policy_tests.BEARERS.items()}
    from unified_agent.audit import producer_sign

    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", by_label.get)
    storage.init_db()
    return SimpleNamespace(
        gh=policy_tests.FakeGitHub(), transport=policy_tests.RecordingTransport()
    )


def _ppoll(state):
    return github_app_worker.run_once(
        run_gh=state.gh, transport=state.transport, policy_config=_policy()
    )


def _policy_orphan(state, monkeypatch, head: str = policy_tests.HEAD) -> int:
    """Queue an unbound row for ``head`` (its status POST fails)."""
    state.gh.open = [policy_tests._pr(head)]
    original = state.transport.create_check_run

    def fail(**_kwargs):
        raise TimeoutError("status POST lost")

    monkeypatch.setattr(state.transport, "create_check_run", fail)
    assert [r["status"] for r in _ppoll(state)] == ["error"]
    monkeypatch.setattr(state.transport, "create_check_run", original)
    rows = approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"]
    return max(int(a["id"]) for a in rows if a["tool"] == github_app.TOOL_NAME)


def _merge(state, head: str = policy_tests.HEAD) -> None:
    state.gh.open = []
    state.gh.closed = policy_tests._pr(head, state="MERGED", merged=True)


def test_policy_authorized_merge_of_an_orphan_posts_policy_success(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    record = policy_tests._authorize(policy_tests._task())

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired", "policy_authorized"]
    assert results[1]["approval_id"] == approval_id
    assert results[1]["authorization_id"] == record["authorization_id"]
    [success] = [c for c in policy_ledger.transport.created if c.get("conclusion") == "success"]
    assert success["head_sha"] == policy_tests.HEAD
    assert success["description"] == (
        f"Policy-authorized: {policy_tests.POLICY_REF} by {policy_tests.STEWARD}"
    )
    assert policy_ledger.transport.comments == []
    row = approvals.get_approval(approval_id)
    assert row["status"] == "expired"
    assert policy_tests.POLICY_REF in row["reason"]
    [outcome] = [
        json.loads(r["payload"]) for r in _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL)
    ]
    assert outcome["approval_id"] == approval_id
    assert outcome["human_approval"] is False
    [event] = _retired_events()
    assert event["outcome"] == "policy_authorized"
    assert event["policy_ref"] == policy_tests.POLICY_REF
    assert event["authorization_id"] == record["authorization_id"]
    assert _ppoll(policy_ledger) == []


def test_orphan_merge_without_authorization_is_only_expired(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired"]
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert _retired_events()[0]["outcome"] == "merged"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert not [c for c in policy_ledger.transport.created if c.get("conclusion") == "success"]


def test_orphan_for_an_older_head_of_a_policy_merge_is_not_policy_settled(
    policy_ledger, monkeypatch
):
    approval_id = _policy_orphan(policy_ledger, monkeypatch, head=policy_tests.OLD_HEAD)
    _merge(policy_ledger)  # merged at HEAD, which never had a row
    policy_tests._authorize(policy_tests._task())

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired"]
    assert _retired_events()[0]["outcome"] == "merged"
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []


def test_unbound_human_denial_blocks_a_policy_success(policy_ledger, monkeypatch):
    denied = _policy_orphan(policy_ledger, monkeypatch, head=policy_tests.OLD_HEAD)
    with policy_tests.as_identity(config.operator_label()):
        approvals.approval_resolve(denied, "deny")
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired"]
    assert _retired_events()[0]["outcome"] == "merged"
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert approvals.get_approval(denied)["status"] == "denied"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert not [c for c in policy_ledger.transport.created if c.get("conclusion") == "success"]


def _successes(state) -> list[dict]:
    return [c for c in state.transport.created if c.get("conclusion") == "success"]


def test_failed_policy_status_post_is_published_on_the_next_poll(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    original = policy_ledger.transport.create_check_run

    def fail(**_kwargs):
        raise TimeoutError("policy status POST lost")

    monkeypatch.setattr(policy_ledger.transport, "create_check_run", fail)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    # The decision is durable before the POST: the row is fenced out of the
    # queue and the publication is retried, not re-decided.
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []

    monkeypatch.setattr(policy_ledger.transport, "create_check_run", original)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len(_successes(policy_ledger)) == 1
    assert len(_channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL)) == 1
    assert _ppoll(policy_ledger) == []


# --- review at f7fcfbd3 (P2): a retry never posts the success twice ----------------------


def test_outcome_write_failure_after_the_post_does_not_post_twice(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    assert len(_successes(policy_ledger)) == 1

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len(_successes(policy_ledger)) == 1  # reconciled, not re-posted
    [outcome] = [
        json.loads(r["payload"]) for r in _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL)
    ]
    assert outcome["approval_id"] == approval_id
    assert _ppoll(policy_ledger) == []


def test_ambiguous_post_that_reached_github_is_reconciled(policy_ledger, monkeypatch):
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.create_check_run

    def accepted_then_lost(**kwargs):
        original(**kwargs)  # GitHub stored it ...
        raise TimeoutError("... but the response was lost")

    monkeypatch.setattr(transport, "create_check_run", accepted_then_lost)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    monkeypatch.setattr(transport, "create_check_run", original)

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len(_successes(policy_ledger)) == 1


def test_unseen_merged_head_ambiguous_post_is_reconciled(policy_ledger, monkeypatch):
    # The shared non-orphan path: an older head bound, the merged head unseen.
    policy_ledger.gh.open = [policy_tests._pr(policy_tests.OLD_HEAD)]
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["pending_approval"]
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.create_check_run

    def accepted_then_lost(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost")

    monkeypatch.setattr(transport, "create_check_run", accepted_then_lost)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    monkeypatch.setattr(transport, "create_check_run", original)

    assert _ppoll(policy_ledger)[-1]["status"] == "policy_authorized"
    assert len(_successes(policy_ledger)) == 1
    assert len(_channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL)) == 1


def test_publication_without_a_reconcile_lookup_does_not_repost(policy_ledger, monkeypatch):
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport

    def lookup_fails(**_kwargs):
        raise TimeoutError("GitHub read failed")

    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    _ppoll(policy_ledger)
    monkeypatch.setattr(transport, "find_completed_check", lookup_fails)
    # The retry cannot tell whether the first POST landed: it keeps the
    # publication open rather than risk a second success.
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    assert len(_successes(policy_ledger)) == 1
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []


# --- review at f7fcfbd3 (P1): a human denial racing the policy decision wins -------------


def _deny_during_files_read(state, approval_id: int) -> None:
    gh = state.gh
    fired = []

    def wrapped(cmd, **kwargs):
        if cmd[:2] == ["gh", "api"] and not fired:
            fired.append(True)
            with policy_tests.as_identity(config.operator_label()):
                approvals.approval_resolve(approval_id, "deny")
        return gh(cmd, **kwargs)

    state.gh = wrapped


def test_denial_of_the_orphan_during_the_files_read_wins(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _deny_during_files_read(policy_ledger, approval_id)

    assert _ppoll(policy_ledger) == []

    assert approvals.get_approval(approval_id)["status"] == "denied"
    assert _successes(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL) == []
    assert _retired_events() == []


def test_denial_on_the_unseen_merged_head_path_during_the_files_read_wins(
    policy_ledger, monkeypatch
):
    policy_ledger.gh.open = [policy_tests._pr(policy_tests.OLD_HEAD)]
    [older] = _ppoll(policy_ledger)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _deny_during_files_read(policy_ledger, older["approval_id"])

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["denied"]
    assert _successes(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL) == []


def test_a_denial_after_the_decision_commits_is_refused(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.create_check_run
    refusals = []

    def deny_then_post(**kwargs):
        # The human tries to deny between the committed decision and the POST.
        with policy_tests.as_identity(config.operator_label()):
            try:
                approvals.approval_resolve(approval_id, "deny")
            except ToolError as exc:
                refusals.append(str(exc))
        return original(**kwargs)

    monkeypatch.setattr(transport, "create_check_run", deny_then_post)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "policy_authorized"]
    assert refusals and "expired" in refusals[0]
    assert approvals.get_approval(approval_id)["status"] == "expired"


# --- review at f7fcfbd3, follow-up: the bound policy path takes the same fence -----------


def _bound_pending_then_merge(state) -> int:
    """A head with a pending status marker that then merges at the same head."""
    state.gh.open = [policy_tests._pr()]
    [pending] = _ppoll(state)
    assert pending["status"] == "pending_approval"
    _merge(state)
    return int(pending["approval_id"])


def _success_updates(state) -> list[dict]:
    return [u for u in state.transport.updated if u.get("conclusion") == "success"]


def test_bound_row_denied_during_the_files_read_gets_no_policy_success(policy_ledger):
    approval_id = _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _deny_during_files_read(policy_ledger, approval_id)

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["denied"]
    assert approvals.get_approval(approval_id)["status"] == "denied"
    assert _success_updates(policy_ledger) == []
    assert _successes(policy_ledger) == []
    assert policy_ledger.transport.updated[-1]["conclusion"] == "failure"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL) == []
    assert _ppoll(policy_ledger) == []


def test_bound_policy_close_commits_before_posting(policy_ledger):
    approval_id = _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.update_check_run
    seen = []

    def update(**kwargs):
        # By the time the success is posted the decision is durable and the
        # row is out of the queue, so a denial now is refused.
        seen.append(
            (
                approvals.get_approval(approval_id)["status"],
                len(_channel_rows(github_app_worker.POLICY_INTENT_CHANNEL)),
            )
        )
        return original(**kwargs)

    transport.update_check_run = update
    [result] = _ppoll(policy_ledger)

    assert result["status"] == "policy_authorized"
    assert result["approval_id"] == approval_id
    assert seen == [("expired", 1)]
    assert len(_success_updates(policy_ledger)) == 1
    assert _ppoll(policy_ledger) == []


def test_bound_policy_close_outcome_failure_does_not_post_twice(policy_ledger, monkeypatch):
    _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    assert len(_success_updates(policy_ledger)) == 1

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len(_success_updates(policy_ledger)) == 1
    assert len(_channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL)) == 1
    assert _ppoll(policy_ledger) == []


# --- review at 6b25b077 (P2): an unposted intent is re-validated before its POST ---------


def _fail_bound_update(state):
    transport = state.transport
    original = transport.update_check_run

    def fail(**_kwargs):
        raise TimeoutError("status POST lost before it reached GitHub")

    transport.update_check_run = fail
    return lambda: setattr(transport, "update_check_run", original)


def test_withdrawn_authorization_cancels_an_unposted_bound_publication(policy_ledger, monkeypatch):
    approval_id = _bound_pending_then_merge(policy_ledger)
    record = policy_tests._authorize(policy_tests._task())
    restore = _fail_bound_update(policy_ledger)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    restore()
    # The authorizer is withdrawn before the success ever reached GitHub.
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "someone_else")
    assert policy_tests._verified() is None

    [withdrawn] = _ppoll(policy_ledger)

    assert withdrawn["status"] == "policy_withdrawn"
    assert withdrawn["approval_id"] == approval_id
    assert _success_updates(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    [event] = [
        json.loads(r["payload"]) for r in _channel_rows(github_app_worker.POLICY_WITHDRAWN_CHANNEL)
    ]
    assert event["authorization_id"] == record["authorization_id"]
    assert event["approval_id"] == approval_id
    # The head then closes out as it would have without a policy.
    [closed] = _ppoll(policy_ledger)
    assert closed["status"] == "merged_without_approval"
    assert _success_updates(policy_ledger) == []
    assert _ppoll(policy_ledger) == []


def test_withdrawn_authorization_still_finishes_a_success_already_on_github(
    policy_ledger, monkeypatch
):
    _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    assert len(_success_updates(policy_ledger)) == 1
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "someone_else")

    # The success is already public: its evidence is completed, nothing reposted.
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len(_success_updates(policy_ledger)) == 1
    assert _channel_rows(github_app_worker.POLICY_WITHDRAWN_CHANNEL) == []


def test_withdrawn_authorization_cancels_an_unposted_orphan_publication(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    original = policy_ledger.transport.create_check_run

    def fail(**_kwargs):
        raise TimeoutError("policy status POST lost")

    monkeypatch.setattr(policy_ledger.transport, "create_check_run", fail)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    monkeypatch.setattr(policy_ledger.transport, "create_check_run", original)
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "someone_else")

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_withdrawn"]
    assert _successes(policy_ledger) == []
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert _ppoll(policy_ledger) == []


# --- review at 6b25b077 (P1): every pending row of the PR is fenced -----------------------


def test_older_bound_row_cannot_be_denied_after_an_unseen_head_decision(policy_ledger):
    # Reviewer repro: bind OLD_HEAD, merge an unseen HEAD under a valid
    # authorization, deny the older row after the intent commits.
    policy_ledger.gh.open = [policy_tests._pr(policy_tests.OLD_HEAD)]
    [older] = _ppoll(policy_ledger)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.create_check_run
    refusals = []

    def deny_then_post(**kwargs):
        if kwargs.get("conclusion") == "success":
            with policy_tests.as_identity(config.operator_label()):
                try:
                    approvals.approval_resolve(older["approval_id"], "deny")
                except ToolError as exc:
                    refusals.append(str(exc))
        return original(**kwargs)

    transport.create_check_run = deny_then_post
    results = _ppoll(policy_ledger)

    assert results[-1]["status"] == "policy_authorized"
    assert refusals and "not 'pending'" in refusals[0]
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    [intent] = [
        json.loads(r["payload"]) for r in _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL)
    ]
    assert [row["id"] for row in intent["fenced_rows"]] == [older["approval_id"]]


def test_same_head_replacement_cannot_be_denied_after_a_bound_decision(policy_ledger, monkeypatch):
    # Reviewer repro: an expired bound row plus a fresh same-head replacement
    # whose pending POST failed; deny the replacement after the intent commits.
    bound = _bound_pending_then_merge(policy_ledger)
    policy_ledger.gh.open = [policy_tests._pr()]  # still open while replaced
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status='expired' WHERE id=?", (bound,))
        conn.commit()
    finally:
        conn.close()
    replacement = _policy_orphan(policy_ledger, monkeypatch)
    assert replacement != bound
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.update_check_run
    refusals = []

    def deny_then_post(**kwargs):
        with policy_tests.as_identity(config.operator_label()):
            try:
                approvals.approval_resolve(replacement, "deny")
            except ToolError as exc:
                refusals.append(str(exc))
        return original(**kwargs)

    transport.update_check_run = deny_then_post
    results = _ppoll(policy_ledger)

    assert results[0]["status"] == "policy_authorized"
    assert refusals and "not 'pending'" in refusals[0]
    assert approvals.get_approval(replacement)["status"] == "expired"


def test_a_denial_on_any_row_of_the_pr_before_the_commit_blocks_it(policy_ledger, monkeypatch):
    bound = _bound_pending_then_merge(policy_ledger)
    policy_ledger.gh.open = [policy_tests._pr()]
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status='expired' WHERE id=?", (bound,))
        conn.commit()
    finally:
        conn.close()
    replacement = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _deny_during_files_read(policy_ledger, replacement)

    _ppoll(policy_ledger)

    assert _success_updates(policy_ledger) == []
    assert _successes(policy_ledger) == []
    assert _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL) == []
    assert approvals.get_approval(replacement)["status"] == "denied"


# --- review at f7fcfbd3 (P2): corrupt rotation state is disposable -----------------------


@pytest.mark.parametrize(
    "value",
    [10**400, "1e400", "nan", "inf", "-inf", -1, "abc", None, [], {"a": 1}, 1e300],
)
def test_bad_rotation_timestamps_are_never_checked(ledger, value):
    path = github_app_worker.orphan_checks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repo": "example/project", "checked": {"123": value, "124": 5.0}}),
        encoding="utf-8",
    )
    assert github_app_worker._load_orphan_checks("example/project") == {124: 5.0}


@pytest.mark.parametrize(
    "checked",
    [{"9" * 5000: 1.0}, {"-3": 1.0}, {"x": 1.0}, ["123", 1.0], "checked"],
)
def test_bad_rotation_keys_or_shapes_are_dropped(ledger, checked):
    path = github_app_worker.orphan_checks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"repo": "example/project", "checked": checked}), encoding="utf-8")
    assert github_app_worker._load_orphan_checks("example/project") == {}


def test_oversized_rotation_file_is_ignored(ledger):
    path = github_app_worker.orphan_checks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    big = {str(n): 1.0 for n in range(1, 200_000)}
    path.write_text(json.dumps({"repo": "example/project", "checked": big}), encoding="utf-8")
    assert path.stat().st_size > github_app_worker.ORPHAN_CHECKS_MAX_BYTES
    assert github_app_worker._load_orphan_checks("example/project") == {}


def test_overflowing_rotation_timestamp_does_not_abort_the_poll(ledger, monkeypatch):
    approval_id = _queue_unbound_row(ledger, monkeypatch)
    ledger.open.clear()
    ledger.detail[123] = _closed()
    path = github_app_worker.orphan_checks_path()
    path.write_text(
        json.dumps({"repo": "example/project", "checked": {"123": 10**400}}), encoding="utf-8"
    )

    results = _poll(ledger)

    assert [r["status"] for r in results] == ["orphan_retired"]
    assert approvals.get_approval(approval_id)["status"] == "expired"
    # The rewritten state is well formed again.
    assert set(json.loads(path.read_text(encoding="utf-8"))["checked"]) <= {"123"}


def test_overflowing_rotation_timestamp_with_no_candidates_is_harmless(ledger):
    path = github_app_worker.orphan_checks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repo": "example/project", "checked": {"123": 10**400}}), encoding="utf-8"
    )
    ledger.open.clear()
    assert _poll(ledger) == []


def _channel_rows(channel: str) -> list:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ulid", (channel,)
        ).fetchall()
    finally:
        conn.close()


# --- review at f7fcfbd3 (P2): the transports' read-only reconcile lookup ------------------


def _stub_github(monkeypatch, payload):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def open_request(request, timeout):
        requests.append((request.get_method(), request.full_url, request.data, timeout))
        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    return requests


def test_user_transport_finds_a_posted_policy_status(monkeypatch):
    description = "Policy-authorized: ref by Desktop_Agent1"
    requests = _stub_github(
        monkeypatch,
        [
            {"context": "other", "state": "success", "description": description},
            {
                "context": github_app.COMMIT_STATUS_CONTEXT,
                "state": "success",
                "description": description,
            },
        ],
    )
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    kwargs = dict(
        repo="example/project",
        head_sha="a" * 40,
        name=github_app.CHECK_RUN_NAME,
        conclusion="success",
        description=description,
    )
    assert transport.find_completed_check(**kwargs) is True
    assert transport.find_completed_check(**{**kwargs, "description": "something else"}) is False
    method, url, body, timeout = requests[0]
    assert (method, body, timeout) == ("GET", None, 30)
    assert url == (
        "https://api.github.com/repos/example/project/commits/"
        + "a" * 40
        + "/statuses?per_page=100&page=1"
    )


def test_app_transport_finds_a_posted_policy_check_run(monkeypatch):
    description = "Policy-authorized: ref by Desktop_Agent1"
    requests = _stub_github(
        monkeypatch,
        {
            "total_count": 2,
            "check_runs": [
                {
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "neutral",
                    "output": {"title": description},
                },
                {
                    "head_sha": "a" * 40,
                    "external_id": "ua-policy-ours",
                    "status": "completed",
                    "conclusion": "success",
                    "output": {"title": description},
                },
            ],
        },
    )
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    kwargs = dict(
        repo="example/project",
        head_sha="a" * 40,
        name=github_app.CHECK_RUN_NAME,
        conclusion="success",
        description=description,
        external_id="ua-policy-ours",
    )
    assert transport.find_completed_check(**kwargs) is True
    # Our run with another conclusion is ambiguous, never proof of absence.
    with pytest.raises(github_app.ReconcileIncomplete):
        transport.find_completed_check(**{**kwargs, "conclusion": "failure"})
    assert "/check-suites?" in requests[0][1]  # the suite window is checked first
    method, url, body, _timeout = next(r for r in requests if "/check-runs?" in r[1])
    assert (method, body) == ("GET", None)
    assert url.startswith(
        "https://api.github.com/repos/example/project/commits/" + "a" * 40 + "/check-runs?"
    )
    assert "filter=all" in url and "check_name=Approval%20Ledger" in url


def test_reconcile_lookup_rejects_a_malformed_response(monkeypatch):
    _stub_github(monkeypatch, {"not": "a list"})
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    with pytest.raises(ToolError):
        transport.find_completed_check(
            repo="example/project",
            head_sha="a" * 40,
            name=github_app.CHECK_RUN_NAME,
            conclusion="success",
            description="x",
        )


# --- review at 6b25b077 (P2): reconciliation proves absence or defers ---------------------


class _FakeStatusAPI:
    """Commit statuses served newest first, paginated like GitHub (offline)."""

    def __init__(self, sha):
        self.sha = sha
        self.statuses = []  # newest first
        self.posts = []
        self.gets = []

    def open(self, request, timeout):
        url = request.full_url
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        if method == "POST" and url.endswith(f"/statuses/{self.sha}"):
            self.posts.append(body)
            self.statuses.insert(0, body)
            payload = {"id": len(self.posts)}
        elif method == "GET" and f"/commits/{self.sha}/statuses" in url:
            self.gets.append(url)
            page = int(url.rsplit("page=", 1)[1].split("&", 1)[0])
            per = int(url.split("per_page=", 1)[1].split("&", 1)[0])
            payload = self.statuses[(page - 1) * per : page * per]
        else:
            raise AssertionError(f"unexpected request {method} {url}")
        data = json.dumps(payload).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return data

        return Response()


def _lookup(transport, **extra):
    return transport.find_completed_check(
        repo="example/project",
        head_sha="a" * 40,
        name=github_app.CHECK_RUN_NAME,
        conclusion="success",
        description="Policy-authorized: ref by someone",
        **extra,
    )


def test_user_lookup_pages_past_newer_unrelated_statuses(monkeypatch):
    api = _FakeStatusAPI("a" * 40)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    api.statuses = [
        {
            "context": github_app.COMMIT_STATUS_CONTEXT,
            "state": "success",
            "description": "Policy-authorized: ref by someone",
        }
    ]
    api.statuses[0:0] = [
        {"context": f"ci/{n}", "state": "success", "description": "ok"} for n in range(100)
    ]
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    assert _lookup(transport) is True
    assert len(api.gets) == 2


def test_user_lookup_proves_absence_only_after_the_last_page(monkeypatch):
    api = _FakeStatusAPI("a" * 40)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    api.statuses = [{"context": f"ci/{n}", "state": "success"} for n in range(150)]
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    assert _lookup(transport) is False
    assert len(api.gets) == 2


def test_user_lookup_that_cannot_finish_defers(monkeypatch):
    api = _FakeStatusAPI("a" * 40)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    pages = github_app.RECONCILE_MAX_PAGES
    api.statuses = [{"context": f"ci/{n}", "state": "success"} for n in range(100 * pages)]
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    with pytest.raises(github_app.ReconcileIncomplete):
        _lookup(transport)
    assert len(api.gets) == pages


def test_app_lookup_reads_a_bound_check_run_directly(monkeypatch):
    requests = _stub_github(
        monkeypatch,
        {
            "id": 77,
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "Policy-authorized: ref by someone"},
        },
    )
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    assert _lookup(transport, check_run_id="77") is True
    assert requests[0][1] == "https://api.github.com/repos/example/project/check-runs/77"


def test_app_lookup_for_another_head_is_not_a_match(monkeypatch):
    _stub_github(
        monkeypatch,
        {
            "id": 77,
            "head_sha": "b" * 40,
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "Policy-authorized: ref by someone"},
        },
    )
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    assert _lookup(transport, check_run_id="77") is False


def test_app_lookup_pages_by_total_count(monkeypatch):
    calls = []
    target = {
        "head_sha": "a" * 40,
        "external_id": "ua-policy-ours",
        "status": "completed",
        "conclusion": "success",
        "output": {"title": "Policy-authorized: ref by someone"},
    }

    def open_request(request, timeout):
        calls.append(request.full_url)
        page = int(request.full_url.rsplit("page=", 1)[1].split("&", 1)[0])
        runs = [{"head_sha": "a" * 40, "status": "completed", "conclusion": "neutral"}] * 100
        payload = {"total_count": 101, "check_runs": runs if page == 1 else [target]}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    assert _lookup(transport, external_id="ua-policy-ours") is True
    assert len([c for c in calls if "/check-runs?" in c]) == 2
    assert any("/check-suites?" in c for c in calls)


def test_real_user_transport_retry_never_double_posts(policy_ledger, monkeypatch):
    # Reviewer repro, end to end through the real user transport: the first
    # success lands, the outcome write fails, and 100 newer unrelated CI
    # statuses push it onto page 2 before the retry.
    api = _FakeStatusAPI(policy_tests.HEAD)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    real = github_app.GitHubUserTokenTransport(lambda: "test-token")
    failing = {"on": True}
    real_create = real.create_check_run

    def create(**kwargs):
        if failing["on"]:
            raise TimeoutError("pending status POST lost")
        return real_create(**kwargs)

    real.create_check_run = create
    policy_ledger.transport = real
    policy_ledger.gh.open = [policy_tests._pr()]
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    failing["on"] = False
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    _ppoll(policy_ledger)
    successes = [p for p in api.posts if p.get("state") == "success"]
    assert len(successes) == 1
    api.statuses[0:0] = [
        {"context": f"ci/{n}", "state": "success", "description": "ok"} for n in range(100)
    ]

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len([p for p in api.posts if p.get("state") == "success"]) == 1


# --- review at 62ad26ed (P2): the first publication is re-validated too -------------------


def _withdraw_during_files_read(state, withdraw) -> None:
    gh = state.gh
    fired = []

    def wrapped(cmd, **kwargs):
        if cmd[:2] == ["gh", "api"] and not fired:
            fired.append(True)
            withdraw()
        return gh(cmd, **kwargs)

    state.gh = wrapped


def _remove_authorizer(monkeypatch):
    return lambda: monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "someone_else")


def _rotate_bearer(monkeypatch):
    from unified_agent.audit import producer_sign

    def rotate():
        monkeypatch.setattr(
            producer_sign,
            "resolve_agent_bearer_token",
            lambda label: "rotated-bearer" if label == policy_tests.STEWARD else None,
        )

    return rotate


@pytest.mark.parametrize("withdrawal", ["remove_authorizer", "rotate_bearer"])
def test_orphan_withdrawn_during_the_files_read_posts_nothing(
    policy_ledger, monkeypatch, withdrawal
):
    # Reviewer repro: the authorization stops verifying during the changed-
    # files read, after _policy_authorization checked it.
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    withdraw = (
        _remove_authorizer(monkeypatch)
        if withdrawal == "remove_authorizer"
        else _rotate_bearer(monkeypatch)
    )
    _withdraw_during_files_read(policy_ledger, withdraw)

    results = _ppoll(policy_ledger)

    assert policy_tests._verified() is None
    assert _successes(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL) == []
    # Retired as an ordinary merged orphan, with no policy claim.
    assert [r["status"] for r in results] == ["orphan_retired"]
    assert _retired_events()[0]["outcome"] == "merged"
    assert approvals.get_approval(approval_id)["status"] == "expired"


def test_bound_row_withdrawn_during_the_files_read_posts_nothing(policy_ledger, monkeypatch):
    _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _withdraw_during_files_read(policy_ledger, _remove_authorizer(monkeypatch))

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["merged_without_approval"]
    assert _success_updates(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []


def test_withdrawal_between_the_commit_and_the_first_post_posts_nothing(policy_ledger, monkeypatch):
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_commit = github_app_worker._commit_policy_intent

    def commit_then_withdraw(*args, **kwargs):
        committed = real_commit(*args, **kwargs)
        monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "someone_else")
        return committed

    monkeypatch.setattr(github_app_worker, "_commit_policy_intent", commit_then_withdraw)

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired", "policy_withdrawn"]
    assert _successes(policy_ledger) == []
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert len(_channel_rows(github_app_worker.POLICY_WITHDRAWN_CHANNEL)) == 1
    assert approvals.get_approval(approval_id)["status"] == "expired"
    assert _ppoll(policy_ledger) == []


# --- review at 62ad26ed (P2): App reconciliation covers every check suite ----------------


class _FakeChecksAPI:
    """Check runs and suites for one head, windowed like GitHub's ref endpoint (offline)."""

    WINDOW = 1000

    def __init__(self, sha):
        self.sha = sha
        self.suites = []  # oldest first; each a list of runs
        self.posts = []
        self.patches = []
        self.fail_posts = 0

    def add_suites(self, count):
        self.suites.extend([] for _ in range(count))

    def open(self, request, timeout):
        url = request.full_url
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        if method == "POST" and url.endswith("/check-runs"):
            if self.fail_posts:
                self.fail_posts -= 1
                raise TimeoutError("check run POST lost")
            run = {**body, "id": len(self.posts) + 1, "output": body.get("output") or {}}
            self.posts.append(run)
            self.suites.append([run])
            payload = {"id": run["id"]}
        elif method == "PATCH" and "/check-runs/" in url:
            run_id = int(url.rsplit("/", 1)[1])
            run = next(r for r in self.posts if r["id"] == run_id)
            run.update(body)
            self.patches.append({"id": run_id, **body})
            payload = {"id": run_id}
        elif method == "GET" and f"/commits/{self.sha}/check-suites" in url:
            payload = {"total_count": len(self.suites), "check_suites": []}
        elif method == "GET" and f"/commits/{self.sha}/check-runs" in url:
            page = int(url.rsplit("page=", 1)[1].split("&", 1)[0])
            name = url.split("check_name=", 1)[1].split("&", 1)[0]
            window = self.suites[-self.WINDOW :]
            runs = [
                run
                for suite in reversed(window)
                for run in suite
                if run.get("name", "").replace(" ", "%20").replace(":", "%3A") == name
            ]
            payload = {"total_count": len(runs), "check_runs": runs[(page - 1) * 100 : page * 100]}
        else:
            raise AssertionError(f"unexpected request {method} {url}")
        data = json.dumps(payload).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return data

        return Response()


def _app_orphan_then_fail_outcome(policy_ledger, monkeypatch, api):
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    policy_ledger.transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.gh.open = [policy_tests._pr()]
    api.fail_posts = 1  # the orphan's pending check never lands
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["error"]
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    assert len([p for p in api.posts if p.get("conclusion") == "success"]) == 1


def test_app_retry_defers_when_newer_suites_push_the_run_out_of_the_window(
    policy_ledger, monkeypatch
):
    # Reviewer repro: the success lands, the outcome write fails, then 1,001
    # newer unrelated suites move the run outside the ref endpoint's window.
    api = _FakeChecksAPI(policy_tests.HEAD)
    _app_orphan_then_fail_outcome(policy_ledger, monkeypatch, api)
    api.add_suites(1001)

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["error"]
    assert len([p for p in api.posts if p.get("conclusion") == "success"]) == 1
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []


def test_app_retry_inside_the_window_reconciles_without_reposting(policy_ledger, monkeypatch):
    api = _FakeChecksAPI(policy_tests.HEAD)
    _app_orphan_then_fail_outcome(policy_ledger, monkeypatch, api)
    api.add_suites(50)

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_authorized"]
    assert len([p for p in api.posts if p.get("conclusion") == "success"]) == 1


def test_app_policy_run_carries_the_intent_external_id(policy_ledger, monkeypatch):
    api = _FakeChecksAPI(policy_tests.HEAD)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    policy_ledger.transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.gh.open = [policy_tests._pr()]
    api.fail_posts = 1
    _ppoll(policy_ledger)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "policy_authorized"]
    [success] = [p for p in api.posts if p.get("conclusion") == "success"]
    [intent] = [
        json.loads(r["payload"]) for r in _channel_rows(github_app_worker.POLICY_INTENT_CHANNEL)
    ]
    assert success["external_id"] == github_app_worker._intent_external_id(intent)


def test_app_lookup_treats_our_unfinished_run_as_ambiguous(monkeypatch):
    payloads = iter(
        [
            {"total_count": 1, "check_suites": []},
            {
                "total_count": 1,
                "check_runs": [
                    {
                        "head_sha": "a" * 40,
                        "external_id": "intent-1",
                        "status": "in_progress",
                        "conclusion": None,
                        "output": {},
                    }
                ],
            },
        ]
    )

    def open_request(request, timeout):
        data = json.dumps(next(payloads)).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return data

        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    with pytest.raises(github_app.ReconcileIncomplete):
        _lookup(transport, external_id="intent-1")


# --- review at 83f278fb (P2): the credential fence, and compensation after the POST ------


def _arm_after_commit(monkeypatch, action):
    """Run ``action`` inside the final pre-POST verification (after the commit)."""
    armed = []
    real_commit = github_app_worker._commit_policy_intent
    real_denied = github_app_worker._human_denied

    def commit(*args, **kwargs):
        committed = real_commit(*args, **kwargs)
        armed.append(True)
        return committed

    def denied(number):
        result = real_denied(number)
        if armed and armed[0] is True:
            armed[0] = "fired"
            action()  # lands after the signature check, before the POST
        return result

    monkeypatch.setattr(github_app_worker, "_commit_policy_intent", commit)
    monkeypatch.setattr(github_app_worker, "_human_denied", denied)


def _conclusions(state) -> list:
    return [c.get("conclusion") for c in state.transport.created]


def test_rotation_inside_the_final_verify_is_corrected_after_the_post(policy_ledger, monkeypatch):
    # Reviewer repro: an out-of-band credential change lands after the final
    # signature check and before the POST. A remote POST cannot be atomic with
    # a local check, so the poll re-verifies after it and publishes a
    # corrective non-success status with its own evidence event.
    approval_id = _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _arm_after_commit(monkeypatch, _rotate_bearer(monkeypatch))

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired", "policy_withdrawn_after_post"]
    assert _conclusions(policy_ledger) == ["success", "cancelled"]
    corrective = policy_ledger.transport.created[-1]
    assert corrective["head_sha"] == policy_tests.HEAD
    assert "withdrawn" in corrective["description"].lower()
    [marker] = [
        json.loads(r["payload"])
        for r in _channel_rows(github_app_worker.POLICY_WITHDRAWN_AFTER_POST_CHANNEL)
    ]
    assert marker["approval_id"] == approval_id
    assert marker["head_sha"] == policy_tests.HEAD
    assert len(_channel_rows(github_app_worker.POLICY_WITHDRAWN_CHANNEL)) == 1
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _ppoll(policy_ledger) == []
    assert _conclusions(policy_ledger) == ["success", "cancelled"]


def test_bound_rotation_inside_the_final_verify_is_corrected_on_the_same_check(
    policy_ledger, monkeypatch
):
    _bound_pending_then_merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _arm_after_commit(monkeypatch, _rotate_bearer(monkeypatch))

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["policy_withdrawn_after_post"]
    updates = policy_ledger.transport.updated
    assert [u["conclusion"] for u in updates[-2:]] == ["success", "cancelled"]
    assert updates[-1]["check_run_id"] == updates[-2]["check_run_id"]
    # The head then closes out without a policy.
    [closed] = _ppoll(policy_ledger)
    assert closed["status"] == "merged_without_approval"


def test_failed_corrective_post_is_retried_without_reposting_success(policy_ledger, monkeypatch):
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _arm_after_commit(monkeypatch, _rotate_bearer(monkeypatch))
    transport = policy_ledger.transport
    original = transport.create_check_run
    calls = []

    def create(**kwargs):
        calls.append(kwargs.get("conclusion"))
        if kwargs.get("conclusion") == "cancelled" and calls.count("cancelled") == 1:
            raise TimeoutError("corrective POST lost")
        return original(**kwargs)

    monkeypatch.setattr(transport, "create_check_run", create)
    assert [r["status"] for r in _ppoll(policy_ledger)] == ["orphan_retired", "error"]
    assert _conclusions(policy_ledger) == ["success"]
    assert len(_channel_rows(github_app_worker.POLICY_WITHDRAWN_AFTER_POST_CHANNEL)) == 1

    assert [r["status"] for r in _ppoll(policy_ledger)] == ["policy_withdrawn_after_post"]
    assert _conclusions(policy_ledger) == ["success", "cancelled"]
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _ppoll(policy_ledger) == []


def test_app_rotation_is_corrected_on_the_same_check_run(policy_ledger, monkeypatch):
    api = _FakeChecksAPI(policy_tests.HEAD)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    policy_ledger.transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.gh.open = [policy_tests._pr()]
    api.fail_posts = 1
    _ppoll(policy_ledger)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    _arm_after_commit(monkeypatch, _rotate_bearer(monkeypatch))

    results = _ppoll(policy_ledger)

    assert [r["status"] for r in results] == ["orphan_retired", "policy_withdrawn_after_post"]
    [success] = [p for p in api.posts if p.get("external_id")]
    assert [p["id"] for p in api.patches] == [success["id"]]
    assert success["conclusion"] == "cancelled"  # the same run, corrected


def test_publisher_holds_the_credential_fence_through_the_post(policy_ledger, monkeypatch):
    import fcntl

    from unified_agent import credential_fence

    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    transport = policy_ledger.transport
    original = transport.create_check_run
    seen = []

    def create(**kwargs):
        if kwargs.get("conclusion") == "success":
            path = credential_fence.authority_lock_path()
            with path.open("a+") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    seen.append("held")
                else:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    seen.append("free")
            seen.append(github_app.current_request_timeout())
        return original(**kwargs)

    monkeypatch.setattr(transport, "create_check_run", create)
    assert _ppoll(policy_ledger)[-1]["status"] == "policy_authorized"
    assert seen == ["held", github_app_worker.POLICY_POST_TIMEOUT_S]
    assert github_app_worker.POLICY_POST_TIMEOUT_S <= 10


def _fence_is_held() -> bool:
    import fcntl

    from unified_agent import credential_fence

    with credential_fence.authority_lock_path().open("a+") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def test_managed_token_lifecycle_takes_the_credential_fence(isolated_env):
    from unified_agent.tools import token_lifecycle

    assert _fence_is_held() is False
    with token_lifecycle._claim_lock():
        assert _fence_is_held() is True
    assert _fence_is_held() is False


def test_token_file_rotation_takes_the_credential_fence(isolated_env, tmp_path, monkeypatch):
    from unified_agent import bootstrap

    token_file = tmp_path / "tokens"
    token_file.write_text("someone:old-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    real_write = bootstrap._write_private
    held = []

    def write(path, body, mode):
        held.append(_fence_is_held())
        return real_write(path, body, mode)

    monkeypatch.setattr(bootstrap, "_write_private", write)
    bootstrap._rotate_in_token_file(token_file, {"someone": "new-token"})
    assert held == [True]
    assert "someone:new-token" in token_file.read_text(encoding="utf-8")


# --- review at 83f278fb (P3): App reconciliation needs our external id -------------------


def _app_runs(monkeypatch, runs):
    payloads = iter(
        [{"total_count": 1}, {"total_count": len(runs), "check_runs": runs}, {"total_count": 1}]
    )

    def open_request(request, timeout):
        data = json.dumps(next(payloads)).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return data

        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    return github_app.GitHubAppHttpTransport(lambda: "test-token")


_TITLE = "Policy-authorized: ref by someone"


@pytest.mark.parametrize("foreign_id", [None, "", "someone-elses-id"])
def test_app_lookup_ignores_a_foreign_run_with_our_title(monkeypatch, foreign_id):
    run = {
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "output": {"title": _TITLE},
        "app": {"id": 999, "slug": "another-app"},
    }
    if foreign_id is not None:
        run["external_id"] = foreign_id
    transport = _app_runs(monkeypatch, [run])
    assert _lookup(transport, external_id="ua-policy-ours") is False


def test_app_lookup_matches_only_our_external_id(monkeypatch):
    run = {
        "head_sha": "a" * 40,
        "external_id": "ua-policy-ours",
        "status": "completed",
        "conclusion": "success",
        "output": {"title": _TITLE},
    }
    transport = _app_runs(monkeypatch, [run])
    assert _lookup(transport, external_id="ua-policy-ours") is True


def test_app_lookup_without_an_external_id_is_refused():
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    with pytest.raises(ValueError, match="external_id"):
        _lookup(transport)


# --- review of the round-4 repair (P2): an in-flight withdrawal is never lost ------------


def _file_bearers(monkeypatch, tmp_path):
    """Real file-backed ``UA_AGENT_TOKENS`` and the real bearer resolver, synthetic tokens.

    Returns the token file and an out-of-band rotation of the authorizer's
    bearer (a direct file rewrite, as a hand edit would be: no hub fence).
    """
    import os
    import secrets

    from unified_agent import env_allowlist
    from unified_agent.audit import producer_sign

    tokens = {label: secrets.token_urlsafe(32) for label in (policy_tests.STEWARD, AUTHOR)}
    path = tmp_path / "bearers"

    def write(values):
        replacement = path.with_suffix(".replacement")
        replacement.write_text("\n".join(f"{label}:{value}" for label, value in values.items()))
        replacement.chmod(0o600)
        os.replace(replacement, path)

    write(tokens)
    monkeypatch.setenv("UA_AGENT_TOKENS", str(path))
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, tokens[AUTHOR])
    monkeypatch.setattr(config, "agent_tokens", _REAL_AGENT_TOKENS)
    monkeypatch.setattr(
        producer_sign, "resolve_agent_bearer_token", env_allowlist.resolve_agent_bearer_token
    )

    def rotate():
        tokens[policy_tests.STEWARD] = secrets.token_urlsafe(32)
        write(tokens)

    return path, rotate


AUTHOR = policy_tests.AUTHOR
_REAL_AGENT_TOKENS = config.agent_tokens


def _real_api(state, monkeypatch, kind):
    import secrets

    api = _FakeChecksAPI(policy_tests.HEAD) if kind == "app" else _FakeStatusAPI(policy_tests.HEAD)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    credential = secrets.token_urlsafe(24)
    cls = (
        github_app.GitHubAppHttpTransport if kind == "app" else github_app.GitHubUserTokenTransport
    )
    state.transport = cls(lambda: credential)
    return api


def _is_success_post(request) -> bool:
    if request.get_method() not in ("POST", "PATCH") or not request.data:
        return False
    body = json.loads(request.data)
    return body.get("conclusion") == "success" or body.get("state") == "success"


def _corrected(api, kind) -> bool:
    if kind == "app":
        # The one run the success created, corrected in place.
        return len(api.posts) == 1 and api.posts[0]["conclusion"] == "cancelled"
    return len(api.posts) == 2 and api.posts[-1]["state"] != "success"


@pytest.mark.parametrize("kind", ["app", "user"])
@pytest.mark.parametrize("failure", ["lost_response", "marker_write", "postcheck_error", "none"])
def test_success_withdrawn_in_flight_is_corrected_whatever_fails(
    policy_ledger, monkeypatch, tmp_path, kind, failure
):
    # Reviewer repro: the authorizer's bearer is rotated out of band while
    # GitHub is accepting the success POST. Then the response is lost, the
    # post-POST verification raises, or the withdrawal marker write fails.
    # Reconciliation must re-verify the uncertain publication and correct it,
    # never complete it as policy_authorized.
    import threading

    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    api = _real_api(policy_ledger, monkeypatch, kind)
    original_open = api.open
    posted = []

    def open_request(request, timeout):
        response = original_open(request, timeout)
        if _is_success_post(request) and not posted:
            posted.append(True)
            thread = threading.Thread(target=rotate)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert policy_tests._verified() is None
            if failure == "lost_response":
                raise TimeoutError("response lost after GitHub accepted the POST")
        return response

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    if failure == "marker_write":
        real_record = github_app_worker._record_after_post
        failed = []

        def record(*args):
            if not failed:
                failed.append(True)
                raise OSError("audit storage failure")
            return real_record(*args)

        monkeypatch.setattr(github_app_worker, "_record_after_post", record)
    if failure == "postcheck_error":
        real_reason = github_app_worker._withdrawal_reason
        failed = []

        def reason(intent):
            if posted and not failed:
                failed.append(True)
                raise OSError("verification read failure")
            return real_reason(intent)

        monkeypatch.setattr(github_app_worker, "_withdrawal_reason", reason)

    _ppoll(policy_ledger)
    _ppoll(policy_ledger)

    assert _corrected(api, kind), f"{kind}/{failure}: public success not corrected"
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert len(_channel_rows(github_app_worker.POLICY_WITHDRAWN_AFTER_POST_CHANNEL)) == 1
    assert _ppoll(policy_ledger) == []


@pytest.mark.parametrize("kind", ["app", "user"])
def test_bootstrap_rotation_waits_until_the_publication_finishes(
    policy_ledger, monkeypatch, tmp_path, kind
):
    import secrets
    import threading

    from unified_agent import bootstrap

    path, _rotate = _file_bearers(monkeypatch, tmp_path)
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    api = _real_api(policy_ledger, monkeypatch, kind)
    started, finished = threading.Event(), threading.Event()
    errors, threads = [], []

    def rotate():
        started.set()
        try:
            bootstrap._rotate_in_token_file(path, {policy_tests.STEWARD: secrets.token_urlsafe(32)})
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            finished.set()

    def arm():
        thread = threading.Thread(target=rotate)
        threads.append(thread)
        thread.start()
        assert started.wait(2)
        assert not finished.wait(0.2), "rotation crossed the publication fence"

    _arm_after_commit(monkeypatch, arm)
    results = _ppoll(policy_ledger)
    for thread in threads:
        thread.join(timeout=5)
    assert finished.is_set() and not errors
    assert results[-1]["status"] == "policy_authorized"
    assert len(api.posts) == 1
    assert policy_tests._verified() is None


def _managed_bearer_for_steward(monkeypatch, tmp_path):
    """The authorizer's bearer lives only in managed secrets (the file holds the worker's)."""
    import os
    import secrets

    from cryptography.fernet import Fernet
    from test_token_lifecycle import _approve_and_run

    from unified_agent.tools import token_lifecycle

    path, _ = _file_bearers(monkeypatch, tmp_path)
    path.write_text(f"{AUTHOR}:{os.environ[github_app_worker.TOKEN_VAR]}\n")
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    with policy_tests.as_identity(policy_tests.STEWARD):
        _approve_and_run(
            token_lifecycle.agent_token_issue, policy_tests.STEWARD, secrets.token_urlsafe(32)
        )


@pytest.mark.parametrize("kind", ["app", "user"])
@pytest.mark.parametrize("operation", ["overwrite", "delete"])
def test_managed_secret_bearer_change_waits_for_the_publication(
    policy_ledger, monkeypatch, tmp_path, kind, operation
):
    # Reviewer repro: an approved generic managed_secret_set of the
    # authorizer's agent-token secret (or a delete of it) is an effective
    # bearer change and must not complete inside the publisher's fence.
    import secrets
    import threading

    from unified_agent import managed_secrets
    from unified_agent.tools import profiles

    _managed_bearer_for_steward(monkeypatch, tmp_path)
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    policy_tests._authorize(policy_tests._task())
    assert policy_tests._verified() is not None
    api = _real_api(policy_ledger, monkeypatch, kind)
    name, value = f"agent-token:{policy_tests.STEWARD}", secrets.token_urlsafe(32)
    if operation == "overwrite":
        with policy_tests.as_identity(policy_tests.STEWARD):
            with pytest.raises(approvals.ApprovalPendingError) as pending:
                profiles.managed_secret_set(name, value, consumer="agent_token")
        with policy_tests.as_identity("Desktop_Person"):
            approvals.approval_resolve(pending.value.approval_id, "approve")

    completed, errors, threads, crossed = threading.Event(), [], [], []

    def mutate():
        try:
            with policy_tests.as_identity(policy_tests.STEWARD):
                if operation == "overwrite":
                    profiles.managed_secret_set(name, value, consumer="agent_token")
                else:
                    assert managed_secrets.delete_secret(name)
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            completed.set()

    def action():
        thread = threading.Thread(target=mutate)
        threads.append(thread)
        thread.start()
        crossed.append(completed.wait(1))

    _arm_after_commit(monkeypatch, action)
    results = _ppoll(policy_ledger)
    for thread in threads:
        thread.join(5)
    assert not errors
    assert crossed == [False], "managed bearer change completed inside the publication fence"
    assert results[-1]["status"] == "policy_authorized"
    assert len(api.posts) == 1
    assert policy_tests._verified() is None  # the change did land, after the POST


def test_only_bearer_secrets_take_the_credential_fence(isolated_env, monkeypatch):
    from cryptography.fernet import Fernet

    from unified_agent import managed_secrets

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    real_lock = managed_secrets._write_lock
    held = []

    def lock():
        held.append(_fence_is_held())
        return real_lock()

    monkeypatch.setattr(managed_secrets, "_write_lock", lock)
    import secrets

    managed_secrets.set_secret(
        "agent-token:someone", secrets.token_urlsafe(16), requested_by="x", consumer="agent_token"
    )
    managed_secrets.delete_secret("agent-token:someone")
    managed_secrets.set_secret("zulip-key", secrets.token_urlsafe(16), requested_by="x")
    assert held == [True, True, False]


def test_token_store_rollback_takes_the_credential_fence(isolated_env, tmp_path, monkeypatch):
    from unified_agent import bootstrap

    token_file = tmp_path / "tokens"
    token_file.write_text("someone:old-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    snapshots = [(token_file, token_file.read_bytes(), 0o600, None)]
    token_file.write_text("someone:new-token\n", encoding="utf-8")
    real_write = bootstrap._write_private
    held = []

    def write(path, body, mode):
        held.append(_fence_is_held())
        return real_write(path, body, mode)

    monkeypatch.setattr(bootstrap, "_write_private", write)
    bootstrap._restore_token_stores(snapshots)
    assert held == [True]
    assert token_file.read_text(encoding="utf-8") == "someone:old-token\n"


def test_credential_fence_is_reentrant_and_exclusive_across_threads(isolated_env):
    import threading

    from unified_agent import credential_fence

    entered = threading.Event()

    def other():
        with credential_fence.authority_fence():
            entered.set()

    with credential_fence.authority_fence():
        with credential_fence.authority_fence():  # same thread: no self-deadlock
            assert _fence_is_held()
        assert _fence_is_held()  # the outer hold survives the inner exit
        thread = threading.Thread(target=other)
        thread.start()
        assert not entered.wait(0.2)
    thread.join(5)
    assert entered.is_set()
    assert not _fence_is_held()


def test_lifecycle_lock_takes_the_fence_before_its_own_lock(isolated_env, monkeypatch):
    # One lock order everywhere (fence first), so a fenced managed-secret
    # write and a lifecycle operation cannot deadlock each other.
    import fcntl
    from contextlib import contextmanager

    from unified_agent import credential_fence
    from unified_agent.tools import token_lifecycle

    lifecycle_lock = config.data_home() / ".deployment_claim.lock"
    seen = []
    real_fence = credential_fence.authority_fence

    @contextmanager
    def fence():
        with lifecycle_lock.open("a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                seen.append("lifecycle lock already held")
            else:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                seen.append("fence first")
        with real_fence():
            yield

    monkeypatch.setattr(credential_fence, "authority_fence", fence)
    with token_lifecycle._claim_lock():
        assert _fence_is_held()
    assert seen == ["fence first"]


# --- review at 75a5f2e8 (P2): unfinished publications rotate; corrections come first ---


def _stuck_intents(count: int, task: int, first: int = 201) -> list[str]:
    """Commit ``count`` earlier intents whose heads no lookup can ever prove (suite cap)."""
    heads = []
    for number in range(first, first + count):
        sha = f"{number:040x}"
        authorization = policy_tests._authorize(task, head=sha, number=number)
        event = github_app.NormalizedPREvent(
            repo="example/project",
            number=number,
            event_type="closed",
            title="old orphan",
            url=f"https://github.com/example/project/pull/{number}",
            state="MERGED",
            head_sha=sha,
            base_branch="main",
            head_branch=policy_tests.BRANCH,
            author_login="author",
            merged=True,
        )
        with policy_tests.as_identity(AUTHOR):
            assert github_app_worker._commit_policy_intent(event, authorization) is not None
        heads.append(sha)
    return heads


def _starving_api(state, monkeypatch, *, failure_stage, rotate, lookups):
    """The real App transport; every earlier head exceeds the 1,000-suite window."""
    api = _real_api(state, monkeypatch, "app")
    original_open = api.open
    lost, correction_failed = [], []

    class Capped:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"total_count": 1001, "check_suites": []}).encode()

    def open_request(request, timeout):
        body = json.loads(request.data) if request.data else {}
        if (
            failure_stage == "correction_failure"
            and body.get("conclusion") == "cancelled"
            and not correction_failed
        ):
            correction_failed.append(True)
            raise TimeoutError("corrective PATCH not delivered")
        url = request.full_url
        if (
            request.get_method() == "GET"
            and "/check-suites?" in url
            and policy_tests.HEAD not in url
        ):
            lookups.append(url)
            return Capped()
        response = original_open(request, timeout)
        if _is_success_post(request) and not lost:
            lost.append(True)
            rotate()
            if failure_stage == "lost_response":
                raise TimeoutError("accepted success, rotated signer, lost response")
        return response

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    return api


@pytest.mark.parametrize("backlog", [4, 5, 9])
@pytest.mark.parametrize("failure_stage", ["lost_response", "correction_failure"])
def test_ambiguous_older_intents_cannot_starve_a_withdrawn_success(
    policy_ledger, monkeypatch, tmp_path, backlog, failure_stage
):
    # Reviewer repro: earlier publications whose lookup can never finish used
    # to hold every retry slot, so a later withdrawn success stayed public.
    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    task = policy_tests._task()
    _stuck_intents(backlog, task)
    policy_tests._authorize(task)
    lookups = []
    api = _starving_api(
        policy_ledger, monkeypatch, failure_stage=failure_stage, rotate=rotate, lookups=lookups
    )
    _ppoll(policy_ledger)
    assert len(api.posts) == 1 and api.posts[0]["conclusion"] == "success"
    for _ in range(3):
        before = len(lookups)
        _ppoll(policy_ledger)
        # Still bounded: at most the per-poll retry budget of capped lookups.
        assert len(lookups) - before <= github_app_worker.POLICY_PUBLICATION_RETRIES_PER_POLL
    assert _channel_rows(github_app.POLICY_AUTHORIZED_CHANNEL) == []
    assert _corrected(api, "app"), f"{backlog}/{failure_stage}: patches={len(api.patches)}"


def test_a_pending_correction_is_retried_within_its_round(policy_ledger, monkeypatch, tmp_path):
    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    _policy_orphan(policy_ledger, monkeypatch)
    _merge(policy_ledger)
    task = policy_tests._task()
    _stuck_intents(8, task)
    policy_tests._authorize(task)
    lookups = []
    api = _starving_api(
        policy_ledger,
        monkeypatch,
        failure_stage="correction_failure",
        rotate=rotate,
        lookups=lookups,
    )
    _ppoll(policy_ledger)
    assert len(_channel_rows(github_app_worker.POLICY_WITHDRAWN_AFTER_POST_CHANNEL)) == 1
    # The rotation withdrew every older intent's authorization too, so all
    # nine share the correction queue in commit order (5 slots a poll with no
    # plain retry open): it is corrected within its first full round.
    _ppoll(policy_ledger)
    _ppoll(policy_ledger)
    assert _corrected(api, "app")


def test_every_unfinished_publication_is_retried_within_bounded_polls(
    policy_ledger, monkeypatch, tmp_path
):
    _file_bearers(monkeypatch, tmp_path)
    task = policy_tests._task()
    heads = _stuck_intents(12, task)
    lookups = []
    _starving_api(
        policy_ledger, monkeypatch, failure_stage="none", rotate=lambda: None, lookups=lookups
    )
    per_poll = github_app_worker.POLICY_PUBLICATION_RETRIES_PER_POLL
    for _ in range(-(-len(heads) // per_poll)):
        _ppoll(policy_ledger)
    looked_up = {url.split("/commits/", 1)[1].split("/", 1)[0] for url in lookups}
    assert looked_up == set(heads)
    assert len(lookups) <= per_poll * -(-len(heads) // per_poll)


@pytest.mark.parametrize("content", ["not json", "[]", '{"repo": "other/repo", "attempted": {}}'])
def test_a_bad_retry_rotation_file_falls_back_to_oldest_first(
    policy_ledger, monkeypatch, tmp_path, content
):
    _file_bearers(monkeypatch, tmp_path)
    heads = _stuck_intents(7, policy_tests._task())
    path = github_app_worker.policy_retries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    lookups = []
    _starving_api(
        policy_ledger, monkeypatch, failure_stage="none", rotate=lambda: None, lookups=lookups
    )
    _ppoll(policy_ledger)
    looked_up = [url.split("/commits/", 1)[1].split("/", 1)[0] for url in lookups]
    per_poll = github_app_worker.POLICY_PUBLICATION_RETRIES_PER_POLL
    assert sorted(set(looked_up)) == heads[:per_poll]


# --- review at 2e87650c: retry rotation needs no clock and stays bounded -----------------


def _failing_retries(monkeypatch, corrections=frozenset()):
    """Every retry fails; ``corrections`` (PR numbers) carry an after-post marker."""
    seen = []

    def fail(_transport, intent, *, reconcile):
        assert reconcile is True
        seen.append(int(intent["number"]))
        raise github_app.ReconcileIncomplete("permanently unprovable lookup")

    real_marker = github_app_worker._after_post_marker
    monkeypatch.setattr(github_app_worker, "_publish_policy_intent", fail)
    monkeypatch.setattr(
        github_app_worker,
        "_after_post_marker",
        lambda intent: {} if int(intent["number"]) in corrections else real_marker(intent),
    )
    return seen


def _wall_clock(monkeypatch, start):
    from datetime import datetime as real_datetime

    state = SimpleNamespace(now=start)

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            state.now += 0.001
            return real_datetime.fromtimestamp(state.now, tz=tz)

    monkeypatch.setattr(github_app_worker, "datetime", Clock)
    return state


@pytest.mark.parametrize("rollback_seconds", [0, 3600])
def test_recovered_correction_is_served_after_a_clock_rollback(
    policy_ledger, monkeypatch, tmp_path, rollback_seconds
):
    # Reviewer repro: six public successes under withdrawn authorizations, all
    # corrective PATCHes failing; then one becomes writable and the worker's
    # wall clock steps back an hour. Ordering must not depend on the clock.
    from datetime import UTC, datetime

    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(6, policy_tests._task())
    intents = github_app_worker._unfinished_policy_intents()
    api = _FakeChecksAPI(policy_tests.HEAD)
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.transport = transport
    policy_ledger.gh.open = []
    for intent in intents:
        run_id = transport.create_check_run(
            repo=intent["repo"],
            head_sha=intent["head_sha"],
            name=github_app.CHECK_RUN_NAME,
            status="completed",
            conclusion="success",
            description=intent["description"],
            external_id=github_app_worker._intent_external_id(intent),
        )
        github_app_worker._record_after_post(intent, "out-of-band withdrawal", run_id)
    rotate()
    assert all(github_app_worker._withdrawal_reason(i) is not None for i in intents)
    clock = _wall_clock(monkeypatch, datetime.now(UTC).timestamp())
    allowed, patched = set(), []

    def open_request(request, timeout):
        if request.get_method() == "PATCH":
            run_id = int(request.full_url.rsplit("/", 1)[1])
            patched.append(run_id)
            if run_id not in allowed:
                raise TimeoutError("corrective PATCH failed")
        return api.open(request, timeout)

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    _ppoll(policy_ledger)
    clock.now += 120
    _ppoll(policy_ledger)
    assert set(patched) == set(range(1, 7))
    # The correction served last becomes writable: the worst case for LRU.
    target = int(patched[-1])
    allowed.add(target)
    clock.now -= rollback_seconds
    for _ in range(6):
        _ppoll(policy_ledger)
        clock.now += 120
    assert api.posts[target - 1]["conclusion"] == "cancelled", f"retries={patched}"


def test_every_failing_intent_rotates_with_corrections_first(policy_ledger, monkeypatch, tmp_path):
    # 6 corrections and 17 plain retries, all failing forever. Corrections
    # round-robin in 4 slots; plain retries keep 1 slot every poll.
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(23, policy_tests._task())
    seen = _failing_retries(monkeypatch, corrections=frozenset(range(201, 207)))
    per_poll = github_app_worker.POLICY_PUBLICATION_RETRIES_PER_POLL
    polls = []
    for _ in range(2 * 17):
        before = len(seen)
        github_app_worker._finish_policy_publications(None)
        polls.append(seen[before:])
        assert len(polls[-1]) == per_poll
        assert sum(n >= 207 for n in polls[-1]) >= 1  # a plain retry every poll
    assert polls[0] == [201, 202, 203, 204, 207]
    # The lead flips each poll: the plain retry goes first on the second.
    assert polls[1] == [208, 205, 206, 201, 202]
    assert all(seen.count(n) >= 2 for n in range(201, 224))


def test_without_plain_retries_corrections_use_every_slot(policy_ledger, monkeypatch, tmp_path):
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(6, policy_tests._task())
    seen = _failing_retries(monkeypatch, corrections=frozenset(range(201, 207)))
    github_app_worker._finish_policy_publications(None)
    github_app_worker._finish_policy_publications(None)
    assert seen == [201, 202, 203, 204, 205, 206, 201, 202, 203, 204]


@pytest.mark.parametrize("limit", [1000, 400])
def test_a_large_backlog_never_resets_rotation(policy_ledger, monkeypatch, tmp_path, limit):
    # Reviewer repro (P3): a retry map larger than the reader's byte limit was
    # discarded, so the tail of a large backlog was never attempted.
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(20, policy_tests._task())
    seen = _failing_retries(monkeypatch)
    for name in ("POLICY_RETRIES_MAX_BYTES", "ORPHAN_CHECKS_MAX_BYTES"):
        monkeypatch.setattr(github_app_worker, name, limit, raising=False)
    for _ in range(4):
        github_app_worker._finish_policy_publications(None)
    assert set(seen) == set(range(201, 221))
    assert github_app_worker.policy_retries_path().stat().st_size <= limit


def test_retry_state_size_does_not_grow_with_the_backlog(policy_ledger, monkeypatch, tmp_path):
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(30, policy_tests._task())
    _failing_retries(monkeypatch)
    github_app_worker._finish_policy_publications(None)
    assert github_app_worker.policy_retries_path().stat().st_size < 256


@pytest.mark.parametrize(
    "position",
    [
        {"cursor": "not-a-ulid", "round_end": "not-a-ulid"},
        {"cursor": 7, "round_end": 7},
        {"cursor": None, "round_end": None},
        {"cursor": [], "round_end": {}},
        {"cursor": "01ARZ3NDEKTSV4RRFFQ69G5FAI", "round_end": "0" * 25},
        "not a mapping",
    ],
)
def test_a_bad_retry_position_starts_a_fresh_round(policy_ledger, monkeypatch, tmp_path, position):
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(7, policy_tests._task())
    path = github_app_worker.policy_retries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repo": "example/project", "corrections": position, "plain": position}),
        encoding="utf-8",
    )
    seen = _failing_retries(monkeypatch)
    github_app_worker._finish_policy_publications(None)
    assert seen == [201, 202, 203, 204, 205]


def test_an_oversized_retry_state_is_not_written(policy_ledger, monkeypatch):
    github_app_worker._save_retry_state("example/project", github_app_worker._empty_retry_state())
    before = github_app_worker.policy_retries_path().read_bytes()
    github_app_worker._save_retry_state("x" * 5000, github_app_worker._empty_retry_state())
    assert github_app_worker.policy_retries_path().read_bytes() == before


# --- review at 32c0da75: rounds stop arrivals overtaking; progress is per attempt -------


def _correction_rows(monkeypatch, tmp_path, count, corrections=()):
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(count, policy_tests._task())
    rows = github_app_worker._unfinished_policy_intent_rows()
    for _ulid, intent in rows:
        if intent["number"] in corrections:
            github_app_worker._record_after_post(
                intent, "out-of-band withdrawal", str(intent["number"])
            )
    return rows


@pytest.mark.parametrize("fatal", [False, True])
def test_a_crashing_first_correction_cannot_starve_the_others(
    policy_ledger, monkeypatch, tmp_path, fatal
):
    # Reviewer repro: the whole batch's cursor was saved before its first
    # attempt, so a process that died on correction 201 every poll made the
    # next poll wrap straight back to 201; correction 202 never ran.
    rows = _correction_rows(monkeypatch, tmp_path, 5, range(201, 205))
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, "nobody")
    assert all(github_app_worker._withdrawal_reason(i) is not None for _u, i in rows)
    api = _FakeChecksAPI(policy_tests.HEAD)
    policy_ledger.transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.gh.open = []
    for _ulid, intent in rows[:4]:
        api.posts.append({"id": intent["number"], "conclusion": "success"})
    patched = []

    def open_request(request, timeout):
        if request.get_method() == "PATCH":
            number = int(request.full_url.rsplit("/", 1)[1])
            patched.append(number)
            if number == 201:
                if fatal:
                    raise SystemExit("process dies in the first correction")
                raise TimeoutError("first correction fails")
        return api.open(request, timeout)

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    for _ in range(8):
        try:
            _ppoll(policy_ledger)
        except SystemExit:
            assert fatal
    assert api.posts[1]["conclusion"] == "cancelled", f"fatal={fatal}, requests={patched}"


def test_real_process_restarts_do_not_skip_unattempted_corrections(
    policy_ledger, monkeypatch, tmp_path
):
    import subprocess
    import sys

    _correction_rows(monkeypatch, tmp_path, 5, range(201, 205))
    attempt_log = tmp_path / "attempts.jsonl"
    code = """import json, os, sys
from unified_agent import github_app_worker as w
class Transport:
    def update_check_run(self, **kw):
        with open(sys.argv[1], 'a') as f:
            f.write(json.dumps(kw['check_run_id']) + '\\n')
        if kw['check_run_id'] == '201':
            os._exit(23)
with w._poll_lock():
    w._finish_policy_publications(Transport())
"""
    import os
    from pathlib import Path

    # The same source tree this process imports, not whatever the venv maps.
    src = str(Path(github_app_worker.__file__).resolve().parents[1])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])),
    }
    for _ in range(4):
        proc = subprocess.run(
            [sys.executable, "-c", code, str(attempt_log)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 23, proc.stderr
    attempted = [json.loads(line) for line in attempt_log.read_text().splitlines()]
    assert "202" in attempted, f"attempts={attempted}"


@pytest.mark.parametrize("arrivals", [False, True])
def test_new_arrivals_cannot_overtake_an_older_withdrawn_success(
    policy_ledger, monkeypatch, tmp_path, arrivals
):
    # Reviewer repro: with four failing corrections and at most two plain
    # retries, one newer plain retry arriving (and finishing) every poll took
    # the plain slot each time, so the older uncertain success was never
    # looked up again and stayed public under a withdrawn authorization.
    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    task = policy_tests._task()
    _stuck_intents(5, task)
    rows = github_app_worker._unfinished_policy_intent_rows()
    target = rows[-1][1]
    api = _FakeChecksAPI(target["head_sha"])
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.transport = transport
    policy_ledger.gh.open = []
    for _ulid, intent in rows:
        run_id = transport.create_check_run(
            repo=intent["repo"],
            head_sha=intent["head_sha"],
            name=github_app.CHECK_RUN_NAME,
            status="completed",
            conclusion="success",
            description=intent["description"],
            external_id=github_app_worker._intent_external_id(intent),
        )
        if intent is not target:
            github_app_worker._record_after_post(intent, "out-of-band withdrawal", run_id)
    target_reads, fail_new, failing = [], set(), [True]

    class Empty:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"total_count":0,"check_suites":[],"check_runs":[]}'

    def open_request(request, timeout):
        method, url = request.get_method(), request.full_url
        if method == "POST" and request.data:
            head = json.loads(request.data).get("head_sha")
            if head in fail_new:
                fail_new.remove(head)
                raise TimeoutError("initial POST failed before delivery")
        if method == "PATCH" and int(url.rsplit("/", 1)[1]) <= 4:
            raise TimeoutError("four permanently failing corrections")
        if method == "GET" and "/commits/" in url:
            if target["head_sha"] in url:
                target_reads.append(url)
                if failing[0]:
                    raise TimeoutError("lost lookup response")
            else:
                return Empty()
        return api.open(request, timeout)

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    _ppoll(policy_ledger)
    assert len(target_reads) == 1
    assert api.posts[4]["conclusion"] == "success"
    rotate()
    assert github_app_worker._withdrawal_reason(target) is not None
    failing[0] = False
    for step in range(8):
        if arrivals:
            _stuck_intents(1, task, first=206 + step)
            new = github_app_worker._unfinished_policy_intent_rows()[-1][1]
            fail_new.add(new["head_sha"])
            with policy_tests.as_identity(AUTHOR), pytest.raises(TimeoutError):
                github_app_worker._publish_policy_intent(transport, new, reconcile=False)
        assert len(github_app_worker._unfinished_policy_intent_rows()) <= 6
        _ppoll(policy_ledger)
    assert api.posts[4]["conclusion"] == "cancelled", f"target lookups={len(target_reads)}"
    assert not github_app_worker._intent_open(target)


@pytest.mark.parametrize("which", ["finished", "ahead", "before", "between"])
@pytest.mark.parametrize("end", ["newest", "cursor"])
def test_a_stale_retry_position_still_rotates_every_open_publication(
    policy_ledger, monkeypatch, tmp_path, which, end
):
    # The saved position may name a publication that has since finished, or
    # (after a database restore) one ahead of or before every open row.
    rows = _correction_rows(monkeypatch, tmp_path, 9)
    cursor = rows[4][0]
    if which == "finished":
        github_app_worker._withdraw_policy_intent(rows[4][1], "closed")
    elif which == "ahead":
        cursor = "7" + "Z" * 25
    elif which == "before":
        cursor = "0" * 26
    else:
        cursor = rows[3][0]
        github_app_worker._withdraw_policy_intent(rows[3][1], "closed")
    state = github_app_worker._empty_retry_state()
    state["plain"] = {"cursor": cursor, "round_end": rows[-1][0] if end == "newest" else cursor}
    github_app_worker._save_retry_state("example/project", state)
    expected = {i["number"] for _u, i in github_app_worker._unfinished_policy_intent_rows()}
    seen = _failing_retries(monkeypatch)
    for _ in range(4):
        github_app_worker._finish_policy_publications(None)
    assert set(seen) == expected
    assert all(seen.count(n) >= 2 for n in expected)


# --- review at c60257f5: a crashing queue cannot hold the other queue's turn ------------


@pytest.mark.parametrize("fatal", [False, True])
def test_a_crashing_correction_cannot_starve_an_uncertain_withdrawn_success(
    policy_ledger, monkeypatch, tmp_path, fatal
):
    # Reviewer repro: one correction whose PATCH kills the process every time,
    # and one public success whose response was lost (no marker) under an
    # authorization since withdrawn. Every poll used to start with the
    # correction, so the uncertain success was never looked up again.
    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(2, policy_tests._task())
    rows = github_app_worker._unfinished_policy_intent_rows()
    target = rows[1][1]
    api = _FakeChecksAPI(target["head_sha"])
    transport = github_app.GitHubAppHttpTransport(lambda: "test-token")
    policy_ledger.transport = transport
    policy_ledger.gh.open = []
    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", api.open)
    for index, (_ulid, intent) in enumerate(rows):
        run_id = transport.create_check_run(
            repo=intent["repo"],
            head_sha=intent["head_sha"],
            name=github_app.CHECK_RUN_NAME,
            status="completed",
            conclusion="success",
            description=intent["description"],
            external_id=github_app_worker._intent_external_id(intent),
        )
        if index == 0:
            github_app_worker._record_after_post(intent, "out-of-band withdrawal", run_id)
    rotate()
    assert github_app_worker._withdrawal_reason(target) is not None
    patched = []

    def open_request(request, timeout):
        if request.get_method() == "PATCH":
            run_id = int(request.full_url.rsplit("/", 1)[-1])
            patched.append(run_id)
            if run_id == 1:
                if fatal:
                    raise SystemExit("the process dies in this correction")
                raise TimeoutError("this correction keeps failing")
        return api.open(request, timeout)

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    for _ in range(8):
        try:
            _ppoll(policy_ledger)
        except SystemExit:
            assert fatal
    assert (
        github_app_worker._record(github_app.POLICY_AUTHORIZED_CHANNEL, target["event_id"]) is None
    )
    assert api.posts[1]["conclusion"] == "cancelled", f"fatal={fatal}, PATCHes={patched}"


def _child_env():
    import os
    from pathlib import Path

    # The same source tree this process imports, not whatever the venv maps.
    src = str(Path(github_app_worker.__file__).resolve().parents[1])
    path = os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))
    return {**os.environ, "PYTHONPATH": path}, src


def test_real_restarts_with_one_crashing_correction_still_reach_plain_work(
    policy_ledger, monkeypatch, tmp_path
):
    import subprocess
    import sys

    _correction_rows(monkeypatch, tmp_path, 2, [201])
    log = tmp_path / "attempts.log"
    env, src = _child_env()
    code = """import os, sys
from unified_agent import github_app_worker as w
assert w.__file__.startswith(sys.argv[2]), w.__file__
class Transport:
    def update_check_run(self, **kw):
        with open(sys.argv[1], 'a') as f:
            f.write('correction:' + str(kw['check_run_id']) + '\\n')
        os._exit(23)
    def locate_completed_check(self, **kw):
        with open(sys.argv[1], 'a') as f:
            f.write('plain lookup\\n')
        raise TimeoutError('lookup failed')
with w._poll_lock():
    w._finish_policy_publications(Transport())
"""
    for _ in range(4):
        proc = subprocess.run(
            [sys.executable, "-c", code, str(log), src],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 23, proc.stderr
    attempts = log.read_text().splitlines()
    assert "plain lookup" in attempts, attempts


def test_the_leading_queue_flips_before_the_first_attempt(policy_ledger, monkeypatch, tmp_path):
    _correction_rows(monkeypatch, tmp_path, 3, [201])
    leads = []

    def crash(_transport, intent, *, reconcile):
        leads.append(int(intent["number"]))
        raise SystemExit("dies on the first attempt")

    monkeypatch.setattr(github_app_worker, "_publish_policy_intent", crash)
    for _ in range(4):
        with pytest.raises(SystemExit):
            github_app_worker._finish_policy_publications(None)
    # Correction first, then a plain retry first, alternating across crashes.
    assert leads == [201, 202, 201, 203]


def test_a_withdrawn_unmarked_publication_takes_a_priority_slot(
    policy_ledger, monkeypatch, tmp_path
):
    # An uncertain publication whose authorization no longer verifies may be
    # a public withdrawn success: it is retried with the corrections, ahead
    # of older plain retries, even before any after-post marker exists.
    _correction_rows(monkeypatch, tmp_path, 9)
    seen = _failing_retries(monkeypatch)
    real_reason = github_app_worker._withdrawal_reason
    monkeypatch.setattr(
        github_app_worker,
        "_withdrawal_reason",
        lambda intent: "withdrawn" if int(intent["number"]) == 209 else real_reason(intent),
    )
    github_app_worker._finish_policy_publications(None)
    assert seen[0] == 209
    assert seen[1:] == [201, 202, 203, 204]


# --- review at 17ef60f6: classification is O(ledger) per poll; cadence; crash bound ------


def _count_chain_scans(monkeypatch):
    from unified_agent import events

    scans = SimpleNamespace(calls=0, entries=0)
    real_verify = events.verify_event_chain

    def verify(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        scans.calls += 1
        scans.entries += int(result.get("total_verified") or 0)
        return result

    monkeypatch.setattr(events, "verify_event_chain", verify)
    return scans


def _ledger_size() -> int:
    conn = storage.connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0])
    finally:
        conn.close()


def test_classification_verifies_the_ledger_once_per_poll(policy_ledger, monkeypatch, tmp_path):
    # Reviewer repro: queue placement re-verified the whole hash chain once per
    # unmarked intent before any retry ran (40 intents x the ledger).
    import time

    from unified_agent import events

    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(40, policy_tests._task())
    for index in range(2000):
        events.record_event("test.filler", payload={"n": index}, originator="filler", notify=False)
    seen = _failing_retries(monkeypatch)
    scans = _count_chain_scans(monkeypatch)
    started = time.perf_counter()
    github_app_worker._finish_policy_publications(None)
    elapsed = time.perf_counter() - started
    assert len(seen) == github_app_worker.POLICY_PUBLICATION_RETRIES_PER_POLL
    assert scans.calls == 1, f"{scans.calls} chain scans, {scans.entries} entries, {elapsed:.2f}s"
    assert scans.entries <= _ledger_size()


def test_classification_still_sees_a_real_withdrawal(policy_ledger, monkeypatch, tmp_path):
    # The shared chain check changes cost, not the answer: a rotated bearer
    # still moves the intents it signed into the correction queue.
    _path, rotate = _file_bearers(monkeypatch, tmp_path)
    rows = _correction_rows(monkeypatch, tmp_path, 3)
    state = github_app_worker._empty_retry_state()
    assert [q for q, _r, _e in github_app_worker._retry_order(rows, state)] == ["plain"] * 3
    rotate()
    state = github_app_worker._empty_retry_state()
    assert [q for q, _r, _e in github_app_worker._retry_order(rows, state)] == ["corrections"] * 3


def test_the_chain_check_is_shared_only_inside_classification(policy_ledger, monkeypatch, tmp_path):
    # The pre-POST re-verification stays authoritative: outside the
    # classification scope every call verifies the chain afresh.
    _file_bearers(monkeypatch, tmp_path)
    _stuck_intents(2, policy_tests._task())
    intents = github_app_worker._unfinished_policy_intents()
    scans = _count_chain_scans(monkeypatch)
    for intent in intents:
        assert github_app_worker._withdrawal_reason(intent) is None
    assert scans.calls == 2
    with github_app.shared_chain_verification():
        for intent in intents:
            assert github_app_worker._withdrawal_reason(intent) is None
    assert scans.calls == 3
    github_app_worker._withdrawal_reason(intents[0])
    assert scans.calls == 4


def test_a_pending_correction_keeps_the_active_cadence(policy_ledger, monkeypatch, tmp_path):
    # Reviewer repro (P3): once the open PRs drained, a remaining public
    # withdrawn success fell back to the 30-minute idle cadence.
    rows = _correction_rows(monkeypatch, tmp_path, 2, [201])
    assert github_app_worker.ledger_in_flight() is True
    github_app_worker._withdraw_policy_intent(rows[0][1], "corrected")
    assert github_app_worker.ledger_in_flight() is False  # plain retries do not


def test_crashing_corrections_still_progress_one_per_leading_turn(
    policy_ledger, monkeypatch, tmp_path
):
    # The documented crash bound: when every correction kills the process,
    # each poll that leads with corrections attempts one, so all eight are
    # reached in eight polls (not ceil(8 / 4)), and the plain retry keeps
    # its turn on the other polls.
    _correction_rows(monkeypatch, tmp_path, 9, range(201, 209))
    batches = []

    def attempt(_transport, intent, *, reconcile):
        batches[-1].append(int(intent["number"]))
        if int(intent["number"]) < 209:
            raise SystemExit("this correction kills the process")
        raise github_app.ReconcileIncomplete("plain lookup fails")

    monkeypatch.setattr(github_app_worker, "_publish_policy_intent", attempt)
    for _ in range(8):
        batches.append([])
        with pytest.raises(SystemExit):
            github_app_worker._finish_policy_publications(None)
    assert batches == [[201], [209, 202], [203], [209, 204], [205], [209, 206], [207], [209, 208]]


# --- review at 4f65f1ba: the shared chain check fails closed, once, and stays scoped ----


def _tamper_the_ledger() -> None:
    from unified_agent import events

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn, "test tamper"):
            conn.execute(
                "UPDATE event_log SET created_at='tampered' "
                "WHERE ROWID=(SELECT MIN(ROWID) FROM event_log)"
            )
        conn.commit()
    finally:
        conn.close()


def test_a_raising_chain_check_is_not_repeated_per_intent(policy_ledger, monkeypatch, tmp_path):
    # Reviewer repro: a verifier exception left the memo empty, so each of 40
    # intents rescanned the whole (damaged) ledger before any retry ran.
    import sqlite3

    from unified_agent import events

    rows = _correction_rows(monkeypatch, tmp_path, 40)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for index in range(2000):
            last, _created, _row = events.record_event_once_on_connection(
                conn, "test.filler", payload={"n": index}, originator="filler"
            )
        conn.commit()
        with events.ledger_maintenance(conn, "late column damage"):
            # SQLite accepts a BLOB in a TEXT column; hashing it raises.
            conn.execute(
                "UPDATE event_log SET originator=? WHERE ulid=?", (sqlite3.Binary(b"x"), last)
            )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(TypeError):
        events.verify_event_chain()
    raised = []
    real_verify = events.verify_event_chain

    def verify(*args, **kwargs):
        try:
            return real_verify(*args, **kwargs)
        except Exception as exc:
            raised.append(type(exc).__name__)
            raise

    monkeypatch.setattr(events, "verify_event_chain", verify)
    order = github_app_worker._retry_order(rows, github_app_worker._empty_retry_state())
    # Fails closed: every intent is treated as unverifiable (correction queue).
    assert [queue for queue, _row, _end in order] == ["corrections"] * 5
    assert raised == ["TypeError"], f"{len(raised)} failing full-chain scans"


@pytest.mark.parametrize("mode", ["task", "to_thread", "plain_thread", "inner_thread"])
def test_child_work_cannot_reuse_the_classification_chain_check(
    policy_ledger, monkeypatch, tmp_path, mode
):
    # Reviewer repro (P3): an asyncio task or to_thread worker started inside
    # the scope inherited the memo, and after the scope closed it posted a
    # success against a chain tampered in the meantime.
    import asyncio
    import threading

    rows = _correction_rows(monkeypatch, tmp_path, 1)
    intent = rows[0][1]
    posts = []

    class Transport:
        def locate_completed_check(self, **_kwargs):
            return None

        def create_check_run(self, **kwargs):
            posts.append(kwargs["conclusion"])
            return "123"

    def publish():
        return github_app_worker._publish_policy_intent(Transport(), intent, reconcile=True)

    async def exercise():
        async def child():
            if mode == "to_thread":
                return await asyncio.to_thread(publish)
            return publish()

        gate, results, thread, task = threading.Event(), [], None, None
        with github_app.shared_chain_verification():
            assert github_app_worker._withdrawal_reason(intent) is None
            if mode in ("plain_thread", "inner_thread"):

                def run():
                    if mode == "plain_thread":
                        assert gate.wait(10)
                    else:
                        _tamper_the_ledger()  # tampered while the scope is still open
                    results.append(publish())

                thread = threading.Thread(target=run)
                thread.start()
                if mode == "inner_thread":
                    thread.join(10)
            else:
                task = asyncio.create_task(child())
        if mode != "inner_thread":
            _tamper_the_ledger()
        from unified_agent import events

        assert events.verify_event_chain()["status"] == "tampered"
        if thread is not None:
            gate.set()
            thread.join(10)
            assert not thread.is_alive()
            return results[0]
        return await task

    result = asyncio.run(exercise())
    assert posts == [], f"{mode}: a child reused the classification chain check"
    assert result["status"] == "policy_withdrawn"


def test_the_shared_check_closes_with_its_scope(policy_ledger, monkeypatch, tmp_path):
    rows = _correction_rows(monkeypatch, tmp_path, 1)
    intent = rows[0][1]
    scans = _count_chain_scans(monkeypatch)
    with github_app.shared_chain_verification():
        assert github_app_worker._withdrawal_reason(intent) is None
        with github_app.shared_chain_verification():  # nested: its own scope
            assert github_app_worker._withdrawal_reason(intent) is None
        assert github_app_worker._withdrawal_reason(intent) is None
    assert scans.calls == 2
    with pytest.raises(RuntimeError), github_app.shared_chain_verification():
        raise RuntimeError("scope body fails")
    _tamper_the_ledger()
    assert github_app_worker._withdrawal_reason(intent) is not None
