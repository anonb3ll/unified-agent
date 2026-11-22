"""Cloud mirror receiver (task #1285): auth-everywhere, dedupe, schema rejection, export/delete."""

from __future__ import annotations

import stat
import sys
import threading
from types import SimpleNamespace

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

from unified_agent import cloud_mirror_receiver as receiver

TOKEN_VAR = "UA_CLOUD_MIRROR_RECEIVER_TOKEN_TEST"
INGEST_TOKEN_VAR = "UA_CLOUD_MIRROR_INGEST_TOKEN_TEST"


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_VAR, "s3cr3t-receiver-token")
    monkeypatch.setenv(INGEST_TOKEN_VAR, "s3cr3t-ingest-token")
    app = receiver.create_app(
        db_path=tmp_path / "mirror.db",
        read_token_env_var=TOKEN_VAR,
        ingest_token_env_var=INGEST_TOKEN_VAR,
    )
    with TestClient(app) as client:
        yield client


def _auth():
    return {"Authorization": "Bearer s3cr3t-receiver-token"}


def _ulid_id(alias: str) -> str:
    """Map compact test aliases to ordered canonical ULIDs."""
    return "0" * 24 + f"{int(alias[1:]):02X}"


def _ingest_auth():
    return {"Authorization": "Bearer s3cr3t-ingest-token"}


def _post_records(client, records):
    return client.post("/ingest", json={"records": records}, headers=_ingest_auth())


def _record(event_id="e1", channel="agent.task.updated", **overrides):
    record = {
        "schema_version": 1,
        "event_id": _ulid_id(event_id) if event_id.startswith("e") else event_id,
        "channel": channel,
        "kind": "task",
        "agent": "Desktop_Agent1",
        "task_id": "42",
        "status": None,
        "timestamp": "2026-09-23T00:00:00Z",
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# Auth required on every route
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/ingest"),
        ("GET", "/timeline"),
        ("GET", "/agents/health"),
        ("GET", "/reviews/pending"),
        ("GET", "/export"),
        ("DELETE", "/data"),
    ],
)
def test_every_route_requires_auth(app_client, method, path):
    response = app_client.request(method, path, json={"records": []} if method == "POST" else None)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/timeline"),
        ("GET", "/agents/health"),
        ("GET", "/reviews/pending"),
        ("GET", "/export"),
    ],
)
def test_wrong_token_is_rejected(app_client, method, path):
    response = app_client.request(method, path, headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_no_token_configured_refuses_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_CLOUD_MIRROR_RECEIVER_TOKEN_UNSET_TEST", raising=False)
    app = receiver.create_app(
        db_path=tmp_path / "mirror.db",
        read_token_env_var="UA_CLOUD_MIRROR_RECEIVER_TOKEN_UNSET_TEST",
        ingest_token_env_var="UA_CLOUD_MIRROR_INGEST_TOKEN_UNSET_TEST",
    )
    with TestClient(app) as client:
        response = client.get("/timeline", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 401


def test_ingest_and_operator_tokens_have_separate_authority(app_client):
    assert app_client.post("/ingest", json={"records": []}, headers=_auth()).status_code == 401
    assert app_client.get("/timeline", headers=_ingest_auth()).status_code == 401
    assert app_client.get("/export", headers=_ingest_auth()).status_code == 401
    assert app_client.request("DELETE", "/data", headers=_ingest_auth()).status_code == 401


def test_identical_ingest_and_operator_tokens_fail_closed(app_client, monkeypatch):
    monkeypatch.setenv(INGEST_TOKEN_VAR, "s3cr3t-receiver-token")
    assert app_client.get("/timeline", headers=_auth()).status_code == 401
    assert app_client.post("/ingest", json={"records": []}, headers=_auth()).status_code == 401


def test_authentication_failures_are_rate_limited(app_client):
    for _ in range(receiver.MAX_FAILED_AUTH_PER_WINDOW):
        response = app_client.get("/timeline", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
    response = app_client.get("/timeline", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 429


# --------------------------------------------------------------------------
# Ingest: dedupe + schema validation
# --------------------------------------------------------------------------


def test_ingest_then_timeline_roundtrip(app_client):
    response = _post_records(app_client, [_record()])
    assert response.status_code == 200
    assert response.json() == {"ingested": 1, "duplicates": 0}

    timeline = app_client.get("/timeline", headers=_auth())
    assert timeline.status_code == 200
    records = timeline.json()["records"]
    assert len(records) == 1
    assert records[0]["event_id"] == _ulid_id("e1")


def test_database_work_does_not_block_asgi_event_loop(tmp_path, monkeypatch):
    """SQLite handlers run in Starlette's pool; ingest explicitly offloads its write."""
    monkeypatch.setenv(TOKEN_VAR, "s3cr3t-receiver-token")
    monkeypatch.setenv(INGEST_TOKEN_VAR, "s3cr3t-ingest-token")
    app = receiver.create_app(
        db_path=tmp_path / "mirror.db",
        read_token_env_var=TOKEN_VAR,
        ingest_token_env_var=INGEST_TOKEN_VAR,
    )
    original_connect = receiver._connect
    loop_thread_id: int | None = None
    database_thread_ids: list[int] = []

    def checked_connect(db_path):
        thread_id = threading.get_ident()
        database_thread_ids.append(thread_id)
        assert thread_id != loop_thread_id
        return original_connect(db_path)

    monkeypatch.setattr(receiver, "_connect", checked_connect)

    async def exercise_routes():
        nonlocal loop_thread_id
        loop_thread_id = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            posted = await client.post(
                "/ingest", json={"records": [_record()]}, headers=_ingest_auth()
            )
            assert posted.status_code == 200
            for path in ("/timeline", "/agents/health", "/reviews/pending", "/export"):
                response = await client.get(path, headers=_auth())
                assert response.status_code == 200
            deleted = await client.delete("/data", headers=_auth())
            assert deleted.status_code == 200

    anyio.run(exercise_routes)
    assert len(database_thread_ids) == 6
    assert all(thread_id != loop_thread_id for thread_id in database_thread_ids)


def test_main_binds_receiver_to_container_interface(tmp_path, monkeypatch):
    invocation = {}
    monkeypatch.setattr(
        receiver,
        "_resolve_database_path",
        lambda configured, data_home: tmp_path / "mirror.db",
    )
    monkeypatch.setattr(receiver, "create_app", lambda **kwargs: "asgi-app")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.delenv("UA_CLOUD_MIRROR_RECEIVER_HOST", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: invocation.update(app=app, **kwargs)),
    )

    assert receiver.main() == 0
    assert invocation == {"app": "asgi-app", "host": "0.0.0.0", "port": 9090}


def test_main_can_bind_receiver_to_loopback_for_iap_ssh_forward(tmp_path, monkeypatch):
    invocation = {}
    monkeypatch.setattr(
        receiver,
        "_resolve_database_path",
        lambda configured, data_home: tmp_path / "mirror.db",
    )
    monkeypatch.setattr(receiver, "create_app", lambda **kwargs: "asgi-app")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("UA_CLOUD_MIRROR_RECEIVER_HOST", "127.0.0.1")
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: invocation.update(app=app, **kwargs)),
    )

    assert receiver.main() == 0
    assert invocation == {"app": "asgi-app", "host": "127.0.0.1", "port": 8080}


def test_ingest_accepts_storage_now_utc_timestamp_format(app_client):
    record = _record(timestamp="2026-09-23T18:32:31+00:00")
    response = _post_records(app_client, [record])

    assert response.status_code == 200
    assert response.json() == {"ingested": 1, "duplicates": 0}


def test_ingest_accepts_spaced_agent_label(app_client):
    response = _post_records(app_client, [_record(agent="Laptop Agent1")])

    assert response.status_code == 200
    assert app_client.get("/agents/health", headers=_auth()).json()["agents"] == [
        {
            "agent": "Laptop Agent1",
            "last_seen": "2026-09-23T00:00:00+00:00",
            "last_channel": "agent.task.updated",
        }
    ]


def test_ingest_normalizes_offsets_before_agent_health_ordering(app_client):
    records = [
        _record(
            event_id="e1",
            agent="Laptop Agent1",
            timestamp="2026-09-23T00:30:00+01:00",
        ),
        _record(
            event_id="e2",
            channel="agent.approval.requested",
            kind="approval",
            agent="Laptop Agent1",
            status="pending",
            timestamp="2026-09-23T00:00:00Z",
        ),
    ]
    response = _post_records(app_client, records)

    assert response.status_code == 200
    assert response.json()["ingested"] == 2
    health = app_client.get("/agents/health", headers=_auth()).json()["agents"][0]
    assert health["last_seen"] == "2026-09-23T00:00:00+00:00"
    assert health["last_channel"] == "agent.approval.requested"
    timeline = app_client.get("/timeline", headers=_auth()).json()["records"]
    assert timeline[0]["timestamp"] == "2026-09-22T23:30:00+00:00"


def test_ingest_dedupes_by_event_id(app_client):
    _post_records(app_client, [_record()])
    response = _post_records(app_client, [_record()])
    assert response.json() == {"ingested": 0, "duplicates": 1}

    timeline = app_client.get("/timeline", headers=_auth())
    assert len(timeline.json()["records"]) == 1


def test_ingest_rejects_unknown_fields(app_client):
    bad = _record()
    bad["notes"] = "this should never be accepted"
    response = _post_records(app_client, [bad])
    assert response.status_code == 422
    assert "unknown fields" in response.json()["error"]

    # Nothing was persisted from the batch.
    timeline = app_client.get("/timeline", headers=_auth())
    assert timeline.json()["records"] == []


def test_ingest_rejects_unsupported_schema_version(app_client):
    bad = _record(schema_version=999)
    response = _post_records(app_client, [bad])
    assert response.status_code == 422
    assert "schema_version" in response.json()["error"]


def test_ingest_rejects_missing_required_field(app_client):
    bad = _record()
    del bad["timestamp"]
    response = _post_records(app_client, [bad])
    assert response.status_code == 422
    assert "missing required fields" in response.json()["error"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"channel": "agent.message.sent"},
        {"kind": "build"},
        {"status": "arbitrary operator text"},
        {"agent": "forged/identity"},
        {"event_id": "not-a-ulid"},
    ],
)
def test_ingest_rejects_forged_metadata_outside_projected_schema(app_client, overrides):
    response = _post_records(app_client, [_record(**overrides)])
    assert response.status_code == 422

    timeline = app_client.get("/timeline", headers=_auth())
    assert timeline.json()["records"] == []


def test_ingest_rejects_non_object_body(app_client):
    response = app_client.post("/ingest", content=b"not json", headers=_ingest_auth())
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Timeline pagination
# --------------------------------------------------------------------------


def test_timeline_pagination(app_client):
    records = [_record(event_id=f"e{i:02d}") for i in range(5)]
    _post_records(app_client, records)

    page1 = app_client.get("/timeline", params={"limit": 2}, headers=_auth()).json()
    assert [r["event_id"] for r in page1["records"]] == [_ulid_id("e00"), _ulid_id("e01")]
    assert page1["next_cursor"] == _ulid_id("e01")

    page2 = app_client.get(
        "/timeline", params={"limit": 2, "after": page1["next_cursor"]}, headers=_auth()
    ).json()
    assert [r["event_id"] for r in page2["records"]] == [_ulid_id("e02"), _ulid_id("e03")]


# --------------------------------------------------------------------------
# agents/health, reviews/pending
# --------------------------------------------------------------------------


def test_agents_health_reports_last_seen_per_agent(app_client):
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(event_id="e1", agent="Desktop_Agent1", timestamp="2026-09-23T00:00:00Z"),
                _record(event_id="e2", agent="Desktop_Agent1", timestamp="2026-09-23T00:05:00Z"),
                _record(event_id="e3", agent="Desktop_Agent2", timestamp="2026-09-23T00:01:00Z"),
            ]
        },
        headers=_ingest_auth(),
    )
    health = app_client.get("/agents/health", headers=_auth()).json()["agents"]
    by_agent = {a["agent"]: a for a in health}
    assert by_agent["Desktop_Agent1"]["last_seen"] == "2026-09-23T00:05:00+00:00"
    assert by_agent["Desktop_Agent2"]["last_seen"] == "2026-09-23T00:01:00+00:00"


def test_reviews_pending_is_state_level_not_event_level(app_client):
    """A still-outstanding elicitation is pending; an unrelated task record with no status is not."""
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(
                    event_id="e1",
                    channel="agent.approval.elicitation",
                    kind="approval",
                    task_id="90",
                    status="requested",
                ),
                _record(
                    event_id="e2",
                    channel="agent.task.updated",
                    kind="task",
                    task_id="91",
                    status=None,
                ),
            ]
        },
        headers=_ingest_auth(),
    )
    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert [r["event_id"] for r in pending] == [_ulid_id("e1")]


def test_reviews_pending_approval_requested_then_approved_is_not_pending(app_client):
    """The whole point of the latest-per-entity grouping: a resolved approval must not linger."""
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(
                    event_id="e01",
                    channel="agent.approval.elicitation",
                    kind="approval",
                    task_id="5",
                    status="requested",
                    timestamp="2026-09-23T00:00:00Z",
                ),
                _record(
                    event_id="e02",
                    channel="agent.approval.requested",
                    kind="approval",
                    task_id="5",
                    status="approved",
                    timestamp="2026-09-23T00:01:00Z",
                ),
            ]
        },
        headers=_ingest_auth(),
    )
    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert pending == []


@pytest.mark.parametrize(
    "elicitation_status",
    [
        "requested",
        "challenge_issued",
        "challenge_rejected",
        "challenge_consumed",
        "cancel",
        "decline",
    ],
)
def test_reviews_pending_keeps_elicitation_progress_for_pending_approval(
    app_client, elicitation_status
):
    response = _post_records(
        app_client,
        [
            _record(
                event_id="e01",
                channel="agent.approval.requested",
                kind="approval",
                task_id="5",
                status="pending",
            ),
            _record(
                event_id="e02",
                channel="agent.approval.elicitation",
                kind="approval",
                task_id="5",
                status=elicitation_status,
                timestamp="2026-09-23T00:01:00Z",
            ),
        ],
    )
    assert response.status_code == 200

    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert [record["event_id"] for record in pending] == [_ulid_id("e02")]


def test_receiver_database_paths_are_private_and_confined(tmp_path):
    root = tmp_path / "private-root"
    resolved = receiver._resolve_database_path("nested/receiver.db", root)
    assert resolved == root / "nested" / "receiver.db"
    with pytest.raises(ValueError, match="below UA_HOME"):
        receiver._resolve_database_path(str(tmp_path / "outside.db"), root)

    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="below UA_HOME"):
        receiver._resolve_database_path("linked/receiver.db", root)


def test_receiver_database_creation_uses_private_modes(tmp_path):
    tmp_path.chmod(0o755)
    existing_parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    db_path = tmp_path / "private" / "nested" / "receiver.db"
    conn = receiver._connect(db_path)
    conn.close()

    assert stat.S_IMODE(tmp_path.stat().st_mode) == existing_parent_mode
    assert stat.S_IMODE(db_path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_receiver_schema_is_initialized_once_when_app_is_built(tmp_path, monkeypatch):
    calls = 0
    original_initialize = receiver._initialize_database

    def counted_initialize(db_path):
        nonlocal calls
        calls += 1
        original_initialize(db_path)

    monkeypatch.setattr(receiver, "_initialize_database", counted_initialize)
    monkeypatch.setenv(TOKEN_VAR, "s3cr3t-receiver-token")
    monkeypatch.setenv(INGEST_TOKEN_VAR, "s3cr3t-ingest-token")
    app = receiver.create_app(
        db_path=tmp_path / "mirror.db",
        read_token_env_var=TOKEN_VAR,
        ingest_token_env_var=INGEST_TOKEN_VAR,
    )

    with TestClient(app) as client:
        assert client.get("/timeline", headers=_auth()).status_code == 200
        assert client.get("/export", headers=_auth()).status_code == 200
    assert calls == 1


def test_reviews_pending_task_doing_to_waiting_review_is_pending(app_client):
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(
                    event_id="e01",
                    channel="agent.task.updated",
                    kind="task",
                    task_id="7",
                    status="doing",
                    timestamp="2026-09-23T00:00:00Z",
                ),
                _record(
                    event_id="e02",
                    channel="agent.task.updated",
                    kind="task",
                    task_id="7",
                    status="waiting_review",
                    timestamp="2026-09-23T00:01:00Z",
                ),
            ]
        },
        headers=_ingest_auth(),
    )
    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert [r["event_id"] for r in pending] == [_ulid_id("e02")]


def test_reviews_pending_task_waiting_review_to_done_is_not_pending(app_client):
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(
                    event_id="e01",
                    channel="agent.task.updated",
                    kind="task",
                    task_id="8",
                    status="waiting_review",
                    timestamp="2026-09-23T00:00:00Z",
                ),
                _record(
                    event_id="e02",
                    channel="agent.task.updated",
                    kind="task",
                    task_id="8",
                    status="done",
                    timestamp="2026-09-23T00:01:00Z",
                ),
            ]
        },
        headers=_ingest_auth(),
    )
    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert pending == []


def test_reviews_pending_approval_kind_pending_status_is_pending(app_client):
    """kind == "approval" (agent.approval.requested, DB-backed status) also participates."""
    app_client.post(
        "/ingest",
        json={
            "records": [
                _record(
                    event_id="e1",
                    channel="agent.approval.requested",
                    kind="approval",
                    task_id="12",
                    status="pending",
                ),
            ]
        },
        headers=_ingest_auth(),
    )
    pending = app_client.get("/reviews/pending", headers=_auth()).json()["pending"]
    assert [r["event_id"] for r in pending] == [_ulid_id("e1")]


# --------------------------------------------------------------------------
# export + delete
# --------------------------------------------------------------------------


def test_export_returns_full_dump(app_client):
    app_client.post(
        "/ingest", json={"records": [_record("e1"), _record("e2")]}, headers=_ingest_auth()
    )
    export = app_client.get("/export", headers=_auth()).json()
    assert export["schema_version"] == 1
    assert {r["event_id"] for r in export["records"]} == {_ulid_id("e1"), _ulid_id("e2")}


def test_delete_wipes_all_data(app_client):
    app_client.post(
        "/ingest", json={"records": [_record("e1"), _record("e2")]}, headers=_ingest_auth()
    )
    delete_response = app_client.request("DELETE", "/data", headers=_auth())
    assert delete_response.json() == {"deleted": 2}

    export = app_client.get("/export", headers=_auth()).json()
    assert export["records"] == []
