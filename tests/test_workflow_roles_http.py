"""HTTP forwards role fields to the common workflow tool without trusting body identity."""

import json

import pytest
from starlette.requests import Request

from unified_agent.webui import api, routes

pytestmark = pytest.mark.smoke


async def request(body):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/actions/workflow",
            "headers": [],
            "ua_identity": "builder",
        },
        receive,
    )


async def test_route_forwards_role_fields_and_server_identity(monkeypatch):
    monkeypatch.setattr(routes, "_guard_mutation", lambda req: None)
    calls = []

    async def run(**kw):
        calls.append(kw)
        return {"instance_id": "known"}

    monkeypatch.setattr(api, "run_workflow", run)
    response = await routes.action_workflow(
        await request(
            {
                "name": "roles",
                "author": "spoofed",
                "instance_id": "known",
                "closure": {"exit": "done"},
                "role_bindings": {"builder": "builder"},
            }
        )
    )
    assert response.status_code == 200
    assert calls == [
        {
            "name": "roles",
            "inputs": {},
            "author": "builder",
            "instance_id": "known",
            "closure": {"exit": "done"},
            "role_bindings": {"builder": "builder"},
        }
    ]


@pytest.mark.parametrize(
    "field,value", [("instance_id", 1), ("closure", []), ("role_bindings", "x")]
)
async def test_bad_role_payload_refused(monkeypatch, field, value):
    monkeypatch.setattr(routes, "_guard_mutation", lambda req: None)
    response = await routes.action_workflow(await request({"name": "roles", field: value}))
    assert response.status_code == 400


async def test_api_calls_common_tool(monkeypatch):
    calls = []

    async def call(tool, **args):
        calls.append((tool, args))
        return {"status": "waiting"}

    monkeypatch.setattr(api, "call_tool", call)
    await api.run_workflow("roles", author="builder", instance_id="known", closure={"exit": "done"})
    assert calls == [
        (
            "workflow_run",
            {
                "name": "roles",
                "inputs": {},
                "author": "builder",
                "instance_id": "known",
                "closure": {"exit": "done"},
            },
        )
    ]
