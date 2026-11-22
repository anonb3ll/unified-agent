"""J7 broker boundary: only a device-bound OAuth caller can spend a hub key."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, llm_broker, run_records, storage, usage_ledger
from unified_agent.server import HubAuthASGIMiddleware
from unified_agent.tools.tasks import task_add, task_claim, task_update


@pytest.fixture
def real_event_writer():
    return llm_broker.llm_spend.events.record_event


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("UA_LLM_BROKER_ENABLED", "1")
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_LLM_BROKER_MODELS", "allowed/model")
    monkeypatch.setenv("UA_LLM_BROKER_DAILY_USD", "1")
    monkeypatch.setenv("UA_LLM_BROKER_RESERVE_USD", "0.1")
    llm_broker._requests.clear()
    events = []
    reads = []
    requests = []

    def load(name, consumer, *, org_id=None):
        reads.append((name, consumer, org_id))
        return "provider-secret"

    def record(channel, payload, **kwargs):
        events.append((channel, payload, {k: v for k, v in kwargs.items() if k != "precondition"}))
        return "01KTESTBROKER0000000000000"

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, json, headers):
            assert method == "POST"
            requests.append((url, json, headers))

            class Stream:
                async def __aenter__(self):
                    if json["stream"]:
                        self.response = httpx.Response(
                            200,
                            content=(
                                b": private diagnostic\n\n"
                                b'event: debug\ndata: {"object":"chat.completion.chunk","choices":[],"secret":"hidden"}\n\n'
                                b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"ok","private":"hidden","reasoning_details":[{"type":"reasoning.text","text":"step","signature":"sig","private":"hidden"}]},"private":"hidden"}],"private":"hidden"}\n\n'
                                b'data: {"object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"cost":0.001}}\n\n'
                                b"data: [DONE]\n\n"
                            ),
                        )
                    else:
                        self.response = httpx.Response(
                            200,
                            json={
                                "id": "chatcmpl-1",
                                "choices": [
                                    {
                                        "message": {
                                            "role": "assistant",
                                            "content": "ok",
                                            "private": "hidden",
                                            "reasoning_details": [
                                                {
                                                    "type": "reasoning.encrypted",
                                                    "data": "opaque-state",
                                                    "id": "r1",
                                                    "format": "anthropic-claude-v1",
                                                    "index": 0,
                                                    "private": "hidden",
                                                }
                                            ],
                                            "tool_calls": [
                                                {
                                                    "id": "call-1",
                                                    "type": "function",
                                                    "function": {
                                                        "name": "demo",
                                                        "arguments": "{}",
                                                        "private": "hidden",
                                                    },
                                                }
                                            ],
                                        },
                                        "private": "hidden",
                                    }
                                ],
                                "private": {"provider_credential": "hidden"},
                                "usage": {
                                    "prompt_tokens": 3,
                                    "completion_tokens": 2,
                                    "cost": 0.001,
                                    "private": "hidden",
                                },
                            },
                        )
                    return self.response

                async def __aexit__(self, *_args):
                    return None

            return Stream()

    monkeypatch.setattr(llm_broker.managed_secrets, "_load_for_consumer", load)
    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", record)
    monkeypatch.setattr(llm_broker.httpx, "AsyncClient", Client)

    class Identity:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            scope["ua_identity"] = "Desktop_Agent2"
            scope["ua_identity_via"] = (
                scope["headers"] and dict(scope["headers"]).get(b"x-test-auth", b"oauth").decode()
            )
            scope["ua_device"] = {
                "node": dict(scope["headers"]).get(b"x-test-node", b"desktop.ts.net").decode(),
                "node_id": dict(scope["headers"]).get(b"x-test-node-id", b"n123").decode(),
            }
            token = config.auth_metadata_var.set(
                {"device_node": {"node": "desktop.ts.net", "node_id": "n123"}}
            )
            try:
                await self.app(scope, receive, send)
            finally:
                config.auth_metadata_var.reset(token)

    app = Identity(
        Starlette(
            routes=[
                Route("/api/llm/v1/chat/completions", llm_broker.chat_completions, methods=["POST"])
            ]
        )
    )
    return TestClient(app), events, reads, requests


def _body(**overrides):
    return {"model": "allowed/model", "messages": [{"role": "user", "content": "hi"}], **overrides}


def test_completion_uses_hub_secret_and_attributes_usage(broker):
    client, events, reads, requests = broker
    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    details = resp.json()["choices"][0]["message"]["reasoning_details"]
    assert details == [
        {
            "type": "reasoning.encrypted",
            "data": "opaque-state",
            "id": "r1",
            "format": "anthropic-claude-v1",
            "index": 0,
        }
    ]
    assert "hidden" not in resp.text
    assert reads == [("llm:openrouter", "llm", None)]
    assert requests[0][2]["Authorization"] == "Bearer provider-secret"
    assert requests[0][1]["max_tokens"] == 512
    assert events[0][0] == "hub.llm.broker_attempt"
    assert events[1][1]["agent"] == "Desktop_Agent2"
    assert events[1][1]["device_node"] == "desktop.ts.net"
    assert events[1][1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "cost_usd": 0.001}
    assert "provider-secret" not in json.dumps(events)


def test_reasoning_continuation_round_trips_without_provider_metadata(broker):
    client, _events, _reads, requests = broker
    first = client.post("/api/llm/v1/chat/completions", json=_body())
    assert first.status_code == 200
    assistant = first.json()["choices"][0]["message"]
    assert assistant["reasoning_details"][0]["data"] == "opaque-state"
    assert "private" not in json.dumps(assistant)
    second = client.post(
        "/api/llm/v1/chat/completions",
        json=_body(
            messages=[
                {"role": "user", "content": "hi"},
                assistant,
                {"role": "user", "content": "continue"},
            ]
        ),
    )
    assert second.status_code == 200
    assert requests[1][1]["messages"][1]["reasoning_details"] == assistant["reasoning_details"]


def test_projected_refusal_can_be_reused_on_next_turn(broker):
    client, _events, _reads, requests = broker
    projected = llm_broker._safe_completion(
        {"choices": [{"message": {"role": "assistant", "content": None, "refusal": "No."}}]}
    )
    refusal = projected["choices"][0]["message"]
    response = client.post(
        "/api/llm/v1/chat/completions",
        json=_body(
            messages=[
                {"role": "user", "content": "first"},
                refusal,
                {"role": "user", "content": "next"},
            ]
        ),
    )
    assert response.status_code == 200
    assert requests[0][1]["messages"][1] == refusal


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"x-test-auth": "bearer"}, _body(), 401),
        ({"x-test-node": "other.ts.net"}, _body(), 401),
        ({"x-test-node-id": "other-id"}, _body(), 401),
        ({}, _body(model="unapproved/model"), 403),
        ({}, _body(stream="yes"), 400),
        ({}, _body(provider={"allow_fallbacks": True}), 422),
        ({}, _body(max_tokens=5000), 400),
        (
            {},
            _body(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "https://example.com"}}
                        ],
                    }
                ]
            ),
            400,
        ),
        ({}, _body(messages=[{"role": "user", "content": "hi", "private": "metadata"}]), 400),
        (
            {},
            _body(
                messages=[
                    {
                        "role": "user",
                        "content": "hi",
                        "tool_calls": [{"function": {"arguments": "{}"}}],
                    }
                ]
            ),
            400,
        ),
    ],
)
def test_refusals_do_not_load_provider_key(broker, headers, body, status):
    client, events, reads, requests = broker
    resp = client.post("/api/llm/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == status
    assert not reads and not requests and not events


def test_disabled_route_does_not_load_key(broker, monkeypatch):
    client, _events, reads, requests = broker
    monkeypatch.setenv("UA_LLM_BROKER_ENABLED", "0")
    assert client.post("/api/llm/v1/chat/completions", json=_body()).status_code == 503
    assert not reads and not requests


def test_streaming_replays_chunks_and_records_usage_before_done(broker):
    client, events, _reads, requests = broker
    resp = client.post("/api/llm/v1/chat/completions", json=_body(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"content":"ok"' in resp.text
    assert (
        '"reasoning_details":[{"type":"reasoning.text","text":"step","signature":"sig"}]'
        in resp.text
    )
    assert "hidden" not in resp.text and "private diagnostic" not in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")
    assert requests[0][1]["stream_options"] == {"include_usage": True}
    assert events[-1][1]["usage"]["cost_usd"] == 0.001


def test_streaming_audit_gap_does_not_emit_done(broker, monkeypatch):
    client, _events, _reads, _requests = broker
    calls = 0

    def record(_channel, payload, **_kwargs):
        nonlocal calls
        del payload
        calls += 1
        if calls == 2:
            raise OSError("audit unavailable")
        return "01KTESTBROKER0000000000000"

    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", record)
    resp = client.post("/api/llm/v1/chat/completions", json=_body(stream=True))
    assert resp.status_code == 200
    assert "broker audit is unavailable" in resp.text
    assert "data: [DONE]" not in resp.text


def test_tenant_key_is_read_only_from_its_own_org(broker, monkeypatch):
    client, _events, reads, _requests = broker
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_test")
    assert client.post("/api/llm/v1/chat/completions", json=_body()).status_code == 200
    assert reads == [("llm:openrouter", "llm", "org_test")]


def test_direct_openai_model_uses_distinct_hub_key_and_fixed_endpoint(broker, monkeypatch):
    client, events, reads, requests = broker
    monkeypatch.setenv("UA_LLM_BROKER_MODELS", "openai-direct/gpt-test")
    resp = client.post("/api/llm/v1/chat/completions", json=_body(model="openai-direct/gpt-test"))
    assert resp.status_code == 200
    assert reads == [("llm:openai", "llm", None)]
    assert requests[0][0] == "https://api.openai.com/v1/chat/completions"
    assert requests[0][1]["model"] == "gpt-test"
    assert events[0][1]["provider"] == events[1][1]["provider"] == "openai"


def test_body_limit_applies_to_streamed_request(broker):
    client, _events, reads, requests = broker

    def chunks():
        yield b"{"
        yield b" " * (256 * 1024)
        yield b"}"

    resp = client.post("/api/llm/v1/chat/completions", content=chunks())
    assert resp.status_code == 413
    assert not reads and not requests


def test_rate_limit_applies_before_provider_spend(broker):
    client, events, reads, requests = broker
    for _ in range(10):
        assert client.post("/api/llm/v1/chat/completions", json=_body()).status_code == 200
    assert client.post("/api/llm/v1/chat/completions", json=_body()).status_code == 429
    assert len(reads) == len(requests) == 10
    assert len(events) == 20


def test_route_reservation_and_audit_share_real_transaction(
    real_event_writer, broker, monkeypatch, tmp_path
):
    client, _events, _reads, _requests = broker
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", real_event_writer)
    response = client.post("/api/llm/v1/chat/completions", json=_body())
    assert response.status_code == 200
    conn = storage.connect()
    try:
        spend = conn.execute(
            "SELECT reserve_usd, actual_usd, outcome FROM llm_broker_spend"
        ).fetchone()
        audit = conn.execute(
            "SELECT channel FROM event_log WHERE channel LIKE 'hub.llm.broker_%' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert (spend["reserve_usd"], spend["actual_usd"], spend["outcome"]) == (
        0.1,
        0.001,
        "completed",
    )
    assert [row["channel"] for row in audit] == [
        "hub.llm.broker_attempt",
        "hub.llm.broker_result",
    ]


def test_audit_failure_refuses_provider_spend(broker, monkeypatch):
    client, _events, _reads, requests = broker

    def unavailable(*_args, **_kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", unavailable)
    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 503
    assert requests == []


def test_post_spend_audit_gap_is_visible_to_caller(broker, monkeypatch):
    client, _events, _reads, requests = broker
    calls = 0

    def record(_channel, payload, **_kwargs):
        nonlocal calls
        del payload
        calls += 1
        if calls == 2:
            raise OSError("audit unavailable")
        return "01KTESTBROKER0000000000000"

    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", record)
    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 200
    assert resp.headers["x-ua-broker-audit"] == "result-unavailable"
    assert len(requests) == 1


def test_real_hub_middleware_static_bearer_cannot_spend(monkeypatch):
    monkeypatch.setenv("UA_LLM_BROKER_ENABLED", "1")
    monkeypatch.setenv("UA_LLM_BROKER_MODELS", "allowed/model")
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = HubAuthASGIMiddleware(
        Starlette(
            routes=[
                Route("/api/llm/v1/chat/completions", llm_broker.chat_completions, methods=["POST"])
            ]
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/llm/v1/chat/completions",
            headers={"Authorization": "Bearer static-token"},
            json=_body(),
        )
    assert resp.status_code == 401
    assert "device-bound OAuth" in resp.json()["error"]["message"]


# --- hub-observed usage ledger (task #1526) ----------------------------------


def test_completed_call_records_one_hub_observed_row(real_event_writer, broker, monkeypatch):
    client, _events, _reads, _requests = broker
    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", real_event_writer)
    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 200
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT actor, provider, model, input_tokens, output_tokens, cost_usd FROM usage_events"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "Desktop_Agent2"
    assert row["provider"] == "openrouter"
    assert row["model"] == "allowed/model"
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (3, 2, 0.001)


def test_provider_error_records_no_usage_row(broker, monkeypatch):
    """An unsuccessful outcome is never recorded as observed usage."""

    class FailingStream:
        async def __aenter__(self):
            self.response = httpx.Response(500, json={"error": "boom"})
            return self.response

        async def __aexit__(self, *_args):
            return None

    class FailingClient:
        def __init__(self, **kwargs):
            del kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FailingStream()

    monkeypatch.setattr(llm_broker.httpx, "AsyncClient", FailingClient)
    client, _events, _reads, _requests = broker
    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 502
    assert usage_ledger.provider_call_count() == 0


def test_record_result_dedupes_a_retried_completed_outcome():
    """Two _record_result calls for the same reservation must write one row.

    This is the no-double-counting guarantee task #1526 asks for: a retried
    audit write (the same attempt, e.g. after a transient failure elsewhere)
    must not double the usage ledger even though the broker knows nothing
    about how many times its own caller invoked it.
    """
    import asyncio

    attempt = ("req-dedupe-1", "evt-1", "openrouter")
    usage = {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.02}

    async def _twice():
        await llm_broker._record_result(
            attempt, "Desktop_Agent1", "node.ts.net", "some/model", "completed", usage=usage
        )
        await llm_broker._record_result(
            attempt, "Desktop_Agent1", "node.ts.net", "some/model", "completed", usage=usage
        )

    asyncio.run(_twice())
    assert usage_ledger.provider_call_count() == 1


def test_run_id_is_snapshotted_before_the_task_finishes_mid_call(
    real_event_writer, broker, monkeypatch
):
    """Task #1526 follow-up (Devin review of PR #875): the run must be
    resolved before the provider call, not after — a task that finishes while
    the request is in flight must not null out the already-in-flight call's
    attribution."""
    tid = task_add("in-flight task", lane="dispatch-ready")["id"]
    task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]

    client, _events, _reads, _requests = broker
    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", real_event_writer)

    real_resolve = usage_ledger.resolve_run_id

    def resolve_then_finish_the_task(actor):
        resolved = real_resolve(actor)
        # The task finishes — clearing tasks.current_run_id — strictly after
        # the broker snapshots it and strictly before the provider "answers".
        task_update(tid, status="done", actor="Desktop_Agent2")
        return resolved

    monkeypatch.setattr(llm_broker.usage_ledger, "resolve_run_id", resolve_then_finish_the_task)

    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    # Sanity: the cache really was cleared before the response was recorded.
    conn = storage.connect()
    try:
        cache_row = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()
        usage_rows = conn.execute("SELECT run_id FROM usage_events").fetchall()
    finally:
        conn.close()
    assert cache_row["current_run_id"] is None
    assert [r["run_id"] for r in usage_rows] == [run_id]


def test_run_id_snapshot_is_not_stolen_by_a_later_claim(real_event_writer, broker, monkeypatch):
    """A second claim by the same agent, landing after the snapshot was taken,
    must not change which run the in-flight call attributes to."""
    tid = task_add("first task", lane="dispatch-ready")["id"]
    task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    run_id = run_records.run_history(task_id=tid)["runs"][0]["run_id"]

    client, _events, _reads, _requests = broker
    monkeypatch.setattr(llm_broker.llm_spend.events, "record_event", real_event_writer)

    real_resolve = usage_ledger.resolve_run_id
    other_holder: list[int] = []

    def resolve_then_claim_another_task(actor):
        # This name is patched onto the shared usage_ledger module, so it must
        # not call usage_ledger.resolve_run_id itself — that would recurse
        # into this same function. Read the ambiguity directly instead.
        resolved = real_resolve(actor)
        other = task_add("second task", lane="dispatch-ready")["id"]
        task_claim(other, "Desktop_Agent2", expected_lane="dispatch-ready")
        other_holder.append(other)
        return resolved

    monkeypatch.setattr(llm_broker.usage_ledger, "resolve_run_id", resolve_then_claim_another_task)

    resp = client.post("/api/llm/v1/chat/completions", json=_body())
    assert resp.status_code == 200
    assert other_holder, "the side effect must have run"
    conn = storage.connect()
    try:
        usage_rows = conn.execute("SELECT run_id FROM usage_events").fetchall()
        run_agents = conn.execute(
            "SELECT DISTINCT current_run_id FROM tasks "
            "WHERE current_run_agent='Desktop_Agent2' AND current_run_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    # Sanity: the agent really does hold two open runs now, i.e. a fresh
    # lookup at this point would be ambiguous (resolve to None).
    assert len(run_agents) == 2
    # But the recorded row keeps the snapshot taken before that happened.
    assert [r["run_id"] for r in usage_rows] == [run_id]
