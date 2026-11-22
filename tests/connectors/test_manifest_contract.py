"""Unit tests for the ConnectorManifest shape itself (task #1204)."""

from __future__ import annotations

import pytest

from unified_agent import connectors, storage
from unified_agent.tools import doctor, horizon2


def _manifest(**overrides) -> connectors.ConnectorManifest:
    kwargs = {
        "name": "widget",
        "version": "1.0.0",
        "capabilities": frozenset({"ingress"}),
        "required_secrets": frozenset({"WIDGET_TOKEN"}),
        "entry_points": {"ingress": "unified_agent.connectors:get_manifest"},
        "default_enabled": False,
    }
    kwargs.update(overrides)
    return connectors.ConnectorManifest(**kwargs)


def test_valid_manifest_round_trips_to_dict():
    manifest = _manifest()
    as_dict = manifest.to_dict()
    assert as_dict["name"] == "widget"
    assert as_dict["capabilities"] == ["ingress"]
    assert as_dict["required_secrets"] == ["WIDGET_TOKEN"]
    assert as_dict["default_enabled"] is False


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"name": "Widget"}, "name must match"),
        ({"name": "wid get"}, "name must match"),
        ({"version": "1.0"}, "semver"),
        ({"version": "v1.0.0"}, "semver"),
        ({"capabilities": frozenset()}, "at least one capability"),
        ({"capabilities": frozenset({"Bad Capability"})}, "invalid capability name"),
        ({"required_secrets": frozenset({"widget_token"})}, "UPPER_SNAKE_CASE"),
        ({"required_secrets": frozenset({"secret-value-not-a-name"})}, "UPPER_SNAKE_CASE"),
    ],
)
def test_invalid_manifest_fields_are_refused(overrides, match):
    with pytest.raises(ValueError, match=match):
        _manifest(**overrides)


def test_capability_without_entry_point_is_refused():
    with pytest.raises(ValueError, match="without an entry point"):
        _manifest(capabilities=frozenset({"ingress", "outbound"}))


def test_entry_point_for_undeclared_capability_is_refused():
    with pytest.raises(ValueError, match="undeclared capabilities"):
        _manifest(
            capabilities=frozenset({"ingress"}),
            entry_points={
                "ingress": "unified_agent.connectors:get_manifest",
                "outbound": "unified_agent.connectors:get_manifest",
            },
        )


def test_malformed_entry_point_string_is_refused():
    with pytest.raises(ValueError, match="module.path:callable"):
        _manifest(entry_points={"ingress": "not-a-dotted-path"})


def test_default_enabled_must_be_bool():
    with pytest.raises(ValueError, match="default_enabled must be a bool"):
        _manifest(default_enabled="false")  # type: ignore[arg-type]


def test_manifest_is_frozen_and_immutable():
    manifest = _manifest()
    with pytest.raises(AttributeError):
        manifest.name = "other"  # type: ignore[misc]


def test_manifest_freezes_nested_inputs():
    capabilities = {"ingress"}
    required_secrets = ["WIDGET_TOKEN"]
    entry_points = {"ingress": "unified_agent.connectors:get_manifest"}
    manifest = _manifest(
        capabilities=capabilities,
        required_secrets=required_secrets,
        entry_points=entry_points,
    )

    capabilities.add("outbound")
    required_secrets.append("WIDGET_SECRET")
    entry_points["ingress"] = "unified_agent.connectors:SLACK_MANIFEST"

    assert manifest.capabilities == frozenset({"ingress"})
    assert manifest.required_secrets == frozenset({"WIDGET_TOKEN"})
    assert manifest.entry_points["ingress"] == "unified_agent.connectors:get_manifest"
    with pytest.raises(TypeError):
        manifest.entry_points["ingress"] = "unified_agent.connectors:SLACK_MANIFEST"  # type: ignore[index]


def test_resolve_entry_point_returns_the_real_callable():
    manifest = _manifest()
    resolved = manifest.resolve_entry_point("ingress")
    assert resolved is connectors.get_manifest


def test_resolve_entry_point_raises_on_missing_attribute():
    manifest = _manifest(entry_points={"ingress": "unified_agent.connectors:nope_not_real"})
    with pytest.raises(ValueError, match="does not resolve to an attribute"):
        manifest.resolve_entry_point("ingress")


def test_resolve_entry_point_requires_callable_target():
    manifest = _manifest(entry_points={"ingress": "unified_agent.connectors:SLACK_MANIFEST"})
    with pytest.raises(ValueError, match="is not callable"):
        manifest.resolve_entry_point("ingress")


def test_resolve_entry_point_raises_on_bad_module():
    manifest = _manifest(entry_points={"ingress": "unified_agent.does_not_exist_module:x"})
    with pytest.raises(ValueError, match="does not import"):
        manifest.resolve_entry_point("ingress")


def test_validate_entry_points_resolve_collects_all_failures():
    manifest = _manifest(
        capabilities=frozenset({"ingress", "outbound"}),
        entry_points={
            "ingress": "unified_agent.does_not_exist_module:x",
            "outbound": "unified_agent.connectors:nope_not_real",
        },
    )
    with pytest.raises(ValueError) as exc_info:
        connectors.validate_entry_points_resolve(manifest)
    message = str(exc_info.value)
    assert "does not import" in message
    assert "does not resolve to an attribute" in message


def test_resolve_entry_point_normalizes_module_init_failure(monkeypatch):
    manifest = _manifest(entry_points={"ingress": "broken_connector:run"})
    original_import = connectors.importlib.import_module

    def fail_during_import(module_name):
        if module_name == "broken_connector":
            raise RuntimeError("startup configuration is invalid")
        return original_import(module_name)

    monkeypatch.setattr(connectors.importlib, "import_module", fail_during_import)
    with pytest.raises(ValueError, match="failed to import.*RuntimeError"):
        manifest.resolve_entry_point("ingress")


class TestManifestRegistry:
    def setup_method(self):
        connectors.unregister_manifest("widget")

    def teardown_method(self):
        connectors.unregister_manifest("widget")

    def test_register_and_get(self):
        manifest = _manifest()
        connectors.register_manifest(manifest)
        assert connectors.get_manifest("widget") is manifest
        assert connectors.require_manifest("widget") is manifest

    def test_duplicate_registration_refused(self):
        connectors.register_manifest(_manifest())
        with pytest.raises(ValueError, match="already registered"):
            connectors.register_manifest(_manifest())

    def test_replace_true_allows_overwrite(self):
        connectors.register_manifest(_manifest())
        replaced = _manifest(version="2.0.0")
        connectors.register_manifest(replaced, replace=True)
        assert connectors.get_manifest("widget").version == "2.0.0"

    def test_require_manifest_raises_for_unknown(self):
        with pytest.raises(ValueError, match="no connector manifest registered"):
            connectors.require_manifest("does-not-exist")

    def test_slack_manifest_is_registered_by_default(self):
        manifest = connectors.require_manifest("slack")
        assert manifest.capabilities == frozenset({"ingress", "outbound", "chat"})
        assert manifest.required_secrets == frozenset({"SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN"})
        assert manifest.default_enabled is False


class TestSecretStatus:
    def test_contradictory_hook_cannot_pass_start_gate(self, monkeypatch):
        broken = _manifest(
            name="contradictory-hook",
            required_secrets=frozenset(),
            secret_status_hook="unified_agent.connectors:contradictory_secret_status_hook",
        )
        monkeypatch.setattr(
            connectors,
            "contradictory_secret_status_hook",
            lambda env: {"present": [], "missing": ["API_TOKEN"], "satisfied": True},
            raising=False,
        )

        assert connectors.secret_status(broken)["error"] == "secret status hook failed (TypeError)"
        with pytest.raises(connectors.ConnectorSecretsMissingError, match="hook failed"):
            connectors.ensure_can_start(broken)

    def test_malformed_hook_result_isolated_from_doctor(self, monkeypatch):
        broken = _manifest(
            name="malformed-hook",
            required_secrets=frozenset(),
            secret_status_hook="unified_agent.connectors:malformed_secret_status_hook",
        )
        monkeypatch.setattr(
            connectors, "malformed_secret_status_hook", lambda env: [], raising=False
        )
        monkeypatch.setattr(connectors, "all_manifests", lambda: {"malformed-hook": broken})

        status = connectors.secret_status(broken)
        assert status["error"] == "secret status hook failed (TypeError)"
        check = doctor._connectors_check()[0]
        assert check["capability"] == "connector:malformed-hook"
        assert check["status"] == "UNKNOWN"

    def test_broken_hook_isolated_from_inventory_and_doctor(self, monkeypatch):
        broken = _manifest(
            name="broken-hook",
            required_secrets=frozenset(),
            secret_status_hook="unified_agent.connectors:missing_secret_status_hook",
        )
        monkeypatch.setattr(
            connectors,
            "all_manifests",
            lambda: {"broken-hook": broken, "slack": connectors.SLACK_MANIFEST},
        )

        packages = horizon2.horizon2_package_list()["packages"]
        broken_package = next(row for row in packages if row["package"] == "broken-hook")
        assert broken_package["secrets"] == {
            "present": [],
            "missing": [],
            "satisfied": False,
            "error": "secret status hook failed (AttributeError)",
        }
        assert any(row["package"] == "slack" for row in packages)

        checks = doctor._connectors_check()
        broken_check = next(row for row in checks if row["capability"] == "connector:broken-hook")
        assert broken_check["status"] == "UNKNOWN"
        assert "secret status hook failed" in broken_check["detail"]
        assert any(row["capability"] == "connector:slack" for row in checks)
        with pytest.raises(connectors.ConnectorSecretsMissingError, match="hook failed"):
            connectors.ensure_can_start(broken)

    def test_reports_names_only_never_values(self, monkeypatch):
        manifest = _manifest(required_secrets=frozenset({"WIDGET_TOKEN", "WIDGET_SECRET"}))
        monkeypatch.setenv("WIDGET_TOKEN", "super-secret-value")
        monkeypatch.delenv("WIDGET_SECRET", raising=False)
        status = connectors.secret_status(manifest)
        assert status["present"] == ["WIDGET_TOKEN"]
        assert status["missing"] == ["WIDGET_SECRET"]
        assert status["satisfied"] is False
        assert "super-secret-value" not in str(status)

    def test_ensure_can_start_raises_when_missing(self, monkeypatch):
        manifest = _manifest()
        monkeypatch.delenv("WIDGET_TOKEN", raising=False)
        with pytest.raises(connectors.ConnectorSecretsMissingError, match="WIDGET_TOKEN"):
            connectors.ensure_can_start(manifest)

    def test_ensure_can_start_returns_status_when_present(self, monkeypatch):
        manifest = _manifest()
        monkeypatch.setenv("WIDGET_TOKEN", "value")
        status = connectors.ensure_can_start(manifest)
        assert status["satisfied"] is True

    def test_explicit_environment_mapping_is_used(self, monkeypatch):
        manifest = _manifest()
        monkeypatch.setenv("WIDGET_TOKEN", "caller-value")
        status = connectors.secret_status(manifest, env={})
        assert status == {"present": [], "missing": ["WIDGET_TOKEN"], "satisfied": False}

    def test_manifest_with_no_required_secrets_always_satisfied(self):
        manifest = _manifest(required_secrets=frozenset())
        status = connectors.ensure_can_start(manifest)
        assert status == {"present": [], "missing": [], "satisfied": True}


def _seed_active_server(name: str) -> None:
    storage.init_db()
    timestamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO governed_servers "
            "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, "
            "revoked_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                f"{name}-active",
                name,
                "sha256:abcdef0123456789",
                '["read"]',
                "Desktop_Agent2",
                "review:connector-contract",
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_doctor_uses_hub_service_secrets_for_active_connector(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        _seed_active_server(manifest.name)
        monkeypatch.delenv("WIDGET_TOKEN", raising=False)
        monkeypatch.setattr(
            doctor,
            "_service_env",
            lambda _unit: {"WIDGET_TOKEN": "hub-value"},
        )

        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        assert check["status"] == "LIVE"
        assert "hub-value" not in str(check)
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_does_not_fill_missing_hub_secret_from_caller(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        _seed_active_server(manifest.name)
        monkeypatch.setenv("WIDGET_TOKEN", "caller-value")
        monkeypatch.setattr(doctor, "_service_env", lambda _unit: {})

        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        assert check["status"] == "MISCONFIGURED"
        assert "missing required secret(s)" in check["detail"]
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_and_package_projection_use_same_hub_secret_source(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        _seed_active_server(manifest.name)
        monkeypatch.setenv("WIDGET_TOKEN", "caller-value")
        monkeypatch.setattr(doctor, "_service_env", lambda _unit: {})

        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        package = next(
            item
            for item in horizon2.horizon2_package_list()["packages"]
            if item["package"] == "widget"
        )
        assert check["status"] == "MISCONFIGURED"
        assert package["secrets"] == {
            "present": [],
            "missing": ["WIDGET_TOKEN"],
            "satisfied": False,
        }
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_marks_connector_unknown_when_registry_is_unreadable(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        monkeypatch.setattr(doctor, "_service_env", lambda _unit: {})

        def fail_database():
            raise OSError("database unavailable")

        monkeypatch.setattr(doctor.storage, "init_db_once", fail_database)
        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        assert check["status"] == "UNKNOWN"
        assert "cannot determine active registration" in check["detail"]
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_marks_active_connector_misconfigured_when_service_secret_is_missing(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        _seed_active_server(manifest.name)
        monkeypatch.delenv("WIDGET_TOKEN", raising=False)
        monkeypatch.setattr(doctor, "_service_env", lambda _unit: {})

        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        assert check["status"] == "MISCONFIGURED"
        assert "missing required secret(s)" in check["detail"]
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_marks_active_connector_unknown_when_hub_secrets_are_unreadable(monkeypatch):
    manifest = _manifest()
    connectors.register_manifest(manifest, replace=True)
    try:
        _seed_active_server(manifest.name)
        monkeypatch.delenv("WIDGET_TOKEN", raising=False)
        monkeypatch.setattr(doctor, "_service_env", lambda _unit: None)

        check = next(
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        )
        assert check["status"] == "UNKNOWN"
        assert "cannot determine" in check["detail"]
    finally:
        connectors.unregister_manifest(manifest.name)


def test_doctor_contains_one_misconfiguration_for_import_init_failure(monkeypatch):
    manifest = _manifest(entry_points={"ingress": "broken_connector:run"})
    connectors.register_manifest(manifest, replace=True)
    original_import = connectors.importlib.import_module

    def fail_during_import(module_name):
        if module_name == "broken_connector":
            raise RuntimeError("startup configuration is invalid")
        return original_import(module_name)

    monkeypatch.setattr(connectors.importlib, "import_module", fail_during_import)
    try:
        checks = [
            row for row in doctor._connectors_check() if row["capability"] == "connector:widget"
        ]
        assert len(checks) == 1
        assert checks[0]["status"] == "MISCONFIGURED"
        assert "failed to import" in checks[0]["detail"]
    finally:
        connectors.unregister_manifest(manifest.name)


def test_manifest_contract_verification_invariants():
    """Desktop_agent3 verification: ensure manifest contract review invariants hold."""
    from types import MappingProxyType

    manifest = _manifest()
    # 1. Entry point must be callable
    assert callable(manifest.resolve_entry_point("ingress"))

    # 5. Entry points mapping is truly immutable
    assert isinstance(manifest.entry_points, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.entry_points["ingress"] = "unified_agent.connectors:SLACK_MANIFEST"  # type: ignore[index]

    # 2. Registered manifest appears in horizon2_package_list
    connectors.register_manifest(manifest, replace=True)
    try:
        packages = horizon2.horizon2_package_list()["packages"]
        names = {p["package"] for p in packages}
        assert "widget" in names
        entry = next(p for p in packages if p["package"] == "widget")
        assert entry["manifest"]["name"] == "widget"
    finally:
        connectors.unregister_manifest(manifest.name)
