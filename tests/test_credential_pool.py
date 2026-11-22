import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import credential_pool, server, storage


def test_registration_is_real_storage_and_registered_mcp_surface():
    result = credential_pool.credential_pool_register(
        "anthropic", "personal-cli", "Claude Code subscription"
    )
    credential = result["credential"]
    assert {"credential_pool_register", "credential_pool_list", "credential_pool_revoke"} <= set(
        server.REGISTERED_TOOLS
    )
    assert credential["status"] == "registered"
    assert credential["active"] is False
    assert "token" not in str(result).lower()

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM credential_pool WHERE id=?", (credential["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["note"] == "Claude Code subscription"
    assert "secret" not in str(dict(row)).lower()


def test_revocation_is_hidden_by_default():
    credential_id = credential_pool.credential_pool_register("openai", "work", "Codex CLI")[
        "credential"
    ]["id"]
    revoked = credential_pool.credential_pool_revoke(credential_id)["credential"]
    assert revoked["status"] == "revoked"
    assert credential_id not in {
        row["id"] for row in credential_pool.credential_pool_list()["credentials"]
    }
    assert credential_id in {
        row["id"]
        for row in credential_pool.credential_pool_list(include_revoked=True)["credentials"]
    }


@pytest.mark.parametrize("provider", ["unknown", "aws"])
def test_provider_is_strictly_validated(provider):
    with pytest.raises(ToolError, match="unsupported provider"):
        credential_pool.credential_pool_register(provider, "x")


def test_secret_material_is_rejected_and_never_persisted():
    with pytest.raises(ToolError, match="must not contain"):
        credential_pool.credential_pool_register("google", "x", "api_key=super-secret")
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM credential_pool").fetchone()[0] == 0
    finally:
        conn.close()
