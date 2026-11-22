"""Tests for GET /api/capability-catalog (task #1066 / #1067)."""

from __future__ import annotations

import json
import secrets
from datetime import date

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.webui import api as webui_api

TOKEN = f"tok-{secrets.token_urlsafe(8)}"

_ITEM_ALLOWLIST = frozenset(
    {
        "id",
        "family",
        "capability",
        "customer_purpose",
        "origin",
        "implementation_state",
        "consumer",
        "entry_point",
        "connection_state",
        "evidence",
        "boundaries",
        "incomplete",
        "wedge",
        "verification",
    }
)
_FAMILY_ALLOWLIST = frozenset({"id", "label", "pilot", "definition"})


@pytest.fixture
def client(monkeypatch, tmp_path):
    """In-process catalog client — no live hub URL/token leakage (task #1127)."""
    data_home = tmp_path / "ua-home"
    monkeypatch.setenv("UA_HOME", str(data_home))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.delenv("UA_HTTP_HUB_URL", raising=False)
    monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
    config.ensure_dirs()
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def test_get_capability_catalog_happy_path(client):
    from unified_agent import object_model

    res = client.get("/api/capability-catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["status"] == 200
    catalog = data["catalog"]
    assert catalog is not None
    assert len(catalog["families"]) == 10
    pilots = [f for f in catalog["families"] if f.get("pilot") is True]
    assert len(pilots) == 5
    assert len(catalog["items"]) == 55
    assert data["meta"]["as_of"]
    assert data["meta"]["total_items"] == 55
    assert len(data["meta"]["pilot_families"]) == 5
    # Task #1127: agents polling the catalog get machine-readable object discovery.
    assert data["manifest"] == object_model.manifest()
    assert len(data["manifest"]["object_types"]) == len(object_model.OBJECT_TYPES)

    for family in catalog["families"]:
        assert set(family.keys()) <= _FAMILY_ALLOWLIST
    for item in catalog["items"]:
        assert set(item.keys()) <= _ITEM_ALLOWLIST

    audit = next(item for item in catalog["items"] if item["id"] == "gov-security-audit-tooling")
    assert audit["implementation_state"] == "shipped-and-used"
    # Verified live 2026-09-19 (task #1122); the payload must reflect the
    # catalog's recorded state and a live claim needs dated live evidence.
    assert audit["connection_state"] == "live"
    assert any(ev.get("type") == "live" and ev.get("date") for ev in audit["evidence"])

    gcp = next(item for item in catalog["items"] if item["id"] == "int-gcp-agent20")
    # #1067: the payload must never upgrade an unknown capability to live.
    assert gcp["connection_state"] == "unknown"
    assert gcp["connection_state"] != "live"
    assert "live" not in gcp["connection_state"]

    # UA #1067 verification projection
    verification_meta = data["meta"]["verification_meta"]
    assert isinstance(verification_meta, dict)
    counts = verification_meta["counts_by_classification"]
    assert sum(counts.values()) == len(catalog["items"])
    live_count = sum(
        1
        for item in catalog["items"]
        if item.get("verification", {}).get("classification") == "connected-and-live"
    )
    assert counts.get("connected-and-live", 0) == live_count

    pilot_ids = {f["id"] for f in catalog["families"] if f.get("pilot") is True}
    for item in catalog["items"]:
        if item.get("family") in pilot_ids:
            assert item["verification"]["classification"]


def test_connected_and_live_honesty(client):
    res = client.get("/api/capability-catalog")
    assert res.status_code == 200
    for item in res.json()["catalog"]["items"]:
        if item.get("verification", {}).get("classification") != "connected-and-live":
            continue
        dated_live = any(
            isinstance(ev, dict)
            and ev.get("type") == "live"
            and isinstance(ev.get("date"), str)
            and ev["date"]
            for ev in item.get("evidence", [])
        )
        assert dated_live, item["id"]


def test_actual_gcp_catalog_item_records_verified_consumer_but_stays_unknown(client):
    data = client.get("/api/capability-catalog").json()
    gcp = next(item for item in data["catalog"]["items"] if item["id"] == "int-gcp-agent20")
    assert "autonomous_cycle_manage" in gcp["consumer"]
    assert gcp["verification"]["classification"] == "unknown"


def test_verification_meta_distinguishes_catalog_snapshot_from_evaluation_date():
    path = webui_api.REPO_ROOT / "docs" / "capability-catalog" / "catalog.json"
    payload = webui_api._build_capability_catalog_payload(path, now=date(2026, 10, 2))
    verification_meta = payload["meta"]["verification_meta"]
    assert verification_meta["as_of"] == payload["meta"]["as_of"]
    assert verification_meta["evaluated_on"] == "2026-10-02"
    assert verification_meta["evaluated_on"] != verification_meta["as_of"]


@pytest.mark.asyncio
async def test_capability_catalog_missing_file(tmp_path):
    from unified_agent import object_model

    missing = tmp_path / "missing.json"
    payload = await webui_api.capability_catalog_payload(catalog_path=missing)
    assert payload["available"] is False
    assert payload["catalog"] is None
    assert payload["status"] == 404
    assert isinstance(payload["error"], str) and payload["error"]
    assert payload["manifest"] == object_model.manifest()


def test_capability_catalog_get_only(client):
    res = client.post("/api/capability-catalog", json={})
    assert res.status_code == 405


def test_capability_catalog_route_inventory_get_only():
    live = {
        route.path: frozenset(m for m in (route.methods or set()) if m not in {"HEAD"})
        for route in webui.web_routes()
    }
    assert live["/api/capability-catalog"] == frozenset({"GET"})


def test_sanitize_hostile_item_redacts_extra_keys():
    hostile = {
        "id": "hostile-item",
        "family": "governance-evidence",
        "capability": "Hostile",
        "customer_purpose": "x",
        "origin": {"kind": "task", "refs": []},
        "implementation_state": "shipped-and-used",
        "consumer": "ops",
        "entry_point": {"kind": "tool", "ref": "x"},
        "connection_state": "unknown",
        "evidence": [{"type": "code", "ref": "a"}],
        "boundaries": {
            "security": "s",
            "identity": "",
            "approval": "",
            "privacy": "",
            "release": "",
            "tenant_workspace": "",
        },
        "secret": "should-not-leak",
        "private_note": "nope",
        "api_key": "AKIA...",
        "verification": {"classification": "connected-and-live"},  # must not pass through
    }
    out = webui_api._sanitize_capability_item(hostile)
    assert out is not None
    assert set(out.keys()) <= _ITEM_ALLOWLIST
    assert "secret" not in out
    assert "private_note" not in out
    assert "api_key" not in out
    assert "verification" not in out


@pytest.mark.asyncio
async def test_route_level_unknown_item_with_live_evidence_stays_unknown(tmp_path):
    """Task #1181 — route-level fixture for the #1067 anti-false-live guard.

    The static assertion on int-gcp-agent20 (above) pins one real catalog row.
    This test pins the *projection logic itself*: an item whose recorded
    connection_state is "unknown" but which otherwise carries every field a
    genuinely-live item carries (dated "live" evidence, a real consumer, a
    concrete entry_point, shipped-and-used implementation_state) must still
    come back as connection_state == "unknown" after going through the real
    payload builder (capability_catalog_payload -> _build_capability_catalog_payload
    -> _sanitize_capability_item). If the projection ever starts inferring or
    upgrading connection_state from evidence/implementation_state instead of
    passing it through verbatim, this test must fail.
    """
    fixture_catalog = {
        "schema_version": "1.0",
        "freshness": {
            "as_of": "2026-09-20",
            "source_baseline_sha": "deadbeef",
            "origin_main_sha": "deadbeef",
            "live_hub_checked_at": "2026-09-20T00:00:00Z",
        },
        "families": [
            {
                "id": "test-family",
                "label": "Test family",
                "pilot": False,
                "definition": "Fixture-only family for task #1181.",
            }
        ],
        "items": [
            {
                "id": "fixture-unknown-with-live-looking-evidence",
                "family": "test-family",
                "capability": "Fixture capability that looks live but is not verified",
                "customer_purpose": "Pins the #1067 projection guard at the payload-builder layer.",
                "origin": {
                    "kind": "source",
                    "refs": ["src/unified_agent/webui/api.py"],
                },
                # Every field below matches what a genuinely-live item carries...
                "implementation_state": "shipped-and-used",
                "consumer": "developer agents and CI",
                "entry_point": {
                    "kind": "mcp-tool",
                    "ref": "fixture_tool",
                    "note": "Looks exactly like a real, wired-up entry point.",
                },
                "evidence": [
                    {
                        "type": "code",
                        "ref": "src/unified_agent/webui/api.py:4310",
                        "detail": "Fixture code evidence.",
                    },
                    {
                        "type": "test",
                        "ref": "tests/test_webui_capability_catalog.py",
                        "detail": "Fixture test evidence.",
                    },
                    {
                        "type": "live",
                        "ref": "fixture live probe via Mac hub",
                        "detail": "Dated live evidence row — the exact shape #1067 requires "
                        "before a connected-and-live classification is honest.",
                        "date": "2026-09-20",
                    },
                ],
                "boundaries": {
                    "security": "n/a",
                    "identity": "n/a",
                    "approval": "n/a",
                    "privacy": "n/a",
                    "release": "n/a",
                    "tenant_workspace": "n/a",
                },
                # ...except the recorded connection_state, which is the one
                # field the catalog author has NOT (yet) verified.
                "connection_state": "unknown",
            }
        ],
        "queue_coverage": [],
        "traceable_paths": [],
    }
    catalog_path = tmp_path / "fixture-catalog.json"
    catalog_path.write_text(json.dumps(fixture_catalog), encoding="utf-8")

    payload = await webui_api.capability_catalog_payload(catalog_path=catalog_path)

    assert payload["available"] is True
    item = payload["catalog"]["items"][0]
    assert item["id"] == "fixture-unknown-with-live-looking-evidence"

    # The #1067 defect class: the payload builder upgrading a recorded
    # "unknown" connection_state to "live" because the item otherwise looks
    # live (dated live evidence, real consumer, shipped implementation).
    assert item["connection_state"] == "unknown"
    assert item["connection_state"] != "live"
    assert "live" not in item["connection_state"]


def test_get_capability_catalog_no_auto_creation(client):
    before = storage.connect().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    res = client.get("/api/capability-catalog")
    assert res.status_code == 200
    after = storage.connect().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert before == after

    data = res.json()
    blob = json.dumps(data)
    assert "auto_dispatch" not in blob
    # No executable action envelope in the read payload.
    assert "action" not in data
    assert "actions" not in data
    for item in data["catalog"]["items"]:
        verification = item.get("verification") or {}
        rec = verification.get("recommendation")
        if rec is not None:
            assert rec.get("actionable") is False
            assert rec.get("review_only") is True
            assert "created" not in rec
            assert "task_id" not in rec
        # Server-computed verification must never carry mutation hooks.
        assert "task_add" not in verification
        assert "dispatch" not in verification
