import json
import sys

import httpx
import pytest

from unified_agent import config, platform_cli, storage


@pytest.fixture
def hub_http(monkeypatch):
    monkeypatch.setattr(config, "executor_agent", lambda: "Desktop_Agent2")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://hub.example.test/mcp")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:test-token")
    monkeypatch.delenv("UA_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv("UA_AGENT_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(storage, "connect", lambda: pytest.fail("local DB opened"))
    state = {"response": httpx.Response(200, json={"ok": True}), "calls": []}

    def handle(request):
        state["calls"].append(request)
        if "error" in state:
            raise state["error"]
        return state["response"]

    client = httpx.Client
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        platform_cli.httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs)
    )
    return state


@pytest.mark.parametrize(
    "args,path",
    [
        (["manifest"], "/api/platform/manifest"),
        (["get", "board"], "/api/platform/objects/board"),
        (["get", "task", "123"], "/api/platform/objects/task/123"),
        (["get", "task", "a/b c"], "/api/platform/objects/task/a%2Fb%20c"),
    ],
)
def test_platform_reads_use_real_hub_auth_and_fixed_paths(hub_http, capsys, args, path):
    assert platform_cli.run_platform_cli(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    request = hub_http["calls"][0]
    assert request.method == "GET"
    assert str(request.url) == f"https://hub.example.test{path}"
    assert request.headers["Authorization"] == "Bearer test-token"


def test_projection_post_uses_hub_auth_and_bounded_json(hub_http):
    from unified_agent.executor.daemon import HubClient

    client = HubClient(agent_label="Desktop_Agent2", allow_fallback=False)
    manifest = {"schema_version": 1, "items": [{"task_id": 307}]}
    assert platform_cli._post_json(client, "/api/tasks/next-work-projection", manifest) == {
        "ok": True
    }
    request = hub_http["calls"][0]
    assert request.method == "POST"
    assert str(request.url) == "https://hub.example.test/api/tasks/next-work-projection"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(request.content) == manifest


def test_projection_post_rejects_oversized_manifest_before_http(hub_http):
    from unified_agent.executor.daemon import HubClient

    client = HubClient(agent_label="Desktop_Agent2", allow_fallback=False)
    with pytest.raises(platform_cli.PlatformCliError, match="1 MiB request limit"):
        platform_cli._post_json(
            client, "/api/tasks/next-work-projection", {"title": "x" * (1024 * 1024)}
        )
    assert hub_http["calls"] == []


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 500])
def test_platform_statuses_refuse_redirects_and_do_not_print_body(hub_http, capsys, status):
    hub_http["response"] = httpx.Response(
        status,
        headers={"Location": "https://other.example.test/capture"},
        json={"secret": "must not print"},
    )
    assert platform_cli.run_platform_cli(["get", "task", "1"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert f"HTTP {status}" in output.err
    assert "must not print" not in output.err
    assert len(hub_http["calls"]) == 1


@pytest.mark.parametrize("missing", ["UA_HTTP_HUB_URL", "UA_AGENT_TOKENS"])
def test_platform_requires_primary_hub_and_token_without_fallback(
    hub_http, monkeypatch, capsys, missing
):
    monkeypatch.delenv(missing)
    assert platform_cli.run_platform_cli(["manifest"]) == 2
    assert "platform read failed" in capsys.readouterr().err
    assert hub_http["calls"] == []


@pytest.mark.parametrize("hub_url", [None, "", "   "])
@pytest.mark.parametrize(
    "agent_label", [None, "", "Desktop_Agent2", "Laptop_Agent2", "Custom_Agent"]
)
@pytest.mark.parametrize("token_source", ["executor", "bearer", "agent_tokens"])
def test_platform_requires_explicit_hub_before_client_or_http(
    monkeypatch, capsys, hub_url, agent_label, token_source
):
    for name in (
        "UA_HTTP_HUB_URL",
        "UA_EXECUTOR_AGENT",
        "UA_EXECUTOR_TOKEN",
        "UA_AGENT_BEARER_TOKEN",
        "UA_AGENT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    if hub_url is not None:
        monkeypatch.setenv("UA_HTTP_HUB_URL", hub_url)
    if agent_label is not None:
        monkeypatch.setenv("UA_EXECUTOR_AGENT", agent_label)
    if token_source == "executor":
        monkeypatch.setenv("UA_EXECUTOR_TOKEN", "test-token")
    elif token_source == "bearer":
        monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "test-token")
    else:
        monkeypatch.setenv("UA_AGENT_TOKENS", "test-token:Custom_Agent")
    monkeypatch.setattr(storage, "connect", lambda: pytest.fail("local DB opened"))
    monkeypatch.setattr(
        platform_cli.httpx, "Client", lambda **kwargs: pytest.fail("HTTP attempted")
    )

    assert platform_cli.run_platform_cli(["manifest"]) == 2
    assert "UA_HTTP_HUB_URL is required" in capsys.readouterr().err


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.InvalidURL])
def test_transport_error_does_not_expose_exception_details(hub_http, capsys, error_type):
    hub_http["error"] = error_type("private error detail test-token")
    assert platform_cli.run_platform_cli(["manifest"]) == 2
    error = capsys.readouterr().err
    assert error_type.__name__ in error
    assert "private error" not in error and "test-token" not in error


@pytest.mark.parametrize(
    "url",
    [
        "https://hub.example.test/not-mcp",
        "https://user@hub.example.test/mcp",
        "https://hub.example.test/mcp?token=private",
        "https://hub.example.test/mcp#fragment",
        "https://hub.example.test/mcp\nprivate",
        "https://[broken/mcp",
        "https://hub.example.test:invalid/mcp",
    ],
)
def test_malformed_hub_urls_fail_before_http(hub_http, monkeypatch, capsys, url):
    monkeypatch.setenv("UA_HTTP_HUB_URL", url)
    assert platform_cli.run_platform_cli(["manifest"]) == 2
    error = capsys.readouterr().err
    assert "platform read failed" in error
    assert "password" not in error and "private" not in error
    assert hub_http["calls"] == []


@pytest.mark.parametrize("bad", ["", ".", "..", "task\n"])
def test_platform_rejects_unsafe_segments(hub_http, capsys, bad):
    assert platform_cli.run_platform_cli(["get", bad, "1"]) == 2
    assert "platform read failed" in capsys.readouterr().err
    assert hub_http["calls"] == []


@pytest.mark.parametrize(
    "body,error",
    [(b"not-json", "invalid JSON"), (b"[]", "non-object"), (b"x" * (2 * 1024 * 1024 + 1), "2 MiB")],
)
def test_platform_rejects_invalid_or_oversized_payloads(hub_http, capsys, body, error):
    hub_http["response"] = httpx.Response(200, content=body)
    assert platform_cli.run_platform_cli(["manifest"]) == 2
    assert error in capsys.readouterr().err


def test_server_platform_dispatch_does_not_initialize_local_db(monkeypatch):
    from unified_agent import server

    monkeypatch.setattr(sys, "argv", ["unified-agent", "platform", "manifest"])
    monkeypatch.setattr(server.storage, "init_db", lambda: pytest.fail("local DB initialized"))
    monkeypatch.setattr(platform_cli, "run_platform_cli", lambda args: 7)
    with pytest.raises(SystemExit, match="7"):
        server.main()


@pytest.mark.parametrize(
    "args,path",
    [
        (
            ["provenance-graph", "--task-id", "123"],
            "/api/platform/provenance-graph?task_id=123",
        ),
        (
            ["provenance-graph", "--query", "by_human", "--human", "Desktop_Person"],
            "/api/platform/provenance-graph?query=by_human&human=Desktop_Person",
        ),
        (
            ["provenance-graph", "--query", "by_release", "--task-ids", "1,2,3"],
            "/api/platform/provenance-graph?query=by_release&task_ids=1%2C2%2C3",
        ),
        (["provenance-graph"], "/api/platform/provenance-graph"),
    ],
)
def test_platform_provenance_graph_builds_the_expected_query_string(hub_http, capsys, args, path):
    assert platform_cli.run_platform_cli(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    request = hub_http["calls"][0]
    assert request.method == "GET"
    assert str(request.url) == f"https://hub.example.test{path}"
    assert request.headers["Authorization"] == "Bearer test-token"
