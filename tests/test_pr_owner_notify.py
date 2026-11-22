"""A task's claimant hears about its pull request, once per head (task #2396)."""

import json

import pytest

from unified_agent import pr_owner_notify as notify
from unified_agent import pr_watch, storage
from unified_agent.pr_models import PREvent, PRSnapshot
from unified_agent.tools.tasks import task_add

REPO = "owner/repo"
OWNER = "Desktop_Agent2"
HEAD = "a" * 40
URL = f"https://github.com/{REPO}/pull/7"


def _task(*, evidence=URL, claimed_by=OWNER, status="doing") -> int:
    tid = int(task_add("work")["id"])
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, claimed_by=?, evidence_ref=? WHERE id=?",
            (status, claimed_by, evidence, tid),
        )
        conn.commit()
    return tid


def _event(kind="ci_failed", *, head=HEAD, rationale="CI / gates", **snap) -> PREvent:
    fields = {"number": 7, "title": "t", "url": URL, "state": "OPEN", "head_sha": head, **snap}
    return PREvent(REPO, kind, PRSnapshot(**fields), rationale=rationale)


def _mail(to=OWNER) -> list[dict]:
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel LIKE 'agent.message.%' ORDER BY rowid"
        ).fetchall()
    found = [json.loads(r[0]) for r in rows]
    return [m for m in found if m.get("to_agent") == to]


def _facts() -> list[dict]:
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY rowid", (notify.CHANNEL,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_a_ci_failure_messages_the_claimant_once_per_head():
    tid = _task()
    assert notify.notify([_event()]) == {"sent": 1, "skipped": 0}
    assert notify.notify([_event()]) == {"sent": 0, "skipped": 0}
    mail = _mail()
    assert len(mail) == 1 and mail[0]["task_id"] == tid
    assert (
        "#7" in mail[0]["body"] and "CI / gates" in mail[0]["body"] and HEAD[:8] in mail[0]["body"]
    )
    notify.notify([_event(head="b" * 40)])
    assert len(_mail()) == 2


@pytest.mark.parametrize(
    ("kind", "snap", "word"),
    [
        ("merged", {"merge_sha": "c" * 40, "state": "MERGED"}, "merged"),
        ("closed", {"state": "CLOSED"}, "closed"),
        ("reviewed", {"last_review_state": "CHANGES_REQUESTED", "last_review_at": "t1"}, "Changes"),
    ],
)
def test_each_change_that_needs_the_owner_is_sent(kind, snap, word):
    _task()
    assert notify.notify([_event(kind, rationale="", **snap)])["sent"] == 1
    assert word in _mail()[0]["body"]


def test_a_second_changes_requested_review_on_one_head_is_new():
    _task()
    for at in ("t1", "t2"):
        event = _event("reviewed", last_review_state="CHANGES_REQUESTED", last_review_at=at)
        assert notify.notify([event])["sent"] == 1


@pytest.mark.parametrize(
    "event",
    [
        _event("reviewed", last_review_state="APPROVED", last_review_at="t1"),
        _event("opened"),
        _event("synchronize"),
        _event("ready_for_review"),
    ],
)
def test_other_changes_are_skipped_with_no_signal(event):
    _task()
    assert notify.notify([event]) == {"sent": 0, "skipped": 1}
    assert _mail() == [] and _facts()[0]["reason"] == notify.NO_SIGNAL


def test_a_pr_with_no_claimed_task_records_why():
    assert notify.notify([_event()]) == {"sent": 0, "skipped": 1}
    assert _facts()[0]["reason"] == notify.NO_OWNER


@pytest.mark.parametrize(
    "task",
    [
        {"claimed_by": None},
        {"status": "done"},
        {"status": "cancelled"},
        {"evidence": "https://github.com/owner/other/pull/7"},
        {"evidence": "https://github.com/owner/repo/pull/70"},
    ],
)
def test_only_a_live_claim_on_this_exact_pr_counts(task):
    _task(**task)
    notify.notify([_event()])
    assert _mail() == [] and _facts()[0]["reason"] == notify.NO_OWNER


def test_two_tasks_on_one_pr_are_not_guessed_between():
    _task()
    _task(claimed_by="Desktop_Agent4")
    assert notify.notify([_event()]) == {"sent": 0, "skipped": 1}
    assert _facts()[0]["reason"] == notify.AMBIGUOUS


def test_the_skip_is_recorded_once():
    notify.notify([_event()])
    notify.notify([_event()])
    assert len(_facts()) == 1


def test_a_send_after_an_earlier_skip_is_recorded_as_sent():
    assert notify.notify([_event()]) == {"sent": 0, "skipped": 1}
    _task()
    assert notify.notify([_event()]) == {"sent": 1, "skipped": 0}
    assert notify.notify([_event()]) == {"sent": 0, "skipped": 0}
    assert [f["outcome"] for f in _facts()] == ["skipped", "sent"]
    assert len(_mail()) == 1


def test_a_refused_send_does_not_stop_the_rest():
    _task(claimed_by="Not A Label!")
    other = _event(head="d" * 40)
    other.snapshot.number = 8
    other.snapshot.url = other.snapshot.url.replace("/7", "/8")
    _task(evidence=other.snapshot.url)
    assert notify.notify([_event(), other])["sent"] == 1
    assert _facts()[0]["reason"] == notify.REFUSED
    assert _facts()[0]["detail"].startswith("ToolError: ")
    assert "detail" not in _facts()[1]


def test_a_closed_roster_without_the_sender_records_why(monkeypatch):
    # Task #2434: the hub's closed roster lacked "pr-watch", so every notice was
    # refused and the ledger only said send_refused.
    monkeypatch.setenv("UA_AGENT_LABELS", f"{OWNER}:Desktop_Agent1")
    _task()
    assert notify.notify([_event()])["sent"] == 0
    fact = _facts()[0]
    assert fact["reason"] == notify.REFUSED
    assert "from_agent 'pr-watch' is not a known agent label" in fact["detail"]
    assert len(fact["detail"]) <= 200
    monkeypatch.setenv("UA_AGENT_LABELS", f"{OWNER}:Desktop_Agent1:pr-watch")
    assert notify.notify([_event(head="e" * 40)])["sent"] == 1
    assert len(_mail()) == 1


def _check(name, conclusion, workflow="CI"):
    return {"name": name, "workflowName": workflow, "conclusion": conclusion}


def test_failing_checks_names_only_real_failures():
    rollup = [
        _check("gates", "FAILURE"),
        _check("test", "SUCCESS"),
        _check("lint", "CANCELLED"),
        _check("boot", "TIMED_OUT"),
        {"context": "unified-agent/review-head", "state": "FAILURE"},
        "not a check",
    ]
    assert notify.failing_checks(rollup) == ("CI / boot", "CI / gates")


def test_check_names_cannot_carry_markup_or_length():
    rollup = [_check("$(rm -rf ~)`x`" + "y" * 200, "FAILURE", workflow="")]
    names = notify.failing_checks(rollup)
    assert "$" not in names[0] and "`" not in names[0] and len(names[0]) <= 60


def test_a_retried_check_that_passed_is_not_a_failure():
    assert notify.failing_checks([_check("gates", "FAILURE"), _check("gates", "SUCCESS")]) == ()


class Gh:
    def __init__(self, rollups):
        self.rollups = rollups

    def __call__(self, cmd):
        assert "statusCheckRollup" in cmd[-1]
        return json.dumps(self.rollups)


def _snapshot(head=HEAD):
    return {7: PRSnapshot(number=7, title="t", url=URL, state="OPEN", head_sha=head)}


def test_ci_failures_reads_open_prs_and_uses_the_live_head():
    rollups = [{"number": 7, "headRefOid": HEAD, "statusCheckRollup": [_check("g", "FAILURE")]}]
    found = notify.ci_failures(REPO, Gh(rollups), _snapshot())
    assert [(e.kind, e.snapshot.number, e.rationale) for e in found] == [("ci_failed", 7, "CI / g")]


def test_ci_failures_ignores_a_head_the_poll_did_not_see():
    rollups = [{"number": 7, "headRefOid": "e" * 40, "statusCheckRollup": [_check("g", "FAILURE")]}]
    assert notify.ci_failures(REPO, Gh(rollups), _snapshot()) == []


class FullGh:
    """`gh pr list`: the poll's own fields, or the CI rollup."""

    def __init__(self, prs, rollup=None, fail_ci=False):
        self.prs, self.rollup, self.fail_ci = prs, rollup or [], fail_ci

    def __call__(self, cmd, **_kwargs):
        class Done:
            returncode = 0
            stderr = ""
            stdout = ""

        done = Done()
        if "statusCheckRollup" in cmd[-1]:
            if self.fail_ci:
                done.returncode, done.stderr = 1, "boom"
                return done
            done.stdout = json.dumps(self.rollup)
        else:
            done.stdout = json.dumps(self.prs)
        return done


def _pr(state="OPEN", **over):
    return {
        "number": 7,
        "title": "t",
        "url": URL,
        "state": state,
        "headRefOid": HEAD,
        "reviews": [],
        **over,
    }


@pytest.fixture
def poller(tmp_path, monkeypatch):
    from unified_agent import config

    home = tmp_path / "ua_home"
    home.mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    monkeypatch.setattr(config, "data_home", lambda: home)
    pr_watch.save_cursor(home / pr_watch.CURSOR_FILE, {REPO: pr_watch.parse_prs([_pr()])})
    return home


def test_the_poll_messages_a_ci_failure_with_no_other_pr_event(poller):
    _task()
    rollup = [{"number": 7, "headRefOid": HEAD, "statusCheckRollup": [_check("g", "FAILURE")]}]
    assert pr_watch.run(repos=[REPO], run_gh=FullGh([_pr()], rollup)) == []
    assert len(_mail()) == 1


def test_the_poll_messages_a_merge(poller):
    _task()
    merged = _pr("MERGED", mergedAt="2026-11-09T00:00:00Z", mergeCommit={"oid": "c" * 40})
    pr_watch.run(repos=[REPO], run_gh=FullGh([merged]))
    assert "merged" in _mail()[0]["body"]


def test_a_dry_run_sends_nothing(poller):
    _task()
    rollup = [{"number": 7, "headRefOid": HEAD, "statusCheckRollup": [_check("g", "FAILURE")]}]
    pr_watch.run(dry_run=True, repos=[REPO], run_gh=FullGh([_pr()], rollup))
    assert _mail() == [] and _facts() == []


def test_a_failed_ci_read_never_costs_the_poll(poller, capsys):
    _task()
    pr_watch.run(repos=[REPO], run_gh=FullGh([_pr()], fail_ci=True))
    assert _mail() == []
    assert (poller / pr_watch.CURSOR_FILE).exists()
    assert "notify" in capsys.readouterr().err
