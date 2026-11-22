"""Tailnet node-identity resolution for authenticated hub requests (task #1250, E3).

The hub serves tailnet-only (loopback or the Tailscale CGNAT range), so the
calling peer's node identity is resolvable server-side from its peer IP via
the local tailscaled — ``tailscale whois --json <ip>``. That is a local
daemon query, not a network call, which is the entire provenance budget this
module spends.

Fail-closed everywhere:

- ``UA_TAILNET_PEER_MODE=off`` turns every lookup off. It is the default on a
  hosted hub (``RAILWAY_ENVIRONMENT_ID`` or ``UA_HOSTED=1``), where the edge
  proxy itself connects from the CGNAT range (task #2114).

- Non-CGNAT peers (loopback probes, LAN addresses, anything public) resolve
  to ``None`` without spawning anything.
- A missing ``tailscale`` binary, a timeout, a non-zero exit, or unparseable
  output all resolve to ``None`` — the request proceeds with no device link.
- Peer keys are never stored or logged: only the node MagicDNS name, stable
  node id, owning user login, and resolution timestamp are kept.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Tailscale CGNAT range — the only addresses a tailnet peer can present.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# whois answers are stable for a session lifetime; a short TTL keeps repeated
# tool calls from a single MCP session from spawning a subprocess per call
# while still picking up renames reasonably quickly.
_CACHE_TTL_S = 300.0
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_lock = threading.Lock()


def _tailscale_bin() -> str:
    return os.environ.get("UA_TAILSCALE_BIN", "tailscale")


def _whois_timeout_s() -> float:
    try:
        return max(0.5, float(os.environ.get("UA_TAILNET_WHOIS_TIMEOUT_S", "3")))
    except ValueError:
        return 3.0


def _run_whois(ip: str) -> subprocess.CompletedProcess | None:
    """Run ``tailscale whois --json`` against local tailscaled; None on any
    failure (missing binary, timeout, spawn error). Never raises."""
    try:
        return subprocess.run(
            [_tailscale_bin(), "whois", "--json", ip],
            capture_output=True,
            text=True,
            timeout=_whois_timeout_s(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def peer_lookup_enabled() -> bool:
    """False when ``UA_TAILNET_PEER_MODE`` is ``off`` (default on a hosted hub)."""
    from . import config

    return config.tailnet_peer_mode() == "whois"


def is_funnel_request(headers) -> bool:
    """True when tailscaled marked the request as public Funnel traffic (#1892).

    ``addTailscaleIdentityHeaders`` in serve.go deletes any client-supplied
    ``Tailscale-Funnel-Request`` and re-sets ``?1`` only when the connection
    arrived through Funnel, so the header's *presence* is ground truth that
    the request came from the public internet — stronger than Host or
    ``X-Forwarded-Host``, both of which a Funnel client controls (TLS
    terminates on SNI, not Host). The marker can only restrict: a forged
    value merely locks the sender out of ``/ui`` and ``/api``, so it is
    honoured on every listener, including direct TCP where injection is
    possible.
    """
    return headers.get("tailscale-funnel-request") is not None


def _is_tailnet_peer(peer_ip: str | None) -> bool:
    if not peer_ip:
        return False
    try:
        return ipaddress.ip_address(peer_ip.strip()) in _CGNAT
    except ValueError:
        return False


def _extract_device(whois: dict[str, Any]) -> dict[str, Any] | None:
    """Whitelist extraction — node name, stable id, owning user. The whois
    document carries node keys and capability maps that must never persist."""
    node = whois.get("Node")
    if not isinstance(node, dict):
        return None
    name = node.get("Name")
    if not isinstance(name, str) or not name.strip():
        return None
    device: dict[str, Any] = {"node": name.strip().rstrip(".")}
    stable_id = node.get("StableID") or node.get("ID")
    if isinstance(stable_id, str) and stable_id.strip():
        device["node_id"] = stable_id.strip()
    elif isinstance(stable_id, int):
        device["node_id"] = str(stable_id)
    profile = whois.get("UserProfile")
    if isinstance(profile, dict):
        login = profile.get("LoginName")
        if isinstance(login, str) and login.strip():
            device["user"] = login.strip()
    device["resolved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return device


def resolve_peer_device(peer_ip: str | None) -> dict[str, Any] | None:
    """Resolve a tailnet peer IP to its node identity, or None (fail-closed).

    ``{"node", "node_id"?, "user"?, "resolved_at"}`` on success — never peer
    keys, never raises.

    ``UA_TAILNET_PEER_MODE=off`` (the default on a hosted hub, task #2114)
    returns None without running whois: Railway's edge proxy connects from the
    CGNAT range, so a hosted peer address is not a tailnet node.
    """
    if not peer_lookup_enabled() or not _is_tailnet_peer(peer_ip):
        return None
    ip = peer_ip.strip()
    now = time.monotonic()
    with _lock:
        cached = _cache.get(ip)
        if cached and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
    proc = _run_whois(ip)
    device: dict[str, Any] | None = None
    if proc is not None and proc.returncode == 0:
        try:
            parsed = json.loads(proc.stdout)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            device = _extract_device(parsed)
    if device is None:
        # Deliberately terse — stderr may carry node identifiers we do not
        # want in logs; the absence of a device link is itself the evidence.
        logger.debug("tailnet whois did not resolve peer %s", ip)
    with _lock:
        _cache[ip] = (now, device)
    return device


# --- peer census for the Hosts page (task #1389) -------------------------------

_peers_cache: tuple[float, list[dict[str, Any]] | None] | None = None


def _peer_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    host = str(raw.get("HostName") or "").strip()
    dns = str(raw.get("DNSName") or "").strip().rstrip(".")
    if not host and not dns:
        return None
    ips = [ip for ip in (raw.get("TailscaleIPs") or []) if isinstance(ip, str)]
    row = {
        "name": host or dns.split(".")[0],
        "dns_name": dns or None,
        "ip": ips[0] if ips else None,
        "online": bool(raw.get("Online")),
        "os": str(raw.get("OS") or "") or None,
    }
    # Status and whois expose the same stable node id when available. The node
    # registry's device_node may contain this id (or the MagicDNS name), even
    # when UA_NODE_NAME makes the host row's name unrelated to Tailscale.
    node_id = raw.get("StableID") or raw.get("ID")
    if isinstance(node_id, str) and node_id.strip():
        row["node_id"] = node_id.strip()
    elif isinstance(node_id, int):
        row["node_id"] = str(node_id)
    return row


def peers(*, now: float | None = None) -> list[dict[str, Any]] | None:
    """Tailnet peers from local ``tailscale status --json``; None when unavailable.

    Whitelisted fields only (name, MagicDNS name, stable node id when present,
    first tailnet IP, online, OS).
    Read-only and cached like whois. This is a census for display, not a
    registry: nothing is stored.
    """
    global _peers_cache
    if not peer_lookup_enabled():
        return None
    clock = time.monotonic() if now is None else now
    with _lock:
        if _peers_cache and clock - _peers_cache[0] < _CACHE_TTL_S:
            return _peers_cache[1]
    try:
        proc = subprocess.run(
            [_tailscale_bin(), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_whois_timeout_s(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    result: list[dict[str, Any]] | None = None
    if proc is not None and proc.returncode == 0:
        try:
            data = json.loads(proc.stdout or "{}")
            rows = [_peer_row(p) for p in (data.get("Peer") or {}).values() if isinstance(p, dict)]
            self_row = (
                _peer_row(data.get("Self") or {}) if isinstance(data.get("Self"), dict) else None
            )
            result = [r for r in rows if r] + ([{**self_row, "self": True}] if self_row else [])
        except (ValueError, AttributeError):
            result = None
    with _lock:
        _peers_cache = (clock, result)
    return result


def unreported_peers(
    host_names: set[str], census: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Peers with no known host name or registered device-node identity."""
    known = {h.casefold().split(".")[0] for h in host_names if h}
    out = []
    for peer in census or []:
        if peer.get("self"):
            continue
        keys = {
            str(peer.get("name") or "").casefold(),
            str(peer.get("dns_name") or "").casefold().split(".")[0],
            str(peer.get("node_id") or "").casefold(),
        }
        if not keys & known:
            out.append(peer)
    return sorted(out, key=lambda p: p.get("name") or "")
