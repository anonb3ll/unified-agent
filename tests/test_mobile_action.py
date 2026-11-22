"""mobile-action.v1 — bounded actions from an authenticated mobile client (task #779).

Covers the bounded envelope validator, the attention-acknowledgement event
store, the POST /api/mobile/v1/action route, and the mobile-client/iPhone
relay/hub/projection round trip. This simulator-style test is not evidence of
an Apple Watch consumer (there is no watchOS harness on the Linux hub host).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.projection import attention_ack, mobile_action_v1
from unified_agent.webui import api as webui_api
from unified_agent.webui import auth

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
FIXED_NOW = datetime(2026, 9, 11, 0, 40, 0, tzinfo=UTC)


def _envelope(**overrides):
    base = {
        "schema_version": "mobile-action.v1",
        "action_id": "ma_1757548800_9f3c1a2b",
        "action": "acknowledge",
        "item_ref": "attention:blocked_task:task_1234",
        "projection_id": "mp_1757548790_0a1b2c3d",
        "issued_at": FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    base.update(overrides)
    return base


# --- envelope validator -----------------------------------------------------


class TestEnvelopeValidator:
    def test_parses_a_well_formed_acknowledge(self):
        req = mobile_action_v1.parse_mobile_action(_envelope(), now=FIXED_NOW)
        assert req.action == "acknowledge"
        assert req.item_ref == "attention:blocked_task:task_1234"
        assert req.action_id == "ma_1757548800_9f3c1a2b"

    @pytest.mark.parametrize(
        "override",
        [
            {"schema_version": "mobile-action.v2"},
            {"action": "redirect"},
            {"action": "dispatch"},
            {"action_id": "nope"},
            {"item_ref": "attention:blocked_task"},
            {"item_ref": "task:1234"},
            {"projection_id": "xp_1"},
        ],
    )
    def test_rejects_bad_fields(self, override):
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(_envelope(**override), now=FIXED_NOW)

    def test_rejects_missing_and_extra_keys(self):
        missing = _envelope()
        del missing["item_ref"]
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(missing, now=FIXED_NOW)
        extra = _envelope(note="please")
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(extra, now=FIXED_NOW)

    def test_rejects_stale_and_future_issued_at(self):
        stale = _envelope(
            issued_at=(FIXED_NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(stale, now=FIXED_NOW)
        future = _envelope(
            issued_at=(FIXED_NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(future, now=FIXED_NOW)

    def test_rejects_oversized_envelope(self):
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.parse_mobile_action(
                _envelope(projection_id="mp_" + "a" * 4000), now=FIXED_NOW
            )

    def test_result_envelope_round_trips(self):
        wire = mobile_action_v1.build_action_result(
            "ma_1_a",
            "applied",
            audit_ref="01J000",
            acknowledged_until="2026-09-11T04:40:00Z",
        )
        parsed = mobile_action_v1.parse_mobile_action_result(wire)
        assert parsed.status == "applied"
        assert parsed.audit_ref == "01J000"

    def test_rejected_result_requires_allowlisted_reason(self):
        with pytest.raises(mobile_action_v1.MobileActionError):
            mobile_action_v1.build_action_result("ma_1_a", "rejected", reason="because")
        ok = mobile_action_v1.build_action_result("ma_1_a", "rejected", reason="policy_denied")
        assert ok["audit_ref"] is None


# --- acknowledgement event store -------------------------------------------


@pytest.fixture
def ua_home(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.delenv("UA_ATTENTION_ACK_TTL_S", raising=False)
    config.ensure_dirs()
    storage.init_db()
    yield


class TestAcknowledgementStore:
    def test_record_then_active_then_expire(self, ua_home):
        ref = "attention:blocked_task:task_9"
        _ulid, until, created = attention_ack.record_acknowledgement(
            ref,
            action="acknowledge",
            acked_by="Desktop_Person",
            action_id="ma_1_a",
            now=FIXED_NOW,
            ttl_s=3600,
        )
        assert created is True
        active = attention_ack.active_acknowledgements(now=FIXED_NOW)
        assert active.get(ref) == until
        # after the TTL it is gone
        later = FIXED_NOW + timedelta(hours=2)
        assert ref not in attention_ack.active_acknowledgements(now=later)

    def test_same_action_id_is_idempotent(self, ua_home):
        ref = "attention:blocked_task:task_9"
        u1, _until, c1 = attention_ack.record_acknowledgement(
            ref, action="acknowledge", acked_by="a", action_id="ma_dup", now=FIXED_NOW
        )
        u2, _until2, c2 = attention_ack.record_acknowledgement(
            ref, action="acknowledge", acked_by="a", action_id="ma_dup", now=FIXED_NOW
        )
        assert c1 is True and c2 is False
        assert u1 == u2

    def test_re_ack_with_new_id_extends_ttl(self, ua_home):
        ref = "attention:blocked_task:task_9"
        attention_ack.record_acknowledgement(
            ref,
            action="acknowledge",
            acked_by="a",
            action_id="ma_1",
            now=FIXED_NOW,
            ttl_s=3600,
        )
        t2 = FIXED_NOW + timedelta(minutes=30)
        _u, until2, _c = attention_ack.record_acknowledgement(
            ref,
            action="acknowledge",
            acked_by="a",
            action_id="ma_2",
            now=t2,
            ttl_s=3600,
        )
        assert attention_ack.active_acknowledgements(now=t2)[ref] == until2

    def test_filter_acknowledged_splits_items(self, ua_home):
        attention_ack.record_acknowledgement(
            "attention:blocked_task:task_9",
            action="acknowledge",
            acked_by="a",
            action_id="ma_1",
            now=FIXED_NOW,
            ttl_s=3600,
        )
        items = [
            {"kind": "blocked_task", "source_subject": "task:9", "severity": "high"},
            {"kind": "blocked_task", "source_subject": "task:10", "severity": "high"},
        ]
        kept, suppressed = attention_ack.filter_acknowledged(items, now=FIXED_NOW)
        assert [i["source_subject"] for i in kept] == ["task:10"]
        assert suppressed == ["attention:blocked_task:task_9"]


# --- POST /api/mobile/v1/action route + round trip -------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Person:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    monkeypatch.delenv("UA_ATTENTION_ACK_TTL_S", raising=False)
    config.ensure_dirs()
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _stub_attention(monkeypatch, *items, apply_ack_filter=False):
    async def fake_attention_payload():
        surfaced = list(items)
        if apply_ack_filter:
            surfaced, _ = attention_ack.filter_acknowledged(surfaced)
        return {
            "items": surfaced,
            "informational": [],
            "counts": {},
            "total": len(surfaced),
            "errors": [],
            "omitted": [],
        }

    monkeypatch.setattr(webui_api, "attention_payload", fake_attention_payload)


BLOCKED_ITEM = {
    "kind": "blocked_task",
    "severity": "high",
    "source": {"task_id": 1234},
    "source_subject": "task:1234",
    "source_host": "laptop",
    "hub_origin": "local",
    "age_s": 60,
    "reason": "blocked on review",
    "title": "do the thing",
}


class TestMobileActionRoute:
    def test_disabled_connector_hides_projection_and_action(self, client, monkeypatch):
        monkeypatch.delenv("UA_MOBILE_WATCH_ENABLED", raising=False)
        res = client.get("/api/mobile/v1/projection", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 404
        res = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_envelope(),
        )
        assert res.status_code == 404

    def test_get_is_not_allowed(self, client):
        assert client.get("/api/mobile/v1/action").status_code == 405

    def test_requires_authenticated_identity(self, client):
        res = client.post("/api/mobile/v1/action", json=_envelope())
        assert res.status_code == 401

    def test_session_cookie_action_still_requires_csrf(self, client, monkeypatch):
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        session_id, csrf = auth.create_session("Desktop_Person")
        client.cookies.set(auth.COOKIE_NAME, session_id)
        body = _envelope(issued_at=_now_iso())

        rejected = client.post("/api/mobile/v1/action", json=body)
        assert rejected.status_code == 403
        assert rejected.json()["error"] == "invalid or missing CSRF token"

        accepted = client.post(
            "/api/mobile/v1/action",
            headers={"x-ua-csrf": csrf},
            json=body,
        )
        assert accepted.status_code == 200

    def test_invalid_envelope_is_400(self, client):
        res = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_envelope(action="redirect"),
        )
        assert res.status_code == 400
        assert res.json()["status"] == "rejected"

    def test_unknown_item_is_noop(self, client, monkeypatch):
        _stub_attention(monkeypatch)  # no items
        res = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_envelope(issued_at=_now_iso()),
        )
        assert res.status_code == 200
        assert res.json()["status"] == "noop"

    def test_acknowledge_applies_then_replays_as_noop(self, client, monkeypatch):
        _stub_attention(monkeypatch, BLOCKED_ITEM)
        body = _envelope(item_ref="attention:blocked_task:task_1234", issued_at=_now_iso())

        res1 = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        assert res1.status_code == 200
        payload1 = res1.json()
        assert payload1["status"] == "applied"
        assert payload1["audit_ref"]
        assert payload1["acknowledged_until"]

        # exact replay of the same action_id: idempotent, no second event
        res2 = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "noop"

    def test_round_trip_suppresses_the_item_in_the_projection(self, client, monkeypatch):
        """Simulator slice: mobile client reads mobile.v1, acks an item, and it
        disappears from the next projection until the acknowledgement expires."""
        _stub_attention(monkeypatch, BLOCKED_ITEM, apply_ack_filter=True)

        projection = client.get("/api/mobile/v1/projection").json()
        assert projection["attention"]["total"] == 1
        item_ref = projection["attention"]["items"][0]["item_ref"]

        res = client.post(
            "/api/mobile/v1/action",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_envelope(item_ref=item_ref, issued_at=_now_iso()),
        )
        assert res.json()["status"] == "applied"

        after = client.get("/api/mobile/v1/projection").json()
        assert after["attention"]["total"] == 0
        assert after["attention"]["items"] == []

        # far in the future the ack has expired: monkeypatch the ack clock
        real_active = attention_ack.active_acknowledgements

        def expired_active(now=None):
            return real_active(now=datetime.now(UTC) + timedelta(days=1))

        monkeypatch.setattr(attention_ack, "active_acknowledgements", expired_active)
        resurfaced = client.get("/api/mobile/v1/projection").json()
        assert resurfaced["attention"]["total"] == 1


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
