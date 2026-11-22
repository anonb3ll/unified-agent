"""The ``unified-agent/review-head`` check the Approval Ledger poll publishes (task #2320)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from unified_agent import (
    config,
    github_app,
    github_app_worker,
    held_pr,
    review_head_status,
    review_reads,
    storage,
)
from unified_agent.policy import PolicyConfig, PolicyRule

REPO = "example/project"
HEAD = "a" * 40
READ_HEAD = "b" * 40
SERVICE = github_app_worker.SERVICE_IDENTITY
NAME = review_head_status.CHECK_NAME


class FakeChecksApi(github_app.GitHubAppHttpTransport):
    """The real App transport over an in-memory Checks API, so nothing above the
    HTTP seam is mocked."""

    def __init__(self):
        super().__init__(lambda: "unused")
        self.runs: dict[int, dict] = {}
        self.writes: list[tuple[str, str]] = []
        self.refuse: list[Exception] = []
        self.lose_response = False
        self.listing_fails = False

    def own(self):
        return [run for run in self.runs.values() if run["name"] == NAME]

    def _request(self, method, path, body):
        if body.get("name") == github_app.CHECK_RUN_NAME:
            return {"id": 1}  # the ledger's own check, not under test
        self.writes.append((method, path))
        if self.refuse:
            raise self.refuse.pop(0)
        if method == "POST":
            run_id = 100 + len(self.runs)
            self.runs[run_id] = {**body, "id": run_id, "output": body.get("output", {})}
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("response lost")
            return {"id": run_id}
        run_id = int(path.rsplit("/", 1)[1])
        self.runs[run_id].update(body)
        return {"id": run_id}

    def _get_json(self, path):
        url = urlparse(path)
        if url.path.endswith("/check-suites"):
            return {"total_count": 1}
        if self.listing_fails:
            raise github_app.ReconcileIncomplete("cannot prove absence")
        name = parse_qs(url.query)["check_name"][0]
        sha = url.path.split("/commits/")[1].split("/")[0]
        runs = [r for r in self.runs.values() if r["name"] == name and r["head_sha"] == sha]
        return {"total_count": len(runs), "check_runs": runs}


def _pr(sha=HEAD, number=123, title="feat: something"):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "state": "OPEN",
        "headRefOid": sha,
        "headRefName": f"task/{number}",
        "baseRefName": "main",
        "isDraft": False,
        "author": {"login": "Desktop_Agent2"},
        "labels": [],
        "reviews": [],
    }


@pytest.fixture
def hub(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", REPO)
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "1")
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "service-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": SERVICE})
    state = SimpleNamespace(prs=[_pr()], compare={}, api=FakeChecksApi())

    def gh(args, **_kwargs):
        if args[:2] == ["gh", "api"]:
            base, head = args[2].split("/compare/", 1)[1].split("...")
            entry = state.compare.get((base, head))
            if entry is None:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(entry))
        return SimpleNamespace(returncode=0, stdout=json.dumps(state.prs))

    state.gh = gh
    return state


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


def _run(hub):
    return github_app_worker.run_once(run_gh=hub.gh, transport=hub.api, policy_config=_policy())


def _poll(hub):
    """The review-head results of one poll, without the ledger's own."""
    return [
        r
        for r in _run(hub)
        if str(r.get("status")).startswith("review_head")
        or str(r.get("event_id", "")).startswith(review_head_status.EVENT_ID)
    ]


def _read(head_sha, **over):
    return review_reads.record(
        **{
            "repo": REPO,
            "number": 123,
            "head_sha": head_sha,
            "kind": review_reads.STEWARD_READ,
            "reader": "reviewer",
            "verdict": "pass",
            **over,
        }
    )


def _ahead(*paths):
    return {"status": "ahead", "files": [{"filename": path} for path in paths]}


def test_a_head_nobody_read_is_action_required_and_a_read_flips_the_same_check(hub):
    [first] = _poll(hub)
    assert (first["status"], first["conclusion"]) == ("review_head_posted", "action_required")
    [run] = hub.api.own()
    assert run["head_sha"] == HEAD
    assert run["output"]["title"] == "No review read recorded at this head"
    assert run["details_url"] == f"https://github.com/{REPO}/pull/123"

    _read(HEAD)
    [second] = _poll(hub)
    assert second["conclusion"] == "success"
    [run] = hub.api.own()
    assert run["conclusion"] == "success"
    assert [method for method, _ in hub.api.writes] == ["POST", "PATCH"]


def test_an_unchanged_answer_makes_no_github_write(hub):
    _poll(hub)
    hub.api.writes.clear()
    assert [r["status"] for r in _poll(hub)] == ["review_head_unchanged"]
    assert hub.api.writes == []


@pytest.mark.parametrize(
    ("compare", "conclusion"),
    [
        (_ahead("src/unified_agent/a.py"), "success"),  # delta-clean
        (_ahead("deploy/gcp/x.sh"), "failure"),  # an uncovered red-tier delta
        ({"status": "diverged"}, "failure"),  # the #1327 class: rebased off every read
        (_ahead(*(f"f{i}.py" for i in range(300))), "failure"),  # too large to prove
    ],
)
def test_a_moved_head_takes_its_conclusion_from_the_delta(hub, compare, conclusion):
    _read(READ_HEAD)
    hub.compare[(READ_HEAD, HEAD)] = compare
    [result] = _poll(hub)
    assert result["conclusion"] == conclusion


def test_a_read_its_reader_rejected_publishes_failure_until_a_pass_replaces_it(hub):
    """#2420: the published check follows the verdict, not only that a read exists."""
    _read(HEAD, verdict="fix-first: cap missing")
    [result] = _poll(hub)
    assert result["conclusion"] == "failure"
    [run] = hub.api.own()
    assert "fix-first: cap missing" in run["output"]["title"]
    _read(HEAD, verdict="pass")
    assert _poll(hub)[0]["conclusion"] == "success"


def test_a_moved_head_fails_until_a_covering_delta_read_lands(hub):
    _read(READ_HEAD)
    hub.compare[(READ_HEAD, HEAD)] = _ahead("deploy/gcp/x.sh")
    assert _poll(hub)[0]["conclusion"] == "failure"
    _read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["deploy/gcp/x.sh"])
    assert _poll(hub)[0]["conclusion"] == "success"
    assert len(hub.api.own()) == 1


def test_a_new_head_gets_its_own_check_and_the_old_one_stays_put(hub):
    _read(READ_HEAD)
    hub.prs[:] = [_pr(sha=READ_HEAD)]
    assert _poll(hub)[0]["conclusion"] == "success"
    hub.prs[:] = [_pr(sha=HEAD)]
    hub.compare[(READ_HEAD, HEAD)] = _ahead("deploy/gcp/x.sh")
    assert _poll(hub)[0]["conclusion"] == "failure"
    assert {run["head_sha"]: run["conclusion"] for run in hub.api.own()} == {
        READ_HEAD: "success",
        HEAD: "failure",
    }


def test_a_markdown_only_pr_gets_the_check(hub):
    # CI filters this PR out. The poll has no path filter, so it is covered the
    # same way, and the docs delta since its read is clean.
    hub.prs[:] = [_pr(title="docs: fix a typo")]
    _read(READ_HEAD)
    hub.compare[(READ_HEAD, HEAD)] = _ahead("docs/automation.md", "README.md")
    assert _poll(hub)[0]["conclusion"] == "success"
    assert len(hub.api.own()) == 1
    assert "files" not in github_app_worker._PR_FIELDS


def test_a_failed_compare_publishes_nothing_and_the_next_poll_recovers(hub):
    _read(READ_HEAD)
    [result] = _poll(hub)
    assert (result["status"], result["error"]) == ("error", "ReviewHeadUnavailable")
    assert NAME in result["detail"]
    assert hub.api.own() == []

    hub.compare[(READ_HEAD, HEAD)] = _ahead("src/unified_agent/a.py")
    assert _poll(hub)[0]["conclusion"] == "success"


def test_a_lost_post_response_is_adopted_not_duplicated(hub):
    hub.api.lose_response = True
    [result] = _poll(hub)
    assert (result["status"], result["error"]) == ("error", "TimeoutError")
    assert len(hub.api.own()) == 1

    [recovered] = _poll(hub)
    assert recovered["status"] == "review_head_posted"
    assert len(hub.api.own()) == 1
    assert [method for method, _ in hub.api.writes] == ["POST", "PATCH"]


def test_a_refused_update_of_a_gone_run_recreates_it_from_a_lookup(hub):
    _poll(hub)
    hub.api.runs.clear()
    hub.api.refuse.append(RuntimeError("404"))
    _read(HEAD)
    assert _poll(hub)[0]["status"] == "error"
    assert _poll(hub)[0]["conclusion"] == "success"
    assert len(hub.api.own()) == 1


def test_a_listing_that_cannot_prove_absence_creates_nothing(hub):
    hub.api.listing_fails = True
    assert _poll(hub)[0]["error"] == "ReconcileIncomplete"
    assert hub.api.writes == []


def test_a_foreign_run_with_the_same_name_is_not_ours(hub):
    hub.api.runs[7] = {"id": 7, "name": NAME, "head_sha": HEAD, "external_id": "other"}
    _poll(hub)
    assert hub.api.runs[7].get("conclusion") is None
    assert len(hub.api.own()) == 2


def test_one_head_failing_does_not_stop_the_next_or_the_ledger(hub):
    other = "c" * 40
    hub.prs[:] = [_pr(sha=HEAD, number=123), _pr(sha=other, number=124)]
    hub.api.refuse.append(RuntimeError("500"))
    results = _run(hub)
    assert sum(r.get("status") == "pending_approval" for r in results) == 2
    assert [
        r["status"] for r in results if str(r.get("event_id", "")).startswith("review-head:")
    ] == ["error"]
    assert {run["head_sha"] for run in hub.api.own()} == {other}


def test_a_backoff_stops_the_pass_and_reads_as_one(hub):
    hub.prs[:] = [_pr(sha=HEAD, number=123), _pr(sha="c" * 40, number=124)]
    hub.api.refuse.append(github_app.GitHubBackoff(60, sent=True))
    last = _run(hub)[-1]
    assert last["event_id"] == review_head_status.EVENT_ID
    assert last["status"] != "error"
    assert hub.api.own() == []


def test_two_open_prs_on_one_head_share_one_check_at_the_worse_answer(hub):
    hub.prs[:] = [_pr(number=123), _pr(number=124)]
    _read(HEAD, number=123)
    [result] = _poll(hub)
    assert (result["number"], result["conclusion"]) == (124, "action_required")
    assert len(hub.api.own()) == 1


def test_a_shared_head_loses_its_success_when_a_sibling_cannot_be_checked(hub):
    hub.prs[:] = [_pr(number=123)]
    _read(HEAD, number=123)
    assert _poll(hub)[0]["conclusion"] == "success"

    hub.prs[:] = [_pr(number=123), _pr(number=124)]
    _read(READ_HEAD, number=124)  # its compare has no answer in this fixture
    [result] = _poll(hub)
    assert (result["number"], result["conclusion"]) == (124, "action_required")
    assert len(hub.api.own()) == 1


def _publish(hub, number=123, head_sha=HEAD):
    return github_app_worker.publish_review_head(
        repo=REPO, number=number, head_sha=head_sha, transport=hub.api, run_gh=hub.gh
    )


def test_a_read_after_the_merge_is_published_without_the_poll(hub):
    """#2410: once merged the head leaves the poll's set, so the read publishes it."""
    hub.prs[:] = []  # merged: no open PR lists this head any more
    _read(HEAD)

    def publish():
        return _publish(hub)

    result = publish()
    assert (result["status"], result["conclusion"]) == ("review_head_posted", "success")
    [run] = hub.api.own()
    assert (run["head_sha"], run["conclusion"]) == (HEAD, "success")

    # The marker from the first publish makes a second one a no-op.
    hub.api.writes.clear()
    assert publish()["status"] == "review_head_unchanged"
    assert hub.api.writes == []


def test_a_publish_that_cannot_be_answered_reports_error_and_keeps_the_row(hub):
    _read(READ_HEAD)
    # No compare entry for READ_HEAD..HEAD: GitHub cannot say, so nothing is
    # published and the caller sees the error instead of a conclusion.
    result = github_app_worker.publish_review_head(
        repo=REPO, number=123, head_sha=HEAD, transport=hub.api, run_gh=hub.gh
    )
    assert (result["status"], result["error"]) == ("error", "ReviewHeadUnavailable")
    assert hub.api.own() == []
    assert len(review_reads.for_pr(REPO, 123)) == 1


def test_a_read_cannot_clear_a_head_an_unread_open_sibling_shares(hub):
    """#2410: the check is per head, so one read must not answer for a sibling."""
    hub.prs[:] = [_pr(number=123), _pr(number=124)]
    _read(HEAD, number=123)
    result = _publish(hub)
    assert (result["number"], result["conclusion"]) == (124, "action_required")
    [run] = hub.api.own()
    assert run["conclusion"] == "action_required"


def test_a_publish_that_cannot_list_the_open_siblings_posts_nothing(hub):
    _read(HEAD)
    failing = hub.gh

    def gh(args, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=1, stdout="")
        return failing(args, **kwargs)

    hub.gh = gh
    with pytest.raises(RuntimeError):
        _publish(hub)
    assert hub.api.own() == []


def test_a_publish_for_a_repo_the_connector_does_not_watch_is_skipped(hub):
    result = github_app_worker.publish_review_head(
        repo="example/other", number=1, head_sha=HEAD, transport=hub.api, run_gh=hub.gh
    )
    assert result["status"] == "skipped"
    assert hub.api.own() == []


def test_a_publish_behind_a_long_poll_defers_and_the_next_poll_delivers_it(hub, monkeypatch):
    """#2410: never publish outside the poll lock. A poll holding a stale verdict
    would overwrite the success, and a merged head gets no correcting poll."""
    monkeypatch.setattr(github_app_worker, "_PUBLISH_LOCK_WAIT_S", 0.05)
    hub.prs[:] = []
    _read(HEAD)
    with github_app_worker._poll_lock():
        result = _publish(hub)
    assert result["status"] == "deferred"
    assert hub.api.own() == []

    [polled] = _poll(hub)
    assert (polled["status"], polled["conclusion"]) == ("review_head_posted", "success")
    [run] = hub.api.own()
    assert (run["head_sha"], run["conclusion"]) == (HEAD, "success")


def test_the_poll_leaves_a_delivered_closed_head_and_an_old_read_alone(hub):
    hub.prs[:] = []
    _read(HEAD)
    assert _publish(hub)["status"] == "review_head_posted"
    hub.api.writes.clear()
    assert _poll(hub) == []
    assert hub.api.writes == []

    # Outside the retry window a closed head is no longer the poll's business.
    _read(READ_HEAD, number=200)
    conn = storage.connect()
    conn.execute("UPDATE review_reads SET created_at='2020-01-01T00:00:00+00:00' WHERE number=200")
    conn.commit()
    conn.close()
    assert _poll(hub) == []


def test_a_closed_head_that_cannot_reach_success_is_recomputed_once_per_read(hub):
    """#2417: a legitimate non-success answer has nothing left to deliver, so a
    closed head must not cost a recompute on every poll for 24 hours."""
    hub.prs[:] = []
    hub.compare[(READ_HEAD, HEAD)] = _ahead("src/unified_agent/safety.py")

    def uncovering_delta_read():
        _read(READ_HEAD)
        _read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["docs/x.md"])

    uncovering_delta_read()
    time.sleep(1.1)  # event_log timestamps are whole seconds, and a tie is "before"

    [first] = _poll(hub)
    assert first["status"] == "review_head_posted" and first["conclusion"] != "success"
    assert _poll(hub) == []

    uncovering_delta_read()  # same answer, newer read
    time.sleep(1.1)
    hub.api.writes.clear()
    [again] = _poll(hub)
    assert again["status"] == "review_head_unchanged"
    assert hub.api.writes == []
    assert _poll(hub) == []


def test_a_closed_head_answered_for_an_unread_sibling_clears_when_the_sibling_moves(hub):
    """The check belongs to the head, so an open sibling's answer can land on a
    closed PR's head. That answer says nothing about the closed PR's own read."""
    hub.prs[:] = [_pr(sha=HEAD, number=124)]
    _read(HEAD, number=123)
    time.sleep(1.1)
    [shared] = _poll(hub)
    assert shared["number"] == 124 and shared["conclusion"] == "action_required"

    hub.prs[:] = [_pr(sha="d" * 40, number=124)]
    results = {result["head_sha"]: result for result in _poll(hub)}

    assert results[HEAD]["number"] == 123 and results[HEAD]["conclusion"] == "success"


def test_an_uncertain_post_on_a_closed_head_is_retried(hub):
    hub.prs[:] = []
    _read(HEAD)
    review_head_status._record(
        REPO, 123, HEAD, conclusion="", summary="", check_run_id="", uncertain=True
    )

    [result] = _poll(hub)

    assert result["status"] == "review_head_posted" and result["conclusion"] == "success"


def test_the_poll_reads_every_closed_heads_marker_in_one_query(hub, monkeypatch):
    hub.prs[:] = []
    for number, sha in ((200, HEAD), (201, READ_HEAD)):
        _read(sha, number=number)
    opened = []
    real = storage.connect
    monkeypatch.setattr(storage, "connect", lambda *a, **k: opened.append(1) or real(*a, **k))

    review_head_status.unpublished_read_heads(REPO, open_numbers=set())

    assert len(opened) == 1


def test_the_poll_never_swaps_an_open_prs_head_for_an_older_read(hub):
    _read(READ_HEAD, number=123)  # read at an older head; HEAD is what is open now
    hub.compare[(READ_HEAD, HEAD)] = _ahead("src/x.py")
    [result] = _poll(hub)
    assert result["head_sha"] == HEAD


def test_the_switch_stops_new_posts(hub, monkeypatch):
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "0")
    assert _poll(hub) == []
    assert hub.api.writes == []


class FakeStatuses(github_app.GitHubUserTokenTransport):
    def __init__(self):
        super().__init__(lambda: "unused")
        self.posts = []

    def _request(self, method, path, body):
        self.posts.append((method, path, body))
        return {}


def test_user_mode_posts_a_status_under_its_own_context_never_the_ledgers(hub):
    transport = FakeStatuses()
    publish = dict(
        repo=REPO,
        number=123,
        head_sha=HEAD,
        answer={
            "status": "no-read-recorded",
            "conclusion": "action_required",
            "summary": "No review read recorded at this head",
        },
    )
    review_head_status._publish(transport, **publish)
    [(method, path, body)] = transport.posts
    assert (method, path) == ("POST", f"/repos/{REPO}/statuses/{HEAD}")
    assert (body["context"], body["state"]) == (NAME, "error")
    assert body["target_url"].endswith("/pull/123")

    # The stored token round-trips, and an unchanged answer posts nothing.
    last = review_head_status._last_posted(REPO, HEAD)
    assert transport._decode_check_run_id(last["check_run_id"])[0] == HEAD
    review_head_status._publish(transport, **publish)
    assert len(transport.posts) == 1


def test_a_brokered_compare_stays_in_process(monkeypatch):
    calls = []

    def answer(token, path):
        calls.append((token, path))
        return {"status": "identical"}

    monkeypatch.setattr(github_app_worker, "_brokered_github_json", answer)
    run = github_app_worker._brokered_gh("broker-credential")
    result = run(["gh", "api", "repos/o/r/compare/a...b"], capture_output=True)
    assert json.loads(result.stdout) == {"status": "identical"}
    assert calls == [("broker-credential", "/repos/o/r/compare/a...b")]

    def refuse(_token, _path):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(github_app_worker, "_brokered_github_json", refuse)
    assert run(["gh", "api", "repos/o/r/compare/a...b"]).returncode == 1

    def backoff(_token, _path):
        raise github_app.GitHubBackoff(60, sent=True)

    monkeypatch.setattr(github_app_worker, "_brokered_github_json", backoff)
    with pytest.raises(github_app.GitHubBackoff):
        run(["gh", "api", "repos/o/r/compare/a...b"])


def test_review_head_is_not_ci_to_the_held_pr_rule(monkeypatch):
    rollup = [
        {"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"},
        {"context": NAME, "state": "ERROR"},
    ]
    monkeypatch.setattr(held_pr.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(
        held_pr.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=json.dumps({"statusCheckRollup": rollup})
        ),
    )
    conclusions = held_pr.gh_pr_check_conclusions("1")
    assert conclusions == {"CI / test": "SUCCESS"}
    assert held_pr.is_ci_green(conclusions) is True
