from __future__ import annotations

import json
import re
import subprocess
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import httpx
import pytest

from unified_agent import config, events, github_app, github_app_worker, policy, pr_watch, storage
from unified_agent.audit import producer_sign
from unified_agent.policy import PolicyConfig, PolicyRule
from unified_agent.tools import approvals


@pytest.fixture(autouse=True)
def configured_repository(monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")


class RecordingTransport(github_app.GitHubUserTokenTransport):
    def __init__(self):
        super().__init__(lambda: "unused")
        self.created = []
        self.updated = []
        self.comments = []
        self.fail_after_comment = False
        self.fail_before_comment = False

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
        if self.fail_before_comment:
            self.fail_before_comment = False
            raise TimeoutError("comment post failed before write")
        self.comments.append(kwargs)
        if self.fail_after_comment:
            self.fail_after_comment = False
            raise TimeoutError("response lost after comment post")
        return "comment-1"

    def find_evidence_comment(self, **kwargs):
        for comment in self.comments:
            marker = re.search(r"<!-- unified-agent github-evidence (\{.*?\}) -->", comment["body"])
            if marker:
                evidence = json.loads(marker.group(1))
                if (
                    evidence["event_id"] == kwargs["event_id"]
                    and evidence["approval_id"] == kwargs["approval_id"]
                ):
                    return {"id": "comment-1", "evidence": evidence}
        return None


def _pr(sha="head-1", title="Release gate", draft=False):
    return {
        "number": 123,
        "title": title,
        "url": "https://github.com/example/project/pull/123",
        "state": "OPEN",
        "headRefOid": sha,
        "headRefName": "codex/release",
        "baseRefName": "main",
        "isDraft": draft,
        "author": {"login": "Desktop_Agent2"},
        "labels": [],
        "reviews": [],
    }


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


def test_brokered_worker_reads_github_in_process(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(github_app, "resolve_user_token", lambda: "synthetic-broker-token")
    seen = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            seen.append((url, kwargs["headers"]["Authorization"]))
            return httpx.Response(200, json=[])

    monkeypatch.setattr(github_app_worker.httpx, "Client", Client)
    assert (
        github_app_worker.run_once(
            run_gh=lambda *_args, **_kwargs: pytest.fail("CLI token leak"),
            transport=RecordingTransport(),
        )
        == []
    )
    assert seen == [
        (
            "https://api.github.com/repos/example/project/pulls",
            "Bearer synthetic-broker-token",
        )
    ]


def test_brokered_pr_detail_normalizes_rest_and_401_marks_reconnect(monkeypatch):
    payload = {
        "number": 123,
        "title": "Release gate",
        "html_url": "https://github.com/example/project/pull/123",
        "state": "closed",
        "merged_at": "2026-09-25T08:00:00Z",
        "head": {"sha": "head-1", "ref": "codex/release"},
        "base": {"ref": "main"},
        "user": {"login": "Desktop_Agent2"},
        "labels": [{"name": "release"}],
    }
    brokered_json = github_app_worker._brokered_github_json
    monkeypatch.setattr(github_app_worker, "_brokered_github_json", lambda *_a: payload)
    snapshot = github_app_worker._brokered_view_pr("synthetic-token", 123)
    assert (snapshot.state, snapshot.head_sha, snapshot.labels) == (
        "MERGED",
        "head-1",
        ("release",),
    )
    monkeypatch.setattr(github_app_worker, "_brokered_github_json", brokered_json)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return httpx.Response(401, json={"message": "bad credentials"})

    seen = []
    monkeypatch.setattr(github_app_worker.httpx, "Client", Client)
    monkeypatch.setattr(
        github_app_worker.connector_oauth,
        "mark_invalid_credential",
        lambda provider, **kwargs: seen.append((provider, kwargs["rejected_token"])),
    )
    with pytest.raises(RuntimeError, match="GitHub PR read failed"):
        github_app_worker._brokered_github_json("synthetic-token", "/repos/o/r/pulls")
    assert seen == [("github", "synthetic-token")]


def test_poll_creates_one_pending_status_then_signed_comment_after_approval(
    isolated_env, monkeypatch
):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")
    prs = [_pr()]

    def gh(_cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    transport = RecordingTransport()
    first = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert first[0]["status"] == "pending_approval"
    assert len(transport.created) == 1
    assert github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy()) == []
    assert len(transport.created) == 1

    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(first[0]["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)

    # A title edit cannot alter the approval payload while the head is fixed.
    prs[0] = _pr(title="Release gate, edited")
    completed = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert completed[0]["status"] == "approved"
    assert len(transport.comments) == 1
    assert transport.updated[0]["conclusion"] == "success"
    assert github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy()) == []

    prs[0] = _pr(sha="head-2")
    next_head = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert next_head[0]["status"] == "pending_approval"
    assert len(transport.created) == 2


def test_poll_fails_closed_without_registered_worker_bearer(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.delenv(github_app_worker.TOKEN_VAR, raising=False)
    with pytest.raises(PermissionError, match=github_app_worker.TOKEN_VAR):
        github_app_worker.run_once(run_gh=lambda *_a, **_k: None)


def test_poll_rejects_another_registered_agent_bearer(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "other-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"other-bearer": "Desktop_Agent5"})
    with pytest.raises(PermissionError, match="Desktop_Agent2"):
        github_app_worker.run_once(run_gh=lambda *_a, **_k: None)


def test_disabled_worker_never_polls(isolated_env, monkeypatch):
    monkeypatch.delenv("UA_GITHUB_APP_ENABLED", raising=False)
    assert github_app_worker.run_once(run_gh=lambda *_a, **_k: pytest.fail("polled")) == []


def test_draft_pr_also_receives_pending_status(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    transport = RecordingTransport()
    result = github_app_worker.run_once(
        run_gh=lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=json.dumps([_pr(draft=True)])
        ),
        transport=transport,
        policy_config=_policy(),
    )
    assert result[0]["status"] == "pending_approval"


def test_pr_watch_schedule_invokes_connector_consumer(isolated_env, monkeypatch):
    called = []
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: [])
    monkeypatch.setattr(github_app_worker, "run_once", lambda: (called.append(True), [])[1])
    monkeypatch.setattr("sys.argv", ["pr-watch", "--repos", github_app_worker._repo()])
    pr_watch.main()
    assert called == [True]


def test_consumed_approval_recovers_posted_comment_without_duplicate(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")

    def gh(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps([_pr()]))

    transport = RecordingTransport()
    pending = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)

    transport.fail_after_comment = True
    failed = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert failed[0]["status"] == "error"
    assert len(transport.comments) == 1
    recovered = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert recovered[0]["status"] == "approved"
    assert len(transport.comments) == 1
    assert transport.updated[0]["conclusion"] == "success"


def test_consumed_approval_retries_when_comment_was_never_posted(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")

    def gh(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps([_pr()]))

    transport = RecordingTransport()
    pending = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    transport.fail_before_comment = True
    failed = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert failed[0]["status"] == "error"
    assert transport.comments == []
    recovered = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert recovered[0]["status"] == "approved"
    assert len(transport.comments) == 1


def test_one_pr_error_does_not_prevent_next_head(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    next_pr = _pr(sha="head-2")
    next_pr["number"] = 124
    next_pr["url"] = "https://github.com/example/project/pull/124"

    def gh(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps([_pr(), next_pr]))

    transport = RecordingTransport()
    original = transport.create_check_run

    def create_check_run(**kwargs):
        if kwargs["head_sha"] == "head-1":
            raise TimeoutError("first head failed")
        return original(**kwargs)

    transport.create_check_run = create_check_run
    results = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert [r["status"] for r in results] == ["error", "pending_approval"]


def test_stuck_corrections_are_bounded_while_github_backs_off(isolated_env, monkeypatch):
    """Task #2237: five corrections polled every 120 s for an hour; GitHub limits #1.

    Before, every poll re-sent every correction: 30 polls x 5 = 150 requests
    an hour. Now the 429 on #1 (``Retry-After: 600``) stops that pass, the
    polls inside the window read and send nothing, and the next pass resumes
    at #2 (review B1: a refused correction must not use up its turn), so #2-#5
    land after one window while #1 keeps one request per window.
    """
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    start = 1_800_000_000.0
    clock = [start]
    monkeypatch.setattr(github_app, "_wall_clock", lambda: clock[0])
    monkeypatch.setattr(github_app, "_monotonic", lambda: clock[0])
    sent, reads, corrected = [], [], set()

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, timeout):
        number = int(request.full_url.rsplit("-", 1)[1])
        sent.append((clock[0] - start, number))
        if number == 1:
            headers = Message()
            headers["Retry-After"] = "600"
            raise HTTPError(request.full_url, 429, "Too Many Requests", headers, BytesIO(b"{}"))
        corrected.add(number)
        return Response(b"{}")

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    rows = [
        (events.new_ulid(), {"event_id": f"event-{n}", "number": n, "head_sha": f"head-{n}"})
        for n in range(1, 6)
    ]
    monkeypatch.setattr(
        github_app_worker,
        "_unfinished_policy_intent_rows",
        lambda: [row for row in rows if row[1]["number"] not in corrected],
    )
    monkeypatch.setattr(github_app_worker, "_needs_correction", lambda _intent: True)
    monkeypatch.setattr(github_app_worker, "_retire_older_heads", lambda *_a, **_k: None)

    def correct(transport, intent, *, reconcile):
        # A correction is a non-success status on the head.
        transport.update_check_run(
            repo="example/project",
            check_run_id=f"status:head-{intent['number']}|",
            status="completed",
            conclusion="action_required",
        )
        return {"status": "corrected", "event_id": intent["event_id"]}

    monkeypatch.setattr(github_app_worker, "_publish_policy_intent", correct)

    def gh(*_args, **_kwargs):
        reads.append(clock[0] - start)
        return SimpleNamespace(returncode=0, stdout="[]")

    transport = github_app.GitHubUserTokenTransport(lambda: "synthetic")
    polls = []
    for _ in range(30):
        polls.append(
            [r["status"] for r in github_app_worker.run_once(run_gh=gh, transport=transport)]
        )
        clock[0] += 120

    windows = [0, 600, 1200, 1800, 2400, 3000]
    assert sent == [
        (0, 1),
        (600, 2),
        (600, 3),
        (600, 4),
        (600, 5),
        (600, 1),
        (1200, 1),
        (1800, 1),
        (2400, 1),
        (3000, 1),
    ]  # 10 requests in the hour, not 150, and every correction got its turn
    assert corrected == {2, 3, 4, 5}
    assert reads == windows
    assert polls[5] == ["corrected"] * 4 + ["backoff"]
    for at, poll in zip(range(0, 3600, 120), polls, strict=True):
        assert poll == (["backoff"] if at != 600 else polls[5])

    # A backoff another process records mid-pass refuses this pass's first
    # attempt unsent: the retry position (cursor and lead) is left as it was.
    before = github_app_worker._load_retry_state("example/project")

    def token_after_another_process_is_limited():
        github_app._record_backoff(transport.backoff_scope(), 600)
        return "synthetic"

    racing = github_app.GitHubUserTokenTransport(token_after_another_process_is_limited)
    [refused] = github_app_worker._finish_policy_publications(racing)
    assert refused["status"] == "backoff" and len(sent) == 10
    assert github_app_worker._load_retry_state("example/project") == before


def test_user_transport_finds_existing_signed_comment(monkeypatch):
    evidence = {"event_id": "event-1", "approval_id": 42}
    payload = [
        {
            "id": 77,
            "body": "Evidence\n<!-- unified-agent github-evidence " + json.dumps(evidence) + " -->",
        }
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    requests = []

    def open_request(request, timeout):
        requests.append((request.full_url, request.get_header("Authorization"), timeout))
        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    found = transport.find_evidence_comment(
        repo="example/project", number=123, event_id="event-1", approval_id=42
    )
    assert found == {"id": "77", "evidence": evidence}
    assert requests == [
        (
            "https://api.github.com/repos/example/project/issues/123/comments?per_page=100&page=1",
            "Bearer test-token",
            30,
        )
    ]


def test_comment_recovery_paginates_past_one_thousand_comments(monkeypatch):
    marker = {"event_id": "event-1", "approval_id": 42}
    pages = [0]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            if pages[0] <= 10:
                return json.dumps([{"id": n, "body": "ordinary"} for n in range(100)]).encode()
            return json.dumps(
                [
                    {
                        "id": 1001,
                        "body": "<!-- unified-agent github-evidence " + json.dumps(marker) + " -->",
                    }
                ]
            ).encode()

    def open_request(_request, timeout):
        assert timeout == 30
        pages[0] += 1
        return Response()

    monkeypatch.setattr(github_app._REDIRECT_SAFE_OPENER, "open", open_request)
    transport = github_app.GitHubUserTokenTransport(lambda: "test-token")
    found = transport.find_evidence_comment(
        repo="example/project", number=123, event_id="event-1", approval_id=42
    )
    assert found == {"id": "1001", "evidence": marker}
    assert pages[0] == 11


def test_manual_worker_exits_nonzero_on_partial_failure(monkeypatch, capsys):
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [{"status": "error"}])
    with pytest.raises(SystemExit) as exc:
        github_app_worker.main()
    assert exc.value.code == 1
    assert '"status": "error"' in capsys.readouterr().out


def test_worker_lock_excludes_concurrent_poll(isolated_env):
    probe = (
        "import fcntl, sys; "
        "f = open(sys.argv[1], 'a+'); "
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with github_app_worker._poll_lock():
        lock_path = config.data_home() / "github-status-locks/dogfood-worker.lock"
        held = subprocess.run([sys.executable, "-c", probe, str(lock_path)], capture_output=True)
        assert held.returncode != 0
    released = subprocess.run([sys.executable, "-c", probe, str(lock_path)], capture_output=True)
    assert released.returncode == 0


def test_denial_is_terminal_and_expiry_requeues(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]

    def gh(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    transport = RecordingTransport()
    first = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(first["approval_id"], "deny")
    finally:
        config.agent_identity_var.reset(token)
    assert (
        github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0][
            "status"
        ]
        == "denied"
    )
    assert github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy()) == []
    assert len(transport.updated) == 1

    prs[0] = _pr(sha="head-2")
    second = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status='expired' WHERE id=?", (second["approval_id"],))
        conn.commit()
    finally:
        conn.close()
    renewed = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    assert renewed["status"] == "pending_approval"
    assert renewed["approval_id"] != second["approval_id"]


def test_connector_error_does_not_suppress_legacy_poll(isolated_env, monkeypatch):
    called = []
    monkeypatch.setattr(pr_watch, "run", lambda **_kwargs: called.append("journal"))
    monkeypatch.setattr(github_app_worker, "run_once", lambda: [{"status": "error"}])
    monkeypatch.setattr("sys.argv", ["pr-watch", "--repos", github_app_worker._repo()])
    with pytest.raises(SystemExit):
        pr_watch.main()
    assert called == ["journal"]


def _merged(sha="head-1"):
    pr = _pr(sha=sha)
    pr["state"] = "MERGED"
    pr["mergedAt"] = "2026-09-24T16:00:00Z"
    return pr


def _gh_open_then_merged(prs, sha="head-1"):
    def gh(cmd, **_kwargs):
        if "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(_merged(sha)))
        return SimpleNamespace(returncode=0, stdout=json.dumps(list(prs)))

    return gh


def test_merged_pr_consumes_approved_pass_once(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")
    prs = [_pr()]
    transport = RecordingTransport()
    pending = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    prs.clear()
    completed = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )
    assert completed[0]["status"] == "approved_post_merge"
    assert transport.updated[-1]["conclusion"] == "success"
    assert len(transport.comments) == 1
    assert (
        github_app_worker.run_once(
            run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
        )
        == []
    )


def test_merge_on_a_new_head_still_closes_the_prior_pass(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")
    prs = [_pr(sha="head-a")]

    def gh(cmd, **_kwargs):
        if "view" in cmd:
            merged = _merged("head-b")
            return SimpleNamespace(returncode=0, stdout=json.dumps(merged))
        return SimpleNamespace(returncode=0, stdout=json.dumps(list(prs)))

    transport = RecordingTransport()
    pending = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    prs.clear()
    closed = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    # The PR merged at head-b, so the head-a pass is closed but head-a is not
    # published as approved: every settle decides from the live head (#2234).
    assert (closed[0]["status"], closed[0]["reason"]) == ("superseded", github_app.HEAD_MOVED)
    assert transport.updated[-1]["conclusion"] == "stale"
    assert transport.comments == []
    assert approvals.approval_pending()["approved_unconsumed"] == []


def test_merged_pr_without_approval_posts_terminal_status(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]
    transport = RecordingTransport()
    pending = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )[0]
    prs.clear()
    closed = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )
    assert closed[0]["status"] == "merged_without_approval"
    assert transport.updated[-1]["conclusion"] == "neutral"
    assert approvals.get_approval(pending["approval_id"])["status"] == "expired"
    assert (
        github_app_worker.run_once(
            run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
        )
        == []
    )


def test_doctor_flags_an_approved_pass_older_than_one_poll(isolated_env, monkeypatch):
    from unified_agent.tools.doctor import _github_approval_backlog_check

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]

    def gh(_cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    pending = github_app_worker.run_once(
        run_gh=gh, transport=RecordingTransport(), policy_config=_policy()
    )[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (pending["approval_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert _github_approval_backlog_check()["status"] == "DEGRADED"
    assert github_app_worker.stale_unconsumed_github_passes(interval_s=10**9) == 0


def _approved_github_pass(
    resolved_at: str, *, org_id: str | None = None, payload: dict | None = None
) -> int:
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, resolved_by, "
            "created_at, updated_at, resolved_at, org_id) "
            "VALUES (?, ?, 'github-app', 'approved', 'Operator', ?, ?, ?, ?)",
            (
                github_app.TOOL_NAME,
                json.dumps(payload or {}),
                resolved_at,
                resolved_at,
                resolved_at,
                org_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_unconsumed_github_passes_match_approval_pending_buckets(isolated_env, monkeypatch):
    """Task #1804: the doctor and approval_pending read the same rows.

    fresh + stale are exactly approval_pending's approved_unconsumed (live
    passes, this tenant), expired is its expired_passes, and another tenant's
    pass is in neither."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    storage.init_db()
    now = datetime.now(UTC)
    fresh = _approved_github_pass(now.isoformat())
    stale = _approved_github_pass((now - timedelta(hours=2)).isoformat())
    expired = _approved_github_pass("2020-01-01T00:00:00+00:00")
    _approved_github_pass(now.isoformat(), org_id="org_someone_else")

    counts = github_app_worker.unconsumed_github_passes()
    queue = approvals.approval_pending(tool=github_app.TOOL_NAME)

    assert counts == {"fresh": 1, "stale": 1, "unreachable": 0, "expired": 1}
    assert sorted(r["id"] for r in queue["approved_unconsumed"]) == [fresh, stale]
    assert [r["id"] for r in queue["expired_passes"]] == [expired]
    assert github_app_worker.stale_unconsumed_github_passes() == 2


def test_doctor_live_detail_reports_fresh_unconsumed_passes(isolated_env, monkeypatch):
    """Task #1804: approval_pending listed nine fresh approved passes while the
    doctor said 'no stale unconsumed GitHub passes'. Fresh passes are not a
    backlog (LIVE stays LIVE), but the detail must count them."""
    from datetime import UTC, datetime

    from unified_agent.tools.doctor import _github_approval_backlog_check

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    storage.init_db()
    pr_watch.record_run_status(ok=True, now=datetime.now(UTC))
    for _ in range(3):
        _approved_github_pass(datetime.now(UTC).isoformat())

    row = _github_approval_backlog_check()

    assert row["status"] == "LIVE", row
    assert row["detail"] == "3 fresh unconsumed GitHub pass(es) (none stale)"


@pytest.mark.parametrize(
    ("open_heads", "expected", "status"),
    [
        ({}, {"stale": 0, "unreachable": 1}, "LIVE"),
        ({7: "newer"}, {"stale": 0, "unreachable": 1}, "LIVE"),
        ({7: "abc123"}, {"stale": 1, "unreachable": 0}, "DEGRADED"),
        (None, {"stale": 1, "unreachable": 0}, "DEGRADED"),
    ],
    ids=["merged", "superseded", "still-open", "no-snapshot"],
)
def test_pass_for_a_closed_pr_is_not_a_poller_backlog(
    isolated_env, monkeypatch, open_heads, expected, status
):
    """Task #2616: row 1954 (PR #1595) was approved, then the PR merged before
    a poll spent the pass. Nothing can consume it and the orphan sweep retires
    it only at its TTL, yet the doctor read DEGRADED for ~22 h. A fresh
    open-head snapshot that no longer lists the PR at the pass's head marks it
    unreachable; without a snapshot (a broken poller) it still counts."""
    from datetime import UTC, datetime, timedelta

    from unified_agent.tools.doctor import _github_approval_backlog_check

    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    storage.init_db()
    now = datetime.now(UTC)
    pr_watch.record_run_status(ok=True, now=now)
    if open_heads is not None:
        github_app_worker._save_open_heads("example/project", open_heads, at=now)
    _approved_github_pass(
        (now - timedelta(hours=2)).isoformat(),
        payload={"repo": "example/project", "number": 7, "head_sha": "abc123"},
    )

    counts = github_app_worker.unconsumed_github_passes()
    row = _github_approval_backlog_check()

    assert {k: counts[k] for k in expected} == expected
    assert row["status"] == status, row
    if expected["unreachable"]:
        assert "1 pass(es) for closed or superseded PR heads" in row["detail"]


def test_denied_merge_posts_failure_not_neutral(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]
    transport = RecordingTransport()
    pending = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "deny")
    finally:
        config.agent_identity_var.reset(token)
    prs.clear()
    closed = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )
    assert closed[0]["status"] == "denied"
    assert transport.updated[-1]["conclusion"] == "failure"


def test_expired_pass_on_a_merged_pr_does_not_open_a_new_check(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]
    transport = RecordingTransport()
    pending = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(pending["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (pending["approval_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    prs.clear()
    closed = github_app_worker.run_once(
        run_gh=_gh_open_then_merged(prs), transport=transport, policy_config=_policy()
    )
    assert closed[0]["status"] == "merged_without_approval"
    assert transport.updated[-1]["conclusion"] == "expired"
    assert len(transport.created) == 1


def test_restoring_a_superseded_head_requests_approval_again(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr(sha="head-a")]

    def gh(_cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    transport = RecordingTransport()
    github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    prs[0] = _pr(sha="head-b")
    github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    prs[0] = _pr(sha="head-a")
    restored = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert restored[0]["status"] == "pending_approval"
    assert transport.created[-1]["head_sha"] == "head-a"


def test_newer_head_retires_the_older_approval(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr()]

    def gh(_cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    transport = RecordingTransport()
    first = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())[0]
    token = config.agent_identity_var.set("Operator")
    try:
        approvals.approval_resolve(first["approval_id"], "approve")
    finally:
        config.agent_identity_var.reset(token)
    prs[0] = _pr(sha="head-2")
    nxt = github_app_worker.run_once(run_gh=gh, transport=transport, policy_config=_policy())
    assert nxt[0]["status"] == "pending_approval"
    assert approvals.get_approval(first["approval_id"])["status"] == "revoked"
    assert approvals.approval_pending()["approved_unconsumed"] == []
    assert transport.updated[-1]["conclusion"] == "stale"


def test_dogfood_policy_keeps_existing_runtime_limits():
    path = Path(__file__).resolve().parents[1] / "examples/policy.github-app-dogfood.yaml"
    rules = {rule.name: rule for rule in policy.load_policy(path).rules}
    assert set(rules) == {
        *(rule.name for rule in policy.DEFAULT_POLICY_RULES),
        "github_pr_requires_human",
    }
    assert rules["github_pr_requires_human"].action == "require_approval"
    assert rules["github_pr_requires_human"].tool == github_app.TOOL_NAME
    for built_in in policy.DEFAULT_POLICY_RULES:
        configured = rules[built_in.name]
        assert configured.type == built_in.type
        assert configured.tool == built_in.tool
        assert configured.max_calls == built_in.max_calls
        assert configured.cooldown_seconds == built_in.cooldown_seconds
        assert configured.bands == built_in.bands
        assert configured.require_episode == built_in.require_episode


@pytest.mark.parametrize(
    "base", [None, "master", "release/2026", "release+hotfix", "team@release", "rélease"]
)
def test_both_pollers_filter_on_the_configured_base_branch(monkeypatch, base):
    # A repository whose default branch is not main must still be polled.
    if base is None:
        monkeypatch.delenv("UA_GITHUB_CONNECTOR_BASE", raising=False)
    else:
        monkeypatch.setenv("UA_GITHUB_CONNECTOR_BASE", base)
    expected = base or "main"
    argv = []

    def gh(args, **_kwargs):
        argv.append(args)
        return SimpleNamespace(returncode=0, stdout="[]")

    assert github_app_worker._open_main_prs(gh) == {}
    assert argv[0][argv[0].index("--base") + 1] == expected

    seen = []

    def brokered(_token, _path, *, params=None):
        seen.append(params)
        return []

    monkeypatch.setattr(github_app_worker, "_brokered_github_json", brokered)
    assert github_app_worker._brokered_open_main_prs("synthetic-token") == {}
    assert seen[0]["base"] == expected


@pytest.mark.parametrize(
    "base",
    [
        "",
        "  ",
        "-main",
        "a..b",
        "x y",
        "ref~1",
        "a:b",
        "/main",
        "x.lock",
        "feature/.hidden",
        "topic/foo.lock/bar",
        "@",
        "x@{y",
        "x/",
        "x//y",
        "x.",
        "x\\y",
        "x?y",
        "x*y",
        "x[y",
        "x^y",
        "x\x7fy",
    ],
)
def test_invalid_base_branch_is_unconfigured(isolated_env, monkeypatch, base):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_BASE", base)
    if not base.strip():
        # Blank means the documented default, not a misconfiguration.
        assert github_app_worker._base_branch() == "main"
        return
    results = github_app_worker.run_once(
        run_gh=lambda *_a, **_k: pytest.fail("polled with an invalid base"),
        transport=RecordingTransport(),
    )
    assert results == [{"status": "unconfigured", "detail": results[0]["detail"]}]
    assert "UA_GITHUB_CONNECTOR_BASE" in results[0]["detail"]
