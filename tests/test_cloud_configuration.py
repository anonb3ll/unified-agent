"""Optional cloud/chat targets must be configured before any external I/O."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, settings
from unified_agent.tools import cloud_recovery, gcloud, zulip
from unified_agent.webui import api


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_cloud_getters_have_no_ambient_targets(monkeypatch, raw):
    for key in ("UA_GCP_PROJECT", "UA_GCP_WORKER"):
        if raw is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, raw)
    assert config.gcp_project() is None
    assert config.gcp_worker() is None


def test_cloud_chat_settings_feed_explicit_env(monkeypatch):
    env = {"UA_GCP_PROJECT": "service-project"}
    data = {
        "cloud": {"project": "settings-project", "worker": "selected-worker"},
        "chat": {"zulip_site": "https://zulip.example.test"},
    }
    values, warnings = settings.to_env(data)
    assert not warnings
    assert values == {
        "UA_GCP_PROJECT": "settings-project",
        "UA_GCP_WORKER": "selected-worker",
        "UA_ZULIP_SITE": "https://zulip.example.test",
    }
    # Test real precedence using a temporary in-memory loader, no user settings.
    monkeypatch.setattr(settings, "load", lambda path: data)
    settings.apply_to_environ(env, path=Path("fixture.toml"))
    assert env["UA_GCP_PROJECT"] == "service-project"
    assert env["UA_GCP_WORKER"] == "selected-worker"


@pytest.mark.parametrize("missing", ["UA_GCP_PROJECT", "UA_GCP_WORKER"])
def test_unconfigured_cloud_paths_do_no_io(monkeypatch, missing):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.delenv(missing, raising=False)
    inventory = Mock(side_effect=AssertionError("unexpected inventory"))
    operations = Mock(side_effect=AssertionError("unexpected operation"))
    audit = Mock(side_effect=AssertionError("unexpected audit read/write"))
    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory)
    monkeypatch.setattr(gcloud, "gcloud_ops", operations)
    monkeypatch.setattr(cloud_recovery, "_recent_attempt", audit)
    monkeypatch.setattr(cloud_recovery.events, "record_event_once", audit)
    for health in (
        gcloud.gcp_Agent20_health(),
        gcloud.classify_gcp_Agent20_health({"sections": {"instances": "ok"}, "instances": []}),
    ):
        assert health["status"] == "UNKNOWN"
        assert missing in health["detail"]
    result = cloud_recovery.cloud_worker_recover(dry_run=False)
    assert result["outcome"] == "unknown"
    assert missing in result["detail"]
    inventory.assert_not_called()
    operations.assert_not_called()
    audit.assert_not_called()


def test_unconfigured_inventory_does_not_resolve_gcloud(monkeypatch):
    monkeypatch.delenv("UA_GCP_PROJECT", raising=False)
    execute = Mock(side_effect=AssertionError("unexpected subprocess"))
    monkeypatch.setattr(gcloud, "_run_gcloud_json", execute)
    with pytest.raises(ToolError, match="UA_GCP_PROJECT"):
        gcloud.gcloud_inventory()
    execute.assert_not_called()


def test_cloud_health_checks_only_selected_worker(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "selected-project")
    monkeypatch.setenv("UA_GCP_WORKER", "selected-worker")
    inventory = Mock(
        return_value={
            "sections": {"instances": "ok"},
            "instances": [
                {"name": "other-worker", "status": "RUNNING"},
                {"name": "selected-worker", "status": "TERMINATED"},
            ],
        }
    )
    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory)
    health = gcloud.gcp_Agent20_health()
    assert health["status"] == "DEGRADED"
    assert "selected-worker is TERMINATED" in health["detail"]
    inventory.assert_called_once_with(project="selected-project", sections=["instances"])


@pytest.mark.parametrize("missing", ["UA_GCP_PROJECT", "UA_GCP_WORKER"])
def test_cloud_panel_ignores_healthy_cache_when_unconfigured(monkeypatch, missing):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setattr(
        api,
        "_cloud_cache",
        (api.time.time(), ("example-project", "example-worker"), {"status": "ok", "instances": []}),
    )
    execute = Mock(side_effect=AssertionError("unexpected cloud tool call"))
    monkeypatch.setattr(api, "_safe", execute)
    result = asyncio.run(api.cloud_payload())
    assert result["status"] == "unknown"
    assert result["instance_state"] == "unknown"
    assert result["budgets"]["state"] == "unknown"
    assert result["remediation"] == "manual"
    assert missing in str(result["errors"])
    execute.assert_not_called()
    assert api._cloud_cache is None


@pytest.mark.parametrize(
    "site",
    [
        None,
        "",
        "http://zulip.example.test",
        "https://",
        "https://" + "fixture-user:fixture-password" + chr(64) + "zulip.example.test",
        "https://zulip.example.test?token=fixture",
        "https://zulip.example.test#fragment",
    ],
)
def test_zulip_requires_explicit_https_site_before_credentials_or_network(monkeypatch, site):
    if site is None:
        monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    else:
        monkeypatch.setenv("UA_ZULIP_SITE", site)
    inspect = Mock(side_effect=AssertionError("unexpected credential access"))
    network = Mock(side_effect=AssertionError("unexpected network access"))
    monkeypatch.setattr(zulip, "_config_path", inspect)
    monkeypatch.setattr(zulip.httpx, "Client", network)
    with pytest.raises(zulip.ZulipRelayError, match="UA_ZULIP_SITE"):
        zulip.load_zuliprc("Desktop_Agent2")
    status = zulip.zulip_credential_status()
    assert status["ready"] is False
    assert "UA_ZULIP_SITE" in status["configuration_error"]
    inspect.assert_not_called()
    network.assert_not_called()


def test_zulip_credential_file_cannot_select_site(monkeypatch, tmp_path):
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    monkeypatch.setattr(config, "data_home", lambda: tmp_path)
    path = tmp_path / "zuliprc-desktop-agent2"
    path.write_text(
        "[api]\nemail=bot@zulip.example.test\nkey=fixture\nsite=https://zulip.example.test\n"
    )
    path.chmod(0o600)
    with pytest.raises(zulip.ZulipRelayError, match="configure UA_ZULIP_SITE"):
        zulip.load_zuliprc("Desktop_Agent2")
    monkeypatch.setenv("UA_ZULIP_SITE", " https://zulip.example.test/ ")
    assert zulip.load_zuliprc("Desktop_Agent2").site == "https://zulip.example.test"
    monkeypatch.setenv("UA_ZULIP_SITE", "https://other.example.test")
    with pytest.raises(zulip.ZulipRelayError, match="allowlist"):
        zulip.load_zuliprc("Desktop_Agent2")


def test_zulip_retirement_still_precedes_config_and_network(monkeypatch):
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    network = Mock(side_effect=AssertionError("unexpected network access"))
    monkeypatch.setattr(zulip.httpx, "Client", network)
    with pytest.raises(zulip.ZulipRelayError, match="retired"):
        zulip.zulip_send_message("Desktop_Agent2", "coord", "test", "hello")
    network.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": "other-worker"},
        {"target": ""},
        {"project": "other-project"},
        {"zone": "other-zone"},
    ],
)
def test_recovery_rejects_caller_targets_before_inventory(monkeypatch, kwargs):
    monkeypatch.setenv("UA_GCP_PROJECT", "selected-project")
    monkeypatch.setenv("UA_GCP_WORKER", "selected-worker")
    inventory = Mock(side_effect=AssertionError("unexpected inventory"))
    operation = Mock(side_effect=AssertionError("unexpected mutation"))
    monkeypatch.setattr(gcloud, "gcloud_inventory", inventory)
    monkeypatch.setattr(gcloud, "gcloud_ops", operation)
    with pytest.raises(ToolError, match="allowed recovery target|server-configured"):
        cloud_recovery.cloud_worker_recover(dry_run=False, **kwargs)
    inventory.assert_not_called()
    operation.assert_not_called()


def test_all_zulip_transport_paths_require_site_without_io(monkeypatch):
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    inspect = Mock(side_effect=AssertionError("unexpected credential access"))
    network = Mock(side_effect=AssertionError("unexpected network access"))
    monkeypatch.setattr(zulip, "_config_path", inspect)
    monkeypatch.setattr(zulip.httpx, "Client", network)
    calls = (
        lambda: zulip.zulip_send_message("Desktop_Agent2", "coord", "test", "hello"),
        lambda: zulip.zulip_fetch_messages("Desktop_Agent2", "coord"),
        lambda: zulip.zulip_register_event_queue("Desktop_Agent2"),
        lambda: zulip.zulip_fetch_events("Desktop_Agent2", "fixture-queue", 1),
    )
    for call in calls:
        with pytest.raises(zulip.ZulipRelayError, match="UA_ZULIP_SITE"):
            call()
    inspect.assert_not_called()
    network.assert_not_called()


def _panel_inventory(project, instances, sections=None):
    return {
        "status": "ok",
        "project_id": project,
        "instances": instances,
        "sections": sections or {"instances": "ok", "project": "ok"},
        "buckets": [],
        "budgets": [],
        "errors": [],
    }


@pytest.mark.parametrize(
    "sections,expected",
    [
        ({"instances": "ok", "project": "ok"}, "absent"),
        ({"instances": "ok", "project": "unavailable"}, "unknown"),
        ({"instances": "unavailable", "project": "ok"}, "unknown"),
    ],
)
def test_cloud_panel_does_not_use_other_vm_for_worker_presence(monkeypatch, sections, expected):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(api, "_cloud_cache", None)

    async def inventory(*args, **kwargs):
        return _panel_inventory(
            "example-project", [{"name": "other-worker", "status": "RUNNING"}], sections
        )

    monkeypatch.setattr(api, "_safe", inventory)
    assert asyncio.run(api.cloud_payload())["instance_state"] == expected


def test_cloud_panel_reports_stopped_worker_beside_running_vm(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(api, "_cloud_cache", None)

    async def inventory(*args, **kwargs):
        return _panel_inventory(
            "example-project",
            [
                {"name": "other-worker", "status": "RUNNING"},
                {"name": "example-worker", "status": "TERMINATED"},
            ],
        )

    monkeypatch.setattr(api, "_safe", inventory)
    result = asyncio.run(api.cloud_payload())
    assert result["instance_state"] == "stopped"
    assert len(result["instances"]) == 2


@pytest.mark.parametrize(
    "status,expected",
    [
        ("RUNNING", "running"),
        ("TERMINATED", "stopped"),
        ("STOPPED", "stopped"),
        ("SUSPENDED", "stopped"),
        ("PROVISIONING", "transitional"),
        ("STAGING", "transitional"),
        ("STOPPING", "transitional"),
        ("SUSPENDING", "transitional"),
        ("REPAIRING", "transitional"),
        ("", "unknown"),
    ],
)
def test_cloud_panel_distinguishes_readable_transitions_from_unknown(monkeypatch, status, expected):
    # A worker that is starting, stopping or being repaired is neither running
    # nor stopped; reporting it as stopped gives the Control Center a false
    # health reading.
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(api, "_cloud_cache", None)

    async def inventory(*args, **kwargs):
        return _panel_inventory("example-project", [{"name": "example-worker", "status": status}])

    monkeypatch.setattr(api, "_safe", inventory)
    assert asyncio.run(api.cloud_payload())["instance_state"] == expected


@pytest.mark.parametrize("change", ["UA_GCP_PROJECT", "UA_GCP_WORKER"])
def test_cloud_panel_invalidates_cache_for_target_changes(monkeypatch, change):
    monkeypatch.setenv("UA_GCP_PROJECT", "project-a")
    monkeypatch.setenv("UA_GCP_WORKER", "worker-a")
    monkeypatch.setattr(api, "_cloud_cache", None)
    seen = []

    async def inventory(name, **kwargs):
        seen.append((name, kwargs["project"], config.gcp_worker()))
        return _panel_inventory(
            kwargs["project"], [{"name": config.gcp_worker(), "status": "RUNNING"}]
        )

    monkeypatch.setattr(api, "_safe", inventory)
    first = asyncio.run(api.cloud_payload())
    assert asyncio.run(api.cloud_payload())["checked_at"] == first["checked_at"]
    assert len(seen) == 1
    monkeypatch.setenv(change, "project-b" if change == "UA_GCP_PROJECT" else "worker-b")
    second = asyncio.run(api.cloud_payload())
    assert len(seen) == 2
    assert second["project_id"] == config.gcp_project()
    assert second["instances"][0]["name"] == config.gcp_worker()
    assert second["instance_state"] == "running"


def test_cloud_panel_reenable_same_target_refetches_after_disable(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(api, "_cloud_cache", None)
    seen = []

    async def inventory(*args, **kwargs):
        seen.append(kwargs)
        return _panel_inventory("example-project", [])

    monkeypatch.setattr(api, "_safe", inventory)
    asyncio.run(api.cloud_payload())
    monkeypatch.delenv("UA_GCP_WORKER")
    disabled = asyncio.run(api.cloud_payload())
    assert disabled["instance_state"] == "unknown"
    assert api._cloud_cache is None
    assert len(seen) == 1
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    asyncio.run(api.cloud_payload())
    assert len(seen) == 2


def test_cloud_panel_invalid_configuration_discards_cache_without_io(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "--invalid-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(
        api,
        "_cloud_cache",
        (api.time.time(), ("example-project", "example-worker"), {"status": "ok"}),
    )
    execute = Mock(side_effect=AssertionError("unexpected cloud tool call"))
    monkeypatch.setattr(api, "_safe", execute)
    result = asyncio.run(api.cloud_payload())
    assert result["instance_state"] == "unknown"
    assert api._cloud_cache is None
    execute.assert_not_called()


def test_cloud_panel_duplicate_worker_names_are_unknown(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.setattr(api, "_cloud_cache", None)

    async def inventory(*args, **kwargs):
        return _panel_inventory(
            "example-project",
            [
                {"name": "example-worker", "status": "RUNNING", "zone": "us-central1-a"},
                {"name": "example-worker", "status": "TERMINATED", "zone": "europe-west1-b"},
            ],
        )

    monkeypatch.setattr(api, "_safe", inventory)
    assert asyncio.run(api.cloud_payload())["instance_state"] == "unknown"
