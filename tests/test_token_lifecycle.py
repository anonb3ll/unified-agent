"""Tests for agent bearer-token lifecycle as a platform feature (task #1198).

Covers: the ``agent_tokens`` migration (hash-only columns), the issuance path
through ``managed_secret_set``, the hash-only guarantee (no raw value is ever
stored, returned, or reachable via the DB/event log), doctor's expiry-age
warning, and the hash-chained rotation event.

Every token value in this file is an obviously-synthetic placeholder — never
a real credential.
"""

from __future__ import annotations

import json

import pytest

from unified_agent import config, storage
from unified_agent.tools import approvals, token_lifecycle
from unified_agent.tools.events import event_log_verify

SYNTHETIC_TOKEN_A = "synthetic-test-token-aaaa-0001"
SYNTHETIC_TOKEN_B = "synthetic-test-token-bbbb-0002"


def _approve_and_run(fn, *args, **kwargs):
    """Run a call that routes through managed_secret_set's force=True approval
    gate (every managed_secret_set call — hence every agent_token_issue /
    agent_token_rotate call — requires one human approval pass, exactly like
    an operator using this feature for real would hit). Auto-approves so
    lifecycle tests exercise the real call path end to end."""
    try:
        return fn(*args, **kwargs)
    except approvals.ApprovalPendingError as pending:
        # The fixture identity is the requester. A different human has to approve.
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            approvals.approval_resolve(pending.approval_id, "approve")
        finally:
            config.agent_identity_var.reset(token)
        return fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    storage._connection = None
    storage.init_db()
    token = config.agent_identity_var.set("Desktop_Agent1")
    yield
    config.agent_identity_var.reset(token)
    storage._connection = None


def test_agent_tokens_table_exists_with_hash_only_columns():
    conn = storage.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()}
    finally:
        conn.close()
    assert {
        "id",
        "agent_label",
        "token_hash",
        "hash_algo",
        "status",
        "issued_at",
        "expires_at",
        "rotated_from",
        "rotated_at",
        "revoked_at",
        "issued_by",
        "note",
    } <= cols
    # No column can hold a plaintext value — the guarantee is structural.
    assert "value" not in cols
    assert "token_value" not in cols
    assert "plaintext" not in cols


def test_issue_returns_hash_never_raw_value():
    result = _approve_and_run(
        token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A, ttl_days=30
    )

    assert SYNTHETIC_TOKEN_A not in json.dumps(result)
    assert result["token_hash"] != SYNTHETIC_TOKEN_A
    assert len(result["token_hash"]) == 64  # sha256 hex digest
    assert result["agent_label"] == "Desktop_Agent1"
    assert result["rotated_from"] is None
    assert result["expires_at"] is not None


def test_issue_digest_matches_managed_secret_set_digest():
    """The hash-only guarantee: token_hash IS managed_secret_set's own digest,
    not an independently computed one — there is exactly one place in this
    codebase that ever touches the raw value."""
    import hashlib

    expected = hashlib.sha256(SYNTHETIC_TOKEN_A.encode()).hexdigest()
    result = _approve_and_run(
        token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A
    )
    assert result["token_hash"] == expected


def test_raw_value_never_lands_in_db_rows():
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    conn = storage.connect()
    try:
        for table in ("agent_tokens", "event_log", "agent_events"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert SYNTHETIC_TOKEN_A not in repr([tuple(r) for r in rows])
    finally:
        conn.close()


def test_redacted_value_repr_never_leaks():
    wrapped = token_lifecycle.RedactedValue(SYNTHETIC_TOKEN_A)
    assert SYNTHETIC_TOKEN_A not in repr(wrapped)
    assert SYNTHETIC_TOKEN_A not in str(wrapped)
    assert wrapped.reveal() == SYNTHETIC_TOKEN_A


def test_list_returns_hash_only_metadata():
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    listing = token_lifecycle.agent_token_list(agent_label="Desktop_Agent1")
    assert len(listing["tokens"]) == 1
    row = listing["tokens"][0]
    assert row["status"] == "active"
    assert SYNTHETIC_TOKEN_A not in json.dumps(listing)


def test_rotate_links_new_row_marks_old_rotated_and_never_leaks_either_value():
    first = _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    result = _approve_and_run(
        token_lifecycle.agent_token_rotate, "Desktop_Agent1", SYNTHETIC_TOKEN_B
    )

    assert result["previous_token_id"] == first["token_id"]
    assert result["new_token_id"] != first["token_id"]
    assert SYNTHETIC_TOKEN_A not in json.dumps(result)
    assert SYNTHETIC_TOKEN_B not in json.dumps(result)

    listing = token_lifecycle.agent_token_list(agent_label="Desktop_Agent1")
    by_id = {row["id"]: row for row in listing["tokens"]}
    assert by_id[first["token_id"]]["status"] == "rotated"
    assert by_id[result["new_token_id"]]["status"] == "active"
    assert by_id[result["new_token_id"]]["rotated_from"] == first["token_id"]


def test_issued_token_authenticates_live_and_rotation_retires_it(monkeypatch):
    """Task #1826: issuance is not bookkeeping only. config.agent_tokens(), the
    per-request bearer lookup, merges active managed tokens, so an issued value
    authenticates at once and a rotated-away managed value stops."""
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    assert config.agent_tokens().get(SYNTHETIC_TOKEN_A) == "Desktop_Agent1"

    _approve_and_run(token_lifecycle.agent_token_rotate, "Desktop_Agent1", SYNTHETIC_TOKEN_B)
    live = config.agent_tokens()
    assert live.get(SYNTHETIC_TOKEN_B) == "Desktop_Agent1"
    assert SYNTHETIC_TOKEN_A not in live


def test_rotate_emits_hash_chained_event():
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    _approve_and_run(token_lifecycle.agent_token_rotate, "Desktop_Agent1", SYNTHETIC_TOKEN_B)

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE channel = 'security.agent_token.rotated' "
            "ORDER BY ROWID DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    # Chained: prev_hash/entry_hash populated by the same machinery every
    # other evidence-ledger entry uses (events.record_event_once).
    assert row["entry_hash"]
    assert row["prev_hash"]
    payload = json.loads(row["payload"])
    assert SYNTHETIC_TOKEN_A not in json.dumps(payload)
    assert SYNTHETIC_TOKEN_B not in json.dumps(payload)
    assert "new_token_hash" in payload


def test_rotate_verifies_against_event_log_verify():
    """The chain this rotation appends to must still pass the ledger's own
    tamper-evidence check (events.event_log_verify), not merely have hash
    columns populated."""
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    _approve_and_run(token_lifecycle.agent_token_rotate, "Desktop_Agent1", SYNTHETIC_TOKEN_B)
    report = event_log_verify()
    assert report["status"] == "valid", report
    assert report["verified"] is True, report


def test_doctor_dormant_with_no_tokens_recorded():
    from unified_agent.tools.doctor import system_doctor

    checks = {c["capability"]: c for c in system_doctor()["checks"]}
    assert checks["agent_token_lifecycle"]["status"] == "DORMANT"


def test_doctor_live_when_tokens_fresh():
    from unified_agent.tools.doctor import system_doctor

    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    checks = {c["capability"]: c for c in system_doctor()["checks"]}
    assert checks["agent_token_lifecycle"]["status"] == "LIVE"
    assert checks["agent_token_lifecycle"]["gate"] == "UA_TOKEN_MAX_AGE_DAYS"


def test_doctor_misconfigured_when_token_older_than_max_age(monkeypatch):
    from unified_agent.tools.doctor import system_doctor

    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agent_tokens SET issued_at = '2000-01-01T00:00:00Z' WHERE agent_label = ?",
            ("Desktop_Agent1",),
        )
        conn.commit()
    finally:
        conn.close()
    checks = {c["capability"]: c for c in system_doctor()["checks"]}
    assert checks["agent_token_lifecycle"]["status"] == "MISCONFIGURED"
    assert "Desktop_Agent1" in checks["agent_token_lifecycle"]["detail"]


def test_token_max_age_days_config_default_and_override(monkeypatch):
    monkeypatch.delenv("UA_TOKEN_MAX_AGE_DAYS", raising=False)
    assert config.token_max_age_days() == 90
    monkeypatch.setenv("UA_TOKEN_MAX_AGE_DAYS", "30")
    assert config.token_max_age_days() == 30
    monkeypatch.setenv("UA_TOKEN_MAX_AGE_DAYS", "not-a-number")
    assert config.token_max_age_days() == 90


def test_issue_requires_authenticated_identity():
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(Exception, match="authenticated"):
            _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    finally:
        config.agent_identity_var.reset(token)


def test_duplicate_value_hash_is_rejected():
    _approve_and_run(token_lifecycle.agent_token_issue, "Desktop_Agent1", SYNTHETIC_TOKEN_A)
    with pytest.raises(Exception, match="already recorded"):
        _approve_and_run(token_lifecycle.agent_token_issue, "Laptop_Agent1", SYNTHETIC_TOKEN_A)


# --- operator issuance for `unified-agent agent add` (task #2453) --------------


def test_operator_issue_works_without_managed_mode_and_records_metadata(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.delenv("UA_MANAGED_SECRETS")
    operator = config.operator_label()
    # An unencrypted store is refused unless the operator opts in (task #2453).
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS", raising=False)
    with pytest.raises(Exception, match="UA_MANAGED_SECRET_KEYS"):
        token_lifecycle.operator_issue("Local_Worker", actor=operator)
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    value, issued = token_lifecycle.operator_issue(
        "Local_Worker", actor=operator, ttl_days=10, agent_kind="cloud"
    )
    raw = value.reveal()
    assert "redacted" in repr(value) and raw not in json.dumps(issued)
    assert config.agent_tokens()[raw] == "Local_Worker"
    [row] = token_lifecycle.agent_token_list("Local_Worker")["tokens"]
    assert (row["status"], row["issued_by"]) == ("active", operator)
    assert row["expires_at"] and row["token_hash"] == issued["token_hash"]
    assert config.is_cloud_hosted_identity("Local_Worker")  # the kind rides the row
    with pytest.raises(Exception, match="already has an active managed token"):
        token_lifecycle.operator_issue("Local_Worker", actor=operator)


def test_operator_issue_requires_a_lifecycle_operator(monkeypatch):
    monkeypatch.delenv("UA_MANAGED_SECRETS")
    with pytest.raises(Exception, match="operator action"):
        token_lifecycle.operator_issue("Local_Worker", actor="Desktop_Agent1")
    assert token_lifecycle.agent_token_list("Local_Worker")["tokens"] == []


def test_operator_secret_write_is_limited_to_agent_tokens(monkeypatch):
    from unified_agent import managed_secrets

    monkeypatch.delenv("UA_MANAGED_SECRETS")
    with pytest.raises(Exception, match="agent-token secret"):
        managed_secrets.set_secret(
            "zulip-bot", "x", requested_by="op", consumer="zulip", operator_agent_token=True
        )
    with pytest.raises(Exception, match="managed secret mode is disabled"):
        managed_secrets.set_secret(
            "agent-token:Local_Worker", "x", requested_by="op", consumer="agent_token"
        )
