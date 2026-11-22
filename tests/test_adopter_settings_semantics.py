"""Reject unusable installation values before writing or seeding settings."""

import json

import pytest

from unified_agent import projects, settings


@pytest.mark.parametrize(
    "name,value",
    [
        ("UA_TRANSPORT", "banana"),
        ("UA_TRANSPORT", "HTTP"),
        ("UA_HTTP_PORT", "-1"),
        ("UA_HTTP_PORT", "65536"),
        ("UA_HTTP_PORT", "1.5"),
        ("UA_OPERATOR_TZ", "Mars/Olympus"),
        ("UA_WORKSPACE", "$HOME/agents"),
        ("UA_VAULT_PATH", "${NOTES}/vault"),
        ("UA_DEV_ROOT", "$CHECKOUTS"),
        ("UA_PROJECTS_JSON", '[{"id":"unified-agent","dirname":"../outside"}]'),
        ("UA_PROJECTS_JSON", '[{"id":"client","dirname":"client"}]'),
    ],
)
def test_semantically_invalid_env_values_are_warned_and_not_seeded(name, value):
    values, warnings = settings.to_env({"env": {name: value}})
    assert values == {}
    assert len(warnings) == 1
    assert name in warnings[0]


@pytest.mark.parametrize(
    "data,name",
    [
        ({"server": {"transport": "banana"}}, "UA_TRANSPORT"),
        ({"server": {"port": -1}}, "UA_HTTP_PORT"),
        ({"operator": {"timezone": "Mars/Olympus"}}, "UA_OPERATOR_TZ"),
        ({"paths": {"workspace": "${HOME}/agents"}}, "UA_WORKSPACE"),
        (
            {"projects": {"entries": [{"id": "unified-agent", "dirname": "../outside"}]}},
            "UA_PROJECTS_JSON",
        ),
    ],
)
def test_typed_invalid_values_use_the_same_preflight(data, name):
    values, warnings = settings.to_env(data)
    assert values == {}
    assert len(warnings) == 1 and name in warnings[0]


def test_check_rejects_invalid_file_and_set_leaves_good_file_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    monkeypatch.setenv("UA_SETTINGS_FILE", str(path))
    path.write_text('[server]\ntransport = "banana"\n')
    assert settings.run_settings_cli(["check"]) == 1
    path.write_text('[server]\ntransport = "http"\n')
    before = path.read_bytes()
    assert settings.run_settings_cli(["set", "server.port", "-1"]) == 2
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_bad_override_preserves_valid_typed_value_and_environment_priority(tmp_path):
    values, warnings = settings.to_env({"server": {"port": 9000}, "env": {"UA_HTTP_PORT": -1}})
    assert values == {"UA_HTTP_PORT": "9000"}
    assert len(warnings) == 1
    path = tmp_path / "settings.toml"
    path.write_text("[server]\nport = 9000\n")
    env = {"UA_SETTINGS_FILE": str(path), "UA_HTTP_PORT": "9001"}
    assert settings.apply_to_environ(env) == {}
    assert env["UA_HTTP_PORT"] == "9001"


def test_valid_values_keep_runtime_semantics_without_expanding_paths():
    registry = [{"id": "unified-agent", "dirname": "client-hub", "repo": "example/hub"}]
    values, warnings = settings.to_env(
        {
            "env": {
                "UA_TRANSPORT": "stdio",
                "UA_HTTP_PORT": 0,
                "UA_OPERATOR_TZ": "Europe/Berlin",
                "UA_WORKSPACE": "~/agents",
                "UA_VAULT_PATH": "",
                "UA_PROJECTS_JSON": json.dumps(registry),
                "UA_FUTURE_COMPATIBILITY_KNOB": "explicit passthrough",
            }
        }
    )
    assert warnings == []
    assert values["UA_HTTP_PORT"] == "0"
    assert values["UA_WORKSPACE"] == "~/agents"
    assert values["UA_VAULT_PATH"] == ""
    assert (
        projects._parse_registry(values["UA_PROJECTS_JSON"])["unified-agent"]["dirname"]
        == "client-hub"
    )
    assert values["UA_FUTURE_COMPATIBILITY_KNOB"] == "explicit passthrough"


def test_empty_timezone_keeps_host_default():
    values, warnings = settings.to_env({"operator": {"timezone": ""}})
    assert values == {"UA_OPERATOR_TZ": ""}
    assert warnings == []
