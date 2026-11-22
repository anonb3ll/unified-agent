"""Task #2056: Control Center write routes return the typed error code.

After #2054 a refusal raised as a ``TypedToolError`` names a machine code
(``conflict``, ``approval_required``, ``permission_denied``). The MCP adapter
puts it in ``structuredContent``; these tests pin the HTTP twin: the shared
``_write_error`` / ``_json_body_error`` helpers emit ``{"error", "code"}``, a
``conflict`` is a 409 whatever the route's default, a parked approval keeps its
202 shape plus the code, and an untyped error keeps its status with no code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import claim_release, config, server, storage, webui
from unified_agent.tool_errors import ConflictError, PermissionDeniedError
from unified_agent.tools.approvals import ApprovalPendingError
from unified_agent.tools.tasks import task_add
from unified_agent.webui import routes

AGENT = "Desktop_Agent2"
TOKENS = {AGENT: "fixture-agent-token"}
CONFLICT_TEXT = "comment 4 changed since revision 2"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    for name in (
        "UA_AGENT_ACCESS_ENABLED",
        "UA_AGENT_ACCESS_READ_ONLY",
        "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        "UA_APPROVAL_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    yield tmp_path


def _client() -> TestClient:
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes())),
        base_url="http://127.0.0.1",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[AGENT]}


def _raising(monkeypatch, exc: BaseException) -> None:
    async def write(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(routes, "_governed_object_write", write)


def _post_comment(client: TestClient):
    return client.post("/api/tasks/7/comments", headers=_headers(), json={"body": "hello"})


def _post_goal(client: TestClient):
    return client.post("/api/goals", headers=_headers(), json={"action": "add", "title": "G"})


# --- the shared write-error mapping, through real routes -----------------------------


@pytest.mark.parametrize("post", [_post_comment, _post_goal])
def test_conflict_is_409_with_the_code(monkeypatch, post):
    _raising(monkeypatch, ConflictError(CONFLICT_TEXT))
    with _client() as client:
        response = post(client)
    assert response.status_code == 409
    assert response.json() == {"error": CONFLICT_TEXT, "code": "conflict"}


@pytest.mark.parametrize("post", [_post_comment, _post_goal])
def test_approval_keeps_the_202_shape_and_adds_the_code(monkeypatch, post):
    _raising(monkeypatch, ApprovalPendingError("queued", approval_id=914))
    with _client() as client:
        response = post(client)
    assert response.status_code == 202
    assert response.json() == {
        "approval_id": 914,
        "status": "pending",
        "code": "approval_required",
    }


@pytest.mark.parametrize("post", [_post_comment, _post_goal])
def test_untyped_errors_keep_their_status_and_carry_no_code(monkeypatch, post):
    _raising(monkeypatch, ToolError("body must not be empty"))
    with _client() as client:
        response = post(client)
    assert response.status_code == 400
    assert response.json() == {"error": "body must not be empty"}


def test_permission_denied_keeps_the_route_status_and_names_the_code(monkeypatch):
    _raising(monkeypatch, PermissionDeniedError("steward may not edit this goal"))
    with _client() as client:
        response = _post_goal(client)
    assert response.status_code == 400
    assert response.json() == {
        "error": "steward may not edit this goal",
        "code": "permission_denied",
    }


# --- the helpers themselves ----------------------------------------------------------


def _body(response) -> dict:
    import json

    return json.loads(response.body)


def test_json_body_error_reads_a_wrapped_typed_cause():
    """``Tool.run`` re-raises a tool's ToolError with the typed one as ``__cause__``."""
    try:
        try:
            raise ConflictError(CONFLICT_TEXT)
        except ConflictError as inner:
            raise ToolError(f"Error executing tool task_comment: {inner}") from inner
    except ToolError as wrapped:
        response = routes._json_body_error(wrapped, status=404)
    assert response.status_code == 409
    assert _body(response) == {
        "error": f"Error executing tool task_comment: {CONFLICT_TEXT}",
        "code": "conflict",
    }


def test_json_body_error_keeps_an_explicit_status_without_a_code():
    response = routes._json_body_error(ToolError("unknown goal 3"), status=404)
    assert response.status_code == 404
    assert _body(response) == {"error": "unknown goal 3"}


def test_json_body_error_keeps_the_message_cap():
    response = routes._json_body_error(ConflictError("x" * 500))
    assert response.status_code == 409
    assert _body(response) == {"error": "x" * 300, "code": "conflict"}


# --- claim release: its 409 now names the shared code --------------------------------


def test_claim_release_conflict_carries_the_code(monkeypatch):
    def refuse(**_kwargs):
        raise claim_release.ClaimReleaseConflict("task 7 has no claim to release")

    monkeypatch.setattr(claim_release, "task_claim_release", refuse)
    with _client() as client:
        response = client.post(
            "/api/tasks/7/claim-release",
            headers=_headers(),
            json={
                "expected_claimant": "Desktop_Agent1",
                "reason": "stale",
                "expected_run_id": "run-1",
            },
        )
    assert response.status_code == 409
    assert response.json() == {"error": "task 7 has no claim to release", "code": "conflict"}


# --- project_build: the claimed-spec refusal is a typed conflict at the MCP layer -----


def _claim(task_id: int, agent: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", (agent, task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_project_build_claimed_by_another_is_a_typed_conflict():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("claimed spec", notes="[project-spec]\nbuild it")
        _claim(spec["id"], "Desktop_Agent1")
        async with Client(server.mcp) as client:
            result = await client.session.call_tool("project_build", {"parent_task_id": spec["id"]})
        assert result.is_error
        assert result.structured_content == {"code": "conflict"}
        text = "".join(getattr(block, "text", "") or "" for block in result.content)
        assert f"task {spec['id']} is claimed by Desktop_Agent1, not Desktop_Agent4" in text
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.anyio
async def test_project_build_missing_marker_stays_untyped():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("no marker", notes="plain notes")
        async with Client(server.mcp) as client:
            result = await client.session.call_tool("project_build", {"parent_task_id": spec["id"]})
        assert result.is_error
        assert not result.structured_content
    finally:
        config.agent_identity_var.reset(token)


# --- review follow-up: routes that built their own error bodies ----------------------

STALE_TEXT = "task 7 changed since expected_updated_at"


def _post_task_update(client: TestClient):
    return client.post(
        "/api/action/task",
        headers=_headers(),
        json={"id": 7, "status": "doing", "expected_updated_at": "2026-10-14T00:00:00Z"},
    )


def test_task_update_conflict_is_409_with_the_code(monkeypatch):
    """The reviewer's repro: a stale revision on /api/action/task was a bare 400."""

    async def update_task(**_kwargs):
        raise ConflictError(STALE_TEXT)

    monkeypatch.setattr(routes.api, "update_task", update_task)
    with _client() as client:
        response = _post_task_update(client)
    assert response.status_code == 409
    assert response.json() == {"error": STALE_TEXT, "code": "conflict"}


def test_task_update_approval_keeps_tool_and_payload_and_adds_the_code(monkeypatch):
    async def update_task(**_kwargs):
        raise ApprovalPendingError("queued", approval_id=918)

    monkeypatch.setattr(routes.api, "update_task", update_task)
    with _client() as client:
        response = _post_task_update(client)
    assert response.status_code == 202
    assert response.json() == {
        "approval_id": 918,
        "status": "pending",
        "code": "approval_required",
        "tool": "task_update",
        "payload": {"id": 7, "status": "doing", "expected_updated_at": "2026-10-14T00:00:00Z"},
    }


def test_task_update_untyped_error_keeps_400_and_its_200_char_cap(monkeypatch):
    async def update_task(**_kwargs):
        raise ToolError("y" * 400)

    monkeypatch.setattr(routes.api, "update_task", update_task)
    with _client() as client:
        response = _post_task_update(client)
    assert response.status_code == 400
    assert response.json() == {"error": "y" * 200}


def test_task_create_conflict_and_approval_carry_the_code(monkeypatch):
    def conflict(**_kwargs):
        raise ConflictError("idempotency key reused with another title")

    monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", conflict)
    with _client() as client:
        response = client.post("/api/tasks", headers=_headers(), json={"title": "T"})
    assert response.status_code == 409
    assert response.json() == {
        "error": "idempotency key reused with another title",
        "code": "conflict",
    }

    def pending(**_kwargs):
        raise ApprovalPendingError("queued", approval_id=5)

    monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", pending)
    with _client() as client:
        response = client.post("/api/tasks", headers=_headers(), json={"title": "T"})
    assert response.status_code == 202
    assert response.json()["code"] == "approval_required"
    assert response.json()["approval_id"] == 5
    assert response.json()["tool"] == "task_add"


def test_dependency_conflict_is_409_and_untyped_stays_400(monkeypatch):
    async def conflict(*_args):
        raise ConflictError("task 7 changed while adding the dependency")

    monkeypatch.setattr(routes.api, "add_dependency", conflict)
    body = {"operation": "add", "task_id": 7, "depends_on_task_id": 8}
    with _client() as client:
        response = client.post("/api/action/dependency", headers=_headers(), json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"

    async def plain(*_args):
        raise ToolError("dependency would form a cycle")

    monkeypatch.setattr(routes.api, "add_dependency", plain)
    with _client() as client:
        response = client.post("/api/action/dependency", headers=_headers(), json=body)
    assert response.status_code == 400
    assert response.json() == {"error": "dependency would form a cycle"}


def test_a_route_message_survives_with_the_code():
    """Routes that prefix their text (dispatch, takeover) keep it and gain the code."""
    exc = ConflictError("task 7 is already claimed")
    response = routes._json_body_error(
        exc, status=400, message=f"dispatch failed: {str(exc)[:200]}"
    )
    assert response.status_code == 409
    assert _body(response) == {
        "error": "dispatch failed: task 7 is already claimed",
        "code": "conflict",
    }
    uncapped = routes._json_body_error(ToolError("z" * 400), status=400, limit=None)
    assert _body(uncapped) == {"error": "z" * 400}


# --- review round 2: approval dicts built inside webui.api helpers -------------------


def _parks(*_args, **_kwargs):
    raise ApprovalPendingError("queued", approval_id=918)


def _assert_parked(result: dict) -> None:
    assert result["pending"] is True
    assert result["approval_id"] == 918
    assert result["code"] == "approval_required"
    assert result["error"] == "queued"


@pytest.mark.anyio
async def test_config_revision_rollback_pending_carries_the_code(monkeypatch):
    from unified_agent import config_revisions

    monkeypatch.setattr(config_revisions, "rollback", _parks)
    result = await routes.api.config_revision_rollback_payload("policy", 3, AGENT)
    _assert_parked(result)
    assert result["status"] == 202


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "body"),
    [("run", {"op": "clear", "task_id": 7}), ("sprint", {"op": "add", "name": "S1"})],
)
async def test_operating_action_pending_carries_the_code(monkeypatch, kind, body):
    from unified_agent import operating_profiles

    monkeypatch.setattr(operating_profiles, "preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(operating_profiles, "manage", _parks)
    result = await routes.api.operating_action_payload(kind, body, AGENT)
    _assert_parked(result)
    assert result["status"] == 202


@pytest.mark.anyio
async def test_team_up_pending_carries_the_code(monkeypatch):
    from unified_agent import team_spec

    monkeypatch.setattr(routes.api, "_team_up_gates", lambda *_a, **_k: None)
    monkeypatch.setattr(team_spec, "resolve", lambda team: team)
    monkeypatch.setattr(team_spec, "boot", _parks)
    result = await routes.api.team_up_payload("office", None, AGENT)
    _assert_parked(result)
    assert result["tool"] == team_spec.TEAM_UP_TOOL


@pytest.mark.anyio
async def test_skills_teams_pending_carries_the_code(monkeypatch):
    from unified_agent import skill_catalog

    monkeypatch.setattr(skill_catalog, "call", _parks)
    result = await routes.api.skills_teams_payload(AGENT, "install", {})
    _assert_parked(result)
    assert result["status"] == 202


# --- tripwire: no approval body without its code -------------------------------------

_WEBUI = Path(__file__).resolve().parents[1] / "src/unified_agent/webui"


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant)}


def _reads_exc_approval_id(node: ast.AST) -> bool:
    """``exc.approval_id`` or ``getattr(exc, "approval_id", ...)`` on a caught error."""
    if isinstance(node, ast.Attribute) and node.attr == "approval_id":
        return isinstance(node.value, ast.Name) and node.value.id == "exc"
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
        args = node.args
        return (
            len(args) >= 2
            and isinstance(args[0], ast.Name)
            and args[0].id == "exc"
            and isinstance(args[1], ast.Constant)
            and args[1].value == "approval_id"
        )
    return False


@pytest.mark.parametrize(("module", "floor"), [("routes.py", 15), ("api.py", 4)])
def test_every_parked_approval_body_names_its_code(module, floor):
    """A dict built from a caught approval's id must carry ``code`` (task #2056).

    This covers every 202 parked-approval body in routes.py and the pending
    payloads webui.api helpers return for routes to forward. Out of scope by
    construction: ``/api/approvals/request``, whose body is the requested
    approval itself (built from the writer's result, not a caught refusal).
    ``floor`` keeps the scan from passing vacuously on a moved or emptied module.
    """
    path = _WEBUI / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bodies = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "approval_id"
            and _reads_exc_approval_id(v)
            for key, v in zip(node.keys, node.values, strict=True)
        )
    ]
    assert len(bodies) >= floor, f"{module}: the scan found only {len(bodies)} approval bodies"
    missing = [node.lineno for node in bodies if "code" not in _dict_keys(node)]
    assert missing == [], f"{module}: approval bodies without 'code' at lines {missing}"


def test_every_202_literal_with_an_approval_id_in_routes_names_its_code():
    """``_json({... "approval_id" ...}, status=202)`` in routes.py must carry ``code``."""
    tree = ast.parse((_WEBUI / "routes.py").read_text(encoding="utf-8"))
    seen = 0
    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_json"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        status = [kw.value for kw in node.keywords if kw.arg == "status"]
        if not (status and isinstance(status[0], ast.Constant) and status[0].value == 202):
            continue
        keys = _dict_keys(node.args[0])
        if "approval_id" in keys:
            seen += 1
            if "code" not in keys:
                missing.append(node.lineno)
    assert seen >= 15, "the scan no longer finds the parked-approval bodies"
    assert missing == [], f"routes.py: 202 approval bodies without 'code' at lines {missing}"


# --- review round 3: chokepoints (approval in a generic handler, returned pauses) ----


def test_a_parked_delegate_dispatch_is_the_pending_202(monkeypatch):
    """``delegate`` is baseline-gated; its refusal used to land in a generic 400."""
    from types import SimpleNamespace

    async def dispatch_prompt(*_args):
        raise ApprovalPendingError("'delegate' requires approval: request 77", approval_id=77)

    monkeypatch.setattr(routes, "parse_dispatch_request", lambda body: SimpleNamespace(agent="x"))
    monkeypatch.setattr(routes.api, "dispatch_prompt", dispatch_prompt)
    with _client() as client:
        response = client.post("/api/action/dispatch", headers=_headers(), json={})
    assert response.status_code == 202
    assert response.json() == {"approval_id": 77, "status": "pending", "code": "approval_required"}


def test_json_body_error_answers_a_parked_approval_as_the_pending_202():
    exc = ApprovalPendingError("queued", approval_id=12)
    for response in (
        routes._json_body_error(exc, status=400),
        routes._json_body_error(exc, status=403, message="dispatch refused: queued"),
    ):
        assert response.status_code == 202
        assert _body(response) == {
            "approval_id": 12,
            "status": "pending",
            "code": "approval_required",
        }
    try:
        try:
            raise exc
        except ApprovalPendingError as inner:
            raise ToolError(f"Error executing tool delegate: {inner}") from inner
    except ToolError as wrapped:
        response = routes._json_body_error(wrapped)
    assert response.status_code == 202
    assert _body(response)["approval_id"] == 12


_PLAN = {"plan_sha256": "a" * 64, "ready": True}


def _team_up(client: TestClient):
    return client.post(
        "/api/action/team-up", headers=_headers(), json={"team": "office", "plan_sha256": "a" * 64}
    )


@pytest.mark.parametrize(
    "review",
    [
        {"mission_id": 3, "pending_id": 41, "reason": "mission review"},
        {
            "mission_id": 3,
            "pending_id": 41,
            "reason": "mission review",
            "global_approval_id": 918,
            "global_approval_status": "pending",
        },
    ],
    ids=["autonomy-only", "globally-gated"],
)
def test_a_returned_team_up_pause_is_202_with_the_code(monkeypatch, review):
    """team_spec.boot returns (not raises) its autonomy-review pause."""
    from unified_agent import team_spec

    paused = {
        "booted": False,
        "autonomy_review": review,
        "reason": "mission review required: a human reviews it, then boot again",
        "plan": _PLAN,
    }
    monkeypatch.setattr(routes.api, "_team_up_gates", lambda *_a, **_k: None)
    monkeypatch.setattr(team_spec, "resolve", lambda team: team)
    monkeypatch.setattr(team_spec, "boot", lambda *_a, **_k: paused)
    with _client() as client:
        response = _team_up(client)
    assert response.status_code == 202
    assert response.json() == {**paused, "pending": True, "code": "approval_required"}


def test_a_typed_conflict_from_the_team_up_gates_is_409(monkeypatch):
    from unified_agent import agent_access

    real = agent_access.enforce

    def enforce(tool, *args, **kwargs):
        if tool == "coordination_manage":  # the first of _team_up_gates' checks
            raise ConflictError("team office changed while booting")
        return real(tool, *args, **kwargs)

    monkeypatch.setattr(agent_access, "enforce", enforce)
    with _client() as client:
        response = _team_up(client)
    assert response.status_code == 409
    assert response.json() == {"error": "team office changed while booting", "code": "conflict"}


def test_an_untyped_team_up_refusal_stays_403(monkeypatch):
    from unified_agent import agent_access

    real = agent_access.enforce

    def enforce(tool, *args, **kwargs):
        if tool == "coordination_manage":
            raise ToolError("team_up is denied by policy " + "p" * 400)
        return real(tool, *args, **kwargs)

    monkeypatch.setattr(agent_access, "enforce", enforce)
    with _client() as client:
        response = _team_up(client)
    assert response.status_code == 403
    assert response.json() == {"error": ("team_up is denied by policy " + "p" * 400)[:300]}


def test_the_json_guard_leaves_accepted_work_and_other_statuses_alone():
    """A dispatch whose agent has not started answers ``status: pending`` as work."""
    accepted = routes._json({"status": "pending", "task_id": 9}, status=202)
    assert _body(accepted) == {"status": "pending", "task_id": 9}
    decided = routes._json({"approval_id": 4, "status": "approved"}, status=200)
    assert _body(decided) == {"approval_id": 4, "status": "approved"}
    named = routes._json({"approval_id": 4, "code": "other"}, status=202)
    assert _body(named) == {"approval_id": 4, "code": "other"}
    parked = routes._json({"pending": True, "approval_id": None}, status=202)
    assert _body(parked)["code"] == "approval_required"


def test_only_the_explicit_approval_request_opts_out_of_the_code():
    """``approval_code=False`` is reserved for /api/approvals/request's result body."""
    source = (_WEBUI / "routes.py").read_text(encoding="utf-8")
    assert source.count("approval_code=False)") == 1
    tree = ast.parse(source)
    owner = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        and "approval_code=False)" in (ast.get_source_segment(source, fn) or "")
    ]
    assert owner == ["action_approval_request"]


# --- Devin round 2: GET reads behind a gate, and the structural tripwire -------------


def test_a_parked_team_plan_read_is_the_pending_202(monkeypatch):
    """``_team_up_gates`` re-raises a parked approval; the plan GET answered 400."""

    def gates(*_args, **_kwargs):
        raise ApprovalPendingError("team_up requires approval: request 55", approval_id=55)

    monkeypatch.setattr(routes.api, "_team_up_gates", gates)
    with _client() as client:
        response = client.get("/api/teams/plan?team=office", headers=_headers())
    assert response.status_code == 202
    assert response.json() == {"approval_id": 55, "status": "pending", "code": "approval_required"}


def test_team_plan_typed_and_untyped_tool_errors(monkeypatch):
    from unified_agent import team_spec

    monkeypatch.setattr(routes.api, "_team_up_gates", lambda *_a, **_k: None)

    def stale(_team):
        raise ConflictError("team office changed while planning")

    monkeypatch.setattr(team_spec, "resolve", stale)
    with _client() as client:
        response = client.get("/api/teams/plan?team=office", headers=_headers())
    assert response.status_code == 409
    assert response.json() == {"error": "team office changed while planning", "code": "conflict"}

    def unknown(_team):
        raise ToolError("unknown team 'office'")

    monkeypatch.setattr(team_spec, "resolve", unknown)
    with _client() as client:
        response = client.get("/api/teams/plan?team=office", headers=_headers())
    assert response.status_code == 400
    assert response.json() == {"error": "unknown team 'office'"}


def test_a_parked_skills_teams_read_is_202_with_the_code(monkeypatch):
    from unified_agent import skill_catalog

    monkeypatch.setattr(routes, "_skills_teams_authority", lambda *_a, **_k: None)
    monkeypatch.setattr(skill_catalog, "call", _parks)
    with _client() as client:
        response = client.get("/api/skills-teams", headers=_headers())
    assert response.status_code == 202
    body = response.json()
    assert body["pending"] is True
    assert body["approval_id"] == 918
    assert body["code"] == "approval_required"


def test_a_skills_teams_read_that_is_not_parked_stays_200(monkeypatch):
    from unified_agent import skill_catalog

    monkeypatch.setattr(routes, "_skills_teams_authority", lambda *_a, **_k: None)
    monkeypatch.setattr(skill_catalog, "call", lambda *_a, **_k: {"skills": [], "teams": []})
    with _client() as client:
        response = client.get("/api/skills-teams", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"skills": [], "teams": []}


#: Handlers allowed to build ``_json`` from a caught refusal themselves. Keep each
#: entry justified; a new one needs review.
_RAW_ERROR_ALLOWLIST = {
    # The in-progress handoff answer adds ``retry`` and a Retry-After header,
    # which the shared helper cannot express; it is matched on an untyped
    # message and is a 409 already.
    ("messages_handoff", '"retry": True'),
}


def _resolve_exception(node: ast.AST):
    import builtins
    import importlib

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    obj = getattr(routes, parts[0], None) or getattr(builtins, parts[0], None)
    if obj is None:
        try:
            obj = importlib.import_module(f"unified_agent.{parts[0]}")
        except ImportError:
            return None
    for part in parts[1:]:
        obj = getattr(obj, part, None)
    return obj if isinstance(obj, type) else None


def test_no_refusal_handler_builds_its_own_error_body():
    """Structural tripwire (task #2056): typed refusals go through the shared helpers.

    Any ``except`` in routes.py that catches ``Exception``, ``BaseException``,
    ``ToolError`` or a ``TypedToolError`` subclass (``ApprovalPendingError``,
    ``ConflictError``, ...) must answer through ``_json_body_error`` or
    ``_write_error``, never ``_json(...)`` built from the exception: that is how
    a conflict lost its 409 and a parked approval its 202. Exempt: a 202 body
    that already names ``code`` (a parked approval with route-specific fields,
    checked by the approval tripwires above) and ``_RAW_ERROR_ALLOWLIST``.
    """
    from unified_agent.tool_errors import TypedToolError

    source = (_WEBUI / "routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owners = {
        child: fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for child in ast.walk(fn)
        if isinstance(child, ast.ExceptHandler)
    }

    def watched(cls) -> bool:
        return cls in (Exception, BaseException, ToolError) or (
            cls is not None and issubclass(cls, TypedToolError)
        )

    handlers = helpers = 0
    offenders = []
    for handler, owner in owners.items():
        if not handler.name or handler.type is None:
            continue
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if not any(watched(_resolve_exception(t)) for t in types):
            continue
        handlers += 1
        body = ast.Module(body=handler.body, type_ignores=[])
        derived = {handler.name}
        for node in ast.walk(body):
            if isinstance(node, ast.Assign) and any(
                isinstance(n, ast.Name) and n.id in derived for n in ast.walk(node.value)
            ):
                derived |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        for call in ast.walk(body):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None)
            if name in {"_json_body_error", "_write_error"}:
                helpers += 1
                continue
            if name != "_json" or not any(
                isinstance(n, ast.Name) and n.id in derived for a in call.args for n in ast.walk(a)
            ):
                continue
            first = call.args[0] if call.args else None
            status = [kw.value for kw in call.keywords if kw.arg == "status"]
            if (
                isinstance(first, ast.Dict)
                and "code" in _dict_keys(first)
                and status
                and isinstance(status[0], ast.Constant)
                and status[0].value == 202
            ):
                continue
            text = ast.get_source_segment(source, call) or ""
            if any(owner == fn and marker in text for fn, marker in _RAW_ERROR_ALLOWLIST):
                continue
            offenders.append(f"{owner}:{call.lineno}")
    assert handlers >= 60, f"the scan found only {handlers} refusal handlers"
    assert helpers >= 60, f"the scan found only {helpers} shared-helper answers"
    assert offenders == [], f"handlers building their own error body: {offenders}"


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "kind,status,code",
    [
        (ConflictError, 409, "conflict"),
        (PermissionDeniedError, 403, "permission_denied"),
    ],
)
def test_node_acceptance_preserves_typed_refusal_status_and_code(
    monkeypatch, wrapped, kind, status, code
):
    from unified_agent.tools import graph

    error = kind("acceptance action refused")
    if wrapped:
        outer = ToolError("coordination action refused: acceptance action refused")
        outer.__cause__ = error
        error = outer

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(graph, "coordination_manage", refuse)
    with _client() as client:
        response = client.post(
            "/api/action/node-acceptance",
            headers=_headers(),
            json={"action": "start", "node": "test-node"},
        )
    assert response.status_code == status
    assert response.json() == {"error": str(error), "code": code}


@pytest.mark.parametrize("status", [403, 409])
def test_node_acceptance_preserves_wrapped_node_error_status(monkeypatch, status):
    from unified_agent import node_queue
    from unified_agent.tools import graph

    def refuse(*_args, **_kwargs):
        try:
            raise node_queue.NodeError("device acceptance unavailable", status=status)
        except node_queue.NodeError as error:
            raise ToolError("node action refused") from error

    monkeypatch.setattr(graph, "coordination_manage", refuse)
    with _client() as client:
        response = client.post(
            "/api/action/node-acceptance",
            headers=_headers(),
            json={"action": "start", "node": "test-node"},
        )
    assert response.status_code == status
    assert response.json() == {"error": "device acceptance unavailable"}
