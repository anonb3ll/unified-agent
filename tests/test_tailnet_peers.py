"""Tailnet peer census for the Devices pane 'unreported' rows (task #1389)."""

from __future__ import annotations

import asyncio
import json
import subprocess

from unified_agent import tailnet

STATUS = {
    "Self": {
        "HostName": "desktop",
        "DNSName": "desktop.example-tailnet.ts.net.",
        "TailscaleIPs": ["100.1.1.1"],
        "Online": True,
    },
    "Peer": {
        "a": {
            "HostName": "example-host",
            "DNSName": "example-host.example-tailnet.ts.net.",
            "Online": True,
            "TailscaleIPs": ["100.1.1.2"],
            "OS": "macOS",
            "StableID": "n-known-mac",
            "PrivateKey": "never-copied",
        },
        "b": {
            "HostName": "Example Laptop",
            "DNSName": "example-laptop.example-tailnet.ts.net.",
            "Online": False,
            "TailscaleIPs": ["100.1.1.3"],
            "OS": "macOS",
        },
        "c": {"HostName": "", "DNSName": ""},
    },
}


def _fake_run(monkeypatch, stdout: str, code: int = 0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout, "")

    monkeypatch.setattr(tailnet.subprocess, "run", run)
    monkeypatch.setattr(tailnet, "_peers_cache", None)
    return calls


def test_peers_whitelists_fields_and_marks_self(monkeypatch):
    _fake_run(monkeypatch, json.dumps(STATUS))
    peers = tailnet.peers(now=0)
    assert {p["name"] for p in peers} == {
        "desktop",
        "example-host",
        "Example Laptop",
    }
    assert all(
        set(p) <= {"name", "dns_name", "node_id", "ip", "online", "os", "self"} for p in peers
    )
    assert next(p for p in peers if p["name"] == "example-host")["node_id"] == "n-known-mac"
    assert "never-copied" not in repr(peers)


def test_unreported_excludes_self_and_known_hosts(monkeypatch):
    _fake_run(monkeypatch, json.dumps(STATUS))
    census = tailnet.peers(now=0)
    out = tailnet.unreported_peers({"example-host.local", "Desktop"}, census)
    assert [p["name"] for p in out] == ["Example Laptop"]


def test_registered_device_identity_matches_peer_with_custom_host_name(monkeypatch):
    _fake_run(monkeypatch, json.dumps(STATUS))
    census = tailnet.peers(now=0)
    # UA_NODE_NAME need not resemble either the peer's hostname or DNS name.
    assert [p["name"] for p in tailnet.unreported_peers({"desk-01", "n-known-mac"}, census)] == [
        "Example Laptop"
    ]
    assert [
        p["name"]
        for p in tailnet.unreported_peers(
            {"desk-01", "example-host.example-tailnet.ts.net"}, census
        )
    ] == ["Example Laptop"]


def test_devices_payload_uses_registered_device_identity(monkeypatch):
    from unified_agent import host_registry
    from unified_agent.webui import api

    _fake_run(monkeypatch, json.dumps(STATUS))

    async def health():
        return {}

    async def oauth():
        return {}

    monkeypatch.setattr(api, "health_payload", health)
    monkeypatch.setattr(api, "oauth_sessions_payload", oauth)
    monkeypatch.setattr(
        host_registry,
        "fleet_payload",
        lambda: {"hosts": [{"host": "desk-01", "node": {"device_node": "n-known-mac"}}]},
    )
    payload = asyncio.run(api.devices_payload())
    assert [p["name"] for p in payload["unreported_peers"]] == ["Example Laptop"]


def test_unavailable_tailscale_is_none(monkeypatch):
    _fake_run(monkeypatch, "", code=1)
    assert tailnet.peers(now=0) is None
    assert tailnet.unreported_peers({"x"}, None) == []


def test_census_is_cached(monkeypatch):
    calls = _fake_run(monkeypatch, json.dumps(STATUS))
    tailnet.peers(now=0)
    tailnet.peers(now=10)
    assert len(calls) == 1


def test_hosted_hub_runs_no_tailscale_census(monkeypatch):
    """Task #2114: with peer lookups off (the hosted default) nothing is spawned."""
    calls = _fake_run(monkeypatch, json.dumps(STATUS))
    monkeypatch.delenv("UA_TAILNET_PEER_MODE", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "env-1")
    assert tailnet.peers(now=0) is None
    assert calls == []
