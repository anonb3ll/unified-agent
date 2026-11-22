"""`/api/approvals` reads its rows in one batch, not one connection per row (task #1786).

The hub ran at ~300% CPU while a Control Center tab polled `/api/approvals`
several times a second: each poll enriched every queue row through
`platform_http.read_object`, and each of those opened its own storage
connection (59 connects for a 56-row queue). These tests pin the batch path:
the connect count is independent of the row count, the payload equals the old
per-row path on the same fixtures (stale ids, gate denial, elided expired
passes, rows stamped for another tenant), and an unchanged poll can be answered
with 304.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ResourceError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    platform_http,
    platform_readers,
    profiles,
    registry,
    server,
    storage,
    webui,
)
from unified_agent.tools import approvals as approvals_tool
from unified_agent.webui import api as webui_api
from unified_agent.webui import routes as webui_routes

TOKEN = f"tok-{secrets.token_urlsafe(8)}"


def _seed(*, pending: int, live: int, expired: int, org_id: str | None = None) -> list[int]:
    """Insert approval rows directly; returns their ids in insert order."""
    storage.init_db()
    now = datetime.now(UTC)
    ids: list[int] = []
    conn = storage.connect()
    try:
        plan = (
            [("pending", None)] * pending
            + [("approved", (now - timedelta(hours=1)).isoformat())] * live
            + [("approved", (now - timedelta(days=3)).isoformat())] * expired
        )
        for index, (status, resolved_at) in enumerate(plan):
            payload = {
                "task_id": 500 + index,
                "task_spec": "s" * 600,
                "token": f"secret-{index}",
            }
            cur = conn.execute(
                "INSERT INTO approvals (tool, payload, requested_by, status, reason, "
                "created_at, updated_at, resolved_at, org_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "delegate",
                    approvals_tool.canonical_approval_payload(payload),
                    "Desktop_Agent2",
                    status,
                    "private reason",
                    now.isoformat(),
                    now.isoformat(),
                    resolved_at,
                    org_id,
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()
    return ids


def _count_connects(monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    real = storage.connect

    def counting(*args, **kwargs):
        # Background post-init hooks (backfills) connect on their own threads
        # and are single-flight, so their count depends on timing, not rows.
        if not threading.current_thread().name.startswith("ua-post-init-"):
            calls["n"] += 1
        return real(*args, **kwargs)

    # The registry heartbeat is throttled per label by wall clock, so whether a
    # call pays for one depends on which identity is ambient and when it last
    # wrote, not on the row count.
    monkeypatch.setattr(registry, "touch_agent", lambda *args, **kwargs: False)
    monkeypatch.setattr(storage, "connect", counting)
    return calls


async def _old_per_row_payload() -> dict:
    """The pre-#1786 implementation, kept here as the parity oracle."""
    result = await webui_api._safe(
        "approval_pending", default={"pending": [], "approved_unconsumed": []}
    )

    async def read_row(row):
        approval_id = row.get("id")
        if approval_id is None:
            return webui_api._approval_platform_row(row)
        try:
            platform_payload = await anyio.to_thread.run_sync(
                lambda: platform_http.read_object("approval", str(approval_id))
            )
        except Exception:  # noqa: BLE001 - mirrors the old fallback exactly
            return webui_api._approval_platform_row(row)
        approval = platform_payload.get("approval") if isinstance(platform_payload, dict) else None
        if not isinstance(approval, dict):
            return webui_api._approval_platform_row(row)
        return webui_api._approval_platform_row({**row, **approval})

    async def read_rows(rows):
        return [await read_row(row) for row in rows]

    return {
        "pending": await read_rows(result.get("pending", [])),
        "approved_unconsumed": await read_rows(result.get("approved_unconsumed", [])),
        "expired_passes": await read_rows(result.get("expired_passes", [])),
        "gated_tools": result.get("gated_tools", []),
        "pass_ttl_hours": result.get("pass_ttl_hours"),
    }


_NEW_KEYS = ("expired_passes_total", "expired_passes_truncated")


def _without_new_keys(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in _NEW_KEYS}


def _strip_volatile(payload: dict) -> dict:
    """Drop the one field that moves with the wall clock between two reads."""
    out = json.loads(json.dumps(payload, default=str))
    for bucket in ("pending", "approved_unconsumed", "expired_passes"):
        for row in out.get(bucket, []):
            row.pop("expires_in_hours", None)
    return out


def test_connect_count_is_independent_of_row_count(monkeypatch):
    _seed(pending=2, live=1, expired=1)
    calls = _count_connects(monkeypatch)
    small = anyio.run(webui_api.approvals_payload)
    small_connects = calls["n"]
    assert len(small["pending"]) == 2

    _seed(pending=30, live=15, expired=15)
    calls["n"] = 0
    large = anyio.run(webui_api.approvals_payload)
    large_connects = calls["n"]

    assert len(large["pending"]) == 32
    assert len(large["approved_unconsumed"]) == 16
    assert len(large["expired_passes"]) == 16
    # O(1): 64 rows cost exactly what 4 did, and far fewer connects than rows.
    assert large_connects == small_connects
    assert large_connects <= 6


def test_batch_reader_uses_one_connection_for_many_ids(monkeypatch):
    ids = _seed(pending=10, live=0, expired=0)
    calls = _count_connects(monkeypatch)
    rows = approvals_tool.get_approvals(ids)
    assert calls["n"] == 1
    assert sorted(rows) == sorted(ids)
    for approval_id in ids:
        assert rows[approval_id] == approvals_tool.get_approval(approval_id)


def test_payload_matches_old_per_row_path(monkeypatch):
    _seed(pending=3, live=2, expired=2)
    # Another tenant's row: both paths are tenant-fenced (task #1793), so the
    # batch path must drop it exactly as the single-row path does.
    other = _seed(pending=1, live=0, expired=0, org_id="org-other")

    old = anyio.run(_old_per_row_payload)
    new = anyio.run(webui_api.approvals_payload)

    assert _strip_volatile(_without_new_keys(new)) == _strip_volatile(old)
    assert new["expired_passes_total"] == 2
    assert new["expired_passes_truncated"] is False
    listed = {row["id"] for bucket in ("pending", "approved_unconsumed") for row in new[bucket]}
    assert not listed & set(other)
    for bucket in ("pending", "approved_unconsumed", "expired_passes"):
        for row in new[bucket]:
            assert "payload" not in row
            assert "reason" not in row
            assert "secret-" not in json.dumps(row)
            assert row["platform"]["kind"] == "approval"
            assert row["links"]["task"]["kind"] == "task"


def test_payload_matches_old_path_for_stale_and_malformed_rows(monkeypatch):
    ids = _seed(pending=2, live=0, expired=0)
    queue = {
        "pending": [
            {"id": ids[0], "tool": "delegate", "status": "pending", "payload": {"task_id": 1}},
            # Gone from the table since the queue read: keeps its queue form.
            {"id": 999_999, "tool": "run_cmd", "status": "pending", "payload": {"task_id": 2}},
            # Rejected by the declared id_pattern: keeps its queue form.
            {"id": "0042", "tool": "run_cmd", "status": "pending", "payload": {}},
            {"id": None, "tool": "run_cmd", "status": "pending", "payload": {"task_id": 3}},
        ],
        "approved_unconsumed": [],
        "expired_passes": [],
        "gated_tools": ["delegate"],
        "pass_ttl_hours": 24,
    }
    monkeypatch.setitem(server.REGISTERED_TOOLS, "approval_pending", lambda **kw: queue)

    old = anyio.run(_old_per_row_payload)
    new = anyio.run(webui_api.approvals_payload)

    assert _without_new_keys(new) == old
    # The live row was refreshed from storage; the queue row's task id lost.
    assert new["pending"][0]["links"]["task"]["id"] == "500"
    assert new["pending"][1]["links"]["task"]["id"] == "2"


def test_payload_matches_old_path_when_profile_denies_the_reader(monkeypatch):
    _seed(pending=2, live=1, expired=0)
    real_queue = approvals_tool.approval_pending()
    monkeypatch.setitem(server.REGISTERED_TOOLS, "approval_pending", lambda **kw: real_queue)
    real_allowed_for = profiles.tool_is_allowed_for

    def deny_reader(profile, tool_name):
        return tool_name != "approval_pending" and real_allowed_for(profile, tool_name)

    monkeypatch.setattr(profiles, "tool_is_allowed_for", deny_reader)

    with pytest.raises(ResourceError):
        platform_http.read_objects("approval", [str(real_queue["pending"][0]["id"])])

    old = anyio.run(_old_per_row_payload)
    new = anyio.run(webui_api.approvals_payload)
    assert _strip_volatile(_without_new_keys(new)) == _strip_volatile(old)


def test_reader_gate_matches_single_row_reader(monkeypatch):
    ids = _seed(pending=1, live=0, expired=0)
    monkeypatch.setattr(profiles, "tool_is_allowed", lambda name: name != "approval_pending")
    with pytest.raises(ResourceError) as single:
        platform_readers.read_approval(str(ids[0]))
    with pytest.raises(ResourceError) as batch:
        platform_readers.read_approvals([str(ids[0])])
    assert str(batch.value) == str(single.value)


def test_batch_reader_matches_single_reader_per_id():
    ids = _seed(pending=2, live=1, expired=0)
    wanted = [str(i) for i in ids] + ["999999", "abc", "0"]
    batch = platform_readers.read_approvals(wanted)
    assert sorted(batch) == sorted(str(i) for i in ids)
    for approval_id in ids:
        assert batch[str(approval_id)] == platform_readers.read_approval(str(approval_id))


def test_expired_passes_are_capped_to_the_newest(monkeypatch):
    monkeypatch.setattr(webui_api, "APPROVALS_EXPIRED_PASSES_CAP", 3)
    ids = _seed(pending=1, live=0, expired=5)
    payload = anyio.run(webui_api.approvals_payload)
    assert [row["id"] for row in payload["expired_passes"]] == ids[-3:]
    assert payload["expired_passes_total"] == 5
    assert payload["expired_passes_truncated"] is True
    assert len(payload["pending"]) == 1


def test_expired_cap_keeps_the_newest_beyond_the_tool_limit():
    # approval_pending bounds each bucket at its `limit` (default 100). With
    # more expired passes than that, slicing its oldest-first window could
    # neither reach the newest rows nor count them all (Devin, PR #973).
    ids = _seed(pending=1, live=0, expired=105)
    payload = anyio.run(webui_api.approvals_payload)
    cap = webui_api.APPROVALS_EXPIRED_PASSES_CAP
    assert [row["id"] for row in payload["expired_passes"]] == ids[-cap:]
    assert payload["expired_passes_total"] == 105
    assert payload["expired_passes_truncated"] is True


def test_approval_pending_default_expired_window_is_unchanged():
    ids = _seed(pending=0, live=0, expired=4)
    default = approvals_tool.approval_pending(limit=3)
    assert [row["id"] for row in default["expired_passes"]] == ids[:3]
    assert "expired_passes_total" not in default

    newest = approvals_tool.approval_pending(limit=3, newest_expired=True)
    assert [row["id"] for row in newest["expired_passes"]] == ids[-3:]
    assert newest["expired_passes_total"] == 4


def test_queue_failure_is_reported_not_swallowed(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setitem(server.REGISTERED_TOOLS, "approval_pending", broken)
    payload = anyio.run(webui_api.approvals_payload)
    assert payload["pending"] == []
    assert "store unavailable" in payload["error"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def test_unchanged_poll_returns_304(client):
    _seed(pending=2, live=0, expired=0)
    first = client.get("/api/approvals")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    assert "no-cache" in first.headers["cache-control"]

    again = client.get("/api/approvals", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag

    # A strong-form or listed validator matches too (weak comparison).
    strong = etag.removeprefix("W/")
    listed = client.get("/api/approvals", headers={"If-None-Match": f'"other", {strong}'})
    assert listed.status_code == 304


def test_changed_queue_returns_200_with_new_etag(client):
    ids = _seed(pending=2, live=0, expired=0)
    first = client.get("/api/approvals")
    etag = first.headers["etag"]

    approvals_tool.approval_resolve(id=ids[0], decision="deny", resolved_by="operator")
    after = client.get("/api/approvals", headers={"If-None-Match": etag})
    assert after.status_code == 200
    assert after.headers["etag"] != etag
    assert [row["id"] for row in after.json()["pending"]] == [ids[1]]


def test_stale_validator_returns_full_body(client):
    _seed(pending=1, live=0, expired=0)
    res = client.get("/api/approvals", headers={"If-None-Match": 'W/"stale"'})
    assert res.status_code == 200
    assert len(res.json()["pending"]) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ("*", True),
        ('W/"abc"', True),
        ('"abc"', True),
        ('"x", W/"abc"', True),
        ('"abcd"', False),
    ],
)
def test_etag_matching(header, expected):
    assert webui_routes._etag_matches(header, 'W/"abc"') is expected
