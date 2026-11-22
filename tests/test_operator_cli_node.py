"""``ua node``: acceptance and retire verbs over the hub's HTTP twins (task #2208).

The gateway is stubbed with an ``httpx.MockTransport`` the same way
``test_operator_cli.py`` does: MCP answers ``profile_status`` for the identity
check, and the ``/api/...`` routes answer the node verbs. The tests pin the
argument parsing, the exact request each verb sends, and how refusals map to
exit codes and the JSON envelope.
"""

from __future__ import annotations

import json

import httpx
import pytest

from unified_agent import device_login, storage
from unified_agent.cli import client
from unified_agent.cli.main import run

TOKEN = "dummy-private-bearer"
NODE = "old-host.local"


@pytest.fixture
def hub(monkeypatch):
    for name in (
        "UA_EXECUTOR_TOKEN",
        "UA_AGENT_TOKENS",
        "UA_EXECUTOR_AGENT",
        "UA_CLI_IDENTITY",
        "UA_CLI_PROJECT",
        "UA_CLI_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://hub.example.test/mcp")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr(device_login, "stored_access_token", lambda **kwargs: None)
    monkeypatch.setattr(storage, "connect", lambda: pytest.fail("client opened local database"))
    state: dict = {"api": [], "mcp_tools": [], "status": 200, "reply": {"ok": True}}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "DELETE":
            return httpx.Response(200)
        if request.url.path.startswith("/api/"):
            # The HTTP twin resolves the identity's own profile, never an MCP session's.
            assert "Mcp-Session-Id" not in request.headers
            state["api"].append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": dict(request.url.params),
                    "body": json.loads(request.content) if request.content else None,
                }
            )
            if state.get("transport_error"):
                raise httpx.ConnectError("down")
            return httpx.Response(state["status"], json=state["reply"])
        assert request.url.path == "/mcp"
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "fixture-transport"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["method"] == "tools/call", body["method"]
        name = body["params"]["name"]
        state["mcp_tools"].append(name)
        assert name == "profile_status", "node verbs must not call MCP tools"
        result = {"structuredContent": {"identity": "Desktop_Operator", "profile": "operator"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_client = httpx.Client
    monkeypatch.setattr(
        client.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return state


def _json(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


@pytest.mark.parametrize("verb", ["start", "evaluate"])
def test_acceptance_start_and_evaluate_post_the_acceptance_route(hub, capsys, verb):
    hub["status"] = 201 if verb == "start" else 200
    hub["reply"] = {"node": NODE, "state": "pending"}
    assert run(["node", "acceptance", verb, NODE, "--format", "json"]) == 0
    out, err = _json(capsys)
    assert err == ""
    assert out["command"] == f"node.acceptance.{verb}"
    assert out["outcome"] == "completed" and out["data"] == hub["reply"]
    assert hub["api"] == [
        {
            "method": "POST",
            "path": "/api/action/node-acceptance",
            "query": {},
            "body": {"action": verb, "node": NODE},
        }
    ]
    assert hub["mcp_tools"] == ["profile_status"]


def test_acceptance_attest_posts_run_id_and_evidence_ref(hub, capsys):
    hub["reply"] = {"node": NODE, "state": "accepted", "run_id": "acc-0123456789abcdef"}
    argv = [
        "node",
        "acceptance",
        "attest",
        NODE,
        "--run-id",
        "  acc-0123456789abcdef  ",
        "--evidence-ref",
        "  host-verified   output  and  cancel ",
        "--no-input",
        "--format",
        "json",
    ]
    assert run(argv) == 0
    out, err = _json(capsys)
    assert err == ""
    assert out["command"] == "node.acceptance.attest"
    assert out["outcome"] == "completed" and out["data"]["state"] == "accepted"
    assert hub["api"] == [
        {
            "method": "POST",
            "path": "/api/action/node-acceptance",
            "query": {},
            "body": {
                "action": "attest",
                "node": NODE,
                "run_id": "acc-0123456789abcdef",
                "evidence_ref": "host-verified output and cancel",
            },
        }
    ]
    assert hub["mcp_tools"] == ["profile_status"]


def test_acceptance_status_reads_one_node_or_every_summary(hub, capsys):
    hub["reply"] = {"node": NODE, "state": "accepted"}
    assert run(["node", "acceptance", "status", NODE, "--format", "json"]) == 0
    out, _ = _json(capsys)
    assert out["command"] == "node.acceptance.status" and out["data"]["state"] == "accepted"
    hub["reply"] = {"nodes": {NODE: {"state": "accepted"}}, "required": False}
    assert run(["node", "acceptance", "status", "--format", "json"]) == 0
    out, _ = _json(capsys)
    assert out["data"]["nodes"][NODE]["state"] == "accepted"
    assert [(c["method"], c["path"], c["query"]) for c in hub["api"]] == [
        ("GET", "/api/nodes/acceptance", {"node": NODE}),
        ("GET", "/api/nodes/acceptance", {}),
    ]


@pytest.mark.parametrize(
    "argv, path",
    [
        (["node", "acceptance", "status"], "/api/nodes/acceptance"),
        (["node", "list"], "/api/nodes"),
    ],
)
def test_an_in_band_hub_error_on_a_200_read_is_a_failure(hub, capsys, argv, path):
    # routes._node_acceptance and api.nodes_payload report a failed store read as
    # 200 with an empty listing and an "error"; that must not exit 0.
    hub["reply"] = {"nodes": {}, "error": "acceptance query failed: database is locked"}
    if path == "/api/nodes":
        hub["reply"] = {"nodes": [], "recent_items": [], "error": "nodes query failed: locked"}
    assert run([*argv, "--format", "json"]) == 1
    out, _ = _json(capsys)
    assert out["outcome"] == "failed" and out["data"] is None
    assert out["error"]["code"] == "hub_error"
    assert "query failed" in out["error"]["message"]
    assert [c["path"] for c in hub["api"]] == [path]


def test_node_list_shows_live_rows_and_retired_markers(hub, capsys):
    hub["reply"] = {
        "nodes": [
            {
                "node": "desk-01.local",
                "agent": "Desktop_Agent2",
                "device_node": None,
                "version": "1.2.3",
                "last_heartbeat": "2026-10-22T08:00:00+00:00",
                "acceptance": {"state": "accepted"},
                "backends": ["codex"],
                "worktree_roots": ["/w"],
            }
        ],
        "recent_items": [{"id": "nd-1", "prompt_preview": "private prompt"}],
        "retired_nodes": [
            {
                "node": NODE,
                "agent": "Desktop_Agent2",
                "last_heartbeat": None,
                "retired_at": "2026-10-21T08:00:00+00:00",
                "retired_by": "Desktop_Operator",
                "retired_reason": "renamed device",
            }
        ],
        "acceptance_required": True,
    }
    assert run(["node", "list", "--format", "json"]) == 0
    out, _ = _json(capsys)
    assert out["command"] == "node.list"
    assert out["data"] == {
        "nodes": [
            {
                "node": "desk-01.local",
                "agent": "Desktop_Agent2",
                "device_node": None,
                "version": "1.2.3",
                "last_heartbeat": "2026-10-22T08:00:00+00:00",
                "acceptance": {"state": "accepted"},
            }
        ],
        "retired_nodes": hub["reply"]["retired_nodes"],
        "truncated": 0,
        "acceptance_required": True,
    }
    assert "private prompt" not in json.dumps(out)
    assert hub["api"] == [{"method": "GET", "path": "/api/nodes", "query": {}, "body": None}]


def _retired(count: int) -> list[dict]:
    return [
        {
            "node": f"old-{i:04d}.local",
            "agent": "Desktop_Agent2",
            "last_heartbeat": None,
            "retired_at": "2026-10-21T08:00:00+00:00",
            "retired_by": "Desktop_Operator",
            "retired_reason": "renamed device",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("output", ["json", "text"])
def test_node_list_caps_retired_rows_and_counts_the_rest(hub, capsys, output):
    """Retired rows only accumulate; `ua node list` shows the first 100 and a count (#2210)."""
    from unified_agent.cli import nodes

    hub["reply"] = {"nodes": [], "retired_nodes": _retired(250), "acceptance_required": False}
    argv = ["node", "list", "--format", "json"] if output == "json" else ["node", "list"]
    assert run(argv) == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)["data"] if output == "json" else json.loads(captured)
    assert nodes.MAX_RETIRED_ROWS == 100
    assert data["retired_nodes"] == hub["reply"]["retired_nodes"][:100]
    assert data["truncated"] == 150


def test_node_list_at_the_retired_cap_truncates_nothing(hub, capsys):
    hub["reply"] = {"nodes": [], "retired_nodes": _retired(100)}
    assert run(["node", "list", "--format", "json"]) == 0
    data, _ = _json(capsys)
    assert len(data["data"]["retired_nodes"]) == 100
    assert data["data"]["truncated"] == 0


def test_a_lost_retire_points_at_node_list(hub, capsys):
    hub["transport_error"] = True
    assert run(["node", "retire", NODE, "--reason", "renamed", "--format", "json"]) == 6
    out, _ = _json(capsys)
    assert "ua node list" in out["error"]["message"]


@pytest.mark.parametrize("verb", ["retire", "unretire"])
def test_retire_and_unretire_post_the_retire_route(hub, capsys, verb):
    hub["reply"] = {"node": NODE, "retired": verb == "retire", "changed": True}
    argv = ["node", verb, NODE, "--reason", "  renamed   device ", "--no-input", "--format", "json"]
    assert run(argv) == 0
    out, _ = _json(capsys)
    assert out["command"] == f"node.{verb}" and out["data"]["changed"] is True
    assert hub["api"] == [
        {
            "method": "POST",
            "path": "/api/action/node-retire",
            "query": {},
            "body": {"action": verb, "node": NODE, "reason": "renamed device"},
        }
    ]


def test_text_output_renders_the_hub_answer(hub, capsys):
    hub["reply"] = {"node": NODE, "retired": True, "changed": True}
    assert run(["node", "retire", NODE, "--reason", "renamed"]) == 0
    assert json.loads(capsys.readouterr().out) == hub["reply"]


@pytest.mark.parametrize(
    "argv",
    [
        ["node", "retire", NODE],  # --reason is required
        ["node", "retire", "--reason", "x"],  # node is required
        ["node", "acceptance", "start"],  # node is required
        ["node", "acceptance", "attest", NODE],  # --run-id and --evidence-ref are required
        ["node", "acceptance", "attest", NODE, "--run-id", "acc-1"],  # --evidence-ref is required
        ["node", "acceptance", "attest", NODE, "--evidence-ref", "ok"],  # --run-id is required
        ["node", "acceptance", "bogus", NODE],  # not a ua verb
        ["node", "acceptance"],
        ["node"],
    ],
)
def test_bad_arguments_are_usage_errors_without_a_hub_call(hub, capsys, argv):
    assert run([*argv, "--format", "json"]) == 2
    out, _ = _json(capsys)
    assert out["error"]["code"] == "usage"
    assert hub["api"] == [] and hub["mcp_tools"] == []


@pytest.mark.parametrize(
    "argv, code",
    [
        (["node", "retire", NODE, "--reason", "   "], "validation"),
        (["node", "retire", NODE, "--reason", "x" * 501], "validation"),
        (["node", "retire", "bad name", "--reason", "x"], "validation"),
        (
            ["node", "acceptance", "attest", NODE, "--run-id", "   ", "--evidence-ref", "ok"],
            "validation",
        ),
        (
            ["node", "acceptance", "attest", NODE, "--run-id", "bad id", "--evidence-ref", "ok"],
            "validation",
        ),
        (
            ["node", "acceptance", "attest", NODE, "--run-id", "acc-1", "--evidence-ref", "   "],
            "validation",
        ),
        (
            [
                "node",
                "acceptance",
                "attest",
                NODE,
                "--run-id",
                "acc-1",
                "--evidence-ref",
                "x" * 1001,
            ],
            "validation",
        ),
    ],
)
def test_local_validation_refuses_before_sending(hub, capsys, argv, code):
    assert run([*argv, "--format", "json"]) == 2
    out, _ = _json(capsys)
    assert out["error"]["code"] == code
    assert hub["api"] == []


@pytest.mark.parametrize(
    "status, code, exit_code, outcome",
    [
        (401, "authentication", 3, "failed"),
        (403, "permission_denied", 4, "failed"),
        (404, "unavailable", 5, "failed"),
        (409, "conflict", 8, "conflict"),
        (400, "validation", 2, "failed"),
        (500, "hub_rejected", 1, "failed"),
    ],
)
def test_refusals_map_to_exit_codes_with_the_hub_reason(
    hub, capsys, status, code, exit_code, outcome
):
    hub["status"] = status
    hub["reply"] = {"error": "node 'old-host.local' has queued or claimed work (nd-1)"}
    assert run(["node", "retire", NODE, "--reason", "renamed", "--format", "json"]) == exit_code
    out, err = _json(capsys)
    assert out["outcome"] == outcome
    assert out["error"]["code"] == code
    if status not in (401,):
        assert "queued or claimed work" in out["error"]["message"]
    assert TOKEN not in json.dumps(out) and TOKEN not in err


def test_a_lost_write_says_the_outcome_is_unknown(hub, capsys):
    hub["transport_error"] = True
    assert run(["node", "retire", NODE, "--reason", "renamed", "--format", "json"]) == 6
    out, _ = _json(capsys)
    assert out["error"]["code"] == "transport"
    assert "outcome is unknown" in out["error"]["message"]


@pytest.mark.parametrize(
    "reply",
    [
        # routes._approval_pending, the shape every node route parks with.
        {"status": "pending", "approval_id": 7, "code": "approval_required"},
        # The older spelling, still accepted.
        {"status": "approval_pending", "approval_id": 7},
    ],
)
def test_an_approval_pending_answer_is_approval_required(hub, capsys, reply):
    hub["status"] = 202
    hub["reply"] = reply
    assert run(["node", "acceptance", "start", NODE, "--format", "json"]) == 9
    out, _ = _json(capsys)
    assert out["outcome"] == "approval_required"
    assert out["error"]["code"] == "approval_required"
    assert out["error"]["approval_id"] == 7
    assert "#7" in out["error"]["message"]


def test_a_parked_retire_through_the_real_route_exits_approval_required(
    tmp_path, monkeypatch, capsys
):
    """End to end: ua -> the real /api/action/node-retire behind the real auth
    middleware, with a policy rule that parks coordination_manage for approval."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import node_queue, server, webui

    operator = "Desktop_Operator"
    for name in (
        "UA_EXECUTOR_TOKEN",
        "UA_EXECUTOR_AGENT",
        "UA_CLI_IDENTITY",
        "UA_CLI_PROJECT",
        "UA_CLI_SESSION",
        "UA_LIFECYCLE_OPERATORS",
        "UA_OPERATOR_ALIASES",
    ):
        monkeypatch.delenv(name, raising=False)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "version: 1\nenabled: true\ndefault_action: allow\nrules:\n"
        "  - name: park_node_actions\n    type: tool\n    tool: coordination_manage\n"
        "    action: require_approval\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "coordination_manage")
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{operator}:{TOKEN}")
    monkeypatch.setenv("UA_OPERATOR_LABEL", operator)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_HTTP_HUB_URL", "https://hub.example.test/mcp")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr(device_login, "stored_access_token", lambda **kwargs: None)
    storage.init_db()
    node_queue.register(NODE, identity="Desktop_Agent2", facts={"backends": ["codex"]})
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))

    with TestClient(app, base_url="http://127.0.0.1") as hub_app:

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(200)
            if request.url.path.startswith("/api/"):
                real = hub_app.request(
                    request.method,
                    request.url.path,
                    content=request.content,
                    headers={
                        "Authorization": request.headers["Authorization"],
                        "Content-Type": "application/json",
                    },
                )
                return httpx.Response(real.status_code, content=real.content)
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "fixture-transport"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            result = {"structuredContent": {"identity": operator, "profile": "operator"}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        real_client = httpx.Client
        monkeypatch.setattr(
            client.httpx,
            "Client",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
        )
        argv = ["node", "retire", NODE, "--reason", "renamed", "--format", "json"]
        assert run(argv) == 9
    out, _ = _json(capsys)
    assert out["outcome"] == "approval_required"
    approval_id = out["error"]["approval_id"]
    assert type(approval_id) is int and approval_id > 0
    conn = storage.connect()
    try:
        row = conn.execute("SELECT retired_at FROM nodes WHERE node=?", (NODE,)).fetchone()
        pending = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    assert row["retired_at"] is None
    assert pending["status"] == "pending"


def test_needs_prints_the_operator_list_from_the_attention_route(hub, capsys):
    rows = [{"kind": "wake_grant", "subject": "s1", "title": "t", "act": {"pane": "sessions"}}]
    hub["reply"] = {"needs_operator": {"rows": rows, "counts": {}, "total": 1, "errors": []}}
    assert run(["needs", "--format", "json"]) == 0
    out, _ = _json(capsys)
    assert out["data"]["rows"] == rows
    assert [(c["method"], c["path"]) for c in hub["api"]] == [("GET", "/api/attention")]
