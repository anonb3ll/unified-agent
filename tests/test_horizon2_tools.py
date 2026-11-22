from __future__ import annotations

import sqlite3

import pytest

from unified_agent import config, connectors
from unified_agent.tools import horizon2


@pytest.fixture
def identity():
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("legacy", [False, True])
def test_governed_server_org_migration_preserves_null_ownership(legacy):
    conn = horizon2.storage.connect()
    try:
        if legacy:
            conn.execute("DROP TABLE governed_servers")
            conn.executescript(horizon2.storage._HORIZON2_SCHEMA)
            conn.execute(
                "INSERT INTO governed_servers (server_id, connector, provenance, scopes_json, "
                "reviewed_by, review_ref, revoked_at, created_at, updated_at) "
                "VALUES ('legacy', 'slack', 'sha256:abc', '[\"read\"]', 'reviewer', 'review:1', "
                "'revoked', 'created', 'updated')"
            )
            conn.commit()
    finally:
        conn.close()
    for _ in range(2):
        horizon2.storage.init_db()
        conn = horizon2.storage.connect()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(governed_servers)")}
            assert "org_id" in columns
            index = conn.execute("PRAGMA index_info(idx_governed_servers_org_id)").fetchall()
            assert index[0]["name"] == "org_id"
            rows = conn.execute("SELECT * FROM governed_servers").fetchall()
            assert len(rows) == int(legacy)
            if legacy:
                assert dict(rows[0]) == {
                    "server_id": "legacy",
                    "connector": "slack",
                    "provenance": "sha256:abc",
                    "scopes_json": '["read"]',
                    "reviewed_by": "reviewer",
                    "review_ref": "review:1",
                    "revoked_at": "revoked",
                    "created_at": "created",
                    "updated_at": "updated",
                    "org_id": None,
                }
        finally:
            conn.close()


def _horizon_org(monkeypatch, org):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    if org is None:
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    else:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)


@pytest.mark.parametrize("org", [None, "org-A"])
def test_horizon_registry_contains_reads_and_authority_to_process_org(monkeypatch, identity, org):
    calls = []
    restored = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda tool, payload, **kwargs: calls.append((tool, payload, kwargs)) or {"approval_id": 7},
    )
    monkeypatch.setattr(horizon2.approvals, "restore_consumed_approval", restored.append)
    ids = {None: "private", "org-A": "server-a", "org-B": "server-b"}
    for scope, server_id in ids.items():
        _horizon_org(monkeypatch, scope)
        horizon2.horizon2_server_register(server_id, "slack", "sha256:abc", ["read"], "review:42")
    conn = horizon2.storage.connect()
    try:
        assert {
            row["server_id"]: row["org_id"]
            for row in conn.execute("SELECT server_id, org_id FROM governed_servers")
        } == {server_id: scope for scope, server_id in ids.items()}
    finally:
        conn.close()
    _horizon_org(monkeypatch, org)
    listing = horizon2.horizon2_server_list(include_revoked=True)
    assert [row["server_id"] for row in listing["servers"]] == [ids[org]]
    assert listing["count"] == 1
    before = len(calls)
    for scope, server_id in ids.items():
        assert horizon2._exists(server_id) == (scope == org)
        if scope != org:
            with pytest.raises(Exception, match="unregistered"):
                horizon2._load(server_id)
            with pytest.raises(Exception, match="unregistered"):
                horizon2.horizon2_server_revoke(server_id)
            with pytest.raises(Exception, match="unregistered"):
                horizon2.horizon2_connector_authorize(server_id, "read", 42, "trace-org")
    assert len(calls) == before
    authorized = horizon2.horizon2_connector_authorize(ids[org], "read", 42, "trace-org")
    assert authorized["server"]["server_id"] == ids[org]
    horizon2.horizon2_server_revoke(ids[org])
    assert horizon2.horizon2_server_list()["count"] == 0
    assert horizon2.horizon2_server_list(include_revoked=True)["count"] == 1
    # The global server_id key remains: a foreign collision restores the consumed pass.
    with pytest.raises(Exception, match="already registered"):
        horizon2.horizon2_server_register("server-b", "slack", "sha256:abc", ["read"], "review:42")
    assert restored == [7]
    assert all(
        kwargs == {"requested_by": "Desktop_Agent2", "force": True}
        for _tool, _payload, kwargs in calls
    )
    _horizon_org(monkeypatch, "org-B")
    assert horizon2._load("server-b").active


@pytest.mark.parametrize("action", ["revoke", "authorize"])
def test_horizon_effect_rechecks_org_after_approval(monkeypatch, identity, action):
    _horizon_org(monkeypatch, "org-A")
    monkeypatch.setattr(
        horizon2.approvals, "check_approval_gate", lambda *a, **k: {"approval_id": 7}
    )
    horizon2.horizon2_server_register("moving", "slack", "sha256:abc", ["read"], "review:42")
    restored = []
    monkeypatch.setattr(horizon2.approvals, "restore_consumed_approval", restored.append)

    def approve_then_move(*args, **kwargs):
        conn = horizon2.storage.connect()
        try:
            conn.execute("UPDATE governed_servers SET org_id='org-B' WHERE server_id='moving'")
            conn.commit()
        finally:
            conn.close()
        return {"approval_id": 7}

    monkeypatch.setattr(horizon2.approvals, "check_approval_gate", approve_then_move)
    with pytest.raises(Exception, match="unregistered"):
        if action == "revoke":
            horizon2.horizon2_server_revoke("moving")
        else:
            horizon2.horizon2_connector_authorize("moving", "read", 42, "trace-moving")
    conn = horizon2.storage.connect()
    try:
        row = conn.execute("SELECT * FROM governed_servers WHERE server_id='moving'").fetchone()
        assert row["org_id"] == "org-B" and row["revoked_at"] is None
    finally:
        conn.close()
    assert restored == [7]


def test_foreign_server_id_collision_restores_real_approval_without_write_lock(
    monkeypatch, identity
):
    _horizon_org(monkeypatch, "org-B")
    monkeypatch.setattr(
        horizon2.approvals, "check_approval_gate", lambda *a, **k: {"approval_id": 7}
    )
    horizon2.horizon2_server_register("collision", "slack", "sha256:abc", ["read"], "review:42")
    _horizon_org(monkeypatch, "org-A")
    conn = horizon2.storage.connect()
    try:
        approval_id = conn.execute(
            "INSERT INTO approvals (tool, payload, status, requested_by, created_at, updated_at, org_id) "
            "VALUES ('horizon2_server_register', '{}', 'consumed', 'Desktop_Agent2', ?, ?, 'org-A')",
            (horizon2.storage.now(), horizon2.storage.now()),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        horizon2.approvals, "check_approval_gate", lambda *a, **k: {"approval_id": approval_id}
    )
    with pytest.raises(Exception, match="already registered"):
        horizon2.horizon2_server_register("collision", "slack", "sha256:abc", ["read"], "review:42")
    conn = horizon2.storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
            == "approved"
        )
        assert (
            conn.execute(
                "SELECT org_id FROM governed_servers WHERE server_id='collision'"
            ).fetchone()[0]
            == "org-B"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("org", [None, "org-A"])
def test_doctor_ignores_foreign_connector_registration(monkeypatch, identity, org):
    from unified_agent.tools import doctor

    manifest = connectors.get_manifest("slack")
    monkeypatch.setattr(connectors, "all_manifests", lambda: {"slack": manifest})
    monkeypatch.setattr(
        connectors, "secret_status", lambda *a, **k: {"satisfied": True, "missing": []}
    )
    monkeypatch.setattr(doctor.service_probes, "connector_secret_environment", lambda: ({}, True))
    monkeypatch.setattr(
        horizon2.approvals, "check_approval_gate", lambda *a, **k: {"approval_id": 7}
    )
    _horizon_org(monkeypatch, "org-B")
    horizon2.horizon2_server_register("foreign", "slack", "sha256:abc", ["read"], "review:42")
    _horizon_org(monkeypatch, org)
    assert doctor._connectors_check()[0]["status"] == "DORMANT"
    horizon2.horizon2_server_register("local", "slack", "sha256:abc", ["read"], "review:42")
    assert doctor._connectors_check()[0]["status"] == "LIVE"


def test_registration_requires_verified_identity():
    with pytest.raises(Exception, match="authenticated"):
        horizon2.horizon2_server_register(
            "slack-prod", "slack", "sha256:abc", ["read"], "review:42"
        )


def test_connector_authorization_uses_canonical_bound_approval(monkeypatch, identity):
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda tool, payload, **kwargs: (
            calls.append((tool, payload, kwargs))
            or {
                "gated": True,
                "approval_id": 7,
            }
        ),
    )
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")

    registered = horizon2.horizon2_server_register(
        "slack-prod", "slack", "sha256:abc", ["read"], "review:42"
    )
    authorized = horizon2.horizon2_connector_authorize(
        "slack-prod", "read", task_id=42, trace_id="trace-1"
    )

    assert registered["approval_id"] == authorized["approval_id"] == 7
    assert calls[-1] == (
        "horizon2_connector_authorize",
        {"server_id": "slack-prod", "scope": "read", "task_id": 42, "trace_id": "trace-1"},
        {"requested_by": "Desktop_Agent2", "force": True},
    )


def test_revocation_persists_and_emits_audit_event(monkeypatch, identity):
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: {"gated": True, "approval_id": 7},
    )
    events = []
    monkeypatch.setattr(
        horizon2.events,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or "event-1",
    )

    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")
    result = horizon2.horizon2_server_revoke("slack-prod")

    assert result["server"]["active"] is False
    assert events[-1][0][0] == "horizon2.connector.revoked"


def test_duplicate_registration_does_not_consume_another_approval(monkeypatch, identity):
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"gated": True, "approval_id": 7},
    )
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")
    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")

    with pytest.raises(Exception, match="already registered"):
        horizon2.horizon2_server_register(
            "slack-prod", "slack", "sha256:abc", ["read"], "review:42"
        )

    assert len(calls) == 1


def test_registration_race_restores_approval_without_duplicate_event(monkeypatch, identity):
    events = []
    monkeypatch.setattr(
        horizon2.events,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or "event-1",
    )

    def approved_race(tool, *args, **kwargs):
        if tool == "horizon2_server_register":
            timestamp = horizon2.storage.now()
            conn = horizon2.storage.connect()
            try:
                conn.execute(
                    "INSERT INTO governed_servers "
                    "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, revoked_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        "slack-prod",
                        "slack",
                        "sha256:abc",
                        '["read"]',
                        "Desktop_Agent2",
                        "review:42",
                        timestamp,
                        timestamp,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return {"gated": True, "approval_id": 7}

    monkeypatch.setattr(horizon2.approvals, "check_approval_gate", approved_race)
    restored = []
    monkeypatch.setattr(horizon2.approvals, "restore_consumed_approval", restored.append)

    with pytest.raises(Exception, match="already registered"):
        horizon2.horizon2_server_register(
            "slack-prod", "slack", "sha256:abc", ["read"], "review:42"
        )

    assert restored == [7]
    assert events == []


def test_revoke_race_restores_approval_without_duplicate_event(monkeypatch, identity):
    events = []
    monkeypatch.setattr(
        horizon2.events,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or "event-1",
    )

    def approved_race(tool, *args, **kwargs):
        if tool == "horizon2_server_revoke":
            conn = horizon2.storage.connect()
            try:
                conn.execute(
                    "UPDATE governed_servers SET revoked_at=? WHERE server_id=?",
                    (horizon2.storage.now(), "slack-prod"),
                )
                conn.commit()
            finally:
                conn.close()
        return {"gated": True, "approval_id": 7}

    monkeypatch.setattr(horizon2.approvals, "check_approval_gate", approved_race)
    restored = []
    monkeypatch.setattr(horizon2.approvals, "restore_consumed_approval", restored.append)
    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")

    result = horizon2.horizon2_server_revoke("slack-prod")

    assert result["already_revoked"] is True
    assert restored == [7]
    assert [event[0][0] for event in events] == ["horizon2.connector.registered"]


def test_authorization_rejects_registered_server_scope_before_consuming_pass(monkeypatch, identity):
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: {"gated": True, "approval_id": 7},
    )
    restored = []
    monkeypatch.setattr(horizon2.approvals, "restore_consumed_approval", restored.append)
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")
    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")

    with pytest.raises(Exception, match="not approved"):
        horizon2.horizon2_connector_authorize("slack-prod", "write", task_id=42, trace_id="trace-1")

    assert restored == []


def test_authorization_rejects_unregistered_server_without_consuming_pass(monkeypatch, identity):
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"gated": True, "approval_id": 7},
    )

    with pytest.raises(Exception, match="unregistered"):
        horizon2.horizon2_connector_authorize(
            "missing-prod", "read", task_id=42, trace_id="trace-1"
        )

    assert calls == []


def test_authorization_rejects_expired_review_before_consuming_pass(monkeypatch, identity):
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("UA_H2_REVIEW_TTL_DAYS", "30")
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"gated": True, "approval_id": 7},
    )
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")
    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")
    calls.clear()
    stamped = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    conn = horizon2.storage.connect()
    try:
        conn.execute(
            "UPDATE governed_servers SET created_at=? WHERE server_id=?",
            (stamped, "slack-prod"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception, match="expired"):
        horizon2.horizon2_connector_authorize("slack-prod", "read", task_id=42, trace_id="trace-1")

    assert calls == []


def test_authorization_rejects_unbound_live_mcp_before_consuming_pass(monkeypatch, identity):
    monkeypatch.setenv("UA_H2_MCP_CONNECTIONS", "gbrain:obsidian")
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"gated": True, "approval_id": 7},
    )
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")
    horizon2.horizon2_server_register("slack-prod", "slack", "sha256:abc", ["read"], "review:42")
    calls.clear()

    with pytest.raises(Exception, match="not bound"):
        horizon2.horizon2_connector_authorize("slack-prod", "read", task_id=42, trace_id="trace-1")

    assert calls == []


def test_package_list_names_registered_connectors_without_tokens():
    listed = horizon2.horizon2_package_list()
    names = {item["package"] for item in listed["packages"]}
    assert names == {
        "slack",
        "drive",
        "obsidian",
        "readwise",
        "granola",
        "github-app",
        "zulip",
        "discord",
        "int-gcp-agent20",
        "mobile-watch",
        "cloud-mirror",
        "evidence-export",
        "browser-projection",
    }
    assert listed["count"] == 13
    assert all("token" not in item for item in listed["packages"])


def test_package_list_includes_manifest_only_connector():
    manifest = connectors.ConnectorManifest(
        name="widget-only",
        version="1.0.0",
        capabilities={"ingress"},
        entry_points={"ingress": "unified_agent.connectors:get_manifest"},
    )
    connectors.register_manifest(manifest, replace=True)
    try:
        listed = horizon2.horizon2_package_list()
        entry = next(item for item in listed["packages"] if item["package"] == "widget-only")
        assert entry["scopes"] == []
        assert entry["manifest"] == manifest.to_dict()
        assert set(entry["secrets"]) == {"present", "missing", "satisfied"}
    finally:
        connectors.unregister_manifest(manifest.name)


def test_server_list_uses_initialized_db_without_startup_initialization(monkeypatch):
    def refuse_initialization():
        raise AssertionError("read inventory must not initialize storage")

    monkeypatch.setattr(horizon2.storage, "init_db", refuse_initialization)
    monkeypatch.setattr(
        horizon2.config,
        "workspace_root",
        lambda: (_ for _ in ()).throw(AssertionError("read inventory touched workspace")),
    )

    assert horizon2.horizon2_server_list() == {"servers": [], "count": 0}


def test_server_list_fails_closed_when_database_parent_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "missing-home"))

    with pytest.raises(sqlite3.OperationalError):
        horizon2.horizon2_server_list()


def test_package_register_uses_canonical_server_register_approval(monkeypatch, identity):
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda tool, payload, **kwargs: (
            calls.append((tool, payload, kwargs)) or {"gated": True, "approval_id": 7}
        ),
    )
    monkeypatch.setattr(horizon2.events, "record_event", lambda *args, **kwargs: "event-1")

    result = horizon2.horizon2_package_register(
        "obsidian", "sha256:abc", "review:42", server_id="obsidian-vault"
    )

    assert result["server"]["connector"] == "obsidian"
    assert result["server"]["server_id"] == "obsidian-vault"
    assert result["server"]["scopes"] == ["read", "write"]
    assert calls[0][0] == "horizon2_server_register"


def test_package_register_rejects_unknown_before_approval(monkeypatch, identity):
    calls = []
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"gated": True, "approval_id": 7},
    )

    with pytest.raises(Exception, match="unknown connector package"):
        horizon2.horizon2_package_register("gmail", "sha256:abc", "review:42")

    assert calls == []
