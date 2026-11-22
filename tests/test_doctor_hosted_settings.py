"""Doctor uses the hub's effective settings, independently of an operator shell."""

import json
from datetime import UTC, datetime

import pytest

from unified_agent import edition
from unified_agent.tools import doctor


@pytest.mark.parametrize(
    ("unit_extra", "settings_extra", "beacon_org", "expected"),
    [
        ({}, {}, "org_settings", "LIVE"),
        ({"UA_EDITION": "enterprise"}, {}, "org_settings", "LIVE"),
        ({"UA_HUB_WORKOS_ORGANIZATION_ID": "org_unit"}, {}, "org_unit", "LIVE"),
        ({"WORKOS_ORGANIZATION_ID": "org_pilot"}, {}, "org_pilot", "LIVE"),
        ({}, {"UA_EDITION": "core"}, None, "DORMANT"),
        ({}, {}, "foreign_org", "DEGRADED"),
    ],
)
def test_effective_hub_org_includes_supported_settings(
    tmp_path, monkeypatch, unit_extra, settings_extra, beacon_org, expected
):
    home = tmp_path / "hub"
    home.mkdir()
    settings_file = home / "settings.toml"
    settings_values = {"UA_HUB_WORKOS_ORGANIZATION_ID": "org_settings", **settings_extra}
    settings_file.write_text(
        "[env]\n" + "\n".join(f'{key} = "{value}"' for key, value in settings_values.items()) + "\n"
    )
    settings_file.chmod(0o600)
    unit = {"UA_HOME": str(home), **unit_extra}
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_unrelated_shell")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_unrelated_pilot_shell")
    monkeypatch.setattr(doctor.service_probes, "_service_env", lambda _unit: unit)
    beacon_dir = home / edition.BEACON_DIRNAME
    beacon_dir.mkdir(mode=0o700)
    (beacon_dir / "6161.json").write_text(
        json.dumps(
            {
                "pid": 6161,
                "process": "test-hub",
                "edition": "enterprise" if beacon_org is not None else "core",
                "org_id": beacon_org,
                "seen_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    before = dict(unit)
    row = doctor._tenant_consistency_check()
    assert row["status"] == expected, row
    assert unit == before
