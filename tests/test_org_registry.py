"""Checked-in org chart projection and its operational consumers."""

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, org_registry, profiles, storage
from unified_agent.tools import doctor, tasks


@pytest.fixture
def charted(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    rows = [
        ("operator", "$operator", ["$operator", "$operator_name"], []),
        ("product-engineering", "Shared_One", ["Shared_One"], []),
        ("platform-engineering", "Builder_One", ["Builder_One", "Shared_One"], [101]),
        ("senior-engineer-steward", "Reviewer_One", ["Reviewer_One", "Cloud_Reviewer"], []),
        ("bench", "Bench_One", ["Bench_One"], []),
    ]
    path = tmp_path / "installation-org.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": office,
                        "name": office,
                        "lead": lead,
                        "members": members,
                        "owned_epics": epics,
                        "default_lanes": [],
                        "decision_rights": ["Test work"],
                    }
                    for office, lead, members, epics in rows
                ],
            }
        )
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    return path


@pytest.fixture
def db(tmp_path, monkeypatch, charted):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "UA_AGENT_LABELS", "Builder_One:Reviewer_One:Bench_One:Shared_One:Cloud_Reviewer"
    )
    storage.init_db()
    return tmp_path


def test_explicit_chart_retains_offices_and_existing_profile_defaults(charted):
    offices = org_registry.load_registry()
    platform = next(row for row in offices if row["id"] == "platform-engineering")

    assert platform["lead"] == "Builder_One"
    assert set(platform["members"]) == {"Builder_One", "Shared_One"}
    assert platform["default_profiles"]["Builder_One"] == "developer"
    assert org_registry.office_for_epic(101, offices) == "platform-engineering"
    assert org_registry.office_for_epic(999999, offices) is None


def test_office_lookup_handles_identities_with_multiple_memberships(charted):
    offices = org_registry.load_registry()

    assert org_registry.offices_for_identity("Shared_One", offices) == [
        "product-engineering",
        "platform-engineering",
    ]
    assert org_registry.office_for_identity("Shared_One", offices) == "product-engineering"


def test_cloud_Agent17_leads_chief_of_staff_office_with_cloud_boundaries(db, charted, monkeypatch):
    """A chart may still name cloud_agent17 in the chief-of-staff office.

    The code default is developer (2026-11-11). The chart grants no authority,
    and hub workspace access stays denied by identity.
    """
    from unified_agent import registry

    chart = yaml.safe_load(charted.read_text())
    chart["offices"].append(
        {
            "id": "chief-of-staff",
            "name": "Chief of Staff",
            "lead": "cloud_agent17",
            "members": ["cloud_agent17", "Laptop_Agent5"],
            "owned_epics": [],
            "default_lanes": ["default"],
            "decision_rights": ["Queue, briefings, dispatch coordination and sweeps"],
        }
    )
    charted.write_text(yaml.safe_dump(chart))
    offices = org_registry.load_registry()

    # Human label/name stay outside the worker roster; doctor exempts the
    # configured operator office while checking explicit agent membership.
    labels = config.agent_labels() | {"cloud_agent17", "Laptop_Agent5"}

    monkeypatch.setenv("UA_AGENT_LABELS", ":".join(sorted(labels)))
    monkeypatch.setattr(config, "expected_unregistered_labels", lambda: set())
    registry.touch_agent("cloud_agent17", via="test", force=True)
    registry.touch_agent("Laptop_Agent5", via="test", force=True)
    check = doctor._org_registry_check()
    assert check["status"] == "LIVE", check["detail"]
    assert org_registry.offices_for_identity("cloud_agent17", offices) == ["chief-of-staff"]
    office = org_registry.find_office("chief-of-staff", offices)
    assert office["lead"] == "cloud_agent17"
    assert office["default_profiles"]["cloud_agent17"] == profiles.DEVELOPER
    assert office["default_profiles"]["Laptop_Agent5"] == profiles.CHIEF_OF_STAFF
    # No UA_AGENT_WORKSPACES entry at all: the identity roster still denies.
    monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
    assert config.resolve_workspace_binding("cloud_agent17") is config.WORKSPACE_DENIED


def test_explicit_chart_admits_cloud_reviewer_membership(charted):
    """Cloud membership is installation data, not a shipped fleet assumption."""
    offices = org_registry.load_registry()
    steward = next(row for row in offices if row["id"] == "senior-engineer-steward")

    assert "Cloud_Reviewer" in steward["members"]
    assert steward["lead"] == "Reviewer_One"
    assert org_registry.office_for_identity("Cloud_Reviewer", offices) == "senior-engineer-steward"


def test_office_ids_must_be_addressable_platform_resource_ids(tmp_path, monkeypatch):
    source = tmp_path / "org.yaml"
    source.write_text(
        "schema_version: 1\noffices:\n  - id: invalid/office\n"
        "    lead: Builder_One\n    members: [Builder_One]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(org_registry, "_ORG_PATH", source)
    with pytest.raises(ValueError, match="invalid or duplicate org office id"):
        org_registry.load_registry()


def test_registry_validation_reports_unknown_members_and_missing_office():
    offices = [
        {
            "id": "platform-engineering",
            "name": "Platform Engineering",
            "lead": "Missing_One",
            "members": ["Missing_One", "Builder_One"],
            "owned_epics": [],
            "default_lanes": ["dispatch-ready"],
            "decision_rights": [],
            "default_profiles": {"Builder_One": "developer"},
        }
    ]

    findings = org_registry.validate_registry({"Builder_One"}, offices)

    assert {finding["identity"] for finding in findings} == {"Missing_One"}
    assert all(finding["office"] == "platform-engineering" for finding in findings)


def test_registry_validation_reports_active_identity_without_an_office():
    findings = org_registry.validate_registry(
        {"Builder_One", "Mystery_Agent"},
        [
            {
                "id": "platform-engineering",
                "name": "Platform Engineering",
                "lead": "Builder_One",
                "members": ["Builder_One"],
                "owned_epics": [],
                "default_lanes": ["dispatch-ready"],
                "decision_rights": [],
                "default_profiles": {"Builder_One": "developer"},
            }
        ],
        active_labels={"Mystery_Agent"},
    )

    assert findings == [
        {
            "identity": "Mystery_Agent",
            "office": None,
            "issue": "active_identity_without_office",
        }
    ]


def test_registry_validation_exempts_declared_service_identities():
    offices = [
        {
            "id": "platform-engineering",
            "name": "Platform Engineering",
            "lead": "Builder_One",
            "members": ["Builder_One"],
            "owned_epics": [],
            "default_lanes": ["dispatch-ready"],
            "decision_rights": [],
            "default_profiles": {"Builder_One": "developer"},
        }
    ]

    findings = org_registry.validate_registry(
        {"Builder_One", "bridge_sender"},
        offices,
        active_labels={"bridge_sender", "Mystery_Agent"},
        service_labels={"bridge_sender"},
    )

    assert findings == [
        {"identity": "Mystery_Agent", "office": None, "issue": "active_identity_without_office"}
    ]

    missing = org_registry.validate_registry(
        {"Builder_One"}, offices, active_labels=set(), service_labels={"bridge_sender"}
    )
    assert missing == [
        {
            "identity": "bridge_sender",
            "office": None,
            "issue": "service_identity_not_in_agent_labels",
        }
    ]

    in_office = org_registry.validate_registry(
        {"Builder_One"}, offices, active_labels=set(), service_labels={"Builder_One"}
    )
    assert in_office == [
        {
            "identity": "Builder_One",
            "office": "platform-engineering",
            "issue": "service_identity_in_office",
        }
    ]


def test_load_service_identities_validates_shape():
    assert org_registry.load_service_identities({"service_identities": None}) == []
    assert org_registry.load_service_identities({}) == []
    assert org_registry.load_service_identities({"service_identities": [" a ", "a", "b"]}) == [
        "a",
        "b",
    ]
    with pytest.raises(ValueError):
        org_registry.load_service_identities({"service_identities": "a"})
    with pytest.raises(ValueError):
        org_registry.load_service_identities({"service_identities": [""]})


@pytest.mark.parametrize("label", ["bad label", "bad:label", "bad.label", "9lead", "a" * 65])
def test_load_service_identities_rejects_malformed_labels(label):
    with pytest.raises(ValueError, match="malformed"):
        org_registry.load_service_identities({"service_identities": [label]})


def _service_chart(tmp_path, services):
    path = tmp_path / "chart.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": "platform-engineering",
                        "name": "Platform Engineering",
                        "lead": "Builder_One",
                        "members": ["Builder_One"],
                        "owned_epics": [],
                        "default_lanes": [],
                        "decision_rights": ["Test work"],
                    }
                ],
                "service_identities": services,
                "personas": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_registry_rejects_malformed_service_identities(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(_service_chart(tmp_path, "bridge_sender")))
    with pytest.raises(ValueError, match="service_identities"):
        org_registry.load_registry()
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(_service_chart(tmp_path, ["bad label"])))
    with pytest.raises(ValueError, match="malformed"):
        org_registry.load_registry()


def test_task_list_role_and_office_filters_use_claimed_identity(db):
    platform_task = tasks.task_add("Platform claimed task")
    other_task = tasks.task_add("Other office task")
    tasks.task_claim(platform_task["id"], agent="Builder_One")
    tasks.task_claim(other_task["id"], agent="Reviewer_One")

    by_office = tasks.task_list(status="doing", office="platform-engineering")["tasks"]
    by_role = tasks.task_list(status="doing", role="platform-engineering")["tasks"]

    assert [row["id"] for row in by_office] == [platform_task["id"]]
    assert [row["id"] for row in by_role] == [platform_task["id"]]


def test_task_list_office_filter_includes_owned_epic_descendants(db, monkeypatch):
    epic = tasks.task_add("Temporary owned epic")
    child = tasks.task_add("Owned epic child", parent_id=epic["id"])
    other = tasks.task_add("Unrelated open task")
    platform = org_registry.find_office("platform-engineering")
    platform["owned_epics"] = [epic["id"]]
    real_find_office = org_registry.find_office
    monkeypatch.setattr(
        org_registry,
        "find_office",
        lambda value, offices=None: (
            platform if value == "platform-engineering" else real_find_office(value, offices)
        ),
    )

    rows = tasks.task_list(status="open", office="platform-engineering")["tasks"]

    assert {row["id"] for row in rows} == {epic["id"], child["id"]}
    assert other["id"] not in {row["id"] for row in rows}


def test_task_list_rejects_unknown_office_filter():
    with pytest.raises(ToolError, match="unknown office"):
        tasks.task_list(office="not-an-office")


def test_doctor_fails_when_registry_identity_is_unknown_or_unassigned(db, monkeypatch):
    from unified_agent import registry

    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One")
    registry.touch_agent("Builder_One", via="test", force=True)

    check = doctor._org_registry_check()

    assert check["status"] == "MISCONFIGURED"
    assert "Shared_One" in check["detail"]
    assert "Builder_One: member_not_in_agent_labels" not in check["detail"]


def test_doctor_fails_when_active_identity_has_no_office(db, monkeypatch):
    from unified_agent import registry

    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One")
    registry.touch_agent("Mystery_Agent", via="test", force=True)

    check = doctor._org_registry_check()

    assert check["status"] == "MISCONFIGURED"
    assert "Mystery_Agent: active_identity_without_office" in check["detail"]


def test_doctor_honors_expected_unregistered_service_and_human_labels(db, monkeypatch):
    from unified_agent import registry

    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One")
    monkeypatch.setattr(
        config,
        "expected_unregistered_labels",
        lambda: {"Desktop_Alex", "unified-agent-cron", "weekly-export-cron"},
    )
    registry.touch_agent("unified-agent-cron", via="test", force=True)
    registry.touch_agent("weekly-export-cron", via="test", force=True)

    check = doctor._org_registry_check()

    assert check["status"] == "MISCONFIGURED"
    assert "Desktop_Alex: member_not_in_agent_labels" not in check["detail"]
    assert "unified-agent-cron: active_identity_without_office" not in check["detail"]
    assert "weekly-export-cron: active_identity_without_office" not in check["detail"]


def test_doctor_accepts_declared_service_identity_without_office(db, monkeypatch, tmp_path):
    from unified_agent import registry

    path = tmp_path / "chart.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "offices": [
                    {
                        "id": "platform-engineering",
                        "name": "Platform Engineering",
                        "lead": "Builder_One",
                        "members": ["Builder_One"],
                        "owned_epics": [],
                        "default_lanes": [],
                        "decision_rights": ["Test work"],
                    }
                ],
                "service_identities": ["bridge_sender"],
                "personas": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One:bridge_sender")
    registry.touch_agent("Builder_One", via="test", force=True)

    check = doctor._org_registry_check()

    assert check["status"] == "LIVE", check["detail"]
    assert "1 declared service identity" in check["detail"]

    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One")
    check = doctor._org_registry_check()
    assert check["status"] == "MISCONFIGURED"
    assert "bridge_sender: service_identity_not_in_agent_labels" in check["detail"]


def test_doctor_open_roster_accepts_declared_active_service_identity(db, monkeypatch, tmp_path):
    from unified_agent import registry

    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(_service_chart(tmp_path, ["bridge_sender"])))
    monkeypatch.setenv("UA_AGENT_LABELS", "")
    registry.touch_agent("Builder_One", via="test", force=True)
    registry.touch_agent("bridge_sender", via="test", force=True)

    check = doctor._org_registry_check()

    assert check["status"] == "LIVE", check["detail"]
    assert "1 declared service identity" in check["detail"]

    # An undeclared active label with no office is still reported in open mode.
    registry.touch_agent("Stray_Agent", via="test", force=True)
    check = doctor._org_registry_check()
    assert check["status"] == "MISCONFIGURED"
    assert "Stray_Agent: active_identity_without_office" in check["detail"]
    assert "bridge_sender" not in check["detail"]


def test_doctor_rejects_malformed_service_matching_malformed_roster(db, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(_service_chart(tmp_path, ["bad label"])))
    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One:bad label")

    check = doctor._org_registry_check()

    assert check["status"] == "MISCONFIGURED"
    assert "malformed" in check["detail"]


def test_explicit_chart_bench_uses_existing_profile_rules(charted):
    bench = org_registry.find_office("bench")
    assert bench["members"] == ["Bench_One"]
    assert bench["default_profiles"]["Bench_One"] == "developer"


def test_starter_chart_contains_only_configured_operator(monkeypatch):
    monkeypatch.delenv("UA_ORG_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    offices = org_registry.load_registry()
    assert len(offices) == 1
    office = offices[0]
    assert office["lead"] == "Desktop_Alex"
    assert office["members"] == ["Desktop_Alex", "Alex"]
    assert office["default_profiles"] == {"Desktop_Alex": "operator", "Alex": "developer"}
    assert office["owned_epics"] == []
    assert office["default_lanes"] == []
    assert org_registry.office_for_epic(101, offices) is None


def test_starter_chart_fresh_install_doctor_is_live(monkeypatch):
    monkeypatch.delenv("UA_ORG_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    assert doctor._org_registry_check()["status"] == "LIVE"


def test_starter_chart_does_not_enroll_configured_agents(monkeypatch):
    monkeypatch.delenv("UA_ORG_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("UA_AGENT_LABELS", "Builder_One")
    check = doctor._org_registry_check()
    assert check["status"] == "MISCONFIGURED"
    assert "Builder_One: active_identity_without_office" in check["detail"]


def test_installation_override_resolves_operator_without_profile_activation(tmp_path, monkeypatch):
    from unified_agent import profiles

    path = tmp_path / "custom-org.yaml"
    path.write_text(
        "schema_version: 1\noffices:\n  - id: custom-office\n"
        "    lead: $operator\n    members: [$operator, Worker_One]\n"
        "    default_lanes: [default]\n    owned_epics: [101]\n"
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(path))
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Alex")
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alex")
    before = profiles.current()
    rows = org_registry.load_registry()
    assert org_registry.registry_path() == path
    assert rows[0]["lead"] == "Desktop_Alex"
    assert rows[0]["members"] == ["Desktop_Alex", "Worker_One"]
    assert rows[0]["default_profiles"]["Worker_One"] == profiles.default_for_identity("Worker_One")
    assert rows[0]["default_lanes"] == ["default"]
    assert org_registry.office_for_epic(101, rows) == "custom-office"
    assert profiles.current() == before
