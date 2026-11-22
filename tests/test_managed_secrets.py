from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import managed_secrets


def test_set_returns_metadata_only_and_internal_consumer_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    result = managed_secrets.set_secret(
        "zulip:Desktop_Agent2", "synthetic-secret", requested_by="Desktop_Agent2"
    )
    assert result["stored"] is True
    assert "value" not in result
    assert "synthetic-secret" not in json.dumps(result)
    path = tmp_path / "ua" / "managed-secrets" / "zulip:Desktop_Agent2"
    assert path.stat().st_mode & 0o777 == 0o600
    assert managed_secrets._load_for_consumer("zulip:Desktop_Agent2", "zulip") == "synthetic-secret"


def test_encrypted_secret_does_not_store_plaintext_and_requires_key(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    managed_secrets.set_secret(
        "oauth:google",
        "synthetic-refresh-token",
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    path = tmp_path / "ua" / "managed-secrets" / "oauth:google"
    assert "synthetic-refresh-token" not in path.read_text()
    assert (
        managed_secrets._load_for_consumer("oauth:google", "connector") == "synthetic-refresh-token"
    )
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS")
    with pytest.raises(ToolError, match="key is unavailable"):
        managed_secrets._load_for_consumer("oauth:google", "connector")


def test_encrypted_secret_key_rotation_reads_old_key(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", old)
    managed_secrets.set_secret(
        "oauth:google",
        "synthetic-refresh-token",
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", f"{new},{old}")
    assert (
        managed_secrets._load_for_consumer("oauth:google", "connector") == "synthetic-refresh-token"
    )
    managed_secrets.set_secret(
        "oauth:google", "new-token", requested_by="Desktop_Agent2", consumer="connector"
    )
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", new)
    assert managed_secrets._load_for_consumer("oauth:google", "connector") == "new-token"


def test_bad_managed_secret_key_refuses_write(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", "invalid")
    with pytest.raises(ToolError, match="valid Fernet keys"):
        managed_secrets.set_secret(
            "oauth:google",
            "synthetic-refresh-token",
            requested_by="Desktop_Agent2",
            consumer="connector",
        )
    assert not (tmp_path / "ua" / "managed-secrets" / "oauth:google").exists()


def test_missing_key_cannot_downgrade_or_create_plaintext_after_encryption(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    managed_secrets.set_secret("x", "encrypted-first", requested_by="Desktop_Agent2")
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS")
    with pytest.raises(ToolError, match="key is required"):
        managed_secrets.set_secret("x", "downgrade", requested_by="Desktop_Agent2")
    with pytest.raises(ToolError, match="key is required"):
        managed_secrets.set_secret("y", "new-plaintext", requested_by="Desktop_Agent2")
    root = tmp_path / "ua" / "managed-secrets"
    assert "downgrade" not in (root / "x").read_text()
    assert not (root / "y").exists()


def test_valid_key_can_replace_damaged_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    managed_secrets.set_secret("x", "old", requested_by="Desktop_Agent2")
    path = tmp_path / "ua" / "managed-secrets" / "x"
    path.write_text("{damaged", encoding="utf-8")
    managed_secrets.set_secret("x", "replacement", requested_by="Desktop_Agent2")
    assert managed_secrets._load_for_consumer("x", "zulip") == "replacement"


def test_invalid_key_refused_before_secret_approval(monkeypatch, tmp_path):
    from unified_agent import config
    from unified_agent.tools import profiles

    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", "invalid")
    monkeypatch.setattr(
        profiles, "check_approval_gate", lambda *_args, **_kwargs: pytest.fail("approval consumed")
    )
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="valid Fernet keys"):
            profiles.managed_secret_set("x", "synthetic-secret")
    finally:
        config.agent_identity_var.reset(token)


def test_public_read_and_metadata_never_return_value(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    managed_secrets.set_secret("x", "synthetic-secret", requested_by="Desktop_Agent2")
    with pytest.raises(managed_secrets.SecretReadDenied, match="write-only"):
        managed_secrets.read_secret("x")
    assert "synthetic-secret" not in json.dumps(managed_secrets.metadata("x"))


def test_consumer_mismatch_and_missing_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    managed_secrets.set_secret(
        "x", "synthetic-secret", requested_by="Desktop_Agent2", consumer="zulip"
    )
    with pytest.raises(Exception, match="consumer"):
        managed_secrets._load_for_consumer("x", "other")
    with pytest.raises(Exception, match="not configured"):
        managed_secrets._load_for_consumer("missing", "zulip")


def test_secret_root_is_not_readable_by_file_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    managed_secrets.set_secret("x", "synthetic-secret", requested_by="Desktop_Agent2")
    from unified_agent.tools.files import fs_read

    with pytest.raises(Exception, match="write-only"):
        fs_read(str(tmp_path / "ua" / "managed-secrets" / "x"))


def test_registered_secret_sink_policy_approval_is_one_pass(monkeypatch, tmp_path):
    from unified_agent import config, server, storage
    from unified_agent.tools import approvals

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\nenabled: true\nrules:\n  - name: secret-review\n    type: tool\n    tool: managed_secret_set\n    action: require_approval\n"
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    token = config.agent_identity_var.set("Desktop_Agent2")
    marker = "synthetic-approval-redaction-marker"
    fn = server.REGISTERED_TOOLS["managed_secret_set"]
    try:
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            fn("zulip:Desktop_Agent2", marker)
        approval_id = pending.value.approval_id
        row = approvals.get_approval(approval_id)
        assert marker not in json.dumps(row)
        assert set(row["payload"]) == {"name", "consumer", "identity", "value_sha256"}
        human = config.agent_identity_var.set("Desktop_Person")
        try:
            approvals.approval_resolve(approval_id, "approve")
        finally:
            config.agent_identity_var.reset(human)
        result = fn("zulip:Desktop_Agent2", marker)
        assert result["stored"] is True and marker not in json.dumps(result)
        assert approvals.get_approval(approval_id)["status"] == "consumed"
        assert not approvals.approval_pending()["pending"]
        with pytest.raises(approvals.ApprovalPendingError):
            fn("zulip:Desktop_Agent2", marker)
        with storage.connect() as conn:
            for table in ("approvals", "event_log", "agent_events", "agent_perf_samples"):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                assert marker not in repr([tuple(row) for row in rows])
    finally:
        config.agent_identity_var.reset(token)


def test_secret_gate_refuses_unauthenticated_and_denied_writes(monkeypatch):
    from unified_agent import config, server
    from unified_agent.tools import approvals

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    fn = server.REGISTERED_TOOLS["managed_secret_set"]
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(Exception, match="authenticated"):
            fn("x", "synthetic-secret")
        assert not approvals.approval_pending()["pending"]
        config.agent_identity_var.set("Desktop_Agent2")
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            fn("x", "synthetic-secret")
        approvals.approval_resolve(pending.value.approval_id, "deny", resolved_by="Operator")
        with pytest.raises(Exception, match="DENIED"):
            fn("x", "synthetic-secret")
        assert not (managed_secrets._root() / "x").exists()
    finally:
        config.agent_identity_var.reset(token)


def test_file_symlink_and_hardlink_cannot_read_managed_value(monkeypatch, tmp_path):
    import os

    from unified_agent import config
    from unified_agent.tools.files import fs_read, fs_search

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    managed_secrets.set_secret("x", "synthetic-secret", requested_by="Desktop_Agent2")
    path = managed_secrets._root() / "x"
    alias = config.workspace_root() / "alias"
    alias.symlink_to(path)
    with pytest.raises(ToolError):
        fs_read(str(alias))
    assert fs_search("synthetic-secret")["matches"] == []
    linked = tmp_path / "linked"
    os.link(path, linked)
    with pytest.raises(Exception, match="single owner-only"):
        managed_secrets._load_for_consumer("x", "zulip")
    with pytest.raises(Exception, match="single owner-only"):
        managed_secrets.metadata("x")


def test_one_approved_row_cannot_be_consumed_by_two_callers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from unified_agent import config, storage
    from unified_agent.tools import approvals

    payload = {"name": "race", "value_sha256": "synthetic-digest"}
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        approvals.check_approval_gate("managed_secret_set", payload, force=True)
    human = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(human)
    original = storage.connect
    barrier = Barrier(2)

    class Connection:
        def __init__(self):
            self.inner = original()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            cursor = self.inner.execute(sql, *args)
            if sql.startswith("SELECT * FROM approvals WHERE tool="):
                rows = cursor.fetchall()
                barrier.wait(timeout=5)
                return SimpleNamespace(fetchall=lambda: rows)
            return cursor

    monkeypatch.setattr(storage, "connect", Connection)

    def attempt():
        try:
            return approvals.check_approval_gate("managed_secret_set", payload, force=True)["gated"]
        except approvals.ApprovalPendingError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]


def test_secret_approval_is_bound_to_verified_identity(monkeypatch):
    from unified_agent import config, server
    from unified_agent.tools import approvals

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    fn = server.REGISTERED_TOOLS["managed_secret_set"]
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            fn("x", "synthetic-secret")
        approval_id = pending.value.approval_id
        human = config.agent_identity_var.set("Desktop_Person")
        try:
            approvals.approval_resolve(approval_id, "approve")
        finally:
            config.agent_identity_var.reset(human)
        config.agent_identity_var.set("Desktop_Agent1")
        with pytest.raises(approvals.ApprovalPendingError):
            fn("x", "synthetic-secret")
        assert approvals.get_approval(approval_id)["status"] == "approved"
        assert not (managed_secrets._root() / "x").exists()
    finally:
        config.agent_identity_var.reset(token)
