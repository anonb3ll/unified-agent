"""Merge receipt v1 (task #2307): projection rules, PR binding, offline verify."""

from __future__ import annotations

import json
import socket

import pytest

from unified_agent import config, events, review_reads, storage
from unified_agent.audit import evidence_cli, merge_receipt
from unified_agent.shipping_preflight_cli import PreflightError

REPO = "example-org/example-repo"
URL = f"https://github.com/{REPO}/pull/1361"
HEAD = "a" * 40
MERGE = "b" * 40
BASE_PARENT = "c" * 40
RUN = "sp-01TESTRUN000000000000000000"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()


def _task(id: int, *, evidence_ref: str | None = None, notes: str = "") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "claimed_by, evidence_ref, notes, project, domain, lane) VALUES "
            "(?, ?, 'done', 'high', '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', "
            "'implementer-agent', ?, ?, 'unified-agent', 'hobby', 'default')",
            (id, f"task {id}", evidence_ref, notes),
        )
        conn.commit()
    finally:
        conn.close()


def _run(task_id: int, *, usage: dict | None = None) -> None:
    base = {"run_id": RUN, "task_id": task_id, "agent": "implementer-agent"}
    events.record_event(_CH_RUN, {**base, "kind": "selfpickup.claimed"}, notify=False)
    events.record_event(
        _CH_RUN,
        {**base, "kind": "selfpickup.finished", "outcome": "completed", "usage": usage or {}},
        notify=False,
    )


_CH_RUN = "agent.run.selfpickup"


def _github(**over) -> dict:
    out = {
        "url": URL,
        "title": "feat: something",
        "base": "main",
        "merged": True,
        "merged_at": "2026-11-01T21:00:00Z",
        "merge_commit": MERGE,
        "head_sha": HEAD,
        "parents": [BASE_PARENT, HEAD],
        "contains_tip": True,
        "reviews": [],
        "check_run": None,
    }
    out.update(over)
    return out


def _gate(status: str = "no-read-recorded", **extra):
    def gate(*, repo, number, head_sha, conn=None):
        return {"status": status, "read_head": extra.get("read_head"), **extra}

    return gate


def _build(task_id: int | None = None, *, github: dict | None = None, gate=None) -> dict:
    conn = storage.connect()
    try:
        return merge_receipt.build_receipt(
            repo=REPO,
            number=1361,
            conn=conn,
            github=github or _github(),
            review_gate=gate or _gate(),
            task_id=task_id,
            now="2026-11-02T00:00:00Z",
        )
    finally:
        conn.close()


def _bound(task_id: int = 2259) -> None:
    _task(task_id, evidence_ref=URL)
    _run(task_id)


# --- binding --------------------------------------------------------------


def test_binding_picks_the_evidence_ref_task_over_note_mentions():
    """The live evidence_report(pr="1361") shape: one task carries the PR URL as
    evidence_ref, others only mention the PR in notes or as '#1361' text."""
    _bound(2259)
    _task(2307, evidence_ref="evidence_report(pr=1361) 2026-11-02", notes=f"see {URL}")
    _task(2308, evidence_ref="PR #1361 reviews: 0 APPROVED")
    receipt = _build()
    assert receipt["pr"]["task_id"] == 2259
    assert receipt["pr"]["binding"] == "evidence_ref"


def test_binding_refuses_two_evidence_ref_claims():
    _bound(10)
    _task(11, evidence_ref=URL)
    with pytest.raises(merge_receipt.ReceiptError, match="ambiguous binding: tasks 10, 11"):
        _build()


def test_binding_is_exact_url_only_never_a_longer_number_other_repo_or_note():
    _task(20, evidence_ref=f"https://github.com/{REPO}/pull/13610")
    _task(22, evidence_ref="https://github.com/other-org/example-repo/pull/1361")
    _task(21, notes=f"related: {URL}")
    with pytest.raises(merge_receipt.ReceiptError, match="tasks 21 only mention it"):
        _build()
    # A shorter PR number never matches the longer one.
    _task(23, evidence_ref=f"https://github.com/{REPO}/pull/136")
    with pytest.raises(merge_receipt.ReceiptError, match="no binding"):
        _build()


def test_explicit_task_only_asserts_the_canonical_binding():
    _task(30, notes=f"follow-up of {URL}")
    with pytest.raises(merge_receipt.ReceiptError, match="no binding"):
        _build(task_id=30)
    _bound(31)
    assert _build(task_id=31)["pr"]["task_id"] == 31
    with pytest.raises(merge_receipt.ReceiptError, match="does not match the binding"):
        _build(task_id=30)
    _task(32, evidence_ref=f"{URL} (mirror)")
    with pytest.raises(merge_receipt.ReceiptError, match="ambiguous binding"):
        _build(task_id=31)


def test_unmerged_pr_is_refused():
    _bound()
    with pytest.raises(merge_receipt.ReceiptError, match="not merged"):
        _build(github=_github(merged=False, merge_commit=None))


# --- projection rules -----------------------------------------------------


def test_missing_approval_renders_not_recorded_never_a_tick():
    _bound()
    receipt = _build()
    assert receipt["authority"]["status"] == "not-recorded"
    assert receipt["authority"]["checkable_without_hub"] is False
    row = next(
        line for line in merge_receipt.render_markdown(receipt).splitlines() if "Approval" in line
    )
    assert "not-recorded" in row and "✓" not in row


def _review(id, user, state, commit=HEAD, at="2026-11-01T10:00:00Z"):
    return {"id": id, "user": user, "state": state, "commit_id": commit, "submitted_at": at}


def test_github_approval_is_credential_attributed_and_ticked():
    _bound()
    review = _review(18823, "reviewer", "APPROVED")
    receipt = _build(github=_github(reviews=[review]))
    authority = receipt["authority"]
    assert authority["status"] == "credential-attributed"
    assert authority["review_id"] == "github:18823" and authority["checkable_without_hub"]
    assert "verified" not in json.dumps(authority)
    row = next(
        line for line in merge_receipt.render_markdown(receipt).splitlines() if "Approval" in line
    )
    assert row.startswith("| ✓ |")


def test_superseded_or_stale_github_approvals_are_not_authority():
    _bound()
    later = "2026-11-01T11:00:00Z"
    for reviews in (
        [_review(1, "reviewer", "APPROVED"), _review(2, "reviewer", "CHANGES_REQUESTED", at=later)],
        [_review(3, "reviewer", "DISMISSED")],
        [_review(4, "reviewer", "APPROVED", commit=BASE_PARENT)],
    ):
        authority = _build(github=_github(reviews=reviews))["authority"]
        assert authority["status"] == "not-recorded", reviews
    # A later comment does not withdraw an approval.
    reviews = [_review(5, "reviewer", "APPROVED"), _review(6, "reviewer", "COMMENTED", at=later)]
    assert _build(github=_github(reviews=reviews))["authority"]["review_id"] == "github:5"


def _approval(tool: str, payload: dict) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, resolved_by, "
            "created_at, updated_at) VALUES (?, ?, 'implementer-agent', 'consumed', "
            "'operator', '2026-11-01', '2026-11-01')",
            (tool, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def test_hub_approval_counts_only_for_this_repo_pr_and_head():
    _bound()
    _approval("fs_write", {"task_id": 2259})
    _approval("github_app.process_pr_event", {"repo": REPO, "number": 1361, "head_sha": MERGE})
    _approval("github_app.process_pr_event", {"repo": REPO, "number": 1360, "head_sha": HEAD})
    assert _build()["authority"]["status"] == "not-recorded"
    _approval("github_app.process_pr_event", {"repo": REPO, "number": 1361, "head_sha": HEAD})
    receipt = _build()
    assert receipt["authority"]["status"] == "hub-attested"
    assert receipt["authority"]["commit_id"] == HEAD
    assert receipt["authority"]["checkable_without_hub"] is False
    row = next(
        line for line in merge_receipt.render_markdown(receipt).splitlines() if "Approval" in line
    )
    assert "✓" not in row


def test_no_read_recorded_is_a_finding_not_a_blank():
    _bound()
    receipt = _build()
    assert receipt["review"]["status"] == "no-read-recorded"
    assert receipt["review"]["finding"] == "no review read recorded at this head"
    assert "**finding:** no review read recorded at this head" in merge_receipt.render_markdown(
        receipt
    )


def test_moved_head_from_the_gate_is_a_finding():
    _bound()

    def gate(**_):
        raise PreflightError("head moved off the recorded read")

    review = _build(gate=gate)["review"]
    assert review["status"] == "no-read-recorded"
    assert "head moved off the recorded read" in review["finding"]

    def offline_gate(**_):
        raise PreflightError("Live PR metadata is unavailable.")

    receipt = _build(gate=offline_gate)
    assert receipt["review"]["status"] == "not-recorded" and "finding" not in receipt["review"]
    assert any(e["field"] == "review.reader" for e in receipt["omitted"])


def test_read_at_head_uses_the_real_gate_and_review_reads_row():
    from unified_agent.shipping_preflight_cli import review_head_gate

    _bound()
    review_reads.record(
        repo=REPO, number=1361, head_sha=HEAD, kind="steward-read", reader="steward", verdict="pass"
    )
    review = _build(gate=review_head_gate)["review"]
    assert review["status"] == "read-at-head" and review["coverage"] == "full"
    assert review["reader"] == "steward" and review["at"]


def test_a_rejected_read_is_a_finding_never_full_coverage():
    """#2420: the receipt names the rejection instead of reporting the head as read."""
    from unified_agent.shipping_preflight_cli import review_head_gate

    _bound()
    review_reads.record(
        repo=REPO,
        number=1361,
        head_sha=HEAD,
        kind="steward-read",
        reader="steward",
        verdict="fix-first: cap missing",
    )
    review = _build(gate=review_head_gate)["review"]
    assert review["coverage"] != "full" and "fix-first: cap missing" in review["finding"]


def test_denials_without_bound_gateway_rows_are_omitted_never_zero():
    _bound()
    events.record_event(
        "agent.gateway.decision",
        {"decision": "deny", "rule": "r", "tool_name": "fs_write", "reason_code": "x"},
        notify=False,
    )
    receipt = _build()
    assert receipt["policy"]["denials"] is None and receipt["policy"]["coverage"] == "none"
    assert any(e["field"] == "policy.denials" for e in receipt["omitted"])
    assert "denied" not in merge_receipt.render_markdown(receipt)


def test_bound_denials_render_with_a_coverage_label():
    _bound()
    events.record_event(
        "agent.gateway.decision",
        {
            "decision": "deny",
            "rule": "secrets",
            "tool_name": "fs_read",
            "reason_code": "denied",
            "profile": "developer",
            "harness": "claude-code",
            "args_sha256": "d" * 64,
        },
        episode_id=RUN,
        notify=False,
    )
    receipt = _build()
    policy = receipt["policy"]
    assert policy["coverage"] == "claude-code" and policy["profile"] == "developer"
    assert policy["denials"][0]["args_sha256"] == "d" * 64
    assert "captured: claude-code (partial) — 1 denied" in merge_receipt.render_markdown(receipt)


def test_bound_allows_without_denials_never_render_a_zero_count():
    _bound()
    events.record_event(
        "agent.gateway.decision",
        {"decision": "allow", "tool": "fs_read"},
        episode_id=RUN,
        notify=False,
    )
    receipt = _build()
    assert receipt["policy"]["denials"] == [] and receipt["policy"]["coverage"] == "hub-gateway"
    text = merge_receipt.render_markdown(receipt)
    assert "no denials recorded (coverage: hub-gateway, partial)" in text
    assert "0 denied" not in text


def test_agent_hub_attested_device_unattested_cost_from_run_usage():
    _task(2259, evidence_ref=URL)
    _run(2259, usage={"model": "model-x", "cost_usd": 1.25, "billing": "api"})
    receipt = _build()
    assert receipt["agent"] == {
        "label": "implementer-agent",
        "harness": None,
        "model": "model-x",
        "attestation": "hub-attested",
        "run_id": RUN,
    }
    assert receipt["device"]["attested"] is False
    assert receipt["cost"] == {"usd": 1.25, "billing": "api", "source": "run usage"}
    fields = {e["field"] for e in receipt["omitted"]}
    assert {"device.attested", "agent.harness"} <= fields and "cost" not in fields


def test_finalize_refuses_unexplained_nulls_and_verified_authority():
    _bound()
    receipt = _build()
    broken = {
        **receipt,
        "cost": None,
        "omitted": [e for e in receipt["omitted"] if e["field"] != "cost"],
    }
    with pytest.raises(merge_receipt.ReceiptError, match="cost is null but not listed"):
        merge_receipt.finalize(broken)
    lying = {**receipt, "authority": {**receipt["authority"], "status": "verified"}}
    with pytest.raises(merge_receipt.ReceiptError, match="never emitted"):
        merge_receipt.finalize(lying)
    device = {**receipt, "device": {**receipt["device"], "attested": True}}
    with pytest.raises(merge_receipt.ReceiptError, match="device.attested"):
        merge_receipt.finalize(device)


# --- verify (offline) -----------------------------------------------------


@pytest.fixture
def hub_unreachable(monkeypatch, tmp_path):
    """No database and no network: every store and socket entry point raises."""

    def refuse(*_a, **_k):
        raise AssertionError("verify touched the hub store or the network")

    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "absent" / "memory.db")
    monkeypatch.setattr(storage, "connect", refuse)
    monkeypatch.setattr(storage, "connect_read_only", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _write(tmp_path, receipt) -> str:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return str(path)


def _redigest(receipt: dict) -> dict:
    out = {k: v for k, v in receipt.items() if k != "digest"}
    out["digest"] = {"alg": "sha256", "canonical": "json-sorted-compact-utf8"}
    out["digest"]["value"] = merge_receipt._digest(out)
    return out


@pytest.fixture
def built(tmp_path):
    _bound()
    return _build()


def test_receipt_carries_no_payload_text_only_its_digest():
    _task(2259, evidence_ref=URL)
    events.record_event(
        "comment.appended", {"task_id": 2259, "body": "private note SECRET-MARKER"}, notify=False
    )
    _run(2259)
    receipt = _build()
    text = json.dumps(receipt)
    assert "SECRET-MARKER" not in text and "selfpickup.claimed" not in text
    row = receipt["chain"]["events"][0]
    assert set(row) == {
        "ulid",
        "channel",
        "created_at",
        "prev_hash",
        "entry_hash",
        "payload_sha256",
    }
    assert receipt["chain"]["payloads"] == "withheld"
    with pytest.raises(merge_receipt.ReceiptError, match="payload text"):
        merge_receipt.finalize(
            {**receipt, "chain": {**receipt["chain"], "events": [{**row, "payload": "x"}]}}
        )


def test_verify_passes_offline_with_the_hub_unreachable(tmp_path, built, hub_unreachable, capsys):
    assert len(built["chain"]["events"]) == built["chain"]["event_count"] >= 2
    assert evidence_cli.main(["verify", _write(tmp_path, built)]) == 0
    out = capsys.readouterr().out
    assert "receipt-verify: ledger_rows: ok" in out
    assert "receipt-verify: consistency checks passed" in out and "verified\n" not in out
    assert "NOT verified: authenticity" in out and "NOT verified: ledger-head membership" in out
    assert "NOT verified: row hashes" in out
    assert "Unsigned:" in merge_receipt.render_markdown(built)


def test_verify_detects_tamper_and_unexplained_nulls(tmp_path, built, hub_unreachable, capsys):
    edited = json.loads(json.dumps(built))
    edited["pr"]["title"] = "something else"
    assert evidence_cli.main(["verify", _write(tmp_path, edited)]) == 1
    assert "digest: FAIL" in capsys.readouterr().out

    edited["chain"]["events"][0]["entry_hash"] = "not-a-hash"
    assert evidence_cli.main(["verify", _write(tmp_path, _redigest(edited))]) == 1
    assert "ledger_rows: FAIL: malformed entry_hash" in capsys.readouterr().out

    nulled = json.loads(json.dumps(built))
    nulled["pr"]["title"] = None
    assert evidence_cli.main(["verify", _write(tmp_path, _redigest(nulled))]) == 1
    assert "pr.title is null but not listed in omitted" in capsys.readouterr().out

    overclaimed = json.loads(json.dumps(built))
    overclaimed["omitted"].append({"field": "pr.title", "reason": "pretend"})
    assert evidence_cli.main(["verify", _write(tmp_path, _redigest(overclaimed))]) == 1
    assert "pr.title is listed in omitted but populated" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(digest="abc"),
        lambda r: r.pop("chain"),
        lambda r: r["pr"].update(number="1361"),
        lambda r: r["authority"].update(status="approved"),
        lambda r: r.update(schema="merge_receipt/2"),
    ],
    ids=["digest-string", "missing-block", "wrong-type", "unknown-status", "wrong-version"],
)
def test_verify_malformed_receipt_exits_cleanly(tmp_path, built, hub_unreachable, capsys, mutate):
    doc = json.loads(json.dumps(built))
    mutate(doc)
    assert evidence_cli.main(["verify", _write(tmp_path, doc)]) == 2
    captured = capsys.readouterr()
    assert "structure: FAIL" in captured.out and "Traceback" not in captured.err


# --- CLI ------------------------------------------------------------------


def test_receipt_cli_writes_json_and_markdown_and_refuses_ambiguity(tmp_path, capsys):
    _bound()

    def api(path):
        if path == f"repos/{REPO}/pulls/1361":
            return {
                "html_url": URL,
                "title": "feat: something",
                "base": {"ref": "main"},
                "merged": True,
                "merged_at": "2026-11-01T21:00:00Z",
                "merge_commit_sha": MERGE,
                "head": {"sha": HEAD},
            }
        if path == f"repos/{REPO}/commits/{MERGE}":
            return {"parents": [{"sha": BASE_PARENT}, {"sha": HEAD}]}
        if path.startswith(f"repos/{REPO}/pulls/1361/reviews"):
            return []
        if path.startswith(f"repos/{REPO}/commits/{HEAD}/check-runs"):
            return {"check_runs": []}
        raise AssertionError(path)

    out = tmp_path / "receipt.json"
    argv = ["--pr", "1361", "--repo", REPO, "--out", str(out), "--markdown"]
    assert evidence_cli.run_receipt_cli(argv, api=api, review_gate=_gate()) == 0
    printed = capsys.readouterr().out
    assert printed.startswith(f"### Merge receipt — {REPO}#1361")
    receipt = json.loads(out.read_text())
    assert receipt["schema"] == "merge_receipt/1" and receipt["head"]["contains_tip"] is True
    assert merge_receipt.verify_receipt(receipt)["ok"]

    _task(2400, evidence_ref=URL)
    assert evidence_cli.run_receipt_cli(argv, api=api, review_gate=_gate()) == 1
    assert "ambiguous binding" in capsys.readouterr().err


def test_a_commit_status_stands_in_for_the_check_run_and_says_so():
    def api(path):
        if path.startswith(f"repos/{REPO}/commits/{HEAD}/check-runs"):
            return {"check_runs": []}
        if path == f"repos/{REPO}/commits/{HEAD}/statuses?per_page=100":
            return [
                {"context": "Approval Ledger", "state": "success"},
                {
                    "context": "unified-agent/review-head",
                    "state": "error",
                    "id": 9,
                    "target_url": URL,
                },
            ]
        if path == f"repos/{REPO}/pulls/1361":
            return {"head": {"sha": HEAD}, "merge_commit_sha": MERGE, "merged": True}
        if path == f"repos/{REPO}/commits/{MERGE}":
            return {"parents": [{"sha": HEAD}]}
        return []

    assert merge_receipt.fetch_github(REPO, 1361, api)["check_run"] == {
        "name": "unified-agent/review-head",
        "conclusion": "action_required",
        "id": 9,
        "url": URL,
        "source": "commit_status",
    }


def test_github_enrichment_failures_become_omitted_reasons():
    _bound()

    def api(path):
        if path == f"repos/{REPO}/pulls/1361":
            return {
                "html_url": URL,
                "title": "t",
                "base": {"ref": "main"},
                "merged": True,
                "merged_at": "2026-11-01T21:00:00Z",
                "merge_commit_sha": MERGE,
                "head": {"sha": HEAD},
            }
        if path == f"repos/{REPO}/commits/{MERGE}":
            return {"parents": [{"sha": BASE_PARENT}, {"sha": "e" * 40}]}
        raise RuntimeError("rate limited")

    receipt = _build(github=merge_receipt.fetch_github(REPO, 1361, api))
    reasons = {e["field"]: e["reason"] for e in receipt["omitted"]}
    assert "compare could not be fetched" in reasons["head.contains_tip"]
    assert "check runs could not be fetched" in reasons["review.check_run"]
    assert "reviews could not be fetched" in reasons["authority.review_id"]

    def broken(path):
        raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        merge_receipt.fetch_github(REPO, 1361, broken)


def test_delta_clean_coverage_names_the_unread_ancestor():
    _bound()
    clean = _build(gate=_gate("delta-clean", read_head="c" * 40, reader="steward"))
    assert clean["review"]["coverage"] == "delta-clean(ancestor read; red-tier untouched)"
    read = _build(gate=_gate("delta-read", read_head="c" * 40, reader="steward", kind="delta-read"))
    assert read["review"]["coverage"] == "delta(red-tier complete)"


def test_usage_reported_after_finish_replaces_the_finish_snapshot():
    _task(2259, evidence_ref=URL)
    _run(
        2259,
        usage={"model": "model-old", "cost_usd": 0.20, "billing": "api", "cost_complete": True},
    )
    events.record_event(
        _CH_RUN,
        {
            "kind": "selfpickup.usage_reported",
            "run_id": RUN,
            "task_id": 2259,
            "agent": "implementer-agent",
            "usage": {
                "model": "model-y",
                "cost_usd": 0.35,
                "billing": "api",
                "cost_complete": True,
            },
        },
        notify=False,
    )
    receipt = _build()
    assert receipt["cost"]["usd"] == 0.35
    assert receipt["agent"]["model"] == "model-y"


def test_review_gate_reads_the_receipt_connection_snapshot():
    _bound()
    conn = storage.connect_read_only()
    try:
        conn.execute("SELECT 1").fetchone()
        review_reads.record(
            repo=REPO,
            number=1361,
            head_sha=HEAD,
            kind="steward-read",
            reader="steward",
            verdict="pass",
        )
        from unified_agent.shipping_preflight_cli import review_head_gate

        receipt = merge_receipt.build_receipt(
            repo=REPO,
            number=1361,
            conn=conn,
            github=_github(),
            review_gate=review_head_gate,
            now="2026-11-02T00:00:00Z",
        )
    finally:
        conn.close()
    assert review_reads.for_pr(REPO, 1361)
    assert receipt["review"]["status"] == "no-read-recorded"


def test_a_token_that_cannot_read_check_runs_still_finds_the_status():
    def api(path):
        if path.startswith(f"repos/{REPO}/commits/{HEAD}/check-runs"):
            raise PermissionError("no Checks access")
        if path == f"repos/{REPO}/commits/{HEAD}/statuses?per_page=100":
            return [{"context": "unified-agent/review-head", "state": "success", "id": 9}]
        if path == f"repos/{REPO}/pulls/1361":
            return {"head": {"sha": HEAD}, "merge_commit_sha": MERGE, "merged": True}
        if path == f"repos/{REPO}/commits/{MERGE}":
            return {"parents": [{"sha": HEAD}]}
        return []

    github = merge_receipt.fetch_github(REPO, 1361, api)
    assert github["check_run"]["source"] == "commit_status"
    assert "check_runs" not in github.get("errors", {})


def test_fetch_github_reads_approving_reviews_past_the_first_page():
    def api(path):
        if path == f"repos/{REPO}/pulls/1361":
            return {
                "html_url": URL,
                "title": "feat: something",
                "base": {"ref": "main"},
                "merged": True,
                "merged_at": "2026-11-01T21:00:00Z",
                "merge_commit_sha": MERGE,
                "head": {"sha": HEAD},
            }
        if path == f"repos/{REPO}/commits/{MERGE}":
            return {"parents": [{"sha": BASE_PARENT}, {"sha": HEAD}]}
        if path == f"repos/{REPO}/pulls/1361/reviews?per_page=100&page=1":
            return [
                {
                    "id": i,
                    "user": {"login": "commenter"},
                    "state": "COMMENTED",
                    "commit_id": HEAD,
                    "submitted_at": "2026-11-01T00:00:00Z",
                }
                for i in range(100)
            ]
        if path == f"repos/{REPO}/pulls/1361/reviews?per_page=100&page=2":
            return [
                {
                    "id": 100,
                    "user": {"login": "approver"},
                    "state": "APPROVED",
                    "commit_id": HEAD,
                    "submitted_at": "2026-11-01T01:00:00Z",
                }
            ]
        if path.startswith(f"repos/{REPO}/commits/{HEAD}/check-runs"):
            return {"check_runs": []}
        raise AssertionError(path)

    reviews = merge_receipt.fetch_github(REPO, 1361, api)["reviews"]
    assert len(reviews) == 101
    assert reviews[-1]["user"] == "approver" and reviews[-1]["state"] == "APPROVED"


def test_verify_rejects_a_non_object_digest_without_crashing():
    result = merge_receipt.verify_receipt({"schema": merge_receipt.SCHEMA, "digest": ["bad"]})
    assert result["ok"] is False
    assert result["malformed"] is True
    assert any(check["name"] == "structure" and check["ok"] is False for check in result["checks"])


def test_unconfigured_git_witness_does_not_fail_verification(built, monkeypatch):
    monkeypatch.delenv("UA_EVIDENCE_ANCHOR_GIT_REPO", raising=False)
    receipt = json.loads(json.dumps(built))
    receipt["chain"]["anchor"] = {
        "backend": "git",
        "ref": {"repo": "/tmp/no-such-witness", "path": "anchor.json", "commit_sha": "a" * 40},
        "anchored_head": "b" * 64,
        "entries_after_anchor": 0,
        "verified": True,
        "checkable_without_hub": True,
    }
    receipt["omitted"] = [entry for entry in receipt["omitted"] if entry["field"] != "chain.anchor"]
    result = merge_receipt.verify_receipt(_redigest(receipt))
    anchor = next(check for check in result["checks"] if check["name"] == "anchor")
    assert anchor["ok"] is None
    assert "witness repo is not configured" in anchor["detail"]
    assert result["ok"] is True
