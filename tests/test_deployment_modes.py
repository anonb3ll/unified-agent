"""Tests for canonical deployment modes and instance claim flow (task #1422, Parity 17)."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, managed_secrets, storage
from unified_agent.tools import approvals, doctor, token_lifecycle
from unified_agent.tools import profiles as profile_tools
from unified_agent.tools.events import event_log_verify


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.delenv("UA_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("UA_AUTHENTICATED_PUBLIC", raising=False)
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
    storage._connection = None
    storage.init_db()
    yield
    storage._connection = None


def test_canonical_constants_and_aliases():
    assert config.CANONICAL_DEPLOYMENT_MODES == (
        "local_trusted",
        "authenticated_private",
        "authenticated_public",
    )
    assert config.normalize_deployment_mode("local_trusted") == "local_trusted"
    assert config.normalize_deployment_mode("local-trusted") == "local_trusted"
    assert config.normalize_deployment_mode("local") == "local_trusted"
    assert config.normalize_deployment_mode("authenticated_private") == "authenticated_private"
    assert config.normalize_deployment_mode("authenticated-private") == "authenticated_private"
    assert config.normalize_deployment_mode("private") == "authenticated_private"
    assert config.normalize_deployment_mode("authenticated_public") == "authenticated_public"
    assert config.normalize_deployment_mode("authenticated-public") == "authenticated_public"
    assert config.normalize_deployment_mode("public") == "authenticated_public"
    assert config.normalize_deployment_mode(None) is None
    assert config.normalize_deployment_mode("") is None
    assert config.normalize_deployment_mode("unknown_mode") is None


def test_deployment_mode_resolution_precedence(monkeypatch):
    # 1. Explicit env var
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local-trusted")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_LOCAL_TRUSTED

    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE

    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC

    # Invalid explicit env var fails closed
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "invalid_custom_mode")
    with pytest.raises(config.DeploymentModeError, match="invalid UA_DEPLOYMENT_MODE"):
        config.deployment_mode()

    # 2. Inferred when unset
    monkeypatch.delenv("UA_DEPLOYMENT_MODE", raising=False)

    # Auth not enforced -> local_trusted
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_LOCAL_TRUSTED

    monkeypatch.setenv("UA_LEGACY_OPEN_POSTURE", "1")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_LOCAL_TRUSTED

    # Auth enforced + private transport -> authenticated_private
    monkeypatch.delenv("UA_LEGACY_OPEN_POSTURE", raising=False)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE

    # Auth enforced + public indicator -> authenticated_public
    monkeypatch.setenv("UA_AUTHENTICATED_PUBLIC", "1")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC

    monkeypatch.delenv("UA_AUTHENTICATED_PUBLIC", raising=False)
    monkeypatch.setenv("UA_PUBLIC_URL", "https://hub.example.com")
    assert config.deployment_mode() == config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC


def test_deployment_mode_from_settings():
    # Explicit mode
    mode, gate = config.deployment_mode_from_settings(
        "private", enforce_raw="1", transport_raw="http", legacy_raw=None
    )
    assert mode == "authenticated_private"
    assert gate == "UA_DEPLOYMENT_MODE"

    # Inferred unforced
    mode, gate = config.deployment_mode_from_settings(
        None, enforce_raw="0", transport_raw="http", legacy_raw=None
    )
    assert mode == "local_trusted"
    assert "unforced" in gate

    # Inferred enforced private
    mode, gate = config.deployment_mode_from_settings(
        None, enforce_raw="1", transport_raw="http", legacy_raw=None
    )
    assert mode == "authenticated_private"
    assert "private" in gate

    # Inferred enforced public
    mode, gate = config.deployment_mode_from_settings(
        None, enforce_raw="1", transport_raw="http", legacy_raw=None, public_raw="1"
    )
    assert mode == "authenticated_public"
    assert "public" in gate

    # Invalid mode
    with pytest.raises(config.DeploymentModeError, match="invalid deployment mode"):
        config.deployment_mode_from_settings(
            "bogus", enforce_raw="1", transport_raw="http", legacy_raw=None
        )


def test_validate_deployment_mode_mismatch_guards():
    # Public mode requires enforced auth
    with pytest.raises(
        config.DeploymentModeError, match="authenticated_public mode refuses unauthenticated"
    ):
        config.validate_deployment_mode(config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC, enforce=False)

    # Public mode requires tokens configured when checked
    with pytest.raises(
        config.DeploymentModeError, match="requires configured authentication credentials"
    ):
        config.validate_deployment_mode(
            config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC, enforce=True, tokens_configured=False
        )

    # Local trusted mode rejects public ingress
    with pytest.raises(
        config.DeploymentModeError,
        match="local_trusted mode cannot be combined with public ingress",
    ):
        config.validate_deployment_mode(
            config.DEPLOYMENT_MODE_LOCAL_TRUSTED, enforce=False, public=True
        )

    with pytest.raises(
        config.DeploymentModeError,
        match="local_trusted mode cannot be combined with public ingress",
    ):
        config.validate_deployment_mode(
            config.DEPLOYMENT_MODE_LOCAL_TRUSTED,
            enforce=False,
            public_url="https://hub.example.com",
        )

    with pytest.raises(
        config.DeploymentModeError,
        match="local_trusted mode cannot be combined with public ingress",
    ):
        config.validate_deployment_mode(
            config.DEPLOYMENT_MODE_LOCAL_TRUSTED,
            enforce=False,
            public_hosts="hub.example.com",
        )

    # Private mode requires enforced auth
    with pytest.raises(
        config.DeploymentModeError,
        match="authenticated_private mode requires enforced authentication",
    ):
        config.validate_deployment_mode(config.DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE, enforce=False)

    # Valid configurations do not raise
    config.validate_deployment_mode(config.DEPLOYMENT_MODE_LOCAL_TRUSTED, enforce=False)
    config.validate_deployment_mode(config.DEPLOYMENT_MODE_AUTHENTICATED_PRIVATE, enforce=True)
    config.validate_deployment_mode(
        config.DEPLOYMENT_MODE_AUTHENTICATED_PUBLIC, enforce=True, tokens_configured=True
    )


def test_deployment_mode_from_settings_public_url():
    # Inferred public mode via public_url_raw
    mode, gate = config.deployment_mode_from_settings(
        None,
        enforce_raw="1",
        transport_raw="http",
        legacy_raw=None,
        public_url_raw="https://hub.example.com",
    )
    assert mode == "authenticated_public"
    assert "public" in gate


def test_deployment_mode_from_settings_public_hosts():
    # Inferred public mode via public_hosts_raw
    mode, gate = config.deployment_mode_from_settings(
        None,
        enforce_raw="1",
        transport_raw="http",
        legacy_raw=None,
        public_hosts_raw="hub.example.com",
    )
    assert mode == "authenticated_public"
    assert "public" in gate


def test_doctor_deployment_mode_check(monkeypatch):
    # Test local_trusted
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    chk = doctor._deployment_mode_check()
    assert chk["capability"] == "deployment_mode"
    assert chk["status"] == "LIVE"
    assert "local_trusted" in chk["detail"]

    # Test authenticated_private
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "LIVE"
    assert "authenticated_private" in chk["detail"]

    # Test mismatch: authenticated_private with UA_ENFORCE_AUTH=0
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "requires enforced authentication" in chk["detail"]

    # Test mismatch: invalid UA_DEPLOYMENT_MODE
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "not_a_real_mode")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "invalid deployment mode" in chk["detail"]

    # Test mismatch: authenticated_public with no credentials configured
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "requires configured authentication credentials" in chk["detail"]

    # Test mismatch: invalid UA_AGENT_TOKENS file does not crash doctor
    monkeypatch.setenv("UA_AGENT_TOKENS", "/nonexistent/token/path")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "invalid UA_AGENT_TOKENS" in chk["detail"]

    # Test mismatch: incomplete OAuth (only flag set) remains MISCONFIGURED
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"

    # Test malformed IPv6 issuer does not crash doctor or validator
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://[broken")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"

    # Test OAuth-only public deployment with approver logins is LIVE
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.com")
    monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", "alice@example.com")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "LIVE"

    # Test partial WorkOS (only organization ID, no identity map / provider vars) is MISCONFIGURED
    monkeypatch.delenv("UA_MCP_OAUTH", raising=False)
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_123")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"

    from cryptography.fernet import Fernet

    valid_fernet_key = Fernet.generate_key().decode()

    # Test invalid Fernet key in WORKOS_COOKIE_PASSWORD remains MISCONFIGURED
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"alice@example.com": "Desktop_Person"}')
    monkeypatch.setenv("WORKOS_API_KEY", "sk_test_123")
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_123")
    monkeypatch.setenv("WORKOS_ISSUER", "https://auth.workos.com/raw")
    monkeypatch.setenv("WORKOS_REDIRECT_URI", "https://hub.example.com/callback")
    monkeypatch.setenv("WORKOS_COOKIE_PASSWORD", "not_a_valid_fernet_key_even_if_long_enough")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"

    # Test complete WorkOS configuration with valid Fernet key is LIVE
    monkeypatch.setenv("WORKOS_COOKIE_PASSWORD", valid_fernet_key)
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "LIVE"

    # Test shell-only UA_PUBLIC_URL does not taint local_trusted when hub setting is unset
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    monkeypatch.setenv("UA_PUBLIC_URL", "https://hub.example.com")
    with patch.object(
        doctor,
        "_hub_posture_setting",
        side_effect=lambda name: (
            (None, "default") if name == "UA_PUBLIC_URL" else (os.environ.get(name), "env")
        ),
    ):
        chk = doctor._deployment_mode_check()
        assert chk["status"] == "LIVE"
        assert "local_trusted" in chk["detail"]

    # Test UA_MCP_OAUTH=true does not enable OAuth (only "1" is accepted)
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_MCP_OAUTH", "true")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.com")
    monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", "alice@example.com")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "requires configured authentication credentials" in chk["detail"]

    # Test local_trusted with UA_HTTP_PUBLIC_HOSTS is MISCONFIGURED
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "hub.example.com")
    chk = doctor._deployment_mode_check()
    assert chk["status"] == "MISCONFIGURED"
    assert "local_trusted mode cannot be combined with public ingress" in chk["detail"]


def test_doctor_deployment_mode_does_not_borrow_caller_credentials_when_hub_unit_readable(
    monkeypatch,
):
    # Operator shell has UA_AGENT_TOKENS set
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:shell-token")
    # But hub unit is readable and sets authenticated_public with UA_ENFORCE_AUTH=1 and NO credentials
    hub_unit_env = {
        "UA_DEPLOYMENT_MODE": "authenticated_public",
        "UA_ENFORCE_AUTH": "1",
        "UA_TRANSPORT": "http",
        "UA_HTTP_HOST": "127.0.0.1",
    }
    with patch.object(doctor, "_service_env", return_value=hub_unit_env):
        chk = doctor._deployment_mode_check()
        assert chk["status"] == "MISCONFIGURED"
        assert "requires configured authentication credentials" in chk["detail"]


def test_deployment_claim_flow(monkeypatch):
    # Set to local_trusted with no active tokens
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    # Claim attempt initially queues an approval request (forced approval gate)
    with pytest.raises(approvals.ApprovalPendingError) as pending_exc:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person",
            value="synthetic-admin-secret-token-12345",
            client_nonce="synthetic-nonce-1234",
            note="first-admin claim",
            ttl_days=30,
        )

    # Human resolves approval
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending_exc.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    # Re-running consumes the one-shot approval pass
    claim_res = token_lifecycle.deployment_claim(
        agent_label="Desktop_Person",
        value="synthetic-admin-secret-token-12345",
        client_nonce="synthetic-nonce-1234",
        note="first-admin claim",
        ttl_days=30,
    )
    assert claim_res["claimed"] is True
    assert claim_res["agent_label"] == "Desktop_Person"
    assert claim_res["deployment_mode"] == "local_trusted"
    assert claim_res["next_mode"] == "authenticated_private"
    assert "activation_step" in claim_res
    assert claim_res["token_id"] > 0
    assert "token_hash" in claim_res

    # The claimed token is now loaded into canonical agent_tokens()
    tokens = config.agent_tokens()
    assert tokens.get("synthetic-admin-secret-token-12345") == "Desktop_Person"
    assert config.has_configured_credentials() is True

    # Verify event logged
    ev_report = event_log_verify()
    assert ev_report["status"] == "valid"

    # Second claim attempt in local_trusted mode must fail closed because an active token exists
    with pytest.raises(ToolError, match="deployment is already claimed and has active credentials"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Agent2",
            value="synthetic-unauthorized-token-67890",
            client_nonce="synthetic-nonce-1234",
        )

    # Transition to authenticated_private
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    config.require_configured_http_credentials()

    # Claim attempt must fail closed because instance is in private mode
    with pytest.raises(ToolError, match="only permitted in local_trusted mode"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Agent2",
            value="synthetic-unauthorized-token-67890",
            client_nonce="synthetic-nonce-1234",
        )


def test_deployment_claim_rejects_unknown_agent_label(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    with pytest.raises(ToolError, match="unknown agent label 'Desktop_Admin'"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Admin",
            value="synthetic-token-12345",
            client_nonce="synthetic-nonce-1234",
        )


def test_deployment_claim_refuses_to_overwrite_existing_secret(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    secret_name = "agent-token:Desktop_Agent2"
    managed_secrets.set_secret(
        secret_name,
        "already-existing-secret",
        requested_by="Desktop_Person",
        consumer="agent_token",
    )

    with patch.object(profile_tools, "check_approval_gate", return_value=None):
        with pytest.raises(
            ToolError, match="already exists; deployment_claim refuses to overwrite"
        ):
            token_lifecycle.deployment_claim(
                agent_label="Desktop_Agent2",
                value="attempted-overwrite-value",
                client_nonce="synthetic-nonce-1234",
            )


def test_deployment_claim_input_validation(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    with pytest.raises(ToolError, match="agent_label must be a non-empty string"):
        token_lifecycle.deployment_claim(agent_label="", value="secret", client_nonce="nonce-12345")

    with pytest.raises(ToolError, match="token value must be a non-empty string"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="", client_nonce="nonce-12345"
        )

    with pytest.raises(ToolError, match="client_nonce .* is required"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce=""
        )

    with pytest.raises(ToolError, match="client_nonce .* is required"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce="short"
        )

    with pytest.raises(ToolError, match="ttl_days must be a positive integer or None"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce="nonce-12345", ttl_days=0
        )

    with pytest.raises(ToolError, match="ttl_days must be a positive integer or None"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce="nonce-12345", ttl_days=-5
        )


def test_deployment_claim_refused_on_authenticated_deployments(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")

    with pytest.raises(ToolError, match="only permitted in local_trusted mode"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce="nonce-12345"
        )

    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    with pytest.raises(ToolError, match="only permitted in local_trusted mode"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret", client_nonce="nonce-12345"
        )


def test_deployment_claim_rollback_on_issue_failure(monkeypatch):
    from unified_agent import managed_secrets

    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    secret_name = "agent-token:Desktop_Person"
    # Pre-existing secret check
    with pytest.raises(ToolError):
        managed_secrets.read_secret(secret_name)

    # Monkeypatch _record_issue to raise unexpected error
    def _exploding_record(*args, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(token_lifecycle, "_record_issue", _exploding_record)

    with pytest.raises(approvals.ApprovalPendingError) as pending_exc:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret123", client_nonce="synthetic-nonce-5678"
        )

    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending_exc.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    with pytest.raises(RuntimeError, match="database exploded"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret123", client_nonce="synthetic-nonce-5678"
        )

    # Verify secret was cleaned up / deleted during rollback
    with pytest.raises(ToolError):
        managed_secrets.read_secret(secret_name)


def test_deployment_claim_session_isolation(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    # Caller 1 starts claim with nonce "client-nonce-1"
    with pytest.raises(approvals.ApprovalPendingError) as pending_1:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person",
            value="secret-admin-pass-1",
            client_nonce="client-nonce-1",
        )

    # Human approves Caller 1's request
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending_1.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    # Caller 2 with nonce "client-nonce-2" tries to consume Caller 1's approval
    # Must queue a distinct pending approval row instead of consuming Caller 1's pass
    with pytest.raises(approvals.ApprovalPendingError) as pending_2:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person",
            value="secret-admin-pass-1",
            client_nonce="client-nonce-2",
        )
    assert pending_2.value.approval_id != pending_1.value.approval_id

    # Caller 1 successfully consumes its approved pass
    res = token_lifecycle.deployment_claim(
        agent_label="Desktop_Person",
        value="secret-admin-pass-1",
        client_nonce="client-nonce-1",
    )
    assert res["claimed"] is True
    assert res["agent_label"] == "Desktop_Person"


def test_validate_deployment_mode_http_host_binding(monkeypatch):
    # local_trusted allows loopback hosts
    for loopback in ("127.0.0.1", "localhost", "::1"):
        config.validate_deployment_mode(
            config.DEPLOYMENT_MODE_LOCAL_TRUSTED,
            enforce=False,
            transport="http",
            http_host=loopback,
        )

    # local_trusted forbids wildcard or non-loopback HTTP host
    for bad_host in ("0.0.0.0", "::", "*", "192.168.1.50"):
        with pytest.raises(
            config.DeploymentModeError, match="forbids wildcard or non-loopback HTTP host"
        ):
            config.validate_deployment_mode(
                config.DEPLOYMENT_MODE_LOCAL_TRUSTED,
                enforce=False,
                transport="http",
                http_host=bad_host,
            )

    # stdio transport does not bind network ports, so host is ignored
    config.validate_deployment_mode(
        config.DEPLOYMENT_MODE_LOCAL_TRUSTED,
        enforce=False,
        transport="stdio",
        http_host="0.0.0.0",
    )


def test_deployment_claim_audit_failure_atomic_rollback(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    secret_name = "agent-token:Desktop_Person"

    with pytest.raises(approvals.ApprovalPendingError) as pending_exc:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret123", client_nonce="synthetic-nonce-atomic"
        )

    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending_exc.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    # Patch events.record_event_once_on_connection to raise on security.agent_token.issued / claimed
    orig_record_event = token_lifecycle.events.record_event_once_on_connection

    def exploding_record(conn, event_name, *args, **kwargs):
        if event_name in ("security.deployment.claimed", "security.agent_token.issued"):
            raise RuntimeError("audit pipeline failed")
        return orig_record_event(conn, event_name, *args, **kwargs)

    monkeypatch.setattr(token_lifecycle.events, "record_event_once_on_connection", exploding_record)

    with pytest.raises(RuntimeError, match="audit pipeline failed"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Person", value="secret123", client_nonce="synthetic-nonce-atomic"
        )

    # Verify both the secret file and the database row were completely rolled back
    with pytest.raises(ToolError):
        managed_secrets.read_secret(secret_name)

    conn = storage.connect()
    try:
        active_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_tokens WHERE status = 'active'"
        ).fetchone()["c"]
        assert active_rows == 0
    finally:
        conn.close()


def test_deployment_claim_create_only_refuses_overwrite(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    secret_name = "agent-token:Desktop_Agent1"

    # Seed an existing secret in managed_secrets
    managed_secrets.set_secret(
        secret_name,
        "pre-existing-secret",
        requested_by="Desktop_Person",
        consumer="agent_token",
    )

    with pytest.raises(approvals.ApprovalPendingError) as pending_exc:
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Agent1",
            value="new-value-1234",
            client_nonce="nonce-overwrite-test",
        )

    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approvals.approval_resolve(pending_exc.value.approval_id, "approve")
    finally:
        config.agent_identity_var.reset(token)

    with pytest.raises(ToolError, match="already exists; deployment_claim refuses to overwrite"):
        token_lifecycle.deployment_claim(
            agent_label="Desktop_Agent1",
            value="new-value-1234",
            client_nonce="nonce-overwrite-test",
        )


def test_doctor_workos_invalid_identity_map_not_live(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_AGENT_TOKENS", "")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_123")
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"subject": null}')
    monkeypatch.setenv("UA_AUTHENTICATED_PUBLIC", "1")

    res = doctor.system_doctor()
    dep_check = next(c for c in res["checks"] if c["capability"] == "deployment_mode")
    assert dep_check["status"] == "MISCONFIGURED"
    assert "requires configured authentication credentials" in dep_check["detail"]


def test_approval_resolve_allowed_in_managed_secret_mode():
    from unified_agent import agent_access

    # Must not raise AgentAccessDenied
    agent_access.enforce("approval_resolve", read_only=False)
    agent_access.enforce("approval_pending", read_only=True)


def test_agent_tokens_collision_and_validation(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "local_trusted")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    # 1. Collision across env and managed secret fails closed
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:shared-collided-tok")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, expires_at, issued_by) "
            "VALUES ('Desktop_Agent2', ?, 'sha256', 'active', ?, NULL, 'test')",
            (hashlib.sha256(b"shared-collided-tok").hexdigest(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    managed_secrets.set_secret(
        "agent-token:Desktop_Agent2",
        "shared-collided-tok",
        requested_by="Desktop_Person",
        consumer="agent_token",
    )

    tokens = config.agent_tokens()
    assert "shared-collided-tok" not in tokens

    # 2. Hash mismatch in managed secret is excluded
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, expires_at, issued_by) "
            "VALUES ('Desktop_Agent1', 'mismatched-hash-value', 'sha256', 'active', ?, NULL, 'test')",
            (storage.now(),),
        )
        conn.commit()
    finally:
        conn.close()
    managed_secrets.set_secret(
        "agent-token:Desktop_Agent1",
        "some-actual-secret",
        requested_by="Desktop_Person",
        consumer="agent_token",
    )
    tokens = config.agent_tokens()
    assert "some-actual-secret" not in tokens

    # 3. Expired token in managed secret is excluded
    past_iso = "2020-01-01T00:00:00Z"
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, expires_at, issued_by) "
            "VALUES ('Desktop_agent3', ?, 'sha256', 'active', ?, ?, 'test')",
            (hashlib.sha256(b"expired-token-value").hexdigest(), past_iso, past_iso),
        )
        conn.commit()
    finally:
        conn.close()
    managed_secrets.set_secret(
        "agent-token:Desktop_agent3",
        "expired-token-value",
        requested_by="Desktop_Person",
        consumer="agent_token",
    )
    tokens = config.agent_tokens()
    assert "expired-token-value" not in tokens


def test_doctor_deployment_mode_recognizes_managed_credentials_when_env_empty(monkeypatch):
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "")
    valid_token = "valid-doctor-managed-token"
    token_hash = hashlib.sha256(valid_token.encode()).hexdigest()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, expires_at, issued_by) "
            "VALUES ('Desktop_Person', ?, 'sha256', 'active', ?, NULL, 'test')",
            (token_hash, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    managed_secrets.set_secret(
        "agent-token:Desktop_Person",
        valid_token,
        requested_by="Desktop_Person",
        consumer="agent_token",
    )

    hub_unit_env = {
        "UA_DEPLOYMENT_MODE": "authenticated_public",
        "UA_ENFORCE_AUTH": "1",
        "UA_TRANSPORT": "http",
        "UA_HTTP_HOST": "127.0.0.1",
        "UA_AGENT_TOKENS": "",
    }
    with patch.object(doctor, "_service_env", return_value=hub_unit_env):
        chk = doctor._deployment_mode_check()
        assert chk["status"] == "LIVE"
        assert "authenticated_public" in chk["detail"]


def test_agent_tokens_tenant_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    storage.init_db()

    # Secret issued under org_tenant
    tenant_secret = "tenant-token-value-12345"
    managed_secrets.set_secret(
        "agent-token:Desktop_Agent2",
        tenant_secret,
        requested_by="Desktop_Person",
        consumer="agent_token",
        org_id="org_tenant",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at, org_id) "
            "VALUES (?, ?, 'sha256', 'active', ?, ?)",
            (
                "Desktop_Agent2",
                hashlib.sha256(tenant_secret.encode()).hexdigest(),
                storage.now(),
                "org_tenant",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # When no tenant context is set, org_tenant credentials must NOT be loaded
    tokens = config.agent_tokens(include_managed=True)
    assert tenant_secret not in tokens

    # When matching tenant context is set, credentials MUST be loaded
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_tenant")
    tokens_tenant = config.agent_tokens(include_managed=True)
    assert tokens_tenant.get(tenant_secret) == "Desktop_Agent2"


def test_token_rotation_rollback_restores_previous_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
    storage.init_db()

    old_secret = "initial-working-secret-token"
    managed_secrets.set_secret(
        "agent-token:Desktop_Agent2",
        old_secret,
        requested_by="Desktop_Person",
        consumer="agent_token",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, hash_algo, status, issued_at) "
            "VALUES (?, ?, 'sha256', 'active', ?)",
            (
                "Desktop_Agent2",
                hashlib.sha256(old_secret.encode()).hexdigest(),
                storage.now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Verify initial secret loads
    tokens = config.agent_tokens(include_managed=True)
    assert tokens.get(old_secret) == "Desktop_Agent2"

    # Patch event recording during rotation to fail
    orig_record = token_lifecycle.events.record_event_once_on_connection

    def exploding_record(conn, event_name, *args, **kwargs):
        if "security.agent_token" in event_name:
            raise RuntimeError("ledger append failed during rotation")
        return orig_record(conn, event_name, *args, **kwargs)

    monkeypatch.setattr(token_lifecycle.events, "record_event_once_on_connection", exploding_record)

    ident_tok = config.agent_identity_var.set("Desktop_Agent1")
    try:
        with pytest.raises(approvals.ApprovalPendingError) as pending:
            token_lifecycle.agent_token_rotate(
                "Desktop_Agent2",
                "new-replacement-secret-token",
            )
        admin_tok = config.agent_identity_var.set("Desktop_Person")
        try:
            approvals.approval_resolve(pending.value.approval_id, "approve")
        finally:
            config.agent_identity_var.reset(admin_tok)

        with pytest.raises(RuntimeError, match="ledger append failed during rotation"):
            token_lifecycle.agent_token_rotate(
                "Desktop_Agent2",
                "new-replacement-secret-token",
            )
    finally:
        config.agent_identity_var.reset(ident_tok)

    # Verify old secret is preserved on disk and still authenticates
    val = managed_secrets._load_for_consumer("agent-token:Desktop_Agent2", consumer="agent_token")
    assert val == old_secret
    tokens_after = config.agent_tokens(include_managed=True)
    assert tokens_after.get(old_secret) == "Desktop_Agent2"
    assert "new-replacement-secret-token" not in tokens_after


def test_doctor_scopes_hub_service_environment(monkeypatch, tmp_path):
    caller_dir = tmp_path / "caller"
    hub_dir = tmp_path / "hub"
    caller_dir.mkdir()
    hub_dir.mkdir()

    monkeypatch.setenv("UA_HOME", str(caller_dir))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    storage.init_db()

    caller_token = "synthetic-caller-only-token"
    managed_secrets.set_secret(
        "agent-token:Desktop_Person",
        caller_token,
        requested_by="Desktop_Person",
        consumer="agent_token",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, status, issued_at) VALUES (?, ?, 'active', ?)",
            ("Desktop_Person", hashlib.sha256(caller_token.encode()).hexdigest(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()

    # The hub service unit points to an empty hub_dir
    hub_env = {
        "UA_HOME": str(hub_dir),
        "UA_DEPLOYMENT_MODE": "authenticated_public",
        "UA_TRANSPORT": "http",
        "UA_ENFORCE_AUTH": "1",
        "UA_AGENT_TOKENS": "",
    }
    with patch.object(doctor, "_service_env", return_value=hub_env):
        result = doctor._deployment_mode_check()
        # Must NOT report LIVE by borrowing caller's credential
        assert result["status"] == "MISCONFIGURED"
        assert "requires configured authentication credentials (none found)" in result["detail"]


def test_doctor_hub_auth_check_detects_managed_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "")
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()

    token_val = "managed-only-token-abc"
    managed_secrets.set_secret(
        "agent-token:Desktop_Agent2",
        token_val,
        requested_by="Desktop_Person",
        consumer="agent_token",
    )
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, status, issued_at) VALUES (?, ?, 'active', ?)",
            ("Desktop_Agent2", hashlib.sha256(token_val.encode()).hexdigest(), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()

    hub_unit_env = {
        "UA_HOME": str(tmp_path),
        "UA_TRANSPORT": "http",
        "UA_ENFORCE_AUTH": "1",
        "UA_AGENT_TOKENS": "",
    }
    with patch.object(doctor, "_service_env", return_value=hub_unit_env):
        chk = doctor._hub_auth_check()
        assert chk["status"] == "LIVE"
        assert "1 token(s) configured" in chk["detail"]
