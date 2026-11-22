"""Tenant-scoped managed secrets (task #1343, Epic J gap).

Managed secrets were global by NAME: any caller that knew a name resolved the
one value. A tenant's secret is now keyed by ``(org_id, name)``. The default
(no org) path stays byte-identical, and an org-agnostic read of a name that any
tenant holds fails closed instead of silently sharing.
"""

from __future__ import annotations

import json
import re

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import managed_secrets


@pytest.fixture
def secrets_home(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    return tmp_path / "ua" / "managed-secrets"


def _set(name, value, org_id=None, consumer="zulip"):
    return managed_secrets.set_secret(
        name, value, requested_by="Desktop_Agent2", consumer=consumer, org_id=org_id
    )


def test_two_tenants_same_name_are_isolated(secrets_home):
    _set("github:oauth", "acme-token", org_id="org_acme")
    _set("github:oauth", "globex-token", org_id="org_globex")
    assert managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_acme") == (
        "acme-token"
    )
    assert managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex") == (
        "globex-token"
    )


def test_tenant_cannot_read_another_tenants_or_root_secret(secrets_home):
    _set("github:oauth", "acme-token", org_id="org_acme")
    _set("shared", "root-value")
    with pytest.raises(ToolError, match="not configured"):
        managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex")
    # A tenant never falls back to the single-tenant root either.
    with pytest.raises(ToolError, match="not configured"):
        managed_secrets._load_for_consumer("shared", "zulip", org_id="org_acme")


def test_unbound_read_fails_closed_when_a_tenant_holds_the_name(secrets_home):
    _set("github:oauth", "root-token")
    assert managed_secrets._load_for_consumer("github:oauth", "zulip") == "root-token"
    _set("github:oauth", "acme-token", org_id="org_acme")
    with pytest.raises(ToolError, match="tenant-scoped"):
        managed_secrets._load_for_consumer("github:oauth", "zulip")
    with pytest.raises(ToolError, match="tenant-scoped"):
        managed_secrets.metadata("github:oauth")


def test_default_path_layout_is_unchanged(secrets_home):
    result = _set("zulip:Desktop_Agent2", "synthetic")
    path = secrets_home / "zulip:Desktop_Agent2"
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert set(payload) == {"consumer", "digest", "value"}, "no org key on the default path"
    assert "org_id" not in result


def test_tenant_layout_and_metadata(secrets_home):
    result = _set("github:oauth", "acme-token", org_id="org_acme")
    path = secrets_home / "orgs" / "org_acme" / "github:oauth"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (secrets_home / "orgs" / "org_acme").stat().st_mode & 0o777 == 0o700
    assert result["org_id"] == "org_acme"
    meta = managed_secrets.metadata("github:oauth", org_id="org_acme")
    assert meta["org_id"] == "org_acme"
    assert "acme-token" not in json.dumps(meta) and "acme-token" not in json.dumps(result)


def test_rotate_and_revoke_are_tenant_scoped(secrets_home):
    _set("github:oauth", "acme-1", org_id="org_acme")
    _set("github:oauth", "globex-1", org_id="org_globex")
    _set("github:oauth", "acme-2", org_id="org_acme")  # rotate acme only
    assert (
        managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_acme") == "acme-2"
    )
    assert (
        managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex")
        == "globex-1"
    )
    assert managed_secrets.delete_secret("github:oauth", org_id="org_acme") is True
    with pytest.raises(ToolError, match="not configured"):
        managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_acme")
    assert (
        managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex")
        == "globex-1"
    )
    assert managed_secrets.delete_secret("github:oauth", org_id="org_acme") is False


@pytest.mark.parametrize("bad", ["", "..", "a/b", "org acme", "x" * 65, "orgs/../x"])
def test_org_id_must_be_one_safe_component(secrets_home, bad):
    with pytest.raises(ToolError, match="org_id"):
        _set("n", "v", org_id=bad)


def test_root_name_orgs_is_reserved(secrets_home):
    with pytest.raises(ToolError, match="reserved"):
        _set("orgs", "v")


def test_consumers_read_from_the_active_tenant(secrets_home, monkeypatch):
    """Zulip and evidence signing resolve under edition.tenant_org_id()."""
    from unified_agent.audit import evidence_signing

    _set("sign", "acme-key", org_id="org_acme", consumer="evidence-signing")
    env = {"UA_EVIDENCE_SIGNING_SECRET_NAME": "sign"}

    monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: "org_acme")
    key, key_id = evidence_signing._key_material(env)
    assert key == b"acme-key" and key_id == "managed:sign"

    monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: "org_globex")
    with pytest.raises(evidence_signing.SigningUnavailable):
        evidence_signing._key_material(env)

    monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: None)
    with pytest.raises(evidence_signing.SigningUnavailable, match="tenant-scoped"):
        evidence_signing._key_material(env)


class TestManagedSecretSetTool:
    @pytest.fixture(autouse=True)
    def _hub_db(self, secrets_home):
        from unified_agent import storage

        storage.init_db()  # approvals live in the hub DB under the new UA_HOME

    def _call(self, monkeypatch, *, tenant, **kwargs):
        from unified_agent import config
        from unified_agent.tools import approvals, profiles

        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: tenant)
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(approvals.ApprovalPendingError) as pending:
                profiles.managed_secret_set("github:oauth", "v1", **kwargs)
            row = approvals.get_approval(pending.value.approval_id)
            human = config.agent_identity_var.set("Desktop_Person")
            try:
                approvals.approval_resolve(pending.value.approval_id, "approve")
            finally:
                config.agent_identity_var.reset(human)
            return row, profiles.managed_secret_set("github:oauth", "v1", **kwargs)
        finally:
            config.agent_identity_var.reset(token)

    def test_single_tenant_payload_is_unchanged(self, secrets_home, monkeypatch):
        row, result = self._call(monkeypatch, tenant=None)
        assert set(row["payload"]) == {"name", "consumer", "identity", "value_sha256"}
        assert "org_id" not in result
        assert (secrets_home / "github:oauth").is_file()

    def test_enforced_tenant_defaults_to_own_org(self, secrets_home, monkeypatch):
        row, result = self._call(monkeypatch, tenant="org_acme")
        assert row["payload"]["org_id"] == "org_acme"
        assert result["org_id"] == "org_acme"
        assert (secrets_home / "orgs" / "org_acme" / "github:oauth").is_file()
        assert not (secrets_home / "github:oauth").exists()

    def test_enforced_tenant_cannot_write_another_org(self, secrets_home, monkeypatch):
        from unified_agent import config
        from unified_agent.tools import profiles

        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: "org_acme")
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(ToolError, match="own org_id"):
                profiles.managed_secret_set("github:oauth", "v1", org_id="org_globex")
        finally:
            config.agent_identity_var.reset(token)
        assert not (secrets_home / "orgs" / "org_globex").exists()

    def test_operator_can_provision_a_tenant_on_single_tenant_hub(self, secrets_home, monkeypatch):
        row, result = self._call(monkeypatch, tenant=None, org_id="org_globex")
        assert row["payload"]["org_id"] == "org_globex"
        assert (secrets_home / "orgs" / "org_globex" / "github:oauth").is_file()


class TestReviewHardening:
    """PR #762 review: early org validation, blank org, cross-tenant revoke, token rows."""

    @pytest.fixture(autouse=True)
    def _hub_db(self, secrets_home):
        from unified_agent import storage

        storage.init_db()

    def _as(self, monkeypatch, tenant):
        from unified_agent import config

        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: tenant)
        return config.agent_identity_var.set("Desktop_Agent2")

    @pytest.mark.parametrize("bad", ["team/a", "", "   "])
    def test_invalid_or_blank_org_id_refused_before_any_approval(
        self, secrets_home, monkeypatch, bad
    ):
        from unified_agent import config, storage
        from unified_agent.tools import profiles

        token = self._as(monkeypatch, None)
        try:
            with pytest.raises(ToolError, match="org_id"):
                profiles.managed_secret_set("github:oauth", "v1", org_id=bad)
        finally:
            config.agent_identity_var.reset(token)
        conn = storage.connect()
        pending = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        conn.close()
        assert pending == 0, "no approval may be queued for an unusable org_id"
        assert not (secrets_home / "github:oauth").exists()

    def test_storage_layer_refuses_cross_tenant_access(self, secrets_home, monkeypatch):
        _set("github:oauth", "globex-token", org_id="org_globex")
        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: "org_acme")
        with pytest.raises(ToolError, match="own org_id"):
            managed_secrets.delete_secret("github:oauth", org_id="org_globex")
        with pytest.raises(ToolError, match="own org_id"):
            managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex")
        with pytest.raises(ToolError, match="own org_id"):
            managed_secrets.metadata("github:oauth", org_id="org_globex")
        with pytest.raises(ToolError, match="own org_id"):
            _set("github:oauth", "x", org_id="org_globex")
        with pytest.raises(ToolError, match="own org_id"):
            _set("rootname", "x")  # the org-less root is not the tenant's either
        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: None)
        assert (
            managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_globex")
            == "globex-token"
        )

    def test_token_rotation_stays_inside_the_tenant(self, secrets_home, monkeypatch):
        from unified_agent import config, storage
        from unified_agent.tools import approvals, token_lifecycle

        def issue(tenant, value, rotate=False):
            token = self._as(monkeypatch, tenant)
            fn = token_lifecycle.agent_token_rotate if rotate else token_lifecycle.agent_token_issue
            try:
                try:
                    return fn("Desktop_Agent2", value)
                except approvals.ApprovalPendingError as pending:
                    human = config.agent_identity_var.set("Desktop_Person")
                    try:
                        approvals.approval_resolve(pending.approval_id, "approve")
                    finally:
                        config.agent_identity_var.reset(human)
                    return fn("Desktop_Agent2", value)
            finally:
                config.agent_identity_var.reset(token)

        a1 = issue("org_acme", "acme-token-1")
        b1 = issue("org_globex", "globex-token-1")
        rotated = issue("org_acme", "acme-token-2", rotate=True)
        assert rotated["previous_token_id"] == a1["token_id"], "must not pick Org B's newer row"

        conn = storage.connect()
        rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT id, status, org_id FROM agent_tokens").fetchall()
        }
        conn.close()
        assert rows[a1["token_id"]]["status"] == "rotated"
        assert rows[b1["token_id"]] == {
            "id": b1["token_id"],
            "status": "active",
            "org_id": "org_globex",
        }
        assert rows[rotated["new_token_id"]]["org_id"] == "org_acme"

        token = self._as(monkeypatch, "org_globex")
        try:
            listed = token_lifecycle.agent_token_list()
        finally:
            config.agent_identity_var.reset(token)
        assert [t["id"] for t in listed["tokens"]] == [b1["token_id"]]


class TestReviewRound2:
    """PR #762 review round 2: per-tenant hash uniqueness, reserved name, delete race."""

    def test_legacy_agent_tokens_rebuild_keeps_rotation_chain(self, secrets_home):
        import sqlite3

        from unified_agent import config, storage

        config.ensure_dirs()
        conn = sqlite3.connect(config.db_path())
        conn.executescript(
            """
            CREATE TABLE agent_tokens (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              agent_label TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,
              hash_algo TEXT NOT NULL DEFAULT 'sha256',
              status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'rotated', 'revoked', 'expired')),
              issued_at TEXT NOT NULL,
              expires_at TEXT,
              rotated_from INTEGER REFERENCES agent_tokens(id) ON DELETE SET NULL,
              rotated_at TEXT,
              revoked_at TEXT,
              issued_by TEXT,
              note TEXT
            );
            INSERT INTO agent_tokens (id, agent_label, token_hash, status, issued_at)
              VALUES (1, 'Desktop_Agent2', 'h1', 'rotated', '2026-01-01');
            INSERT INTO agent_tokens (id, agent_label, token_hash, issued_at, rotated_from)
              VALUES (2, 'Desktop_Agent2', 'h2', '2026-02-01', 1);
            """
        )
        conn.commit()
        conn.close()

        storage.init_db()
        conn = storage.connect()
        try:
            rows = conn.execute("SELECT id, rotated_from, org_id FROM agent_tokens ORDER BY id")
            assert [tuple(r) for r in rows] == [(1, None, None), (2, 1, None)]
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='agent_tokens'"
            ).fetchone()[0]
            assert "token_hash TEXT NOT NULL UNIQUE" not in sql
            assert re.search(r"REFERENCES \"?agent_tokens\"?\(id\)", sql)
            # Same hash in two tenants is fine; twice in one tenant is refused.
            conn.execute(
                "INSERT INTO agent_tokens (agent_label, token_hash, issued_at, org_id)"
                " VALUES ('Desktop_Agent2', 'h2', '2026-03-01', 'org_acme')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO agent_tokens (agent_label, token_hash, issued_at)"
                    " VALUES ('Desktop_Agent2', 'h2', '2026-03-01')"
                )
        finally:
            conn.close()
        storage.init_db()  # idempotent: a second run does not rebuild again

    def test_reserved_root_name_refused_before_any_approval(self, secrets_home, monkeypatch):
        from unified_agent import config, storage
        from unified_agent.tools import profiles

        storage.init_db()
        monkeypatch.setattr("unified_agent.edition.tenant_org_id", lambda: None)
        token = config.agent_identity_var.set("Desktop_Agent2")
        try:
            with pytest.raises(ToolError, match="reserved"):
                profiles.managed_secret_set("orgs", "v1")
        finally:
            config.agent_identity_var.reset(token)
        conn = storage.connect()
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        conn.close()
        # Inside a tenant, "orgs" is an ordinary name.
        _set("orgs", "v", org_id="org_acme")

    def test_delete_with_expected_digest_spares_a_newer_rotation(self, secrets_home):
        first = _set("github:oauth", "v1", org_id="org_acme")
        _set("github:oauth", "v2", org_id="org_acme")  # rotation landed first
        assert (
            managed_secrets.delete_secret(
                "github:oauth", org_id="org_acme", expected_digest=first["digest"]
            )
            is False
        )
        assert (
            managed_secrets._load_for_consumer("github:oauth", "zulip", org_id="org_acme") == "v2"
        )

    def test_writes_and_deletes_are_serialized(self, secrets_home):
        import threading

        _set("k", "v0")
        errors: list[Exception] = []

        def churn(i):
            try:
                for n in range(20):
                    _set("k", f"v{i}-{n}")
                    managed_secrets.delete_secret("k")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        assert not list(secrets_home.glob(".k.*")), "no stray temp files"


def test_failed_agent_tokens_rebuild_rolls_back_to_the_original(secrets_home):
    """PR #762 review round 3: an interrupted rebuild never loses token history."""
    import sqlite3

    from unified_agent import config, storage

    config.ensure_dirs()
    conn = sqlite3.connect(config.db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE agent_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_label TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          issued_at TEXT NOT NULL,
          rotated_from INTEGER REFERENCES agent_tokens(id) ON DELETE SET NULL
        );
        INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES ('a', 'h1', 't');
        -- Occupy the index name the rebuild recreates, as a TABLE, so the script
        -- fails after DROP TABLE agent_tokens has already run.
        CREATE TABLE idx_agent_tokens_label_status (x);
        """
    )
    conn.commit()
    with pytest.raises(sqlite3.Error):
        storage._rebuild_agent_tokens_without_global_hash_unique(conn)
    rows = conn.execute("SELECT agent_label, token_hash FROM agent_tokens").fetchall()
    assert [tuple(r) for r in rows] == [("a", "h1")], "original table and rows survive"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='agent_tokens__rebuild'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_rebuild_keeps_the_autoincrement_high_water_mark(secrets_home):
    """PR #762 review round 4: a retired highest token id is never reissued."""
    import sqlite3

    from unified_agent import config, storage

    config.ensure_dirs()
    conn = sqlite3.connect(config.db_path())
    conn.executescript(
        """
        CREATE TABLE agent_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_label TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          hash_algo TEXT NOT NULL DEFAULT 'sha256',
          status TEXT NOT NULL DEFAULT 'active',
          issued_at TEXT NOT NULL,
          expires_at TEXT,
          rotated_from INTEGER REFERENCES agent_tokens(id) ON DELETE SET NULL,
          rotated_at TEXT,
          revoked_at TEXT,
          issued_by TEXT,
          note TEXT
        );
        INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES ('a', 'h1', 't');
        INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES ('a', 'h2', 't');
        INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES ('a', 'h3', 't');
        DELETE FROM agent_tokens WHERE id = 3;
        """
    )
    conn.commit()
    conn.close()
    storage.init_db()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES ('a', 'h4', 't')"
        )
        assert cur.lastrowid == 4, "id 3 was retired and must not be reused"
    finally:
        conn.close()
