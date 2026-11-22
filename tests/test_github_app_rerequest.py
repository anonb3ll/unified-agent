"""Re-request an Approval Ledger row for one PR head (task #1854).

A missed poll or webhook left PR #1002 head a52c0ab2 with no
``github_app.process_pr_event`` approval row, so nothing could be approved and
landing needed a manual push. An operator can now ask the hub to open the row
for an exact, still-current PR head. The re-request only queues the approval:
the operator still approves it like any other row, and no agent can resolve it
through this path.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from review_evidence_fixture import review_evidence
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_github_app_worker import RecordingTransport, _policy, _pr

from unified_agent import (
    config,
    github_app,
    github_app_fastpath,
    github_app_worker,
    server,
    storage,
)
from unified_agent.audit import producer_sign
from unified_agent.tools import approvals
from unified_agent.tools.graph import coordination_manage
from unified_agent.webui.routes import web_routes

OPERATOR = "Operator"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def connector(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "worker-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"worker-bearer": "Desktop_Agent2"})
    prs = [_pr(sha="head-1")]
    calls: list[list[str]] = []

    def gh(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    return SimpleNamespace(prs=prs, gh=gh, calls=calls, transport=RecordingTransport())


def _rerequest(c, head_sha="head-1", number=123, who=OPERATOR):
    with _as(who):
        return github_app_worker.rerequest_head(
            number, head_sha, run_gh=c.gh, transport=c.transport, policy_config=_policy()
        )


def _review_request(c, proposal=None):
    head = "a" * 40
    c.prs[0] = _pr(sha=head)
    proposal = proposal if proposal is not None else review_evidence(head_sha=head)
    with _as(OPERATOR):
        return github_app_worker.rerequest_head(
            123,
            head,
            independent_review=proposal,
            run_gh=c.gh,
            transport=c.transport,
            policy_config=_policy(),
        )


def _ledger() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator FROM event_log WHERE channel=? ORDER BY ulid",
            (github_app_worker.RE_REQUEST_CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    return [{**json.loads(r["payload"]), "_originator": r["originator"]} for r in rows]


def _github_rows() -> list[dict]:
    return [
        a
        for a in approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"]
        if a["tool"] == github_app.TOOL_NAME
    ]


def test_rerequest_opens_the_missing_row_for_the_current_head(connector):
    assert _github_rows() == []
    out = _rerequest(connector)
    assert out["status"] == "pending_approval"
    approval = approvals.get_approval(out["approval_id"])
    # Queued, never resolved: the operator approves it like any other row.
    assert approval["status"] == "pending"
    assert approval["requested_by"] == "Desktop_Agent2"
    assert len(connector.transport.created) == 1
    assert connector.transport.created[0]["head_sha"] == "head-1"
    [event] = _ledger()
    assert event["number"] == 123 and event["head_sha"] == "head-1"
    assert event["actor"] == OPERATOR and event["_originator"] == OPERATOR
    assert event["outcome"] == "pending_approval"
    assert event["approval_id"] == out["approval_id"]
    # The scheduled poll then sees the row and does not mint a second one.
    assert (
        github_app_worker.run_once(
            run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
        )
        == []
    )
    assert len(connector.transport.created) == 1


def test_fast_path_builds_its_transport_for_the_connector_repo(connector, monkeypatch):
    """App mode mints for one repository, so the fast path must name it (task #2339)."""
    out = _rerequest(connector)
    built = []

    def build_transport(**kwargs):
        built.append(kwargs)
        return RecordingTransport()

    monkeypatch.setattr(github_app, "build_transport", build_transport)
    github_app_fastpath.advance_resolved(
        out["approval_id"], run_gh=connector.gh, policy_config=_policy()
    )
    assert built == [{"repo": "example/project"}]


def test_rerequest_of_an_existing_row_reports_it_and_changes_nothing(connector):
    first = _rerequest(connector)
    again = _rerequest(connector)
    assert again["status"] == "already_requested"
    assert again["approval_id"] == first["approval_id"]
    assert again["approval_status"] == "pending"
    assert len(connector.transport.created) == 1
    assert len(_github_rows()) == 1


def test_review_proposal_is_bound_to_the_existing_release_decision_and_survives_poll(connector):
    first = _review_request(connector)
    row = approvals.get_approval(first["approval_id"])
    assert row["tool"] == github_app.TOOL_NAME and row["status"] == "pending"
    assert row["payload"]["independent_review"] == review_evidence()
    assert _review_request(connector)["approval_id"] == first["approval_id"]
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    result = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert [r["status"] for r in result] == ["approved"]
    assert approvals.get_approval(first["approval_id"])["status"] == "consumed"
    assert len(connector.transport.comments) == 1
    body = connector.transport.comments[0]["body"]
    assert "Human-attested independent review" in body
    assert "openai/gpt-6.1-sol" not in body and "openai/gpt-6-astra" not in body
    assert '"author_count":1' in body
    from test_shipping_preflight import check

    assert (
        check(first["approval_id"])["independent_review"]["reviewer"]["model"]
        == "openai/gpt-6-astra"
    )


def test_enrichment_replaces_only_an_undecided_proposal_without_mutating_its_payload(connector):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    old_payload = approvals.get_approval(first["approval_id"])["payload"]
    second = _review_request(connector)
    assert second["approval_id"] != first["approval_id"]
    old = approvals.get_approval(first["approval_id"])
    assert old["status"] == "expired" and old["payload"] == old_payload
    assert approvals.get_approval(second["approval_id"])["status"] == "pending"


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_existing_human_decision_is_never_extended_with_new_review_claims(connector, decision):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], decision)
    before = approvals.get_approval(first["approval_id"])
    with pytest.raises(ToolError, match="cannot attest"):
        _review_request(connector)
    assert approvals.get_approval(first["approval_id"]) == before


def test_missing_or_invalid_review_is_rejected_before_replacing_a_pending_proposal(connector):
    first = _review_request(connector)
    proposal = review_evidence()
    proposal["reviewer"]["model"] = proposal["authors"][0]["model"]
    with pytest.raises(ToolError, match="distinct actor and model"):
        _review_request(connector, proposal)
    assert approvals.get_approval(first["approval_id"])["status"] == "pending"


def test_settled_head_cannot_be_reopened_to_attest_new_review_metadata(connector):
    first = _review_request(connector)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    with pytest.raises(ToolError, match="settled"):
        _review_request(connector)


@pytest.mark.parametrize("decision", ["approve", "deny", "consume"])
def test_native_resolution_wins_pending_replacement_race_without_a_successor(
    connector, monkeypatch, decision
):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    original = github_app_worker._expire_pending_review_proposal

    def resolve_first(latest):
        with _as(OPERATOR):
            approvals.approval_resolve(
                first["approval_id"], "deny" if decision == "deny" else "approve"
            )
        if decision == "consume":
            approvals.check_approval_gate(
                github_app.TOOL_NAME,
                approvals.get_approval(first["approval_id"])["payload"],
                requested_by="Desktop_Agent2",
                force=True,
            )
        original(latest)

    monkeypatch.setattr(github_app_worker, "_expire_pending_review_proposal", resolve_first)
    with pytest.raises(ToolError, match="changed concurrently"):
        _review_request(connector)
    assert (
        approvals.get_approval(first["approval_id"])["status"]
        == {"approve": "approved", "deny": "denied", "consume": "consumed"}[decision]
    )
    conn = storage.connect()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE tool=?", (github_app.TOOL_NAME,)
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_pending_replacement_wins_then_native_resolution_of_old_proposal_is_refused(connector):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    latest = github_app_worker._head_approval(
        github_app_worker._event(github_app_worker.pr_watch.parse_prs(connector.prs)[123])
    )
    github_app_worker._expire_pending_review_proposal(latest)
    with _as(OPERATOR), pytest.raises(ToolError, match="not 'pending'"):
        approvals.approval_resolve(first["approval_id"], "approve")
    second = _review_request(connector)
    assert second["approval_id"] != first["approval_id"]
    assert approvals.get_approval(second["approval_id"])["status"] == "pending"


def test_competing_replacement_fences_a_stale_proposal_writer(connector):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    latest = github_app_worker._head_approval(
        github_app_worker._event(github_app_worker.pr_watch.parse_prs(connector.prs)[123])
    )
    second = _review_request(connector)
    with pytest.raises(ToolError, match="newest approval"):
        github_app_worker._expire_pending_review_proposal(latest)
    assert approvals.get_approval(second["approval_id"])["status"] == "pending"
    assert approvals.get_approval(first["approval_id"])["status"] == "expired"


def test_ttl_expired_approved_row_cannot_be_silently_replaced_with_review_claims(connector):
    connector.prs[0] = _pr(sha="a" * 40)
    first = _rerequest(connector, head_sha="a" * 40)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    conn = storage.connect()
    conn.execute(
        "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (first["approval_id"],),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ToolError, match="cannot attest"):
        _review_request(connector)
    assert approvals.get_approval(first["approval_id"])["status"] == "approved"


def test_coordination_manage_forwards_explicit_review_metadata_through_the_existing_action(
    connector, monkeypatch
):
    seen = []
    proposal = review_evidence()

    def requested(number, head_sha, *, independent_review):
        seen.append((number, head_sha, independent_review))
        return {"status": "pending_approval"}

    monkeypatch.setattr(github_app_worker, "rerequest_head", requested)
    with _as(OPERATOR):
        result = coordination_manage(
            "approval_ledger_rerequest",
            {"number": 123, "head_sha": "a" * 40, "independent_review": proposal},
        )
    assert result["status"] == "pending_approval"
    assert seen == [(123, "a" * 40, proposal)]


def test_status_post_recovery_preserves_the_same_complete_review_proposal(connector, monkeypatch):
    restore = _fail_status_posts(connector, monkeypatch)
    with pytest.raises(RuntimeError, match="synthetic"):
        _review_request(connector)
    restore()
    rows = _github_rows()
    assert len(rows) == 1
    second = _review_request(connector)
    assert second["approval_id"] == rows[0]["id"]
    assert (
        approvals.get_approval(second["approval_id"])["payload"]["independent_review"]
        == review_evidence()
    )


def test_rerequest_after_expiry_opens_a_fresh_row(connector):
    first = _rerequest(connector)
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status='expired' WHERE id=?", (first["approval_id"],))
        conn.commit()
    finally:
        conn.close()
    again = _rerequest(connector)
    assert again["status"] == "pending_approval"
    assert again["approval_id"] != first["approval_id"]
    assert approvals.get_approval(again["approval_id"])["status"] == "pending"


def test_rerequest_replaces_an_approved_row_past_its_ttl(connector, monkeypatch):
    # An approved pass that outlived its TTL can no longer be spent, so the
    # re-request must queue a replacement rather than report it (#1152).
    first = _rerequest(connector)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    monkeypatch.setattr(config, "approval_pass_ttl_hours", lambda: 1)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first["approval_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    again = _rerequest(connector)
    assert again["status"] == "pending_approval"
    assert again["approval_id"] != first["approval_id"]
    assert approvals.get_approval(first["approval_id"])["status"] == "expired"
    assert approvals.get_approval(again["approval_id"])["status"] == "pending"


def test_rerequest_refuses_a_stale_or_closed_head(connector):
    with pytest.raises(ToolError, match="head is 'head-1'"):
        _rerequest(connector, head_sha="head-0")
    connector.prs[0] = {**_pr(sha="head-1"), "state": "CLOSED"}
    with pytest.raises(ToolError, match="not open"):
        _rerequest(connector)
    assert _github_rows() == []
    # Both calls reached GitHub, so both are on the record with the refusal (#1152).
    stale, closed = _ledger()
    assert stale["outcome"] == "stale_head" and stale["head_sha"] == "head-0"
    assert closed["outcome"] == "not_open"
    assert stale["actor"] == closed["actor"] == OPERATOR


def test_rerequest_records_a_failed_github_read(connector):
    # The PR read reached GitHub and failed: still on the record (#2020).
    connector.gh = lambda cmd, **_kw: SimpleNamespace(returncode=1, stdout="")
    with pytest.raises(RuntimeError, match="GitHub PR view failed"):
        _rerequest(connector)
    assert _github_rows() == []
    [event] = _ledger()
    assert event["outcome"] == "read_failed" and event["error"] == "RuntimeError"
    assert event["actor"] == OPERATOR and event["head_sha"] == "head-1"


def test_rerequest_records_an_unresolvable_token_as_not_read(connector, monkeypatch):
    # No credential, so GitHub was never asked: not a failed read (#2020).
    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")

    def no_token():
        raise RuntimeError("connector token unavailable")

    monkeypatch.setattr(github_app, "resolve_user_token", no_token)
    with pytest.raises(RuntimeError, match="connector token unavailable"):
        _rerequest(connector)
    [event] = _ledger()
    assert event["outcome"] == "token_unavailable" and event["error"] == "RuntimeError"


@pytest.mark.parametrize(
    ("name", "value"),
    [("UA_GITHUB_TOKEN_MODE", "invalid"), ("UA_GITHUB_USER_TOKEN_SOURCE", "invalid")],
)
def test_rerequest_distinguishes_invalid_configuration_from_missing_credentials(
    connector, monkeypatch, name, value
):
    monkeypatch.setenv("UA_GITHUB_TOKEN_MODE", "user")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        _rerequest(connector)
    [event] = _ledger()
    assert event["outcome"] == "configuration_invalid"
    assert connector.calls == []


def test_rerequest_is_operator_only(connector):
    with pytest.raises(ToolError, match="operator"):
        _rerequest(connector, who="Desktop_Agent1")
    with pytest.raises(ToolError, match="authenticated"):
        github_app_worker.rerequest_head(
            123, "head-1", run_gh=connector.gh, transport=connector.transport
        )
    assert connector.calls == []
    assert _github_rows() == []


def test_rerequest_needs_the_connector_enabled(connector, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    with pytest.raises(ToolError, match="not enabled"):
        _rerequest(connector)
    assert connector.calls == []


def test_coordination_manage_exposes_the_rerequest(connector, monkeypatch):
    seen = []
    monkeypatch.setattr(
        github_app_worker,
        "rerequest_head",
        lambda number, head_sha: seen.append((number, head_sha)) or {"status": "ok"},
    )
    with _as(OPERATOR):
        out = coordination_manage(
            "approval_ledger_rerequest", {"number": 123, "head_sha": "head-1"}
        )
    assert out == {"status": "ok"} and seen == [(123, "head-1")]


# --- Control Center route -------------------------------------------------------------

TOKENS = {"Desktop_Agent1": "fixture-peer-token", OPERATOR: "fixture-operator-token"}


@pytest.fixture
def http(connector, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    # The connector fixture stubbed the token map with the worker bearer only.
    monkeypatch.setattr(
        config,
        "agent_tokens",
        lambda: {**{v: k for k, v in TOKENS.items()}, "worker-bearer": "Desktop_Agent2"},
    )
    for name in ("UA_AGENT_ACCESS_ENABLED", "UA_APPROVAL_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=web_routes())),
        base_url="http://127.0.0.1",
    )


def test_http_rerequest_by_the_verified_operator(http, connector, monkeypatch):
    # The verified human operator needs no lifecycle-operator entry.
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Someone_Else")
    seen = []

    def fake(number, head_sha):
        seen.append((config.verified_agent_identity(), number, head_sha))
        github_app_worker._authorize_rerequest(config.verified_agent_identity())
        return {"status": "pending_approval", "approval_id": 7}

    monkeypatch.setattr(github_app_worker, "rerequest_head", fake)
    url = "/api/approvals/ledger-rerequest"
    peer = http.post(
        url,
        headers={"Authorization": "Bearer " + TOKENS["Desktop_Agent1"]},
        json={"number": 123, "head_sha": "head-1"},
    )
    assert peer.status_code == 403, peer.text
    auth = {"Authorization": "Bearer " + TOKENS[OPERATOR]}
    bad = http.post(url, headers=auth, json={"number": "x", "head_sha": "head-1"})
    assert bad.status_code == 400
    extra = http.post(url, headers=auth, json={"number": 1, "head_sha": "h", "approve": True})
    assert extra.status_code == 400
    ok = http.post(url, headers=auth, json={"number": 123, "head_sha": "head-1"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "pending_approval"
    assert seen[-1] == (OPERATOR, 123, "head-1")


@pytest.mark.parametrize("status", ["pending", "approved", "consumed", "denied"])
def test_rerequest_recovers_partial_status_failure_without_spending_pass(
    connector, monkeypatch, status
):
    original = connector.transport.create_check_run

    def fail(**kwargs):
        raise RuntimeError("synthetic status-post failure")

    monkeypatch.setattr(connector.transport, "create_check_run", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        _rerequest(connector)
    [row] = _github_rows()
    if status != "pending":
        with _as(OPERATOR):
            approvals.approval_resolve(row["id"], "deny" if status == "denied" else "approve")
        if status == "consumed":
            conn = storage.connect()
            try:
                conn.execute("UPDATE approvals SET status='consumed' WHERE id=?", (row["id"],))
                conn.commit()
            finally:
                conn.close()
    monkeypatch.setattr(connector.transport, "create_check_run", original)
    out = _rerequest(connector)
    assert out["approval_id"] == row["id"]
    assert approvals.get_approval(row["id"])["status"] == status
    binding = github_app_worker._record(github_app.PENDING_CHANNEL, out["event_id"])
    assert binding["approval_id"] == row["id"]
    assert binding["check_run_id"] == f"status:head-1|{connector.prs[0]['url']}"
    again = _rerequest(connector)
    assert again["status"] == "already_requested"
    assert approvals.get_approval(row["id"])["status"] == status
    assert len(connector.transport.created) == 1
    if status == "approved":
        results = github_app_worker.run_once(
            run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
        )
        assert results[0]["status"] == "approved"
        assert approvals.get_approval(row["id"])["status"] == "consumed"


def test_normal_worker_binds_status_before_consuming_after_partial_failure(connector, monkeypatch):
    original = connector.transport.create_check_run

    def fail(**kwargs):
        raise RuntimeError("synthetic status-post failure")

    monkeypatch.setattr(connector.transport, "create_check_run", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        _rerequest(connector)
    [row] = _github_rows()
    with _as(OPERATOR):
        approvals.approval_resolve(row["id"], "approve")
    monkeypatch.setattr(connector.transport, "create_check_run", original)
    first = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert first[0]["status"] == "already_requested"
    assert approvals.get_approval(row["id"])["status"] == "approved"
    assert github_app_worker._record(github_app.PENDING_CHANNEL, first[0]["event_id"])
    second = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert second[0]["status"] == "approved"
    assert approvals.get_approval(row["id"])["status"] == "consumed"


# --- review follow-ups (#2010) ----------------------------------------------------------


def _fail_status_posts(connector, monkeypatch):
    original = connector.transport.create_check_run

    def fail(**kwargs):
        raise RuntimeError("synthetic status-post failure")

    monkeypatch.setattr(connector.transport, "create_check_run", fail)
    return lambda: monkeypatch.setattr(connector.transport, "create_check_run", original)


def _orphan_row(connector, monkeypatch, *, resolve: str | None = None) -> dict:
    """Queue a row whose pending-status POST fails, so it has no binding."""
    restore = _fail_status_posts(connector, monkeypatch)
    with pytest.raises(RuntimeError, match="synthetic"):
        _rerequest(connector)
    restore()
    [row] = _github_rows()
    if resolve is not None:
        with _as(OPERATOR):
            approvals.approval_resolve(row["id"], resolve)
    payload = row["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    return {**row, "event_id": payload["event_id"]}


def _all_github_approvals() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, status, payload FROM approvals WHERE tool=? ORDER BY id",
            (github_app.TOOL_NAME,),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "status": r["status"], **json.loads(r["payload"])} for r in rows]


def _copy_row(approval_id: int, status: str) -> int:
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, "
            "updated_at, org_id) SELECT tool, payload, requested_by, ?, created_at, "
            "updated_at, org_id FROM approvals WHERE id=?",
            (status, approval_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_rerequest_after_a_title_edit_binds_the_approved_row(connector, monkeypatch):
    row = _orphan_row(connector, monkeypatch, resolve="approve")
    connector.prs[0] = _pr(sha="head-1", title="Release gate, retitled")

    out = _rerequest(connector)

    assert out["approval_id"] == row["id"]
    assert [a["id"] for a in _all_github_approvals()] == [row["id"]]
    assert approvals.get_approval(row["id"])["status"] == "approved"
    binding = github_app_worker._record(github_app.PENDING_CHANNEL, out["event_id"])
    # The binding carries the approved payload, not the edited snapshot.
    assert binding["approval_id"] == row["id"]
    assert binding["event"]["title"] == "Release gate"
    [result] = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert result["status"] == "approved" and result["approval_id"] == row["id"]
    assert approvals.get_approval(row["id"])["status"] == "consumed"


def test_worker_after_a_title_edit_binds_the_approved_row(connector, monkeypatch):
    row = _orphan_row(connector, monkeypatch, resolve="approve")
    connector.prs[0] = _pr(sha="head-1", title="Release gate, retitled")

    first = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert first[0]["approval_id"] == row["id"]
    assert [a["id"] for a in _all_github_approvals()] == [row["id"]]
    second = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )
    assert second[0]["status"] == "approved" and second[0]["approval_id"] == row["id"]


@pytest.mark.parametrize("action", [None, "deny"])
def test_rerequest_follows_policy_without_a_require_approval_rule(connector, action):
    from unified_agent.policy import PolicyConfig, PolicyRule

    rules = []
    if action == "deny":
        rules = [
            PolicyRule(name="no-ledger", tool=github_app.TOOL_NAME, type="tool", action="deny")
        ]
    with _as(OPERATOR), pytest.raises(ToolError, match="policy"):
        github_app_worker.rerequest_head(
            123,
            "head-1",
            run_gh=connector.gh,
            transport=connector.transport,
            policy_config=PolicyConfig(rules=rules),
        )
    assert _all_github_approvals() == []
    assert connector.transport.created == []


@pytest.mark.parametrize("older", ["denied", "approved", "pending"])
def test_rerequest_never_binds_a_decision_older_than_a_revoked_row(connector, monkeypatch, older):
    row = _orphan_row(
        connector, monkeypatch, resolve={"denied": "deny", "approved": "approve"}.get(older)
    )
    newest = _copy_row(row["id"], "revoked")

    with pytest.raises(ToolError, match="earlier"):
        _rerequest(connector)

    assert approvals.get_approval(row["id"])["status"] == older
    assert approvals.get_approval(newest)["status"] == "revoked"
    assert github_app_worker._record(github_app.PENDING_CHANNEL, row["event_id"]) is None
    assert connector.transport.created == []


@pytest.mark.parametrize("resolve", [None, "approve"])
def test_new_head_retires_an_unbound_row_for_an_older_head(connector, monkeypatch, resolve):
    old = _orphan_row(connector, monkeypatch, resolve=resolve)
    connector.prs[0] = _pr(sha="head-2")

    out = _rerequest(connector, head_sha="head-2")

    assert out["status"] == "pending_approval"
    retired = approvals.get_approval(old["id"])["status"]
    assert retired == ("revoked" if resolve == "approve" else "expired")
    assert github_app_worker._record(github_app.SUPERSEDED_CHANNEL, old["event_id"])
    assert [a["head_sha"] for a in _all_github_approvals() if a["status"] == "pending"] == [
        "head-2"
    ]


def test_worker_retires_an_unbound_row_for_an_older_head(connector, monkeypatch):
    old = _orphan_row(connector, monkeypatch)
    connector.prs[0] = _pr(sha="head-2")

    github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )

    assert approvals.get_approval(old["id"])["status"] == "expired"


def test_http_rerequest_keeps_the_profile_action_ceiling(http, connector, monkeypatch):
    from unified_agent import profiles

    # A lifecycle operator whose profile cannot run coordination_manage over MCP.
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1")
    monkeypatch.setitem(profiles.DEFAULT_PROFILES_BY_IDENTITY, "Desktop_Agent1", profiles.OPERATOR)
    reached = []
    monkeypatch.setattr(
        github_app_worker,
        "rerequest_head",
        lambda number, head_sha: reached.append(number) or {"status": "pending_approval"},
    )
    resp = http.post(
        "/api/approvals/ledger-rerequest",
        headers={"Authorization": "Bearer " + TOKENS["Desktop_Agent1"]},
        json={"number": 123, "head_sha": "head-1"},
    )
    assert resp.status_code == 403, resp.text
    assert reached == []


# --- independent review follow-ups (#2010) ----------------------------------------------


def test_worker_never_spends_an_older_pass_behind_a_revoked_orphan(connector, monkeypatch):
    row = _orphan_row(connector, monkeypatch, resolve="approve")
    _copy_row(row["id"], "revoked")

    [result] = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )

    assert result["status"] == "error"
    assert approvals.get_approval(row["id"])["status"] == "approved"
    assert connector.transport.created == []


def _bound_then_fenced(connector) -> tuple[dict, int]:
    first = _rerequest(connector)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    newest = _copy_row(first["approval_id"], "revoked")
    return first, newest


def test_worker_never_spends_a_bound_older_pass_behind_a_revoked_row(connector):
    first, _newest = _bound_then_fenced(connector)

    [result] = github_app_worker.run_once(
        run_gh=connector.gh, transport=connector.transport, policy_config=_policy()
    )

    assert result["status"] == "error"
    assert approvals.get_approval(first["approval_id"])["status"] == "approved"
    assert connector.transport.comments == []


def test_rerequest_does_not_report_a_bound_older_pass_behind_a_revoked_row(connector):
    first, newest = _bound_then_fenced(connector)

    with pytest.raises(ToolError, match="earlier"):
        _rerequest(connector)

    assert approvals.get_approval(first["approval_id"])["status"] == "approved"
    assert approvals.get_approval(newest)["status"] == "revoked"


def test_http_rerequest_runs_the_policy_gate(http, connector, tmp_path, monkeypatch):
    (tmp_path / "policy.yaml").write_text(
        "rules:\n"
        "  - name: no-ledger-rerequest\n"
        "    type: tool\n"
        "    tool: approval_ledger_rerequest\n"
        "    action: deny\n",
        encoding="utf-8",
    )
    reached = []
    monkeypatch.setattr(
        github_app_worker,
        "rerequest_head",
        lambda number, head_sha: reached.append(number) or {"status": "pending_approval"},
    )
    # A lifecycle operator on the developer profile: MCP refuses by policy...
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1")
    with _as("Desktop_Agent1"), pytest.raises(ToolError, match="denied by policy"):
        coordination_manage("approval_ledger_rerequest", {"number": 123, "head_sha": "head-1"})
    # ...and so does HTTP, for that caller and for the verified operator.
    for who in ("Desktop_Agent1", OPERATOR):
        resp = http.post(
            "/api/approvals/ledger-rerequest",
            headers={"Authorization": "Bearer " + TOKENS[who]},
            json={"number": 123, "head_sha": "head-1"},
        )
        assert resp.status_code == 403, (who, resp.text)
        assert "policy" in resp.json()["error"]
    assert reached == []
    assert _all_github_approvals() == []


# --- independent review, round 2 (#2010) ------------------------------------------------


def _copy_row_retitled(approval_id: int, status: str, title: str) -> int:
    conn = storage.connect()
    try:
        src = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        payload = json.loads(src["payload"])
        payload["title"] = title
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, "
            "updated_at, org_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                src["tool"],
                approvals.canonical_approval_payload(payload),
                src["requested_by"],
                status,
                src["created_at"],
                src["updated_at"],
                src["org_id"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_merged_head_never_spends_an_older_pass_behind_a_revoked_row(connector, monkeypatch):
    from unified_agent.audit import producer_sign

    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", lambda _: "worker-bearer")
    first = _rerequest(connector)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    newest = _copy_row_retitled(first["approval_id"], "revoked", "Release gate, retitled")
    merged = {**_pr(sha="head-1"), "state": "MERGED", "mergedAt": "2026-10-12T00:00:00Z"}

    def gh(cmd, **_kwargs):
        if "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(merged))
        return SimpleNamespace(returncode=0, stdout=json.dumps([]))

    results = github_app_worker.run_once(
        run_gh=gh, transport=connector.transport, policy_config=_policy()
    )

    assert [r["status"] for r in results] == ["merged_without_approval"]
    assert approvals.get_approval(first["approval_id"])["status"] != "consumed"
    assert approvals.get_approval(newest)["status"] == "revoked"
    assert connector.transport.comments == []
    assert all(u.get("conclusion") != "success" for u in connector.transport.updated)


def test_a_failed_rerequest_after_changes_is_still_audited(connector, monkeypatch):
    old = _orphan_row(connector, monkeypatch)
    connector.prs[0] = _pr(sha="head-2")
    before = len(_ledger())
    restore = _fail_status_posts(connector, monkeypatch)

    with pytest.raises(RuntimeError, match="synthetic"):
        _rerequest(connector, head_sha="head-2")
    restore()

    # The older head's row was retired and a fresh row queued: the operator
    # who started that is on the record, with the failure.
    assert approvals.get_approval(old["id"])["status"] == "expired"
    events_after = _ledger()[before:]
    assert len(events_after) == 1
    [event] = events_after
    assert event["actor"] == OPERATOR and event["_originator"] == OPERATOR
    assert event["head_sha"] == "head-2"
    assert event["outcome"] == "error" and event["error"] == "RuntimeError"


def _uncertain_review_comment(c, *, enriched=True):
    if enriched:
        first = _review_request(c)
    else:
        c.prs[0] = _pr(sha="a" * 40)
        first = _rerequest(c, head_sha="a" * 40)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    c.transport.fail_after_comment = True
    assert _poll_review(c)[0]["status"] == "error"
    assert approvals.get_approval(first["approval_id"])["status"] == "consumed"
    return first


def _poll_review(c):
    return github_app_worker.run_once(run_gh=c.gh, transport=c.transport, policy_config=_policy())


def _rewrite_review_summary(c, change):
    comment = c.transport.comments[0]
    marker = re.search(r"<!-- unified-agent github-evidence (\{.*?\}) -->", comment["body"])
    evidence = json.loads(marker.group(1))
    change(evidence)
    # The old signature authenticates the producer claim, not this summary.
    assert producer_sign.verify_producer_signature(evidence)
    comment["body"] = "<!-- unified-agent github-evidence " + json.dumps(evidence) + " -->"


@pytest.mark.parametrize("enriched", [False, True])
def test_recovery_rejects_forged_review_summary(connector, enriched):
    _uncertain_review_comment(connector, enriched=enriched)
    _rewrite_review_summary(
        connector,
        lambda evidence: evidence.update(
            independent_review={
                "version": 1,
                "author_count": 32,
                "verdict": "approve",
                "source": "human-attested-native-approval",
                "model": "forged/model",
            }
        ),
    )
    assert _poll_review(connector)[0]["status"] == "error"
    assert not any(item.get("conclusion") == "success" for item in connector.transport.updated)


@pytest.mark.parametrize(
    "mutation", ["absent", "null", "count_bool", "version_bool", "wrong_count", "extra"]
)
def test_recovery_requires_exact_native_review_summary(connector, mutation):
    _uncertain_review_comment(connector)

    def change(evidence):
        if mutation == "absent":
            evidence.pop("independent_review")
        elif mutation == "null":
            evidence["independent_review"] = None
        else:
            field, value = {
                "count_bool": ("author_count", True),
                "version_bool": ("version", True),
                "wrong_count": ("author_count", 2),
                "extra": ("model", "forged/model"),
            }[mutation]
            evidence["independent_review"][field] = value

    _rewrite_review_summary(connector, change)
    assert _poll_review(connector)[0]["status"] == "error"


@pytest.mark.parametrize("enriched", [False, True])
def test_unchanged_review_comment_recovers_once(connector, enriched):
    first = _uncertain_review_comment(connector, enriched=enriched)
    recovered = _poll_review(connector)[0]
    assert recovered["status"] == "approved"
    assert recovered["approval_id"] == first["approval_id"]
    assert len(connector.transport.comments) == 1


@pytest.mark.parametrize("phase", ["find", "post", "status"])
def test_review_transport_mutation_never_records_success(connector, monkeypatch, phase):
    if phase == "find":
        first = _uncertain_review_comment(connector)
    else:
        first = _review_request(connector)
        with _as(OPERATOR):
            approvals.approval_resolve(first["approval_id"], "approve")
    captured = []
    original_row = github_app._approval_row

    def capture(event):
        captured.append(event)
        return original_row(event)

    monkeypatch.setattr(github_app, "_approval_row", capture)
    method = {
        "find": "find_evidence_comment",
        "post": "create_evidence_comment",
        "status": "update_check_run",
    }[phase]
    original_transport = getattr(connector.transport, method)

    def mutate(**kwargs):
        result = original_transport(**kwargs)
        captured[-1].independent_review["reviewer"]["subject"] = "t3:mutated-after-transport"
        return result

    monkeypatch.setattr(connector.transport, method, mutate)
    assert _poll_review(connector)[0]["status"] == "error"
    assert github_app_worker._record(github_app.EVIDENCE_CHANNEL, first["event_id"]) is None
