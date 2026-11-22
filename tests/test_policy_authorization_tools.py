"""MCP tools for policy authorizations (task #1798).

`policy_authorization_record` lets a `UA_POLICY_AUTHORIZERS` member cite a
standing policy for one exact pull-request head under its own verified
identity; `policy_authorization_list` reads the records back newest first
with each row's current validity. Both raise `ToolError` on refusal, and
neither ever returns a signature.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, github_app, profiles, storage
from unified_agent.audit import producer_sign
from unified_agent.tools import policy_authorization

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


BRANCH = "task/ship"


def _task(
    claimed_by: str | None = AUTHOR, branch: str | None = BRANCH, status: str = "doing"
) -> int:
    now = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, status, priority, claimed_by, branch, created_at, "
            "updated_at) VALUES ('ship a fix', ?, 'med', ?, ?, ?, ?)",
            (status, claimed_by, branch, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _record(task_id: int, *, head: str = HEAD, identity: str = STEWARD, number: int = 123):
    with as_identity(identity):
        return policy_authorization.policy_authorization_record(
            repo="example/project",
            pr=number,
            head_sha=head,
            policy_ref=POLICY_REF,
            task_id=task_id,
        )


def _list(*, head: str | None = None, number: int = 123):
    return policy_authorization.policy_authorization_list(
        repo="example/project", pr=number, head_sha=head
    )


# --- policy_authorization_record ---------------------------------------------


def test_record_refuses_a_non_authorizer():
    task_id = _task()
    with pytest.raises(ToolError, match="not a configured policy authorizer"):
        _record(task_id, identity=OTHER)
    assert _list()["authorizations"] == []


def test_record_refuses_when_no_authorizers_are_configured(monkeypatch):
    monkeypatch.delenv(github_app.POLICY_AUTHORIZERS_VAR)
    task_id = _task()
    with pytest.raises(ToolError, match="not a configured policy authorizer"):
        _record(task_id)


def test_record_refuses_self_authorization_by_default():
    task_id = _task(claimed_by=STEWARD)
    with pytest.raises(ToolError, match="its own"):
        _record(task_id)


def test_record_allows_self_authorization_only_when_enabled(monkeypatch):
    monkeypatch.setenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, "1")
    task_id = _task(claimed_by=STEWARD)
    record = _record(task_id)
    assert record["authorized_by"] == STEWARD


def test_record_binds_the_verified_identity_and_never_returns_a_signature():
    task_id = _task()
    record = _record(task_id)
    assert record["status"] == "recorded"
    assert record["authorized_by"] == STEWARD
    assert record["head_sha"] == HEAD
    assert "signature" not in record
    assert "scheme" not in record


def test_record_requires_a_verified_identity():
    task_id = _task()
    with as_identity(None), pytest.raises(ToolError, match="verified"):
        _record(task_id, identity=None)


def test_record_refuses_a_closed_task():
    task_id = _task(status="done")
    with pytest.raises(ToolError, match="done"):
        _record(task_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "abc123"),
        ("head_sha", "Z" * 40),
        ("policy_ref", "has spaces"),
        ("repo", "not-a-repo"),
        ("pr", 0),
        ("task_id", 0),
    ],
)
def test_record_validates_its_binding(field, value):
    task_id = _task()
    kwargs = {
        "repo": "example/project",
        "pr": 123,
        "head_sha": HEAD,
        "policy_ref": POLICY_REF,
        "task_id": task_id,
    }
    kwargs[field] = value
    with as_identity(STEWARD), pytest.raises(ToolError):
        policy_authorization.policy_authorization_record(**kwargs)


# --- policy_authorization_list -----------------------------------------------


def test_list_returns_authorizations_newest_first_with_valid_flags():
    first = _record(_task())
    second = _record(_task())
    result = _list()
    rows = result["authorizations"]
    assert [row["authorization_id"] for row in rows] == [
        second["authorization_id"],
        first["authorization_id"],
    ]
    assert all(row["valid"] for row in rows)
    assert all(row["head_sha"] == HEAD for row in rows)
    assert all("signature" not in row and "scheme" not in row for row in rows)
    assert result["count"] == 2


def test_list_filters_to_the_exact_head():
    _record(_task())
    _record(_task(), head=OLD_HEAD)
    assert [row["head_sha"] for row in _list(head=HEAD)["authorizations"]] == [HEAD]
    assert [row["head_sha"] for row in _list(head=OLD_HEAD)["authorizations"]] == [OLD_HEAD]
    assert len(_list()["authorizations"]) == 2


def test_list_is_bound_to_the_exact_head_for_validity():
    """An authorization for one head is not a valid authorization for another."""
    _record(_task())
    assert _list(head=OLD_HEAD)["authorizations"] == []
    assert _list(head=OLD_HEAD)["valid_for_head"] is None
    assert _list(head=HEAD)["valid_for_head"]["head_sha"] == HEAD


def test_valid_for_head_is_the_newest_valid_row_or_none():
    recorded = _record(_task())
    valid = _list(head=HEAD)["valid_for_head"]
    assert valid["authorization_id"] == recorded["authorization_id"]
    assert valid["policy_ref"] == POLICY_REF
    # No head requested: there is nothing to be valid for.
    assert _list()["valid_for_head"] is None
    # A different pull request has no rows at all.
    empty = _list(number=124)
    assert empty["authorizations"] == []
    assert empty["valid_for_head"] is None


def test_list_marks_rows_invalid_when_the_authorizer_is_removed(monkeypatch):
    _record(_task())
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, OTHER)
    row = _list()["authorizations"][0]
    assert row["valid"] is False
    assert _list(head=HEAD)["valid_for_head"] is None


def test_list_marks_rows_invalid_on_a_rotated_bearer(monkeypatch):
    _record(_task())
    monkeypatch.setattr(
        producer_sign,
        "resolve_agent_bearer_token",
        lambda label: "rotated-bearer" if label == STEWARD else None,
    )
    assert _list()["authorizations"][0]["valid"] is False


def test_list_marks_rows_invalid_when_the_self_rule_turns_back_on(monkeypatch):
    monkeypatch.setenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, "1")
    _record(_task(claimed_by=STEWARD))
    monkeypatch.delenv(github_app.POLICY_SELF_AUTHORIZATION_VAR)
    assert _list()["authorizations"][0]["valid"] is False


def test_list_marks_a_tampered_or_forged_row_invalid():
    _record(_task())
    stored = events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL])
    forged = {**stored[0]["payload"], "task_claimant": OTHER}
    events.record_event(
        github_app.POLICY_AUTHORIZATION_CHANNEL,
        payload=forged,
        originator=STEWARD,
    )
    rows = _list()["authorizations"]
    assert len(rows) == 2
    # Newest first: the forged row sorts ahead and does not verify.
    assert rows[0]["valid"] is False
    assert rows[0]["task_claimant"] == OTHER
    assert rows[1]["valid"] is True
    # The one valid row still answers the head query.
    assert _list(head=HEAD)["valid_for_head"]["task_claimant"] == AUTHOR


def test_list_never_returns_a_signature():
    _record(_task())
    stored = events.query_events(channels=[github_app.POLICY_AUTHORIZATION_CHANNEL])
    signature = stored[0]["payload"]["signature"]
    assert signature
    import json

    assert signature not in json.dumps(_list())
    assert signature not in json.dumps(_list(head=HEAD))


def test_list_validates_its_arguments():
    with pytest.raises(ToolError):
        _list(number=0)
    with pytest.raises(ToolError):
        policy_authorization.policy_authorization_list(repo="bad", pr=1)
    with pytest.raises(ToolError):
        _list(head="abc")


# --- registration, classification and the developer ceiling -------------------


def test_both_tools_are_registered_and_classified():
    from unified_agent import server, tool_classification

    assert "policy_authorization_record" in server.REGISTERED_TOOLS
    assert "policy_authorization_list" in server.REGISTERED_TOOLS
    assert (
        tool_classification.classify_tool("policy_authorization_record")
        == tool_classification.ToolExposureClass.WITHHELD
    )
    assert (
        tool_classification.classify_tool("policy_authorization_list")
        == tool_classification.ToolExposureClass.OMITTED
    )


def test_developer_profile_gains_record_and_stays_under_the_ceiling():
    """record joins the developer surface; list and one rarely used existing
    tool (dispatch_delivery_receipts_list, an operator receipts pane read)
    are pruned so the profile holds at the 100-tool ceiling."""
    from unified_agent import server

    exposed = profiles._allowed_tools_for_profile(profiles.DEVELOPER, set(server.REGISTERED_TOOLS))
    assert len(exposed) <= 100
    assert "policy_authorization_record" in exposed
    assert "policy_authorization_list" not in exposed
    assert "dispatch_delivery_receipts_list" not in exposed
    # Pruned, not deleted: an explicit profile still reaches them.
    for name in ("policy_authorization_list", "dispatch_delivery_receipts_list"):
        assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, name), name
        assert profiles.tool_is_allowed_for(profiles.STEWARD, name), name


def test_both_tools_belong_to_the_admin_toolset():
    from unified_agent import toolsets

    assert toolsets.ASSIGNMENTS["policy_authorization_record"] == "admin"
    assert toolsets.ASSIGNMENTS["policy_authorization_list"] == "admin"
