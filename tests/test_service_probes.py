"""Doctor's systemd probes live in a services-level module (task #1722).

The executor, delivery receipts and the Horizon 2 tools used to import
``tools.doctor`` (an adapter) for its service probes, three upward imports in
the layer map. The probes now live in ``unified_agent.service_probes``.
``tools.doctor`` re-exports every moved name, and a patch made through either
module reaches every caller, so tests that patch ``doctor._service_env`` keep
faking the probe the moved code reads.
"""

from __future__ import annotations

import pytest

from unified_agent import hub_client, service_probes, tool_surface
from unified_agent.dispatch import receipt
from unified_agent.executor import daemon
from unified_agent.tools import doctor, horizon2

_HUB = service_probes._SERVICE_UNITS["hub_service"]


@pytest.mark.parametrize("name", service_probes.DOCTOR_REEXPORTS)
def test_doctor_reexports_every_moved_name(name):
    assert getattr(doctor, name) is getattr(service_probes, name)


def test_patch_through_doctor_reaches_the_probe_module(monkeypatch):
    original = service_probes._service_env
    monkeypatch.setattr(doctor, "_service_env", lambda unit: {"UA_X": unit})

    assert service_probes.connector_secret_environment() == ({"UA_X": _HUB}, True)
    assert horizon2.service_probes.connector_secret_environment()[0] == {"UA_X": _HUB}

    monkeypatch.undo()
    assert service_probes._service_env is original
    assert doctor._service_env is original


def test_patch_on_the_probe_module_reaches_doctor_callers(monkeypatch):
    monkeypatch.setattr(service_probes, "_service_env", lambda unit: {"UA_DEPLOYMENT_MODE": "x"})

    assert doctor._hub_setting("UA_DEPLOYMENT_MODE") == ("x", True)


@pytest.mark.parametrize("name", service_probes.DOCTOR_REEXPORTS)
def test_patch_on_the_probe_module_is_read_through_doctor(monkeypatch, name):
    """A direct read of a re-export sees a patch made on ``service_probes``."""
    original = getattr(service_probes, name)
    fake = object()
    # A nested context: undoing it must not also undo conftest's own patches.
    with monkeypatch.context() as patch:
        patch.setattr(service_probes, name, fake)

        assert getattr(doctor, name) is fake
        assert vars(doctor)[name] is fake, "doctor's own bare-name callers see it too"

    assert getattr(doctor, name) is original
    assert getattr(service_probes, name) is original


def test_service_env_patched_on_the_probe_module_answers_direct_doctor_calls(monkeypatch):
    monkeypatch.setattr(service_probes, "_service_env", lambda unit: {"UA_X": "new"})

    assert service_probes.connector_secret_environment() == ({"UA_X": "new"}, True)
    assert doctor._service_env(_HUB) == {"UA_X": "new"}


def test_unit_property_patch_through_doctor_reaches_service_env(monkeypatch):
    monkeypatch.setattr(doctor, "_unit_property", lambda unit, prop: "UA_A=1" if prop else None)
    monkeypatch.setattr(doctor, "_unit_environment_files", lambda unit: [])

    assert service_probes._service_env(_HUB) == {"UA_A": "1"}


def test_receipt_reads_executor_auth_from_the_probe_module(monkeypatch):
    monkeypatch.setattr(
        service_probes,
        "_active_executor_check",
        lambda: {"status": "LIVE", "detail": "x: active, probe_process=separate"},
    )

    assert receipt._executor_auth_proof() == {
        "status": "LIVE",
        "authenticated": True,
        "probe_process": "separate",
    }


def test_executor_fallback_runs_system_doctor_through_the_tool_surface(monkeypatch):
    seen = []

    def fake_system_doctor(**kwargs):
        seen.append(kwargs)
        return {"checks": [], "via": "tool_surface"}

    monkeypatch.setitem(tool_surface.REGISTERED_TOOLS, "system_doctor", fake_system_doctor)
    client = daemon.HubClient(
        agent_label="Executor_Test", hub_url="http://127.0.0.1:9/mcp", bearer_token="t"
    )

    assert client._direct_tool_fallback("system_doctor", {}) == {
        "checks": [],
        "via": "tool_surface",
    }
    assert seen == [{}]


def test_hub_auth_probe_uses_the_transport_client_without_fallback(monkeypatch):
    seen = []

    def fake_ping(self):
        seen.append((isinstance(self, hub_client.HubClient), self.allow_fallback))

    monkeypatch.setattr(hub_client.HubClient, "ping", fake_ping)

    ok, detail = service_probes._hub_auth_probe("http://127.0.0.1:9/mcp", "tok", 0.1)

    assert ok, detail
    assert seen == [(True, False)]


def test_executor_client_adds_only_the_fallback_to_the_probed_transport():
    """The probe builds ``hub_client.HubClient``; the executor's subclass must
    not change the request, or the probe would vouch for a different path."""
    own = {
        name
        for name in vars(daemon.HubClient)
        if not (name.startswith("__") and name.endswith("__"))
    }
    assert own == {"_direct_tool_fallback"}
    assert daemon.HubClient.__bases__ == (hub_client.HubClient,)


def test_unit_check_designed_exit_policy_stays_with_doctor(monkeypatch):
    """Doctor's timer exit-code tables apply only through doctor's wrapper."""
    monkeypatch.setattr(
        service_probes,
        "_service_probe",
        lambda unit, timeout: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "LastExitCode": "1",
        },
    )
    monkeypatch.setattr(doctor, "_steward_audit_gate_tripped", lambda: True)
    monkeypatch.setitem(
        doctor._TIMER_DESIGNED_EXIT_EVIDENCE,
        "steward_audit_cron",
        {"1": lambda: True},
    )
    unit = doctor._TIMER_UNITS["steward_audit_cron"]

    assert doctor._unit_check("steward_audit_cron", unit)["status"] == "DEGRADED"
    assert service_probes._unit_check("steward_audit_cron", unit)["status"] == "MISCONFIGURED"


@pytest.mark.parametrize("root", ["<array/>", "<string>fixture</string>", "<true/>", ""])
def test_moved_probe_preserves_main_non_mapping_plist_guard(tmp_path, monkeypatch, root):
    path = tmp_path / "fixture.plist"
    path.write_text('<?xml version="1.0"?><plist version="1.0">' + root + "</plist>")
    monkeypatch.setattr(service_probes, "_mac_launchagent_plist", lambda unit: path)
    assert service_probes._mac_launchagent_environment_layers("fixture.service") == ({}, {}, False)


def test_valid_launchagent_layers_survive_the_move(tmp_path, monkeypatch):
    import plistlib

    path = tmp_path / "fixture.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {"UA_FIXTURE": "ok"},
                "ProgramArguments": ["/usr/bin/fixture-launcher"],
            }
        )
    )
    monkeypatch.setattr(service_probes, "_mac_launchagent_plist", lambda unit: path)
    assert service_probes._mac_launchagent_environment_layers("fixture.service") == (
        {"UA_FIXTURE": "ok"},
        {},
        True,
    )
