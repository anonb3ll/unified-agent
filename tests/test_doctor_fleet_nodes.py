"""doctor's fleet_nodes row: every declared host, live or not (tasks #1393, #1835)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import config, node_registry, settings, storage
from unified_agent.tools import doctor


@pytest.fixture
def registry(monkeypatch):
    nodes: dict[str, dict] = {}
    # The doctor rows read the registry through read_nodes, which reports a
    # store failure instead of hiding it as an empty fleet (task #1987).
    monkeypatch.setattr(node_registry, "read_nodes", lambda: nodes)
    monkeypatch.delenv("UA_FLEET_NODES", raising=False)
    monkeypatch.delenv("UA_FLEET_OPTIONAL_NODES", raising=False)
    return nodes


def _beat(nodes, name, *, ago_s=5):
    nodes[name] = {
        "last_heartbeat": (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat(),
        "heartbeat_floor": None,
    }


def _declare(monkeypatch, required="", optional=""):
    monkeypatch.setenv("UA_FLEET_NODES", required)
    monkeypatch.setenv("UA_FLEET_OPTIONAL_NODES", optional)


def test_live_when_every_required_node_beats(registry, monkeypatch):
    _declare(monkeypatch, "alpha, beta,gamma", "delta")
    for name in ("alpha", "beta", "gamma"):
        _beat(registry, name)
    row = doctor._fleet_nodes_check()
    assert row["status"] == "LIVE"
    assert "alpha live; beta live; gamma live" in row["detail"]
    # The optional host is absent by decision: named, not counted against the row.
    assert "delta unreported, optional by operator decision" in row["detail"]
    states = {host["node"]: (host["required"], host["state"]) for host in row["hosts"]}
    assert states == {
        "alpha": (True, "live"),
        "beta": (True, "live"),
        "gamma": (True, "live"),
        "delta": (False, "unregistered"),
    }


def test_optional_node_that_reports_is_shown_live(registry, monkeypatch):
    _declare(monkeypatch, "alpha", "delta")
    _beat(registry, "alpha")
    _beat(registry, "delta")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "LIVE" and "delta live (optional)" in row["detail"]


def test_a_stale_optional_node_never_degrades_the_row(registry, monkeypatch):
    _declare(monkeypatch, "alpha", "delta")
    _beat(registry, "alpha")
    _beat(registry, "delta", ago_s=3 * 3600)
    row = doctor._fleet_nodes_check()
    assert row["status"] == "LIVE"
    assert (
        "delta unreported (last heartbeat 3h ago), optional by operator decision" in row["detail"]
    )


@pytest.mark.parametrize("stamp", [None, "garbage"])
def test_a_registered_node_without_a_readable_heartbeat_says_so(registry, monkeypatch, stamp):
    _declare(monkeypatch, "alpha")
    registry["alpha"] = {"last_heartbeat": stamp, "heartbeat_floor": None}
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DEGRADED"
    assert "alpha no readable heartbeat" in row["detail"]
    assert row["hosts"][0]["state"] == "stale"


def test_a_future_dated_heartbeat_is_not_live(registry, monkeypatch):
    _declare(monkeypatch, "alpha", "delta")
    _beat(registry, "alpha", ago_s=-3600)
    _beat(registry, "delta", ago_s=-30)  # small skew is tolerated
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DEGRADED"
    assert "alpha heartbeat stamped" in row["detail"] and "in the future" in row["detail"]
    assert "delta live (optional)" in row["detail"]


def test_a_naive_heartbeat_is_read_as_utc(registry, monkeypatch):
    _declare(monkeypatch, "alpha")
    registry["alpha"] = {
        "last_heartbeat": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "heartbeat_floor": None,
    }
    assert doctor._fleet_nodes_check()["status"] == "LIVE"


def test_a_name_in_both_lists_is_checked_as_required(registry, monkeypatch):
    _declare(monkeypatch, "alpha", "alpha")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DEGRADED" and "alpha never registered" in row["detail"]
    assert row["hosts"] == [
        {"node": "alpha", "required": True, "state": "unregistered", "last_heartbeat": None}
    ]


def test_an_impossible_required_name_is_misconfigured_and_liveness_still_shows(
    registry, monkeypatch
):
    _declare(monkeypatch, "alpha,build box", "-delta")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "MISCONFIGURED"
    assert "not a valid node name: build box" in row["detail"]
    assert "not a valid optional node name: -delta" in row["detail"]
    assert "alpha never registered" in row["detail"]
    assert [host["node"] for host in row["hosts"]] == ["alpha", "build box", "-delta"]


def test_an_impossible_optional_name_never_hides_a_down_required_node(registry, monkeypatch):
    _declare(monkeypatch, "alpha", "laptop air")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DEGRADED"
    assert "alpha never registered" in row["detail"]
    assert "not a valid optional node name: laptop air" in row["detail"]
    _beat(registry, "alpha")
    assert doctor._fleet_nodes_check()["status"] == "LIVE"


def test_degraded_names_each_missing_required_node(registry, monkeypatch):
    _declare(monkeypatch, "alpha,beta,gamma")
    _beat(registry, "alpha")
    _beat(registry, "beta", ago_s=3 * 3600)
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DEGRADED"
    assert "beta last heartbeat 3h ago" in row["detail"]
    assert "gamma never registered" in row["detail"]
    assert "alpha live" in row["detail"]


def test_a_node_just_outside_the_window_is_not_live(registry, monkeypatch):
    _declare(monkeypatch, "alpha")
    _beat(registry, "alpha", ago_s=node_registry.NODE_LIVE_WINDOW_S + 5)
    assert doctor._fleet_nodes_check()["status"] == "DEGRADED"


def test_undeclared_live_nodes_are_listed(registry, monkeypatch):
    _declare(monkeypatch, "alpha")
    _beat(registry, "alpha")
    _beat(registry, "newcomer")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "LIVE"
    assert "also live, undeclared: newcomer" in row["detail"]


def test_dormant_without_a_declared_fleet_names_what_is_live(registry, monkeypatch):
    _beat(registry, "alpha")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DORMANT"
    assert "UA_FLEET_NODES" in row["detail"] and "live now: alpha" in row["detail"]
    _declare(monkeypatch, "", "delta")
    row = doctor._fleet_nodes_check()
    assert row["status"] == "DORMANT"
    # Optional hosts are described in the text too, not only in ``hosts``.
    assert "delta unreported, optional by operator decision" in row["detail"]
    assert row["hosts"] == [
        {"node": "delta", "required": False, "state": "unregistered", "last_heartbeat": None}
    ]


def test_unknown_when_the_registry_cannot_be_read(registry, monkeypatch):
    _declare(monkeypatch, "alpha")

    def boom():
        raise RuntimeError("store locked")

    monkeypatch.setattr(node_registry, "read_nodes", boom)
    row = doctor._fleet_nodes_check()
    assert row["status"] == "UNKNOWN" and "store locked" in row["detail"]


def test_a_name_listed_both_ways_is_required():
    env = {"UA_FLEET_NODES": "alpha,alpha, ", "UA_FLEET_OPTIONAL_NODES": "alpha,delta"}
    assert config.fleet_nodes(env) == ("alpha",)
    assert config.fleet_optional_nodes(env) == ("delta",)


def test_settings_file_seeds_the_fleet(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '[fleet]\nnodes = ["alpha", "beta"]\noptional_nodes = ["delta"]\n', encoding="utf-8"
    )
    env: dict[str, str] = {}
    settings.apply_to_environ(env, path=path, quiet=True)
    assert config.fleet_nodes(env) == ("alpha", "beta")
    assert config.fleet_optional_nodes(env) == ("delta",)
    # A value already in the environment wins over the file.
    env = {"UA_FLEET_NODES": "gamma"}
    settings.apply_to_environ(env, path=path, quiet=True)
    assert config.fleet_nodes(env) == ("gamma",)


def test_system_doctor_row_keeps_the_row_contract(registry, monkeypatch):
    _declare(monkeypatch, "alpha")
    _beat(registry, "alpha")
    rows = [r for r in doctor.system_doctor()["checks"] if r["capability"] == "fleet_nodes"]
    assert len(rows) == 1 and rows[0]["status"] == "LIVE"
    assert set(rows[0]) == {"capability", "status", "detail", "gate", "hosts"}


# --- sibling fleet rows: clock skew and registry failures (task #1987) ---------


def test_vault_sync_row_does_not_count_a_future_dated_heartbeat_as_live(registry):
    state = {"status": "MISCONFIGURED", "detail": "broken", "as_of": datetime.now(UTC).isoformat()}
    _beat(registry, "alpha", ago_s=-3600)
    registry["alpha"]["vault_sync"] = state
    _beat(registry, "beta", ago_s=-30)  # small skew is tolerated
    registry["beta"]["vault_sync"] = {**state, "status": "LIVE", "detail": "ok"}
    row = doctor._fleet_vault_sync_check()
    assert row["status"] == "LIVE" and "alpha" not in row["detail"]


def test_node_versions_row_does_not_count_a_future_dated_heartbeat_as_live(registry, monkeypatch):
    monkeypatch.setattr(doctor, "_hub_code_sha", lambda: "a" * 40)
    _beat(registry, "alpha", ago_s=-3600)
    registry["alpha"]["code_sha"] = "b" * 40
    _beat(registry, "beta", ago_s=-30)  # small skew is tolerated
    registry["beta"]["code_sha"] = "a" * 40
    row = doctor._fleet_node_versions_check()
    assert row["status"] == "LIVE" and "alpha" not in row["detail"]


def test_an_unreadable_store_is_unknown_on_every_fleet_row_not_an_empty_fleet(monkeypatch):
    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "connect", locked)
    monkeypatch.setenv("UA_FLEET_NODES", "alpha")
    for check in (
        doctor._fleet_nodes_check,
        doctor._fleet_vault_sync_check,
        doctor._fleet_node_versions_check,
    ):
        row = check()
        assert row["status"] == "UNKNOWN", row
        assert "database is locked" in row["detail"]
    # The never-raising read the other callers use keeps its contract.
    assert node_registry.list_nodes() == {}
    with pytest.raises(sqlite3.Error):
        node_registry.read_nodes()
