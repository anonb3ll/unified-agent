"""Tenant (org_id) fence on policy-authorization reads (task #1798).

``events.record_event`` stamps every ``event_log`` row with
``edition.tenant_org_id()``, but ``github_app.list_policy_authorizations``
and the merge-time read ``verified_policy_authorization`` selected rows by
channel and pull-request binding alone: one tenant could list another
tenant's authorizations, and another tenant's signed record could cover
this tenant's merge. Both reads now carry ``org_id IS ?``, so a record
written under another tenant reads exactly like none was recorded. The
untenanted private hub (tenant ``None``) keeps matching NULL rows via
``IS``.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from unified_agent import config, github_app, storage
from unified_agent.audit import producer_sign

STEWARD = "Desktop_Agent1"
AUTHOR = "Desktop_Agent2"
REPO = "example/project"
NUMBER = 123
HEAD = "a" * 40
POLICY_REF = "deploy-steward-2026-10-01"
BRANCH = "task/ship"
BEARERS = {
    "steward-bearer": STEWARD,
    "worker-bearer": AUTHOR,
}

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture(autouse=True)
def ledger_env(monkeypatch):
    monkeypatch.setenv(github_app.POLICY_AUTHORIZERS_VAR, STEWARD)
    monkeypatch.delenv(github_app.POLICY_SELF_AUTHORIZATION_VAR, raising=False)
    monkeypatch.setattr(config, "agent_tokens", lambda *a, **k: dict(BEARERS))
    by_label = {label: bearer for bearer, label in BEARERS.items()}
    monkeypatch.setattr(producer_sign, "resolve_agent_bearer_token", by_label.get)
    storage.init_db()


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)

    def _switch(org: str | None) -> None:
        if org is None:
            monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        else:
            monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


@contextmanager
def as_identity(label: str | None):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _task() -> int:
    now = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, status, priority, claimed_by, branch, created_at, "
            "updated_at) VALUES ('ship a fix', 'doing', 'med', ?, ?, ?, ?)",
            (AUTHOR, BRANCH, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _authorize(task_id: int) -> dict:
    with as_identity(STEWARD):
        return github_app.record_policy_authorization(
            repo=REPO,
            number=NUMBER,
            head_sha=HEAD,
            policy_ref=POLICY_REF,
            task_id=task_id,
        )


def _verified() -> dict | None:
    return github_app.verified_policy_authorization(repo=REPO, number=NUMBER, head_sha=HEAD)


def _listed(head_sha: str | None = None) -> list[dict]:
    return github_app.list_policy_authorizations(repo=REPO, number=NUMBER, head_sha=head_sha)


def test_record_under_org_a_reads_like_missing_under_org_b(as_org):
    as_org(ORG_A)
    record = _authorize(_task())
    assert _verified()["authorization_id"] == record["authorization_id"]
    assert [row["authorization_id"] for row in _listed()] == [record["authorization_id"]]

    as_org(ORG_B)
    assert _verified() is None
    assert _listed() == []
    assert _listed(head_sha=HEAD) == []

    as_org(ORG_A)
    assert _verified()["authorization_id"] == record["authorization_id"]
    assert [row["authorization_id"] for row in _listed()] == [record["authorization_id"]]


def test_org_b_reads_only_its_own_records(as_org):
    as_org(ORG_A)
    a_record = _authorize(_task())
    as_org(ORG_B)
    b_record = _authorize(_task())
    assert _verified()["authorization_id"] == b_record["authorization_id"]
    assert [row["authorization_id"] for row in _listed()] == [b_record["authorization_id"]]
    as_org(ORG_A)
    assert [row["authorization_id"] for row in _listed()] == [a_record["authorization_id"]]


def test_untenanted_hub_reads_only_untenanted_records(as_org):
    as_org(None)
    untenanted = _authorize(_task())
    as_org(ORG_A)
    a_record = _authorize(_task())

    # The foreign record is newer, so an unfenced read would return it.
    as_org(None)
    assert _verified()["authorization_id"] == untenanted["authorization_id"]
    assert [row["authorization_id"] for row in _listed()] == [untenanted["authorization_id"]]

    as_org(ORG_A)
    assert _verified()["authorization_id"] == a_record["authorization_id"]
    assert [row["authorization_id"] for row in _listed()] == [a_record["authorization_id"]]
