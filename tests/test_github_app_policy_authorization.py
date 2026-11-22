"""Policy-authorized Approval Ledger outcome (task #1407).

A merge covered by a recorded standing authorization is its own evidence
class: distinct from a human approval, never a substitute for one on
red-tier scope, and bound to the exact pull-request head.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from unified_agent import config, events, github_app, github_app_worker, storage
from unified_agent.audit import evidence_report, producer_sign
from unified_agent.policy import PolicyConfig, PolicyRule
from unified_agent.tools import approvals

STEWARD = "Desktop_Agent1"
AUTHOR = "Desktop_Agent2"
OTHER = "Desktop_Agent4"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
POLICY_REF = "deploy-steward-2026-10-01"
BEARERS = {
    "steward-bearer": STEWARD,
    "worker-bearer": AUTHOR,
    "other-bearer": OTHER,
}


@pytest.fixture(autouse=True)
def ledger_env(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, STEWARD)
    monkeypatch.delenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, raising=False)
    monkeypatch.setattr(config, "agent_tokens", lambda *a, **k: dict(BEARERS))
    by_label = {label: bearer for bearer, label in BEARERS.items()}
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", by_label.get)
    storage.init_db()


@contextmanager
def as_identity(label: str | None):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


# The head branch of the pull request in these tests (see _pr below); a task
# is bound to the PR whose head branch is the task's own branch.
BRANCH = "task/ship"


def _task(claimed_by: str | None = AUTHOR, branch: str | None = BRANCH) -> int:
    now = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, status, priority, claimed_by, branch, created_at, "
            "updated_at) VALUES ('ship a fix', 'doing', 'med', ?, ?, ?, ?)",
            (claimed_by, branch, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _authorize(task_id: int, *, head: str = HEAD, identity: str = STEWARD, number: int = 123):
    with as_identity(identity):
        return github_app.record_policy_authorization(
            repo="example/project",
            number=number,
            head_sha=head,
            policy_ref=POLICY_REF,
            task_id=task_id,
        )


def _verified(head: str = HEAD, number: int = 123):
    return github_app.verified_policy_authorization(
        repo="example/project", number=number, head_sha=head
    )


# --- recording: who may create a policy authorization ------------------------


def test_record_requires_a_verified_identity():
    task_id = _task()
    with as_identity(None), pytest.raises(PermissionError, match="verified"):
        github_app.record_policy_authorization(
            repo="example/project",
            number=123,
            head_sha=HEAD,
            policy_ref=POLICY_REF,
            task_id=task_id,
        )


def test_unconfigured_creator_is_refused():
    task_id = _task()
    with pytest.raises(PermissionError, match="not a configured policy authorizer"):
        _authorize(task_id, identity=OTHER)
    assert _verified() is None


def test_no_authorizers_configured_refuses_everyone(monkeypatch):
    monkeypatch.delenv(github_app.POLICY_AUTHORIZERS_VAR)
    task_id = _task()
    with pytest.raises(PermissionError, match="not a configured policy authorizer"):
        _authorize(task_id)


def test_human_operator_label_cannot_be_a_policy_authorizer(monkeypatch):
    operator = config.operator_label()
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, f"{STEWARD},{operator}")
    assert operator not in github_app.policy_authorizers()
    assert STEWARD in github_app.policy_authorizers()


def test_self_authorization_is_refused_by_default():
    task_id = _task(claimed_by=STEWARD)
    with pytest.raises(PermissionError, match="its own"):
        _authorize(task_id)


def test_self_authorization_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, "1")
    task_id = _task(claimed_by=STEWARD)
    record = _authorize(task_id)
    assert record["authorized_by"] == STEWARD
    assert _verified() is not None


def test_closing_the_task_later_does_not_rewrite_the_author():
    task_id = _task()
    _authorize(task_id)
    # A done transition stamps the closer into claimed_by; the signed record
    # keeps the claimant from authorization time.
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET status='done', claimed_by=? WHERE id=?", (STEWARD, task_id))
        conn.commit()
    finally:
        conn.close()
    assert _verified() is not None


def test_turning_self_authorization_off_withdraws_self_records(monkeypatch):
    monkeypatch.setenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, "1")
    task_id = _task(claimed_by=STEWARD)
    _authorize(task_id)
    monkeypatch.delenv(github_app.POLICY_SELF_AUTHORIZATION_VAR)
    assert _verified() is None


def test_tampered_claimant_fails_the_signature():
    task_id = _task()
    _authorize(task_id)
    stored = events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL])
    forged = {**stored[0]["payload"], "task_claimant": OTHER, "head_sha": OLD_HEAD}
    events.record_event(github_app.POLICY_AUTHORIZATION_CHANNEL, payload=forged, originator=STEWARD)
    assert _verified(head=OLD_HEAD) is None


def test_unclaimed_task_cannot_establish_separation_of_duties():
    task_id = _task(claimed_by=None)
    with pytest.raises(PermissionError, match="claimant"):
        _authorize(task_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "abc123"),
        ("head_sha", "Z" * 40),
        ("policy_ref", "has spaces"),
        ("policy_ref", ""),
        ("repo", "not-a-repo"),
        ("number", 0),
        ("task_id", 0),
    ],
)
def test_record_validates_its_binding(field, value):
    task_id = _task()
    kwargs = {
        "repo": "example/project",
        "number": 123,
        "head_sha": HEAD,
        "policy_ref": POLICY_REF,
        "task_id": task_id,
    }
    kwargs[field] = value
    with as_identity(STEWARD), pytest.raises(ValueError):
        github_app.record_policy_authorization(**kwargs)


def test_same_head_and_policy_for_a_second_task_is_recorded_for_that_task():
    """Review: the dedupe key once omitted the task, so task 2 got task 1's row."""
    first = _task()
    second = _task()
    one = _authorize(first)
    two = _authorize(second)
    assert two["authorization_id"] != one["authorization_id"]
    stored = {
        row["ulid"]: row["payload"]["task_id"]
        for row in events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL])
    }
    assert stored[two["authorization_id"]] == second
    assert _verified()["task_id"] == second
    # A true repeat is still deduplicated to the persisted row.
    assert _authorize(second)["authorization_id"] == two["authorization_id"]


def test_task_without_a_branch_cannot_be_bound_to_a_pull_request():
    task_id = _task(branch=None)
    with pytest.raises(PermissionError, match="branch"):
        _authorize(task_id)


def test_record_is_signed_bound_to_the_exact_head_and_never_red_tier():
    task_id = _task()
    record = _authorize(task_id)
    assert record["evidence_class"] == github_app.EVIDENCE_CLASS_POLICY
    assert record["head_sha"] == HEAD
    assert record["red_tier"] is False
    verified = _verified()
    assert verified is not None
    assert verified["authorization_id"] == record["authorization_id"]
    assert verified["policy_ref"] == POLICY_REF
    assert verified["authorized_by"] == STEWARD
    # Stale head: an authorization for one head never covers another.
    assert _verified(head=OLD_HEAD) is None
    assert _verified(number=124) is None


def test_removing_the_authorizer_from_config_invalidates_the_record(monkeypatch):
    task_id = _task()
    _authorize(task_id)
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, OTHER)
    assert _verified() is None


def test_rotated_bearer_fails_the_signature_closed(monkeypatch):
    task_id = _task()
    _authorize(task_id)
    monkeypatch.setattr(
        producer_sign,
        "resolve_agent_bearer_token",
        lambda label: "rotated-bearer" if label == STEWARD else None,
    )
    assert _verified() is None


def test_forged_row_with_another_originator_is_ignored():
    task_id = _task()
    record = _authorize(task_id)
    stored = events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL])
    payload = dict(stored[0]["payload"])
    assert payload["signature"]
    events.record_event(
        github_app.POLICY_AUTHORIZATION_CHANNEL,
        payload={**payload, "head_sha": OLD_HEAD},
        originator=STEWARD,
    )
    # The signature covers the head, so re-pointing a copy at another head fails.
    assert _verified(head=OLD_HEAD) is None
    events.record_event(
        github_app.POLICY_AUTHORIZATION_CHANNEL,
        payload=payload,
        originator=OTHER,
    )
    assert _verified()["authorization_id"] == record["authorization_id"]


def test_policy_description_names_the_policy_and_authorizer_not_a_human():
    text = github_app.policy_authorized_description(
        {"policy_ref": POLICY_REF, "authorized_by": STEWARD}
    )
    assert text == f"Policy-authorized: {POLICY_REF} by {STEWARD}"
    assert "human" not in text.lower()
    assert len(github_app.policy_authorized_description({"policy_ref": "x" * 200})) <= 140


def test_user_transport_posts_success_with_the_policy_description(monkeypatch):
    calls = []
    monkeypatch.setattr(
        github_app._GitHubHttpTransportBase,
        "_request",
        lambda self, method, path, body: calls.append((method, path, dict(body))) or {"id": 1},
    )
    transport = github_app.GitHubUserTokenTransport(lambda: "synthetic")
    check_id = transport.create_check_run(
        repo="example/project",
        head_sha=HEAD,
        name=github_app.CHECK_RUN_NAME,
        status="queued",
        details_url="https://github.com/example/project/pull/123",
    )
    description = f"Policy-authorized: {POLICY_REF} by {STEWARD}"
    transport.update_check_run(
        repo="example/project",
        check_run_id=check_id,
        status="completed",
        conclusion="success",
        description=description,
    )
    assert calls[-1][2]["state"] == "success"
    assert calls[-1][2]["description"] == description
    # Without an override the human outcome text is unchanged.
    transport.update_check_run(
        repo="example/project", check_run_id=check_id, status="completed", conclusion="success"
    )
    assert calls[-1][2]["description"] == "Human approval recorded"


def test_app_transport_carries_the_policy_description_as_check_output(monkeypatch):
    calls = []
    monkeypatch.setattr(
        github_app._GitHubHttpTransportBase,
        "_request",
        lambda self, method, path, body: calls.append((method, path, dict(body))) or {"id": 7},
    )
    transport = github_app.GitHubAppHttpTransport(lambda: "synthetic")
    transport.update_check_run(
        repo="example/project",
        check_run_id="7",
        status="completed",
        conclusion="success",
        description="Policy-authorized: p by L",
    )
    assert calls[-1][2]["output"]["title"] == "Policy-authorized: p by L"
    transport.update_check_run(
        repo="example/project", check_run_id="7", status="completed", conclusion="success"
    )
    assert "output" not in calls[-1][2]


# --- the ledger outcome at merge ---------------------------------------------


class RecordingTransport(github_app.GitHubUserTokenTransport):
    def __init__(self):
        super().__init__(lambda: "unused")
        self.created = []
        self.updated = []
        self.comments = []

    def create_check_run(self, **kwargs):
        self.created.append(kwargs)
        return f"status:{kwargs['head_sha']}|{kwargs['details_url']}"

    def update_check_run(self, **kwargs):
        self.updated.append(kwargs)

    def status_matches(self, *, repo, check_run_id, status, conclusion, description=None):
        # The newest status this double "posted" on the head, compared the way
        # the real transport compares the combined status (no network).
        sha = str(check_run_id).split(":", 1)[-1].split("|", 1)[0]
        latest = None
        for call in self.created:
            if call.get("head_sha") == sha:
                latest = call
        for call in self.updated:
            if str(call.get("check_run_id", "")).split(":", 1)[-1].split("|", 1)[0] == sha:
                latest = call
        if latest is None:
            return False
        key = github_app.GitHubUserTokenTransport.status_key
        return key(latest["status"], latest.get("conclusion"), latest.get("description")) == key(
            status, conclusion, description
        )

    def find_completed_check(self, **kwargs):
        # Reconcile against what this double "posted" (no network): created
        # checks, and updates of a bound check (``status:<sha>|<url>`` ids).
        posted = list(self.created) + [
            {**u, "head_sha": str(u["check_run_id"]).split(":", 1)[-1].split("|", 1)[0]}
            for u in self.updated
        ]
        return any(
            c.get("head_sha") == kwargs["head_sha"]
            and c.get("status") == "completed"
            and c.get("conclusion") == kwargs["conclusion"]
            and c.get("description") == kwargs.get("description")
            for c in posted
        )

    def create_evidence_comment(self, **kwargs):
        self.comments.append(kwargs)
        return "comment-1"

    def find_evidence_comment(self, **_kwargs):
        return None


def _pr(sha=HEAD, *, state="OPEN", merged=False):
    return {
        "number": 123,
        "title": "Ship a fix",
        "url": "https://github.com/example/project/pull/123",
        "state": state,
        "headRefOid": sha,
        "headRefName": BRANCH,
        "baseRefName": "main",
        "isDraft": False,
        "author": {"login": "fleet-bot"},
        "labels": [],
        "reviews": [],
        "mergedAt": "2026-10-01T16:00:00Z" if merged else None,
        "closedAt": "2026-10-01T16:00:00Z" if state != "OPEN" else None,
    }


class FakeGitHub:
    def __init__(self, files=("src/unified_agent/tasks_view.py", "tests/test_tasks_view.py")):
        self.open = [_pr()]
        self.closed = _pr(state="MERGED", merged=True)
        self.files = list(files)
        self.files_error = False
        self.file_calls = 0

    def __call__(self, cmd, **_kwargs):
        if cmd[:2] == ["gh", "api"]:
            self.file_calls += 1
            if self.files_error:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="".join(f"{f}\n" for f in self.files))
        if "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.closed))
        return SimpleNamespace(returncode=0, stdout=json.dumps(list(self.open)))


def _policy():
    return PolicyConfig(
        rules=[
            PolicyRule(
                name="dogfood-pr-approval",
                tool=github_app.TOOL_NAME,
                type="tool",
                action="require_approval",
            )
        ]
    )


def _poll(gh, transport):
    return github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())


def _pending_then_merge(gh, transport, *, merged_head=HEAD):
    pending = _poll(gh, transport)[0]
    assert pending["status"] == "pending_approval"
    gh.open = []
    gh.closed = _pr(merged_head, state="MERGED", merged=True)
    return pending


def test_merge_with_exact_head_policy_authorization_is_policy_authorized():
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    pending = _pending_then_merge(gh, transport)
    record = _authorize(task_id)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "policy_authorized"
    assert closed[0]["evidence_class"] == github_app.EVIDENCE_CLASS_POLICY
    assert closed[0]["authorization_id"] == record["authorization_id"]
    assert transport.updated[-1]["conclusion"] == "success"
    assert transport.updated[-1]["description"] == (f"Policy-authorized: {POLICY_REF} by {STEWARD}")
    # No human evidence comment: the class is never presented as human approval.
    assert transport.comments == []
    approval = approvals.get_approval(pending["approval_id"])
    assert approval["status"] == "expired"
    assert approval["resolved_by"] is None
    outcome = events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL])
    assert len(outcome) == 1
    payload = outcome[0]["payload"]
    assert payload["evidence_class"] == "policy-authorized"
    assert payload["human_approval"] is False
    assert payload["task_id"] == task_id
    assert payload["head_sha"] == HEAD
    # Terminal: a later poll does nothing more.
    assert _poll(gh, transport) == []


def test_authorization_for_an_older_head_does_not_cover_the_merged_head():
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id, head=OLD_HEAD)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "merged_without_approval"
    assert transport.updated[-1]["conclusion"] == "neutral"
    assert gh.file_calls == 0


def test_red_tier_scope_ignores_policy_authorization():
    gh = FakeGitHub(files=("src/unified_agent/tasks_view.py", "src/unified_agent/server.py"))
    transport = RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "merged_without_approval"
    assert transport.updated[-1]["conclusion"] == "neutral"
    assert gh.file_calls == 1
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL]) == []


@pytest.mark.parametrize(
    "path",
    ["AGENTS.md", "deploy/mac-hub/hub.plist", ".mcp.json", "src/unified_agent/safety.py"],
)
def test_each_red_tier_pattern_blocks_policy(path):
    gh = FakeGitHub(files=("README.md", path))
    transport = RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id)

    assert _poll(gh, transport)[0]["status"] == "merged_without_approval"


def test_unreadable_changed_files_fail_closed():
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.files_error = True
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "merged_without_approval"
    assert gh.file_calls == 1


def test_empty_changed_file_list_fails_closed():
    gh, transport = FakeGitHub(files=()), RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id)

    assert _poll(gh, transport)[0]["status"] == "merged_without_approval"


def test_human_denial_is_never_overridden_by_policy():
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    pending = _pending_then_merge(gh, transport)
    _authorize(task_id)
    with as_identity(config.operator_label()):
        approvals.approval_resolve(pending["approval_id"], "deny")

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "denied"
    assert transport.updated[-1]["conclusion"] == "failure"


def test_closed_without_merge_is_not_policy_authorized():
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    gh.closed = _pr(state="CLOSED")
    _authorize(task_id)

    assert _poll(gh, transport)[0]["status"] == "closed_without_approval"


def test_human_approval_still_records_human_evidence_when_both_exist(monkeypatch):
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    pending = _pending_then_merge(gh, transport)
    _authorize(task_id)
    with as_identity(config.operator_label()):
        approvals.approval_resolve(pending["approval_id"], "approve")

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "approved_post_merge"
    assert len(transport.comments) == 1
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL]) == []


def test_merged_head_never_polled_supersedes_the_older_head_then_records_policy():
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.open = [_pr(OLD_HEAD)]
    task_id = _task()
    older = _pending_then_merge(gh, transport, merged_head=HEAD)
    _authorize(task_id)

    closed = _poll(gh, transport)

    assert closed[-1]["status"] == "policy_authorized"
    assert closed[-1]["approval_id"] is None
    stale = [call for call in transport.updated if call["conclusion"] == "stale"]
    assert len(stale) == 1
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    terminal = transport.created[-1]
    assert terminal["head_sha"] == HEAD
    assert terminal["status"] == "completed"
    assert terminal["conclusion"] == "success"
    assert terminal["description"] == f"Policy-authorized: {POLICY_REF} by {STEWARD}"
    assert _poll(gh, transport) == []


def test_task_from_another_branch_cannot_authorize_this_pull_request():
    """Review: citing an unrelated task's claimant must not pass separation for this PR."""
    gh, transport = FakeGitHub(), RecordingTransport()
    unrelated = _task(branch="task/someone-elses-work")
    _pending_then_merge(gh, transport)
    _authorize(unrelated)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "merged_without_approval"
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL]) == []


def test_unseen_merged_head_never_overrides_a_denied_older_head():
    """Review: a human "no" on the PR is never replaced by a policy success."""
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.open = [_pr(OLD_HEAD)]
    task_id = _task()
    older = _pending_then_merge(gh, transport, merged_head=HEAD)
    _authorize(task_id)
    with as_identity(config.operator_label()):
        approvals.approval_resolve(older["approval_id"], "deny")

    closed = _poll(gh, transport)

    assert [r["status"] for r in closed] == ["denied"]
    assert not [c for c in transport.created if c.get("conclusion") == "success"]
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL]) == []


def test_unseen_merged_head_status_failure_is_retried_next_poll():
    """Review: retiring the older head first stranded the merged head on a failed post."""
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.open = [_pr(OLD_HEAD)]
    task_id = _task()
    older = _pending_then_merge(gh, transport, merged_head=HEAD)
    _authorize(task_id)

    real_create = transport.create_check_run

    def fail_once(**kwargs):
        transport.create_check_run = real_create
        raise RuntimeError("GitHub timed out")

    transport.create_check_run = fail_once
    assert _poll(gh, transport)[0]["status"] == "error"
    # The committed decision fenced the older row (PR #1302 review round 2),
    # but its status marker is unresolved, so the next poll finds the PR.
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    assert any(
        int(p["approval_id"]) == older["approval_id"]
        for p in github_app_worker._unresolved_pending()
    )

    closed = _poll(gh, transport)

    assert closed[-1]["status"] == "policy_authorized"
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    assert len(events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL])) == 1
    assert _poll(gh, transport) == []


def test_unseen_merged_head_outcome_write_failure_is_retried(monkeypatch):
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.open = [_pr(OLD_HEAD)]
    task_id = _task()
    older = _pending_then_merge(gh, transport, merged_head=HEAD)
    _authorize(task_id)
    real_record = github_app_worker._record_policy_outcome

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(github_app_worker, "_record_policy_outcome", real_record)
        raise RuntimeError("event log busy")

    monkeypatch.setattr(github_app_worker, "_record_policy_outcome", fail_once)
    assert _poll(gh, transport)[0]["status"] == "error"
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    assert any(
        int(p["approval_id"]) == older["approval_id"]
        for p in github_app_worker._unresolved_pending()
    )

    closed = _poll(gh, transport)

    assert closed[-1]["status"] == "policy_authorized"
    assert len(events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL])) == 1
    assert approvals.get_approval(older["approval_id"])["status"] == "expired"
    # PR #1302 review: the retry reconciles the success it already posted.
    assert [c.get("conclusion") for c in transport.created].count("success") == 1


def test_merged_head_never_polled_without_authorization_is_unchanged():
    gh, transport = FakeGitHub(), RecordingTransport()
    gh.open = [_pr(OLD_HEAD)]
    _task()
    _pending_then_merge(gh, transport, merged_head=HEAD)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "merged_without_approval"
    assert gh.file_calls == 0


# --- evidence report ----------------------------------------------------------


def test_evidence_report_shows_policy_authorization_as_its_own_class():
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    _authorize(task_id)
    _poll(gh, transport)

    report = evidence_report.build_evidence_report(task_id=task_id)
    record = report["records"][0]
    merge_auth = record["proof"]["merge_authorization"]
    assert merge_auth["evidence_class"] == "policy-authorized"
    assert merge_auth["human_approval"] is False
    assert merge_auth["policy_ref"] == POLICY_REF
    assert merge_auth["authorized_by"] == STEWARD
    assert merge_auth["head_sha"] == HEAD
    # Never promoted to human authority.
    assert record["human_provenance"]["verified"] is False

    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert f"policy-authorized ({POLICY_REF} by {STEWARD}; not a human approval)" in markdown
    text = evidence_report.format_evidence_report(report, "text")
    assert "merge_authorization=policy-authorized" in text


def test_evidence_report_without_policy_authorization_keeps_its_shape():
    task_id = _task()
    report = evidence_report.build_evidence_report(task_id=task_id)
    assert "merge_authorization" not in report["records"][0]["proof"]


# --- recording is a pre-merge step for live work (review: completed tasks
# lose their branch binding) ------------------------------------------------


def _set_task(task_id: int, **fields) -> None:
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn = storage.connect()
    try:
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_terminal_task_is_refused_with_a_pre_merge_error(status):
    """A closed task no longer identifies the PR or its author: refuse plainly."""
    task_id = _task()
    # Completion releases the worktree, clears the branch and stamps the
    # closer into claimed_by (tools/tasks.py done transition).
    _set_task(task_id, status=status, claimed_by=STEWARD, branch=None, worktree_path=None)
    with pytest.raises(PermissionError, match=f"is {status}.*before the merge"):
        _authorize(task_id)
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL]) == []


def test_terminal_task_is_refused_even_when_its_branch_survived():
    """A done task whose worktree release was skipped keeps a stale branch; still refused."""
    task_id = _task()
    _set_task(task_id, status="done")
    with pytest.raises(PermissionError, match="is done"):
        _authorize(task_id)
    assert _verified() is None


@pytest.mark.parametrize("status", ["open", "waiting_review", "blocked"])
def test_live_task_statuses_with_a_branch_may_be_authorized(status):
    task_id = _task()
    _set_task(task_id, status=status)
    assert _authorize(task_id)["task_branch"] == BRANCH


def test_released_worktree_branch_is_refused_and_not_taken_from_the_caller():
    """A live task whose branch was cleared (new run, released worktree) is refused."""
    task_id = _task()
    _set_task(task_id, branch=None, worktree_path=None)
    with pytest.raises(PermissionError, match="no branch recorded"):
        _authorize(task_id)


def test_authorization_recorded_before_completion_survives_the_branch_being_cleared():
    """Pre-merge record, then merge and task close: the signed branch still binds the PR."""
    gh, transport = FakeGitHub(), RecordingTransport()
    task_id = _task()
    _pending_then_merge(gh, transport)
    record = _authorize(task_id)
    _set_task(task_id, status="done", claimed_by=STEWARD, branch=None, worktree_path=None)

    closed = _poll(gh, transport)

    assert closed[0]["status"] == "policy_authorized"
    assert closed[0]["authorization_id"] == record["authorization_id"]
    payload = events.query_events(channels=[github_app.POLICY_AUTHORIZED_CHANNEL])[0]["payload"]
    assert payload["task_id"] == task_id


# --- recording is atomic with the task row it binds --------------------------


def _close_task_now(task_id: int) -> None:
    """Commit the done transition on a fresh connection, failing fast if locked."""
    conn = storage.connect()
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute(
            "UPDATE tasks SET status='done', claimed_by=?, branch=NULL, worktree_path=NULL "
            "WHERE id=?",
            (STEWARD, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_task_closure_cannot_commit_between_validation_and_the_append(monkeypatch):
    """The window between the task check and the append is a single write transaction.

    A closer that tries to commit after the task row was validated, but before
    the signed record is appended, is locked out; it can only close the task
    once the authorization (bound to the live row) is durable.
    """
    task_id = _task()
    attempts: list[str] = []
    real_sign = github_app._sign_policy_binding

    def close_inside_the_window(identity, binding, tid):
        try:
            _close_task_now(task_id)
            attempts.append("committed")
        except sqlite3.OperationalError as exc:
            attempts.append(f"locked: {exc}")
        return real_sign(identity, binding, tid)

    monkeypatch.setattr(github_app, "_sign_policy_binding", close_inside_the_window)
    record = _authorize(task_id)
    monkeypatch.setattr(github_app, "_sign_policy_binding", real_sign)

    assert len(attempts) == 1 and attempts[0].startswith("locked"), attempts
    assert record["task_branch"] == BRANCH and record["task_claimant"] == AUTHOR
    # The closer retries afterwards: the pre-merge record stays valid.
    _close_task_now(task_id)
    assert _verified()["authorization_id"] == record["authorization_id"]


def test_closure_committed_after_the_request_starts_is_refused_and_writes_nothing(
    monkeypatch,
):
    """A closer that holds the write lock first wins: the authorizer sees the closed row.

    The authorizer starts while the closure is in flight. Under the old split
    read it would have validated the still-live row and appended after the
    closure committed; now its read waits for the lock and refuses.
    """
    task_id = _task()
    closer = storage.connect()
    closer.execute("BEGIN IMMEDIATE")
    closer.execute(
        "UPDATE tasks SET status='done', claimed_by=?, branch=NULL, worktree_path=NULL WHERE id=?",
        (STEWARD, task_id),
    )
    started = threading.Event()
    real_connect = storage.connect

    def connect_and_signal():
        conn = real_connect()
        started.set()
        return conn

    monkeypatch.setattr(storage, "connect", connect_and_signal)
    outcome: dict[str, BaseException | dict] = {}

    def authorize():
        try:
            outcome["result"] = _authorize(task_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=authorize)
    worker.start()
    try:
        assert started.wait(5)
        time.sleep(0.2)  # let the authorizer reach its lock wait
        closer.commit()
    finally:
        closer.close()
        worker.join(10)
    monkeypatch.setattr(storage, "connect", real_connect)

    assert not worker.is_alive()
    assert "result" not in outcome, outcome
    assert isinstance(outcome["error"], PermissionError)
    assert "is done" in str(outcome["error"])
    assert events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL]) == []
