"""Synthetic installation charts for membership/routing tests, never product defaults."""

import pytest
import yaml


@pytest.fixture
def installation_org(tmp_path, monkeypatch):
    # Harness identities exercise existing profile rules. Office ownership and
    # memberships below are test data, not a copy of an operator's organization.
    rows = [
        ("chief-of-staff", "Desktop_Agent5", ["Desktop_Agent5"], []),
        ("product-engineering", "Desktop_Agent7", ["Desktop_Agent7", "Desktop_Agent4"], [102]),
        (
            "platform-engineering",
            "Desktop_Agent2",
            [
                "Desktop_Agent2",
                "Desktop_agent3",
                "Desktop_Agent4",
                "server_agent3",
                "server_agent2",
            ],
            [101],
        ),
        (
            "senior-engineer-steward",
            "Desktop_Agent1",
            ["Desktop_Agent1", "server_agent1", "cloud_agent1"],
            [],
        ),
        ("bench", "Desktop_agent12", ["Desktop_agent12", "Desktop_agent10", "server_agent4"], []),
    ]
    chart = {
        "schema_version": 1,
        "offices": [
            {
                "id": office,
                "name": office.replace("-", " ").title(),
                "lead": lead,
                "members": members,
                "owned_epics": epics,
                "default_lanes": [],
                "decision_rights": ["Implement assigned test work"],
            }
            for office, lead, members, epics in rows
        ],
    }
    path = tmp_path / "installation-org.yaml"
    path.write_text(yaml.safe_dump(chart), encoding="utf-8")
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    return path
