"""Task #1250 (Epic E, E3): tailnet node identity -> evidence device link.

The hub serves tailnet-only; an authenticated request's peer CGNAT address
resolves to a Tailscale node via the local tailscaled (`tailscale whois`) —
no network call beyond local daemon state. The resolved node identity is
persisted on producer-stamped events and rendered in the evidence report as
a `device` link. Fail-closed everywhere: non-tailnet/loopback peers, missing
binary, timeouts, and unparseable answers leave the link absent — and a
hostname-only fallback is explicitly marked `unverified-device`.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from unified_agent import config, storage, tailnet
from unified_agent.audit import evidence_report, producer_sign


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    tailnet._cache.clear()
    yield
    tailnet._cache.clear()


def _whois_result(node_name: str, stable_id: str = "nTEST1", user: str = "operator@github"):
    return subprocess.CompletedProcess(
        args=["tailscale", "whois", "--json", "100.64.0.12"],
        returncode=0,
        stdout=json.dumps(
            {
                "Node": {
                    "ID": 12345,
                    "StableID": stable_id,
                    "Name": node_name,
                    "Key": "nodekey:SECRET-MUST-NEVER-LEAK",
                },
                "UserProfile": {"LoginName": user},
            }
        ),
        stderr="",
    )


# ---------------------------------------------------------------------------
# Resolver unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"RAILWAY_ENVIRONMENT_ID": "env-1"},
        {"UA_HOSTED": "1"},
        {"RAILWAY_ENVIRONMENT_ID": "env-1", "UA_TAILNET_PEER_MODE": "whois"},
        {"UA_HOSTED": "1", "UA_TAILNET_PEER_MODE": "whois"},
        {"UA_TAILNET_PEER_MODE": "off"},
        {"UA_TAILNET_PEER_MODE": "nonsense"},
    ],
)
def test_hosted_hub_never_runs_whois_for_an_edge_proxy_peer(monkeypatch, env):
    """Task #2114: Railway's edge proxy connects from 100.64.0.x, inside CGNAT.

    On a hosted hub that address is not a tailnet node, so whois must not run
    and the peer must resolve to None even when whois would have answered.
    """
    for name in ("RAILWAY_ENVIRONMENT_ID", "UA_HOSTED", "UA_TAILNET_PEER_MODE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    calls = []

    def whois(ip):
        calls.append(ip)
        return _whois_result("edge-proxy.tailnet-x.ts.net.")

    monkeypatch.setattr(tailnet, "_run_whois", whois)
    assert config.tailnet_peer_mode() == "off"
    assert tailnet.resolve_peer_device("100.64.0.2") is None
    assert calls == []


def test_peer_mode_defaults_and_explicit_whois_on_a_local_hub(monkeypatch):
    for name in ("RAILWAY_ENVIRONMENT_ID", "UA_HOSTED", "UA_TAILNET_PEER_MODE"):
        monkeypatch.delenv(name, raising=False)
    assert config.tailnet_peer_mode() == "whois"
    monkeypatch.setenv("UA_TAILNET_PEER_MODE", "off")
    assert config.tailnet_peer_mode() == "off"
    # A local listener can opt back in; a hosted proxy listener cannot.
    monkeypatch.setenv("UA_TAILNET_PEER_MODE", "whois")
    monkeypatch.setattr(
        tailnet, "_run_whois", lambda ip: _whois_result("desktop-host.tailnet-x.ts.net.")
    )
    assert tailnet.resolve_peer_device("100.64.0.2")["node"] == "desktop-host.tailnet-x.ts.net"


def test_tailnet_peer_resolves_to_node_identity(monkeypatch):
    monkeypatch.setattr(
        tailnet, "_run_whois", lambda ip: _whois_result("desktop-host.tailnet-x.ts.net.")
    )
    device = tailnet.resolve_peer_device("100.64.0.12")
    assert device is not None
    assert device["node"] == "desktop-host.tailnet-x.ts.net"
    assert device["node_id"] == "nTEST1"
    assert device["user"] == "operator@github"
    assert device["resolved_at"]
    # Peer keys are never carried through.
    assert "Key" not in device
    assert "SECRET-MUST-NEVER-LEAK" not in json.dumps(device)


def test_non_tailnet_peer_fails_closed_without_calling_whois(monkeypatch):
    calls = []
    monkeypatch.setattr(tailnet, "_run_whois", lambda ip: calls.append(ip))
    for ip in ("127.0.0.1", "::1", "192.168.1.10", "8.8.8.8", "not-an-ip", "", None):
        assert tailnet.resolve_peer_device(ip) is None, ip
    assert calls == [], "whois must never run for non-CGNAT peers"


def test_whois_failure_and_malformed_output_fail_closed(monkeypatch):
    monkeypatch.setattr(
        tailnet,
        "_run_whois",
        lambda ip: subprocess.CompletedProcess([], returncode=1, stdout="", stderr="no peer"),
    )
    assert tailnet.resolve_peer_device("100.64.0.12") is None

    monkeypatch.setattr(
        tailnet,
        "_run_whois",
        lambda ip: subprocess.CompletedProcess([], returncode=0, stdout="not json", stderr=""),
    )
    tailnet._cache.clear()
    assert tailnet.resolve_peer_device("100.64.0.12") is None

    monkeypatch.setattr(tailnet, "_run_whois", lambda ip: None)  # timeout/OSError path
    tailnet._cache.clear()
    assert tailnet.resolve_peer_device("100.64.0.12") is None


def test_whois_result_is_cached_per_ip(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tailnet,
        "_run_whois",
        lambda ip: calls.append(ip) or _whois_result("node-a.ts.net."),
    )
    first = tailnet.resolve_peer_device("100.64.0.12")
    second = tailnet.resolve_peer_device("100.64.0.12")
    assert first == second and calls == ["100.64.0.12"]


# ---------------------------------------------------------------------------
# Producer-event persistence
# ---------------------------------------------------------------------------


def _stamp_with_device(device):
    token = config.peer_device_var.set(device)
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        return producer_sign.stamp_authenticated_producer_event(
            kind="approval", ref_id="42", task_id="1250"
        )
    finally:
        config.agent_identity_var.reset(identity)
        config.peer_device_var.reset(token)


def test_producer_event_persists_verified_device(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:bearer-x")
    device = {
        "node": "desktop-host.tailnet-x.ts.net",
        "node_id": "nTEST1",
        "user": "operator@github",
        "resolved_at": "2026-09-22T00:00:00+00:00",
    }
    assert _stamp_with_device(device) is not None

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ROWID DESC LIMIT 1",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row["payload"])
    assert payload["device"] == device


def test_producer_event_omits_device_when_unresolved(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:bearer-x")
    assert _stamp_with_device(None) is not None
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY ROWID DESC LIMIT 1",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()
    finally:
        conn.close()
    assert "device" not in json.loads(row["payload"])


def test_verifying_producer_events_carry_device(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:bearer-x")
    device = {
        "node": "desktop-host.tailnet-x.ts.net",
        "node_id": "nTEST1",
        "user": "operator@github",
        "resolved_at": "2026-09-22T00:00:00+00:00",
    }
    _stamp_with_device(device)
    conn = storage.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT ulid, channel, payload, originator, created_at, entry_hash FROM event_log"
            ).fetchall()
        ]
    finally:
        conn.close()
    producers = producer_sign.verifying_producer_events(rows, chain_valid=True)
    assert producers and producers[0]["device"] == device
    # A broken chain yields no verified producers, so no device link either.
    assert producer_sign.verifying_producer_events(rows, chain_valid=False) == []


# ---------------------------------------------------------------------------
# Evidence-report device link
# ---------------------------------------------------------------------------


def _insert_task(task_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, "
            "domain, lane) VALUES (?, 'device link wedge', 'done', 'high', "
            "'2026-09-22T00:00:00+00:00', '2026-09-22T00:00:00+00:00', "
            "'2026-09-22T00:00:00+00:00', 'test-device.local', 'Desktop_Agent4', "
            "NULL, '', 'unified-agent', 'hobby', 'hobby')",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_via_approval(task_id: int, device) -> dict:
    from unified_agent.tools.approvals import (
        ApprovalPendingError,
        approval_pending,
        approval_resolve,
        check_approval_gate,
    )

    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo device", "task_id": task_id},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo device", "task_id": task_id}), rid),
        )
        conn.commit()
    finally:
        conn.close()

    identity = config.agent_identity_var.set("Desktop_Person")
    dev = config.peer_device_var.set(device)
    try:
        approval_resolve(rid, "approve")
    finally:
        config.peer_device_var.reset(dev)
        config.agent_identity_var.reset(identity)
    return evidence_report.build_evidence_report(task_id=task_id)


def test_report_renders_verified_device_link(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:bearer-h")
    _insert_task(1250)
    report = _resolve_via_approval(
        1250,
        {
            "node": "mac-hub.tailnet-x.ts.net",
            "node_id": "nMBP9",
            "user": "operator@github",
            "resolved_at": "2026-09-22T01:00:00+00:00",
        },
    )
    rec = report["records"][0]
    link = rec["device_link"]
    assert link["verified"] is True
    assert link["node"] == "mac-hub.tailnet-x.ts.net"
    assert link["node_id"] == "nMBP9"
    assert link["resolved_at"] == "2026-09-22T01:00:00+00:00"
    assert rec["device"].startswith("mac-hub.tailnet-x.ts.net")
    assert "unverified-device" not in rec["device"]


def test_report_marks_hostname_only_fallback_unverified(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:bearer-h")
    _insert_task(1251)
    # No device resolved (loopback / non-tailnet / whois failure).
    report = _resolve_via_approval(1251, None)
    rec = report["records"][0]
    assert rec["device_link"]["verified"] is False
    assert rec["device_link"]["host"] == "test-device.local"
    assert rec["device"] == "unverified-device:test-device.local"


def test_report_ambiguous_verified_devices_fail_closed(monkeypatch):
    """Two producer stamps from different nodes cannot settle one device —
    the link must degrade to unverified rather than pick one arbitrarily."""
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:bearer-h")
    _insert_task(1252)
    producers = [
        {"identity": "Desktop_Person", "device": {"node": "a.ts.net", "node_id": "n1"}},
        {"identity": "Desktop_Person", "device": {"node": "b.ts.net", "node_id": "n2"}},
    ]
    link = evidence_report._device_link(producers, "test-device.local")
    assert link["verified"] is False
    assert sorted(link["nodes"]) == ["a.ts.net", "b.ts.net"]
