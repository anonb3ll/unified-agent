"""Durable proof consumption must not block unrelated HTTP requests."""

import asyncio
import json
import secrets
import sqlite3
import threading
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from unified_agent import config, device_key, harness_config
from unified_agent.webui import routes


@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_liveness_while_proof_waits_for_sqlite_writer(
    isolated_env, tmp_path, monkeypatch, method
):
    # The middleware's verified identity is fixture input; guards and proof
    # verification remain real, including the context propagated to the worker.
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HOSTED", "1")
    key = device_key.load_or_create(tmp_path / "node-token")
    token = secrets.token_urlsafe(32)
    metadata = {
        "auth_method": "device_code",
        "device_node": device_key.binding_from_form(device_key.public_jwk_json(key)),
    }
    downstream = []

    def payload(node, *, identity, device_node):
        assert identity == "Desktop_Agent1"
        assert config.current_auth_metadata() == metadata
        downstream.append(node)
        return {"node": node}

    monkeypatch.setattr(harness_config, "node_config_payload", payload)

    async def post(request):
        body, identity, error = await routes._node_request(request, {"node"})
        if error is not None:
            return error
        return JSONResponse(payload(body["node"], identity=identity, device_node=None))

    app = Starlette(
        routes=[
            Route("/healthz", routes.healthz),
            Route("/api/node/config", routes.node_config, methods=["GET"]),
            Route("/api/node/proof-test", post, methods=["POST"]),
        ]
    )

    async def authenticated(scope, receive, send):
        scope["ua_identity"] = "Desktop_Agent1"
        scope["ua_identity_via"] = "oauth"
        context_token = config.auth_metadata_var.set(metadata)
        try:
            await app(scope, receive, send)
        finally:
            config.auth_metadata_var.reset(context_token)

    path = "/api/node/config?node=desk" if method == "GET" else "/api/node/proof-test"
    body = b"" if method == "GET" else b'{"node":"desk"}'
    proof = device_key.sign_proof(key, method=method, path=path, access_token=token, body=body)
    headers = {"Authorization": f"Bearer {token}", device_key.PROOF_HEADER: proof}
    started = threading.Event()
    original_verify = device_key.verify_proof

    def verify(*args, **kwargs):
        started.set()
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(device_key, "verify_proof", verify)
    writer = sqlite3.connect(config.db_path(), check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    release = threading.Event()
    emergency = threading.Event()

    def unlock():
        if not release.wait(1.5):
            emergency.set()
        writer.rollback()

    unlocker = threading.Thread(target=unlock)
    unlocker.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=authenticated), base_url="http://testserver"
        ) as client:
            pending = asyncio.create_task(
                client.request(method, path, headers=headers, content=body)
            )
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()
                before = time.monotonic()
                healthy = await client.get("/healthz")
                assert healthy.status_code == 200
                assert time.monotonic() - before < 0.5
                assert not emergency.is_set()
                assert not pending.done()
                assert downstream == []
            finally:
                release.set()
            accepted = await pending
            assert accepted.status_code == 200, accepted.text
            replay = await client.request(method, path, headers=headers, content=body)
            assert replay.status_code == 403
            assert downstream == ["desk"]
            assert "error" in json.loads(replay.content)
    finally:
        release.set()
        unlocker.join(timeout=2)
        writer.close()
