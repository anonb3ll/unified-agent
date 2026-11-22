"""Identity join: every agent label resolves to an owning host (task #1388).

Source precedence: live ua-node > UA_AGENT_HOSTS binding > heartbeat host >
last (offline) node > label prefix onto a unique live same-prefix node.
Credential metadata is an issued lifecycle token id or a hash fingerprint,
never a token value.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from unified_agent import config, host_registry, node_queue, registry, storage


def _agents(payload: dict) -> dict:
    return {a["label"]: a for a in payload["agents"]}


def _doctor(host: str) -> None:
    host_registry.record_doctor_snapshot(
        host, {"checks": [], "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0}}
    )


def _setup(monkeypatch, tokens: str = "") -> None:
    monkeypatch.setenv("UA_SOURCE_HOST", "hubhost")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.delenv("UA_AGENT_HOSTS", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", tokens)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("UA_EDITION", raising=False)
    _doctor("hubhost")


def test_prefix_places_labels_on_a_same_prefix_node_and_server_on_the_hub(monkeypatch):
    _setup(monkeypatch)
    node_queue.register("Desktop", identity="Desktop_agent3", facts={})
    node_queue.register("mac-hub", identity="server_agent3", facts={})
    for label in ("Desktop_agent3", "Desktop_Agent2", "Desktop_Agent5", "server_agent5"):
        registry.touch_agent(label, via="hub_request", force=True)
    agents = _agents(host_registry.fleet_payload())
    assert (agents["Desktop_agent3"]["host"], agents["Desktop_agent3"]["host_source"]) == (
        "Desktop",
        "node",
    )
    assert (agents["Desktop_Agent2"]["host"], agents["Desktop_Agent2"]["host_source"]) == (
        "Desktop",
        "prefix",
    )
    assert (agents["Desktop_Agent5"]["host"], agents["Desktop_Agent5"]["host_source"]) == (
        "Desktop",
        "prefix",
    )
    assert (agents["server_agent5"]["host"], agents["server_agent5"]["host_source"]) == (
        "mac-hub",
        "prefix",
    )


def test_gcp_prefix_joins_the_gcp_node(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("UA_AGENT_LABELS", "gcp_agent5:gcp_agent18:server_agent3")
    node_queue.register("ua-worker", identity="gcp_agent5", facts={})
    registry.touch_agent("gcp_agent18", via="hub_request", force=True)
    registry.touch_agent("gcp_agent5", via="hub_request", force=True)
    agents = _agents(host_registry.fleet_payload())
    assert agents["gcp_agent18"]["host"] == "ua-worker"
    assert agents["gcp_agent18"]["host_source"] == "prefix"


def test_binding_beats_heartbeat_and_node_beats_binding(monkeypatch):
    _setup(monkeypatch)
    _doctor("mac-laptop")
    _doctor("other")
    registry.touch_agent("Laptop_Agent1", via="hub_request", host="other", force=True)
    assert _agents(host_registry.fleet_payload())["Laptop_Agent1"]["host_source"] == "heartbeat"
    monkeypatch.setenv("UA_AGENT_HOSTS", "Laptop_Agent1:mac-laptop")
    row = _agents(host_registry.fleet_payload())["Laptop_Agent1"]
    assert (row["host"], row["host_source"]) == ("mac-laptop", "binding")
    node_queue.register("airnode", identity="Laptop_Agent1", facts={})
    row = _agents(host_registry.fleet_payload())["Laptop_Agent1"]
    assert (row["host"], row["host_source"]) == ("airnode", "node")


def test_offline_node_is_a_last_known_location(monkeypatch):
    _setup(monkeypatch)
    old = datetime.now(UTC) - timedelta(days=3)
    node_queue.register("oldbox", identity="Laptop_Agent2", facts={}, now=old)
    registry.touch_agent("Laptop_Agent2", via="hub_request", force=True)
    row = _agents(host_registry.fleet_payload())["Laptop_Agent2"]
    assert (row["host"], row["host_source"]) == ("oldbox", "node_last")


def test_unknown_host_is_unplaced_not_invented(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("UA_AGENT_HOSTS", "Laptop_Agent1:nowhere")
    registry.touch_agent("Laptop_Agent1", via="hub_request", force=True)
    payload = host_registry.fleet_payload()
    row = _agents(payload)["Laptop_Agent1"]
    assert row["host"] is None and row["unplaced_host"] == "nowhere"
    assert "nowhere" not in {h["host"] for h in payload["hosts"]}


def test_unseen_configured_agent_with_binding_is_placed_but_unknown(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("UA_AGENT_LABELS", "Laptop_Agent1")
    monkeypatch.setenv("UA_AGENT_HOSTS", "Laptop_Agent1:mac-laptop")
    _doctor("mac-laptop")
    row = _agents(host_registry.fleet_payload())["Laptop_Agent1"]
    assert (row["host"], row["host_source"], row["status"]) == (
        "mac-laptop",
        "binding",
        "unknown",
    )
    assert row["credential"]["issued_token_ids"] == []


def test_unseen_configured_agent_without_binding_remains_unplaced(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_agent3:Desktop_Agent2")
    node_queue.register("desktop", identity="Desktop_agent3", facts={})
    row = _agents(host_registry.fleet_payload())["Desktop_Agent2"]
    assert row["host"] is None
    assert row["host_source"] is None
    assert row["status"] == "unknown"


def test_offline_same_prefix_node_does_not_place_another_label(monkeypatch):
    _setup(monkeypatch)
    old = datetime.now(UTC) - timedelta(seconds=node_queue.NODE_LIVE_WINDOW_S + 5)
    node_queue.register("old-desktop", identity="Desktop_agent3", facts={}, now=old)
    registry.touch_agent("Desktop_Agent2", via="hub_request", force=True)
    row = _agents(host_registry.fleet_payload())["Desktop_Agent2"]
    assert row["host"] is None and row["host_source"] is None


def test_offline_node_only_agent_has_last_known_node_source(monkeypatch):
    _setup(monkeypatch)
    old = datetime.now(UTC) - timedelta(days=3)
    node_queue.register("old-desktop", identity="Desktop_agent3", facts={}, now=old)
    row = _agents(host_registry.fleet_payload())["Desktop_agent3"]
    assert row["host"] == "old-desktop"
    assert row["host_source"] == "node_last"


def test_multiple_live_same_prefix_nodes_are_ambiguous(monkeypatch):
    _setup(monkeypatch)
    node_queue.register("desktop-a", identity="Desktop_agent3", facts={})
    node_queue.register("desktop-b", identity="Desktop_Agent1", facts={})
    registry.touch_agent("Desktop_Agent2", via="hub_request", force=True)
    row = _agents(host_registry.fleet_payload())["Desktop_Agent2"]
    assert row["host"] is None and row["host_source"] is None


def test_credentials_are_ids_and_fingerprints_never_values(monkeypatch):
    secret = f"t-{secrets.token_hex(12)}"
    _setup(monkeypatch, tokens=f"server_agent5:{secret}")
    monkeypatch.setenv("UA_AGENT_HOSTS", "server_agent5:hubhost")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_tokens (agent_label, token_hash, issued_at) VALUES (?, ?, ?)",
            ("server_agent5", "h", storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    registry.touch_agent("server_agent5", via="hub_request", force=True)
    payload = host_registry.fleet_payload()
    cred = _agents(payload)["server_agent5"]["credential"]
    assert cred["source"] == "lifecycle" and len(cred["issued_token_ids"]) == 1
    assert "token_ids" not in cred
    assert cred["fingerprints"] == [hashlib.sha256(secret.encode()).hexdigest()[:12]]
    assert secret not in repr(payload)
    hub_row = next(h for h in payload["hosts"] if h["host"] == "hubhost")
    assert next(a for a in hub_row["agents"] if a["label"] == "server_agent5")["credential"] == cred


def test_credential_metadata_is_scoped_to_tenant(monkeypatch):
    secret = f"t-{secrets.token_hex(12)}"
    _setup(monkeypatch, tokens=f"server_agent5:{secret}")
    conn = storage.connect()
    try:
        for org in (None, "acme", "other"):
            conn.execute(
                "INSERT INTO agent_tokens (agent_label, token_hash, issued_at, org_id) "
                "VALUES (?, ?, ?, ?)",
                ("server_agent5", f"hash-{org}", storage.now(), org),
            )
        conn.commit()
    finally:
        conn.close()
    registry.touch_agent("server_agent5", via="hub_request", force=True)
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "acme")
    credential = _agents(host_registry.fleet_payload())["server_agent5"]["credential"]
    assert len(credential["issued_token_ids"]) == 1
    assert credential["fingerprints"] == []
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "other")
    other = _agents(host_registry.fleet_payload())["server_agent5"]["credential"]
    assert len(other["issued_token_ids"]) == 1
    assert other["issued_token_ids"] != credential["issued_token_ids"]
    assert other["fingerprints"] == []
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID")
    local = _agents(host_registry.fleet_payload())["server_agent5"]["credential"]
    assert len(local["issued_token_ids"]) == 1
    assert local["fingerprints"] == [hashlib.sha256(secret.encode()).hexdigest()[:12]]


def test_agent_hosts_config_drops_conflicts():
    assert config.agent_hosts("a:h1,b:h2,a:h3") == {"b": "h2"}
    assert config.agent_hosts("a:h1,a:h1") == {"a": "h1"}
