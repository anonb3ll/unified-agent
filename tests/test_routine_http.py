"""The routine HTTP trigger shares hub identity and durable fire receipts."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import policy, profiles, safety, server, storage, webui
from unified_agent.routines import routine_add, routine_pause
from unified_agent.tools.approvals import ApprovalPendingError


@pytest.fixture
def client(monkeypatch):
    chief_token = secrets.token_urlsafe(24)
    developer_token = secrets.token_urlsafe(24)
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Laptop_Agent5:{chief_token},Desktop_Agent2:{developer_token}",
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ROUTINES_ENABLED", "1")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    # Production Laptop_Agent5 is a developer. Pin chief-of-staff here so the
    # fire handler is what these tests exercise. cloud_agent13 cannot open /api.
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "Laptop_Agent5", profiles.CHIEF_OF_STAFF
    )
    storage.init_db()
    with TestClient(server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))) as http:
        yield http, chief_token, developer_token


def _fire(client, routine_id, token=None, key="event-1", **kwargs):
    headers = {"Idempotency-Key": key}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(kwargs.pop("headers", {}))
    return client.post(f"/api/routines/{routine_id}/fire", headers=headers, **kwargs)


def test_http_fire_creates_one_task_and_replay_returns_same_receipt(client):
    http, chief_token, _ = client
    routine_id = routine_add("On demand", trigger="api")["id"]
    first = _fire(http, routine_id, chief_token)
    assert first.status_code == 200
    assert first.json() == {"status": "created", "task_id": 1}
    assert _fire(http, routine_id, chief_token).json() == first.json()
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM routine_runs").fetchone()[0] == 1


def test_http_fire_requires_bearer_and_chief_of_staff_even_in_warn_mode(client):
    http, chief_token, developer_token = client
    routine_id = routine_add("Protected", trigger="webhook")["id"]
    assert _fire(http, routine_id).status_code == 401
    assert _fire(http, routine_id, developer_token).status_code == 403
    assert _fire(http, routine_id, chief_token).status_code == 200


def test_http_fire_rejects_cookie_session(client):
    http, chief_token, _ = client
    routine_id = routine_add("Cookie boundary", trigger="api")["id"]
    session = http.post("/api/session", json={"token": chief_token})
    assert session.status_code == 200
    assert _fire(http, routine_id).status_code == 401


def test_http_fire_rejects_bad_key_origin_and_unfirable_definition(client):
    http, token, _ = client
    api_id = routine_add("API", trigger="api")["id"]
    cron_id = routine_add("Cron", trigger="cron", schedule="* * * * *")["id"]
    assert _fire(http, api_id, token, key="").status_code == 400
    assert _fire(http, api_id, token, key=" event-1 ").status_code == 400
    assert _fire(http, api_id, token, key="x" * 201).status_code == 400
    assert (
        _fire(http, api_id, token, headers={"Origin": "https://untrusted.example"}).status_code
        == 403
    )
    assert _fire(http, cron_id, token).status_code == 409
    assert _fire(http, 999999, token).status_code == 404
    routine_pause(api_id)
    assert _fire(http, api_id, token).status_code == 409


def test_http_fire_requires_runtime_opt_in(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Disabled", trigger="webhook")["id"]
    monkeypatch.delenv("UA_ROUTINES_ENABLED")
    assert _fire(http, routine_id, token).status_code == 503


def test_http_fire_runs_the_registered_tool_policy_gate(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Policy protected", trigger="api")["id"]

    def deny(tool, payload, **_kwargs):
        assert tool == "routine_fire"
        assert payload == {"routine_id": routine_id, "idempotency_key": "event-1"}
        raise policy.PolicyViolation("routine fire denied")

    monkeypatch.setattr(safety, "check_policy_gate", deny)
    response = _fire(http, routine_id, token)
    assert response.status_code == 403
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_http_fire_surfaces_pending_policy_approval_without_creating_work(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Approval protected", trigger="webhook")["id"]

    def require_approval(_tool, _payload, **_kwargs):
        raise ApprovalPendingError("pending", approval_id=42)

    monkeypatch.setattr(safety, "check_policy_gate", require_approval)
    response = _fire(http, routine_id, token)
    assert response.status_code == 202
    assert response.json() == {
        "status": "approval_pending",
        "approval_id": 42,
        "code": "approval_required",
    }
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_approved_fire_retry_returns_receipt_without_second_approval(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Approved once", trigger="api")["id"]
    first = _fire(http, routine_id, token)
    assert first.status_code == 200

    def second_approval_would_be_wrong(_tool, _payload, **_kwargs):
        raise ApprovalPendingError("a new pass would be required", approval_id=43)

    monkeypatch.setattr(safety, "check_policy_gate", second_approval_would_be_wrong)
    assert _fire(http, routine_id, token).json() == first.json()
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_receipt_replay_records_tool_activity_and_sample(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Observed replay", trigger="api")["id"]
    touched = []
    samples = []
    monkeypatch.setattr(
        server.registry, "touch_agent", lambda *args, **kwargs: touched.append(kwargs)
    )
    monkeypatch.setattr(storage, "record_perf_sample", lambda **kwargs: samples.append(kwargs))
    assert _fire(http, routine_id, token).status_code == 200
    assert _fire(http, routine_id, token).status_code == 200
    assert sum(call.get("via") == "tool_call" for call in touched) == 2
    assert [sample["tool"] for sample in samples] == ["routine_fire", "routine_fire"]


def test_concurrent_same_key_retry_uses_one_approval(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Concurrent", trigger="api")["id"]
    entered, release = Event(), Event()
    calls = []

    def gate(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(safety, "check_policy_gate", gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_fire, http, routine_id, token)
        assert entered.wait(5)
        second = pool.submit(_fire, http, routine_id, token)
        release.set()
        receipts = [first.result(timeout=5), second.result(timeout=5)]
    assert all(response.status_code == 200 for response in receipts)
    assert receipts[0].json() == receipts[1].json()
    assert len(calls) == 1


def test_pause_cannot_consume_approval_between_check_and_fire(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Pause race", trigger="api")["id"]
    entered, release, pause_started = Event(), Event(), Event()

    def gate(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(safety, "check_policy_gate", gate)

    def pause_after_signal():
        pause_started.set()
        return routine_pause(routine_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        firing = pool.submit(_fire, http, routine_id, token)
        assert entered.wait(5)
        pausing = pool.submit(pause_after_signal)
        assert pause_started.wait(5)
        assert not pausing.done()
        release.set()
        assert firing.result(timeout=5).status_code == 200
        assert pausing.result(timeout=5)["paused"] == 1
    with storage.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_production_block_allows_development_but_blocks_production(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Environment gate", trigger="api")["id"]
    monkeypatch.setenv("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", "1")
    monkeypatch.setenv("UA_RUNTIME_ENV", "development")
    assert _fire(http, routine_id, token).status_code == 200
    monkeypatch.setenv("UA_RUNTIME_ENV", "production")
    assert _fire(http, routine_id, token, key="event-2").status_code == 403


def test_unfirable_routine_does_not_enter_approval_gate(client, monkeypatch):
    http, token, _ = client
    routine_id = routine_add("Paused", trigger="api")["id"]
    routine_pause(routine_id)

    def must_not_gate(_tool, _payload, **_kwargs):
        raise AssertionError("approval pass consumed before routine validation")

    monkeypatch.setattr(safety, "check_policy_gate", must_not_gate)
    assert _fire(http, routine_id, token).status_code == 409
    monkeypatch.delenv("UA_ROUTINES_ENABLED")
    assert _fire(http, routine_id, token).status_code == 503
