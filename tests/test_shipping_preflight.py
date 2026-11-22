"""Exact-head human approval is a separate gate from a nominal GitHub status."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from review_evidence_fixture import review_evidence

from unified_agent import (
    config,
    events,
    github_app,
    review_holds,
    review_reads,
    shipping_preflight_cli,
    storage,
)
from unified_agent.tools.tasks import task_add

HEAD = "a" * 40
OLD = "b" * 40
REPO = "example/project"


_DEFAULT = object()


def approval(
    *,
    resolution=True,
    resolver_verified=_DEFAULT,
    event_overrides=None,
    originator=_DEFAULT,
    **overrides,
):
    row = {
        "tool": github_app.TOOL_NAME,
        "payload": {
            "connector": github_app.GITHUB_APP_CONNECTOR,
            "repo": REPO,
            "number": 123,
            "head_sha": HEAD,
            "base_branch": "main",
            "state": "OPEN",
            "merged": False,
        },
        "requested_by": "Desktop_Agent2",
        "resolved_by": "Desktop_Person",
        "status": "approved",
        "created_at": storage.now(),
        "resolved_at": storage.now(),
        "updated_at": storage.now(),
    }
    row.update(overrides)
    if "payload" not in overrides:
        row["payload"]["independent_review"] = review_evidence(REPO, 123, HEAD)
    conn = storage.connect()
    try:
        inserted = conn.execute(
            "INSERT INTO approvals(tool,payload,requested_by,resolved_by,status,created_at,resolved_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                row["tool"],
                json.dumps(row["payload"]),
                row["requested_by"],
                row["resolved_by"],
                row["status"],
                row["created_at"],
                row["resolved_at"],
                row["updated_at"],
            ),
        )
        aid = inserted.lastrowid
        if resolution:
            evidence = {
                "approval_id": aid,
                "tool": row["tool"],
                "decision": "approve",
                "status": "approved",
                "prior_status": "pending",
                "requested_by": row["requested_by"],
                "resolved_by": row["resolved_by"],
                "resolver_verified": row["resolved_by"]
                if resolver_verified is _DEFAULT
                else resolver_verified,
                "resolved_at": row["resolved_at"],
                "payload_sha256": hashlib.sha256(json.dumps(row["payload"]).encode()).hexdigest(),
            }
            evidence.update(event_overrides or {})
            events.record_event_once_on_connection(
                conn,
                "agent.approval.resolved",
                payload=evidence,
                originator=row["resolved_by"] if originator is _DEFAULT else originator,
                dedupe_key=f"approval-resolution:{aid}:approved",
            )
        conn.commit()
        return aid
    finally:
        conn.close()


def gh(**overrides):
    row = {"headRefOid": HEAD, "state": "OPEN", "isDraft": False, "baseRefName": "main"}
    row.update(overrides)
    row.setdefault("statusCheckRollup", _required_ci())

    def invoke(args, **kwargs):
        assert args[:3] == ["gh", "pr", "view"]
        assert args[3] == "123" and args[4:6] == ["--repo", REPO]
        assert kwargs["timeout"] <= 30
        return SimpleNamespace(returncode=0, stdout=json.dumps(row))

    return invoke


def check(approval_id, run_gh=None):
    return shipping_preflight_cli.preflight(
        repo=REPO, number=123, head_sha=HEAD, approval_id=approval_id, run_gh=run_gh or gh()
    )


# --- the narrow head-moved rule (task #2259, Operator 2026-10-31) -------------

READ_HEAD = "c" * 40


def compare(pairs):
    """Fake the ``gh api repos/{repo}/compare/{base}...{head}`` endpoint."""

    def invoke(args, **kwargs):
        assert args[:2] == ["gh", "api"], args
        base, head = args[2].split("/compare/", 1)[1].split("...")
        entry = pairs.get((base, head))
        if entry is None:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(entry))

    return invoke


def gate(pairs, *, head_sha=HEAD):
    return shipping_preflight_cli.review_head_gate(
        repo=REPO, number=123, head_sha=head_sha, run_gh=compare(pairs)
    )


def never(_args, **_kwargs):  # pragma: no cover - asserts by being called
    pytest.fail("the gate needed a comparison it should not have needed")


def record_read(head_sha, **over):
    kwargs: dict[str, Any] = dict(
        repo=REPO,
        number=123,
        head_sha=head_sha,
        kind=review_reads.STEWARD_READ,
        reader="Desktop_Agent1",
        verdict="pass",
    )
    kwargs.update(over)
    return review_reads.record(**kwargs)


def test_a_read_at_the_merging_head_needs_no_comparison():
    record_read(HEAD)
    out = shipping_preflight_cli.review_head_gate(
        repo=REPO, number=123, head_sha=HEAD, run_gh=never
    )
    assert out["status"] == "read-at-head" and out["read_head"] == HEAD
    assert out["readers"] == ["Desktop_Agent1"]


def test_every_reader_at_the_head_is_named():
    """Task #2641: one row per reader, so an independent read stays visible."""
    record_read(HEAD)
    record_read(HEAD, reader="Desktop_Agent5")
    out = shipping_preflight_cli.review_head_gate(
        repo=REPO, number=123, head_sha=HEAD, run_gh=never
    )
    assert out["status"] == "read-at-head"
    assert out["readers"] == ["Desktop_Agent5", "Desktop_Agent1"]


def test_a_rejecting_read_cannot_be_overwritten_away_by_a_later_pass():
    """The second reader no longer replaces the first row, so a rejection at
    this head still holds it — the direction #2641 was filed for."""
    record_read(HEAD, reader="Desktop_Agent5", verdict="fix-first: unread variable")
    record_read(HEAD, reader="Desktop_Agent1", verdict="pass")
    with pytest.raises(shipping_preflight_cli.ReadRejected):
        shipping_preflight_cli.review_head_gate(repo=REPO, number=123, head_sha=HEAD, run_gh=never)


def test_no_recorded_read_is_not_a_refusal():
    out = shipping_preflight_cli.review_head_gate(
        repo=REPO, number=123, head_sha=HEAD, run_gh=never
    )
    assert out["status"] == "no-read-recorded"


def test_a_non_red_delta_after_the_read_is_allowed():
    record_read(READ_HEAD)
    out = gate({(READ_HEAD, HEAD): {"status": "ahead", "files": [{"filename": "src/a.py"}]}})
    assert out["status"] == "delta-clean" and out["read_head"] == READ_HEAD


def test_delta_clean_names_every_reader_at_the_read_head():
    """Task #2641: the readers belong to the read's head, not the merging head.

    Two readers at C, a docs-only push to D, and the gate used to return
    ``readers: []`` because it looked at D; the merge receipt then fell back to
    the single selected reader and dropped the second.
    """
    record_read(READ_HEAD)
    record_read(READ_HEAD, reader="Desktop_Agent5")
    out = gate({(READ_HEAD, HEAD): {"status": "ahead", "files": [{"filename": "src/a.py"}]}})
    assert out["status"] == "delta-clean"
    assert out["readers"] == ["Desktop_Agent5", "Desktop_Agent1"]


def test_a_red_tier_delta_after_the_read_is_held():
    record_read(READ_HEAD)
    pairs = {
        (READ_HEAD, HEAD): {
            "status": "ahead",
            "files": [{"filename": "src/a.py"}, {"filename": "deploy/gcp/install.sh"}],
        }
    }
    with pytest.raises(shipping_preflight_cli.PreflightError) as exc:
        gate(pairs)
    assert "red-tier" in str(exc.value) and "deploy/gcp/install.sh" in str(exc.value)


def test_renaming_a_red_tier_file_away_is_held():
    """GitHub reports a rename under its new name; the old path is what is red-tier."""
    record_read(READ_HEAD)
    renamed = {
        "filename": "src/unified_agent/notes.txt",
        "previous_filename": "src/unified_agent/safety.py",
        "status": "renamed",
    }
    with pytest.raises(shipping_preflight_cli.PreflightError, match="safety.py"):
        gate({(READ_HEAD, HEAD): {"status": "ahead", "files": [renamed]}})


def test_a_recorded_delta_read_clears_a_red_tier_delta():
    record_read(READ_HEAD)
    record_read(
        HEAD,
        kind=review_reads.DELTA_READ,
        from_sha=READ_HEAD,
        files=["src/a.py", "deploy/gcp/install.sh"],
    )
    pairs = {
        (READ_HEAD, HEAD): {
            "status": "ahead",
            "files": [{"filename": "deploy/gcp/install.sh"}],
        }
    }
    out = gate(pairs)
    assert out["status"] == "delta-read" and out["read_head"] == READ_HEAD
    assert out["red_tier_delta"] == ["deploy/gcp/install.sh"]


def test_a_delta_read_that_misses_a_red_tier_path_is_held():
    record_read(READ_HEAD)
    record_read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["src/a.py"])
    pairs = {
        (READ_HEAD, HEAD): {
            "status": "ahead",
            "files": [{"filename": "src/a.py"}, {"filename": ".env"}],
        }
    }
    with pytest.raises(shipping_preflight_cli.PreflightError) as exc:
        gate(pairs)
    assert ".env" in str(exc.value)


def _unanchored_delta_read(head_sha, from_sha, files):
    """A delta-read row written around ``review_reads.record``, which refuses one
    with no full read at its base: a legacy row or a direct database write."""
    conn = storage.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO review_reads (repo, number, head_sha, from_sha, kind, reader, "
                "verdict, files_json, created_at) VALUES (?, 123, ?, ?, ?, 'reviewer', 'pass', "
                "?, ?)",
                (
                    REPO,
                    head_sha,
                    from_sha,
                    review_reads.DELTA_READ,
                    json.dumps(files),
                    datetime.now(UTC).isoformat(),
                ),
            )
    finally:
        conn.close()


def test_a_delta_read_without_a_full_read_at_its_base_is_not_counted():
    # Without the check this delta read covers deploy/x.sh and the head passes,
    # although nobody read the tree at its base.
    record_read(READ_HEAD)
    _unanchored_delta_read(HEAD, OLD, ["deploy/x.sh"])
    red = {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}
    with pytest.raises(shipping_preflight_cli.PreflightError, match="deploy/x.sh"):
        gate({(OLD, HEAD): red, (READ_HEAD, HEAD): red})


def test_only_unanchored_delta_reads_hold_rather_than_read_as_unread():
    _unanchored_delta_read(HEAD, OLD, ["deploy/x.sh"])
    with pytest.raises(shipping_preflight_cli.PreflightError, match="no full read is recorded"):
        shipping_preflight_cli.review_head_gate(repo=REPO, number=123, head_sha=HEAD, run_gh=never)


MID = "e" * 40
_RED_AB = {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}
_DOCS = {"status": "ahead", "files": [{"filename": "docs/a.md"}]}


@pytest.mark.parametrize(
    "a_to_b, a_to_c, message",
    [
        # The delta read at B named no red-tier path, but A..B changed one.
        (_RED_AB, {"status": "ahead", "files": _RED_AB["files"] + _DOCS["files"]}, "x.sh"),
        # A is not an ancestor of B (or of C): nothing proves the delta.
        ({"status": "diverged"}, {"status": "diverged"}, "none is an ancestor"),
        # A..B is past GitHub's file cap, so it cannot be proven covered.
        (
            {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(300)]},
            {"status": "ahead", "files": [{"filename": f"f{i}.py"} for i in range(301)]},
            "too large",
        ),
    ],
)
def test_a_delta_read_that_fails_at_its_head_is_not_an_ancestor_after_a_push(
    a_to_b, a_to_c, message
):
    record_read(READ_HEAD)
    record_read(MID, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["src/a.py"])
    pairs = {(READ_HEAD, MID): a_to_b, (MID, HEAD): _DOCS, (READ_HEAD, HEAD): a_to_c}
    with pytest.raises(shipping_preflight_cli.PreflightError):
        gate(pairs, head_sha=MID)
    # After a docs-only push the delta read at B must not stand in for A..B.
    with pytest.raises(shipping_preflight_cli.PreflightError, match=message):
        gate(pairs)


def strict(pairs, *, head_sha=HEAD):
    return shipping_preflight_cli.review_head_check(
        repo=REPO, number=123, head_sha=head_sha, run_gh=compare(pairs)
    )


def test_the_strict_check_never_passes_a_head_nobody_read():
    # The gate lets this through for the preflight; the required check must not.
    assert gate({})["status"] == "no-read-recorded"
    out = strict({})
    assert (out["conclusion"], out["summary"]) == (
        "action_required",
        "No review read recorded at this head",
    )


def test_the_strict_check_maps_each_passing_gate_answer_to_success():
    record_read(READ_HEAD)
    clean = {(READ_HEAD, HEAD): {"status": "ahead", "files": [{"filename": "src/a.py"}]}}
    assert (strict(clean)["status"], strict(clean)["conclusion"]) == ("delta-clean", "success")
    red = {(READ_HEAD, HEAD): {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}}
    assert strict(red)["conclusion"] == "failure"
    record_read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["deploy/x.sh"])
    assert (strict(red)["status"], strict(red)["conclusion"]) == ("delta-read", "success")
    record_read(HEAD)
    out = strict({})
    assert (out["status"], out["conclusion"]) == ("read-at-head", "success")
    assert out["summary"] == "Read at this head (steward-read)"


def test_the_strict_check_fails_a_rebase_off_every_read_within_the_title_limit():
    record_read(READ_HEAD)
    out = strict({(READ_HEAD, HEAD): {"status": "diverged"}})
    assert (out["status"], out["conclusion"]) == ("head-moved", "failure")
    assert out["summary"].startswith("Recorded reads exist") and len(out["summary"]) <= 140


@pytest.mark.parametrize("entry", [None, {"status": "ahead"}, ["not", "a", "dict"]])
def test_the_strict_check_has_no_answer_when_github_has_none(entry):
    record_read(READ_HEAD)
    pairs = {} if entry is None else {(READ_HEAD, HEAD): entry}
    with pytest.raises(shipping_preflight_cli.ReviewHeadUnavailable):
        strict(pairs)


def test_an_oversized_comparison_is_a_failure_not_an_outage():
    record_read(READ_HEAD)

    def huge(_args, **_kwargs):
        return SimpleNamespace(
            returncode=0, stdout=" " * (shipping_preflight_cli._COMPARE_JSON_LIMIT + 1)
        )

    out = shipping_preflight_cli.review_head_check(
        repo=REPO, number=123, head_sha=HEAD, run_gh=huge
    )
    assert out["conclusion"] == "failure"


def test_a_rebased_head_off_every_recorded_read_fails_closed():
    record_read(READ_HEAD)
    with pytest.raises(shipping_preflight_cli.PreflightError, match="none is an ancestor"):
        gate({(READ_HEAD, HEAD): {"status": "diverged"}})


def test_an_unavailable_comparison_holds():
    record_read(READ_HEAD)
    with pytest.raises(shipping_preflight_cli.PreflightError, match="comparison is unavailable"):
        gate({})


@pytest.mark.parametrize("files", [None, "src/a.py", {"filename": "deploy/x.sh"}])
def test_an_ahead_comparison_without_a_file_list_cannot_prove_itself_clean(files):
    record_read(READ_HEAD)
    entry = {"status": "ahead"} if files is None else {"status": "ahead", "files": files}
    with pytest.raises(shipping_preflight_cli.PreflightError, match="comparison is unavailable"):
        gate({(READ_HEAD, HEAD): entry})


def test_an_identical_comparison_is_clean_without_a_file_list():
    record_read(READ_HEAD)
    out = gate({(READ_HEAD, HEAD): {"status": "identical"}})
    assert out["status"] == "delta-clean"


def test_a_change_set_at_the_file_cap_cannot_prove_itself_clean():
    record_read(READ_HEAD)
    files = [{"filename": f"src/f{i}.py"} for i in range(300)]
    with pytest.raises(shipping_preflight_cli.PreflightError, match="too large"):
        gate({(READ_HEAD, HEAD): {"status": "ahead", "files": files}})


def test_the_gate_holds_inside_a_full_preflight():
    aid = approval()
    record_read(READ_HEAD)
    pairs = {(READ_HEAD, HEAD): {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}}
    view = gh()
    cmp_ = compare(pairs)

    def both(args, **kwargs):
        return cmp_(args, **kwargs) if args[:2] == ["gh", "api"] else view(args, **kwargs)

    with pytest.raises(shipping_preflight_cli.PreflightError, match="red-tier"):
        check(aid, run_gh=both)


# --- a read's verdict decides, not only its existence (task #2420) ------------

_CLEAN = {"status": "ahead", "files": [{"filename": "docs/a.md"}]}
_RED = {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}


@pytest.mark.parametrize(
    "verdict",
    # Wording the hub's review_reads table actually holds, plus the policy writer's.
    [
        "pass",
        "PASS",
        "passed",
        "pass-after-delta",
        "pass with notes",
        "ship",
        "SHIP: full read at head",
        "Code PASS; covering CI pending",
        "delta pass: one tested fix",
        "validated",
    ],
)
def test_an_affirmative_verdict_at_the_head_passes(verdict):
    record_read(HEAD, verdict=verdict)
    assert strict({})["conclusion"] == "success"


@pytest.mark.parametrize(
    "verdict",
    [
        "fail",
        "fix-first: cancel after reveal hides evidence",
        "hold",
        "HOLD: red-tier approval missing",
        "pass?",
        "not pass",
        "do not ship",
        "web job 20->30 min; only the web job changed",
    ],
)
def test_a_rejected_or_unrecognised_verdict_at_the_head_fails_and_is_kept(verdict):
    record_read(HEAD, verdict=verdict)
    with pytest.raises(shipping_preflight_cli.PreflightError, match="not a pass"):
        gate({})
    out = strict({})
    assert out["conclusion"] == "failure" and verdict[:40] in out["summary"]
    # The honest negative record is evidence and stays as written.
    assert [row["verdict"] for row in review_reads.for_head(REPO, 123, HEAD)] == [verdict]


def test_a_pipeline_pass_cannot_outvote_a_rejection_at_the_same_head():
    record_read(HEAD, verdict="fix-first: failover sends before CAS")
    record_read(HEAD, kind=review_reads.POLICY, reader="review-cron", verdict="validated")
    assert strict({})["conclusion"] == "failure"


def test_a_rejected_delta_read_at_the_head_fails():
    record_read(READ_HEAD)
    record_read(
        HEAD,
        kind=review_reads.DELTA_READ,
        from_sha=READ_HEAD,
        files=["deploy/x.sh"],
        verdict="fix-first: force field clobbers a locked note",
    )
    out = strict({(READ_HEAD, HEAD): _RED})
    assert out["conclusion"] == "failure" and "fix-first" in out["summary"]


def test_a_delta_read_from_a_rejected_full_read_does_not_count():
    record_read(READ_HEAD, verdict="fix-first: check_py_ast false negative")
    record_read(HEAD, kind=review_reads.DELTA_READ, from_sha=READ_HEAD, files=["deploy/x.sh"])
    out = strict({(READ_HEAD, HEAD): _RED})
    assert out["conclusion"] == "failure" and "fix-first" in out["summary"]


def test_a_clean_push_after_a_rejected_read_fails():
    record_read(READ_HEAD, verdict="fix-first")
    out = strict({(READ_HEAD, HEAD): _CLEAN})
    assert out["conclusion"] == "failure" and "fix-first" in out["summary"]


@pytest.mark.parametrize(
    "mid_to_head",
    [_CLEAN, {"status": "diverged"}],  # a docs push on the rejected head, or a rebase off it
)
def test_a_later_rejection_supersedes_an_older_pass(mid_to_head):
    record_read(READ_HEAD)
    record_read(MID, verdict="fix-first")
    pairs = {(MID, HEAD): mid_to_head, (READ_HEAD, HEAD): _CLEAN}
    out = strict(pairs)
    assert out["conclusion"] == "failure" and "fix-first" in out["summary"]


def test_a_newer_affirmative_read_clears_an_older_rejection():
    record_read(READ_HEAD, verdict="fix-first")
    record_read(MID)
    out = strict({(MID, HEAD): _CLEAN})
    assert (out["status"], out["conclusion"]) == ("delta-clean", "success")
    # A delta read recorded after the rejection, from a passing full read, also clears it.
    record_read(HEAD, kind=review_reads.DELTA_READ, from_sha=MID, files=["deploy/x.sh"])
    out = strict({(MID, HEAD): _RED})
    assert (out["status"], out["conclusion"]) == ("delta-read", "success")


def test_a_full_preflight_refuses_a_head_its_reader_rejected():
    aid = approval()
    record_read(HEAD, verdict="fix-first")
    with pytest.raises(shipping_preflight_cli.PreflightError, match="fix-first"):
        check(aid)


@pytest.mark.parametrize("status", ["approved", "consumed"])
def test_current_human_binding_is_read_only_and_not_consumed(status):
    aid = approval(status=status)
    conn = storage.connect()
    before = [tuple(row) for row in conn.execute("SELECT * FROM approvals")]
    events_before = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    conn.close()
    out = check(aid)
    review = out.pop("independent_review")
    assert review["authors"][0]["model"] == "openai/gpt-6.1-sol"
    assert review["reviewer"]["model"] == "openai/gpt-6-astra"
    assert review["reviewer"]["subject"] == "t3:review-task-1"
    assert "execution_ref" not in review["reviewer"] and "review_ref" not in review
    assert out == {
        "status": "ready",
        "scope": "exact-head-human-attested-review",
        "repo": REPO,
        "number": 123,
        "head_sha": HEAD,
        "approval_id": aid,
        "approval_status": status,
        # No read is recorded for this PR, so the narrow head-moved rule has
        # nothing to compare against and does not refuse it (#2259).
        "review_head": {"status": "no-read-recorded", "read_head": None, "red_tier_delta": []},
    }
    conn = storage.connect()
    assert [tuple(row) for row in conn.execute("SELECT * FROM approvals")] == before
    assert conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == events_before
    conn.close()


def test_an_operator_keep_after_the_approval_refuses_until_released():
    aid = approval()
    pr = f"https://github.com/{REPO}/pull/123"
    task_id = task_add("held", notes="[waiting-for: human-merge]", evidence_ref=pr)["id"]
    review_holds.record(task_id, "keep", actor="Desktop_Operator", reason="wait")
    with pytest.raises(shipping_preflight_cli.PreflightError, match="kept the review hold"):
        check(aid)
    # An approval resolved after the keep supersedes it.
    conn = storage.connect()
    conn.execute("UPDATE review_hold_decisions SET decided_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    assert check(aid)["status"] == "ready"
    review_holds.record(task_id, "release", actor="Desktop_Operator")
    assert check(aid)["status"] == "ready"


def test_a_release_on_one_hold_never_clears_a_keep_on_another():
    aid = approval()
    pr = f"https://github.com/{REPO}/pull/123"
    red = task_add("red", notes="[review: red-tier @ t] deploy/x", evidence_ref=pr)["id"]
    held = task_add("held", notes="[waiting-for: human-merge]", evidence_ref=pr)["id"]
    review_holds.record(red, "keep", actor="Desktop_Operator")
    review_holds.record(held, "release", actor="Desktop_Operator")
    with pytest.raises(shipping_preflight_cli.PreflightError, match=f"task #{red}"):
        check(aid)


@pytest.mark.parametrize("status", ["pending", "denied", "revoked", "expired"])
def test_unapproved_binding_is_refused(status):
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(approval(status=status))


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", OLD),
        ("base_branch", "release"),
        ("base_branch", None),
        ("repo", "other/project"),
        ("number", 456),
        ("connector", "other"),
        ("state", "CLOSED"),
        ("merged", True),
    ],
)
def test_old_or_unrelated_approval_cannot_clear_current_head(field, value):
    payload = {
        "connector": github_app.GITHUB_APP_CONNECTOR,
        "repo": REPO,
        "number": 123,
        "head_sha": HEAD,
        "base_branch": "main",
        "state": "OPEN",
        "merged": False,
        field: value,
    }
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(approval(payload=payload))


@pytest.mark.parametrize(
    "fields",
    [
        {"tool": "different_tool"},
        {"resolved_by": "Desktop_Agent2"},
        {"resolved_by": None},
        {"requested_by": "Operator"},
        {"payload": []},
        {"resolved_at": None, "created_at": "invalid"},
        {"resolved_at": (datetime.now(UTC) - timedelta(days=30)).isoformat()},
    ],
)
def test_invalid_or_nonhuman_grant_is_refused(fields):
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(approval(**fields))


def test_missing_binding_does_not_trust_github_success():
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(999999)


@pytest.mark.parametrize(
    "fields",
    [
        {"headRefOid": OLD},
        {"isDraft": True},
        {"state": "MERGED"},
        {"baseRefName": "other"},
        {},
    ],
)
def test_live_pr_must_still_be_selected_open_head(fields):
    aid = approval()
    invocation = (
        gh(**fields) if fields else lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="{}")
    )
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(aid, invocation)


def test_head_change_during_preflight_invalidates_approval():
    aid = approval()
    seen = 0

    def invoke(args, **kwargs):
        nonlocal seen
        seen += 1
        return gh(headRefOid=HEAD if seen == 1 else OLD)(args, **kwargs)

    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(aid, invoke)
    assert seen == 2


def test_revocation_during_head_refresh_is_refused():
    aid = approval()
    seen = 0

    def invoke(args, **kwargs):
        nonlocal seen
        seen += 1
        if seen == 2:
            conn = storage.connect()
            conn.execute("UPDATE approvals SET status='revoked' WHERE id=?", (aid,))
            conn.commit()
            conn.close()
        return gh()(args, **kwargs)

    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(aid, invoke)


def test_missing_or_retired_store_never_creates_a_local_db(monkeypatch, tmp_path):
    home = tmp_path / "retired"
    home.mkdir()
    monkeypatch.setenv("UA_HOME", str(home))
    for marker in (False, True):
        if marker:
            (home / "NO_LOCAL_MEMORY_DB").touch()
        with pytest.raises(shipping_preflight_cli.PreflightError):
            check(1)
        assert not (home / "memory.db").exists()


def test_database_is_opened_read_only(monkeypatch):
    aid = approval()
    original = sqlite3.connect
    uris = []

    def opened(database, *args, **kwargs):
        assert "mode=ro" in database and kwargs.get("uri") is True
        uris.append(database)
        return original(database, *args, **kwargs)

    monkeypatch.setattr(shipping_preflight_cli.sqlite3, "connect", opened)
    check(aid)
    assert uris


def test_approval_comment_names_its_head_in_visible_and_structured_evidence():
    event = github_app.NormalizedPREvent(
        repo=REPO,
        number=123,
        event_type="synchronize",
        title="fixture",
        url="https://github.com/example/project/pull/123",
        state="OPEN",
        head_sha=HEAD,
        base_branch="main",
        head_branch="fixture",
        author_login="Desktop_Agent2",
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        body = github_app._evidence_comment(
            event,
            {"id": 7, "resolved_by": "Desktop_Person"},
            signed_event_id="fixture-event",
            signature="fixture-signature",
            chain_head="fixture-chain",
        )
    finally:
        config.agent_identity_var.reset(token)
    assert f"Approved head: `{HEAD}`." in body
    evidence = json.loads(body.split("Evidence: ", 1)[1].split("\n", 1)[0])
    assert evidence["head_sha"] == HEAD and evidence["approval_id"] == 7


@pytest.mark.parametrize("status", ["approved", "consumed"])
def test_human_named_row_without_resolution_provenance_is_refused(status):
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(approval(status=status, resolution=False))


@pytest.mark.parametrize("verified", [None, "Desktop_Agent2", "Operator", "unverified-human"])
def test_claimed_human_label_cannot_replace_verified_resolver(verified):
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(approval(resolver_verified=verified))


@pytest.mark.parametrize("originator", [None, "Desktop_Agent2", "Operator"])
def test_resolution_originator_must_match_verified_human(originator):
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(approval(originator=originator))


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_id", True),
        ("approval_id", 999999),
        ("tool", "other"),
        ("decision", "deny"),
        ("status", "denied"),
        ("prior_status", "approved"),
        ("requested_by", "other-requester"),
        ("resolved_by", "Operator"),
        ("resolved_at", "other-time"),
        ("payload_sha256", "0" * 64),
    ],
)
def test_resolution_event_must_bind_original_payload_and_decision(field, value):
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(approval(event_overrides={field: value}))


def test_legacy_unhashed_resolution_is_refused(monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(events.edition, "is_feature_available", lambda _feature: False)
        aid = approval()
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(aid)


def test_corrupt_resolution_entry_hash_is_refused(monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(events, "compute_entry_hash", lambda *_args: "0" * 64)
        aid = approval()
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(aid)


def test_changed_approval_payload_cannot_reuse_original_resolution():
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    payload["title"] = "changed after human resolution"
    conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), aid))
    conn.commit()
    conn.close()
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(aid)


@pytest.mark.parametrize("newer_status", ["pending", "denied", "approved", "consumed", "revoked"])
def test_returned_old_head_cannot_reuse_approval_after_newer_request(newer_status):
    original = approval(status="consumed")
    other_payload = {
        "connector": github_app.GITHUB_APP_CONNECTOR,
        "repo": REPO,
        "number": 123,
        "head_sha": OLD,
        "base_branch": "main",
        "state": "OPEN",
        "merged": False,
    }
    approval(status=newer_status, payload=other_payload)
    # GitHub now reports HEAD again; the old consumed row is still present.
    with pytest.raises(shipping_preflight_cli.PreflightError, match="superseded"):
        check(original)


def test_worker_superseded_consumed_binding_requires_fresh_resolution():
    original = approval(status="consumed")
    events.record_event_once(
        github_app.SUPERSEDED_CHANNEL,
        payload={"event_id": "prior-event", "approval_id": original, "outcome": "superseded"},
        originator="fixture-worker",
        dedupe_key="fixture-superseded",
    )
    with pytest.raises(shipping_preflight_cli.PreflightError, match="superseded"):
        check(original)
    fresh = approval()
    assert check(fresh)["approval_id"] == fresh
    with pytest.raises(shipping_preflight_cli.PreflightError, match="superseded"):
        check(original)


def test_newer_request_for_other_pr_does_not_invalidate_selected_binding():
    original = approval(status="consumed")
    other_payload = {
        "connector": github_app.GITHUB_APP_CONNECTOR,
        "repo": REPO,
        "number": 456,
        "head_sha": HEAD,
        "base_branch": "main",
        "state": "OPEN",
        "merged": False,
    }
    approval(payload=other_payload)
    assert check(original)["approval_id"] == original


@pytest.mark.parametrize(
    "kind", ["absent", "self", "same_model", "stale", "missing_author", "unattested"]
)
def test_human_release_decision_alone_cannot_clear_missing_or_invalid_peer_review(kind):
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    conn.close()
    if kind == "absent":
        payload.pop("independent_review")
        payload["title"] = "review: approved by different-model peer"
    elif kind == "self":
        payload["independent_review"]["reviewer"]["subject"] = payload["independent_review"][
            "authors"
        ][0]["subject"]
    elif kind == "same_model":
        payload["independent_review"]["reviewer"]["model"] = payload["independent_review"][
            "authors"
        ][0]["model"]
    elif kind == "stale":
        payload["independent_review"]["head_sha"] = OLD
    elif kind == "missing_author":
        payload["independent_review"]["authors"] = []
    else:
        payload["independent_review"]["statement"] = "independent review was claimed"
    # Even a fresh native human decision on structurally invalid metadata must hold.
    selected = approval(payload=payload)
    with pytest.raises(shipping_preflight_cli.PreflightError):
        check(selected)


def test_review_metadata_cannot_be_added_to_an_existing_human_resolution():
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    payload["independent_review"]["reviewer"]["subject"] = "t3:replacement-review"
    conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), aid))
    conn.commit()
    conn.close()
    with pytest.raises(shipping_preflight_cli.PreflightError, match="verified resolution"):
        check(aid)


def test_native_human_resolution_does_not_waive_cli_evidence_privacy(monkeypatch, capsys):
    aid = approval()
    conn = storage.connect()
    payload = json.loads(
        conn.execute("SELECT payload FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    )
    conn.close()
    payload["independent_review"]["review_ref"] = "Authorization: Bearer private-review-reference"
    payload["independent_review"]["reviewer"]["execution_ref"] = (
        "Authorization: Bearer private-execution-reference"
    )
    selected = approval(payload=payload)
    monkeypatch.setattr(shipping_preflight_cli, "_live_head", lambda *_args: None)
    assert (
        shipping_preflight_cli.main(
            ["--repo", REPO, "--pr", "123", "--head", HEAD, "--approval", str(selected)]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "private-review-reference" not in output and "private-execution-reference" not in output
    assert json.loads(output)["independent_review"]["reviewer"]["model"] == "openai/gpt-6-astra"


APPROVED_1345 = "6a798c3149ea6099c801ea63922f50cfcc16417f"
MERGED_1345 = "5e58a2294698ec5cd989a785612eb99ecec266ba"


def test_merge_admission_refuses_when_the_approved_sha_is_not_the_head():
    """PR #1345 merged at 5e58a229; the Ledger approval was on 6a798c31."""
    decision = shipping_preflight_cli.merge_admission(
        approved_sha=APPROVED_1345,
        head_sha=MERGED_1345,
        statuses=_required_ci(),
    )
    assert decision["action"] == "refuse"
    assert APPROVED_1345 in decision["reason"]
    assert MERGED_1345 in decision["reason"]


def test_preflight_refuses_an_approval_recorded_on_another_commit():
    payload = {
        "connector": github_app.GITHUB_APP_CONNECTOR,
        "repo": REPO,
        "number": 123,
        "head_sha": OLD,
        "base_branch": "main",
        "state": "OPEN",
        "merged": False,
        "independent_review": review_evidence(REPO, 123, OLD),
    }
    with pytest.raises(shipping_preflight_cli.PreflightError, match="not the current head"):
        check(approval(payload=payload), gh(reviews=[], statusCheckRollup=_required_ci()))


def _required_ci(state: str = "SUCCESS") -> list[dict]:
    names = (*config.DEFAULT_REVIEW_CI_REQUIRED_CHECKS, "CI / gates", "CI / full-verification")
    return [{"name": name, "conclusion": state} for name in names]


def test_preflight_passes_with_green_required_ci_and_ledger_without_devin():
    """A Ledger approval and green required CI pass with no Devin review."""
    result = check(approval(), gh(reviews=[], statusCheckRollup=_required_ci()))
    assert result["status"] == "ready"
    assert result["approval_status"] == "approved"
    assert result["head_sha"] == HEAD


def test_preflight_holds_when_required_ci_failed_or_skipped():
    """A failing or skipped required check holds even with a Ledger approval."""
    aid = approval()
    for state in ("FAILURE", "SKIPPED"):
        rollup = _required_ci()
        rollup[0] = {**rollup[0], "conclusion": state}
        with pytest.raises(
            shipping_preflight_cli.PreflightError, match="required checks not SUCCESS"
        ) as exc:
            check(aid, gh(reviews=[], statusCheckRollup=rollup))
        assert f"={state}" in str(exc.value)
    stale = [{**row, "sha": OLD} for row in _required_ci()]
    with pytest.raises(shipping_preflight_cli.PreflightError, match="required checks not SUCCESS"):
        check(aid, gh(reviews=[], statusCheckRollup=stale))


def test_preflight_holds_when_gates_or_full_verification_is_not_green():
    """Green test shards are not enough: gates and the full-verification join hold too."""
    aid = approval()
    for name in ("CI / gates", "CI / full-verification"):
        for state in ("FAILURE", None):
            rollup = [row for row in _required_ci() if row["name"] != name]
            if state is not None:
                rollup.append({"name": name, "conclusion": state})
            with pytest.raises(
                shipping_preflight_cli.PreflightError, match="required checks not SUCCESS"
            ) as exc:
                check(aid, gh(reviews=[], statusCheckRollup=rollup))
            assert f"{name}={state or 'absent'}" in str(exc.value)


def test_merge_checks_extend_an_overridden_review_set(monkeypatch):
    """UA_REVIEW_CI_REQUIRED_CHECKS narrows the pytest-skip set, never the merge bar."""
    monkeypatch.setenv("UA_REVIEW_CI_REQUIRED_CHECKS", "CI / test (shard=0)")
    names = shipping_preflight_cli.merge_required_checks()
    assert names[0] == "CI / test (shard=0)"
    assert {"CI / gates", "CI / full-verification"} <= set(names)
    assert len(names) == len(set(names))


def test_public_approval_comment_does_not_echo_native_review_evidence_references():
    review = review_evidence()
    review["review_ref"] = "Authorization: Bearer private-review-reference"
    review["reviewer"]["execution_ref"] = "Authorization: Bearer private-execution-reference"
    event = github_app.NormalizedPREvent(
        repo=REPO,
        number=123,
        event_type="synchronize",
        title="fixture",
        url="https://github.com/example/project/pull/123",
        state="OPEN",
        head_sha=HEAD,
        base_branch="main",
        head_branch="fixture",
        author_login="Desktop_Agent2",
        independent_review=review,
    )
    body = github_app._evidence_comment(
        event,
        {"id": 7, "resolved_by": "Desktop_Person", "payload": event.to_payload()},
        signed_event_id="fixture",
        signature="fixture",
        chain_head="fixture",
    )
    assert "private-review-reference" not in body and "private-execution-reference" not in body
    assert "openai/gpt-6-astra" not in body and "review_ref_sha256" not in body
    assert '"source":"human-attested-native-approval"' in body
