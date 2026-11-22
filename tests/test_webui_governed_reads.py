"""Governed read admission for the raw event-log HTTP reads (task #1684).

``/api/ledger/rows`` and ``/api/events/stream`` read the event log directly
rather than through the registered ``event_log_query`` tool, so they used to
skip the gates that tool call passes: the agent-access off switch, policy
rules and per-caller rate limits. These tests pin that both routes now admit a
read through those gates before any row is read or any stream slot is taken,
that the operator keeps the human-recovery exemption from the off switch (and
only from it), and that an admitted read keeps its cursor and replay behavior.

The SSE route is consumed through a finite adapter or a finite sentinel
generator: an SSE body never completes, so a plain HTTP read would hang.
"""

from __future__ import annotations

import json

import anyio
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import events, gateway, server, storage, webui
from unified_agent.webui import routes, sse

AGENT = "Desktop_Agent2"
OPERATOR = "Desktop_Person"
TOKENS = {AGENT: "fixture-agent-token", OPERATOR: "fixture-operator-token"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_UI_STREAM_POLL_S", "0.02")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    for name in (
        "UA_AGENT_ACCESS_ENABLED",
        "UA_AGENT_ACCESS_READ_ONLY",
        "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        "UA_MANAGED_SECRETS",
        "UA_APPROVAL_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    gateway.reset_limits()
    routes._CAPACITY_DENIALS.clear()
    yield tmp_path
    gateway.reset_limits()
    routes._CAPACITY_DENIALS.clear()


def _policy(tmp_path, text: str) -> None:
    (tmp_path / "policy.yaml").write_text(text, encoding="utf-8")


DENY_POLICY = """
rules:
  - name: deny_event_reads
    type: tool
    tool: event_log_query
    action: deny
"""

RATE_POLICY = """
rules:
  - name: event_read_cap
    type: rate_limit
    tool: event_log_query
    max_calls: 1
    window_seconds: 600
"""

ALLOW_POLICY = """
rules:
  - name: allow_event_reads
    type: tool
    tool: event_log_query
    action: allow
  - name: event_read_budget
    type: rate_limit
    tool: event_log_query
    max_calls: 50
    window_seconds: 600
"""


def _headers(identity: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[identity]}


def _client(app=None) -> TestClient:
    return TestClient(
        server.HubAuthASGIMiddleware(app or Starlette(routes=webui.web_routes())),
        base_url="http://127.0.0.1",
    )


@pytest.fixture
def stream_sentinel(monkeypatch):
    """Replace the live generator with a finite one and record each construction."""
    constructed: list[dict] = []

    def finite_stream(*args, **kwargs):
        constructed.append(kwargs)

        async def empty_stream():
            if False:
                yield {}

        return empty_stream()

    monkeypatch.setattr(sse, "event_stream", finite_stream)
    return constructed


def _direct_request(path: str, identity: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"127.0.0.1")],
            "query_string": query,
            "ua_identity": identity,
            "ua_identity_via": "bearer",
        }
    )


# --- agent-access off switch -----------------------------------------------------------


def test_disabled_access_refuses_ledger_rows_at_the_route(monkeypatch):
    """The route itself enforces the switch, not only the outer middleware."""
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})

    async def scenario():
        return await routes.ledger_rows(_direct_request("/api/ledger/rows", AGENT))

    response = anyio.run(scenario)
    assert response.status_code == 403
    assert b"agent access is disabled" in response.body
    assert b"fixture-private" not in response.body


def test_disabled_access_refuses_the_stream_at_the_route(monkeypatch, stream_sentinel):
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")

    async def scenario():
        return await routes.events_stream(_direct_request("/api/events/stream", AGENT))

    response = anyio.run(scenario)
    assert response.status_code == 403
    assert b"agent access is disabled" in response.body
    assert stream_sentinel == []
    assert sse.active_streams() == 0


@pytest.mark.parametrize("path", ["/api/ledger/rows", "/api/events/stream", "/api/events"])
def test_disabled_access_refuses_an_agent_over_http(monkeypatch, stream_sentinel, path):
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _client() as client:
        response = client.get(path, headers=_headers(AGENT))
    assert response.status_code == 403
    assert "fixture-private" not in response.text
    assert stream_sentinel == []


def test_operator_recovery_survives_the_off_switch(monkeypatch, _isolated):
    """The operator's own off switch must not blind the operator's recovery reads."""
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    first = events.record_event("agent.task.updated", payload={"n": 1})
    events.record_event("agent.task.updated", payload={"n": 2})
    with _client() as client:
        ledger = client.get("/api/ledger/rows?after_seq=0", headers=_headers(OPERATOR))
    assert ledger.status_code == 200, ledger.text
    assert [json.loads(r["payload_raw"]) for r in ledger.json()["rows"]] == [{"n": 1}, {"n": 2}]

    frames = _consume_stream(f"since={first}&channels=agent.task.*", OPERATOR, count=2)
    assert json.loads(frames[1]["data"])["payload"] == {"n": 2}


def test_operator_recovery_needs_an_authenticated_channel(monkeypatch):
    """A scope naming the operator without an auth channel gets no exemption."""
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    request = _direct_request("/api/ledger/rows", OPERATOR)
    request.scope["ua_identity_via"] = None

    async def scenario():
        return await routes.ledger_rows(request)

    assert anyio.run(scenario).status_code == 403


# --- /api/events: the registered event_log_query (task #1811) -------------------------


def _events_route(request: Request):
    async def scenario():
        return await routes._read_only(routes._events)(request)

    return anyio.run(scenario)


def test_operator_reads_api_events_with_agent_access_off(monkeypatch):
    """The third event-log read keeps the operator's recovery exemption too."""
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    events.record_event("agent.task.updated", payload={"n": 1})
    events.record_event("agent.task.updated", payload={"n": 2})
    with _client() as client:
        response = client.get(
            "/api/events?after_seq=0&channels=agent.task.*", headers=_headers(OPERATOR)
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body
    assert [row["payload"] for row in body["events"]] == [{"n": 1}, {"n": 2}]


def test_api_events_filters_by_item_id():
    """The session transcript loads its own item's events (task #1992)."""
    events.record_event("agent.node", payload={"item_id": "nd-a", "n": 1})
    events.record_event("agent.node", payload={"item_id": "nd-b", "n": 2})
    events.record_event("agent.node", payload={"item_id": "nd-a", "n": 3})
    with _client() as client:
        response = client.get(
            "/api/events?after_seq=0&channels=agent.node&item_id=nd-a",
            headers=_headers(OPERATOR),
        )
    assert response.status_code == 200, response.text
    assert [row["payload"]["n"] for row in response.json()["events"]] == [1, 3]


def test_api_events_refuses_an_agent_with_a_status_not_a_200(monkeypatch):
    """The route itself answers 403, not an error inside a 200."""
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    response = _events_route(_direct_request("/api/events", AGENT, b"after_seq=0"))
    assert response.status_code == 403
    assert b"agent access is disabled" in response.body
    assert b"fixture-private" not in response.body


def test_api_events_operator_recovery_needs_an_authenticated_channel(monkeypatch):
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    request = _direct_request("/api/events", OPERATOR, b"after_seq=0")
    request.scope["ua_identity_via"] = None
    assert _events_route(request).status_code == 403


@pytest.mark.parametrize("identity", [AGENT, OPERATOR])
def test_policy_deny_refuses_api_events_with_403(_isolated, identity):
    """Policy is a tool gate: the operator's recovery exemption does not skip it."""
    _policy(_isolated, DENY_POLICY)
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _client() as client:
        response = client.get("/api/events?after_seq=0", headers=_headers(identity))
    assert response.status_code == 403
    assert "deny_event_reads" in response.json()["error"]
    assert "events" not in response.json()
    assert "fixture-private" not in response.text


def test_exhausted_read_rate_refuses_api_events_with_429(_isolated):
    _policy(_isolated, RATE_POLICY)
    with _client() as client:
        assert client.get("/api/events", headers=_headers(AGENT)).status_code == 200
        second = client.get("/api/events", headers=_headers(AGENT))
    assert second.status_code == 429
    assert "events" not in second.json()


def test_approval_gated_tool_parks_api_events_until_approved(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    url = "/api/events?after_seq=0&limit=10"
    with _client() as client:
        parked = client.get(url, headers=_headers(AGENT))
        assert parked.status_code == 202
        assert parked.json()["status"] == "pending"
        assert "fixture-private" not in parked.text
        _approve(parked.json()["approval_id"])
        admitted = client.get(url, headers=_headers(AGENT))
        assert admitted.status_code == 200
        assert admitted.json()["count"] >= 1
        assert client.get(url, headers=_headers(AGENT)).status_code == 202
    # A pass for the ledger route's equal arguments is the same request.
    from unified_agent.tools.approvals import approval_pending

    [pending] = approval_pending()["pending"]
    assert pending["payload"] == {"after_seq": 0, "limit": 10}


def test_failed_api_events_read_is_a_500_not_a_200(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("fixture read failure")

    monkeypatch.setattr(events, "query_events", broken)
    with _client() as client:
        response = client.get("/api/events", headers=_headers(AGENT))
    assert response.status_code == 500
    assert "fixture read failure" in response.json()["error"]


# --- policy deny -----------------------------------------------------------------------


@pytest.mark.parametrize("identity", [AGENT, OPERATOR])
def test_policy_deny_refuses_ledger_rows(_isolated, identity):
    """Policy is a tool gate: the operator's recovery exemption does not skip it."""
    _policy(_isolated, DENY_POLICY)
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _client() as client:
        response = client.get("/api/ledger/rows?after_seq=0", headers=_headers(identity))
    assert response.status_code == 403
    assert "deny_event_reads" in response.json()["error"]
    assert "rows" not in response.json()
    assert "fixture-private" not in response.text


@pytest.mark.parametrize("identity", [AGENT, OPERATOR])
@pytest.mark.parametrize("query", ["", "since=01J8ZQ4T7X9K2M5N7P8R0S3T6W", "after_seq=0"])
def test_policy_deny_refuses_the_stream(_isolated, stream_sentinel, identity, query):
    _policy(_isolated, DENY_POLICY)
    with _client() as client:
        response = client.get("/api/events/stream?" + query, headers=_headers(identity))
    assert response.status_code == 403
    assert "deny_event_reads" in response.json()["error"]
    assert stream_sentinel == []
    assert sse.active_streams() == 0


# --- read rate limits ------------------------------------------------------------------


def test_exhausted_read_rate_refuses_ledger_rows(_isolated):
    _policy(_isolated, RATE_POLICY)
    events.record_event("agent.task.updated", payload={"n": 1})
    with _client() as client:
        first = client.get("/api/ledger/rows?after_seq=0", headers=_headers(AGENT))
        second = client.get("/api/ledger/rows?after_seq=0", headers=_headers(AGENT))
        other = client.get("/api/ledger/rows?after_seq=0", headers=_headers(OPERATOR))
    assert first.status_code == 200
    assert first.json()["count"] == 1
    assert second.status_code == 429
    assert "rate limited" in second.json()["error"]
    assert "rows" not in second.json()
    # The budget is per caller: another identity still has its own slot.
    assert other.status_code == 200


def test_exhausted_read_rate_refuses_the_stream(_isolated, stream_sentinel):
    _policy(_isolated, RATE_POLICY)
    with _client() as client:
        first = client.get("/api/events/stream", headers=_headers(AGENT))
        second = client.get("/api/events/stream?after_seq=0", headers=_headers(AGENT))
    assert first.status_code == 200
    assert second.status_code == 429
    assert "rate limited" in second.json()["error"]
    assert len(stream_sentinel) == 1
    assert sse.active_streams() == 0


def test_ledger_and_stream_share_one_event_log_read_budget(_isolated, stream_sentinel):
    """Both raw reads are event_log_query reads, so one budget covers them."""
    _policy(_isolated, RATE_POLICY)
    with _client() as client:
        assert client.get("/api/ledger/rows", headers=_headers(AGENT)).status_code == 200
        assert client.get("/api/events/stream", headers=_headers(AGENT)).status_code == 429
    assert stream_sentinel == []


# --- an admitted read keeps cursor and replay behavior ---------------------------------


def _consume_stream(query: str, identity: str, *, count: int) -> list[dict]:
    """Open the real route behind real auth and read ``count`` frames finitely."""

    async def consume(request):
        response = await routes.events_stream(request)
        if response.status_code != 200:
            return response
        gen = response.body_iterator
        frames = []
        try:
            with anyio.fail_after(5):
                while len(frames) < count:
                    frames.append(await gen.__anext__())
        finally:
            await gen.aclose()
        return JSONResponse({"frames": frames})

    app = Starlette(routes=[Route("/api/events/stream", consume)])
    with _client(app) as client:
        response = client.get("/api/events/stream?" + query, headers=_headers(identity))
    assert response.status_code == 200, response.text
    assert sse.active_streams() == 0
    return response.json()["frames"]


def test_admitted_ledger_read_keeps_after_seq_paging(_isolated):
    _policy(_isolated, ALLOW_POLICY)
    for n in range(3):
        events.record_event("agent.task.updated", payload={"n": n})
    with _client() as client:
        page = client.get("/api/ledger/rows?after_seq=0&limit=2", headers=_headers(AGENT)).json()
        assert [json.loads(r["payload_raw"]) for r in page["rows"]] == [{"n": 0}, {"n": 1}]
        rest = client.get(
            f"/api/ledger/rows?after_seq={page['next_after_seq']}&limit=2",
            headers=_headers(AGENT),
        ).json()
    assert [json.loads(r["payload_raw"]) for r in rest["rows"]] == [{"n": 2}]
    assert rest["next_after_seq"] == rest["rows"][-1]["seq"]


def test_admitted_stream_keeps_since_and_after_seq_replay(_isolated):
    _policy(_isolated, ALLOW_POLICY)
    first = events.record_event("agent.task.updated", payload={"n": 1})
    events.record_event("agent.approval.requested", payload={"n": "filtered"})
    events.record_event("agent.task.updated", payload={"n": 2})

    by_ulid = _consume_stream(f"since={first}&channels=agent.task.*", AGENT, count=2)
    assert json.loads(by_ulid[0]["data"])["cursor"] == first
    assert json.loads(by_ulid[1]["data"])["payload"] == {"n": 2}
    replayed_id = by_ulid[1]["id"]

    by_seq = _consume_stream("after_seq=0", AGENT, count=4)
    assert json.loads(by_seq[0]["data"])["seq"] == 0
    assert [json.loads(f["data"])["payload"] for f in by_seq[1:]] == [
        {"n": 1},
        {"n": "filtered"},
        {"n": 2},
    ]
    assert by_seq[3]["id"] == replayed_id


def test_policy_approval_rule_parks_the_read_without_rows(_isolated, stream_sentinel):
    """A require_approval rule parks the read (202) instead of serving it."""
    _policy(
        _isolated,
        "rules:\n  - name: approve_event_reads\n    type: tool\n"
        "    tool: event_log_query\n    action: require_approval\n",
    )
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _client() as client:
        ledger = client.get("/api/ledger/rows", headers=_headers(AGENT))
        stream = client.get("/api/events/stream", headers=_headers(AGENT))
    for response in (ledger, stream):
        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert "fixture-private" not in response.text
    assert stream_sentinel == []


# --- review follow-ups (PR #981) -------------------------------------------------------


def _decisions(caller: str | None = None) -> list[dict]:
    rows = [
        event["payload"]
        for event in events.query_events(channels=[gateway.CHANNEL_GATEWAY_DECISION])
        if event["payload"].get("tool_name") == "event_log_query"
    ]
    return [row for row in rows if caller is None or row.get("caller") == caller]


def _approve(approval_id: int) -> None:
    from unified_agent import config
    from unified_agent.tools.approvals import approval_resolve

    token = config.agent_identity_var.set(OPERATOR)
    try:
        approval_resolve(int(approval_id), "approve")
    finally:
        config.agent_identity_var.reset(token)


def test_approval_gated_tool_parks_ledger_rows_until_approved(monkeypatch):
    """UA_APPROVAL_TOOLS gates the HTTP read; one approval admits one read."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    url = "/api/ledger/rows?after_seq=0&limit=10"
    with _client() as client:
        parked = client.get(url, headers=_headers(AGENT))
        assert parked.status_code == 202
        assert parked.json()["status"] == "pending"
        assert "fixture-private" not in parked.text
        _approve(parked.json()["approval_id"])
        admitted = client.get(url, headers=_headers(AGENT))
        assert admitted.status_code == 200
        assert admitted.json()["count"] >= 1
        # The pass was one-shot: the next identical read parks again.
        assert client.get(url, headers=_headers(AGENT)).status_code == 202


def test_approval_gated_tool_parks_the_stream_before_it_opens(monkeypatch, stream_sentinel):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    with _client() as client:
        parked = client.get("/api/events/stream?after_seq=0", headers=_headers(AGENT))
        assert parked.status_code == 202
        assert stream_sentinel == []
        _approve(parked.json()["approval_id"])
        admitted = client.get("/api/events/stream?after_seq=0", headers=_headers(AGENT))
    assert admitted.status_code == 200
    assert len(stream_sentinel) == 1
    assert {k: v for k, v in stream_sentinel[0].items() if k not in ("on_open", "slot")} == {
        "channels": None,
        "since": None,
        "after_seq": 0,
    }
    assert callable(stream_sentinel[0]["on_open"])
    assert sse.active_streams() == 0


def test_approval_pass_does_not_cover_a_different_stream_start(monkeypatch, stream_sentinel):
    """The cursor stays in the gate payload: a pass for one start is not a replay from another.

    Known cost (task #1811): under ``UA_APPROVAL_TOOLS`` a reconnect whose
    ``Last-Event-ID`` differs from the approved cursor parks a new approval.
    """
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    with _client() as client:
        parked = client.get("/api/events/stream", headers=_headers(AGENT))
        _approve(parked.json()["approval_id"])
        assert client.get("/api/events/stream", headers=_headers(AGENT)).status_code == 200
        replay = client.get(
            "/api/events/stream", headers={**_headers(AGENT), "Last-Event-ID": "seq:0"}
        )
    assert replay.status_code == 202


def test_approval_pass_uses_the_tool_argument_shape(monkeypatch):
    """A pass requested with event_log_query's argument names admits the equal HTTP read."""
    from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    with pytest.raises(ApprovalPendingError) as pending:
        check_approval_gate("event_log_query", {"after_seq": 0, "limit": 10}, requested_by=AGENT)
    _approve(pending.value.approval_id)
    with _client() as client:
        response = client.get("/api/ledger/rows?after_seq=0&limit=10", headers=_headers(AGENT))
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/ledger/rows", "/api/events/stream"])
def test_rate_limit_capacity_is_throttling_not_forbidden(monkeypatch, stream_sentinel, path):
    from unified_agent import policy, safety

    def full(tool, payload=None, episode_id=None, originator=None):
        raise policy.PolicyViolation(
            "rate-limit buckets are full", reason_code="rate_limit_capacity"
        )

    monkeypatch.setattr(safety, "check_policy_gate", full)
    with _client() as client:
        response = client.get(path, headers=_headers(AGENT))
    assert response.status_code == 429
    assert "rows" not in response.json()
    assert stream_sentinel == []


def test_ledger_paging_to_the_tail_ends_on_an_empty_page():
    """Devin on #981: a read that appends to the log it pages must not feed itself."""
    for n in range(5):
        events.record_event("agent.task.updated", payload={"n": n})
    after_seq, pages = 0, 0
    with _client() as client:
        while True:
            pages += 1
            assert pages <= 10, "ledger paging never reached an empty page"
            body = client.get(
                f"/api/ledger/rows?after_seq={after_seq}&limit=2", headers=_headers(AGENT)
            ).json()
            if not body["rows"]:
                break
            after_seq = body["next_after_seq"]
        # Polling the tail again stays empty: an allowed read writes no row.
        again = client.get(f"/api/ledger/rows?after_seq={after_seq}", headers=_headers(AGENT))
    assert pages == 4
    assert again.json()["rows"] == []
    assert _decisions(AGENT) == []


def _stream_frames(
    query: str, *, count: int, identity: str = AGENT
) -> tuple[int, list[dict], str | None]:
    """Open the real route and generator; read up to ``count`` frames or an error."""

    async def consume(request):
        response = await routes.events_stream(request)
        if response.status_code != 200:
            return response
        gen = response.body_iterator
        frames: list[dict] = []
        error = None
        try:
            with anyio.fail_after(5):
                while len(frames) < count:
                    frames.append(await gen.__anext__())
        except StopAsyncIteration:
            pass
        except RuntimeError as exc:
            error = str(exc)
        finally:
            await gen.aclose()
        return JSONResponse({"frames": frames, "error": error})

    app = Starlette(routes=[Route("/api/events/stream", consume)])
    with _client(app) as client:
        response = client.get("/api/events/stream?" + query, headers=_headers(identity))
    body = response.json()
    return response.status_code, body.get("frames", []), body.get("error")


def test_opened_stream_records_one_allow_after_its_first_replay_query():
    events.record_event("agent.task.updated", payload={"n": 1})
    status, frames, error = _stream_frames("after_seq=0&channels=agent.task.*", count=2)
    assert (status, error) == (200, None)
    assert json.loads(frames[1]["data"])["payload"] == {"n": 1}
    assert [(d["decision"], d["reason_code"]) for d in _decisions(AGENT)] == [("allow", "allowed")]
    assert sse.active_streams() == 0


def test_stream_capacity_refusal_records_a_deny_not_an_allow(monkeypatch):
    monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")
    assert sse._acquire_stream()  # another stream already holds the only slot
    try:
        status, frames, error = _stream_frames("after_seq=0", count=2)
    finally:
        sse._release_stream()
    # Refused before a response exists: HTTP 429, not a 200 with an error frame.
    assert status == 429
    assert frames == []
    assert [(d["decision"], d["reason_code"]) for d in _decisions(AGENT)] == [
        ("deny", "stream_capacity")
    ]


def test_stream_replay_failure_records_an_error_not_an_allow(monkeypatch):
    def broken(channels, after_seq, limit):
        raise RuntimeError("fixture replay failure")

    monkeypatch.setattr(sse, "_query", broken)
    status, frames, error = _stream_frames("after_seq=0", count=2)
    assert status == 200
    assert [f["event"] for f in frames] == ["cursor"]
    assert error == "fixture replay failure"
    assert [(d["decision"], d["reason_code"]) for d in _decisions(AGENT)] == [
        ("error", "tool_error")
    ]
    assert sse.active_streams() == 0


def test_stream_closed_before_opening_records_no_allow():
    status, frames, error = _stream_frames("after_seq=0", count=1)
    assert [f["event"] for f in frames] == ["cursor"]
    assert [d["decision"] for d in _decisions(AGENT)] == ["error"]


@pytest.mark.parametrize("path", ["/api/ledger/rows", "/api/events/stream"])
def test_refused_read_keeps_only_the_gate_refusal_record(_isolated, stream_sentinel, path):
    _policy(_isolated, DENY_POLICY)
    with _client() as client:
        assert client.get(path, headers=_headers(AGENT)).status_code == 403
    decisions = _decisions(AGENT)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "deny"
    assert decisions[0]["rule"] == "deny_event_reads"


def test_rate_limited_ledger_read_records_only_the_refusal(_isolated):
    _policy(_isolated, RATE_POLICY)
    with _client() as client:
        assert client.get("/api/ledger/rows", headers=_headers(AGENT)).status_code == 200
        assert client.get("/api/ledger/rows", headers=_headers(AGENT)).status_code == 429
    assert [(d["decision"], d["reason_code"]) for d in _decisions(AGENT)] == [
        ("deny", "rate_limited")
    ]


def test_parked_read_records_one_pending_decision(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "event_log_query")
    with _client() as client:
        assert client.get("/api/ledger/rows", headers=_headers(AGENT)).status_code == 202
    assert [d["decision"] for d in _decisions(AGENT)] == ["pending"]


def test_failed_ledger_read_records_an_error_not_an_allow(monkeypatch):
    def broken(after_seq, limit):
        raise RuntimeError("fixture read failure")

    monkeypatch.setattr(events, "query_ledger_rows", broken)
    with _client() as client, pytest.raises(RuntimeError, match="fixture read failure"):
        client.get("/api/ledger/rows", headers=_headers(AGENT))
    assert [(d["decision"], d["reason_code"]) for d in _decisions(AGENT)] == [
        ("error", "tool_error")
    ]


# --- round 3 review follow-ups (PR #981) -----------------------------------------------


def _on_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def decision_threads(monkeypatch):
    """Record, per gateway decision write, whether it ran on the event loop."""
    calls: list[bool] = []
    real = gateway.record_call_result

    def spy(tool_name, invocation, error, result=None):
        calls.append(_on_event_loop())
        return real(tool_name, invocation, error, result)

    monkeypatch.setattr(gateway, "record_call_result", spy)
    return calls


def _hold_the_only_stream_slot(monkeypatch):
    monkeypatch.setenv("UA_UI_MAX_STREAMS", "1")
    assert sse._acquire_stream()


def test_stream_replay_failure_is_recorded_off_the_event_loop(monkeypatch, decision_threads):
    def broken(channels, after_seq, limit):
        raise RuntimeError("fixture replay failure")

    monkeypatch.setattr(sse, "_query", broken)
    _stream_frames("after_seq=0", count=2)
    assert decision_threads == [False]
    assert [d["decision"] for d in _decisions(AGENT)] == ["error"]


def test_stream_capacity_refusal_is_recorded_off_the_event_loop(monkeypatch, decision_threads):
    _hold_the_only_stream_slot(monkeypatch)
    try:
        _stream_frames("after_seq=0", count=2)
    finally:
        sse._release_stream()
    assert decision_threads == [False]
    assert [d["reason_code"] for d in _decisions(AGENT)] == ["stream_capacity"]


def test_failed_ledger_read_is_recorded_off_the_event_loop(monkeypatch, decision_threads):
    def broken(after_seq, limit):
        raise RuntimeError("fixture read failure")

    monkeypatch.setattr(events, "query_ledger_rows", broken)
    with _client() as client, pytest.raises(RuntimeError):
        client.get("/api/ledger/rows", headers=_headers(AGENT))
    assert decision_threads == [False]


def test_cancelled_stream_still_reports_its_failure_once(monkeypatch):
    """A cancellation before the stream opens must not drop its one report."""
    import threading

    reports: list[tuple[str, bool]] = []
    loop_thread: list[int] = []

    def on_open(error):
        reports.append((type(error).__name__, threading.get_ident() in loop_thread))

    async def never_opens(channels, after_seq, on_first_batch=None):
        await anyio.sleep(5)  # cancelled here, before the first batch
        yield {}

    monkeypatch.setattr(sse, "_event_source", never_opens)

    async def scenario():
        loop_thread.append(threading.get_ident())
        gen = sse.event_stream(after_seq=0, on_open=on_open)
        assert (await gen.__anext__())["event"] == "cursor"
        with anyio.move_on_after(0.05):
            await gen.__anext__()
        await gen.aclose()

    anyio.run(scenario)
    assert len(reports) == 1
    name, on_loop = reports[0]
    assert "Cancel" in name
    assert on_loop is False
    assert sse.active_streams() == 0


def test_capacity_refusal_is_a_429_that_asks_the_client_to_back_off(monkeypatch):
    _hold_the_only_stream_slot(monkeypatch)
    try:
        with _client() as client:
            response = client.get("/api/events/stream", headers=_headers(AGENT))
    finally:
        sse._release_stream()
    assert response.status_code == 429
    assert response.json() == {"error": "too many concurrent streams"}
    assert int(response.headers["retry-after"]) == sse.CAPACITY_RETRY_MS // 1000 >= 10


def test_repeated_capacity_refusals_are_coalesced_per_caller(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(routes._CAPACITY_DENIALS, "clock", lambda: clock[0])
    _hold_the_only_stream_slot(monkeypatch)
    try:
        for _ in range(5):
            _stream_frames("after_seq=0", count=1)
            clock[0] += 1
        assert len(_decisions(AGENT)) == 1
        # Another caller gets its own first denial.
        _stream_frames("after_seq=0", count=1, identity=OPERATOR)
        assert len(_decisions(OPERATOR)) == 1
        # After the window a new refusal is recorded again.
        clock[0] += routes._CAPACITY_DENIALS.window_s
        _stream_frames("after_seq=0", count=1)
    finally:
        sse._release_stream()
    assert [d["reason_code"] for d in _decisions(AGENT)] == ["stream_capacity"] * 2
    assert [d["reason_code"] for d in _decisions(OPERATOR)] == ["stream_capacity"]


def test_capacity_coalescing_is_bounded():
    denials = routes._CapacityDenials(window_s=60.0, max_callers=3)
    clock = [0.0]
    denials.clock = lambda: clock[0]
    for caller in ("a", "b", "c", "d"):
        assert denials.should_record(caller)
    assert len(denials._last) == 3
    assert denials.should_record("a")  # evicted as the oldest, so recorded again
    assert not denials.should_record("d")
